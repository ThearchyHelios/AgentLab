"""数据目录的审阅队列、批量审阅、表被哪些运行用过、审阅进度。

**为什么要有它。** 起草（数据库注释、外键、命名推断、数据剖析、模型）一次写出成百上千项，绝大多数停在「推断」：SQL 检查的
错误级规则只认已确认、有确证的项，推断的项只作提示。逐表点开确认太慢，也看不出先确认哪几项。队列把推断的项摊平，按
「这张表常不常用 × 这一项错了影响多大 × 这一项从哪来」排好，最该先看的在前面；勾几项一次确认、驳回或撤销审阅。

- **队列**（review_queue，GET /catalog/review-queue）：只列推断（proposed）的项。priority = 使用次数权重 × 风险权重 ×
  来源权重，从高到低；同分按表名、路径排，结果稳定。
  - 使用次数权重：1 + log2(1 + 使用次数)。使用次数同目录清单（catalog.table_usage，清单的「使用 N 次」和排序、起草的默认
    范围都按它）：按对数增长，常用表在前，但不至于让常用表上的中文名压过所有表上影响数字的项。
  - 风险权重（RISK_WEIGHT）：有效记录条件、去重规则、业务日期、关联关系最高（直接改变数字），业务主键、度量类型次之
    （计数和能不能加总），码值、粒度、表类型再次，中文名、说明、含义、单位最低。
  - 来源权重（SOURCE_WEIGHT）：模型起草、命名推断是猜的，比数据库注释优先；数据剖析（码值候选、业务日期、没核实的关系）
    居中。
  - 两组（kind）：facts 影响数字的项（有效记录条件、去重规则、业务日期、关联关系、业务主键、度量类型）；names 名称和说明
    （中文名、说明、含义、单位、码值、粒度、表类型）。表的「说明」和中文名、含义一样只是说明文字，归 names。
  - 表结构里已经没有的表、列上的项不列（确认了也没有用，目录页上另有清理的提示）。
  - field_label 是这一项给人看的位置：「表的有效记录条件」「列 status 的码值」「指向 members 的关联关系」（item_label，
    和表助手提案、导入冲突里的说法一致）；列级的项另给 column。
  - table_usage 是所在表的使用次数（同清单）。reasons 用中文说为什么排在这里：常用表（被 N 次运行用到）、影响数字的项
    （有效记录条件）、模型起草，未确认……「被 N 次运行用到」数的是运行（同 /usage 的 total）；同一次运行里查了几次、
    使用次数比运行次数多时两个都写（「被 2 次运行用到，使用 5 次」），和清单、运行列表上的数都对得上。
- **批量审阅**（review_batch，POST /catalog/review/batch）：逐表按 if_version 乐观锁，同一张表的几条在一次写入里完成，
  一张表冲突、写不进去不影响别的表；路径不对、表不存在逐条报（results 和请求一一对应）。审阅的规则同单项审阅
  （catalog.review_item）。写进去之后影响数字的项被驳回了的，口径账本在后台标依据了它的答案「依据有变」
  （catalog.after_review → ledger/recall，next-round 02 做法 7 二）。
- **被哪些运行用过**（table_runs，GET /catalog/{table}/usage）：和使用次数数的是同一批查询快照（catalog.query_snapshot_tables），
  但按运行去重，新的在前；total 是运行次数。使用次数按查询计，同一次运行里查了几次这张表，使用次数就比 total 多几。
- **进度**（review_progress，GET /catalog/review-progress）：常用表（使用次数前 TOP_N 张，和起草对话框「使用次数最多的
  20 张表」是同一批：catalog.default_tables 的取法，用到的不够 TOP_N 张时按表结构的顺序补齐）、全部、影响数字的项各自的
  确认情况。项的范围和队列一样：表结构里没有的表、列不算。
"""
from __future__ import annotations

import math
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.data import catalog, source_objects
from app.db.models import Run

