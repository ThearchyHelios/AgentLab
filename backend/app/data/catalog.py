"""业务数据目录：每张表一份业务说明，给助手建图、db_schema 工具和后续的检查用。

**为什么要有它。** 接进来的业务库往往一个注释都没有：几百张表、上千个字段，助手只看得到名字，模型就会
照着名字编字段、猜关联。目录把「一行代表什么、业务主键是哪几列、业务日期按哪列算、金额是什么单位、
状态码各是什么意思、这张表和哪张表怎么连」记下来，并注明每一条是怎么来的、有没有人确认过。

**只记数据事实，不放计算公式。** 公式（指标怎么算）在口径卡里；这里只描述数据本身。所以 notes 的
字段是固定的一组，不认识的字段一律拒收（validate_notes）。

notes 结构（每个「项」都是 {value, source, status}，可选 note、updated_at）::

    {"label": 项(str), "description": 项(str), "grain": 项(str), "keys": 项(list[str]),
     "kind": 项("fact"|"dimension"|"snapshot"|"log"|"config"),
     "business_date": 项({"column": str, "rule": str, "timezone": str}),
     "valid_filter": 项(str，SQL 条件片段), "dedup": 项(str),
     "columns": {列名: {"label": 项, "meaning": 项, "unit": 项,
                       "measure": 项("flow"|"stock"|"ratio"|"identifier"|"status"|"attribute"),
                       "codes": 项(dict[str, str])}},
     "relations": [{"id", "columns", "to_table", "to_columns", "cardinality", "coverage",
                    "source", "status", "note"?}]}

来源（source）：comment 数据库注释、fk 外键约束、name 命名推断、profile 数据剖析（阶段 2）、llm 模型起草、
human 人工填写。状态（status）：proposed 推断（命名推断、模型起草、数据库注释）、verified 有确证（外键
约束；阶段 2 的数据剖析也写这个状态）、confirmed 人工确认、rejected 人工驳回。只有 confirmed 和 verified
将来会触发错误级检查；给模型看时 proposed 的项标「推断，未确认」；rejected 的项留着（防止下次起草又提出
来），但模型和检查都看不到。

本模块分几块：存取（带乐观锁）、结构校验、合并与审阅、起草、渲染、关系图、使用次数、冻结。存取以外的
函数尽量是纯函数，接口层、db_schema 工具和后续阶段（1B 助手挑表、2 数据剖析、4 维护闭环）共用。
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import re
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.data.names import name_key
from app.db.models import Artifact, CatalogNote, Run

# ==========================================================================
# 取值
# ==========================================================================

#: 项的来源
ITEM_SOURCES = ("comment", "fk", "name", "profile", "llm", "human")
#: 项的状态
ITEM_STATUSES = ("proposed", "verified", "confirmed", "rejected")
#: 表类型
TABLE_KINDS = ("fact", "dimension", "snapshot", "log", "config")
#: 列的度量类型：flow 流量（可跨期加总）、stock 存量（时点值，不能跨期加总）、ratio 比率、
#: identifier 标识、status 状态、attribute 属性
COLUMN_MEASURES = ("flow", "stock", "ratio", "identifier", "status", "attribute")
#: 关系的基数（从本表看过去）
CARDINALITIES = ("many_to_one", "one_to_one", "one_to_many")

#: 表级的项，按渲染顺序
TABLE_FIELDS = ("label", "description", "grain", "keys", "kind", "business_date", "valid_filter", "dedup")
#: 列级的项，按渲染顺序
COLUMN_FIELDS = ("label", "meaning", "unit", "measure", "codes")

#: 字段的中文叫法：校验报错、审阅接口的说明都用它，不在句子里裸写字段名
TABLE_FIELD_LABEL = {
    "label": "中文名", "description": "说明", "grain": "粒度", "keys": "业务主键", "kind": "表类型",
    "business_date": "业务日期", "valid_filter": "有效记录条件", "dedup": "去重规则",
}
COLUMN_FIELD_LABEL = {"label": "中文名", "meaning": "含义", "unit": "单位", "measure": "度量类型", "codes": "码值"}

#: 各来源起草出来的项的初始状态：外键约束是数据库强制的，算有确证；人工填写即确认；其余都是推断
_INITIAL_STATUS = {"fk": "verified", "human": "confirmed"}

_ITEM_KEYS = frozenset({"value", "source", "status", "note", "updated_at"})
_RELATION_KEYS = frozenset({"id", "columns", "to_table", "to_columns", "cardinality", "coverage", "source",
                            "status", "note", "updated_at"})
_BUSINESS_DATE_KEYS = frozenset({"column", "rule", "timezone"})
#: 文本项的长度上限。目录是给模型和人读的短说明，长篇大论放口径卡或知识库
_TEXT_MAX = 500
_LONG_TEXT_MAX = 2000


def now_iso() -> str:
    """项的 updated_at 用的时间戳（UTC，ISO 8601）。"""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def initial_status(source: str) -> str:
    """某个来源起草出来的项的初始状态。"""
    return _INITIAL_STATUS.get(source, "proposed")


def make_item(value: Any, source: str, status: str | None = None, *, note: str | None = None,
              at: str | None = None) -> dict[str, Any]:
    """构造一个项。status 不给时按来源取初始状态（initial_status）。"""
    out: dict[str, Any] = {"value": value, "source": source, "status": status or initial_status(source)}
    if note:
        out["note"] = note
    if at:
        out["updated_at"] = at
    return out


# ==========================================================================
# 异常
# ==========================================================================


class CatalogConflict(Exception):
    """写入时带的版本和库里的不一致：别人刚改过这张表。message 给人看。"""

    def __init__(self, table: str, current: int) -> None:
        super().__init__(f"表「{table}」的数据目录刚被修改过，请重新载入后再提交")
        self.table = table
        self.current = current


class CatalogInvalid(ValueError):
    """notes 结构不合规。problems 是逐条的中文说明。"""

    def __init__(self, problems: list[str]) -> None:
        super().__init__("；".join(problems[:5]) + ("…" if len(problems) > 5 else ""))
        self.problems = problems


# ==========================================================================
# 结构校验
# ==========================================================================


def _text_ok(value: Any, limit: int = _TEXT_MAX) -> bool:
    return isinstance(value, str) and bool(value.strip()) and len(value) <= limit


def _str_list_ok(value: Any) -> bool:
    return isinstance(value, list) and bool(value) and all(isinstance(v, str) and v.strip() for v in value)


def _business_date_ok(value: Any) -> bool:
    if not isinstance(value, dict) or set(value) - _BUSINESS_DATE_KEYS:
        return False
    if not _text_ok(value.get("column")):
        return False
    return all(isinstance(value[k], str) for k in ("rule", "timezone") if k in value)


def _codes_ok(value: Any) -> bool:
    return (isinstance(value, dict) and bool(value)
            and all(isinstance(k, str) and isinstance(v, str) and v.strip() for k, v in value.items()))


#: 表类型、度量类型在界面上的叫法（和 frontend/src/lib/terms.ts 的 CATALOG_KIND_LABEL / CATALOG_MEASURE_LABEL 一致）。
#: 校验报错会原样上界面（接口 422 的 detail），按它写：以前写「事实表」「流量」，界面上却是「明细表」「可累加」。
#: 给模型看的渲染另用 KIND_LABEL / MEASURE_LABEL，保留「事实表」「流量，可跨期加总」——模型认得这套说法
UI_KIND_LABEL = {"fact": "明细表", "dimension": "维度表", "snapshot": "快照表", "log": "日志表", "config": "配置表"}
UI_MEASURE_LABEL = {"flow": "可累加", "stock": "存量", "ratio": "比率", "identifier": "标识", "status": "状态",
                    "attribute": "属性"}


def _one_of(labels: Iterable[str]) -> str:
    names = list(labels)
    return f"应为{'、'.join(names[:-1])}或{names[-1]}之一"


#: 每个字段的值怎么查、不合规时怎么说
_TABLE_VALUE_RULES: dict[str, tuple[Any, str]] = {
    "label": (_text_ok, "应为不超过 500 字的文字"),
    "description": (lambda v: _text_ok(v, _LONG_TEXT_MAX), "应为不超过 2000 字的文字"),
    "grain": (_text_ok, "应为不超过 500 字的文字"),
    "keys": (_str_list_ok, "应为列名的列表"),
    "kind": (lambda v: v in TABLE_KINDS, _one_of(UI_KIND_LABEL[k] for k in TABLE_KINDS)),
    "business_date": (_business_date_ok, "应写明日期列，规则和时区写成文字"),
    "valid_filter": (_text_ok, "应为不超过 500 字的条件"),
    "dedup": (_text_ok, "应为不超过 500 字的文字"),
}
_COLUMN_VALUE_RULES: dict[str, tuple[Any, str]] = {
    "label": (_text_ok, "应为不超过 500 字的文字"),
    "meaning": (_text_ok, "应为不超过 500 字的文字"),
    "unit": (lambda v: _text_ok(v, 50), "应为不超过 50 字的文字"),
    "measure": (lambda v: v in COLUMN_MEASURES, _one_of(UI_MEASURE_LABEL[k] for k in COLUMN_MEASURES)),
    "codes": (_codes_ok, "应为码值到含义的对照，含义写成文字"),
}


def _item_problems(where: str, item: Any, rule: tuple[Any, str]) -> list[str]:
    if not isinstance(item, dict) or not {"value", "source", "status"} <= set(item):
        return [f"{where}不是有效的目录项：需要值、来源和状态"]
    problems: list[str] = []
    if extra := sorted(set(item) - _ITEM_KEYS):
        problems.append(f"{where}含有不认识的字段「{'、'.join(extra)}」")
    if item["source"] not in ITEM_SOURCES:
        problems.append(f"{where}的来源「{item['source']}」不在可选值内")
    if item["status"] not in ITEM_STATUSES:
        problems.append(f"{where}的状态「{item['status']}」不在可选值内")
    check, hint = rule
    if not check(item["value"]):
        problems.append(f"{where}{hint}")
    if "note" in item and not isinstance(item["note"], str):
        problems.append(f"{where}的备注应为文字")
    if "updated_at" in item and not isinstance(item["updated_at"], str):
        problems.append(f"{where}的修改时间应为文字")
    return problems


def _relation_problems(rel: Any, seen: set[str]) -> list[str]:
    if not isinstance(rel, dict):
        return ["关联关系应为对象"]
    rid = rel.get("id")
    # 带编号时后面空一格再接说明：「关联关系 r1a2 的目标字段…」。两端的叫法和界面一致：本表字段 → 目标表的目标字段
    where = f"关联关系 {rid} " if isinstance(rid, str) and rid else "关联关系"
    problems: list[str] = []
    if not (isinstance(rid, str) and rid.strip()) or "." in str(rid):
        problems.append(f"{where}缺少编号，或编号里含有句点")
    elif rid in seen:
        problems.append(f"{where}的编号重复")
    else:
        seen.add(rid)
    if extra := sorted(set(rel) - _RELATION_KEYS):
        problems.append(f"{where}含有不认识的字段「{'、'.join(extra)}」")
    cols, to_cols = rel.get("columns"), rel.get("to_columns")
    if not _str_list_ok(cols):
        problems.append(f"{where}的本表字段应为列名的列表")
    if not _text_ok(rel.get("to_table"), 255):
        problems.append(f"{where}缺少目标表")
    if not _str_list_ok(to_cols):
        problems.append(f"{where}的目标字段应为列名的列表")
    elif _str_list_ok(cols) and len(cols) != len(to_cols):
        problems.append(f"{where}两端的字段数不一致")
    if rel.get("cardinality") not in (*CARDINALITIES, None):
        problems.append(f"{where}的基数「{rel.get('cardinality')}」不在可选值内")
    coverage = rel.get("coverage")
    if coverage is not None and not (isinstance(coverage, (int, float)) and not isinstance(coverage, bool)
                                     and 0 <= coverage <= 1):
        problems.append(f"{where}的覆盖率应为 0 到 1 之间的数")
    if rel.get("source") not in ITEM_SOURCES:
        problems.append(f"{where}的来源「{rel.get('source')}」不在可选值内")
    if rel.get("status") not in ITEM_STATUSES:
        problems.append(f"{where}的状态「{rel.get('status')}」不在可选值内")
    if "note" in rel and not isinstance(rel["note"], str):
        problems.append(f"{where}的备注应为文字")
    if "updated_at" in rel and not isinstance(rel["updated_at"], str):
        problems.append(f"{where}的修改时间应为文字")
    return problems


def validate_notes(notes: Any) -> list[str]:
    """notes 的结构问题，逐条中文说明；合规时返回空列表。

    只认固定的一组字段：目录只记数据事实，不认识的字段（比如想塞一个计算公式进来）一律拒收。
    列名不核对是否存在于当前表结构：表结构会变，目录里先写着的列等结构同步后自然对上。
    """
    if not isinstance(notes, dict):
        return ["数据目录应为对象"]
    problems: list[str] = []
    if extra := sorted(set(notes) - set(TABLE_FIELDS) - {"columns", "relations"}):
        problems.append(f"数据目录含有不认识的字段「{'、'.join(extra)}」")
    for field_name in TABLE_FIELDS:
        if field_name in notes:
            problems += _item_problems(f"表的{TABLE_FIELD_LABEL[field_name]}", notes[field_name],
                                       _TABLE_VALUE_RULES[field_name])
    columns = notes.get("columns", {})
    if not isinstance(columns, dict):
        problems.append("列的目录应为以列名为键的对象")
        columns = {}
    for col, items in columns.items():
        if not isinstance(items, dict):
            problems.append(f"列 {col} 的目录应为对象")
            continue
        if extra := sorted(set(items) - set(COLUMN_FIELDS)):
            problems.append(f"列 {col} 含有不认识的字段「{'、'.join(extra)}」")
        for field_name in COLUMN_FIELDS:
            if field_name in items:
                problems += _item_problems(f"列 {col} 的{COLUMN_FIELD_LABEL[field_name]}", items[field_name],
                                           _COLUMN_VALUE_RULES[field_name])
    relations = notes.get("relations", [])
    if not isinstance(relations, list):
        problems.append("关联关系应为列表")
        relations = []
    seen: set[str] = set()
    for rel in relations:
        problems += _relation_problems(rel, seen)
    return problems


def _canonical(notes: Any) -> str:
    return json.dumps(notes, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def same_notes(a: Any, b: Any) -> bool:
    """两份 notes 内容是否相同（不计键的顺序）。"""
    return _canonical(a) == _canonical(b)


# ==========================================================================
# 存取
# ==========================================================================


@dataclass(frozen=True)
class CatalogEntry:
    """一张表的目录，连同版本和最近一次写入的署名、时间。"""

    table: str
    notes: dict[str, Any]
    version: int
    updated_by: str | None = None
    updated_at: datetime | None = None


def _entry(row: CatalogNote) -> CatalogEntry:
    return CatalogEntry(table=row.table_name, notes=copy.deepcopy(row.notes or {}), version=int(row.version or 0),
                        updated_by=row.updated_by, updated_at=row.updated_at)


async def _row(session: AsyncSession, source_id: str, table: str) -> CatalogNote | None:
    return (await session.execute(
        select(CatalogNote).where(CatalogNote.source_id == source_id, CatalogNote.table_name == table)
    )).scalar_one_or_none()


async def read_entry(session: AsyncSession, source_id: str, table: str) -> CatalogEntry | None:
    """一张表的目录；还没有时返回 None（版本按 0 算）。"""
    row = await _row(session, source_id, table)
    return _entry(row) if row is not None else None


async def read_catalog(session: AsyncSession, source_id: str) -> dict[str, CatalogEntry]:
    """这个数据源所有有目录的表：{表名: CatalogEntry}。"""
    rows = (await session.execute(select(CatalogNote).where(CatalogNote.source_id == source_id))).scalars()
    return {row.table_name: _entry(row) for row in rows}


async def write_entry(session: AsyncSession, source_id: str, table: str, notes: dict[str, Any], *,
                      if_version: int, actor: str | None) -> CatalogEntry:
    """整份写入一张表的目录（乐观锁），提交事务，返回写入后的目录。

    - if_version 是写入方读到的版本，还没有目录时传 0；和库里的不一致抛 CatalogConflict，什么都不写。
      比对和写入在同一条 UPDATE … WHERE version = ? 里完成：两个请求同时读到第 3 版，只有一个写得进去。
    - 内容和库里的完全相同时不写、不升版本：冻结进表结构快照的是「版本 + 内容」，空写一次也升版本，
      快照哈希就会无故变化（目录不变则快照不变）。
    - notes 先过 validate_notes，不合规抛 CatalogInvalid。

    这里不改写项的来源和状态：人工编辑记成 human / confirmed 的规则在 apply_human_edit 里，起草的
    合并规则在 merge_notes 里，调用方先算好再写。
    """
    if problems := validate_notes(notes):
        raise CatalogInvalid(problems)
    stored = copy.deepcopy(notes)
    row = await _row(session, source_id, table)
    current = int(row.version or 0) if row is not None else 0
    if if_version != current:
        raise CatalogConflict(table, current)
    if row is not None and same_notes(row.notes or {}, stored):
        return _entry(row)
    if row is None:
        row = CatalogNote(source_id=source_id, table_name=table, notes=stored, version=1, updated_by=actor)
        session.add(row)
        try:
            await session.commit()
        except IntegrityError as e:
            # 唯一约束 (source_id, table_name)：另一个请求抢先建了这张表的目录
            await session.rollback()
            raise CatalogConflict(table, 1) from e
        return _entry(row)
    result = await session.execute(
        update(CatalogNote)
        .where(CatalogNote.id == row.id, CatalogNote.version == current)
        .values(notes=stored, version=current + 1, updated_by=actor, updated_at=datetime.now(timezone.utc))
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        await session.rollback()
        raise CatalogConflict(table, current + 1)
    await session.commit()
    await session.refresh(row)
    return _entry(row)


def relation_id(table: str, columns: list[str], to_table: str, to_columns: list[str]) -> str:
    """关系的稳定编号：由两端的表和列算出来，同一条关系不论哪个来源、第几次起草都得到同一个编号。

    列按 (本表列, 被指向列) 成对排序后再算，复合外键列的书写顺序不影响编号；不区分大小写（外键约束
    和命名推断拿到的大小写可能不同）。编号里不含句点：审阅接口的路径按句点切分（relations.<编号>）。
    """
    pairs = sorted(zip((c.lower() for c in columns), (c.lower() for c in to_columns)))
    raw = json.dumps([table.lower(), to_table.lower(), pairs], ensure_ascii=False, separators=(",", ":"))
    return "r" + hashlib.sha256(raw.encode()).hexdigest()[:12]


# ==========================================================================
# 槽位：把 notes 摊平成「一项一个键」，合并、审阅、人工编辑都在这上面做
# ==========================================================================

#: 槽位键：("t", 字段) 表级项、("c", 列名, 字段) 列级项、("r", 关系编号) 关系
Slot = tuple[str, ...]

#: 关系里决定「是不是同一条、值有没有变」的字段（来源、状态、备注、时间之外的部分）
_RELATION_VALUE_KEYS = ("columns", "to_table", "to_columns", "cardinality", "coverage")


def _slots(notes: Mapping[str, Any] | None) -> dict[Slot, dict[str, Any]]:
    """notes → {槽位: 项}（深拷贝，不改入参）。顺序：表级项、列级项、关系，各自保持原来的顺序。"""
    out: dict[Slot, dict[str, Any]] = {}
    notes = notes or {}
    for name in TABLE_FIELDS:
        if isinstance(notes.get(name), dict):
            out[("t", name)] = copy.deepcopy(notes[name])
    columns = notes.get("columns") if isinstance(notes.get("columns"), dict) else {}
    for col, items in columns.items():
        for name in COLUMN_FIELDS:
            if isinstance(items, dict) and isinstance(items.get(name), dict):
                out[("c", col, name)] = copy.deepcopy(items[name])
    relations = notes.get("relations") if isinstance(notes.get("relations"), list) else []
    for rel in relations:
        if isinstance(rel, dict) and rel.get("id"):
            out[("r", str(rel["id"]))] = copy.deepcopy(rel)
    return out


def _assemble(slots: Mapping[Slot, dict[str, Any]]) -> dict[str, Any]:
    """槽位 → notes。空的列、空的关系列表不留：目录内容相同则序列化结果相同（冻结的哈希靠它）。"""
    notes: dict[str, Any] = {}
    columns: dict[str, dict[str, Any]] = {}
    relations: list[dict[str, Any]] = []
    for key, item in slots.items():
        if key[0] == "t":
            notes[key[1]] = item
        elif key[0] == "c":
            columns.setdefault(key[1], {})[key[2]] = item
        else:
            relations.append(item)
    if columns:
        notes["columns"] = columns
    if relations:
        notes["relations"] = relations
    return notes


def _value_of(key: Slot, item: Mapping[str, Any]) -> Any:
    """一项的「值」：比较人工有没有改动时只看它。"""
    if key[0] == "r":
        return {k: item.get(k) for k in _RELATION_VALUE_KEYS}
    return item.get("value")


def _core(item: Mapping[str, Any]) -> dict[str, Any]:
    """去掉修改时间的项：判断起草结果和已有的是不是一回事。"""
    return {k: v for k, v in item.items() if k != "updated_at"}


def visible_notes(notes: Mapping[str, Any] | None) -> dict[str, Any]:
    """去掉被驳回的项和关系。给模型看、冻结进证据、将来的检查都只用这一份。"""
    return _assemble({k: v for k, v in _slots(notes).items() if v.get("status") != "rejected"})


def status_counts(notes: Mapping[str, Any] | None) -> dict[str, int]:
    """各状态的项数（表级项、列级项、关系都算），四种状态都有键。"""
    counts = dict.fromkeys(ITEM_STATUSES, 0)
    for item in _slots(notes).values():
        if item.get("status") in counts:
            counts[item["status"]] += 1
    return counts


# ==========================================================================
# 合并：起草结果并入已有目录
# ==========================================================================

#: 来源的可信程度。不同来源对同一个槽位各有说法时，高的留下；同级（模型起草和命名推断）先到先得
_SOURCE_RANK = {"human": 5, "fk": 4, "profile": 3, "comment": 2, "llm": 1, "name": 1}
#: 人工定过的状态：起草永远不碰
_LOCKED = ("confirmed", "rejected")


@dataclass(frozen=True)
class MergeStats:
    """一次合并新增、更新、删除了几项。"""

    added: int = 0
    updated: int = 0
    removed: int = 0

    def __add__(self, other: "MergeStats") -> "MergeStats":
        return MergeStats(self.added + other.added, self.updated + other.updated, self.removed + other.removed)


def merge_notes(existing: Mapping[str, Any] | None, draft: Mapping[str, Any] | None, *,
                covered: Iterable[str] = (), at: str | None = None) -> tuple[dict[str, Any], MergeStats]:
    """把起草结果并入已有目录，返回 (新目录, 计数)。不改入参。

    规则（逐个槽位）：
    - 已有项是 confirmed 或 rejected：不动。驳回的留着，同一项下次起草就不会再冒出来。
    - 起草结果里没有这一项：已有项的来源在 covered 里（这一轮完整算过这个来源）就删掉，否则留着。
      外键约束被删掉以后，原来那条「有确证」的关系不能还挂着；而这一轮没调模型，就不能因此删掉模型起草的项。
    - 同一来源：值或状态有变化就更新。
    - 不同来源：来源更可信的（_SOURCE_RANK）顶掉不如它的，反过来不行。

    新增和更新的项盖上 at（缺省为当前时间）作为 updated_at；内容没变的项保持原样，修改时间也不动。
    """
    at = at or now_iso()
    covered = set(covered)
    old, new = _slots(existing), _slots(draft)
    out: dict[Slot, dict[str, Any]] = {}
    added = updated = removed = 0
    for key, item in old.items():
        cand = new.get(key)
        if item.get("status") in _LOCKED:
            out[key] = item
        elif cand is None:
            if item.get("source") in covered:
                removed += 1
            else:
                out[key] = item
        elif (cand.get("source") == item.get("source")
              or _SOURCE_RANK.get(cand.get("source"), 0) > _SOURCE_RANK.get(item.get("source"), 0)):
            if _core(cand) != _core(item):
                out[key] = {**_core(cand), "updated_at": at}
                updated += 1
            else:
                out[key] = item
        else:
            out[key] = item
    for key, cand in new.items():
        if key not in old:
            out[key] = {**_core(cand), "updated_at": at}
            added += 1
    return _assemble(out), MergeStats(added, updated, removed)


# ==========================================================================
# 单项审阅与人工编辑
# ==========================================================================

#: 单项审阅的操作：confirm 确认、reject 驳回、reset 撤销审阅（回到来源的初始状态；人工填写的项直接删掉）
REVIEW_ACTIONS = ("confirm", "reject", "reset")


class CatalogPathError(ValueError):
    """审阅的路径或操作不对：路径写错、项不存在、操作不认识。message 给人看。"""


def parse_path(path: str) -> Slot:
    """审阅路径 → 槽位。

    写法：表级项直接写字段名（grain）；列级项 columns.<列名>.<字段>（列名里可以有句点，按最后一个句点切出
    字段）；关系 relations.<编号>。
    """
    path = (path or "").strip()
    if path in TABLE_FIELDS:
        return ("t", path)
    if path.startswith("columns."):
        col, _, name = path[len("columns."):].rpartition(".")
        if col and name in COLUMN_FIELDS:
            return ("c", col, name)
    if path.startswith("relations.") and path[len("relations."):]:
        return ("r", path[len("relations."):])
    raise CatalogPathError("无法识别要审阅的项，请重新载入后再试")


def describe_slot(key: Slot) -> str:
    """槽位的中文说法：「表的粒度」「列 amount 的度量类型」「关联关系 r1a2b3」。"""
    if key[0] == "t":
        return f"表的{TABLE_FIELD_LABEL.get(key[1], key[1])}"
    if key[0] == "c":
        return f"列 {key[1]} 的{COLUMN_FIELD_LABEL.get(key[2], key[2])}"
    return f"关联关系 {key[1]}"


def review_item(notes: Mapping[str, Any] | None, path: str, action: str, *, at: str | None = None) -> dict[str, Any]:
    """对一项做审阅，返回新目录（不改入参）。路径不对、项不存在、操作不认识都抛 CatalogPathError。"""
    if action not in REVIEW_ACTIONS:
        raise CatalogPathError("不支持这种审阅操作，只能确认、驳回或撤销审阅")
    key = parse_path(path)
    slots = _slots(notes)
    item = slots.get(key)
    if item is None:
        raise CatalogPathError(f"找不到{describe_slot(key)}，可能已被修改，请重新载入")
    if action == "reset" and item.get("source") == "human":
        # 人工填写的项没有「推断时的样子」可回：撤销就是删掉，下次起草可以重新提出
        del slots[key]
        return _assemble(slots)
    item["status"] = {"confirm": "confirmed", "reject": "rejected"}.get(action) or initial_status(item.get("source"))
    item["updated_at"] = at or now_iso()
    return _assemble(slots)


def _normalize_submitted(submitted: Mapping[str, Any], table: str) -> dict[str, Any]:
    """人工提交的目录：补上没写的来源和状态（下面会按规则改写），关系编号按两端重算。"""
    out = copy.deepcopy(dict(submitted))

    def fill(item: Any) -> None:
        if isinstance(item, dict) and "value" in item:
            item.setdefault("source", "human")
            item.setdefault("status", "confirmed")

    for name in TABLE_FIELDS:
        fill(out.get(name))
    columns = out.get("columns") if isinstance(out.get("columns"), dict) else {}
    for items in columns.values():
        for name in COLUMN_FIELDS:
            fill(items.get(name) if isinstance(items, dict) else None)
    relations = out.get("relations") if isinstance(out.get("relations"), list) else []
    for rel in relations:
        if not isinstance(rel, dict):
            continue
        rel.setdefault("source", "human")
        rel.setdefault("status", "confirmed")
        rel.setdefault("cardinality", None)
        rel.setdefault("coverage", None)
        cols, to_table, to_cols = rel.get("columns"), rel.get("to_table"), rel.get("to_columns")
        if _str_list_ok(cols) and isinstance(to_table, str) and to_table and _str_list_ok(to_cols):
            # 编号由两端决定：改了指向就是另一条关系，沿用旧编号会让「同一条关系同一个编号」失效
            rel["id"] = relation_id(table, cols, to_table, to_cols)
    return out


def apply_human_edit(existing: Mapping[str, Any] | None, submitted: Mapping[str, Any], *, table: str,
                     at: str | None = None) -> dict[str, Any]:
    """人工提交的整份目录 → 要写入的目录（不改入参）。

    - 值改过的项、新填的项：记为 human / confirmed。提交里写的来源和状态不作数——客户端不能冒充外键约束
      或数据剖析。
    - 值没动的项：保持原来的来源和状态；提交里把状态改成确认、驳回或来源的初始状态的，照改（等同单项审阅）；
      改成别的状态的不认（不能把推断改成「有确证」）。
    - 已有、但提交里没有的项：删掉。
    - 关系编号按两端重算（_normalize_submitted）。

    结构不合规抛 CatalogInvalid。
    """
    if not isinstance(submitted, Mapping):
        raise CatalogInvalid(["数据目录应为对象"])
    normalized = _normalize_submitted(submitted, table)
    if problems := validate_notes(normalized):
        raise CatalogInvalid(problems)
    at = at or now_iso()
    old, new = _slots(existing), _slots(normalized)
    out: dict[Slot, dict[str, Any]] = {}
    for key, item in new.items():
        prev = old.get(key)
        if prev is None or _value_of(key, item) != _value_of(key, prev):
            fresh = {k: v for k, v in item.items() if k != "updated_at"}
            fresh.update(source="human", status="confirmed", updated_at=at)
            out[key] = fresh
            continue
        kept = dict(prev)
        status = item.get("status")
        if status != prev.get("status") and status in ("confirmed", "rejected", initial_status(prev.get("source"))):
            kept["status"] = status
            kept["updated_at"] = at
        if item.get("note") != prev.get("note"):
            if item.get("note"):
                kept["note"] = item["note"]
            else:
                kept.pop("note", None)
            kept["updated_at"] = at
        out[key] = kept
    return _assemble(out)


async def review_entry(session: AsyncSession, source_id: str, table: str, path: str, action: str, *,
                       if_version: int, actor: str | None) -> CatalogEntry:
    """单项审阅并写入（乐观锁同 write_entry）。版本不符抛 CatalogConflict，路径不对抛 CatalogPathError。"""
    entry = await read_entry(session, source_id, table)
    current = entry.version if entry else 0
    if if_version != current:
        raise CatalogConflict(table, current)
    notes = review_item(entry.notes if entry else {}, path, action)
    return await write_entry(session, source_id, table, notes, if_version=current, actor=actor)


async def save_human_edit(session: AsyncSession, source_id: str, table: str, submitted: Mapping[str, Any], *,
                          if_version: int, actor: str | None) -> CatalogEntry:
    """人工提交整份目录并写入（规则见 apply_human_edit，乐观锁同 write_entry）。"""
    entry = await read_entry(session, source_id, table)
    current = entry.version if entry else 0
    if if_version != current:
        raise CatalogConflict(table, current)
    notes = apply_human_edit(entry.notes if entry else {}, submitted, table=table)
    return await write_entry(session, source_id, table, notes, if_version=current, actor=actor)


# ==========================================================================
# 目录修改提案：对话里的纠正（阶段 4A）
#
# 用户在助手里说出一条数据事实（「status=9 表示作废」），模型发一条 catalog_patch；这里逐项核对、算出改前和
# 改后，转给前端做成卡片。**提案本身从不落库**：要人点「保存到数据目录」，带着读到的版本走 save_patch，
# 改动记为人工填写、已确认——和人工编辑同一条规矩，只是改动由模型起草。
# ==========================================================================

#: 新增关联关系的路径：relations.new，值写两端；编号按两端算（relation_id），同一条关系不论谁提都是同一个编号
NEW_RELATION_PATH = "relations.new"
#: 一条提案最多几项：用户说的是一两条事实，几十项的「提案」多半是模型在重写整张表
PATCH_MAX_CHANGES = 20
_REASON_MAX = 500
#: 看起来像计算公式的文字：「转化率=下单数/访问数」「= SUM(amount)」。公式进口径卡，目录只记数据事实。
#: 不认减号：「上线日期=2024-01-01」不是公式。有效记录条件本来就是 SQL 条件，不查它
_FORMULA = re.compile(r"[=＝][^=<>!]*?[+*/×÷]|\b(?:sum|count|avg|average)\s*\(", re.IGNORECASE)
_FORMULA_CHECKED = frozenset({("t", "label"), ("t", "description"), ("t", "grain"), ("c", "label"),
                              ("c", "meaning"), ("c", "unit"), ("c", "codes")})


@dataclass(frozen=True)
class PatchChange:
    """提案里的一项，核对过的。

    - path：规范化后的路径（新增关系写成 relations.<编号>）；
    - before / before_status：当前目录里这一项的值和状态（被驳回的和没有的都算没有，为 None）；
    - after：保存后的值（码值是补充后的完整对照）；value：要交回保存的原样取值（码值只有补充的那几个，
      409 之后在最新的目录上重新补）；
    - state：change 值有变化；confirm 值相同、但还不是已确认（保存即确认）；same 已经是这个值且已确认。
    """

    path: str
    before: Any
    before_status: str | None
    after: Any
    value: Any
    reason: str
    state: str

    def as_dict(self) -> dict[str, Any]:
        return {"path": self.path, "before": self.before, "before_status": self.before_status, "after": self.after,
                "value": self.value, "reason": self.reason, "state": self.state}


@dataclass(frozen=True)
class PatchPlan:
    """一条提案核对的结果：合法的项（changes）和不合法的项的中文说明（problems）。"""

    changes: list[PatchChange]
    problems: list[str]


def _column_names(meta: Mapping[str, Any] | None) -> set[str] | None:
    """表结构里的列名；不知道（表结构里没有这张表）时为 None，不核对。"""
    if not isinstance(meta, Mapping):
        return None
    return {str(c.get("name")) for c in meta.get("columns") or [] if isinstance(c, Mapping) and c.get("name")}


def _formula_like(key: Slot, value: Any) -> bool:
    if key[:1] + key[-1:] not in _FORMULA_CHECKED:
        return False
    texts = list(value.values()) if isinstance(value, dict) else [value]
    return any(isinstance(t, str) and _FORMULA.search(t) for t in texts)


def _patch_relation(table: str, raw: Any, path_id: str | None, tables: Mapping[str, Any] | None,
                    columns: set[str] | None) -> tuple[dict[str, Any] | None, str | None]:
    """关联关系的取值 → (关系的值部分, 问题)。值写两端（columns、to_table、to_columns），基数可选。"""
    if not isinstance(raw, Mapping):
        return None, "关联关系的值应写明本表字段、目标表和目标字段"
    to_table = raw.get("to_table")
    if isinstance(to_table, str) and tables is not None:
        to_table = resolve_table_name({"tables": tables}, to_table)
        if to_table is None:
            return None, f"关联关系的目标表「{raw.get('to_table')}」不在数据源的表结构里"
    value = {"columns": raw.get("columns"), "to_table": to_table, "to_columns": raw.get("to_columns"),
             "cardinality": raw.get("cardinality"), "coverage": None}
    probe = {"id": "probe", **value, "source": "human", "status": "confirmed"}
    if problems := _relation_problems(probe, set()):
        return None, problems[0].replace("关联关系 probe ", "关联关系")
    if columns is not None and (missing := [c for c in value["columns"] if c not in columns]):
        return None, f"关联关系的本表字段「{'、'.join(missing)}」不在表结构里"
    target = _column_names((tables or {}).get(to_table)) if tables is not None else None
    if target is not None and (missing := [c for c in value["to_columns"] if c not in target]):
        return None, f"关联关系的目标字段「{'、'.join(missing)}」不在 {to_table} 的表结构里"
    rid = relation_id(table, value["columns"], to_table, value["to_columns"])
    if path_id is not None and path_id != rid:
        return None, f"关联关系 {path_id} 的两端与取值不一致：要改指向，请驳回原关系后再新增"
    return value, None


def plan_patch(notes: Mapping[str, Any] | None, changes: Any, *, table: str,
               tables: Mapping[str, Any] | None = None) -> PatchPlan:
    """逐项核对一条目录修改提案，算出改前、改后。不改入参、不抛异常：不合法的项写进 problems。

    changes 是 [{path, value, reason?}]。path 用审阅接口的写法：表级项写字段名，列级项 columns.<列名>.<字段>，
    关系 relations.<编号>；新增关系写 relations.new。tables 是数据源 schema_cache 的 tables：给了就核对列名、
    业务主键、业务日期列、关系两端在不在表结构里（模型会照着名字编列名）；不给不核对。

    规则：
    - 取值格式和 validate_notes 同一套（_TABLE_VALUE_RULES / _COLUMN_VALUE_RULES / _relation_problems）。
    - 码值是补充：在现有（没被驳回的）码值上加或改给出的码，不整份替换——「status=9 表示作废」不该把已确认的
      其余码值删掉。
    - 看起来像计算公式的文字不收（_FORMULA）：公式进口径卡。
    - 同一个路径出现两次只认第一次。
    """
    meta = (tables or {}).get(table) if tables is not None else None
    columns = _column_names(meta)
    slots = _slots(notes)
    out: list[PatchChange] = []
    problems: list[str] = []
    seen: set[str] = set()
    if not isinstance(changes, list) or not changes:
        return PatchPlan([], ["提案里没有要修改的项"])
    if len(changes) > PATCH_MAX_CHANGES:
        problems.append(f"提案里有 {len(changes)} 项，只核对前 {PATCH_MAX_CHANGES} 项")
        changes = changes[:PATCH_MAX_CHANGES]
    for raw in changes:
        if not isinstance(raw, Mapping) or not isinstance(raw.get("path"), str) or "value" not in raw:
            problems.append("提案里有一项不是「路径 + 取值」的形式")
            continue
        path = raw["path"].strip()
        value = copy.deepcopy(raw["value"])
        reason = raw.get("reason")
        reason = reason.strip()[:_REASON_MAX] if isinstance(reason, str) else ""
        if path == NEW_RELATION_PATH:
            rel, problem = _patch_relation(table, value, None, tables, columns)
            if problem:
                problems.append(problem)
                continue
            key: Slot = ("r", relation_id(table, rel["columns"], rel["to_table"], rel["to_columns"]))
            value = rel
        else:
            try:
                key = parse_path(path)
            except CatalogPathError:
                problems.append(f"无法识别要修改的项（{path[:60]}）")
                continue
            if key[0] == "r":
                rel, problem = _patch_relation(table, value, key[1], tables, columns)
                if problem:
                    problems.append(problem)
                    continue
                value = rel
        where = describe_slot(key) if key[0] != "r" else "关联关系"
        if key[0] == "t":
            check, hint = _TABLE_VALUE_RULES[key[1]]
            if not check(value):
                problems.append(f"{where}{hint}")
                continue
            if columns is not None:
                cited = value if key[1] == "keys" else [value["column"]] if key[1] == "business_date" else []
                if missing := [c for c in cited if c not in columns]:
                    problems.append(f"{where}里的「{'、'.join(missing)}」不在表结构里")
                    continue
        elif key[0] == "c":
            if columns is not None and key[1] not in columns:
                problems.append(f"列 {key[1]} 不在表结构里")
                continue
            check, hint = _COLUMN_VALUE_RULES[key[2]]
            if not check(value):
                problems.append(f"{where}{hint}")
                continue
        if _formula_like(key, value):
            problems.append(f"{where}看起来是计算公式：公式请写进口径卡，数据目录只记数据事实")
            continue
        norm = f"relations.{key[1]}" if key[0] == "r" else path
        if norm in seen:
            problems.append(f"{where}在提案里出现了两次，只采用第一次")
            continue
        seen.add(norm)
        prev = slots.get(key)
        live = prev if prev is not None and prev.get("status") != "rejected" else None
        before = _value_of(key, live) if live is not None else None
        after = value
        if key[0] == "c" and key[2] == "codes" and isinstance(before, dict):
            after = {**before, **value}
        if before is not None and before == after:
            state = "same" if live.get("status") == "confirmed" else "confirm"
        else:
            state = "change"
        out.append(PatchChange(path=norm, before=copy.deepcopy(before),
                               before_status=live.get("status") if live is not None else None,
                               after=after, value=value, reason=reason, state=state))
    return PatchPlan(out, problems)


def apply_patch(notes: Mapping[str, Any] | None, changes: Any, *, table: str,
                tables: Mapping[str, Any] | None = None, at: str | None = None) -> dict[str, Any]:
    """把一条提案并进目录，返回新目录（不改入参）。有一项不合法就整条不收，抛 CatalogInvalid。

    值有变化的项记为人工填写、已确认（和人工编辑同一条规矩：改动出自人的确认，不是模型起草）；值没变的只把
    状态改成已确认、来源不变（等同单项确认）；已经确认过的原样不动。
    """
    plan = plan_patch(notes, changes, table=table, tables=tables)
    if plan.problems:
        raise CatalogInvalid(plan.problems)
    at = at or now_iso()
    slots = _slots(notes)
    for change in plan.changes:
        key = parse_path(change.path)
        if change.state == "same":
            continue
        prev = slots.get(key)
        if change.state == "confirm" and prev is not None:
            slots[key] = {**prev, "status": "confirmed", "updated_at": at}
            continue
        if key[0] == "r":
            slots[key] = {"id": key[1], **change.after, "source": "human", "status": "confirmed", "updated_at": at}
        else:
            slots[key] = {"value": change.after, "source": "human", "status": "confirmed", "updated_at": at}
    out = _assemble(slots)
    if problems := validate_notes(out):
        raise CatalogInvalid(problems)
    return out


async def save_patch(session: AsyncSession, source_id: str, table: str, changes: Any, *, if_version: int,
                     actor: str | None, tables: Mapping[str, Any] | None = None) -> CatalogEntry:
    """保存一条目录修改提案（规则见 apply_patch，乐观锁同 write_entry）。版本不符抛 CatalogConflict。"""
    entry = await read_entry(session, source_id, table)
    current = entry.version if entry else 0
    if if_version != current:
        raise CatalogConflict(table, current)
    notes = apply_patch(entry.notes if entry else {}, changes, table=table, tables=tables)
    return await write_entry(session, source_id, table, notes, if_version=current, actor=actor)


# ==========================================================================
# 起草：数据库注释、外键约束、命名推断（只看探查缓存，不发数据库查询）
# ==========================================================================


@dataclass(frozen=True)
class TableDraft:
    """一张表的起草结果。covered：这一轮完整算过的来源（merge_notes 据此删掉这些来源里已经没有的旧项）。"""

    notes: dict[str, Any]
    covered: frozenset[str] = frozenset()


def system_notes_source(source: Any) -> bool:
    """这个源的表说明、列说明是不是系统生成的（导入表格）。

    上传表格建的源（origin=upload）：简单导入时未规整的表写着 UNSHAPED_NOTE，按配方导入时表和列的说明
    由 recipe_notes 按核对结果生成（schema_cache 顶层 import_mode=recipe）。这些说明有严格的规矩（跟着
    核对结果走、不带数字和坐标），目录不能重复它们，也不能和它们矛盾：起草时不拷进中文名和含义，渲染时
    保留说明、只追加说明没有覆盖的项（render_table_notes）。
    """
    if (getattr(source, "origin", None) or "manual") == "upload":
        return True
    return (getattr(source, "schema_cache", None) or {}).get("import_mode") == "recipe"


def resolve_table_name(schema_cache: Mapping[str, Any] | None, name: str) -> str | None:
    """在表结构里找表，返回 schema_cache["tables"] 的键。全名、只有表名、大小写不一致都认。"""
    tables = (schema_cache or {}).get("tables") or {}
    if name in tables:
        return name
    short = name.rsplit(".", 1)[-1]
    if short in tables:
        return short
    lowered = short.lower()
    return next((n for n in tables if n.lower() == lowered), None)


#: 数据库注释多长、带不带句读，决定它是「名字」还是「解释」
_LABEL_MAX = 24
_SENTENCE_MARKS = re.compile(r"[，。；;,.!?！？：:\n]")


def _comment_is_label(text: str) -> bool:
    """短、不带句读的注释是名字（「实收金额」），否则是一段解释（「状态：1 表示有效…」）。"""
    return len(text) <= _LABEL_MAX and not _SENTENCE_MARKS.search(text)


def _cardinality(meta: Mapping[str, Any], columns: list[str]) -> str:
    """本表这几列指向别的表时的基数：这几列恰好是本表的主键或某个唯一约束，就是一对一，否则多对一。"""
    wanted = {c.lower() for c in columns}
    keys = [meta.get("primary_key") or []] + [u for u in meta.get("unique") or [] if isinstance(u, list)]
    return "one_to_one" if any(wanted == {c.lower() for c in k} for k in keys if k) else "many_to_one"


def _relation(table: str, columns: list[str], to_table: str, to_columns: list[str], source: str, *,
              cardinality: str | None) -> dict[str, Any]:
    return {"id": relation_id(table, columns, to_table, to_columns), "columns": list(columns), "to_table": to_table,
            "to_columns": list(to_columns), "cardinality": cardinality, "coverage": None, "source": source,
            "status": initial_status(source)}


def fk_relations(schema_cache: Mapping[str, Any] | None, table: str) -> list[dict[str, Any]] | None:
    """外键约束 → 关系（fk / verified）。缓存里没有 foreign_keys（升级前探查的）返回 None：不知道有没有外键。

    外键没写被指向的列（SQLite 的 REFERENCES t 可以省略列）时取被指向表的主键；列数对不上的跳过。
    """
    tables = (schema_cache or {}).get("tables") or {}
    meta = tables.get(table)
    if not isinstance(meta, dict) or not isinstance(meta.get("foreign_keys"), list):
        return None
    out: dict[str, dict[str, Any]] = {}
    for fk in meta["foreign_keys"]:
        if not isinstance(fk, dict):
            continue
        cols, to_table = list(fk.get("columns") or []), fk.get("to_table")
        to_cols = list(fk.get("to_columns") or [])
        if not cols or not isinstance(to_table, str) or not to_table:
            continue
        if not to_cols:
            to_cols = list((tables.get(to_table) or {}).get("primary_key") or [])
        if len(to_cols) != len(cols):
            continue
        rel = _relation(table, cols, to_table, to_cols, "fk", cardinality=_cardinality(meta, cols))
        out.setdefault(rel["id"], rel)
    return list(out.values())


# ---- 命名推断

#: xxx_id（不分大小写）、xxxId、xxxID。没有分隔的 paid、uuid、PAID 都不算
_SNAKE_ID = re.compile(r"^(?P<stem>.+?)_id$", re.IGNORECASE)
_CAMEL_ID = re.compile(r"^(?P<stem>.*[a-z0-9])(?:Id|ID)$")
_WORDS = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+")


def _words(name: str) -> list[str]:
    """名字切成小写的词：下划线和驼峰都是分隔（memberTag、member_tag、MEMBER_TAG 都是 member + tag）。"""
    return [w.lower() for part in name.split("_") for w in _WORDS.findall(part)]


def _singulars(word: str) -> set[str]:
    """一个词可能的单数形式（含它自己）。两边都取这个集合、有交集就算同一个词：categories 和 category、
    courses 和 course、status 和 statuses 都对得上，不靠一套「复数变单数」的规则硬猜。"""
    out = {word}
    if word.endswith("ies") and len(word) > 3:
        out.add(word[:-3] + "y")
    if word.endswith("es") and len(word) > 2:
        out.add(word[:-2])
    if word.endswith("s") and not word.endswith("ss") and len(word) > 1:
        out.add(word[:-1])
    return out


def _name_index(tables: Mapping[str, Any]) -> dict[tuple[str, str], set[str]]:
    """{(前面几个词拼起来, 最后一个词的单数形式): 表名}。"""
    index: dict[tuple[str, str], set[str]] = {}
    for name in tables:
        words = _words(name)
        if not words:
            continue
        prefix = "".join(words[:-1])
        for form in _singulars(words[-1]):
            index.setdefault((prefix, form), set()).add(name)
    return index


def _type_family(type_name: str | None) -> str | None:
    """列类型的大类：数字、文字、时间；认不出来返回 None（不据此排除）。"""
    t = (type_name or "").upper()
    if not t:
        return None
    if any(k in t for k in ("CHAR", "TEXT", "CLOB", "STRING", "UUID")):
        return "text"
    if any(k in t for k in ("INT", "NUMBER", "NUMERIC", "DECIMAL", "SERIAL", "REAL", "FLOAT", "DOUBLE")):
        return "number"
    if any(k in t for k in ("DATE", "TIME")):
        return "time"
    return None


def _id_stem(column: str) -> str | None:
    m = _SNAKE_ID.match(column) or _CAMEL_ID.match(column)
    return m.group("stem") if m else None


def infer_name_relations(schema_cache: Mapping[str, Any] | None, table: str, *,
                         _index: dict[tuple[str, str], set[str]] | None = None) -> list[dict[str, Any]]:
    """按列名推断关系（name / proposed）：xxx_id、xxxId、xxxID 对上表名的单复数、驼峰和下划线变体，指向那张表的主键。

    推不准就不推。以下情况一律放过：
    - 列上已经有外键约束（外键说了算）；
    - 词根太短（x_id）；
    - 没有对得上的表，或者对得上的表不止一张（guide 和 guides 都在）；
    - 对上的是本表自己（employees.employee_id 是工号，不是自引用）；
    - 被指向的表没有主键，或主键不止一列；
    - 两边的类型大类不同（文字列指向整数主键）。
    """
    tables = (schema_cache or {}).get("tables") or {}
    meta = tables.get(table)
    if not isinstance(meta, dict):
        return []
    index = _index if _index is not None else _name_index(tables)
    constrained = {c.lower() for fk in meta.get("foreign_keys") or [] if isinstance(fk, dict)
                   for c in fk.get("columns") or []}
    out: list[dict[str, Any]] = []
    for col in meta.get("columns") or []:
        name = str(col.get("name") or "")
        stem = _id_stem(name)
        if stem is None or name.lower() in constrained:
            continue
        words = _words(stem)
        if not words or len("".join(words)) < 2:
            continue
        prefix = "".join(words[:-1])
        candidates: set[str] = set()
        for form in _singulars(words[-1]):
            candidates |= index.get((prefix, form), set())
        if len(candidates) != 1:
            continue
        target = next(iter(candidates))
        if target == table:
            continue
        target_meta = tables.get(target) or {}
        pk = list(target_meta.get("primary_key") or [])
        if len(pk) != 1:
            continue
        pk_type = next((c.get("type") for c in target_meta.get("columns") or [] if c.get("name") == pk[0]), None)
        mine, theirs = _type_family(col.get("type")), _type_family(pk_type)
        if mine and theirs and mine != theirs:
            continue
        out.append(_relation(table, [name], target, pk, "name", cardinality=_cardinality(meta, [name])))
    return out


def draft_structure(source: Any, table: str) -> TableDraft:
    """只看表结构就能起草的部分：数据库注释、外键约束、命名推断。table 必须是 schema_cache["tables"] 的键。

    - 数据库注释 → 中文名或含义（comment / proposed）：短而不带句读的是名字，记进表或列的中文名；否则是
      一段解释，记进表的说明或列的含义。导入表格的源例外（system_notes_source）：说明是系统生成的，不拷。
    - 外键约束 → 关系（fk / verified）。
    - 命名推断 → 关系（name / proposed）。
    """
    cache = getattr(source, "schema_cache", None) or {}
    meta = (cache.get("tables") or {}).get(table)
    if not isinstance(meta, dict):
        raise KeyError(table)
    notes: dict[str, Any] = {}
    if not system_notes_source(source):
        comment = (meta.get("comment") or "").strip()
        if comment:
            notes["label" if _comment_is_label(comment) else "description"] = make_item(comment, "comment")
        columns: dict[str, dict[str, Any]] = {}
        for col in meta.get("columns") or []:
            text = (col.get("comment") or "").strip()
            if text:
                columns[col["name"]] = {("label" if _comment_is_label(text) else "meaning"): make_item(text, "comment")}
        if columns:
            notes["columns"] = columns
    fks = fk_relations(cache, table)
    relations = list(fks or [])
    seen = {r["id"] for r in relations}
    relations += [r for r in infer_name_relations(cache, table) if r["id"] not in seen]
    if relations:
        notes["relations"] = relations
    covered = {"comment", "name"} | ({"fk"} if fks is not None else set())
    return TableDraft(notes=notes, covered=frozenset(covered))


# ==========================================================================
# 起草：模型（可选，按表分批，结构化输出）
# ==========================================================================

#: 每批最多几张表、合计多少列。一批太大模型容易漏表、输出被截断；太小则调用次数多
MODEL_BATCH_TABLES = 4
MODEL_BATCH_COLUMNS = 120
#: 同时发出的批数
MODEL_CONCURRENCY = 3
#: 一批最多等多久
MODEL_TIMEOUT_S = 120
#: 输出额度。一批四张表的目录远用不完；给太大的额度有的服务方会直接拒收
MODEL_MAX_TOKENS = 8192
#: 不指定表时起草使用次数最多的前几张
DRAFT_DEFAULT_TABLES = 20


class CatalogModelUnavailable(RuntimeError):
    """没有可用于起草的模型（没有接入、停用、只有演示接入、缺 key）。message 给人看；在调用模型之前判定。"""


CATALOG_DRAFT_SYSTEM = """你在为一个业务数据库编写数据目录：说明每张表、每一列在业务上是什么。只根据给出的表结构推断，\
不要编造不存在的表和列。

