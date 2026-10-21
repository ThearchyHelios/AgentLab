"""目录事实：结果列给人看的叫法、度量类型，表的业务日期（细案 01 §2.1、§6、§7.3）。

算子要知道「gmv 是流量还是比率」（比率当可加是调用错误；窗口不等时流量判不可比、其余只提醒）、「region 叫地区」（判定语
写「分散在各地区」）、「orders 的业务日期是 order_date」（可比性检查按它取时间条件）。这些在数据目录里，可目录描述的是表的
列，快照里的是 SQL 的结果列，所以先按 SQL 认结果列取自哪张表的哪一列，认法同单元格的显示规则（engine/cell_format：最外层
SELECT 里按别名或列名找到那一项，看它是不是聚合、聚合的参数是不是一列、这一列在 FROM / JOIN 的哪张实表上），再查那一列
的目录项：

- SUM(x)、不聚合的 x：x 的度量类型；COUNT(*) / COUNT(x)：计数，按流量算；AVG(x)：平均值，按比率算（不能按段相加）；
  COUNT(DISTINCT x)、MIN / MAX、算式：认不出（当没标）。
- 叫法：x 的中文名（目录里的 label）；没有就是没有，调用方用列名。
- 只认没被驳回的项（catalog.visible_notes）。度量类型另记有没有人确认过（confirmed / verified）：只有确认过的才拿来判
  调用错误，推断的只写进附带说明（同 SQL 检查「宁可漏报，不可误报」）。

**复算不读目录**：run 时查过的都记进结论的 params["catalog"]（record），复算时 Facts 从那里读，同一个键得到同一个答案；
目录事后改了，封存的结论照样复算得出来。
"""
from __future__ import annotations

from collections.abc import Mapping
from functools import lru_cache
from typing import Any

from app.data.names import name_key

#: 有确证的项状态：只有它们拿来判调用错误
_STRONG = frozenset({"confirmed", "verified"})


def _notes_of(entry: Any) -> Mapping[str, Any] | None:
    """目录里一张表的 notes：CatalogEntry（有 .notes）或 notes 本身；去掉被驳回的项。"""
    from app.data import catalog

    notes = getattr(entry, "notes", entry)
    return catalog.visible_notes(notes) if isinstance(notes, Mapping) else None


def _table_notes(catalog: Mapping[str, Any] | None, table: str | None) -> Mapping[str, Any] | None:
    if not catalog or not table:
        return None
    wanted = {name_key(table), name_key(table.rsplit(".", 1)[-1])}
    for name, entry in catalog.items():
        if name_key(str(name)) in wanted or name_key(str(name).rsplit(".", 1)[-1]) in wanted:
            return _notes_of(entry)
    return None


def _item(notes: Mapping[str, Any] | None, column: str, key: str) -> Mapping[str, Any] | None:
    columns = (notes or {}).get("columns") if isinstance(notes, Mapping) else None
    if not isinstance(columns, Mapping):
        return None
    for name, items in columns.items():
        if name_key(str(name)) == name_key(column) and isinstance(items, Mapping):
            item = items.get(key)
            return item if isinstance(item, Mapping) else None
    return None


@lru_cache(maxsize=128)
def _outer_select(sql: str) -> Any:
    """最外层的 SELECT（engine/cell_format 的读法）；同一条 SQL 只解析一次（只读，不改这棵树）。"""
    from app.engine import cell_format as cf

    try:
        return cf._outer_select(sql)
    except Exception:  # noqa: BLE001
        return None


def source_of(sql: str, column: str) -> tuple[str | None, str | None, str | None, bool] | None:
    """结果列 column 取自哪里：(表, 列, 聚合, 去重)。聚合是 sum / count / avg / min / max / other（别的聚合）或 None（不聚合）；
    表在 SELECT * 、子查询、认不准的时候为 None。SQL 解析不了、结果列在最外层 SELECT 里找不到为 None。"""
    from sqlglot import exp

    from app.engine import cell_format as cf

    try:
        select = _outer_select(sql)
        if select is None:
            return None
        item = cf._projection(select, column)
        if item is None:
            return None
        value = cf._unwrap(item.unalias())
        agg: str | None = None
        distinct = False
        if isinstance(value, exp.AggFunc):
            agg = {exp.Sum: "sum", exp.Count: "count", exp.Avg: "avg", exp.Min: "min", exp.Max: "max"}.get(
                type(value), "other")
            arg = value.this
            if isinstance(arg, exp.Distinct):
                distinct = True
                arg = arg.expressions[0] if len(arg.expressions) == 1 else None
            value = cf._unwrap(arg) if arg is not None else None
        if not isinstance(value, exp.Column) or isinstance(value.this, exp.Star):
            return None, None, agg, distinct
        tables = cf._tables(select)
        table = tables.get(name_key(value.table)) if value.table else \
            (next(iter(tables.values())) if len(tables) == 1 else None)
        return table, value.name, agg, distinct
    except Exception:  # noqa: BLE001 - 认不出就当没标
        return None


