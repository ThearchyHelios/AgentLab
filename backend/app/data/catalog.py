"""业务数据目录：每张表一份业务说明，给助手建图、db_schema 工具和 SQL 检查（data/sqlcheck.py）用。

**为什么要有它。** 接进来的业务库往往一个注释都没有：几百张表、上千个字段，助手只看得到名字，模型就会
照着名字编字段、猜关联。目录把「一行代表什么、业务主键是哪几列、业务日期按哪列算、金额是什么单位、
状态码各是什么意思、这张表和哪张表怎么连」记下来，并注明每一条是怎么来的、有没有人确认过。

**只记数据事实，不放计算公式。** 公式（指标怎么算）在口径卡里；这里只描述数据本身。所以 notes 的
字段是固定的一组，不认识的字段一律拒收（validate_notes）。

notes 结构（每个「项」都是 {value, source, status}，可选 note、reason、updated_at）::

    {"label": 项(str), "description": 项(str), "grain": 项(str), "keys": 项(list[str]),
     "kind": 项("fact"|"dimension"|"snapshot"|"log"|"config"),
     "business_date": 项({"column": str, "rule": str, "timezone": str}),
     "valid_filter": 项(str，SQL 条件片段), "dedup": 项(str),
     "columns": {列名: {"label": 项, "meaning": 项, "unit": 项,
                       "measure": 项("flow"|"stock"|"ratio"|"identifier"|"status"|"attribute"),
                       "codes": 项(dict[码值, 含义]；含义可为空串，表示待填写；项上可带 complete: true，
                                  表示已列出全部取值)}},
     "relations": [{"id", "columns", "to_table", "to_columns", "cardinality", "coverage",
                    "source", "status", "note"?, "reason"?, "cardinality_checked"?}]}

note 是机器写的说明（模型起草的依据、数据剖析的测量、双盲复算的出处……）；reason 是保存修改提案时使用者确认的依据
（界面上叫「确认时的依据」，最多 500 字），只由 apply_patch 写，两者分开存，规矩见 apply_patch。

关系上的 cardinality_checked: true 只由数据剖析写：基数是用数据核实的（子表一侧数过 COUNT 和 COUNT(DISTINCT)，
或者有主键、唯一约束）。外键约束、命名推断给的基数是按表结构推的，子表一侧可能每个键恰好一行；SQL 检查只在
基数用数据核实过、或者人工确认过时，才把一对多关联后的重复计算报成错误（data/sqlcheck.py）。

来源（source）：comment 数据库注释、fk 外键约束、name 命名推断、profile 数据剖析（data/catalog_profile.py）、
llm 模型起草、human 人工填写。状态（status）：proposed 推断（命名推断、模型起草、数据库注释）、verified 有确证
（外键约束；数据剖析核实了覆盖率和父键唯一的关系也是，见 profile_relation_holds）、confirmed 人工确认、
rejected 人工驳回。SQL 检查只在依据全是 confirmed、verified
时报错误级（data/sqlcheck.py），依据里有 proposed 就降为提示；给模型看时 proposed 的项标「推断，未确认」；
rejected 的项留着（防止下次起草又提出来），但模型和检查都看不到。

本模块分几块：存取（带乐观锁）、结构校验、合并与审阅、对话提案、起草、渲染、关系图、使用次数、冻结。存取
以外的函数尽量是纯函数，接口层、db_schema 工具、助手挑表（api/copilot_context.py）、数据剖析
（data/catalog_profile.py）和 SQL 检查（data/sqlcheck.py）共用；几处两边都要用的判断也放在这里只写一份：
表名的匹配（resolve_table_name，按 name_key）、名字切词（name_words）、日期列的判定（is_date_column）。
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import re
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.data.names import name_key
from app.db.models import Artifact, CatalogNote, DataSource, Run

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

_ITEM_KEYS = frozenset({"value", "source", "status", "note", "reason", "updated_at"})
#: 码值项另有一个「已列全」标记（complete: true）：码值表列出了这一列的全部取值。只有人工明确确认、或者数据剖析
#: 没截断地看完了全表取值时才有；没有它的码值表只是「已知的一部分」，SQL 检查不能拿它判断某个值「不在码值表里」
_CODES_ITEM_KEYS = _ITEM_KEYS | {"complete"}
_RELATION_KEYS = frozenset({"id", "columns", "to_table", "to_columns", "cardinality", "coverage", "source",
                            "status", "note", "reason", "updated_at", "cardinality_checked"})
_BUSINESS_DATE_KEYS = frozenset({"column", "rule", "timezone"})
#: 文本项的长度上限。目录是给模型和人读的短说明，长篇大论放口径卡或知识库
_TEXT_MAX = 500
_LONG_TEXT_MAX = 2000
#: 确认时的依据（项上的 reason）的长度上限：同文本项
REASON_MAX = _TEXT_MAX


def now_iso() -> str:
    """项的 updated_at 用的时间戳（UTC，ISO 8601）。"""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def initial_status(source: str) -> str:
    """某个来源起草出来的项的初始状态。"""
    return _INITIAL_STATUS.get(source, "proposed")


#: 数据剖析把关系升为有确证的抽样覆盖率下限（data/catalog_profile.py 判定，撤销审阅时按它判回剖析的结论）
PROFILE_VERIFY_COVERAGE = 0.95


def profile_relation_holds(rel: Mapping[str, Any]) -> bool:
    """一条数据剖析得出的关系站不站得住：抽样覆盖率不低于 PROFILE_VERIFY_COVERAGE，而且被指向列核实唯一。

    剖析只在被指向列核实唯一时才写基数（多对一或一对一），所以「基数有值」就是「父键唯一」。结论全在关系
    自己的字段里，撤销审阅时照原样判得回来，不必另存一份「审阅前的状态」。
    """
    coverage = rel.get("coverage")
    return (rel.get("source") == "profile" and isinstance(coverage, (int, float)) and not isinstance(coverage, bool)
            and coverage >= PROFILE_VERIFY_COVERAGE and rel.get("cardinality") in ("many_to_one", "one_to_one"))


def unreviewed_status(item: Mapping[str, Any]) -> str:
    """撤销审阅后回到的状态：来源的初始状态。

    数据剖析是例外：它的状态随数据而定（覆盖率够、父键唯一的关系是有确证，其余是推断），不能一律回到
    initial_status("profile") 的推断——那样人工确认过再撤销，有确证就无故丢了，要等下一次剖析才回来。
    剖析提的码值候选、业务日期永远是推断。
    """
    if item.get("source") == "profile" and "to_table" in item:
        return "verified" if profile_relation_holds(item) else "proposed"
    return initial_status(item.get("source"))


def make_item(value: Any, source: str, status: str | None = None, *, note: str | None = None,
              at: str | None = None, complete: bool = False, reason: str | None = None) -> dict[str, Any]:
    """构造一个项。status 不给时按来源取初始状态（initial_status）。complete 只用于码值项（已列出全部取值），
    为假时不写这个键：没有标记和标记为否是一回事，写法统一，冻结进快照的哈希才稳定。reason 是确认时的依据
    （apply_patch 写），没有时不写这个键。"""
    out: dict[str, Any] = {"value": value, "source": source, "status": status or initial_status(source)}
    if note:
        out["note"] = note
    if reason:
        out["reason"] = reason
    if at:
        out["updated_at"] = at
    if complete:
        out["complete"] = True
    return out


def clip_reason(reason: Any) -> str | None:
    """提案里的依据 → 存进项里的 reason：去掉首尾空白，超过 REASON_MAX 字截成 REASON_MAX - 1 字加「…」。没有（空的、
    不是文字）为 None。预览（plan_patch）和保存（apply_patch）用的是同一句。"""
    text = reason.strip() if isinstance(reason, str) else ""
    if not text:
        return None
    return text if len(text) <= REASON_MAX else text[:REASON_MAX - 1] + "…"


def codes_complete(item: Any) -> bool:
    """码值项是不是标了「已列全」。"""
    return isinstance(item, Mapping) and item.get("complete") is True


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
    """码值对照：{码值: 含义}，都是文字。含义可以是空串——数据剖析只知道列里出现过哪些取值，含义要等人填；
    以前要求含义非空，剖析得到的码值候选就写不进来。"""
    return (isinstance(value, dict) and bool(value)
            and all(isinstance(k, str) and isinstance(v, str) for k, v in value.items()))


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


def _item_problems(where: str, item: Any, rule: tuple[Any, str], *,
                   keys: frozenset[str] = _ITEM_KEYS) -> list[str]:
    if not isinstance(item, dict) or not {"value", "source", "status"} <= set(item):
        return [f"{where}不是有效的目录项：需要值、来源和状态"]
    problems: list[str] = []
    if extra := sorted(set(item) - keys):
        problems.append(f"{where}含有不认识的字段「{'、'.join(extra)}」")
    if "complete" in item and not isinstance(item["complete"], bool):
        problems.append(f"{where}是否已列出全部取值应为是或否")
    if item["source"] not in ITEM_SOURCES:
        problems.append(f"{where}的来源「{item['source']}」不在可选值内")
    if item["status"] not in ITEM_STATUSES:
        problems.append(f"{where}的状态「{item['status']}」不在可选值内")
    check, hint = rule
    if not check(item["value"]):
        problems.append(f"{where}{hint}")
    if "note" in item and not isinstance(item["note"], str):
        problems.append(f"{where}的备注应为文字")
    if "reason" in item and not _reason_ok(item["reason"]):
        problems.append(f"{where}{_REASON_HINT}")
    if "updated_at" in item and not isinstance(item["updated_at"], str):
        problems.append(f"{where}的修改时间应为文字")
    return problems


def _reason_ok(value: Any) -> bool:
    return isinstance(value, str) and len(value) <= REASON_MAX


_REASON_HINT = f"确认时的依据应为不超过 {REASON_MAX} 字的文字"


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
    if "reason" in rel and not _reason_ok(rel["reason"]):
        problems.append(f"{where}{_REASON_HINT}")
    if "cardinality_checked" in rel and not isinstance(rel["cardinality_checked"], bool):
        problems.append(f"{where}的「基数已用数据核实」应为是或否")
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
                                           _COLUMN_VALUE_RULES[field_name],
                                           keys=_CODES_ITEM_KEYS if field_name == "codes" else _ITEM_KEYS)
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
                      if_version: int, actor: str | None, commit: bool = True) -> CatalogEntry:
    """整份写入一张表的目录（乐观锁），提交事务，返回写入后的目录。

    commit=False 只写进当前事务、不提交，由调用方和它自己的写入一起提交：剖析一张表结束时，目录和这张表的剖析结果要么
    都写进去、要么都没写（进程在两次提交之间没了，剖析结果会说「没写进目录」，目录里其实已经写了）。冲突时照样回滚。

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
            await (session.commit() if commit else session.flush())
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
    if commit:
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


