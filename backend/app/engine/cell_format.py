"""查询结果里哪几列是标识列：报告里显示这几列的数不分千位（evidence.render_cell 的 plain，单元格显示规则 3 版起）。

部署上撞过：产线编码这类数值型的编码列（编码 1203）在报告的表格里写成「1,203」，读的人以为是个量。千分位只该给量用。
按序看三样信号，第一样说得出的算数；都说不出的照旧分千位：

1. 查询当时冻结的数据目录（查询快照的 schema_artifact 指向的表结构快照里的 catalog）给源列标的度量类型：identifier
   （标识）、status（状态）、attribute（属性）不分千位；flow（流量）、stock（存量）、ratio（比率）是量，分千位；
2. 结果列取自的源列名像编码、编号：CODE、ID、NO 整个词或者在结尾（STATION_CODE、order_id、userNo），带「编码」「编号」
   「代码」的；
3. 结果列是分组键（GROUP BY 里有它，写列、写别名、写序号都算），不是聚合。

SUM、COUNT、AVG 这类聚合出来的列是量，分千位，不看后面几样；MIN、MAX 取的是那一列本身的值，按那一列判。SQL 解析不了、
结果列在 SQL 里找不到（SELECT *、UNION）的，只按结果列名本身和目录里唯一那张表判。

只看查询快照（SQL、列名）和它指向的表结构快照（都按内容寻址、不会变）：同一份快照什么时候判都是同一个结果，组装、
流式渲染、出口复核一致。离线核对页（bundle/verifier.js）没法解析 SQL，照引用上记下的 plain 渲染（见 evidence._resolve_cell）。
"""
from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from typing import Any

from app.data.names import name_key

#: 目录里不是量的度量类型：这些列的数是编号、码值、属性，不分千位
PLAIN_MEASURES = frozenset({"identifier", "status", "attribute"})
#: 目录里是量的度量类型
COUNTED_MEASURES = frozenset({"flow", "stock", "ratio"})
#: 列名像编码、编号：CODE / ID / NO 整个词、或者以 _CODE、_ID、_NO 结尾（不分大小写）；驼峰的 stationCode、orderId、userNo；
#: 带「编码」「编号」「代码」的
_CODE_NAME = re.compile(r"(?i:(?:^|_)(?:code|id|no)$)|[a-z0-9](?:Code|Id|ID|No|NO)$|编码|编号|代码")
#: 解析 SQL 按这几种读法依次试（快照里没记是哪种库）：先通用读法，读不通再换，取第一种读得通的——同一条 SQL 结果固定
_READINGS = (None, "mysql", "postgres", "oracle", "sqlite")


def code_like(name: Any) -> bool:
    """列名像编码、编号（_CODE_NAME）。"""
    return isinstance(name, str) and bool(_CODE_NAME.search(name.strip()))


def plain_columns(snapshot: Mapping[str, Any], schema: Callable[[str], Any] | None = None) -> frozenset[str]:
    """查询快照里要按标识列显示（数不分千位）的结果列名（照快照 columns 的写法）。

    schema 取表结构快照的内容（参数是 schema_artifact，取不到返回 None），用来读冻结的数据目录；不给就不看目录。
    不抛异常：分析出了意外就当判断不了（照旧分千位）。"""
    columns = [str(c) for c in snapshot.get("columns") or []]
    if not columns:
        return frozenset()
    try:
        catalog = _frozen_catalog(snapshot, schema)
        select = _outer_select(snapshot.get("sql"))
        return frozenset(c for c in columns if _plain(c, select, catalog))
    except Exception:  # noqa: BLE001 - 判断不了就照旧分千位，不能让报告渲染崩掉
        return frozenset()


def _frozen_catalog(snapshot: Mapping[str, Any], schema: Callable[[str], Any] | None) -> dict[str, dict[str, str]]:
    """冻结的数据目录里各列的度量类型：{表名的 name_key: {列名的 name_key: 度量类型}}。表名带库名的，最后一段也记一份。"""
    artifact = snapshot.get("schema_artifact")
    content = schema(artifact) if schema is not None and isinstance(artifact, str) and artifact else None
    frozen = content.get("catalog") if isinstance(content, dict) else None
    out: dict[str, dict[str, str]] = {}
    for table, entry in (frozen or {}).items() if isinstance(frozen, dict) else ():
        notes = entry.get("notes") if isinstance(entry, dict) else None
        cols = notes.get("columns") if isinstance(notes, dict) else None
        measures = {name_key(str(c)): str(item["measure"]["value"]) for c, item in (cols or {}).items()
                    if isinstance(item, dict) and isinstance(item.get("measure"), dict)
                    and isinstance(item["measure"].get("value"), str)}
        if measures:
            for key in {name_key(str(table)), name_key(str(table).rsplit(".", 1)[-1])}:
                out.setdefault(key, {}).update(measures)
    return out


