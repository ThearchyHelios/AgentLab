from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.errors import GraphRecursionError
from langgraph.types import Command
from sqlalchemy import select, update

from app.core.bus import bus
from app.core.config import settings
from app.core.errors import describe_exception, raw_detail
from app.core.events import EventType, RunEventModel
from app.db.base import SessionLocal
from app.db.models import Approval, Run, RunEvent, Workflow
from app.engine.approval import read_decision
from app.engine.compiler import compile_graph, initial_state
from app.engine.context import NodeError, RunContext
from app.engine.replay import PROTOCOL, PROTOCOL_KEY, protocol_of
from app.engine.schema import GraphSpec, NodeType, loop_steps, topology_of, validate_graph

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
        #: 谁停的：取消执行任务时记下，run.cancelled 和关掉的审批上写得出人
        self._stopped_by: dict[str, str | None] = {}

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
            stranded = list((await session.execute(
                select(Run.id, Run.last_seq).where(Run.status == "running")
            )).all())
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
        # 进程被强杀时连 server_shutdown 日志都来不及发，事件流停在半路。补上这一条，
        # 回放它的客户端才知道它是挂起了，而不是还在跑
        for run_id, last_seq in stranded:
            bus.set_seq(run_id, last_seq or 0)
            await self._emit(run_id, EventType.LOG, data={
                "level": "warn", "code": "server_shutdown",
                "message": "服务上次没有正常关停，这次运行停在了半路；可以从断点接着跑",
            })

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
        ts: float | None = None,
    ) -> None:
        event = self._event(run_id, event_type, node_id=node_id, data=data, ts=ts)
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
                await _stage(session, event)
                await session.commit()
        await bus.publish(event)

    @staticmethod
    def _event(
        run_id: str,
        event_type: EventType | str,
        *,
        node_id: str | None = None,
        data: dict[str, Any] | None = None,
        ts: float | None = None,
    ) -> RunEventModel:
        return RunEventModel(
            run_id=run_id,
            seq=bus.next_seq(run_id),
            type=EventType(str(event_type)),
            node_id=node_id,
            data=data or {},
            # 节点里发出的事件带着它发生的时刻。以前一律取转发时刻：并行的成员
            # 先做完的那条也要排到整轮结束才被转发，时间线上的耗时就全错了
            **({"ts": ts} if ts else {}),
        )

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
        approval_default: str | None = None,
    ) -> Run:
        spec = GraphSpec.model_validate(graph)
        report = validate_graph(spec)
        if not report.ok:
            raise ValueError(
                "工作流没有通过校验，先在画布上修好标红的节点再运行："
                + "；".join(i.message for i in report.issues if i.level == "error")
            )
        if approval_default is None:
            approval_default = await _approval_default(run_class=run_class)

        async with SessionLocal() as session:
            from app.tools.trust import run_snapshot

            tool_trust = await run_snapshot(session, run_class=run_class)
            agent_limits = await _agent_limits(session)
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
                memory_scope=memory_scope,
                collection=collection,
                approval_default=approval_default,
                tool_trust=tool_trust,
                agent_limits=agent_limits,
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
                approval_default=approval_default,
                tool_trust=tool_trust,
                agent_limits=agent_limits,
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
                raise ValueError(_cannot_resume(run.status))
            spec = GraphSpec.model_validate(run.graph)
            workflow_id = run.workflow_id
            carried = _carried(run)

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
        if stale := await _stale_replay(run_id, spec, snapshot):
            raise ValueError(
                f"这次运行是升级前停在「{stale}」上的，引擎记录断点的方式已经变了：现在恢复，"
                f"「{stale}」里已经执行过的工具会再执行一次。请放弃这次运行，重新发起"
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
                    raise ValueError("这条审批已经处理过了，或者不属于这次运行。刷新看看最新状态")
            elif len(pending) == 1:
                target = pending[0]
            elif len(pending) > 1:
                raise ValueError(
                    f"这次运行有 {len(pending)} 条待处理的审批，"
                    "得说明回复的是哪一条（approval_id），请在对应的审批卡上操作"
                )

            now = datetime.now(timezone.utc)
            if target is not None:
                # 存全文，不能用 _safe 截：多条审批并存时，交给引擎的就是这一份
                # （见下面 answers）。截过的版本会把人工改过的长稿砍掉一截再送进节点
                full = json.loads(json.dumps(response, ensure_ascii=False, default=str))
                # 条件更新：上面读到它还是 pending，到这里之间可能已经被放弃运行关掉
                # （或者另一个人先批了）。无条件写回去，会把关掉的审批又改成已回复
                marked = await session.execute(
                    update(Approval)
                    .where(Approval.id == target.id, Approval.status == "pending")
                    .values(status="answered",  # 已回复，但还没提交给引擎
                            response=full if isinstance(full, dict) else {"value": full},
                            resolved_at=now, **({"resolved_by": actor} if actor else {}))
                )
                if marked.rowcount != 1:
                    await session.rollback()
                    raise ValueError("这条审批已经处理过了，或者这次运行已经放弃了。刷新看看最新状态")
                await session.commit()
                # 审批卡上点的是「始终允许」：这个工具以后改由门控把关。只有带 trust_key 的
                # 审批才有这个按钮（MCP / 自定义工具、探索运行），别的审批带了 always 也不理。
                # 这次运行里本节点后面的调用由节点自己从答复里认出来（见 llm.py 的 granted）
                trust_key = (target.payload or {}).get("trust_key")
                if isinstance(trust_key, str) and trust_key and read_decision(response).always:
                    from app.tools.trust import set_trust

                    await set_trust(session, trust_key, "gated")

            # 凑齐没有？引擎还在等的每一个 interrupt 都得有答案
            answered = list((
                await session.execute(
                    select(Approval).where(
                        Approval.run_id == run_id,
                        Approval.status.in_(["answered", "pending"]),
                    ).execution_options(populate_existing=True)
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
                if run.status != "interrupted":
                    # 审批没了是因为运行在这期间被放弃了：不能回一个「已记下」
                    raise ValueError(_cannot_resume(run.status))
                return run

            # 齐了：正式落 resolved，然后驱动引擎。谁批的跟着交给节点，
            # human.resolved 里才写得出签批人
            ready = [rec for rec in answered if rec.status == "answered"]
            actors: dict[str, str | None] = {rec.node_id: rec.resolved_by for rec in ready}
            # 回到 running 也用条件更新占住：从上面读状态到这里隔了好几次 await，
            # 放弃运行可能正好在这中间把它收成了 cancelled（已经封存、发过 run.cancelled）。
            # 无条件写 running，一次取消了的运行就又被拉起来跑了
            await _claim(session, run_id, ("interrupted",), _cannot_resume)
            await session.execute(
                update(Approval)
                .where(Approval.id.in_([rec.id for rec in ready]), Approval.status == "answered")
                .values(status="resolved")
            )
            run = await session.get(Run, run_id)
            bus.set_seq(run_id, run.last_seq)
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
            {"message": "服务重启时中断的运行，从断点接着跑", "actor": actor} if command is None
            else {"response": _safe(response), "actor": actor}
        ))
        self._tasks[run_id] = asyncio.create_task(
            self._drive(run_id, spec, command, workflow_id=workflow_id, **carried,
                        resumed=sorted(_replayed(snapshot)),
                        actors=actors)
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

        服务重启挂起的运行（interrupted 且没有待审批）也走这里：它没有在等谁，
        断点完好，从断点驱动和失败后接着跑是同一件事。界面上两者都叫「接着跑」。
        """
        async with SessionLocal() as session:
            run = await session.get(Run, run_id)
            if not run:
                raise KeyError(f"找不到运行 {run_id}")
            has_pending = run.status == "interrupted" and (await session.execute(
                select(Approval.id).where(
                    Approval.run_id == run_id, Approval.status.in_(["pending", "answered"])
                ).limit(1)
            )).first() is not None
            if not (run.status == "failed" or (run.status == "interrupted" and not has_pending)):
                raise ValueError(_cannot_continue(run.status, has_pending))
            observed = run.status
            spec = GraphSpec.model_validate(run.graph)
            workflow_id = run.workflow_id
            carried = _carried(run)

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
        if stale := await _stale_replay(run_id, spec, snapshot):
            raise ValueError(
                f"这次运行是升级前停在「{stale}」上的，引擎记录断点的方式已经变了：现在接着跑，"
                f"「{stale}」里已经执行过的工具会再执行一次。请重新发起一次运行"
            )

        done = [n for n in (snapshot.values or {}).get("nodes", {}) if n not in pending]
        async with SessionLocal() as session:
            # 和 resume 一样用条件更新占住：读状态之后，放弃运行、或者另一次接着跑
            # 可能已经先动了它
            await _claim(session, run_id, (observed,),
                              lambda status: _cannot_continue(status, False),
                              **({"graph": spec.model_dump(mode="json")} if graph is not None else {}),
                              **({"started_by": actor} if actor else {}))
            run = await session.get(Run, run_id)
            bus.set_seq(run_id, run.last_seq)
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
                "actor": actor,
            },
        )
        self._tasks[run_id] = asyncio.create_task(
            self._drive(run_id, spec, None, workflow_id=workflow_id, **carried, resumed=pending)
        )
        return run

    async def cancel(self, run_id: str, *, actor: str | None = None) -> str | None:
        """停止一次运行。返回 "stopping"（执行任务已取消，终态由它自己收尾）、
        "cancelled"（停在断点上的，这里直接收成终态），或者 None（已经是终态、或不存在）。

        停在断点上的运行——等审批的、服务重启挂起的——手上没有执行任务。以前这里只会
        去取消任务，于是它们一律「不在执行中」：待审批的那张卡只能批了它、让它跑完。
        """
        if run_id in self._finalizing:
            # 执行任务已经跑完、正在落状态——刚发出 run.interrupted、还在写 interrupted
            # 的那一刻，界面上已经给出了「放弃这次运行」。这时取消任务会把收尾从中间
            # 打断：状态停在 running、审批还挂着、任务却没了，再点一次只剩 409。
            # 等它落完，再按落下的状态处理
            await self.wait_idle(run_id, timeout=30.0)
            if run_id in self._finalizing:      # 收尾卡住了（库被锁着）：也不去打断它
                return None
        task = self._tasks.get(run_id)
        if task and not task.done():
            self._stopped_by[run_id] = actor
            task.cancel()
            return "stopping"
        return "cancelled" if await self._abandon(run_id, actor) else None

    async def _abandon(self, run_id: str, actor: str | None) -> bool:
        """把停在断点上的运行收成 cancelled：关掉待审批、发 run.cancelled、封存。"""
        now = datetime.now(timezone.utc)
        async with SessionLocal() as session:
            # 条件更新占住这次收尾：同一时刻有人批了它（resume 把它改回 running），
            # 或者另一个取消先到了，这里就什么都不做
            claimed = await session.execute(
                update(Run).where(Run.id == run_id, Run.status == "interrupted")
                .values(status="cancelled", finished_at=now)
            )
            if claimed.rowcount != 1:
                await session.rollback()
                return False
            closed = await _close_approvals(session, run_id, actor, now)
            run = await session.get(Run, run_id)
            timing = await _timing(session, run, 0, now)
            run.usage = {**(run.usage or {}), "duration_ms": timing["active_ms"], **timing}
            run.error = "用户取消：在等审批时放弃了这次运行" if closed else "用户取消：挂起期间放弃了这次运行"
            bus.set_seq(run_id, run.last_seq or 0)
            # 和状态同一个事务提交，理由同 _finalize
            event = self._event(run_id, EventType.RUN_CANCELLED, data={
                "timing": timing, "actor": actor,
                "message": f"放弃了这次运行，{closed} 条待审批一并关闭" if closed
                           else "放弃了这次挂起的运行",
            })
            await _stage(session, event, run)
            await session.commit()

        await bus.publish(event)
        await self._seal(run_id)
        # 等审批时在线的连接没有关（它还在等恢复），这里让它收到终态再结束
        await bus.close(run_id)
        with contextlib.suppress(Exception):
            from app.sandbox.manager import sandbox_manager

            await sandbox_manager.cleanup(run_id)
        return True

    def is_active(self, run_id: str) -> bool:
        task = self._tasks.get(run_id)
        return bool(task and not task.done())

    async def wait_idle(self, run_id: str, timeout: float = 10.0) -> None:
        """等这次运行手上的执行任务收完尾。不取消它，也不抛它的异常。"""
        task = self._tasks.get(run_id)
        if task is not None and not task.done():
            await asyncio.wait({task}, timeout=timeout)

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
        approval_default: str | None = None,
        tool_trust: dict[str, str] | None = None,
        agent_limits: dict[str, Any] | None = None,
        resumed: list[str] | tuple[str, ...] = (),
        actors: dict[str, str | None] | None = None,
    ) -> None:
        started = time.perf_counter()
        status = "succeeded"
        error: str | None = None
        final_state: dict[str, Any] = {}
        #: 终态事件（run.failed / run.cancelled）等 _finalize 算完耗时再发，timing 才带得上
        terminal: tuple[EventType, dict[str, Any]] | None = None
        error_node: str | None = None

        deadline: asyncio.Timeout | None = None
        async with self._semaphore:
            try:
                async with SessionLocal() as session:
                    row = await session.get(Run, run_id)
                    if row is not None:
                        row.status = "running"
                        # started_at 只在第一次开始时写。以前每段 _drive 都覆盖它，
                        # 审批恢复、接着跑之后，运行的"开始时间"就成了最后一段的开始
                        if row.started_at is None:
                            row.started_at = datetime.now(timezone.utc)
                        row.finished_at = None
                        row.error_node_id = None
                        await session.commit()
                await self._emit(
                    run_id,
                    EventType.RUN_STARTED,
                    data={"nodes": len(spec.nodes),
                          "resumed": payload is None or isinstance(payload, Command),
                          # 实际生效的运行参数进事件流，也就进了封存清单：
                          # 正式运行查的是哪个库、审批默认是什么，事后可复核
                          "memory_scope": memory_scope, "collection": collection,
                          "approval_default": approval_default,
                          # MCP / 自定义工具的信任三档（只列不是「等审批」的；正式运行记 formal）
                          "tool_trust": _trust_event(tool_trust),
                          # agent 护栏的上限（步数兜底、令牌 / 金额预算），None 是升级前的运行
                          "agent_limits": agent_limits,
                          # 这一段是按哪一版重放协议跑的，恢复时据此认出升级前做下的工作
                          "replay_protocol": PROTOCOL},
                )

                run_ctx = RunContext(
                    run_id=run_id,
                    thread_id=run_id,
                    spec=spec,
                    workflow_id=workflow_id,
                    memory_scope=memory_scope,
                    collection=collection,
                    approval_default=approval_default,
                    tool_trust=tool_trust,
                    agent_limits=agent_limits,
                    extra={"resumed": set(resumed), "actors": dict(actors or {})},
                )
                app = compile_graph(spec, run_ctx).compile(checkpointer=self.checkpointer)
                # 循环按自己声明的轮数另记一份预算，全局上限只管没人把关的环
                extra_steps = loop_steps(spec)
                config = {
                    "configurable": {"thread_id": run_id},
                    "recursion_limit": settings.max_graph_steps + extra_steps,
                    # 进每个 checkpoint 的 metadata：恢复时据此认出旧版引擎停下的断点
                    "metadata": {PROTOCOL_KEY: PROTOCOL},
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
                    terminal = (EventType.RUN_CANCELLED, {"actor": self._stopped_by.pop(run_id, None)})
                raise
            except TimeoutError as e:
                status = "failed"
                if deadline is not None and deadline.expired():
                    error = (
                        f"这次执行超过了 {settings.max_run_seconds} 秒的上限，已中止。"
                        "跑得慢的多半是模型或工具没有响应；确实需要更久，调大 AGENTLAB_MAX_RUN_SECONDS"
                    )
                    terminal = (EventType.RUN_FAILED, {"error": error})
                else:   # 别处抛的超时，不是这道上限——按普通失败说
                    error, terminal = _failure(e, spec)
                    logger.exception("run %s 失败", run_id)
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
                terminal = (EventType.RUN_FAILED, {"error": error})
            except Exception as e:  # noqa: BLE001
                status = "failed"
                error, terminal = _failure(e, spec)
                error_node = terminal[1].get("node_id")
                logger.exception("run %s 失败", run_id)
            finally:
                elapsed = int((time.perf_counter() - started) * 1000)
                self._finalizing.add(run_id)
                try:
                    await self._finalize(run_id, status, error, final_state, elapsed,
                                         terminal=terminal, error_node=error_node)
                finally:
                    # 收尾被打断（关停时的取消、库连接出错）也得把自己摘掉。以前写在
                    # _finalize 后面，一被打断就漏掉，留下的任务让下一次 shutdown 的
                    # gather 在别的事件循环里炸开
                    self._finalizing.discard(run_id)
                    self._tasks.pop(run_id, None)
                    self._stopped_by.pop(run_id, None)
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
            ts=chunk.get("ts"),
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
        *,
        terminal: tuple[EventType, dict[str, Any]] | None = None,
        error_node: str | None = None,
    ) -> None:
        usage = dict(state.get("usage") or {})
        output = dict(state.get("output") or {})
        now = datetime.now(timezone.utc)
        timing = {"wall_ms": elapsed_ms, "active_ms": elapsed_ms, "wait_ms": 0}

        # 终态事件和运行记录在同一个事务里提交，提交之后再广播。两边都有人在读：
        # 读到 failed 的一方马上去翻事件，要翻得到 run.failed（报错原因和定位都在
        # 那条事件里）；收到 run.finished 的一方马上 GET 这次运行取完整成果（事件里
        # 的 output 是截断的），要读得到 succeeded 和 output。分两次提交，总有一边
        # 会读到半截——以前先提交状态，前一种偶尔翻不到；反过来先落事件，后一种
        # 每次都读到 running 和空的 output
        event: RunEventModel | None = None
        async with SessionLocal() as session:
            run = await session.get(Run, run_id)
            if run:
                timing = await _timing(session, run, elapsed_ms, now)
                if status != "succeeded":
                    # 状态里的用量只算跑完了的节点。失败、取消、停在审批上的那个节点已经
                    # 花出去的调用（校验修复、agent 的前几步）只在 llm.end 里——以前这部分
                    # 在运行记录上凭空消失，失败的运行看着像没花钱
                    usage = _spent(usage, await _usage_of_events(session, run_id))
                # duration_ms 是各段执行时长之和（= active_ms）。以前用这一段的耗时
                # 覆盖它：87 秒、中间等过一次审批的运行，列表上写着 22ms
                usage.update(duration_ms=timing["active_ms"], **timing)

            if status == "succeeded":
                # 事件里的 output 仍然截断（事件体积要控制），但截了就要说：消费方据此
                # 回源 GET /api/runs/{id} 取全文。以前截得悄无声息，问数据页把 2000 字处
                # 切断的半截答案当成完整结论落了库
                clipped, truncated = _clip(output)
                data: dict[str, Any] = {"output": clipped, "usage": usage,
                                        "duration_ms": timing["active_ms"], "timing": timing}
                if truncated:
                    data["output_truncated"] = True
                event = self._event(run_id, EventType.RUN_FINISHED, data=data)
            elif terminal is not None:
                kind, data = terminal
                event = self._event(run_id, kind, data={**data, "timing": timing})

            if run:
                run.status = status
                run.error = error
                run.output = output
                run.usage = usage
                run.error_node_id = error_node
                if status != "interrupted":
                    run.finished_at = now
                if status in ("failed", "cancelled"):
                    # 并行支路上已经停下来等人的那张审批卡，运行结束了它也就没有意义了；
                    # 留着的话它一直挂在全局待办里，批了也只会被告知运行不在断点上
                    actor = terminal[1].get("actor") if terminal else None
                    await _close_approvals(session, run_id, actor, now)
                if event is not None:
                    await _stage(session, event, run)
                await session.commit()
        if event is not None:
            await bus.publish(event)

        if status != "interrupted":
            # 终态事件落库之后再封存。以前在 run.finished 之前算，清单里恰恰少了
            # 那条宣布"跑完了、成果是什么"的事件——改它的成果，清单照样对得上。
            # 中断态不封：事件还会继续追加
            await self._seal(run_id)
        if status in ("succeeded", "failed", "cancelled") or (
            status == "interrupted" and self._closing
        ):
            # 关停时挂起的也要让在线的客户端收尾：进程马上就没了，不说一声，
            # 界面会一直以为它还在跑
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
                    "message": "这次运行还没有封存（没跑完，或者停在人工审批）"}
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
    return _clip(value, limit)[0]


def _clip(value: Any, limit: int = 4000) -> tuple[Any, bool]:
    """同 _safe，另外说出有没有真的截掉东西。"""
    if isinstance(value, str):
        return value[:limit], len(value) > limit
    if isinstance(value, (dict, list)):
        items = list(value.items()) if isinstance(value, dict) else list(enumerate(value))
        cut = len(items) > 50
        kept: list[tuple[Any, Any]] = []
        for key, item in items[:50]:
            clipped, lost = _clip(item, limit // 2)
            kept.append((key, clipped))
            cut = cut or lost
        return (dict(kept) if isinstance(value, dict) else [v for _, v in kept]), cut
    if isinstance(value, (int, float, bool)) or value is None:
        return value, False
    text = str(value)
    return text[:limit], len(text) > limit


_STATUS_TEXT = {
    "queued": "还在排队", "running": "正在运行", "interrupted": "停在断点上",
    "succeeded": "已完成", "failed": "失败了", "cancelled": "已取消",
}


def _cannot_resume(status: str) -> str:
    hint = {
        "running": "它还在跑，不需要恢复。",
        "queued": "它还没开始跑。",
        "succeeded": "要再跑一次，请重新发起运行。",
        "failed": "失败的运行用「接着跑」从出错的节点继续。",
        "cancelled": "已取消的运行请重新发起。",
    }.get(status, "")
    return f"这次运行{_STATUS_TEXT.get(status, status)}，没有在等审批，不能恢复。{hint}"


def _cannot_continue(status: str, has_pending: bool) -> str:
    """接着跑被拒时说清楚为什么、下一步做什么。

    以前一律套一句「在等待审批，只有失败的运行能接着跑」：服务重启挂起的也被说成
    在等审批，还让人去一张不存在的审批卡上处理；已取消的只说不行，不说该怎么办。
    """
    if status == "interrupted" and has_pending:
        return "这次运行在等审批，不是失败了，不用接着跑。在审批卡上放行或驳回，它会自己往下走。"
    return {
        "cancelled": "这次运行已取消，不能接着跑。要继续，请重新发起一次运行。",
        "succeeded": "这次运行已完成，没有要接着跑的——只有失败的运行或被服务重启打断的运行"
                     "能接着跑。要再跑一次，请重新发起运行。",
        "running": "这次运行正在运行，不需要接着跑。",
        "queued": "这次运行还在排队，还没开始跑，不需要接着跑。",
    }.get(status, f"这次运行{_STATUS_TEXT.get(status, status)}，"
                  "只有失败的运行或被服务重启打断的运行能接着跑。")


def _carried(run: Run) -> dict[str, Any]:
    """第一次发起时定下的运行参数。恢复、接着跑都沿用，老运行没记的按缺省。"""
    return {
        "memory_scope": run.memory_scope or "default",
        "collection": run.collection or "default",
        "approval_default": run.approval_default,
        "tool_trust": run.tool_trust,
        "agent_limits": run.agent_limits,
    }


async def _agent_limits(session: Any) -> dict[str, Any]:
    from app.api.settings import run_defaults
    from app.engine.guards import run_limits

    return run_limits(await run_defaults(session), settings.max_agent_steps)


def _trust_event(tool_trust: dict[str, str] | None) -> Any:
    from app.tools.trust import FORMAL_MARK, snapshot_levels

    if tool_trust is None:
        return None
    return "formal" if tool_trust.get(FORMAL_MARK) else snapshot_levels(tool_trust)


def _failure(exc: BaseException, spec: GraphSpec) -> tuple[str, tuple[EventType, dict[str, Any]]]:
    """失败的首行报错（给人看）和 run.failed 的负载（带上定位和原始异常）。"""
    if isinstance(exc, NodeError):
        # NodeError 的 message 本来就是写给人看的（"人工驳回：…"），
        # 再套一层类型名只会让界面上的错误更难读
        message = str(exc)
        detail = raw_detail(exc.__cause__) if exc.__cause__ is not None else None
        node_id: str | None = exc.node_id
    else:
        message = (f"运行出错：{describe_exception(exc)}。技术细节已记在服务日志里；"
                   "可以点「接着跑」重试，仍然失败请把运行编号反馈给维护者")
        detail = raw_detail(exc)
        node_id = None
    data: dict[str, Any] = {"error": message}
    if detail:
        data["detail"] = detail
    if node_id:
        node = spec.node_map().get(node_id)
        data.update(node_id=node_id, label=node.title if node else node_id)
    return message, (EventType.RUN_FAILED, data)


async def _approval_default(*, run_class: str) -> str:
    """工具审批策略的全局默认，取自设置里的「危险工具默认需要人工确认」。

    正式运行不吃"关"。它跑的是封存的发布版，审批行为只该由那一版里写明的配置
    决定，不能随一个随时能改的偏好变；受管门禁要求危险工具至少人工确认，也不能被
    全局开关悄悄降掉。不能按工作流的 status 判断是不是受管：画布上再存一次，status
    就退回 draft，而 published_version 仍指着受管的那一版。
    """
    if run_class == "formal":
        return "dangerous"
    from app.api.settings import run_defaults, tool_approval_default

    async with SessionLocal() as session:
        return tool_approval_default(await run_defaults(session))


def _replayed(snapshot: Any) -> set[str]:
    """从断点驱动时要（重新）执行的节点：还没跑的（next），加上停在 interrupt 上、或者
    失败了的。恢复过一次的节点手里已经有答复的写入，不在 next 里，只能从 tasks 上认。"""
    again = {str(x) for x in getattr(snapshot, "next", None) or ()}
    again.update(str(t.name) for t in getattr(snapshot, "tasks", None) or ()
                 if getattr(t, "interrupts", None) or getattr(t, "error", None))
    return again


async def _stale_replay(run_id: str, spec: GraphSpec, snapshot: Any) -> str | None:
    """旧版重放协议写下的断点，按现在的代码重放会不会把做过的工具再做一遍。会的话返回节点名。

    有风险的是：断点之后，旧版代码在这个节点里已经执行过工具、或者已经批过一次。它们
    的 task 缓存排在旧的位置上，新代码多插了记录 task，位置全错开了。停在第一次审批上、
    之前什么都没做的，重放只会多发一条 human.requested，照常放行；人工审批节点同理。

    「断点之后」按 checkpoint 的写入时刻算：同一个 superstep 里恢复不会写新的 checkpoint，
    所以升级后恢复过一次的运行，断点仍是旧版写的，而那之后的工作是新版做的——
    哪一段是哪一版，看那一段 run.started 里记的 replay_protocol。
    """
    if protocol_of(getattr(snapshot, "metadata", None)) >= PROTOCOL:
        return None
    nodes = spec.node_map()
    suspects = [n for n in _replayed(snapshot) if n in nodes and nodes[n].type != NodeType.HUMAN]
    if not suspects:
        return None
    try:
        since = datetime.fromisoformat(str(snapshot.created_at)).timestamp()
    except (AttributeError, TypeError, ValueError):
        since = 0.0
    async with SessionLocal() as session:
        rows = (await session.execute(
            select(RunEvent.type, RunEvent.node_id, RunEvent.ts, RunEvent.data)
            .where(RunEvent.run_id == run_id,
                   RunEvent.type.in_([EventType.RUN_STARTED, EventType.TOOL_START,
                                      EventType.HUMAN_RESOLVED]))
            .order_by(RunEvent.seq)
        )).all()
    protocol = 1
    for kind, owner, ts, data in rows:
        if kind == EventType.RUN_STARTED:
            protocol = protocol_of({PROTOCOL_KEY: (data or {}).get("replay_protocol")})
            continue
        if protocol >= PROTOCOL or (ts or 0) < since:
            continue
        for node_id in suspects:
            if owner == node_id or (owner or "").startswith(f"{node_id}/"):
                return nodes[node_id].title or node_id
    return None


async def _claim(
    session: Any, run_id: str, expected: tuple[str, ...], refusal: Callable[[str], str],
    **values: Any,
) -> None:
    """只在状态仍是 expected 时把运行改成 running；已经被别人改过就回滚、说明原因。"""
    claimed = await session.execute(
        update(Run).where(Run.id == run_id, Run.status.in_(expected))
        .values(status="running", error=None, **values)
    )
    if claimed.rowcount != 1:
        await session.rollback()
        status = await session.scalar(select(Run.status).where(Run.id == run_id))
        raise ValueError(refusal(status or ""))


async def _stage(session: Any, event: RunEventModel, run: Run | None = None) -> None:
    """把一条事件写进这个 session，随它一起提交。run 已经在 session 里时直接改它的 last_seq。"""
    session.add(RunEvent(run_id=event.run_id, seq=event.seq, type=str(event.type),
                         node_id=event.node_id, ts=event.ts, data=event.data))
    if run is not None:
        run.last_seq = event.seq
    else:
        await session.execute(
            update(Run).where(Run.id == event.run_id).values(last_seq=event.seq)
        )


async def _close_approvals(session: Any, run_id: str, actor: str | None, now: datetime) -> int:
    """运行已经结束，它还没处理完的审批一并关掉。返回关掉了几条。"""
    rows = list((await session.execute(
        select(Approval).where(Approval.run_id == run_id,
                               Approval.status.in_(["pending", "answered"]))
    )).scalars())
    for approval in rows:
        approval.status = "cancelled"
        approval.resolved_at = now
        approval.resolved_by = actor
    return len(rows)


_USAGE_KEYS = ("input_tokens", "output_tokens", "cost_usd", "calls")


async def _usage_of_events(session: Any, run_id: str) -> dict[str, Any]:
    """按 llm.end 事件逐次加总的用量：每次模型调用都有一条，不管节点最后成没成。"""
    rows = await session.execute(
        select(RunEvent.data).where(RunEvent.run_id == run_id, RunEvent.type == EventType.LLM_END)
    )
    total: dict[str, Any] = dict.fromkeys(_USAGE_KEYS, 0)
    for (data,) in rows:
        for key in _USAGE_KEYS:
            value = (data or {}).get(key, 1 if key == "calls" else 0)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                total[key] += value
    total["cost_usd"] = round(total["cost_usd"], 6)
    return total


def _spent(state_usage: dict[str, Any], events_usage: dict[str, Any]) -> dict[str, Any]:
    """两本账取大的那本。新引擎每次调用都发 llm.end，事件那本不会更少；
    加这条事件之前的老运行（agent 节点不发 llm.end）状态那本更全。"""
    out = dict(state_usage)
    for key in _USAGE_KEYS:
        mine, theirs = out.get(key) or 0, events_usage.get(key) or 0
        if isinstance(mine, (int, float)) and theirs > mine:
            out[key] = theirs
    if "input_tokens" in out or "output_tokens" in out:
        out["total_tokens"] = int(out.get("input_tokens") or 0) + int(out.get("output_tokens") or 0)
    return out


async def _timing(session: Any, run: Run, elapsed_ms: int, now: datetime) -> dict[str, int]:
    """wall：从第一次开始到现在的墙钟；active：各段执行时长之和；wait：等人审批的总时长。

    等待按事件算：每段从 run.interrupted 到下一条 run.resumed。服务重启挂起的那段
    没有 run.interrupted，不算等审批；失败后到接着跑之间也不算。
    """
    prev = run.usage or {}
    before = prev.get("active_ms", prev.get("duration_ms", 0))
    active = int(before or 0) + elapsed_ms

    rows = await session.execute(
        select(RunEvent.type, RunEvent.ts)
        .where(RunEvent.run_id == run.id,
               RunEvent.type.in_([EventType.RUN_INTERRUPTED, EventType.RUN_RESUMED]))
        .order_by(RunEvent.seq)
    )
    waited, since = 0.0, None
    for kind, ts in rows:
        if kind == EventType.RUN_INTERRUPTED:
            since = ts if since is None else since
        elif since is not None:
            waited += max(0.0, ts - since)
            since = None
    if since is not None:
        waited += max(0.0, now.timestamp() - since)

    wall = int((now - run.started_at).total_seconds() * 1000) if run.started_at else active
    return {"wall_ms": max(wall, active), "active_ms": active, "wait_ms": int(waited * 1000)}


run_manager = RunManager()
