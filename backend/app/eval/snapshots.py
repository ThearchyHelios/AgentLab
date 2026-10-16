"""目录快照与影子起草（细案 01 做法 6；评测-D5、D6）。

**为什么要快照。** 「目录的贡献」要做对照（当前目录 vs 关、只用已核实的项、起草模型 A 的目录 vs B 的），而评测一跑几十分钟，
期间目录照常被起草、剖析、审阅改动。评测按快照读目录（data/catalog_view.CatalogView(mode="snapshot")），这次用的是哪份
目录就定下来了，也不用为了可比性占住数据源的队、挡住起草和剖析。

**快照的内容**是内容寻址工件（kind=eval_catalog_snapshot），读法由 data/catalog_view.py 定：
{"sources": {源 id: {"tables": {表名: {"version": 版本, "notes": 目录}}}}}；另带 format、taken（谁、何时、来源），读法不看
它们。目录照原样拍（驳回的项也在，消费方照旧经 visible_notes 去掉），statuses 的筛选在读的时候按视图做。工件一旦写好
内容就不变（改一个字哈希就变），EvalCatalogSnapshot 这一行写好 artifact_id 之后也不再改：快照不可变，同一份可以被多次
评测引用。

**两种来源**：
- 当前目录（take_live）：选定数据源此刻的目录，当场拍好。
- 影子起草（后台任务 ShadowDraft）：用指定的起草模型（不指定就按设置里起草模型的取值链，data/draft_model.py）、起草要求，
  把题集用得到的表重新起草进一份**内存里的目录副本**——从当前目录拷一份起步，可选先去掉模型起草、还没人审过的项
  （strip_llm）；起草引擎还是同一个（data/catalog_draft.run_shadow_draft），每一段写出来就按 catalog.merge_draft（和写库
  同一套并入规矩）并进副本。**不调 write_entry、不碰线上目录；不查业务库**（有效行过滤不去数排除比例，见 run_shadow_draft）；
  **不占数据源的队**（lane_of 返回 None），起草、剖析照常进行。全部起草完才写工件：停止、出错、服务重启的不出快照，
  半截的目录拿去对照会误导。有的表没写出来时仍出快照，状态记 partial、逐表原因写在进度里。
- 起草按表计费，大库很贵：只起草题集用得到的表（从期望 SQL、口径的取数 SQL 推，plan_tables），开始前先估算花费
  （estimate，照实写是按经验估的、不含思考）。

进程内的后台任务（core/job_runtime 的 JobManager）：服务重启时没结束的标中断、不自动续跑。startup() 是给 main.py 启动
整理调的那一步（接进 lifespan 和评测执行一起，第 3 波）；在那之前读接口按 reconcile_stale 当场整理：记着进行中、这个进程
里却没在跑的，照实标中断。
"""
from __future__ import annotations

import copy
import logging
import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import job_runtime as runtime
from app.data import catalog
from app.data.catalog_view import SNAPSHOT_KIND
from app.db.base import SessionLocal, utcnow
from app.db.models_eval import EvalCatalogSnapshot

logger = logging.getLogger(__name__)

FORMAT = 1
#: 一次最多拍几个数据源、影子起草最多起草几张表（起草按表计费）
MAX_SOURCES = 20
MAX_SHADOW_TABLES = 200
GUIDANCE_MAX = 2000

#: 机器码
SNAPSHOT_INVALID = "eval_snapshot_invalid"
SNAPSHOT_MODEL_UNAVAILABLE = "eval_snapshot_model_unavailable"
SNAPSHOT_FINISHED = "eval_snapshot_finished"

SOURCE_GONE = "数据源不存在，可能已被删除"
INTERRUPTED = "服务重启时影子起草还没结束，没有出快照。请重新发起"
STOPPED = "影子起草已停止，没有出快照"

