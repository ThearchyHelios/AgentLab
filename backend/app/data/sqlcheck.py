"""基于数据目录的 SQL 检查：按数据源方言把 SQL 解析成语法树，对照目录里记下的事实找出「跑得通、数却不对」的写法。

**为什么要有它。** 助手自查以前只核对列名在不在（copilot.sql_unknown_columns，正则）。列名全对的 SQL 照样会算错：
订单关联订单明细以后对订单金额求和（每笔订单按明细行数重复相加）、把每天的库存加成「一个月的库存」、对折扣率求
平均、忘了筛掉作废的入园记录……这些错误数据库不会报，结果看起来也像回事。数据目录记下了判断它们要的事实（关系的
基数、列的度量类型、有效记录条件、码值、业务日期），这里把两者对起来。

**七条规则，code 稳定**（界面、修复、出具都按它认）：

- fanout_sum：沿一对多方向关联之后，对「一」那一侧的度量列求和或计数（重复计算）；
- stock_summed：对存量列求和，分组里却没有这张表的业务日期（跨期加总存量）；
- join_unconfirmed：关联条件对不上目录里的任何关系；
- ratio_aggregated：对比率列直接求和或求平均；
- missing_valid_filter：表定义了有效记录条件，查询完全没涉及条件里的列；
- unknown_code：对有码值的列写了 = 或 IN，值不在码值表里；
- wrong_date_column：表定义了业务日期，查询却按这张表的另一个时间列分组或筛选。

**级别看依据。** 依据全是 confirmed / verified（人工确认、外键约束、数据剖析）时，fanout_sum、stock_summed 是 error，
其余是 warning；依据里有 proposed（命名推断、模型起草）就一律降为 info。join_unconfirmed 反过来看：对上推断的关系是
info，什么都对不上是 warning，对上有确证的关系不报。rejected 的项一律视而不见（catalog.visible_notes），被驳回的关系
既不算「对上了」，也不拿来判断基数。

**宁可漏报，不可误报。** error 会把助手搭的图打回去改、挡住受管级别的发布、让出具降档；误报一次，用户就会学会无视它。
所以拿不准一律跳过：解析不了（语法错、方言特性、多条语句、不是查询）、模板里有控制语句、列对不上表（别名不认识、
没加表前缀又有歧义、来自子查询）、关联条件里有 OR 或解析不了的列、分组写成表达式之后看不清粒度……都不报。
写法上能想到的「其实没问题」的情形也放过：按明细主键分组、去重聚合、窗口函数、两边的列一起算、业务日期筛到某一天、
CTE 或子查询里已经筛过有效记录。

这里不改写、不执行 SQL，也和 engine/direct_select.py 无关：那边刻意只用守卫的 SQLite 分词（证据下钻要和执行时的读法
完全一致），这边是多方言的静态检查，读错了最多漏报一条提示。

用法：SqlChecker 按一个数据源建一次（目录读一次、关系图用到时才建、建了就复用），check(sql) 返回 SqlCheck 列表。
助手自查、发布门禁、数据源查询工具三处共用它（load_checkers 从库里读目录，graph_checks 查一张图里写死的 SQL）。
"""
from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from app.data import catalog

logger = logging.getLogger(__name__)

#: 规则编号，按严重程度大致排序
CHECK_CODES = ("fanout_sum", "stock_summed", "join_unconfirmed", "ratio_aggregated", "missing_valid_filter",
               "unknown_code", "wrong_date_column")
#: 结果的级别，从重到轻
LEVELS = ("error", "warning", "info")
#: 依据有确证时报 error 的规则：它们算出来的数必然是错的。其余规则只是「很可能不对」
_ERROR_CODES = frozenset({"fanout_sum", "stock_summed"})
_STRONG = frozenset({"confirmed", "verified"})
_STATUS_RANK = {"confirmed": 3, "verified": 2, "proposed": 1}

#: 数据源类型 → sqlglot 方言。不认识的类型不查：拿别的方言硬读，读错了就是误报
_DIALECTS = {"sqlite": "sqlite", "postgres": "postgres", "postgresql": "postgres", "mysql": "mysql",
             "mariadb": "mysql", "oracle": "oracle"}

#: 太长的 SQL 不查：解析耗时随长度增长，数据源查询工具每次执行都要跑一遍，不能拖慢查询
MAX_SQL_CHARS = 20_000
#: 一条 SQL 最多报这么多条：再多就是 SQL 整体有问题，逐条列出只会淹没重点
MAX_CHECKS = 20
_EXCERPT_MAX = 160

