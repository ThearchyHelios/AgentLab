"""期望答案：SQL 探针、口径探针、从运行认定（细案 01 做法 8、14；评测-D4）。

**期望不靠人填数，也不冻结数值。** 每次要期望（保存题目时试算一次、评测时作答前后各一次）都在业务库上重算一遍：

- SQL 题、从运行认定：建一张「入口 → 调用工具（db_query__数据源，args.sql = 按截至时间渲染好的 SQL）→ 出口」的裸图，
  以来源 eval 发起探索运行。期望值取这次运行封存的查询快照里指定的格、列或整张表（cases.py 的 expectations）。从运行认定
  的题和 SQL 题算法一样，只是 SQL 出自某次运行里的一条查询、记着谁在什么时候认定的。
- 分析期望（贡献、判定）：在同一份查询快照上用平台的算子算一次分析（app.analysis.run，和作答里的分析工具、分析卡同一个
  函数；随数据变化的调用错误记成算不了，refuse_data_errors），判定就是期望（app/eval/judgements.py）。阈值用这次评测的
  分析设置快照（Context.analysis，和作答的运行同一份；试算时现读设置），遮罩列、目录事实、现查表的日期范围和分析工具
  同一套取法（app/tools/analysis.py）。算不了的这一项照实记（评分时不能评分），参数和期望 SQL 的结果对不上（列不在、期次
  不在）的整题没有可用的值。
- 口径题：app/ledger/derive.caliber_probe 从钉住版本拷出口径卡读的取数节点、接一张 caliber_from 固定到那一版的口径卡，
  跑探索运行，期望值就是口径卡的指标（取整、单位、比率都由口径卡自己定）。不可复用的卡拒收并给原因。

为什么不发起钉版本的正式运行：会被 pick_base 和应用视图的「最近一次封存」取到，正式运行也不该为了评测而发；为什么不引用
最近一次封存的正式运行：数据一直在变，期望会过期；为什么不直接调 run_query：期望没有封存快照，事后说不清当时查的是什么。

**守住的几条**（每次都查，不只保存时）：
- 只读：渲染好的 SQL 先过只读守卫（data/guard.check，readonly=True）；运行再带上 read_only_data（可写源上的写语句也由
  守卫拒掉）和 mask_results 两个选项，同使用者的问数据运行。
- 遮罩：查询结果在工具边界就按 engine/masking 的派生遮罩遮好（按 SQL 认出改了名字的遮罩列，认不全的整份遮）。快照里有
  任何一列、一格是遮掉的，期望就拒收——评测页会显示期望值，遮罩值不能从这里露出来。认法和证据面板同一套，认不准就拒。
- 照实说：取不到值（列不在、行不够、空值、该没有数据的却有数据）、运行失败、超时、停在人工确认上（评测不代批，直接
  取消），各写各的原因；不混进「答错」。

**变量**：问法、期望 SQL、口径输入里的 {{ schedule.* }} 用定时任务那份 VARIABLES / template_values，按评测的「截至时间」
在题集的时区渲染（cases.variable_values）。

**口径升版**（做法 14）：口径题钉 workflow_id@version。每次算期望之前，对探针图调一次 engine/governance.
unresolved_caliber_upgrades（和正式运行用的是同一个判断），再按 apps/versions.subgraph_pin_problems 看钉住的版本退役
没有。有了新的已发布版本、或者钉住的版本退役了，又没有处置到那一版，这道题就跳过，写「口径已升版到 vN，待处置」。
题目上三选一（dispose），没有缺省：
- recompute：改钉新版本，历史在这里断开（题目指纹跟着变，和上一次比时标不可比）；
- dual：新旧两版各算一份期望，都报（旧的那一份照旧可比）；
- incomparable：旧题归档，按新版本另建一道新题、重开基线。

**作答前后各算一次**（bracket / settle，第 3 波的执行调它）：两次一样才判；不一样就记「作答期间数据变了，不判」。A/B 两臂
共用同一次算出的期望：在 answer 里同时跑两臂即可。两次比的是期望值本身：分析结论里随运行当天、现查时刻、输入快照的
工件变的几项不比（judgements.steady）。
"""
from __future__ import annotations

import asyncio
import copy
import dataclasses
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, TypeVar

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.eval.cases import CALIBER_NOT_REUSABLE, GOLD_GUARD, NEEDS_GOLD, render, variable_values

logger = logging.getLogger(__name__)
T = TypeVar("T")

#: 结论：ok 算出来了；skipped 口径升版待处置（跳过这道题）；refused 规矩不许（守卫、遮罩、卡不能复用）；no_value 运行
#: 好好的、按期望取不到值；failed 运行没成（SQL 错、数据源用不了、停在人工确认上）；timeout 超时没算完（已停止那次运行）
STATUSES = ("ok", "skipped", "refused", "no_value", "failed", "timeout")
#: 机器码（试算拒收时接口回 422 的 code）
GOLD_MASKED = "eval_gold_masked"
GOLD_NO_VALUE = "eval_gold_no_value"
GOLD_FAILED = "eval_gold_failed"
GOLD_TIMEOUT = "eval_gold_timeout"
UPGRADE_PENDING = "eval_caliber_upgrade_pending"
#: 作答前后两次期望不一样
GOLD_DRIFTED = "eval_gold_drifted"