def _edit_value(key: Slot, item: Mapping[str, Any]) -> Any:
    """人工编辑时比较「改没改」看的部分：值，码值另加「已列全」标记——只勾上这个标记也是人工改动（记为人工填写、
    已确认），检查据此认定码值表是完整的。"""
    if key[0] == "c" and key[2] == "codes":
        return item.get("value"), codes_complete(item)
    return _value_of(key, item)


def _core(item: Mapping[str, Any]) -> dict[str, Any]:
    """去掉修改时间的项：判断起草结果和已有的是不是一回事。"""
    return {k: v for k, v in item.items() if k != "updated_at"}


#: 数据剖析写的备注都以「数据剖析：」开头（data/catalog_profile.py 的 NOTE_PREFIX 就是它）。说明里不写日期：以前写的是
#: UTC 日期，和界面上按本地时间显示的修改时间对不上；什么时候剖析的，看这一项的 updated_at
PROFILE_NOTE_PREFIX = "数据剖析："
#: 升级前的剖析备注带着当天的 UTC 日期：「数据剖析（2026-10-01）：」。旧数据里还有，照样认
_PROFILE_NOTE_DAY = re.compile(r"^数据剖析（\d{4}-\d{2}-\d{2}）(?=：)")


def is_profile_note(note: Any) -> bool:
    """这段备注是不是数据剖析写的（新写法「数据剖析：」，或者升级前带日期的「数据剖析（日期）：」）。"""
    return isinstance(note, str) and (note.startswith(PROFILE_NOTE_PREFIX) or bool(_PROFILE_NOTE_DAY.match(note)))


def _undated(note: str) -> str:
    """剖析备注去掉升级前写的日期：「数据剖析（2026-10-01）：…」→「数据剖析：…」。别的备注原样返回。"""
    return _PROFILE_NOTE_DAY.sub("数据剖析", note, count=1)


#: 剖析备注改过的说法：(升级前的写法, 现在的写法)。大表看的是按存储顺序的前 N 行（多半是最老的数据）、关系取的是
#: 数据库先给出的那几个键值，都不是随机抽样，说法改成照实的（data/catalog_profile.py 的 _relation_note、码值的
#: 「统计范围」、FILTER_SAMPLED）。只换这几处固定的句式，换完和现在的写法一字不差才算同一句
_LEGACY_PROFILE_WORDING = (
    (re.compile(r"抽样覆盖率 (?=\S+?，可能不是这条关系)"), "覆盖率 "),
    (re.compile(r"子表抽样 (?=\d+ 个不同键值)"), "子表取了 "),
    (re.compile(r"（只看前 (?=\d+ 行）)"), "（按存储顺序只看前 "),
    (re.compile(r"按前 (?=[\d,]+ 行统计)"), "按存储顺序取前 "),
)
#: 起草时核对有效行过滤写进备注的那句（catalog_profile.FILTER_SAMPLED / FILTER_SAMPLED_EDGE）：接在模型给的依据后面，
#: 整段不以「数据剖析：」开头，只换这一句
_LEGACY_FILTER_WORDING = re.compile(r"剖析核对：按前 (?=[\d,]+ 行统计)")


def _current_wording(note: str) -> str:
    """剖析备注里升级前的说法换成现在的写法（_LEGACY_PROFILE_WORDING）。只用来比较，不改写目录里存着的备注。"""
    for pattern, now in _LEGACY_PROFILE_WORDING:
        note = pattern.sub(now, note)
    return note


def same_profile_note(a: Any, b: Any) -> bool:
    """两段备注是否相同；数据剖析写的备注只差开头的日期（升级前每次都写当天的日期），或者一段带日期、一段是不带
    日期的新写法，或者只差改过的几处说法（_LEGACY_PROFILE_WORDING：「抽样覆盖率」→「覆盖率」之类），也算相同。

    隔天重跑、数据没变，结论一字不差、只有日期变了：要是这也算「变了」，目录就升一个版本，发布时记下的目录版本
    对不上，之后每次正式运行都提醒「目录有变化」。升级后第一次重新剖析也是一样：结论没变，只是新写法不带日期、
    措辞改了。算相同时保留旧的那段（目录不动），旧说法要等结论真的变了、重写备注时才换掉。

    起草写的备注（有效行过滤）里接着的剖析核对那句同理，只认那一句的旧说法（_LEGACY_FILTER_WORDING）。
    """
    if a == b:
        return True
    if not (isinstance(a, str) and isinstance(b, str)):
        return False
    profile_a, profile_b = is_profile_note(a), is_profile_note(b)
    if profile_a and profile_b:
        return _current_wording(_undated(a)) == _current_wording(_undated(b))
    if profile_a or profile_b:
        return False
    return (_LEGACY_FILTER_WORDING.sub("剖析核对：按存储顺序取前 ", a)
            == _LEGACY_FILTER_WORDING.sub("剖析核对：按存储顺序取前 ", b))


def _same_core(a: Mapping[str, Any], b: Mapping[str, Any]) -> bool:
    """两项除修改时间外相同（剖析备注里的日期不算，same_profile_note）。"""
    ca, cb = _core(a), _core(b)
    return same_profile_note(ca.pop("note", None), cb.pop("note", None)) and ca == cb


def visible_notes(notes: Mapping[str, Any] | None) -> dict[str, Any]:
    """去掉被驳回的项和关系。给模型看、冻结进证据、SQL 检查（data/sqlcheck.py）都只用这一份。"""
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


def _about_gone_columns(key: Slot, item: Mapping[str, Any], columns: set[str]) -> bool:
    """这一项说的列已经不在表结构里了：列级的项看列名，业务主键、业务日期看它们指着的列。"""
    if key[0] == "c":
        return key[1] not in columns
    value = item.get("value")
    if key == ("t", "keys") and isinstance(value, list):
        return any(str(col) not in columns for col in value)
    if key == ("t", "business_date") and isinstance(value, Mapping) and value.get("column") is not None:
        return str(value["column"]) not in columns
    return False


def merge_notes(existing: Mapping[str, Any] | None, draft: Mapping[str, Any] | None, *,
                covered: Iterable[str] = (), at: str | None = None,
                columns: Iterable[str] | None = None) -> tuple[dict[str, Any], MergeStats]:
    """把起草结果并入已有目录，返回 (新目录, 计数)。不改入参。

    规则（逐个槽位）：
    - 已有项是 confirmed 或 rejected：不动（确认时的依据 reason 跟着留着）。驳回的留着，同一项下次起草就不会再冒出来。
    - 起草结果里没有这一项：
      - 模型起草的项（llm）留着。模型这次留空、给的值没通过校验（_from_model 里就丢掉了），都不能把以前起草好的
        删掉——实测整库重跑一次，模型这次没给的几百项全被删掉，有的表覆盖率从八成多掉到五成。只有这一轮完整起草过
        （covered 含 llm）、而它说的列已经不在表结构里（columns：表结构里的列名；业务主键、业务日期指着的列也算）
        才删；不知道表结构（columns 为 None）时不删。
      - 别的来源：来源在 covered 里（这一轮完整算过这个来源）就删掉，否则留着。外键约束被删掉以后，原来那条「有确证」
        的关系不能还挂着；库注释改了，旧的那条注释也不能还挂着。
    - 同一来源：值或状态有变化就更新。数据剖析写的备注只是日期变了不算变化（same_profile_note），保留旧的项。
    - 不同来源：来源更可信的（_SOURCE_RANK）顶掉不如它的，反过来不行。

    新增和更新的项盖上 at（缺省为当前时间）作为 updated_at；内容没变的项保持原样，修改时间也不动。
    removed 只数真删掉的项。
    """
    at = at or now_iso()
    covered = set(covered)
    known = {str(c) for c in columns} if columns is not None else None
    old, new = _slots(existing), _slots(draft)
    out: dict[Slot, dict[str, Any]] = {}
    added = updated = removed = 0
    for key, item in old.items():
        cand = new.get(key)
        if item.get("status") in _LOCKED:
            out[key] = item
        elif cand is None:
            if item.get("source") == "llm":
                gone = "llm" in covered and known is not None and _about_gone_columns(key, item, known)
            else:
                gone = item.get("source") in covered
            if gone:
                removed += 1
            else:
                out[key] = item
        elif (cand.get("source") == item.get("source")
              or _SOURCE_RANK.get(cand.get("source"), 0) > _SOURCE_RANK.get(item.get("source"), 0)):
            if not _same_core(cand, item):
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
    """对一项做审阅，返回新目录（不改入参）。路径不对、项不存在、操作不认识都抛 CatalogPathError。

    确认时的依据（reason）：确认、驳回不带依据，原有的留着；撤销审阅回到推断，去掉——推断项上挂着「使用者说：……」，
    看起来像使用者确认过它。"""
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
    item["status"] = {"confirm": "confirmed", "reject": "rejected"}.get(action) or unreviewed_status(item)
    if action == "reset":
        item.pop("reason", None)
    item["updated_at"] = at or now_iso()
    return _assemble(slots)


