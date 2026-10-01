"""按配方导入的流程（recipe_imports，WP-5c）：用假 Pipeline 测状态机、提交事务、并发与失败回滚。

假 Pipeline 返回固定的扫描、网格、Extraction、核对和说明；试运行写一个真的 SQLite 小库（publish_build、
探查、哈希核对都是真的）。各用例按需改 FakeState。接口层的错误码在 test_table_imports_api 里逐个打。
夹具一律合成、假名；不调用任何真实模型（AI 起草换成桩，first_call_text 的一致性用 recipe_ai 自己的
ai_draft 加假模型核对）。
"""
from __future__ import annotations

import asyncio
import copy
import dataclasses
import datetime as dt
import hashlib
import inspect
import json
import sqlite3
import threading
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from app.core.config import settings
from app.data import (
    recipe, recipe_ai, recipe_imports, recipe_notes, recipe_parsers, recipe_types, table_versions, xlsx_cells,
    xlsx_scan,
)
from app.data.recipe_ai import AiDraftResult, AiTooLarge, Compressed
from app.data.recipe_imports import Pipeline
from app.data.recipe_types import (
    REASON_SLOT, AiAvailability, AiUsage, Card, CheckResult, ColumnOut, ConfirmItem, DiffItem, Draft, DraftFacts,
    Extraction, Grid, GridCell, PeriodOut, Problem, Question, Recipe, RecipeProblem, RegionMark, SchemaNotes,
    TableNote, TableOut,
)
from app.data.xlsx_scan import Bounds, SheetScan, UnsupportedTable, WorkbookScan
from app.db.base import SessionLocal
from app.db.models import DataSource, ImportStaging, SourceSnapshot, TableImport, TableRecipe
from app.main import app

FLOW_RECIPE = json.loads((Path(__file__).parent / "fixtures" / "recipes" / "flow_recipe.json").read_text("utf-8"))
FLOW_FULL = Recipe.model_validate(FLOW_RECIPE).model_dump(mode="json")
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


# ==========================================================================
# 假 Pipeline
# ==========================================================================


def ok_check(cid: str, kind: str = "row_count", category: str = "structure") -> CheckResult:
    return CheckResult(id=cid, kind=kind, title=f"合成核对 {cid}", status="passed", category=category, checked=1)


QUESTIONS = [
    Question("q_placeholder:·", "「·」表示无数据吗",
             [{"value": "null", "label": "是，存为空值", "needs_reason": False},
              {"value": "reject", "label": "不是，不导入", "needs_reason": False}], None,
             {"null": [], "reject": [{"op": "remove", "path": "/sheets/0/blocks/0/values/placeholders/0"}]}),
    Question("q_relation:F1", "第 5 行 = 第 6 行 + 第 7 行：要登记为每期核对吗",
             [{"value": "register", "label": "登记", "needs_reason": False},
              {"value": "dismiss", "label": "不登记", "needs_reason": True}], None,
             {"register": [{"op": "replace", "path": "/relations/0", "value": FLOW_FULL["relations"][0]}],
              "dismiss": [{"op": "replace", "path": "/relations/0",
                           "value": {"id": "R1", "kind": "dismissed", "claims": "F1", "reason": REASON_SLOT}}]}),
]
FACTS = DraftFacts(candidates={"日间时段客流（人次）": ["日间", "日间时段客流"]})


@dataclass
class FakeState:
    complete: bool = True
    recipe_problems: list[RecipeProblem] = field(default_factory=list)
    draft_problems: list[Problem] = field(default_factory=list)
    trial_problems: list[Problem] = field(default_factory=list)
    needs_period: bool = False
    checks: list[CheckResult] = field(default_factory=lambda: [
        ok_check("C1", "context_agree", "data_quality"), ok_check("R1", "relation_sum_eq", "data_quality"),
        ok_check("N1")])
    notes_problems: list[str] = field(default_factory=list)
    #: 只在带着接受（提交时）生成说明才报的问题：试运行能过、提交时 notes_invalid
    notes_problems_on_accept: list[str] = field(default_factory=list)
    #: 说明里多写一张库里没有的表：apply_notes 写不进表结构
    notes_extra_table: bool = False
    diff: list[DiffItem] = field(default_factory=list)
    confirm_extra: list[ConfirmItem] = field(default_factory=list)
    availability: AiAvailability = field(default_factory=lambda: AiAvailability(True, model="假模型",
                                                                               provider="假接入"))
    compress_too_large: bool = False
    #: 规则起草连半成品都没有（Draft.recipe=None）：暂存区没有工作配方
    no_recipe: bool = False
    #: 测试所在事件循环的解析名额（execute 里记下当时还剩几个）
    slot: Any = None
    ai_result: Any = None
    ai_hook: Any = None
    rows: int = 2
    #: 让工作表大于网格上限：交互时只做部分干跑
    big_sheet: bool = False
    calls: dict[str, list[Any]] = field(default_factory=lambda: defaultdict(list))


def fake_scan(raw: bytes, state: FakeState) -> WorkbookScan:
    if raw.startswith(b"bad"):
        raise UnsupportedTable("无法读取这个文件：不是有效的 Excel 文件")
    return WorkbookScan(sheets=[
        SheetScan(name="甲表", state="visible", path="xl/worksheets/sheet1.xml", bounds=Bounds(1, 1, 3, 2),
                  nonempty=10 ** 6 if state.big_sheet else 4, row_runs=[(1, 3, 2)]),
        SheetScan(name="配置", state="hidden", path="xl/worksheets/sheet2.xml"),
    ])


def fake_grid(raw: bytes, scan: WorkbookScan, sheet: str, *, max_rows: int | None = None) -> Grid:
    return Grid(sheet=sheet, bounds=(1, 1, 3, 2), cells={
        (1, 1): GridCell("区域"), (1, 2): GridCell("数量"), (2, 1): GridCell("分区甲"), (2, 2): GridCell(1)})


def make_trial_db(path: str, rows: int, tag: str) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute('CREATE TABLE "t" ("n" INTEGER, "s" TEXT) STRICT')
        conn.executemany('INSERT INTO "t" VALUES (?, ?)', [(i, f"{tag}-{i}") for i in range(rows)])
        conn.commit()
    finally:
        conn.close()


def fake_pipeline(state: FakeState) -> Pipeline:
    def validate(data, *, facts=None, origin="manual", **kw):
        # 期 3：现行配方作为 base 传进来（只在有现行配方时传，P3-SPEC 12.0）
        state.calls["validate"].append({"origin": origin, "facts": facts, **kw})
        try:
            parsed = Recipe.model_validate(data)
        except Exception:  # noqa: BLE001
            return None, [RecipeProblem("/sheets", "schema", "第 1 个工作表：缺少必填项")]
        return parsed, list(state.recipe_problems)

    def execute(rec, raw, filename, scan, db_path, *, context_inputs=None, max_rows=None):
        state.calls["execute"].append({"db": db_path, "ctx": context_inputs, "max_rows": max_rows,
                                       "filename": filename, "thread": threading.get_ident(),
                                       "slots_free": state.slot._value if state.slot is not None else None})
        problems = list(state.trial_problems if db_path else state.draft_problems)
        period = PeriodOut("2026-08-01", "2026-08-31", "cells", cells=["甲表!A1"],
                           texts={"甲表!A1": "统计时间范围：2026年8月1日至2026年8月31日"})
        if state.needs_period:
            pi = (context_inputs or {}).get("统计期")
            if pi is not None:
                period = PeriodOut(pi.start.isoformat(), pi.end.isoformat(), "human", signed_by=pi.signed_by)
            else:
                period = None
                problems.append(Problem("period_missing", "input", "未能从表格中解析出统计期，请为本期录入统计期",
                                        model_message="未能解析出统计期"))
        blocking = any(p.category in ("structure", "input", "recipe") for p in problems)
        ex = Extraction(
            ok=not blocking, problems=problems, partial=max_rows is not None,
            tables=[TableOut("t", "甲表", "data", [ColumnOut("n", "INTEGER", role="measure"),
                                                   ColumnOut("s", "TEXT", role="text")], [], state.rows)],
            regions=[RegionMark("甲表", "value", "A2:B3")], period=period,
            context_checks=[ok_check("C1", "context_agree", "data_quality")], expected_rows={"t": state.rows},
            table_hashes={"t": "0" * 64},
        )
        if db_path and not blocking:
            make_trial_db(db_path, state.rows, hashlib.sha256(raw).hexdigest()[:8])
        return ex

    def run_checks(db_path, ex, rec):
        state.calls["run_checks"].append(db_path)
        assert Path(db_path).is_file()
        return copy.deepcopy(state.checks)

    def null_counts(db_path, rec):
        state.calls["null_counts"].append(db_path)
        return {"t": {"n": 0, "s": 0}}

    def build_notes(rec, ex, checks, acceptances, *, null_counts=None):
        state.calls["build_notes"].append({"acceptances": list(acceptances), "null_counts": null_counts})
        tables = {"t": TableNote(comment="合成说明", columns={"n": "单位：人次"})}
        if state.notes_extra_table:
            tables["缺表"] = TableNote(comment="合成说明")
        problems = list(state.notes_problems) + (list(state.notes_problems_on_accept) if acceptances else [])
        return SchemaNotes(tables=tables, templates={"t": "合成说明"}, problems=problems)

    def detect_facts(grids, scan, *, recipe=None):
        state.calls["detect_facts"].append(recipe)
        return copy.deepcopy(FACTS)

    def draft(scan, grids, filename, *, validate, dry_run):
        state.calls["draft"].append(filename)
        rec = copy.deepcopy(FLOW_FULL)
        parsed, problems = validate(rec, FACTS, "rules")
        if parsed is not None and not problems and dry_run is not None:
            dry_run(parsed)
        failures = [] if state.complete else ["第 9 行像合计行，但标签无法确定区间"]
        if state.no_recipe:
            rec = None
        return Draft(recipe=rec, complete=state.complete, origin="rules", cards=[Card("mode", "导入模式：每期替换", "合成")],
                     questions=copy.deepcopy(QUESTIONS), facts=copy.deepcopy(FACTS), failures=failures,
                     failures_for_model=["第 9 行像合计行"] if failures else [])

    def questions_for(rec, facts, grids, *, extraction=None):
        state.calls["questions_for"].append({"recipe": rec, "extraction": extraction})
        return [Card("mode", "导入模式：每期替换", "合成")], copy.deepcopy(QUESTIONS)

    async def ai_availability(session):
        state.calls["ai_availability"].append(True)
        return state.availability

    def compress(grids, scan, facts, *, mask_headers=frozenset()):
        state.calls["compress"].append(set(mask_headers))
        if state.compress_too_large:
            raise AiTooLarge("表格结构压缩后有 30000 字，超过上限 20000 字，无法交给 AI 起草")
        text = "## 工作表「甲表」 已用区域 A1:B3，非空格 4"
        return Compressed(text=text, chars=len(text), truncated=False,
                          sha256=hashlib.sha256(text.encode()).hexdigest())

    def first_call_text(compressed, *, grids, scan, rules_draft):
        tail = "；".join(rules_draft.failures_for_model) if rules_draft else "无"
        return "系统提示：只输出 JSON\n\n" + compressed.text + "\n\n# 单位词表\n人次\n\n# 规则起草的半成品\n" + tail

    async def ai_draft(session, *, grids, scan, facts, rules_draft, filename, validate, dry_run, compressed):
        state.calls["ai_draft"].append({"compressed": compressed, "rules": rules_draft})
        if state.ai_hook is not None:
            await state.ai_hook()
        if state.ai_result is not None:
            return state.ai_result
        rec = copy.deepcopy(FLOW_FULL)
        rec["other_visible_sheets"] = "reject"
        parsed, problems = validate(rec, facts, "ai")
        state.calls["ai_dry_is_coroutine"].append(inspect.iscoroutinefunction(dry_run))
        ext = await dry_run(parsed)
        d = Draft(recipe=rec, complete=True, origin="ai",
                  cards=[Card("ai_origin", "AI 起草：请逐项核对", "合成"), Card("mode", "导入模式：每期替换", "合成")],
                  questions=copy.deepcopy(QUESTIONS), facts=facts)
        state.calls["ai_dry"].append(ext)
        usage = [AiUsage("假模型", 120, 80, 0.0012, True, "2026-10-01T00:00:00+00:00", 1)]
        return AiDraftResult(d, usage, 1, None, compressed.chars, compressed.sha256)

    def diff_reports(prev, cur, *, prev_file_name, cur_file_name, recipe=None, recipe_changed=None, **kw):
        # 期 3：按期累积时多一个 accumulate=（只在有累积计划时传，P3-SPEC 12.0）
        state.calls["diff_reports"].append({"prev": prev, "cur": cur, "prev_file_name": prev_file_name,
                                            "cur_file_name": cur_file_name, "recipe": recipe,
                                            "recipe_changed": recipe_changed, **kw})
        return copy.deepcopy(state.diff)

    def confirm_items(ctx):
        state.calls["confirm_items"].append(ctx)
        items = [ConfirmItem("mode", "导入模式：每期替换")]
        if ctx.recipe_origin in ("ai", "mixed"):
            items.append(ConfirmItem("ai_origin", "本配方由 AI 起草，已逐项核对"))
        items += copy.deepcopy(state.confirm_extra)
        for d in ctx.diff or []:
            d = d if isinstance(d, dict) else dataclasses.asdict(d)
            if d.get("requires_confirm"):
                items.append(ConfirmItem(d["confirm_id"], d["label"], source="diff"))
        return items

    return Pipeline(
        validate=validate, recipe_sha256=recipe.recipe_sha256, canonical=recipe.canonical_recipe,
        apply_patch=recipe.apply_patch, scan=lambda raw: fake_scan(raw, state),
        scan_to_json=xlsx_scan.scan_to_json, scan_from_json=xlsx_scan.scan_from_json, read_grid=fake_grid,
        preview=xlsx_cells.preview, execute=execute, run_checks=run_checks, column_null_counts=null_counts,
        build_notes=build_notes, apply_notes=recipe_notes.apply_notes, detect_facts=detect_facts, draft=draft,
        questions_for=questions_for, ai_availability=ai_availability, ai_draft=ai_draft, compress=compress,
        first_call_text=first_call_text, diff_reports=diff_reports, confirm_items=confirm_items,
        breaking_changes=lambda old, new: {}, grid_max_cells=1000, draft_max_rows=500, units=("人次", "元"),
    )


