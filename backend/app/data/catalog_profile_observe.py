"""取值观察：剖析引擎的四类观察检查（docs/design/next-round/03-数据接入与目录维护.md B2）。

**为什么要有它。** 码值出现新取值、比例剧变、业务日期断档、某天没数据，以前都看不到：确认过的码值列不再取分布，大表只看
物理顺序最老的前 10 万行，没有最新业务日期、按日行数、总行数的观察和历史；「已列全」过期以后 SQL 检查还照它提示。

**四类检查**（和关系、码值、日期检查一样是剖析的检查项：计入每张表的预算、止损，每条查询都留痕）：

- rows：这张表的行数（剖析开头估行数那一条的结果，不另发查询）；口径 stats（数据库统计的估算值）/ count（数到上限）；
- codes_observe：码值列的取值分布——**所有**码值列都观察，包括人工确认过、标了已列全的（目录-D6）；只读，不改含义；
- freshness：业务日期列（已确认或已验证）的最新日期；
- daily_volume：业务日期最近 35 天每天的行数。

**大表的口径**（目录-D7）：小表（行数不超过整表统计的上限）看整表（full）。大表：业务日期已确认或已验证、而且这一列打头
有索引（探查记下的 index_first_columns）时看最近 35 天的窗口（window，走索引的范围扫描）；PostgreSQL / Oracle 没有就块采样
（sample，按数据块随机取约 p%，p 按表的大小取一档，不跨档比）；MySQL / SQLite 没有块采样，只看按存储顺序的前 N 行
（first_rows），照实写口径。最新业务日期、按日行数在大表上只在业务日期列打头有索引时才查，否则记「表太大」跳过。每条观察
都记口径（basis），只和口径相同的历史比（data/value_drift）。

**遮罩**：遮罩列不出现在任何一条观察查询里（被遮罩的码值列、业务日期列记「被遮罩」跳过；窗口要用的业务日期列被遮罩时
不用窗口）。

**落库与比较**（save，和这张表的剖析结果、目录在同一个事务里）：每条观察写进 table_observations，每张表每种观察（码值按
列）留最近 26 条；拿同口径的历史比出取值变化写进 source_changes。机器唯一能写进目录的事：数据证明码值表不再列全时撤销
「已列全」（目录版本 +1，现有的目录漂移、冻结快照、SQL 检查自然跟上）；新取值本身、含义、状态都不改，交给人在变化里处理。

**什么时候观察**：剖析任务的范围 observe（只观察）和 maintenance（需要剖析的表 + 观察到期的表，定时维护用）才带观察检查；
人点的「需要剖析 / 全部 / 指定的表」照旧不观察。观察到期（observe_due）：有数据目录、不是视图的表，从没观察过，或者上一次
观察在 OBSERVE_EVERY_H 小时以前。
"""
from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.data import catalog
from app.db.base import utcnow
from app.db.models_sources import TableObservation

logger = logging.getLogger(__name__)

OBSERVE_KINDS = ("rows", "codes_observe", "freshness", "daily_volume")
#: 按日行数、窗口观察看最近几天
WINDOW_DAYS = 35
#: 码值观察最多取几个取值（多出来的记 truncated，不判新码值、不算比例）
OBSERVE_CODES_MAX = 50
#: 块采样的比例按表的大小取一档：目标约 max_scan_rows 行，向下取到这几档里的一档（同一张表大小不变时口径不变）
SAMPLE_LADDER = (10.0, 5.0, 2.0, 1.0, 0.5, 0.2, 0.1, 0.05, 0.02, 0.01)
#: 上一次观察多久以前算「观察到期」（每天跑一次的定时维护每次都会观察；每小时跑一次的一天观察两次）
OBSERVE_EVERY_H = 12
#: 撤销「已列全」时目录的署名
OBSERVER = "取值观察"

KIND_LABEL = {"rows": "行数", "codes_observe": "码值观察", "freshness": "最新业务日期", "daily_volume": "按日行数"}

TOO_LARGE_DATE = ("{table} 的行数超过整表统计的行数上限，业务日期列 {column} 打头没有索引，不看{what}"
                  "（大表只在业务日期列打头有索引时才查）")
NO_DATE = "{table} 没有已确认或已验证的业务日期，不看{what}"