#: 估算用的经验值：每列约 50 个输出 token（不算思考，见 data/catalog.py 分段的说明），每张表另加表级的项；输入按提示词
#: 的字数折算（中文为主的提示词约 1.5 个字一个 token）
EST_OUTPUT_PER_COLUMN = 50
EST_OUTPUT_PER_TABLE = 120
EST_CHARS_PER_TOKEN = 1.5
EST_CHARS_PER_COLUMN = 40
EST_NOTE = ("按经验估算：每列约 50 个输出 token、输入按提示词字数折算；不含思考（会思考的模型思考也算在输出里，"
            "实际可能高几倍）、不含拆小重试")


class SnapshotInvalid(ValueError):
    """请求不合规（422）或者开始不了（409）。"""

    def __init__(self, message: str, code: str = SNAPSHOT_INVALID, status: int = 422, field: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status
        self.field = field


# ==========================================================================
# 当前目录的快照
# ==========================================================================


def _content(sources: Mapping[str, Mapping[str, Mapping[str, Any]]], *, taken: Mapping[str, Any]) -> dict[str, Any]:
    """{源 id: {表名: {version, notes}}} → 快照工件的内容（读法见 data/catalog_view.py）。"""
    return {"format": FORMAT, "taken": dict(taken),
            "sources": {sid: {"tables": {t: {"version": int(body.get("version") or 0), "notes": body.get("notes") or {}}
                                         for t, body in tables.items()}}
                        for sid, tables in sources.items()}}


async def _sources(session: AsyncSession, source_ids: Sequence[str]) -> list[Any]:
    from app.db.models import DataSource

    ids = list(dict.fromkeys(s for s in source_ids if isinstance(s, str) and s))
    if not ids:
        raise SnapshotInvalid("请选择要拍快照的数据源", field="source_ids")
    if len(ids) > MAX_SOURCES:
        raise SnapshotInvalid(f"一次最多拍 {MAX_SOURCES} 个数据源", field="source_ids")
    rows = []
    for sid in ids:
        row = await session.get(DataSource, sid)
        if row is None:
            raise SnapshotInvalid(f"数据源 {sid} 不存在，可能已被删除", field="source_ids")
        rows.append(row)
    return rows


async def _live_tables(session: AsyncSession, source_ids: Sequence[str]) -> dict[str, dict[str, dict[str, Any]]]:
    """这几个源此刻的目录原样：{源 id: {表名: {version, notes}}}（驳回的项也在）。"""
    found = await catalog.read_catalogs(session, list(source_ids))
    return {sid: {t: {"version": e.version, "notes": copy.deepcopy(e.notes)} for t, e in (found.get(sid) or {}).items()}
            for sid in source_ids}


async def take_live(session: AsyncSession, source_ids: Sequence[str], *, actor: str | None) -> EvalCatalogSnapshot:
    """拍选定数据源此刻的目录，当场写工件、登记一行（succeeded）。"""
    from app.core.artifact_store import put_json

    rows = await _sources(session, source_ids)
    ids = [r.id for r in rows]
    at = utcnow()
    taken = {"origin": "live", "by": actor, "at": at.isoformat()}
    content = _content(await _live_tables(session, ids), taken=taken)
    artifact = await put_json(content, kind=SNAPSHOT_KIND, meta={"origin": "live", "sources": ids})
    snap = EvalCatalogSnapshot(origin="live", status="succeeded", artifact_id=artifact,
                               sources=[{"id": r.id, "name": r.name} for r in rows], created_by=actor,
                               started_at=at, finished_at=at)
    session.add(snap)
    await session.commit()
    await session.refresh(snap)
    return snap


# ==========================================================================
# 影子起草：起草哪些表、估算
# ==========================================================================


def _schema_tables(source: Any) -> dict[str, Any]:
    return (getattr(source, "schema_cache", None) or {}).get("tables") or {}


def _sql_tables(sql: str) -> list[str]:
    from app.engine.evidence import sql_tables

    try:
        return sql_tables(sql or "")
    except Exception:  # noqa: BLE001 - 解析不了就当没用到表：只影响起草哪些表
        return []


async def suite_tables(session: AsyncSession, suite_id: str) -> dict[str, list[str]]:
    """题集用得到的表：{源 id: [SQL 里写的表名]}。从期望 SQL（SQL 题、从运行认定）和口径题的取数 SQL 推；工作流题、
    只有反问的题推不出。名字还没对上表结构（plan_shadow 再对）。"""
    from app.db.models import DataSource
    from app.db.models_eval import EvalCase, EvalSuite
    from app.eval.cases import render, variable_values
    from app.ledger import derive
    from app.tools.datasource import QUERY_PREFIX

    suite = await session.get(EvalSuite, suite_id)
    if suite is None:
        raise SnapshotInvalid("题集不存在，可能已被删除", field="suite_id")
    values = variable_values(utcnow(), suite.timezone)
    out: dict[str, list[str]] = {}
    rows = (await session.execute(select(EvalCase).where(EvalCase.suite_id == suite_id, EvalCase.archived.is_(False))
                                  .order_by(EvalCase.seq))).scalars().all()
    by_name: dict[str, str] = {}
    for case in rows:
        gold = case.gold or {}
        if gold.get("kind") in ("sql", "from_run"):
            out.setdefault(gold["datasource_id"], []).extend(_sql_tables(render(gold.get("sql") or "", values)))
        elif gold.get("kind") == "caliber":
            try:
                pinned = await derive.load_pinned(session, gold["workflow_id"], gold["version"], gold["node_id"])
            except derive.NotDerivable:
                continue
            for node in pinned.fetches:
                cfg = (node.get("data") or {}).get("config") or {}
                tool = str(cfg.get("tool") or "")
                name = tool[len(QUERY_PREFIX):]
                if name not in by_name:
                    sid = (await session.execute(select(DataSource.id).where(DataSource.name == name))).scalar()
                    by_name[name] = sid or ""
                if by_name[name]:
                    out.setdefault(by_name[name], []).extend(_sql_tables(str((cfg.get("args") or {}).get("sql") or "")))
    return {sid: list(dict.fromkeys(names)) for sid, names in out.items()}


def estimate(sources: Sequence[tuple[Any, Sequence[str]]], model_id: str) -> dict[str, Any]:
    """估一次影子起草要调几次模型、多少 token、多少钱（照实写是按经验估的）。分段同起草引擎：不超过 MODEL_BATCH_COLUMNS
    列的表按顺序最多 MODEL_BATCH_TABLES 张拼一段，宽表是表级一段加列分段（catalog_draft.parts_for）。"""
    from app.data import catalog_draft
    from app.providers.catalog import estimate_cost, find_pricing

    width = max(1, int(catalog.MODEL_BATCH_COLUMNS))
    per_call = max(1, int(catalog.MODEL_BATCH_TABLES))
    system = len(catalog.CATALOG_DRAFT_SYSTEM)
    calls = tables = columns = 0
    chars = out_tokens = 0
    for source, names in sources:
        meta = _schema_tables(source)
        batch_tables = batch_columns = 0
        for name in names:
            n = len([c for c in (meta.get(name) or {}).get("columns") or [] if isinstance(c, dict)])
            tables += 1
            columns += n
            out_tokens += EST_OUTPUT_PER_TABLE + n * EST_OUTPUT_PER_COLUMN
            if n > width:
                parts = catalog_draft.parts_for(n)
                calls += parts
                # 表级那段给全部列名，列分段各给自己那几列
                chars += parts * system + n * EST_CHARS_PER_COLUMN * 2
                continue
            if batch_tables and (batch_tables >= per_call or batch_columns + n > width):
                calls += 1
                chars += system
                batch_tables = batch_columns = 0
            batch_tables += 1
            batch_columns += n
            chars += n * EST_CHARS_PER_COLUMN
        if batch_tables:
            calls += 1
            chars += system
    in_tokens = int(math.ceil(chars / EST_CHARS_PER_TOKEN))
    priced = bool(model_id) and find_pricing(model_id) is not None
    return {"tables": tables, "columns": columns, "calls": calls, "input_tokens": in_tokens, "output_tokens": out_tokens,
            "model": model_id or None, "cost_usd": round(estimate_cost(model_id, in_tokens, out_tokens), 4) if priced
            else None, "unpriced": not priced, "note": EST_NOTE}


@dataclass
class ShadowPlan:
    """一次影子起草要做什么：每个源起草哪些表（表结构里的键）、对不上表结构的表名；估算；起草设定。"""

    sources: list[dict[str, Any]] = field(default_factory=list)
    estimate: dict[str, Any] = field(default_factory=dict)
    draft: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"sources": copy.deepcopy(self.sources), "estimate": dict(self.estimate), "draft": dict(self.draft)}