@pytest.fixture(autouse=True)
def store(monkeypatch, tmp_path):
    """每个测试一个独立的数据目录：上传目录、原件存档、工件都在里面。"""
    data = tmp_path / "data"
    (data / "uploads").mkdir(parents=True)
    monkeypatch.setattr(settings, "data_dir", data)
    yield data


@pytest.fixture
def fake(monkeypatch) -> FakeState:
    state = FakeState()
    pipe = fake_pipeline(state)
    monkeypatch.setattr(recipe_imports, "pipeline", lambda: pipe)
    return state


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


def unique(prefix: str = "wp5c") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def raw_bytes() -> bytes:
    """内容独一份的「原件」：原件按内容共用，别的测试传过同样字节就会互相影响。"""
    return f"PK 合成原件 {uuid.uuid4().hex}".encode()


async def get(model, key):
    async with SessionLocal() as session:
        return await session.get(model, key)


async def api_stage(client, name: str, raw: bytes | None = None, filename: str = "月报导出_2026-08-01_2026-08-31.xlsx"):
    return await client.post("/api/datasources/imports/stage",
                             files={"file": (filename, raw if raw is not None else raw_bytes(), XLSX)},
                             data={"name": name})


async def staged(client, name: str | None = None, **kw) -> dict[str, Any]:
    resp = await api_stage(client, name or unique(), **kw)
    assert resp.status_code == 201, resp.text
    return resp.json()