#: 模板占位（{{ input.week }}）换成这个标识符再解析：在引号里是一个认得出的字符串（码值检查跳过它），
#: 在引号外是一个不属于任何表的列名（各条规则都对不上它）。不换成数字 0：放在引号里的 0 会被当成码值去比
_SENTINEL = "__agentlab_tpl__"
_TEMPLATE = re.compile(r"\{\{.*?\}\}", re.S)

#: 名字看得出是时间的列：xxx_at、xxx_time、xxx_date、xxx_on、createdAt…（SQLite 的日期常存成文本，只看类型认不全）
_TEMPORAL_NAME = re.compile(r"(?:^|_)(?:at|time|date|day|dt|ts|on|timestamp|datetime)$|[a-z0-9](?:At|Time|Date|On)$",
                            re.IGNORECASE)
_TEMPORAL_TYPE = re.compile(r"DATE|TIME", re.IGNORECASE)


def dialect_of(kind: str | None) -> str | None:
    """数据源类型对应的 sqlglot 方言；不支持的类型返回 None（不查）。"""
    return _DIALECTS.get((kind or "").strip().lower())


@dataclass(frozen=True)
class SqlCheck:
    """一条检查结果。

    message 给人看（界面、发布前检查、出具降档的原因），for_model 交回模型改写时用（写 SQL 里的真名和改法）。
    table 是 schema_cache 里的表名；column、relation_id、sql_excerpt 没有时不出现在 as_dict 里。
    """

    code: str
    level: str
    message: str
    for_model: str
    table: str
    column: str | None = None
    relation_id: str | None = None
    sql_excerpt: str | None = None

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"code": self.code, "level": self.level, "message": self.message,
                               "for_model": self.for_model, "table": self.table}
        for key in ("column", "relation_id", "sql_excerpt"):
            if (value := getattr(self, key)) is not None:
                out[key] = value
        return out


def _level(code: str, statuses: Iterable[str | None]) -> str:
    """依据全有确证：error（fanout_sum、stock_summed）或 warning；有一项只是推断：info。"""
    if all(s in _STRONG for s in statuses):
        return "error" if code in _ERROR_CODES else "warning"
    return "info"


# ==========================================================================
# 检查器：一个数据源一个
# ==========================================================================