async def resolve_model(session: AsyncSession, provider: str | None, model: str | None) -> tuple[Any, str]:
    """影子起草用的模型：(模型, 模型 id)。指定了接入或模型就用它（同一个起草的建法，catalog.build_draft_model）；都没指定
    按设置里起草模型的取值链（catalog.resolve_draft_model）。用不了抛 SnapshotInvalid（409）。不发请求。"""
    from app.core.errors import not_configured
    from app.providers.factory import ProviderNotConfigured, resolve_provider

    if not provider and not model:
        try:
            return await catalog.resolve_draft_model(session)
        except catalog.CatalogModelUnavailable as e:
            raise SnapshotInvalid(str(e), SNAPSHOT_MODEL_UNAVAILABLE, 409) from e
    try:
        row = await resolve_provider(session, provider, model)
        if row is None:
            raise SnapshotInvalid("未配置模型接入，无法影子起草。请到「设置 → 模型接入」添加", SNAPSHOT_MODEL_UNAVAILABLE, 409)
        if row.kind == "mock":
            raise SnapshotInvalid("演示用的模型接入不能用于起草目录：起草出来的目录会被当成真的拿去对照",
                                  SNAPSHOT_MODEL_UNAVAILABLE, 409)
        return catalog.build_draft_model(row, provider, model)
    except ProviderNotConfigured as e:
        raise SnapshotInvalid(f"影子起草指定的模型用不了：{not_configured(e)}", SNAPSHOT_MODEL_UNAVAILABLE, 409) from e


