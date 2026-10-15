"""数据目录的导出导入：本机整理好的目录带到部署实例上（GET /catalog/export、POST /catalog/import/preview、/import）。

**为什么要有它。** 目录（尤其是人工确认、驳回过的项）是在本机对着开发库一项项审出来的；部署实例连的是同一套业务库，
却没有办法把这些结论带过去，只能在那边从头再审一遍。导出是一个 JSON 文件，导入先预览、再确认。

**文件**（export_catalog）：{format: "agentlab-catalog", version: 1, exported_at, source: {name, kind}, guidance,
tables: {表名: {notes, version}}}，含全部状态的项，表结构里已经没有的表也带上（目录里有什么就导出什么）。

**导入的规矩**（plan_import，预览和导入共用这一份）：

- 表按名字对：表结构里的写法、忽略大小写（name_key）、带 schema 前缀的对不上时去掉前缀再对（catalog.resolve_table_name）。
  只对目标数据源表结构里的表；对不上的列在 unmatched_tables。文件里两张表对上目标的同一张时，写法完全一样的那张优先，
  另一张算对不上。列名同样按表结构的写法、再忽略大小写对；业务主键、业务日期、关联关系两端的列和指向的表都换成目标的写法，
  关联关系的编号按目标的表名、列名重算（catalog.relation_id）。
- 文件里已确认、已驳回的项照带：来源、状态、备注、确认时的依据（reason）、修改时间都不变。目标那一格空着就新增；目标是
  推断、有确证的就更新；目标也是已确认、已驳回的，和文件一样就保留，不一样（值或状态不同）就保留目标的、记一条冲突。关联
  关系的「一样」看两端和基数，覆盖率是测量值、不算。
- 文件里推断、有确证的项只填目标的空缺，目标那一格有东西（哪怕是驳回）就保留目标的，不算冲突。这样的项不带确认时的依据
  （文件被手改过才会有）：推断项上挂着依据，看起来像使用者确认过它。
- 跳过并写原因：表结构里没有的列、业务主键和业务日期指着的列没有的、关联关系两端对不上的（指向的表、两端的列）、
  目标数据源遮罩的列上的码值（码值就是这一列的取值，遮罩了就不该进目录）、不认识的字段、取值不合规的项（说法同
  catalog.validate_notes）。
- 有效记录条件是表级的一项、却写着列名，按列跳过的规矩拦不住它：按目标数据源的方言解析出用到的列
  （filter_columns_problem），不在目标的表结构里、是别的表的列、解析不了或不是一个条件、带子查询（没法核对子查询用到的表
  和列）的跳过。人工写的条件不限函数（模型起草时的那套白名单只管模型写的条件）；列名大小写和目标不同、带本表表名的照带。
- 逐表写（乐观锁，撞上别人刚改过就重读重算，最多三次），一张表写不进去只记在那张表上，不影响别的表。
- 起草要求：勾了 include_guidance、文件里有、目标还没有时才写（action: replace）；目标已有的不覆盖（keep）；没勾或文件里
  没有是 none。
- 文件格式不对（不是对象、格式标记或版本不对、表的目录不是对象、起草要求不是文字或超长）抛 TransferInvalid，接口回 422。

有排队或进行中的起草、剖析任务时不导入（接口经 catalog_jobs.import_write，409 catalog_draft_busy / profile_busy，导入期间
占着这个源的队）；目标数据源没有表结构时接口回 409（什么也对不上）。
"""
from __future__ import annotations

import copy
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.data import catalog, catalog_jobs
from app.data.catalog_review import item_label, slot_path
from app.data.engine import masked_columns
from app.data.names import name_key
from app.data.sqlcheck import dialect_of

FORMAT = "agentlab-catalog"
VERSION = 1
FILE_SUFFIX = "-数据目录.json"
_BAD_FILENAME = re.compile(r'[\\/:*?"<>|\x00-\x1f\x7f]')
_NAME_MAX = 80
_UNNAMED = "数据源"

