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
- unknown_code：对码值表标了「已列全」的列写了 = 或 IN，值不在码值表里（只补了一部分的码值表不拿来判断）；
- wrong_date_column：表定义了业务日期，查询却按这张表的另一个时间列分组或筛选。

**级别看依据。** 依据全是 confirmed / verified（人工确认、外键约束、数据剖析）时，fanout_sum、stock_summed 是 error，
其余是 warning；依据里有 proposed（命名推断、模型起草）就一律降为 info。fanout_sum 另有一道门槛：error 只给「一对多
确有其事」的——那条一对多的关系人工确认过，或者数据剖析在数据上核实过子表一侧的键不唯一（关系上的
cardinality_checked）。只凭外键约束推出的一对多降为 warning、说「可能重复计算」：外键只保证父键唯一，子表一侧完全
可能每个键恰好一行，照「会重复计算」报错就是误报，还会让出具降档。join_unconfirmed 反过来看：对上推断的关系是
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
from app.data.names import name_key

logger = logging.getLogger(__name__)

#: 规则编号，按严重程度大致排序
CHECK_CODES = ("fanout_sum", "stock_summed", "join_unconfirmed", "ratio_aggregated", "missing_valid_filter",
               "unknown_code", "wrong_date_column")
#: 结果的级别，从重到轻
LEVELS = ("error", "warning", "info")
#: 依据有确证时报 error 的规则：照这样写，算出来的数很可能是错的。其余规则只是「可能不对」。
#: fanout_sum 另要那条一对多的基数人工确认过、或用数据核实过（_ScopeCheck.fanout）
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
        # 表名按 name_key 比（只把 ASCII 字母转小写），和 catalog.resolve_table_name、使用次数、影响面同一个口径
        self._visible_lower = {name_key(k): v for k, v in self._visible.items()}
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
        return self._visible.get(table) or self._visible_lower.get(name_key(table)) or {}

    def edges_between(self, a: str, b: str) -> list[catalog.JoinEdge]:
        """从 a 连到 b 的边（每条关系正反各一条）。关系图第一次用到时才建，之后复用。

        和助手挑表看到的是同一张图（catalog.relation_graph）：目录里的关系，加上没起草过的表按外键、命名现推的；
        被驳回的关系不进图，也不会被现推回来。
        """
        return [e for e in self.edges_from(a) if name_key(e.to_table) == name_key(b)]

    def edges_from(self, a: str) -> list[catalog.JoinEdge]:
        """从 a 出发的全部边。关系图第一次用到时才建，之后复用。"""
        if self._edges is None:
            graph = catalog.relation_graph(self._raw, schema_cache=self.schema_cache)
            index: dict[str, list[catalog.JoinEdge]] = {}
            for table, edges in graph.items():
                index.setdefault(name_key(table), []).extend(edges)
            self._edges = index
        return list(self._edges.get(name_key(a), []))

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