class SqlChecker:
    """按一个数据源的方言、表结构和目录检查 SQL。

    notes 是 {表名: 目录 notes}，原样传（含被驳回的项）：关系图要知道哪些关系被驳回了，免得按表结构又推回来；
    各条规则只看去掉驳回项之后的那份。没有目录（notes 为空）的源什么都不查——没有可对照的东西，
    也不让还没起草目录的部署多出一堆提示；这时 check 不解析 SQL，几乎没有开销。
    """

    def __init__(self, *, kind: str | None, schema_cache: Mapping[str, Any] | None,
                 notes: Mapping[str, Mapping[str, Any]] | None, source_name: str | None = None) -> None:
        self.dialect = dialect_of(kind)
        self.schema_cache: Mapping[str, Any] = schema_cache or {}
        self.source_name = source_name
        self._raw = {str(k): v for k, v in (notes or {}).items() if isinstance(v, Mapping) and v}
        self._visible = {k: catalog.visible_notes(v) for k, v in self._raw.items()}
        self._visible_lower = {k.lower(): v for k, v in self._visible.items()}
        self._edges: dict[str, list[catalog.JoinEdge]] | None = None

    @property
    def enabled(self) -> bool:
        """有没有可能报出东西：方言认得、有目录、探查过结构。"""
        return bool(self.dialect and self._raw and (self.schema_cache.get("tables") or {}))

    # ---- 表、目录、关系

    def table_meta(self, table: Any) -> tuple[str, Mapping[str, Any]] | None:
        """SQL 里的一张表 → (schema_cache 的表名, 表结构)。带了别的 schema、库名的不认（宁可当作不认识）。"""
        tables = self.schema_cache.get("tables") or {}
        name, db = table.name, table.text("db")
        if not name or table.text("catalog"):
            return None
        if db:
            qualified = f"{db}.{name}"
            if qualified in tables:
                return qualified, tables[qualified]
            schema = str(self.schema_cache.get("schema") or "")
            if not schema or schema.lower() != db.lower():
                return None
        key = catalog.resolve_table_name(self.schema_cache, name)
        meta = tables.get(key) if key else None
        return (key, meta) if key and isinstance(meta, Mapping) else None

    def notes_of(self, table: str) -> dict[str, Any]:
        return self._visible.get(table) or self._visible_lower.get(table.lower()) or {}

    def edges_between(self, a: str, b: str) -> list[catalog.JoinEdge]:
        """从 a 连到 b 的边（每条关系正反各一条）。关系图第一次用到时才建，之后复用。

        和助手挑表看到的是同一张图（catalog.relation_graph）：目录里的关系，加上没起草过的表按外键、命名现推的；
        被驳回的关系不进图，也不会被现推回来。
        """
        if self._edges is None:
            graph = catalog.relation_graph(self._raw, schema_cache=self.schema_cache)
            index: dict[str, list[catalog.JoinEdge]] = {}
            for table, edges in graph.items():
                index.setdefault(table.lower(), []).extend(edges)
            self._edges = index
        return [e for e in self._edges.get(a.lower(), []) if e.to_table.lower() == b.lower()]

    # ---- 检查

    def check(self, sql: Any) -> list[SqlCheck]:
        """检查一条 SQL。解析不了、不是单条查询、或者不支持的写法，返回空列表；从不抛异常。"""
        if not self.enabled or not isinstance(sql, str):
            return []
        text = sql.strip()
        if not text or len(text) > MAX_SQL_CHARS or "{%" in text:
            return []
        text = _TEMPLATE.sub(_SENTINEL, text)
        try:
            import sqlglot
            from sqlglot import exp
            from sqlglot.optimizer.scope import traverse_scope

            statements = [s for s in sqlglot.parse(text, read=self.dialect) if s is not None]
            if len(statements) != 1 or not isinstance(statements[0], exp.Query):
                return []
            root = statements[0]
            scopes = traverse_scope(root)
        except Exception:  # noqa: BLE001 - 语法错、方言特性、sqlglot 不支持的写法：一律不查
            return []
        names = {c.name.lower() for c in root.find_all(exp.Column) if c.name}
        found: list[SqlCheck] = []
        for scope in scopes:
            if not isinstance(scope.expression, exp.Select):
                continue
            try:
                found += _ScopeCheck(self, scope, statement_columns=names).run()
            except Exception:  # noqa: BLE001 - 规则本身的缺陷不能连累查询；这一段不报，记下来修
                logger.warning("SQL 检查跳过了一段查询", exc_info=True)
        return _fold(found)

    def check_dicts(self, sql: Any) -> list[dict[str, Any]]:
        return [c.as_dict() for c in self.check(sql)]


def check_sql(sql: Any, *, kind: str | None, schema_cache: Mapping[str, Any] | None,
              notes: Mapping[str, Mapping[str, Any]] | None, source_name: str | None = None) -> list[SqlCheck]:
    """只查一条时的便捷写法。要查多条（同一个源）就建一个 SqlChecker 复用：关系图只建一次。"""
    return SqlChecker(kind=kind, schema_cache=schema_cache, notes=notes, source_name=source_name).check(sql)


def _fold(found: list[SqlCheck]) -> list[SqlCheck]:
    """同一件事只报一次（规则、表、列、关系相同的留最重的那条），重的在前，最多 MAX_CHECKS 条。"""
    kept: dict[tuple[Any, ...], SqlCheck] = {}
    for check in found:
        key = (check.code, check.table.lower(), (check.column or "").lower(), check.relation_id)
        if key not in kept or LEVELS.index(check.level) < LEVELS.index(kept[key].level):
            kept[key] = check
    ordered = sorted(kept.values(), key=lambda c: LEVELS.index(c.level))
    return ordered[:MAX_CHECKS]


# ==========================================================================
# 一段 SELECT 里的分析
# ==========================================================================