# ==========================================================================
# 列出检查（纯函数）
# ==========================================================================


def _business_date(meta: Mapping[str, Any], notes: Mapping[str, Any]) -> str | None:
    """已确认或已验证的业务日期列（表结构里的写法）；没有为 None。"""
    from app.data.catalog_profile import _column

    item = notes.get("business_date") if isinstance(notes.get("business_date"), Mapping) else None
    if not item or item.get("status") not in ("confirmed", "verified"):
        return None
    column = str((item.get("value") or {}).get("column") or "")
    return _column(dict(meta), column) if column else None


def code_columns(meta: Mapping[str, Any], notes: Mapping[str, Any]) -> list[str]:
    """要观察的码值列：目录里有码值项（没被驳回）的列，加上 ENUM / SET 列；按表结构的顺序。人工确认过、标了已列全的照样
    观察（目录-D6）。"""
    from app.data.catalog_profile import _is_enum

    described = notes.get("columns") if isinstance(notes.get("columns"), Mapping) else {}
    out: list[str] = []
    for col in meta.get("columns") or []:
        if not isinstance(col, Mapping):
            continue
        name = str(col.get("name") or "")
        items = described.get(name) if isinstance(described.get(name), Mapping) else {}
        codes = items.get("codes") if isinstance(items, Mapping) else None
        listed = isinstance(codes, Mapping) and codes.get("status") != "rejected"
        if listed or (_is_enum(col.get("type"), col.get("enum_values")) and not isinstance(codes, Mapping)):
            out.append(name)
    return out


def plan_observations(table: str, meta: Mapping[str, Any] | None, notes: Mapping[str, Any] | None, *,
                      masked: set[str]) -> list[Any]:
    """这张表要做的观察检查（catalog_profile.PlannedCheck），接在关系、码值、日期检查后面：行数、各码值列、最新业务日期、
    按日行数。被遮罩的列照样列出来、带 skip（不查，让人看得见为什么没有）。视图、表结构里没有的返回空列表。"""
    from app.data.catalog_profile import PlannedCheck

    if not isinstance(meta, Mapping) or meta.get("is_view"):
        return []
    notes = notes or {}
    out = [PlannedCheck("rows", table, None)]
    for column in code_columns(meta, notes):
        skip = (("masked", f"{table}.{column} 在数据源设置中被遮罩，不观察取值", False)
                if column.lower() in masked else None)
        out.append(PlannedCheck("codes_observe", column, f"columns.{column}.codes", skip, column=column))
    day = _business_date(meta, notes)
    for kind, what in (("freshness", "最新业务日期"), ("daily_volume", "按日行数")):
        if day is None:
            continue
        skip = (("masked", f"{table}.{day} 在数据源设置中被遮罩，不看{what}", False) if day.lower() in masked else None)
        out.append(PlannedCheck(kind, day, "business_date", skip, column=day))
    return out


# ==========================================================================
# 执行（在 catalog_profile._TableCx 上发查询：预算、止损、留痕都在那里）
# ==========================================================================


def sample_percent(rows: int | None, target: int) -> float | None:
    """块采样取几档：约 target 行、向下取到 SAMPLE_LADDER 的一档；表不大于 target 时不采样（None）。"""
    if not rows or rows <= target:
        return None
    want = 100.0 * target / rows
    return next((p for p in SAMPLE_LADDER if p <= want), SAMPLE_LADDER[-1])


def _today() -> date:
    return datetime.now(timezone.utc).date()


def _since() -> date:
    return _today() - timedelta(days=WINDOW_DAYS)


def _window_ok(cx: Any, meta: Mapping[str, Any], day: str | None) -> bool:
    """大表能不能看窗口：业务日期列已确认或已验证、打头有索引、没被遮罩。"""
    if not day or cx.is_masked(day):
        return False
    return day.lower() in {str(c).lower() for c in meta.get("index_first_columns") or []}


def _observation(kind: str, column: str, basis: dict[str, Any], data: dict[str, Any], refs: list[int]) -> dict[str, Any]:
    key = basis_key(basis)
    return {"kind": kind, "column": column, "basis": {**basis, "query_refs": refs}, "basis_key": key, "data": data}