def _normalize_submitted(submitted: Mapping[str, Any], table: str) -> dict[str, Any]:
    """人工提交的目录：补上没写的来源和状态（下面会按规则改写），关系编号按两端重算。"""
    out = copy.deepcopy(dict(submitted))

    def fill(item: Any) -> None:
        if isinstance(item, dict) and "value" in item:
            item.setdefault("source", "human")
            item.setdefault("status", "confirmed")
            if item.get("complete") is False:
                del item["complete"]          # 「没列全」就是没有这个标记，写法统一（make_item）

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
                     at: str | None = None, inferable: Iterable[str] = ()) -> dict[str, Any]:
    """人工提交的整份目录 → 要写入的目录（不改入参）。

    - 值改过的项、新填的项：记为 human / confirmed。提交里写的来源和状态不作数——客户端不能冒充外键约束
      或数据剖析。码值的「已列全」标记跟着值走：只勾上或取消它也算改过（_edit_value）。
    - 值没动的项：保持原来的来源和状态；提交里把状态改成确认、驳回或来源的初始状态的，照改（等同单项审阅）；
      改成别的状态的不认（不能把推断改成「有确证」）。
    - 确认时的依据（reason）只由保存修改提案写（apply_patch），提交里带的不作数：值改过、新填的项去掉（原来的依据说的
      是旧值，界面原样带回来的也不认）；值没动的留着库里的；状态改回来源的初始状态（等同撤销审阅）的去掉。
    - 已有、但提交里没有的表级项、列级项：删掉。
    - 已有、但提交里没有的关联关系：外键约束、命名推断、数据剖析、模型起草来的，转为驳回、保留编号；只有人工
      新建的才真正删掉。删掉的推断关系下次起草会再提出来，关系图（助手挑表、SQL 检查）也会按表结构现推回来，
      照样驱动检查和助手——驳回才留得住。改了两端的关系编号跟着变，旧编号在提交里就「没有了」，同样按这条处理。
      inferable 是表结构能推出来的关系编号（外键约束、命名推断，structure_relation_ids）：人工改过基数的外键关系
      记成了人工填写，删掉照样会被现推回来，也只能驳回。
    - 关系编号按两端重算（_normalize_submitted）。
    - 值改过、新填的文字项看起来像计算公式（「客单价=金额/人数」）就整份不收，和对话提案同一套判断（_formula_like）：
      公式进口径卡。只查改动过的项：库里早就有的含公式的项值没动，照常提交——不放进 validate_notes 也是这个
      道理，否则旧数据会把这张表的任何写入都挡住。

    结构不合规、有公式都抛 CatalogInvalid。
    """
    if not isinstance(submitted, Mapping):
        raise CatalogInvalid(["数据目录应为对象"])
    normalized = _normalize_submitted(submitted, table)
    if problems := validate_notes(normalized):
        raise CatalogInvalid(problems)
    at = at or now_iso()
    old, new = _slots(existing), _slots(normalized)
    formulas = [_formula_problem(key) for key, item in new.items()
                if (key not in old or _value_of(key, item) != _value_of(key, old[key]))
                and _formula_like(key, item.get("value"))]
    if formulas:
        raise CatalogInvalid(formulas)
    inferable = set(inferable)
    out: dict[Slot, dict[str, Any]] = {}
    for key, item in new.items():
        prev = old.get(key)
        if prev is None or _edit_value(key, item) != _edit_value(key, prev):
            # 「基数已用数据核实」只由数据剖析写：人工改过的关系记成人工确认，提交里带的这个标记不作数；确认时的依据只由
            # 保存修改提案写，说的是旧值，不留
            fresh = {k: v for k, v in item.items() if k not in ("updated_at", "cardinality_checked", "reason")}
            fresh.update(source="human", status="confirmed", updated_at=at)
            out[key] = fresh
            continue
        kept = dict(prev)
        status = item.get("status")
        if status != prev.get("status") and status in ("confirmed", "rejected", unreviewed_status(prev)):
            kept["status"] = status
            kept["updated_at"] = at
            if status not in ("confirmed", "rejected"):
                kept.pop("reason", None)                     # 等同撤销审阅：回到推断，确认时的依据不留
        if item.get("note") != prev.get("note"):
            if item.get("note"):
                kept["note"] = item["note"]
            else:
                kept.pop("note", None)
            kept["updated_at"] = at
        out[key] = kept
    for key, prev in old.items():
        if key in new or key[0] != "r":
            continue
        if prev.get("source") == "human" and key[1] not in inferable:
            continue                                         # 人工新建的：真正删掉
        out[key] = prev if prev.get("status") == "rejected" else {**prev, "status": "rejected", "updated_at": at}
    return _assemble(out)


def structure_relation_ids(schema_cache: Mapping[str, Any] | None, table: str) -> set[str]:
    """表结构能推出来的这张表的关系编号（外键约束、命名推断）。表结构里没有这张表时为空集。"""
    if not isinstance(((schema_cache or {}).get("tables") or {}).get(table), Mapping):
        return set()
    rels = (fk_relations(schema_cache, table) or []) + infer_name_relations(schema_cache, table)
    return {str(r["id"]) for r in rels}


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
                          if_version: int, actor: str | None,
                          schema_cache: Mapping[str, Any] | None = None) -> CatalogEntry:
    """人工提交整份目录并写入（规则见 apply_human_edit，乐观锁同 write_entry）。

    schema_cache 用来判断哪些关系表结构推得出来（去掉时只能驳回）；不给就读数据源记录上的，和 SQL 检查建关系图
    用的是同一份（sqlcheck.load_checkers）。
    """
    entry = await read_entry(session, source_id, table)
    current = entry.version if entry else 0
    if if_version != current:
        raise CatalogConflict(table, current)
    if schema_cache is None:
        row = await session.get(DataSource, source_id)
        schema_cache = getattr(row, "schema_cache", None)
    notes = apply_human_edit(entry.notes if entry else {}, submitted, table=table,
                             inferable=structure_relation_ids(schema_cache, table))
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
#: 一条提案最多几项（预览、保存都按它）。对话里的纠正通常是一两条事实；表助手（data/catalog_assistant.py）会逐列补中文名、
#: 含义，界面上跨几轮攒下的待保存修改一次保存，宽表上常常几十上百项，所以放到 200
PATCH_MAX_CHANGES = 200
#: 看起来像计算公式的文字：「转化率=下单数/访问数」「= SUM(amount)」。公式进口径卡，目录只记数据事实。
#: 不认减号：「上线日期=2024-01-01」不是公式。有效记录条件本来就是 SQL 条件，不查它。
#: 对话提案（plan_patch）、人工整份提交（apply_human_edit，只查改动过的项）、模型起草（_from_model，丢掉这一项）
#: 三条路径都用它；不放进 validate_notes——库里已有的含公式的项会把那张表的任何写入都挡住
_FORMULA = re.compile(r"[=＝][^=<>!]*?[+*/×÷]|\b(?:sum|count|avg|average)\s*\(", re.IGNORECASE)
_FORMULA_CHECKED = frozenset({("t", "label"), ("t", "description"), ("t", "grain"), ("c", "label"),
                              ("c", "meaning"), ("c", "unit"), ("c", "codes")})


@dataclass(frozen=True)
class PatchChange:
    """提案里的一项，核对过的。

    - path：规范化后的路径（新增关系写成 relations.<编号>）；
    - before / before_status：当前目录里这一项的值和状态，没有这一项为 None。被驳回的照样给出原值、状态写 rejected
      （同导入预览「目标里这一项已经驳回」）：只是给人看的，算改后、算 state 都按没有这一项，保存时换成人工确认的新值；
    - after：保存后的值（码值是补充后的完整对照）；value：要交回保存的原样取值（码值只有补充的那几个，
      409 之后在最新的目录上重新补）；
    - reason：提案的依据，按保存时的样子截过（clip_reason），没有为空串；
    - state：change 值有变化；confirm 值相同、但还不是已确认（保存即确认）；same 已经是这个值且已确认。
    - note：服务端对这一项的说明，给人看；没有时为 None，as_dict 里也不出现。目前只有一种：新增的关联关系没写
      基数、又推算不出来（_relation_after）。
    """

    path: str
    before: Any
    before_status: str | None
    after: Any
    value: Any
    reason: str
    state: str
    note: str | None = None

    def as_dict(self) -> dict[str, Any]:
        out = {"path": self.path, "before": self.before, "before_status": self.before_status, "after": self.after,
               "value": self.value, "reason": self.reason, "state": self.state}
        if self.note:
            out["note"] = self.note
        return out


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


def _formula_problem(key: Slot) -> str:
    return f"{describe_slot(key)}看起来是计算公式：公式请写进口径卡，数据目录只记数据事实"


def _patch_relation(table: str, raw: Any, path_id: str | None, tables: Mapping[str, Any] | None,
                    columns: set[str] | None) -> tuple[dict[str, Any] | None, str | None]:
    """关联关系的取值 → (关系的值部分, 问题)。值写两端（columns、to_table、to_columns），基数可选。

    返回的是提案原样的取值：基数没写就是 None，覆盖率一律 None（模型写了也不认）。保存后的值由 _relation_after
    并上目录里已有的测量结果再算。
    """
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


def _relation_after(table: str, value: Mapping[str, Any], prev: Mapping[str, Any] | None,
                    tables: Mapping[str, Any] | None) -> tuple[dict[str, Any], str | None]:
    """提案里一条关系保存后的值：(值, 给人看的说明)。

    以前直接拿提案的取值整份替换：覆盖率写成空、基数取提案原值（模型多半不写）。数据剖析测得多对一、覆盖率 100%
    的关系，经对话「确认一下」就丢了基数，一对多关联后重复计算的检查跟着失效。所以：
    - 覆盖率是数据剖析测出来的，一律沿用目录里这条关系的（包括被驳回的那条留下的），没有就是空；
    - 基数：提案写了用提案的；没写时，目录里已有这条关系就沿用它的；新增的按目标表的主键或唯一约束推算
      （目标列是键，本表这几列也是键就是一对一，否则多对一，同外键约束的 _cardinality）；推算不了就留空，
      并说明为什么空着。
    """
    out = dict(value)
    out["coverage"] = prev.get("coverage") if prev is not None else None
    if out.get("cardinality") is not None:
        return out, None
    if prev is not None:
        out["cardinality"] = prev.get("cardinality")
        return out, None
    meta = tables.get(table) if tables is not None else None
    target = tables.get(str(out.get("to_table"))) if tables is not None else None
    if not isinstance(meta, Mapping) or not isinstance(target, Mapping):
        return out, "没有写明基数，表结构里也查不到两端的主键和唯一约束，基数暂时空着；一对多关联后重复计算的检查用不上这条关系"
    if not _is_key(target, list(out.get("to_columns") or [])):
        return out, (f"没有写明基数，目标字段不是 {out.get('to_table')} 的主键或唯一约束，推算不出，基数暂时空着；"
                     "一对多关联后重复计算的检查用不上这条关系，可以在数据目录里补上基数")
    out["cardinality"] = _cardinality(meta, list(out.get("columns") or []))
    return out, None


def _merge_codes(before: Mapping[str, str], value: Mapping[str, str]) -> dict[str, str]:
    """码值的补充：给出的码加进去或改掉原来的含义。含义为空表示「只知道有这个码、含义待填写」（数据剖析的码值
    候选就是这样），所以提案里含义为空的码不覆盖已有的含义，只在原来没有这个码时加进去。"""
    out = dict(before)
    for code, meaning in value.items():
        if not str(meaning).strip() and str(out.get(code) or "").strip():
            continue
        out[code] = meaning
    return out


