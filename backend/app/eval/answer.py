"""评测里的一轮作答：无头地走一遍问数据页走的那一轮（细案 01 做法 1、9；总方案 2.1）。

**量的就是用户拿到的那一轮。** 不另写一份编排：一轮的流程是 app/chat/drive.TurnDriver（问数据的一轮在服务端跑也是它，
app/chat/job.py），生成过程是 api/copilot.answer_ops，几个「决定」是 chat/turn 的纯函数，复核是 api/copilot.review_of_run。这里
只写评测和问数据不一样的那几项（Driver 是 TurnDriver 的子类）：

    answer_ops → final.next（chat/turn.next_step）→ launch_run(origin=eval) → 等终态 → review_of_run → 要的话带原图重来一次

- 生成：问法就是 instruction，intent=answer，限定的数据源同题目；助手模型按变体覆盖（GenerateIn.provider / model，挑表、
  复核跟着换）；读哪份目录按变体（answer_ops 的 catalog_view，查看的表详情、自查的 SQL 检查器都按它）。
- 结局：reply 直接回答、asked 反问、fail 判失败（建图失败）、ready 只建不跑、retry（老服务端那条路：没自查过的图带着原因
  重来一次）、run 运行。生成过程交出 error（助手本轮未完成）、开流前就拒绝（模型没配好、数据源不在）都是建图失败，另记
  「助手调用失败」——连续几题都这样就止损（runner.py）；只有生成过程一个操作都没给（只写了思考，error.next=retry）时按
  同一个规矩先带着原因重来一次，还不行才记建图失败。每次重来的原因照实记在 retries。
- 运行：fill_input 填入口字段（单轮，history 为空），来源 eval，名字「评测：题集名·第 n 题」，选项带目录视图（Run.options
  的 catalog_view：运行时的 db_schema、冻结快照、自查都按它）、只读和遮罩（同使用者的问数据运行：评测页会显示答案里的数，
  遮罩值不能从这里露出来；评测不写业务库）。写作模型按变体写进图的 defaults：节点自己点了模型的不覆盖，照实记在
  own_models。
- 终态：停在审批上（危险工具、人工节点）的直接取消、记「评测不代批」；被驳回的（cancelled + end_reason=rejected，总方案
  X9）记路径「被驳回」，不算运行失败；失败、被停止记运行失败。
- 复核：总是等完（问数据页超过 25 秒不等，评测里照等，复核用时另记，超过 25 秒的单列，见 stats.py）。复核给了
  retry_instruction 就带着这次运行的图重来一次（chat/turn 的规则，最多试 MAX_ATTEMPTS 次）。

用量按角色记（做法 3、11）：助手 = 生成（answer_ops 末尾的 usage）+ 复核（review_of_run 的 usage）；写作 = 答案运行里的
llm.end（按模型分）。重来的话两次都算——那都是这一题花的钱。

时限、停止由调用方管（runner.py 用 asyncio.wait_for 包住 drive）：这里随时把正在跑的那次运行记在 current_run，调用方
超时、停止时据此取消它（stop_run）。
"""
from __future__ import annotations

import copy
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select

# 一轮的流程和结果抽到了 chat/drive.py（问数据、评测共用），这里留着老名字：runner、stats 和测试照旧从这里取
from app.chat.drive import CANCEL_GRACE_S, Turn, TurnDriver, add_usage  # noqa: F401

logger = logging.getLogger(__name__)

#: 问数据页最多等复核这么久（chat/job.REVIEW_WAIT_S，和以前页面上的 REVIEW_TIMEOUT_MS 同一个数）：评测照等，超过的单列
REVIEW_PAGE_WAIT_S = 25.0
#: 运行停在审批上：评测不代批
APPROVAL_REASON = "运行停在审批上（危险工具、人工节点）：评测不代批，已取消这次运行"
#: 每一步在做什么（EvalResult.current）
PHASE_TEXT = {"gold": "正在算期望", "build": "正在建图", "run": "运行中", "review": "正在复核", "score": "正在评分",
              "gold_after": "作答后复算期望", "judge": "正在请裁判判断"}


@dataclass
class TurnContext:
    """这一轮作答的来历（记进运行 origin）和要用的东西。"""

    suite_id: str
    suite_name: str
    eval_run_id: str
    case_id: str | None
    case_seq: int | None
    actor: str | None
    arm: str | None = None
    repeat: int = 0
    #: 变体：助手模型 {provider, model}、写作模型 {provider, model}
    assistant: Mapping[str, Any] = field(default_factory=dict)
    writer: Mapping[str, Any] = field(default_factory=dict)
    #: 目录视图（Run.options['catalog_view'] 的形状）
    view: Mapping[str, Any] | None = None


async def writer_usage(run_ids: list[str]) -> dict[str, Any]:
    """答案运行里的模型调用（llm.end，按模型分）：写作这一角色的用量，形状同 providers/usage 的汇总。封存之后追加的
    （按需裁判、质询）不算：那是事后另记的账（裁判记在评测的账上另算，见 judging.py）。"""
    from app.core.events import EventType
    from app.db.base import SessionLocal
    from app.db.models import RunEvent
    from app.providers.catalog import unpriced_call

    out = add_usage(None, None)
    if not run_ids:
        return out
    async with SessionLocal() as session:
        rows = (await session.execute(select(RunEvent.data).where(
            RunEvent.run_id.in_(run_ids), RunEvent.type == EventType.LLM_END))).scalars().all()
    for data in rows:
        data = data if isinstance(data, dict) else {}
        if data.get("post_seal"):
            continue
        model = str(data.get("model") or "unknown")
        piece = {"input_tokens": int(data.get("input_tokens") or 0), "output_tokens": int(data.get("output_tokens") or 0),
                 "calls": int(data.get("calls") or 1), "cost_usd": float(data.get("cost_usd") or 0.0),
                 "unpriced_calls": 1 if unpriced_call(data) else 0}
        out = add_usage(out, {**piece, "by_model": {model: piece}})
    return out