#: 队列的筛选：全部、影响数字的项、名称和说明
KINDS = ("all", "facts", "names")
#: 队列一页最多几项；批量审阅一次最多几条；「被哪些运行用过」一次最多几次运行
QUEUE_LIMIT_MAX = 200
BATCH_MAX = 200
RUNS_LIMIT_MAX = 100
#: 进度里的「常用表」取使用次数前几张（同起草的默认范围 catalog.DRAFT_DEFAULT_TABLES）
TOP_N = 20

#: 每一项属于哪一组、风险几档：键是 (t, 字段) / (c, 字段) / (r, "")，档 4 最高
FIELD_GROUP: dict[tuple[str, str], tuple[str, int]] = {
    ("t", "valid_filter"): ("facts", 4), ("t", "dedup"): ("facts", 4), ("t", "business_date"): ("facts", 4),
    ("r", ""): ("facts", 4),
    ("t", "keys"): ("facts", 3), ("c", "measure"): ("facts", 3),
    ("c", "codes"): ("names", 2), ("t", "grain"): ("names", 2), ("t", "kind"): ("names", 2),
    ("t", "label"): ("names", 1), ("t", "description"): ("names", 1), ("c", "label"): ("names", 1),
    ("c", "meaning"): ("names", 1), ("c", "unit"): ("names", 1),
}
RISK_WEIGHT = {4: 4.0, 3: 3.0, 2: 2.0, 1: 1.0}
#: 来源权重：没写的（外键约束、人工填写，它们本来就不是推断）按 1
SOURCE_WEIGHT = {"llm": 1.5, "name": 1.5, "profile": 1.25, "comment": 1.0}

#: 来源的界面叫法（和 frontend/src/lib/terms.ts 的 CATALOG_SOURCE_LABEL 一致，有测试核对）
SOURCE_LABEL = {"comment": "数据库注释", "fk": "外键约束", "name": "命名推断", "profile": "数据剖析", "llm": "模型起草",
                "human": "人工填写"}
RELATION_LABEL = "关联关系"
#: reasons 的说法：常用表（被几次运行用到）；使用次数比运行次数多（同一次运行里查了几次）时两个都写
REASON_TOP = "常用表（{used}）"
USED_RUNS = "被 {runs} 次运行用到"
USED_RUNS_AND_QUERIES = "被 {runs} 次运行用到，使用 {n} 次"
REASON_RISK = {4: "影响数字的项（{label}）", 3: "影响计数和汇总的项（{label}）", 2: "帮助理解取值的项（{label}）",
               1: "名称和说明（{label}）"}
REASON_PENDING_CODES = "{n} 个码值的含义待填写"
REASON_SOURCE = "{source}，未确认"
#: 码值含义为空（数据剖析写进来的候选）时的说法，同界面的 CODES_TEXT.pending
CODE_PENDING = "含义待填写"

#: 批量审阅逐条结果的机读码
CODE_CONFLICT = "catalog_conflict"
CODE_PATH = "catalog_path_invalid"
CODE_INVALID = "catalog_invalid"
CODE_TABLE_MISSING = "table_missing"

Slot = catalog.Slot


# ==========================================================================
# 一项的路径、叫法、显示
# ==========================================================================


def slot_path(key: Slot) -> str:
    """槽位 → 审阅接口的路径写法（catalog.parse_path 的反过来）。"""
    if key[0] == "t":
        return key[1]
    if key[0] == "c":
        return f"columns.{key[1]}.{key[2]}"
    return f"relations.{key[1]}"


def field_of(key: Slot) -> tuple[str, str]:
    """槽位 → FIELD_GROUP 的键。"""
    if key[0] == "r":
        return ("r", "")
    return (key[0], key[-1])


def field_name(key: Slot) -> str:
    """这一项的字段名：表级、列级的项是字段名，关联关系是 relation。"""
    return "relation" if key[0] == "r" else key[-1]