def plan_patch(notes: Mapping[str, Any] | None, changes: Any, *, table: str,
               tables: Mapping[str, Any] | None = None) -> PatchPlan:
    """逐项核对一条目录修改提案，算出改前、改后。不改入参、不抛异常：不合法的项写进 problems。

    changes 是 [{path, value, reason?}]。path 用审阅接口的写法：表级项写字段名，列级项 columns.<列名>.<字段>，
    关系 relations.<编号>；新增关系写 relations.new。tables 是数据源 schema_cache 的 tables：给了就核对列名、
    业务主键、业务日期列、关系两端在不在表结构里（模型会照着名字编列名）；不给不核对。

    规则：
    - 取值格式和 validate_notes 同一套（_TABLE_VALUE_RULES / _COLUMN_VALUE_RULES / _relation_problems）。
    - 码值是补充：在现有（没被驳回的）码值上加或改给出的码，不整份替换——「status=9 表示作废」不该把已确认的
      其余码值删掉。含义为空的码（数据剖析写进来的「含义待填写」）可以被补上含义；提案里含义为空的码只在原来
      没有这个码时加进去，不会把已有的含义抹掉（_merge_codes）。
    - 关联关系不抹掉测量结果：覆盖率沿用目录里的，基数没写时沿用或推算（_relation_after）。两端、基数、覆盖率都和
      目录里一样时只是确认，来源不变。
    - 被驳回的项算没有：码值不在驳回的码值上补，和驳回的值一样也是 change（保存时换成人工确认的新值）。改前照样写出
      驳回的原值、before_status 为 rejected，不然界面上只看到「未填写」，看不出原来有一个被驳回的值。
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
        reason = clip_reason(raw.get("reason")) or ""
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
            problems.append(_formula_problem(key))
            continue
        norm = f"relations.{key[1]}" if key[0] == "r" else path
        if norm in seen:
            problems.append(f"{where}在提案里出现了两次，只采用第一次")
            continue
        seen.add(norm)
        prev = slots.get(key)
        live = prev if prev is not None and prev.get("status") != "rejected" else None
        before = _value_of(key, live) if live is not None else None
        after, note = value, None
        if key[0] == "r":
            after, note = _relation_after(table, value, prev, tables)
        elif key[0] == "c" and key[2] == "codes" and isinstance(before, dict):
            after = _merge_codes(before, value)
        if before is not None and before == after:
            state = "same" if live.get("status") == "confirmed" else "confirm"
        else:
            state = "change"
        # 改前给人看：被驳回的也写出原值（上面算改后、算 state 照旧按没有这一项）
        shown = live if live is not None else prev
        out.append(PatchChange(path=norm, before=copy.deepcopy(_value_of(key, shown)) if shown is not None else None,
                               before_status=shown.get("status") if shown is not None else None,
                               after=after, value=value, reason=reason, state=state, note=note))
    return PatchPlan(out, problems)


def apply_patch(notes: Mapping[str, Any] | None, changes: Any, *, table: str,
                tables: Mapping[str, Any] | None = None, at: str | None = None) -> dict[str, Any]:
    """把一条提案并进目录，返回新目录（不改入参）。有一项不合法就整条不收，抛 CatalogInvalid。

    值有变化的项记为人工填写、已确认（和人工编辑同一条规矩：改动出自人的确认，不是模型起草）；值没变的只把
    状态改成已确认、来源不变（等同单项确认）；已经确认过的原样不动。

    提案里的依据（reason，clip_reason 截过）记进这一项的 reason（确认时的依据），和备注 note 分开：note 是起草的依据、
    剖析的测量这类机器写的说明，照旧不拼依据——以前拼进 note，再剖析时测量的说法换不了新（catalog_profile._patch_confirmed
    只换剖析自己写的备注）、旧依据叠在新依据后面、撤销审阅后还挂在推断项上。依据每次保存整句换掉、不叠加：值改了的项
    （关联关系改了值也一样）是这一次的依据，没写就没有（原来的说的是旧值）；值不变、保存即确认的也换成这一次的。
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
        reason = change.reason or None
        if change.state == "confirm" and prev is not None:
            item = {k: v for k, v in prev.items() if k != "reason"}
            item.update(status="confirmed", updated_at=at, **({"reason": reason} if reason else {}))
            slots[key] = item
            continue
        if key[0] == "r":
            rel = {"id": key[1], **change.after, "source": "human", "status": "confirmed", "updated_at": at}
            if prev is not None and prev.get("note"):
                rel["note"] = prev["note"]       # 覆盖率沿用了，说明它怎么测的那段备注也留着
            if reason:
                rel["reason"] = reason
            slots[key] = rel
        else:
            # 码值是补充、不删已有的码：原来标了「已列全」的，补充之后仍然列全。提案自己不能标「已列全」——
            # 用户在对话里说一个码值的意思，不等于确认这一列只有这些取值
            done = key[0] == "c" and key[2] == "codes" and prev is not None and prev.get("status") != "rejected" \
                and codes_complete(prev)
            slots[key] = make_item(change.after, "human", "confirmed", at=at, complete=done, reason=reason)
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
    """一张表的起草结果。covered：这一轮完整算过的来源（merge_notes 据此删掉这些来源里已经没有的旧项；模型起草的项
    只在列已经不在表结构里时才删）。

    error：模型起草只写出了一部分（有的段没写出来）时，没写出来的是哪部分、为什么。这时 covered 不含 llm：没写全
    就不算完整起草过。"""

    notes: dict[str, Any]
    covered: frozenset[str] = frozenset()
    error: str | None = None


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
    """在表结构里找表，返回 schema_cache["tables"] 的键。全名、只有表名、大小写不一致都认。

    大小写按 name_key 比：只把 ASCII 字母转小写，和 SQLite 比较标识符、使用次数（table_usage）、影响面
    （catalog_impact）的口径一致。以前这里按 Unicode 转小写，表名里有非 ASCII 大写字母时（「CAFÉ_ORDERS」）
    这里认得、那两处认不得，同一张表在目录页和影响面上对不上。
    """
    tables = (schema_cache or {}).get("tables") or {}
    if name in tables:
        return name
    short = name.rsplit(".", 1)[-1]
    if short in tables:
        return short
    key = name_key(short)
    return next((n for n in tables if name_key(n) == key), None)


#: 数据库注释多长、带不带句读，决定它是「名字」还是「解释」
_LABEL_MAX = 24
_SENTENCE_MARKS = re.compile(r"[，。；;,.!?！？：:\n]")


def _comment_is_label(text: str) -> bool:
    """短、不带句读的注释是名字（「实收金额」），否则是一段解释（「状态：1 表示有效…」）。"""
    return len(text) <= _LABEL_MAX and not _SENTENCE_MARKS.search(text)


#: 标识符写法的注释：只有字母、数字、下划线、点，不带空格（「member_card」「ord.status」「GMV」）
_IDENTIFIER = re.compile(r"[A-Za-z0-9_.]+")
#: 比较注释和名字时忽略的字符：下划线、连字符、空白
_NAME_FILLER = re.compile(r"[\s_\-]+")


def _bare_name(text: str) -> str:
    return _NAME_FILLER.sub("", text).lower()


def comment_role(text: str, name: str) -> str | None:
    """数据库注释在目录里记成什么：label 中文名；note 一段解释（表记进说明，列记进含义）；None 不记。

    - 注释就是名字本身（和表名、列名相同，忽略大小写、下划线、连字符和空白；表名带 schema 前缀的也比不带前缀的），
      或者是标识符写法（只有字母、数字、下划线、点，不带空格）：说明不了什么，不记。以前这种注释短、不带句读，就当
      中文名记下了；库注释的级别又比模型起草高，模型起草的中文名永远进不了目录。交给模型的表结构里照旧有这条注释；
    - 短、不带句读的是名字（「实收金额」）；
    - 否则是一段解释（「状态：1 表示有效…」）。

    只影响以后的起草：已有的目录不另做迁移，起草到那张表时按这一轮的注释重新算（旧的那条随之删掉）。
    """
    bare = _bare_name(text)
    names = {name, name.rsplit(".", 1)[-1]}
    if not bare or any(bare == _bare_name(n) for n in names) or _IDENTIFIER.fullmatch(text):
        return None
    return "label" if _comment_is_label(text) else "note"


def _is_key(meta: Mapping[str, Any], columns: list[str]) -> bool:
    """这几列恰好是这张表的主键或某个唯一约束（不分大小写、不计顺序）。"""
    wanted = {str(c).lower() for c in columns}
    keys = [meta.get("primary_key") or []] + [u for u in meta.get("unique") or [] if isinstance(u, list)]
    return bool(wanted) and any(wanted == {str(c).lower() for c in k} for k in keys if k)


def _cardinality(meta: Mapping[str, Any], columns: list[str]) -> str:
    """本表这几列指向别的表（的键）时的基数：这几列恰好是本表的主键或某个唯一约束，就是一对一，否则多对一。"""
    return "one_to_one" if _is_key(meta, columns) else "many_to_one"


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


def name_words(name: str) -> list[str]:
    """名字切成小写的词：下划线和驼峰都是分隔（memberTag、member_tag、MEMBER_TAG 都是 member + tag）。

    命名推断、数据剖析认码值列和日期列都用这一个切法（以前剖析另写了一份一样的）。
    """
    return [w.lower() for part in name.split("_") for w in _WORDS.findall(part)]


#: 名字像日期、时间的列，切词后的最后一个词：visit_time、ordered_at、joined_on、createdAt、biz_dt、load_ts……
_DATE_TAILS = frozenset({"at", "on", "date", "time", "day", "dt", "ts", "timestamp", "datetime"})
#: 单独做列名也算日期的词。at、on 单独做列名不是日期
_DATE_WORDS = frozenset({"date", "time", "day", "dt", "ts", "timestamp", "datetime"})
_TEXT_TYPES = ("CHAR", "TEXT", "STRING", "CLOB")


def is_date_column(name: str, type_name: str | None, *, enum_values: Any = None) -> bool:
    """日期类列：类型是日期、时间戳（DATE、DATETIME、TIMESTAMP……）；或者类型是文字（或者不知道类型）、名字像日期
    （SQLite 的日期常存成文字）。只有时刻的 TIME、整数存的时间戳不算：取值不是日期。有取值列表的（MySQL 的 ENUM /
    SET、探查记下 enum_values 的 PostgreSQL 枚举）不算：PG 的枚举编译出来是类型名本身，update_reason 里也有「DATE」。

    数据剖析（表里只有一个日期类列时提议它作业务日期）和 SQL 检查（按业务日期以外的时间列统计）共用这一个判断。
    以前两边各写一套：剖析认的「唯一日期列」，SQL 检查可能认为还有别的时间列，反过来也一样，两边对不上。
    """
    t = _type_words(type_name)
    if enum_values or _MYSQL_ENUM.match(t) or "INTERVAL" in t:
        return False
    if "DATE" in t or "TIMESTAMP" in t:
        return True
    if t and not any(k in t for k in _TEXT_TYPES):
        return False
    words = name_words(name)
    return bool(words) and words[-1] in _DATE_TAILS and (len(words) > 1 or words[0] in _DATE_WORDS)


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
        words = name_words(name)
        if not words:
            continue
        prefix = "".join(words[:-1])
        for form in _singulars(words[-1]):
            index.setdefault((prefix, form), set()).add(name)
    return index


#: 类型写法里带引号的部分（ENUM('int','date') 的取值）：认类型时去掉，取值里的词不能让一列变成数字或时间
_QUOTED_IN_TYPE = re.compile(r"'(?:[^']|'')*'")


def _type_words(type_name: str | None) -> str:
    """认类型用的写法：大写、去掉引号里的取值。探查存的是按方言编译的完整写法（ENUM('paid','refund')、
    VARCHAR2(20 CHAR)、TIMESTAMP WITH TIME ZONE），不再是 SQLAlchemy 的通用写法。"""
    return _QUOTED_IN_TYPE.sub("''", (type_name or "").upper()).strip()


