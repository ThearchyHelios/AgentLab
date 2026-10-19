"""口径召回：口径升版、目录事实被驳回或修改、表结构变动之后，追回已经交付的答案（docs/design/next-round/02-口径账本.md
做法 7，拍板 L6、L7；总方案 X1、X3、X6）。

**一、口径升版。** 工作流 W 发布了新版本 N（api/workflows.commit_publish 提交之后在后台排队，失败不影响发布）：

- 反查（ledger/uses）：W 的旧版本 V < N 被哪些问数回答、正式运行、固定了它的工作流、定时任务、应用、导出用过。旧版本里
  没有源头口径卡（metrics 节点、不是转抄别处的 caliber_from）的不是口径，不召回。
- 分组：按（旧版本、新版本、input_key(参数)）去重成组。参数先按旧版本入口字段整理（只留入口有的字段、空的补旧版的缺省值），
  这样问数回答绑定出的 {start, end} 和定时任务的 {start, end, store: all} 是同一组。组按最近一次用到的先后排。
- 每组一次召回运行（来源 recall，探索运行，系统派生的运行不进使用统计）：派生图经 ledger/derive.recall_graph（全站唯一的
  派生图生成器）——入口（这一组的参数）→ 旧版取数 + 旧版口径卡、新版取数 + 新版口径卡（caliber_from.nodes 改名，不撞 id）
  → 召回卡（recall_card：每个两版都有的指标三个数：旧版、新版、两版之差；差值在卡里算，叙述层无算术）→ 出口（契约
  metrics_from 指向召回卡；不加报告、不调模型）。两版在同一次运行里查同一份数据，差值只反映口径；数据随时间的变化另外拿
  原答案当时的值对照着说（task_payload 的 original）。
- 不能重算的照实记「无法重算：原因」，不发起：口径的数据部分不能复用（derive.NotDerivable）、新版里没有这张口径卡、两版
  没有同名的指标、新版入口要的参数这一组里没有或不合规（compose.check_inputs 同一套校验）、新版的图里会写外部系统
  （apps/backfill.graph_external_write，和按新口径补算、按口径出具同一个判断）。新版图里别处有人工节点、按流程使用不安全的
  照样重算：召回图只有两版的取数和口径卡，碰不到它们（口径卡上游的人工节点 derive 已经认作数据部分不能复用）；「按新版
  重新出具」走完整的正式运行，仍按 graph_side_effect 全套拒绝（ledger/issue.check_target）。
- 上限：每次发布最多自动重算 ledger.recall_limit 组（设置，缺省 50；0 表示不自动重算，只列出受影响的答案）。超出的写
  「另有 M 组未重算」，在版本历史里点「接着算」手动接着发起下一批（每批同样的组数，上限是 0 时按缺省值）。
- **计划从已有的召回运行重算**（照 apps/backfill 的写法，不建任务表）：一组算没算过，看有没有 origin 指向（W、V、N、
  参数指纹）的召回运行；不能重算的每次按同样的规则重新判出来。幂等：重复排队、服务重启之后重算一遍计划，已经算过的跳过。
  启动时（resume）把最近 RESUME_DAYS 天发布、自动那一批没发完的接着发完，结束了还没收尾的召回运行补上收尾。同一个（W、N）
  的计划和发起在进程内串行（单进程部署，同 apps/backfill 的锁）。
- **收尾**（settle，召回运行结束时 runner 调）：读召回卡（封存范围内的指标集），每个指标按它的单位、小数位渲染的旧值和新值
  不同就是数变了。数变了：给这一组答案的主人开 recall 待办（挂在召回运行上；开登录时每个主人一条，只列他的答案；关登录时
  一条派给任何人），推 ledger.recall 通知。数没变：只记账——在每个原答案的运行上追加 ledger.recalled（「已按 vN 核对，
  不影响」）。召回运行自己也追加一条 ledger.recalled 记结论（只记变没变、几个答案、几条待办，不记数），它同时是「收过尾」的
  记号。召回运行没成功的、成功了却读不出召回卡（或缺了某个比较的旧值、新值）的照实记重算失败，原答案一个不动。

**二、目录事实被驳回或修改**（只管影响数字的那一组：有效记录条件、去重、业务日期、关联、业务主键、度量类型，同
data/catalog_review.FIELD_GROUP 的 facts）：审阅写入之后（data/catalog 的审阅、人工编辑、保存提案，catalog_review 的批量审阅）
在后台找出查过这张表、而且查询快照冻结的目录里看得到这一项的探索运行，追加 ledger.basis_changed（kind=catalog），界面标
「依据有变」；对这些运行执行过的 SQL 按新目录跑一遍 SQL 检查（data/sqlcheck，纯计算），比冻结目录下多出错误级问题的才开
一条 recall 待办（reason=catalog，只有「知道了」）通知答案的主人（拍板 L7：不自动重问）。同一个数据源排着做、批量审阅的几张表
合成一遍（catalog_pass）：查询快照按数据源在库里筛、凑够就停，冻结的表结构快照（大库的能有几 MB）同一份只读一次。

**三、表结构变动**：消费 data/source_changes（list_changes(since_seq) 按 seq 游标拉，游标记在设置表里；探查、剖析任务结束、
处理变化改了目录时触发）。只看 triggers_recall 认的：表或列被删、列的类型变了标「依据的表结构已变」（kind=structure）；
触及有效记录条件的新码值（values）、处理时改了目录的（catalog_edit）标依据有变，同二按新目录跑 SQL 检查。表结构变了的，
受影响的口径（最新的已发布版本里查这张表的口径）按最近一次正式运行的输入派生数据部分试算一次（召回运行，origin.probe；
这一版还没有正式运行的退回最近一次等价于它的问数回答的参数，再没有用入口的缺省值；参数凑不齐的不试算），算不出来才开
recall 待办（reason=structure）通知发布人。

**通知**：ledger.recall 只推口径升版、数真变了的那一种待办（消息照 notify/service 的规矩：标题、状态、条数、链接，不放
新旧值和差值）；目录和表结构的两种待办照别的待办推 task.opened。全局渠道按一批待办（一次召回开给几个主人的那几条，
batch_of_key）只推一次，个人渠道每个主人各一条（notify/service.notify_recall）。

**答案上的标记**（answer_marks）：账本接口 GET /api/runs/{id}/ledger 的 recall——已按 vN 核对、依据有变、看的这个人手上说的
是这个答案的 recall 待办，给问数据的轮次和证据面板「口径」一步。

**自动的那几样**（发布后自动那一批、探查剖析之后拉表结构变化、启动时接着做）和调度循环同一个开关（automatic，
AGENTLAB_SCHEDULER）：它们是在后台替人查业务库，测试、拷来的数据目录（检查脚本）不做；关着时版本历史里照样列出受影响的
答案、能手动接着算，审阅之后标依据有变照做（只读应用库）。

只读：召回运行带 READ_ONLY，只查库、不写；不调模型。召回是辅助步骤：出错只记日志，不影响发布、审阅和探查。
"""
from __future__ import annotations

import asyncio
import logging
import weakref
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, not_, select

from app.core.events import EventType
from app.db.base import SessionLocal, utcnow
from app.db.models import Run, RunEvent, Workflow
from app.db.portable import json_eq, json_flag, json_text
from app.ledger import uses as ledger_uses

logger = logging.getLogger(__name__)

ORIGIN = "recall"
#: 召回待办的种类（process/tasks.RECALL_KIND）和三种来由
TASK_KIND = "recall"
REASONS = ("caliber", "catalog", "structure")
#: 发起的批次：发布后自动的、版本历史里手动接着算的
BATCHES = ("auto", "manual")
#: 一组的结论
OUTCOMES = ("changed", "unchanged", "failed", "running", "pending", "not_recomputable")
#: 启动时接着做最近几天发布的
RESUME_DAYS = 7
#: 目录、表结构变了时最多看多少次探索运行（新的在前）
BASIS_MAX_RUNS = 200
#: 设置表里记表结构变化游标的键（通知自己记的账同 notify_state:* 的写法，设置接口不列）
CURSOR_KEY = "ledger_state:recall_changes"
#: 被拒的机器码
REFUSED = "ledger_recall_refused"

#: 不能重算的原因
NOT_CALIBER = "这一版里没有源头口径卡，不是口径，不召回"
NOT_REUSABLE = "口径的数据部分不能复用：{reason}"
CARD_GONE = "新版 v{version} 里没有口径卡「{card}」"
NO_COMMON = "新旧两版的口径卡没有同名的指标，比不了"
SIDE_EFFECT = "「{name}」v{version}{reason}，召回不代人做这些事"
NEW_INPUTS = "新版 v{version} 的入口要的参数这一组里没有或不合规：{problem}"
NEW_GONE = "新版本不存在，可能已被删除"
LAUNCH_REFUSED = "没能发起召回运行：{reason}"
RUN_FAILED = "重算失败：{reason}"
#: 召回运行成功了，召回卡（或其中某个比较的旧值、新值）却读不出来：照实算重算失败，不当成数没变
CARD_UNREADABLE = "读不出召回卡"
#: 这一版已经不是当前发布版：不再接着算（新版本的召回本来就覆盖更早的所有版本，同 resume 只接着做当前发布版）
SUPERSEDED = "已有更新的发布版 v{latest}，召回在 v{latest} 那一版接着看"
NOT_CURRENT = "v{version} 已不是当前发布版，召回不再接着算"
#: 收尾
CHANGED_TITLE = "口径「{name}」升到 v{version}，答案的数变了"
CATALOG_TITLE = "答案依据的数据目录有变：按新目录检查出了新的问题"
STRUCTURE_TITLE = "口径「{name}」v{version} 依据的表结构变了，试算没有算出来"
#: 依据有变的四种（ledger.basis_changed 的 kind）
BASIS_KINDS = ("catalog", "structure", "values", "catalog_edit")
#: 目录里影响数字的那一组（同 data/catalog_review.FIELD_GROUP 的 facts）
FACT_SLOTS = (("t", "valid_filter"), ("t", "dedup"), ("t", "business_date"), ("t", "keys"), ("c", "measure"))


class RecallRefused(Exception):
    def __init__(self, status_code: int, message: str, code: str = REFUSED) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.code = code


# ==========================================================================
# 计划
# ==========================================================================


@dataclass
class Pair:
    """一对口径卡：旧版那一张、新版同 id 的那一张。"""

    old: Any
    new: Any


@dataclass
class Group:
    """（旧版本、新版本、参数）一组：这一组的用法、能不能重算、算过没有。"""

    old_version: int
    new_version: int
    key: str
    binding: dict[str, Any]
    uses: list[ledger_uses.Use] = field(default_factory=list)
    latest: datetime | None = None
    problem: str | None = None
    #: 发起召回运行用的输入（按新版入口校验过、补了缺省值；旧版独有的字段接在后面）
    inputs: dict[str, Any] = field(default_factory=dict)
    run: Run | None = None
    refused: str | None = None

    @property
    def id(self) -> str:
        return f"{self.old_version}-{self.key[:16]}"


