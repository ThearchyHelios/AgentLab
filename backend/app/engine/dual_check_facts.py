"""双盲复算的选择写进数据目录：审核人在不一致的指标上选了原流程或盲算，而两边只差一个方面、又正好对得上数据目录
里的一项事实时，把被采纳那一边的写法记成人工确认过的目录项。

对得上的事实只有三种（data/catalog.py 的表级项）：

- 两边按不同的时间列筛时间 → 业务日期（business_date）：被采纳的一边按哪一列筛，业务日期就记哪一列
- 一边多一个只涉及底层表列的筛选条件 → 有效记录条件（valid_filter）：被采纳的一边有这个条件才记，记的就是这个条件
- 聚合只差去不去重 → 去重规则（dedup）：按被采纳那一边去不去重写一句

**为什么要这么严。** 目录是给助手、db_schema 工具和 SQL 检查看的数据事实，人工确认过的项还会让 SQL 检查报错误级。
只差一个方面时，人选的那一边就是对这一处的判断；差了好几处、或者差异说明里有兜底（未能归纳的差异），人选的是
「整体上哪边对」，拆不出是哪一处对，写进去就是替人编了一条事实。两条 SQL 规范化后一致却算出不同的值（两次取数之间
数据变了、结果被截断），差在数据不在写法，更没有可记的事实。

**怎么写。** 照对话提案的规矩核对（plan_patch：列在不在表结构里、取值格式），记为人工填写、已确认，备注写明来自哪次
运行、哪个节点、哪个指标。目录里这一项已经是人工确认过的另一种说法时不覆盖，照实交回原因；已经是同样的说法就什么都
不写（再写一遍也不升版本）。整个写入由节点放在 ask() 之后的一个 @task 里：审批恢复重放、接着运行时不会再写一遍。

catalog.save_patch 不收备注，这里用它的同一套原料（read_entry、apply_patch、write_entry 的乐观锁）写，只多把备注放进项里。
"""
from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select

from app.data import catalog
from app.db.base import SessionLocal
from app.db.models import DataSource

logger = logging.getLogger(__name__)

#: 能写的三项（表级）
FACT_ITEMS = ("business_date", "valid_filter", "dedup")
#: 审核人采纳的一边在差异说明里的位置：a 是原流程、b 是盲算（节点调用 explain_sql_diff 时按这个顺序给两边）
SIDE_LABEL = {"a": "原流程", "b": "盲算"}
#: 差异条目的方面（sql_semantics.FacetDiff.aspect）在说明里的叫法
ASPECT_LABEL = {
    "time": "时间列", "time_range": "时间范围", "filter": "筛选条件", "aggregate": "聚合方式", "expression": "选出的列",
    "join": "关联", "tables": "查询的表", "set_op": "几支查询的合并方式", "group_by": "分组", "having": "分组后的筛选",
    "limit": "取哪几行", "order": "排序", "parse": "能否解析", "other": "未能归纳的地方",
}
#: 聚合函数的说法（去重规则那一句用）
_VERB = {"count": "计数", "sum": "求和", "avg": "求平均"}
#: 写入时版本对不上（别人刚改过这张表）最多重试几次
_RETRIES = 2


@dataclass(frozen=True)
class Fact:
    """要写进目录的一项：哪个数据源的哪张表、哪一项、写什么。metric、choice 只用来写备注和说明。"""

    source: str
    table: str
    item: str
    value: Any
    metric: str
    choice: str