要写的内容：
- label：表的中文名，简短（例如「入园记录」）。
- grain：一行代表什么（例如「每张门票每次检票一行」）。
- kind：表类型，fact（事实表，记录业务事件）、dimension（维度表，描述对象）、snapshot（快照表，某一时点的状态）、\
log（日志表）、config（配置表）之一。
- keys：业务主键，即在业务上唯一确定一行的列（可以和数据库主键不同），写列名。
- business_date：业务日期按哪一列算，rule 写一句规则，timezone 写时区（不确定就留空）。
- columns：每一列的 label（中文名）、meaning（含义，一句话）、unit（单位，例如「元」「人」，没有单位就留空）、\
measure（度量类型：flow 流量，可跨期加总；stock 存量，时点值，不能跨期加总；ratio 比率；identifier 标识；\
status 状态；attribute 属性）。

规矩：
- 只写数据事实，不写计算公式，不写指标怎么算。
- 拿不准的字段留空，不要猜。列名、表名照原样写，不要翻译或改写。
- 标着「系统说明」的内容是导入时按核对结果生成的，以它为准：不要重复，也不要与之矛盾。
- 标着「已确认」的目录项由人工确认过，以它为准。"""

CATALOG_DRAFT_PROMPT = "请为下面 {n} 张表编写数据目录，每张表输出一项，table 写表名。\n\n{tables}"

#: 拼表结构描述用的固定片段（写给模型）
CATALOG_DRAFT_PROMPT_LABELS = {
    "table": "表 {name}",
    "view": "表 {name}（视图）",
    "comment": "数据库注释：{text}",
    "system": "系统说明：{text}",
    "columns": "列：",
    "column": "- {name} {type}",
    "pk": "主键",
    "not_null": "非空",
    "fk": "外键 → {target}",
    "col_comment": "注释：{text}",
    "col_system": "系统说明：{text}",
    "unique": "唯一约束：{cols}",
    "confirmed": "已确认的目录项：",
}

CATALOG_DRAFT_PROMPT_SCHEMA: dict[str, Any] = {
    "title": "catalog_draft",
    "description": "数据目录草稿，每张表一项",
    "type": "object",
    "properties": {
        "tables": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "table": {"type": "string", "description": "表名，照原样写"},
                    "label": {"type": "string", "description": "表的中文名"},
                    "grain": {"type": "string", "description": "一行代表什么"},
                    "kind": {"type": "string", "enum": list(TABLE_KINDS)},
                    "keys": {"type": "array", "items": {"type": "string"}, "description": "业务主键的列名"},
                    "business_date": {
                        "type": "object",
                        "properties": {"column": {"type": "string"}, "rule": {"type": "string"},
                                       "timezone": {"type": "string"}},
                        "required": ["column"],
                    },
                    "columns": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string", "description": "列名，照原样写"},
                                "label": {"type": "string"}, "meaning": {"type": "string"},
                                "unit": {"type": "string"},
                                "measure": {"type": "string", "enum": list(COLUMN_MEASURES)},
                            },
                            "required": ["name"],
                        },
                    },
                },
                "required": ["table"],
            },
        },
    },
    "required": ["tables"],
}

_P = CATALOG_DRAFT_PROMPT_LABELS


async def resolve_draft_model(session: AsyncSession) -> tuple[Any, str]:
    """(模型, 模型 id)：用设置里助手的模型，关掉思考、温度 0。没有可用的模型抛 CatalogModelUnavailable。

    和 recipe_ai.resolve_draft_model 一样不用 get_chat_model：它在没有接入时静默退回演示模型，拿演示模型
    起草出来的目录会被当成真的写进库里。数据层不导入接口层，设置键取 recipe_ai 里那份（与助手的同值，
    那边有测试核对）。
    """
    from app.core.errors import not_configured
    from app.data.recipe_ai import COPILOT_SETTING_KEY
    from app.db.models import Setting
    from app.providers.factory import ModelSpec, ProviderNotConfigured, build_chat_model, resolve_provider

    row = await session.get(Setting, COPILOT_SETTING_KEY)
    saved = (row.value if row else None) or {}
    spec = ModelSpec(provider=saved.get("provider") or None, model=saved.get("model") or None,
                     max_tokens=MODEL_MAX_TOKENS, temperature=0, thinking="off")
    unconfigured = "助手使用的模型尚未配置完成：{reason}。请到「设置 → 模型接入」检查"
    try:
        provider = await resolve_provider(session, spec.provider, spec.model)
    except ProviderNotConfigured as e:
        raise CatalogModelUnavailable(unconfigured.format(reason=not_configured(e, with_hint=False))) from e
    if provider is None:
        if spec.provider:
            raise CatalogModelUnavailable(f"助手设置里的模型接入「{spec.provider}」不存在。请到「设置 → 模型接入」检查")
        raise CatalogModelUnavailable("未配置模型接入，无法由助手起草数据目录。请到「设置 → 模型接入」添加")
    if provider.kind == "mock":
        raise CatalogModelUnavailable("当前只有演示用的模型接入，无法用于起草数据目录。"
                                      "请到「设置 → 模型接入」添加真实的模型接入")
    if not provider.enabled:
        raise CatalogModelUnavailable(f"助手使用的模型接入「{provider.name}」已停用。"
                                      "请到「设置 → 模型接入」启用它，或为助手换一个模型")
    try:
        model = build_chat_model(provider, spec)
    except ProviderNotConfigured as e:
        raise CatalogModelUnavailable(unconfigured.format(reason=not_configured(e, with_hint=False))) from e
    return model, spec.model or provider.default_model or ""


def _confirmed_only(notes: Mapping[str, Any] | None) -> dict[str, Any]:
    return _assemble({k: v for k, v in _slots(notes).items() if v.get("status") in ("confirmed", "verified")})


def _describe_for_model(name: str, meta: Mapping[str, Any], *, system: bool, existing: Mapping[str, Any] | None) -> str:
    """一张表交给模型的结构描述：列、类型、主键、外键、唯一约束、注释；不含任何数据值。"""
    lines = [(_P["view"] if meta.get("is_view") else _P["table"]).format(name=name)]
    comment = (meta.get("comment") or "").strip()
    if comment:
        lines.append((_P["system"] if system else _P["comment"]).format(text=comment))
    pk = set(meta.get("primary_key") or [])
    fk_of: dict[str, str] = {}
    for fk in meta.get("foreign_keys") or []:
        if isinstance(fk, dict) and len(fk.get("columns") or []) == 1:
            to_cols = fk.get("to_columns") or []
            fk_of[fk["columns"][0]] = f"{fk.get('to_table')}.{to_cols[0]}" if to_cols else str(fk.get("to_table"))
    lines.append(_P["columns"])
    for col in meta.get("columns") or []:
        marks = []
        if col["name"] in pk:
            marks.append(_P["pk"])
        if not col.get("nullable", True):
            marks.append(_P["not_null"])
        if col["name"] in fk_of:
            marks.append(_P["fk"].format(target=fk_of[col["name"]]))
        if text := (col.get("comment") or "").strip():
            marks.append((_P["col_system"] if system else _P["col_comment"]).format(text=text))
        line = _P["column"].format(name=col["name"], type=col.get("type") or "")
        lines.append(line + ("，" + "，".join(marks) if marks else ""))
    for group in meta.get("unique") or []:
        if isinstance(group, list) and group:
            lines.append(_P["unique"].format(cols="、".join(group)))
    confirmed = _notes_lines(_confirmed_only(existing), meta=meta, system_notes=False)
    if confirmed:
        lines.append(_P["confirmed"])
        lines += confirmed
    return "\n".join(lines)


def _batches(names: list[str], tables: Mapping[str, Any]) -> list[list[str]]:
    """按表分批：每批不超过 MODEL_BATCH_TABLES 张、合计不超过 MODEL_BATCH_COLUMNS 列（一张表超了就自成一批）。"""
    out: list[list[str]] = []
    batch: list[str] = []
    width = 0
    for name in names:
        n = len((tables.get(name) or {}).get("columns") or [])
        if batch and (len(batch) >= MODEL_BATCH_TABLES or width + n > MODEL_BATCH_COLUMNS):
            out.append(batch)
            batch, width = [], 0
        batch.append(name)
        width += n
    if batch:
        out.append(batch)
    return out


def _model_tables(raw: Any) -> list[dict[str, Any]] | None:
    """模型的回复 → 每张表一项的列表。结构化输出给的对象、正文里的 JSON 都认；对不上格式返回 None。"""
    if hasattr(raw, "model_dump"):
        raw = raw.model_dump()
    if isinstance(raw, str):
        text = raw.strip()
        start = min((i for i in (text.find("{"), text.find("[")) if i >= 0), default=-1)
        if start < 0:
            return None
        end = text.rfind("}" if text[start] == "{" else "]")
        try:
            raw = json.loads(text[start:end + 1])
        except ValueError:
            return None
    if isinstance(raw, dict):
        raw = raw.get("tables")
    if not isinstance(raw, list):
        return None
    return [t for t in raw if isinstance(t, dict)]


async def _ask_model(model: Any, messages: list[Any]) -> list[dict[str, Any]]:
    """问一批。先走结构化输出；模型不支持、调用报错、或给回来的不是约定的结构，再退回纯文本、从正文里取 JSON。

    结构化输出报错也退回（同 judge._ask）：有的网关不支持工具调用，一调就报错，纯文本却能用。代价是真的
    网络故障会多试一次，两次加起来仍受 MODEL_TIMEOUT_S 约束。
    """
    try:
        parsed = _model_tables(await model.with_structured_output(CATALOG_DRAFT_PROMPT_SCHEMA).ainvoke(messages))
    except Exception:  # noqa: BLE001 - 退回纯文本
        parsed = None
    if parsed is not None:
        return parsed
    from app.engine.state import message_text

    reply = await model.ainvoke(messages)
    parsed = _model_tables(message_text(reply))
    if parsed is None:
        raise ValueError("模型的回复不是约定的格式")
    return parsed


def _clean_text(value: Any, limit: int = _TEXT_MAX) -> str | None:
    if not isinstance(value, str):
        return None
    text = " ".join(value.split())
    return text if text and len(text) <= limit else None


def _from_model(entry: Mapping[str, Any], meta: Mapping[str, Any], *, system: bool) -> dict[str, Any]:
    """模型给的一张表 → 目录项（llm / proposed）。不合规的值整项丢掉，不硬塞：列名对不上表结构的、
    表类型和度量类型不在取值里的、业务主键里有不存在的列的。导入表格的源，说明已经覆盖的字段也丢掉。"""
    columns = {c["name"]: c for c in meta.get("columns") or []}
    notes: dict[str, Any] = {}
    table_has_system_note = system and bool((meta.get("comment") or "").strip())
    for name in ("label", "grain"):
        if name == "grain" and table_has_system_note:
            continue
        if (text := _clean_text(entry.get(name))) is not None:
            notes[name] = make_item(text, "llm")
    if entry.get("kind") in TABLE_KINDS:
        notes["kind"] = make_item(entry["kind"], "llm")
    keys = entry.get("keys")
    if _str_list_ok(keys) and all(k in columns for k in keys):
        notes["keys"] = make_item(list(dict.fromkeys(keys)), "llm")
    date = entry.get("business_date")
    if isinstance(date, dict) and date.get("column") in columns:
        value = {"column": date["column"]}
        for k in ("rule", "timezone"):
            if (text := _clean_text(date.get(k))) is not None:
                value[k] = text
        notes["business_date"] = make_item(value, "llm")
    out_cols: dict[str, dict[str, Any]] = {}
    for col in entry.get("columns") or []:
        if not isinstance(col, dict) or col.get("name") not in columns:
            continue
        name = col["name"]
        has_system_note = system and bool((columns[name].get("comment") or "").strip())
        items: dict[str, Any] = {}
        for field_name, limit in (("label", _TEXT_MAX), ("meaning", _TEXT_MAX), ("unit", 50)):
            if has_system_note and field_name in ("meaning", "unit"):
                continue
            if (text := _clean_text(col.get(field_name), limit)) is not None:
                items[field_name] = make_item(text, "llm")
        if col.get("measure") in COLUMN_MEASURES:
            items["measure"] = make_item(col["measure"], "llm")
        if items:
            out_cols[name] = items
    if out_cols:
        notes["columns"] = out_cols
    return notes


async def draft_with_model(model: Any, source: Any, tables: list[str], *,
                           existing: Mapping[str, Mapping[str, Any]] | None = None) -> dict[str, TableDraft | str]:
    """用模型起草这些表的中文名、粒度、表类型、业务主键、业务日期和每列的中文名、含义、单位、度量类型。

    model 要有 with_structured_output(schema) 或 ainvoke(messages)（测试注入假模型）。按表分批（_batches），
    最多 MODEL_CONCURRENCY 批同时发。返回 {表名: TableDraft 或失败原因}：一批失败只影响这一批的表，
    其余照常；失败的表不算 covered，已有的模型起草项不会因此被删。

    交给模型的只有表结构（列、类型、主键、外键、唯一约束、注释），不发任何数据值；existing 里已确认、
    有确证的项作为「以此为准」的背景一起交过去。
    """
    from langchain_core.messages import HumanMessage, SystemMessage

    all_tables = (getattr(source, "schema_cache", None) or {}).get("tables") or {}
    system = system_notes_source(source)
    names = [t for t in tables if isinstance(all_tables.get(t), dict)]
    results: dict[str, TableDraft | str] = {}
    gate = asyncio.Semaphore(MODEL_CONCURRENCY)

    async def run(batch: list[str]) -> None:
        described = "\n\n".join(_describe_for_model(n, all_tables[n], system=system,
                                                    existing=(existing or {}).get(n)) for n in batch)
        messages = [SystemMessage(content=CATALOG_DRAFT_SYSTEM),
                    HumanMessage(content=CATALOG_DRAFT_PROMPT.format(n=len(batch), tables=described))]
        try:
            async with gate:
                got = await asyncio.wait_for(_ask_model(model, messages), timeout=MODEL_TIMEOUT_S)
        except asyncio.TimeoutError:
            for n in batch:
                results[n] = f"模型起草超时：{MODEL_TIMEOUT_S} 秒内没有收到回复"
            return
        except Exception as e:  # noqa: BLE001 - 一批失败不影响其他批和其他来源
            from app.core.errors import describe_exception, first_line

            for n in batch:
                results[n] = f"模型起草失败：{first_line(e) or describe_exception(e)}"
            return
        by_name = {str(t.get("table")): t for t in got}
        for n in batch:
            entry = by_name.get(n)
            if entry is None:
                results[n] = "模型的回复里没有这张表"
                continue
            results[n] = TableDraft(notes=_from_model(entry, all_tables[n], system=system),
                                    covered=frozenset({"llm"}))

    await asyncio.gather(*(run(b) for b in _batches(names, all_tables)))
    return {n: results[n] for n in names if n in results}


# ==========================================================================
# 起草并写入
# ==========================================================================


@dataclass
class TableDraftResult:
    """一张表这次起草的结果。error：这张表没起草（表结构里没有、写入一直冲突）；model_error：只是模型那部分失败。"""

    table: str
    added: int = 0
    updated: int = 0
    removed: int = 0
    version: int = 0
    error: str | None = None
    model_error: str | None = None


@dataclass
class DraftReport:
    """一次起草的结果：每张表各一项，以及模型用没用上。"""

    tables: list[TableDraftResult] = field(default_factory=list)
    #: 这次有没有调模型、调的是哪个
    model_used: bool = False
    model: str = ""
    #: 模型整体用不了的原因（没有接入等）；单张表的模型失败记在 TableDraftResult.model_error
    model_error: str | None = None


async def draft_catalog(session: AsyncSession, source: Any, *, tables: list[str] | None = None,
                        use_model: bool = False, model: Any = None, actor: str | None = None) -> DraftReport:
    """起草这些表的目录并写入，同步完成。

    - source：手工源的 DataSource，或上传源绑定当前快照的视图（table_versions.resolve_source）；按 source.id 存。
    - tables 为空时按使用次数（table_usage）取前 DRAFT_DEFAULT_TABLES 张，次数相同按表结构里的顺序。
    - 每张表：数据库注释、外键约束、命名推断照做；use_model 时再加模型起草。model 可注入（测试用假模型），
      不给就按设置解析（resolve_draft_model）；模型用不了、某一批失败，都不影响其余来源照常写入。
    - 并入已有目录按 merge_notes 的规则：人工确认、驳回过的项不动。内容没变不写、不升版本。
    - 写入撞上别人刚改过（乐观锁）就重读重并，最多三次。
    """
    cache = getattr(source, "schema_cache", None) or {}
    all_tables = cache.get("tables") or {}
    report = DraftReport()
    if tables:
        wanted = list(dict.fromkeys(str(t) for t in tables))
    else:
        usage = await table_usage(session, source)
        order = {name: i for i, name in enumerate(all_tables)}
        wanted = sorted(all_tables, key=lambda n: (-usage.get(n, 0), order[n]))[:DRAFT_DEFAULT_TABLES]
    picked: list[str] = []
    for name in wanted:
        key = resolve_table_name(cache, name)
        if key is None:
            report.tables.append(TableDraftResult(table=name, error=f"表结构里没有 {name}，请先重新探查结构"))
        elif key not in picked:
            picked.append(key)

    llm: dict[str, TableDraft | str] = {}
    if use_model and picked:
        try:
            if model is None:
                model, report.model = await resolve_draft_model(session)
            else:
                report.model = str(getattr(model, "model_name", "") or "")
        except CatalogModelUnavailable as e:
            report.model_error = str(e)
        else:
            report.model_used = True
            existing = await read_catalog(session, source.id)
            llm = await draft_with_model(model, source, picked, existing={t: e.notes for t, e in existing.items()})

    at = now_iso()
    for table in picked:
        result = TableDraftResult(table=table)
        structure = draft_structure(source, table)
        modeled = llm.get(table)
        if isinstance(modeled, str):
            result.model_error = modeled
        for _attempt in range(3):
            entry = await read_entry(session, source.id, table)
            version = entry.version if entry else 0
            merged, stats = merge_notes(entry.notes if entry else {}, structure.notes, covered=structure.covered,
                                        at=at)
            if isinstance(modeled, TableDraft):
                merged, more = merge_notes(merged, modeled.notes, covered=modeled.covered, at=at)
                stats += more
            result.added, result.updated, result.removed = stats.added, stats.updated, stats.removed
            result.version = version
            if entry is None and not merged:
                break                    # 什么也没起草出来，不建空目录
            try:
                written = await write_entry(session, source.id, table, merged, if_version=version, actor=actor)
            except CatalogConflict:
                continue
            result.version = written.version
            break
        else:
            result.error = "这张表的数据目录正被其他人修改，请稍后再起草"
            result.added = result.updated = result.removed = 0
        report.tables.append(result)
    return report


# ==========================================================================
# 渲染：交给模型看的文本
# ==========================================================================

#: proposed 的项在模型可见文本里的标记
INFERRED_MARK = "（推断，未确认）"
KIND_LABEL = {"fact": "事实表", "dimension": "维度表", "snapshot": "快照表", "log": "日志表", "config": "配置表"}
MEASURE_LABEL = {"flow": "流量，可跨期加总", "stock": "存量，不能跨期加总", "ratio": "比率，不能直接加总",
                 "identifier": "标识", "status": "状态", "attribute": "属性"}
CARDINALITY_LABEL = {"many_to_one": "多对一", "one_to_one": "一对一", "one_to_many": "一对多"}
#: 单表目录的标题行
NOTES_HEADING = "数据目录（标「推断，未确认」的项是推断，使用前请核实）："


def _mark(item: Mapping[str, Any]) -> str:
    return INFERRED_MARK if item.get("status") == "proposed" else ""


def _shown(item: Any, *, covered: bool) -> bool:
    """这一项给不给模型看：被驳回的不给；系统说明已经覆盖的，只给人工确认过的。"""
    if not isinstance(item, dict) or item.get("status") == "rejected":
        return False
    return not covered or item.get("status") == "confirmed"


def _date_text(value: Mapping[str, Any]) -> str:
    parts = [str(value.get("column") or "")]
    if value.get("rule"):
        parts.append(f"规则：{value['rule']}")
    if value.get("timezone"):
        parts.append(f"时区：{value['timezone']}")
    return "；".join(parts)


def _relation_text(rel: Mapping[str, Any]) -> str:
    left = ", ".join(rel.get("columns") or [])
    right = ", ".join(f"{rel.get('to_table')}.{c}" for c in rel.get("to_columns") or [])
    extra = []
    if rel.get("cardinality") in CARDINALITY_LABEL:
        extra.append(CARDINALITY_LABEL[rel["cardinality"]])
    coverage = rel.get("coverage")
    if isinstance(coverage, (int, float)) and not isinstance(coverage, bool):
        extra.append(f"覆盖率 {coverage:.0%}")
    return f"{left} → {right}" + (f"（{'，'.join(extra)}）" if extra else "") + _mark(rel)


def _notes_lines(notes: Mapping[str, Any] | None, *, meta: Mapping[str, Any] | None,
                 system_notes: bool) -> list[str]:
    """单表目录的正文行（不含标题），两格缩进起。"""
    notes = notes or {}
    meta = meta or {}
    table_covered = system_notes and bool((meta.get("comment") or "").strip())
    lines: list[str] = []
    for name in TABLE_FIELDS:
        item = notes.get(name)
        if not _shown(item, covered=table_covered and name in ("grain", "description")):
            continue
        value = item["value"]
        if name == "kind":
            text = KIND_LABEL.get(value, str(value))
        elif name == "keys":
            text = ", ".join(value)
        elif name == "business_date":
            text = _date_text(value)
        else:
            text = str(value)
        lines.append(f"  {TABLE_FIELD_LABEL[name]}：{text}{_mark(item)}")

    col_comment = {c.get("name"): bool((c.get("comment") or "").strip()) for c in meta.get("columns") or []}
    order = [c.get("name") for c in meta.get("columns") or []]
    columns = notes.get("columns") if isinstance(notes.get("columns"), dict) else {}
    col_lines: list[str] = []
    for col in [c for c in order if c in columns] + [c for c in columns if c not in order]:
        items = columns[col] if isinstance(columns[col], dict) else {}
        covered = system_notes and col_comment.get(col, False)
        parts: list[str] = []
        for name in COLUMN_FIELDS:
            item = items.get(name)
            if not _shown(item, covered=covered and name in ("meaning", "unit")):
                continue
            value = item["value"]
            if name == "measure":
                text = MEASURE_LABEL.get(value, str(value))
            elif name == "codes":
                text = "、".join(f"{k}={v}" for k, v in value.items())
            else:
                text = str(value)
            # 第一段是中文名（没有中文名时是含义），不加前缀；其余写明是什么
            plain = not parts and name in ("label", "meaning")
            parts.append(("" if plain else f"{COLUMN_FIELD_LABEL[name]}：") + text + _mark(item))
        if parts:
            col_lines.append(f"    {col}：{'；'.join(parts)}")
    if col_lines:
        lines.append("  列：")
        lines += col_lines

    relations = notes.get("relations") if isinstance(notes.get("relations"), list) else []
    rel_lines = [f"    {_relation_text(r)}" for r in relations if isinstance(r, dict) and r.get("status") != "rejected"]
    if rel_lines:
        lines.append("  关联关系：")
        lines += rel_lines
    return lines


def render_table_notes(notes: Mapping[str, Any] | None, *, meta: Mapping[str, Any] | None = None,
                       system_notes: bool = False) -> str:
    """单表目录的完整版，给 db_schema 工具和助手挑表后补上下文用。没有可给模型看的内容时返回空串。

    - 被驳回的项、关系不出现；proposed 的项标「推断，未确认」，有确证和人工确认的不标。
    - meta 是这张表在 schema_cache 里的结构：列按表结构的顺序列出。
    - system_notes（导入表格的源）：表和列已有系统生成的说明。表有说明时，粒度、说明只在人工确认过时追加；
      列有说明时，含义、单位同理；其余的项（中文名、表类型、业务主键、度量类型、码值、关系……）说明里不写，
      照常追加。
    """
    lines = _notes_lines(notes, meta=meta, system_notes=system_notes)
    return "\n".join([NOTES_HEADING, *lines]) if lines else ""


def render_table_index(schema_cache: Mapping[str, Any] | None, notes_by_table: Mapping[str, Mapping[str, Any]], *,
                       tables: Iterable[str] | None = None) -> str:
    """紧凑的表目录，每张表一行：表名｜中文名｜粒度。给助手按任务挑表用。

    表名用全名（模型照着写 SQL）；中文名或粒度是推断的，行尾标「推断，未确认」。没有目录的表只写表名。
    tables 给了就只列这些（按给的顺序），否则按表结构的顺序列全部。
    """
    all_tables = (schema_cache or {}).get("tables") or {}
    lines = []
    for name in (list(tables) if tables is not None else list(all_tables)):
        meta = all_tables.get(name)
        if not isinstance(meta, dict):
            continue
        notes = notes_by_table.get(name) or {}
        label = notes.get("label") if _shown(notes.get("label"), covered=False) else None
        grain = notes.get("grain") if _shown(notes.get("grain"), covered=False) else None
        parts = [str(meta.get("qualified") or name)]
        if label:
            parts.append(str(label["value"]))
        if grain:
            parts.append(str(grain["value"]) if label else f"粒度：{grain['value']}")
        mark = INFERRED_MARK if any(i and i.get("status") == "proposed" for i in (label, grain)) else ""
        lines.append("｜".join(parts) + mark)
    return "\n".join(lines)


# ==========================================================================
# 关系图
# ==========================================================================

_FLIP = {"many_to_one": "one_to_many", "one_to_many": "many_to_one", "one_to_one": "one_to_one"}


@dataclass(frozen=True)
class JoinEdge:
    """关系图里的一条有向边：从 from_table 的 from_columns 连到 to_table 的 to_columns。

    每条关系在图里有正反两条边；cardinality 是从 from_table 看过去的基数。
    """

    from_table: str
    from_columns: tuple[str, ...]
    to_table: str
    to_columns: tuple[str, ...]
    relation_id: str
    cardinality: str | None
    source: str
    status: str

    def reversed(self) -> "JoinEdge":
        return JoinEdge(self.to_table, self.to_columns, self.from_table, self.from_columns, self.relation_id,
                        _FLIP.get(self.cardinality or ""), self.source, self.status)

    def condition(self) -> str:
        """连接条件：visits.id = channel_visits.visit_id（多列用 AND 连接）。"""
        return " AND ".join(f"{self.from_table}.{a} = {self.to_table}.{b}"
                            for a, b in zip(self.from_columns, self.to_columns))


def relation_graph(notes_by_table: Mapping[str, Mapping[str, Any]], *,
                   schema_cache: Mapping[str, Any] | None = None) -> dict[str, list[JoinEdge]]:
    """邻接表 {表: [从这张表出发的边]}，每条关系正反各一条边；被驳回的关系不进图，自己连自己的也不进。

    schema_cache 给了的话，没起草过目录的关系按表结构现推（外键约束 + 命名推断）补进来：助手挑表时不必等
    每张表都起草过。目录里已有的关系（含驳回的）以目录为准。
    """
    rels: dict[str, tuple[str, Mapping[str, Any]]] = {}
    rejected: set[str] = set()
    for table, notes in notes_by_table.items():
        relations = (notes or {}).get("relations")
        for rel in relations if isinstance(relations, list) else []:
            if not isinstance(rel, dict) or not rel.get("id"):
                continue
            if rel.get("status") == "rejected":
                rejected.add(rel["id"])
            else:
                rels.setdefault(rel["id"], (table, rel))
    if schema_cache:
        all_tables = schema_cache.get("tables") or {}
        index = _name_index(all_tables)
        for table in all_tables:
            inferred = (fk_relations(schema_cache, table) or []) + infer_name_relations(schema_cache, table,
                                                                                         _index=index)
            for rel in inferred:
                if rel["id"] not in rels and rel["id"] not in rejected:
                    rels[rel["id"]] = (table, rel)
    graph: dict[str, list[JoinEdge]] = {}
    for rid, (table, rel) in rels.items():
        if rel.get("to_table") == table:
            continue
        edge = JoinEdge(table, tuple(rel.get("columns") or ()), str(rel.get("to_table")),
                        tuple(rel.get("to_columns") or ()), rid, rel.get("cardinality"), str(rel.get("source")),
                        str(rel.get("status")))
        graph.setdefault(edge.from_table, []).append(edge)
        graph.setdefault(edge.to_table, []).append(edge.reversed())
    return graph


def join_paths(graph: Mapping[str, list[JoinEdge]], start: str, end: str, *,
               max_hops: int = 2) -> list[tuple[JoinEdge, ...]]:
    """两张表之间不超过 max_hops 跳（最多 2）的连接路径，短的在前，同样长的推断边少的在前。"""
    paths: list[tuple[JoinEdge, ...]] = [(e,) for e in graph.get(start, []) if e.to_table == end]
    if max_hops >= 2:
        for first in graph.get(start, []):
            mid = first.to_table
            if mid in (start, end):
                continue
            paths += [(first, second) for second in graph.get(mid, []) if second.to_table == end]
    return sorted(paths, key=lambda p: (len(p), sum(e.status == "proposed" for e in p)))


# ==========================================================================
# 使用次数
# ==========================================================================

#: 数据源查询工具存查询快照用的工件类型（tools/datasource.py）
QUERY_SNAPSHOT_KIND = "query_snapshot"

#: 老的查询快照引用（meta 里没记源和表名）读过一次的结果：{工件 id: (源名, 表名)}。工件内容按哈希寻址、
#: 永不改变，缓存不会过期
_LEGACY_QUERY_TABLES: dict[str, tuple[str | None, tuple[str, ...]]] = {}


def _load_artifact(artifact_id: str) -> Any:
    from app.core.artifact_store import load

    return load(artifact_id)


def _legacy_query(artifact_id: str) -> tuple[str | None, tuple[str, ...]]:
    if artifact_id not in _LEGACY_QUERY_TABLES:
        from app.engine.evidence import sql_tables

        content = _load_artifact(artifact_id)
        if isinstance(content, dict):
            found = (content.get("source"), tuple(sql_tables(str(content.get("sql") or ""))))
        else:
            found = (None, ())
        _LEGACY_QUERY_TABLES[artifact_id] = found
    return _LEGACY_QUERY_TABLES[artifact_id]


def query_snapshot_meta(source: Any, sql: str) -> dict[str, Any]:
    """查询快照工件引用的 meta：源 id、源名、SQL 里的表名。数据源查询工具存快照时写进去，table_usage 据此计数。

    表名的取法和证据台账一样（evidence.sql_tables：FROM / JOIN 后面的表，去掉注释和字符串）。meta 只在
    工件引用表里，不进工件内容：查询快照的内容哈希不变。不抛异常：它和查询快照在同一个 try 里，
    这里出错会让快照存不下，证据就断了。
    """
    from app.engine.evidence import sql_tables

    try:
        tables = sql_tables(sql or "")
    except Exception:  # noqa: BLE001 - 解析不了就当没用到表，只影响使用次数
        tables = []
    return {"source_id": getattr(source, "id", None), "source": getattr(source, "name", None), "tables": tables}


async def table_usage(session: AsyncSession, source: Any) -> dict[str, int]:
    """每张表被运行查询过的次数：{schema_cache 的表名: 次数}，没被查过的表不出现。

    取法：数据源查询工具每次成功查询都存一份查询快照工件（tools/datasource.py），工件引用表 artifacts 里
    按 (内容哈希, 运行) 各记一行。数「属于某次运行（runs 里有这次运行）、属于这个源、SQL 里用到了这张表」的
    行：同一次运行里结果一模一样的重复查询只算一次，工具库里的试用（不属于任何运行）不算。

    表名在查询当时就记进了引用的 meta（query_snapshot_meta），这里只读 artifacts 一张表；升级前的老引用
    没有 meta，退回读工件内容里的源名和 SQL，读过的按工件 id 缓存（内容永不改变）。SQL 里写的表名去掉
    schema 前缀后按 name_key（只把 ASCII 字母转小写，和 SQLite 比较标识符的口径一致）对到表结构上，对不上的不计。
    """
    rows = (await session.execute(
        select(Artifact.id, Artifact.meta).join(Run, Run.id == Artifact.run_id)
        .where(Artifact.kind == QUERY_SNAPSHOT_KIND)
    )).all()
    tables = (getattr(source, "schema_cache", None) or {}).get("tables") or {}
    lookup: dict[str, str] = {}
    for name in tables:
        lookup.setdefault(name_key(name), name)
    counts: Counter[str] = Counter()
    for artifact_id, meta in rows:
        meta = meta if isinstance(meta, dict) else {}
        if isinstance(meta.get("tables"), list):
            source_id, source_name, names = meta.get("source_id"), meta.get("source"), meta["tables"]
        else:
            source_id = None
            source_name, names = _legacy_query(str(artifact_id))
        if source_id is not None:
            if source_id != source.id:
                continue
        elif source_name != source.name:
            continue
        hit = {lookup.get(name_key(str(n).rsplit(".", 1)[-1])) for n in names}
        counts.update(t for t in hit if t is not None)
    return dict(counts)


# ==========================================================================
# 冻结进证据
# ==========================================================================


def frozen_catalog(entries: Mapping[str, CatalogEntry], tables: Iterable[str]) -> dict[str, dict[str, Any]]:
    """要冻结进表结构快照的目录：{表名: {"version": 版本, "notes": 去掉驳回项的目录}}。

    只收表结构快照里有的表（tables）；去掉驳回项以后什么都不剩的表不收。一张也没有时返回空 dict，
    调用方据此不加这个键：没有目录的源，快照内容和以前一字不差。目录不变（版本和内容都不变）则返回值
    不变，快照哈希也就不变。
    """
    wanted = set(tables)
    out: dict[str, dict[str, Any]] = {}
    for name in sorted(entries):
        if name not in wanted:
            continue
        notes = visible_notes(entries[name].notes)
        if notes:
            out[name] = {"version": entries[name].version, "notes": notes}
    return out


__all__ = [
    "CARDINALITIES", "CARDINALITY_LABEL", "CATALOG_DRAFT_PROMPT", "CATALOG_DRAFT_PROMPT_SCHEMA",
    "CATALOG_DRAFT_SYSTEM", "COLUMN_FIELDS", "COLUMN_FIELD_LABEL", "COLUMN_MEASURES", "CatalogConflict",
    "CatalogEntry", "CatalogInvalid", "CatalogModelUnavailable", "CatalogPathError", "DRAFT_DEFAULT_TABLES",
    "DraftReport", "INFERRED_MARK", "ITEM_SOURCES", "ITEM_STATUSES", "JoinEdge", "KIND_LABEL", "MEASURE_LABEL",
    "MergeStats", "NEW_RELATION_PATH", "PATCH_MAX_CHANGES", "PatchChange", "PatchPlan", "QUERY_SNAPSHOT_KIND",
    "REVIEW_ACTIONS", "TABLE_FIELDS", "TABLE_FIELD_LABEL", "TABLE_KINDS", "TableDraft", "TableDraftResult",
    "UI_KIND_LABEL", "UI_MEASURE_LABEL", "apply_human_edit", "apply_patch", "describe_slot", "draft_catalog",
    "draft_structure", "draft_with_model", "fk_relations", "frozen_catalog", "infer_name_relations",
    "initial_status", "join_paths", "make_item", "merge_notes", "now_iso", "parse_path", "plan_patch",
    "query_snapshot_meta", "read_catalog", "read_entry", "relation_graph", "relation_id", "render_table_index",
    "render_table_notes", "resolve_draft_model", "resolve_table_name", "review_entry", "review_item", "same_notes",
    "save_human_edit", "save_patch", "status_counts", "system_notes_source", "table_usage", "validate_notes",
    "visible_notes", "write_entry",
]
