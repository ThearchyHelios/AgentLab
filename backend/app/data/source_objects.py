"""数据源的对象索引、探查范围、表结构的物化与按需加入（docs/design/next-round/03-数据接入与目录维护.md A1、A2）。

几千张表的库以前用不了：探查结构每个源最多取 200 个对象，按数据库列出的顺序截断，多出来的表目录、look、剖析、SQL 检查都
看不到，还会被说成「表已不在」。现在分三层：

- **对象索引**（list_objects / refresh_index）：每种方言一条字典查询，拿全库对象的名字、类型、注释、行数估算（SQLite 查
  sqlite_master，MySQL 查 information_schema.TABLES，PostgreSQL 查 pg_class 加 obj_description，Oracle 查 ALL_OBJECTS 加
  ALL_TAB_COMMENTS）。字典查询走数据库查询的同一个入口（engine.run_query：只读守卫、单条时限）；查不了退回 Inspector 的
  get_table_names / get_view_names，只是没有注释。最多收 INDEX_LIMIT 个对象（按名字排序取前面的），超了照实记 capped，
  这时不判删除（没收进来的不等于不在了）。升级前同步探查过的源，对象索引第一次建起来时把老缓存里的表结构接进来
  （legacy_seed）：不然物化出来的会缩成这一次已经读完的那几张。
- **探查范围**（ScopeRules / plan_scope）：数据源 options.introspect_scope 的规则——{mode: all|rules, include: [通配],
  exclude: [通配], tables: [显式], removed: [显式移出], views: bool, max_tables}。按 names.name_key 不分大小写匹配，排除优先。
  有数据目录的表、已发布模板写死的 SQL 用到的表一律在范围里（pinned），规则排不掉，只有显式移出才行。规则命中（含 pinned、
  显式）超过上限时拒绝开始并说出命中了多少张，不悄悄截断。按需加入的表另算，总数不超过 SCOPE_MAX。
- **物化**（rebuild_schema_cache）：schema_cache 由这一个函数从范围内已反射的行物化出来，形状不变（tables、total、
  truncated、schema、synced_at），另加 scope 摘要（in_scope、cap、out_of_scope……）。total 是对象索引里的对象总数，truncated
  的意思变成「有范围外的对象」：两百多处读 schema_cache 的地方不用改。

**按需加入**（add_to_scope）：look 点名或挑中范围外的表、运行时 db_schema__ 点名范围外的表、目录页「加入范围」，都当场只读
反射这一张、落盘（scope_reason on_demand / explicit），记下谁、经哪个入口、哪一轮或哪次运行、需求原文。写 schema_cache 只走
rebuild_schema_cache，按源加进程内锁（source_lock）：和后台探查任务、别的按需加入互斥。前提和回收锁、SourceLanes 一样是
单进程部署；多进程部署要换成库里的锁。

**三种「不在」**（absent_reasons）：表已不在（对象索引里没有了，removed_at）、在探查范围外、在范围里却读不到结构，分开说。
只有第一种说「表已不在」。

只读：只发字典查询和反射，不写业务库。
"""
from __future__ import annotations

import asyncio
import fnmatch
import hashlib
import json
import logging
import weakref
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import bindparam, delete, func, insert, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import describe_exception, first_line
from app.data.names import name_key
from app.db.base import SessionLocal, utcnow
from app.db.models_sources import SourceObject

logger = logging.getLogger(__name__)

#: options 里探查范围的键（由 PUT /api/datasources/{id}/introspect-scope 维护，数据源表单不显示、不提交它）
SCOPE_OPTION = "introspect_scope"
#: 对象索引最多收几个对象（目录-D3）
INDEX_LIMIT = 50_000
#: 探查范围缺省、最多几张（目录-D3）：500 张 × 25 列约 1 MB 的 schema_cache，每次读数据源都整份加载
SCOPE_DEFAULT = 500
SCOPE_MAX = 2_000
#: 范围规则里每种最多几条、每条最长几个字；显式列出、显式移出的表最多几张
RULES_MAX = 100
RULE_CHARS = 200
#: 后台探查一批读几张：PostgreSQL 的批量反射一批一次往返，MySQL 每表一次往返；一批也是停止的最小单位
REFLECT_BATCH = 25
#: 每次「加入范围」最多几张（目录页手工加）
ADD_LIMIT = 20

# 按需加入的入口
VIA_LOOK = "look"
VIA_SCHEMA_TOOL = "db_schema"
VIA_CATALOG = "catalog"

# 三种「不在」（目录清单的 absent_reason、look 和 db_schema 的说法都按它），另有「从来没有过」和「说不清」
ABSENT_REMOVED = "removed"
ABSENT_OUT_OF_SCOPE = "out_of_scope"
ABSENT_REFLECT_FAILED = "reflect_failed"
#: 对象索引里从来没有过（名字写错了，或者是对象索引刷新之后才建的）
ABSENT_MISSING = "missing"
#: 升级前探查的源（还没有对象索引）、被截断的老缓存：说不清是哪一种
ABSENT_UNKNOWN = "unknown"

SCOPE_REASONS = ("pinned", "explicit", "rule", "on_demand")


# ==========================================================================
# 对象索引：每种方言一条字典查询
# ==========================================================================


@dataclass(frozen=True)
class IndexedObject:
    """对象索引里的一个对象：名字是 Inspector 规整过的写法（schema_cache 的键）。quote：Oracle 库里本来就是小写的名字
    （SQLAlchemy 反射回来是 quoted_name(quote=True)），反射时要原样带引号。"""

    name: str
    kind: str = "table"
    comment: str | None = None
    est_rows: int | None = None
    quote: bool = False


@dataclass
class IndexResult:
    objects: list[IndexedObject]
    #: dictionary 字典查询 | inspector 退回 Inspector（没有注释）
    method: str
    #: 数据库里一共几个（超过 INDEX_LIMIT 时 objects 只收了前面的）
    total: int
    capped: bool = False
    #: 字典查询失败、退回 Inspector 时的原因（留痕；不挡探查）
    note: str | None = None