#: 两次算期望的角色（记进运行 origin 的 role）：作答前、作答后
ROLES = ("gold", "gold_after")
ROLE_LABEL = {"gold": "算期望", "gold_after": "作答后复算期望"}
#: 保存题目时试算一次等多久（秒）。评测里每题的时限另有预算（per_case_timeout_s）
TRIAL_TIMEOUT_S = 120.0
#: 超时停止那次运行之后，最多再等它收尾多久
CANCEL_GRACE_S = 15.0
#: SQL 探针里调用工具节点的 id
QUERY_NODE = "gold_q"

#: 口径升版的三种处置（评测里的说法；节点上的 upgrade_policy 叫法见 engine/labels）
UPGRADE_LABEL = {"recompute": "改为固定新版本（历史在这里断开）", "dual": "新旧两版各算一份期望",
                 "incomparable": "旧题归档，按新版本另建新题"}


# ==========================================================================
# 结果的形状
# ==========================================================================


@dataclass
class GoldSet:
    """一次探针算出来的一份期望。version 是口径题这一份按的版本（SQL 题为空）；values 是 {期望 id: 取到的值}。"""

    run_id: str
    snapshot: str | None
    values: dict[str, dict[str, Any]]
    version: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"run_id": self.run_id, "snapshot": self.snapshot, "version": self.version,
                "values": copy.deepcopy(self.values)}

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> GoldSet:
        return cls(run_id=str(raw.get("run_id") or ""), snapshot=raw.get("snapshot"),
                   values=copy.deepcopy(dict(raw.get("values") or {})), version=raw.get("version"))


@dataclass
class GoldOutcome:
    """一次算期望的结果。sets：一份（SQL 题、口径题）或两份（口径题选了 dual：旧版在前、新版在后）。run_ids 是这次发起的
    全部运行（失败的也在，证据点得开）。rendered 是按截至时间渲染好的问法、SQL、口径输入。"""

    status: str
    as_of: str
    reason: str | None = None
    code: str | None = None
    sets: list[GoldSet] = field(default_factory=list)
    run_ids: list[str] = field(default_factory=list)
    rendered: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    def to_dict(self) -> dict[str, Any]:
        return {"status": self.status, "as_of": self.as_of, "reason": self.reason, "code": self.code,
                "sets": [s.to_dict() for s in self.sets], "run_ids": list(self.run_ids),
                "rendered": copy.deepcopy(self.rendered)}

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> GoldOutcome:
        return cls(status=str(raw.get("status") or "failed"), as_of=str(raw.get("as_of") or ""),
                   reason=raw.get("reason"), code=raw.get("code"),
                   sets=[GoldSet.from_dict(s) for s in raw.get("sets") or []],
                   run_ids=list(raw.get("run_ids") or []), rendered=copy.deepcopy(dict(raw.get("rendered") or {})))

    def summary(self) -> dict[str, Any]:
        """记进题目 trial 的那一份：不带期望值（期望每次重算，不冻结数值）。"""
        return {"status": self.status, "code": self.code, "reason": self.reason, "as_of": self.as_of,
                "run_ids": list(self.run_ids)}


def _outcome(status: str, as_of: datetime, reason: str | None = None, code: str | None = None, **kw: Any) -> GoldOutcome:
    return GoldOutcome(status=status, as_of=as_of.isoformat(), reason=reason, code=code, **kw)


@dataclass(frozen=True)
class Context:
    """算期望时题目以外的那几样：截至时间、时区、定时的 cron（手动发起为空）、发起人、记进运行 origin 的来历。"""

    as_of: datetime
    tz: str = "Asia/Shanghai"
    cron: str | None = None
    actor: str | None = None
    suite_id: str | None = None
    suite_name: str = ""
    eval_run_id: str | None = None
    role: str = "gold"
    #: 记进运行 origin 的其余字段（臂、第几次重复、试算……）
    extra: Mapping[str, Any] = field(default_factory=dict)
    timeout_s: float = TRIAL_TIMEOUT_S
    #: 这次评测的分析设置快照（runner 开评时拍一份，作答的运行带同一份）；没有（试算）时现读设置
    analysis: Mapping[str, Any] | None = None

    def variables(self) -> dict[str, str]:
        return variable_values(self.as_of, self.tz, self.cron)


def _run_name(case: Mapping[str, Any], ctx: Context) -> str:
    which = f"第 {case['seq']} 题" if case.get("seq") else "新题"
    what = "试算" if ctx.extra.get("trial") else ROLE_LABEL.get(ctx.role, "算期望")
    return f"评测：{ctx.suite_name or '题集'}·{which}·{what}"


def _origin(case: Mapping[str, Any], ctx: Context, **more: Any) -> dict[str, Any]:
    data = {"suite_id": ctx.suite_id, "eval_run_id": ctx.eval_run_id, "case_id": case.get("id"), "role": ctx.role,
            **dict(ctx.extra), **more}
    return {k: v for k, v in data.items() if v is not None}


# ==========================================================================
# 发起探针、等它结束
# ==========================================================================


def _probe_options() -> dict[str, Any]:
    """探针运行带的选项：查询一律只读、结果在工具边界就按遮罩遮好（同使用者的问数据运行）。"""
    from app.tools.datasource import MASK_RESULTS_OPTION, READ_ONLY_OPTION

    return {MASK_RESULTS_OPTION: True, READ_ONLY_OPTION: True}


@dataclass
class _Ran:
    status: str
    run_id: str | None
    reason: str | None = None
    code: str | None = None