def derive(candidate: Mapping[str, Any], schema_cache: Mapping[str, Any] | None) -> tuple[Fact | None, str | None]:
    """一个候选（审核人在某个指标上选了 a 或 b）→ (要写的目录项, None) 或 (None, 为什么不写)。

    candidate：{metric 指标名, choice "a" | "b", source 数据源名, dialect, sql: {a, b}, diff: {equivalent, fallback,
    items: [FacetDiff.as_dict()]}}。只看差异说明，不重新比 SQL：审核人看到的就是这份说明。
    """
    name = str(candidate.get("metric") or "")
    side = candidate.get("choice")
    diff = candidate.get("diff") if isinstance(candidate.get("diff"), Mapping) else {}
    if side not in SIDE_LABEL:
        return None, f"指标「{name}」没有采用任何一边的结果，不写入数据目录"
    if diff.get("equivalent"):
        return None, f"指标「{name}」两边的 SQL 规范化后一致，差异来自数据而不是写法，不写入数据目录"
    items = [i for i in diff.get("items") or [] if isinstance(i, Mapping)]
    if diff.get("fallback") or len(items) != 1:
        return None, (f"指标「{name}」两边的 SQL 不止差一个方面，或有未能归纳的差异，"
                      "无法确定审核人认可的是哪一处，不写入数据目录")
    item = items[0]
    aspect = str(item.get("aspect") or "")
    if aspect == "time":
        fact, why = _business_date(item, side)
    elif aspect == "filter":
        fact, why = _valid_filter(item, side, candidate.get("dialect"))
    elif aspect == "aggregate":
        fact, why = _dedup(item, side, candidate, schema_cache)
    else:
        return None, (f"指标「{name}」两边差在{ASPECT_LABEL.get(aspect, '其他地方')}，"
                      "对不上数据目录里的业务日期、有效记录条件或去重规则，不写入数据目录")
    if why is not None:
        return None, f"指标「{name}」{why}，不写入数据目录"
    table, key, value = fact
    resolved = catalog.resolve_table_name(schema_cache, table)
    if resolved is None:
        return None, f"指标「{name}」涉及的表「{table}」不在数据源的表结构里，不写入数据目录"
    return Fact(source=str(candidate.get("source") or ""), table=resolved, item=key, value=value, metric=name,
                choice=str(side)), None


def _business_date(item: Mapping[str, Any], side: str) -> tuple[tuple[str, str, Any] | None, str | None]:
    detail = item.get("detail") if isinstance(item.get("detail"), Mapping) else {}
    mine = [c for c in detail.get(side) or [] if isinstance(c, Mapping) and c.get("table") and c.get("column")]
    if not mine:
        return None, f"被采纳的{SIDE_LABEL[side]}没有按时间筛选，没有可记的业务日期"
    if len(mine) > 1:
        return None, f"被采纳的{SIDE_LABEL[side]}按好几列筛时间，无法确定业务日期按哪一列"
    return (str(mine[0]["table"]), "business_date", {"column": str(mine[0]["column"])}), None


def _valid_filter(item: Mapping[str, Any], side: str, dialect: Any) -> tuple[tuple[str, str, Any] | None, str | None]:
    other = "b" if side == "a" else "a"
    condition, rival = item.get(side), item.get(other)
    if rival is not None:
        if condition is None:
            return None, f"多出筛选条件的是{SIDE_LABEL[other]}，被采纳的{SIDE_LABEL[side]}没有这个条件"
        return None, "两边各有不同的筛选条件，不是一边多出一个条件"
    if not isinstance(condition, str) or not condition.strip():
        return None, "差异说明里没有被采纳一边的筛选条件"
    detail = item.get("detail") if isinstance(item.get("detail"), Mapping) else {}
    tables = {str(c.get("table")) for c in detail.get("columns") or [] if isinstance(c, Mapping) and c.get("table")}
    if len(tables) != 1:
        return None, "筛选条件不是只涉及一张表的列"
    (table,) = tables
    return (table, "valid_filter", _unqualified(condition.strip(), dialect)), None


def _unqualified(condition: str, dialect: Any) -> str:
    """条件里的列去掉表名：有效记录条件记在这张表自己名下，写成 status <> 'refunded' 而不是 orders.status <> …。
    解析不了就原样。"""
    import sqlglot
    from sqlglot import exp

    from app.data.sqlcheck import dialect_of

    read = dialect_of(dialect) if isinstance(dialect, str) else None
    try:
        tree = sqlglot.parse_one(condition, read=read)
    except Exception:  # noqa: BLE001 - 写法特殊、解析不了：差异说明里给人看的就是这一句，原样记
        return condition
    if not any(col.table for col in tree.find_all(exp.Column)):
        return condition
    for col in tree.find_all(exp.Column):
        col.set("table", None)
    try:
        return tree.sql(dialect=read)
    except Exception:  # noqa: BLE001
        return condition