def frozen_checker(schema_snapshot: Any, *, kind: str | None) -> SqlChecker | None:
    """按表结构快照里冻结的那一版目录重建检查器：事后核对一次运行当时的 SQL 检查用。快照里没冻结目录返回 None。

    数据源查询工具每次查询都把当时的表结构和目录冻结进表结构快照（tools/datasource._store_schema，
    catalog.frozen_catalog）。冻结的目录去掉了驳回项，只留被驳回关系的编号和状态，关系图据此不把它们按外键推回来。
    快照里没有数据源类型，方言由调用方给（数据源的 kind）。
    """
    if not isinstance(schema_snapshot, Mapping):
        return None
    frozen = schema_snapshot.get("catalog")
    tables = schema_snapshot.get("tables")
    if not isinstance(frozen, Mapping) or not frozen or not isinstance(tables, Mapping):
        return None
    notes = {str(name): entry.get("notes") for name, entry in frozen.items()
             if isinstance(entry, Mapping) and isinstance(entry.get("notes"), Mapping)}
    cache = {"tables": tables, **({"schema": schema_snapshot["schema"]} if schema_snapshot.get("schema") else {})}
    return SqlChecker(kind=kind, schema_cache=cache, notes=notes, source_name=schema_snapshot.get("source"))


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
        """这一列是不是时间列（catalog.is_date_column：和数据剖析认日期列是同一个判断，剖析提议业务日期时认的
        「唯一日期列」和这里认的时间列对得上）。"""
        meta = next((c for c in self.meta.get("columns") or []
                     if isinstance(c, Mapping) and str(c.get("name")).lower() == column.lower()), None)
        return catalog.is_date_column(column, (meta or {}).get("type"))


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
        out: list[SqlCheck] = []
        if self.tables:
            out += self.matched_join_checks()
            out += self.fanout()
            out += self.stock_summed()
            out += self.ratio_aggregated()
            out += self.missing_valid_filter()
            out += self.unknown_code()
            out += self.wrong_date_column()
        return out

    # ---- 关联：认出每一对表的关联条件，对照目录里的关系

    def matched_joins(self) -> list[tuple[str, str, catalog.JoinEdge]]:
        """对上目录关系的关联：[(别名 x, 别名 y, 从 x 看过去的边)]。顺带记下 join_unconfirmed。"""
        if self._matched is None:
            self._matched = []
            self._analyze_joins()
        return self._matched

    def matched_join_checks(self) -> list[SqlCheck]:
        self.matched_joins()
        return self._join_checks

    def _analyze_joins(self) -> None:
        from sqlglot import exp

        groups: dict[tuple[str, str], dict[str, Any]] = {}
        skip: set[str] = set()
        seen: list[str] = []
        first = self.select.args.get("from_")
        if first is not None:
            seen.append(first.this.alias_or_name.lower())

        def add(conds: list[Any], *, owner: str | None) -> None:
            for cond in conds:
                cond = _unparen(cond)
                if not isinstance(cond, exp.EQ):
                    continue
                left, right = _unparen(cond.left), _unparen(cond.right)
                if not (isinstance(left, exp.Column) and isinstance(right, exp.Column)):
                    continue
                a, b = self.resolve(left), self.resolve(right)
                if a is None or b is None:
                    if owner is not None:
                        skip.add(owner)       # 关联条件里有认不出的列：说不清这一连是按什么连的
                    continue
                if a[0].alias == b[0].alias:
                    continue
                if a[0].order > b[0].order:
                    a, b = b, a
                group = groups.setdefault((a[0].alias, b[0].alias), {"pairs": set(), "conds": []})
                group["pairs"].add((a[1].lower(), b[1].lower()))
                group["conds"].append(cond)

        for join in self.select.args.get("joins") or []:
            alias = join.this.alias_or_name.lower()
            if join.args.get("method"):           # NATURAL JOIN：按同名列连，不去猜
                skip.add(alias)
            on, using = join.args.get("on"), join.args.get("using")
            if on is not None:
                conds = _conjuncts(on)
                if any(isinstance(_unparen(c), exp.Or) for c in conds):
                    skip.add(alias)
                add(conds, owner=alias)
            elif using:
                right = self.tables.get(alias)
                for ident in using:
                    name = ident.name.lower()
                    lefts = [self.tables[s] for s in seen if s in self.tables and name in self.tables[s].columns]
                    if right is None or len(lefts) != 1 or name not in right.columns:
                        skip.add(alias)
                        break
                    left = lefts[0]
                    group = groups.setdefault((left.alias, right.alias), {"pairs": set(), "conds": []})
                    group["pairs"].add((name, name))
                    group["conds"].append(ident)
            seen.append(alias)
        # 逗号连接（以及显式 JOIN 外加的等值条件）写在 WHERE 里
        add(_conjuncts(self.where()), owner=None)

        for (x, y), group in groups.items():
            if x in skip or y in skip:
                continue
            a, b = self.tables[x], self.tables[y]
            if name_key(a.name) == name_key(b.name):
                continue                          # 自己连自己：目录不记这种关系
            pairs = group["pairs"]
            candidates = self.checker.edges_between(a.name, b.name)
            matched = [e for e in candidates
                       if {(f.lower(), t.lower()) for f, t in zip(e.from_columns, e.to_columns)} <= pairs]
            best = max(matched, key=lambda e: _STATUS_RANK.get(e.status, 0), default=None)
            if best is not None:
                self._matched.append((x, y, best))
            if not (a.notes or b.notes):
                continue                          # 两张表都没写目录：没有可对照的，不判
            condition = " AND ".join(filter(None, (self.excerpt(c) for c in group["conds"])))
            if best is None:
                parent = self._shared_parent(a, b, pairs)
                self._join_checks.append(self._join_sibling(a, b, parent, condition) if parent
                                         else self._join_unknown(a, b, candidates, condition))
            elif best.status not in _STRONG:
                self._join_checks.append(self._join_proposed(a, b, best, condition))

    def _shared_parent(self, a: _Table, b: _Table, pairs: set[tuple[str, str]]) -> str | None:
        """兄弟关联：每一对连接列都是两边各自指向同一张父表的同一组列（a.member_id → members.id ← b.member_id）。
        返回父表名；不是这种连法返回 None。

        目录里不会记 a 和 b 之间的关系，但这不是「连错了列」：比如评价表和标签关联表各自按门店编号指向门店、
        彼此按门店编号相连。照「对不上任何关系」报警告是误报。
        """
        def parents(table: _Table, column: str) -> set[tuple[str, tuple[str, ...]]]:
            return {(e.to_table.lower(), tuple(c.lower() for c in e.to_columns))
                    for e in self.checker.edges_from(table.name)
                    if tuple(c.lower() for c in e.from_columns) == (column,)
                    and e.cardinality in (None, "many_to_one", "one_to_one")}

        shared: set[tuple[str, tuple[str, ...]]] | None = None
        for left, right in pairs:
            common = parents(a, left) & parents(b, right)
            shared = common if shared is None else shared & common
            if not shared:
                return None
        names = {table for table, _ in shared or ()}
        return next(iter(names)) if len(names) == 1 else None

    def _join_sibling(self, a: _Table, b: _Table, parent: str, condition: str) -> SqlCheck:
        item = (self.checker.notes_of(parent) or {}).get("label")
        label = str(item["value"]) if isinstance(item, dict) and isinstance(item.get("value"), str) \
            and item["value"].strip() else parent
        return SqlCheck(
            code="join_unconfirmed", level="info", table=b.name, sql_excerpt=condition or None,
            message=(f"「{a.label()}」与「{b.label()}」之间没有直接的关系，这里经共同的「{label}」相连。"
                     "关联后行数可能成倍增加：确认其中一侧每个键只有一行，或先各自汇总再关联"),
            for_model=(f"{a.name} 和 {b.name} 按 {condition or '这组条件'} 关联：两边都指向 {parent}，彼此是一对多对多。"
                       "如果其中一侧每个键不止一行，关联后另一侧的行会被重复，求和、计数会放大；"
                       "先把一侧筛到每个键一行（例如只取主记录），或分别汇总到这个键再关联"))

    def _join_unknown(self, a: _Table, b: _Table, candidates: list[catalog.JoinEdge], condition: str) -> SqlCheck:
        known = "；".join(dict.fromkeys(e.condition() for e in candidates))
        hint = f"目录里这两张表的关系是：{known}。照它改关联条件" if known else \
            "目录里没有这两张表之间的关系。确认这样连两边各是什么粒度、会不会一对多"
        return SqlCheck(
            code="join_unconfirmed", level="warning", table=b.name, sql_excerpt=condition or None,
            message=(f"「{a.label()}」与「{b.label()}」的关联条件对不上数据目录中的任何关系，可能连错了列。"
                     "请对照数据目录修改关联条件，或在数据目录中补充并确认这条关系"),
            for_model=(f"{a.name} 和 {b.name} 按 {condition or '这组条件'} 关联，数据目录里没有对应的关系。{hint}；"
                       "确实要这样连的话，避免一对多关联之后对「一」那一侧的列求和"))

    def _join_proposed(self, a: _Table, b: _Table, edge: catalog.JoinEdge, condition: str) -> SqlCheck:
        return SqlCheck(
            code="join_unconfirmed", level="info", table=b.name, relation_id=edge.relation_id,
            sql_excerpt=condition or None,
            message=(f"「{a.label()}」与「{b.label()}」按推断的关系关联（{edge.condition()}），这条关系尚未确认。"
                     "请在数据目录中确认"),
            for_model=(f"{a.name} 和 {b.name} 的关联依据的是推断出来的关系（{edge.condition()}），没人确认过。"
                       "核对两边的列确实对应、基数和你以为的一致"))

    # ---- fanout_sum

    def _fanned(self, start: str) -> tuple[str, str, list[catalog.JoinEdge]] | None:
        """start 这张表会不会被关联放大：沿多对一、一对一往外走，碰到一条一对多的边就会。返回 (V, W, 路径)。

        按 W 的业务主键分组时，每组只有一条 W：这条边不放大，接着往外找。对不上关系的关联不知道基数，不往外走。
        """
        adjacency: dict[str, list[tuple[str, catalog.JoinEdge]]] = {}
        for x, y, edge in self.matched_joins():
            adjacency.setdefault(x, []).append((y, edge))
            adjacency.setdefault(y, []).append((x, edge.reversed()))
        grouped = {(r[0].alias, r[1].lower()) for r in self.group_refs() if r}
        # 反连接（LEFT JOIN 明细 … WHERE 明细.id IS NULL）：留下的是没有明细的订单，每条订单至多一行
        absent = {r[0].alias for c in _conjuncts(self.where()) if self._is_null(c) for r in self.refs(c) if r}
        visited = {start}
        queue: list[tuple[str, list[catalog.JoinEdge]]] = [(start, [])]
        while queue:
            here, path = queue.pop(0)
            for there, edge in adjacency.get(here, []):
                if there in visited:
                    continue
                keys = self.tables[there].keys()
                collapsed = there in absent or (bool(keys) and all((there, k.lower()) in grouped for k in keys[0]))
                if edge.cardinality == "one_to_many" and not collapsed:
                    return here, there, path + [edge]
                if edge.cardinality in ("many_to_one", "one_to_one") or collapsed:
                    visited.add(there)
                    queue.append((there, path + [edge]))
        return None

    @staticmethod
    def _is_null(cond: Any) -> bool:
        """col IS NULL（不含 IS NOT NULL）。"""
        from sqlglot import exp

        cond = _unparen(cond)
        return isinstance(cond, exp.Is) and isinstance(_unparen(cond.expression), exp.Null) and \
            isinstance(_unparen(cond.this), exp.Column)

    def fanout(self) -> list[SqlCheck]:
        from sqlglot import exp

        if not self.matched_joins():
            return []
        out = []
        for node, arg in self.aggregates(exp.Sum, exp.Count):
            refs = self.refs(arg)
            if not refs or any(r is None for r in refs) or len({r[0].alias for r in refs}) != 1:
                continue
            table = refs[0][0]
            basis = self._fanout_basis(table, [r[1] for r in refs], counting=isinstance(node, exp.Count))
            if basis is None:
                continue
            cause = self._fanned(table.alias)
            if cause is None:
                continue
            column, status = basis
            via, many, path = cause
            out.append(self._fanout_check(node, table, column, self.tables[via], self.tables[many], path,
                                          [status] + [e.status for e in path], counting=isinstance(node, exp.Count)))
        return out

    @staticmethod
    def _one_to_many_established(edge: catalog.JoinEdge) -> bool:
        """这条一对多是不是确有其事：人工确认过（基数是人定的），或者数据剖析在数据上核实过子表一侧的键不唯一
        （cardinality_checked）。外键约束、命名推断给的基数只是按表结构推的：子表一侧可能每个键恰好一行。"""
        return edge.status == "confirmed" or edge.cardinality_checked

    def _fanout_basis(self, table: _Table, columns: list[str], *, counting: bool) -> tuple[str, str] | None:
        """参数里能说明「这是一张表自己的数」的那一列和它的依据：求和看度量类型（流量、存量），
        计数另外认标识列和业务主键（数订单数却数成了明细行数）。"""
        keys = table.keys()
        for column in columns:
            measure = table.measure(column)
            wanted = ("flow", "stock", "identifier") if counting else ("flow", "stock")
            if measure and measure[0] in wanted:
                return column, measure[1]
            if counting and keys and column.lower() in {k.lower() for k in keys[0]}:
                return column, keys[1]
        return None

    def _fanout_check(self, node: Any, table: _Table, column: str, via: _Table, many: _Table,
                      path: list[catalog.JoinEdge], statuses: list[str], *, counting: bool) -> SqlCheck:
        what = "计数" if counting else "求和"
        excerpt = self.excerpt(node)
        level = _level("fanout_sum", statuses)
        if not self._one_to_many_established(path[-1]):
            # 只凭表结构推出的一对多：不说「会重复计算」，最多是提醒；下一步先确认是不是真的一对多
            return SqlCheck(
                code="fanout_sum", level="warning" if level == "error" else level, table=table.name, column=column,
                relation_id=path[-1].relation_id, sql_excerpt=excerpt,
                message=(f"按表结构，「{via.label()}」关联「{many.label()}」是一对多，"
                         f"对「{table.label()}」的「{table.column_label(column)}」{what}可能重复计算。"
                         f"请确认关联表「{many.label()}」里每个键只有一行，或先汇总再关联"),
                for_model=(f"按表结构，{via.name} 关联 {many.name} 是一对多（这个基数没用数据核对过），"
                           f"{excerpt or what} 可能把 {table.name}.{column} 重复计算。确认 {many.name} 里每个关联键"
                           f"只有一行；不确定就先在子查询里把 {many.name} 汇总成每条 {via.name} 一行再关联"))
        if via.alias == table.alias:
            head = f"「{table.label()}」关联「{many.label()}」是一对多，"
            model_head = f"{table.name} 关联 {many.name} 是一对多（一行 {table.name} 对多行 {many.name}），"
        else:
            head = f"「{via.label()}」关联「{many.label()}」是一对多，「{table.label()}」的每一行会随之重复，"
            model_head = (f"{via.name} 关联 {many.name} 是一对多，经 {via.name} 连过去之后 {table.name} 的每一行"
                          f"都按 {many.name} 的行数重复，")
        # 直接连的：常见的本意是「明细的数」，改法之一是换成对明细求和；隔一层连的（明细经订单连支付记录），
        # 本意是明细自己的数，要么先把支付记录汇总，要么根本不该连它
        if counting:
            fix = f"请改为对「{table.label()}」去重计数，或先在子查询中把「{many.label()}」汇总后再关联"
            model_fix = (f"改成 COUNT(DISTINCT {table.name} 的主键)，或者先在子查询里把 {many.name} 汇总成每条 "
                         f"{via.name} 一行再关联")
        elif via.alias == table.alias:
            fix = (f"请先在子查询中把「{many.label()}」按「{table.label()}」汇总后再关联，"
                   f"或改为对「{many.label()}」上的对应列求和")
            model_fix = (f"先在子查询里把 {many.name} 按关联列汇总成每条 {table.name} 一行再关联；或者改成对 {many.name} "
                         f"上的对应列求和；只要 {table.name} 自己的数就不要关联 {many.name}")
        else:
            fix = f"请先在子查询中把「{many.label()}」按「{via.label()}」汇总后再关联，或去掉与「{many.label()}」的关联"
            model_fix = (f"先在子查询里把 {many.name} 按关联列汇总成每条 {via.name} 一行再关联；用不到 {many.name} 的列"
                         f"就去掉这个关联")
        return SqlCheck(
            code="fanout_sum", level=level, table=table.name, column=column,
            relation_id=path[-1].relation_id, sql_excerpt=excerpt,
            message=f"{head}对「{table.label()}」的「{table.column_label(column)}」{what}会重复计算。{fix}",
            for_model=f"{model_head}{excerpt or what} 会把 {table.name}.{column} 重复计算。{model_fix}")

    # ---- stock_summed

    def _date_equivalents(self, table: _Table, column: str) -> set[tuple[str, str]]:
        """业务日期列，加上关联、筛选条件里和它等值的列（经日期维度表分组也算按日期分组）。"""
        out = {(table.alias, column.lower())}
        conds = _conjuncts(self.where())
        for join in self.select.args.get("joins") or []:
            conds += _conjuncts(join.args.get("on"))
        from sqlglot import exp

        for cond in conds:
            cond = _unparen(cond)
            if isinstance(cond, exp.EQ):
                a, b = self.refs(cond.left), self.refs(cond.right)
                if len(a) == 1 and len(b) == 1 and a[0] and b[0]:
                    ka, kb = (a[0][0].alias, a[0][1].lower()), (b[0][0].alias, b[0][1].lower())
                    if ka in out:
                        out.add(kb)
                    elif kb in out:
                        out.add(ka)
        return out

    def _mentions(self, refs: list[_Ref | None], wanted: set[tuple[str, str]], name: str) -> bool:
        """refs 里有没有 wanted 里的列；认不出来、但名字就是 name 的列也算有（宁可漏报）。"""
        return any((r[0].alias, r[1].lower()) in wanted if r else False for r in refs) or \
            self._unresolved_named(refs, name)

    def _unresolved_named(self, refs: list[_Ref | None], name: str) -> bool:
        return False if not any(r is None for r in refs) else name.lower() in self.statement_columns

    def stock_summed(self) -> list[SqlCheck]:
        from sqlglot import exp

        out = []
        for node, arg in self.aggregates(exp.Sum):
            refs = self.refs(arg)
            if not refs or any(r is None for r in refs) or len({r[0].alias for r in refs}) != 1:
                continue
            table = refs[0][0]
            stock = next(((c, m[1]) for c in (r[1] for r in refs) if (m := table.measure(c)) and m[0] == "stock"), None)
            date = table.business_date()
            if stock is None or date is None or any(r[1].lower() == date[0].lower() for r in refs):
                continue
            equivalents = self._date_equivalents(table, date[0])
            if self._mentions(self.group_refs(), equivalents, date[0]):
                continue
            pinned = False
            for cond in (self.where().walk() if self.where() is not None else []):
                if isinstance(cond, (exp.EQ, exp.In)) and self._mentions(self.refs(cond), equivalents, date[0]):
                    pinned = True              # 筛到某一天（或某几天）：不算跨期加总
                    break
            if pinned:
                continue
            column, label = stock[0], table.column_label(stock[0])
            date_label = table.column_label(date[0])
            excerpt = self.excerpt(node)
            out.append(SqlCheck(
                code="stock_summed", level=_level("stock_summed", [stock[1], date[1]]), table=table.name,
                column=column, sql_excerpt=excerpt,
                message=(f"「{table.label()}」的「{label}」是存量，跨日期求和会把不同日期的存量加在一起。"
                         f"请按「{date_label}」分组，或筛选到某一天后再求和"),
                for_model=(f"{table.name}.{column} 是存量（某一时点的值），{excerpt or 'SUM'} 把不同日期的存量加在一起了。"
                           f"按 {date[0]} 分组，或者在 WHERE 里把 {date[0]} 限定到某一天（比如期末那天）再求和；"
                           "要看一段时间的水平就用 AVG，或取期末那天的值")))
        return out

    # ---- ratio_aggregated

    def ratio_aggregated(self) -> list[SqlCheck]:
        from sqlglot import exp

        out = []
        for node, arg in self.aggregates(exp.Sum, exp.Avg):
            column = _bare_column(arg)
            ref = self.resolve(column) if column is not None else None
            measure = ref[0].measure(ref[1]) if ref else None
            if not ref or not measure or measure[0] != "ratio":
                continue
            table, name = ref
            what = "求平均" if isinstance(node, exp.Avg) else "求和"
            excerpt = self.excerpt(node)
            out.append(SqlCheck(
                code="ratio_aggregated", level=_level("ratio_aggregated", [measure[1]]), table=table.name,
                column=name, sql_excerpt=excerpt,
                message=(f"「{table.label()}」的「{table.column_label(name)}」是比率，直接{what}得不到正确的结果。"
                         "请分别汇总分子和分母后再相除"),
                for_model=(f"{table.name}.{name} 是比率，{excerpt or what} 直接把比率{what}了。分别对分子、分母求和以后"
                           f"再相除；要按权重平均就写 SUM(权重 * {name}) / SUM(权重)")))
        return out

    # ---- missing_valid_filter

    def missing_valid_filter(self) -> list[SqlCheck]:
        out = []
        done: set[str] = set()
        local = [self.resolve(c) for c in self.columns]
        for table in sorted(self.tables.values(), key=lambda t: t.order):
            item = table.item("valid_filter")
            condition = item.get("value") if item else None
            if table.name in done or not isinstance(condition, str) or not condition.strip():
                continue
            wanted = self._filter_columns(condition, table)
            if not wanted:
                continue
            done.add(table.name)
            if any(r and r[0].name == table.name and r[1].lower() in wanted for r in local):
                continue
            # 没加前缀、认不出是哪张表的同名列：可能就是它，不报
            if any(c.name.lower() in wanted for c, r in zip(self.columns, local) if r is None):
                continue
            # 子查询、CTE 里的表：外层可能按透出的列筛（SELECT … FROM (SELECT * FROM t) x WHERE x.status = 1）
            if not self.scope.is_root and wanted & self.statement_columns:
                continue
            cols = "、".join(table.columns[c] for c in sorted(wanted))
            out.append(SqlCheck(
                code="missing_valid_filter", level=_level("missing_valid_filter", [item.get("status")]),
                table=table.name, sql_excerpt=self.excerpt(table.node),
                message=(f"「{table.label()}」定义了有效记录条件「{condition.strip()}」，查询中没有按它筛选，"
                         "结果会包含无效记录。请在筛选条件中加上该条件，或确认确实需要包含全部记录"),
                for_model=(f"{table.name} 的有效记录条件是 {condition.strip()}，这条 SQL 没有用到其中的列（{cols}）。"
                           "在 WHERE 里加上这个条件（有别名就加上别名前缀）；确实要包含无效记录的话，在条件里显式写出这一列")))
        return out

    def _filter_columns(self, condition: str, table: _Table) -> set[str]:
        """有效记录条件里用到的、这张表上的列（小写）。条件解析不了就返回空集（不查）。"""
        import sqlglot
        from sqlglot import exp

        try:
            parsed = sqlglot.parse_one(f"SELECT 1 FROM t WHERE {condition}", read=self.dialect)
        except Exception:  # noqa: BLE001
            return set()
        return {c.name.lower() for c in parsed.find_all(exp.Column) if c.name and c.name.lower() in table.columns}

    # ---- unknown_code

    def unknown_code(self) -> list[SqlCheck]:
        from sqlglot import exp

        hits: dict[tuple[str, str], tuple[_Table, str, Mapping[str, Any], list[str], Any]] = {}
        for node in self.nodes:
            if isinstance(node, exp.EQ):
                pairs = [(node.left, [node.right]), (node.right, [node.left])]
            elif isinstance(node, exp.In) and not node.args.get("query") and node.expressions:
                pairs = [(node.this, node.expressions)]
            else:
                continue
            for target, values in pairs:
                column = _unparen(target)
                if not isinstance(column, exp.Column):
                    continue
                ref = self.resolve(column)
                item = ref[0].column_item(ref[1], "codes") if ref else None
                codes = item.get("value") if item else None
                # 只有标了「已列全」的码值表才能说某个值「不在里面」：经对话只补了 status=9 表示作废，之后 status = 1
                # 就会被报，模型还会照着改错。没标的码值表只是已知的一部分，不判（宁可漏报）
                if not isinstance(codes, Mapping) or not codes or not catalog.codes_complete(item):
                    continue
                for value in values:
                    literal = _literal(value)
                    if literal is None or _known_code(literal, codes):
                        continue
                    key = (ref[0].name, ref[1].lower())
                    entry = hits.setdefault(key, (ref[0], ref[1], item, [], node))
                    if literal[0] not in entry[3]:
                        entry[3].append(literal[0])
        out = []
        for table, column, item, values, node in hits.values():
            codes = item["value"]
            shown = "、".join(f"{k}={v}" for k, v in list(codes.items())[:10]) + ("等" if len(codes) > 10 else "")
            told = "、".join(f"「{v}」" for v in values[:5])
            listed = "、".join(f"{k}（{v}）" for k, v in list(codes.items())[:10])
            hint = f"，或者用 db_schema__{self.checker.source_name} 看这张表的码值说明" if self.checker.source_name else ""
            # 给模型的话只叫它核对，不叫它「照目录改」：码值表也可能漏了某个取值，照改会把本来对的 SQL 改错
            out.append(SqlCheck(
                code="unknown_code", level=_level("unknown_code", [item.get("status")]), table=table.name,
                column=column, sql_excerpt=self.excerpt(node),
                message=(f"「{table.label()}」的「{table.column_label(column)}」没有码值{told}（数据目录中已列出的全部码值："
                         f"{shown}）。请核对筛选条件里的取值"),
                for_model=(f"{table.name}.{column} 的码值表已列全，只有 {listed}，SQL 里写的 {'、'.join(values[:5])} "
                           f"不在其中。核对这个取值有没有写错（比如写成了含义、别的列的取值，或者类型不对）；拿不准就先查一下"
                           f"这一列实际有哪些取值{hint}。确认取值存在就保留原写法，并在结果说明里写明数据目录的码值表可能缺了它")))
        return out

    # ---- wrong_date_column

    def wrong_date_column(self) -> list[SqlCheck]:
        from sqlglot import exp

        out = []
        where = self.where()
        where_refs = [(c, self.resolve(c)) for c in (where.find_all(exp.Column) if where is not None else [])
                      if not any(isinstance(p, (exp.Subquery, exp.Select)) for p in _parents(c, stop=where))]
        group = self.group_refs()
        for table in sorted(self.tables.values(), key=lambda t: t.order):
            date = table.business_date()
            if date is None:
                continue
            own = {(table.alias, date[0].lower())}
            if self._mentions([r for _, r in where_refs] + group, own, date[0]):
                continue                          # 业务日期已经用上了：另一个时间列只是附加条件
            used: dict[str, set[str]] = {}
            for column, ref in where_refs:
                # IS NULL / IS NOT NULL 只是要求有值，不是按这一列划时间范围
                if ref and ref[0].alias == table.alias and not isinstance(column.parent, exp.Is):
                    used.setdefault(ref[1], set()).add("筛选")
            for ref in group:
                if ref and ref[0].alias == table.alias:
                    used.setdefault(ref[1], set()).add("分组")
            for column, how in used.items():
                if column.lower() == date[0].lower() or not table.temporal(column):
                    continue
                verb = "分组和筛选" if len(how) == 2 else next(iter(how))
                label, date_label = table.column_label(column), table.column_label(date[0])
                item = table.item("business_date")
                rule = (item.get("value") or {}).get("rule") if item else None
                out.append(SqlCheck(
                    code="wrong_date_column", level=_level("wrong_date_column", [date[1]]), table=table.name,
                    column=column, sql_excerpt=self.excerpt(next((c for c, r in where_refs if r and
                                                                  r[1] == column), None) or exp.column(column)),
                    message=(f"「{table.label()}」的业务日期按「{date_label}」计，查询却按「{label}」{verb}。"
                             f"请改用「{date_label}」，或确认确实要按「{label}」统计"),
                    for_model=(f"{table.name} 的业务日期列是 {date[0]}{f'（{rule}）' if rule else ''}，这条 SQL 却按 "
                               f"{column} {verb}。按业务日期统计就把 {column} 换成 {date[0]}；确实要按 {column} 统计的话"
                               "保留，并在结果说明里写清口径")))
        return out


