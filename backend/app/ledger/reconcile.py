"""口径对账：路径二b，差在哪、差多少（docs/design/next-round/02-口径账本.md 做法 6，总方案 X3）。

**差在哪**在比对时就有（ledger/match：explain_sql_diff 的条目，这次 / 口径两边说），不跑查询。**差多少**要点开才跑：
按那一版口径确定性派生一张只读小图（ledger/derive.reconcile_graph，全站唯一的派生图生成器），跑一次探索运行
（来源 reconcile，服务端专用、系统派生、不进使用统计）：

  入口（口径的入口字段，缺省值是这次的参数）→「这次的取数」ours_q1（执行过的 SQL 原文）→ 口径的取数（模板里保留
  {{ input.x }}）→ 口径卡（caliber_from 固定那一版）→（多行时）按维度对齐的合并查询 align → 对账卡（这次、口径、差值、
  相对差）→ 出口（契约 metrics_from 指向对账卡；不加报告、不调模型）

两边在同一次运行里查，差值在对账卡里算（叙述层无算术，也不在 Python 里算：compare.py 是同一工作流两次运行配对用的）。
面板上每个数都点得开 /evidence/metrics 的出处链。

**参数。** 默认用比对时绑定出来的参数（缺的补缺省值）。参数是启发得来的（绑定在 heuristic 一级配上）、有没绑上的、补完
缺省值表单还不齐、或者里面有遮罩值时，要人先核对：动作里写 needs_check 和原因，发起时要带 confirm 和 inputs（按口径入口
的字段校验，ledger/compose.check_inputs 同一套），界面写「参数按这次的 SQL 推断，请核对」。

**列怎么配。** 只有一行结果时：口径卡上的指标式就是取数第一行某一列（cell(nodes.取数, 0, '列')）的，按那一列在口径取数
结果里的位置配这次结果同一位置的列（等价时已实测列的先后计；不等价的两条 SQL 选择列表一样时位置照样对得上）。差异
有兜底说明（fallback：两条 SQL 对不上结构）、列数不同、一个都配不上时不猜，要人选（pairs：{ours: 这次的列, metric: 口径卡
上的指标 id}），这个选择记进对账运行的 origin。

**多行结果**（这次的结果不止一行）：被报告引用了的那几行（最多 MAX_ROWS 行），按维度列（不是数的列）经合并查询和口径
取数的结果按键对齐（LEFT JOIN，按引用的先后排），每个被引用的行、每个被引用的列出一组对账指标。两边的列按位置配（列数
不同时不猜，照实拒绝）。维度列是遮罩列、或者这次的结果里合并键不唯一时照实拒绝：前者对齐要用到遮住的值，后者对不准
是哪一行。被引用的列是遮罩列的同样不对账。

只读：派生图只查库、不写；使用者发起时带上 MASK_RESULTS / READ_ONLY（同问数据），搭建者也带 READ_ONLY。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select

from app.db.base import SessionLocal
from app.db.models import Run, Workflow

ORIGIN = "reconcile"
#: 被拒的机器码
REFUSED = "ledger_reconcile_refused"
CHECK_INPUTS = "ledger_reconcile_check_inputs"
NEEDS_PAIRS = "ledger_reconcile_needs_pairs"
FORBIDDEN = "ledger_reconcile_forbidden"
#: 多行对账最多几行、几列（每行每列出四个对账指标）
MAX_ROWS = 10
MAX_COLUMNS = 5

#: 给人看的说法
NOT_FOUND = "运行记录不存在"
NO_ACCESS = "没有权限查看这次运行"
NOT_MATCHED = "这次运行还没有比对口径，比对完才能对账"
NO_QUERY = "这次运行的比对记录里没有这条查询"
NOT_RELATED = "这条查询没有和「{name}」v{version} 比出差异（只有相关口径才对账）"
NO_CARD = "「{name}」v{version} 里没有读这个取数节点的口径卡，对不了账"
SNAPSHOT = "这条查询的快照读不出来（文件缺失或与哈希不一致），对不了账"
CHECK_REASON = "参数按这次的 SQL 推断，请核对"
CHECK_MASKED = "参数里有遮罩值，请核对参数"
CHECK_INCOMPLETE = "按这次的 SQL 没能推断出全部参数，请补全并核对"
CONFIRM_FIRST = "参数按这次的 SQL 推断，发起前请先核对参数"
PAIRS_FALLBACK = "两条 SQL 的结构对不上，认不出这次的哪一列对应口径的哪个指标，请选择要对照的指标"
PAIRS_NONE = "认不出这次的哪一列对应口径的哪个指标，请选择要对照的指标"
PAIR_COLUMN = "这次的结果里没有列「{column}」"
PAIR_MASKED = "列「{column}」设置了遮罩，不能对账"
PAIR_METRIC = "口径卡「{card}」上没有指标「{metric}」"
PAIRS_SHAPE = "要对照的指标写成 [{ours: 这次的列, metric: 口径卡上的指标 id}]"
ROWS_NONE = "报告里没有引用这条查询的格，不知道要对哪几行"
ROWS_NO_KEY = "这次的结果有多行，但没有可以用来对齐的维度列（不是数的列）"
ROWS_MASKED_KEY = "用来对齐的维度列「{column}」设置了遮罩，不能对账"
ROWS_DUPLICATE = "这次的结果里按维度列（{columns}）有重复的行，对不准是哪一行"
ROWS_COLUMNS = "这次的结果和口径的取数列数不同（{ours} 列对 {theirs} 列），按位置对不上"
ROWS_CALIBER_COLUMNS = "认不出口径取数的结果列，按位置对不上"


class ReconcileRefused(Exception):
    def __init__(self, status_code: int, message: str, code: str = REFUSED) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.code = code


# ==========================================================================
# 动作：GET 账本里能对的账
# ==========================================================================


def _needs_check(cand: dict[str, Any], fields: list[dict[str, Any]], hide: Any) -> str | None:
    """参数要不要人先核对：要的回原因（见模块说明），不要的为 None。fields 是比对工件里记的入口字段（name、required、
    has_default）。"""
    binding = cand.get("binding") or {}
    if hide is not None and any(hide(v) for v in binding.values()):
        return CHECK_MASKED
    if cand.get("unbound") or not binding and any(f.get("name") for f in fields):
        return CHECK_INCOMPLETE
    if any(f.get("required") and not f.get("has_default") and f.get("name") not in binding for f in fields):
        return CHECK_INCOMPLETE
    if cand.get("via") in (None, "heuristic"):
        return CHECK_REASON
    return None


def actions_of(content: dict[str, Any], names: dict[str, str], hide: Any = None) -> list[dict[str, Any]]:
    """比对工件里每条有相关口径的查询、每份相关口径一项「看差多少」：{kind: reconcile, query, workflow_id, version, node_id,
    needs_check, check_reason?, binding（遮过的）}。界面据此摆按钮、要不要先核对参数。"""
    from app.ledger.labels import LEVEL_TEXT, binding_view

    hide = hide or (lambda _value: False)
    fields = {c.get("caliber"): c.get("fields") or [] for c in content.get("calibers") or []}
    out: list[dict[str, Any]] = []
    for query in content.get("queries") or []:
        if query.get("status") not in ("related",):
            continue
        for cand in query.get("candidates") or []:
            if cand.get("status") != "related" or cand.get("comparable") is False:
                continue
            why = _needs_check(cand, list(fields.get(cand.get("caliber")) or []), hide)
            item = {"kind": "reconcile", "query": query.get("artifact"), "caliber": cand.get("caliber"),
                    "workflow_id": cand.get("workflow_id"), "version": cand.get("version"),
                    "node_id": cand.get("node_id"), "workflow_name": names.get(str(cand.get("workflow_id") or "")),
                    "level_text": LEVEL_TEXT.get(str(cand.get("level") or ""), ""),
                    "needs_check": why is not None, "binding": binding_view(cand.get("binding"), hide),
                    "fallback": bool((cand.get("diff") or {}).get("fallback"))}
            if why is not None:
                item["check_reason"] = why
            out.append(item)
    return out


async def reconciled_runs(session: Any, run_id: str) -> list[Run]:
    """对这次运行对过的账（origin.source_run 是它），新的在前。"""
    rows = (await session.execute(select(Run).where(Run.origin_kind == ORIGIN).order_by(Run.created_at.desc()))).scalars()
    return [r for r in rows if isinstance(r.origin, dict) and r.origin.get("source_run") == run_id]


def reconciled_view(run: Run) -> dict[str, Any]:
    origin = run.origin if isinstance(run.origin, dict) else {}
    return {"run_id": run.id, "status": run.status, "query": origin.get("query"), "caliber": origin.get("caliber"),
            "workflow_id": origin.get("workflow_id"), "version": origin.get("version"),
            "inputs_from": origin.get("inputs_from"), "rows": origin.get("rows"),
            "created_at": run.created_at.isoformat() if run.created_at else None}


# ==========================================================================
# 派生图
# ==========================================================================


@dataclass
class Pair:
    """一组对照：这次结果的哪一列，对口径卡上的哪个指标（metric）或口径取数结果的哪一列（column）。"""

    ours: str
    metric: str | None = None
    column: str | None = None
    #: 口径卡上那个指标的位置（metric 配对时）
    index: int | None = None
    name: str = ""
    unit: str = ""
    decimals: Any = None

    def describe(self) -> dict[str, Any]:
        return {"ours": self.ours, **({"metric": self.metric} if self.metric else {}),
                **({"caliber": self.column} if self.column else {})}


@dataclass
class Plan:
    """一次对账要派生的东西（纯计算的结果，测试锁住确定性）。"""

    pairs: list[Pair]
    rows: list[int] | None = None
    keys: list[tuple[str, str]] = field(default_factory=list)
    key_values: list[tuple[Any, ...]] = field(default_factory=list)


def _lit(value: Any) -> str:
    """一个值写进合并查询（SQLite）的字面量。"""
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return repr(value)
    return "'" + str(value).replace("'", "''") + "'"


def _ident(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def _equals(alias: str, column: str, value: Any) -> str:
    return f"{alias}.{_ident(column)} IS NULL" if value is None else f"{alias}.{_ident(column)} = {_lit(value)}"


def align_sql(keys: list[tuple[str, str]], pairs: list[Pair], values: list[tuple[Any, ...]]) -> str:
    """多行对账的合并查询：这次（o）LEFT JOIN 口径（c）按维度列对齐，只留被引用的那几行，按引用的先后排。"""
    select_ = [f"o.{_ident(ours)} AS {_ident(ours)}" for ours, _ in keys]
    select_ += [f"o.{_ident(p.ours)} AS {_ident(f'ours_{j}')}, c.{_ident(p.column or '')} AS {_ident(f'cal_{j}')}"
                for j, p in enumerate(pairs)]
    on = " AND ".join(f"o.{_ident(ours)} = c.{_ident(theirs)}" for ours, theirs in keys)
    rows = ["(" + " AND ".join(_equals("o", ours, v) for (ours, _), v in zip(keys, row)) + ")" for row in values]
    order = " ".join(f"WHEN {cond} THEN {i}" for i, cond in enumerate(rows))
    return (f"SELECT {', '.join(select_)} FROM o LEFT JOIN c ON {on} WHERE {' OR '.join(rows)} "
            f"ORDER BY CASE {order} END")


def _metric(mid: str, name: str, expression: str, *, unit: str = "", decimals: Any = None,
            fmt: str | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"id": mid, "name": name, "unit": unit, "expression": expression}
    if decimals is not None:
        out["decimals"] = decimals
    if fmt:
        out["format"] = fmt
    return out


def _four(n: str, label: str, ours: str, theirs: str, pair: Pair) -> list[dict[str, Any]]:
    """一组对照的四个对账指标：这次、口径、差值（这次 − 口径）、相对差（差值 ÷ 口径，口径是 0 时没有值）。"""
    diff = f"({ours}) - ({theirs})"
    return [
        _metric(f"ours_{n}", f"{label} · 这次", ours, unit=pair.unit, decimals=pair.decimals),
        _metric(f"cal_{n}", f"{label} · 口径", theirs, unit=pair.unit, decimals=pair.decimals),
        _metric(f"diff_{n}", f"{label} · 差值", diff, unit=pair.unit, decimals=pair.decimals),
        _metric(f"rel_{n}", f"{label} · 相对差", f"({diff}) / ({theirs}) if ({theirs}) else None", decimals=4,
                fmt="percent_of_ratio"),
    ]


def tail_nodes(plan: Plan, *, card_id: str, fetch_id: str) -> list[dict[str, Any]]:
    """派生图的 tail：（多行时）对齐的合并查询 + 对账卡。card_id、fetch_id 是图里口径卡、口径取数节点的 id。"""
    from app.ledger.derive import ALIGN_ID, OURS_ID, RECONCILE_CARD, _node

    metrics: list[dict[str, Any]] = []
    nodes: list[dict[str, Any]] = []
    if plan.rows is None:
        for j, pair in enumerate(plan.pairs):
            ours = f"cell(nodes.{OURS_ID}, 0, {pair.ours!r})"
            theirs = (f"nodes.{card_id}.metrics[{pair.index}].value" if pair.metric is not None
                      else f"cell(nodes.{fetch_id}, 0, {pair.column!r})")
            metrics += _four(str(j), pair.name or pair.ours, ours, theirs, pair)
    else:
        nodes.append(_node(ALIGN_ID, "merge", "按维度对齐", inputs={"o": OURS_ID, "c": fetch_id},
                           sql=align_sql(plan.keys, plan.pairs, plan.key_values)))
        for i, row in enumerate(plan.key_values):
            where = "，".join(f"{ours}={v}" for (ours, _), v in zip(plan.keys, row))
            for j, pair in enumerate(plan.pairs):
                metrics += _four(f"{i}_{j}", f"{where} · {pair.name or pair.ours}",
                                 f"cell(nodes.{ALIGN_ID}, {i}, 'ours_{j}')", f"cell(nodes.{ALIGN_ID}, {i}, 'cal_{j}')",
                                 pair)
    nodes.append(_node(RECONCILE_CARD, "metrics", "对账卡", caliber="口径对账", caliber_version="v1", on_missing="null",
                       metrics=metrics))
    return nodes


def _numeric(rows: list[Any], index: int) -> bool:
    values = [r[index] for r in rows if isinstance(r, (list, tuple)) and index < len(r) and r[index] is not None]
    return bool(values) and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in values)


def _plain_cell(expression: str, fetch: str) -> str | None:
    """指标式就是取数第一行某一列（cell(nodes.<fetch>, 0, '列')）时交回列名，别的写法为 None。"""
    import ast

    from app.engine.expressions import cell_parts

    parts = cell_parts(expression.strip())
    if parts is None or parts[0] != f"nodes.{fetch}" or parts[1].strip() != "0":
        return None
    try:
        column = ast.literal_eval(parts[2])
    except (ValueError, SyntaxError):
        return None
    return column if isinstance(column, str) else None


def plan_pairs(*, ours_columns: list[str], ours_rows: list[Any], hidden: set[str], caliber_columns: list[str] | None,
               metrics: list[dict[str, Any]], fetch: str, card: str, fallback: bool, chosen: Any,
               referenced: list[tuple[int, str]]) -> Plan:
    """配列、（多行时）定行和键：交回 Plan，配不了的抛 ReconcileRefused（见模块说明）。纯计算。"""
    def check_column(column: str) -> None:
        if column not in ours_columns:
            raise ReconcileRefused(422, PAIR_COLUMN.format(column=column))
        if column.lower() in hidden:
            raise ReconcileRefused(409, PAIR_MASKED.format(column=column))

    by_id = {str(m.get("id")): (i, m) for i, m in enumerate(metrics)}
    multi = len(ours_rows) > 1
    if multi:
        return _plan_rows(ours_columns, ours_rows, hidden, caliber_columns, referenced, check_column)
    if chosen is not None:
        if not isinstance(chosen, list) or not chosen or not all(isinstance(p, dict) for p in chosen):
            raise ReconcileRefused(422, PAIRS_SHAPE, NEEDS_PAIRS)
        pairs = []
        for item in chosen:
            ours, metric = str(item.get("ours") or ""), str(item.get("metric") or "")
            check_column(ours)
            if metric not in by_id:
                raise ReconcileRefused(422, PAIR_METRIC.format(card=card, metric=metric))
            index, m = by_id[metric]
            pairs.append(Pair(ours=ours, metric=metric, index=index, name=str(m.get("name") or metric),
                              unit=str(m.get("unit") or ""), decimals=m.get("decimals")))
        return Plan(pairs=pairs)
    if fallback:
        raise ReconcileRefused(409, PAIRS_FALLBACK, NEEDS_PAIRS)
    pairs = []
    if caliber_columns is not None and len(caliber_columns) == len(ours_columns):
        for index, m in enumerate(metrics):
            column = _plain_cell(str(m.get("expression") or ""), fetch)
            if column is None or column not in caliber_columns:
                continue
            ours = ours_columns[caliber_columns.index(column)]
            if ours.lower() in hidden:
                continue
            pairs.append(Pair(ours=ours, metric=str(m.get("id")), index=index, name=str(m.get("name") or m.get("id")),
                              unit=str(m.get("unit") or ""), decimals=m.get("decimals")))
    if not pairs:
        raise ReconcileRefused(409, PAIRS_NONE, NEEDS_PAIRS)
    return Plan(pairs=pairs)


def _plan_rows(ours_columns: list[str], rows: list[Any], hidden: set[str], caliber_columns: list[str] | None,
               referenced: list[tuple[int, str]], check_column: Any) -> Plan:
    if caliber_columns is None:
        raise ReconcileRefused(409, ROWS_CALIBER_COLUMNS)
    if len(caliber_columns) != len(ours_columns):
        raise ReconcileRefused(409, ROWS_COLUMNS.format(ours=len(ours_columns), theirs=len(caliber_columns)))
    keys = [i for i in range(len(ours_columns)) if not _numeric(rows, i)]
    if not keys:
        raise ReconcileRefused(409, ROWS_NO_KEY)
    if masked := [ours_columns[i] for i in keys if ours_columns[i].lower() in hidden]:
        raise ReconcileRefused(409, ROWS_MASKED_KEY.format(column=masked[0]))
    tuples = [tuple(r[i] if isinstance(r, (list, tuple)) and i < len(r) else None for i in keys) for r in rows]
    if len(set(map(json.dumps, [list(t) for t in tuples]))) != len(tuples):
        raise ReconcileRefused(409, ROWS_DUPLICATE.format(columns="、".join(ours_columns[i] for i in keys)))
    wanted_rows = list(dict.fromkeys(r for r, _ in referenced if isinstance(r, int) and 0 <= r < len(rows)))[:MAX_ROWS]
    columns = list(dict.fromkeys(c for _, c in referenced if c in ours_columns and ours_columns.index(c) not in keys))
    if not wanted_rows or not columns:
        raise ReconcileRefused(409, ROWS_NONE)
    for column in columns:
        check_column(column)
    pairs = [Pair(ours=c, column=caliber_columns[ours_columns.index(c)], name=c) for c in columns[:MAX_COLUMNS]]
    return Plan(pairs=pairs, rows=wanted_rows, keys=[(ours_columns[i], caliber_columns[i]) for i in keys],
                key_values=[tuples[r] for r in wanted_rows])


def _referenced(sealed: Any, artifact: str) -> list[tuple[int, str]]:
    """报告里引用了这条查询的哪几格：[(行, 列)]，按正文的先后（单元格、表格里的格）。"""
    from app.api.evidence import _reports
    from app.engine.evidence import iter_units

    out: list[tuple[int, str]] = []
    for report in _reports(sealed):
        doc = report.doc if report.hash_ok is True else None
        if not isinstance(doc, dict):
            continue
        catalog = doc.get("catalog") if isinstance(doc.get("catalog"), dict) else {}
        for _, unit in iter_units(doc):
            for seg in unit.get("segments") or []:
                cite = seg.get("cite") if isinstance(seg, dict) and isinstance(seg.get("cite"), dict) else {}
                entry = catalog.get(cite.get("alias")) if isinstance(catalog.get(cite.get("alias")), dict) else {}
                locator = cite.get("locator") if isinstance(cite.get("locator"), dict) else {}
                if cite.get("kind") == "cell" and entry.get("artifact") == artifact \
                        and isinstance(locator.get("row"), int) and isinstance(locator.get("column"), str):
                    out.append((locator["row"], locator["column"]))
    return out


def _caliber_columns(template: str, inputs: dict[str, Any], fields: list[dict[str, Any]], dialect: str | None,
                     schema: dict[str, Any] | None) -> list[str] | None:
    """口径取数按这次的参数渲染之后选出来的列名（按位置）；认不出的为 None。"""
    from app.data.sql_semantics import _read, _result_columns, _schema_info, render_bound

    try:
        rendered = render_bound(template, inputs, fields)
        return _result_columns(rendered, _read(dialect), _schema_info(schema))
    except Exception:  # noqa: BLE001 - 认不出就按认不出说
        return None


# ==========================================================================
# 发起
# ==========================================================================


@dataclass
class Request:
    query: str
    workflow_id: str
    version: int
    inputs: dict[str, Any] | None = None
    confirm: bool = False
    pairs: Any = None


async def reconcile(run_id: str, req: Request, *, principal: Any) -> Run:
    """对这次运行的一条查询和一份相关口径对一次账：派生只读图、发起探索运行（来源 reconcile）。不能对的抛 ReconcileRefused。"""
    from app.api.deps import can_read_run
    from app.api.evidence import _Masks, _sealed
    from app.core.artifact_store import load
    from app.engine.launch import LaunchRefused, launch_run
    from app.ledger import compose, derive, store
    from app.ledger.issue import _conversation_of
    from app.ledger.labels import caliber_key
    from app.ledger.match import _frozen
    from app.ledger.view import _hider
    from app.tools.datasource import MASK_RESULTS_OPTION, QUERY_PREFIX, READ_ONLY_OPTION

    async with SessionLocal() as session:
        run = await session.get(Run, run_id)
    if run is None:
        raise ReconcileRefused(404, NOT_FOUND)
    if principal.mode != "off" and not await can_read_run(principal, run):
        raise ReconcileRefused(403, NO_ACCESS, FORBIDDEN)
    recorded = await store.latest(run_id)
    if recorded is None or recorded.content is None:
        raise ReconcileRefused(409, NOT_MATCHED)
    content = recorded.content
    key = caliber_key(req.workflow_id, int(req.version))
    query = next((q for q in content.get("queries") or [] if q.get("artifact") == req.query), None)
    if query is None:
        raise ReconcileRefused(404, NO_QUERY)
    async with SessionLocal() as session:
        workflow = await session.get(Workflow, req.workflow_id)
    name = workflow.name if workflow is not None else req.workflow_id
    cand = next((c for c in query.get("candidates") or [] if c.get("caliber") == key and c.get("status") == "related"),
                None)
    if cand is None:
        raise ReconcileRefused(409, NOT_RELATED.format(name=name, version=req.version))
    summary = next((c for c in content.get("calibers") or [] if c.get("caliber") == key), {})
    pinned = None
    async with SessionLocal() as session:
        for card_id in summary.get("cards") or []:
            try:
                candidate = await derive.load_pinned(session, req.workflow_id, int(req.version), str(card_id))
            except derive.NotDerivable:
                continue
            if any(node == cand.get("node_id") for node, _, _ in candidate.reads):
                pinned = candidate
                break
    if pinned is None:
        raise ReconcileRefused(409, NO_CARD.format(name=name, version=req.version))

    # 参数：比对时绑定出来的（补缺省值），要核对的要人带着核对过的参数再来
    sealed = await _sealed(run_id)
    hider = await _hider(sealed)
    why = _needs_check(cand, list(summary.get("fields") or []), hider.hide)
    if req.confirm and req.inputs is not None:
        try:
            inputs = compose.check_inputs(pinned, req.inputs)
        except compose.ComposeRefused as e:
            raise ReconcileRefused(422, e.message, CHECK_INPUTS) from e
        inputs_from = "checked"
    elif why is not None:
        raise ReconcileRefused(409, CONFIRM_FIRST if why == CHECK_REASON else why, CHECK_INPUTS)
    else:
        try:
            inputs = compose.check_inputs(pinned, dict(cand.get("binding") or {}))
        except compose.ComposeRefused as e:
            raise ReconcileRefused(409, f"{CHECK_INCOMPLETE}：{e.message}", CHECK_INPUTS) from e
        inputs_from = "binding"

    # 这次的那条查询：快照、遮罩列、被引用的格
    try:
        snap = load(req.query)
    except ValueError:
        snap = None
    if not isinstance(snap, dict) or not isinstance(snap.get("sql"), str) or req.query not in sealed.queries:
        raise ReconcileRefused(409, SNAPSHOT)
    source = str(snap.get("source") or query.get("source") or "")
    columns = [str(c) for c in snap.get("columns") or []]
    rows = snap.get("rows") if isinstance(snap.get("rows"), list) else []
    masks = _Masks(sealed)
    hidden = set((await masks.sets(req.query, source, snap)).hidden)
    kind = await masks.kind(source)
    schema, _ = _frozen(snap.get("schema_artifact"))
    fetch = next(n for n in pinned.fetches if n["id"] == cand.get("node_id"))
    template = str(((fetch.get("data") or {}).get("config") or {}).get("args", {}).get("sql") or "")
    plan = plan_pairs(ours_columns=columns, ours_rows=rows, hidden=hidden,
                      caliber_columns=_caliber_columns(template, inputs, list(pinned.entry_fields), kind, schema),
                      metrics=pinned.metrics, fetch=str(fetch["id"]), card=pinned.card_title,
                      fallback=bool((cand.get("diff") or {}).get("fallback")), chosen=req.pairs,
                      referenced=_referenced(sealed, req.query))
    graph = build(pinned, sql=snap["sql"], tool=f"{QUERY_PREFIX}{source}", plan=plan, inputs=inputs,
                  fetch=str(fetch["id"]))

    async with SessionLocal() as session:
        origin = {"source_run": run_id, **await _conversation_of(session, run), "query": req.query, "caliber": key,
                  "workflow_id": req.workflow_id, "version": int(req.version), "inputs_from": inputs_from,
                  "binding_via": cand.get("via"), "pairs": [p.describe() for p in plan.pairs],
                  **({"rows": plan.rows} if plan.rows is not None else {}),
                  **({"pairs_chosen": True} if req.pairs is not None else {}), "derive": graph.origin}
        options = {READ_ONLY_OPTION: True}
        if principal.mode != "off" and not principal.at_least("builder"):
            options[MASK_RESULTS_OPTION] = True
        try:
            return await launch_run(session, graph=graph.graph, input_payload=inputs, run_class="exploratory",
                                    actor=principal.username, origin_kind=ORIGIN, origin=origin, options=options,
                                    workflow_name=f"口径对账：{name} v{req.version}")
        except LaunchRefused as e:
            raise ReconcileRefused(e.status_code, e.message, e.code or REFUSED) from e


def build(pinned: Any, *, sql: str, tool: str, plan: Plan, inputs: dict[str, Any], fetch: str) -> Any:
    """按 Plan 派生对账图（ledger/derive.reconcile_graph）。fetch 是这条查询比的那个口径取数节点（那一版里的 id）。
    纯计算，同样的输入同一张图。"""
    from app.ledger import derive

    prefix = derive.reconcile_prefix(pinned)
    tail = tail_nodes(plan, card_id=f"{prefix}{pinned.node_id}", fetch_id=f"{prefix}{fetch}")
    return derive.reconcile_graph(pinned, derive.ours_block(sql, tool), tail, inputs=inputs)


__all__ = ["CHECK_INPUTS", "MAX_COLUMNS", "MAX_ROWS", "NEEDS_PAIRS", "ORIGIN", "REFUSED", "Pair", "Plan",
           "ReconcileRefused", "Request", "actions_of", "align_sql", "build", "plan_pairs", "reconcile",
           "reconciled_runs", "reconciled_view", "tail_nodes"]