def _dedup(item: Mapping[str, Any], side: str, candidate: Mapping[str, Any],
           schema_cache: Mapping[str, Any] | None) -> tuple[tuple[str, str, Any] | None, str | None]:
    """聚合只差去不去重（同一个函数、同一个参数，一边写了 DISTINCT）。表从被采纳那一边的 SQL 里认：参数列属于哪张实表。"""
    from app.data.sql_semantics import facets

    pair = _aggregate_pair(item.get("a"), item.get("b"), candidate.get("dialect"))
    if pair is None:
        return None, "两边的聚合不只是去不去重的差别"
    func, distinct = pair
    adopted = distinct[0] if side == "a" else distinct[1]
    sql = (candidate.get("sql") or {}).get(side) if isinstance(candidate.get("sql"), Mapping) else None
    found = facets(str(sql or ""), candidate.get("dialect"), schema=schema_cache)
    hits = [a for a in found.aggregates if a.func == func and a.distinct == adopted and not a.scope]
    columns = {ref for a in hits for ref in a.arg.columns}
    if len(columns) != 1:
        return None, "无法确定去重的是哪一张表的哪一列"
    ((table, column),) = columns
    if table is None:
        # 参数列没认出属于哪张表（没给表结构、列没写表名）：整条语句只查了一张表时就是它
        names = list(dict.fromkeys(t.text for t in found.tables))
        if len(names) != 1:
            return None, "无法确定去重的列属于哪一张表"
        table = names[0]
    verb = _VERB.get(func, func)
    text = f"对 {column} {verb}时去重" if adopted else f"对 {column} {verb}时不去重"
    return (str(table), "dedup", text), None


def _aggregate_pair(a: Any, b: Any, dialect: Any) -> tuple[str, tuple[bool, bool]] | None:
    """两边的聚合写法 → (函数名, (A 去重吗, B 去重吗))；不是「同一个函数、同一个参数、只差 DISTINCT」返回 None。"""
    import sqlglot
    from sqlglot import exp

    from app.data.sqlcheck import dialect_of

    read = dialect_of(dialect) if isinstance(dialect, str) else None

    def shape(text: Any) -> tuple[str, str, bool] | None:
        if not isinstance(text, str) or not text.strip():
            return None
        try:
            node = sqlglot.parse_one(text, read=read)
        except Exception:  # noqa: BLE001
            return None
        if not isinstance(node, exp.AggFunc):
            return None
        arg = node.this
        distinct = isinstance(arg, exp.Distinct)
        if distinct:
            inner = arg.expressions
            if len(inner) != 1:
                return None
            arg = inner[0]
        if arg is None:
            return None
        return node.key, arg.sql(dialect=read), distinct

    left, right = shape(a), shape(b)
    if left is None or right is None or left[:2] != right[:2] or left[2] == right[2]:
        return None
    return left[0], (left[2], right[2])


# --------------------------------------------------------------------------
# 写入
# --------------------------------------------------------------------------


def note_of(fact: Fact, *, run_id: str, node_title: str) -> str:
    """目录项上的备注：来自哪次运行、哪个节点、哪个指标、采纳了哪一边。"""
    return (f"双盲复算（运行 {run_id}，节点「{node_title}」）：审核人在指标「{fact.metric}」上"
            f"采用了{SIDE_LABEL.get(fact.choice, fact.choice)}的写法")


def _value_text(item: str, value: Any) -> str:
    if item == "business_date" and isinstance(value, Mapping):
        return str(value.get("column") or "")
    return str(value)


def _same_fact(item: str, a: Any, b: Any) -> bool:
    """目录里的一项和要写的是不是同一个说法：业务日期只看哪一列（规则、时区是补充），文字压掉多余的空白比。"""
    if item == "business_date":
        return isinstance(a, Mapping) and isinstance(b, Mapping) and a.get("column") == b.get("column")
    return isinstance(a, str) and isinstance(b, str) and " ".join(a.split()) == " ".join(b.split())