def _parents(node: Any, *, stop: Any) -> Iterable[Any]:
    """node 到 stop 之间（不含两端）的各层父节点。"""
    parent = node.parent
    while parent is not None and parent is not stop:
        yield parent
        parent = parent.parent


def _literal(node: Any) -> tuple[str, bool] | None:
    """字面量 → (文本, 是不是字符串)。负数照常认；模板占位、参数、表达式返回 None（值到运行时才知道）。"""
    from sqlglot import exp

    node = _unparen(node)
    negative = False
    if isinstance(node, exp.Neg):
        node, negative = _unparen(node.this), True
    if not isinstance(node, exp.Literal):
        return None
    text = str(node.this)
    if _SENTINEL in text:
        return None
    if node.is_string:
        return (text, True) if not negative else None
    return (f"-{text}" if negative else text), False


def _number(text: str) -> float | None:
    try:
        return float(text.strip())
    except ValueError:
        return None


def _known_code(literal: tuple[str, bool], codes: Mapping[str, Any]) -> bool:
    """值在不在码值表里：原样相等、不分大小写相等、或者两边都是数且数值相等（1 和 '1'、1.0 都算 1）。

    不分大小写是有意的：MySQL 默认的排序规则比较字符串本来就不分大小写，status = 'paid' 照样筛得出 'PAID'；
    PostgreSQL、SQLite 区分大小写，这样比会漏报一些，但宁可漏报，不能把在 MySQL 上完全正确的写法报成错。
    """
    text = literal[0]
    keys = [str(k) for k in codes]
    if text in keys or text.strip().lower() in {k.strip().lower() for k in keys}:
        return True
    number = _number(text)
    return number is not None and any(_number(k) == number for k in keys)