def field_text(key: Slot) -> str:
    """字段的界面叫法（不带列名）：中文名、码值、有效记录条件、关联关系……"""
    if key[0] == "t":
        return catalog.TABLE_FIELD_LABEL.get(key[1], key[1])
    if key[0] == "c":
        return catalog.COLUMN_FIELD_LABEL.get(key[2], key[2])
    return RELATION_LABEL


def item_label(key: Slot, item: Mapping[str, Any] | None = None) -> str:
    """一项给人看的位置：「表的有效记录条件」「列 status 的码值」「指向 members 的关联关系」（不知道指向哪张表时写编号）。"""
    if key[0] == "r" and isinstance(item, Mapping) and item.get("to_table"):
        return f"指向 {item['to_table']} 的{RELATION_LABEL}"
    return catalog.describe_slot(key)


def item_value(key: Slot, item: Mapping[str, Any]) -> Any:
    """一项的值：关联关系是两端、基数和覆盖率，别的是 value。"""
    if key[0] == "r":
        return {k: item.get(k) for k in ("columns", "to_table", "to_columns", "cardinality", "coverage")}
    return item.get("value")


def _percent(ratio: float) -> str:
    """覆盖率写成百分数：整数不带小数，其余留一位；不到 100% 的不写成 100%。"""
    text = f"{ratio * 100:.1f}".rstrip("0").rstrip(".")
    return "99.9%" if text == "100" and ratio < 1 else f"{text}%"


def relation_text(rel: Mapping[str, Any]) -> str:
    """关联关系写成一行：member_id → members.id（多对一，覆盖率 98%）。"""
    left = "、".join(str(c) for c in rel.get("columns") or [])
    right = "、".join(f"{rel.get('to_table')}.{c}" for c in rel.get("to_columns") or [])
    extra = []
    if rel.get("cardinality") in catalog.CARDINALITY_LABEL:
        extra.append(catalog.CARDINALITY_LABEL[rel["cardinality"]])
    coverage = rel.get("coverage")
    if isinstance(coverage, (int, float)) and not isinstance(coverage, bool):
        extra.append(f"覆盖率 {_percent(float(coverage))}")
    return f"{left} → {right}" + (f"（{'，'.join(extra)}）" if extra else "")


def display(key: Slot, value: Any) -> str:
    """一项的值写成一行字，用界面上的说法（明细表、可累加、多对一……）。"""
    if key[0] == "r":
        return relation_text(value if isinstance(value, Mapping) else {})
    field_ = key[-1]
    if field_ == "kind":
        return catalog.UI_KIND_LABEL.get(value, str(value))
    if field_ == "measure":
        return catalog.UI_MEASURE_LABEL.get(value, str(value))
    if field_ == "keys" and isinstance(value, list):
        return "、".join(str(v) for v in value)
    if field_ == "codes" and isinstance(value, Mapping):
        return "、".join(f"{k}={v if str(v).strip() else CODE_PENDING}" for k, v in value.items())
    if field_ == "business_date" and isinstance(value, Mapping):
        parts = [str(value.get("column") or "")]
        if value.get("rule"):
            parts.append(f"规则：{value['rule']}")
        if value.get("timezone"):
            parts.append(f"时区：{value['timezone']}")
        return "；".join(parts)
    return value if isinstance(value, str) else str(value)


# ==========================================================================
# 排序
# ==========================================================================


def usage_weight(usage: int) -> float:
    return 1.0 + math.log2(1 + max(0, int(usage or 0)))


def priority(usage: int, key: Slot, source: Any) -> float:
    """一项的优先级：使用次数权重 × 风险权重 × 来源权重（不取整；排序和交给界面时取四位小数）。"""
    _, tier = FIELD_GROUP[field_of(key)]
    return usage_weight(usage) * RISK_WEIGHT[tier] * SOURCE_WEIGHT.get(str(source), 1.0)