#: 状态：照带的、只填空缺的
_LOCKED = ("confirmed", "rejected")
#: 状态的界面叫法（同 frontend/src/lib/terms.ts 的 CATALOG_STATUS_LABEL）
_STATUS_LABEL = {"proposed": "推断", "verified": "已验证", "confirmed": "已确认", "rejected": "已驳回"}

#: 写入一直撞上别人刚改过（重读重算三次仍冲突）
WRITE_CONFLICT_ERROR = "这张表的数据目录正被其他人修改，没有导入，请稍后重新导入"
WRITE_ERROR = "写入这张表的数据目录时出错，没有导入：{line}"


class TransferInvalid(ValueError):
    """文件格式不对。message 给人看（接口回 422）。"""


# ==========================================================================
# 导出
# ==========================================================================


def attachment_name(source_name: str) -> str:
    """附件名「<数据源名>-数据目录.json」。文件名里不能有的字换成下划线。"""
    name = _BAD_FILENAME.sub("_", source_name or "").strip(" .")[:_NAME_MAX].strip(" .")
    return f"{name or _UNNAMED}{FILE_SUFFIX}"


def content_disposition(filename: str, source_id: str) -> str:
    """附件头：中文名按 RFC 5987 写在 filename* 里；filename 给不认 filename* 的老客户端一个纯 ASCII 的名字。"""
    from urllib.parse import quote

    ascii_id = re.sub(r"[^A-Za-z0-9]", "", source_id)[:8]
    return f"attachment; filename=\"agentlab-catalog-{ascii_id}.json\"; filename*=UTF-8''{quote(filename, safe='')}"


async def export_catalog(session: AsyncSession, row: Any) -> dict[str, Any]:
    """导出这个数据源的数据目录（row 是数据源记录，目录按它的 id 存）。"""
    entries = await catalog.read_catalog(session, row.id)
    return {
        "format": FORMAT,
        "version": VERSION,
        "exported_at": catalog.now_iso(),
        "source": {"name": row.name, "kind": row.kind},
        "guidance": catalog_jobs.guidance_of(row),
        "tables": {name: {"notes": entry.notes, "version": entry.version} for name, entry in sorted(entries.items())},
    }


# ==========================================================================
# 读文件
# ==========================================================================


@dataclass(frozen=True)
class ImportFile:
    tables: dict[str, dict[str, Any]]
    guidance: str | None


def read_file(content: Any, *, include_guidance: bool) -> ImportFile:
    """导出的那个对象 → ImportFile。界面直接交来文件的原文（一段 JSON 文字）也认。格式不对抛 TransferInvalid（说法给人看）。"""
    if isinstance(content, str):
        try:
            content = json.loads(content)
        except ValueError as e:
            raise TransferInvalid("文件内容不是合法的 JSON，请选择从 AgentLab 导出的数据目录文件") from e
    if not isinstance(content, Mapping):
        raise TransferInvalid("文件内容不是数据目录导出的格式，请选择从 AgentLab 导出的数据目录文件")
    if content.get("format") != FORMAT:
        raise TransferInvalid("这个文件不是 AgentLab 导出的数据目录（文件里的格式标记不对），请重新选择文件")
    version = content.get("version")
    if version != VERSION:
        raise TransferInvalid(f"这个数据目录文件的格式版本是「{version}」，当前只能导入第 {VERSION} 版的文件，"
                              "请用同一版本的 AgentLab 重新导出")
    tables = content.get("tables")
    if not isinstance(tables, Mapping):
        raise TransferInvalid("数据目录文件里没有表的目录，文件可能不完整，请重新导出")
    out: dict[str, dict[str, Any]] = {}
    for name, body in tables.items():
        notes = body.get("notes") if isinstance(body, Mapping) else None
        if not isinstance(name, str) or not name.strip() or not isinstance(notes, Mapping):
            raise TransferInvalid(f"数据目录文件里表「{str(name)[:60]}」的目录格式不正确，文件可能被改动过，请重新导出")
        out[name] = copy.deepcopy(dict(notes))
    guidance = content.get("guidance")
    if guidance is not None and not isinstance(guidance, str):
        raise TransferInvalid("数据目录文件里的起草要求格式不正确，文件可能被改动过，请重新导出")
    text = guidance.strip() if isinstance(guidance, str) else ""
    if include_guidance and len(text) > catalog_jobs.GUIDANCE_MAX:
        raise TransferInvalid(f"数据目录文件里的起草要求有 {len(text)} 字，超过了 {catalog_jobs.GUIDANCE_MAX} 字的上限，"
                              "请删减后再导入，或者不导入起草要求")
    return ImportFile(tables=out, guidance=text or None)