# ==========================================================================
# 工作流里写死的 SQL、从库里读目录
# ==========================================================================


def tool_query(node: Any) -> tuple[str, str] | None:
    """调用工具节点里写死的查询：(数据源名, SQL)。不是数据源查询工具、SQL 不是字符串的返回 None。

    Agent 节点的 SQL 要到运行时才由模型写出来，静态拿不到，这里不管；它们由数据源查询工具在执行后检查。
    """
    from app.tools.datasource import QUERY_PREFIX

    if str(getattr(node, "type", "")) != "tool":
        return None
    config = getattr(node, "config", None) or {}
    tool = str(config.get("tool") or "")
    args = config.get("args")
    sql = args.get("sql") if isinstance(args, Mapping) else None
    if not tool.startswith(QUERY_PREFIX) or not isinstance(sql, str) or not sql.strip():
        return None
    return tool[len(QUERY_PREFIX):], sql


def graph_sources(nodes: Iterable[Any]) -> set[str]:
    """一张图里调用工具节点写死查询用到的数据源名。"""
    return {found[0] for node in nodes if (found := tool_query(node))}


def graph_checks(nodes: Iterable[Any], checkers: Mapping[str, SqlChecker], *,
                 only: set[str] | None = None) -> list[tuple[Any, SqlCheck]]:
    """一张图里调用工具节点写死的 SQL 逐个检查：[(节点, 结果)]。only 给了就只查这些节点。"""
    out: list[tuple[Any, SqlCheck]] = []
    for node in nodes:
        if only is not None and getattr(node, "id", None) not in only:
            continue
        found = tool_query(node)
        checker = checkers.get(found[0]) if found else None
        if checker is not None:
            out += [(node, check) for check in checker.check(found[1])]
    return out