async def _launch_and_wait(graph: dict[str, Any], payload: dict[str, Any], *, name: str, origin: dict[str, Any],
                           ctx: Context) -> _Ran:
    """发起一次探索运行（来源 eval），等它结束（最多 ctx.timeout_s 秒）。"""
    from app.db.base import SessionLocal
    from app.db.models import Run
    from app.engine.launch import LaunchRefused, launch_run
    from app.engine.runner import run_manager

    try:
        async with SessionLocal() as session:
            run = await launch_run(session, graph=graph, input_payload=payload, actor=ctx.actor,
                                   options=_probe_options(), origin_kind="eval", origin=origin, workflow_name=name)
            run_id = run.id
    except LaunchRefused as e:
        return _Ran("failed", None, f"算期望的运行没有发起：{e.message}", GOLD_FAILED)
    from app.cluster import slots

    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(0.0, float(ctx.timeout_s))
    last = loop.time()
    while True:
        # 执行任务收完尾（含封存）才回来；任务已经不在了（刚建好还没排上、或者已经结束）就立刻回来，再按库里的状态判
        await run_manager.wait_idle(run_id, timeout=max(0.05, min(1.0, deadline - loop.time())))
        now = loop.time()
        if await slots.waiting(run_id, here=run_manager.is_active(run_id)):
            # 排队等名额的那一段不算时限（复核：满载时排队不该算进每题时限）
            deadline += now - last
        last = now
        async with SessionLocal() as session:
            row = await session.get(Run, run_id)
            status, error = (row.status, row.error) if row is not None else ("failed", "运行记录不见了")
        if status in ("succeeded", "failed", "cancelled", "interrupted") or loop.time() >= deadline:
            break
        await asyncio.sleep(min(0.2, max(0.0, deadline - loop.time())))
    if status == "succeeded":
        return _Ran("ok", run_id)
    if status in ("failed", "cancelled"):
        return _Ran("failed", run_id, f"算期望的运行{'失败' if status == 'failed' else '被停止'}：{error or '没有记下原因'}",
                    GOLD_FAILED)
    stopping = status == "interrupted"
    await run_manager.cancel(run_id, actor=ctx.actor)
    await run_manager.wait_idle(run_id, timeout=CANCEL_GRACE_S)
    if stopping:
        return _Ran("failed", run_id, "算期望的运行停在人工确认上：评测不代批，已取消这次运行", GOLD_FAILED)
    return _Ran("timeout", run_id, f"算期望的运行超过 {ctx.timeout_s:g} 秒还没结束，已停止这次运行", GOLD_TIMEOUT)


async def _artifacts(run_id: str, kind: str) -> list[tuple[str | None, tuple[str, Any]]]:
    """这次运行落下的某一种工件：[(节点 id, (工件 id, 内容))]，按写入的先后。内容和哈希对不上的内容为 None。"""
    from app.core import artifact_store
    from app.db.base import SessionLocal
    from app.db.models import Artifact

    async with SessionLocal() as session:
        rows = (await session.execute(select(Artifact.id, Artifact.node_id).where(
            Artifact.run_id == run_id, Artifact.kind == kind).order_by(Artifact.created_at, Artifact.id))).all()
    out: list[tuple[str | None, tuple[str, Any]]] = []
    for artifact_id, node_id in rows:
        try:
            out.append((node_id, (artifact_id, artifact_store.load(artifact_id))))
        except ValueError:
            out.append((node_id, (artifact_id, None)))
    return out


def masked_problem(snapshot: Mapping[str, Any]) -> str | None:
    """查询快照里有遮掉的列、格（查询时就按遮罩遮好的，engine/masking.mask_query_result）：给人看的原因；没有为 None。"""
    if snapshot.get("masked_whole"):
        return ("期望 SQL 的写法认不全哪些结果取自遮罩列，整份结果都按遮罩处理：期望答案不能用遮罩列（评测页会显示期望值）。"
                "请换一种写法（不要用 *、整行引用），只选不涉及遮罩列的列")
    columns = [str(c) for c in snapshot.get("masked_at_query") or []]
    if columns:
        return (f"期望 SQL 的结果里「{'、'.join(columns[:5])}」取自数据源设了遮罩的列：期望答案不能用遮罩列"
                "（评测页会显示期望值）。请去掉这几列")
    if snapshot.get("masked_cells"):
        return "期望 SQL 的结果里有和遮罩值相同的文字：期望答案不能用遮罩列（评测页会显示期望值）。请换一个不涉及遮罩列的问法"
    return None


# ==========================================================================
# 从快照、口径卡里取值
# ==========================================================================


class _NoValue(Exception):
    pass


def _column(columns: Sequence[str], name: str) -> int:
    if name in columns:
        return list(columns).index(name)
    lowered = [i for i, c in enumerate(columns) if c.lower() == name.lower()]
    if len(lowered) == 1:
        return lowered[0]
    raise _NoValue(f"期望 SQL 的结果里没有列「{name}」（结果的列：{'、'.join(columns) or '（没有）'}）")


def _same(cell: Any, wanted: Any) -> bool:
    if cell == wanted:
        return True
    if isinstance(cell, (int, float)) and isinstance(wanted, (int, float)) and not isinstance(cell, bool):
        return float(cell) == float(wanted)
    return cell is not None and str(cell) == str(wanted)