def _literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def index_sql(kind: str | None, schema: str | None) -> str | None:
    """拿全库对象清单的那条字典查询：四列（名字、类型、注释、行数估算），按名字排序。schema 是要查的 schema（Oracle 已经
    按数据字典的写法转好大写）；为 None 时查连接的当前 schema。不认识的数据库类型返回 None（退回 Inspector）。"""
    k = (kind or "").lower()
    if k == "sqlite":
        # SQLite 一个文件一个 main（写了 schema 就查那个库的 sqlite_master：写错了照实报「没有这个库」）；临时表不进来；
        # sqlite_ 开头的是内部表（Inspector 也排掉）
        master = ('"' + schema.replace('"', '""') + '".sqlite_master') if schema else "sqlite_master"
        return (f"SELECT name, type, NULL AS comment, NULL AS est_rows FROM {master} "
                "WHERE type IN ('table', 'view') AND substr(name, 1, 7) <> 'sqlite_' ORDER BY name")
    if k in ("mysql", "mariadb"):
        where = _literal(schema) if schema else "DATABASE()"
        return ("SELECT TABLE_NAME, TABLE_TYPE, TABLE_COMMENT, TABLE_ROWS FROM information_schema.TABLES "
                f"WHERE TABLE_SCHEMA = {where} ORDER BY TABLE_NAME")
    if k in ("postgres", "postgresql"):
        where = _literal(schema) if schema else "current_schema()"
        # r 普通表、p 分区表的主表、f 外部表、v 视图、m 物化视图；分区的子表不进（relispartition）：几千个按月分区的子表
        # 会把真正要找的表淹掉，查主表就能查到子表的数据
        return ("SELECT c.relname, c.relkind, obj_description(c.oid, 'pg_class'), c.reltuples "
                "FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
                f"WHERE n.nspname = {where} AND c.relkind IN ('r', 'p', 'f', 'v', 'm') AND NOT c.relispartition "
                "ORDER BY c.relname")
    if k == "oracle":
        owner = _literal(schema) if schema else "SYS_CONTEXT('USERENV', 'CURRENT_SCHEMA')"
        # generated = 'N' 排掉系统生成的对象（IOT 溢出段这类），BIN$ 是回收站里的表
        return ("SELECT o.object_name, o.object_type, c.comments, t.num_rows FROM all_objects o "
                "LEFT JOIN all_tab_comments c ON c.owner = o.owner AND c.table_name = o.object_name "
                "LEFT JOIN all_tables t ON t.owner = o.owner AND t.table_name = o.object_name "
                f"WHERE o.owner = {owner} AND o.object_type IN ('TABLE', 'VIEW') AND o.generated = 'N' "
                "AND o.object_name NOT LIKE 'BIN$%' ORDER BY o.object_name")
    return None


_VIEW_TYPES = frozenset({"view", "v", "m", "system view"})


def _object_kind(raw: Any) -> str:
    return "view" if str(raw or "").strip().lower() in _VIEW_TYPES else "table"


def _est_rows(raw: Any) -> int | None:
    """数据字典的行数估算：≤0（PostgreSQL 没分析过的表是 -1）、读不出来都是不知道。"""
    try:
        n = int(float(str(raw)))
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def _comment(raw: Any, kind: str) -> str | None:
    text = str(raw).strip() if raw is not None else ""
    # MySQL 的视图在 TABLE_COMMENT 里写的是字面的「VIEW」
    if not text or (kind == "view" and text.upper() == "VIEW"):
        return None
    return text


async def list_objects(source: Any, schema: str | None) -> IndexResult:
    """全库的对象清单（不落盘）。字典查询失败退回 Inspector；两条路都走不通抛出原来的异常（调用方照实记成探查失败）。"""
    from app.data import engine as data_engine
    from app.data.guard import QueryLimits

    kind = (getattr(source, "kind", None) or "").lower()
    engine = await data_engine.engines.get(source)
    dialect = engine.dialect
    owner = schema
    if schema and kind == "oracle":
        owner = dialect.denormalize_name(schema)
    sql = index_sql(kind, owner)
    note: str | None = None
    if sql is not None:
        limits = QueryLimits(max_rows=INDEX_LIMIT + 1, max_bytes=64 * 1024 * 1024,
                             timeout_seconds=data_engine.query_timeout(source))
        try:
            result = await data_engine.run_query(source, sql, limits=limits)
        except Exception as e:  # noqa: BLE001 - 字典查询不认（老版本、权限不够）：退回 Inspector
            note = first_line(e) or describe_exception(e)
            logger.info("数据源 %s 的对象清单字典查询失败，退回 Inspector：%s", getattr(source, "name", "?"), note)
        else:
            objects: list[IndexedObject] = []
            for row in result.rows:
                if not row or row[0] is None:
                    continue
                raw_name = str(row[0])
                name: Any = dialect.normalize_name(raw_name) if kind == "oracle" else raw_name
                kind_of = _object_kind(row[1] if len(row) > 1 else None)
                objects.append(IndexedObject(
                    name=str(name), kind=kind_of, comment=_comment(row[2] if len(row) > 2 else None, kind_of),
                    est_rows=_est_rows(row[3] if len(row) > 3 else None), quote=_forced(name)))
            capped = len(objects) > INDEX_LIMIT or bool(getattr(result, "truncated", False))
            return IndexResult(objects=objects[:INDEX_LIMIT], method="dictionary",
                               total=len(objects), capped=capped)

    from sqlalchemy import inspect as sa_inspect

    def _names(sync_conn: Any) -> tuple[list[Any], list[Any]]:
        inspector = sa_inspect(sync_conn)
        return list(inspector.get_table_names(schema=schema)), list(inspector.get_view_names(schema=schema))

    async with engine.connect() as conn:
        tables, views = await conn.run_sync(_names)
    merged = sorted([IndexedObject(str(n), "table", quote=_forced(n)) for n in tables]
                    + [IndexedObject(str(n), "view", quote=_forced(n)) for n in views], key=lambda o: name_key(o.name))
    return IndexResult(objects=merged[:INDEX_LIMIT], method="inspector", total=len(merged),
                       capped=len(merged) > INDEX_LIMIT, note=note)


def _forced(name: Any) -> bool:
    return getattr(name, "quote", None) is True


# ==========================================================================
# 探查范围
# ==========================================================================