def date_source(sql: str, column: str) -> tuple[str, str] | None:
    """期次结果列取自哪张表的哪个日期列：那一项选取里只引用了一列（order_date、DATE(order_date)、
    strftime('%Y-%m', order_date)）时就是它。认不出为 None。"""
    from sqlglot import exp

    from app.engine import cell_format as cf

    try:
        select = _outer_select(sql)
        item = cf._projection(select, column) if select is not None else None
        if item is None:
            return None
        cols = [c for c in item.unalias().find_all(exp.Column) if not isinstance(c.this, exp.Star)]
        if len({(name_key(c.table), name_key(c.name)) for c in cols}) != 1:
            return None
        ref = cols[0]
        tables = cf._tables(select)
        table = tables.get(name_key(ref.table)) if ref.table else \
            (next(iter(tables.values())) if len(tables) == 1 else None)
        return (table, ref.name) if table else None
    except Exception:  # noqa: BLE001
        return None


class Facts:
    """目录事实的取法：run 时 catalogs 是 context["catalogs"]（数据源名 → {表名: notes 或 CatalogEntry}），复算时 recorded 是
    结论里记下的 params["catalog"]。查过的都记下（record），同一个键只认一次。"""

    def __init__(self, catalogs: Mapping[str, Any] | None = None, recorded: Mapping[str, Any] | None = None) -> None:
        self._catalogs = catalogs or {}
        self._recorded = recorded
        self._labels: dict[str, str | None] = {}
        self._measures: dict[str, tuple[str | None, bool]] = {}
        self._dates: dict[str, str | None] = {}

    def _catalog(self, source: str) -> Mapping[str, Any] | None:
        found = self._catalogs.get(source)
        return found if isinstance(found, Mapping) else None

    def label(self, snap: Any, column: str) -> str | None:
        """结果列给人看的叫法（目录里取自的那一列的中文名）；没有为 None。"""
        if column in self._labels:
            return self._labels[column]
        if self._recorded is not None:
            value = (self._recorded.get("labels") or {}).get(column)
        else:
            value = None
            src = source_of(snap.sql, column)
            if src is not None and src[1] is not None:
                item = _item(_table_notes(self._catalog(snap.source), src[0]), src[1], "label")
                text = item.get("value") if item else None
                value = text.strip() if isinstance(text, str) and text.strip() else None
        self._labels[column] = value
        return value

    def measure(self, snap: Any, column: str) -> tuple[str | None, bool]:
        """结果列的度量类型和有没有人确认过：(flow / stock / ratio / identifier / status / attribute 或 None, 确认过)。"""
        if column in self._measures:
            return self._measures[column]
        if self._recorded is not None:
            kind = (self._recorded.get("measures") or {}).get(column)
            out = (kind, kind is not None and column not in (self._recorded.get("unconfirmed") or []))
        else:
            out = self._live_measure(snap, column)
        self._measures[column] = out
        return out

    def _live_measure(self, snap: Any, column: str) -> tuple[str | None, bool]:
        src = source_of(snap.sql, column)
        if src is None:
            return None, False
        table, name, agg, distinct = src
        if agg == "count" and not distinct:
            return "flow", True
        if agg == "avg":
            return "ratio", True
        if agg not in (None, "sum") or name is None:
            return None, False
        item = _item(_table_notes(self._catalog(snap.source), table), name, "measure")
        kind = item.get("value") if item else None
        if not isinstance(kind, str):
            return None, False
        return kind, item.get("status") in _STRONG

    def business_date(self, source: str, table: str) -> str | None:
        """表的业务日期列（目录的 business_date）；没写为 None。"""
        key = name_key(table)
        if key in self._dates:
            return self._dates[key]
        if self._recorded is not None:
            value = (self._recorded.get("dates") or {}).get(key)
        else:
            notes = _table_notes(self._catalog(source), table)
            item = notes.get("business_date") if isinstance(notes, Mapping) else None
            raw = item.get("value") if isinstance(item, Mapping) else None
            col = raw.get("column") if isinstance(raw, Mapping) else None
            value = col if isinstance(col, str) and col.strip() else None
        self._dates[key] = value
        return value

    def record(self) -> dict[str, Any] | None:
        """查过、查得到的事实（写进 params["catalog"]）；一项都没有为 None。"""
        out: dict[str, Any] = {}
        labels = {k: v for k, v in self._labels.items() if v is not None}
        measures = {k: v[0] for k, v in self._measures.items() if v[0] is not None}
        unconfirmed = sorted(k for k, v in self._measures.items() if v[0] is not None and not v[1])
        dates = {k: v for k, v in self._dates.items() if v is not None}
        for key, value in (("labels", labels), ("measures", measures), ("unconfirmed", unconfirmed), ("dates", dates)):
            if value:
                out[key] = value
        return out or None


__all__ = ["Facts", "date_source", "source_of"]