#: MySQL 的 ENUM / SET 编译出来的写法：ENUM('a','b')、SET('x','y')，只认带括号的。PostgreSQL 的枚举编译出来是类型名本身，
#: setup_status、enum_kind 这种以 set、enum 开头的名字不能按前缀认成 MySQL 的 SET / ENUM——PG 的枚举一律靠探查记下的
#: enum_values 认（column_family）
_MYSQL_ENUM = re.compile(r"^(ENUM|SET)\s*\(")


def _mysql_enum_kind(type_name: str | None) -> str | None:
    """类型写法是 MySQL 的 ENUM(…) / SET(…) 时返回 "ENUM" / "SET"，否则 None。"""
    m = _MYSQL_ENUM.match(_type_words(type_name))
    return m.group(1) if m else None


def _type_family(type_name: str | None) -> str | None:
    """列类型的大类：数字、文字、时间；认不出来返回 None（不据此排除）。

    认的是按方言编译的完整写法：MySQL 的 ENUM / SET 是文字（取值列表里的词不算），Oracle 的 INTERVAL DAY TO SECOND
    是时长、不算数字（以前按子串「INT」认成了数字）。PostgreSQL 的枚举编译出来是类型名本身，只看类型认不出来，
    要看列上的取值列表（column_family）。"""
    t = _type_words(type_name)
    if not t:
        return None
    if _MYSQL_ENUM.match(t):
        return "text"
    if "INTERVAL" in t:
        return None
    if any(k in t for k in ("CHAR", "TEXT", "CLOB", "STRING", "UUID")):
        return "text"
    if any(k in t for k in ("INT", "NUMBER", "NUMERIC", "DECIMAL", "SERIAL", "REAL", "FLOAT", "DOUBLE")):
        return "number"
    if any(k in t for k in ("DATE", "TIME")):
        return "time"
    return None


def column_family(col: Mapping[str, Any] | None) -> str | None:
    """表结构里一列的类型大类：有取值列表（探查记下的 enum_values：MySQL 的 ENUM / SET、PostgreSQL 的枚举）的是
    文字，其余按类型写法认（_type_family）。"""
    if not isinstance(col, Mapping):
        return None
    if col.get("enum_values"):
        return "text"
    return _type_family(col.get("type"))


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
        words = name_words(stem)
        if not words or len("".join(words)) < 2:
            continue
        prefix = "".join(words[:-1])
        candidates: set[str] = set()
        for form in _singulars(words[-1]):
            candidates |= index.get((prefix, form), set())
        if not candidates:
            # 直接对不上：库里的表常带业务前缀（store_tags），列名却只写词根（tagId）。
            # 只认和引用它的表共用前缀的那张，从最长的共用前缀试起；仍然不止一张就不推
            own = name_words(table)
            for k in range(len(own) - 1, 0, -1):
                for form in _singulars(words[-1]):
                    candidates |= index.get(("".join(own[:k]) + prefix, form), set())
                if candidates:
                    break
        if len(candidates) != 1:
            continue
        target = next(iter(candidates))
        if target == table:
            continue
        target_meta = tables.get(target) or {}
        pk = list(target_meta.get("primary_key") or [])
        if len(pk) != 1:
            continue
        pk_col = next((c for c in target_meta.get("columns") or [] if c.get("name") == pk[0]), None)
        mine, theirs = column_family(col), column_family(pk_col)
        if mine and theirs and mine != theirs:
            continue
        out.append(_relation(table, [name], target, pk, "name", cardinality=_cardinality(meta, [name])))
    return out


def draft_structure(source: Any, table: str) -> TableDraft:
    """只看表结构就能起草的部分：数据库注释、外键约束、命名推断。table 必须是 schema_cache["tables"] 的键。

    - 数据库注释 → 中文名或含义（comment / proposed）：短而不带句读的是名字，记进表或列的中文名；否则是
      一段解释，记进表的说明或列的含义；就是名字本身、标识符写法的不记（comment_role）。导入表格的源例外
      （system_notes_source）：说明是系统生成的，不拷。
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
        role = comment_role(comment, table) if comment else None
        if role is not None:
            notes["label" if role == "label" else "description"] = make_item(comment, "comment")
        columns: dict[str, dict[str, Any]] = {}
        for col in meta.get("columns") or []:
            text = (col.get("comment") or "").strip()
            role = comment_role(text, str(col["name"])) if text else None
            if role is not None:
                columns[col["name"]] = {("label" if role == "label" else "meaning"): make_item(text, "comment")}
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

#: 每次调用最多几张表、合计多少列（data/catalog_draft.py 按它分段）。实测每列约 50 个输出 token（不算思考），
#: 会思考的模型思考也算在输出额度里：一段太大，想完就没额度写正文。列数超过 MODEL_BATCH_COLUMNS 的宽表拆成
#: 「表级一段 + 列分段」，列分段数是列数除以它向上取整，各段列数平均（相差不超过 1），不留很小的尾段
MODEL_BATCH_TABLES = 4
MODEL_BATCH_COLUMNS = 40
#: 一次起草里额度用完或超时累计这么多次，之后新开始的段列数上限减半（MODEL_BATCH_COLUMNS → 一半 → …），
#: 最低 MODEL_MIN_PART_COLUMNS 列
MODEL_SHRINK_AFTER = 2
MODEL_MIN_PART_COLUMNS = 10
#: 一段（连同它拆出来的、补问的、重试的）最多调用几次模型，到了次数还没写完的记没写出来
MODEL_PART_MAX_CALLS = 8
#: 提醒：最近 MODEL_NOTICE_WINDOW 次调用里有 MODEL_NOTICE_ON 次以上没写出来时提醒换模型；之后少于 MODEL_NOTICE_OFF
#: 次时清掉
MODEL_NOTICE_WINDOW = 6
MODEL_NOTICE_ON = 4
MODEL_NOTICE_OFF = 2
#: 全进程同时进行的模型调用最多几个（所有起草任务、同步起草共用）
MODEL_CONCURRENCY = 3
#: 单次调用最多等多久（秒）。会思考的模型想得久，一段 40 列常常要几分钟；超时了拆小再问（同额度用完）
MODEL_TIMEOUT_S = 420
#: 输出额度。会思考的模型（经 OpenAI 兼容网关的推理模型等）思考也算在里面：8192 时上百列的表常常想完就没额度
#: 写正文。保留思考（不关），加大额度；服务方嫌额度太大时退回 MODEL_FALLBACK_MAX_TOKENS（同 copilot、recipe_ai）
MODEL_MAX_TOKENS = 32768
MODEL_FALLBACK_MAX_TOKENS = 8192
#: 一段没写出来（额度用完、只给空壳）时拆成两半再问：最多拆几层、拆到最少几列为止
MODEL_SPLIT_DEPTH = 3
MODEL_SPLIT_MIN_COLUMNS = 5
#: 止损（按比例）：这次起草最近 MODEL_HALT_WINDOW 次调用里有 MODEL_HALT_ON 次以上没写出来（额度用完、超时、空壳、出错），
#: 之后的段不再拆小、每块只问一次，直到比例降下去。模型明显用不了时不该一段一段拆到底烧额度；以前按「连续几段」算，
#: 偶尔一次写出来就清零，会思考的模型在宽表上 90 分钟停不下来。每段另有 MODEL_PART_MAX_CALLS 次封顶
MODEL_HALT_WINDOW = 10
MODEL_HALT_ON = 7
#: 同一张表连续这么多个原始段都一项没写出来，这张表剩下的段跳过不问（可以换一个起草模型后只重试这张表）
MODEL_TABLE_GIVE_UP_AFTER = 3
#: 网络错误、429、5xx 最多重试几次，每次重试前等多久（秒，依次取用）。超时不原样重试，当作这一段太大、拆小再问
MODEL_RETRIES = 2
MODEL_RETRY_DELAYS_S = (10.0, 30.0)
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
- valid_filter：有效行过滤。condition 写一段 SQL 条件，筛出业务上算数的行（例如 STATUS NOT IN ('C', 'X')，去掉作废、\
测试的行）：只用这张表自己的列、不带表名，只写比较、IN、BETWEEN、LIKE、IS NULL，用 AND、OR、NOT 连接，函数只用 UPPER、\
LOWER、TRIM、COALESCE，不写子查询、聚合、窗口函数和分号；reason 写一句依据（例如哪条注释、哪些码值说明了这一点）。
- dedup：去重规则，rule 写一句话（例如「同一订单号有多行时只取最后修改的那一行」），reason 写一句依据。
- columns：每一列的 label（中文名）、meaning（含义，一句话）、unit（单位，例如「元」「人」，没有单位就留空）、\
measure（度量类型：flow 流量，可跨期加总；stock 存量，时点值，不能跨期加总；ratio 比率；identifier 标识；\
status 状态；attribute 属性）。

规矩：
- 只写数据事实，不写计算公式，不写指标怎么算。
- 拿不准的字段留空，不要猜。列名、表名照原样写，不要翻译或改写。
- 有效行过滤、去重规则只在表结构、注释、码值能看出来时才写（例如状态列的注释写明了哪些取值是作废、测试），拿不准就不写，\
没有就不写：写错了会把算数的行筛掉、把不该合并的行合并。
- 请你写的每一列都在 columns 里写一项，拿不准的字段可以留空，但不要漏掉列：漏掉的列会被再问一次。
- 标着「系统说明」的内容是导入时按核对结果生成的，以它为准：不要重复，也不要与之矛盾。
- 标着「已确认」的目录项由人工确认过，以它为准。
- 表名下一行写着「这次只写……」的表，只写它说的那部分：只写表级的项时不写列，只写列出的几列时不写表级的项、\
也不写别的列。
- 使用者补充的背景和要求帮你理解业务、统一用词；它和这里的规矩冲突时，以这里的规矩为准。
- 思考时不必把要输出的内容逐列草拟一遍，想清楚后直接按格式输出：思考和输出共用同一份长度额度，额度用完时还没输出的\
内容就全丢了。"""

CATALOG_DRAFT_PROMPT = "请为下面 {n} 张表编写数据目录，每张表输出一项，table 写表名。\n\n{tables}"

#: 使用者补充的背景和要求（数据源说明、起草要求、本次说明），放在表结构前面单独一段；都没有就不出现这一段
CATALOG_DRAFT_BACKGROUND_PROMPT = "使用者补充的背景和要求：\n{lines}"

