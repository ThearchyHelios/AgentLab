"""题集的导出导入（细案 01 做法 19）：本机整理好的题集带到部署实例上；仓库里的「零售样例题集」也按这个格式写
（tests/fixtures/eval/retail_suite.json）。

**为什么要有它。** 真实业务的题集只在部署实例上建、上跑（评测-D15：真实数据不出部署实例、不进公开仓库）；样例题集、
本机写好的题目要能带过去。硬编码在测试里的题集部署实例上用不了。

**文件**（export_suite）：{format: "agentlab-eval-suite", version: 1, exported_at, suite: {name, description, timezone,
budget}, cases: [{seq, kind, question, datasources: [数据源名], workflow: {name, version}, input_template, expectations,
gold, tags, enabled}]}。期望来源里的数据源、工作流写名字（id 只在这台机器上有效）：sql 题 {kind, datasource, sql}，
口径题 {kind, workflow, version, node_id, inputs}，从运行认定 {kind, datasource, sql, confirmed_by, confirmed_at}。
**不含期望值、答案、运行**：期望每次评测都重算，不冻结数值；从运行认定的那次运行、升版的处置记录只在这台机器上有意义，
不导出。默认变体也不导出（里面是这台机器上的模型、目录快照）。归档的题不导出。

**导入**（plan_import 预览和导入共用这一份规矩）：
- 数据源、工作流按名字对上（一字不差）；对不上的题跳过、写原因。
- 每道题过和新建题目同一套校验（cases.normalize_case + resolve_case），导入时再试算一次（和保存题目时同一道关：只读守卫、
  遮罩拒收、取得到值），没过的跳过、写原因（接口层做，见 api/evals.import_suite）。一道都导不进来就不建题集。
- 文件格式不对（不是对象、格式标记或版本不对）抛 TransferInvalid，接口回 422。
"""
from __future__ import annotations

import copy
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.eval.cases import CaseInvalid, normalize_case, resolve_case
from app.eval.suites import normalize_suite

FORMAT = "agentlab-eval-suite"
VERSION = 1
FILE_SUFFIX = "-题集.json"
#: 一个文件最多几道题
MAX_CASES = 500
_BAD_FILENAME = re.compile(r'[\\/:*?"<>|\x00-\x1f\x7f]')

#: 跳过的原因
SOURCE_UNMATCHED = "数据源「{name}」在这里没有（按名字对，一字不差）"
WORKFLOW_UNMATCHED = "工作流「{name}」在这里没有（按名字对，一字不差）"


class TransferInvalid(ValueError):
    """文件格式不对。message 给人看（接口回 422）。"""


def attachment_name(suite_name: str) -> str:
    name = _BAD_FILENAME.sub("_", suite_name or "").strip(" .")[:80].strip(" .")
    return f"{name or '题集'}{FILE_SUFFIX}"


# ==========================================================================
# 导出
# ==========================================================================


async def _names(session: AsyncSession, model: Any, ids: set[str]) -> dict[str, str]:
    if not ids:
        return {}
    rows = await session.execute(select(model.id, model.name).where(model.id.in_(sorted(ids))))
    return {i: n for i, n in rows.all()}


def _gold_out(gold: Mapping[str, Any] | None, sources: Mapping[str, str], workflows: Mapping[str, str]
              ) -> dict[str, Any] | None:
    if not gold:
        return None
    kind = gold.get("kind")
    if kind == "caliber":
        return {"kind": "caliber", "workflow": workflows.get(gold.get("workflow_id"), gold.get("workflow_id")),
                "version": gold.get("version"), "node_id": gold.get("node_id"),
                "inputs": copy.deepcopy(gold.get("inputs") or {})}
    out = {"kind": kind, "datasource": sources.get(gold.get("datasource_id"), gold.get("datasource_id")),
           "sql": gold.get("sql")}
    if kind == "from_run":
        out |= {k: gold[k] for k in ("confirmed_by", "confirmed_at") if gold.get(k)}
    return out


