"""这个进程是哪一版代码、要的是哪一版库结构；库里还有活着的、版本不同的进程时不许加入（不支持新旧版本混跑）。

**为什么要拦。** 升级时只停了一部分进程、或者新旧两批容器同时连着一个库：新版的进程启动时补列、补索引，旧版的进程不认识新的列和
规矩（认领、租约、接手的说法），两边各按各的写同一批行。以前只在 README 里写「升级要先停掉全部进程」，现在代码里拦住。

- 代码版本 code_version：包版本 + 构建号（AGENTLAB_BUILD，Docker 镜像构建时写进去）或 git 提交 + 源码指纹（app/ 下全部 .py
  的内容哈希）。比的是源码指纹：同一份代码一定相同，改了一行就不同，取不到 git、没有构建号时也区分得开。
- 库结构版本 schema_version：模型里各表各列（名字、类型、可空）加迁移清单的哈希。

怎么拦：PostgreSQL 时，补列之前先看一眼（preflight）——还有活着的、版本不同的进程就不启动；它的心跳停着（多半是强杀之后留下的、
租约还没到期）就等它过期再起，心跳还在走就照实报错：哪几个进程、什么版本、要先停掉再升级。登记那一下再在事务级咨询锁里查一遍
（cluster/workers.register），两个不同版本同时启动也只有先登记上的那一个起得来。SQLite 只有一个进程，不用看。
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import subprocess
from functools import lru_cache
from pathlib import Path
from typing import Any

from app.cluster import identity

logger = logging.getLogger(__name__)

_APP_DIR = Path(__file__).resolve().parents[1]


class VersionMismatch(RuntimeError):
    """库里还有活着的、版本不同的进程。"""


@lru_cache(maxsize=1)
def source_fingerprint() -> str:
    digest = hashlib.sha256()
    for path in sorted(_APP_DIR.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        digest.update(str(path.relative_to(_APP_DIR)).encode())
        try:
            digest.update(path.read_bytes())
        except OSError:
            continue
    return digest.hexdigest()[:12]


def _package() -> str:
    try:
        from importlib.metadata import version

        return version("agentlab-backend")
    except Exception:  # noqa: BLE001
        return "0"


def _build() -> str:
    if build := (os.environ.get("AGENTLAB_BUILD") or "").strip():
        return build[:40]
    try:
        out = subprocess.run(["git", "rev-parse", "--short=10", "HEAD"], cwd=str(_APP_DIR), capture_output=True,
                             text=True, timeout=3)
        return out.stdout.strip() if out.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


@lru_cache(maxsize=1)
def code_version() -> str:
    """包版本+构建号或 git 提交+源码指纹，例如 0.1.0+e6a3ee8f12+3f9a…（最后一段是比较用的）。"""
    return "+".join(p for p in (_package(), _build(), source_fingerprint()) if p)


def fingerprint_of(version: str | None) -> str:
    return (version or "").rsplit("+", 1)[-1]


@lru_cache(maxsize=1)
def schema_version() -> str:
    from app.db import models  # noqa: F401  模型都登记进 metadata
    from app.db.base import _COLUMN_MIGRATIONS, _INDEX_MIGRATIONS, Base

    digest = hashlib.sha256()
    for name in sorted(Base.metadata.tables):
        table = Base.metadata.tables[name]
        digest.update(name.encode())
        for col in table.columns:
            digest.update(f"{col.name}:{col.type!r}:{col.nullable}".encode())
    digest.update(repr(_COLUMN_MIGRATIONS).encode() + repr(_INDEX_MIGRATIONS).encode())
    return digest.hexdigest()[:12]


def same(code: str | None, schema: str | None) -> bool:
    return fingerprint_of(code) == fingerprint_of(code_version()) and (schema or "") == schema_version()


async def _peers(conn: Any) -> list[dict[str, Any]] | None:
    """库里登记着的别的进程（id、版本、租约、心跳）；表还不在（新库）交回 None。在一条连接上查（preflight 在建表之前）。"""
    from sqlalchemy import inspect, text

    def has(sync_conn: Any) -> tuple[bool, set[str]]:
        insp = inspect(sync_conn)
        if not insp.has_table("cluster_workers"):
            return False, set()
        return True, {c["name"] for c in insp.get_columns("cluster_workers")}

    exists, cols = await conn.run_sync(has)
    if not exists:
        return None
    extra = ", ".join(c for c in ("code_version", "schema_version") if c in cols)
    rows = (await conn.execute(text(f"SELECT id, lease_until, heartbeat_at{', ' + extra if extra else ''} "
                                    "FROM cluster_workers WHERE id <> :me"), {"me": identity.owner()})).mappings().all()
    return [dict(r) for r in rows]


def _describe(peer: dict[str, Any]) -> str:
    code = peer.get("code_version") or "（升级前的版本，没有记版本）"
    schema = peer.get("schema_version") or "未知"
    return f"{identity.describe(peer['id'])}（代码 {code}，库结构 {schema}）"


async def preflight(*, observe_s: float | None = None) -> None:
    """PostgreSQL 时、建表补列之前：库里还有活着的、版本不同的进程就不启动（见模块说明）。observe_s：看它心跳还在不在走的时长
    （缺省两拍心跳）。"""
    from app.core.config import settings

    if settings.db_dialect != "postgresql":
        return
    from app.cluster.workers import heartbeat_s, lease_s
    from app.db.base import utcnow

    observe = 2 * heartbeat_s() + 1.0 if observe_s is None else observe_s
    loop = asyncio.get_running_loop()
    started = loop.time()
    first_beats: dict[str, Any] = {}
    warned = False
    while True:
        mismatched = await _mismatched_alive(utcnow())
        if not mismatched:
            return
        for peer in mismatched:
            first_beats.setdefault(peer["id"], peer.get("heartbeat_at"))
        # 心跳还在走：真活着，照实报错。心跳停着的（强杀、断电留下的登记）等它们的租约过期
        beating = [p for p in mismatched if p.get("heartbeat_at") != first_beats.get(p["id"])]
        waited = loop.time() - started
        if beating or waited >= lease_s() * 2 + observe:
            raise VersionMismatch(_message(beating or mismatched))
        if waited >= observe and not warned:
            warned = True
            logger.warning("库里登记着 %d 个版本不同的进程，心跳停着（多半是强杀之后留下的）：等它们的租约过期再启动",
                           len(mismatched))
        await asyncio.sleep(min(0.5, max(0.1, heartbeat_s() / 2)))


async def _mismatched_alive(now: Any) -> list[dict[str, Any]]:
    from app.db.base import engine

    async with engine.connect() as conn:
        peers = await _peers(conn)
    if not peers:
        return []
    naive = now.replace(tzinfo=None)
    alive = [p for p in peers if p.get("lease_until") is not None and _naive(p["lease_until"]) > naive]
    return [p for p in alive if not same(p.get("code_version"), p.get("schema_version"))]


def _naive(moment: Any) -> Any:
    return moment.replace(tzinfo=None) if getattr(moment, "tzinfo", None) else moment


def _message(peers: list[dict[str, Any]]) -> str:
    listed = "；".join(_describe(p) for p in peers)
    return (f"库里还有 {len(peers)} 个版本不同的 AgentLab 进程活着：{listed}。本进程是代码 {code_version()}、库结构 "
            f"{schema_version()}。不支持新旧版本混跑：先停掉这些进程再起新版本（升级步骤见 README「多进程」的「升级」）")


def check_rows(rows: list[Any], now: Any) -> None:
    """登记那一下（cluster/workers.register，持着事务级咨询锁）再查一遍：别的活着的进程里有版本不同的就抛 VersionMismatch。"""
    naive = now.replace(tzinfo=None)
    bad = [{"id": r.id, "code_version": r.code_version, "schema_version": r.schema_version} for r in rows
           if r.lease_until is not None and _naive(r.lease_until) > naive and r.id != identity.owner()
           and not same(r.code_version, r.schema_version)]
    if bad:
        raise VersionMismatch(_message(bad))


__all__ = ["VersionMismatch", "check_rows", "code_version", "preflight", "same", "schema_version", "source_fingerprint"]