@dataclass
class _Table:
    """一段 SELECT 里出现的一张实表：别名、schema_cache 表名、表结构、目录（去掉驳回项）。"""

    alias: str
    name: str
    meta: Mapping[str, Any]
    notes: Mapping[str, Any]
    order: int
    #: FROM / JOIN 里写这张表的那个节点（摘录 SQL 用）
    node: Any = None
    columns: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.columns = {str(c.get("name")).lower(): str(c.get("name"))
                        for c in self.meta.get("columns") or [] if isinstance(c, Mapping) and c.get("name")}

    def item(self, name: str) -> Mapping[str, Any] | None:
        value = self.notes.get(name)
        return value if isinstance(value, Mapping) and "value" in value else None

    def column_item(self, column: str, name: str) -> Mapping[str, Any] | None:
        columns = self.notes.get("columns") if isinstance(self.notes.get("columns"), Mapping) else {}
        items = columns.get(column)
        if not isinstance(items, Mapping):
            items = next((v for k, v in columns.items() if str(k).lower() == column.lower()), None)
        value = items.get(name) if isinstance(items, Mapping) else None
        return value if isinstance(value, Mapping) and "value" in value else None

    def label(self) -> str:
        item = self.item("label")
        return str(item["value"]) if item and isinstance(item.get("value"), str) and item["value"].strip() \
            else self.name

    def column_label(self, column: str) -> str:
        item = self.column_item(column, "label")
        return str(item["value"]) if item and isinstance(item.get("value"), str) and item["value"].strip() \
            else column

    def measure(self, column: str) -> tuple[str, str] | None:
        """(度量类型, 状态)。"""
        item = self.column_item(column, "measure")
        return (str(item["value"]), str(item.get("status"))) if item else None

    def keys(self) -> tuple[list[str], str] | None:
        """业务主键：目录写了用目录的（带状态），没写用表结构的主键（数据库约束，算有确证）。"""
        item = self.item("keys")
        if item and isinstance(item.get("value"), list) and item["value"]:
            return [str(k) for k in item["value"]], str(item.get("status"))
        pk = self.meta.get("primary_key")
        return ([str(k) for k in pk], "verified") if isinstance(pk, list) and pk else None

    def business_date(self) -> tuple[str, str] | None:
        """(业务日期列的真名, 状态)。列不在表结构里（写的是表达式、或者列已改名）就当没有。"""
        item = self.item("business_date")
        value = item.get("value") if item else None
        column = value.get("column") if isinstance(value, Mapping) else None
        if not isinstance(column, str) or column.lower() not in self.columns:
            return None
        return self.columns[column.lower()], str(item.get("status"))

    def temporal(self, column: str) -> bool:
        meta = next((c for c in self.meta.get("columns") or []
                     if isinstance(c, Mapping) and str(c.get("name")).lower() == column.lower()), None)
        kind = str((meta or {}).get("type") or "")
        return bool(_TEMPORAL_TYPE.search(kind) or _TEMPORAL_NAME.search(column))


#: 一列解析到的位置：(表, 列的真名)
_Ref = tuple[_Table, str]


def _unparen(node: Any) -> Any:
    from sqlglot import exp

    while isinstance(node, exp.Paren):
        node = node.this
    return node


def _conjuncts(node: Any) -> list[Any]:
    """AND 连起来的各个条件（只拆最外层）。"""
    from sqlglot import exp

    node = _unparen(node)
    if isinstance(node, exp.And):
        return _conjuncts(node.left) + _conjuncts(node.right)
    return [node] if node is not None else []


def _bare_column(node: Any) -> Any:
    """剥掉不改变「这是哪一列」的外壳（括号、类型转换、COALESCE / IFNULL 给默认值、ROUND），剩下的是列就返回它。"""
    from sqlglot import exp

    while True:
        node = _unparen(node)
        if isinstance(node, (exp.Cast, exp.TryCast, exp.Round)):
            node = node.this
        elif isinstance(node, exp.Coalesce) and all(isinstance(_unparen(e), exp.Literal)
                                                    for e in node.expressions):
            node = node.this
        else:
            break
    return node if isinstance(node, exp.Column) else None


