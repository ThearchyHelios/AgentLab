from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from datetime import datetime, timezone
from typing import Any

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.types import Command
from sqlalchemy import select, update

from app.core.bus import bus
from app.core.config import settings
from app.core.events import EventType, RunEventModel
from app.db.base import SessionLocal
from app.db.models import Approval, Run, RunEvent, Workflow
from app.engine.compiler import compile_graph, initial_state
from app.engine.context import RunContext
from app.engine.schema import GraphSpec, validate_graph

logger = logging.getLogger(__name__)

# 流式增量（正文 token、思考 delta）量太大，只做实时推送不落库；
# 轨迹里正文靠 node.finished 的 preview，思考靠调用结束时的单条 llm.thinking 汇总
_EPHEMERAL = {EventType.LLM_TOKEN, EventType.LLM_THINKING_DELTA}


class RunManager:
    """负责一次运行的完整生命周期：启动、串流、中断、恢复、取消。"""

    def __init__(self) -> None:
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._checkpointer: AsyncSqliteSaver | None = None
        self._cm: Any = None
        self._semaphore = asyncio.Semaphore(settings.max_concurrent_runs)

    # ---------------- 生命周期 ----------------

    async def setup(self) -> None:
        settings.ensure_dirs()
        self._cm = AsyncSqliteSaver.from_conn_string(str(settings.checkpoint_path))
        self._checkpointer = await self._cm.__aenter__()
        # 上次进程被强杀时留下的 running 记录，标成中断态而不是永远转圈
        async with SessionLocal() as session:
            await session.execute(
                update(Run)
                .where(Run.status.in_(["running", "queued"]))
                .values(status="interrupted", error="服务重启，运行已挂起，可从断点恢复")
            )
            await session.commit()

    async def shutdown(self) -> None:
        for task in list(self._tasks.values()):
            task.cancel()
        await asyncio.gather(*self._tasks.values(), return_exceptions=True)
        if self._cm is not None:
            with contextlib.suppress(Exception):
                await self._cm.__aexit__(None, None, None)

    @property
    def checkpointer(self) -> AsyncSqliteSaver:
        if self._checkpointer is None:
            raise RuntimeError("RunManager 还没初始化")
        return self._checkpointer

    # ---------------- 事件 ----------------

    async def _emit(
        self,
        run_id: str,
        event_type: EventType | str,
        *,
        node_id: str | None = None,
        data: dict[str, Any] | None = None,
        persist: bool = True,
    ) -> None:
        event = RunEventModel(
            run_id=run_id,
            seq=bus.next_seq(run_id),
            type=EventType(str(event_type)),
            node_id=node_id,
            data=data or {},
        )
        await bus.publish(event)
        if persist and event.type not in _EPHEMERAL:
            async with SessionLocal() as session:
                session.add(
                    RunEvent(
                        run_id=run_id,
                        seq=event.seq,
                        type=str(event.type),
                        node_id=event.node_id,
                        ts=event.ts,
                        data=event.data,
                    )
                )
                await session.execute(
                    update(Run).where(Run.id == run_id).values(last_seq=event.seq)
                )
                await session.commit()

    async def note(
        self, run_id: str, event_type: EventType | str, *, node_id: str | None = None,
        **data: Any,
    ) -> None:
        """给 API 层用的事件入口（比如 formal 启动时记录口径升版处置）。"""
        await self._emit(run_id, event_type, node_id=node_id, data=data)

    # ---------------- 启动 ----------------

    async def start(
        self,
        *,
        graph: dict[str, Any],
        input_payload: dict[str, Any],
        workflow_id: str | None = None,
        workflow_name: str = "",
        memory_scope: str = "default",
        collection: str = "default",
        run_class: str = "exploratory",
        version: int | None = None,
        version_hash: str | None = None,
        started_by: str | None = None,
    ) -> Run:
        spec = GraphSpec.model_validate(graph)
        report = validate_graph(spec)
        if not report.ok:
            raise ValueError(
                "图校验未通过：" + "；".join(i.message for i in report.issues if i.level == "error")
            )

        async with SessionLocal() as session:
            run = Run(
                workflow_id=workflow_id,
                workflow_name=workflow_name,
                status="queued",
                graph=graph,
                input=input_payload,
                run_class=run_class,
                version=version,
                version_hash=version_hash,
                started_by=started_by,
            )
            run.thread_id = run.id  # 一个 run 一条 checkpoint 线程
            session.add(run)
            await session.commit()
            await session.refresh(run)

        self._tasks[run.id] = asyncio.create_task(
            self._drive(
                run.id,
                spec,
                initial_state(input_payload),
                workflow_id=workflow_id,
                memory_scope=memory_scope,
                collection=collection,
            )
        )
        return run

    async def resume(self, run_id: str, response: Any) -> Run:
        """从人工介入的断点继续。"""
        async with SessionLocal() as session:
            run = await session.get(Run, run_id)
            if not run:
                raise KeyError(f"找不到运行 {run_id}")
            if run.status not in ("interrupted",):
                raise ValueError(f"当前状态是 {run.status}，无法恢复")
            spec = GraphSpec.model_validate(run.graph)
            bus.set_seq(run_id, run.last_seq)
            run.status = "running"
            run.error = None

            # 把这次恢复对应的待审批记录结掉，前端的待办列表才不会一直挂着
            pending = (
                await session.execute(
                    select(Approval).where(
                        Approval.run_id == run_id, Approval.status == "pending"
                    )
                )
            ).scalars()
            for approval in pending:
                approval.status = "resolved"
                approval.response = _safe(response) if isinstance(response, dict) else {"value": _safe(response)}
                approval.resolved_at = datetime.now(timezone.utc)

            await session.commit()
            await session.refresh(run)

        await self._emit(run_id, EventType.RUN_RESUMED, data={"response": _safe(response)})
        self._tasks[run_id] = asyncio.create_task(
            self._drive(run_id, spec, Command(resume=response), workflow_id=run.workflow_id)
        )
        return run

    async def cancel(self, run_id: str) -> bool:
        task = self._tasks.get(run_id)
        if task and not task.done():
            task.cancel()
            return True
        return False

    def is_active(self, run_id: str) -> bool:
        task = self._tasks.get(run_id)
        return bool(task and not task.done())

    # ---------------- 执行 ----------------

    async def _drive(
        self,
        run_id: str,
        spec: GraphSpec,
        payload: Any,
        *,
        workflow_id: str | None = None,
        memory_scope: str = "default",
        collection: str = "default",
    ) -> None:
        started = time.perf_counter()
        status = "succeeded"
        error: str | None = None
        final_state: dict[str, Any] = {}

        async with self._semaphore:
            try:
                async with SessionLocal() as session:
                    await session.execute(
                        update(Run)
                        .where(Run.id == run_id)
                        .values(status="running", started_at=datetime.now(timezone.utc))
                    )
                    await session.commit()
                await self._emit(
                    run_id,
                    EventType.RUN_STARTED,
                    data={"nodes": len(spec.nodes), "resumed": isinstance(payload, Command)},
                )

                run_ctx = RunContext(
                    run_id=run_id,
                    thread_id=run_id,
                    spec=spec,
                    workflow_id=workflow_id,
                    memory_scope=memory_scope,
                    collection=collection,
                )
                app = compile_graph(spec, run_ctx).compile(checkpointer=self.checkpointer)
                config = {
                    "configurable": {"thread_id": run_id},
                    "recursion_limit": settings.max_graph_steps,
                }

                interrupted = False
                async for mode, chunk in app.astream(
                    payload, config=config, stream_mode=["custom", "updates"]
                ):
                    if mode == "custom":
                        await self._handle_custom(run_id, chunk)
                    elif mode == "updates":
                        if await self._handle_updates(run_id, chunk):
                            interrupted = True

                snapshot = await app.aget_state(config)
                final_state = dict(snapshot.values or {})
                if interrupted or snapshot.interrupts:
                    status = "interrupted"
                elif snapshot.next:
                    status = "interrupted"

            except asyncio.CancelledError:
                status = "cancelled"
                error = "用户取消"
                await self._emit(run_id, EventType.RUN_CANCELLED, data={})
                raise
            except Exception as e:  # noqa: BLE001
                status = "failed"
                error = f"{type(e).__name__}: {e}"
                logger.exception("run %s 失败", run_id)
                await self._emit(run_id, EventType.RUN_FAILED, data={"error": error})
            finally:
                elapsed = int((time.perf_counter() - started) * 1000)
                await self._finalize(run_id, status, error, final_state, elapsed)
                self._tasks.pop(run_id, None)

    async def _handle_custom(self, run_id: str, chunk: Any) -> None:
        """节点通过 get_stream_writer 发来的事件。"""
        if not isinstance(chunk, dict) or not chunk.get("__agentlab__"):
            return
        await self._emit(
            run_id,
            chunk.get("type", EventType.LOG),
            node_id=chunk.get("node_id"),
            data=chunk.get("data") or {},
        )

    async def _handle_updates(self, run_id: str, chunk: Any) -> bool:
        """LangGraph 的状态更新。这里只关心中断信号 —— 节点级事件已经由 custom 流覆盖。"""
        if not isinstance(chunk, dict):
            return False
        interrupts = chunk.get("__interrupt__")
        if not interrupts:
            return False

        for item in interrupts:
            value = getattr(item, "value", item)
            interrupt_id = getattr(item, "id", None)
            payload = value if isinstance(value, dict) else {"value": value}
            async with SessionLocal() as session:
                existing = (
                    await session.execute(
                        select(Approval).where(
                            Approval.run_id == run_id,
                            Approval.interrupt_id == interrupt_id,
                            Approval.status == "pending",
                        )
                    )
                ).scalar_one_or_none()
                if existing is None:
                    session.add(
                        Approval(
                            run_id=run_id,
                            node_id=str(payload.get("node_id") or ""),
                            interrupt_id=interrupt_id,
                            mode=str(payload.get("mode") or "approve"),
                            title=str(payload.get("title") or "需要你确认"),
                            payload=payload,
                            schema_=payload.get("schema") or {},
                        )
                    )
                    await session.commit()
            await self._emit(
                run_id,
                EventType.RUN_INTERRUPTED,
                node_id=payload.get("node_id"),
                data={"interrupt_id": interrupt_id, "payload": _safe(payload)},
            )
        return True

    async def _finalize(
        self,
        run_id: str,
        status: str,
        error: str | None,
        state: dict[str, Any],
        elapsed_ms: int,
    ) -> None:
        usage = dict(state.get("usage") or {})
        usage["duration_ms"] = elapsed_ms
        output = dict(state.get("output") or {})

        async with SessionLocal() as session:
            run = await session.get(Run, run_id)
            if run:
                run.status = status
                run.error = error
                run.output = output
                run.usage = usage
                if status != "interrupted":
                    run.finished_at = datetime.now(timezone.utc)
                    # 终态时对全部已落库事件计算清单哈希：事后任何对事件流的
                    # 增删改都会与这个值对不上。中断态不算——事件还会继续追加。
                    from app.core.artifact_store import manifest_hash as _manifest

                    rows = await session.execute(
                        select(RunEvent.seq, RunEvent.type, RunEvent.node_id, RunEvent.data)
                        .where(RunEvent.run_id == run_id)
                        .order_by(RunEvent.seq)
                    )
                    run.manifest_hash = _manifest([tuple(r) for r in rows])
                await session.commit()

        if status == "succeeded":
            await self._emit(
                run_id,
                EventType.RUN_FINISHED,
                data={"output": _safe(output), "usage": usage, "duration_ms": elapsed_ms},
            )
        if status in ("succeeded", "failed", "cancelled"):
            await bus.close(run_id)


def _safe(value: Any, limit: int = 4000) -> Any:
    """把任意对象裁剪成能安全放进事件负载的形状。"""
    if isinstance(value, str):
        return value[:limit]
    if isinstance(value, dict):
        return {k: _safe(v, limit // 2) for k, v in list(value.items())[:50]}
    if isinstance(value, list):
        return [_safe(v, limit // 2) for v in value[:50]]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return str(value)[:limit]


run_manager = RunManager()