def _numeric(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _dims(columns: Sequence[str], row: Sequence[Any], skip: int) -> dict[str, Any]:
    """这一行的维度：非数值的格（评分按它和答案里那一格所在行配对，第 3 波）。"""
    return {c: row[i] for i, c in enumerate(columns) if i != skip and i < len(row) and row[i] is not None
            and not _numeric(row[i])}


def _sql_value(exp: Mapping[str, Any], snapshot: Mapping[str, Any]) -> dict[str, Any]:
    columns = [str(c) for c in snapshot.get("columns") or []]
    rows = [list(r) if isinstance(r, (list, tuple)) else [r] for r in snapshot.get("rows") or []]
    truncated = bool(snapshot.get("truncated"))
    kind = exp["kind"]
    if kind == "none":
        if rows:
            raise _NoValue(f"这道题期望「没有数据」，但期望 SQL 返回了 {len(rows)} 行")
        return {"kind": "none", "rows": 0}
    if kind == "scalar":
        ci = _column(columns, exp["column"])
        if exp.get("where"):
            keys = {_column(columns, k): v for k, v in exp["where"].items()}
            hits = [i for i, r in enumerate(rows) if all(i_ < len(r) and _same(r[i_], v) for i_, v in keys.items())]
            cond = "、".join(f"{k}={v}" for k, v in exp["where"].items())
            if not hits:
                raise _NoValue(f"期望 SQL 的结果里没有 {cond} 的行" + ("（结果被截断，只取回了前几行）" if truncated else ""))
            if len(hits) > 1:
                raise _NoValue(f"期望 SQL 的结果里 {cond} 的行有 {len(hits)} 行，无法确定是哪一行：请在 where 里多写一列")
            ri = hits[0]
        else:
            ri = int(exp.get("row") or 0)
            if ri >= len(rows):
                raise _NoValue("期望 SQL 没有返回行，没有可用的值：期望没有数据的题请把期望写成「没有数据」" if not rows else
                               f"期望 SQL 只返回了 {len(rows)} 行，没有第 {ri + 1} 行")
        value = rows[ri][ci] if ci < len(rows[ri]) else None
        if value is None:
            raise _NoValue(f"期望的那一格（第 {ri + 1} 行「{columns[ci]}」）是空值：期望没有数据的题请把期望写成「没有数据」")
        return {"kind": "scalar", "value": value, "column": columns[ci], "row": ri,
                "dims": _dims(columns, rows[ri], ci), **({"where": dict(exp["where"])} if exp.get("where") else {})}
    if truncated:
        raise _NoValue("期望 SQL 的结果被截断（只取回了前一部分行），多行、名单的期望算不准：请缩小范围或在 SQL 里汇总")
    if kind == "set":
        ci = _column(columns, exp["column"])
        values = [r[ci] if ci < len(r) else None for r in rows]
        if exp.get("top_n"):
            values = values[:exp["top_n"]]
        return {"kind": "set", "column": columns[ci], "values": values, "ordered": bool(exp.get("ordered"))}
    if kind == "table":
        key_idx = [_column(columns, k) for k in exp["keys"]]
        wanted = [_column(columns, c) for c in exp.get("columns") or columns]
        picked = rows[:exp["top_n"]] if exp.get("top_n") else rows
        seen: set[tuple[str, ...]] = set()
        out_rows: list[dict[str, Any]] = []
        for r in picked:
            key = tuple(str(r[i]) if i < len(r) else "" for i in key_idx)
            if key in seen:
                raise _NoValue(f"期望 SQL 的结果按「{'、'.join(exp['keys'])}」配对不唯一（{'、'.join(key)} 出现了不止一次）")
            seen.add(key)
            out_rows.append({columns[i]: (r[i] if i < len(r) else None) for i in dict.fromkeys(key_idx + wanted)})
        return {"kind": "table", "keys": [columns[i] for i in key_idx], "columns": [columns[i] for i in wanted],
                "rows": out_rows, "ordered": bool(exp.get("ordered"))}
    raise _NoValue(f"「{kind}」的期望不从期望 SQL 取值")


def _metric_value(exp: Mapping[str, Any], card: Mapping[str, Any]) -> dict[str, Any]:
    metric = next((m for m in card.get("metrics") or [] if m.get("id") == exp["metric"]), None)
    if metric is None:
        raise _NoValue(f"口径卡这次没有算出指标「{exp['metric']}」")
    if metric.get("value") is None:
        raise _NoValue(f"口径卡的「{metric.get('name') or metric['id']}」缺输入，没有值（取数没有返回需要的格）")
    if metric.get("incomplete"):
        raise _NoValue(f"口径卡的「{metric.get('name') or metric['id']}」{metric.get('incomplete_reason') or '基于被截断的查询结果'}"
                       "，期望算不准")
    out = {"kind": "scalar", "value": metric["value"], "metric": metric["id"], "name": metric.get("name") or metric["id"],
           "unit": metric.get("unit") or "", "decimals": metric.get("decimals"), "format": metric.get("format"),
           "rendered": metric.get("rendered"), "caliber": card.get("caliber"),
           "caliber_version": card.get("caliber_version")}
    if metric.get("sql_check_failed"):
        out["sql_check_failed"] = True
    return out


def _values(expectations: Sequence[Mapping[str, Any]], pick: Callable[[Mapping[str, Any]], dict[str, Any]]
            ) -> dict[str, dict[str, Any]]:
    return {e["id"]: pick(e) for e in expectations if e.get("kind") in NEEDS_GOLD}


# ==========================================================================
# 分析期望：在期望 SQL 的快照上算一次分析（app/eval/judgements.py）
# ==========================================================================


@dataclass
class _Analyses:
    """一道题的几项分析期望共用的那几样：输入快照（含遮罩列）、阈值、算子的运行期依赖。用到时才备（_prepared）。"""

    snapshot: dict[str, Any]
    artifact: str
    run_id: str
    ctx: Context
    ready: tuple[Any, dict[str, Any], Any] | None = None


async def _prepared(found: _Analyses) -> tuple[Any, dict[str, Any], Any]:
    """(算子的输入快照, 阈值, 运行期依赖)：遮罩列、目录事实、现查表的日期范围同分析工具（app/tools/analysis.py），按这次
    探针运行固定的数据版本解析数据源；阈值是这次评测的分析设置快照，没有（试算）现读设置。"""
    if found.ready is not None:
        return found.ready
    from app.db.base import SessionLocal
    from app.db.models import Run
    from app.eval.judgements import GOLD_REF
    from app.tools.analysis import context_of, input_snapshot, masked_of, settings_of
    from app.tools.registry import ToolContext

    inp = input_snapshot(found.snapshot, ref=GOLD_REF, artifact=found.artifact, node_id=QUERY_NODE,
                         masked=await masked_of(found.snapshot, found.artifact))
    async with SessionLocal() as session:
        row = await session.get(Run, found.run_id)
        versions = dict(row.data_versions) if row is not None and row.data_versions is not None else None
    tool_ctx = ToolContext(run_id=found.run_id, node_id=QUERY_NODE, data_versions=versions, mask_results=True,
                           force_readonly=True, fixed_scope=True)
    context = await context_of([inp], tool_ctx=tool_ctx)
    found.ready = (inp, await settings_of(found.ctx.analysis), context)
    return found.ready


async def _analysis_value(exp: Mapping[str, Any], at: int, found: _Analyses) -> dict[str, Any]:
    """一项分析期望的期望值（judgements.expected）。算不了（历史不够、本期不完整……）照实记成这一项的结论；参数和期望 SQL
    的结果对不上（列不在、期次不在、不是乘积关系……）抛 _NoValue（整题没有可用的值，试算时照实交回去改）。"""
    import app.analysis as analysis_ops   # 按模块取 run：测试换替身时只换模块上的这一个函数
    from app.analysis.contract import CallError
    from app.eval import judgements

    where = f"第 {at + 1} 个期望（{'贡献' if exp.get('kind') == 'contribution' else '判定'}）"
    if found.snapshot.get("truncated"):
        raise _NoValue("期望 SQL 的结果被截断（只取回了前一部分行），分析期望算不准：请缩小范围或在 SQL 里汇总")
    inp, settings, context = await _prepared(found)
    try:
        request = judgements.request_of(exp, found.ctx.variables(), found.snapshot)
    except judgements.ParamsInvalid as e:
        raise _NoValue(f"{where}算不出期望，{e.message}") from None
    try:
        result = await analysis_ops.run(request, lambda name: inp if name == judgements.GOLD_REF else None,
                                        settings=settings, context=context, refuse_data_errors=True)
    except CallError as e:
        raise _NoValue(f"{where}的分析参数和期望 SQL 的结果对不上：{e.message}") from None
    return judgements.expected(exp, result)


async def _sql_values(case: Mapping[str, Any], snapshot: dict[str, Any], artifact: str, run_id: str,
                      ctx: Context) -> dict[str, dict[str, Any]]:
    """SQL 题每个期望的值（按期望的先后）：取格、列、整张表的照快照取，分析期望在同一份快照上算。取不到抛 _NoValue。"""
    from app.eval.judgements import ANALYSIS_KINDS

    found = _Analyses(snapshot=snapshot, artifact=artifact, run_id=run_id, ctx=ctx)
    out: dict[str, dict[str, Any]] = {}
    for at, exp in enumerate(case["expectations"]):
        if exp.get("kind") not in NEEDS_GOLD:
            continue
        out[exp["id"]] = await _analysis_value(exp, at, found) if exp.get("kind") in ANALYSIS_KINDS \
            else _sql_value(exp, snapshot)
    return out


# ==========================================================================
# SQL 探针（SQL 题、从运行认定）
# ==========================================================================


def sql_probe_graph(source_name: str, sql: str) -> dict[str, Any]:
    """入口 → 调用工具（db_query__数据源，args.sql 是渲染好的字面 SQL）→ 出口。"""
    from app.tools.datasource import QUERY_PREFIX

    def node(nid: str, ntype: str, label: str, x: int, **config: Any) -> dict[str, Any]:
        return {"id": nid, "type": ntype, "position": {"x": x, "y": 0}, "data": {"label": label, "config": config}}

    nodes = [node("gold_in", "input", "入口", 0), node(QUERY_NODE, "tool", "期望 SQL", 260,
                                                       tool=f"{QUERY_PREFIX}{source_name}", args={"sql": sql}),
             node("gold_out", "output", "出口", 520)]
    return {"nodes": nodes, "edges": [{"source": a["id"], "target": b["id"]} for a, b in zip(nodes, nodes[1:])]}


async def _sql_gold(case: Mapping[str, Any], ctx: Context) -> GoldOutcome:
    from app.data.guard import SqlRejected, check
    from app.db.base import SessionLocal
    from app.db.models import DataSource

    gold = case["gold"]
    sql = render(gold["sql"], ctx.variables())
    rendered = {"sql": sql}
    async with SessionLocal() as session:
        source = await session.get(DataSource, gold["datasource_id"])
        name, kind, enabled = (source.name, source.kind, source.enabled) if source is not None else (None, None, False)
    if name is None:
        return _outcome("failed", ctx.as_of, "期望 SQL 查的数据源不存在，可能已被删除", GOLD_FAILED, rendered=rendered)
    if not enabled:
        return _outcome("failed", ctx.as_of, f"期望 SQL 查的数据源「{name}」已停用", GOLD_FAILED, rendered=rendered)
    try:
        check(sql, readonly=True, source_name=name, dialect=(kind or "").lower())
    except SqlRejected as e:
        return _outcome("refused", ctx.as_of, f"期望 SQL 没有通过只读守卫：{e}", GOLD_GUARD, rendered=rendered)
    ran = await _launch_and_wait(sql_probe_graph(name, sql), {}, name=_run_name(case, ctx), origin=_origin(case, ctx),
                                 ctx=ctx)
    runs = [ran.run_id] if ran.run_id else []
    if ran.status != "ok":
        return _outcome(ran.status, ctx.as_of, ran.reason, ran.code, run_ids=runs, rendered=rendered)
    snapshots = [(nid, item) for nid, item in await _artifacts(ran.run_id, "query_snapshot") if nid == QUERY_NODE]
    if not snapshots or snapshots[-1][1][1] is None:
        return _outcome("failed", ctx.as_of, "算期望的运行没有留下查询快照，期望无从取值", GOLD_FAILED, run_ids=runs,
                        rendered=rendered)
    artifact_id, snapshot = snapshots[-1][1]
    if problem := masked_problem(snapshot):
        return _outcome("refused", ctx.as_of, problem, GOLD_MASKED, run_ids=runs, rendered=rendered)
    try:
        values = await _sql_values(case, snapshot, artifact_id, ran.run_id, ctx)
    except _NoValue as e:
        return _outcome("no_value", ctx.as_of, str(e), GOLD_NO_VALUE, run_ids=runs, rendered=rendered)
    return _outcome("ok", ctx.as_of, sets=[GoldSet(run_id=ran.run_id, snapshot=artifact_id, values=values)],
                    run_ids=runs, rendered=rendered)


# ==========================================================================
# 口径探针、升版处置
# ==========================================================================


@dataclass(frozen=True)
class UpgradeState:
    """口径题钉的那一版现在怎么样。pending：有新的已发布版本（或钉住的退役了）而没处置到那一版，这道题跳过。
    versions：这道题要算的版本（钉住的；dual 时再加新版）。"""

    pinned: int
    latest: int | None
    retired: bool
    pending: bool
    message: str | None
    policy: str | None
    versions: tuple[int, ...]

    def to_dict(self) -> dict[str, Any]:
        return {"pinned": self.pinned, "latest": self.latest, "retired": self.retired, "pending": self.pending,
                "message": self.message, "policy": self.policy, "versions": list(self.versions)}


def _covered(upgrade: Mapping[str, Any] | None, latest: int | None) -> str | None:
    """题目上的处置管不管得到最新的那一版：管得到交回处置，否则 None。"""
    if not upgrade or upgrade.get("policy") not in UPGRADE_LABEL:
        return None
    if latest is not None and int(upgrade.get("to_version") or 0) < latest:
        return None
    return str(upgrade["policy"])


async def upgrade_state(session: AsyncSession, gold: Mapping[str, Any], pinned: Any) -> UpgradeState:
    """按正式运行同一个判断（unresolved_caliber_upgrades）、同一个退役认法（subgraph_pin_problems）看这道口径题要不要处置。"""
    from app.apps.versions import CALIBER_RETIRED, latest_published, subgraph_pin_problems
    from app.engine.governance import unresolved_caliber_upgrades
    from app.engine.schema import GraphSpec
    from app.ledger import derive

    newest = await latest_published(session, pinned.workflow_id)
    latest = newest.version if newest is not None else None
    upgrade = gold.get("upgrade") or None
    policy = _covered(upgrade, latest)
    spec = GraphSpec.model_validate(derive.caliber_probe(pinned, upgrade_policy=policy).graph)
    errors, _events = await unresolved_caliber_upgrades(session, spec)
    retired = any(p.code == CALIBER_RETIRED for p in await subgraph_pin_problems(session, spec))
    pending = bool(errors) or retired
    message = None
    if errors:
        message = f"口径已升版到 v{latest}，待处置"
    elif retired:
        message = f"固定的「{pinned.workflow_name}」v{pinned.version} 已退役，待处置"
    versions: tuple[int, ...] = (pinned.version,)
    if policy == "dual" and upgrade and int(upgrade.get("to_version") or 0) > pinned.version:
        versions = (pinned.version, int(upgrade["to_version"]))
    return UpgradeState(pinned=pinned.version, latest=latest, retired=retired, pending=pending, message=message,
                        policy=policy, versions=versions)


async def _caliber_set(case: Mapping[str, Any], pinned: Any, ctx: Context, payload: dict[str, Any],
                       policy: str | None) -> tuple[GoldSet | None, GoldOutcome | None]:
    from app.ledger import derive

    probe = derive.caliber_probe(pinned, upgrade_policy=policy)
    ran = await _launch_and_wait(probe.graph, payload, name=_run_name(case, ctx),
                                 origin=_origin(case, ctx, derived=probe.origin), ctx=ctx)
    runs = [ran.run_id] if ran.run_id else []
    if ran.status != "ok":
        return None, _outcome(ran.status, ctx.as_of, ran.reason, ran.code, run_ids=runs)
    for _nid, (_aid, snapshot) in await _artifacts(ran.run_id, "query_snapshot"):
        if isinstance(snapshot, dict) and (problem := masked_problem(snapshot)):
            return None, _outcome("refused", ctx.as_of, problem.replace("期望 SQL", "口径的取数"), GOLD_MASKED,
                                  run_ids=runs)
    cards = [(aid, body) for nid, (aid, body) in await _artifacts(ran.run_id, "metric_set") if nid == probe.card_ids[0]]
    if not cards or not isinstance(cards[-1][1], dict):
        return None, _outcome("failed", ctx.as_of, "算期望的运行没有留下口径卡的指标集，期望无从取值", GOLD_FAILED,
                              run_ids=runs)
    artifact_id, card = cards[-1]
    try:
        values = _values(case["expectations"], lambda e: _metric_value(e, card))
    except _NoValue as e:
        return None, _outcome("no_value", ctx.as_of, str(e), GOLD_NO_VALUE, run_ids=runs)
    return GoldSet(run_id=ran.run_id, snapshot=artifact_id, values=values, version=pinned.version), None


async def _caliber_gold(case: Mapping[str, Any], ctx: Context) -> GoldOutcome:
    from app.db.base import SessionLocal
    from app.ledger import derive

    gold = case["gold"]
    variables = ctx.variables()
    payload = {k: render(v, variables) for k, v in (gold.get("inputs") or {}).items()}
    rendered = {"inputs": payload}
    async with SessionLocal() as session:
        try:
            pinned = await derive.load_pinned(session, gold["workflow_id"], gold["version"], gold["node_id"])
        except derive.NotDerivable as e:
            return _outcome("refused", ctx.as_of, f"这张口径卡不能当口径题：{e.message}", CALIBER_NOT_REUSABLE,
                            rendered=rendered)
        state = await upgrade_state(session, gold, pinned)
        if state.pending:
            return _outcome("skipped", ctx.as_of, state.message, UPGRADE_PENDING, rendered=rendered)
        pins = [pinned]
        for version in state.versions[1:]:
            try:
                pins.append(await derive.load_pinned(session, gold["workflow_id"], version, gold["node_id"]))
            except derive.NotDerivable as e:
                return _outcome("refused", ctx.as_of, f"新版 v{version} 不能当口径题：{e.message}", CALIBER_NOT_REUSABLE,
                                rendered=rendered)
    sets: list[GoldSet] = []
    runs: list[str] = []
    for pin in pins:
        found, failed = await _caliber_set(case, pin, ctx, payload, state.policy)
        if failed is not None:
            failed.run_ids = runs + failed.run_ids
            failed.rendered = rendered
            if len(pins) > 1:
                failed.reason = f"v{pin.version}：{failed.reason}"
            return failed
        assert found is not None
        sets.append(found)
        runs.append(found.run_id)
    return _outcome("ok", ctx.as_of, sets=sets, run_ids=runs, rendered=rendered)


# ==========================================================================
# 对外：算一次、作答前后各算一次
# ==========================================================================


async def compute_gold(case: Mapping[str, Any], ctx: Context) -> GoldOutcome:
    """按 case（cases.case_fields 的形状，或者还没保存的 normalize_case 结果）算一次期望。没有期望来源的题（只有反问、
    文字要点）不用算，交回 ok、没有值。不抛异常：意外也照实记成 failed。"""
    gold = case.get("gold") or None
    rendered_question = render(case.get("question") or "", ctx.variables())
    try:
        if gold is None:
            outcome = _outcome("ok", ctx.as_of)
        elif gold["kind"] in ("sql", "from_run"):
            outcome = await _sql_gold(case, ctx)
        else:
            outcome = await _caliber_gold(case, ctx)
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001 - 算期望是附加的一步：出了意外照实记下，不让评测整体停下
        from app.core.errors import describe_exception

        logger.exception("评测：算第 %s 题的期望时出错", case.get("seq"))
        outcome = _outcome("failed", ctx.as_of, f"算期望时出错：{describe_exception(e)}", GOLD_FAILED)
    outcome.rendered = {"question": rendered_question, **outcome.rendered}
    return outcome


@dataclass
class Settled:
    """作答前后两次期望合起来的结论。status：ok 两次一样、可以判；drifted 作答期间数据变了，不判；其余照搬没算出来的
    那一次（skipped / refused / no_value / failed / timeout）。expected 是能判时用的那一份（作答前的）。"""

    status: str
    reason: str | None
    code: str | None
    before: GoldOutcome
    after: GoldOutcome | None

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    @property
    def expected(self) -> list[GoldSet]:
        return self.before.sets if self.ok else []

    def to_dict(self) -> dict[str, Any]:
        """EvalResult.gold 的形状。"""
        return {"status": self.status, "reason": self.reason, "code": self.code, "before": self.before.to_dict(),
                "after": self.after.to_dict() if self.after is not None else None}


def stable_sets(sets: Sequence[Any]) -> list[list[Any]]:
    """比较用的几份期望值：[[版本, {期望 id: 值}]]，分析结论里随运行当天、现查时刻、输入快照的工件变的几项去掉
    （judgements.steady）。sets 是 GoldSet 或它的 to_dict。作答前后两次（_fingerprint）、和上一次比的「数据变了」
    （runner.gold_key）都按它。"""
    from app.eval.judgements import steady

    out = []
    for s in sets:
        version, values = (s.version, s.values) if isinstance(s, GoldSet) else (s.get("version"), s.get("values"))
        out.append([version, {k: steady(v) for k, v in values.items()} if isinstance(values, Mapping) else values])
    return out


def _fingerprint(outcome: GoldOutcome) -> str:
    from app.core.artifact_store import canonical_json

    return canonical_json(stable_sets(outcome.sets))


def settle(before: GoldOutcome, after: GoldOutcome | None) -> Settled:
    """两次期望一样才判。没算出来的那一次的原因照搬（先看作答前的）。"""
    if not before.ok:
        return Settled(before.status, before.reason, before.code, before, after)
    if after is None:
        return Settled("failed", "作答后没有复算期望", GOLD_FAILED, before, after)
    if not after.ok:
        return Settled(after.status, f"作答后复算期望：{after.reason}", after.code, before, after)
    if _fingerprint(before) != _fingerprint(after):
        return Settled("drifted", "作答期间数据变了（作答前后算出的期望不一样），这道题不判", GOLD_DRIFTED, before, after)
    return Settled("ok", None, None, before, after)


async def bracket(case: Mapping[str, Any], ctx: Context, answer: Callable[[GoldOutcome], Awaitable[T]]
                  ) -> tuple[T | None, Settled]:
    """作答前算一次期望 → answer(作答前的期望) → 作答后再算一次 → settle。作答前就算不出来的（升版待处置、拒收、失败）
    不作答，省下一次作答的钱，结论照搬原因（第 3 波记「跳过」或「不能评分」，不算答错）。A/B 两臂共用期望：在 answer
    里同时跑两臂。"""
    before = await compute_gold(case, ctx)
    if not before.ok:
        return None, settle(before, None)
    result = await answer(before)
    after = await compute_gold(case, dataclasses.replace(ctx, role="gold_after"))
    return result, settle(before, after)


# ==========================================================================
# 口径升版的处置（题目上三选一）
# ==========================================================================


class DisposeRefused(Exception):
    """处置不了：message 给人看，code 给程序认（接口回 409 / 422）。"""

    def __init__(self, message: str, code: str, status: int = 409) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status


#: 处置被拒的机器码
DISPOSE_NOT_CALIBER = "eval_dispose_not_caliber"
DISPOSE_NOTHING = "eval_dispose_nothing_pending"
DISPOSE_INVALID = "eval_dispose_invalid"


async def plan_dispose(session: AsyncSession, case: Mapping[str, Any], policy: str) -> tuple[dict[str, Any], int]:
    """按 policy 算出处置后的期望来源（还不写库）：(新的 gold, 新版本号)。recompute、incomparable 交回改钉新版本的 gold，
    dual 交回带上 upgrade 的旧 gold。新版的卡不能当口径题（不能复用、没有这道题的指标）就拒（DisposeRefused）。"""
    from app.db.base import utcnow
    from app.ledger import derive

    if policy not in UPGRADE_LABEL:
        raise DisposeRefused(f"口径升版的处置只能是 {'、'.join(UPGRADE_LABEL)} 之一，没有缺省", DISPOSE_INVALID, 422)
    gold = case.get("gold") or {}
    if gold.get("kind") != "caliber":
        raise DisposeRefused("只有口径题有升版处置", DISPOSE_NOT_CALIBER, 422)
    try:
        pinned = await derive.load_pinned(session, gold["workflow_id"], gold["version"], gold["node_id"])
    except derive.NotDerivable as e:
        raise DisposeRefused(f"固定的口径版本无法读取：{e.message}", CALIBER_NOT_REUSABLE) from e
    state = await upgrade_state(session, gold, pinned)
    if not state.pending or state.latest is None or state.latest <= pinned.version:
        raise DisposeRefused("这道题固定的口径没有待处置的新版本" if not state.pending else
                             f"{state.message}：已经没有更新的已发布版本可以改为固定，只能另建新题或删除这道题",
                             DISPOSE_NOTHING)
    latest = state.latest
    if policy == "dual" and state.retired:
        raise DisposeRefused(f"固定的 v{pinned.version} 已退役，不能新旧两版并算：请改为固定新版本或另建新题", DISPOSE_INVALID)
    try:
        newer = await derive.load_pinned(session, gold["workflow_id"], latest, gold["node_id"])
    except derive.NotDerivable as e:
        raise DisposeRefused(f"新版 v{latest} 不能当口径题：{e.message}。请另建新题", CALIBER_NOT_REUSABLE) from e
    have = {m["id"] for m in newer.metrics}
    lost = [e["metric"] for e in case.get("expectations") or [] if e.get("kind") == "scalar" and e["metric"] not in have]
    if lost:
        raise DisposeRefused(f"新版 v{latest} 的口径卡上没有指标「{'、'.join(lost)}」：请另建新题", CALIBER_NOT_REUSABLE)
    record = {"policy": policy, "from_version": pinned.version, "to_version": latest, "at": utcnow().isoformat()}
    if policy == "dual":
        return {**copy.deepcopy(dict(gold)), "upgrade": record}, latest
    return {**{k: copy.deepcopy(v) for k, v in gold.items() if k != "upgrade"}, "version": latest,
            **({"upgrade": record} if policy == "recompute" else {})}, latest


__all__ = ["CANCEL_GRACE_S", "Context", "DISPOSE_INVALID", "DISPOSE_NOTHING", "DISPOSE_NOT_CALIBER", "DisposeRefused",
           "GOLD_DRIFTED", "GOLD_FAILED", "GOLD_MASKED", "GOLD_NO_VALUE", "GOLD_TIMEOUT", "GoldOutcome", "GoldSet",
           "QUERY_NODE", "ROLES", "STATUSES", "Settled", "TRIAL_TIMEOUT_S", "UPGRADE_LABEL", "UPGRADE_PENDING",
           "UpgradeState", "bracket", "compute_gold", "masked_problem", "plan_dispose", "settle", "sql_probe_graph",
           "stable_sets", "upgrade_state"]