def _text(value: Any, where: str, field_name: str) -> str | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str) or len(value.strip()) > GUIDANCE_MAX:
        raise SnapshotInvalid(f"{where}需要是不超过 {GUIDANCE_MAX} 个字符的文字", field=field_name)
    return value.strip() or None


async def plan_shadow(session: AsyncSession, body: Mapping[str, Any], *, model_id: str | None = None) -> ShadowPlan:
    """影子起草的请求 → 计划。body：{sources: [{id, tables?}], suite_id?, provider?, model?, guidance?, note?,
    strip_llm?}。每个源没给表就按题集推（suite_tables）；推不出、也没给的报错。model_id 给了就按它估价（接口先解析模型）。"""
    if not isinstance(body, Mapping):
        raise SnapshotInvalid("请求内容需要是 JSON 对象")
    raw = body.get("sources")
    if not isinstance(raw, list) or not raw or not all(isinstance(s, Mapping) and isinstance(s.get("id"), str)
                                                       for s in raw):
        raise SnapshotInvalid("请选择要影子起草的数据源：sources 是 [{id, tables}]", field="sources")
    suite_id = body.get("suite_id") if isinstance(body.get("suite_id"), str) and body.get("suite_id") else None
    from_suite = await suite_tables(session, suite_id) if suite_id else {}
    rows = await _sources(session, [s["id"] for s in raw])
    picked: list[dict[str, Any]] = []
    sources_for_estimate: list[tuple[Any, list[str]]] = []
    total = 0
    for row, item in zip(rows, raw):
        wanted = item.get("tables")
        if wanted is not None and (not isinstance(wanted, list) or not all(isinstance(t, str) for t in wanted)):
            raise SnapshotInvalid("tables 需要是一组表名", field="sources")
        wanted = list(wanted) if wanted else from_suite.get(row.id, [])
        if not wanted:
            raise SnapshotInvalid(f"数据源「{row.name}」没有指定要起草的表"
                                  + ("，题集里也推不出用到它的哪些表" if suite_id else "：请列出表名，或给出题集（按题集的期望 SQL 推）"),
                                  field="sources")
        keys, unknown = catalog.pick_tables(getattr(row, "schema_cache", None) or {}, wanted)
        if not keys:
            raise SnapshotInvalid(f"数据源「{row.name}」的表结构里没有这些表：{'、'.join(unknown[:10])}。请先重新探查结构",
                                  field="sources")
        total += len(keys)
        picked.append({"id": row.id, "name": row.name, "tables": keys, "unknown": unknown})
        sources_for_estimate.append((row, keys))
    if total > MAX_SHADOW_TABLES:
        raise SnapshotInvalid(f"一次影子起草最多 {MAX_SHADOW_TABLES} 张表（这次 {total} 张）：起草按表计费，请缩小范围",
                              field="sources")
    strip = body.get("strip_llm", False)
    if not isinstance(strip, bool):
        raise SnapshotInvalid("strip_llm 需要是 true 或 false", field="strip_llm")
    draft = {"provider": body.get("provider") or None, "model": body.get("model") or None, "model_id": model_id,
             "guidance": _text(body.get("guidance"), "起草要求", "guidance"),
             "note": _text(body.get("note"), "本次说明", "note"), "strip_llm": strip, "suite_id": suite_id}
    return ShadowPlan(sources=picked, estimate=estimate(sources_for_estimate, model_id or ""), draft=draft)