@dataclass
class ScopeRules:
    """数据源的探查范围规则（options.introspect_scope）。缺省 mode=all：全部对象（视图也算），上限 500 张。"""

    mode: str = "all"
    include: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)
    tables: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    views: bool = True
    max_tables: int = SCOPE_DEFAULT
    updated_by: str | None = None
    updated_at: str | None = None

    @classmethod
    def from_options(cls, options: Mapping[str, Any] | None) -> ScopeRules:
        raw = (options or {}).get(SCOPE_OPTION)
        return cls.from_dict(raw if isinstance(raw, Mapping) else {})

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> ScopeRules:
        """存着的规则 → ScopeRules。不合规的项按缺省读（存进去之前已经核对过：这里只防手改过的库）。"""
        def words(key: str) -> list[str]:
            value = raw.get(key)
            items = value if isinstance(value, list) else []
            return list(dict.fromkeys(str(x).strip() for x in items if isinstance(x, (str, int)) and str(x).strip()))

        cap = raw.get("max_tables")
        cap = cap if isinstance(cap, int) and not isinstance(cap, bool) and 1 <= cap <= SCOPE_MAX else SCOPE_DEFAULT
        return cls(mode="rules" if raw.get("mode") == "rules" else "all", include=words("include"),
                   exclude=words("exclude"), tables=words("tables"), removed=words("removed"),
                   views=raw.get("views") is not False, max_tables=cap,
                   updated_by=raw.get("updated_by") if isinstance(raw.get("updated_by"), str) else None,
                   updated_at=raw.get("updated_at") if isinstance(raw.get("updated_at"), str) else None)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"mode": self.mode, "include": list(self.include), "exclude": list(self.exclude),
                               "tables": list(self.tables), "removed": list(self.removed), "views": self.views,
                               "max_tables": self.max_tables}
        if self.updated_by is not None:
            out["updated_by"] = self.updated_by
        if self.updated_at is not None:
            out["updated_at"] = self.updated_at
        return out

    def rules_only(self) -> dict[str, Any]:
        """留痕用的规则本身（不含谁改的）。"""
        return {k: v for k, v in self.to_dict().items() if k not in ("updated_by", "updated_at")}

    def matches(self, name: str, kind: str) -> bool:
        """规则命中：mode=all 时全部（包含列表不看），rules 时命中包含里的一条；命中排除的一律不算（排除优先）；
        views=false 时视图不算。显式列出、pinned、按需加入不经这里。"""
        if kind == "view" and not self.views:
            return False
        key = name_key(name)
        if any(_glob(key, p) for p in self.exclude):
            return False
        if self.mode == "all":
            return True
        return any(_glob(key, p) for p in self.include)


def _glob(key: str, pattern: str) -> bool:
    """通配（* ? [...]）按 name_key 比；写了 schema 前缀的（sales.ord_*）只看表名那一段。"""
    pat = name_key(pattern.rsplit(".", 1)[-1])
    return fnmatch.fnmatchcase(key, pat)


def rules_problem(raw: Any) -> str | None:
    """PUT /introspect-scope 交来的规则合不合规：不合规返回一句中文，合规返回 None。"""
    if not isinstance(raw, Mapping):
        return "探查范围要是一个对象：{mode, include, exclude, tables, removed, views, max_tables}"
    if raw.get("mode", "all") not in ("all", "rules"):
        return "探查范围的方式只能是「全部对象」（all）或「按规则」（rules）"
    for key, label, limit in (("include", "包含的规则", RULES_MAX), ("exclude", "排除的规则", RULES_MAX),
                              ("tables", "显式列出的表", SCOPE_MAX), ("removed", "显式移出的表", SCOPE_MAX)):
        value = raw.get(key, [])
        if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
            return f"{label}要是一组文字"
        if len(value) > limit:
            return f"{label}最多 {limit} 条，当前 {len(value)} 条"
        if any(len(x) > RULE_CHARS for x in value):
            return f"{label}每条最长 {RULE_CHARS} 个字"
    cap = raw.get("max_tables", SCOPE_DEFAULT)
    if not isinstance(cap, int) or isinstance(cap, bool) or not 1 <= cap <= SCOPE_MAX:
        return f"探查范围的上限要在 1 到 {SCOPE_MAX:,} 张之间"
    if "views" in raw and not isinstance(raw["views"], bool):
        return "是否包含视图要是 true 或 false"
    return None


@dataclass
class ScopePlan:
    """按规则圈出的范围。in_scope：{名字: 原因}，按名字排序；counts 各原因几张；capped_count 是计上限的那部分（规则、
    pinned、显式；按需加入另算）。"""

    in_scope: dict[str, str]
    counts: dict[str, int]
    cap: int
    index_total: int
    out_of_scope: int

    @property
    def capped_count(self) -> int:
        return sum(n for r, n in self.counts.items() if r != "on_demand")

    @property
    def over(self) -> bool:
        return self.capped_count > self.cap or len(self.in_scope) > SCOPE_MAX

    def summary(self, rules: ScopeRules | None = None) -> dict[str, Any]:
        out: dict[str, Any] = {"in_scope": len(self.in_scope), "hits": self.capped_count, "cap": self.cap,
                               "out_of_scope": self.out_of_scope, "index_total": self.index_total,
                               **{r: self.counts.get(r, 0) for r in SCOPE_REASONS}}
        if rules is not None:
            out["rules"] = rules.rules_only()
        return out


def plan_scope(objects: Iterable[tuple[str, str]], rules: ScopeRules, *, pinned: set[str],
               on_demand: set[str]) -> ScopePlan:
    """objects 是对象索引里还在的对象 (名字, 类型)；pinned、on_demand 是 name_key 的集合。原因的先后：显式移出 > pinned >
    显式列出 > 规则 > 按需加入。"""
    removed = {name_key(n.rsplit(".", 1)[-1]) for n in rules.removed}
    explicit = {name_key(n.rsplit(".", 1)[-1]) for n in rules.tables}
    picked: dict[str, str] = {}
    total = 0
    for name, kind in objects:
        total += 1
        key = name_key(name)
        if key in removed:
            continue
        if key in pinned:
            picked[name] = "pinned"
        elif key in explicit:
            picked[name] = "explicit"
        elif rules.matches(name, kind):
            picked[name] = "rule"
        elif key in on_demand:
            picked[name] = "on_demand"
    counts = {r: 0 for r in SCOPE_REASONS}
    for reason in picked.values():
        counts[reason] += 1
    ordered = dict(sorted(picked.items(), key=lambda kv: name_key(kv[0])))
    return ScopePlan(in_scope=ordered, counts=counts, cap=rules.max_tables, index_total=total,
                     out_of_scope=total - len(ordered))


def too_large_message(plan: ScopePlan) -> str:
    """规则命中超过上限时的整句：命中多少张、其中一律在范围里的几张、上限、怎么办。"""
    pinned = plan.counts.get("pinned", 0)
    held = f"（其中有数据目录或被已发布模板用到、一律在范围内的 {pinned:,} 张）" if pinned else ""
    if len(plan.in_scope) > SCOPE_MAX and plan.capped_count <= plan.cap:
        return (f"探查范围里已有 {len(plan.in_scope):,} 张表（含按需加入的 {plan.counts.get('on_demand', 0):,} 张），"
                f"超过最多 {SCOPE_MAX:,} 张。请在「探查范围」里移出一些按需加入的表，再探查")
    return (f"按探查范围的规则命中 {plan.capped_count:,} 张表{held}，超过上限 {plan.cap:,} 张，没有开始探查。"
            f"请在「探查范围」里收窄规则（加包含、排除的通配），或者把上限调高（最多 {SCOPE_MAX:,} 张）")