@dataclass
class Plan:
    workflow_id: str
    workflow_name: str
    new_version: int
    #: 工作流现在的发布版（不是 new_version 的不再接着算，见 stale）
    published_version: int | None = None
    groups: list[Group] = field(default_factory=list)
    #: 每个旧版本的口径卡对、旧版本自己的问题（不是口径、数据部分不能复用……）
    pairs: dict[int, list[Pair]] = field(default_factory=dict)
    version_problem: dict[int, str] = field(default_factory=dict)
    found: ledger_uses.Found | None = None
    problem: str | None = None
    #: 自动那一批已经发起了几组
    auto_launched: int = 0

    def todo(self) -> list[Group]:
        """还没算、能算的组（按最近一次用到的先后）。"""
        return [g for g in self.groups if g.run is None and g.problem is None]

    def stale(self) -> str | None:
        """这一版已经不是当前发布版时不再发起（手动接着算也不）的原因：之后又发布了更新的版本，新版本的召回覆盖更早的所有
        版本（同 resume 只接着做当前发布版），再按这一版开「口径升到 vN」的待办只是添乱。是当前发布版为 None。"""
        if self.published_version == self.new_version:
            return None
        if self.published_version is not None and self.published_version > self.new_version:
            return SUPERSEDED.format(latest=self.published_version)
        return NOT_CURRENT.format(version=self.new_version)


