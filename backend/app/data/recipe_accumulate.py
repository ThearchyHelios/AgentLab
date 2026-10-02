"""按期累积（期 3，WP-4）：配方演进的分档、累积计划、并集的物化（P3-SPEC 2.4–2.7）。

**为什么物化，不用视图。** 对外的表是各期最新分区的并集。视图（final.md R1 的否定结论）没法以 immutable 只读打开、
没法按文件哈希核对，运行固定的版本也就说不清查的是哪一份数据。所以并集是一个真正的 STRICT + PRIMARY KEY 的
SQLite 文件，和期 1、期 2 的快照库一样内容寻址、只读、按哈希核对；它也登记成一个构建（table_versions）。

**为什么在试运行里物化。** 用户启用前看到的行数、结构核对和说明，就是启用后模型看到的那一份（H2）；提交在版本
存储锁里只做文件 link 和写记录，不在锁里花几秒物化。所以这里的函数都是同步的，调用方放进线程池并占 parse_slot。

**确定性。** 写入顺序固定（期按统计期排序，表按列序，行按 rowid），同样的输入物化两次库文件哈希相同。publish_snapshot
复活已回收的快照时靠这一条：重新物化出的文件哈希必须等于登记值。

这里不碰数据库记录、不碰数据源：PartInfo 由 WP-5 组装（统计期取自各期导入清单），计划和报告原样交回给它。
"""
from __future__ import annotations

import datetime as _dt
import os
import secrets
import sqlite3
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote

from app.data import raw_store
from app.data.engine import open_checked_sqlite
from app.data.names import collide_key
from app.data.recipe_confirm import table_changes
from app.data.recipe_parsers import hour_range, hour_range_total, match_key, text_label
from app.data.recipe_types import (
    UNION_VER, AccumulatePlan, ChangeClass, CheckResult, ColumnOut, CrosstabBlock, DerivedSegment,
    DimensionSegment, PartInfo, Recipe, UnionPart, UnionReport, accumulate_blockers, derive_tables,
)
from app.data.table_versions import settle_journal, sha_json

#: table_changes 报出这些 kind 就是不兼容：单位、类型、常量取值只要和任何一期不同，同一列里就混了两种口径
#: （P3-SPEC 2.5）。table_removed / column_removed 不在其中：按「当前版本的表结构」另判退役；
#: placeholder_meaning 属于兼容（占位符本来就存空值，只是说明的写法变了）
INCOMPATIBLE_KINDS = ("type", "unit", "grain", "table_kind", "const_value", "source", "store")