# ==========================================================================
# 数据源的 schema 与锁
# ==========================================================================


def index_schema(row: Any) -> str:
    """对象索引按哪个 schema 记：数据源配置的 schema（一个数据源一个 schema，目录-D2），没配是 ""（连接的默认 schema）。"""
    return str((getattr(row, "options", None) or {}).get("schema") or "").strip()


def target_schema(row: Any) -> str | None:
    """反射、字典查询用的 schema：配置了就是它，没配是 None（当前用户的默认 schema）。"""
    return index_schema(row) or None


_locks: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict[str, asyncio.Lock]]" = weakref.WeakKeyDictionary()


def source_lock(source_id: str) -> asyncio.Lock:
    """写一个源的对象索引和 schema_cache 时持的锁（进程内，按事件循环各一份：asyncio.Lock 不能跨循环用）。后台探查的开头
    和收尾、按需加入、加入范围都持它。"""
    loop = asyncio.get_running_loop()
    by_source = _locks.get(loop)
    if by_source is None:
        by_source = _locks[loop] = {}
    lock = by_source.get(source_id)
    if lock is None:
        lock = by_source[source_id] = asyncio.Lock()
    return lock


def _scope_filter(source_id: str, schema: str) -> tuple[Any, ...]:
    return (SourceObject.source_id == source_id, SourceObject.schema_name == schema)


async def has_index(session: AsyncSession, source_id: str, schema: str) -> bool:
    """这个源在这个 schema 下有没有对象索引（升级前探查的源、上传表格都没有）。"""
    return (await session.execute(
        select(SourceObject.id).where(*_scope_filter(source_id, schema)).limit(1))).first() is not None


# ==========================================================================
# 对象索引落盘
# ==========================================================================


@dataclass
class RefreshStats:
    total: int
    added: int
    removed: int
    method: str
    capped: bool
    note: str | None = None
    #: 第一次建对象索引时从升级前的老缓存接进来几张（legacy_seed）
    adopted: int = 0


def _chunks(items: list[Any], size: int = 500) -> Iterable[list[Any]]:
    for i in range(0, len(items), size):
        yield items[i:i + size]


def legacy_seed(cache: Mapping[str, Any] | None, schema: str,
                names: Iterable[str]) -> dict[str, dict[str, Any]]:
    """升级前同步探查写的老缓存里、对象索引第一次建起来时要接进来的表：{对象索引里的名字: 这一行要带上的列}。

    为什么要接：schema_cache 只从对象索引物化（rebuild_schema_cache），老缓存里的表结构却从来没进过对象索引。不接的话，
    升级后第一次探查只要中途停止、连接断开、服务重启，或者排队时有人按需加入、探查失败要记一笔，物化出来的就只剩已经读完
    的那几张：老缓存里还没重读的表静默消失，助手和问数据看到的表突然变少。接进来以后它们就是那时的探查范围（老缓存是按
    「前 200 个对象」探的）：在范围里、算规则命中，带着老结构和它的指纹；结构版本记 0（还没按对象索引读过），第一次读到
    记 1，不把反射格式的差异当成一次结构变化。老缓存里读不到结构的（skipped）照样记成范围里读不到结构。第一次探查任务
    开始执行时照这一次的规则重圈（_IntrospectRun._apply），规则外的照常移出；没重读到的沿用老结构，和以后每一次探查中途
    停下时一样。

    只认老缓存：带 scope 摘要的是从对象索引物化的，不再接；缓存的 schema 和这一次的对不上（以前真探查能临时换 schema）
    也不接，免得把别的 schema 的结构当成这一个的。按名字对，对不上的按 name_key 对（只认唯一的）；对象索引里没有的不接：
    库里已经没有这张表了。"""
    if not isinstance(cache, Mapping) or "scope" in cache:
        return {}
    if str(cache.get("schema") or "").strip() != schema:
        return {}
    tables = cache.get("tables") if isinstance(cache.get("tables"), Mapping) else {}
    skipped = cache.get("skipped") if isinstance(cache.get("skipped"), Mapping) else {}
    if not tables and not skipped:
        return {}
    old: dict[str, tuple[str, Any]] = {str(n): ("meta", m) for n, m in tables.items() if isinstance(m, dict) and m}
    for n, why in skipped.items():
        old.setdefault(str(n), ("error", why))
    by_key: dict[str, list[str]] = {}
    for n in old:
        by_key.setdefault(name_key(n), []).append(n)
    out: dict[str, dict[str, Any]] = {}
    for name in names:
        hit = name if name in old else None
        if hit is None and len(by_key.get(name_key(name), [])) == 1:
            hit = by_key[name_key(name)][0]
        if hit is None:
            continue
        what, value = old[hit]
        seed: dict[str, Any] = {"in_scope": True, "scope_reason": "rule"}
        if what == "meta":
            seed.update(meta=value, fingerprint=fingerprint(value))
        else:
            seed["reflect_error"] = str(value or "") or "读不到列"
        out[name] = seed
    return out


