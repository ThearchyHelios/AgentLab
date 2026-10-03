"""探查数据库结构。

产出两种粒度，对应两种消费者：

- `summary()`：表名 + 一句话，几百 token。常驻 Copilot 的 system prompt，
  让它知道"有哪些库、每个库大概有什么"。
- `describe_table()`：某张表的完整字段。按需取，Copilot 决定要查某张表时才拉。

分两种粒度是因为 token 预算：一个中等业务库几百张表、每表几十个字段，
全量塞进 prompt 轻松几万 token，既贵又会把真正的需求淹掉。

用 SQLAlchemy 的 Inspector 而不是手写各家的 information_schema 查询——
方言差异它已经处理过了，我们不必再趟一遍。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import inspect as sa_inspect
from sqlalchemy.engine.reflection import ObjectKind

from app.core.errors import describe_exception, first_line
from app.data.engine import engines

# 一次探查最多带回多少张表。生产库动辄上千张，全拉回来既慢又没人看得完。
_MAX_TABLES = 200
# summary 里每张表列几个代表字段。够 Copilot 判断"这表是不是我要的"即可。
_PREVIEW_COLUMNS = 6
# 批量反射失败、逐个对象重试时，开头连着这么多个都取不到就不再试了
_GIVE_UP_AFTER = 5


# 各家的系统 schema，发现可用 schema 时要排掉——否则用户面对的是一屏
# MDSYS / CTXSYS / XDB 这类内部对象，真正的业务库反而埋在里面。
_SYSTEM_SCHEMAS = frozenset({
    # Oracle
    "sys", "system", "sysaux", "mdsys", "ctxsys", "xdb", "olapsys", "wmsys",
    "lbacsys", "ordsys", "orddata", "outln", "dbsnmp", "appqossys", "audsys",
    "gsmadmin_internal", "dvsys", "ojvmsys", "dbsfwuser", "remote_scheduler_agent",
    # PostgreSQL / MySQL
    "information_schema", "pg_catalog", "pg_toast", "performance_schema", "mysql", "sys",
})


# 统计每个 schema 下有多少对象。两条路径覆盖全部支持的库：
# information_schema 是 SQL 标准（MySQL / PostgreSQL / SQL Server 都有），
# Oracle 不遵循这条标准，用它自己的 all_tables / all_views 数据字典。
#
# 不用"逐个 schema 调 get_table_names"的通用写法：这个库有 94 个 schema，
# 那样是 94 次往返，几十秒起步。
_SCHEMA_STATS_SQL = {
    "oracle": """
        SELECT owner AS s, COUNT(*) AS n FROM (
            SELECT owner FROM all_tables
            UNION ALL SELECT owner FROM all_views
        ) GROUP BY owner ORDER BY COUNT(*) DESC
    """,
    "_standard": """
        SELECT table_schema AS s, COUNT(*) AS n
        FROM information_schema.tables
        GROUP BY table_schema ORDER BY COUNT(*) DESC
    """,
}


async def schema_stats(source: Any) -> list[tuple[str, int]]:
    """每个 schema 有多少表/视图，多的在前。拿不到就返回空表让调用方降级。"""
    from sqlalchemy import text as _text

    kind = (source.kind or "").lower()
    if kind == "sqlite":
        return []  # SQLite 只有 main，没有可选项
    sql = _SCHEMA_STATS_SQL.get(kind, _SCHEMA_STATS_SQL["_standard"])
    engine = await engines.get(source)
    try:
        async with engine.connect() as conn:
            rows = (await conn.execute(_text(sql))).fetchall()
        return [(str(r[0]), int(r[1])) for r in rows if r[0]]
    except Exception:  # noqa: BLE001 - 方言不认就算了，降级到纯名字列表
        return []


async def list_schemas(source: Any) -> list[str]:
    """列出**可能装着业务数据**的 schema，最有可能的排在最前。

    企业环境里只读账号名下往往一张表都没有——数据在别的 schema，靠跨库授权或
    synonym 访问。直接返回"0 张表"等于把人晾在那儿。

    但光列出名字也不够用：实测一个 Oracle 实例有 94 个用户，靠黑名单过滤完
    还剩 75 个（anonymous、C##OGGADMIN、xs$null…），人在里面找不到那两个
    业务 schema。所以主要依据不是"猜哪些是系统的"，而是"哪些里面真有东西"——
    黑名单只用来排掉已知的系统 schema（它们对象数往往还很多，光靠排序压不下去）。
    """
    engine = await engines.get(source)

    def _names(sync_conn: Any) -> list[str]:
        try:
            return list(sa_inspect(sync_conn).get_schema_names())
        except Exception:  # noqa: BLE001 - 有些方言不支持
            return []

    stats = await schema_stats(source)
    if stats:
        # 有统计就只留真有对象的，并按对象数降序——业务库自然浮到前面
        return [s for s, n in stats if n > 0 and s.lower() not in _SYSTEM_SCHEMAS]

    async with engine.connect() as conn:
        names = await conn.run_sync(_names)
    return [n for n in names if n.lower() not in _SYSTEM_SCHEMAS]


def _target_schema(source: Any, explicit: str | None) -> str | None:
    """探查哪个 schema：显式参数 > 数据源配置 > 当前用户默认。"""
    if explicit:
        return explicit
    configured = (source.options or {}).get("schema")
    return str(configured) if configured else None


def _foreign_keys(raw: list[dict[str, Any]], target_schema: str | None,
                  default_schema: str | None) -> list[dict[str, Any]]:
    """反射出来的外键 → [{columns, to_table, to_columns}]。

    to_table 和 schema_cache["tables"] 的键同一个口径：指向同一个 schema 的只写表名；跨 schema 的写
    「schema.表名」（那张表不在这次探查的结果里，关系照记，图里是一个悬空的端点）。to_columns 可能为空
    （SQLite 的 REFERENCES t 省略了列），由数据目录按被指向表的主键补。
    """
    home = {s for s in (target_schema, default_schema) if s}
    out: list[dict[str, Any]] = []
    for fk in raw:
        cols = [c for c in fk.get("constrained_columns") or [] if c]
        to_table = fk.get("referred_table")
        if not cols or not to_table:
            continue
        ref_schema = fk.get("referred_schema")
        out.append({
            "columns": cols,
            "to_table": to_table if not ref_schema or ref_schema in home else f"{ref_schema}.{to_table}",
            "to_columns": [c for c in fk.get("referred_columns") or [] if c],
        })
    return out


def _unique_groups(constraints: list[dict[str, Any]], indexes: list[dict[str, Any]],
                   pk: list[str]) -> list[list[str]]:
    """唯一约束和唯一索引的列组，去重，不含和主键相同的那组。

    同一组唯一列在各家可能出现两次：MySQL 的 UNIQUE 约束本身就是唯一索引，PostgreSQL 的唯一约束背后也有
    一个索引，Oracle 的主键背后还有一个唯一索引。表达式索引（列名里有 None）不算。
    """
    seen = {tuple(c.lower() for c in pk)} if pk else set()
    groups: list[list[str]] = []
    candidates = [u.get("column_names") for u in constraints]
    candidates += [ix.get("column_names") for ix in indexes if ix.get("unique")]
    for cols in candidates:
        if not cols or any(c is None for c in cols):
            continue
        key = tuple(str(c).lower() for c in cols)
        if key in seen:
            continue
        seen.add(key)
        groups.append([str(c) for c in cols])
    return groups


async def introspect(source: Any, *, schema: str | None = None) -> dict[str, Any]:
    """探查整个库的结构，结果直接存进 DataSource.schema_cache。

    每张表：列（名字、类型、可空、注释）、主键、表注释、是不是视图；读得到的话还有外键约束
    （foreign_keys）和唯一约束 / 唯一索引（unique）——读不到的表不写这两个键，探查照常完成。
    """
    engine = await engines.get(source)
    target = _target_schema(source, schema)

    def _collect(sync_conn: Any) -> dict[str, Any]:
        inspector = sa_inspect(sync_conn)
        target_schema = target
        names = inspector.get_table_names(schema=target_schema)
        views = inspector.get_view_names(schema=target_schema)
        truncated = len(names) + len(views) > _MAX_TABLES
        picked = (names + views)[:_MAX_TABLES]

        # 批量反射接口（SQLAlchemy 2.0）。
        #
        # **别指望它能治超时。** 实测 60 张表：逐表调用 121 次往返、批量 122 次
        # ——SQLAlchemy 的反射缓存本来就把 get_columns / get_pk_constraint 合并
        # 成了同一次底层查询，而 MySQL 方言压根没实现 get_multi_*，落到基类
        # 还是每表一次 SHOW CREATE TABLE。真正能少往返的是 PostgreSQL 这类
        # 原生实现了批量的方言。
        #
        # 换成它是因为这是 2.0 的正经接口、少三层 try/except，不是因为它快。
        # shop 那次 "Lost connection to MySQL server during query"
        # （run 554a0f92）多半是偶发的服务端超时——所以真正的对策不在这里，
        # 在下面：失败要如实记成失败，让工具和界面都说真话。
        #
        # 批量是一锤子买卖：其中一个对象反射出错，整批都拿不到。SQLite 连接关了双引号
        # 字符串兼容以后，老库里一个写着 `"yes" AS flag` 的视图就让 PRAGMA 报错；以前这里
        # 吞掉异常返回空，好好的表跟着一起消失，探查却显示「成功、0 张表」。所以批量失败
        # 时逐个对象再取一遍，只跳过真正取不到的那个，名字和原因记进 skipped。
        failures: dict[str, str] = {}

        def _multi(fn: Any, single: Any, names: list[str], *,
                   errors: dict[str, str] | None = None, ok: set[str] | None = None) -> dict[Any, Any]:
            """批量反射，失败时逐个对象重试。ok 给了的话，记下真正读到了的对象名：批量成功时是全部
            （没有约束的表批量接口也会给空列表，或者干脆不给），逐个重试时只有没报错的那些。"""
            try:
                got = dict(fn(schema=target_schema, filter_names=names, kind=ObjectKind.ANY))
                if ok is not None:
                    ok.update(names)
                return got
            except NotImplementedError:
                return {}          # 表注释这类不是所有方言都支持
            except Exception:  # noqa: BLE001 - 批量取不到，逐个对象再试
                pass
            out: dict[Any, Any] = {}
            for name in names:
                try:
                    out[(target_schema, name)] = single(name, schema=target_schema)
                    if ok is not None:
                        ok.add(name)
                except NotImplementedError:
                    break
                except Exception as e:  # noqa: BLE001 - 只跳过这一个对象
                    if errors is not None:
                        errors[name] = first_line(e) or describe_exception(e)
                    if not out and len(errors or ()) >= _GIVE_UP_AFTER:
                        break      # 开头几个全取不到，多半是连接本身出了问题，不再挨个耗时间
            return out

        columns_by = _multi(inspector.get_multi_columns, inspector.get_columns, picked,
                            errors=failures)
        readable = [n for n in picked if columns_by.get((target_schema, n))]
        pk_by = _multi(inspector.get_multi_pk_constraint, inspector.get_pk_constraint, readable)
        comment_by = _multi(inspector.get_multi_table_comment, inspector.get_table_comment, readable)
        # 外键和唯一约束给数据目录起草关系用（data/catalog.py）。放在列、主键、注释之后读：PostgreSQL 上一条
        # 语句报错会让整个事务作废，先读的那几样不能被它连累。读不到的表不写这两个键（ok 集合里没有它），
        # 下游据此分得清「读过、没有」和「不知道」——升级前探查的缓存里也没有这两个键。
        fk_ok: set[str] = set()
        uq_ok: set[str] = set()
        ix_ok: set[str] = set()
        fk_by = _multi(inspector.get_multi_foreign_keys, inspector.get_foreign_keys, readable, errors={}, ok=fk_ok)
        uq_by = _multi(inspector.get_multi_unique_constraints, inspector.get_unique_constraints, readable,
                       errors={}, ok=uq_ok)
        ix_by = _multi(inspector.get_multi_indexes, inspector.get_indexes, readable, errors={}, ok=ix_ok)
        try:
            default_schema = inspector.default_schema_name
        except Exception:  # noqa: BLE001 - 只用来判断外键是不是指向别的 schema
            default_schema = None

        tables: dict[str, Any] = {}
        skipped: dict[str, str] = {}
        for name in picked:
            key = (target_schema, name)
            columns = columns_by.get(key)
            if not columns:
                # 这个对象反射不出来，跳过，别毁掉整次探查；但要留下名字和原因
                skipped[name] = failures.get(name) or "读不到列"
                continue
            pk = (pk_by.get(key) or {}).get("constrained_columns") or []
            comment = (comment_by.get(key) or {}).get("text")

            tables[name] = {
                # 写 SQL 要用的全名。没有 schema 前缀时 Oracle 直接 ORA-00942——
                # 只读账号名下没有这些对象，全靠跨 schema 授权
                "qualified": f"{target_schema}.{name}" if target_schema else name,
                "schema": target_schema,
                "columns": [
                    {
                        "name": c["name"],
                        "type": str(c.get("type") or ""),
                        "nullable": bool(c.get("nullable", True)),
                        "comment": c.get("comment") or None,
                    }
                    for c in columns
                ],
                "primary_key": pk,
                "comment": comment,
                "is_view": name in views,
            }
            if name in fk_ok:
                tables[name]["foreign_keys"] = _foreign_keys(fk_by.get(key) or [], target_schema, default_schema)
            if name in uq_ok:
                tables[name]["unique"] = _unique_groups(uq_by.get(key) or [],
                                                        (ix_by.get(key) or []) if name in ix_ok else [], pk)
        payload: dict[str, Any] = {"tables": tables, "truncated": truncated,
                                   "total": len(names) + len(views)}
        if failures and not tables:
            # 一个对象也取不到、而且确实出了错：如实记成失败，不能显示成「已同步、0 张表」
            payload.update(failed=True, error=next(iter(failures.values())))
        elif skipped:
            payload["skipped"] = skipped
        return payload

    try:
        async with engine.connect() as conn:
            payload = await conn.run_sync(_collect)
    except Exception as exc:  # noqa: BLE001
        # schema 名填错是高频事故（大小写、拼写、或者根本不知道该填什么），
        # 直接把驱动异常抛出去等于让人自己去猜。先看看连接本身好不好：
        # 能列出候选说明只是 schema 不对，这种情况给出可选项比报错有用得多。
        #
        # 但**这里回来的是一次失败，不是"还没探查"**。以前两者存成了一模一样
        # 的形状（tables 空 + 一个没人读的 error 字段），于是工具对 agent 说
        # "还没有探查过结构，请在设置页里执行一次"——而用户明明执行过，是超时了。
        # run 554a0f92 里 agent 因此花了 6 次调用、29 秒自己重建 schema。
        # failed 这个标记就是为了让下游分得清。
        candidates = await list_schemas(source)
        if not candidates:
            raise
        # error 原样画在数据源卡片上、也交给 agent 看：驱动的异常类名和文档链接去掉，只留它自己的说明
        payload = {"tables": {}, "truncated": False, "total": 0,
                   "failed": True, "error": first_line(exc) or describe_exception(exc)}

    payload["synced_at"] = datetime.now(timezone.utc).isoformat()
    payload["schema"] = target

    # 一张表都没探到时，别只回一个空壳——多半是数据在别的 schema 里，
    # 把候选列出来，用户才知道下一步该填什么
    if not payload["tables"]:
        payload.setdefault("available_schemas", await list_schemas(source))
    return payload


def summary(source: Any, *, max_tables: int = 40, detail: bool = True) -> str:
    """结构摘要。detail=False 只给对象名，不列字段——库多了要靠它压 token。

    实测一个 53 对象的库：带字段 2715 token，只给名字 956 token（35%）。
    接三五个库的差别就是 15k 和 4.5k，后者才塞得进 system prompt。
    """
    if not detail:
        cache = source.schema_cache or {}
        names = table_names(source)
        if not names:
            return _empty_summary(source, cache)
        head = (
            f"- {source.name}（{source.kind}，{'只读' if source.readonly else '可写'}）："
            f"{source.description or '未填说明'}"
        )
        return head + "\n    对象：" + "、".join(names)
    return _detailed_summary(source, max_tables=max_tables)


def why_empty(cache: dict[str, Any]) -> str:
    """结构为什么是空的。一句话，给 agent 和用户看的是同一句。

    分清"还没探查"和"探查失败了"：前者该去点一下按钮，后者点了也没用，
    要先解决超时或换 schema。以前两者说的是同一句话。
    """
    if not cache.get("failed"):
        return "尚未探查结构"
    err = str(cache.get("error") or "").strip()
    return f"结构探查失败：{err[:200]}" if err else "结构探查失败"


def _empty_summary(source: Any, cache: dict[str, Any]) -> str:
    hint = ""
    candidates = cache.get("available_schemas") or []
    if candidates:
        hint = f"｜该账号名下没有对象，数据可能在这些 schema：{'、'.join(candidates[:8])}"
    return (
        f"- {source.name}（{source.kind}）：{source.description or '未填说明'}"
        f"｜{why_empty(cache)}{hint}"
    )


def _detailed_summary(source: Any, *, max_tables: int = 40) -> str:
    """给 Copilot 的紧凑摘要。没探查过就明说，别让它对着空气编表名。"""
    cache = source.schema_cache or {}
    tables = cache.get("tables") or {}
    if not tables:
        return _empty_summary(source, cache)

    schema = cache.get("schema")
    lines = [
        f"- {source.name}（{source.kind}，{'只读' if source.readonly else '可写'}）："
        f"{source.description or '未填说明'}"
        + (f"｜schema：{schema}" if schema else "")
    ]
    for name, meta in list(tables.items())[:max_tables]:
        cols = meta.get("columns") or []
        preview = "、".join(c["name"] for c in cols[:_PREVIEW_COLUMNS])
        more = f" 等 {len(cols)} 字段" if len(cols) > _PREVIEW_COLUMNS else ""
        note = f"（{meta['comment']}）" if meta.get("comment") else ""
        kind = "视图" if meta.get("is_view") else "表"
        # 用全名：模型照着写 SQL 时少了 schema 前缀在 Oracle 上直接报 ORA-00942
        lines.append(f"    · {meta.get('qualified', name)}[{kind}]{note}: {preview}{more}")
    if len(tables) > max_tables:
        lines.append(f"    · …另有 {len(tables) - max_tables} 个对象，用 db_schema 工具查看")
    return "\n".join(lines)


def find_table(source: Any, table: str) -> dict[str, Any] | None:
    """在缓存里找一张表。全名、只有表名、大小写不一致都认——模型和人都可能这么写。"""
    tables = (source.schema_cache or {}).get("tables") or {}
    meta = tables.get(table)
    if meta is None and "." in table:
        meta = tables.get(table.rsplit(".", 1)[-1])
    if meta is None:
        lowered = table.rsplit(".", 1)[-1].lower()
        meta = next((m for n, m in tables.items() if n.lower() == lowered), None)
    return meta or None


def table_columns(meta: dict[str, Any]) -> list[dict[str, Any]]:
    """结构化的列：{name, type, pk, not_null, comment}。

    界面要画表格，以前只能去解析 describe_table 那段给模型看的文本，文本格式
    一改就全乱。两份出自同一份缓存，说法一致。
    """
    pk = set(meta.get("primary_key") or [])
    return [
        {
            "name": col["name"],
            "type": col.get("type") or "",
            "pk": col["name"] in pk,
            "not_null": not col.get("nullable", True),
            "comment": col.get("comment") or None,
        }
        for col in meta.get("columns") or []
    ]


def describe_table(source: Any, table: str) -> str:
    """单表的完整字段说明。agent 调 db_schema 工具时返回这个。"""
    cache = source.schema_cache or {}
    tables = cache.get("tables") or {}
    # 模型可能传全名（ANALYTICS.V_TRIP_FACT），也可能只传表名，两种都认
    meta = find_table(source, table)
    if not meta:
        if not tables:
            # 缓存是空的，说"里面没有这张表"就是在误导——真相是我们什么都不知道。
            # run 554a0f92 里 agent 一次问了 7 个表名，收到 7 条一模一样的
            # "没有 X。现有的对象：（还没探查过结构）"，白花一整步
            return (
                f"数据源「{source.name}」的结构信息不可用（{why_empty(cache)}），"
                "因此无法判断是否存在这张表。请直接查询数据字典"
                "（information_schema.tables / columns，SQLite 用 sqlite_master）。"
            )
        available = "、".join(
            m.get("qualified", n) for n, m in list(tables.items())[:30]
        )
        return f"数据源「{source.name}」中没有 {table}。现有的对象：{available}"

    head = f"{'视图' if meta.get('is_view') else '表'} {meta.get('qualified', table)}"
    if meta.get("comment"):
        head += f"（{meta['comment']}）"
    lines = [head]
    pk = set(meta.get("primary_key") or [])
    for col in meta.get("columns") or []:
        marks = []
        if col["name"] in pk:
            marks.append("主键")
        if not col.get("nullable", True):
            marks.append("非空")
        if col.get("comment"):
            marks.append(col["comment"])
        suffix = f"  {'、'.join(marks)}" if marks else ""
        lines.append(f"  {col['name']}  {col['type']}{suffix}")
    return "\n".join(lines)


def table_names(source: Any) -> list[str]:
    """带 schema 前缀的对象全名。模型照着这个写 SQL 才不会漏掉前缀。"""
    tables = (source.schema_cache or {}).get("tables") or {}
    return [meta.get("qualified", name) for name, meta in tables.items()]
