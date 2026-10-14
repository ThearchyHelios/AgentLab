"""能力字段语义的审阅：字段语义怎么写进来、待审阅的项怎么排、批量审阅（D-i，§2.6 A6 第 1 条）。

**为什么和数据目录用同一套。** 能力登记的 fields 按 JSON 指针记返回字段的语义：{"/amount": {"label": 项, "unit": 项}}，
「项」的形状、状态、审阅规矩都沿用数据目录（data/catalog.py）：{value, source, status, note, reason, updated_at}，状态
proposed（推断）/ verified（有确证）/ confirmed（已确认）/ rejected（驳回）。一个返回字段就当数据目录里的一列：能写的项
同列级的项（中文名、含义、单位、度量类型、码值），取值规矩、公式检查、人工编辑的语义都直接用 catalog 的实现（把 fields
当成 {"columns": fields} 交给它），说法另换成「返回字段」。一种说法（P5）：助手查看能力时读的中文名、审阅队列、界面
上的状态，和数据目录是同一回事。

- **写进来**（normalize_fields，能力治理 PUT /api/capabilities/{name}/governance 调）：同数据目录的人工编辑
  （catalog.apply_human_edit）——新填、值改过的项记为人工填写、已确认；值没动的保持原来的来源和状态（提交里把状态改成
  确认、驳回、来源的初始状态的照改，等同单项审阅）；提交里没有的项删掉。客户端不能冒充有确证的来源。只多一条：提交里
  明确标成推断（status=proposed，来源是模型起草或命名推断）的新项、改过的项，按推断收下、进审阅队列——起草、导入的
  结果走这条；把一项标成推断只会降低它的可信度，冒充不了什么。
- **队列**（review_queue，GET /api/capabilities/review-queue）：只列推断的项。priority = 使用次数权重 × 效果权重 ×
  风险权重 × 来源权重，从高到低；同分按能力名、指针、项排，结果稳定。
  - 使用次数：用到这项能力的运行有几次——运行发起时把用到的能力的治理快照记进 Run.options["capabilities"]（A6 第 4
    条），按它数。权重同数据目录（catalog_review.usage_weight）：按对数增长。
  - 效果权重（EFFECT_WEIGHT）：写入能力最高（字段的含义错了，确认卡、生效记录的预览就跟着错，而写出去的收不回来），
    视参数而定次之，只读最低。
  - 风险权重、来源权重同数据目录的列级项（catalog_review.FIELD_GROUP、SOURCE_WEIGHT）：度量类型（能不能加总）最高，
    码值次之，中文名、含义、单位最低；模型起草、命名推断比别的来源优先。
  - 来源已经删掉的能力（missing）不列：确认了也没有用。
- **批量审阅**（review_batch，POST /api/capabilities/review/batch）：逐个能力按 version 乐观锁（能力治理同一个版本号，
  改一次加一，运行固定的也是它），同一个能力的几条在一次写入里完成，写审计；一个能力冲突、写不进去不影响别的能力；
  指针、项不对逐条报（results 和请求一一对应）。审阅的规则同数据目录的单项审阅（catalog.review_item）。
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.data import catalog
from app.data.catalog_review import (
    FIELD_GROUP, REASON_RISK, REASON_SOURCE, RISK_WEIGHT, SOURCE_LABEL, SOURCE_WEIGHT, USED_RUNS, display,
    usage_weight,
)

#: 一个返回字段下能写的项：同数据目录列级的项
FIELD_KEYS = catalog.COLUMN_FIELDS
FIELD_KEY_LABEL = catalog.COLUMN_FIELD_LABEL
#: 队列的筛选：全部、影响数字的项（度量类型）、名称和说明（中文名、含义、单位、码值）。同数据目录
KINDS = ("all", "facts", "names")
QUEUE_LIMIT_MAX = 200
BATCH_MAX = 200
#: 效果权重：写入能力的字段含义错了，确认卡、生效记录的预览跟着错
EFFECT_WEIGHT = {"write": 2.0, "dynamic": 1.5, "read": 1.0}
#: reasons 里效果那一条的说法（只读能力不写）
REASON_EFFECT = {"write": "写入能力，确认卡和生效记录按它展示", "dynamic": "视参数而定的能力，可能写入外部系统"}
#: 提交里能按推断收下的来源：起草、导入写进来的。有确证的来源（外键约束、数据剖析）只能由系统写，客户端不能冒充
PROPOSAL_SOURCES = ("llm", "name")

#: 批量审阅逐条结果的机读码
CODE_CONFLICT = "capability_conflict"
CODE_PATH = "capability_path_invalid"
CODE_MISSING = "capability_missing"


class FieldsInvalid(ValueError):
    """fields 的结构或取值不合规。message 是给人看的一句（多条用分号连起来）。"""

    def __init__(self, problems: list[str]) -> None:
        super().__init__("；".join(problems[:5]) + ("…" if len(problems) > 5 else ""))
        self.problems = problems


# ==========================================================================
# 说法：数据目录的「列」在这里是「返回字段」
# ==========================================================================


def where(pointer: str, key: str) -> str:
    """一项给人看的位置：「返回字段 /amount 的中文名」。"""
    return f"返回字段 {pointer} 的{FIELD_KEY_LABEL.get(key, key)}"


_COLUMN_HEAD = re.compile(r"^列 ")


def _words(problem: str) -> str:
    """数据目录的校验说法换成能力字段的：「列 /amount 的…」→「返回字段 /amount 的…」。"""
    out = _COLUMN_HEAD.sub("返回字段 ", problem)
    return out.replace("数据目录只记数据事实", "字段语义只记数据事实").replace("目录项", "字段语义的项")


# ==========================================================================
# 写进来
# ==========================================================================


def _shape(submitted: Any) -> dict[str, dict[str, Any]]:
    """提交的 fields → {指针: {项名: 项}}。一项可以只写值（{"label": "金额"}），等同 {"label": {"value": "金额"}}。"""
    if not isinstance(submitted, Mapping):
        raise FieldsInvalid(["字段语义需要写成按 JSON 指针分项的对象"])
    problems: list[str] = []
    out: dict[str, dict[str, Any]] = {}
    for pointer, items in submitted.items():
        if not isinstance(pointer, str) or len(pointer) < 2 or not pointer.startswith("/") or pointer != pointer.strip():
            problems.append(f"字段语义的键需要是返回里某个字段的位置（JSON 指针，以 / 开头），当前为「{pointer}」")
            continue
        if not isinstance(items, Mapping):
            problems.append(f"返回字段 {pointer} 的语义需要写成按项分开的对象（中文名、含义、单位……）")
            continue
        out[pointer] = {k: (dict(v) if isinstance(v, Mapping) and "value" in v else {"value": v})
                        for k, v in items.items()}
    if problems:
        raise FieldsInvalid(problems)
    return out


def normalize_fields(existing: Mapping[str, Any] | None, submitted: Any, *, at: str | None = None) -> dict[str, Any]:
    """能力治理提交的 fields → 要写入的 fields（不改入参）。规则见模块说明；不合规抛 FieldsInvalid。"""
    shaped = _shape(submitted)
    at = at or catalog.now_iso()
    try:
        notes = catalog.apply_human_edit({"columns": dict(existing or {})}, {"columns": shaped}, table="", at=at)
    except catalog.CatalogInvalid as e:
        raise FieldsInvalid([_words(p) for p in e.problems]) from e
    old = catalog._slots({"columns": dict(existing or {})})
    slots = catalog._slots(notes)
    for pointer, items in shaped.items():
        for key, item in items.items():
            slot = ("c", pointer, key)
            if slot not in slots or item.get("status") != "proposed" or item.get("source") not in PROPOSAL_SOURCES:
                continue
            prev = old.get(slot)
            if prev is not None and catalog._edit_value(slot, slots[slot]) == catalog._edit_value(slot, prev):
                continue                                       # 值没动的，按人工编辑的规矩已经处理过
            note = item.get("note") if isinstance(item.get("note"), str) else None
            slots[slot] = catalog.make_item(slots[slot]["value"], item["source"], "proposed", note=note, at=at,
                                            complete=catalog.codes_complete(item))
    return catalog._assemble(slots).get("columns", {})


# ==========================================================================
# 用得多不多
# ==========================================================================


async def run_counts(session: AsyncSession) -> dict[str, int]:
    """每项能力被几次运行用到：{能力名: 运行次数}，没用过的不出现。按运行发起时记下的能力治理快照数
    （Run.options["capabilities"]，一次运行里一项能力只记一次）。只用 SQLite 的 json_each，一次查询。"""
    rows = await session.execute(text(
        "SELECT j.key, COUNT(*) FROM runs, json_each(runs.options, '$.capabilities') AS j "
        "WHERE json_valid(runs.options) GROUP BY j.key"))
    return {str(name): int(count) for name, count in rows.all()}


# ==========================================================================
# 队列
# ==========================================================================


def priority(runs: int, effect: str, key: str, source: Any) -> float:
    """一项的优先级：使用次数权重 × 效果权重 × 风险权重 × 来源权重。"""
    _, tier = FIELD_GROUP[("c", key)]
    return (usage_weight(runs) * EFFECT_WEIGHT.get(effect, 1.0) * RISK_WEIGHT[tier]
            * SOURCE_WEIGHT.get(str(source), 1.0))


def _reasons(runs: int, effect: str, key: str, item: Mapping[str, Any]) -> list[str]:
    out: list[str] = []
    if runs > 0:
        out.append(USED_RUNS.format(runs=f"{runs:,}"))
    if effect in REASON_EFFECT:
        out.append(REASON_EFFECT[effect])
    _, tier = FIELD_GROUP[("c", key)]
    out.append(REASON_RISK[tier].format(label=FIELD_KEY_LABEL.get(key, key)))
    source = str(item.get("source") or "")
    out.append(REASON_SOURCE.format(source=SOURCE_LABEL.get(source, source)))
    return out


async def review_queue(session: AsyncSession, *, kind: str = "all", capability: str | None = None,
                       limit: int = 50, offset: int = 0) -> dict[str, Any]:
    """审阅队列：{total, items}。每项：能力（名字、所属系统、标题、效果）、位置（指针、项、给人看的位置）、值和一行字、
    来源、状态、备注、优先级、为什么排在这里、用到它的运行次数、能力的版本（批量审阅时带上）。"""
    from app.capabilities.registry import effective_effect
    from app.db.models import Capability

    stmt = select(Capability).where(Capability.missing.is_(False))
    if capability:
        stmt = stmt.where(Capability.name == capability)
    counts = await run_counts(session)
    items: list[dict[str, Any]] = []
    for row in (await session.execute(stmt)).scalars():
        fields = row.fields if isinstance(row.fields, dict) else {}
        if not fields:
            continue
        effect = effective_effect(row)
        runs = counts.get(row.name, 0)
        for (_, pointer, key), item in catalog._slots({"columns": fields}).items():
            if item.get("status") != "proposed":
                continue
            group, _ = FIELD_GROUP[("c", key)]
            if kind != "all" and group != kind:
                continue
            value = item.get("value")
            items.append({
                "capability": row.name,
                "system": row.system,
                "title": row.title or row.name,
                "effect": effect,
                "pointer": pointer,
                "field": key,
                "field_text": FIELD_KEY_LABEL.get(key, key),
                "field_label": where(pointer, key),
                "value": value,
                "display": display(("c", pointer, key), value),
                "source": item.get("source"),
                "status": item.get("status"),
                "note": item.get("note") if isinstance(item.get("note"), str) and item.get("note") else None,
                "priority": round(priority(runs, effect, key, item.get("source")), 4),
                "reasons": _reasons(runs, effect, key, item),
                "runs": runs,
                "version": row.version,
            })
    items.sort(key=lambda i: (-i["priority"], i["capability"], i["pointer"], FIELD_KEYS.index(i["field"])))
    return {"total": len(items), "items": items[offset:offset + limit]}


# ==========================================================================
# 批量审阅
# ==========================================================================


@dataclass(frozen=True)
class BatchItem:
    capability: str
    pointer: str
    field: str
    action: str
    if_version: int


def _result(item: BatchItem, *, ok: bool, version: int | None = None, error: str | None = None,
            code: str | None = None) -> dict[str, Any]:
    return {"capability": item.capability, "pointer": item.pointer, "field": item.field, "ok": ok,
            "version": version if ok else None, "error": error, "code": code}


def _conflict(name: str) -> str:
    return f"能力「{name}」的治理设置刚被修改过，请重新载入后再审阅"


def review_field(fields: Mapping[str, Any] | None, pointer: str, key: str, action: str, *,
                 at: str | None = None) -> dict[str, Any]:
    """对一项做审阅，返回新的 fields（不改入参）。规则同数据目录的单项审阅：确认、驳回；撤销审阅回到来源的初始状态
    （人工填写的撤销就是删掉）。项不存在、操作不认识抛 catalog.CatalogPathError（说法已换成返回字段的）。"""
    if action not in catalog.REVIEW_ACTIONS:
        raise catalog.CatalogPathError("不支持这种审阅操作，只能确认、驳回或撤销审阅")
    if key not in FIELD_KEYS or ("c", pointer, key) not in catalog._slots({"columns": dict(fields or {})}):
        raise catalog.CatalogPathError(f"找不到{where(pointer, key)}，可能已被修改，请重新载入")
    notes = catalog.review_item({"columns": dict(fields or {})}, f"columns.{pointer}.{key}", action, at=at)
    return notes.get("columns", {})


async def review_batch(session: AsyncSession, items: Iterable[BatchItem], *, actor: str | None,
                       verified: bool) -> list[dict[str, Any]]:
    """批量审阅，返回逐条结果（和请求一一对应）。

    同一个能力的几条按请求里的顺序在当前 fields 上依次审阅，一次写入（条件 UPDATE：version 还是读到的那个才写，写成功
    version 加一）：版本不符（if_version 和库里的不一样）、位置不对的那几条不做，其余照做；写入时撞上别人刚改过，这个能力
    的几条都记冲突。每个写成功的能力记一条审计（capability.review）。"""
    from app.auth.audit import record
    from app.db.models import Capability

    items = list(items)
    results: list[dict[str, Any] | None] = [None] * len(items)
    groups: dict[str, list[int]] = {}
    for i, item in enumerate(items):
        groups.setdefault(item.capability, []).append(i)
    at = catalog.now_iso()
    wrote = False
    for name, indexes in groups.items():
        row = await session.get(Capability, name)
        if row is None or row.missing:
            for i in indexes:
                results[i] = _result(items[i], ok=False, code=CODE_MISSING,
                                     error=f"能力「{name}」不存在，或它的来源已被删除")
            continue
        current = row.version
        fields: dict[str, Any] = dict(row.fields or {})
        applied: list[int] = []
        changes: list[dict[str, Any]] = []
        for i in indexes:
            item = items[i]
            if item.if_version != current:
                results[i] = _result(item, ok=False, code=CODE_CONFLICT, error=_conflict(name))
                continue
            before = ((fields.get(item.pointer) or {}).get(item.field) or {}).get("status") \
                if isinstance(fields.get(item.pointer), dict) else None
            try:
                fields = review_field(fields, item.pointer, item.field, item.action, at=at)
            except catalog.CatalogPathError as e:
                results[i] = _result(item, ok=False, code=CODE_PATH, error=str(e))
                continue
            after = ((fields.get(item.pointer) or {}).get(item.field) or {}).get("status")
            applied.append(i)
            changes.append({"pointer": item.pointer, "field": item.field, "action": item.action,
                            "before": before, "after": after})
        if not applied:
            continue
        done = await session.execute(
            update(Capability).where(Capability.name == name, Capability.version == current)
            .values(fields=fields, version=current + 1, updated_by=actor))
        if done.rowcount == 0:
            for i in applied:
                results[i] = _result(items[i], ok=False, code=CODE_CONFLICT, error=_conflict(name))
            continue
        await record(session, "capability.review", "capability", name, actor=actor, verified=verified,
                     data={"version": current + 1, "items": changes})
        wrote = True
        for i in applied:
            results[i] = _result(items[i], ok=True, version=current + 1)
    if wrote:
        await session.commit()
    return [r for r in results if r is not None]


__all__ = ["BATCH_MAX", "BatchItem", "CODE_CONFLICT", "CODE_MISSING", "CODE_PATH", "EFFECT_WEIGHT", "FIELD_KEYS",
           "FieldsInvalid", "KINDS", "PROPOSAL_SOURCES", "QUEUE_LIMIT_MAX", "normalize_fields", "priority",
           "review_batch", "review_field", "review_queue", "run_counts", "where"]