async def refresh_index(session: AsyncSession, source_id: str, schema: str, result: IndexResult, *,
                        legacy: Mapping[str, Any] | None = None) -> RefreshStats:
    """把字典查询的结果写进对象索引（不提交）：新出现的对象加一行；还在的更新类型、注释、行数估算、last_seen_at（删了又
    出现的清掉 removed_at）；不在了的记 removed_at。索引被截断（capped）时不判删除：没收进来的不等于不在了。

    legacy 是数据源此刻的 schema_cache：这个 schema 的对象索引第一次建起来时，老缓存里的表结构接进对应的行（legacy_seed）。"""
    now = utcnow()
    existing = {r.name: r for r in (await session.execute(
        select(SourceObject.id, SourceObject.name, SourceObject.removed_at, SourceObject.kind, SourceObject.comment,
               SourceObject.est_rows).where(*_scope_filter(source_id, schema)))).all()}
    seeds = legacy_seed(legacy, schema, (o.name for o in result.objects)) if not existing else {}
    seen: set[str] = set()
    new_rows: list[dict[str, Any]] = []
    changed: list[dict[str, Any]] = []
    revived: list[str] = []
    for obj in result.objects:
        if obj.name in seen:
            continue
        seen.add(obj.name)
        old = existing.get(obj.name)
        if old is None:
            new_rows.append({"source_id": source_id, "schema_name": schema, "name": obj.name,
                             "name_key": name_key(obj.name), "kind": obj.kind, "comment": obj.comment,
                             "est_rows": obj.est_rows, "quote_name": obj.quote, "first_seen_at": now,
                             "last_seen_at": now, **seeds.get(obj.name, {})})
            continue
        if old.removed_at is not None:
            revived.append(old.id)
        if (old.kind, old.comment, old.est_rows) != (obj.kind, obj.comment, obj.est_rows):
            changed.append({"_id": old.id, "kind": obj.kind, "comment": obj.comment, "est_rows": obj.est_rows})
    _SO = SourceObject
    for chunk in _chunks(new_rows):
        await session.execute(insert(_SO), chunk)
    if changed:
        # 按表（不按 ORM 实体）的 executemany：ORM 的按主键批量更新要求参数里写主键列的名字
        table = SourceObject.__table__
        stmt = (update(table).where(table.c.id == bindparam("_id"))
                .values(kind=bindparam("kind"), comment=bindparam("comment"), est_rows=bindparam("est_rows")))
        conn = await session.connection()
        for chunk in _chunks(changed):
            await conn.execute(stmt, chunk)
    still = [r.id for name, r in existing.items() if name in seen]
    for chunk in _chunks(still):
        await session.execute(update(_SO).where(_SO.id.in_(chunk)).values(last_seen_at=now)
                              .execution_options(synchronize_session=False))
    for chunk in _chunks(revived):
        await session.execute(update(_SO).where(_SO.id.in_(chunk)).values(removed_at=None)
                              .execution_options(synchronize_session=False))
    gone: list[str] = []
    if not result.capped:
        gone = [r.id for name, r in existing.items() if name not in seen and r.removed_at is None]
        for chunk in _chunks(gone):
            # 真删除：不在范围里了，结构（meta）留着——下一波的结构差异要拿它和以前比
            await session.execute(update(_SO).where(_SO.id.in_(chunk)).values(removed_at=now, in_scope=False)
                                  .execution_options(synchronize_session=False))
    return RefreshStats(total=len(seen), added=len(new_rows), removed=len(gone), method=result.method,
                        capped=result.capped, note=result.note, adopted=len(seeds))


async def live_objects(session: AsyncSession, source_id: str, schema: str) -> list[tuple[str, str]]:
    """对象索引里还在的对象 (名字, 类型)，按名字。"""
    return [(r.name, r.kind) for r in (await session.execute(
        select(SourceObject.name, SourceObject.kind).where(*_scope_filter(source_id, schema),
                                                           SourceObject.removed_at.is_(None))
        .order_by(SourceObject.name_key))).all()]


async def pinned_keys(session: AsyncSession, row: Any, names: Iterable[str]) -> set[str]:
    """一律在范围里的表（name_key）：有数据目录的表，加上已发布模板写死的 SQL 用到的这个源的表（catalog_impact._published
    + graph_sql_tables，和影响面同一份）。图解析不了的版本跳过。"""
    from app.data.catalog_impact import _published, graph_sql_tables
    from app.db.models import CatalogNote
    from app.engine.schema import GraphSpec

    keys = {name_key(t) for t in (await session.execute(
        select(CatalogNote.table_name).where(CatalogNote.source_id == row.id))).scalars()}
    known = list(names)
    for _workflow, version in await _published(session):
        try:
            spec = GraphSpec.model_validate(version.graph or {})
        except Exception:  # noqa: BLE001 - 解析不了的版本发起不了正式运行
            continue
        hits = graph_sql_tables(spec, {row.name: known}).get(row.name)
        if hits:
            keys |= {name_key(t) for t in hits}
    return keys


async def on_demand_keys(session: AsyncSession, source_id: str, schema: str) -> set[str]:
    return {name_key(n) for n in (await session.execute(
        select(SourceObject.name).where(*_scope_filter(source_id, schema), SourceObject.scope_reason == "on_demand",
                                        SourceObject.in_scope.is_(True)))).scalars()}


async def plan_for(session: AsyncSession, row: Any, rules: ScopeRules | None = None) -> ScopePlan:
    """按规则（不给是数据源存着的）和这一刻的对象索引圈范围。"""
    schema = index_schema(row)
    rules = rules or ScopeRules.from_options(getattr(row, "options", None))
    objects = await live_objects(session, row.id, schema)
    return plan_scope(objects, rules, pinned=await pinned_keys(session, row, [n for n, _ in objects]),
                      on_demand=await on_demand_keys(session, row.id, schema))


async def apply_scope(session: AsyncSession, source_id: str, schema: str, plan: ScopePlan) -> None:
    """把圈好的范围写进对象索引（不提交）：离开范围的清掉结构（指纹和版本留着），范围内的按原因记。"""
    current = {r.name: r for r in (await session.execute(
        select(SourceObject.id, SourceObject.name, SourceObject.scope_reason).where(
            *_scope_filter(source_id, schema), SourceObject.in_scope.is_(True)))).all()}
    leaving = [r.id for name, r in current.items() if name not in plan.in_scope]
    for chunk in _chunks(leaving):
        await session.execute(update(SourceObject).where(SourceObject.id.in_(chunk))
                              .values(in_scope=False, scope_reason=None, meta=None, reflect_error=None)
                              .execution_options(synchronize_session=False))
    by_reason: dict[str, list[str]] = {}
    for name, reason in plan.in_scope.items():
        old = current.get(name)
        if old is None or old.scope_reason != reason:
            by_reason.setdefault(reason, []).append(name)
    for reason, names in by_reason.items():
        for chunk in _chunks(names):
            await session.execute(update(SourceObject).where(*_scope_filter(source_id, schema),
                                                             SourceObject.name.in_(chunk))
                                  .values(in_scope=True, scope_reason=reason)
                                  .execution_options(synchronize_session=False))


# ==========================================================================
# 反射与结构版本
# ==========================================================================