async def record_choices(candidates: list[Mapping[str, Any]], *, run_id: str, node_title: str,
                         actor: str | None) -> dict[str, list[dict[str, Any]]]:
    """把审核人的选择写进数据目录：{written: [{source, table, item}], skipped: [{reason}]}。永远不抛：数据源没了、
    目录写不进去，都落在 skipped 里照实说。"""
    written: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for candidate in candidates:
        name = str(candidate.get("source") or "")
        try:
            async with SessionLocal() as session:
                row = (await session.execute(select(DataSource).where(DataSource.name == name))).scalar_one_or_none()
                if row is None:
                    skipped.append({"reason": f"数据源「{name}」已不存在，指标「{candidate.get('metric')}」的选择未写入数据目录"})
                    continue
                fact, why = derive(candidate, row.schema_cache)
                if fact is None:
                    skipped.append({"reason": why})
                    continue
                done, why = await _write(session, row, fact, note=note_of(fact, run_id=run_id, node_title=node_title),
                                         actor=actor)
        except Exception:  # noqa: BLE001 - 目录写不进去不能毁掉审批的结果：缺口照样按选择记
            logger.exception("双盲复算的选择写入数据目录失败（数据源 %s）", name)
            skipped.append({"reason": f"指标「{candidate.get('metric')}」的选择写入数据目录时出错，未写入"})
            continue
        if done:
            written.append({"source": fact.source, "table": fact.table, "item": fact.item})
        else:
            skipped.append({"reason": why})
    return {"written": written, "skipped": skipped}


async def _write(session: Any, row: DataSource, fact: Fact, *, note: str, actor: str | None) -> tuple[bool, str | None]:
    """写一项。(写了没有, 没写的原因)。"""
    label = catalog.TABLE_FIELD_LABEL.get(fact.item, fact.item)
    tables = (row.schema_cache or {}).get("tables") if isinstance(row.schema_cache, dict) else None
    for _ in range(_RETRIES):
        entry = await catalog.read_entry(session, row.id, fact.table)
        notes = entry.notes if entry else {}
        version = entry.version if entry else 0
        stored = notes.get(fact.item) if isinstance(notes.get(fact.item), dict) else None
        if stored is not None and stored.get("status") == "rejected" and _same_fact(fact.item, stored.get("value"),
                                                                                     fact.value):
            # 同一个说法以前被人明确驳回过：两次人工判断对不上，不替人拿主意
            return False, (f"「{fact.table}」的{label}「{_value_text(fact.item, fact.value)}」曾被人工驳回，"
                           f"与这次指标「{fact.metric}」的选择相反，未写入。如需更改，请到数据目录中修改")
        live = stored if stored is not None and stored.get("status") != "rejected" else None
        value = fact.value
        if fact.item == "business_date" and live is not None and isinstance(live.get("value"), dict) \
                and live["value"].get("column") == value.get("column"):
            # 业务日期按哪一列是这次选出来的事实；目录里同一列另写了规则、时区的，原样留着
            value = live["value"]
        changes = [{"path": fact.item, "value": value, "reason": note}]
        plan = catalog.plan_patch(notes, changes, table=fact.table, tables=tables)
        if plan.problems:
            return False, f"「{fact.table}」的{label}未写入数据目录：{plan.problems[0]}"
        change = plan.changes[0]
        if change.state == "same":
            return False, (f"「{fact.table}」的{label}已是人工确认过的「{_value_text(fact.item, value)}」，"
                           "无需重复写入")
        if change.before_status == "confirmed":
            return False, (f"「{fact.table}」的{label}已有人工确认过的说法「{_value_text(fact.item, change.before)}」，"
                           f"与这次指标「{fact.metric}」采纳的「{_value_text(fact.item, value)}」不同，未覆盖。"
                           "如需更改，请到数据目录中修改")
        at = catalog.now_iso()
        try:
            merged = catalog.apply_patch(notes, changes, table=fact.table, tables=tables, at=at)
        except catalog.CatalogInvalid as e:
            return False, f"「{fact.table}」的{label}未写入数据目录：{e}"
        # 不论原来是谁提的：这一项现在出自人的选择，记为人工填写、已确认，备注写明出处
        merged[fact.item] = catalog.make_item(change.after, "human", "confirmed", note=note, at=at)
        try:
            await catalog.write_entry(session, row.id, fact.table, merged, if_version=version, actor=actor)
        except catalog.CatalogConflict:
            await session.rollback()
            continue
        except catalog.CatalogInvalid as e:
            return False, f"「{fact.table}」的{label}未写入数据目录：{e}"
        return True, None
    return False, f"「{fact.table}」的数据目录正被修改，{label}未能写入，请稍后在数据目录中确认"


__all__ = ["FACT_ITEMS", "Fact", "derive", "note_of", "record_choices"]
