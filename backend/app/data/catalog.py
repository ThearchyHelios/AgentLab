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

import copy
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import CatalogNote

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


#: 每个字段的值怎么查、不合规时怎么说
_TABLE_VALUE_RULES: dict[str, tuple[Any, str]] = {
    "label": (_text_ok, "应为不超过 500 字的文字"),
    "description": (lambda v: _text_ok(v, _LONG_TEXT_MAX), "应为不超过 2000 字的文字"),
    "grain": (_text_ok, "应为不超过 500 字的文字"),
    "keys": (_str_list_ok, "应为列名的列表"),
    "kind": (lambda v: v in TABLE_KINDS, "应为事实表、维度表、快照表、日志表或配置表之一"),
    "business_date": (_business_date_ok, "应写明日期列，规则和时区写成文字"),
    "valid_filter": (_text_ok, "应为不超过 500 字的条件"),
    "dedup": (_text_ok, "应为不超过 500 字的文字"),
}
_COLUMN_VALUE_RULES: dict[str, tuple[Any, str]] = {
    "label": (_text_ok, "应为不超过 500 字的文字"),
    "meaning": (_text_ok, "应为不超过 500 字的文字"),
    "unit": (lambda v: _text_ok(v, 50), "应为不超过 50 字的文字"),
    "measure": (lambda v: v in COLUMN_MEASURES, "应为流量、存量、比率、标识、状态或属性之一"),
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
    where = f"关联关系 {rid}" if isinstance(rid, str) and rid else "关联关系"
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
        problems.append(f"{where}的本表列应为列名的列表")
    if not _text_ok(rel.get("to_table"), 255):
        problems.append(f"{where}缺少被指向的表")
    if not _str_list_ok(to_cols):
        problems.append(f"{where}的被指向列应为列名的列表")
    elif _str_list_ok(cols) and len(cols) != len(to_cols):
        problems.append(f"{where}两端的列数不一致")
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
    import hashlib

    pairs = sorted(zip((c.lower() for c in columns), (c.lower() for c in to_columns)))
    raw = json.dumps([table.lower(), to_table.lower(), pairs], ensure_ascii=False, separators=(",", ":"))
    return "r" + hashlib.sha256(raw.encode()).hexdigest()[:12]


__all__ = [
    "CARDINALITIES", "COLUMN_FIELDS", "COLUMN_FIELD_LABEL", "COLUMN_MEASURES", "CatalogConflict", "CatalogEntry",
    "CatalogInvalid", "ITEM_SOURCES", "ITEM_STATUSES", "TABLE_FIELDS", "TABLE_FIELD_LABEL", "TABLE_KINDS",
    "initial_status", "make_item", "now_iso", "read_catalog", "read_entry", "relation_id", "same_notes",
    "validate_notes", "write_entry",
]
