"""模板函数 file_text：运行时把附件抽成文字交给 agent 或报告读（docs/design/normal-user-apps.md N9、§3.8）。

写法：{{ file_text(input.附件) }}，或者带上限 {{ file_text(input.附件, 8000) }}（默认 TEXT_DEFAULT 字，最多 TEXT_MAX）。

- 只读这次运行绑定的文件（提交入口、回复待办时绑上的，files/store.bound_files；节点执行前现读，见 prepare）：
  引用是输入里带来的，不能凭一个哈希读到别人的文件；
- 抽取沿用知识库上传的那一份（memory/parsing.extract，表格也转成文字）；抽出来的全文按内容哈希缓存（上传时就在后台
  抽一遍，运行时多半直接读缓存）；
- 超过上限照实截断，并在文字末尾写明全文多少字、读入了多少；抽不出（图片、旧版 Office、扫描件）照实写进返回的文字，
  不报错、不编；
- 每次抽取记一条运行事件 file.extracted（字数、是否截断），读入的那一段文字按工件存下（进证据）。

模板渲染是同步的：缓存没命中时就在这里抽取。文件大小有上传上限（max_upload_mb），上传时已经在后台抽过一遍。
"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

from app.core import atomic
from app.files.store import as_ref, bound_files, files_root, read_blob

logger = logging.getLogger(__name__)

#: 默认读入多少字、最多读入多少字
TEXT_DEFAULT = 20_000
TEXT_MAX = 200_000
#: 抽取文字工件的种类
TEXT_KIND = "file_text"
#: 存工件的后台任务：留着引用，不让它们半路被回收
_PENDING: set[asyncio.Task[Any]] = set()


def _cache_path(sha: str) -> Path:
    return files_root() / "_text" / sha[:2] / f"{sha}.json"


def drop_cache(sha: str) -> None:
    try:
        _cache_path(sha).unlink(missing_ok=True)
    except (OSError, ValueError):
        pass


def extract_text(sha: str, name: str, mime: str) -> tuple[str | None, str | None]:
    """(全文, 抽不出的原因)。按内容哈希缓存：同一份内容只抽一次。"""
    from app.memory.parsing import UnsupportedDocument, extract

    cache = _cache_path(sha)
    try:
        cached = json.loads(cache.read_text(encoding="utf-8"))
        if isinstance(cached, dict):
            return cached.get("text"), cached.get("problem")
    except (OSError, ValueError):
        pass
    raw = read_blob(sha)
    if raw is None:
        return None, "文件不在了或者内容被改动过"
    text: str | None = None
    problem: str | None = None
    if mime.startswith("image/"):
        problem = "这是图片，抽不出文字"
    else:
        try:
            text = extract(raw, name, mime, tables=True)
        except UnsupportedDocument as e:
            problem = str(e)
        except Exception as e:  # noqa: BLE001 - 解析库的各种异常照实写成抽不出
            logger.warning("附件 %s 抽取文字出错", name, exc_info=True)
            problem = f"读取出错：{type(e).__name__}"
        if text is not None and not text.strip():
            text, problem = None, "没有抽出文字（可能是扫描件）"
    try:
        # 临时文件名带随机后缀（core/atomic）：两个进程同时抽同一个附件，不会一个把另一个写到一半的改名过去
        atomic.write_text(cache, json.dumps({"text": text, "problem": problem}, ensure_ascii=False))
    except OSError:
        logger.warning("附件 %s 抽出的文字没能缓存", name, exc_info=True)
    return text, problem


async def warm(sha: str, name: str, mime: str) -> None:
    """上传之后在后台先抽一遍，运行时直接读缓存。出错不影响上传。"""
    try:
        await asyncio.to_thread(extract_text, sha, name, mime)
    except Exception:  # noqa: BLE001
        logger.warning("附件 %s 的文字预抽取出错", name, exc_info=True)


#: TemplateScope.memo 里放这次运行绑定的文件的键
_BOUND = "bound_files"


async def prepare(scope: Any) -> None:
    """节点配置里用到 file_text 时，执行前读一次这次运行绑定的文件（engine/expressions.TEMPLATE_PREPARERS）。"""
    run_id = getattr(getattr(scope.node, "run", None), "run_id", None)
    scope.memo[_BOUND] = await bound_files(run_id) if run_id else {}


def _limit(max_chars: Any) -> int:
    if isinstance(max_chars, (int, float)) and not isinstance(max_chars, bool) and max_chars > 0:
        return int(min(max_chars, TEXT_MAX))
    return TEXT_DEFAULT


def file_text(ref: Any, max_chars: Any = None) -> str:
    """模板函数本体（engine/expressions.TEMPLATE_FUNCTIONS）。返回给模型读的文字；读不到的照实说为什么。"""
    from app.engine.expressions import TEMPLATE_SCOPE

    parsed = as_ref(ref)
    if parsed is None:
        return "（没有附件）" if ref in (None, "") else "（附件的引用不对，读不到）"
    name = str(parsed.get("name") or "附件")
    scope = TEMPLATE_SCOPE.get()
    node = scope.node if scope is not None else None
    if scope is None or node is None or _BOUND not in scope.memo:
        return f"（附件「{name}」在运行时才读取）"
    meta = scope.memo[_BOUND].get(parsed["file_id"])
    if meta is None:
        return f"（附件「{name}」没有随这次运行提交，读不到）"
    limit = _limit(max_chars)
    memo_key = f"file_text:{meta['file_id']}:{limit}"
    if memo_key in scope.memo:
        return scope.memo[memo_key]
    text, problem = extract_text(meta["sha256"], meta["name"], meta["type"])
    total = len(text or "")
    if text is None:
        shown = f"（附件「{meta['name']}」抽不出文字：{problem}）"
        included = ""
    elif total > limit:
        included = text[:limit]
        shown = f"{included}\n……（附件「{meta['name']}」共 {total} 字，这里只读入了前 {limit} 字，其余没有读入）"
    else:
        included = shown = text
    scope.memo[memo_key] = shown
    _record(node, meta, included=included, total=total, truncated=text is not None and total > limit, problem=problem)
    return shown


def _record(node: Any, meta: dict[str, Any], *, included: str, total: int, truncated: bool,
            problem: str | None) -> None:
    """记一条 file.extracted（字数、是否截断），读入的那一段按工件存。工件 id 就是内容哈希，这里先算出来写进事件，
    落盘和登记交给后台任务（模板渲染是同步的）。"""
    from app.core.artifact_store import canonical_json, content_hash, put_json
    from app.core.events import EventType

    payload = {"file_id": meta["file_id"], "name": meta["name"], "sha256": meta["sha256"], "text": included,
               "total_chars": total, "truncated": truncated, **({"problem": problem} if problem else {})}
    artifact = content_hash(canonical_json(payload))
    run_id = getattr(node.run, "run_id", None)
    node_id = getattr(getattr(node, "node", None), "id", None)
    try:
        task = asyncio.get_running_loop().create_task(
            put_json(payload, kind=TEXT_KIND, run_id=run_id, node_id=node_id))
        _PENDING.add(task)
        task.add_done_callback(_PENDING.discard)
    except RuntimeError:  # 不在事件循环里（不该出现）：工件不存，事件照发
        artifact = None
    data = {"file_id": meta["file_id"], "name": meta["name"], "sha256": meta["sha256"], "chars": len(included),
            "total_chars": total, "truncated": truncated, "ok": problem is None, "artifact": artifact,
            **({"reason": problem} if problem else {})}
    try:
        node.emit(EventType.FILE_EXTRACTED, **data)
    except Exception:  # noqa: BLE001 - 事件只是留痕，读入的文字照样交回
        logger.warning("file.extracted 事件没发出去", exc_info=True)