class UnionError(ValueError):
    """物化失败。code：part_missing（某一期的构建库不在了）、part_tampered（某一期的库哈希对不上，物化第 0 步）、
    union_failed（目标表结构推不出来、某一期的 rowid 不连续、某一期有并集里没有位置的表或列；U1–U3 不通过时
    materialize_union 不抛它、只返回 ok=False 的报告，要异常的调用方用 require_union_ok）。
    part 是出问题的那一期的序号（从 1 起，按统计期排序），与哪一期无关时为 None。message 给人看。"""

    def __init__(self, message: str, *, code: str, part: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.part = part


# ==========================================================================
# 小工具
# ==========================================================================


def _q(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def _period_text(start: str | None, end: str | None) -> str:
    return f"{start} 至 {end}" if start and end else "统计期未记录"


def _period_key(start: str | None, end: str | None) -> str:
    """快照清单、报告里一期的写法：「2026-08-01~2026-08-31」。"""
    return f"{start}~{end}"


def _day(text: str) -> _dt.date:
    return _dt.date.fromisoformat(str(text))


def _intersects(a: tuple[str | None, str | None], b: tuple[str | None, str | None]) -> bool:
    """两个统计期有没有交集（ISO 日期按字符串比较即可）。统计期不全的不算相交：替换模式下的旧一期可能没有统计期。"""
    if not (a[0] and a[1] and b[0] and b[1]):
        return False
    return not (a[1] < b[0] or b[1] < a[0])


def _gaps(periods: list[tuple[str, str]]) -> list[dict[str, str]]:
    """相邻两期之间的空缺：前一期 end 加一天 < 后一期 start。periods 已按统计期排序。"""
    out: list[dict[str, str]] = []
    for (_s1, e1), (s2, _e2) in zip(periods, periods[1:]):
        first, last = _day(e1) + _dt.timedelta(days=1), _day(s2) - _dt.timedelta(days=1)
        if first <= last:
            out.append({"start": first.isoformat(), "end": last.isoformat()})
    return out


def _dump(recipe: Recipe) -> dict[str, Any]:
    return recipe.model_dump(mode="json")


def overlap_message(period: dict[str, Any], overlaps: Iterable[dict[str, Any]]) -> str:
    """period_overlap 问题的消息（P3-SPEC 2.3）。WP-5 组装试运行问题时用，免得两边各写一套文字。"""
    others = "、".join(_period_text(o.get("start"), o.get("end")) for o in overlaps)
    return (f"本期统计期 {_period_text(period.get('start'), period.get('end'))} 与已有的 {others} 部分重叠：按期累积要求"
            "各期互不重叠。请检查文件的统计期；如需重新开始累积，可以在数据源卡片的「版本」中启用更早的版本，"
            "或在配方中改为每期替换")


# ==========================================================================
# 配方演进：兼容、退役、不兼容（P3-SPEC 2.5）
# ==========================================================================


def _tables_of_parts(parts: list[PartInfo]) -> dict[str, list[ColumnOut]]:
    """没有当前版本的实际表结构时，退回各期配方推出的表结构的并集（按首次出现的顺序）。"""
    out: dict[str, list[ColumnOut]] = {}
    for part in parts:
        if part.recipe is None:
            continue
        tables, _ = derive_tables(part.recipe)
        for name, cols in tables.items():
            have = out.setdefault(name, [])
            known = {c.name for c in have}
            have.extend(c for c in cols if c.name not in known)
    return out


def _label_canon(seg: Any, label: str) -> str:
    """维度、派生分段的期望标签 → 解析器的规范写法（执行器存进库里的就是它）。解析不了的退回 match_key。"""
    if isinstance(seg, DerivedSegment):
        parsed = hour_range_total(label)
        return parsed.canonical if parsed is not None else match_key(label)
    if seg.dim.parser == "hour_range":
        parsed = hour_range(label)
        return parsed.canonical if parsed is not None else match_key(label)
    return text_label(label) or match_key(label)


def _dim_segments(recipe: Recipe) -> list[tuple[Any, str, str]]:
    """配方里写进表的维度、派生分段：(分段, 表, 维度列)。只核对不另存的派生分段不进表，不算。"""
    out: list[tuple[Any, str, str]] = []
    for sheet in recipe.sheets:
        for block in sheet.blocks:
            if not isinstance(block, CrosstabBlock):
                continue
            for seg in block.segments:
                if isinstance(seg, DimensionSegment):
                    out.append((seg, seg.table, seg.dim.name))
                elif isinstance(seg, DerivedSegment) and seg.keep_as is not None:
                    out.append((seg, seg.keep_as.table, seg.keep_as.dim))
    return out


def _label_sets(parts: list[PartInfo]) -> list[dict[str, Any]]:
    """各期维度取值的差异（评审一-M2）：按分段 id 比较各期配方里 dimension、derived 段的期望标签（规范写法，
    按 match_key 比）。只在含这个分段的各期之间比；集合都相同就不列。

    missing 是相对全部各期并集少了哪些，extra 是比全部各期都有的（交集）多了哪些：D04 去掉「7-8」以后，
    9 月的 missing 是「7-8」，8 月的 extra 是「7-8」。集合不同不算不兼容（table_changes 为空），但跨期比较会
    悄悄变得不可比，所以记进计划和快照清单，说明加 dim_values_vary。
    """
    seen: dict[str, dict[str, Any]] = {}
    for part in parts:
        if part.recipe is None:
            continue
        for seg, table, column in _dim_segments(part.recipe):
            entry = seen.setdefault(seg.id, {"table": table, "column": column, "periods": []})
            labels: dict[str, str] = {}
            for label in seg.labels.expect:
                canon = _label_canon(seg, label)
                labels.setdefault(match_key(canon), canon)
            entry["periods"].append((part, labels))
    out: list[dict[str, Any]] = []
    for sid, entry in seen.items():
        periods = entry["periods"]
        if len(periods) < 2:
            continue
        order: dict[str, str] = {}
        for _part, labels in periods:
            for key, canon in labels.items():
                order.setdefault(key, canon)
        common = set.intersection(*(set(labels) for _part, labels in periods))
        if all(set(labels) == set(order) for _part, labels in periods):
            continue
        out.append({
            "table": entry["table"], "column": entry["column"], "segment": sid,
            "periods": [{
                "start": part.start, "end": part.end,
                "missing": [canon for key, canon in order.items() if key not in labels],
                "extra": [canon for key, canon in order.items() if key in labels and key not in common],
            } for part, labels in periods],
        })
    return out


def classify_changes(parts: list[PartInfo], target: Recipe, *,
                     current_tables: dict[str, list[ColumnOut]] | None,
                     retired_status: dict[str, dict[str, dict]] | None = None) -> ChangeClass:
    """目标配方相对结果快照各期、当前版本的变化分三档（P3-SPEC 2.5，评审二-M3 后改）。

    1. **不兼容**：拿目标配方和 parts 里**每一期的配方**分别调 table_changes，出现 INCOMPATIBLE_KINDS 就是不兼容。
       逐期比是因为单位、类型、常量取值只要和任何一期不同，同一列里就混了两种口径。
    2. **新增和退役**：拿目标配方的表结构和**当前版本实际的表结构**（current_tables，含早先已退役、仍保留在并集
       里的列；为 None 时退回各期配方推出的表结构的并集）比。当前有、目标没有的是退役，其中 retired_status
       （当前快照清单的 columns）里早已标为 retired 的记进 retired_existing、不再出确认项，这次才退役的记进
       retired_new。不这样分的话，8 月的配方永远含「分区乙」，以后每个月都会再报一次退役。整张表退役：这张表在
       retired_status 里的每一列都已标 retired，才算早已退役。
       新增的表名、列名和当前版本里退役的名字 collide_key 相同、写法不同（「PM2_5」退役、「pm2_5」新增）时按
       不兼容处理：SQLite 的名字不分大小写，并集建表会报重名。

    label_sets 只在 parts 之间比：要把本期算进去，调用方把本期也作为一期传进来（plan_accumulate 就这么做）。
    change：有不兼容为 semantic；否则有这次才退役的为 retire；否则配方与各期都相同、也没有新增为 same；其余 compatible。
    """
    semantic: list[str] = []

    def _say(text: str) -> None:
        if text not in semantic:
            semantic.append(text)

    for k, part in enumerate(parts, 1):
        if part.recipe is None:
            _say(f"第 {k} 期（{_period_text(part.start, part.end)}）没有配方记录，无法比较表结构")
            continue
        for ch in table_changes(part.recipe, target):
            if ch.kind in INCOMPATIBLE_KINDS:
                _say(f"表「{ch.table}」：{ch.message}")
    target_tables, problems = derive_tables(target)
    for p in problems:
        _say(p.message)
    current = current_tables if current_tables is not None else _tables_of_parts(parts)
    status = retired_status or {}

    added: list[dict[str, Any]] = []
    retired_new: list[dict[str, Any]] = []
    retired_existing: list[dict[str, Any]] = []
    gone_tables: dict[str, str] = {}
    for name, cols in current.items():
        info = status.get(name) or {}
        tgt = target_tables.get(name)
        if tgt is None:
            gone_tables[collide_key(name)] = name
            names = [c.name for c in cols]
            done = bool(names) and all((info.get(c) or {}).get("status") == "retired" for c in names)
            (retired_existing if done else retired_new).append({"table": name, "column": None})
            continue
        keep = {c.name for c in tgt}
        for col in cols:
            if col.name not in keep:
                entry = {"table": name, "column": col.name}
                done = (info.get(col.name) or {}).get("status") == "retired"
                (retired_existing if done else retired_new).append(entry)
    for name, cols in target_tables.items():
        have = current.get(name)
        if have is None:
            added.append({"table": name, "column": None})
            other = gone_tables.get(collide_key(name))
            if other is not None and other != name:
                _say(f"新增的表「{name}」与当前版本里退役的表「{other}」只差大小写或全半角：数据库中的表名不分大小写，"
                     "两张表不能并存")
            continue
        names = {c.name for c in have}
        gone = {collide_key(c.name): c.name for c in have if c.name not in {x.name for x in cols}}
        for col in cols:
            if col.name in names:
                continue
            added.append({"table": name, "column": col.name})
            other = gone.get(collide_key(col.name))
            if other is not None and other != col.name:
                _say(f"表「{name}」新增的列「{col.name}」与退役的列「{other}」只差大小写或全半角：数据库中的列名不分"
                     "大小写，两列不能并存")

    label_sets = _label_sets(parts)
    target_dump = _dump(target)
    same = all(p.recipe is not None and _dump(p.recipe) == target_dump for p in parts)
    if semantic:
        change = "semantic"
    elif retired_new:
        change = "retire"
    elif same and not added and not label_sets:
        change = "same"
    else:
        change = "compatible"
    return ChangeClass(change=change, added=added, retired_new=retired_new, retired_existing=retired_existing,
                       semantic=semantic, label_sets=label_sets)


# ==========================================================================
# 累积计划（P3-SPEC 2.4）
# ==========================================================================


def _part_dict(part: PartInfo, blockers: list[str]) -> dict[str, Any]:
    return {"import_id": part.import_id, "seq": part.seq, "start": part.start, "end": part.end,
            "file_name": part.file_name, "new": False, "rows": dict(part.rows), "blockers": list(blockers)}


def plan_accumulate(*, new_recipe: Recipe, new_period: tuple[str, str], period_source: str,
                    staging_kind: str, current_mode: str | None, current_snapshot_kind: str | None,
                    parts: list[PartInfo], current_tables: dict[str, list[ColumnOut]] | None,
                    retired_status: dict[str, dict[str, dict]] | None = None) -> AccumulatePlan:
    """这次导入启用后，当前版本由哪几期组成（P3-SPEC 2.4 的判定表）。不物化：union 留给调用方物化后填。

    - 新配方是每期替换：replace，结果只有本期；当前是累积快照时 mode_switch=accumulate->replace，各期列进 dropped。
    - 新配方是按期累积：
      - 没有当前快照、当前是简单导入、首次导入或从简单导入切换：first；
      - 现行也是累积：先判不兼容（restart 优先于 rejected，评审一-m12）——目标配方对结果快照里留下的任何一期
        不兼容，或者留下的某一期的配方不满足累积资格（parts[].blockers），restart，结果只有本期，此前各期列进
        dropped，部分重叠的各期写进 overlaps 只作提示。否则按统计期判：部分重叠 rejected；起止都相同
        replace_period；不相交 append（早于已有的某一期时 backfill）。修改配方、不换文件（staging_kind=redraft）
        用的就是最晚一期的原件，替换最晚一期（replace_period）；
      - 现行是替换（模式切换 replace->accumulate）：当前那一期有统计期、满足资格、配方兼容时作为第一期保留
        （append / replace_period）；没有统计期或不满足资格时 first（reason 写明），不兼容时 restart。

    period 除了 start、end 还带 source（period_source：cells / human）：补传早期而本期统计期是人工录入时，差异卡的
    period_backfill 要改为需确认（2.3，评审一-m12），WP-3 只拿得到计划，从这里读。
    parts 里本期标 new: true；本期的 file_name、rows 这里不知道（签名里没有），留空，调用方按试运行回执补上。
    结果只剩本期时不比新增、退役（没有早期各期可保留，列就跟着本期走，破坏性变化由期 2 的 breaking 确认项兜住），
    label_sets 也为空（restart 的结果只有本期）。rejected 时 parts 是原样不动的当前各期，label_sets 为空。
    current_tables 为 None 时按当前快照各期的配方推出表结构（含要被替换的那一期），新增、退役照样比得出来；
    早已退役的列只在真实的表结构里有，所以有当前快照时调用方应当给 current_tables。
    """
    start, end = (new_period or (None, None))[0], (new_period or (None, None))[1]
    period = {"start": start, "end": end, "source": period_source}
    ordered = sorted(parts, key=lambda p: (p.start or "", p.end or "", p.seq or 0))
    blockers: dict[int, list[str]] = {}
    for p in ordered:
        blockers[id(p)] = ([b.message for b in accumulate_blockers(p.recipe)] if p.recipe is not None
                           else ["这一期没有配方记录"])
    new_entry = {"import_id": None, "seq": None, "start": start, "end": end, "file_name": "", "new": True,
                 "rows": {}, "blockers": []}
    plan = AccumulatePlan(
        mode=new_recipe.mode, action="replace", period=period, parts=[new_entry], replaces=None, dropped=[],
        overlaps=[], gaps=[], backfill=False, change="same", added=[], retired_new=[], retired_existing=[],
        semantic=[], label_sets=[], mode_switch=None, reason=None, union=None,
    )

    def entry(p: PartInfo) -> dict[str, Any]:
        return _part_dict(p, blockers[id(p)])

    if new_recipe.mode != "accumulate":
        if current_mode == "accumulate" and current_snapshot_kind == "recipe" and ordered:
            plan.mode_switch = "accumulate->replace"
            plan.dropped = [entry(p) for p in ordered]
        return plan

    if not start or not end:
        raise ValueError("按期累积需要本期的统计期：统计期解析不出来时先由人录入，再做累积计划")
    new_blockers = [b.message for b in accumulate_blockers(new_recipe)]
    if new_blockers:
        # 静态校验已经拦过（mode=accumulate 时报 accumulate_blockers），走到这里说明配方没过校验就试运行了
        plan.action, plan.reason, plan.parts = "rejected", new_blockers[0], [entry(p) for p in ordered]
        return plan
    if (staging_kind in ("first", "switch") or current_snapshot_kind in (None, "simple") or current_mode is None
            or not ordered):
        plan.action = "first"
        return plan

    new_part = PartInfo(import_id="", seq=0, start=start, end=end, build_id="", db_path="", db_sha256="",
                        manifest_db_sha256=None, recipe_id=None, recipe=new_recipe, recipe_sha256=None,
                        raw_state="kept", file_name="", manifest_artifact=None)
    switching = current_mode != "accumulate"
    if switching:
        plan.mode_switch = "replace->accumulate"
        cur = ordered[-1]
        if not (cur.start and cur.end) or blockers[id(cur)]:
            plan.action = "first"
            plan.dropped = [entry(p) for p in ordered]
            if not (cur.start and cur.end):
                plan.reason = "当前版本没有统计期，不能作为第一期：启用后当前版本只含本期"
            else:
                plan.reason = (f"当前版本的配方不满足按期累积的要求（{blockers[id(cur)][0]}），不能作为第一期："
                               "启用后当前版本只含本期")
            return plan

    if staging_kind == "redraft":
        replaced: PartInfo | None = ordered[-1]
        kept = ordered[:-1]
        clash = [p for p in kept if _intersects((p.start, p.end), (start, end))]
    else:
        replaced = next((p for p in ordered if (p.start, p.end) == (start, end)), None)
        kept = [p for p in ordered if p is not replaced]
        clash = [p for p in kept if _intersects((p.start, p.end), (start, end))]

    # 没给当前版本的实际表结构时，按当前快照各期（含要被替换的那一期，它此刻仍在当前版本里）的配方推。不能让
    # classify_changes 自己退回它的 parts：那里面有本期，本期的列也算成「当前已有」，新增列就永远报不出来
    current = current_tables if current_tables is not None else _tables_of_parts(ordered)
    cc = classify_changes([*kept, new_part], new_recipe, current_tables=current, retired_status=retired_status)
    plan.change, plan.semantic, plan.label_sets = cc.change, cc.semantic, cc.label_sets
    if kept:
        plan.added, plan.retired_new, plan.retired_existing = cc.added, cc.retired_new, cc.retired_existing
    elif cc.change == "retire":
        plan.change = "compatible"
    blocked = [(k, p) for k, p in enumerate(ordered, 1) if p in kept and blockers[id(p)]]
    if cc.semantic or blocked:
        plan.action = "restart"
        plan.dropped = [entry(p) for p in ordered]
        plan.overlaps = [entry(p) for p in clash]
        if cc.semantic:
            plan.reason = "表结构有不兼容的变化：" + "；".join(cc.semantic)
        else:
            k, p = blocked[0]
            plan.reason = (f"第 {k} 期（{_period_text(p.start, p.end)}）的配方不满足按期累积的要求："
                           f"{blockers[id(p)][0]}")
        # 结果只有本期：没有早期各期可保留，新增、退役、各期维度取值的差异都无从谈起。label_sets 不清的话，
        # 差异卡会说「本期没有「7-8」，此前各期有」，可那些期并不在新版本里
        plan.added, plan.retired_new, plan.retired_existing, plan.label_sets = [], [], [], []
        if plan.change == "retire":
            plan.change = "semantic"
        return plan
    if clash:
        plan.action = "rejected"
        plan.overlaps = [entry(p) for p in clash]
        plan.parts = [entry(p) for p in ordered]
        # 拒收时没有结果版本，本期与各期的取值差异没有落点
        plan.label_sets = []
        return plan

    result = sorted([*kept, new_part], key=lambda p: (p.start or "", p.end or ""))
    plan.parts = [new_entry if p is new_part else entry(p) for p in result]
    if replaced is not None:
        plan.action, plan.replaces = "replace_period", entry(replaced)
    else:
        plan.action = "append"
        plan.backfill = any((p.start or "") > start for p in kept)
    plan.gaps = _gaps([(p.start, p.end) for p in result if p.start and p.end])
    return plan


# ==========================================================================
# 并集 id 与物化（P3-SPEC 2.1、2.6）
# ==========================================================================


def retired_columns(change: Any, current_tables: dict[str, list[ColumnOut]] | None) -> dict[str, list[ColumnOut]]:
    """把计划（AccumulatePlan）或 classify_changes 的结果里的退役项，换成 materialize_union 的 retired 参数。

    顺序有讲究（P3-SPEC 2.6「退役列按退役先后追加」）：**先 retired_existing，再 retired_new**，各自按
    current_tables 里的列序。当前版本的表结构里，早先退役的列本来就排在目标配方的列之后、按退役先后排着；这次才
    退役的列原是目标配方里的列，排在前面。照 current_tables 的顺序直接拼，新退役的就跑到早先退役的前面去了。
    退役列的顺序决定并集的建表语句，也就决定库文件的哈希：复活时重新物化要得到同一个哈希，同一组输入就必须
    拼出同一个顺序，所以由这里统一拼，调用方不要自己拼。整张表退役（column 为 None）给这张表的全部列。
    """
    tables = current_tables or {}
    out: dict[str, list[ColumnOut]] = {}
    for entry in [*(getattr(change, "retired_existing", None) or []), *(getattr(change, "retired_new", None) or [])]:
        table, column = entry.get("table"), entry.get("column")
        cols = tables.get(table) or []
        picks = list(cols) if column is None else [c for c in cols if c.name == column]
        have = out.setdefault(table, [])
        known = {c.name for c in have}
        for col in picks:
            if col.name not in known:
                have.append(_copy_col(col))
                known.add(col.name)
        if not have:
            del out[table]
    return out


def retired_layout(retired: dict[str, list[ColumnOut]] | None) -> list[list[Any]]:
    """退役列的规范写法 [[表, [[列, 类型], …]], …]，**保留给定的顺序**（表的先后、列的先后都进库文件的哈希），
    同一张表里重复的列只记第一次，没有列的表不记。并集 id、并集构建的 options 都用它。"""
    out: list[list[Any]] = []
    for table, cols in (retired or {}).items():
        seen: set[str] = set()
        listed: list[list[str]] = []
        for col in cols:
            if col.name in seen:
                continue
            seen.add(col.name)
            listed.append([str(col.name), str(col.type)])
        if listed:
            out.append([str(table), listed])
    return out


def _ordered_parts(parts: list[tuple[str, str, str]]) -> list[list[str]]:
    return sorted(([str(s), str(e), str(b)] for s, e, b in parts), key=lambda x: (x[0], x[1]))


def union_id(source_id: str, parts: list[tuple[str, str, str]], target_recipe_sha: str, *,
             retired: dict[str, list[ColumnOut]] | None = None) -> str:
    """并集 id = sha_json({source_id, parts: [[起, 止, 构建 id]…] 按统计期排序, 目标配方哈希, union_ver})，
    有退役列时再加 "retired": retired_layout(retired)。

    只取决于各期构建和并集的表结构，**不含导入记录 id**：同一个 9 月文件换个文件名再传，导入记录是新的，
    并集文件照样复用。

    **retired 必须和传给 materialize_union 的是同一个**（用 retired_columns 拼）。退役列是有历史的：同样的各期、
    同样的目标配方，在含「分区乙」的历史上移除某一期，并集里留着全为空值的「分区乙」；回滚之后重新导入同样两个
    文件，就没有这一列。id 不含退役列的话，两份结构不同的文件同一个 id，发布时目标已存在、哈希等于登记值就直接
    复用旧文件，用户在试运行里核对的和真正发布的不是同一份（H2）。没有退役列时 id 与不带这个键时相同。
    """
    payload: dict[str, Any] = {"source_id": source_id, "parts": _ordered_parts(parts),
                               "target_recipe_sha256": target_recipe_sha, "union_ver": UNION_VER}
    layout = retired_layout(retired)
    if layout:
        payload["retired"] = layout
    return sha_json(payload)


def union_options(parts: list[tuple[str, str, str]], target_recipe_sha: str, *,
                  retired: dict[str, list[ColumnOut]] | None = None) -> dict[str, Any]:
    """并集构建登记的 options（TableBuild.options，P3-SPEC 2.1）：{"kind": "union", "parts", "target_recipe_sha256"}，
    有退役列时加 "retired"。与 union_id 的输入一一对应，管理员按 options 就能复核 id 是怎么来的。"""
    options: dict[str, Any] = {"kind": "union", "parts": _ordered_parts(parts),
                               "target_recipe_sha256": target_recipe_sha}
    layout = retired_layout(retired)
    if layout:
        options["retired"] = layout
    return options


class _Table:
    """并集里的一张表：列（目标配方的在前、退役的按给定顺序追加在后）、主键、日期轴列。"""

    def __init__(self, name: str, cols: list[ColumnOut], grain: list[str]) -> None:
        self.name = name
        self.cols = cols
        self.grain = grain
        self.axis = next((c.name for c in cols if c.role == "axis"), None)
        self.names = [c.name for c in cols]


def _copy_col(col: ColumnOut) -> ColumnOut:
    return ColumnOut(name=col.name, type=col.type, header=col.header, unit=col.unit, role=col.role)


def _layout(target: Recipe, parts: list[UnionPart],
            retired: dict[str, list[ColumnOut]] | None) -> dict[str, _Table]:
    """并集的表结构：derive_tables(target) 的列序，加上退役列。退役列、整张退役的表，类型、单位、表头、主键
    取最后一个还含它的那一期的配方（「类型取最后一个含它的那一期」，说明的单位也要用它）。

    退役列按 retired 给定的顺序追加，整张退役的表按 retired 的顺序排在目标配方的表之后。**这个顺序进建表语句，
    也就进库文件的哈希**：retired 要用 retired_columns 拼（先早先退役的、再这次才退役的），union_id 要带上同一个
    retired，否则同一个并集 id 会对应结构不同的文件，复活时重新物化也对不上登记的哈希（build_conflict）。"""
    tables, problems = derive_tables(target)
    if problems:
        raise UnionError("目标配方推不出表结构：" + "；".join(p.message for p in problems[:3]), code="union_failed")
    specs = {t.name: t for t in target.tables}
    derived = [(derive_tables(p.recipe)[0], {t.name: t for t in p.recipe.tables}) for p in parts]

    def last_with(table: str, column: str | None) -> tuple[ColumnOut | None, list[str] | None]:
        for cols, pspecs in reversed(derived):
            have = cols.get(table)
            if have is None:
                continue
            grain = list(pspecs[table].grain) if table in pspecs else []
            if column is None:
                return None, grain
            found = next((c for c in have if c.name == column), None)
            if found is not None:
                return found, grain
        return None, None

    out: dict[str, _Table] = {}
    retired = retired or {}
    for name, cols in tables.items():
        mine = [_copy_col(c) for c in cols]
        known = {c.name for c in mine}
        for col in retired.get(name, []):
            if col.name in known:
                continue
            found, _ = last_with(name, col.name)
            mine.append(_copy_col(found) if found is not None else _copy_col(col))
            known.add(col.name)
        spec = specs.get(name)
        out[name] = _Table(name, mine, list(spec.grain) if spec is not None else [])
    for name, cols in retired.items():
        if name in out:
            continue
        base: list[ColumnOut] = []
        for cols_k, _pspecs in reversed(derived):
            if name in cols_k:
                base = [_copy_col(c) for c in cols_k[name]]
                break
        known = {c.name for c in base}
        for col in cols:
            if col.name not in known:
                found, _ = last_with(name, col.name)
                base.append(_copy_col(found) if found is not None else _copy_col(col))
                known.add(col.name)
        _, grain = last_with(name, None)
        out[name] = _Table(name, base, [g for g in grain or [] if g in known])
    return out


def _create(conn: sqlite3.Connection, table: _Table) -> None:
    """建表，写法与执行器相同（recipe_engine._create_tables）：STRICT；主键列 NOT NULL；单列 INTEGER 主键写成
    列约束的 PRIMARY KEY DESC，不让它成为 rowid 的别名（rowid 必须是插入序号，期 4 按 rowid 区间溯源）。"""
    types = {c.name: c.type for c in table.cols}
    alias = len(table.grain) == 1 and types.get(table.grain[0]) == "INTEGER"
    defs = []
    for col in table.cols:
        d = f"{_q(col.name)} {col.type}"
        if alias and col.name == table.grain[0]:
            d += " PRIMARY KEY DESC NOT NULL"
        elif col.name in table.grain:
            d += " NOT NULL"
        defs.append(d)
    if table.grain and not alias:
        defs.append(f"PRIMARY KEY ({', '.join(_q(g) for g in table.grain)})")
    conn.execute(f"CREATE TABLE {_q(table.name)} ({', '.join(defs)}) STRICT")


def _check(cid: str, kind: str, title: str, *, checked: int, failed: list[str], sql: str | None,
           params: list[Any] | None = None) -> CheckResult:
    return CheckResult(id=cid, kind=kind, title=title, status="mismatch" if failed else "passed",  # type: ignore[arg-type]
                       category="structure", checked=checked, failed=len(failed), sql=sql,
                       params=list(params or []), details=failed[:20])


def materialize_union(out: Path, *, target: Recipe, parts: list[UnionPart],
                      retired: dict[str, list[ColumnOut]] | None = None) -> UnionReport:
    """按目标配方的表结构，把各期构建库物化成一个并集库（P3-SPEC 2.6）。同步，调用方放进线程池并占 parse_slot。

    parts 已按统计期排序，可以只有一期（移除、撤销之后剩一期而那一期的配方不是目标配方，7.4）。retired 是退役列：
    表 → 列，整张退役的表给全部的列，**用 retired_columns(计划, current_tables) 拼**（先 retired_existing、再
    retired_new，顺序进库哈希），并且原样传给 union_id、union_options。

    0. 逐期算构建库文件的 sha256，和 UnionPart.db_sha256（TableBuild 登记的）、manifest_db_sha256（导入清单里的）
       都比对，不一致抛 UnionError(part_tampered)；文件不在抛 part_missing。各期以 immutable 方式附加，SQLite 不会
       再校验，被改过的库附加进来就被「洗白」成新版本了，所以必须在附加之前算（评审一-M4）。
    1. 先写 <out>.tmp-<随机>（open_checked_sqlite，关 DQS，回滚日志、不开 WAL），成功后 os.replace 到 out。
    2. 建表：目标配方的列在前、退役列追加在后；grain 非空时加 PRIMARY KEY，主键列 NOT NULL。
    3. 逐期附加（mode=ro&immutable=1）、断言该期每张表 rowid 从 1 连续（不成立抛 union_failed：期 4 按偏移把并集
       rowid 换算回该期构建库，依赖这一条）、INSERT … SELECT … ORDER BY rowid（只选这一期有的列）、每期一个事务、
       提交后 DETACH（SQLite 不允许在事务里 DETACH；逐期附加也绕开了最多附加 10 个库的上限）。
    4. 记下每期每张表的 rowid 区间（并集里的、该期构建库里的）。
    5. 结构核对 U1 行数、U2 主键（写入时撞上主键约束也记成 U2 不通过）、U3 日期落在该期统计期内。
    6. table_hashes 按 union/1 的组合定义（目标表结构加各期登记的表哈希），不逐行重算；空值数按表、按期分开记
       （按期的只记该期构建库里有的列：不含这一列的期是结构性空值，说明不能说成「原表为空格」）。
    7. 关连接、settle_journal、算 db_sha256、os.replace。

    **U1–U3 有不通过的不抛异常**：不产出文件（临时文件删掉，out 上旧的同名文件也删掉，免得被当成这次的结果发布），
    返回 ok=False、db_sha256 为空的报告。试运行要把核对结果（哪一期哪张表几行）给人看，所以看 report.ok 判
    rejected；版本页的写操作（移除、撤销）没有地方展示核对结果，调 require_union_ok(report) 换成
    UnionError(union_failed)，接口层映射成 409 union_failed（7.4）。漏检也不会静默发布：没有文件，提交时是
    trial_missing。其余失败（part_missing、part_tampered、rowid 不连续等）抛 UnionError。
    """
    out = Path(out)
    if not parts:
        raise ValueError("物化至少要有一期")
    keys = [(p.start, p.end) for p in parts]
    if keys != sorted(keys):
        raise ValueError("parts 必须按统计期排序")
    for k, part in enumerate(parts, 1):
        path = Path(part.db_path)
        label = f"第 {k} 期（{_period_text(part.start, part.end)}）"
        if not path.is_file():
            # 出路照 7.4 写：丢了文件的那一期移出去，或回到更早的版本。接口层直接转发这句
            raise UnionError(f"{label}的数据文件已丢失，无法重建当前版本。可以先移除这一期，"
                             "或在数据源卡片的「版本」中启用更早的版本", code="part_missing", part=k)
        actual = raw_store.sha256_file(path)
        expected = {str(part.db_sha256 or "").strip()}
        if part.manifest_db_sha256 is not None:
            expected.add(str(part.manifest_db_sha256).strip())
        if expected != {actual}:
            raise UnionError(f"{label}的数据文件与登记的不一致，可能被修改过", code="part_tampered", part=k)
    layout = _layout(target, parts, retired)
    targets = {name: {c.name for c in cols} for name, cols in derive_tables(target)[0].items()}
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.parent / f"{out.name}{raw_store.TMP_MARK}{secrets.token_hex(6)}"
    try:
        report = _materialize(tmp, layout, parts, targets)
        if not report.ok:
            _remove_db_files(tmp)
            _remove_db_files(out)
            return report
        settle_journal(tmp)
        report.db_sha256 = raw_store.sha256_file(tmp)
        os.replace(tmp, out)
        return report
    except BaseException:
        _remove_db_files(tmp)
        raise


def require_union_ok(report: UnionReport) -> None:
    """U1–U3 有不通过的就抛 UnionError(union_failed)，消息写出不通过的细节（前三条）。

    materialize_union 遇到 U1–U3 不通过只返回 ok=False 的报告（试运行要展示核对结果）；版本页的移除、撤销没有地方
    展示，用它把报告换成异常，接口层照 7.4 回 409 union_failed。"""
    if report.ok:
        return
    details = [d for c in report.checks if c.status != "passed" for d in (c.details or [c.title])]
    raise UnionError("累积后的数据没有通过结构核对，当前版本未受影响：" + "；".join(details[:3]), code="union_failed")


def _remove_db_files(path: Path) -> None:
    for suffix in ("", "-journal", "-wal", "-shm"):
        Path(f"{path}{suffix}").unlink(missing_ok=True)


def _materialize(tmp: Path, layout: dict[str, _Table], parts: list[UnionPart],
                 targets: dict[str, set[str]]) -> UnionReport:
    conn = open_checked_sqlite(str(tmp), readonly=False)
    conn.isolation_level = None
    part_rows: list[dict[str, dict[str, list[int]]]] = []
    part_cols: list[dict[str, list[str]]] = []
    part_counts: list[dict[str, int]] = []
    pk_failures: list[str] = []
    #: 撞上主键约束、整期回滚的那几期（下标从 0 起）
    rolled_back: set[int] = set()
    try:
        conn.execute("BEGIN")
        for table in layout.values():
            _create(conn, table)
        conn.execute("COMMIT")
        for k, part in enumerate(parts, 1):
            label = f"第 {k} 期（{_period_text(part.start, part.end)}）"
            conn.execute("ATTACH DATABASE ? AS part", (f"file:{quote(os.fspath(part.db_path))}?mode=ro&immutable=1",))
            ranges: dict[str, dict[str, list[int]]] = {}
            cols_k: dict[str, list[str]] = {}
            counts: dict[str, int] = {}
            try:
                present = [r[0] for r in conn.execute(
                    "SELECT name FROM part.sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
                stray = [t for t in present if t not in layout]
                if stray:
                    raise UnionError(f"{label}有表" + "、".join(f"「{t}」" for t in stray)
                                     + "在累积后的表结构里没有位置", code="union_failed", part=k)
                conn.execute("BEGIN")
                try:
                    for name, table in layout.items():
                        if name not in present:
                            continue
                        have = [r[1] for r in conn.execute(f"PRAGMA part.table_info({_q(name)})")]
                        extra = [c for c in have if c not in table.names]
                        if extra:
                            raise UnionError(f"{label}的「{name}」有列" + "、".join(f"「{c}」" for c in extra)
                                             + "在累积后的表结构里没有位置", code="union_failed", part=k)
                        lo, hi, n = conn.execute(f"SELECT MIN(rowid), MAX(rowid), COUNT(*) FROM part.{_q(name)}").fetchone()
                        if n and (lo != 1 or hi != n):
                            raise UnionError(f"{label}的「{name}」行号不连续（{lo} 至 {hi}，共 {n} 行），无法按行号溯源",
                                             code="union_failed", part=k)
                        cols = [c for c in table.names if c in have]
                        before = conn.execute(f"SELECT COALESCE(MAX(rowid), 0) FROM main.{_q(name)}").fetchone()[0]
                        listed = ", ".join(_q(c) for c in cols)
                        conn.execute(f"INSERT INTO main.{_q(name)} ({listed}) SELECT {listed} FROM part.{_q(name)} "
                                     "ORDER BY rowid")
                        after = conn.execute(f"SELECT COALESCE(MAX(rowid), 0) FROM main.{_q(name)}").fetchone()[0]
                        if after - before != n:
                            raise UnionError(f"{label}的「{name}」写入了 {after - before} 行，应为 {n} 行",
                                             code="union_failed", part=k)
                        ranges[name] = {"union": [before + 1, after], "part": [1, n]}
                        cols_k[name] = cols
                        counts[name] = n
                    conn.execute("COMMIT")
                except sqlite3.IntegrityError as e:
                    conn.execute("ROLLBACK")
                    pk_failures.append(_pk_conflict(conn, layout, parts, part_rows, k, e))
                    ranges, cols_k, counts = {}, {}, {}
                    rolled_back.add(k - 1)
                except BaseException:
                    if conn.in_transaction:
                        conn.execute("ROLLBACK")
                    raise
            finally:
                conn.execute("DETACH DATABASE part")
            part_rows.append(ranges)
            part_cols.append(cols_k)
            part_counts.append(counts)
        return _finish(conn, layout, parts, part_rows, part_cols, part_counts, pk_failures, rolled_back, targets)
    finally:
        conn.close()


def _pk_conflict(conn: sqlite3.Connection, layout: dict[str, _Table], parts: list[UnionPart],
                 part_rows: list[dict[str, dict[str, list[int]]]], k: int, error: Exception) -> str:
    """第 k 期写入时撞上了主键约束：找出和哪一期、在哪个主键上重复（库已回滚到第 k 期之前，part 还附加着）。"""
    label = f"第 {k} 期（{_period_text(parts[k - 1].start, parts[k - 1].end)}）"
    for name, table in layout.items():
        if not table.grain:
            continue
        try:
            listed = ", ".join(_q(g) for g in table.grain)
            nulls = conn.execute(f"SELECT COUNT(*) FROM part.{_q(name)} WHERE "
                                 + " OR ".join(f"{_q(g)} IS NULL" for g in table.grain)).fetchone()[0]
            if nulls:
                return f"{label}的「{name}」有 {nulls} 行主键列为空值"
            dup = conn.execute(f"SELECT {listed} FROM part.{_q(name)} GROUP BY {listed} HAVING COUNT(*) > 1 LIMIT 1"
                               ).fetchone()
            if dup is not None:
                key = "，".join(f"{g}={v}" for g, v in zip(table.grain, dup))
                return f"{label}的「{name}」自身在主键（{key}）上重复"
            hit = conn.execute(f"SELECT m.rowid, {', '.join('m.' + _q(g) for g in table.grain)} FROM main.{_q(name)} m "
                               f"JOIN part.{_q(name)} p USING ({listed}) LIMIT 1").fetchone()
        except sqlite3.Error:
            continue
        if hit is None:
            continue
        rowid, values = hit[0], hit[1:]
        other = next((j for j, ranges in enumerate(part_rows, 1)
                      if name in ranges and ranges[name]["union"][0] <= rowid <= ranges[name]["union"][1]), None)
        key = "，".join(f"{g}={v}" for g, v in zip(table.grain, values))
        who = f"第 {other} 期" if other is not None else "此前的某一期"
        return f"{who}与第 {k} 期在「{name}」的主键（{key}）上重复"
    return f"{label}写入时违反了主键约束（{error}）"


def _finish(conn: sqlite3.Connection, layout: dict[str, _Table], parts: list[UnionPart],
            part_rows: list[dict[str, dict[str, list[int]]]], part_cols: list[dict[str, list[str]]],
            part_counts: list[dict[str, int]], pk_failures: list[str], rolled_back: set[int],
            targets: dict[str, set[str]]) -> UnionReport:
    rows: dict[str, int] = {}
    null_counts: dict[str, dict[str, int]] = {}
    for name, table in layout.items():
        exprs = ", ".join(f"COUNT({_q(c)})" for c in table.names)
        got = conn.execute(f"SELECT COUNT(*), {exprs} FROM {_q(name)}").fetchone()
        rows[name] = got[0]
        null_counts[name] = {c: got[0] - n for c, n in zip(table.names, got[1:])}

    # U1 行数：每张表的并集行数 = 各期该表行数之和
    u1: list[str] = []
    for name in layout:
        want = sum(c.get(name, 0) for c in part_counts)
        # 撞了主键而整期回滚的那几期没有计入 part_counts：按它们在构建库里的行数补上，U1 才会如实报出少了的行
        want += sum(_part_count(parts[k], name) for k in sorted(rolled_back))
        if rows[name] != want:
            u1.append(f"表「{name}」累积后 {rows[name]} 行，各期之和 {want} 行")
    first = next(iter(layout), None)
    checks = [_check("U1", "union_rows", "累积后每张表的行数等于各期行数之和", checked=len(layout), failed=u1,
                     sql=f"SELECT COUNT(*) FROM {_q(first)}" if first else None)]

    # U2 主键：按目标 grain 分组没有重复；写入时撞上主键约束的也记在这里
    u2 = list(pk_failures)
    keyed = [t for t in layout.values() if t.grain]
    sql2 = None
    for table in keyed:
        listed = ", ".join(_q(g) for g in table.grain)
        sql2 = f"SELECT COUNT(*) FROM (SELECT 1 FROM {_q(table.name)} GROUP BY {listed} HAVING COUNT(*) > 1)"
        dup = conn.execute(sql2).fetchone()[0]
        if dup:
            u2.append(f"表「{table.name}」有 {dup} 组主键重复")
    checks.append(_check("U2", "union_pk", "累积后主键不重复", checked=len(keyed), failed=u2, sql=sql2))

    # U3 统计期：第 k 期的 rowid 区间里，日期都在 [start_k, end_k] 之内
    u3: list[str] = []
    sql3, params3, checked3 = None, [], 0
    for k, (part, ranges) in enumerate(zip(parts, part_rows), 1):
        for name, span in ranges.items():
            axis = layout[name].axis
            if axis is None or axis not in part_cols[k - 1].get(name, []):
                continue
            a, b = span["union"]
            if b < a:
                continue
            checked3 += 1
            sql3 = (f"SELECT COUNT(*) FROM {_q(name)} WHERE rowid BETWEEN ? AND ? "
                    f"AND ({_q(axis)} < ? OR {_q(axis)} > ?)")
            params3 = [a, b, part.start, part.end]
            bad = conn.execute(sql3, params3).fetchone()[0]
            if bad:
                u3.append(f"第 {k} 期（{_period_text(part.start, part.end)}）的「{name}」有 {bad} 行日期不在该期统计期内")
    checks.append(_check("U3", "union_period", "各期的日期都在该期的统计期内", checked=checked3, failed=u3,
                         sql=sql3, params=params3))

    part_nulls: list[dict[str, dict[str, int]]] = []
    for ranges, cols_k in zip(part_rows, part_cols):
        mine: dict[str, dict[str, int]] = {}
        for name, span in ranges.items():
            cols = cols_k.get(name, [])
            a, b = span["union"]
            if not cols:
                continue
            if b < a:
                mine[name] = {c: 0 for c in cols}
                continue
            got = conn.execute(f"SELECT {', '.join(f'COUNT(*) - COUNT({_q(c)})' for c in cols)} FROM {_q(name)} "
                               "WHERE rowid BETWEEN ? AND ?", (a, b)).fetchone()
            mine[name] = dict(zip(cols, got))
        part_nulls.append(mine)

    table_hashes: dict[str, str] = {}
    for name, table in layout.items():
        hashes: list[str | None] = []
        for k, (part, ranges) in enumerate(zip(parts, part_rows), 1):
            if name not in ranges:
                hashes.append(None)
                continue
            got = part.table_hashes.get(name)
            if not got:
                raise UnionError(f"第 {k} 期（{_period_text(part.start, part.end)}）缺少表「{name}」的表哈希登记",
                                 code="union_failed", part=k)
            hashes.append(got)
        table_hashes[name] = sha_json({"union_ver": UNION_VER, "columns": [[c.name, c.type] for c in table.cols],
                                       "grain": table.grain, "parts": hashes})

    added: list[dict[str, Any]] = []
    retired: list[dict[str, Any]] = []
    for name, table in layout.items():
        with_table = [_period_key(p.start, p.end) for p, ranges in zip(parts, part_rows) if name in ranges]
        if name not in targets:
            retired.append({"table": name, "column": None, "periods": with_table})
            continue
        if len(with_table) < len(parts):
            added.append({"table": name, "column": None, "periods": with_table})
        for col in table.cols:
            having = [_period_key(p.start, p.end) for p, cols_k in zip(parts, part_cols)
                      if col.name in cols_k.get(name, [])]
            if col.name not in targets[name]:
                retired.append({"table": name, "column": col.name, "periods": having})
            elif len(having) < len(with_table):
                added.append({"table": name, "column": col.name, "periods": having})

    ok = not (u1 or u2 or u3)
    return UnionReport(
        tables={name: table.cols for name, table in layout.items()},
        grains={name: table.grain for name, table in layout.items()},
        rows=rows, part_rows=part_rows, table_hashes=table_hashes, null_counts=null_counts,
        part_null_counts=part_nulls, added=added, retired=retired, checks=checks, ok=ok, db_sha256="",
    )


def _part_count(part: UnionPart, name: str) -> int:
    """某一期构建库里一张表的行数（只读打开）。表不在时为 0。"""
    try:
        conn = open_checked_sqlite(part.db_path, readonly=True)
    except sqlite3.Error:
        return 0
    try:
        return int(conn.execute(f"SELECT COUNT(*) FROM {_q(name)}").fetchone()[0])
    except sqlite3.Error:
        return 0
    finally:
        conn.close()