# ==========================================================================
# 对表、对列
# ==========================================================================


def match_tables(file_tables: list[str], schema_cache: Mapping[str, Any]) -> tuple[dict[str, str], list[str]]:
    """文件里的表 → 目标表结构里的键。返回 ({文件里的表名: 目标表名}, 对不上的表名)。写法完全一样的先认领。"""
    tables = (schema_cache or {}).get("tables") or {}
    matched: dict[str, str] = {}
    claimed: set[str] = set()
    ordered = sorted(file_tables, key=lambda n: n not in tables)
    for name in ordered:
        key = catalog.resolve_table_name(schema_cache, name)
        if key is None or key in claimed:
            continue
        matched[name] = key
        claimed.add(key)
    unmatched = [name for name in file_tables if name not in matched]
    return {name: matched[name] for name in file_tables if name in matched}, unmatched


class _Columns:
    """一张目标表的列：按表结构的写法认，再忽略大小写认（只认唯一对上的）。"""

    def __init__(self, meta: Mapping[str, Any] | None) -> None:
        names = [str(c.get("name")) for c in (meta or {}).get("columns") or [] if isinstance(c, Mapping)
                 and c.get("name") is not None]
        self.names = set(names)
        self._loose: dict[str, list[str]] = {}
        for name in names:
            self._loose.setdefault(name_key(name), []).append(name)

    def find(self, name: Any) -> str | None:
        if not isinstance(name, str):
            return None
        if name in self.names:
            return name
        found = self._loose.get(name_key(name)) or []
        return found[0] if len(found) == 1 else None


def filter_columns_problem(condition: Any, meta: Mapping[str, Any] | None, *, kind: str | None, table: str,
                           also: Iterable[str] = (), where: str = "表结构") -> str | None:
    """有效记录条件用到的列是不是都在这张表的表结构里（给人看的一句话；没问题返回 None）。

    按数据源的方言（sqlcheck.dialect_of）解析成一个条件：解析不了、不止一句、是一条查询、带子查询（没法核对子查询用到的
    表和列）的不收；列不带表名，或带这张表的表名（table、表结构里的 qualified、also 里的写法，比如文件里的表名；忽略大小写、
    去掉 schema 前缀），列名忽略大小写在表结构里。不限函数、运算。不是文字的交给合规检查说（返回 None）；数据源类型
    没有对应的方言时没法解析，不核对。where 是说法里「表结构」的叫法（导入时是「目标的表结构」）。"""
    if not isinstance(condition, str) or not condition.strip():
        return None
    dialect = dialect_of(kind)
    if dialect is None:
        return None
    import sqlglot
    from sqlglot import exp

    try:
        parsed = [s for s in sqlglot.parse(condition.strip(), read=dialect) if s is not None]
    except Exception:  # noqa: BLE001 - 语法错、方言不支持的写法
        parsed = []
    if len(parsed) != 1 or isinstance(parsed[0], exp.Query) or not isinstance(parsed[0], exp.Condition):
        return f"有效记录条件不是一个能解析的 SQL 条件（按 {kind} 的写法），没法核对它用到的列"
    expression = parsed[0]
    if any(isinstance(node, (exp.Query, exp.Subquery)) for node in expression.walk()):
        return "有效记录条件里有子查询，没法核对子查询用到的表和列"
    own = {name_key(str(n).rsplit(".", 1)[-1]) for n in (table, (meta or {}).get("qualified"), *also) if n}
    columns = {str(c.get("name")).lower() for c in (meta or {}).get("columns") or []
               if isinstance(c, Mapping) and c.get("name") is not None}
    foreign: list[str] = []
    missing: list[str] = []
    for column in expression.find_all(exp.Column):
        if column.is_star:
            continue
        if column.table and name_key(column.table) not in own:
            foreign.append(column.sql(dialect=dialect))
        elif column.name.lower() not in columns:
            missing.append(column.name)
    if foreign:
        return f"有效记录条件引用了别的表的列（{'、'.join(dict.fromkeys(foreign))}），只能用这张表的列"
    if missing:
        return f"有效记录条件里的列「{'、'.join(dict.fromkeys(missing))}」不在{where}里"
    return None