# ==========================================================================
# 影子起草：后台任务
# ==========================================================================


def strip_model_items(notes: Mapping[str, Any]) -> dict[str, Any]:
    """去掉模型起草、还没人审过的项（source=llm、status=proposed）；人工确认、驳回过的模型项留着（审过了就是人的结论）。"""
    slots = catalog._slots(notes)
    return catalog._assemble({k: v for k, v in slots.items()
                              if not (v.get("source") == "llm" and v.get("status") == "proposed")})


async def start_shadow(session: AsyncSession, plan: ShadowPlan, *, actor: str | None) -> EvalCatalogSnapshot:
    """登记一次影子起草（queued）并在后台开始。"""
    snap = EvalCatalogSnapshot(origin="shadow", status="queued", created_by=actor,
                               sources=[{"id": s["id"], "name": s["name"]} for s in plan.sources],
                               tables={s["id"]: list(s["tables"]) for s in plan.sources}, draft=dict(plan.draft),
                               estimate=dict(plan.estimate),
                               progress={"tables_total": sum(len(s["tables"]) for s in plan.sources), "tables_done": 0,
                                         "tables_failed": 0, "failures": {}})
    session.add(snap)
    await session.commit()
    await session.refresh(snap)
    manager.start(snap.id)
    return snap


class ShadowDraft(runtime.JobRun):
    """一次影子起草在后台的执行。不占数据源的队；全部起草完才写快照工件。"""

    kind = "eval_shadow_draft"
    model = EvalCatalogSnapshot
    #: 测试注入的起草模型（不给就按登记的设定解析）
    model_override: Any = None

    def __init__(self, job_id: str) -> None:
        super().__init__(job_id)
        self.model_id = ""
        self.copy: dict[str, dict[str, dict[str, Any]]] = {}
        self.usage = {"calls": 0, "input_tokens": 0, "output_tokens": 0, "timeouts": 0, "failed_calls": 0}
        self.done = 0
        #: 没起草出来的表（一项都没写出来、表结构里没有）和只写出一部分的表：{「源名.表名」: 原因}
        self.failed: dict[str, str] = {}
        self.partial: dict[str, str] = {}
        self.notice: str | None = None

    def lane_of(self, job: Any) -> str | None:
        return None

    async def _claim(self, session: AsyncSession) -> bool:
        return await runtime.claim(session, EvalCatalogSnapshot, self.job_id)

    async def _run(self) -> None:
        from app.data import catalog_draft, table_versions
        from app.db.models import DataSource

        if not await self._write(self._claim):
            return
        async with SessionLocal() as session:
            snap = await session.get(EvalCatalogSnapshot, self.job_id)
            draft = dict(snap.draft or {})
            plan = dict(snap.tables or {})
            ids = [s["id"] for s in snap.sources or []]
            sources: list[tuple[Any, list[str]]] = []
            for sid in ids:
                row = await session.get(DataSource, sid)
                if row is None:
                    raise runtime.JobFailed(SOURCE_GONE)
                try:
                    sources.append((await table_versions.resolve_source(session, row), list(plan.get(sid) or [])))
                except table_versions.SnapshotMissing as e:
                    raise runtime.JobFailed(str(e)) from e
            if self.model_override is not None:
                model, model_id = self.model_override, str(getattr(self.model_override, "model_name", "") or "")
            else:
                try:
                    model, model_id = await resolve_model(session, draft.get("provider"), draft.get("model"))
                except SnapshotInvalid as e:
                    raise runtime.JobFailed(e.message) from e
            live = await _live_tables(session, ids)
        self.model_id = model_id
        if draft.get("strip_llm"):
            live = {sid: {t: {**body, "notes": strip_model_items(body["notes"])} for t, body in tables.items()}
                    for sid, tables in live.items()}
        self.copy = live
        background = catalog_draft.Background(guidance=draft.get("guidance"), note=draft.get("note"))
        at = catalog.now_iso()
        for source, tables in sources:
            names = [t for t in tables if isinstance(_schema_tables(source).get(t), dict)]
            for gone in [t for t in tables if t not in names]:
                self.failed[f"{source.name}.{gone}"] = catalog.missing_table_error(gone)
            existing = {t: body["notes"] for t, body in self.copy.get(source.id, {}).items()}
            hooks = catalog_draft.Hooks(call_done=self._call_done, part_written=self._part_written(source, at),
                                        table_done=self._table_done(source, at))
            await catalog_draft.run_shadow_draft(model, source, names, existing=existing, background=background,
                                                 hooks=hooks)
        await self._settle()
        await self._finish(at)

    # ---------------------------------------------------------------- 进度回调（起草引擎调）

    def _body(self, source: Any, table: str) -> dict[str, Any]:
        # 版本照抄当前目录的：影子起草改的是副本，不是线上目录的新版本（线上那张表没有目录的记 0）
        return self.copy.setdefault(source.id, {}).setdefault(table, {"version": 0, "notes": {}})

    def _merge(self, source: Any, table: str, modeled: Any, at: str) -> None:
        """整张表起草完：按写库同一套规矩（catalog.merge_draft）并进副本。"""
        body = self._body(source, table)
        body["notes"], _ = catalog.merge_draft(body.get("notes") or {}, source, table, modeled, at=at)
        if not body["notes"] and not body.get("version"):
            self.copy[source.id].pop(table, None)

    async def _call_done(self, report: Any) -> None:
        self.usage["calls"] += 1
        self.usage["input_tokens"] += int(report.input_tokens or 0)
        self.usage["output_tokens"] += int(report.output_tokens or 0)
        self.usage["timeouts"] += 1 if report.timeout else 0
        self.usage["failed_calls"] += 1 if report.failure else 0
        self.notice = report.notice
        await self._write(self._save_progress)

    def _part_written(self, source: Any, at: str):
        async def hook(table: str, written: Any) -> None:
            # 宽表写出来的一段：并进副本，这一次不删任何东西（同 catalog_jobs.write_part）；整张表的收尾在 table_done
            body = self._body(source, table)
            merged, _ = catalog.merge_notes(body.get("notes") or {}, catalog.draft_structure(source, table).notes, at=at)
            body["notes"], _ = catalog.merge_notes(merged, written.notes, at=at)
        return hook

    def _table_done(self, source: Any, at: str):
        async def hook(table: str, outcome: Any) -> None:
            self._merge(source, table, outcome, at)
            self.done += 1
            if outcome is not None and outcome.status == "failed":
                self.failed[f"{source.name}.{table}"] = outcome.error or catalog.MODEL_EMPTY_REASON
            elif outcome is not None and outcome.status == "partial":
                self.partial[f"{source.name}.{table}"] = outcome.error or "只写出了一部分"
            await self._write(self._save_progress)
        return hook

    # ---------------------------------------------------------------- 库操作

    def _cost(self) -> dict[str, Any]:
        from app.providers.catalog import estimate_cost, find_pricing

        model_id = self.model_id or ""
        priced = bool(model_id) and find_pricing(model_id) is not None
        cost = estimate_cost(model_id, self.usage["input_tokens"], self.usage["output_tokens"]) if priced else None
        return {**self.usage, "cost_usd": round(cost, 6) if cost is not None else None,
                "unpriced": bool(self.usage["calls"]) and not priced}

    def _progress(self, current: dict[str, Any]) -> dict[str, Any]:
        return {**current, "tables_done": self.done, "tables_failed": len(self.failed),
                "tables_partial": len(self.partial), "failures": {**self.partial, **self.failed},
                "notice": self.notice}

    async def _save_progress(self, session: AsyncSession) -> None:
        snap = await session.get(EvalCatalogSnapshot, self.job_id)
        if snap is None:
            return
        snap.progress = self._progress(dict(snap.progress or {}))
        snap.usage = self._cost()
        draft = dict(snap.draft or {})
        if self.model_id and draft.get("model_id") != self.model_id:
            snap.draft = {**draft, "model_id": self.model_id}
        await session.commit()

    async def _finish(self, at: str) -> None:
        from app.core.artifact_store import put_json

        async with SessionLocal() as session:
            snap = await session.get(EvalCatalogSnapshot, self.job_id)
            if snap is None:
                return
            ids = [s["id"] for s in snap.sources or []]
            planned = dict(snap.tables or {})
            taken = {"origin": "shadow", "by": snap.created_by, "at": utcnow().isoformat(),
                     "draft": {**dict(snap.draft or {}), "model_id": self.model_id or None}, "tables": planned}
        content = _content({sid: self.copy.get(sid, {}) for sid in ids}, taken=taken)
        total = sum(len(v or []) for v in planned.values())
        # 一张都没起草出来才算失败（不出快照）；有没写出来、只写出一部分的表是 partial，快照照出、原因写在进度里
        wrote = total - len(self.failed)
        status = "failed" if wrote <= 0 else "partial" if self.failed or self.partial else "succeeded"
        artifact = await put_json(content, kind=SNAPSHOT_KIND, meta={"origin": "shadow", "sources": ids}) \
            if status != "failed" else None

        async def write(session: AsyncSession) -> None:
            row = await session.get(EvalCatalogSnapshot, self.job_id)
            row.progress = self._progress(dict(row.progress or {}))
            row.usage = self._cost()
            row.status = status
            row.artifact_id = artifact
            row.finished_at = utcnow()
            if status == "failed":
                row.error = "每张表都没有起草出来，没有出快照：" + "；".join(
                    f"{t}：{why}" for t, why in list(self.failed.items())[:5])
            await session.commit()

        await self._write(write)

    async def _close(self, status: str, error: str) -> None:
        """停止、出错时收尾：不写工件（半截的目录拿去对照会误导），进度和用量照实留着。"""
        async def write(session: AsyncSession) -> None:
            row = await session.get(EvalCatalogSnapshot, self.job_id)
            if row is None or row.status not in runtime.ACTIVE:
                return
            row.status, row.error, row.finished_at = status, error, utcnow()
            row.usage = self._cost()
            row.progress = self._progress(dict(row.progress or {}))
            await session.commit()

        await self._settle()
        try:
            await self._write(write)
        except Exception:  # noqa: BLE001
            logger.exception("影子起草 %s 收尾时写库出错", self.job_id)

    async def _stopped(self) -> None:
        if self.stop_reason == "shutdown":
            await self._close("interrupted", INTERRUPTED)
        else:
            await self._close("cancelled", STOPPED)

    async def _failed(self, reason: str) -> None:
        await self._close("failed", reason)

    async def _crashed(self, e: Exception) -> None:
        from app.core.errors import describe_exception

        logger.exception("影子起草 %s 出错", self.job_id)
        await self._close("failed", f"影子起草出错：{describe_exception(e)}")