def basis_key(basis: Mapping[str, Any]) -> str:
    """口径的规范写法：只和它相同的历史比。"""
    method = basis.get("method")
    if method == "window":
        return f"window:{basis.get('column')}:{basis.get('days')}"
    if method == "sample":
        return f"sample:{basis.get('percent'):g}"
    if method == "first_rows":
        return f"first_rows:{basis.get('rows')}"
    return str(method or "")


def basis_text(basis: Mapping[str, Any]) -> str:
    """口径的说法（跳过、留痕的说明里用）。"""
    method = basis.get("method")
    if method == "window":
        return f"业务日期 {basis.get('column')} 最近 {basis.get('days')} 天"
    if method == "sample":
        return f"块采样约 {basis.get('percent'):g}%"
    if method == "first_rows":
        return f"按存储顺序取前 {basis.get('rows')} 行"
    if method == "stats":
        return "数据库统计的估算值"
    if method == "count":
        return "逐行数出"
    return "整表"


async def execute(cx: Any, table: str, check: Any, result: Any) -> None:
    """执行一项观察检查，观察到的追加进 result.observations。按规则不查的抛 catalog_profile._Skip。"""
    from app.data.catalog_profile import _Skip

    meta = cx.tables[table]
    start = len(result.query_log)
    size = await cx.size_with_data(table, result) if check.kind != "rows" else await cx.size(table, result)
    if check.kind == "rows":
        basis: dict[str, Any] = {"method": size.method}
        data = {"rows": size.rows, "at_least": size.at_least, "method": size.method}
        if size.method == "unknown":
            raise _Skip("no_data", f"{table} 的行数没能得到（统计信息和计数都没查成），不记行数观察")
        result.observations.append(_observation("rows", "", basis, data, []))
        return
    before = len(result.observations)
    if check.kind == "codes_observe":
        await _codes(cx, table, str(check.column), meta, size, result)
    else:
        await _dates(cx, table, check.kind, str(check.column), meta, size, result)
    if len(result.observations) > before:
        # 留痕：这条观察是哪几条查询得出的（这张表 query_log 里的下标）
        result.observations[-1]["basis"]["query_refs"] = list(range(start, len(result.query_log)))


async def _codes(cx: Any, table: str, column: str, meta: Mapping[str, Any], size: Any, result: Any) -> None:
    from app.data import catalog_profile as cp

    if cx.is_masked(column):
        raise cp._Skip("masked", f"{table}.{column} 在数据源设置中被遮罩，不观察取值", carry=False)
    d = cx.dialect
    col_meta = cp._column_meta(dict(meta), column) or {}
    t, c = d.table(dict(meta), table), d.column(dict(meta), column)
    n = OBSERVE_CODES_MAX + 1
    day = _business_date(meta, getattr(cx, "table_notes", None) or {})
    if size.within(cx.settings.max_scan_rows):
        basis: dict[str, Any] = {"method": "full"}
        sql = d.value_counts_sql(t, c, n, scan_cap=None)
    elif _window_ok(cx, meta, day):
        since = _since()
        basis = {"method": "window", "column": day, "days": WINDOW_DAYS, "since": since.isoformat()}
        text = catalog.column_family(cp._column_meta(dict(meta), str(day))) != "time"
        sql = d.window_value_counts_sql(t, c, d.column(dict(meta), str(day)), since, n, text=text)
    else:
        # 数到上限、只知道「至少多少行」的按下限取档（取到的档偏大，多看一点，不会少看）
        percent = sample_percent(size.rows if size.rows is not None else size.at_least,
                                 max(cx.settings.max_scan_rows, cx.settings.sample_size))
        sampled = d.sample_value_counts_sql(t, c, percent, n) if percent else None
        if sampled is not None:
            basis, sql = {"method": "sample", "percent": percent}, sampled
        else:
            cap = cx.scan_cap(size) or max(cx.settings.sample_size, cx.settings.max_scan_rows)
            basis, sql = {"method": "first_rows", "rows": cap}, d.value_counts_sql(t, c, n, scan_cap=cap)
    res = await cx.run(sql, max_rows=n)
    fixed = cp.is_fixed_char(col_meta.get("type"))
    counts: dict[str, int] = {}
    for row in res.rows[:OBSERVE_CODES_MAX]:
        key = cp._code_key(row[0], fixed=fixed)
        if key is None:
            raise cp._Skip("unsupported", f"{table}.{column} 的取值不是整数或短文字，不观察", carry=False)
        counts[key] = counts.get(key, 0) + (cp._as_int(row[1]) or 0)
    data = {"values": [[k, v] for k, v in counts.items()], "rows": sum(counts.values()),
            "truncated": len(res.rows) > OBSERVE_CODES_MAX}
    result.observations.append(_observation("codes_observe", column, basis, data, []))