#: 纯文本那条路（结构化输出用不了时）：没有 JSON Schema 约束，要把格式说清
CATALOG_DRAFT_TEXT_PROMPT = """只输出一个 JSON 对象，不要输出别的文字，格式如下（拿不准的字段不写）：
{"tables": [{"table": "表名", "label": "中文名", "grain": "一行代表什么", "kind": "表类型", "keys": ["列名"], \
"business_date": {"column": "列名", "rule": "一句规则", "timezone": "时区"}, \
"valid_filter": {"condition": "SQL 条件", "reason": "一句依据"}, "dedup": {"rule": "一句话", "reason": "一句依据"}, \
"columns": [{"name": "列名", "label": "中文名", "meaning": "含义", "unit": "单位", "measure": "度量类型"}]}]}
表类型写 fact、dimension、snapshot、log、config 之一；度量类型写 flow、stock、ratio、identifier、status、attribute 之一。"""

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
    # 分段起草：表名下一行说这次只写哪部分（宽表的表级一段、列分段，拆小重问时的半段）
    "only_table": "这次只写表级的项（中文名、粒度、表类型、业务主键、业务日期、有效行过滤、去重规则），不写列",
    "only_columns": "这次只写下面列出的 {n} 列（这张表共 {total} 列），不写表级的项",
    "table_and_columns": "这次只写表级的项和这 {n} 列：{cols}",
    "brief_columns": "列（共 {n} 列，这里只列名字和类型）：",
    "drafted": "已起草的表级内容：",
    # 开了数据剖析的源：目录里记下的码值（剖析看到的取值和已知含义）交给模型，判断有效行过滤用；遮罩的列不给
    "codes": "目录里记下的码值（数据剖析看到的取值和已知含义，可据此判断有效行过滤）：",
    "code": "- {name}：{codes}",
    # 使用者补充的背景和要求
    "description": "数据源说明：{text}",
    "guidance": "起草要求：{text}",
    "note": "本次说明：{text}",
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
                    "valid_filter": {
                        "type": "object",
                        "description": "有效行过滤：只在表结构、注释、码值能看出来时才写",
                        "properties": {"condition": {"type": "string", "description": "SQL 条件，只用这张表的列"},
                                       "reason": {"type": "string", "description": "一句依据"}},
                        "required": ["condition"],
                    },
                    "dedup": {
                        "type": "object",
                        "description": "去重规则：只在表结构、注释能看出来时才写",
                        "properties": {"rule": {"type": "string", "description": "一句话"},
                                       "reason": {"type": "string", "description": "一句依据"}},
                        "required": ["rule"],
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

class DraftModel:
    """起草用的模型：先按 MODEL_MAX_TOKENS 的额度调；服务方嫌额度太大（budget_rejected）时换成 MODEL_FALLBACK_MAX_TOKENS
    的那一个，之后的调用都用它（shrink）。一次起草（一个起草任务、一次同步起草）共用一个，退回之后不再先失败一次。

    只转发起草用到的两个方法：with_structured_output 每次都取当前那个模型的，ainvoke 同理。"""

    def __init__(self, big: Any, small: Any | None, model_name: str = "") -> None:
        self.model, self._small = big, small
        self.model_name = model_name
        self.budget = MODEL_MAX_TOKENS

    def shrink(self) -> bool:
        """换成小额度的模型。已经换过、没有小的，返回 False。"""
        if self._small is None or self.model is self._small:
            return False
        self.model, self.budget = self._small, MODEL_FALLBACK_MAX_TOKENS
        return True

    def with_structured_output(self, schema: Any, **kwargs: Any) -> Any:
        return self.model.with_structured_output(schema, **kwargs)

    async def ainvoke(self, messages: list[Any]) -> Any:
        return await self.model.ainvoke(messages)


async def resolve_draft_model(session: AsyncSession) -> tuple[Any, str]:
    """(模型, 模型 id)：用哪个接入、哪个模型按 data/draft_model.py 的顺序（设置里的起草模型 > 助手的模型 > 默认接入），
    和按配方导入的 AI 起草共用。没有可用的模型抛 CatalogModelUnavailable。

    不关思考（spec 里不传 thinking，按接入默认）、温度 0、额度 MODEL_MAX_TOKENS，服务方嫌大时退回
    MODEL_FALLBACK_MAX_TOKENS（DraftModel）。单次调用的时限 MODEL_TIMEOUT_S 也交给 SDK，免得它先按全局的 5 分钟断开。
    SDK 自己不重试（max_retries=0）：429、5xx、网络错误由起草引擎退避重试（最多 MODEL_RETRIES 次）；超时当作这一段
    太大，拆小再问（data/catalog_draft.py），不原样重试。不然两层叠起来一段最多发 9 次请求，报错却写「已重试 2 次」，
    网关正在限流时还会放大流量。
    只建模型对象，不发请求。

    和 recipe_ai.resolve_draft_model 一样不用 get_chat_model：它在没有接入时静默退回演示模型，拿演示模型
    起草出来的目录会被当成真的写进库里。
    """
    from app.data.draft_model import DraftModelUnavailable, draft_unavailable, resolve_draft_provider
    from app.providers.factory import ModelSpec, ProviderNotConfigured, build_chat_model

    try:
        provider, choice = await resolve_draft_provider(
            session, none_reason="未配置模型接入，无法由模型起草数据目录。请到「设置 → 模型接入」添加",
            mock_reason="当前只有演示用的模型接入，无法用于起草数据目录。请到「设置 → 模型接入」添加真实的模型接入")
    except DraftModelUnavailable as e:
        raise CatalogModelUnavailable(str(e)) from e
    spec = ModelSpec(provider=choice.provider, model=choice.model, max_tokens=MODEL_MAX_TOKENS, temperature=0,
                     timeout=MODEL_TIMEOUT_S, max_retries=0)
    try:
        big = build_chat_model(provider, spec)
        small = build_chat_model(provider, spec.model_copy(update={"max_tokens": MODEL_FALLBACK_MAX_TOKENS}))
    except ProviderNotConfigured as e:
        raise CatalogModelUnavailable(draft_unavailable(e, choice.origin)) from e
    model_id = spec.model or provider.default_model or ""
    return DraftModel(big, small, model_id), model_id


def _confirmed_only(notes: Mapping[str, Any] | None) -> dict[str, Any]:
    return _assemble({k: v for k, v in _slots(notes).items() if v.get("status") in ("confirmed", "verified")})


#: 模型给了这张表、但一项可用内容都没有（只有表名的空壳，或者给的值全不合规）：这张表记模型失败，不算起草成功。
#: 不然界面只看到「新增 0 项」，会说成「没有变化」
MODEL_EMPTY_REASON = "模型没有给出这张表的可用内容"


def _model_tables(raw: Any) -> list[dict[str, Any]] | None:
    """模型的回复 → 每张表一项的列表。结构化输出给的对象、正文里的 JSON 都认；对不上格式返回 None。

    实测模型有时不带 tables 外壳、只回单个表对象（{"table": "x", "label": …}），也认成一张表。
    """
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
        raw = [raw] if "tables" not in raw and raw.get("table") else raw.get("tables")
    if not isinstance(raw, list):
        return None
    return [t for t in raw if isinstance(t, dict)]


def _clean_text(value: Any, limit: int = _TEXT_MAX) -> str | None:
    if not isinstance(value, str):
        return None
    text = " ".join(value.split())
    return text if text and len(text) <= limit else None


#: 模型给的依据（有效行过滤、去重规则的 reason）写进这一项的 note
FACT_REASON_NOTE = "依据：{reason}"
#: 有效行过滤按数据源的写法整理（列名加了引号）以后超过目录里这一项的长度上限
FACT_FILTER_TOO_LONG = "{label}按数据源的写法整理后超过 {limit} 字"


def _model_fact(raw: Any, key: str) -> tuple[str, str | None] | None:
    """模型给的有效行过滤、去重规则 → (取值, 依据)。约定是对象（{condition 或 rule, reason}），只写了一段文字的也认；
    取值不是非空文字的不要。"""
    if isinstance(raw, str):
        text, reason = raw, None
    elif isinstance(raw, Mapping):
        text, reason = raw.get(key), raw.get("reason")
        if text is None:
            text = raw.get("value")
    else:
        return None
    if not isinstance(text, str) or not text.strip():
        return None
    return text.strip(), _clean_text(reason)


def _fact_item(value: str, reason: str | None) -> dict[str, Any]:
    return make_item(value, "llm", note=FACT_REASON_NOTE.format(reason=reason) if reason else None)


def _model_facts(entry: Mapping[str, Any], meta: Mapping[str, Any], *, kind: str | None, table: str | None,
                 dropped: list[tuple[str, str]] | None = None) -> dict[str, Any]:
    """模型提议的有效行过滤、去重规则（llm / proposed，依据写进 note）。

    有效行过滤按数据源的方言解析成一个条件、只能用这张表的列（sqlcheck.parse_valid_filter），不过就丢掉、不算这张表
    失败：服务端日志记一条，dropped 给了就记下 (原因, 模型写的条件)，起草引擎把它写进这张表的日志（起草任务逐表的
    log）。存的是 sqlcheck.stored_valid_filter 整理过的写法：原文照旧，只把列换成表结构里的真名、不带表名，按数据源的
    需要加引号——剖析核对的、SQL 检查让模型照着加的都是这一条。去重规则是一句话，同文字项的规矩。"""
    from app.data import sqlcheck

    out: dict[str, Any] = {}
    found = _model_fact(entry.get("valid_filter"), "condition")
    if found is not None:
        text, reason = found
        parsed = sqlcheck.parse_valid_filter(text, meta, kind=kind, table=table)
        problem = parsed.problem
        if problem is None:
            stored = sqlcheck.stored_valid_filter(parsed, kind)
            if _text_ok(stored):
                out["valid_filter"] = _fact_item(stored, reason)
            else:
                # 加了引号以后超过上限：写进去会让整张表写入失败（validate_notes），不收
                problem = FACT_FILTER_TOO_LONG.format(label=TABLE_FIELD_LABEL["valid_filter"], limit=_TEXT_MAX)
        if problem is not None:
            import logging

            logging.getLogger(__name__).info("起草数据目录：表 %s 的有效行过滤没有通过校验，已丢掉：%s（%s）",
                                             table or meta.get("qualified") or entry.get("table"), problem, text[:200])
            if dropped is not None:
                dropped.append((problem, text))
    found = _model_fact(entry.get("dedup"), "rule")
    if found is not None and (text := _clean_text(found[0])) is not None:
        out["dedup"] = _fact_item(text, found[1])
    return out


def _from_model(entry: Mapping[str, Any], meta: Mapping[str, Any], *, system: bool, source_kind: str | None = None,
                table: str | None = None, dropped: list[tuple[str, str]] | None = None) -> dict[str, Any]:
    """模型给的一张表 → 目录项（llm / proposed）。不合规的值整项丢掉，不硬塞：列名对不上表结构的、
    表类型和度量类型不在取值里的、业务主键里有不存在的列的、看起来是计算公式的（_formula_like，公式进口径卡）。
    该是文字的地方写成了数组、对象（列名、表类型、业务日期的列、度量类型……）同样丢掉，不抛异常：实测见过把列名
    写成数组的，以前在这里抛 TypeError，整张表记出错。导入表格的源，说明已经覆盖的字段也丢掉。

    有效行过滤、去重规则见 _model_facts：source_kind 是数据源类型（按它的方言解析条件，不认识的类型不收有效行过滤），
    table 是表名（条件里的列可以带这张表的表名），dropped 收没通过校验的有效行过滤 (原因, 模型写的条件)。"""
    columns = {c["name"]: c for c in meta.get("columns") or []}
    notes: dict[str, Any] = {}
    table_has_system_note = system and bool((meta.get("comment") or "").strip())
    for name in ("label", "grain"):
        if name == "grain" and table_has_system_note:
            continue
        if (text := _clean_text(entry.get(name))) is not None and not _formula_like(("t", name), text):
            notes[name] = make_item(text, "llm")
    kind = entry.get("kind")
    if isinstance(kind, str) and kind in TABLE_KINDS:
        notes["kind"] = make_item(kind, "llm")
    keys = entry.get("keys")
    if _str_list_ok(keys) and all(k in columns for k in keys):
        notes["keys"] = make_item(list(dict.fromkeys(keys)), "llm")
    date = entry.get("business_date")
    if isinstance(date, dict) and isinstance(date.get("column"), str) and date["column"] in columns:
        value = {"column": date["column"]}
        for k in ("rule", "timezone"):
            if (text := _clean_text(date.get(k))) is not None:
                value[k] = text
        notes["business_date"] = make_item(value, "llm")
    notes.update(_model_facts(entry, meta, kind=source_kind, table=table, dropped=dropped))
    out_cols: dict[str, dict[str, Any]] = {}
    listed = entry.get("columns")
    for col in listed if isinstance(listed, list) else []:
        if not isinstance(col, dict) or not isinstance(col.get("name"), str) or col["name"] not in columns:
            continue
        name = col["name"]
        has_system_note = system and bool((columns[name].get("comment") or "").strip())
        items: dict[str, Any] = {}
        for field_name, limit in (("label", _TEXT_MAX), ("meaning", _TEXT_MAX), ("unit", 50)):
            if has_system_note and field_name in ("meaning", "unit"):
                continue
            text = _clean_text(col.get(field_name), limit)
            if text is not None and not _formula_like(("c", name, field_name), text):
                items[field_name] = make_item(text, "llm")
        measure = col.get("measure")
        if isinstance(measure, str) and measure in COLUMN_MEASURES:
            items["measure"] = make_item(measure, "llm")
        if items:
            out_cols[name] = items
    if out_cols:
        notes["columns"] = out_cols
    return notes


async def draft_with_model(model: Any, source: Any, tables: list[str], *,
                           existing: Mapping[str, Mapping[str, Any]] | None = None,
                           guidance: str | None = None, note: str | None = None) -> dict[str, TableDraft | str]:
    """用模型起草这些表的中文名、粒度、表类型、业务主键、业务日期和每列的中文名、含义、单位、度量类型。

    实现在 data/catalog_draft.py（分段、按失败原因处理、捡回被截断的部分）；这里是给老调用方的形状：返回
    {表名: TableDraft 或失败原因}。全部写出来的 covered 是 {"llm"}；只写出一部分的仍是 TableDraft，covered 为空、
    error 写明哪部分没写出来；一项都没写出来的是失败原因。表结构里没有的表不在结果里。

    model 要有 with_structured_output(schema) 或 ainvoke(messages)（测试注入假模型）。交给模型的只有表结构和使用者
    补充的背景（数据源说明、guidance 起草要求、note 本次说明），不发任何数据值；existing 里已确认、有确证的项作为
    「以此为准」的背景一起交过去。
    """
    from app.data import catalog_draft

    outcomes = await catalog_draft.run_model_draft(
        model, source, tables, existing=existing, background=catalog_draft.Background(guidance=guidance, note=note))
    out: dict[str, TableDraft | str] = {}
    for name, outcome in outcomes.items():
        if outcome.status == "failed":
            out[name] = outcome.error or MODEL_EMPTY_REASON
        else:
            out[name] = TableDraft(notes=outcome.notes, covered=frozenset({"llm"}) if outcome.status == "ok"
                                   else frozenset(), error=outcome.error)
    return out


# ==========================================================================
# 起草并写入
# ==========================================================================


@dataclass
class TableDraftResult:
    """一张表这次起草的结果。error：这张表没起草（表结构里没有、写入一直冲突）；model_error：只是模型那部分失败，
    或者只写出了一部分（哪部分没写出来、为什么）。"""

    table: str
    added: int = 0
    updated: int = 0
    removed: int = 0
    version: int = 0
    error: str | None = None
    model_error: str | None = None
    #: 模型给了这张表几项可用内容（并入目录之前，按项数）。没用模型、模型这部分一项都没写出来时为 None。
    #: 大于 0 而新增、更新、删除都是 0，才是「模型给了内容、只是和现有目录一致」
    model_items: int | None = None
    #: ok 写入了（用模型时各段都写出来了）、partial 模型只写出一部分、failed 没写入或模型一段都没写出来、
    #: skipped 表结构里没有这张表
    status: str = "ok"
    #: 模型分几段起草（不用模型时为 None）
    parts_total: int | None = None
    #: 模型拿不准、留空了的列（catalog_draft.ModelOutcome.blank，按表结构的顺序），只留并入目录之后仍然没有中文名和
    #: 含义的；表的状态不因此变
    blank: list[str] = field(default_factory=list)


@dataclass
class DraftReport:
    """一次起草的结果：每张表各一项，以及模型用没用上。"""

    tables: list[TableDraftResult] = field(default_factory=list)
    #: 这次有没有调模型、调的是哪个
    model_used: bool = False
    model: str = ""
    #: 模型整体用不了的原因（没有接入等）；单张表的模型失败记在 TableDraftResult.model_error
    model_error: str | None = None


#: 表结构里没有要起草的表（被删了、改名了，或者名字写错了）
def missing_table_error(name: str) -> str:
    return f"表结构里没有 {name}，请先重新探查结构"


#: 写入一直撞上别人刚改过（乐观锁重读重并三次仍冲突）
WRITE_CONFLICT_ERROR = "这张表的数据目录正被其他人修改，请稍后再起草"


async def write_draft(session: AsyncSession, source: Any, table: str, modeled: Any = None, *, at: str,
                      actor: str | None) -> TableDraftResult:
    """一张表的起草并入目录并写入：数据库注释、外键约束、命名推断照做，modeled（catalog_draft.ModelOutcome，
    没用模型时为 None）有可用内容的再并上。同步起草和后台起草任务（每张表一结束就调）共用这一处。

    - 并入已有目录按 merge_notes 的规则：人工确认、驳回过的项不动。内容没变不写、不升版本。
    - 模型这次没给的项（留空、没通过校验）不删以前起草的；列已经不在表结构里的（业务主键、业务日期指着的列也算），
      整张表起草完（各段都写出来的 ok、只写出一部分的 partial 都一样）就按完整覆盖的规则删掉：删的只是说已经不存在的
      列的模型起草项，和这次写没写全无关。后台任务里宽表每写出一段就先并入一次（catalog_jobs.write_part，那一次不删
      任何东西），这里是整张表的收尾。
    - 模型留空的列（blank）按并入之后的目录再筛一遍：已经有中文名或含义的（以前起草的、库注释、起草进行中有人填的）
      不算留空。
    - 写入撞上别人刚改过（乐观锁）就重读重并，最多三次。
    """
    result = TableDraftResult(table=table)
    structure = draft_structure(source, table)
    meta = ((getattr(source, "schema_cache", None) or {}).get("tables") or {}).get(table) or {}
    columns = [str(c["name"]) for c in meta.get("columns") or [] if isinstance(c, dict) and c.get("name") is not None]
    model_draft: TableDraft | None = None
    if modeled is not None:
        result.parts_total = modeled.parts_total
        result.model_error = modeled.error
        if modeled.items:
            result.model_items = modeled.items
            result.blank = list(getattr(modeled, "blank", None) or [])
            model_draft = TableDraft(notes=modeled.notes, covered=frozenset({"llm"})
                                     if modeled.status in ("ok", "partial") else frozenset())
    for _attempt in range(3):
        entry = await read_entry(session, source.id, table)
        version = entry.version if entry else 0
        merged, stats = merge_notes(entry.notes if entry else {}, structure.notes, covered=structure.covered, at=at,
                                    columns=columns)
        if model_draft is not None:
            merged, more = merge_notes(merged, model_draft.notes, covered=model_draft.covered, at=at, columns=columns)
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
        result.error = WRITE_CONFLICT_ERROR
        result.added = result.updated = result.removed = 0
        result.blank = []
    if result.blank:
        named = visible_notes(merged).get("columns") or {}
        result.blank = [c for c in result.blank if not any(f in (named.get(c) or {}) for f in ("label", "meaning"))]
    if result.error or (modeled is not None and modeled.status == "failed"):
        result.status = "failed"
    elif modeled is not None and modeled.status == "partial":
        result.status = "partial"
    return result


def pick_tables(cache: Mapping[str, Any], wanted: Iterable[str]) -> tuple[list[str], list[str]]:
    """要起草的表 → (表结构里的键，去重、按给的顺序；表结构里没有的原样名字)。"""
    picked: list[str] = []
    unknown: list[str] = []
    for name in dict.fromkeys(str(t) for t in wanted):
        key = resolve_table_name(cache, name)
        if key is None:
            unknown.append(name)
        elif key not in picked:
            picked.append(key)
    return picked, unknown


async def default_tables(session: AsyncSession, source: Any) -> list[str]:
    """不指定表时起草哪些：按使用次数（table_usage）取前 DRAFT_DEFAULT_TABLES 张，次数相同按表结构里的顺序。"""
    all_tables = (getattr(source, "schema_cache", None) or {}).get("tables") or {}
    usage = await table_usage(session, source)
    order = {name: i for i, name in enumerate(all_tables)}
    return sorted(all_tables, key=lambda n: (-usage.get(n, 0), order[n]))[:DRAFT_DEFAULT_TABLES]


async def draft_catalog(session: AsyncSession, source: Any, *, tables: list[str] | None = None,
                        use_model: bool = False, model: Any = None, actor: str | None = None,
                        guidance: str | None = None, note: str | None = None) -> DraftReport:
    """起草这些表的目录并写入，同步完成（POST /catalog/draft；界面改用后台起草任务，见 data/catalog_jobs.py）。

    - source：手工源的 DataSource，或上传源绑定当前快照的视图（table_versions.resolve_source）；按 source.id 存。
    - tables 为空时按使用次数取前 DRAFT_DEFAULT_TABLES 张（default_tables）。
    - 每张表：数据库注释、外键约束、命名推断照做；use_model 时再加模型起草（data/catalog_draft.py：分段、按失败
      原因处理）。model 可注入（测试用假模型），不给就按设置解析（resolve_draft_model）；模型用不了、某一段失败，
      都不影响其余来源照常写入。guidance 是数据源级的起草要求，note 是本次说明，和数据源说明一起交给模型。
    - 并入、写入见 write_draft。
    """
    cache = getattr(source, "schema_cache", None) or {}
    report = DraftReport()
    wanted = list(tables) if tables else await default_tables(session, source)
    picked, unknown = pick_tables(cache, wanted)
    for name in unknown:
        report.tables.append(TableDraftResult(table=name, error=missing_table_error(name), status="skipped"))

    outcomes: dict[str, Any] = {}
    if use_model and picked:
        try:
            if model is None:
                model, report.model = await resolve_draft_model(session)
            else:
                report.model = str(getattr(model, "model_name", "") or "")
        except CatalogModelUnavailable as e:
            report.model_error = str(e)
        else:
            from app.data import catalog_draft

            report.model_used = True
            existing = await read_catalog(session, source.id)
            outcomes = await catalog_draft.run_model_draft(
                model, source, picked, existing={t: e.notes for t, e in existing.items()},
                background=catalog_draft.Background(guidance=guidance, note=note))

    at = now_iso()
    for table in picked:
        report.tables.append(await write_draft(session, source, table, outcomes.get(table), at=at, actor=actor))
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


def _codes_text(codes: Mapping[str, Any]) -> str:
    """码值对照的文字：有含义的写「1=有效」，含义待填写的单独说明——不能写成「1=」，模型会当成含义就是空。"""
    filled = [f"{k}={v}" for k, v in codes.items() if str(v).strip()]
    pending = [str(k) for k, v in codes.items() if not str(v).strip()]
    if not pending:
        return "、".join(filled)
    if not filled:
        return f"{'、'.join(pending)}（含义待填写）"
    return f"{'、'.join(filled)}，{'、'.join(pending)} 的含义待填写"


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
                text = _codes_text(value)
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

    每条关系在图里有正反两条边；cardinality 是从 from_table 看过去的基数。cardinality_checked：基数是数据剖析
    用数据核实的（关系上的同名字段），不是按表结构推的。
    """

    from_table: str
    from_columns: tuple[str, ...]
    to_table: str
    to_columns: tuple[str, ...]
    relation_id: str
    cardinality: str | None
    source: str
    status: str
    cardinality_checked: bool = False

    def reversed(self) -> "JoinEdge":
        return JoinEdge(self.to_table, self.to_columns, self.from_table, self.from_columns, self.relation_id,
                        _FLIP.get(self.cardinality or ""), self.source, self.status, self.cardinality_checked)

    def condition(self) -> str:
        """连接条件：visits.id = channel_visits.visit_id（多列用 AND 连接）。"""
        return " AND ".join(f"{self.from_table}.{a} = {self.to_table}.{b}"
                            for a, b in zip(self.from_columns, self.to_columns))


def relation_graph(notes_by_table: Mapping[str, Mapping[str, Any]], *,
                   schema_cache: Mapping[str, Any] | None = None) -> dict[str, list[JoinEdge]]:
    """邻接表 {表: [从这张表出发的边]}，每条关系正反各一条边；被驳回的关系不进图，自己连自己的也不进。

    schema_cache 给了的话，目录里没有的关系按表结构现推（外键约束 + 命名推断）补进来：助手挑表时不必等每张表
    都起草过，只做过部分注释（目录里一条关系都没记）的表也不会丢掉外键关系——所以不能只给「没有目录的表」现推。
    目录里已有的关系（含驳回的）以目录为准：编号由两端的表和列算出（relation_id，含本表表名），目录里驳回过的
    编号就不再现推。这要求「去掉一条推断出来的关系」落成驳回而不是删除（apply_human_edit），删掉就留不下编号，
    下次又被现推回来。
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
                        str(rel.get("status")), rel.get("cardinality_checked") is True)
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