def with_writer(graph: dict[str, Any], writer: Mapping[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """写作模型按变体写进图的 defaults（只写给了的那几项）。返回 (图, 自己点了模型、不会被覆盖的节点 id)。"""
    provider, model = writer.get("provider"), writer.get("model")
    if not (provider or model):
        return graph, []
    out = copy.deepcopy(graph)
    defaults = dict(out.get("defaults") or {})
    if provider:
        defaults["provider"] = provider
    if model:
        defaults["model"] = model
    out["defaults"] = defaults
    own = []
    for node in out.get("nodes") or []:
        config = ((node or {}).get("data") or {}).get("config") or {}
        if isinstance(config, Mapping) and (config.get("provider") or config.get("model")):
            own.append(str(node.get("id")))
    return out, own


def _options(view: Mapping[str, Any] | None) -> dict[str, Any]:
    """作答运行带的选项：目录视图、只读、遮罩（见模块说明）。"""
    from app.data.catalog_view import OPTION_KEY
    from app.tools.datasource import MASK_RESULTS_OPTION, READ_ONLY_OPTION

    out: dict[str, Any] = {MASK_RESULTS_OPTION: True, READ_ONLY_OPTION: True}
    if view:
        out[OPTION_KEY] = dict(view)
    return out


def run_name(suite_name: str, case_seq: int | None) -> str:
    return f"评测：{suite_name or '题集'}·第 {case_seq or '?'} 题"


async def stop_run(run_id: str | None, actor: str | None = None) -> None:
    """取消一次还没结束的运行、等它收尾（超时、停止评测、停在审批上时）。已经结束的什么都不做。"""
    from app.engine.runner import run_manager

    if not run_id:
        return
    try:
        await run_manager.cancel(run_id, actor=actor)
        await run_manager.wait_idle(run_id, timeout=CANCEL_GRACE_S)
    except Exception:  # noqa: BLE001 - 取消是收尾，出错只记日志
        logger.exception("评测：取消运行 %s 时出错", run_id)


class Driver(TurnDriver):
    """一轮作答（流程在 TurnDriver）。drive() 跑完一轮，结果在 turn 上；on_phase(阶段) 在每一步开始时调（runner 据此写
    「正在做什么」）。和问数据不一样的：助手模型、目录视图按变体；写作模型按变体写进图；来源 eval、只读和遮罩；停在审批上
    直接取消（评测不代批）；复核一直等。"""

    def __init__(self, question: str, *, datasource_ids: list[str], ctx: TurnContext,
                 on_phase: Callable[[str], Awaitable[None]] | None = None) -> None:
        super().__init__(question, datasource_ids=datasource_ids, on_phase=on_phase,
                         provider=ctx.assistant.get("provider"), model=ctx.assistant.get("model"))
        self.ctx = ctx

    def catalog_view(self) -> Any:
        return self.ctx.view

    def prepare_graph(self, graph: dict[str, Any]) -> dict[str, Any]:
        graph, own = with_writer(graph, self.ctx.writer)
        self.turn.own_models = sorted(set(self.turn.own_models) | set(own))
        return graph

    async def launch_args(self, session: Any, graph: dict[str, Any]) -> dict[str, Any]:
        origin = {k: v for k, v in {
            "suite_id": self.ctx.suite_id, "eval_run_id": self.ctx.eval_run_id, "case_id": self.ctx.case_id,
            "role": "answer", "arm": self.ctx.arm, "repeat": self.ctx.repeat, "attempt": self.turn.attempts}.items()
            if v is not None}
        return {"actor": self.ctx.actor, "options": _options(self.ctx.view), "origin_kind": "eval", "origin": origin,
                "workflow_name": run_name(self.ctx.suite_name, self.ctx.case_seq)}

    async def on_interrupted(self, run_id: str, row: Any) -> Any | None:
        t = self.turn
        waiting = await self._pending_approvals(run_id)
        await stop_run(run_id, self.ctx.actor)
        t.path = "run_failed"
        t.reason = APPROVAL_REASON if waiting else "运行停在断点上（服务可能重启过）：评测不继续这次运行，已取消"
        return None

    async def drive_workflow(self, graph: dict[str, Any], inputs: dict[str, Any]) -> Turn:
        """工作流题：钉住那一版的图按输入跑一次探索运行（来源 eval）。不建图、不复核：考的是这一版本身。不发正式运行——
        正式运行会被 pick_base 和应用视图的「最近一次封存」取到（同期望探针不发正式运行的理由）。"""
        import time

        t = self.turn
        started = time.monotonic()
        try:
            await self._phase("run")
            if await self._run(graph, inputs) is not None:
                t.path = "ran"
            return t
        finally:
            t.timings["total_ms"] = int((time.monotonic() - started) * 1000)


__all__ = ["APPROVAL_REASON", "Driver", "PHASE_TEXT", "REVIEW_PAGE_WAIT_S", "Turn", "TurnContext", "add_usage",
           "run_name", "stop_run", "with_writer", "writer_usage"]
