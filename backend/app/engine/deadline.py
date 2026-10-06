"""这一段执行的截止时刻：核查节点据此在运行的时长上限之内收尾，不让整次运行被中止。

runner 给每一段执行（两次人工介入之间的那一段）套一道 asyncio.timeout(max_run_seconds)，到点整次运行判失败。核查
节点一个就能把这一段的时间用光：反方审稿逐句串行反查，默认 5 句、每句 90 秒再加 30 秒收尾宽限，最坏就是 600 秒；双盲
复算每项 120 秒加宽限，指标多了也一样。被中止时节点里装着反查、盲算的那个 @task 还没做完、没进 checkpoint，「继续
运行」把它从头再做一遍，多花一份钱，再次超时。

所以 runner 进入时长上限时把截止时刻放进这里的 ContextVar（segment），退出时清掉：节点、节点里的 @task、子工作流都
在这个上下文里执行，都看得到。核查节点每开始一项（一句反查、一项盲算）之前问 allot()：给下游留出余量之后还够不够
再做一项，这一项的时长上限收紧到多少；不够就不再开始，剩下的照各自「没能核查」的路径记下来，节点正常收尾。决定都
发生在节点自己的 @task 里，接着运行时取 checkpoint 里的结果，重放确定。

截止时刻用事件循环的钟（loop.time()），和 asyncio.timeout 同一个钟。没有上限时（不经 runner 执行的节点：单元测试、
审批质询接口）为 None，allot() 原样放行，节点的行为和没有这道检查时一样。

因此没核成的，节点发日志时带上 cause: RUN_DEADLINE 和这一段的总时长（run_seconds），另起一条，不和别的原因混在一起：
界面据此说「运行时长上限将至」、给对的下一步——调高核查自己的上限只会更快撞线，检查模型、数据源也无济于事。
"""
from __future__ import annotations

import asyncio
import contextlib
import math
from collections.abc import AsyncIterator, Callable
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from app.core.events import EventType
from app.engine.schema import GraphSpec, NodeType

#: 给下游节点留的余量，按份算。一份给收尾的节点：核查节点后面通常还有成果节点（出具契约复核报告），核查把时间用到
#: 最后一刻，后面的节点一开始就撞上时长上限，整次运行照样被中止。一份取 RESERVE_S 和这一段总时长的 RESERVE_SHARE 中
#: 大的：60 秒够成果节点复核、再做一两次普通的模型调用；管理员调大了时长上限的，多半是因为节点本来就慢，余量跟着放大
RESERVE_S = 60.0
RESERVE_SHARE = 0.1
#: 这一段不到 RESERVE_S / SHORT_SHARE（240 秒）时，一份按总时长的 SHORT_SHARE 算：时长上限调得很短（比如 180 秒）时，
#: 固定的 60 秒余量加上收尾宽限，前面的节点用掉一分钟核查就一项都开始不了，比没有这道检查时还差
SHORT_SHARE = 0.25
#: 下游这一段里每个还要调模型的节点再各加一份：Copilot 指南里的惯常接法是「口径卡 → 双盲复算 → 写报告 → 反方审稿 →
#: 成果」，双盲复算只留一份的话，写报告那次模型调用一慢，运行就在写报告时被中止。反方审稿也记一份：默认 600 秒的一段
#: 一份是 60 秒，正好够它开始一句（它自己那份余量之外，还要收尾宽限和最短的查库时长各 30 秒）。估得粗，宁可多留：
#: 核查少做几项记成没核成，好过整次运行被中止
MODEL_NODES = frozenset({NodeType.LLM, NodeType.AGENT, NodeType.SUPERVISOR, NodeType.SUBGRAPH, NodeType.REPORT,
                         NodeType.DUAL_CHECK, NodeType.CHALLENGE})
#: 余量合计最多占这一段总时长的这么多：下游节点再多，核查节点也总还能开始几项
RESERVE_CAP = 0.5
#: 日志里的 cause：这几项没核成是因为运行时长上限将至（不再开始、收紧后到了点）
RUN_DEADLINE = "run_deadline"
#: 收紧过的核查到了点：核查 agent 自己发的 verify_limit 改成这一句（relabel）
SQUEEZED_LIMIT = ("核查已达上限（本次运行即将达到 {seconds:g} 秒的时长上限，时长已按剩余时间收紧到 {timeout:g} 秒），"
                  "已停止查询，按已有结果收尾")


@dataclass(frozen=True)
class Segment:
    """这一段执行：截止时刻（事件循环时间）和总时长（秒，说明里写、按它算余量）。"""

    deadline: float
    seconds: float


_SEGMENT: ContextVar[Segment | None] = ContextVar("agentlab_segment", default=None)


@contextlib.asynccontextmanager
async def segment(deadline: float | None, seconds: float) -> AsyncIterator[None]:
    """runner 用：这一段执行的截止时刻（asyncio.Timeout.when()）和总时长，退出时清掉。写成异步的上下文管理器，好和
    时长上限写在同一个 async with 里。deadline 为 None（没有上限）时放 None：里面看不到外面的截止时刻。"""
    token = _SEGMENT.set(Segment(float(deadline), float(seconds)) if deadline is not None else None)
    try:
        yield
    finally:
        _SEGMENT.reset(token)