async def query_snapshot_tables(session: AsyncSession, source: Any) -> list[tuple[str, frozenset[str]]]:
    """这个源上属于运行的每一行查询快照引用：(运行 id, 这条查询用到的表——schema_cache 的表名)，没对上任何表的不收。

    使用次数（table_usage）和「这张表被哪些运行用过」（data/catalog_review.py）共用这一份取法：数据源查询工具每次成功
    查询都存一份查询快照工件（tools/datasource.py），工件引用表 artifacts 里按 (内容哈希, 运行) 各记一行；只收属于某次运行
    （runs 里有这次运行）、属于这个源的行，工具库里的试用（不属于任何运行）不算。

    表名在查询当时就记进了引用的 meta（query_snapshot_meta），这里只读 artifacts 一张表；升级前的老引用没有 meta，退回读
    工件内容里的源名和 SQL，读过的按工件 id 缓存（内容永不改变）。SQL 里写的表名去掉 schema 前缀后按 name_key（只把
    ASCII 字母转小写，和 SQLite 比较标识符的口径一致）对到表结构上，对不上的不计。
    """
    rows = (await session.execute(
        select(Artifact.id, Artifact.meta, Artifact.run_id).join(Run, Run.id == Artifact.run_id)
        .where(Artifact.kind == QUERY_SNAPSHOT_KIND)
    )).all()
    tables = (getattr(source, "schema_cache", None) or {}).get("tables") or {}
    lookup: dict[str, str] = {}
    for name in tables:
        lookup.setdefault(name_key(name), name)
    out: list[tuple[str, frozenset[str]]] = []
    for artifact_id, meta, run_id in rows:
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
        hit = frozenset(t for t in (lookup.get(name_key(str(n).rsplit(".", 1)[-1])) for n in names) if t is not None)
        if hit:
            out.append((str(run_id), hit))
    return out