# ==========================================================================
# 一张表怎么并
# ==========================================================================


@dataclass
class TablePlan:
    """文件里一张表并进目标的计划。writes 是要写进目标的项（目标的槽位 → 项）。"""

    table_name: str
    matched_as: str
    add: int = 0
    update: int = 0
    keep: int = 0
    conflicts: list[dict[str, Any]] = field(default_factory=list)
    skipped: list[dict[str, Any]] = field(default_factory=list)
    writes: dict[catalog.Slot, dict[str, Any]] = field(default_factory=dict)
    version: int = 0
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"table_name": self.table_name, "matched_as": self.matched_as, "add": self.add, "update": self.update,
                "keep": self.keep, "conflicts": self.conflicts, "skipped": self.skipped, "version": self.version,
                "error": self.error}


def _raw_items(notes: Mapping[str, Any]) -> list[tuple[str, catalog.Slot | None, Any, str | None]]:
    """文件里一张表的每一项：(文件里的路径, 槽位, 项, 不认的原因)。不认识的字段、不是对象的列也逐个列出来（槽位为 None）。"""
    out: list[tuple[str, catalog.Slot | None, Any, str | None]] = []
    for name, item in notes.items():
        if name in catalog.TABLE_FIELDS:
            out.append((name, ("t", name), item, None))
        elif name not in ("columns", "relations"):
            out.append((str(name), None, item, f"数据目录里不认识的字段「{name}」"))
    columns = notes.get("columns", {})
    if not isinstance(columns, Mapping):
        out.append(("columns", None, columns, "列的目录应为以列名为键的对象"))
        columns = {}
    for col, items in columns.items():
        if not isinstance(items, Mapping):
            out.append((f"columns.{col}", None, items, f"列 {col} 的目录应为对象"))
            continue
        for name, item in items.items():
            path = f"columns.{col}.{name}"
            if name in catalog.COLUMN_FIELDS:
                out.append((path, ("c", str(col), name), item, None))
            else:
                out.append((path, None, item, f"列 {col} 含有不认识的字段「{name}」"))
    relations = notes.get("relations", [])
    if not isinstance(relations, list):
        out.append(("relations", None, relations, "关联关系应为列表"))
        relations = []
    for rel in relations:
        rid = rel.get("id") if isinstance(rel, Mapping) else None
        out.append((f"relations.{rid}", ("r", str(rid)), rel, None))
    return out


def _item_problem(key: catalog.Slot, item: Any) -> str | None:
    """这一项本身合不合规（说法同 catalog.validate_notes）。"""
    if key[0] == "t":
        probe: dict[str, Any] = {key[1]: item}
    elif key[0] == "c":
        probe = {"columns": {key[1]: {key[2]: item}}}
    else:
        probe = {"relations": [item]}
    problems = catalog.validate_notes(probe)
    return problems[0] if problems else None