async def export_suite(session: AsyncSession, suite: Any, cases: list[Any]) -> dict[str, Any]:
    from app.db.base import utcnow
    from app.db.models import DataSource, Workflow

    kept = [c for c in cases if not c.archived]
    source_ids = {sid for c in kept for sid in c.datasource_ids or []} | {
        (c.gold or {}).get("datasource_id") for c in kept if (c.gold or {}).get("datasource_id")}
    workflow_ids = {c.workflow_id for c in kept if c.workflow_id} | {
        (c.gold or {}).get("workflow_id") for c in kept if (c.gold or {}).get("workflow_id")}
    sources = await _names(session, DataSource, {s for s in source_ids if s})
    workflows = await _names(session, Workflow, {w for w in workflow_ids if w})
    out_cases = []
    for c in sorted(kept, key=lambda c: c.seq):
        item: dict[str, Any] = {"seq": c.seq, "kind": c.kind, "question": c.question,
                                "datasources": [sources.get(s, s) for s in c.datasource_ids or []],
                                "expectations": copy.deepcopy(list(c.expectations or [])),
                                "gold": _gold_out(c.gold, sources, workflows), "tags": list(c.tags or []),
                                "enabled": bool(c.enabled)}
        if c.kind == "workflow":
            item["workflow"] = {"name": workflows.get(c.workflow_id, c.workflow_id), "version": c.workflow_version}
            item["input_template"] = dict(c.input_template or {})
        out_cases.append(item)
    return {"format": FORMAT, "version": VERSION, "exported_at": utcnow().isoformat(),
            "suite": {"name": suite.name, "description": suite.description or "", "timezone": suite.timezone,
                      "budget": dict(suite.budget or {})},
            "cases": out_cases}


# ==========================================================================
# 导入
# ==========================================================================


@dataclass
class PlannedCase:
    """文件里的一道题：对上了的（fields 是 normalize_case 的结果，可以直接建）或者跳过的（reason）。"""

    seq: int | None
    question: str
    fields: dict[str, Any] | None = None
    reason: str | None = None
    code: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"seq": self.seq, "question": self.question, "ok": self.fields is not None, "reason": self.reason,
                "code": self.code}


@dataclass
class ImportPlan:
    suite: dict[str, Any]
    cases: list[PlannedCase] = field(default_factory=list)
    #: 文件里提到的数据源、工作流名 → 这里的 id（对不上为 None）
    sources: dict[str, str | None] = field(default_factory=dict)
    workflows: dict[str, str | None] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"suite": dict(self.suite), "cases": [c.to_dict() for c in self.cases],
                "sources": dict(self.sources), "workflows": dict(self.workflows),
                "importable": sum(1 for c in self.cases if c.fields is not None),
                "skipped": sum(1 for c in self.cases if c.fields is None)}


def read_file(content: Any) -> tuple[dict[str, Any], list[Any]]:
    if not isinstance(content, Mapping):
        raise TransferInvalid("题集文件需要是一个 JSON 对象")
    if content.get("format") != FORMAT:
        raise TransferInvalid(f"不是题集文件（format 应为 {FORMAT}）")
    if content.get("version") != VERSION:
        raise TransferInvalid(f"题集文件的版本是 {content.get('version')}，这里只认 {VERSION}")
    suite = content.get("suite")
    cases = content.get("cases")
    if not isinstance(suite, Mapping) or not isinstance(cases, list):
        raise TransferInvalid("题集文件里需要有 suite（对象）和 cases（列表）")
    if len(cases) > MAX_CASES:
        raise TransferInvalid(f"一个题集文件最多 {MAX_CASES} 道题")
    return dict(suite), cases


async def _lookup(session: AsyncSession, model: Any, names: set[str]) -> dict[str, str | None]:
    found = dict((await session.execute(select(model.name, model.id).where(model.name.in_(sorted(names))))).all()) \
        if names else {}
    return {n: found.get(n) for n in sorted(names)}


