from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.errors import GraphRecursionError
from langgraph.types import Command
from sqlalchemy import select, update

from app.core.bus import bus
from app.core.config import settings
from app.core.events import EventType, RunEventModel
from app.db.base import SessionLocal
from app.db.models import Approval, Run, RunEvent, Workflow
from app.engine.compiler import compile_graph, initial_state
from app.engine.context import NodeError, RunContext
from app.engine.schema import GraphSpec, loop_steps, topology_of, validate_graph

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
        #: 正在关停。此时收到的取消来自关停而不是用户，运行要记成可恢复的中断
        self._closing = False
        #: 已经跑完、正在落状态和封存的运行。关停不去打断它们
        self._finalizing: set[str] = set()

    # ---------------- 生命周期 ----------------

    async def setup(self) -> None:
        self._closing = False
        settings.ensure_dirs()
        self._cm = AsyncSqliteSaver.from_conn_string(str(settings.checkpoint_path))
        self._checkpointer = await self._cm.__aenter__()
        # 上次进程被强杀时留下的记录。running 和 queued 要分开处理：
        # running 至少跑过一步、有 checkpoint，可以续；queued 还在排队，
        # 一个 checkpoint 都没写过，"恢复"它只会从 START 用默认值重跑一遍，
        # 还丢掉 run.input——那不是恢复，是伪造一份结果。
        async with SessionLocal() as session:
            await session.execute(
                update(Run)
                .where(Run.status == "running")
                .values(status="interrupted", error="服务重启，运行已挂起，可从断点恢复")
            )
            await session.execute(
                update(Run)
                .where(Run.status == "queued")
                .values(status="failed", error="服务重启时这次运行还在排队，没有可恢复的进度，请重新发起")
            )
            await session.commit()

    async def shutdown(self) -> None:
        self._closing = True
        # 只等还没结束的：结束了的没什么可等，而且它可能属于另一个事件循环，
        # gather 会直接抛 "future belongs to a different loop"
        pending = [t for t in self._tasks.values() if not t.done()]
        for run_id, task in list(self._tasks.items()):
            # 正在收尾的不打断：状态和封存写到一半，比哪一步都没写更难收拾
            if not task.done() and run_id not in self._finalizing:
                task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        self._tasks.clear()
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
        # 先落库，再广播。顺序反过来会漏事件，而且是结构性的漏：
        #
        # 订阅者的完整性靠"读历史 + 接实时"两段拼起来。它先订阅、再读历史，
        # 于是需要这条保证——**提交早于我这次 SELECT 的，在历史里；提交晚于
        # 我这次 SELECT 的，广播也晚于我订阅，在实时流里**。这条保证只有在
        # "提交先于广播"时才成立。
        #
        # 先广播的话，一条在订阅建立之前广播、却在读历史之后才提交的事件，
        # 两边都拿不到。实测跑一次 25ms 的图，六条事件里的 run.started 有
        # 一半几率就这么没了——界面表现为某个节点永远在转圈。
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
        await bus.publish(event)

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

    async def resume(
        self, run_id: str, response: Any, *, approval_id: str | None = None,
        actor: str | None = None,
    ) -> Run:
        """从人工介入的断点继续。

        两件事以前是错的：

        一是不看有没有 checkpoint 就发 Command。重启扫描会把 queued（从没跑过、
        没有任何 checkpoint）也标成 interrupted，对这种 run 发 Command，LangGraph
        会拿一个空 state 从 START 重跑整张图——run.input 完全没被注入，最后还落成
        succeeded。跑出来的是一份用默认值算的假结果。

        二是同一 superstep 里有多个 interrupt 时，只发标量 Command 会让 LangGraph
        直接抛 RuntimeError 打爆整个 run，而所有 pending 审批还都被写成同一个回复。
        现在按 interrupt_id 逐条收集，凑齐了才继续，没凑齐就继续等下一个人回复。
        """
        async with SessionLocal() as session:
            run = await session.get(Run, run_id)
            if not run:
                raise KeyError(f"找不到运行 {run_id}")
            if run.status not in ("interrupted",):
                raise ValueError(f"当前状态是 {run.status}，无法恢复")
            spec = GraphSpec.model_validate(run.graph)
            workflow_id = run.workflow_id

        # 先问引擎：这条线程到底停在哪、还等着哪些 interrupt
        run_ctx = RunContext(run_id=run_id, thread_id=run_id, spec=spec, workflow_id=workflow_id)
        app = compile_graph(spec, run_ctx).compile(checkpointer=self.checkpointer)
        config = {"configurable": {"thread_id": run_id}}
        snapshot = await app.aget_state(config)
        waiting = list(getattr(snapshot, "interrupts", None) or [])

        if not waiting and not getattr(snapshot, "next", None):
            # 没有断点可续。多半是重启时被标成 interrupted 的 queued 运行。
            raise ValueError(
                "这次运行没有可恢复的断点（很可能重启前还没真正开始跑）。"
                "请重新发起一次运行，而不是恢复——继续下去只会用默认值跑出一份假结果。"
            )

        answers: dict[str, Any] = {}
        async with SessionLocal() as session:
            pending = list((
                await session.execute(
                    select(Approval).where(
                        Approval.run_id == run_id, Approval.status == "pending"
                    )
                )
            ).scalars())

            # 定位这次回复的是哪一条：显式指定优先，否则只在唯一 pending 时才敢猜
            target = None
            if approval_id:
                target = next((a for a in pending if a.id == approval_id), None)
                if target is None:
                    raise ValueError(f"审批 {approval_id} 不在待处理列表里")
            elif len(pending) == 1:
                target = pending[0]
            elif len(pending) > 1:
                raise ValueError(
                    f"这次运行有 {len(pending)} 条待处理的人工介入，"
                    "恢复时必须指定 approval_id 说明回复的是哪一条"
                )

            now = datetime.now(timezone.utc)
            if target is not None:
                target.status = "answered"  # 已回复，但还没提交给引擎
                # 存全文，不能用 _safe 截：多条审批并存时，交给引擎的就是这一份
                # （见下面 answers）。截过的版本会把人工改过的长稿砍掉一截再送进节点
                full = json.loads(json.dumps(response, ensure_ascii=False, default=str))
                target.response = full if isinstance(full, dict) else {"value": full}
                target.resolved_at = now
                if actor:
                    target.resolved_by = actor
                await session.commit()

            # 凑齐没有？引擎还在等的每一个 interrupt 都得有答案
            answered = list((
                await session.execute(
                    select(Approval).where(
                        Approval.run_id == run_id,
                        Approval.status.in_(["answered", "pending"]),
                    )
                )
            ).scalars())
            by_iid = {a.interrupt_id: a for a in answered if a.interrupt_id}
            missing = []
            for item in waiting:
                iid = getattr(item, "id", None)
                rec = by_iid.get(iid)
                if rec is None or rec.status != "answered":
                    missing.append(iid)
                    continue
                payload = rec.response or {}
                # 只拆我们自己包的那层 {"value": x}（裸值回复）。带 approved / note / args
                # 的结构体要原样交给节点——以前一律取 value，只要回复里带了内容，
                # 驳回、备注、改过的参数就全在这里丢了
                wrapped = isinstance(payload, dict) and set(payload) == {"value"}
                answers[iid] = payload["value"] if wrapped else payload

            if missing:
                # 还差人没回。保持 interrupted，把已回复的那条留在 answered 上等齐
                await session.commit()
                run = await session.get(Run, run_id)
                return run

            # 齐了：正式落 resolved，然后驱动引擎
            for rec in answered:
                if rec.status == "answered":
                    rec.status = "resolved"
            run = await session.get(Run, run_id)
            bus.set_seq(run_id, run.last_seq)
            run.status = "running"
            run.error = None
            await session.commit()
            await session.refresh(run)

        # 单个 interrupt 用标量（LangGraph 两种都认），多个必须用 {interrupt_id: value} 映射。
        #
        # 一个都没在等：这是服务重启时停下的运行（启动扫描或关停时标成 interrupted），
        # 只能从断点接着跑（输入传 None）。以前这里照样发 Command(resume=…)——那个值
        # 会被后面遇到的第一个 interrupt() 当成答复，一个还没轮到的人工关卡就这么被
        # 自动作答了；当前版本的 LangGraph 则直接在内部崩掉。两样都不是"接着跑"
        command: Command | None
        if not waiting:
            command = None
        elif len(waiting) == 1:
            command = Command(resume=response)
        else:
            command = Command(resume=answers)
        await self._emit(run_id, EventType.RUN_RESUMED, data=(
            {"message": "服务重启时中断的运行，从断点接着跑"} if command is None
            else {"response": _safe(response)}
        ))
        self._tasks[run_id] = asyncio.create_task(
            self._drive(run_id, spec, command, workflow_id=workflow_id)
        )
        return run

    async def continue_failed(
        self, run_id: str, *, graph: dict[str, Any] | None = None, actor: str | None = None
    ) -> Run:
        """从失败的地方接着跑，而不是整张图从头来一遍。

        为什么值得单独开这条路：库里 28 条 failed 里，模型 id 写错、401、循环
        条件语法错、缺必填输入加起来占了大半——全是**配置错了**，改一下就能跑。
        而现在"改一下再跑"意味着前面花两分钟查完的表结构、跑完的 SQL 全部重来。
        checkpointer 一直开着（thread_id = run_id），那些结果本来就在库里躺着。

        graph 可以带一张改过的图，但只准改配置：节点 id、类型、连线必须和原来
        一模一样。checkpoint 是按节点名存的，改了结构就对不上，续下去会拿着
        错位的通道状态跑出一份似是而非的结果——那比直接报错糟得多。
        """
        async with SessionLocal() as session:
            run = await session.get(Run, run_id)
            if not run:
                raise KeyError(f"找不到运行 {run_id}")
            if run.status != "failed":
                raise ValueError(
                    f"当前状态是 {run.status}，只有失败的运行能接着跑。"
                    "（等人工介入的用恢复，已完成的重新发起一次。）"
                )
            spec = GraphSpec.model_validate(run.graph)
            workflow_id = run.workflow_id

        if graph is not None:
            revised = GraphSpec.model_validate(graph)
            if topology_of(revised) != topology_of(spec):
                raise ValueError(
                    "接着跑只能改节点配置，不能增删节点或改连线——"
                    "断点是按节点名存的，结构变了就对不上了。"
                    "要改结构请重新发起一次运行。"
                )
            report = validate_graph(revised)
            if not report.ok:
                first = next((i.message for i in report.issues if i.level == "error"), "")
                raise ValueError(f"改过的图没有通过校验：{first}")
            spec = revised

        # 先问引擎到底停在哪。没有待跑的节点就不是"能接着跑"的情形——
        # 硬发一个 None 下去，LangGraph 会从 START 重跑整张图
        run_ctx = RunContext(run_id=run_id, thread_id=run_id, spec=spec, workflow_id=workflow_id)
        app = compile_graph(spec, run_ctx).compile(checkpointer=self.checkpointer)
        config = {"configurable": {"thread_id": run_id}}
        snapshot = await app.aget_state(config)
        pending = [str(n) for n in (getattr(snapshot, "next", None) or [])]
        if not pending:
            raise ValueError(
                "这次运行没有留下可以接着跑的断点（多半是还没跑到第一个节点就挂了）。"
                "请重新发起一次运行——继续下去只会从头跑一遍，还看不出来。"
            )

        done = [n for n in (snapshot.values or {}).get("nodes", {}) if n not in pending]
        async with SessionLocal() as session:
            run = await session.get(Run, run_id)
            bus.set_seq(run_id, run.last_seq)
            run.status = "running"
            run.error = None
            if graph is not None:
                run.graph = spec.model_dump(mode="json")
            if actor:
                run.started_by = actor
            await session.commit()
            await session.refresh(run)

        labels = {n.id: n.title for n in spec.nodes}
        await self._emit(
            run_id, EventType.RUN_RESUMED,
            data={
                "from": pending,
                # 说清楚"哪些没重来"——否则用户看到时间线又动起来，
                # 分不清这次是接着跑还是整张图又跑了一遍
                "message": f"从「{labels.get(pending[0], pending[0])}」接着跑"
                           + (f"，前面 {len(done)} 个节点的结果保留" if done else ""),
            },
        )
        self._tasks[run_id] = asyncio.create_task(
            self._drive(run_id, spec, None, workflow_id=workflow_id)
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

        deadline: asyncio.Timeout | None = None
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
                    data={"nodes": len(spec.nodes),
                          "resumed": payload is None or isinstance(payload, Command)},
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
                # 循环按自己声明的轮数另记一份预算，全局上限只管没人把关的环
                extra_steps = loop_steps(spec)
                config = {
                    "configurable": {"thread_id": run_id},
                    "recursion_limit": settings.max_graph_steps + extra_steps,
                }

                interrupted = False
                # 单次执行的墙钟上限。以前 max_run_seconds 只存在于配置里，没有任何
                # 代码执行它——一个挂住的模型或工具能把运行和它占着的并发名额一直拖着。
                # 按"这一段执行"计：等人工审批时 _drive 已经结束，那段时间不算。
                deadline = asyncio.timeout(settings.max_run_seconds)
                async with deadline:
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
                if self._closing:
                    # 服务关停（dev.sh 的 --reload 改一个文件就是一次）不是用户取消。
                    # checkpoint 完好，记成中断、重启后从断点接着跑——和强杀后启动
                    # 扫描的处理一致。以前一律记"用户取消"，resume 和 continue 都拒绝它
                    status = "interrupted"
                    error = "服务重启，运行已挂起，可从断点恢复"
                    await self._emit(run_id, EventType.LOG, data={
                        "level": "warn", "code": "server_shutdown",
                        "message": "服务关停时这次运行还在跑，已挂起；重启后可以从断点接着跑",
                    })
                else:
                    status = "cancelled"
                    error = "用户取消"
                    await self._emit(run_id, EventType.RUN_CANCELLED, data={})
                raise
            except TimeoutError as e:
                status = "failed"
                if deadline is not None and deadline.expired():
                    error = (
                        f"这次执行超过了 {settings.max_run_seconds} 秒的上限，已中止。"
                        "跑得慢的多半是模型或工具没有响应；确实需要更久，调大 AGENTLAB_MAX_RUN_SECONDS"
                    )
                else:   # 别处抛的超时，不是这道上限——按普通失败说
                    error = f"{type(e).__name__}: {e}"
                    logger.exception("run %s 失败", run_id)
                await self._emit(run_id, EventType.RUN_FAILED, data={"error": error})
            except GraphRecursionError:
                status = "failed"
                # LangGraph 的原文是英文，还叫人去调 recursion_limit——用户碰不到那个键。
                # 循环的轮数已经另算了预算，走到这里的多半是不归 loop 节点管的环
                error = (
                    f"走满了 {settings.max_graph_steps + extra_steps} 步还没跑完，已中止"
                    f"（图最大步数 {settings.max_graph_steps}"
                    + (f"，另按循环轮数预留了 {extra_steps} 步" if extra_steps else "") + "）。"
                    "多半是有个环在空转：分支连回了上游、却没有 loop 节点给它定轮数上限。"
                    "用 loop 节点包住它；确实需要这么多步，调大 AGENTLAB_MAX_GRAPH_STEPS"
                )
                await self._emit(run_id, EventType.RUN_FAILED, data={"error": error})
            except Exception as e:  # noqa: BLE001
                status = "failed"
                # NodeError 的 message 本来就是写给人看的（"人工驳回：…"），
                # 再套一层类型名只会让界面上的错误更难读
                error = str(e) if isinstance(e, NodeError) else f"{type(e).__name__}: {e}"
                logger.exception("run %s 失败", run_id)
                await self._emit(run_id, EventType.RUN_FAILED, data={"error": error})
            finally:
                elapsed = int((time.perf_counter() - started) * 1000)
                self._finalizing.add(run_id)
                try:
                    await self._finalize(run_id, status, error, final_state, elapsed)
                finally:
                    # 收尾被打断（关停时的取消、库连接出错）也得把自己摘掉。以前写在
                    # _finalize 后面，一被打断就漏掉，留下的任务让下一次 shutdown 的
                    # gather 在别的事件循环里炸开
                    self._finalizing.discard(run_id)
                    self._tasks.pop(run_id, None)
                # 运行到终态就把沙箱会话收掉。thread_id 每个 run 都是新的，
                # 不收的话每跑一次带代码节点的图就多一台常驻 microVM（几百 MB），
                # 而且没有任何自动回收路径会碰它——闲置回收只在"下次有人执行
                # 代码"时才触发。interrupted 要留着，人回来还得接着跑。
                if status in ("succeeded", "failed", "cancelled"):
                    with contextlib.suppress(Exception):
                        from app.sandbox.manager import sandbox_manager

                        await sandbox_manager.cleanup(run_id)

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
                await session.commit()

        if status == "succeeded":
            await self._emit(
                run_id,
                EventType.RUN_FINISHED,
                data={"output": _safe(output), "usage": usage, "duration_ms": elapsed_ms},
            )
        if status != "interrupted":
            # 终态事件落库之后再封存。以前在 run.finished 之前算，清单里恰恰少了
            # 那条宣布"跑完了、成果是什么"的事件——改它的成果，清单照样对得上。
            # 中断态不封：事件还会继续追加
            await self._seal(run_id)
        if status in ("succeeded", "failed", "cancelled"):
            await bus.close(run_id)

    async def _seal(self, run_id: str) -> None:
        """对到此为止的全部事件算清单哈希，记下封到了哪一条。"""
        from app.core.artifact_store import manifest_hash as _manifest

        async with SessionLocal() as session:
            run = await session.get(Run, run_id)
            if run is None:
                return
            rows = [tuple(r) for r in await session.execute(
                select(RunEvent.seq, RunEvent.type, RunEvent.node_id, RunEvent.data)
                .where(RunEvent.run_id == run_id)
                .order_by(RunEvent.seq)
            )]
            run.manifest_hash = _manifest(rows)
            run.manifest_seq = rows[-1][0] if rows else 0
            await session.commit()


_TERMINAL = (EventType.RUN_FINISHED, EventType.RUN_FAILED, EventType.RUN_CANCELLED)


async def verify_manifest(run_id: str) -> dict[str, Any]:
    """重算封存范围内的清单哈希，和封存时记下的对一下。

    以前只写不验：README 说"事后修改流水将无法对齐"，可仓库里没有一行代码去对。
    封存范围之后追加的事件不参与核对。没有 manifest_seq 的是加这一列之前封存的
    老运行——当时的哈希是在 run.finished 落库之前算的，按那时的口径还原范围。
    """
    from app.core.artifact_store import manifest_hash as _manifest

    async with SessionLocal() as session:
        run = await session.get(Run, run_id)
        if run is None:
            raise KeyError(f"找不到运行 {run_id}")
        if not run.manifest_hash:
            return {"sealed": False, "ok": None,
                    "message": "这次运行还没有封存（没跑完，或者停在人工介入）"}
        rows = [tuple(r) for r in await session.execute(
            select(RunEvent.seq, RunEvent.type, RunEvent.node_id, RunEvent.data)
            .where(RunEvent.run_id == run_id)
            .order_by(RunEvent.seq)
        )]
        expected, sealed_at = run.manifest_hash, run.manifest_seq

    legacy = sealed_at is None
    if legacy:
        last = next((r for r in reversed(rows) if r[1] in _TERMINAL), None)
        if last is None:
            sealed_at = rows[-1][0] if rows else 0
        elif last[1] == EventType.RUN_FINISHED:
            sealed_at = last[0] - 1             # 老口径：run.finished 本身不在清单里
        else:
            sealed_at = last[0]                 # run.failed / run.cancelled 在封存前就落库了
    covered = [r for r in rows if r[0] <= sealed_at]
    ok = _manifest(covered) == expected
    return {
        "sealed": True, "ok": ok, "events": len(covered), "sealed_at": sealed_at, "legacy": legacy,
        "message": "事件流与封存时一致" if ok else "事件流和封存时对不上：封存之后有事件被改过、删过或插过",
    }


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