class ShadowDrafts(runtime.JobManager):
    def __init__(self) -> None:
        super().__init__(ShadowDraft, label="影子起草任务", name="eval-shadow")


manager = ShadowDrafts()


async def startup() -> int:
    """服务启动时：没结束的影子起草标中断（不自动续跑）。"""
    async def close(session: AsyncSession, job_id: str) -> None:
        await session.execute(update(EvalCatalogSnapshot).where(
            EvalCatalogSnapshot.id == job_id, EvalCatalogSnapshot.status.in_(runtime.ACTIVE))
            .values(status="interrupted", error=INTERRUPTED, finished_at=utcnow())
            .execution_options(synchronize_session=False))
        await session.commit()

    return await runtime.interrupt_on_startup(EvalCatalogSnapshot, close, running=manager.running)


async def reconcile_stale(session: AsyncSession, rows: Iterable[EvalCatalogSnapshot]) -> None:
    """读接口当场整理：记着排队、进行中，这个进程里却没在跑（服务重启过）的，照实标中断。"""
    stale = [r for r in rows if r.status in runtime.ACTIVE and not manager.running(r.id)]
    for row in stale:
        row.status, row.error, row.finished_at = "interrupted", INTERRUPTED, utcnow()
    if stale:
        await session.commit()


async def stop(job_id: str) -> bool:
    """停止一次影子起草。排队中的直接记 cancelled；进行中的停掉、等它收尾。已经结束的返回 False。"""
    async with SessionLocal() as session:
        row = await session.get(EvalCatalogSnapshot, job_id)
        if row is None or row.status not in runtime.ACTIVE:
            return False
    if await manager.stop(job_id, "user"):
        return True
    async with SessionLocal() as session:
        result = await session.execute(update(EvalCatalogSnapshot).where(
            EvalCatalogSnapshot.id == job_id, EvalCatalogSnapshot.status.in_(runtime.ACTIVE))
            .values(status="cancelled", error=STOPPED, finished_at=utcnow()).execution_options(synchronize_session=False))
        await session.commit()
        return bool(result.rowcount)


