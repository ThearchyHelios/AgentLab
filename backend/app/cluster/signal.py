"""进程之间的叫醒：PostgreSQL 的 LISTEN / NOTIFY（频道 agentlab）。SQLite 只有一个进程，什么都不发、也不听——本进程里的
叫醒（事件总线、问数据的等待者、名额和同源队的等待者）各自照旧。

**只叫醒，不带数据。** 载荷是「类别|发出方|哪一个|到第几条」：运行事件 r|…|<run_id>|<seq>，问数据一轮的事件
t|…|<turn_id>|<seq>，发给某个进程的请求 q|…|<owner>，名额 / 同源队有了空位 slot、lane|…|<source_id>，进程走了 gone|…|<owner>，
缓存要重读 cache|…。发出方是 OWNER 的随机串：自己发的自己不处理（本进程里已经叫醒过了）。收到的一方按 id 回库里读——NOTIFY
丢了、连接断了重连期间漏了，靠各处的兜底轮询补上（运行的转发、问数据的流、名额等待都隔一会儿回库里看一次），叫醒只让它们不必
等到那一拍。

例外是不落库的那两样：运行里的流式增量（正文 token、思考 delta）和问数据助手的保活心跳，只在有别的进程在看的时候（它们发
w 登记「我在看」，cluster/relay.py）由执行的进程把内容放进载荷转过去（攒一小段合成一条、截在 7000 字节以内）。丢了无妨：
它们本来就不进回放，下一条落库的事件会把画面带到正确的样子。

**发在提交之后。** 调用方提交了事务再 publish：收到的一方去读时一定读得到。发是攒一下再批量发的（FLUSH_S），同一个 id 只发
最后一个 seq：一次运行几十条事件，跨进程的订阅方只要知道「到第几条了」。用一条专门的连接（不占连接池），收和发都走它；
断了隔一会儿重连，重连之后叫一遍各处的「补一次」（on_resync），漏掉的那一段由它们回库里读。
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from app.cluster import identity
from app.core.config import settings

logger = logging.getLogger(__name__)

CHANNEL = "agentlab"
#: 攒多久发一批（秒）
FLUSH_S = 0.02
#: 重连的间隔（秒）
RETRY_S = 2.0

Handler = Callable[[list[str]], Any]


def listen_dsn() -> str:
    """收发通知那条专用连接的地址：LISTEN 要一条一直开着的会话，设了直连地址（AGENTLAB_DATABASE_DIRECT_URL）就走它——普通地址
    可能是 PgBouncer 的事务模式，那里收不到通知。"""
    from app.db.urls import libpq_conninfo

    return libpq_conninfo(settings.session_db_url)


def _tag() -> str:
    return identity.owner().rsplit(":", 1)[-1]


class Signals:
    def __init__(self) -> None:
        self._handlers: dict[str, list[Handler]] = {}
        self._raw: set[str] = set()
        self._seq = 0
        self._resync: list[Callable[[], Any]] = []
        self._pending: dict[tuple[str, ...], str] = {}
        self._conn: Any = None
        self._task: asyncio.Task[None] | None = None
        self._flusher: asyncio.Task[None] | None = None
        self._wake: asyncio.Event | None = None
        self._stopping = False

    # ---------------- 登记 ----------------

    def on(self, kind: str, handler: Handler, *, raw: bool = False) -> None:
        """收到 kind 这一类时调 handler(其余几段)。可以是协程函数。同一个 handler 重复登记无害。raw：载荷里带着内容（JSON），
        只按第一段 id 切开，交回 [id, 余下的原文]。"""
        handlers = self._handlers.setdefault(kind, [])
        if handler not in handlers:
            handlers.append(handler)
        if raw:
            self._raw.add(kind)

    def on_resync(self, handler: Callable[[], Any]) -> None:
        """重连之后（可能漏了通知）调一次。"""
        if handler not in self._resync:
            self._resync.append(handler)

    # ---------------- 发 ----------------

    @property
    def active(self) -> bool:
        return self._task is not None and not self._task.done()

    def publish(self, kind: str, *parts: object, keep: bool = False) -> None:
        """攒一条要发的（没在听，也就是 SQLite、没起服务的测试里，什么都不做）。同一类同一个 id 只留最后一条；keep 时每条都发
        （带内容的转发：前后几段都要）。"""
        if not self.active:
            return
        text = [str(p) for p in parts]
        if keep:
            self._seq += 1
            key: tuple[str, ...] = (kind, "#", str(self._seq))
        else:
            key = (kind, text[0]) if text else (kind,)
        self._pending[key] = "|".join([kind, _tag(), *text])
        if self._wake is not None:
            self._wake.set()

    async def _flush_loop(self) -> None:
        assert self._wake is not None
        while not self._stopping:
            await self._wake.wait()
            self._wake.clear()
            await asyncio.sleep(FLUSH_S)
            batch, self._pending = list(self._pending.values()), {}
            if not batch or self._conn is None:
                continue
            try:
                await self._conn.execute(f"SELECT pg_notify('{CHANNEL}', p) FROM unnest($1::text[]) AS p", batch)
            except Exception:  # noqa: BLE001 - 发不出去只是别的进程晚一拍知道（它们会回库里看）
                logger.debug("NOTIFY 没发出去", exc_info=True)

    # ---------------- 收 ----------------

    def _received(self, _conn: Any, _pid: int, _channel: str, payload: str) -> None:
        head = payload.split("|", 2)
        if len(head) < 2 or head[1] == _tag():
            return
        kind, body = head[0], head[2] if len(head) > 2 else ""
        rest = (body.split("|", 1) if body else []) if kind in self._raw else (body.split("|") if body else [])
        for handler in list(self._handlers.get(kind, ())):
            try:
                out = handler(rest)
                if asyncio.iscoroutine(out):
                    task = asyncio.ensure_future(out)
                    task.add_done_callback(_quiet)
            except Exception:  # noqa: BLE001
                logger.exception("处理进程间通知 %s 出错", kind)

    async def _listen(self) -> None:
        import asyncpg

        from app.db.base import APPLICATION_NAME

        dsn = listen_dsn()
        first = True
        while not self._stopping:
            lost = asyncio.Event()
            try:
                conn = await asyncpg.connect(dsn, server_settings={"application_name": APPLICATION_NAME})
            except Exception as e:  # noqa: BLE001
                logger.warning("进程间通知的连接没连上（%s），%.0f 秒后重试", e, RETRY_S)
                await asyncio.sleep(RETRY_S)
                continue
            try:
                conn.add_termination_listener(lambda _c: lost.set())
                await conn.add_listener(CHANNEL, self._received)
                self._conn = conn
                if not first:
                    logger.info("进程间通知的连接已重连")
                    self._fire_resync()
                first = False
                await lost.wait()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.warning("进程间通知的连接出错，%.0f 秒后重连", RETRY_S, exc_info=True)
            finally:
                self._conn = None
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(conn.close(), timeout=2.0)
            if not self._stopping:
                await asyncio.sleep(RETRY_S)

    def _fire_resync(self) -> None:
        for handler in list(self._resync):
            try:
                out = handler()
                if asyncio.iscoroutine(out):
                    asyncio.ensure_future(out).add_done_callback(_quiet)
            except Exception:  # noqa: BLE001
                logger.exception("重连之后补一次出错")

    # ---------------- 起停 ----------------

    async def start(self) -> None:
        """应用库是 PostgreSQL 时开始收发；SQLite 什么都不做。"""
        if settings.db_dialect != "postgresql" or self.active:
            return
        self._stopping = False
        self._wake = asyncio.Event()
        self._task = asyncio.create_task(self._listen(), name="agentlab-cluster-listen")
        self._flusher = asyncio.create_task(self._flush_loop(), name="agentlab-cluster-notify")

    async def ready(self, timeout: float = 5.0) -> bool:
        """等连接连上（测试、启动时用）。"""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while self.active and self._conn is None and loop.time() < deadline:
            await asyncio.sleep(0.02)
        return self._conn is not None

    async def stop(self) -> None:
        self._stopping = True
        tasks = [t for t in (self._flusher, self._task) if t is not None]
        # 停之前把攒着的发出去：关停时交班、删登记这几条别的进程要尽快知道
        if self._pending and self._conn is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._conn.execute(
                    f"SELECT pg_notify('{CHANNEL}', p) FROM unnest($1::text[]) AS p", list(self._pending.values())),
                    timeout=1.0)
        self._pending = {}
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._task = self._flusher = None
        self._conn = None


def _quiet(task: asyncio.Future[Any]) -> None:
    if not task.cancelled() and task.exception() is not None:
        logger.error("处理进程间通知出错", exc_info=task.exception())


signals = Signals()

__all__ = ["CHANNEL", "Signals", "signals"]