def fingerprint(meta: Mapping[str, Any]) -> str:
    """表结构的指纹：反射到的整份结构的规范 JSON（键排序）的 sha256。"""
    text = json.dumps(meta, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass
class Reflected:
    tables: dict[str, dict[str, Any]]
    skipped: dict[str, str]


async def reflect(source: Any, names: list[tuple[str, bool]], *, views: set[str],
                  timeout: float | None = None) -> Reflected:
    """只读反射这几个对象（名字, 要不要强制引号）。连接本身出错抛出原来的异常；单个对象读不到的进 skipped。"""
    from sqlalchemy.sql.elements import quoted_name

    from app.data import engine as data_engine
    from app.data import introspect

    engine = await data_engine.engines.get(source)
    target = target_schema(source)
    picked = [quoted_name(n, True) if quote else n for n, quote in names]

    def _go(sync_conn: Any) -> dict[str, Any]:
        return introspect.reflect_names(sync_conn, target, picked, views)

    async def _run() -> dict[str, Any]:
        async with engine.connect() as conn:
            return await conn.run_sync(_go)

    got = await (asyncio.wait_for(_run(), timeout=timeout) if timeout else _run())
    return Reflected(tables={str(k): v for k, v in got["tables"].items()},
                     skipped={str(k): v for k, v in got["skipped"].items()})


async def save_reflected(session: AsyncSession, source_id: str, schema: str, got: Reflected) -> dict[str, int]:
    """反射结果写进对象索引的行（不提交）：读到的换上新结构，指纹变了结构版本加 1（第一次读到是 1）；读不到的记原因，
    结构留着上一次读到的。返回 {名字: 结构版本}。"""
    names = list(got.tables) + list(got.skipped)
    if not names:
        return {}
    rows = {r.name: r for r in (await session.execute(
        select(SourceObject.id, SourceObject.name, SourceObject.fingerprint, SourceObject.struct_version).where(
            *_scope_filter(source_id, schema), SourceObject.name.in_(names)))).all()}
    now = utcnow()
    versions: dict[str, int] = {}
    for name, meta in got.tables.items():
        row = rows.get(name)
        if row is None:
            continue
        fp = fingerprint(meta)
        version = row.struct_version or 0
        if fp != row.fingerprint or not version:
            version += 1
        versions[name] = version
        await session.execute(update(SourceObject).where(SourceObject.id == row.id).values(
            meta=meta, fingerprint=fp, struct_version=version, reflect_error=None, reflected_at=now)
            .execution_options(synchronize_session=False))
    for name, why in got.skipped.items():
        row = rows.get(name)
        if row is None:
            continue
        versions[name] = row.struct_version or 0
        await session.execute(update(SourceObject).where(SourceObject.id == row.id).values(reflect_error=why)
                              .execution_options(synchronize_session=False))
    return versions


# ==========================================================================
# 物化 schema_cache
# ==========================================================================

#: schema_cache 里由探查失败那一刻写的键：下一次物化时（探查成功）清掉，按需加入时原样留着
_FAILURE_KEYS = ("failed", "error", "available_schemas")


async def rebuild_schema_cache(session: AsyncSession, row: Any, *, failure: Mapping[str, Any] | None = None,
                               keep_failure: bool = False, synced: bool = False,
                               index: RefreshStats | None = None,
                               candidates: list[str] | None = None) -> dict[str, Any]:
    """从范围内已反射的行物化 schema_cache（唯一写它的地方），写进 row（不提交），返回新的缓存。

    形状和以前一样：tables（表在前、视图在后，各按名字）、truncated（有范围外的对象）、total（对象索引里的对象总数）、
    schema、synced_at；范围里却读不到结构的进 skipped（同以前的「跳过的对象」）；另加 scope 摘要。failure 给了的记上
    探查失败（failed、error、available_schemas，表结构照旧留着：助手还能按上一次读到的写 SQL）；keep_failure 时沿用缓存里
    原有的失败标记（按需加入不改它）。synced：这次是一次探查完成（synced_at 换成现在），否则沿用原来的。index：这次刷新
    对象索引的结果（字典查询还是 Inspector、有没有被截断），不给沿用缓存里原来记的。candidates：对象索引一个对象都没有时
    这台服务器上能选的 schema（建任务时拿的，这里不连库），写进 available_schemas——schema 填错多半是这样，不是失败，
    failed 不置。"""
    from app.data.source_objects import ScopeRules as _Rules

    schema = index_schema(row)
    rows = (await session.execute(
        select(SourceObject.name, SourceObject.kind, SourceObject.in_scope, SourceObject.scope_reason,
               SourceObject.meta, SourceObject.reflect_error, SourceObject.removed_at)
        .where(*_scope_filter(row.id, schema)).order_by(SourceObject.name_key))).all()
    old = dict(getattr(row, "schema_cache", None) or {})
    tables: dict[str, Any] = {}
    views: dict[str, Any] = {}
    skipped: dict[str, str] = {}
    counts = {r: 0 for r in SCOPE_REASONS}
    total = in_scope = failed = 0
    for r in rows:
        if r.removed_at is not None:
            continue
        total += 1
        if not r.in_scope:
            continue
        in_scope += 1
        if r.scope_reason in counts:
            counts[r.scope_reason] += 1
        if isinstance(r.meta, dict) and r.meta:
            (views if r.kind == "view" else tables)[r.name] = r.meta
        if r.reflect_error and not (isinstance(r.meta, dict) and r.meta):
            skipped[r.name] = r.reflect_error
            failed += 1
    rules = _Rules.from_options(getattr(row, "options", None))
    old_scope = old.get("scope") if isinstance(old.get("scope"), dict) else {}
    index_info = ({"index_method": index.method, "index_capped": index.capped, "index_at": utcnow().isoformat()}
                  if index is not None else {k: old_scope[k] for k in ("index_method", "index_capped", "index_at")
                                             if k in old_scope})
    cache: dict[str, Any] = {
        "tables": {**tables, **views},
        "truncated": total > in_scope,
        "total": total,
        "schema": target_schema(row),
        "synced_at": datetime.now(timezone.utc).isoformat() if synced or not old.get("synced_at")
        else old.get("synced_at"),
        "scope": {"in_scope": in_scope, "reflected": len(tables) + len(views), "failed": failed,
                  "out_of_scope": total - in_scope, "cap": rules.max_tables, "index_total": total, **counts,
                  **index_info},
    }
    if skipped:
        cache["skipped"] = skipped
    if candidates and not total:
        cache["available_schemas"] = list(candidates)
    if failure:
        cache.update({k: failure[k] for k in _FAILURE_KEYS if k in failure})
    elif keep_failure:
        cache.update({k: old[k] for k in _FAILURE_KEYS if k in old})
    row.schema_cache = cache
    return cache


# ==========================================================================
# 三种「不在」、对象索引里的查找
# ==========================================================================


async def absent_reasons(session: AsyncSession, row: Any, names: Iterable[str]) -> dict[str, str]:
    """一批不在 schema_cache 里的表名 → 为什么不在（removed / missing / out_of_scope / reflect_failed / unknown）。

    没有对象索引的源（升级前探查的）：缓存的跳过清单里有的是读不到结构；老缓存截断过的说不清（unknown）；没截断的就是
    表已不在。有对象索引的：记了 removed_at 的是表已不在（真删除）；索引里从来没有的是 missing（索引被截断过的说不清）；
    不在范围里的是范围外；在范围里的是读不到结构。"""
    wanted = list(dict.fromkeys(str(n) for n in names))
    if not wanted:
        return {}
    cache = getattr(row, "schema_cache", None) or {}
    schema = index_schema(row)
    if not await has_index(session, row.id, schema):
        skipped = cache.get("skipped") or {}
        truncated = bool(cache.get("truncated"))
        return {n: ABSENT_REFLECT_FAILED if n in skipped else ABSENT_UNKNOWN if truncated else ABSENT_REMOVED
                for n in wanted}
    keys = {name_key(n.rsplit(".", 1)[-1]): n for n in wanted}
    found = {r.name_key: r for r in (await session.execute(
        select(SourceObject.name_key, SourceObject.in_scope, SourceObject.removed_at, SourceObject.meta,
               SourceObject.reflect_error)
        .where(*_scope_filter(row.id, schema), SourceObject.name_key.in_(list(keys))))).all()}
    capped = bool((cache.get("scope") or {}).get("index_capped"))
    out: dict[str, str] = {}
    for key, name in keys.items():
        r = found.get(key)
        if r is None:
            out[name] = ABSENT_UNKNOWN if capped else ABSENT_MISSING
        elif r.removed_at is not None:
            out[name] = ABSENT_REMOVED
        elif not r.in_scope:
            out[name] = ABSENT_OUT_OF_SCOPE
        else:
            out[name] = ABSENT_REFLECT_FAILED
    return out


@dataclass(frozen=True)
class OutsideObject:
    """对象索引里在、范围外的一个对象（助手检索、目录搜索用）。"""

    name: str
    kind: str
    comment: str | None
    est_rows: int | None = None


async def outside_objects(session: AsyncSession, row: Any) -> list[OutsideObject]:
    """范围外、还在的对象，按名字。没有对象索引是空列表。"""
    return [OutsideObject(r.name, r.kind, r.comment, r.est_rows) for r in (await session.execute(
        select(SourceObject.name, SourceObject.kind, SourceObject.comment, SourceObject.est_rows)
        .where(*_scope_filter(row.id, index_schema(row)), SourceObject.removed_at.is_(None),
               SourceObject.in_scope.is_(False)).order_by(SourceObject.name_key))).all()]


async def outside_names(session: AsyncSession, row: Any) -> frozenset[str]:
    """范围外、还在的对象的 name_key（剖析分清「范围外」和「表结构里没有」、SQL 检查列「未按目录检查的表」用）。"""
    return frozenset(r for r in (await session.execute(
        select(SourceObject.name_key).where(*_scope_filter(row.id, index_schema(row)),
                                            SourceObject.removed_at.is_(None),
                                            SourceObject.in_scope.is_(False)))).scalars())


async def search_index(session: AsyncSession, row: Any, query: str, *, limit: int = 50) -> tuple[list[dict[str, Any]], int]:
    """按名字、注释在对象索引里找（不分大小写的包含），名字完全相同的在前、再是开头相同的、再是名字里有的、注释里有的。
    返回 (前 limit 个 {name, kind, comment, est_rows, in_scope, scope_reason, absent_reason}, 一共命中几个)。"""
    text = query.strip()
    if not text:
        return [], 0
    key = name_key(text.rsplit(".", 1)[-1])
    filt = (*_scope_filter(row.id, index_schema(row)), SourceObject.removed_at.is_(None),
            or_(SourceObject.name_key.contains(key, autoescape=True),
                SourceObject.comment.contains(text, autoescape=True)))
    total = int((await session.execute(select(func.count()).select_from(SourceObject).where(*filt))).scalar() or 0)
    rows = (await session.execute(
        select(SourceObject.name, SourceObject.name_key, SourceObject.kind, SourceObject.comment,
               SourceObject.est_rows, SourceObject.in_scope, SourceObject.scope_reason, SourceObject.meta,
               SourceObject.reflect_error).where(*filt))).all()

    def rank(r: Any) -> tuple[int, str]:
        if r.name_key == key:
            return 0, r.name_key
        if r.name_key.startswith(key):
            return 1, r.name_key
        if key in r.name_key:
            return 2, r.name_key
        return 3, r.name_key

    out = []
    for r in sorted(rows, key=rank)[:limit]:
        reflected = isinstance(r.meta, dict) and bool(r.meta)
        out.append({"name": r.name, "kind": r.kind, "comment": r.comment, "est_rows": r.est_rows,
                    "in_scope": bool(r.in_scope), "scope_reason": r.scope_reason if r.in_scope else None,
                    "absent_reason": None if r.in_scope and reflected
                    else ABSENT_REFLECT_FAILED if r.in_scope else ABSENT_OUT_OF_SCOPE})
    return out, total


async def on_demand_list(session: AsyncSession, row: Any, *, limit: int = 200) -> list[dict[str, Any]]:
    """按需加入、目录页手工加入范围的表和留痕，新的在前。"""
    rows = (await session.execute(
        select(SourceObject).where(*_scope_filter(row.id, index_schema(row)), SourceObject.in_scope.is_(True),
                                   SourceObject.added_at.is_not(None))
        .order_by(SourceObject.added_at.desc()).limit(limit))).scalars()
    return [trail_dict(r) for r in rows]


def trail_dict(r: SourceObject) -> dict[str, Any]:
    return {"name": r.name, "scope_reason": r.scope_reason, "via": r.added_via, "by": r.added_by,
            "run_id": r.added_run_id, "conversation_id": r.added_conversation_id, "ref": r.added_ref,
            "need": r.added_need, "at": r.added_at.isoformat() if r.added_at else None}


async def count_added_in_run(session: AsyncSession, run_id: str) -> int:
    """这次运行里按需加入了几张（各源合计）：每次运行最多 ON_DEMAND_PER_RUN 张。"""
    return int((await session.execute(
        select(func.count()).select_from(SourceObject).where(SourceObject.added_run_id == run_id,
                                                             SourceObject.scope_reason == "on_demand")
    )).scalar() or 0)


# ==========================================================================
# 按需加入、加入范围
# ==========================================================================


@dataclass
class Trail:
    """按需加入的留痕：经哪个入口、谁、哪次运行 / 哪一轮对话、哪一下（look 编号、节点 id）、需求原文。"""

    via: str
    actor: str | None = None
    run_id: str | None = None
    conversation_id: str | None = None
    ref: str | None = None
    need: str | None = None


@dataclass
class AddOutcome:
    """加入范围的结果。added：加进来、读到了结构的（schema_cache 的键）；already：本来就在范围里、读得到的；failed：
    {名字: 读不到的原因}（也加进了范围，下次探查再读）；refused：{名字: 为什么没加}（对象索引里没有、范围满了）；
    cache：加完以后的 schema_cache（什么都没加时为 None）。"""

    added: list[str] = field(default_factory=list)
    already: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)
    refused: dict[str, str] = field(default_factory=dict)
    cache: dict[str, Any] | None = None