async def trialed(client, sid: str, **body) -> dict[str, Any]:
    resp = await client.post(f"/api/datasources/imports/{sid}/trial", json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()


async def committed(client, st: dict[str, Any], **body) -> dict[str, Any]:
    body.setdefault("confirmations", [i["id"] for i in st["trial"]["confirm_items"]])
    resp = await client.post(f"/api/datasources/imports/{st['id']}/commit",
                             json={"trial_id": st["trial"]["trial_id"], **body})
    assert resp.status_code == 201, resp.text
    return resp.json()


async def recipe_source(client, name: str | None = None) -> dict[str, Any]:
    """用假管线建一个按配方导入的源：暂存 → 试运行 → 提交。返回提交的响应。"""
    st = await staged(client, name)
    st = await trialed(client, st["id"])
    return await committed(client, st)


# ==========================================================================
# 纯函数
# ==========================================================================


def test_replay_answers_choosing_a_then_b_equals_choosing_b():
    p = recipe_imports.default_pipeline()
    base = copy.deepcopy(FLOW_FULL)
    a = recipe_imports.replay_answers(p, base, QUESTIONS, {"q_relation:F1": {"value": "dismiss", "reason": "巧合"}})
    assert a["relations"][0] == {"id": "R1", "kind": "dismissed", "claims": "F1", "reason": "巧合"}
    b_direct = recipe_imports.replay_answers(p, base, QUESTIONS, {"q_relation:F1": {"value": "register"}})
    # 在 A 的结果上不会再叠一次：重放总是从起点开始
    b_after_a = recipe_imports.replay_answers(p, base, QUESTIONS, {"q_relation:F1": {"value": "register"}})
    assert b_direct == b_after_a == base
    both = recipe_imports.replay_answers(p, base, QUESTIONS, {"q_placeholder:·": {"value": "reject"},
                                                             "q_relation:F1": {"value": "register"}})
    assert both["sheets"][0]["blocks"][0]["values"]["placeholders"] == []
    assert base == FLOW_FULL, "起点配方不被修改"


def test_replay_answers_reports_effects_that_do_not_apply():
    p = recipe_imports.default_pipeline()
    broken = copy.deepcopy(FLOW_FULL)
    broken["relations"] = []
    with pytest.raises(ValueError):
        recipe_imports.replay_answers(p, broken, QUESTIONS, {"q_relation:F1": {"value": "register"}})


@pytest.mark.parametrize(("problems", "checks", "notes", "expected"), [
    ([], [ok_check("N1")], [], "passed"),
    ([Problem("label_missing", "structure", "合成")], [], [], "rejected"),
    ([], [], ["说明中含有数字「3」"], "rejected"),
    ([], [CheckResult("K1", "derived_sum", "合计", "mismatch", "structure")], [], "rejected"),
    ([], [CheckResult("K1", "derived_sum", "合计", "unverifiable", "structure", acceptable=True)], [], "needs_decision"),
    ([], [CheckResult("R1", "relation_sum_eq", "关系", "mismatch", "data_quality", acceptable=True)], [],
     "needs_decision"),
    ([Problem("period_missing", "input", "合成")], [], [], "needs_input"),
    ([Problem("outside_digits", "confirm", "合成")], [CheckResult("R2", "relation_not_comparable", "口径", "info", "info")],
     [], "passed"),
])
def test_trial_status(problems, checks, notes, expected):
    assert recipe_imports.trial_status(problems, checks, notes) == expected


def test_extraction_round_trip_through_json():
    from tests.fixtures.xlsx.flow import flow_workbook

    raw, fn = flow_workbook(dt.date(2026, 8, 1), 31, seed=5)
    scan = xlsx_scan.scan(raw)
    parsed = Recipe.model_validate(FLOW_RECIPE)
    from app.data import recipe_engine

    ex = recipe_engine.execute(parsed, raw, fn, scan, None)
    data = json.loads(json.dumps(dataclasses.asdict(ex), ensure_ascii=False, default=str))
    back = recipe_imports.extraction_from_dict(data)
    assert json.loads(json.dumps(dataclasses.asdict(back), ensure_ascii=False, default=str)) == data
    assert isinstance(back.period, PeriodOut) and isinstance(back.tables[0], TableOut)
    assert isinstance(back.tables[0].columns[0], ColumnOut)


async def test_first_call_text_is_exactly_what_ai_draft_sends_first():
    """同意框展示的全文 = ai_draft 第一次调用的系统提示 + 首次提示词（含单位词表、规则半成品、格式说明）。"""
    from langchain_core.messages import AIMessage

    from tests.fixtures.xlsx.lab import kv_form

    raw, fn = kv_form()
    scan = xlsx_scan.scan(raw)
    grids = {s.name: xlsx_cells.read_grid(raw, scan, s.name, max_rows=500) for s in scan.sheets
             if s.state == "visible" and s.nonempty}
    from app.data import recipe_suggest

    facts = recipe_suggest.detect_facts(grids, scan)
    rules = recipe_suggest.draft(scan, grids, fn,
                                 validate=lambda d, f, o: recipe.validate_recipe(d, facts=f, origin=o))
    compressed = recipe_ai.compress(grids, scan, facts)
    text = recipe_ai.first_call_text(compressed, grids=grids, scan=scan, rules_draft=rules)

    class Model:
        calls: list = []

        async def ainvoke(self, messages):
            self.calls.append(list(messages))
            return AIMessage(content="不是 JSON", usage_metadata={"input_tokens": 1, "output_tokens": 1,
                                                                 "total_tokens": 2})

    model = Model()
    result = await recipe_ai.ai_draft(None, grids=grids, scan=scan, facts=facts, rules_draft=rules, filename=fn,
                                      validate=lambda d, f, o: recipe.validate_recipe(d, facts=f, origin=o),
                                      dry_run=None, model=model, compressed=compressed)
    assert result.draft is None
    first = model.calls[0]
    assert text == first[0].content + "\n\n" + first[1].content
    assert compressed.text in text and "# 单位词表" in text and "# 配方 JSON Schema" in text
    assert "# 规则起草的半成品与失败原因" in text


def test_effective_origin():
    st = ImportStaging(recipe_origin="manual", drafts={"ai_adopted": {"draft": {"recipe": {}}}})
    assert recipe_imports.effective_origin(st, None) == "mixed"
    st = ImportStaging(recipe_origin="manual", drafts={"rules": {}})
    assert recipe_imports.effective_origin(st, None) == "manual"
    assert recipe_imports.effective_origin(st, "ai") == "mixed"
    st = ImportStaging(recipe_origin="rules", drafts={})
    assert recipe_imports.effective_origin(st, None) == "rules"


# ==========================================================================
# 暂存与回答
# ==========================================================================


async def test_stage_first_does_not_create_the_source_and_keeps_the_raw(client, fake, store):
    name = unique()
    st = await staged(client, name)
    assert st["kind"] == "first" and st["status"] == "drafting" and st["recipe_origin"] == "rules"
    assert st["source"] == {"id": st["source"]["id"], "name": name, "exists": False, "import_mode": None}
    assert st["skipped_sheets"] == [{"sheet": "配置", "state": "hidden"}]
    assert st["sheets"][0]["name"] == "甲表" and st["sheets"][0]["bounds"] == "A1:B3"
    assert st["grids"][0]["cells"] and st["units"] == ["人次", "元"]
    assert st["candidates"] == FACTS.candidates
    assert [q["id"] for q in st["questions"]] == [q.id for q in QUESTIONS]
    assert st["marks"] == [{"sheet": "甲表", "role": "value", "ref": "A2:B3", "block": None}]
    assert st["expires_at"] and st["file"]["sha256_prefix"]
    assert await get(DataSource, st["source"]["id"]) is None
    row = await get(ImportStaging, st["id"])
    assert (store / "uploads" / "raw" / row.raw_sha256[:2] / row.raw_sha256).is_file()
    assert row.answers_base == FLOW_FULL and row.recipe == recipe.canonical_recipe(Recipe.model_validate(FLOW_FULL))


async def test_answers_replay_from_base_and_reset_by_put(client, fake):
    st = await staged(client)
    sid = st["id"]
    resp = await client.post(f"/api/datasources/imports/{sid}/answers", json={"answers": {
        "q_relation:F1": {"value": "dismiss", "reason": "合成理由"}, "q_placeholder:·": {"value": "reject"}}})
    assert resp.status_code == 200, resp.text
    a = resp.json()
    assert a["recipe"]["relations"][0]["kind"] == "dismissed" and a["recipe"]["relations"][0]["reason"] == "合成理由"
    assert a["recipe"]["sheets"][0]["blocks"][0]["values"]["placeholders"] == []
    assert a["answers"]["q_relation:F1"] == {"value": "dismiss", "reason": "合成理由"}
    # 改选 B：等于直接选 B
    resp = await client.post(f"/api/datasources/imports/{sid}/answers", json={"answers": {
        "q_relation:F1": {"value": "register"}}})
    b = resp.json()
    assert b["recipe"] == FLOW_FULL and b["answers"] == {"q_relation:F1": {"value": "register", "reason": None}}
    # 每次回答前按工作配方取系统发现
    assert fake.calls["detect_facts"][-1] == FLOW_FULL
    # PUT 之后起点换掉。期 3（第 8 节遗留项 2）：新起点仍体现「登记」这个回答（R1 已在配方里），回答保留、
    # answers_dropped 为空，不再一律清空
    changed = copy.deepcopy(FLOW_FULL)
    changed["tables"][0]["note"] = "口径说明"
    resp = await client.put(f"/api/datasources/imports/{sid}/recipe", json={"recipe": changed})
    put = resp.json()
    assert put["answers"] == {"q_relation:F1": {"value": "register", "reason": None}}
    assert put["answers_dropped"] == [] and put["recipe"]["tables"][0]["note"] == "口径说明"
    assert put["recipe_origin"] == "manual"
    row = await get(ImportStaging, sid)
    assert row.answers_base["tables"][0]["note"] == "口径说明"


@pytest.mark.parametrize(("answers", "why"), [
    ({"q_nope": {"value": "x"}}, "问题不存在"),
    ({"q_relation:F1": {"value": "maybe"}}, "选项不存在"),
    ({"q_relation:F1": {"value": "dismiss"}}, "needs_reason 没给理由"),
    ({"q_relation:F1": {"value": "dismiss", "reason": "   "}}, "理由是空白"),
    ({"q_relation:F1": {"value": "dismiss", "reason": "长" * 201}}, "理由超过 200 字"),
    ({"q_relation:F1": "register"}, "回答不是对象"),
])
async def test_answer_invalid(client, fake, answers, why):
    st = await staged(client)
    resp = await client.post(f"/api/datasources/imports/{st['id']}/answers", json={"answers": answers})
    assert resp.status_code == 422, why
    assert resp.json()["code"] == "answer_invalid"


async def test_partial_dry_run_for_big_sheets(client, fake):
    fake.big_sheet = True
    st = await staged(client)
    assert st["draft_partial"] is True
    assert all(c["max_rows"] == 500 for c in fake.calls["execute"] if c["db"] is None)
    resp = await client.post(f"/api/datasources/imports/{st['id']}/answers",
                             json={"answers": {"q_relation:F1": {"value": "register"}}})
    assert resp.json()["draft_partial"] is True and fake.calls["execute"][-1]["max_rows"] == 500
    # 试运行一律完整执行
    st = await trialed(client, st["id"])
    assert fake.calls["execute"][-1]["max_rows"] is None and fake.calls["execute"][-1]["db"]


# ==========================================================================
# 试运行
# ==========================================================================


async def test_trial_stores_extraction_checks_and_null_counts(client, fake):
    st = await staged(client)
    st = await trialed(client, st["id"])
    trial = st["trial"]
    assert st["status"] == "trialed" and trial["status"] == "passed"
    assert trial["receipt"]["db_sha256"] and trial["receipt"]["canonicalized_total"] == 0
    assert set(recipe_imports.TRIAL_INTERNAL).isdisjoint(trial)
    assert trial["notes"] == {"t": {"comment": "合成说明", "columns": {"n": "单位：人次"}}}
    assert trial["confirm_items"] == [{"id": "mode", "label": "导入模式：每期替换", "detail": "", "required": True,
                                       "source": "recipe"}]
    assert trial["diff"] is None
    # 试运行预览的说明拿到了各列的空值数
    assert fake.calls["build_notes"][-1]["null_counts"] == {"t": {"n": 0, "s": 0}}
    assert fake.calls["null_counts"]
    row = await get(ImportStaging, st["id"])
    assert row.trial["extraction"]["tables"][0]["name"] == "t" and row.trial["null_counts"]
    assert Path(row.trial_path).is_file() and row.trial_path.endswith(f"{row.trial_key[:16]}.db")
    ctx = fake.calls["confirm_items"][-1]
    assert ctx.kind == "first" and ctx.prev is None and ctx.diff is None and ctx.old_recipe is None
    assert isinstance(ctx.extraction, dict) and "problems" in ctx.extraction


async def test_retrial_replaces_the_old_trial_db(client, fake):
    st = await staged(client)
    await trialed(client, st["id"])
    first = (await get(ImportStaging, st["id"])).trial_path
    resp = await client.put(f"/api/datasources/imports/{st['id']}/recipe",
                            json={"recipe": {**FLOW_FULL, "other_visible_sheets": "reject"}})
    assert resp.json()["status"] == "drafting"
    assert not Path(first).exists(), "配方变了：旧试运行库作废并删掉"
    await trialed(client, st["id"])
    second = (await get(ImportStaging, st["id"])).trial_path
    assert second != first and Path(second).is_file()


async def test_trial_rejected_keeps_no_db_and_commit_refuses(client, fake):
    fake.trial_problems = [Problem("label_missing", "structure", "分段「日间」少了标签「7-8」", fix="remove_label")]
    st = await staged(client)
    st = await trialed(client, st["id"])
    assert st["status"] == "rejected" and st["trial"]["status"] == "rejected"
    assert st["trial"]["problems"][0]["code"] == "label_missing"
    assert st["trial"]["confirm_items"] == [] and st["trial"]["diff"] is None
    assert (await get(ImportStaging, st["id"])).trial_path is None
    resp = await client.post(f"/api/datasources/imports/{st['id']}/commit",
                             json={"trial_id": st["trial"]["trial_id"], "confirmations": []})
    assert resp.status_code == 409 and resp.json()["code"] == "trial_not_passed"


async def test_notes_problems_reject_the_trial(client, fake):
    fake.notes_problems = ["表「t」的说明：说明中含有数字「3」"]
    st = await trialed(client, (await staged(client))["id"])
    assert st["trial"]["status"] == "rejected"
    assert any(p["code"] == "notes_invalid" and p["category"] == "recipe" for p in st["trial"]["problems"])


async def test_needs_input_then_human_period_changes_the_build(client, fake):
    fake.needs_period = True
    st = await staged(client)
    sid = st["id"]
    # 第一次试运行就给录入：上一次没有要求录入 → 422
    resp = await client.post(f"/api/datasources/imports/{sid}/trial",
                             json={"context_inputs": {"统计期": {"start": "2026-08-01", "end": "2026-08-31"}}})
    assert resp.status_code == 422 and resp.json()["code"] == "context_not_needed"
    st = await trialed(client, sid)
    assert st["trial"]["status"] == "needs_input" and st["status"] == "trialed"
    resp = await client.post(f"/api/datasources/imports/{sid}/commit",
                             json={"trial_id": st["trial"]["trial_id"], "confirmations": ["mode"]})
    assert resp.status_code == 409 and resp.json()["code"] == "trial_not_passed"
    for bad in ({"统计期": {"start": "2026-02-30", "end": "2026-03-01"}},
                {"统计期": {"start": "2026-09-30", "end": "2026-09-01"}}, {"别的": {"start": "2026-09-01"}}):
        resp = await client.post(f"/api/datasources/imports/{sid}/trial", json={"context_inputs": bad})
        assert resp.status_code == 422 and resp.json()["code"] == "context_invalid"
    st = await trialed(client, sid, context_inputs={"统计期": {"start": "2026-08-01", "end": "2026-08-31"}},
                       signed_by="录入员")
    assert st["trial"]["status"] == "passed"
    assert st["context_inputs"] == {"统计期": {"start": "2026-08-01", "end": "2026-08-31", "signed_by": "录入员"}}
    row = await get(ImportStaging, sid)
    assert row.trial["inputs"] == {"context": {"统计期": {"start": "2026-08-01", "end": "2026-08-31"}}}
    out = await committed(client, st)
    imp = await get(TableImport, out["import_id"])
    assert imp.context["source"] == "human" and imp.context["signed_by"] == "录入员"
    without = table_versions.recipe_build_id(out["source"]["id"], row.raw_sha256, row.recipe_sha256, {})
    assert out["build_id"] != without, "人工录入计入构建哈希"


# ==========================================================================
# 提交
# ==========================================================================


async def test_commit_writes_everything_in_one_go(client, fake):
    name = unique()
    st = await trialed(client, (await staged(client, name))["id"])
    out = await committed(client, st, signed_by="测试员")
    source = await get(DataSource, out["source"]["id"])
    assert source.name == name and source.origin == "upload" and source.current_recipe_id == out["recipe_id"]
    assert source.current_snapshot_id == out["snapshot_id"]
    snap = await get(SourceSnapshot, out["snapshot_id"])
    imp = await get(TableImport, out["import_id"])
    rec = await get(TableRecipe, out["recipe_id"])
    staging = await get(ImportStaging, st["id"])
    assert snap.schema_cache["import_manifests"] == [imp.manifest_artifact] and imp.manifest_artifact
    assert snap.schema_cache["tables"]["t"]["comment"] == "合成说明"
    assert imp.recipe_id == rec.id and imp.staging_id == st["id"] and imp.signed_by == "测试员"
    assert imp.checks[0]["id"] == "C1" and imp.overrides == [] and imp.waivers == []
    assert [c["id"] for c in imp.confirmations] == ["mode"]
    assert rec.origin == "rules" and rec.seq == 1 and rec.recipe == staging.recipe
    assert staging.status == "committed" and staging.committed_import_id == imp.id
    assert staging.scan is None and staging.trial is None and staging.trial_path is None
    from app.core import artifact_store

    manifest = artifact_store.load(imp.manifest_artifact)
    assert manifest["import_id"] == imp.id and manifest["snapshot_id"] == snap.id
    assert manifest["db_sha256"] == snap.db_sha256 and manifest["recipe"]["sha256"] == rec.recipe_sha256
    assert "problems" not in manifest["receipt"] and "axes" in manifest["receipt"]
    # 提交时说明拿到的是实际的接受（这里没有）和空值数
    assert fake.calls["build_notes"][-1] == {"acceptances": [], "null_counts": {"t": {"n": 0, "s": 0}}}


async def test_commit_requires_every_confirm_item(client, fake):
    fake.confirm_extra = [ConfirmItem("outside_digits:甲表!B9", "区域外有含数字的文字「注：9月15日…」",
                                      source="outside")]
    st = await trialed(client, (await staged(client))["id"])
    resp = await client.post(f"/api/datasources/imports/{st['id']}/commit",
                             json={"trial_id": st["trial"]["trial_id"], "confirmations": ["mode"]})
    assert resp.status_code == 422 and resp.json()["code"] == "confirm_required"
    assert "outside_digits:甲表!B9" in resp.json()["detail"]
    await committed(client, st)


async def test_commit_requires_reasons_and_records_overrides_and_waivers(client, fake):
    fake.checks = [ok_check("C1", "context_agree", "data_quality"),
                   CheckResult("R1", "relation_sum_eq", "全日客流 = 分区甲 + 分区乙", "mismatch", "data_quality",
                               checked=31, failed=1, acceptable=True),
                   CheckResult("K1", "derived_sum", "合计", "unverifiable", "structure", checked=93,
                               unverifiable=3, acceptable=True, reasons={"uncached": 3})]
    st = await trialed(client, (await staged(client))["id"])
    assert st["trial"]["status"] == "needs_decision" and st["trial"]["acceptable"] == ["R1", "K1"]
    url = f"/api/datasources/imports/{st['id']}/commit"
    base = {"trial_id": st["trial"]["trial_id"], "confirmations": ["mode"]}
    resp = await client.post(url, json=base)
    assert resp.status_code == 422 and resp.json()["code"] == "acceptance_required"
    resp = await client.post(url, json={**base, "acceptances": [{"check_id": "R1", "reason": "第 10 日为补录"},
                                                                {"check_id": "K1", "reason": "  "}]})
    assert resp.status_code == 422 and resp.json()["code"] == "acceptance_required" and "K1" in resp.json()["detail"]
    out = await committed(client, st, acceptances=[{"check_id": "R1", "reason": "第 10 日为补录"},
                                                   {"check_id": "K1", "reason": "原表未经计算保存"}],
                          signed_by="测试员")
    imp = await get(TableImport, out["import_id"])
    assert [(o["check_id"], o["reason"]) for o in imp.overrides] == [("R1", "第 10 日为补录")]
    assert [(w["check_id"], w["reason"]) for w in imp.waivers] == [("K1", "原表未经计算保存")]
    assert {a.check_id for a in fake.calls["build_notes"][-1]["acceptances"]} == {"R1", "K1"}


async def test_trial_key_invalidated_by_recipe_change(client, fake):
    st = await trialed(client, (await staged(client))["id"])
    old_trial = st["trial"]
    resp = await client.post(f"/api/datasources/imports/{st['id']}/answers",
                             json={"answers": {"q_placeholder:·": {"value": "reject"}}})
    after = resp.json()
    assert after["status"] == "drafting" and after["trial"]["trial_id"] == old_trial["trial_id"], "回执留着标「已失效」"
    resp = await client.post(f"/api/datasources/imports/{st['id']}/commit",
                             json={"trial_id": old_trial["trial_id"], "confirmations": ["mode"]})
    assert resp.status_code == 409 and resp.json()["code"] == "trial_required"
    # 选回原样：配方哈希回到试运行时的值，但试运行库已作废，仍要重新试运行
    await client.post(f"/api/datasources/imports/{st['id']}/answers", json={"answers": {}})
    resp = await client.post(f"/api/datasources/imports/{st['id']}/commit",
                             json={"trial_id": old_trial["trial_id"], "confirmations": ["mode"]})
    assert resp.status_code == 409 and resp.json()["code"] == "trial_required"


async def test_commit_with_wrong_trial_id_or_missing_db(client, fake):
    st = await trialed(client, (await staged(client))["id"])
    url = f"/api/datasources/imports/{st['id']}/commit"
    resp = await client.post(url, json={"trial_id": "0" * 16, "confirmations": ["mode"]})
    assert resp.status_code == 409 and resp.json()["code"] == "trial_required"
    row = await get(ImportStaging, st["id"])
    Path(row.trial_path).unlink()
    resp = await client.post(url, json={"trial_id": st["trial"]["trial_id"], "confirmations": ["mode"]})
    assert resp.status_code == 409 and resp.json()["code"] == "trial_required"
    row = await get(ImportStaging, st["id"])
    assert row.status == "drafting" and row.trial_path is None and row.trial_key is None and row.trial


async def test_failed_publish_leaves_pointer_and_returns_staging_to_drafting(client, fake, monkeypatch):
    done = await recipe_source(client)
    source_id = done["source"]["id"]
    resp = await client.post(f"/api/datasources/{source_id}/reupload",
                             files={"file": ("下月.xlsx", raw_bytes(), XLSX)})
    st = resp.json()
    assert st["trial"]["status"] == "passed"

    def boom(cache, report, sid):
        raise table_versions.PublishError("导入失败：新数据的表结构与解析结果对不上，已停止发布，当前版本未受影响")

    monkeypatch.setattr(table_versions, "check_probe", boom)
    resp = await client.post(f"/api/datasources/imports/{st['id']}/commit",
                             json={"trial_id": st["trial"]["trial_id"], "confirmations": ["mode"]})
    assert resp.status_code == 500 and resp.json()["code"] == "publish_failed"
    source = await get(DataSource, source_id)
    assert source.current_snapshot_id == done["snapshot_id"]
    row = await get(ImportStaging, st["id"])
    assert row.status == "drafting" and row.trial_path is None and row.trial_key is None and row.trial
    async with SessionLocal() as session:
        n = len(list((await session.execute(select(TableImport).where(TableImport.source_id == source_id))).scalars()))
    assert n == 1


async def test_failed_first_commit_does_not_create_the_source(client, fake, monkeypatch):
    st = await trialed(client, (await staged(client))["id"])

    def boom(cache, report, sid):
        raise table_versions.PublishError("导入失败：合成的探查错误，当前版本未受影响")

    monkeypatch.setattr(table_versions, "check_probe", boom)
    resp = await client.post(f"/api/datasources/imports/{st['id']}/commit",
                             json={"trial_id": st["trial"]["trial_id"], "confirmations": ["mode"]})
    assert resp.status_code == 500
    assert await get(DataSource, st["source"]["id"]) is None


async def test_tampered_trial_db_is_refused(client, fake):
    st = await trialed(client, (await staged(client))["id"])
    row = await get(ImportStaging, st["id"])
    conn = sqlite3.connect(row.trial_path)
    conn.execute('INSERT INTO "t" VALUES (999, "改过")')
    conn.commit()
    conn.close()
    resp = await client.post(f"/api/datasources/imports/{st['id']}/commit",
                             json={"trial_id": st["trial"]["trial_id"], "confirmations": ["mode"]})
    assert resp.status_code == 409 and resp.json()["code"] == "trial_tampered"
    assert (await get(ImportStaging, st["id"])).status == "drafting"


async def test_base_changed_is_judged_inside_the_lock(client, fake):
    """两个上传新一期都试运行通过；两个提交都过了锁外预检、在锁上排队，先拿到锁的 201，另一个 409。"""
    done = await recipe_source(client)
    source_id = done["source"]["id"]
    a = (await client.post(f"/api/datasources/{source_id}/reupload",
                           files={"file": ("甲.xlsx", raw_bytes(), XLSX)})).json()
    b = (await client.post(f"/api/datasources/{source_id}/reupload",
                           files={"file": ("乙.xlsx", raw_bytes(), XLSX)})).json()
    assert a["trial"]["status"] == b["trial"]["status"] == "passed"

    async def commit(st):
        return await client.post(f"/api/datasources/imports/{st['id']}/commit",
                                 json={"trial_id": st["trial"]["trial_id"], "confirmations": ["mode"]})

    lock = table_versions.store_lock()
    await lock.acquire()
    try:
        tasks = [asyncio.create_task(commit(a)), asyncio.create_task(commit(b))]
        # 等两个提交都排到锁上（预检都已通过）
        for _ in range(200):
            await asyncio.sleep(0.01)
            if len(getattr(lock, "_waiters", None) or []) >= 2:
                break
        assert len(lock._waiters) >= 2
    finally:
        lock.release()
    results = await asyncio.gather(*tasks)
    codes = sorted(r.status_code for r in results)
    assert codes == [201, 409], [r.text for r in results]
    loser = next(r for r in results if r.status_code == 409)
    assert loser.json()["code"] == "base_changed"
    winner = next(r for r in results if r.status_code == 201).json()
    assert (await get(DataSource, source_id)).current_snapshot_id == winner["snapshot_id"]


async def test_same_new_name_committed_concurrently(client, fake):
    name = unique()
    a = await trialed(client, (await staged(client, name))["id"])
    b = await trialed(client, (await staged(client, name))["id"])
    assert a["source"]["id"] != b["source"]["id"]

    async def commit(st):
        return await client.post(f"/api/datasources/imports/{st['id']}/commit",
                                 json={"trial_id": st["trial"]["trial_id"], "confirmations": ["mode"]})

    lock = table_versions.store_lock()
    await lock.acquire()
    try:
        tasks = [asyncio.create_task(commit(a)), asyncio.create_task(commit(b))]
        for _ in range(200):
            await asyncio.sleep(0.01)
            if len(getattr(lock, "_waiters", None) or []) >= 2:
                break
    finally:
        lock.release()
    results = await asyncio.gather(*tasks)
    assert sorted(r.status_code for r in results) == [201, 409], [r.text for r in results]
    assert next(r for r in results if r.status_code == 409).json()["code"] == "name_taken"
    async with SessionLocal() as session:
        rows = list((await session.execute(select(DataSource).where(DataSource.name == name))).scalars())
    assert len(rows) == 1


async def _wait_on_lock(lock: asyncio.Lock, n: int) -> None:
    """等 n 个协程排到版本存储锁上（它们都已过了锁外预检）。"""
    for _ in range(300):
        await asyncio.sleep(0.01)
        if len(getattr(lock, "_waiters", None) or []) >= n:
            return


async def test_name_taken_is_judged_inside_the_lock(client, fake, monkeypatch):
    """锁外预检之后、进锁之前别处建了同名源：锁内的检查拦下（CommitConflict），不靠 UNIQUE 约束兜底。"""
    name = unique()
    st = await trialed(client, (await staged(client, name))["id"])
    seen: list[BaseException] = []
    real = table_versions.publish_build

    async def spy(*a, **kw):
        try:
            return await real(*a, **kw)
        except BaseException as e:
            seen.append(e)
            raise

    monkeypatch.setattr(table_versions, "publish_build", spy)
    lock = table_versions.store_lock()
    await lock.acquire()
    try:
        task = asyncio.create_task(client.post(f"/api/datasources/imports/{st['id']}/commit",
                                               json={"trial_id": st["trial"]["trial_id"], "confirmations": ["mode"]}))
        await _wait_on_lock(lock, 1)
        assert len(lock._waiters) >= 1
        async with SessionLocal() as session:
            session.add(DataSource(name=name, kind="sqlite", database="/tmp/合成.db"))
            await session.commit()
    finally:
        lock.release()
    resp = await task
    assert resp.status_code == 409 and resp.json()["code"] == "name_taken", resp.text
    assert [type(e) for e in seen] == [recipe_imports.CommitConflict], seen
    assert await get(DataSource, st["source"]["id"]) is None
    assert (await get(ImportStaging, st["id"])).status == "drafting"


async def test_name_taken_falls_back_on_the_unique_constraint(client, fake, monkeypatch):
    """锁内检查之后才冒出来的同名源（这里在 before_commit 之后、同一事务里插一条）：UNIQUE 约束的
    IntegrityError 兜底转成 409 name_taken，不是 500。"""
    name = unique()
    st = await trialed(client, (await staged(client, name))["id"])
    real = table_versions.publish_build

    async def sneaky(session, source, **kw):
        inner = kw["before_commit"]

        async def before_commit(imp_id, snap_id):
            await inner(imp_id, snap_id)
            session.add(DataSource(name=name, kind="sqlite", database="/tmp/合成.db"))

        return await real(session, source, **{**kw, "before_commit": before_commit})

    monkeypatch.setattr(table_versions, "publish_build", sneaky)
    resp = await client.post(f"/api/datasources/imports/{st['id']}/commit",
                             json={"trial_id": st["trial"]["trial_id"], "confirmations": ["mode"]})
    assert resp.status_code == 409 and resp.json()["code"] == "name_taken", resp.text
    assert await get(DataSource, st["source"]["id"]) is None
    async with SessionLocal() as session:
        assert (await session.execute(select(DataSource).where(DataSource.name == name))).first() is None
    assert (await get(ImportStaging, st["id"])).status == "drafting"


async def test_commit_after_putting_a_schema_broken_recipe_is_trial_required(client, fake):
    """试运行后 PUT 一份 schema 不过的配方（照存、试运行作废但回执留着），拿旧的 trial_id 提交：
    409 trial_required，不是 pydantic 英文原文的 400。"""
    st = await trialed(client, (await staged(client))["id"])
    sid = st["id"]
    url = f"/api/datasources/imports/{sid}/commit"
    body = {"trial_id": st["trial"]["trial_id"], "confirmations": ["mode"]}
    resp = await client.put(f"/api/datasources/imports/{sid}/recipe",
                            json={"recipe": {"recipe_format": "agentlab-recipe/2", "tables": []}})
    assert resp.status_code == 200 and resp.json()["status"] == "drafting"
    assert resp.json()["trial"]["trial_id"] == st["trial"]["trial_id"], "回执留着标「已失效」"
    resp = await client.post(url, json=body)
    assert resp.status_code == 409 and resp.json()["code"] == "trial_required", resp.text
    # 键还在、存下的配方却读不成 Recipe（记录被改坏）：同样 409 trial_required
    await client.put(f"/api/datasources/imports/{sid}/recipe", json={"recipe": FLOW_FULL})
    st = await trialed(client, sid)
    async with SessionLocal() as session:
        row = await session.get(ImportStaging, sid)
        row.recipe = {"recipe_format": "agentlab-recipe/2", "tables": []}
        await session.commit()
    resp = await client.post(url, json={**body, "trial_id": st["trial"]["trial_id"]})
    assert resp.status_code == 409 and resp.json()["code"] == "trial_required", resp.text
    assert "validation error" not in resp.json()["detail"]


@pytest.mark.parametrize("change", ["parser_ver", "engine_ver", "file_name"])
async def test_commit_recomputes_the_trial_key(client, fake, monkeypatch, change):
    """提交时按当前的解析器、执行器版本和文件名重算 trial_key，不信存下的键。"""
    st = await trialed(client, (await staged(client))["id"])
    if change == "parser_ver":
        monkeypatch.setattr(recipe_parsers, "PARSER_VER", recipe_parsers.PARSER_VER + "-next")
    elif change == "engine_ver":
        monkeypatch.setattr(recipe_types, "RECIPE_ENGINE_VER", recipe_types.RECIPE_ENGINE_VER + "-next")
    else:
        async with SessionLocal() as session:
            row = await session.get(ImportStaging, st["id"])
            row.file_name = "改过名字的月报.xlsx"
            await session.commit()
    resp = await client.post(f"/api/datasources/imports/{st['id']}/commit",
                             json={"trial_id": st["trial"]["trial_id"], "confirmations": ["mode"]})
    assert resp.status_code == 409 and resp.json()["code"] == "trial_required", resp.text


async def test_commit_notes_invalid_keeps_the_trial_and_the_pointer(client, fake):
    """7.8 第 3 步：试运行通过，提交时带上接受理由生成的说明有问题 → 422 notes_invalid；指针不动，
    暂存区仍是 trialed、试运行库还在（没有进发布流程）。"""
    done = await recipe_source(client)
    source_id = done["source"]["id"]
    fake.checks = [ok_check("C1", "context_agree", "data_quality"),
                   CheckResult("R1", "relation_sum_eq", "全日客流 = 分区甲 + 分区乙", "mismatch", "data_quality",
                               checked=31, failed=1, acceptable=True)]
    fake.notes_problems_on_accept = ["表「t」的说明：说明中含有数字「10」"]
    st = (await client.post(f"/api/datasources/{source_id}/reupload",
                            files={"file": ("下月.xlsx", raw_bytes(), XLSX)})).json()
    assert st["trial"]["status"] == "needs_decision"
    resp = await client.post(f"/api/datasources/imports/{st['id']}/commit", json={
        "trial_id": st["trial"]["trial_id"], "confirmations": [i["id"] for i in st["trial"]["confirm_items"]],
        "acceptances": [{"check_id": "R1", "reason": "当日为补录"}]})
    assert resp.status_code == 422 and resp.json()["code"] == "notes_invalid", resp.text
    assert "说明中含有数字" in resp.json()["detail"]
    assert (await get(DataSource, source_id)).current_snapshot_id == done["snapshot_id"]
    row = await get(ImportStaging, st["id"])
    assert row.status == "trialed" and row.trial_key and Path(row.trial_path).is_file()
    async with SessionLocal() as session:
        n = len(list((await session.execute(select(TableImport).where(TableImport.source_id == source_id))).scalars()))
    assert n == 1


async def test_notes_that_do_not_fit_the_schema_fail_the_publish(client, fake):
    """说明写不进表结构（apply_notes 记了问题）→ 发布中止：500 publish_failed，指针不动，暂存区退回 drafting。"""
    done = await recipe_source(client)
    source_id = done["source"]["id"]
    fake.notes_extra_table = True
    st = (await client.post(f"/api/datasources/{source_id}/reupload",
                            files={"file": ("下月.xlsx", raw_bytes(), XLSX)})).json()
    assert st["trial"]["status"] == "passed"
    resp = await client.post(f"/api/datasources/imports/{st['id']}/commit",
                             json={"trial_id": st["trial"]["trial_id"], "confirmations": ["mode"]})
    assert resp.status_code == 500 and resp.json()["code"] == "publish_failed", resp.text
    assert "缺表" in resp.json()["detail"]
    assert (await get(DataSource, source_id)).current_snapshot_id == done["snapshot_id"]
    row = await get(ImportStaging, st["id"])
    assert row.status == "drafting" and row.trial_path is None and row.trial


async def test_same_as_import_rechecks_base_inside_the_lock(client, fake):
    """同一文件再传（same_as_import）和另一期的提交排在锁上，另一期先拿到锁、切走了指针：same_as_import
    那次在锁内重读指针 → 409 base_changed，不返回已被替换的那期导入。"""
    raw = raw_bytes()
    first = await committed(client, await trialed(client, (await staged(client, raw=raw))["id"]))
    source_id = first["source"]["id"]
    same = (await client.post(f"/api/datasources/{source_id}/reupload",
                              files={"file": ("月报导出_2026-08-01_2026-08-31.xlsx", raw, XLSX)})).json()
    other = (await client.post(f"/api/datasources/{source_id}/reupload",
                               files={"file": ("下月.xlsx", raw_bytes(), XLSX)})).json()
    assert same["trial"]["same_as_import"]["id"] == first["import_id"]
    assert other["trial"]["status"] == "passed" and other["trial"]["same_as_import"] is None

    async def commit(st):
        return await client.post(f"/api/datasources/imports/{st['id']}/commit",
                                 json={"trial_id": st["trial"]["trial_id"], "confirmations": ["mode"]})

    lock = table_versions.store_lock()
    await lock.acquire()
    try:
        t_other = asyncio.create_task(commit(other))
        await _wait_on_lock(lock, 1)
        t_same = asyncio.create_task(commit(same))
        await _wait_on_lock(lock, 2)
        assert len(lock._waiters) >= 2, "same_as_import 的提交也要排到锁上"
    finally:
        lock.release()
    r_other, r_same = await asyncio.gather(t_other, t_same)
    assert r_other.status_code == 201, r_other.text
    assert r_same.status_code == 409 and r_same.json()["code"] == "base_changed", r_same.text
    assert (await get(DataSource, source_id)).current_snapshot_id == r_other.json()["snapshot_id"]
    row = await get(ImportStaging, same["id"])
    assert row.status == "trialed" and row.committed_import_id is None


# ==========================================================================
# 上传新一期、修改配方
# ==========================================================================


async def test_reupload_replays_without_claims_and_diff_gets_the_full_receipt(client, fake):
    fake.diff = [DiffItem("period", "统计期：2026-08-01 至 2026-08-31 → 2026-09-01 至 2026-09-30"),
                 DiffItem("label_writing", "分段「日间」的标签写法变了", requires_confirm=True,
                          confirm_id="diff:label_writing:日间")]
    done = await recipe_source(client)
    source_id = done["source"]["id"]
    fake.calls.clear()
    resp = await client.post(f"/api/datasources/{source_id}/reupload",
                             files={"file": ("月报导出_2026-09-01_2026-09-30.xlsx", raw_bytes(), XLSX)})
    assert resp.status_code == 201, resp.text
    st = resp.json()
    assert st["kind"] == "reupload" and st["status"] == "trialed"
    # 现行配方原样重放：静态校验按 replay、不带系统发现；系统发现按现行配方的写法算过、存下
    assert all(v["origin"] == "replay" and v["facts"] is None for v in fake.calls["validate"])
    assert fake.calls["detect_facts"][0] == FLOW_FULL
    call = fake.calls["diff_reports"][-1]
    assert call["recipe_changed"] is False and isinstance(call["recipe"], Recipe)
    assert call["prev"]["format"] == recipe_imports.MANIFEST_FORMAT, "上一期取现行导入的导入清单"
    assert call["prev_file_name"] == "月报导出_2026-08-01_2026-08-31.xlsx"
    assert {"axes", "tables", "ledger", "period", "lineage", "canonicalized"} <= set(call["cur"]["receipt"])
    assert "problems" not in call["cur"]["receipt"] and call["cur"]["recipe_sha256"]
    ctx = fake.calls["confirm_items"][-1]
    assert ctx.kind == "reupload" and ctx.old_recipe is not None and ctx.prev is call["prev"]
    assert [d.kind for d in ctx.diff] == ["period", "label_writing"]
    assert [i["id"] for i in st["trial"]["confirm_items"]] == ["mode", "diff:label_writing:日间"]
    # 在上传新一期的暂存区里改配方：之后的静态校验带系统发现
    fake.calls.clear()
    resp = await client.put(f"/api/datasources/imports/{st['id']}/recipe",
                            json={"recipe": {**FLOW_FULL, "other_visible_sheets": "reject"}})
    assert resp.status_code == 200
    assert fake.calls["validate"][-1]["facts"] is not None and fake.calls["validate"][-1]["origin"] == "manual"


async def test_redraft_uses_the_current_raw_and_recipe(client, fake):
    done = await recipe_source(client)
    source_id = done["source"]["id"]
    fake.calls.clear()
    resp = await client.post(f"/api/datasources/{source_id}/redraft", json={})
    assert resp.status_code == 201, resp.text
    st = resp.json()
    assert st["kind"] == "redraft" and st["status"] == "drafting" and st["recipe"] == FLOW_FULL
    assert st["ai"]["offered"] is False and st["draft"] is None
    assert fake.calls["detect_facts"][0] == FLOW_FULL
    assert fake.calls["validate"][-1]["facts"] is not None, "修改配方：静态校验带系统发现"
    imp = await get(TableImport, done["import_id"])
    row = await get(ImportStaging, st["id"])
    assert row.raw_sha256 == imp.raw_sha256 and row.base_recipe_id == done["recipe_id"]
    # 改了配方再提交：新配方 seq=2，旧的 superseded
    resp = await client.put(f"/api/datasources/imports/{st['id']}/recipe",
                            json={"recipe": {**FLOW_FULL, "other_visible_sheets": "reject"}})
    st = await trialed(client, st["id"])
    assert fake.calls["diff_reports"][-1]["recipe_changed"] is True
    out = await committed(client, st)
    assert out["recipe_id"] != done["recipe_id"]
    old, new = await get(TableRecipe, done["recipe_id"]), await get(TableRecipe, out["recipe_id"])
    assert old.status == "superseded" and new.status == "active" and new.seq == 2 and new.origin == "manual"


async def test_switch_from_simple_upload(client, fake):
    from tests.test_table_versions import V1, xlsx

    name = unique()
    resp = await client.post("/api/datasources/upload", files={"file": ("客流.xlsx", xlsx(V1), XLSX)},
                             data={"name": name})
    assert resp.status_code == 201, resp.text
    old = resp.json()
    assert old["source"]["import_mode"] == "simple" and old["source"]["current_recipe"] is None
    st = await staged(client, name)
    assert st["kind"] == "switch" and st["source"]["id"] == old["source"]["id"]
    assert st["source"]["exists"] is True and st["source"]["import_mode"] == "simple"
    st = await trialed(client, st["id"])
    call = fake.calls["diff_reports"][-1]
    assert "ledger" not in call["prev"]["receipt"] and call["prev"]["receipt"]["tables"]
    ctx = fake.calls["confirm_items"][-1]
    assert ctx.kind == "switch" and ctx.old_schema_cache and ctx.old_recipe is None
    out = await committed(client, st)
    assert out["source"]["id"] == old["source"]["id"] and out["source"]["import_mode"] == "recipe"


async def test_redraft_without_raw_and_non_recipe_sources(client, fake):
    from tests.test_table_versions import V1, xlsx

    name = unique()
    resp = await client.post("/api/datasources/upload", files={"file": ("客流.xlsx", xlsx(V1), XLSX)},
                             data={"name": name})
    simple_id = resp.json()["source"]["id"]
    for path in (f"/api/datasources/{simple_id}/redraft", f"/api/datasources/{simple_id}/reupload"):
        kw = {"json": {}} if path.endswith("redraft") else {"files": {"file": ("x.xlsx", raw_bytes(), XLSX)}}
        resp = await client.post(path, **kw)
        assert resp.status_code == 409 and resp.json()["code"] == "not_recipe_source", path
    resp = await client.get(f"/api/datasources/{simple_id}/recipe")
    assert resp.status_code == 409 and resp.json()["code"] == "not_recipe_source"
    for path in ("/api/datasources/nope/redraft", "/api/datasources/nope/recipe"):
        resp = await (client.post(path, json={}) if path.endswith("redraft") else client.get(path))
        assert resp.status_code == 404, path

    done = await recipe_source(client)
    source_id = done["source"]["id"]
    resp = await client.post(f"/api/datasources/{source_id}/imports/{done['import_id']}/purge-raw",
                             json={"reason": "合成理由：含敏感信息"})
    assert resp.status_code == 200, resp.text
    resp = await client.post(f"/api/datasources/{source_id}/redraft", json={})
    assert resp.status_code == 409 and resp.json()["code"] == "raw_missing"


async def test_purge_raw_discards_open_stagings_on_the_same_raw(client, fake):
    raw = raw_bytes()
    st = await trialed(client, (await staged(client, raw=raw))["id"])
    out = await committed(client, st)
    source_id = out["source"]["id"]
    resp = await client.post(f"/api/datasources/{source_id}/reupload", files={"file": ("同一份.xlsx", raw, XLSX)})
    again = resp.json()
    trial_path = (await get(ImportStaging, again["id"])).trial_path
    assert trial_path and Path(trial_path).is_file()
    resp = await client.post(f"/api/datasources/{source_id}/imports/{out['import_id']}/purge-raw",
                             json={"reason": "合成理由"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["discarded_stagings"] == [again["id"]]
    row = await get(ImportStaging, again["id"])
    assert row.status == "discarded" and row.grid_preview is None and row.trial is None
    assert not Path(trial_path).exists()


async def test_commit_recomputes_required_items_from_the_stored_diff(client, fake):
    """提交时按试运行存下的差异卡重算必勾项：差异卡里需确认的那一项不勾 → 422，detail 里有它。"""
    done = await recipe_source(client)
    fake.diff = [DiffItem("label_writing", "分段「日间」的标签写法变了", requires_confirm=True,
                          confirm_id="diff:label_writing:日间")]
    st = (await client.post(f"/api/datasources/{done['source']['id']}/reupload",
                            files={"file": ("下月.xlsx", raw_bytes(), XLSX)})).json()
    assert [i["id"] for i in st["trial"]["confirm_items"]] == ["mode", "diff:label_writing:日间"]
    fake.diff = []
    n = len(fake.calls["diff_reports"])
    resp = await client.post(f"/api/datasources/imports/{st['id']}/commit",
                             json={"trial_id": st["trial"]["trial_id"], "confirmations": ["mode"]})
    assert resp.status_code == 422 and resp.json()["code"] == "confirm_required", resp.text
    assert "diff:label_writing:日间" in resp.json()["detail"]
    assert len(fake.calls["diff_reports"]) == n, "提交时不重算差异卡，用存下的那份"
    await committed(client, st)


@pytest.mark.parametrize("where", ["execute", "confirm_items"])
async def test_reupload_whose_auto_trial_fails_is_discarded(client, fake, store, monkeypatch, where):
    """上传新一期的自动试运行出错：接口回错误，刚建的暂存区放弃（瘦身）、原件和试运行库都不留。"""
    done = await recipe_source(client)
    source_id = done["source"]["id"]
    pipe = recipe_imports.pipeline()
    if where == "execute":
        def broken_execute(rec, raw, filename, scan, db_path, **kw):
            raise UnsupportedTable("工作表「甲表」读取失败：文件已损坏")
        monkeypatch.setattr(pipe, "execute", broken_execute)
    else:
        def broken_confirm(ctx):
            raise ValueError("合成的调用方错误")
        monkeypatch.setattr(pipe, "confirm_items", broken_confirm)
    raw = raw_bytes()
    resp = await client.post(f"/api/datasources/{source_id}/reupload", files={"file": ("下月.xlsx", raw, XLSX)})
    assert resp.status_code == 400, resp.text
    if where == "execute":
        assert resp.json()["code"] == "file_unreadable"
    async with SessionLocal() as session:
        rows = list((await session.execute(select(ImportStaging).where(
            ImportStaging.source_id == source_id, ImportStaging.kind == "reupload"))).scalars())
    assert [r.status for r in rows] == ["discarded"] and rows[0].scan is None
    sha = hashlib.sha256(raw).hexdigest()
    assert not (store / "uploads" / "raw" / sha[:2] / sha).exists()
    trials = table_versions.tables_root() / source_id / table_versions.TRIALS_DIR
    assert not list(trials.glob(f"{rows[0].id}-*")), "这次新写的试运行库也删掉"
    listing = {s["id"]: s for s in (await client.get("/api/datasources")).json()}
    assert listing[source_id]["open_staging"] is None
    assert (await get(DataSource, source_id)).current_snapshot_id == done["snapshot_id"]


# ==========================================================================
# 放弃、过期
# ==========================================================================


async def test_discard_slims_and_releases_the_raw(client, fake, store):
    st = await trialed(client, (await staged(client))["id"])
    row = await get(ImportStaging, st["id"])
    raw_path = store / "uploads" / "raw" / row.raw_sha256[:2] / row.raw_sha256
    trial = Path(row.trial_path)
    assert raw_path.is_file() and trial.is_file()
    resp = await client.delete(f"/api/datasources/imports/{st['id']}")
    assert resp.status_code == 204
    row = await get(ImportStaging, st["id"])
    assert row.status == "discarded" and row.closed_at and row.scan is None and row.trial is None
    assert not raw_path.exists() and not trial.exists()
    resp = await client.delete(f"/api/datasources/imports/{st['id']}")
    assert resp.status_code == 409 and resp.json()["code"] == "staging_closed"
    for method, path, kw in (
        ("post", "answers", {"json": {"answers": {}}}), ("put", "recipe", {"json": {"recipe": FLOW_FULL}}),
        ("post", "trial", {"json": {}}), ("post", "commit", {"json": {"trial_id": "x"}}),
        ("get", "draft-ai/preview", {}), ("post", "draft-ai", {"json": {"consent": True}}),
    ):
        resp = await getattr(client, method)(f"/api/datasources/imports/{st['id']}/{path}", **kw)
        assert resp.status_code == 409 and resp.json()["code"] == "staging_closed", path


async def test_expiry_happens_on_get(client, fake):
    st = await trialed(client, (await staged(client))["id"])
    async with SessionLocal() as session:
        row = await session.get(ImportStaging, st["id"])
        trial = Path(row.trial_path)
        row.expires_at = row.created_at - dt.timedelta(days=1)
        await session.commit()
    resp = await client.get(f"/api/datasources/imports/{st['id']}")
    out = resp.json()
    assert out["status"] == "expired" and out["grids"] == [] and out["trial"] is None
    assert not trial.exists()


# ==========================================================================
# AI 起草（P2-SPEC 9.6 的 WP-5c 部分）
# ==========================================================================


async def test_ai_gates(client, fake):
    # 规则草稿完整 → 不需要
    st = await staged(client)
    assert st["ai"]["offered"] is False
    resp = await client.get(f"/api/datasources/imports/{st['id']}/draft-ai/preview")
    assert resp.status_code == 409 and resp.json()["code"] == "ai_not_needed"
    resp = await client.post(f"/api/datasources/imports/{st['id']}/draft-ai", json={"consent": True})
    assert resp.status_code == 409 and resp.json()["code"] == "ai_not_needed"
    # 上传新一期 → 不提供
    done = await recipe_source(client)
    re_ = (await client.post(f"/api/datasources/{done['source']['id']}/reupload",
                             files={"file": ("x.xlsx", raw_bytes(), XLSX)})).json()
    resp = await client.post(f"/api/datasources/imports/{re_['id']}/draft-ai", json={"consent": True})
    assert resp.status_code == 409 and resp.json()["code"] == "ai_not_offered"
    # 规则不完整、接入不可用 → 409，桩没被调
    fake.complete = False
    fake.availability = AiAvailability(False, reason="未配置模型接入，无法使用 AI 起草。请到「设置 → 模型接入」添加")
    st = await staged(client)
    assert st["ai"] == {"offered": True, "available": False, "reason": fake.availability.reason, "model": "",
                        "provider": ""}
    resp = await client.get(f"/api/datasources/imports/{st['id']}/draft-ai/preview")
    assert resp.status_code == 409 and resp.json()["code"] == "ai_unavailable"
    resp = await client.post(f"/api/datasources/imports/{st['id']}/draft-ai", json={"consent": True,
                                                                                  "preview_sha256": "x"})
    assert resp.status_code == 409 and resp.json()["code"] == "ai_unavailable"
    assert fake.calls["ai_draft"] == []
    # consent 缺省或不是 true → 422
    for body in ({}, {"consent": False}, {"consent": "true"}):
        resp = await client.post(f"/api/datasources/imports/{st['id']}/draft-ai", json=body)
        assert resp.status_code == 422 and resp.json()["code"] == "ai_consent_required"
    # 太大 → 413
    fake.availability = AiAvailability(True, model="假模型", provider="假接入")
    fake.compress_too_large = True
    resp = await client.get(f"/api/datasources/imports/{st['id']}/draft-ai/preview")
    assert resp.status_code == 413 and resp.json()["code"] == "ai_too_large"
    resp = await client.post(f"/api/datasources/imports/{st['id']}/draft-ai", json={"consent": True,
                                                                                  "preview_sha256": "x"})
    assert resp.status_code == 413 and fake.calls["ai_draft"] == []


async def test_ai_preview_is_the_full_first_call_and_stale_sha_is_refused(client, fake):
    fake.complete = False
    st = await staged(client)
    resp = await client.get(f"/api/datasources/imports/{st['id']}/draft-ai/preview")
    assert resp.status_code == 200, resp.text
    pv = resp.json()
    assert pv["model"] == "假模型" and pv["provider"] == "假接入" and pv["chars"] == len(pv["text"])
    assert pv["text_sha256"] == hashlib.sha256(pv["text"].encode()).hexdigest()
    # 同意令牌绑定全文、接入名、模型 id（AU-7）
    assert pv["sha256"] == recipe_imports.consent_token(pv["text_sha256"], fake.availability)
    assert pv["sha256"] != pv["text_sha256"]
    assert pv["text"].startswith("系统提示") and "第 9 行像合计行" in pv["text"], "规则半成品的失败原因也在全文里"
    resp = await client.post(f"/api/datasources/imports/{st['id']}/draft-ai",
                             json={"consent": True, "preview_sha256": "0" * 64})
    assert resp.status_code == 409 and resp.json()["code"] == "ai_preview_stale"
    assert fake.calls["ai_draft"] == []
    resp = await client.post(f"/api/datasources/imports/{st['id']}/draft-ai",
                             json={"consent": True, "preview_sha256": pv["sha256"], "signed_by": "测试员"})
    assert resp.status_code == 200, resp.text
    out = resp.json()
    sent = fake.calls["ai_draft"][-1]["compressed"]
    assert sent.text in pv["text"]
    assert out["recipe_origin"] == "ai" and out["answers"] == {} and out["recipe"]["other_visible_sheets"] == "reject"
    assert out["cards"][0]["id"] == "ai_origin"
    consent = out["ai_consents"][-1]
    assert consent["preview_sha256"] == pv["sha256"] and consent["compressed_sha256"] == sent.sha256
    assert consent["text_sha256"] == pv["text_sha256"]
    assert consent["signed_by"] == "测试员" and consent["chars"] == len(pv["text"]) and consent["model"] == "假模型"
    assert out["ai_draft"]["usage"][0]["input_tokens"] == 120 and out["ai_draft"]["total_tokens"] == 200
    assert "failures_for_model" not in (out["ai_draft"]["draft"] or {})
    # AI 起草时静态校验用 origin=ai
    assert fake.calls["validate"][-1]["origin"] == "ai"


async def test_ai_failed_keeps_the_recipe_and_records_usage_first(client, fake):
    fake.complete = False
    fake.ai_result = AiDraftResult(None, [AiUsage("假模型", 10, 0, 0.0, False, "2026-10-01T00:00:00+00:00", 1),
                                          AiUsage("假模型", 10, 5, 0.0, False, "2026-10-01T00:00:01+00:00", 2)],
                                   2, "AI 的输出被截断，配方没有写完（输出被截断）", 40, "a" * 64)
    st = await staged(client)
    assert st["recipe"]
    pv = (await client.get(f"/api/datasources/imports/{st['id']}/draft-ai/preview")).json()
    resp = await client.post(f"/api/datasources/imports/{st['id']}/draft-ai",
                             json={"consent": True, "preview_sha256": pv["sha256"]})
    assert resp.status_code == 502 and resp.json()["code"] == "ai_failed"
    # 原话 + 下一步：有工作配方（规则半成品）时叫用户在面板里接着改
    assert resp.json()["detail"] == fake.ai_result.error + "。可以重试，或在配方面板中修改现有配方"
    out = (await client.get(f"/api/datasources/imports/{st['id']}")).json()
    assert out["recipe"] == st["recipe"] and out["recipe_origin"] == "rules"
    assert out["ai_draft"]["error"] == fake.ai_result.error and out["ai_draft"]["draft"] is None
    assert len(out["ai_consents"]) == 1
    row = await get(ImportStaging, st["id"])
    assert [u["attempt"] for u in row.ai_usage] == [1, 2]


async def test_ai_failed_without_a_working_recipe_points_to_pasting_one(client, fake):
    """规则起草连半成品都没有时，配方面板里只有「粘贴配方」、没有表单可填：AI 起草失败（502）和表格太大
    （413）的下一步都说粘贴一份完整的配方，不说「填写」。"""
    fake.complete = False
    fake.no_recipe = True
    fake.ai_result = AiDraftResult(None, [AiUsage("假模型", 10, 0, 0.0, False, "2026-10-01T00:00:00+00:00", 1)],
                                   1, "AI 未能起草出合法的配方", 40, "a" * 64)
    st = await staged(client)
    assert st["recipe"] is None and st["ai"]["offered"] is True
    pv = (await client.get(f"/api/datasources/imports/{st['id']}/draft-ai/preview")).json()
    resp = await client.post(f"/api/datasources/imports/{st['id']}/draft-ai",
                             json={"consent": True, "preview_sha256": pv["sha256"]})
    assert resp.status_code == 502 and resp.json()["code"] == "ai_failed"
    assert resp.json()["detail"] == "AI 未能起草出合法的配方。可以重试，或在配方面板中粘贴一份完整的配方"
    fake.compress_too_large = True
    resp = await client.get(f"/api/datasources/imports/{st['id']}/draft-ai/preview")
    assert resp.status_code == 413 and resp.json()["code"] == "ai_too_large"
    assert resp.json()["detail"].endswith("无法交给 AI 起草。请在配方面板中粘贴一份完整的配方"), resp.json()["detail"]


async def test_ai_draft_dry_run_is_awaited_inside_a_parse_slot_off_the_event_loop(client, fake):
    """AI 每次尝试之后的干跑（WP-4 遗留）：接口层交给 ai_draft 的是协程函数，执行器在线程池里跑、
    占着一个解析名额；不在事件循环线程上同步执行。"""
    fake.complete = False
    fake.slot = table_versions.parse_slot()
    st = await staged(client)
    pv = (await client.get(f"/api/datasources/imports/{st['id']}/draft-ai/preview")).json()
    n = len(fake.calls["execute"])
    resp = await client.post(f"/api/datasources/imports/{st['id']}/draft-ai",
                             json={"consent": True, "preview_sha256": pv["sha256"]})
    assert resp.status_code == 200, resp.text
    assert fake.calls["ai_dry_is_coroutine"] == [True]
    dry = fake.calls["execute"][n]                      # ai_draft 里那次干跑（之后才是采用草稿时的重算）
    assert dry["db"] is None and dry["thread"] != threading.get_ident(), "干跑不在事件循环线程上"
    assert dry["slots_free"] == table_versions.MAX_PARALLEL_PARSES - 1, "干跑时占着一个解析名额"


async def test_ai_draft_does_not_hold_a_parse_slot_while_waiting(client, fake):
    fake.complete = False
    seen: dict[str, Any] = {}

    async def wait_for_model():
        slot = table_versions.parse_slot()
        seen["free"] = slot._value
        # 名额全占满，「等模型」这一步照样走得完
        for _ in range(table_versions.MAX_PARALLEL_PARSES):
            await slot.acquire()
        try:
            await asyncio.sleep(0.01)
        finally:
            for _ in range(table_versions.MAX_PARALLEL_PARSES):
                slot.release()

    fake.ai_hook = wait_for_model
    st = await staged(client)
    pv = (await client.get(f"/api/datasources/imports/{st['id']}/draft-ai/preview")).json()
    resp = await asyncio.wait_for(client.post(f"/api/datasources/imports/{st['id']}/draft-ai",
                                              json={"consent": True, "preview_sha256": pv["sha256"]}), timeout=10)
    assert resp.status_code == 200, resp.text
    assert seen["free"] == table_versions.MAX_PARALLEL_PARSES


@pytest.mark.parametrize("outcome", ["drafted", "failed"])
async def test_ai_usage_is_recorded_even_if_the_staging_closes_while_waiting(client, fake, outcome):
    """等模型期间暂存区被放弃：用量照样落库（瘦身后的行也留用量），草稿不写回已瘦身的行，回 409 staging_closed。"""
    fake.complete = False
    if outcome == "failed":
        fake.ai_result = AiDraftResult(None, [AiUsage("假模型", 10, 5, 0.0, False, "2026-10-01T00:00:00+00:00", 1)],
                                       1, "AI 没有返回合法的配方", 40, "a" * 64)
    st = await staged(client)
    pv = (await client.get(f"/api/datasources/imports/{st['id']}/draft-ai/preview")).json()

    async def discard_meanwhile():
        resp = await client.delete(f"/api/datasources/imports/{st['id']}")
        assert resp.status_code == 204

    fake.ai_hook = discard_meanwhile
    resp = await client.post(f"/api/datasources/imports/{st['id']}/draft-ai",
                             json={"consent": True, "preview_sha256": pv["sha256"]})
    assert resp.status_code == 409 and resp.json()["code"] == "staging_closed", resp.text
    row = await get(ImportStaging, st["id"])
    assert row.status == "discarded" and [u["model"] for u in row.ai_usage] == ["假模型"]
    assert row.drafts is None, "已瘦身的行不再写回草稿"
    assert len(row.ai_consents) == 1


async def test_ai_usage_and_summary_are_kept_when_adopting_the_draft_fails(client, fake, monkeypatch):
    """采用 AI 草稿那一步出错（例如原件已不在）：用量和这次起草的摘要已经落库，工作配方不变。"""
    fake.complete = False
    st = await staged(client)
    pv = (await client.get(f"/api/datasources/imports/{st['id']}/draft-ai/preview")).json()

    def adopt_fails(*a, **kw):
        raise recipe_imports.ImportRefused(409, "raw_missing", recipe_imports._RAW_GONE)

    monkeypatch.setattr(recipe_imports, "_adopt_ai_work", adopt_fails)
    resp = await client.post(f"/api/datasources/imports/{st['id']}/draft-ai",
                             json={"consent": True, "preview_sha256": pv["sha256"]})
    assert resp.status_code == 409 and resp.json()["code"] == "raw_missing", resp.text
    out = (await client.get(f"/api/datasources/imports/{st['id']}")).json()
    assert out["recipe"] == st["recipe"] and out["recipe_origin"] == "rules"
    assert out["ai_draft"]["usage"][0]["input_tokens"] == 120 and out["ai_draft"]["draft"]["recipe"]
    row = await get(ImportStaging, st["id"])
    assert [u["model"] for u in row.ai_usage] == ["假模型"] and "ai_adopted" not in row.drafts


async def test_ai_draft_commit_needs_ai_origin_and_records_usage(client, fake):
    fake.complete = False
    st = await staged(client)
    pv = (await client.get(f"/api/datasources/imports/{st['id']}/draft-ai/preview")).json()
    resp = await client.post(f"/api/datasources/imports/{st['id']}/draft-ai",
                             json={"consent": True, "preview_sha256": pv["sha256"]})
    assert resp.status_code == 200
    st = await trialed(client, st["id"])
    assert st["trial"]["status"] == "passed"
    url = f"/api/datasources/imports/{st['id']}/commit"
    resp = await client.post(url, json={"trial_id": st["trial"]["trial_id"], "confirmations": ["mode"]})
    assert resp.status_code == 422 and resp.json()["code"] == "confirm_required"
    assert "ai_origin" in resp.json()["detail"]
    out = await committed(client, st)
    rec = await get(TableRecipe, out["recipe_id"])
    assert rec.origin == "ai" and rec.ai_usage["calls"][0]["model"] == "假模型"
    assert rec.ai_usage["consents"][0]["preview_sha256"] == pv["sha256"]
    imp = await get(TableImport, out["import_id"])
    from app.core import artifact_store

    manifest = artifact_store.load(imp.manifest_artifact)
    assert manifest["ai"]["usage"] and manifest["ai"]["consents"][0]["preview_sha256"] == pv["sha256"]
    assert manifest["recipe"]["origin"] == "ai"


async def test_ai_then_manual_edit_commits_as_mixed(client, fake):
    fake.complete = False
    st = await staged(client)
    pv = (await client.get(f"/api/datasources/imports/{st['id']}/draft-ai/preview")).json()
    await client.post(f"/api/datasources/imports/{st['id']}/draft-ai",
                      json={"consent": True, "preview_sha256": pv["sha256"]})
    resp = await client.put(f"/api/datasources/imports/{st['id']}/recipe",
                            json={"recipe": {**FLOW_FULL, "other_visible_sheets": "confirm"}})
    assert resp.json()["recipe_origin"] == "manual"
    st = await trialed(client, st["id"])
    assert fake.calls["confirm_items"][-1].recipe_origin == "mixed"
    out = await committed(client, st)
    assert (await get(TableRecipe, out["recipe_id"])).origin == "mixed"


async def test_interactive_dry_run_failure_becomes_a_problem(client, fake, monkeypatch):
    st = await staged(client)
    pipe = recipe_imports.pipeline()
    real = pipe.execute

    def flaky(rec, raw, filename, scan, db_path, **kw):
        if db_path is None:
            raise RuntimeError("合成的执行器错误 12345")
        return real(rec, raw, filename, scan, db_path, **kw)

    monkeypatch.setattr(pipe, "execute", flaky)
    resp = await client.post(f"/api/datasources/imports/{st['id']}/answers",
                             json={"answers": {"q_relation:F1": {"value": "register"}}})
    assert resp.status_code == 200, resp.text
    probs = resp.json()["draft_problems"]
    assert [p["code"] for p in probs] == ["dry_run_failed"] and "12345" not in probs[0]["message"]
    # 试运行不吞错：照常执行（这里写库那一步是好的）
    st = await trialed(client, st["id"])
    assert st["trial"]["status"] == "passed"


async def test_trial_unreadable_file_is_a_400(client, fake, monkeypatch):
    st = await staged(client)
    pipe = recipe_imports.pipeline()

    def unreadable(rec, raw, filename, scan, db_path, **kw):
        raise UnsupportedTable("工作表「甲表」读取失败：文件已损坏")

    monkeypatch.setattr(pipe, "execute", unreadable)
    resp = await client.post(f"/api/datasources/imports/{st['id']}/trial", json={})
    assert resp.status_code == 400 and resp.json()["code"] == "file_unreadable"
    assert (await get(ImportStaging, st["id"])).trial_path is None


# --------------------------------------------------------------------------
# 整体审查（AU / SE）修复
# --------------------------------------------------------------------------


async def test_ai_draft_identical_to_working_recipe_invalidates_the_trial(client, fake):
    """AU-4：AI 草稿与已试运行的工作配方逐字相同（哈希没变），来源却从手改变成了 AI：试运行作废，
    重跑之后清单里有 ai_origin，照它勾就能提交。"""
    fake.complete = False
    st = await staged(client)
    resp = await client.put(f"/api/datasources/imports/{st['id']}/recipe",
                            json={"recipe": {**FLOW_FULL, "other_visible_sheets": "reject"}})
    assert resp.status_code == 200 and resp.json()["recipe_origin"] == "manual"
    st = await trialed(client, st["id"])
    assert "ai_origin" not in [i["id"] for i in st["trial"]["confirm_items"]]
    pv = (await client.get(f"/api/datasources/imports/{st['id']}/draft-ai/preview")).json()
    resp = await client.post(f"/api/datasources/imports/{st['id']}/draft-ai",
                             json={"consent": True, "preview_sha256": pv["sha256"]})
    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert out["recipe_origin"] == "ai" and out["recipe_sha256" if "recipe_sha256" in out else "recipe"] is not None
    assert out["status"] == "drafting", "来源变了，试运行作废"
    resp = await client.post(f"/api/datasources/imports/{st['id']}/commit",
                             json={"trial_id": st["trial"]["trial_id"],
                                   "confirmations": [i["id"] for i in st["trial"]["confirm_items"]]})
    assert resp.status_code == 409 and resp.json()["code"] == "trial_required"
    st = await trialed(client, st["id"])
    assert "ai_origin" in [i["id"] for i in st["trial"]["confirm_items"]]
    await committed(client, st)


async def test_commit_requiring_items_the_trial_did_not_show_asks_for_a_new_trial(client, fake):
    """AU-4（通用）：提交时服务端重算出的必勾项比试运行给界面的多：试运行作废、回 409 trial_required，
    而不是回一个界面清单里根本没有的 422 confirm_required。"""
    st = await trialed(client, (await staged(client))["id"])
    fake.confirm_extra = [ConfirmItem("mask_lost:金额_元", "遮罩列在新结构里找不到")]
    resp = await client.post(f"/api/datasources/imports/{st['id']}/commit",
                             json={"trial_id": st["trial"]["trial_id"],
                                   "confirmations": [i["id"] for i in st["trial"]["confirm_items"]]})
    assert resp.status_code == 409 and resp.json()["code"] == "trial_required", resp.text
    row = await get(ImportStaging, st["id"])
    assert row.status == "drafting" and row.trial_key is None and row.trial_path is None
    st = await trialed(client, st["id"])
    assert "mask_lost:金额_元" in [i["id"] for i in st["trial"]["confirm_items"]]
    await committed(client, st)


async def test_concurrent_ai_drafts_on_one_staging_are_refused(client, fake):
    """SE-11：同一暂存区并发两个 POST draft-ai：第二个回 409 ai_in_progress、不调模型；同意记录和用量各一条。"""
    fake.complete = False
    st = await staged(client)
    pv = (await client.get(f"/api/datasources/imports/{st['id']}/draft-ai/preview")).json()
    gate = asyncio.Event()

    async def hold():
        await gate.wait()

    fake.ai_hook = hold
    url = f"/api/datasources/imports/{st['id']}/draft-ai"
    first = asyncio.create_task(client.post(url, json={"consent": True, "preview_sha256": pv["sha256"],
                                                       "signed_by": "甲"}))
    for _ in range(200):
        if fake.calls["ai_draft"]:
            break
        await asyncio.sleep(0.01)
    assert len(fake.calls["ai_draft"]) == 1
    try:
        second = await asyncio.wait_for(
            client.post(url, json={"consent": True, "preview_sha256": pv["sha256"], "signed_by": "乙"}), timeout=5)
    finally:
        gate.set()
    assert second.status_code == 409 and second.json()["code"] == "ai_in_progress", second.text
    resp = await asyncio.wait_for(first, timeout=10)
    assert resp.status_code == 200, resp.text
    assert len(fake.calls["ai_draft"]) == 1
    row = await get(ImportStaging, st["id"])
    assert [c["signed_by"] for c in row.ai_consents] == ["甲"] and len(row.ai_usage) == 1
    # 结束后标记清掉：可以再起草一次
    fake.ai_hook = None
    pv = (await client.get(f"/api/datasources/imports/{st['id']}/draft-ai/preview")).json()
    resp = await client.post(url, json={"consent": True, "preview_sha256": pv["sha256"]})
    assert resp.status_code == 200, resp.text


async def test_discard_waiting_behind_a_commit_does_not_overwrite_committed(client, fake, monkeypatch):
    """SE-8：提交持锁发布时，放弃请求已过了开头的检查、在锁上排队；提交完成后放弃拿到锁：回 409 staging_closed，
    暂存区仍是 committed。"""
    st = await trialed(client, (await staged(client))["id"])
    release = asyncio.Event()
    entered = asyncio.Event()
    real_close = table_versions.close_staging

    def slow_close(staging, status, **kw):
        if status == "committed":
            entered.set()
        return real_close(staging, status, **kw)

    real_publish = table_versions.publish_build

    async def publish(*a, **kw):
        before = kw["before_commit"]

        async def wait_then(imp_id, snap_id):
            await before(imp_id, snap_id)
            entered.set()
            await release.wait()
        kw["before_commit"] = wait_then
        return await real_publish(*a, **kw)

    monkeypatch.setattr(table_versions, "publish_build", publish)
    commit = asyncio.create_task(client.post(f"/api/datasources/imports/{st['id']}/commit",
                                             json={"trial_id": st["trial"]["trial_id"],
                                                   "confirmations": [i["id"] for i in st["trial"]["confirm_items"]]}))
    await asyncio.wait_for(entered.wait(), timeout=10)
    discard = asyncio.create_task(client.delete(f"/api/datasources/imports/{st['id']}"))
    for _ in range(200):
        if table_versions.store_lock()._waiters:
            break
        await asyncio.sleep(0.01)
    assert table_versions.store_lock()._waiters, "放弃请求在锁上排队"
    release.set()
    resp = await asyncio.wait_for(commit, timeout=10)
    assert resp.status_code == 201, resp.text
    d = await asyncio.wait_for(discard, timeout=10)
    assert d.status_code == 409 and d.json()["code"] == "staging_closed", d.text
    row = await get(ImportStaging, st["id"])
    assert row.status == "committed" and row.committed_import_id == resp.json()["import_id"]


async def test_commit_after_discard_finished_during_preflight_is_refused(client, fake, monkeypatch):
    """SE-8：提交预检期间放弃已经完成：提交不发布、不把已放弃的暂存区改回 drafting。"""
    st = await trialed(client, (await staged(client))["id"])
    real_context = recipe_imports._context
    done = {"n": 0}

    async def context_then_discard(session, staging):
        cx = await real_context(session, staging)
        if done["n"] == 0:
            done["n"] += 1
            resp = await client.delete(f"/api/datasources/imports/{st['id']}")
            assert resp.status_code == 204
        return cx

    monkeypatch.setattr(recipe_imports, "_context", context_then_discard)
    resp = await client.post(f"/api/datasources/imports/{st['id']}/commit",
                             json={"trial_id": st["trial"]["trial_id"],
                                   "confirmations": [i["id"] for i in st["trial"]["confirm_items"]]})
    assert resp.status_code == 409 and resp.json()["code"] in ("staging_closed", "trial_required"), resp.text
    row = await get(ImportStaging, st["id"])
    assert row.status == "discarded" and row.committed_import_id is None


async def test_publish_after_discard_is_refused_inside_the_lock(client, fake, monkeypatch):
    """SE-8：预检都过了、发布前一刻暂存区被放弃（试运行库碰巧还在）：锁内重读状态，不发布已放弃的暂存区。"""
    st = await trialed(client, (await staged(client))["id"])
    monkeypatch.setattr(table_versions, "remove_trial_file", lambda path: None)

    async def keep_raw(session, staging):     # 原件还被别的暂存区引用着
        return None
    monkeypatch.setattr(table_versions, "release_staging_raw", keep_raw)
    real_publish = table_versions.publish_build

    async def discard_then_publish(*a, **kw):
        resp = await client.delete(f"/api/datasources/imports/{st['id']}")
        assert resp.status_code == 204
        return await real_publish(*a, **kw)

    monkeypatch.setattr(table_versions, "publish_build", discard_then_publish)
    resp = await client.post(f"/api/datasources/imports/{st['id']}/commit",
                             json={"trial_id": st["trial"]["trial_id"],
                                   "confirmations": [i["id"] for i in st["trial"]["confirm_items"]]})
    assert resp.status_code == 409 and resp.json()["code"] == "staging_closed", resp.text
    row = await get(ImportStaging, st["id"])
    assert row.status == "discarded" and row.committed_import_id is None
    async with SessionLocal() as session:
        n = (await session.execute(select(func.count()).select_from(TableImport)
                                   .where(TableImport.source_id == st["source"]["id"]))).scalar()
    assert n == 0