async def _dates(cx: Any, table: str, kind: str, column: str, meta: Mapping[str, Any], size: Any, result: Any) -> None:
    from app.data import catalog_profile as cp

    what = KIND_LABEL[kind]
    if cx.is_masked(column):
        raise cp._Skip("masked", f"{table}.{column} 在数据源设置中被遮罩，不看{what}", carry=False)
    if not size.within(cx.settings.max_scan_rows) and not _window_ok(cx, meta, column):
        raise cp._Skip("too_large", TOO_LARGE_DATE.format(table=table, column=column, what=what))
    d = cx.dialect
    t, c = d.table(dict(meta), table), d.column(dict(meta), column)
    text = catalog.column_family(cp._column_meta(dict(meta), column)) != "time"
    if kind == "freshness":
        res = await cx.run(d.max_date_sql(t, c, text=text))
        value = res.rows[0][0] if res.rows and res.rows[0] else None
        if value is None or not cp._DATE_TEXT.match(str(value)):
            raise cp._Skip("no_data", f"{table}.{column} 没有可识别的日期值")
        result.observations.append(_observation("freshness", column, {"method": "full"},
                                                {"max": str(value)[:19]}, []))
        return
    since = _since()
    res = await cx.run(d.day_volume_sql(t, c, since, text=text), max_rows=WINDOW_DAYS + 2)
    days: dict[str, int] = {}
    for row in res.rows:
        day = str(row[0])[:10] if row and row[0] is not None else ""
        if cp._ISO_DAY.fullmatch(day):
            days[day] = days.get(day, 0) + (cp._as_int(row[1]) or 0)
    basis = {"method": "window", "column": column, "days": WINDOW_DAYS, "since": since.isoformat()}
    result.observations.append(_observation("daily_volume", column, basis,
                                            {"since": since.isoformat(), "until": _today().isoformat(),
                                             "days": days}, []))


# ==========================================================================
# 落库、比较、撤销「已列全」
# ==========================================================================


async def _history(session: AsyncSession, source_id: str, table: str, kind: str, column: str) -> list[TableObservation]:
    return list((await session.execute(select(TableObservation).where(
        TableObservation.source_id == source_id, TableObservation.table_name == table, TableObservation.kind == kind,
        TableObservation.column_name == column).order_by(TableObservation.at.desc(), TableObservation.id.desc())
        .limit(TableObservation.KEEP))).scalars())


async def _prune(session: AsyncSession, source_id: str, table: str, kind: str, column: str) -> None:
    keep = [r.id for r in await _history(session, source_id, table, kind, column)]
    if keep:
        await session.execute(delete(TableObservation).where(
            TableObservation.source_id == source_id, TableObservation.table_name == table,
            TableObservation.kind == kind, TableObservation.column_name == column,
            TableObservation.id.not_in(keep)).execution_options(synchronize_session=False))


def _rules_columns(notes: Mapping[str, Any], meta: Mapping[str, Any] | None, table: str, kind: str | None) -> set[str]:
    """有效记录条件用到的列（小写）。"""
    from app.data import sqlcheck

    item = notes.get("valid_filter") if isinstance(notes.get("valid_filter"), Mapping) else None
    if not item or item.get("status") == "rejected":
        return set()
    parsed = sqlcheck.parse_valid_filter(item.get("value"), meta, kind=kind, table=table)
    return {c.lower() for c in parsed.columns}