def top_tables(tables: Mapping[str, Any], usage: Mapping[str, int]) -> list[str]:
    """使用次数前 TOP_N 张表（同 catalog.default_tables，起草对话框的「使用次数最多的 20 张表」）：使用次数从多到少，
    一样多的（包括都没用过的）按表结构里的顺序。表结构里不到 TOP_N 张时就是全部。"""
    order = {name: i for i, name in enumerate(tables)}
    return sorted(tables, key=lambda n: (-usage.get(n, 0), order[n]))[:TOP_N]


@dataclass(frozen=True)
class Usage:
    """每张表用得多不多（没被用过的表不出现）。

    queries：使用次数，同 catalog.table_usage（目录清单的「使用 N 次」、清单的排序、起草的默认范围）；
    runs：用到它的运行有几次，同「被哪些运行用过」（table_runs）的 total。同一次运行里查了几次这张表，queries 比 runs 多。
    """

    queries: dict[str, int]
    runs: dict[str, int]


async def table_usage_counts(session: AsyncSession, source: Any) -> Usage:
    """一次读出每张表的使用次数和运行次数（同一份查询快照引用：catalog.query_snapshot_tables）。"""
    queries: Counter[str] = Counter()
    runs: dict[str, set[str]] = {}
    for run, hit in await catalog.query_snapshot_tables(session, source):
        queries.update(hit)
        for name in hit:
            runs.setdefault(name, set()).add(run)
    return Usage(dict(queries), {name: len(ids) for name, ids in runs.items()})


def usage_text(queries: int, runs: int) -> str:
    """「被 2 次运行用到」；使用次数比运行次数多时「被 2 次运行用到，使用 5 次」。"""
    if queries > runs:
        return USED_RUNS_AND_QUERIES.format(runs=f"{runs:,}", n=f"{queries:,}")
    return USED_RUNS.format(runs=f"{runs:,}")


def _reasons(key: Slot, item: Mapping[str, Any], usage: int, runs: int, top: bool) -> list[str]:
    out: list[str] = []
    if usage > 0:
        used = usage_text(usage, runs)
        out.append(REASON_TOP.format(used=used) if top else used)
    _, tier = FIELD_GROUP[field_of(key)]
    out.append(REASON_RISK[tier].format(label=field_text(key)))
    if key[0] == "c" and key[2] == "codes" and isinstance(item.get("value"), Mapping):
        pending = sum(1 for v in item["value"].values() if not str(v).strip())
        if pending:
            out.append(REASON_PENDING_CODES.format(n=pending))
    source = str(item.get("source") or "")
    out.append(REASON_SOURCE.format(source=SOURCE_LABEL.get(source, source)))
    return out


def live_slots(notes: Mapping[str, Any] | None, meta: Mapping[str, Any] | None) -> list[tuple[Slot, dict[str, Any]]]:
    """一张表目录里还算数的项：表结构里已经没有的列上的项不算（不知道表结构时都算）。"""
    columns = catalog._column_names(meta)
    return [(key, item) for key, item in catalog._slots(notes).items()
            if not (key[0] == "c" and columns is not None and key[1] not in columns)]


# ==========================================================================
# 队列
# ==========================================================================


