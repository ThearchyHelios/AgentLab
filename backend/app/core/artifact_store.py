from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

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
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.rename(path)  # 原子落盘，写一半崩了不会留下半个工件

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