async def _revoke_complete(session: AsyncSession, source_id: str, table: str, column: str) -> int | None:
    """撤销码值项上的「已列全」（目录版本 +1，不提交）：值、含义、状态、来源都不动。撞上别人刚改过重读重来，最多三次。
    已经不是已列全了（别人刚改过）返回 None。"""
    for _attempt in range(3):
        entry = await catalog.read_entry(session, source_id, table)
        if entry is None:
            return None
        notes = entry.notes
        item = ((notes.get("columns") or {}).get(column) or {}).get("codes")
        if not catalog.codes_complete(item):
            return None
        item.pop("complete", None)
        item["updated_at"] = catalog.now_iso()
        try:
            written = await catalog.write_entry(session, source_id, table, notes, if_version=entry.version,
                                                actor=OBSERVER, commit=False)
        except catalog.CatalogConflict:
            continue
        return written.version
    return None


async def save(session: AsyncSession, source: Any, table: str, result: Any, *, job_id: str | None,
               seq: int | None, detected: Any) -> int:
    """这张表这一次的观察写进 table_observations、和同口径的历史比，比出来的取值变化写进 source_changes（不提交：和剖析
    结果、目录在同一个事务里）。返回记了几条新变化。出错只记日志、不挡剖析（观察是辅助步骤）。"""
    from app.data import source_changes as sc
    from app.data import value_drift as vd

    observations = list(getattr(result, "observations", None) or [])
    if not observations:
        return 0
    fresh = 0
    try:
        entry = await catalog.read_entry(session, source.id, table)
        notes = entry.notes if entry else {}
        meta = ((getattr(source, "schema_cache", None) or {}).get("tables") or {}).get(table)
        rules = _rules_columns(notes, meta, table, getattr(source, "kind", None))
        now = utcnow()
        today = now.date()
        for obs in observations:
            kind, column = obs["kind"], obs["column"]
            history = await _history(session, source.id, table, kind, column)
            session.add(TableObservation(source_id=source.id, table_name=table, column_name=column, kind=kind,
                                         basis=obs["basis"], basis_key=obs["basis_key"], data=obs["data"],
                                         job_id=job_id, task_seq=seq, at=now))
            await session.flush()
            await _prune(session, source.id, table, kind, column)
            item = ((notes.get("columns") or {}).get(column) or {}).get("codes") if column else None
            known = list((item or {}).get("value") or {}) if isinstance(item, Mapping) and \
                item.get("status") != "rejected" else []
            current = vd.Obs(kind, obs["basis_key"], obs["data"], now)
            past = [vd.Obs(h.kind, h.basis_key, h.data or {}, h.at) for h in history]
            findings = vd.compare(current, past, today=today, known=known,
                                  complete=catalog.codes_complete(item))
            for f in findings:
                after = dict(f.after) if isinstance(f.after, Mapping) else {"value": f.after}
                if f.revoke_complete:
                    version = await _revoke_complete(session, source.id, table, column)
                    after.update(complete_revoked=version is not None, catalog_version=version)
                touched = bool(column) and column.lower() in rules
                change = sc.NewChange(kind=f.kind, table=table, column=column or None, severity=f.severity,
                                      before=f.before, after=after,
                                      basis={"method": obs["basis"].get("method"), "key": obs["basis_key"],
                                             "query_refs": obs["basis"].get("query_refs") or []},
                                      affects={"catalog": [], "templates": [], "rules_touched": touched},
                                      key=(obs["basis_key"], f.key))
                _row, new = await sc.record(session, source, change, detected)
                fresh += int(new)
    except Exception:  # noqa: BLE001 - 观察是辅助步骤：落库、比较出错不挡剖析结果
        logger.exception("保存 %s 的取值观察时出错", table)
    return fresh