async def review_queue(session: AsyncSession, source: Any, *, kind: str = "all", table: str | None = None,
                       limit: int = 50, offset: int = 0) -> dict[str, Any]:
    """审阅队列：{total, items}。source 是读表结构用的源（上传源是绑定当前快照的视图）。"""
    cache = getattr(source, "schema_cache", None) or {}
    tables = cache.get("tables") or {}
    wanted = None
    if table:
        wanted = catalog.resolve_table_name(cache, table) or table
    entries = await catalog.read_catalog(session, source.id)
    usage = await table_usage_counts(session, source)
    top = set(top_tables(tables, usage.queries))
    items: list[dict[str, Any]] = []
    for name, entry in entries.items():
        meta = tables.get(name)
        if meta is None or (wanted is not None and name != wanted):
            continue
        used = usage.queries.get(name, 0)
        for key, item in live_slots(entry.notes, meta):
            if item.get("status") != "proposed":
                continue
            group, _ = FIELD_GROUP[field_of(key)]
            if kind != "all" and group != kind:
                continue
            value = item_value(key, item)
            items.append({
                "table_name": name,
                "path": slot_path(key),
                "field": field_name(key),
                "field_label": item_label(key, item),
                # 列级的项是哪一列（表级的项、关联关系为 null）
                "column": key[1] if key[0] == "c" else None,
                "value": value,
                "display": display(key, value),
                "source": item.get("source"),
                "status": item.get("status"),
                "note": item.get("note") if isinstance(item.get("note"), str) and item.get("note") else None,
                "priority": round(priority(used, key, item.get("source")), 4),
                "reasons": _reasons(key, item, used, usage.runs.get(name, 0), name in top),
                "table_usage": used,
                "version": entry.version,
            })
    items.sort(key=lambda i: (-i["priority"], i["table_name"], i["path"]))
    return {"total": len(items), "items": items[offset:offset + limit]}


# ==========================================================================
# 批量审阅
# ==========================================================================


@dataclass(frozen=True)
class BatchItem:
    table_name: str
    path: str
    action: str
    if_version: int


def _result(item: BatchItem, *, ok: bool, version: int | None = None, error: str | None = None,
            code: str | None = None) -> dict[str, Any]:
    return {"table_name": item.table_name, "path": item.path, "ok": ok, "version": version if ok else None,
            "error": error, "code": code}


async def review_batch(session: AsyncSession, source: Any, items: Iterable[BatchItem], *,
                       actor: str | None) -> list[dict[str, Any]]:
    """批量审阅，返回逐条结果（和请求一一对应）。

    表名按表结构认（全名、大小写不一致都认）；表结构里没有、但还留着目录的也认（同单项审阅）。同一张表的几条按请求里的
    顺序在当前目录上依次审阅，一次写入：版本不符（if_version 和库里的不一样）、路径不对的那几条不做，其余照做；写入时撞上
    别人刚改过，这张表的几条都记冲突。"""
    items = list(items)
    cache = getattr(source, "schema_cache", None) or {}
    results: list[dict[str, Any] | None] = [None] * len(items)
    groups: dict[str, list[int]] = {}
    known: dict[str, str | None] = {}
    for i, item in enumerate(items):
        if item.table_name not in known:
            key = catalog.resolve_table_name(cache, item.table_name)
            if key is None and await catalog.read_entry(session, source.id, item.table_name) is not None:
                key = item.table_name
            known[item.table_name] = key
        key = known[item.table_name]
        if key is None:
            results[i] = _result(item, ok=False, code=CODE_TABLE_MISSING,
                                 error=await source_objects.absent_message(session, source, item.table_name))
            continue
        groups.setdefault(key, []).append(i)
    at = catalog.now_iso()
    for key, indexes in groups.items():
        entry = await catalog.read_entry(session, source.id, key)
        current = entry.version if entry else 0
        notes: dict[str, Any] = entry.notes if entry else {}
        applied: list[int] = []
        for i in indexes:
            item = items[i]
            if item.if_version != current:
                results[i] = _result(item, ok=False, code=CODE_CONFLICT, error=str(catalog.CatalogConflict(key, current)))
                continue
            try:
                notes = catalog.review_item(notes, item.path, item.action, at=at)
            except catalog.CatalogPathError as e:
                results[i] = _result(item, ok=False, code=CODE_PATH, error=str(e))
                continue
            applied.append(i)
        if not applied:
            continue
        try:
            written = await catalog.write_entry(session, source.id, key, notes, if_version=current, actor=actor)
        except catalog.CatalogConflict as e:
            for i in applied:
                results[i] = _result(items[i], ok=False, code=CODE_CONFLICT, error=str(e))
            continue
        except catalog.CatalogInvalid as e:
            for i in applied:
                results[i] = _result(items[i], ok=False, code=CODE_INVALID, error=str(e))
            continue
        for i in applied:
            results[i] = _result(items[i], ok=True, version=written.version)
        # 驳回了影响数字的项：口径账本在后台标依据了它的答案「依据有变」（catalog.after_review，同单项审阅）
        catalog.after_review(source.id, key, entry.notes if entry else None, written)
    return [r for r in results if r is not None]