async def table_usage(session: AsyncSession, source: Any) -> dict[str, int]:
    """每张表被运行查询过的次数：{schema_cache 的表名: 次数}，没被查过的表不出现。

    按查询快照引用的行数（query_snapshot_tables）：同一次运行里结果一模一样的重复查询只算一次，同一次运行里不同的查询
    各算一次，工具库里的试用不算。
    """
    counts: Counter[str] = Counter()
    for _run, hit in await query_snapshot_tables(session, source):
        counts.update(hit)
    return dict(counts)


# ==========================================================================
# 冻结进证据
# ==========================================================================


def frozen_catalog(entries: Mapping[str, CatalogEntry], tables: Iterable[str]) -> dict[str, dict[str, Any]]:
    """要冻结进表结构快照的目录：{表名: {"version": 版本, "notes": 去掉驳回项的目录}}。

    只收表结构快照里有的表（tables）；去掉驳回项以后什么都不剩的表不收。一张也没有时返回空 dict，
    调用方据此不加这个键：没有目录的源，快照内容和以前一字不差。目录不变（版本和内容都不变）则返回值
    不变，快照哈希也就不变。

    被驳回的关系留下编号和状态（{"id", "status": "rejected"}，不放内容）：拿冻结的这份重建 SQL 检查器
    （sqlcheck.frozen_checker）时，关系图（relation_graph）认得这些编号，按外键、命名现推的关系不会把驳回的推回来。
    只有驳回关系的表也因此要冻结。visible_notes 照样把它们去掉，给人看、给模型看的内容不变。
    """
    wanted = set(tables)
    out: dict[str, dict[str, Any]] = {}
    for name in sorted(entries):
        if name not in wanted:
            continue
        notes = visible_notes(entries[name].notes)
        rejected = [{"id": str(item["id"]), "status": "rejected"} for key, item in _slots(entries[name].notes).items()
                    if key[0] == "r" and item.get("status") == "rejected"]
        if rejected:
            notes = {**notes, "relations": [*(notes.get("relations") or []), *rejected]}
        if notes:
            out[name] = {"version": entries[name].version, "notes": notes}
    return out


__all__ = [
    "CARDINALITIES", "CARDINALITY_LABEL", "CATALOG_DRAFT_BACKGROUND_PROMPT", "CATALOG_DRAFT_PROMPT",
    "CATALOG_DRAFT_PROMPT_LABELS", "CATALOG_DRAFT_PROMPT_SCHEMA", "CATALOG_DRAFT_SYSTEM", "CATALOG_DRAFT_TEXT_PROMPT",
    "COLUMN_FIELDS", "COLUMN_FIELD_LABEL", "COLUMN_MEASURES", "CatalogConflict",
    "CatalogEntry", "CatalogInvalid", "CatalogModelUnavailable", "CatalogPathError", "DRAFT_DEFAULT_TABLES",
    "DraftModel", "DraftReport", "INFERRED_MARK", "ITEM_SOURCES", "ITEM_STATUSES", "JoinEdge", "KIND_LABEL",
    "MEASURE_LABEL", "MODEL_BATCH_COLUMNS", "MODEL_BATCH_TABLES", "MODEL_CONCURRENCY", "MODEL_EMPTY_REASON",
    "FACT_REASON_NOTE", "MODEL_FALLBACK_MAX_TOKENS", "MODEL_HALT_ON", "MODEL_HALT_WINDOW", "MODEL_MAX_TOKENS",
    "MODEL_MIN_PART_COLUMNS", "MODEL_NOTICE_OFF", "MODEL_NOTICE_ON", "MODEL_NOTICE_WINDOW", "MODEL_PART_MAX_CALLS",
    "MODEL_RETRIES", "MODEL_RETRY_DELAYS_S", "MODEL_SHRINK_AFTER", "MODEL_SPLIT_DEPTH", "MODEL_SPLIT_MIN_COLUMNS",
    "MODEL_TABLE_GIVE_UP_AFTER", "MODEL_TIMEOUT_S", "MergeStats", "NEW_RELATION_PATH",
    "PATCH_MAX_CHANGES", "REASON_MAX", "clip_reason",
    "PROFILE_NOTE_PREFIX", "PROFILE_VERIFY_COVERAGE", "WRITE_CONFLICT_ERROR", "default_tables",
    "missing_table_error", "pick_tables", "write_draft",
    "PatchChange", "PatchPlan", "QUERY_SNAPSHOT_KIND", "REVIEW_ACTIONS", "TABLE_FIELDS", "TABLE_FIELD_LABEL",
    "TABLE_KINDS", "TableDraft", "TableDraftResult", "UI_KIND_LABEL", "UI_MEASURE_LABEL", "apply_human_edit",
    "apply_patch", "codes_complete", "comment_role", "describe_slot", "draft_catalog", "draft_structure",
    "draft_with_model",
    "fk_relations", "frozen_catalog", "infer_name_relations", "initial_status", "is_date_column", "is_profile_note",
    "join_paths",
    "make_item", "merge_notes", "name_words", "now_iso", "parse_path", "plan_patch", "profile_relation_holds",
    "query_snapshot_meta", "read_catalog", "read_entry", "relation_graph", "relation_id", "render_table_index",
    "render_table_notes", "resolve_draft_model", "resolve_table_name", "review_entry", "review_item", "same_notes",
    "same_profile_note", "save_human_edit", "save_patch", "status_counts", "structure_relation_ids",
    "system_notes_source", "table_usage", "unreviewed_status", "validate_notes", "visible_notes", "write_entry",
]