def snapshot_out(row: EvalCatalogSnapshot) -> dict[str, Any]:
    """接口的形状。"""
    return {"id": row.id, "origin": row.origin, "status": row.status, "artifact_id": row.artifact_id,
            "sources": list(row.sources or []), "tables": row.tables, "draft": row.draft, "estimate": row.estimate,
            "usage": dict(row.usage or {}), "progress": dict(row.progress or {}), "error": row.error,
            "created_by": row.created_by, "created_at": row.created_at.isoformat() if row.created_at else None,
            "started_at": row.started_at.isoformat() if row.started_at else None,
            "finished_at": row.finished_at.isoformat() if row.finished_at else None,
            # 变体按工件 id 指定目录：data/catalog_view.CatalogView(mode="snapshot", snapshot=…)
            "view": {"mode": "snapshot", "statuses": "all", "snapshot": row.artifact_id} if row.artifact_id else None}


__all__ = ["FORMAT", "SNAPSHOT_FINISHED", "SNAPSHOT_INVALID", "SNAPSHOT_MODEL_UNAVAILABLE", "ShadowDraft", "ShadowPlan",
           "SnapshotInvalid", "estimate", "manager", "plan_shadow", "reconcile_stale", "resolve_model", "snapshot_out",
           "start_shadow", "startup", "stop", "strip_model_items", "suite_tables", "take_live"]