def _decision(key: catalog.Slot, item: Mapping[str, Any]) -> Any:
    """判断「一样不一样」看的部分：状态，加上值（码值另看「已列全」）。关联关系的两端由编号定了，再看基数；覆盖率不算。"""
    if key[0] == "r":
        return item.get("status"), item.get("cardinality")
    if key[0] == "c" and key[2] == "codes":
        return item.get("status"), item.get("value"), catalog.codes_complete(item)
    return item.get("status"), item.get("value")


def _conflict_why(file_status: str, target_status: str) -> str:
    if target_status == "rejected":
        return f"目标里这一项已经驳回，文件里是{_STATUS_LABEL.get(file_status, file_status)}，保留目标的"
    if file_status == "rejected":
        return "目标里这一项已经人工确认过，文件里是已驳回，保留目标的"
    return "目标里这一项已经人工确认过，和文件里的不一样，保留目标的"


class _Mapper:
    """把文件里的一项换成目标的写法：列名、业务主键和业务日期的列、关联关系两端和编号；有效记录条件核对用到的列。对不上
    返回原因。name 是文件里的表名（条件里带它当表名也认），kind 是目标数据源的类型（解析条件用它的方言）。"""

    def __init__(self, name: str, target: str, schema_cache: Mapping[str, Any], masked: set[str],
                 kind: str | None) -> None:
        self.name = name
        self.target = target
        self.cache = schema_cache
        self.tables = (schema_cache or {}).get("tables") or {}
        self.columns = _Columns(self.tables.get(target))
        self.masked = masked
        self.kind = kind

    def __call__(self, key: catalog.Slot, item: Any) -> tuple[catalog.Slot | None, Any, str | None]:
        if not isinstance(item, Mapping):
            return None, item, None              # 交给合规检查说
        item = copy.deepcopy(dict(item))
        if key[0] == "c":
            col = self.columns.find(key[1])
            if col is None:
                return None, item, f"目标的表结构里没有列 {key[1]}，跳过"
            if key[2] == "codes" and col.lower() in self.masked:
                return None, item, f"列 {col} 在目标数据源的设置中被遮罩，码值是这一列的取值，不导入"
            return ("c", col, key[2]), item, None
        if key[0] == "t":
            value = item.get("value")
            if key[1] == "keys" and isinstance(value, list):
                mapped = [self.columns.find(c) for c in value]
                if missing := [str(c) for c, m in zip(value, mapped) if m is None]:
                    return None, item, f"业务主键里的列「{'、'.join(missing)}」不在目标的表结构里，跳过"
                item["value"] = mapped
            elif key[1] == "business_date" and isinstance(value, Mapping) and isinstance(value.get("column"), str):
                col = self.columns.find(value["column"])
                if col is None:
                    return None, item, f"业务日期的列「{value['column']}」不在目标的表结构里，跳过"
                item["value"] = {**value, "column": col}
            elif key[1] == "valid_filter":
                problem = filter_columns_problem(value, self.tables.get(self.target), kind=self.kind,
                                                 table=self.target, also=(self.name,), where="目标的表结构")
                if problem is not None:
                    return None, item, f"{problem}，跳过"
            return key, item, None
        return self._relation(item)

    def _relation(self, rel: dict[str, Any]) -> tuple[catalog.Slot | None, Any, str | None]:
        cols, to_table, to_cols = rel.get("columns"), rel.get("to_table"), rel.get("to_columns")
        if not (isinstance(cols, list) and isinstance(to_cols, list) and isinstance(to_table, str)):
            return None, rel, None
        target_table = catalog.resolve_table_name(self.cache, to_table)
        if target_table is None:
            return None, rel, f"关联关系指向的表 {to_table} 不在目标数据源的表结构里，跳过"
        mine = [self.columns.find(c) for c in cols]
        if missing := [str(c) for c, m in zip(cols, mine) if m is None]:
            return None, rel, f"关联关系的本表字段「{'、'.join(missing)}」不在目标的表结构里，跳过"
        theirs_columns = _Columns(self.tables.get(target_table))
        theirs = [theirs_columns.find(c) for c in to_cols]
        if missing := [str(c) for c, m in zip(to_cols, theirs) if m is None]:
            return None, rel, f"关联关系的目标字段「{'、'.join(missing)}」不在 {target_table} 的表结构里，跳过"
        rid = catalog.relation_id(self.target, mine, target_table, theirs)
        return ("r", rid), {**rel, "id": rid, "columns": mine, "to_table": target_table, "to_columns": theirs}, None


