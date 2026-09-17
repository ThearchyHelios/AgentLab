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

from app.data.engine import engines

# 一次探查最多带回多少张表。生产库动辄上千张，全拉回来既慢又没人看得完。
_MAX_TABLES = 200
# summary 里每张表列几个代表字段。够 Copilot 判断"这表是不是我要的"即可。
_PREVIEW_COLUMNS = 6


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


async def list_schemas(source: Any) -> list[str]:
    """列出可访问的非系统 schema。

    企业环境里只读账号名下往往一张表都没有——数据在别的 schema，靠跨库授权或
    synonym 访问。直接返回"0 张表"等于把人晾在那儿，得告诉他数据可能在哪。
    """
    engine = await engines.get(source)

    def _collect(sync_conn: Any) -> list[str]:
        try:
            names = sa_inspect(sync_conn).get_schema_names()
        except Exception:  # noqa: BLE001 - 有些方言不支持
            return []
        return [n for n in names if n.lower() not in _SYSTEM_SCHEMAS]

    async with engine.connect() as conn:
        return await conn.run_sync(_collect)


def _target_schema(source: Any, explicit: str | None) -> str | None:
    """探查哪个 schema：显式参数 > 数据源配置 > 当前用户默认。"""
    if explicit:
        return explicit
    configured = (source.options or {}).get("schema")
    return str(configured) if configured else None


async def introspect(source: Any, *, schema: str | None = None) -> dict[str, Any]:
    """探查整个库的结构，结果直接存进 DataSource.schema_cache。"""
    engine = await engines.get(source)
    target = _target_schema(source, schema)

    def _collect(sync_conn: Any) -> dict[str, Any]:
        inspector = sa_inspect(sync_conn)
        target_schema = target
        names = inspector.get_table_names(schema=target_schema)
        views = inspector.get_view_names(schema=target_schema)
        truncated = len(names) + len(views) > _MAX_TABLES
        picked = (names + views)[:_MAX_TABLES]

        tables: dict[str, Any] = {}
        for name in picked:
            try:
                columns = inspector.get_columns(name, schema=target_schema)
            except Exception:  # noqa: BLE001 - 单表失败不该让整次探查失败
                continue
            try:
                pk = (inspector.get_pk_constraint(name, schema=target_schema) or {}).get(
                    "constrained_columns"
                ) or []
            except Exception:  # noqa: BLE001
                pk = []
            try:
                comment = (inspector.get_table_comment(name, schema=target_schema) or {}).get("text")
            except Exception:  # noqa: BLE001 - 不是所有方言都支持表注释
                comment = None

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
        return {"tables": tables, "truncated": truncated, "total": len(names) + len(views)}

    async with engine.connect() as conn:
        payload = await conn.run_sync(_collect)

    payload["synced_at"] = datetime.now(timezone.utc).isoformat()
    payload["schema"] = target

    # 一张表都没探到时，别只回一个空壳——多半是数据在别的 schema 里，
    # 把候选列出来，用户才知道下一步该填什么
    if not payload["tables"]:
        payload["available_schemas"] = await list_schemas(source)
    return payload


def summary(source: Any, *, max_tables: int = 40) -> str:
    """给 Copilot 的紧凑摘要。没探查过就明说，别让它对着空气编表名。"""
    cache = source.schema_cache or {}
    tables = cache.get("tables") or {}
    if not tables:
        hint = ""
        candidates = cache.get("available_schemas") or []
        if candidates:
            hint = f"｜该账号名下没有对象，数据可能在这些 schema：{'、'.join(candidates[:8])}"
        return (
            f"- {source.name}（{source.kind}）：{source.description or '未填说明'}"
            f"｜尚未探查结构{hint}"
        )

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


def describe_table(source: Any, table: str) -> str:
    """单表的完整字段说明。agent 调 db_schema 工具时返回这个。"""
    cache = source.schema_cache or {}
    tables = cache.get("tables") or {}
    # 模型可能传全名（OWODS.VI_IKE_MARC），也可能只传表名，两种都认
    meta = tables.get(table)
    if meta is None and "." in table:
        meta = tables.get(table.rsplit(".", 1)[-1])
    if meta is None:
        lowered = table.rsplit(".", 1)[-1].lower()
        meta = next((m for n, m in tables.items() if n.lower() == lowered), None)
    if not meta:
        available = "、".join(
            m.get("qualified", n) for n, m in list(tables.items())[:30]
        ) or "（还没探查过结构）"
        return f"数据源「{source.name}」里没有 {table}。现有的对象：{available}"

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
