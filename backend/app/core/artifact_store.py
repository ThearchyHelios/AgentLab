from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from app.core import atomic
from app.core.config import settings

# --------------------------------------------------------------------------
# 内容寻址的工件库
#
# 事件流里的输出是给画布看的截断预览；这里存的是完整证据。
# 工件按内容 sha256 寻址：同一份内容只存一份，且任何篡改都会改变地址本身，
# 这比"相信数据库没被改过"强得多。出具溯源、周对比 diff、基线回归、
# 轨迹提模板，全部踩在这一层上。
# --------------------------------------------------------------------------


def canonical_json(obj: Any) -> str:
    """规范化 JSON：键排序、紧凑分隔符。同一数据永远得到同一字节串，哈希才稳定。"""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def content_hash(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode()
    return hashlib.sha256(data).hexdigest()


def graph_hash(graph: dict[str, Any]) -> str:
    """图定义的指纹。formal run 钉住的就是这个值。

    viewport 是纯视觉状态，挪一下画布不该改变图的身份，剔除后再哈希。
    """
    slim = {k: v for k, v in graph.items() if k != "viewport"}
    return content_hash(canonical_json(slim))


def _path_of(artifact_id: str) -> Path:
    # 按前两位分桶，避免单目录塞几万个文件
    return settings.data_dir / "artifacts" / artifact_id[:2] / f"{artifact_id}.json"


async def put_json(
    obj: Any,
    *,
    kind: str,
    run_id: str | None = None,
    node_id: str | None = None,
    meta: dict[str, Any] | None = None,
) -> str:
    """存一个 JSON 工件，返回其内容哈希（即工件 id）。

    幂等：同内容重复写只落一次盘；同一工件被多个 run 引用时，
    数据库里各记一行引用（溯源需要知道"谁产出的"），文件只有一份。
    """
    from app.db.base import SessionLocal
    from app.db.models import Artifact

    text = canonical_json(obj)
    artifact_id = content_hash(text)
    path = _path_of(artifact_id)
    if not path.exists():
        # 原子落盘（core/atomic）：写一半崩了不会留下半个工件；临时文件名带随机后缀，两个进程同时写同一份内容
        # 各写各的，不会一个把另一个写到一半的截断（以前共用 <id>.tmp，后到的会把先到的半成品改名成正式文件）
        atomic.write_text(path, text)

    async with SessionLocal() as session:
        existing = await session.get(Artifact, (artifact_id, run_id or ""))
        if existing is None:
            session.add(
                Artifact(
                    id=artifact_id,
                    run_id=run_id or "",
                    kind=kind,
                    node_id=node_id,
                    size=len(text.encode()),
                    meta=meta or {},
                )
            )
            await session.commit()
    return artifact_id


def load(artifact_id: str) -> Any | None:
    """按 id 取回工件内容。取回时重新校验哈希——工件库的承诺就是内容不可变。"""
    if not artifact_id or not artifact_id.isalnum():
        return None
    path = _path_of(artifact_id)
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8")
    if content_hash(text) != artifact_id:
        raise ValueError(f"工件 {artifact_id[:12]}… 内容与哈希不符，疑似被篡改")
    return json.loads(text)


def read_bytes(artifact_id: str, *, verify: bool = True) -> bytes | None:
    """按 id 取回工件文件的原始字节。文件本身就是 put_json 写下的规范 JSON，字节原样，不解析、不重排。

    可验证报告要把工件按原字节嵌进页面，让拿到文件的人自己重算哈希，所以不能像 load 那样解析了再交出去。
    默认同样复验哈希、对不上就抛 ValueError（和 load 一样的承诺）；verify=False 只给要把字节原样交给别人
    去核的调用方——被改过的工件照实交出去，核的那一方就会看到对不上。id 不对或者文件不在了返回 None。
    """
    if not artifact_id or not artifact_id.isalnum():
        return None
    path = _path_of(artifact_id)
    if not path.exists():
        return None
    data = path.read_bytes()
    if verify and content_hash(data) != artifact_id:
        raise ValueError(f"工件 {artifact_id[:12]}… 内容与哈希不符，疑似被篡改")
    return data


async def forget(ids: Any) -> list[str]:
    """回收工件文件：这几个 id 里，引用表（artifacts）里**一条引用都不剩**的，删掉文件；还有引用的一个不动。返回删掉了
    哪几个（docs/design/next-round/01-效果度量与回归评测.md 做法 16）。

    为什么按引用数、不按调用方说的：工件按内容寻址，同样的内容只存一份——评测的一次运行和用户的一次运行查到同一份结果、
    冻结了同一份表结构，文件就是同一个。调用方先删自己那几行引用（删运行时它名下的引用行），再把这些 id 交给这里；别的
    运行、目录快照、对比结果还引用着的文件照留。put_json 每写一个工件都记一行引用，所以「没有引用」就是没人要了。

    先查引用再删文件，两步之间同一份内容又被写一次（put_json 先写文件、后记引用）的话会删掉一个刚有人要的文件：单进程部署，
    回收在评测清理的后台任务里串行做，和写入撞上的窗口极小；真撞上了，读的一方照实报「工件不在」（load 返回 None），不会
    读到错的内容。"""
    from sqlalchemy import select

    from app.db.base import SessionLocal
    from app.db.models import Artifact

    wanted = sorted({i for i in ids or () if isinstance(i, str) and i and i.isalnum()})
    if not wanted:
        return []
    async with SessionLocal() as session:
        kept: set[str] = set()
        for start in range(0, len(wanted), 500):
            chunk = wanted[start:start + 500]
            kept |= set((await session.execute(select(Artifact.id).where(Artifact.id.in_(chunk)))).scalars())
    removed: list[str] = []
    for artifact_id in wanted:
        if artifact_id in kept:
            continue
        path = _path_of(artifact_id)
        try:
            path.unlink()
        except FileNotFoundError:
            continue
        removed.append(artifact_id)
    return removed


def manifest_hash(rows: list[tuple[int, str, str | None, dict[str, Any]]]) -> str:
    """把一次运行的全部事件压成一个清单哈希。

    rows: [(seq, type, node_id, data)]。存在 Run 上之后，事后任何对
    事件流的增删改都能被这一个值戳穿。
    """
    lines = [
        f"{seq}|{etype}|{node_id or ''}|{content_hash(canonical_json(data))}"
        for seq, etype, node_id, data in sorted(rows, key=lambda r: r[0])
    ]
    return content_hash("\n".join(lines))