def plan_table(table_name: str, target: str, file_notes: Mapping[str, Any], target_notes: Mapping[str, Any] | None,
               schema_cache: Mapping[str, Any], masked: set[str], kind: str | None = None) -> TablePlan:
    """文件里一张表并进目标那张表的计划（纯函数，不写库）。kind 是目标数据源的类型（解析有效记录条件用）。"""
    plan = TablePlan(table_name=table_name, matched_as=target)
    existing = catalog._slots(target_notes)
    mapper = _Mapper(table_name, target, schema_cache, masked, kind)
    seen: set[catalog.Slot] = set()
    for path, key, item, why in _raw_items(file_notes):
        if why is not None or key is None:
            plan.skipped.append({"path": path, "why": why or "无法识别这一项"})
            continue
        mapped, item, why = mapper(key, item)
        if why is not None:
            plan.skipped.append({"path": path, "why": why})
            continue
        if isinstance(item, dict) and item.get("status") not in _LOCKED and "reason" in item:
            # 推断、有确证的项不带确认时的依据：在校验之前去掉，手改的文件里依据不合规也不把整项跳过
            item = {k: v for k, v in item.items() if k != "reason"}
        problem = _item_problem(mapped or key, item)
        if problem is not None or mapped is None:
            plan.skipped.append({"path": path, "why": problem or "无法识别这一项"})
            continue
        if mapped in seen:
            plan.skipped.append({"path": path, "why": f"和文件里前面的{catalog.describe_slot(mapped)}是同一项，只导入第一项"})
            continue
        seen.add(mapped)
        status = item.get("status")
        have = existing.get(mapped)
        if have is None:
            plan.add += 1
            plan.writes[mapped] = item
        elif status not in _LOCKED:
            plan.keep += 1                                   # 推断、有确证的只填空缺
        elif have.get("status") in _LOCKED:
            if _decision(mapped, have) == _decision(mapped, item):
                plan.keep += 1
            else:
                plan.conflicts.append({
                    "path": slot_path(mapped), "label": item_label(mapped, item),
                    "file_value": _shown_value(mapped, item), "target_value": _shown_value(mapped, have),
                    "file_status": status, "target_status": have.get("status"),
                    "why": _conflict_why(str(status), str(have.get("status"))),
                })
        elif _decision(mapped, have) == _decision(mapped, item):
            plan.keep += 1
        else:
            plan.update += 1
            plan.writes[mapped] = item
    return plan


def _shown_value(key: catalog.Slot, item: Mapping[str, Any]) -> Any:
    if key[0] == "r":
        return {k: item.get(k) for k in ("columns", "to_table", "to_columns", "cardinality", "coverage")}
    return item.get("value")


# ==========================================================================
# 预览、导入
# ==========================================================================


@dataclass
class ImportReport:
    tables: list[TablePlan]
    unmatched: list[str]
    guidance: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "tables": [t.as_dict() for t in self.tables],
            "unmatched_tables": self.unmatched,
            "totals": {"tables": len(self.tables), "add": sum(t.add for t in self.tables),
                       "update": sum(t.update for t in self.tables), "keep": sum(t.keep for t in self.tables),
                       "conflicts": sum(len(t.conflicts) for t in self.tables),
                       "skipped": sum(len(t.skipped) for t in self.tables)},
            "guidance": self.guidance,
        }