# ==========================================================================
# 被哪些运行用过
# ==========================================================================


async def table_runs(session: AsyncSession, source: Any, table: str, *, limit: int = 20) -> dict[str, Any]:
    """用过这张表的运行：{total, runs}，新的在前（按开始时间，没开始的按建立时间）。数的是和使用次数同一批查询快照
    （catalog.query_snapshot_tables），但按运行去重：total 是运行次数，使用次数按查询计、可能比它大。"""
    run_ids = {run for run, hit in await catalog.query_snapshot_tables(session, source) if table in hit}
    if not run_ids:
        return {"total": 0, "runs": []}
    started = func.coalesce(Run.started_at, Run.created_at)
    rows = (await session.execute(
        select(Run.id, Run.workflow_id, Run.workflow_name, Run.run_class, Run.status, Run.started_at)
        .where(Run.id.in_(run_ids)).order_by(started.desc(), Run.id).limit(limit)
    )).all()
    return {
        "total": len(run_ids),
        "runs": [{"run_id": r.id, "workflow_id": r.workflow_id, "workflow_name": r.workflow_name or "",
                  "run_class": r.run_class, "status": r.status,
                  "started_at": r.started_at.isoformat() if r.started_at else None} for r in rows],
    }


# ==========================================================================
# 进度
# ==========================================================================


async def review_progress(session: AsyncSession, source: Any) -> dict[str, Any]:
    """审阅进度：常用 TOP_N 张表（top_tables）、全部、影响数字的项，项按状态计数。全部的 items_total 里还有有确证的项
    （不单列）。"""
    tables = (getattr(source, "schema_cache", None) or {}).get("tables") or {}
    entries = await catalog.read_catalog(session, source.id)
    usage = await catalog.table_usage(session, source)
    top = set(top_tables(tables, usage))
    every: Counter[str] = Counter()
    head: Counter[str] = Counter()
    facts: Counter[str] = Counter()
    for name, entry in entries.items():
        meta = tables.get(name)
        if meta is None:
            continue
        for key, item in live_slots(entry.notes, meta):
            status = str(item.get("status"))
            every[status] += 1
            if name in top:
                head[status] += 1
            if FIELD_GROUP[field_of(key)][0] == "facts":
                facts[status] += 1
    return {
        "top_n": TOP_N,
        "top": {"tables": len(top), "items_total": sum(head.values()), "items_confirmed": head["confirmed"]},
        "all": {"items_total": sum(every.values()), "items_confirmed": every["confirmed"],
                "items_proposed": every["proposed"], "items_rejected": every["rejected"]},
        "facts": {"proposed": facts["proposed"], "confirmed": facts["confirmed"]},
    }


__all__ = ["BATCH_MAX", "BatchItem", "CODE_CONFLICT", "CODE_INVALID", "CODE_PATH", "CODE_TABLE_MISSING",
           "FIELD_GROUP", "KINDS", "QUEUE_LIMIT_MAX", "RISK_WEIGHT", "RUNS_LIMIT_MAX", "SOURCE_LABEL", "SOURCE_WEIGHT",
           "TOP_N", "Usage", "display", "field_name", "field_of", "field_text", "item_label", "item_value",
           "live_slots", "priority", "relation_text", "review_batch", "review_progress", "review_queue", "slot_path",
           "table_runs", "table_usage_counts", "top_tables", "usage_text", "usage_weight"]