def coverage_drops(findings: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """关系核对的结论里覆盖率掉到有确证的线以下的（以前够、这次不够）：[{path, target, before, after}]。剖析本身照旧按
    规则降回推断（或只给人工确认的补上新的覆盖率），这里只把「覆盖率下降」记成一条取值变化。"""
    out: list[dict[str, Any]] = []
    line = catalog.PROFILE_VERIFY_COVERAGE
    for f in findings:
        if not isinstance(f, Mapping) or f.get("kind") != "relation":
            continue
        before, after = f.get("previous_coverage"), f.get("coverage")
        if isinstance(before, (int, float)) and isinstance(after, (int, float)) and before >= line > after:
            out.append({"path": f.get("path"), "target": f.get("target"), "before": before, "after": after,
                        "confirmed": bool(f.get("confirmed"))})
    return out


async def record_coverage_drops(session: AsyncSession, source: Any, table: str, result: Any, *, detected: Any) -> int:
    """这张表这次剖析里覆盖率掉下来的关系记成 coverage_drop（不提交，和剖析结果同一个事务）。人点的剖析也记：变化记录是
    全站一份。出错只记日志。"""
    from app.data import source_changes as sc

    try:
        fresh = 0
        for drop in coverage_drops(getattr(result, "findings", None) or []):
            _row, new = await sc.record(session, source, sc.NewChange(
                kind=sc.COVERAGE_DROP, table=table, severity="warning",
                before={"coverage": drop["before"], "path": drop["path"], "target": drop["target"]},
                after={"coverage": drop["after"], "confirmed": drop["confirmed"]},
                basis={"method": "relation_sample", "key": "relation"},
                affects={"catalog": [{"table": table, "path": drop["path"], "label": drop["target"],
                                      "status": "confirmed" if drop["confirmed"] else "proposed",
                                      "source": None, "needs_action": False}], "templates": [], "rules_touched": False},
                key=(drop["path"], round(float(drop["after"]), 3))), detected)
            fresh += int(new)
        return fresh
    except Exception:  # noqa: BLE001
        logger.exception("记录 %s 的关联覆盖率下降时出错", table)
        return 0


# ==========================================================================
# 观察到期、给助手的观察结论
# ==========================================================================


async def last_observed(session: AsyncSession, source_id: str,
                        tables: Iterable[str] | None = None) -> dict[str, datetime]:
    """每张表最近一次观察的时刻。"""
    query = select(TableObservation.table_name, func.max(TableObservation.at)).where(
        TableObservation.source_id == source_id).group_by(TableObservation.table_name)
    if tables is not None:
        wanted = list(tables)
        if not wanted:
            return {}
        query = query.where(TableObservation.table_name.in_(wanted))
    return {name: at for name, at in (await session.execute(query)).all() if at is not None}


def observe_reason(last: datetime | None, *, now: datetime | None = None) -> str | None:
    """观察到期没有：never 从没观察过；observe_due 上一次在 OBSERVE_EVERY_H 小时以前；没到期为 None。"""
    if last is None:
        return "never"
    now = now or utcnow()
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return "observe_due" if now - last >= timedelta(hours=OBSERVE_EVERY_H) else None


async def observed_facts(session: AsyncSession, source_id: str,
                         tables: Iterable[str] | None = None) -> dict[str, dict[str, Any]]:
    """给助手看的观察结论（经 schema_view.profile_lines 和剖析事实同一节，next-round X7）：{表名: {freshness: [{column, max,
    at}], rows: {rows, method, at_least, at}}}，每种取最近一次。遮罩在渲染那一刻再核（不在这里滤）。"""
    query = select(TableObservation).where(TableObservation.source_id == source_id,
                                           TableObservation.kind.in_(("freshness", "rows")))
    if tables is not None:
        wanted = list(tables)
        if not wanted:
            return {}
        query = query.where(TableObservation.table_name.in_(wanted))
    out: dict[str, dict[str, Any]] = {}
    seen: set[tuple[str, str, str]] = set()
    for r in (await session.execute(query.order_by(TableObservation.at.desc()))).scalars():
        key = (r.table_name, r.kind, r.column_name)
        if key in seen:
            continue
        seen.add(key)
        facts = out.setdefault(r.table_name, {})
        at = r.at.isoformat() if r.at else None
        if r.kind == "freshness" and (r.data or {}).get("max"):
            facts.setdefault("freshness", []).append({"column": r.column_name, "max": r.data["max"], "at": at})
        elif r.kind == "rows":
            facts["rows"] = {**{k: (r.data or {}).get(k) for k in ("rows", "method", "at_least")}, "at": at}
    return out


__all__ = ["KIND_LABEL", "OBSERVE_CODES_MAX", "OBSERVE_EVERY_H", "OBSERVE_KINDS", "OBSERVER", "SAMPLE_LADDER",
           "WINDOW_DAYS", "basis_key", "basis_text", "code_columns", "coverage_drops", "execute", "last_observed", "observe_reason",
           "observed_facts", "plan_observations", "record_coverage_drops", "sample_percent", "save"]