def current() -> Segment | None:
    """这一段执行的截止时刻；不在 runner 的执行里时为 None。"""
    return _SEGMENT.get()


def reserve(seconds: float, after: int = 0) -> float:
    """给下游节点留的余量（秒）：一份给收尾的节点，after 是下游这一段里还要调模型的节点数（models_after），各再加一份；
    合计不超过总时长的 RESERVE_CAP。一份多大、为什么，见 RESERVE_S、SHORT_SHARE、MODEL_NODES。"""
    share = max(min(RESERVE_S, SHORT_SHARE * seconds), RESERVE_SHARE * seconds)
    return min(share * (1 + max(after, 0)), RESERVE_CAP * seconds)


def models_after(spec: GraphSpec, node_id: str) -> int:
    """这个节点下游、这一段执行里还要调模型的节点（MODEL_NODES）有几个。沿连线往下走，人工审批节点不往后走：审批之后是
    新的一段，重新计时。分支汇合、环路里的节点只算一次，自己不算；分支上不一定执行的也算上，宁可多留。"""
    nodes = spec.node_map()
    children: dict[str, list[str]] = {}
    for edge in spec.edges:
        children.setdefault(edge.source, []).append(edge.target)
    seen: set[str] = {node_id}
    stack = list(children.get(node_id, []))
    count = 0
    while stack:
        cur = stack.pop()
        node = nodes.get(cur)
        if cur in seen or node is None:
            continue
        seen.add(cur)
        if node.type == NodeType.HUMAN:
            continue
        if node.type in MODEL_NODES:
            count += 1
        stack.extend(children.get(cur, []))
    return count


def _now() -> float:
    return asyncio.get_running_loop().time()


@dataclass(frozen=True)
class Allot:
    """allot() 的答复。start：够不够再开始一项。timeout_s：这一项的时长上限，没收紧时就是传进来的那个值（不限时为
    None）。tightened：按剩余时间收紧过（比配置的短，或者配置的是不限）。seconds：这一段的总时长，不再开始、收紧过的
    时候说明里写；没有上限时为 None。"""

    start: bool
    timeout_s: float | None
    tightened: bool = False
    seconds: float | None = None


def allot(timeout_s: float | None, *, grace: float, after: int = 0) -> Allot:
    """开始一项核查之前问一句：够不够，这一项的时长上限收紧到多少。timeout_s 是这一项配置的时长上限（None 不限），
    grace 是到点之后收尾那次调用的宽限（verify_agent.SETTLE_GRACE_S），after 是下游这一段里还要调模型的节点数
    （models_after，按它多留余量）。

    一项最坏要用「时长上限 + 收尾宽限」。剩下的时间先扣掉给下游的余量、再扣掉收尾宽限，就是这一项查库最多能用的时长：
    比配置的短就收紧到它（取到 0.1 秒，往下取：时间线上写「时长上限 69.7 秒」，不写一串小数）。收紧之后不到
    min(配置的时长上限, 收尾宽限) 就不再开始：收尾宽限是留给一次模型调用的时间，查库的时长比它还短，连一轮都做不完，
    只会白花一次收尾的钱、记成没能核查。配置的时长上限本来就比它短的，按配置的整段算；配置的是不限的，按收尾宽限算。
    没有上限时原样放行。"""
    found = _SEGMENT.get()
    if found is None:
        return Allot(True, timeout_s)
    room = math.floor((found.deadline - _now() - reserve(found.seconds, after) - grace) * 10) / 10
    limit = room if timeout_s is None else min(timeout_s, room)
    floor = grace if timeout_s is None else min(timeout_s, grace)
    if limit < floor:
        return Allot(False, None, seconds=found.seconds)
    return Allot(True, limit, tightened=timeout_s is None or room < timeout_s, seconds=found.seconds)


def relabel(emit: Callable[..., Any], allot: Allot) -> Callable[..., Any]:
    """交给核查 agent 的 emit。这一项的时长收紧过时，它到点自己发的 verify_limit 写的是收紧后的秒数（「时长上限 52.3
    秒」），和任何设置都对不上，读的人会去找这个数是在哪儿配的、甚至去调大它：改说运行时长上限将至，带上 cause 和这一段
    的总时长。别的事件、别的上限原样转发；没收紧时原样交回 emit。"""
    if not allot.tightened:
        return emit

    def wrapped(kind: Any, **data: Any) -> Any:
        if str(kind) == EventType.LOG and data.get("code") == "verify_limit" and data.get("limit") == "timeout_s":
            data = {**data, "cause": RUN_DEADLINE, "run_seconds": allot.seconds,
                    "message": SQUEEZED_LIMIT.format(seconds=allot.seconds, timeout=allot.timeout_s)}
        return emit(kind, **data)

    return wrapped


__all__ = ["MODEL_NODES", "RESERVE_CAP", "RESERVE_S", "RESERVE_SHARE", "RUN_DEADLINE", "SHORT_SHARE", "Allot",
           "Segment", "allot", "current", "models_after", "relabel", "reserve", "segment"]