async def load_checkers(session: Any, sources: Iterable[Any]) -> dict[str, SqlChecker]:
    """给一批数据源（DataSource 行）各建一个检查器：{源名: SqlChecker}。没有目录的源不建。

    一次查询读出这些源的全部目录。读不到就当没有目录（返回空 dict）：检查是附加的，不能挡住自查和发布本身。
    """
    from sqlalchemy import select

    from app.db.models import CatalogNote

    rows = [s for s in sources if getattr(s, "id", None) and dialect_of(getattr(s, "kind", None))]
    if not rows:
        return {}
    try:
        notes = (await session.execute(
            select(CatalogNote.source_id, CatalogNote.table_name, CatalogNote.notes)
            .where(CatalogNote.source_id.in_([r.id for r in rows]))
        )).all()
    except Exception:  # noqa: BLE001
        logger.exception("读取数据目录失败，本次不做基于目录的 SQL 检查")
        return {}
    by_source: dict[str, dict[str, Any]] = {}
    for source_id, table, value in notes:
        if isinstance(value, dict) and value:
            by_source.setdefault(source_id, {})[table] = value
    return {r.name: SqlChecker(kind=r.kind, schema_cache=r.schema_cache, notes=by_source[r.id], source_name=r.name)
            for r in rows if r.id in by_source}


async def load_checkers_by_name(session: Any, names: Iterable[str]) -> dict[str, SqlChecker]:
    """按数据源名建检查器（只看启用着的源）。发布门禁用：图里写的是源名。"""
    from sqlalchemy import select

    from app.db.models import DataSource

    wanted = sorted({n for n in names if n})
    if not wanted:
        return {}
    try:
        rows = list((await session.execute(
            select(DataSource).where(DataSource.name.in_(wanted), DataSource.enabled.is_(True))
        )).scalars())
    except Exception:  # noqa: BLE001
        logger.exception("读取数据源失败，本次不做基于目录的 SQL 检查")
        return {}
    return await load_checkers(session, rows)


__all__ = ["CHECK_CODES", "LEVELS", "MAX_CHECKS", "MAX_SQL_CHARS", "SqlCheck", "SqlChecker", "check_sql",
           "dialect_of", "graph_checks", "graph_sources", "load_checkers", "load_checkers_by_name", "tool_query"]