def _guidance(file: ImportFile, row: Any, include: bool) -> dict[str, Any]:
    target = catalog_jobs.guidance_of(row)
    if not include or file.guidance is None:
        action = "none"
    elif target:
        action = "keep"
    else:
        action = "replace"
    return {"file": file.guidance, "target": target, "action": action}


async def plan_import(session: AsyncSession, row: Any, source: Any, content: Any, *,
                      include_guidance: bool) -> ImportReport:
    """预览：逐表算出要新增、更新、保留什么，冲突和跳过的项及原因。不写库。row 是数据源记录（遮罩、起草要求按它），
    source 是读表结构用的源（上传源是绑定当前快照的视图）。格式不对抛 TransferInvalid。"""
    file = read_file(content, include_guidance=include_guidance)
    cache = getattr(source, "schema_cache", None) or {}
    matched, unmatched = match_tables(list(file.tables), cache)
    masked = {c.lower() for c in masked_columns(getattr(row, "options", None))}
    plans: list[TablePlan] = []
    for name, target in matched.items():
        entry = await catalog.read_entry(session, source.id, target)
        plan = plan_table(name, target, file.tables[name], entry.notes if entry else None, cache, masked, row.kind)
        plan.version = entry.version if entry else 0
        plans.append(plan)
    return ImportReport(plans, unmatched, _guidance(file, row, include_guidance))


async def run_import(session: AsyncSession, row: Any, source: Any, content: Any, *, include_guidance: bool,
                     actor: str | None) -> ImportReport:
    """导入：按 plan_import 的规矩逐表写入（乐观锁，撞上别人刚改过就重读重算，最多三次），再按需写起草要求。
    一张表写不进去只记在它的 error 上（新增、更新记 0），不影响别的表。"""
    file = read_file(content, include_guidance=include_guidance)
    cache = getattr(source, "schema_cache", None) or {}
    matched, unmatched = match_tables(list(file.tables), cache)
    masked = {c.lower() for c in masked_columns(getattr(row, "options", None))}
    plans: list[TablePlan] = []
    for name, target in matched.items():
        plans.append(await _import_table(session, source, name, target, file.tables[name], cache, masked, row.kind,
                                         actor))
    guidance = _guidance(file, row, include_guidance)
    if guidance["action"] == "replace":
        await catalog_jobs.save_guidance(session, row, file.guidance)
    return ImportReport(plans, unmatched, guidance)


async def _import_table(session: AsyncSession, source: Any, name: str, target: str, notes: Mapping[str, Any],
                        cache: Mapping[str, Any], masked: set[str], kind: str | None, actor: str | None) -> TablePlan:
    plan = TablePlan(table_name=name, matched_as=target)
    try:
        for _attempt in range(3):
            entry = await catalog.read_entry(session, source.id, target)
            version = entry.version if entry else 0
            plan = plan_table(name, target, notes, entry.notes if entry else None, cache, masked, kind)
            plan.version = version
            if not plan.writes:
                return plan
            slots = catalog._slots(entry.notes if entry else None)
            slots.update(plan.writes)
            try:
                written = await catalog.write_entry(session, source.id, target, catalog._assemble(slots),
                                                    if_version=version, actor=actor)
            except catalog.CatalogConflict:
                continue
            plan.version = written.version
            return plan
        plan.error = WRITE_CONFLICT_ERROR
    except catalog.CatalogInvalid as e:
        await session.rollback()
        plan.error = WRITE_ERROR.format(line=str(e))
    except Exception as e:  # noqa: BLE001 - 这一张写不进去不影响别的表
        from app.core.errors import describe_exception, first_line

        await session.rollback()
        plan.error = WRITE_ERROR.format(line=first_line(e) or describe_exception(e))
    plan.add = plan.update = 0
    return plan


__all__ = ["FORMAT", "ImportFile", "ImportReport", "TablePlan", "TransferInvalid", "VERSION", "attachment_name",
           "content_disposition", "export_catalog", "filter_columns_problem", "match_tables", "plan_import",
           "plan_table", "read_file", "run_import"]