async def plan_import(session: AsyncSession, content: Any) -> ImportPlan:
    """按这台机器上的数据源、工作流对上文件里的每道题，逐题过 normalize_case + resolve_case。不写库、不查业务库。"""
    from app.db.models import DataSource, Workflow

    raw_suite, raw_cases = read_file(content)
    try:
        suite = normalize_suite({k: raw_suite.get(k) for k in ("name", "description", "timezone", "budget")
                                 if raw_suite.get(k) is not None})
    except CaseInvalid as e:
        raise TransferInvalid(f"题集：{e.message}") from e
    source_names = set()
    workflow_names = set()
    for c in raw_cases:
        if not isinstance(c, Mapping):
            continue
        source_names |= {s for s in c.get("datasources") or [] if isinstance(s, str)}
        gold = c.get("gold") if isinstance(c.get("gold"), Mapping) else {}
        if isinstance(gold.get("datasource"), str):
            source_names.add(gold["datasource"])
        if isinstance(gold.get("workflow"), str):
            workflow_names.add(gold["workflow"])
        if isinstance(c.get("workflow"), Mapping) and isinstance(c["workflow"].get("name"), str):
            workflow_names.add(c["workflow"]["name"])
    plan = ImportPlan(suite=suite, sources=await _lookup(session, DataSource, source_names),
                      workflows=await _lookup(session, Workflow, workflow_names))
    for i, c in enumerate(raw_cases):
        plan.cases.append(await _plan_case(session, c, i, plan, suite["timezone"]))
    return plan


async def _plan_case(session: AsyncSession, raw: Any, i: int, plan: ImportPlan, tz: str) -> PlannedCase:
    if not isinstance(raw, Mapping):
        return PlannedCase(seq=None, question="", reason=f"第 {i + 1} 道题不是一个对象")
    seq = raw.get("seq") if isinstance(raw.get("seq"), int) and not isinstance(raw.get("seq"), bool) else None
    question = str(raw.get("question") or "")
    planned = PlannedCase(seq=seq, question=question)

    def source_id(name: Any) -> str | None:
        return plan.sources.get(name) if isinstance(name, str) else None

    def workflow_id(name: Any) -> str | None:
        return plan.workflows.get(name) if isinstance(name, str) else None

    body: dict[str, Any] = {k: copy.deepcopy(raw[k]) for k in ("kind", "question", "expectations", "tags", "enabled",
                                                                "input_template") if k in raw}
    ids = []
    for name in raw.get("datasources") or []:
        if (sid := source_id(name)) is None:
            planned.reason = SOURCE_UNMATCHED.format(name=name)
            return planned
        ids.append(sid)
    body["datasource_ids"] = ids
    if isinstance(raw.get("workflow"), Mapping):
        if (wid := workflow_id(raw["workflow"].get("name"))) is None:
            planned.reason = WORKFLOW_UNMATCHED.format(name=raw["workflow"].get("name"))
            return planned
        body["workflow_id"], body["workflow_version"] = wid, raw["workflow"].get("version")
    gold = raw.get("gold")
    trusted = False
    if isinstance(gold, Mapping):
        gold = dict(gold)
        if "datasource" in gold:
            name = gold.pop("datasource")
            if (sid := source_id(name)) is None:
                planned.reason = SOURCE_UNMATCHED.format(name=name)
                return planned
            gold["datasource_id"] = sid
        if "workflow" in gold:
            name = gold.pop("workflow")
            if (wid := workflow_id(name)) is None:
                planned.reason = WORKFLOW_UNMATCHED.format(name=name)
                return planned
            gold["workflow_id"] = wid
        # 从运行认定的题带着认定人和时间过来（运行本身不导出）；它只能由服务端建，这里就是服务端
        trusted = gold.get("kind") == "from_run"
    body["gold"] = gold
    try:
        fields = normalize_case(body, trusted_gold=trusted)
        await resolve_case(session, fields, tz=tz)
    except CaseInvalid as e:
        planned.reason, planned.code = e.message, e.code
        return planned
    planned.fields = fields
    return planned


__all__ = ["FORMAT", "FILE_SUFFIX", "ImportPlan", "MAX_CASES", "PlannedCase", "TransferInvalid", "VERSION",
           "attachment_name", "export_suite", "plan_import", "read_file"]