NOT_IN_INDEX = "对象索引里没有这张表（数据源里没有，或者是对象索引刷新之后才建的：重新探查结构后再试）"
REMOVED_FROM_INDEX = "对象索引里这张表已经不在了（表已删除）"
SCOPE_FULL = f"探查范围已有 {{n:,}} 张表，最多 {SCOPE_MAX:,} 张，不能再加入"
EXPLICITLY_REMOVED = "这张表被显式移出了探查范围：要用它，请在「探查范围」里把它从移出的表里去掉"


async def add_to_scope(source_id: str, names: list[str], *, trail: Trail, reason: str = "on_demand") -> AddOutcome:
    """把对象索引里范围外的这几张表加进范围：当场只读反射、落盘、记下留痕，再物化 schema_cache。reason：on_demand（助手、
    运行时按需加入）或 explicit（目录页手工加入，同时记进规则的显式列出）。用自己的会话，不碰调用方会话里加载的对象。
    反射时限是这个源的单条查询时限（大约 5 条字典查询，见 engine.query_timeout）。"""
    from app.data import engine as data_engine
    from app.db.models import DataSource

    out = AddOutcome()
    wanted = list(dict.fromkeys(n.strip() for n in names if n and n.strip()))
    if not wanted:
        return out
    async with source_lock(source_id):
        async with SessionLocal() as session:
            row = await session.get(DataSource, source_id)
            if row is None:
                out.refused = dict.fromkeys(wanted, "数据源不存在，可能已被删除")
                return out
            schema = index_schema(row)
            rules = ScopeRules.from_options(row.options)
            removed = {name_key(n.rsplit(".", 1)[-1]) for n in rules.removed}
            keys = {name_key(n.rsplit(".", 1)[-1]): n for n in wanted}
            found = {r.name_key: r for r in (await session.execute(
                select(SourceObject).where(*_scope_filter(source_id, schema),
                                           SourceObject.name_key.in_(list(keys))))).scalars()}
            in_scope = int((await session.execute(select(func.count()).select_from(SourceObject).where(
                *_scope_filter(source_id, schema), SourceObject.in_scope.is_(True),
                SourceObject.removed_at.is_(None)))).scalar() or 0)
            todo: list[SourceObject] = []
            for key, asked in keys.items():
                obj = found.get(key)
                if obj is None:
                    out.refused[asked] = NOT_IN_INDEX
                elif obj.removed_at is not None:
                    out.refused[asked] = REMOVED_FROM_INDEX
                elif obj.in_scope and isinstance(obj.meta, dict) and obj.meta:
                    out.already.append(obj.name)
                elif key in removed and reason != "explicit":
                    out.refused[asked] = EXPLICITLY_REMOVED
                elif not obj.in_scope and in_scope + len(todo) >= SCOPE_MAX:
                    out.refused[asked] = SCOPE_FULL.format(n=in_scope + len(todo))
                else:
                    todo.append(obj)
            if not todo:
                return out
            try:
                got = await reflect(row, [(o.name, bool(o.quote_name)) for o in todo],
                                    views={o.name for o in todo if o.kind == "view"},
                                    timeout=data_engine.query_timeout(row) * 2)
            except asyncio.TimeoutError:
                for o in todo:
                    out.failed[o.name] = f"读表结构超过 {data_engine.query_timeout(row) * 2:g} 秒没有完成"
                got = Reflected(tables={}, skipped=dict(out.failed))
            except Exception as e:  # noqa: BLE001 - 连不上：照实记下，不挡调用方
                why = first_line(e) or describe_exception(e)
                for o in todo:
                    out.failed[o.name] = why
                got = Reflected(tables={}, skipped=dict(out.failed))
            now = utcnow()
            for o in todo:
                was_in = bool(o.in_scope)
                o.in_scope = True
                if not was_in or (reason == "explicit" and o.scope_reason in (None, "on_demand")):
                    o.scope_reason = reason
                elif was_in:
                    # 本来就在范围里、只是上次没读到结构（pinned、规则命中的）：这次重读一遍，不算「加入」，不记留痕
                    continue
                o.added_via, o.added_by, o.added_at = trail.via, (trail.actor or None), now
                o.added_run_id, o.added_conversation_id = trail.run_id, trail.conversation_id
                o.added_ref = (trail.ref or None) and str(trail.ref)[:64]
                o.added_need = (trail.need or None) and str(trail.need)[:2000]
            await session.flush()
            await save_reflected(session, source_id, schema, got)
            out.added = [o.name for o in todo if o.name in got.tables]
            for name, why in got.skipped.items():
                out.failed.setdefault(name, why)
            if reason == "explicit":
                listed = {name_key(t) for t in rules.tables}
                rules.tables += [o.name for o in todo if name_key(o.name) not in listed]
                rules.removed = [n for n in rules.removed
                                 if name_key(n.rsplit(".", 1)[-1]) not in {name_key(o.name) for o in todo}]
                rules.updated_by, rules.updated_at = trail.actor, now.isoformat()
                row.options = {**(row.options or {}), SCOPE_OPTION: rules.to_dict()}
            out.cache = await rebuild_schema_cache(session, row, keep_failure=True)
            await session.commit()
    logger.info("数据源 %s 按需加入范围：%s（经 %s）", source_id, "、".join(out.added) or "无", trail.via)
    return out