class _ScopeCheck:
    """一段 SELECT（根查询、CTE、子查询、UNION 的一支各算一段）对照目录检查。"""

    def __init__(self, checker: SqlChecker, scope: Any, *, statement_columns: set[str]) -> None:
        from sqlglot import exp

        self.checker = checker
        self.dialect = checker.dialect
        self.scope = scope
        self.select = scope.expression
        self.statement_columns = statement_columns
        self.tables: dict[str, _Table] = {}
        #: 不是实表的来源（子查询、CTE、认不出的表）：{别名: 它透出的列名，说不清就是 None}
        self.opaque: dict[str, set[str] | None] = {}
        for order, (alias, (node, source)) in enumerate(scope.selected_sources.items()):
            key = str(alias).lower()
            if isinstance(source, exp.Table):
                found = checker.table_meta(source)
                if found:
                    name, meta = found
                    self.tables[key] = _Table(key, name, meta, checker.notes_of(name), order, node)
                    continue
                self.opaque[key] = None
            else:
                inner = getattr(source, "expression", None)
                exposes = None
                if isinstance(inner, exp.Select) and not any(isinstance(s, exp.Star) or
                                                             (isinstance(s, exp.Column) and s.is_star)
                                                             for s in inner.selects):
                    exposes = {n.lower() for n in inner.named_selects}
                self.opaque[key] = exposes
        # FROM / JOIN 里还有没进 selected_sources 的东西（表函数、LATERAL）：没加前缀的列可能出自它们
        items = ([self.select.args["from_"].this] if self.select.args.get("from_") else []) + \
            [j.this for j in self.select.args.get("joins") or []]
        if len(items) != len(scope.selected_sources):
            self.opaque["\0"] = None
        self.nodes = list(scope.walk())
        self.columns = [n for n in self.nodes if isinstance(n, exp.Column)]
        self._matched: list[tuple[str, str, catalog.JoinEdge]] | None = None
        self._join_checks: list[SqlCheck] = []

    # ---- 工具

    def excerpt(self, node: Any) -> str | None:
        try:
            text = node.sql(dialect=self.dialect)
        except Exception:  # noqa: BLE001
            return None
        text = text.replace(_SENTINEL, "{{…}}")
        return text if len(text) <= _EXCERPT_MAX else text[:_EXCERPT_MAX - 1] + "…"

    def resolve(self, column: Any) -> _Ref | None:
        """列 → (表, 真名)。别名不认识、列不在那张表里、没加前缀又可能出自别处，都返回 None。"""
        name = column.name.lower() if column.name else ""
        if not name or column.is_star:
            return None
        qualifier = column.table.lower() if column.table else ""
        if qualifier:
            table = self.tables.get(qualifier)
            return (table, table.columns[name]) if table and name in table.columns else None
        if any(exposes is None or name in exposes for exposes in self.opaque.values()):
            return None
        hits = [t for t in self.tables.values() if name in t.columns]
        return (hits[0], hits[0].columns[name]) if len(hits) == 1 else None

    def refs(self, node: Any) -> list[_Ref | None]:
        """node 里每一列的解析结果（不进嵌套的子查询）。"""
        from sqlglot import exp

        if node is None:
            return []
        if isinstance(node, exp.Column):
            return [self.resolve(node)]
        return [self.resolve(c) for c in node.find_all(exp.Column)
                if not any(isinstance(p, (exp.Subquery, exp.Select)) for p in _parents(c, stop=node))]

    def group_refs(self) -> list[_Ref | None]:
        """分组里用到的列。GROUP BY 别名、GROUP BY 1 换成 SELECT 里对应的表达式（两种读法都算上，宁可多认）。"""
        from sqlglot import exp

        group = self.select.args.get("group")
        if group is None:
            return []
        out = self.refs(group)
        selects = self.select.selects
        by_alias = {s.alias.lower(): s.this for s in selects if isinstance(s, exp.Alias) and s.alias}
        for item in group.expressions:
            item = _unparen(item)
            if isinstance(item, exp.Literal) and not item.is_string and str(item.this).isdigit():
                index = int(item.this) - 1
                if 0 <= index < len(selects):
                    out += self.refs(selects[index].this if isinstance(selects[index], exp.Alias)
                                     else selects[index])
            elif isinstance(item, exp.Column) and not item.table and item.name.lower() in by_alias:
                out += self.refs(by_alias[item.name.lower()])
        return out

    def where(self) -> Any:
        where = self.select.args.get("where")
        return where.this if where is not None else None

    def aggregates(self, *kinds: type) -> list[tuple[Any, Any]]:
        """(聚合节点, 参数)：不含窗口函数、去重聚合、COUNT(*)、参数里套子查询的。"""
        from sqlglot import exp

        out = []
        for node in self.nodes:
            if not isinstance(node, kinds) or isinstance(node.parent, exp.Window):
                continue
            arg = node.this
            if arg is None or isinstance(arg, (exp.Distinct, exp.Star)) or \
                    any(isinstance(n, (exp.Subquery, exp.Select)) for n in arg.walk()):
                continue
            out.append((node, arg))
        return out

    def run(self) -> list[SqlCheck]:
        return []


def _parents(node: Any, *, stop: Any) -> Iterable[Any]:
    """node 到 stop 之间（不含两端）的各层父节点。"""
    parent = node.parent
    while parent is not None and parent is not stop:
        yield parent
        parent = parent.parent


__all__ = ["CHECK_CODES", "LEVELS", "MAX_CHECKS", "MAX_SQL_CHARS", "SqlCheck", "SqlChecker", "check_sql",
           "dialect_of"]