def _outer_select(sql: Any) -> Any:
    """最外层的 SELECT（sqlglot 的节点）；解析不了、最外层不是一个 SELECT（UNION 这类）的为 None。"""
    from sqlglot import exp

    from app.data.sql_semantics import _parse

    for read in _READINGS:
        root, _ = _parse(sql, read)
        if root is not None:
            return root if isinstance(root, exp.Select) else None
    return None


def _plain(column: str, select: Any, catalog: dict[str, dict[str, str]]) -> bool:
    from sqlglot import exp

    item = _projection(select, column) if select is not None else None
    if item is None:
        # SELECT *、UNION、解析不了：只看结果列名本身，目录按唯一那张表找
        tables = _tables(select) if select is not None else {}
        measure = _measure(catalog, next(iter(tables.values())) if len(tables) == 1 else None, column)
        return measure in PLAIN_MEASURES if measure else code_like(column)
    value = _unwrap(item.unalias())
    if isinstance(value, exp.AggFunc) and not isinstance(value, (exp.Min, exp.Max, exp.AnyValue)):
        return False                                   # SUM / COUNT / AVG……：量
    if isinstance(value, (exp.Min, exp.Max, exp.AnyValue)):
        value = _unwrap(value.this)
    if isinstance(value, exp.Column):
        tables = _tables(select)
        table = tables.get(name_key(value.table)) if value.table else \
            (next(iter(tables.values())) if len(tables) == 1 else None)
        measure = _measure(catalog, table, value.name)
        if measure in PLAIN_MEASURES:
            return True
        if measure in COUNTED_MEASURES:
            return False
        if code_like(value.name):
            return True
    return _grouped(select, item)


def _unwrap(node: Any) -> Any:
    from sqlglot import exp

    while isinstance(node, (exp.Paren, exp.Cast, exp.TryCast)):
        node = node.this
    return node


def _projection(select: Any, column: str) -> Any:
    """结果列 column 对应的那一项选取（按别名或列名，先一字不差、再按 name_key）。找不到、或者对上不止一项的为 None。"""
    items = [p for p in select.expressions if p.alias_or_name]
    for same in (lambda a: a == column, lambda a: name_key(a) == name_key(column)):
        hits = [p for p in items if same(p.alias_or_name)]
        if hits:
            return hits[0] if len(hits) == 1 else None
    return None


def _tables(select: Any) -> dict[str, str]:
    """这一层 FROM / JOIN 里的实表：{别名或表名的 name_key: 表名（带库名的照写）}。子查询、CTE 不算实表。"""
    from sqlglot import exp

    with_ = select.args.get("with_") or select.args.get("with")
    ctes = {name_key(c.alias_or_name) for c in with_.expressions} if with_ is not None else set()
    sources = [(select.args.get("from_") or select.args.get("from"))]
    sources += [j for j in select.args.get("joins") or []]
    out: dict[str, str] = {}
    for source in sources:
        table = getattr(source, "this", None)
        if not isinstance(table, exp.Table) or name_key(table.name) in ctes:
            continue
        name = ".".join(p for p in (table.db, table.name) if p)
        out[name_key(table.alias_or_name)] = name
    return out


def _measure(catalog: dict[str, dict[str, str]], table: str | None, column: str) -> str | None:
    if not table or not catalog:
        return None
    for key in (name_key(table), name_key(table.rsplit(".", 1)[-1])):
        if key in catalog:
            return catalog[key].get(name_key(column))
    return None


def _grouped(select: Any, item: Any) -> bool:
    """这一项选取是分组键：GROUP BY 里写了同样的表达式、它的别名，或者它的序号（GROUP BY 1）。"""
    from sqlglot import exp

    group = select.args.get("group")
    if group is None:
        return False
    value = _unwrap(item.unalias())
    position = next((k for k, p in enumerate(select.expressions, start=1) if p is item), None)
    alias = name_key(item.alias) if item.alias else None
    for key in group.expressions:
        key = _unwrap(key)
        if isinstance(key, exp.Literal) and key.is_int and int(key.this) == position:
            return True
        if alias and isinstance(key, exp.Column) and not key.table and name_key(key.name) == alias:
            return True
        if key == value:
            return True
    return False


__all__ = ["COUNTED_MEASURES", "PLAIN_MEASURES", "code_like", "plain_columns"]