async def delete_index(session: AsyncSession, source_id: str) -> None:
    """删掉一个源的对象索引（测试、清理用；删数据源时随外键级联删除）。"""
    await session.execute(delete(SourceObject).where(SourceObject.source_id == source_id))


__all__ = [
    "ABSENT_MISSING", "ABSENT_OUT_OF_SCOPE", "ABSENT_REFLECT_FAILED", "ABSENT_REMOVED", "ABSENT_UNKNOWN", "ADD_LIMIT", "AddOutcome",
    "INDEX_LIMIT", "IndexResult", "IndexedObject", "OutsideObject", "REFLECT_BATCH", "RULES_MAX", "Reflected",
    "RefreshStats", "SCOPE_DEFAULT", "SCOPE_MAX", "SCOPE_OPTION", "SCOPE_REASONS", "ScopePlan", "ScopeRules", "Trail",
    "VIA_CATALOG", "VIA_LOOK", "VIA_SCHEMA_TOOL", "absent_reasons", "add_to_scope", "apply_scope",
    "count_added_in_run", "fingerprint", "has_index", "index_schema", "index_sql", "legacy_seed", "list_objects",
    "live_objects",
    "on_demand_keys", "on_demand_list", "outside_names", "outside_objects", "pinned_keys", "plan_for", "plan_scope",
    "rebuild_schema_cache", "reflect", "refresh_index", "rules_problem", "save_reflected", "search_index",
    "source_lock", "target_schema", "too_large_message", "trail_dict",
]