def _aware(moment: datetime | None) -> datetime | None:
    if moment is None:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def normalize(binding: Mapping[str, Any], fields: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """参数按旧版入口整理：只留入口有的字段，没给（或给了空）的补旧版的缺省值。分组、发起都按它。"""
    out: dict[str, Any] = {}
    for f in fields:
        name = str(f.get("name") or "")
        if not name:
            continue
        value = binding.get(name)
        if value in (None, ""):
            value = f.get("default")
        if value not in (None, ""):
            out[name] = value
    return out


def source_cards(graph: Any) -> list[str]:
    """一版图里的源头口径卡（metrics 节点、不是转抄别处的 caliber_from），按图里的先后。"""
    out: list[str] = []
    for node in (graph or {}).get("nodes") or [] if isinstance(graph, dict) else []:
        if not isinstance(node, dict) or node.get("type") != "metrics":
            continue
        cfg = (node.get("data") or {}).get("config") if isinstance(node.get("data"), dict) else None
        if isinstance(cfg, dict) and cfg.get("caliber_from") in (None, "", {}):
            out.append(str(node.get("id")))
    return out


async def recall_runs(session: Any, workflow_id: str, new_version: int | None = None) -> list[Run]:
    """以 W（新版本 N）发起过的召回运行（试算的不算），新的在前。origin_kind 有索引，先按它筛再看 origin。"""
    stmt = select(Run).where(Run.origin_kind == ORIGIN,
                             json_eq(Run.origin, "workflow_id", workflow_id), not_(json_flag(Run.origin, "probe")))
    if new_version is not None:
        stmt = stmt.where(json_eq(Run.origin, "to_version", int(new_version)))
    return list((await session.execute(stmt.order_by(Run.created_at.desc()))).scalars())


async def build_plan(session: Any, workflow_id: str, new_version: int) -> Plan:
    """W 升到 N 的召回计划（见模块说明）。只读，不发起。"""
    from app.apps import versions as app_versions
    from app.apps.backfill import graph_external_write
    from app.ledger import compose, derive

    workflow = await session.get(Workflow, workflow_id)
    plan = Plan(workflow_id=workflow_id, workflow_name=workflow.name if workflow is not None else "",
                new_version=int(new_version),
                published_version=workflow.published_version if workflow is not None else None)
    new_row = await app_versions.version_row(session, workflow_id, int(new_version)) if workflow else None
    if new_row is None:
        plan.problem = NEW_GONE
        return plan
    found = await ledger_uses.find_uses(session, workflow_id, below=int(new_version))
    plan.found = found
    side = await graph_external_write(session, new_row.graph)
    for version in found.versions():
        old_row = await app_versions.version_row(session, workflow_id, version)
        cards = source_cards(old_row.graph if old_row is not None else None)
        if not cards:
            plan.version_problem[version] = NOT_CALIBER
            continue
        pairs: list[Pair] = []
        problems: list[str] = []
        for card in cards:
            try:
                old = await derive.load_pinned(session, workflow_id, version, card)
            except derive.NotDerivable as e:
                problems.append(NOT_REUSABLE.format(reason=e.message))
                continue
            try:
                new = await derive.load_pinned(session, workflow_id, int(new_version), card)
            except derive.NotDerivable as e:
                problems.append(CARD_GONE.format(version=new_version, card=old.card_title)
                                if e.code == derive.GONE else NOT_REUSABLE.format(reason=e.message))
                continue
            if not {m["id"] for m in old.metrics} & {m["id"] for m in new.metrics}:
                problems.append(NO_COMMON)
                continue
            pairs.append(Pair(old, new))
        if side:
            plan.version_problem[version] = SIDE_EFFECT.format(name=plan.workflow_name, version=new_version,
                                                               reason=side)
        elif not pairs:
            plan.version_problem[version] = problems[0] if problems else NOT_CALIBER
        plan.pairs[version] = pairs
    groups: dict[tuple[int, str], Group] = {}
    for use in found.uses:
        if plan.version_problem.get(use.version) == NOT_CALIBER:
            continue
        pairs = plan.pairs.get(use.version) or []
        fields = pairs[0].old.entry_fields if pairs else ()
        binding = normalize(use.binding, fields) if pairs else dict(use.binding)
        key = ledger_uses._key(binding)
        group = groups.get((use.version, key))
        if group is None:
            group = groups[(use.version, key)] = Group(old_version=use.version, new_version=int(new_version), key=key,
                                                       binding=binding, problem=plan.version_problem.get(use.version))
        group.uses.append(use)
        at = _aware(use.at)
        if at is not None and (group.latest is None or at > group.latest):
            group.latest = at
    for group in groups.values():
        pairs = plan.pairs.get(group.old_version) or []
        if group.problem is not None or not pairs:
            continue
        new = pairs[0].new
        new_names = {str(f.get("name")) for f in new.entry_fields}
        try:
            checked = compose.check_inputs(new, {k: v for k, v in group.binding.items() if k in new_names})
        except compose.ComposeRefused as e:
            group.problem = NEW_INPUTS.format(version=new_version, problem=e.message)
            continue
        group.inputs = {**checked, **{k: v for k, v in group.binding.items() if k not in new_names}}
    existing = await recall_runs(session, workflow_id, int(new_version))
    by_key: dict[tuple[int, str], Run] = {}
    for run in existing:
        origin = run.origin if isinstance(run.origin, dict) else {}
        k = (int(origin.get("from_version") or 0), str(origin.get("input_key") or ""))
        by_key.setdefault(k, run)
        if origin.get("batch") == "auto":
            plan.auto_launched += 1
    for k, group in groups.items():
        group.run = by_key.get(k)
        group.refused = _refusals.get((workflow_id, int(new_version), k[0], k[1]))
    far = datetime.min.replace(tzinfo=timezone.utc)
    plan.groups = sorted(groups.values(), key=lambda g: (-(g.latest or far).timestamp(), g.old_version, g.key))
    return plan


#: 发起时被拒的组（launch_run 拒绝了：工具不在、数据源停用……）：(W, N, V, 参数指纹) → 原因。只在进程里记（下次「接着算」
#: 重试；重启后忘掉、照样算待算）
_refusals: dict[tuple[str, int, int, str], str] = {}


# ==========================================================================
# 召回卡与发起
# ==========================================================================


def _metric(mid: str, name: str, expression: str, src: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {"id": mid, "name": name, "unit": str(src.get("unit") or ""), "expression": expression}
    if src.get("decimals") is not None:
        out["decimals"] = src["decimals"]
    if src.get("format"):
        out["format"] = src["format"]
    return out


def recall_card(pairs: Sequence[Pair]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """召回卡（derive.recall_graph 的 tail）和每个比较的说明。每个两版都有（按指标 id）的指标三个数：old_k 旧版、new_k
    新版、diff_k 两版之差（新版 − 旧版），单位、小数位、显示格式照新版的定义。缺输入记空值（on_missing=null）。纯计算。"""
    from app.ledger import derive

    metrics: list[dict[str, Any]] = []
    meta: list[dict[str, Any]] = []
    k = 0
    for i, pair in enumerate(pairs):
        old_prefix, new_prefix = derive.recall_prefixes(i)
        new_by = {m["id"]: (j, m) for j, m in enumerate(pair.new.metrics)}
        for idx, m in enumerate(pair.old.metrics):
            if m["id"] not in new_by:
                continue
            j, nm = new_by[m["id"]]
            old_expr = f"nodes.{old_prefix}{pair.old.node_id}.metrics[{idx}].value"
            new_expr = f"nodes.{new_prefix}{pair.new.node_id}.metrics[{j}].value"
            name = str(nm.get("name") or m["id"])
            metrics += [_metric(f"old_{k}", f"{name} · 旧版 v{pair.old.version}", old_expr, nm),
                        _metric(f"new_{k}", f"{name} · 新版 v{pair.new.version}", new_expr, nm),
                        _metric(f"diff_{k}", f"{name} · 两版之差", f"({new_expr}) - ({old_expr})", nm)]
            # 旧版的式子（定义，不是数）一并记下：问数回答当时的值要按它找到答案里对应的那一格（original_values）
            item = {"k": k, "card": pair.old.node_id, "metric": m["id"], "name": name,
                    "unit": str(nm.get("unit") or ""), "decimals": nm.get("decimals"), "format": nm.get("format"),
                    "expression": str(m.get("expression") or "")}
            if not (_rows_stable(pair.old, m) and _rows_stable(pair.new, nm)):
                # 两版都按行号取多行结果里的一格、查询又没有 ORDER BY：两次查询交回的行序可能不同，第几行不一定是同一行，
                # 按位置比出来的「变了 / 没变」都不可信（compare 照实记行序不定）
                item["row_order"] = "unstable"
            meta.append(item)
            k += 1
    node = derive._node(derive.RECALL_CARD, "metrics", "召回卡", caliber="口径召回", caliber_version="v1",
                        on_missing="null", metrics=metrics)
    return node, meta


def _rows_stable(pinned: Any, metric: Mapping[str, Any]) -> bool:
    """这个指标按行号取的每一格（cell(nodes.<取数>, 行, '列')），在那一版的取数 SQL 上重跑时还是同一行（sql_semantics.row_stable）。
    行号、SQL 认不出来的照旧当认得住（以前就是按位置比的，不凭猜测改结论）。"""
    import ast

    from app.data.sql_semantics import row_stable
    from app.engine.expressions import ExpressionError, cell_parts, leaf_refs, parse_expression

    sql_of = {str(n.get("id")): ((n.get("data") or {}).get("config") or {}).get("args", {}).get("sql")
              for n in pinned.fetches if isinstance(n, dict)}
    try:
        leaves = leaf_refs(parse_expression(str(metric.get("expression") or ""))[0])
    except ExpressionError:
        return True
    for leaf in leaves:
        parts = cell_parts(str(leaf))
        if parts is None or not parts[0].startswith("nodes."):
            continue
        sql = sql_of.get(parts[0].removeprefix("nodes."))
        try:
            row = ast.literal_eval(parts[1].strip())
        except (ValueError, SyntaxError):
            continue
        if isinstance(sql, str) and isinstance(row, int) and not row_stable(sql, row):
            return False
    return True


def build_graph(group: Group, pairs: Sequence[Pair]) -> tuple[Any, list[dict[str, Any]]]:
    """一组的召回图（derive.recall_graph）和比较说明。同样的输入永远得到同一张图。"""
    from app.ledger import derive

    card, meta = recall_card(pairs)
    graph = derive.recall_graph([(p.old, p.new) for p in pairs], [card], inputs=group.inputs)
    return graph, meta


async def _launch_group(plan: Plan, group: Group, *, batch: str, actor: str | None) -> Run | None:
    from app.engine.launch import LaunchRefused, launch_run
    from app.tools.datasource import READ_ONLY_OPTION

    pairs = plan.pairs.get(group.old_version) or []
    graph, meta = build_graph(group, pairs)
    origin = {"workflow_id": plan.workflow_id, "from_version": group.old_version, "to_version": group.new_version,
              "input_key": group.key, "binding": dict(group.binding), "batch": batch,
              "cards": [p.old.node_id for p in pairs], "metrics": meta,
              "uses": [u.describe() for u in group.uses], "derive": graph.origin}
    key = (plan.workflow_id, plan.new_version, group.old_version, group.key)
    async with SessionLocal() as session:
        try:
            run = await launch_run(session, graph=graph.graph, input_payload=dict(group.inputs),
                                   run_class="exploratory", actor=actor, origin_kind=ORIGIN, origin=origin,
                                   options={READ_ONLY_OPTION: True},
                                   workflow_name=f"口径召回：{plan.workflow_name} v{group.old_version} → v{group.new_version}")
        except LaunchRefused as e:
            _refusals[key] = LAUNCH_REFUSED.format(reason=e.message)
            logger.info("口径召回没能发起（%s v%s → v%s）：%s", plan.workflow_id, group.old_version, group.new_version,
                        e.message)
            return None
    _refusals.pop(key, None)
    return run


#: 同一（W、N）的计划和发起串行：{事件循环: {键: 锁}}
_locks: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict[tuple[str, int], asyncio.Lock]]" = \
    weakref.WeakKeyDictionary()


def _lock(key: tuple[str, int]) -> Any:
    """跨进程的锁（app/cluster/mutex）：同一（W、N）的计划和发起多进程时也串行，「已经自动发过几次」读得准。"""
    from app.cluster import mutex

    return mutex.lock(f"ledger-recall:{key[0]}:{key[1]}")


async def launch(workflow_id: str, new_version: int, *, batch: str = "auto", actor: str | None = None) -> list[str]:
    """按计划发起一批召回运行，交回发起了的运行 id。auto：发布后自动的那一批，一次发布最多 recall_limit 组（已经自动发起
    过的扣掉，重复调幂等）；manual：「接着算」，再发起一批（上限是 0 时按缺省的组数）。"""
    from app.api.settings import LEDGER_DEFAULTS, ledger_settings

    if batch not in BATCHES:
        raise ValueError(f"不认识的召回批次：{batch}")
    async with _lock((workflow_id, int(new_version))):
        async with SessionLocal() as session:
            plan = await build_plan(session, workflow_id, int(new_version))
            limit = int((await ledger_settings(session))["recall_limit"])
        if plan.problem is not None or plan.stale() is not None:
            return []
        if batch == "auto":
            size = max(0, limit - plan.auto_launched)
        else:
            size = limit or int(LEDGER_DEFAULTS["recall_limit"])
        launched: list[str] = []
        for group in plan.todo()[:size]:
            run = await _launch_group(plan, group, batch=batch, actor=actor)
            if run is not None:
                launched.append(run.id)
        if launched:
            logger.info("口径召回：%s 升到 v%s，%s 发起了 %d 组", workflow_id, new_version, batch, len(launched))
        return launched


# ==========================================================================
# 预告、批次、版本用量
# ==========================================================================


def _counts(plan: Plan) -> dict[str, int]:
    out = dict.fromkeys(OUTCOMES, 0)
    for group in plan.groups:
        out[_group_state(group)[0]] += 1
    return out


async def preview(session: Any, workflow_id: str, version: int) -> dict[str, Any]:
    """发布弹窗的召回预告：把 version 发布成新版本之后，旧版本被多少问数回答、导出、定时任务、应用用过，预计重算几组，超出
    上限的写明。只读、不发起（和发布之后真的召回走同一份计划，规模一致）。"""
    from app.api.settings import ledger_settings

    plan = await build_plan(session, workflow_id, int(version))
    limit = int((await ledger_settings(session))["recall_limit"])
    found = plan.found or ledger_uses.Found(uses=[])
    per_version = []
    for v in sorted({*found.versions(), *plan.version_problem}):
        if plan.version_problem.get(v) == NOT_CALIBER:
            continue
        sub = ledger_uses.Found(uses=[u for u in found.uses if u.version == v], apps={v: found.apps.get(v, [])})
        counts = ledger_uses.summarize(sub)
        counts["exports"] = await ledger_uses.exports_count(session, [u.run_id for u in sub.uses])
        per_version.append({"version": v, **counts, **({"problem": plan.version_problem[v]}
                                                       if v in plan.version_problem else {})})
    todo = len(plan.todo())
    done = sum(1 for g in plan.groups if g.run is not None)
    auto_left = max(0, limit - plan.auto_launched)
    will = min(todo, auto_left)
    return {
        "workflow_id": workflow_id, "version": int(version), "limit": limit, "caliber": bool(plan.pairs),
        "problem": plan.problem, "versions": per_version, "groups": len(plan.groups),
        "recomputable": todo + done, "not_recomputable": sum(1 for g in plan.groups if g.problem is not None),
        "already": done, "will_recompute": will, "over_limit": max(0, todo - will),
        "truncated": found.truncated,
        "reasons": _reasons(plan),
    }


def _reasons(plan: Plan) -> list[dict[str, Any]]:
    """不能重算的原因各几组（同一句话合在一起）。"""
    by: dict[str, int] = {}
    for group in plan.groups:
        if group.problem:
            by[group.problem] = by.get(group.problem, 0) + 1
    return [{"reason": r, "groups": n} for r, n in sorted(by.items(), key=lambda x: (-x[1], x[0]))]


async def outcome_of(session: Any, run: Run) -> tuple[str, dict[str, Any]]:
    """一次召回运行的结论：(changed / unchanged / failed / running, 收尾时记下的那一条 ledger.recalled 的内容)。"""
    data = await _settled(session, run.id)
    if data is not None:
        if data.get("outcome") == "failed":
            return "failed", data
        return ("changed" if data.get("changed") else "unchanged"), data
    if run.status in ("failed", "cancelled"):
        return "failed", {"error": run.error}
    return "running", {}


def _group_state(group: Group) -> tuple[str, str | None]:
    """没读收尾记录之前的状态：不能重算、待算、发起被拒（照样算待算，原因另写）、算过（具体结论看 outcome_of）。"""
    if group.run is not None:
        return "running", None
    if group.problem is not None:
        return "not_recomputable", group.problem
    return "pending", group.refused


async def batches(session: Any, workflow_id: str, version: int) -> dict[str, Any]:
    """版本历史里「这一版的召回」：每组的结论（数变了 / 没变 / 无法重算：原因 / 重算失败 / 还在算 / 未重算），「另有 M 组
    未重算」和能不能接着算（还有没算的组却不能接着算时 continue_reason 写为什么：之后又发布了更新的版本……）。
    接口只给搭建者（api/ledger 的 router）：参数原样给，同他在召回运行的输入里读到的一样；使用者
    看的是 recall 待办（task_payload，按遮罩换过）。"""
    from app.api.settings import LEDGER_DEFAULTS, ledger_settings

    plan = await build_plan(session, workflow_id, int(version))
    limit = int((await ledger_settings(session))["recall_limit"])
    counts = dict.fromkeys(OUTCOMES, 0)
    items: list[dict[str, Any]] = []
    for group in plan.groups:
        state, reason = _group_state(group)
        extra: dict[str, Any] = {}
        if group.run is not None:
            state, data = await outcome_of(session, group.run)
            if state == "failed":
                reason = RUN_FAILED.format(reason=str(data.get("error") or group.run.error or "运行没有成功结束")[:200])
            extra = {"run_id": group.run.id, "batch": (group.run.origin or {}).get("batch"),
                     "tasks": len(data.get("tasks") or []) if data else 0,
                     "changed_metrics": [m.get("name") for m in data.get("metrics") or [] if m.get("changed")]
                     if data else []}
        counts[state] += 1
        kinds: dict[str, int] = {}
        for use in group.uses:
            kinds[use.kind] = kinds.get(use.kind, 0) + 1
        items.append({"id": group.id, "from_version": group.old_version, "to_version": group.new_version,
                      "binding": [{"field": k, "value": v} for k, v in sorted(group.binding.items())],
                      "uses": [{"kind": k, "kind_text": ledger_uses.KIND_TEXT.get(k, k), "count": n}
                               for k, n in sorted(kinds.items())],
                      "latest": group.latest.isoformat() if group.latest else None,
                      "state": state, **({"reason": reason} if reason else {}), **extra})
    pending = counts["pending"]
    why_not = (plan.problem or plan.stale()) if pending > 0 else None
    return {"workflow_id": workflow_id, "workflow_name": plan.workflow_name, "version": int(version), "limit": limit,
            "batch_size": limit or int(LEDGER_DEFAULTS["recall_limit"]), "problem": plan.problem,
            "caliber": bool(plan.pairs) or bool(plan.groups), "counts": counts, "groups": items,
            "remaining": pending, "can_continue": pending > 0 and why_not is None, "continue_reason": why_not,
            "truncated": bool(plan.found and plan.found.truncated)}


async def version_recall_counts(session: Any, workflow_id: str, version: int) -> dict[str, Any]:
    """版本用量里的召回：以这一版为新版本召回过几组（其中数变了几组），这一版作为旧版本被召回过几组。"""
    as_new = await recall_runs(session, workflow_id, version)
    changed = 0
    for run in as_new:
        state, _ = await outcome_of(session, run)
        changed += state == "changed"
    as_old = int(await session.scalar(select(func.count()).select_from(Run).where(
        Run.origin_kind == ORIGIN, json_eq(Run.origin, "workflow_id", workflow_id),
        json_eq(Run.origin, "from_version", int(version)), not_(json_flag(Run.origin, "probe")))) or 0)
    return {"groups": len(as_new), "changed": changed, "recalled_from": as_old}


# ==========================================================================
# 收尾：数变没变、开待办或记账
# ==========================================================================


async def _settled(session: Any, run_id: str) -> dict[str, Any] | None:
    """召回运行上收尾时记的 ledger.recalled（role=recall）；没收过尾为 None。"""
    rows = (await session.execute(select(RunEvent.data).where(
        RunEvent.run_id == run_id, RunEvent.type == EventType.LEDGER_RECALLED).order_by(RunEvent.seq.desc()))).scalars()
    for data in rows:
        if isinstance(data, dict) and data.get("role") == "recall":
            return data
    return None


async def card_values(run_id: str) -> dict[str, dict[str, Any]]:
    """召回运行封存范围内的召回卡：{指标 id: 指标}（value、rendered……）。读不出为空。"""
    from app.api.evidence import _sealed, _sealed_cards
    from app.ledger import derive

    sealed = await _sealed(run_id)
    for node, _, card in _sealed_cards(sealed):
        if node == derive.RECALL_CARD:
            return {str(m.get("id")): m for m in card.get("metrics") or [] if isinstance(m, dict)}
    return {}


def compare(meta: Sequence[Mapping[str, Any]], values: Mapping[str, Mapping[str, Any]]) -> list[dict[str, Any]]:
    """每个比较的旧值、新值、两版之差，和数变没变：按指标的单位、小数位渲染的旧值和新值不同就是变了（召回卡上的渲染，
    同报告里显示的那个字）。只读召回卡，不在这里算差值。"""
    out = []
    for item in meta:
        k = item.get("k")
        old, new, diff = (values.get(f"{p}_{k}") or {} for p in ("old", "new", "diff"))
        unstable = item.get("row_order") == "unstable"
        # 行序不定的（recall_card 记的）：按位置比不出变没变，changed 为空，照实记 row_order，不假装对上
        changed = None if unstable else str(old.get("rendered")) != str(new.get("rendered"))
        out.append({"k": k, "card": item.get("card"), "metric": item.get("metric"), "name": item.get("name"),
                    "unit": item.get("unit"), "old": old.get("value"), "new": new.get("value"),
                    "diff": diff.get("value"), "old_text": old.get("rendered"), "new_text": new.get("rendered"),
                    "diff_text": diff.get("rendered"), "changed": changed,
                    **({"row_order": "unstable"} if unstable else {})})
    return out


_settling: set[str] = set()
_background: set[asyncio.Task[Any]] = set()


def spawn(coro: Any) -> None:
    """在后台做（召回是辅助步骤：出错只记日志）。"""
    task = asyncio.create_task(_guarded(coro))
    _background.add(task)
    task.add_done_callback(_background.discard)


async def _guarded(coro: Any) -> None:
    try:
        await coro
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - 辅助步骤
        logger.exception("口径召回的后台任务出错")


def schedule_settle(run_id: str) -> None:
    """召回运行结束了（runner 收尾时调）：在后台收尾。"""
    spawn(settle(run_id))


async def _mark(run_id: str, data: dict[str, Any]) -> bool:
    from app.engine.run_append import append_events

    return await append_events(run_id, [(EventType.LEDGER_RECALLED, None, data)], require="sealed") is not None


async def settle(run_id: str) -> str | None:
    """一次召回运行结束之后：数变了开 recall 待办、数没变在原答案上记 ledger.recalled，最后在召回运行上记结论。返回结论
    （changed / unchanged / failed），不是召回运行、还没结束、收过尾的为 None。幂等：待办按 correlation_key 认、原答案上的
    记账按召回运行认，中途断了再收一次不会多开。"""
    if run_id in _settling:
        return None
    _settling.add(run_id)
    try:
        # 多进程：收尾的进程（runner 收尾时）和当班进程启动时的「补上收尾」可能同时来，跨进程的锁里先看收过没有
        from app.cluster import mutex

        async with mutex.lock(f"ledger-settle:{run_id}"):
            return await _settle(run_id)
    finally:
        _settling.discard(run_id)


async def _settle(run_id: str) -> str | None:
    async with SessionLocal() as session:
        run = await session.get(Run, run_id)
        if run is None or run.origin_kind != ORIGIN or run.status not in ("succeeded", "failed", "cancelled") \
                or not run.manifest_hash:
            return None
        if await _settled(session, run_id) is not None:
            return None
    origin = run.origin if isinstance(run.origin, dict) else {}
    if origin.get("probe"):
        return await _settle_probe(run)
    base = {"role": "recall", "workflow_id": origin.get("workflow_id"), "from_version": origin.get("from_version"),
            "to_version": origin.get("to_version")}
    if run.status != "succeeded":
        await _mark(run_id, {**base, "outcome": "failed", "changed": None,
                             "error": str(run.error or "运行没有成功结束")[:300]})
        return "failed"
    meta = origin.get("metrics") or []
    values = await card_values(run_id)
    # 读不出召回卡、少了哪个比较的旧值或新值：两边都是空，按渲染比会被当成「没变」，给每个原答案盖上「已核对，不影响」
    # ——照实记重算失败，原答案一个不动（收过尾就不再核对，盖错了收不回来）
    if not meta or any(f"{side}_{m.get('k')}" not in values for m in meta for side in ("old", "new")):
        await _mark(run_id, {**base, "outcome": "failed", "changed": None, "error": CARD_UNREADABLE})
        return "failed"
    results = compare(meta, values)
    changed = any(r["changed"] for r in results)
    answers = list(dict.fromkeys(str(u.get("run_id")) for u in origin.get("uses") or [] if u.get("run_id")))
    tasks: list[str] = []
    if changed:
        tasks = await open_tasks(run, reason="caliber")
    else:
        for answer in answers:
            await _mark_answer(answer, run)
    await _mark(run_id, {**base, "outcome": "changed" if changed else "unchanged", "changed": changed,
                         "answers": len(answers), "tasks": tasks,
                         "metrics": [{"k": r["k"], "name": r["name"], "changed": r["changed"]} for r in results]})
    return "changed" if changed else "unchanged"


async def _mark_answer(answer_id: str, recall_run: Run) -> None:
    """数没变：在原答案的运行上记一条 ledger.recalled（已按 vN 核对，不影响）。这次召回记过的不再记。"""
    origin = recall_run.origin if isinstance(recall_run.origin, dict) else {}
    async with SessionLocal() as session:
        rows = (await session.execute(select(RunEvent.data).where(
            RunEvent.run_id == answer_id, RunEvent.type == EventType.LEDGER_RECALLED))).scalars()
        if any(isinstance(d, dict) and d.get("recall_run") == recall_run.id for d in rows):
            return
    await _mark(answer_id, {"role": "answer", "changed": False, "recall_run": recall_run.id,
                            "workflow_id": origin.get("workflow_id"), "from_version": origin.get("from_version"),
                            "to_version": origin.get("to_version")})


# ==========================================================================
# 待办
# ==========================================================================


async def _owner_targets(session: Any, owners: Iterable[tuple[str, str]]) -> list[tuple[str, str]]:
    """答案的主人 → 待办派给谁：开登录时每个主人一份（组照留，开待办时展开；服务账号不派），关登录时派给任何人。"""
    from app.auth import current_mode
    from app.process.tasks import _service_accounts

    if current_mode() == "off":
        return [("anyone", "")]
    owners = list(dict.fromkeys((k, n) for k, n in owners if k in ("user", "group") and n))
    service = await _service_accounts(session, [n for k, n in owners if k == "user"])
    out = [(k, n) for k, n in owners if not (k == "user" and n in service)]
    return out or [("anyone", "")]


def _uses_of(origin: Mapping[str, Any], owner: tuple[str, str]) -> list[dict[str, Any]]:
    """这条待办列哪几个答案：派给任何人的列全部，派给某人（组）的只列他是主人的。"""
    items = [u for u in origin.get("uses") or [] if isinstance(u, dict)]
    if owner[0] == "anyone":
        return items
    return [u for u in items if [owner[0], owner[1]] in (u.get("owners") or [])]


def correlation(run_id: str, owner: tuple[str, str], reason: str = "caliber", extra: str = "") -> str:
    """recall 待办的 correlation_key：来由:运行[:附加]:派给的种类:名字。同一条按它认，最后两段是派给谁（_owner_of_key）。"""
    return ":".join([reason, run_id, *([extra] if extra else []), owner[0], owner[1]])


async def open_tasks(run: Run, *, reason: str, owners: Iterable[tuple[str, str]] | None = None,
                     title: str | None = None, extra: str = "") -> list[str]:
    """给答案的主人开 recall 待办（挂在 run 上）。owners 不给就按召回运行 origin 里各答案的主人。按 correlation_key 认同一条，
    开过的不再开。提交之后推通知。返回这次新开的待办 id。"""
    from app.process import tasks as process_tasks

    origin = run.origin if isinstance(run.origin, dict) else {}
    async with SessionLocal() as session:
        if owners is None:
            owners = [tuple(o) for u in origin.get("uses") or [] for o in u.get("owners") or [] if len(o) == 2]
        targets = await _owner_targets(session, owners)
        name = await ledger_uses.workflow_name(session, str(origin.get("workflow_id") or ""))
        if title is None:
            title = CHANGED_TITLE.format(name=name or "口径", version=origin.get("to_version"))
        opened: list[str] = []
        for owner in targets:
            key = correlation(run.id, owner, reason, extra)
            if await session.scalar(select(process_tasks.Task.id).where(process_tasks.Task.correlation_key == key)):
                continue
            task = await process_tasks.open_recall(session, run=run, title=title, owner=owner, correlation_key=key)
            opened.append(task.id)
        await session.commit()
    for task_id in opened:
        await process_tasks.announce(task_id)
    return opened


def _owner_of_key(key: str) -> tuple[str, str]:
    """correlation_key 的最后两段是派给谁（见 correlation）。"""
    parts = (key or "").split(":")
    return (parts[-2], parts[-1]) if len(parts) >= 4 else ("anyone", "")


def batch_of_key(key: str) -> str:
    """correlation_key 去掉派给谁的那两段：同一次召回（同一个来由、同一次运行、同一条依据）给各个主人开的那一批待办是同一个
    值。全局渠道按它只推一次（notify/service.notify_recall）。"""
    parts = (key or "").split(":")
    return ":".join(parts[:-2]) if len(parts) >= 4 else key or ""


async def task_payload(session: Any, task: Any, principal: Any) -> dict[str, Any]:
    """recall 待办给处理人看的内容（待办箱、待办详情的 payload，process/tasks._describe 调）：旧口径的值、新口径的值、两版之差
    和原答案当时的值（每个都带出处：哪次运行、哪张卡、哪个指标，点得开），这一组的参数，能做的动作。使用者看到的值照样
    遮罩（按那次运行查到过的遮罩值，同账本接口）。"""
    from app.api.evidence import _sealed
    from app.ledger.view import _hider

    run = await session.get(Run, task.run_id)
    origin = run.origin if run is not None and isinstance(run.origin, dict) else {}
    key = task.correlation_key or ""
    reason = key.split(":", 1)[0] if key.split(":", 1)[0] in REASONS else "caliber"
    base: dict[str, Any] = {"kind": TASK_KIND, "reason": reason, "recall_run": task.run_id,
                            "workflow_id": origin.get("workflow_id"), "from_version": origin.get("from_version"),
                            "to_version": origin.get("to_version")}
    if origin.get("workflow_id"):
        base["workflow_name"] = await ledger_uses.workflow_name(session, str(origin["workflow_id"]))
    pending = task.status == "pending"
    if reason != "caliber":
        return {**base, "basis": await _basis_of(session, task, run, reason),
                "actions": [{"decision": "ack", "available": pending}]}
    builder = getattr(principal, "mode", "off") == "off" or principal.at_least("builder")
    hide = (await _hider(await _sealed(task.run_id))).hide if not builder and run is not None else (lambda _v: False)
    values = await card_values(task.run_id) if run is not None else {}
    metrics = []
    for item in compare(origin.get("metrics") or [], values):
        cells = {}
        for side in ("old", "new", "diff"):
            value, text = item[side], item[f"{side}_text"]
            masked = value is not None and hide(value)
            cells[side] = {"value": None if masked else value, "text": "已遮罩" if masked else text,
                           "metric": f"{side}_{item['k']}", "masked": masked}
        metrics.append({"k": item["k"], "name": item["name"], "unit": item["unit"], "changed": item["changed"],
                        **({"row_order": item["row_order"]} if item.get("row_order") else {}),
                        "card": item["card"], "metric": item["metric"], **cells})
    answers = []
    for use in _uses_of(origin, _owner_of_key(key)):
        answers.append({**{k: use.get(k) for k in ("kind", "run_id", "version", "conversation_id", "turn_id",
                                                     "schedule_id", "app_id", "at", "via_workflow", "via_version")
                           if use.get(k) is not None},
                        "kind_text": ledger_uses.KIND_TEXT.get(str(use.get("kind")), str(use.get("kind"))),
                        "original": await original_values(use, origin.get("metrics") or [], hide_for=not builder)})
    binding = [{"field": k, "value": None if hide(v) else v, "masked": bool(hide(v))}
               for k, v in sorted((origin.get("binding") or {}).items())]
    actions = [await reissue_action(session, task, principal, origin), {"decision": "ack", "available": pending}]
    return {**base, "binding": binding, "metrics": metrics, "answers": answers, "actions": actions,
            "outcome": task.outcome}


async def _basis_of(session: Any, task: Any, run: Run | None, reason: str) -> dict[str, Any]:
    """目录、表结构两种 recall 待办说的是什么：目录的读答案运行上那一条 ledger.basis_changed（correlation_key 里记着它的序号），
    表结构的读试算运行的 origin 和它为什么没算出来。"""
    if reason == "structure":
        origin = run.origin if run is not None and isinstance(run.origin, dict) else {}
        return {"workflow_id": origin.get("workflow_id"), "version": origin.get("version"), "table": origin.get("table"),
                "column": origin.get("column"), "change_kind": origin.get("change_kind"), "change": origin.get("change"),
                "source_id": origin.get("source_id"), "error": (run.error if run is not None else None),
                "probe_run": task.run_id}
    parts = (task.correlation_key or "").split(":")
    seq = int(parts[2]) if len(parts) >= 5 and parts[2].isdigit() else None
    data = await session.scalar(select(RunEvent.data).where(
        RunEvent.run_id == task.run_id, RunEvent.type == EventType.LEDGER_BASIS_CHANGED, RunEvent.seq == seq)) \
        if seq is not None else None
    data = data if isinstance(data, dict) else {}
    return {k: data.get(k) for k in ("kind", "source_id", "source", "table", "items", "new_errors", "checks",
                                     "catalog_version") if data.get(k) is not None}


async def original_values(use: Mapping[str, Any], meta: Sequence[Mapping[str, Any]], *,
                          hide_for: bool) -> list[dict[str, Any]]:
    """原答案当时的值（每个比较一项）：正式运行读它封存的口径卡（固定了这一版的读转抄来的那张）；问数回答读等价的那条查询
    快照里那一格（口径卡的指标就是取数第一行某一列时，按列映射找这次的列）。读不出、原答案里没有单独这个数的为空，写为什么。
    hide_for：给使用者看，按那次运行查到过的遮罩值遮。"""
    from app.api.evidence import _sealed, _sealed_cards
    from app.ledger.view import _hider

    run_id = str(use.get("run_id") or "")
    sealed = await _sealed(run_id)
    hide = (await _hider(sealed)).hide if hide_for else (lambda _v: False)
    out: list[dict[str, Any]] = []
    if use.get("kind") != "answer":
        cards = {node: card for node, _, card in _sealed_cards(sealed)}
        for item in meta:
            card = _card_for(sealed, cards, str(item.get("card")))
            metric = next((m for m in (card or {}).get("metrics") or [] if m.get("id") == item.get("metric")), None)
            out.append(_cell(item, metric.get("value") if metric else None, metric.get("rendered") if metric else None,
                             hide, node=_card_node(sealed, cards, str(item.get("card"))), metric=item.get("metric"),
                             missing=None if metric else "原答案里没有这个指标"))
        return out
    import ast

    from app.core.artifact_store import load
    from app.engine.evidence import render_number
    from app.engine.expressions import cell_parts
    from app.ledger import store
    from app.ledger.labels import candidates_of

    recorded = await store.latest(run_id)
    content = recorded.content if recorded is not None else None
    query = next((q for q in (content or {}).get("queries") or [] if q.get("artifact") == use.get("query")), None)
    # 整条证明了的、逐格证明了的都算：口径的哪一列对这次的哪一列，按各自的列映射找（逐格的只映射那一列）
    proven = [c for c in candidates_of(query or {})
              if c.get("status") == "proven" and c.get("node_id") == use.get("node_id")]
    try:
        snap = load(str(use.get("query") or "")) if use.get("query") else None
    except ValueError:
        snap = None
    columns = [str(c) for c in (snap or {}).get("columns") or []] if isinstance(snap, dict) else []
    rows = (snap or {}).get("rows") if isinstance(snap, dict) else None
    for item in meta:
        value = text = missing = None
        expression = str(item.get("expression") or "")
        parts = cell_parts(expression.strip()) if expression else None
        column = None
        if parts is not None and parts[0] == f"nodes.{use.get('node_id')}" and parts[1].strip() == "0":
            try:
                column = ast.literal_eval(parts[2])
            except (ValueError, SyntaxError):
                column = None
        ours = next((m.get("ours") for c in proven for m in c.get("columns") or [] if m.get("caliber") == column),
                    None) if isinstance(column, str) else None
        if ours in columns and isinstance(rows, list) and rows and isinstance(rows[0], (list, tuple)):
            value = rows[0][columns.index(ours)]
            try:
                text = render_number(value, unit=str(item.get("unit") or ""), decimals=item.get("decimals"),
                                     fmt=item.get("format"))
            except Exception:  # noqa: BLE001 - 渲染不出就写原值
                text = str(value)
        else:
            missing = "原答案里没有单独的这个数"
        out.append(_cell(item, value, text, hide, query=use.get("query"), column=ours, missing=missing))
    return out


def _card_for(sealed: Any, cards: Mapping[str, Any], card_id: str) -> Mapping[str, Any] | None:
    node = _card_node(sealed, cards, card_id)
    return cards.get(node) if node else None


def _card_node(sealed: Any, cards: Mapping[str, Any], card_id: str) -> str | None:
    """正式运行里这张口径卡的节点：同 id 的（W 自己的运行），或者 caliber_from 固定到它的那张（固定了这一版的工作流）。"""
    if card_id in cards:
        return card_id
    for node in (sealed.graph or {}).get("nodes") or []:
        cfg = (node.get("data") or {}).get("config") if isinstance(node, dict) and isinstance(node.get("data"), dict) \
            else None
        ref = cfg.get("caliber_from") if isinstance(cfg, dict) else None
        if isinstance(ref, dict) and ref.get("node_id") == card_id and node.get("id") in cards:
            return str(node["id"])
    return None


def _cell(item: Mapping[str, Any], value: Any, text: Any, hide: Any, *, missing: str | None = None,
          **where: Any) -> dict[str, Any]:
    masked = value is not None and bool(hide(value))
    out = {"k": item.get("k"), "value": None if masked else value, "text": "已遮罩" if masked else text,
           "masked": masked, **{k: v for k, v in where.items() if v is not None}}
    if missing:
        out["missing"] = missing
    return out


# ==========================================================================
# 答案上的召回标记（账本接口 GET /api/runs/{id}/ledger 的 recall，ledger/view.run_view）
# ==========================================================================


async def answer_marks(session: Any, run_id: str, principal: Any) -> dict[str, Any]:
    """一个答案（探索运行、正式运行）身上的召回标记，给问数据的轮次和证据面板「口径」一步（细案做法 7：界面标「依据有变」、
    「已按 vN 核对，不影响」）：

    - checked：数没变、追加在这次运行上的 ledger.recalled（role=answer）——按哪份口径、从哪一版核对到哪一版、哪次召回、什么时候；
    - basis：ledger.basis_changed——哪一种依据（目录事实、表结构、取值、处理时改了目录）、几项、按新目录新出现几条错误级问题、
      什么时候；
    - tasks：看的这个人手上还没处理的、说的是这个答案的 recall 待办（数变了的那种挂在召回运行上，列着这个答案；目录有变的
      挂在这次运行上），界面链到待办箱。

    不放数（新旧值、差值只在待办里给，按遮罩换过），不放表名、取值。"""
    from app.process.tasks import Task, _assignments, handles

    checked: list[dict[str, Any]] = []
    basis: list[dict[str, Any]] = []
    rows = (await session.execute(select(RunEvent.type, RunEvent.data, RunEvent.ts).where(
        RunEvent.run_id == run_id, RunEvent.type.in_((EventType.LEDGER_RECALLED, EventType.LEDGER_BASIS_CHANGED)))
        .order_by(RunEvent.seq))).all()
    def at(ts: Any) -> str | None:
        return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds") \
            if isinstance(ts, (int, float)) else None

    workflows: set[str] = set()
    for etype, data, ts in rows:
        data = data if isinstance(data, dict) else {}
        if etype == EventType.LEDGER_RECALLED and data.get("role") == "answer":
            checked.append({k: data.get(k) for k in ("workflow_id", "from_version", "to_version", "recall_run")}
                           | {"changed": bool(data.get("changed")), "at": at(ts)})
            workflows.add(str(data.get("workflow_id") or ""))
        elif etype == EventType.LEDGER_BASIS_CHANGED:
            items = data.get("items")
            basis.append({"kind": data.get("kind"), "items": len(items) if isinstance(items, list) else 0,
                          "new_errors": int(data.get("new_errors") or 0), "at": at(ts)})
    names = dict((await session.execute(select(Workflow.id, Workflow.name).where(
        Workflow.id.in_(sorted(w for w in workflows if w))))).all()) if workflows - {""} else {}
    for item in checked:
        item["workflow_name"] = names.get(str(item.get("workflow_id") or ""))
    # 说的是这个答案、还没处理的 recall 待办：挂在这次运行上的（目录有变），和召回运行 origin.uses 里列着它的（数变了）。
    # 运行 id 是 32 位十六进制，在 uses 的 JSON 文字里找子串不会误中，找到的再逐条核对
    candidates = (await session.execute(
        select(Task, Run.origin).join(Run, Run.id == Task.run_id)
        .where(Task.kind == TASK_KIND, Task.status == "pending",
               (Task.run_id == run_id) | ((Run.origin_kind == ORIGIN)
                                          & json_text(Run.origin, "uses").contains(run_id, autoescape=True)))
        .order_by(Task.created_at, Task.id))).all()
    mine = await _assignments(session, [t.id for t, _ in candidates])
    tasks: list[dict[str, Any]] = []
    for task, origin in candidates:
        key = task.correlation_key or ""
        reason = key.split(":", 1)[0] if key.split(":", 1)[0] in REASONS else "caliber"
        if task.run_id != run_id:
            origin = origin if isinstance(origin, dict) else {}
            if not any(u.get("run_id") == run_id for u in _uses_of(origin, _owner_of_key(key))):
                continue
        if not handles(mine.get(task.id, []), principal):
            continue
        tasks.append({"id": task.id, "reason": reason,
                      **({"to_version": (origin or {}).get("to_version")} if reason == "caliber" else {})})
    return {"checked": checked, "basis": basis, "tasks": tasks}


# ==========================================================================
# 「按新版重新出具」「知道了」
# ==========================================================================


async def _reissue_plan(session: Any, task: Any, principal: Any, origin: Mapping[str, Any]) -> Any:
    """按新版重新出具的打算（走按口径出具同一套判法，ledger/issue.check_target：已发布未退役、没有人工节点和写入、参数过
    校验、使用者只限有权使用的应用固定着的那一版、每日上限）。来源记成这条待办列的第一个答案。"""
    from app.ledger import issue as ledger_issue
    from app.ledger.labels import caliber_key

    workflow_id, version = str(origin.get("workflow_id") or ""), int(origin.get("to_version") or 0)
    plan = ledger_issue.Plan(run_id=task.run_id, workflow_id=workflow_id, version=version,
                             caliber=caliber_key(workflow_id, version), how="recall",
                             binding=dict(origin.get("binding") or {}))
    workflow = await session.get(Workflow, workflow_id)
    plan.workflow_name = workflow.name if workflow is not None else ""
    answers = _uses_of(origin, _owner_of_key(task.correlation_key or ""))
    source = next((u["run_id"] for u in answers if u.get("kind") == "answer"), None) \
        or next((u["run_id"] for u in answers), None) or task.run_id
    await ledger_issue.check_target(session, plan, workflow, principal=principal, source_run=str(source))
    return plan, str(source)


async def reissue_action(session: Any, task: Any, principal: Any, origin: Mapping[str, Any]) -> dict[str, Any]:
    """待办里「按新版重新出具」这一项：能不能、不能的原因、已经出具过的那一次。"""
    out: dict[str, Any] = {"decision": "reissue", "available": False}
    if task.status != "pending":
        issued = (task.outcome or {}).get("issued_run")
        return {**out, **({"issued_run": issued} if issued else {})}
    plan, _ = await _reissue_plan(session, task, principal, origin)
    out["available"] = plan.reason is None
    if plan.reason:
        out["reason"] = plan.reason
    if plan.existing:
        out["issued_run"] = plan.existing
    return out


async def reissue(task_id: str, principal: Any) -> Run:
    """按新版重新出具：经 launch_run 发起一次正式运行（来源 ledger_issue，origin 记来源答案、召回运行、这条待办）。同一个来源、
    同一版、同一组参数出具过的交回那一次。不能出具抛 RecallRefused。"""
    from app.engine.launch import LaunchRefused, launch_run
    from app.ledger import issue as ledger_issue
    from app.process.tasks import Task

    async with SessionLocal() as session:
        task = await session.get(Task, task_id)
        if task is None:
            raise RecallRefused(404, "待办不存在")
        run = await session.get(Run, task.run_id)
        origin = run.origin if run is not None and isinstance(run.origin, dict) else {}
        plan, source = await _reissue_plan(session, task, principal, origin)
        if plan.reason is not None:
            raise RecallRefused(plan.status, plan.reason, plan.code)
        if plan.existing is not None:
            return await session.get(Run, plan.existing)
        from app.apps.resolve import input_key

        try:
            return await launch_run(
                session, workflow_id=plan.workflow_id, run_class="formal", version=plan.version,
                input_payload=plan.inputs, actor=principal.username, origin_kind=ledger_issue.ORIGIN,
                origin={"source_run": source, "recall_run": task.run_id, "task_id": task_id, "caliber": plan.caliber,
                        "how": "recall", "binding_hash": input_key(plan.binding), "auto": False,
                        **({"granted_by_app": plan.granted_by_app} if plan.granted_by_app else {})},
                expect_hash=plan.expect_hash)
        except LaunchRefused as e:
            raise RecallRefused(e.status_code, e.message, e.code or REFUSED) from e


# ==========================================================================
# 二、目录事实被驳回或修改
# ==========================================================================


def fact_changes(before: Mapping[str, Any] | None, after: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """这次写入改动了的影响数字的项：写入前看得到（没驳回）、写入后被驳回、删掉或改了值的。{path, label, change}。纯函数。"""
    from app.data import catalog

    old, new = catalog._slots(before), catalog._slots(after)
    out: list[dict[str, Any]] = []
    for key, item in old.items():
        if item.get("status") == "rejected":
            continue
        fact = key[0] == "r" or (key[0], key[-1]) in FACT_SLOTS
        if not fact:
            continue
        now = new.get(key)
        if now is None:
            change = "removed"
        elif now.get("status") == "rejected":
            change = "rejected"
        elif catalog._value_of(key, now) != catalog._value_of(key, item):
            change = "changed"
        else:
            continue
        from app.data.catalog_review import slot_path

        out.append({"path": slot_path(key), "label": catalog.describe_slot(key), "change": change})
    return out


#: 查询快照一页取几行（按运行结束时间从新到旧翻，每张表凑够 BASIS_MAX_RUNS 次运行就不再往下翻）
SNAPSHOT_PAGE = 500


async def _explore_snapshots(session: Any, source: Any, tables: Iterable[str], *, before: datetime | None = None
                             ) -> dict[str, list[tuple[str, str]]]:
    """查过 source 这几张表的探索运行（计入统计的、成功封存的）和那几条查询快照：{表: [(运行, 快照)]}，新的在前，每张表最多
    BASIS_MAX_RUNS 次运行。表名按 name_key 比，表已经不在表结构里也认（表被删了正要找用过它的）。

    数据源在库里就筛掉（引用的 meta 里记着源 id 和源名，query_snapshot_meta）：部署久了查询快照有几万条，别的源的不该读回来；
    按运行结束时间、运行 id 排序分页翻（同一次运行的几条挨在一起），每张表都凑够了就停。表名不在库里筛：meta 里的表名是
    SQL 里写的样子（带 schema 前缀、大小写不一），按 name_key 比只能在这里做。"""
    from sqlalchemy import or_

    from app.data.catalog import QUERY_SNAPSHOT_KIND
    from app.data.names import name_key
    from app.db.models import Artifact
    from app.engine.origin import counted_runs

    want: dict[str, list[str]] = {}
    for t in dict.fromkeys(tables):
        want.setdefault(name_key(t), []).append(t)
    out: dict[str, list[tuple[str, str]]] = {t: [] for names in want.values() for t in names}
    runs: dict[str, set[str]] = {t: set() for t in out}
    stmt = (select(Artifact.id, Artifact.meta, Run.id, Run.finished_at).join(Run, Run.id == Artifact.run_id)
            .where(Artifact.kind == QUERY_SNAPSHOT_KIND, Run.run_class == "exploratory", Run.status == "succeeded",
                   Run.manifest_hash.is_not(None), counted_runs(),
                   or_(json_eq(Artifact.meta, "source_id", source.id),
                       json_eq(Artifact.meta, "source", source.name)))
            .order_by(Run.finished_at.desc(), Run.id, Artifact.id))
    cut = _aware(before)
    offset = 0
    last_run: str | None = None
    while want:
        page = (await session.execute(stmt.limit(SNAPSHOT_PAGE).offset(offset))).all()
        offset += len(page)
        for artifact, meta, run_id, finished in page:
            if run_id != last_run and all(len(seen) >= BASIS_MAX_RUNS for seen in runs.values()):
                return out
            last_run = run_id
            if cut is not None and (_aware(finished) is None or _aware(finished) > cut):
                continue
            meta = meta if isinstance(meta, dict) else {}
            if meta.get("source_id") != source.id and meta.get("source") != source.name:
                continue
            hit = {name_key(str(t).rsplit(".", 1)[-1]) for t in meta.get("tables") or []}
            for table in (t for key in hit & want.keys() for t in want[key]):
                if run_id not in runs[table] and len(runs[table]) >= BASIS_MAX_RUNS:
                    continue
                runs[table].add(run_id)
                # 翻页之间进来了新的运行，偏移会错开一两行：同一条不收两次
                if (run_id, artifact) not in out[table]:
                    out[table].append((run_id, artifact))
        if len(page) < SNAPSHOT_PAGE:
            break
    return out


def _frozen_of(snapshot_id: str, cache: dict[str, Any] | None = None
               ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """查询快照 → (快照内容, 它当时冻结的表结构快照)。读不出为 None。cache 是这一遍里读过的工件（{工件 id: 内容}）：工件按内容
    寻址、不会变，几十次运行冻结的常常是同一份表结构快照（大库的能有几 MB），同一遍里只读一次。"""
    from app.core.artifact_store import load

    def read(artifact_id: str) -> Any:
        if cache is not None and artifact_id in cache:
            return cache[artifact_id]
        try:
            content = load(artifact_id)
        except ValueError:
            content = None
        if cache is not None:
            cache[artifact_id] = content
        return content

    snap = read(snapshot_id)
    if not isinstance(snap, dict):
        return None, None
    schema = read(str(snap["schema_artifact"])) if snap.get("schema_artifact") else None
    return snap, schema if isinstance(schema, dict) else None


def _visible(frozen: Mapping[str, Any] | None, table: str, paths: Iterable[str]) -> list[str]:
    """冻结的目录里这张表看得到这几项里的哪几项。"""
    from app.data import catalog
    from app.data.names import name_key

    entries = (frozen or {}).get("catalog") if isinstance((frozen or {}).get("catalog"), dict) else {}
    notes = next((e.get("notes") for t, e in entries.items() if name_key(t) == name_key(table)
                  and isinstance(e, dict)), None)
    slots = catalog._slots(notes if isinstance(notes, dict) else {})
    out = []
    for path in paths:
        try:
            key = catalog.parse_path(path)
        except catalog.CatalogPathError:
            continue
        if key in slots and slots[key].get("status") != "rejected":
            out.append(path)
    return out


def new_errors(sql: str, frozen: Mapping[str, Any], current: Mapping[str, Mapping[str, Any]],
               kind: str | None) -> list[dict[str, Any]]:
    """这条 SQL 按新目录（current：现在的目录）跑 SQL 检查，比按冻结的目录多出来的错误级问题。纯计算。"""
    from app.data.sqlcheck import SqlChecker, frozen_checker

    def key(c: Any) -> tuple[Any, ...]:
        return (c.code, (c.table or "").lower(), (c.column or "").lower(), c.relation_id)

    old = frozen_checker(frozen, kind=kind)
    before = {key(c) for c in (old.check(sql) if old is not None else []) if c.level == "error"}
    cache = {"tables": frozen.get("tables") or {}, **({"schema": frozen["schema"]} if frozen.get("schema") else {})}
    after = SqlChecker(kind=kind, schema_cache=cache, notes=dict(current), source_name=frozen.get("source")).check(sql)
    return [{"code": c.code, "message": c.message, "table": c.table, **({"column": c.column} if c.column else {})}
            for c in after if c.level == "error" and key(c) not in before]


async def _basis_marked(session: Any, run_id: str, match: Mapping[str, Any]) -> bool:
    rows = (await session.execute(select(RunEvent.data).where(
        RunEvent.run_id == run_id, RunEvent.type == EventType.LEDGER_BASIS_CHANGED))).scalars()
    return any(isinstance(d, dict) and all(d.get(k) == v for k, v in match.items()) for d in rows)


async def _answer_owner(session: Any, run_id: str) -> list[tuple[str, str]]:
    """一次探索运行（答案）的主人：对话的 created_by，没有的是发起人。"""
    use = ledger_uses.Use(kind="answer", run_id=run_id, version=0, binding={})
    await ledger_uses._chat_owners(session, [use])
    return use.owners


@dataclass
class CatalogEdit:
    """一张表这一次影响数字的改动（目录审阅写入、新码值、处理变化时改了目录）：catalog_changed 一遍里可以有好几张表。"""

    table: str
    changes: Sequence[Mapping[str, Any]]
    catalog_version: int | None = None
    kind: str = "catalog"
    change_id: str | None = None


async def catalog_changed(source_id: str, table: str, changes: Sequence[Mapping[str, Any]], *,
                          catalog_version: int | None, kind: str = "catalog", change_id: str | None = None) -> int:
    """目录里这张表影响数字的几项被驳回或改了（见模块说明二）：冻结目录里看得到其中某项的探索运行追加 ledger.basis_changed；
    按新目录多出错误级问题的开 recall 待办（reason=catalog）。返回标了几次运行。同一个数据源的排着做（_catalog_lock）。"""
    return await catalog_pass(source_id, [CatalogEdit(table=table, changes=changes, catalog_version=catalog_version,
                                                      kind=kind, change_id=change_id)])


#: 同一个数据源的目录核对排着做：{事件循环: {数据源: 锁}}。批量审阅几十张表、几个人同时审阅，每一遍都要翻查询快照、读冻结
#: 的表结构快照，同时跑几十遍会把单进程的服务拖住
_catalog_locks: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict[str, asyncio.Lock]]" = \
    weakref.WeakKeyDictionary()


def _catalog_lock(source_id: str) -> Any:
    from app.cluster import mutex

    return mutex.lock(f"ledger-catalog:{source_id}")


async def catalog_pass(source_id: str, edits: Sequence[CatalogEdit]) -> int:
    """一遍核对同一个数据源的几张表（catalog_changed、审阅之后排着的那一批）：查询快照只翻一次，冻结的表结构快照同一份只读
    一次。返回标了几次运行。"""
    async with _catalog_lock(source_id):
        return await _catalog_pass(source_id, edits)


async def _catalog_pass(source_id: str, edits: Sequence[CatalogEdit]) -> int:
    from app.data import catalog
    from app.db.models import DataSource

    # 目录审阅那一种得有改了的项（路径）才核对；新码值、处理时改了目录的没有逐项路径，照样核对
    edits = [e for e in edits if e.kind != "catalog" or any(c.get("path") for c in e.changes)]
    if not edits:
        return 0
    async with SessionLocal() as session:
        source = await session.get(DataSource, source_id)
        if source is None:
            return 0
        found = await _explore_snapshots(session, source, list(dict.fromkeys(e.table for e in edits)))
        current = {t: e.notes for t, e in (await catalog.read_catalog(session, source_id)).items()}
    cache: dict[str, Any] = {}
    marked = 0
    for edit in edits:
        by_run: dict[str, list[tuple[dict[str, Any], dict[str, Any]]]] = {}
        for run_id, snapshot in found.get(edit.table) or []:
            snap, frozen = await asyncio.to_thread(_frozen_of, snapshot, cache)
            if snap is None or frozen is None:
                continue
            by_run.setdefault(run_id, []).append((snap, frozen))
        marked += await _mark_catalog(source, edit, by_run, current)
    return marked


async def _mark_catalog(source: Any, edit: CatalogEdit,
                        by_run: Mapping[str, list[tuple[dict[str, Any], dict[str, Any]]]],
                        current: Mapping[str, Mapping[str, Any]]) -> int:
    """一张表的这次改动：冻结目录里看得到改动项的运行追加 ledger.basis_changed，按新目录多出错误级问题的开待办。"""
    from app.engine.run_append import append_events

    table, changes = edit.table, edit.changes
    paths = [str(c.get("path")) for c in changes if c.get("path")]
    marked = 0
    labels = {str(c.get("path")): c for c in changes}
    for run_id, items in by_run.items():
        seen = sorted({p for _, frozen in items for p in _visible(frozen, table, paths)}) if paths else []
        if paths and not seen:
            continue
        errors: list[dict[str, Any]] = []
        for snap, frozen in items:
            if isinstance(snap.get("sql"), str):
                errors += new_errors(snap["sql"], frozen, current, source.kind)
        match = {"kind": edit.kind, "source_id": source.id, "table": table,
                 **({"catalog_version": edit.catalog_version} if edit.catalog_version is not None else {}),
                 **({"change": edit.change_id} if edit.change_id else {})}
        async with SessionLocal() as session:
            if await _basis_marked(session, run_id, match):
                continue
        data = {**match, "source": source.name,
                "items": [{"path": p, "label": labels.get(p, {}).get("label"), "change": labels.get(p, {}).get("change")}
                          for p in seen], "new_errors": len(errors), "checks": errors[:10]}
        written = await append_events(run_id, [(EventType.LEDGER_BASIS_CHANGED, None, data)], require="sealed")
        if not written:
            continue
        marked += 1
        if errors:
            await _open_basis_task(run_id, written[0].seq)
    return marked


async def _open_basis_task(run_id: str, seq: int) -> list[str]:
    """答案依据的目录有变、按新目录检查出了新的错误级问题：给答案的主人开一条 recall 待办（reason=catalog，挂在答案的运行
    上，只有「知道了」）。说的是哪一条依据有变记在 correlation_key 里（那条 ledger.basis_changed 的序号，_basis_of 按它读）。"""
    async with SessionLocal() as session:
        run = await session.get(Run, run_id)
        if run is None:
            return []
        owners = await _answer_owner(session, run_id)
    return await open_tasks(run, reason="catalog", owners=owners, title=CATALOG_TITLE, extra=str(seq))


#: 审阅之后排着的目录改动：{事件循环: {数据源: [CatalogEdit]}}。一个数据源一个后台任务（_drain_catalog），批量审阅逐表写完
#: 逐表调 after_review，这期间（不让出事件循环）进来的几张表合成一遍做；做的时候又进来的，做完这一遍接着做
_catalog_queues: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict[str, list[CatalogEdit]]]" = \
    weakref.WeakKeyDictionary()


def after_review(source_id: str, table: str, before: Mapping[str, Any] | None, after: Mapping[str, Any] | None,
                 version: int | None) -> int:
    """审阅写入之后（data/catalog、catalog_review 调）：有影响数字的项被驳回或改了，排进这个数据源的后台任务标依据有变（同一个
    数据源的几张表合成一遍）。返回改了几项。出错只记日志：审阅已经写进去了。"""
    try:
        changes = fact_changes(before, after)
        if changes:
            queues = _catalog_queues.setdefault(asyncio.get_running_loop(), {})
            edit = CatalogEdit(table=table, changes=changes, catalog_version=version)
            if source_id in queues:
                queues[source_id].append(edit)
            else:
                queues[source_id] = [edit]
                spawn(_drain_catalog(source_id))
        return len(changes)
    except Exception:  # noqa: BLE001 - 辅助步骤
        logger.exception("审阅写入后排队标依据有变时出错（%s · %s）", source_id, table)
        return 0


async def _drain_catalog(source_id: str) -> None:
    """一个数据源排着的目录改动一遍一遍做完。一遍出错只记日志，接着做后面排进来的。"""
    queues = _catalog_queues.setdefault(asyncio.get_running_loop(), {})
    try:
        while edits := queues.get(source_id):
            queues[source_id] = []
            try:
                await catalog_pass(source_id, edits)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - 辅助步骤
                logger.exception("审阅之后标依据有变时出错（%s）", source_id)
    finally:
        queues.pop(source_id, None)


# ==========================================================================
# 三、表结构变动
# ==========================================================================

#: 「依据的表结构已变」认哪几种结构变化（表或列被删、列的类型变了）
STRUCTURE_KINDS = ("table_removed", "column_removed", "column_type")


def basis_kind(change: Mapping[str, Any]) -> str | None:
    """一条变化记录算哪一种依据有变（triggers_recall 认的里面）：structure / values / catalog_edit；别的结构变化（新加了列、
    注释变了……）不动答案，为 None。"""
    from app.data import source_changes

    if not source_changes.triggers_recall(change):
        return None
    if (change.get("resolution") or {}).get("catalog_version"):
        return "catalog_edit"
    if change.get("kind") in STRUCTURE_KINDS:
        return "structure"
    if change.get("kind") == source_changes.CODES_NEW:
        return "values"
    return None


async def process_change(change: Mapping[str, Any]) -> int:
    """处理一条变化记录：用过这张表的探索运行标依据有变；目录有改动的（新码值、处理时改了目录）按新目录查 SQL；表结构变了的
    让受影响的口径试算一次。返回标了几次运行。"""
    kind = basis_kind(change)
    if kind is None or not change.get("table"):
        return 0
    if kind in ("values", "catalog_edit"):
        return await catalog_changed(str(change["source_id"]), str(change["table"]), [],
                                     catalog_version=(change.get("resolution") or {}).get("catalog_version"),
                                     kind=kind, change_id=str(change.get("id")))
    marked = await _mark_structure(change)
    await _probe_calibers(change)
    return marked


async def _mark_structure(change: Mapping[str, Any]) -> int:
    from app.db.models import DataSource
    from app.engine.run_append import append_events

    async with SessionLocal() as session:
        source = await session.get(DataSource, str(change.get("source_id")))
        if source is None:
            return 0
        detected = change.get("detected_at")
        at = datetime.fromisoformat(detected) if isinstance(detected, str) else None
        table = str(change["table"])
        found = (await _explore_snapshots(session, source, [table], before=at)).get(table) or []
    marked = 0
    for run_id in dict.fromkeys(r for r, _ in found):
        match = {"kind": "structure", "change": change.get("id")}
        async with SessionLocal() as session:
            if await _basis_marked(session, run_id, match):
                continue
        data = {**match, "source_id": source.id, "source": source.name, "table": change.get("table"),
                "column": change.get("column"), "change_kind": change.get("kind"), "seq": change.get("seq"),
                "items": [{"label": str(change.get("kind")), "column": change.get("column")}]}
        if await append_events(run_id, [(EventType.LEDGER_BASIS_CHANGED, None, data)], require="sealed") is not None:
            marked += 1
    return marked


async def _probe_calibers(change: Mapping[str, Any]) -> list[str]:
    """表结构变了：最新的已发布版本里查这张表的口径，按最近一次正式运行的输入（没有就按入口的缺省值）派生数据部分试算一次
    （召回运行，origin.probe）。同一条变化、同一版口径只试算一次。返回发起了的运行。"""
    from app.data.names import name_key
    from app.engine.launch import LaunchRefused, launch_run
    from app.ledger import compose, derive
    from app.ledger import match as ledger_match
    from app.tools.datasource import READ_ONLY_OPTION

    table = name_key(str(change.get("table") or "").rsplit(".", 1)[-1])
    launched: list[str] = []
    async with SessionLocal() as session:
        calibers = await ledger_match.load_calibers(session)
    for caliber in calibers:
        if not any(f.source == change.get("source_name") and table in f.tables for f in caliber.fetches):
            continue
        async with SessionLocal() as session:
            done = await session.scalar(select(func.count()).select_from(Run).where(
                Run.origin_kind == ORIGIN, json_flag(Run.origin, "probe"),
                json_eq(Run.origin, "workflow_id", caliber.workflow_id),
                json_eq(Run.origin, "version", caliber.version),
                json_eq(Run.origin, "change", change.get("id"))))
            if done:
                continue
            latest = (await session.execute(select(Run.input).where(
                Run.workflow_id == caliber.workflow_id, Run.version == caliber.version, Run.run_class == "formal",
                Run.status == "succeeded").order_by(Run.finished_at.desc()).limit(1))).scalar_one_or_none()
            inputs_from = "formal" if latest else "defaults"
            if not latest:
                # 这一版还没有正式运行：退回最近一次等价于它的问数回答绑定出的参数，再没有才只用入口的缺省值
                found = await ledger_uses.find_uses(session, caliber.workflow_id, only=caliber.version)
                answer = next((u for u in found.uses if u.kind == "answer"), None)
                if answer is not None:
                    latest, inputs_from = dict(answer.binding), "answer"
            for card in caliber.cards[:1]:
                pinned = None
                try:
                    pinned = await derive.load_pinned(session, caliber.workflow_id, caliber.version, card)
                    names = {str(f.get("name")) for f in pinned.entry_fields}
                    given = {k: v for k, v in (latest or {}).items() if k in names}
                    inputs = compose.check_inputs(pinned, given)
                except (derive.NotDerivable, compose.ComposeRefused) as e:
                    # 凑不齐参数（这一版没有正式运行、也没有等价的问数回答，必填的入口字段又没有缺省值）或者派生不了：不试算，
                    # 照实记一条缺哪几个参数——日志里一条，变化记录的影响里一条（变化页照它写），不再一声不响地跳过
                    await _probe_skipped(change, caliber, pinned, latest, inputs_from, e.message)
                    continue
                probe = derive.caliber_probe(pinned)
                origin = {"probe": 1, "workflow_id": caliber.workflow_id, "version": caliber.version,
                          "change": change.get("id"), "source_id": change.get("source_id"), "table": change.get("table"),
                          "column": change.get("column"), "change_kind": change.get("kind"),
                          "inputs_from": inputs_from, "derive": probe.origin}
                try:
                    run = await launch_run(session, graph=probe.graph, input_payload=inputs, run_class="exploratory",
                                           actor=None, origin_kind=ORIGIN, origin=origin,
                                           options={READ_ONLY_OPTION: True},
                                           workflow_name=f"口径试算：{pinned.workflow_name} v{caliber.version}")
                except LaunchRefused as e:
                    logger.info("表结构变了之后口径试算没能发起（%s v%s）：%s", caliber.workflow_id, caliber.version,
                                e.message)
                    continue
                launched.append(run.id)
    return launched


#: 变化记录 affects 里试算情况的键（data/source_changes 再次探查到同一条变化时保留它）
PROBES_KEY = "probes"


def missing_inputs(pinned: Any, given: Mapping[str, Any] | None) -> list[str]:
    """口径入口里必填、又没给值也没有缺省值的字段（按入口字段的先后）。"""
    out = []
    for f in pinned.entry_fields if pinned is not None else ():
        name = str(f.get("name") or "").strip()
        if not name or not f.get("required") or f.get("default") not in (None, ""):
            continue
        if (given or {}).get(name) in (None, ""):
            out.append(name)
    return out


async def _probe_skipped(change: Mapping[str, Any], caliber: Any, pinned: Any, given: Mapping[str, Any] | None,
                         inputs_from: str, reason: str) -> None:
    """表结构变了、这一版口径没能试算：记一条日志，写进变化记录的 affects.probes（同一版只记一条，换成最新的说法）。"""
    from app.db.models_sources import SourceChange

    missing = missing_inputs(pinned, given)
    name = getattr(pinned, "workflow_name", None) or caliber.workflow_id
    said = f"缺少参数 {'、'.join(missing)}" if missing else reason
    logger.info("表结构变了之后口径「%s」v%s 没有试算：%s（参数来自%s）", name, caliber.version, said,
                {"formal": "最近一次正式运行", "answer": "等价的问数回答", "defaults": "入口的缺省值"}.get(inputs_from, inputs_from))
    note = {"workflow_id": caliber.workflow_id, "version": caliber.version, "name": str(name), "outcome": "skipped",
            "missing": missing, "reason": reason, "inputs_from": inputs_from}
    async with SessionLocal() as session:
        row = (await session.execute(select(SourceChange).where(SourceChange.id == change.get("id")))).scalar_one_or_none()
        if row is None:
            return
        affects = dict(row.affects or {})
        kept = [p for p in affects.get(PROBES_KEY) or [] if isinstance(p, dict)
                and (p.get("workflow_id"), p.get("version")) != (caliber.workflow_id, caliber.version)]
        affects[PROBES_KEY] = [*kept, note]
        row.affects = affects
        await session.commit()


async def _settle_probe(run: Run) -> str:
    """试算运行结束：算出来了只记结论；没算出来给那一版口径的发布人开 recall 待办（reason=structure）。"""
    from app.apps import versions as app_versions

    origin = run.origin if isinstance(run.origin, dict) else {}
    ok = run.status == "succeeded"
    tasks: list[str] = []
    if not ok:
        async with SessionLocal() as session:
            row = await app_versions.version_row(session, str(origin.get("workflow_id")), int(origin.get("version") or 0))
            name = await ledger_uses.workflow_name(session, str(origin.get("workflow_id") or ""))
        publisher = getattr(row, "published_by", None) if row is not None else None
        tasks = await open_tasks(run, reason="structure", owners=[("user", publisher)] if publisher else [],
                                 title=STRUCTURE_TITLE.format(name=name or "口径", version=origin.get("version")))
    await _mark(run.id, {"role": "recall", "probe": True, "outcome": "unchanged" if ok else "failed",
                         "changed": None, "workflow_id": origin.get("workflow_id"), "version": origin.get("version"),
                         "tasks": tasks, **({} if ok else {"error": str(run.error or "")[:300]})})
    return "unchanged" if ok else "failed"


async def _cursor(session: Any) -> int:
    from app.db.models import Setting

    row = await session.get(Setting, CURSOR_KEY)
    value = (row.value or {}).get("seq") if row is not None and isinstance(row.value, dict) else None
    return int(value) if isinstance(value, int) else 0


async def _set_cursor(session: Any, seq: int) -> None:
    from app.db.models import Setting

    row = await session.get(Setting, CURSOR_KEY)
    if row is None:
        session.add(Setting(key=CURSOR_KEY, value={"seq": int(seq)}))
    else:
        row.value = {"seq": int(seq)}
    await session.commit()


_consuming: set[asyncio.AbstractEventLoop] = set()


async def consume_source_changes() -> int:
    """按 seq 游标拉变化记录（data/source_changes.list_changes），逐条处理（process_change），游标前移。探查、剖析任务结束时
    调。同一时刻只拉一份。返回处理了几条。"""
    from app.data import source_changes

    if not automatic():
        return 0
    loop = asyncio.get_running_loop()
    if loop in _consuming:
        return 0
    _consuming.add(loop)
    try:
        # 多进程：几个进程的探查、剖析结束时都会来拉，游标是设置表里先读后写的一行——跨进程的锁里一份接一份，后来的接着游标往下拉
        from app.cluster import mutex

        async with mutex.lock("ledger-consume-changes"):
            return await _consume(source_changes)
    finally:
        _consuming.discard(loop)


async def _consume(source_changes: Any) -> int:
    count = 0
    while True:
        async with SessionLocal() as session:
            since = await _cursor(session)
            batch = await source_changes.list_changes(session, since_seq=since, limit=200)
        if not batch:
            return count
        for change in batch:
            try:
                if basis_kind(change) is not None:
                    await process_change(change)
                    count += 1
            except Exception:  # noqa: BLE001 - 一条出错不挡后面的
                logger.exception("召回处理变化记录 #%s 出错", change.get("seq"))
        async with SessionLocal() as session:
            await _set_cursor(session, int(batch[-1]["seq"]))


# ==========================================================================
# 发布钩子、启动时接着做、测试用的 drain
# ==========================================================================


def automatic() -> bool:
    """自动召回（发布后自动那一批、探查剖析之后拉表结构变化、启动时接着做）开没开：和调度循环同一个开关
    （AGENTLAB_SCHEDULER）。这几样都是在后台替人查业务库（召回运行、口径试算），测试、拷来的数据目录（检查脚本）不做——
    同 ledger/match 的启动补比、定时任务。手动「接着算」、审阅之后标依据有变（只读应用库）不受它管。"""
    from app.core.config import settings

    return bool(settings.scheduler)


def after_publish(workflow_id: str, version: int, actor: str | None) -> bool:
    """工作流发布了第 version 版（api/workflows.commit_publish 提交之后）：在后台按计划自动召回一批（automatic 关着时不做，
    版本历史里照样列出受影响的答案、能手动接着算）。不挡发布。返回排没排上。"""
    if not automatic():
        return False
    try:
        spawn(launch(workflow_id, int(version), batch="auto", actor=actor))
        return True
    except Exception:  # noqa: BLE001 - 辅助步骤
        logger.exception("工作流 %s 发布 v%s 后排队召回出错", workflow_id, version)
        return False


async def resume(*, days: int = RESUME_DAYS) -> dict[str, int]:
    """启动时：最近 days 天发布过、现在仍是当前发布版的版本，自动那一批没发完的接着发（之后又发布了更新的版本的不再接着做：
    新版本的召回本来就覆盖更早的所有版本）；结束了还没收尾的召回运行补上收尾；表结构变化的游标没有时从现在开始（升级之前的
    变化不追）。"""
    from app.db.models import WorkflowVersion

    since = utcnow() - timedelta(days=days)
    async with SessionLocal() as session:
        rows = (await session.execute(select(WorkflowVersion.workflow_id, WorkflowVersion.version, WorkflowVersion.published_by)
                                      .join(Workflow, Workflow.id == WorkflowVersion.workflow_id)
                                      .where(WorkflowVersion.published_at >= since,
                                             WorkflowVersion.level.is_not(None),
                                             Workflow.published_version == WorkflowVersion.version))).all()
        unsettled = [r for (r,) in (await session.execute(select(Run.id).where(
            Run.origin_kind == ORIGIN, Run.status.in_(("succeeded", "failed", "cancelled")),
            Run.manifest_hash.is_not(None)))).all()]
        from app.db.models import Setting

        if await session.get(Setting, CURSOR_KEY) is None:
            from app.db.models_sources import SourceChange

            top = int(await session.scalar(select(func.max(SourceChange.seq))) or 0)
            await _set_cursor(session, top)
    launched = 0
    for workflow_id, version, actor in rows:
        launched += len(await launch(workflow_id, int(version), batch="auto", actor=actor))
    settled = 0
    for run_id in unsettled:
        settled += await settle(run_id) is not None
    return {"launched": launched, "settled": settled}


def start_resume() -> bool:
    """启动时在后台接着做（engine/runner 的 setup 调）。和调度循环同一个开关（automatic）：拷来的数据目录、测试不替原库补做
    后台的事。"""
    if not automatic():
        return False
    spawn(resume())
    return True


def _mine() -> list[asyncio.Task[Any]]:
    loop = asyncio.get_running_loop()
    return [t for t in _background if t.get_loop() is loop]


async def drain() -> None:
    """等这个事件循环里召回的后台任务都做完（测试用）。"""
    while True:
        live = [t for t in _mine() if not t.done()]
        if not live:
            return
        await asyncio.gather(*live, return_exceptions=True)


async def shutdown() -> None:
    tasks = _mine()
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


__all__ = ["BASIS_KINDS", "BATCHES", "CURSOR_KEY", "OUTCOMES", "ORIGIN", "REASONS", "TASK_KIND", "CatalogEdit",
           "Group", "Pair", "Plan", "RecallRefused", "after_publish", "after_review", "answer_marks", "automatic",
           "basis_kind", "batch_of_key", "batches", "build_graph", "build_plan", "card_values", "catalog_changed",
           "catalog_pass", "compare", "consume_source_changes", "drain", "fact_changes", "launch", "new_errors",
           "normalize", "open_tasks", "original_values", "outcome_of", "preview", "process_change", "recall_card",
           "recall_runs", "reissue", "reissue_action", "resume", "schedule_settle", "settle", "shutdown",
           "source_cards", "spawn", "start_resume", "task_payload", "version_recall_counts"]
