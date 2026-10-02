"""期 2 集成验收（WP-7，P2-SPEC 9.1 / 9.2 / 9.3）：真管线（default_pipeline）经接口走完整流程。

不用假 Pipeline：暂存、规则起草、回答问题、试运行、差异卡、确认项、提交全部是各工作包合并后的真代码，
接口一律经 AsyncClient(transport=ASGITransport(app=app))。夹具全部合成、假名（tests/fixtures/xlsx）；
不调用任何真实模型（这几组用例都走规则起草，用不到 AI）。

- 9.1：flow_workbook(2026-08-01, 31 天) 走 暂存 → 规则起草 → 回答问题 → 改宽表名 → 试运行，逐项对 FLOW_EXPECT。
- 9.2：31 例漂移（30 例 + D28b）逐例参数化。每例先把 D00 首次导入并启用（D00 的已确认配方就是现行配方、
  D00 的导入就是上一期），再把该例文件作为「上传新一期」重放，断言试运行状态、行数、差异卡 kind 的精确集合、
  确认项的精确集合、拒收原因与坐标、说明片段，以及能否提交。
- 9.3：实验室用例 c01 / c04 / c05 / c06 / c08 / c10 的各变体。

断言照规格写；跑不过的保留失败，由验收报告逐条诊断，不放宽、不标 xfail。
"""
from __future__ import annotations

import json
import re
import sqlite3
import uuid
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from app.core import artifact_store
from app.core.config import settings
from app.data import recipe, recipe_parsers
from app.data.recipe_types import prose_problems
from app.db.base import SessionLocal
from app.db.models import DataSource, SourceSnapshot, TableImport
from app.main import app
from tests.fixtures.xlsx import lab
from tests.fixtures.xlsx.drift import AUG, DRIFT_CASES, DriftCase
from tests.fixtures.xlsx.flow import FLOW_EXPECT, flow_workbook

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
REF_RECIPE = json.loads((Path(__file__).parent / "fixtures" / "recipes" / "flow_recipe.json").read_text("utf-8"))
#: 基线和各例共用的随机种子：D00 例「同一文件再传」要求基线与它字节相同
SEED = 0
SHEET = "客流汇总"
#: 9.2 的 expect → TrialOut.status。确认项不影响状态（5.3），所以「需要确认」的各例试运行是 passed
TRIAL_STATUS = {"passed": "passed", "confirm": "passed", "rejected": "rejected", "decision": "needs_decision"}
#: 2.4 末尾实测的 derive_tables 结果：表 → [(列名, SQLite 类型)]，以及主键
REF_COLUMNS = {
    "日客流": [("日期", "TEXT"), ("全日客流", "INTEGER"), ("分区甲", "INTEGER"), ("分区乙", "INTEGER")],
    "时段客流": [("日期", "TEXT"), ("时段", "TEXT"), ("时段类别", "TEXT"), ("起始小时", "INTEGER"),
               ("结束小时", "INTEGER"), ("客流", "INTEGER")],
    "时段客流_表内合计": [("日期", "TEXT"), ("合计项", "TEXT"), ("起始小时", "INTEGER"), ("结束小时", "INTEGER"),
                   ("客流", "INTEGER")],
}
REF_GRAIN = {"日客流": ["日期"], "时段客流": ["日期", "时段"], "时段客流_表内合计": ["日期", "合计项"]}
TABLE_ORDER = ("日客流", "时段客流", "时段客流_表内合计")


# ==========================================================================
# 夹具与小工具
# ==========================================================================


@pytest.fixture(autouse=True)
def store(monkeypatch, tmp_path):
    """每个测试一个独立的数据目录：原件存档、构建库、试运行库、工件互不串。"""
    data = tmp_path / "data"
    (data / "uploads").mkdir(parents=True)
    monkeypatch.setattr(settings, "data_dir", data)
    yield data


@pytest.fixture(autouse=True)
def real_pipeline_no_model(monkeypatch):
    """验收必须跑真管线：确认 recipe_imports.pipeline() 接的是各工作包的真函数（不是别的测试留下的假实现）。

    规则起草不完整的用例（c04、c05、c06）在 StagingOut 里会查 AI 可用性；这里把模型解析换成「不可用」的桩，
    测试里连模型对象都不建，更不会发请求。
    """
    from app.data import recipe_ai, recipe_engine, recipe_imports, recipe_suggest
    from app.data.recipe_types import AiAvailability

    p = recipe_imports.pipeline()
    assert p.execute is recipe_engine.execute and p.draft is recipe_suggest.draft
    assert p.ai_availability is recipe_ai.ai_availability

    async def no_model(session):
        return None, "", AiAvailability(False, reason="验收测试不接模型")

    monkeypatch.setattr(recipe_ai, "resolve_draft_model", no_model)


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


def unique(prefix: str = "wp7a") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


async def get(model, key):
    async with SessionLocal() as session:
        return await session.get(model, key)


async def stage(client, raw: bytes, filename: str, name: str | None = None) -> dict[str, Any]:
    resp = await client.post("/api/datasources/imports/stage", files={"file": (filename, raw, XLSX)},
                             data={"name": name or unique()})
    assert resp.status_code == 201, resp.text
    return resp.json()


async def trial(client, sid: str, **body) -> dict[str, Any]:
    resp = await client.post(f"/api/datasources/imports/{sid}/trial", json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()


async def answer(client, sid: str, answers: dict[str, dict[str, Any]]) -> dict[str, Any]:
    resp = await client.post(f"/api/datasources/imports/{sid}/answers", json={"answers": answers})
    assert resp.status_code == 200, resp.text
    return resp.json()


async def put_recipe(client, sid: str, rec: dict[str, Any]) -> dict[str, Any]:
    resp = await client.put(f"/api/datasources/imports/{sid}/recipe", json={"recipe": rec})
    assert resp.status_code == 200, resp.text
    return resp.json()


async def commit(client, st: dict[str, Any], confirmations: list[str] | None = None,
                 acceptances: list[dict[str, str]] | None = None):
    """提交；返回原始响应（调用方自己判状态码）。confirmations 缺省 = 勾上全部确认项。"""
    body: dict[str, Any] = {"trial_id": st["trial"]["trial_id"],
                            "confirmations": confirm_ids(st) if confirmations is None else confirmations}
    if acceptances is not None:
        body["acceptances"] = acceptances
    return await client.post(f"/api/datasources/imports/{st['id']}/commit", json=body)


async def reupload(client, source_id: str, raw: bytes, filename: str) -> dict[str, Any]:
    resp = await client.post(f"/api/datasources/{source_id}/reupload", files={"file": (filename, raw, XLSX)})
    assert resp.status_code == 201, resp.text
    return resp.json()


def confirm_ids(st: dict[str, Any]) -> list[str]:
    return [i["id"] for i in st["trial"]["confirm_items"]]


def check(st: dict[str, Any], cid: str) -> dict[str, Any] | None:
    return next((c for c in st["trial"]["checks"] if c["id"] == cid), None)


def problems(st: dict[str, Any], code: str | None = None) -> list[dict[str, Any]]:
    return [p for p in st["trial"]["problems"] if code is None or p["code"] == code]


def rows_of(st: dict[str, Any]) -> dict[str, int]:
    return {t["name"]: t["rows"] for t in st["trial"]["receipt"]["tables"]}


def all_comments(st: dict[str, Any]) -> str:
    """试运行的说明预览里全部表说明和列说明拼在一起（找片段用）。"""
    notes = st["trial"]["notes"] or {}
    parts = []
    for n in notes.values():
        parts.append(n.get("comment") or "")
        parts += list((n.get("columns") or {}).values())
    return "\n".join(parts)


def match_confirms(actual: list[str], expected: tuple[str, ...] | frozenset[str]) -> tuple[list[str], list[str]]:
    """确认项的精确集合比较；期望里带 * 的按前缀。返回 (多出来的, 缺少的)。"""
    def hit(item: str, pattern: str) -> bool:
        return item.startswith(pattern[:-1]) if pattern.endswith("*") else item == pattern

    extra = [a for a in actual if not any(hit(a, p) for p in expected)]
    missing = [p for p in expected if not any(hit(a, p) for a in actual)]
    return extra, missing


def rename_wide(rec: dict[str, Any], old: str, new: str) -> dict[str, Any]:
    """把规则起草的宽表名改成 new：表名、写入它的分段（id 一起改）、关系里引用的表名（9.7 第 1 步）。"""
    rec = json.loads(json.dumps(rec, ensure_ascii=False))
    for sheet in rec["sheets"]:
        for block in sheet["blocks"]:
            for seg in block.get("segments", []):
                if seg.get("table") == old:
                    seg["table"] = new
                if seg.get("id") == old:
                    seg["id"] = new
    for t in rec["tables"]:
        if t["name"] == old:
            t["name"] = new
    for r in rec["relations"]:
        if r.get("table") == old:
            r["table"] = new
        for side in ("a", "b"):
            if isinstance(r.get(side), dict) and r[side].get("table") == old:
                r[side]["table"] = new
    return rec


def sql(path: str, query: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    """只读打开库文件查一句（不改文件，库哈希不受影响）。"""
    with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as conn:
        return list(conn.execute(query, params))


async def snapshot_db(snapshot_id: str) -> str:
    snap = await get(SourceSnapshot, snapshot_id)
    assert snap is not None and snap.db_path, snapshot_id
    return snap.db_path


async def current_snapshot(source_id: str) -> str | None:
    return (await get(DataSource, source_id)).current_snapshot_id


async def schema_of(client, source_id: str, table: str) -> dict[str, Any]:
    resp = await client.get(f"/api/datasources/{source_id}/schema", params={"table": table})
    assert resp.status_code == 200, resp.text
    return resp.json()


# ==========================================================================
# D00：暂存 → 规则起草 → 回答问题 → 改宽表名 → 试运行（→ 提交）
# ==========================================================================


async def flow_trial(client, seed: int = SEED, name: str | None = None) -> dict[str, Any]:
    """D00 走到试运行，返回 {"staged": 暂存时的 StagingOut, "st": 试运行后的 StagingOut}。

    回答：系统发现一律「登记」，占位符「·」选「存为空值」（参考配方的写法）；然后把规则起草的宽表名
    （「客流汇总_按日」）改成参考配方的「日客流」。
    """
    raw, fn = flow_workbook(AUG, 31, seed=seed)
    staged = await stage(client, raw, fn, name)
    # 期 3：q_mode（每期怎么更新）答「每期替换」，按参考配方比哈希（P3-SPEC 12.0、1.5）
    answers = {q["id"]: {"value": "register" if q["id"].startswith("q_relation")
                         else "replace" if q["id"] == "q_mode" else "null"}
               for q in staged["questions"]}
    after = await answer(client, staged["id"], answers)
    wide = next(t["name"] for t in after["recipe"]["tables"] if t["name"].endswith("_按日"))
    await put_recipe(client, staged["id"], rename_wide(after["recipe"], wide, "日客流"))
    st = await trial(client, staged["id"])
    return {"staged": staged, "st": st, "raw": raw, "file_name": fn}


async def baseline(client, seed: int = SEED) -> dict[str, Any]:
    """D00 首次导入并启用（勾全部确认项）。返回 flow_trial 的结果加上提交响应。"""
    done = await flow_trial(client, seed)
    assert done["st"]["trial"]["status"] == "passed", done["st"]["trial"]["problems"]
    resp = await commit(client, done["st"])
    assert resp.status_code == 201, resp.text
    done["commit"] = resp.json()
    done["source_id"] = done["commit"]["source"]["id"]
    return done


# ==========================================================================
# 9.1 合成「真实结构」夹具：D00 的期望（FLOW_EXPECT）
# ==========================================================================


async def test_9_1_rules_draft_answers_and_rename_give_the_reference_recipe(client):
    """规则起草完整；回答问题、改宽表名之后，工作配方与参考配方 recipe_sha256 相同（9.1 的期望以参考配方为前提）。"""
    raw, fn = flow_workbook(AUG, 31, seed=SEED)
    staged = await stage(client, raw, fn)
    assert staged["draft"]["complete"] is True, staged["draft"]["failures"]
    assert staged["recipe_origin"] == "rules" and staged["recipe_problems"] == []
    assert len(staged["cards"]) == 10, [c["id"] for c in staged["cards"]]
    qids = {q["id"] for q in staged["questions"]}
    assert {"q_placeholder:·", "q_relation:F1", "q_relation:F2"} <= qids
    # 6.1 第 9、10 条：占位符和系统发现的问题都没有默认值，必须由人选
    assert all(q["default"] is None for q in staged["questions"] if q["id"] in
               ("q_placeholder:·", "q_relation:F1", "q_relation:F2"))
    done = await flow_trial(client)
    st = done["st"]
    assert st["recipe_problems"] == []
    ref, ref_problems = recipe.validate_recipe(REF_RECIPE, origin="manual")
    mine, mine_problems = recipe.validate_recipe(st["recipe"], origin="manual")
    assert ref_problems == [] and mine_problems == []
    assert recipe.recipe_sha256(mine) == recipe.recipe_sha256(ref)
    assert st["trial"]["status"] == "passed", st["trial"]["problems"]


async def test_9_1_sheet_scan_and_cell_ledger(client):
    """格子账：非空格 771 = 各去向之和；未认领 0；两遍一致（扫描数 = 读取数）。"""
    st = (await flow_trial(client))["st"]
    sheet = next(s for s in st["sheets"] if s["name"] == SHEET)
    assert sheet["bounds"] == FLOW_EXPECT["bounds"]
    assert sheet["nonempty"] == FLOW_EXPECT["nonempty"]
    assert sheet["merged"] == FLOW_EXPECT["merged"]
    assert sheet["formulas"] == FLOW_EXPECT["formulas"] and sheet["formulas_uncached"] == 0
    ledger = st["trial"]["receipt"]["ledger"]
    assert [x["sheet"] for x in ledger] == [SHEET]
    led = ledger[0]
    assert led["nonempty_scan"] == FLOW_EXPECT["nonempty"]
    assert led["nonempty_read"] == led["nonempty_scan"], "两遍对账一致"
    assert led["unclaimed"] == 0
    assert {k: v for k, v in led["roles"].items() if v} == FLOW_EXPECT["ledger"]
    assert sum(led["roles"].values()) == FLOW_EXPECT["nonempty"]


async def test_9_1_tables_columns_types_rows_strict_and_primary_key(client):
    done = await flow_trial(client)
    st = done["st"]
    tables = {t["name"]: t for t in st["trial"]["receipt"]["tables"]}
    assert list(tables) == list(TABLE_ORDER)
    assert rows_of(st) == FLOW_EXPECT["rows"]
    for name, cols in REF_COLUMNS.items():
        assert [(c["name"], c["type"]) for c in tables[name]["columns"]] == cols, name
        assert tables[name]["grain"] == REF_GRAIN[name], name
    assert tables["时段客流_表内合计"]["kind"] == "reported_total"
    assert tables["日客流"]["kind"] == tables["时段客流"]["kind"] == "data"

    resp = await commit(client, st)
    assert resp.status_code == 201, resp.text
    db = await snapshot_db(resp.json()["snapshot_id"])
    for name, cols in REF_COLUMNS.items():
        ddl = sql(db, "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (name,))[0][0]
        assert re.search(r"\)\s*STRICT\s*$", ddl), ddl
        assert "PRIMARY KEY" in ddl.upper(), ddl
        info = sql(db, f'PRAGMA table_info("{name}")')
        assert [(r[1], r[2]) for r in info] == cols, name
        assert sorted((r[5], r[1]) for r in info if r[5]) == list(enumerate(REF_GRAIN[name], start=1)), name
        assert sql(db, f'SELECT COUNT(*) FROM "{name}"')[0][0] == FLOW_EXPECT["rows"][name]


async def test_9_1_placeholders_keep_null_rows(client):
    """占位符 {"·": 31}；时段='7-8' 的 31 行客流为 NULL（D11：整期无数据的行保留为空值行）。"""
    done = await flow_trial(client)
    st = done["st"]
    assert st["trial"]["receipt"]["placeholders"] == FLOW_EXPECT["placeholders"]
    resp = await commit(client, st)
    assert resp.status_code == 201, resp.text
    db = await snapshot_db(resp.json()["snapshot_id"])
    rows = sql(db, 'SELECT "时段类别", "起始小时", "结束小时", "客流" FROM "时段客流" WHERE "时段" = ?', ("7-8",))
    assert len(rows) == 31
    assert {r[:3] for r in rows} == {("日间", 7, 8)}
    assert all(r[3] is None for r in rows)
    assert sql(db, 'SELECT COUNT(*) FROM "时段客流" WHERE "客流" IS NULL')[0][0] == 31


async def test_9_1_checks(client):
    """C1 passed（一处）、C2 passed；K1 passed（3 × 31 格）；G1 passed；R1 passed（31/31）；R2 info（31 天中 0 天相等）；
    N1–N3、P1–P3 passed。"""
    st = (await flow_trial(client))["st"]
    assert {c["id"]: c["status"] for c in st["trial"]["checks"]} == FLOW_EXPECT["checks"]
    c1 = check(st, "C1")
    assert c1["cells"] == FLOW_EXPECT["period_cells"]
    k1 = check(st, "K1")
    assert k1["checked"] == 3 * 31 and k1["failed"] == 0 and k1["unverifiable"] == 0
    assert k1["category"] == "structure"
    g1 = check(st, "G1")
    assert g1["checked"] == 3 * 31 and g1["failed"] == 0
    r1 = check(st, "R1")
    assert r1["checked"] == 31 and r1["failed"] == 0 and r1["unverifiable"] == 0
    assert r1["category"] == "data_quality"
    r2 = check(st, "R2")
    assert r2["category"] == "info"
    assert any(f"31 天中 {FLOW_EXPECT['r2_equal_days']} 天相等" in d for d in r2["details"]), r2["details"]
    # 每条 SQL 核对的原文进 CheckResult（5.1）；C1、C2 来自执行器的统计期判定，不是 SQL
    for c in st["trial"]["checks"]:
        if c["id"] not in ("C1", "C2"):
            assert c.get("sql"), c["id"]
    assert st["trial"]["acceptable"] == []


async def test_9_1_canonicalized_outside_text_and_period(client):
    """规范写法对照 0 条（合计标签的写法记在 SegmentLabels）；区域外文字只有 B3（text）；PeriodOut.texts 只有 B2、
    annotated 为空。"""
    st = (await flow_trial(client))["st"]
    rec = st["trial"]["receipt"]
    assert rec["canonicalized_total"] == FLOW_EXPECT["canonicalized"] and rec["canonicalized"] == []
    totals = next(x for x in rec["labels"] if x["segment"] == "夜间合计")
    assert totals["raw"] == ["18-22 时合计", "22-24 时合计", "18-24 时合计"]
    assert totals["canonical"] == ["18-22时合计", "22-24时合计", "18-24时合计"]
    assert [(o["cell"], o["text"], o["kind"]) for o in rec["outside_text"]] == FLOW_EXPECT["outside_text"]
    period = rec["period"]
    assert period["source"] == "cells"
    assert (period["start"], period["end"]) == ("2026-08-01", "2026-08-31")
    assert period["cells"] == FLOW_EXPECT["period_cells"]
    assert list(period["texts"]) == FLOW_EXPECT["period_cells"]
    assert period["annotated"] == {}


async def test_9_1_confirm_items_exact(client):
    st = (await flow_trial(client))["st"]
    ids = confirm_ids(st)
    assert len(ids) == len(set(ids)), ids
    assert set(ids) == FLOW_EXPECT["confirms"]
    assert all(i["required"] for i in st["trial"]["confirm_items"])


async def test_9_1_notes(client):
    """三张表的说明（模板与渲染后的文字）过 prose_problems(known=…)；日客流含「已逐日核对」；都不含 E7 那句。
    提交后表结构里的说明照样，列「全日客流」的说明是「单位：人次」。"""
    st = (await flow_trial(client))["st"]
    notes = st["trial"]["notes"]
    assert set(notes) == set(TABLE_ORDER)
    for frag in FLOW_EXPECT["note_has"]:
        assert frag in notes["日客流"]["comment"], notes["日客流"]["comment"]
    for name in TABLE_ORDER:
        for frag in FLOW_EXPECT["note_lacks"]:
            assert frag not in notes[name]["comment"], (name, notes[name]["comment"])
    assert notes["日客流"]["columns"]["全日客流"] == "单位：人次"
    # 5.4 按核对结果选模板：K1、G1 都通过 → 表内合计表写「已按明细重算核对一致」；基表指向表内合计表；
    # 值列的空值只来自占位符 → 列说明写占位符的空值含义
    assert "原表写明的合计，已按明细重算核对一致" in notes["时段客流_表内合计"]["comment"]
    assert "原表的合计在 时段客流_表内合计，不要与本表相加" in notes["时段客流"]["comment"]
    assert "空值表示原表为无数据占位符，不代表零" in notes["时段客流"]["columns"]["客流"]
    assert "空值" not in notes["时段客流_表内合计"]["columns"].get("客流", "")

    resp = await commit(client, st)
    assert resp.status_code == 201, resp.text
    out = resp.json()
    imp = await get(TableImport, out["import_id"])
    manifest = artifact_store.load(imp.manifest_artifact)
    known = {"列": {c for cols in REF_COLUMNS.values() for c, _ in cols}, "表": set(TABLE_ORDER),
             "单位": set(recipe_parsers.UNITS)}
    templates = manifest["notes"]["templates"]
    for name in TABLE_ORDER:
        assert name in templates, sorted(templates)
        assert prose_problems(templates[name], known=known) == [], (name, templates[name])
        assert prose_problems(manifest["notes"]["rendered"][name]["comment"], known=known) == [], name
    for key, text in templates.items():
        assert prose_problems(text, known=known) == [], (key, text)
    schema = await schema_of(client, out["source"]["id"], "日客流")
    assert "已逐日核对" in (schema["comment"] or "")
    col = next(c for c in schema["columns"] if c["name"] == "全日客流")
    assert col.get("comment") == "单位：人次"


# ==========================================================================
# 9.2 31 例漂移：以 D00 的已确认配方为现行配方、D00 的导入为上一期，逐例「上传新一期」
# ==========================================================================

#: 拒收各例的 fix（9.2 表「其他关键结果」）
REJECT_FIX = {"D04": {"remove_label"}, "D09": {"rename_title"}, "D13": {"add_label"}, "D16": {"ignore_cells"},
              "D18": {"declare_total"}, "D21": {"add_label", "ignore_cells"}}
#: 拒收各例的问题坐标（按 flow.py 的版式推出：9 月 30 天，日期列 C..AF）
REJECT_CELLS = {
    "D13": {"客流汇总!B8"},          # 「分区丙（人次）」在第 8 行
    "D16": {"客流汇总!AG4"},         # 右侧多出的「合计」表头（同 P25 的 AH4）
    "D18": {"客流汇总!B21"},         # 日间末尾的「7-18 时合计」
    "D21": {"客流汇总!B32"},         # 第 32 行补录
}


#: 各例的统计期（9.2：9 月 = 2026-09-01 起 30 天，10 月 = 2026-10-01 起；D00 是 8 月 31 天）
PERIOD = {"D00": ("2026-08-01", "2026-08-31"), "D02": ("2026-10-01", "2026-10-31"),
          "D03": ("2026-10-01", "2026-10-15"), "D03b": ("2026-10-01", "2026-10-07")}
SEP_PERIOD = ("2026-09-01", "2026-09-30")


def _check_id(code: str) -> bool:
    return re.fullmatch(r"[A-Z]\d+", code) is not None


async def _extra_D00(client, ctx):
    """同一文件再传：same_as_import 指向基线导入，提交 unchanged、返回原导入。"""
    st = ctx["st"]
    assert st["trial"]["same_as_import"] is not None
    assert st["trial"]["same_as_import"]["id"] == ctx["base"]["commit"]["import_id"]
    resp = await commit(client, st)
    assert resp.status_code == 201, resp.text
    out = resp.json()
    assert out["unchanged"] is True and out["import_id"] == ctx["base"]["commit"]["import_id"]
    assert out["snapshot_id"] == ctx["base"]["commit"]["snapshot_id"]
    return True


async def _extra_D02(client, ctx):
    assert rows_of(ctx["st"]) == FLOW_EXPECT["rows"], "行数与 D00 相同"


async def _annotated_title(ctx, cell: str = f"{SHEET}!B3", title: str = "客流汇总表"):
    period = ctx["st"]["trial"]["receipt"]["period"]
    assert cell in period["annotated"], period
    assert recipe_parsers.match_key(period["annotated"][cell]) == recipe_parsers.match_key(title)
    assert cell in period["texts"], period
    out = next((o for o in ctx["st"]["trial"]["receipt"]["outside_text"] if o["cell"] == cell), None)
    assert out is not None and out["kind"] == "text_digits" and out["period_source"] is True, out
    assert check(ctx["st"], "C1")["status"] == "passed"


async def _extra_D17(client, ctx):
    await _annotated_title(ctx)


async def _extra_D27(client, ctx):
    await _annotated_title(ctx)
    c1 = check(ctx["st"], "C1")
    assert {f"{SHEET}!B2", f"{SHEET}!B3"} <= set(c1["cells"]), c1


async def _extra_D23(client, ctx):
    assert check(ctx["st"], "C2")["status"] == "info"


async def _extra_D25(client, ctx):
    k1 = check(ctx["st"], "K1")
    assert k1["status"] == "passed"
    assert any("保存值与重算一致" in d for d in k1["details"]), k1["details"]
    assert ctx["st"]["trial"]["receipt"]["full_calc_on_load"] is True


async def _extra_D07(client, ctx):
    assert check(ctx["st"], "K1")["status"] == "passed"
    assert check(ctx["st"], "G1") is None, "合计写死：没有 G1"
    assert ctx["st"]["trial"]["receipt"]["derived_form"].get("夜间合计") == "literal"


async def _extra_D11(client, ctx):
    labels = next(x for x in ctx["st"]["trial"]["receipt"]["labels"] if x["segment"] == "日间")
    assert labels["raw"][0] == "07:00-08:00" and labels["canonical"][0] == "7-8"
    assert not [i for i in confirm_ids(ctx["st"]) if i.startswith("diff:canon:")]
    resp = await commit(client, ctx["st"])
    assert resp.status_code == 201, resp.text
    db = await snapshot_db(resp.json()["snapshot_id"])
    got = {r[0] for r in sql(db, 'SELECT DISTINCT "时段" FROM "时段客流"')}
    assert "7-8" in got and all(re.fullmatch(r"\d{1,2}-\d{1,2}", x) for x in got), got
    return True


async def _extra_D22(client, ctx):
    labels = next(x for x in ctx["st"]["trial"]["receipt"]["labels"] if x["segment"] == "夜间合计")
    assert labels["canonical"] == ["18-22时合计", "22-24时合计", "18-24时合计"]
    resp = await commit(client, ctx["st"])
    assert resp.status_code == 201, resp.text
    db = await snapshot_db(resp.json()["snapshot_id"])
    got = {r[0] for r in sql(db, 'SELECT DISTINCT "合计项" FROM "时段客流_表内合计"')}
    assert got == {"18-22时合计", "22-24时合计", "18-24时合计"}
    return True


async def _extra_D20(client, ctx):
    assert ctx["st"]["trial"]["receipt"]["sheets"]["other_visible"] == ["说明"]


async def _extra_D28(client, ctx):
    out = next((o for o in ctx["st"]["trial"]["receipt"]["outside_text"] if o["cell"] == f"{SHEET}!B32"), None)
    assert out is not None and out["kind"] == "text_digits", out


async def _extra_D28b(client, ctx):
    period = ctx["st"]["trial"]["receipt"]["period"]
    assert f"{SHEET}!B32" in period["annotated"], period
    c1 = check(ctx["st"], "C1")
    assert c1["status"] == "passed" and {f"{SHEET}!B2", f"{SHEET}!B32"} <= set(c1["cells"]), c1


async def _extra_D08(client, ctx):
    k1 = check(ctx["st"], "K1")
    assert k1["status"] == "mismatch" and k1["category"] == "structure"
    assert any(d.startswith("G28") for d in k1["details"]), k1["details"]


async def _extra_D26(client, ctx):
    """R1 mismatch（data_quality、可接受）；不带接受 → 422；写理由接受 → 201，说明改写。"""
    st = ctx["st"]
    r1 = check(st, "R1")
    assert r1["status"] == "mismatch" and r1["category"] == "data_quality" and r1["acceptable"] is True
    assert st["trial"]["acceptable"] == ["R1"]
    text = " ".join(r1["details"] + r1["cells"])
    assert "2026-09-10" in text and "L5" in text, r1
    resp = await commit(client, st)
    assert resp.status_code == 422 and resp.json()["code"] == "acceptance_required", resp.text
    resp = await commit(client, st, acceptances=[{"check_id": "R1", "reason": "合成理由：第 10 天的全日数据为人工补录"}])
    assert resp.status_code == 201, resp.text
    out = resp.json()
    imp = await get(TableImport, out["import_id"])
    assert [o["check_id"] for o in imp.overrides or []] == ["R1"]
    comment = (await schema_of(client, ctx["source_id"], "日客流"))["comment"] or ""
    for frag in ctx["case"].note_has:
        assert frag in comment, comment
    for frag in ctx["case"].note_lacks:
        assert frag not in comment, comment
    return True


#: 各例「其他关键结果」。返回 True 的表示已自己提交过
EXTRA = {"D00": _extra_D00, "D02": _extra_D02, "D17": _extra_D17, "D27": _extra_D27, "D23": _extra_D23,
         "D25": _extra_D25, "D07": _extra_D07, "D11": _extra_D11, "D22": _extra_D22, "D20": _extra_D20,
         "D28": _extra_D28, "D28b": _extra_D28b, "D08": _extra_D08, "D26": _extra_D26}


@pytest.mark.parametrize("code", list(DRIFT_CASES))
async def test_9_2_drift_case(client, code):
    case: DriftCase = DRIFT_CASES[code]
    base = await baseline(client)
    source_id = base["source_id"]
    snap_before = await current_snapshot(source_id)
    assert snap_before == base["commit"]["snapshot_id"]
    raw, fn = case.build(SEED)
    st = await reupload(client, source_id, raw, fn)
    tr = st["trial"]
    # 上传新一期按现行配方重放、自动试运行，不起草、不提供 AI（7.1、7.4）
    assert st["kind"] == "reupload" and st["draft"] is None and st["ai"]["offered"] is False
    assert tr["base_snapshot_id"] == snap_before

    # 试运行状态
    assert tr["status"] == TRIAL_STATUS[case.expect], (tr["status"], tr["problems"],
                                                       [c for c in tr["checks"] if c["status"] != "passed"])
    # 行数、统计期
    if case.rows is not None:
        assert tuple(rows_of(st)[t] for t in TABLE_ORDER) == case.rows
        period = tr["receipt"]["period"]
        assert (period["start"], period["end"]) == PERIOD.get(code, SEP_PERIOD), period
        assert period["source"] == "cells"
    if code != "D00":
        assert tr["same_as_import"] is None
    # 差异卡 kind 的精确集合（拒收时没有差异卡）
    if case.diff_kinds is None:
        assert tr["diff"] is None
    else:
        assert tr["diff"] is not None
        assert {d["kind"] for d in tr["diff"]} == set(case.diff_kinds), [(d["kind"], d["label"]) for d in tr["diff"]]
        for d in tr["diff"]:
            if d["requires_confirm"]:
                assert d["confirm_id"] and d["confirm_id"] in confirm_ids(st), d
    # 确认项的精确集合（配方没变：只有本期类和 diff:*）
    extra, missing = match_confirms(confirm_ids(st), case.confirms)
    assert not extra and not missing, {"多出": extra, "缺少": missing}
    assert all(i["required"] for i in tr["confirm_items"]), tr["confirm_items"]
    # 拒收原因 / 核对 id
    for c in case.codes:
        if _check_id(c):
            chk = check(st, c)
            assert chk is not None and chk["status"] != "passed", (c, chk)
        else:
            assert problems(st, c), (c, [(p["code"], p["cells"]) for p in tr["problems"]])
    if case.expect == "rejected":
        structural = {p["code"] for p in tr["problems"] if p["category"] in ("structure", "recipe")}
        assert structural == {c for c in case.codes if not _check_id(c)}, structural
    if code in REJECT_FIX:
        fixes = {p["fix"] for p in problems(st, case.codes[0])}
        assert fixes and fixes <= REJECT_FIX[code], fixes
    if code in REJECT_CELLS:
        cells = {c for p in problems(st, case.codes[0]) for c in p["cells"]}
        assert REJECT_CELLS[code] <= cells, cells
        if code == "D21":
            assert all(c.startswith(f"{SHEET}!") and c.endswith("32") for c in cells), cells
    # 说明片段（D26 的片段是接受之后重新生成的，在 _extra_D26 里查）
    if case.expect != "decision":
        comments = all_comments(st)
        for frag in case.note_has:
            assert frag in comments, (frag, st["trial"]["notes"])
        for frag in case.note_lacks:
            assert frag not in comments, (frag, st["trial"]["notes"])

    committed = False
    if code in EXTRA:
        committed = bool(await EXTRA[code](client, {"st": st, "base": base, "source_id": source_id, "case": case}))

    # 能否提交
    if case.expect == "rejected":
        assert st["status"] == "rejected"
        resp = await commit(client, st)
        assert resp.status_code == 409 and resp.json()["code"] == "trial_not_passed", resp.text
        assert await current_snapshot(source_id) == snap_before, "拒收不动当前快照"
    elif not committed:
        if case.confirms:
            resp = await commit(client, st, confirmations=[])
            assert resp.status_code == 422 and resp.json()["code"] == "confirm_required", resp.text
            for item in confirm_ids(st):
                assert item in resp.json()["detail"], (item, resp.json()["detail"])
            assert await current_snapshot(source_id) == snap_before
        resp = await commit(client, st)
        assert resp.status_code == 201, resp.text
        out = resp.json()
        assert out["unchanged"] is False and out["recipe_id"] == base["commit"]["recipe_id"]
        assert await current_snapshot(source_id) == out["snapshot_id"] != snap_before


# ==========================================================================
# 9.3 实验室难缠用例
# ==========================================================================


def blocks_of(rec: dict[str, Any]) -> list[dict[str, Any]]:
    return [b for s in rec["sheets"] for b in s["blocks"]]


async def test_9_3_c01_literal_total(client):
    """写死合计：列表块 + total_row(pick=合计, keep_as)；passed；T1 passed；确认项含 outside_digits ×2（C1、C3）。"""
    raw, fn = lab.c01("literal")
    staged = await stage(client, raw, fn)
    blocks = blocks_of(staged["recipe"])
    assert [b["layout"] for b in blocks] == ["list"]
    total = blocks[0]["rows"]["total_row"]
    assert total is not None and total["pick"] == "合计" and total["keep_as"], total
    st = await trial(client, staged["id"])
    assert st["trial"]["status"] == "passed", st["trial"]["problems"]
    assert check(st, "T1")["status"] == "passed"
    outside = sorted(i for i in confirm_ids(st) if i.startswith("outside_digits:"))
    assert outside == ["outside_digits:月报!C1", "outside_digits:月报!C3"]
    resp = await commit(client, st)
    assert resp.status_code == 201, resp.text


async def test_9_3_c01_formula_without_cache(client):
    """公式无缓存：needs_decision，T1 unverifiable（可接受）；接受后提交，表内合计值为 NULL，说明写「未能核对」。"""
    raw, fn = lab.c01("uncached")
    staged = await stage(client, raw, fn)
    st = await trial(client, staged["id"])
    tr = st["trial"]
    assert tr["status"] == "needs_decision", tr["problems"]
    t1 = check(st, "T1")
    assert t1["status"] == "unverifiable" and t1["acceptable"] is True
    assert t1["reasons"].get("uncached"), t1
    assert tr["acceptable"] == ["T1"]
    resp = await commit(client, st)
    assert resp.status_code == 422 and resp.json()["code"] == "acceptance_required", resp.text
    resp = await commit(client, st, acceptances=[{"check_id": "T1", "reason": "合成理由：导出工具不保存计算结果"}])
    assert resp.status_code == 201, resp.text
    out = resp.json()
    total_table = blocks_of(staged["recipe"])[0]["rows"]["total_row"]["keep_as"]
    db = await snapshot_db(out["snapshot_id"])
    rows = sql(db, f'SELECT "销量", "金额" FROM "{total_table}"')
    assert rows and all(v is None for r in rows for v in r), rows
    imp = await get(TableImport, out["import_id"])
    assert [w["check_id"] for w in imp.waivers or []] == ["T1"]
    comment = (await schema_of(client, out["source"]["id"], total_table))["comment"] or ""
    assert "未能核对" in comment and "一致" not in comment, comment


async def test_9_3_c01_formula_with_cache(client):
    raw, fn = lab.c01("cached")
    staged = await stage(client, raw, fn)
    st = await trial(client, staged["id"])
    assert st["trial"]["status"] == "passed", st["trial"]["problems"]
    assert check(st, "T1")["status"] == "passed"


def _option_with(question: dict[str, Any], value: str) -> str:
    """选项里 effects 把某个字段设成 value 的那个选项值。"""
    for opt in question["options"]:
        if value in json.dumps(question.get("effects", {}).get(opt["value"], []), ensure_ascii=False):
            return opt["value"]
    raise AssertionError(f"问题 {question['id']} 没有设成「{value}」的选项：{question['options']}")


async def test_9_3_c04_merged_cells(client):
    """合并单元格：header_rows=2，列表头「2026年上半年_销量」等；merged_data=fill 的问题；回答后 passed，确认项
    merged_fill:*；卡片提示表头含年份。"""
    raw, fn = lab.c04()
    staged = await stage(client, raw, fn)
    assert staged["recipe"] is not None, staged["draft"]["failures"]
    blocks = blocks_of(staged["recipe"])
    assert [b["layout"] for b in blocks] == ["list"]
    block = blocks[0]
    assert block["header_rows"] == 2
    headers = [c["header"] for c in block["columns"]]
    assert headers == ["地区", "产品", "2026年上半年_销量", "2026年上半年_金额", "2026年下半年_销量",
                       "2026年下半年_金额"], headers
    assert block["merged_data"] == "fill"
    assert any("年份" in (c["title"] + c["reason"]) for c in staged["cards"]), staged["cards"]
    merged_q = [q for q in staged["questions"] if "fill" in json.dumps(q.get("effects", {}), ensure_ascii=False)]
    assert merged_q, [q["id"] for q in staged["questions"]]
    st = await answer(client, staged["id"], {merged_q[0]["id"]: {"value": _option_with(merged_q[0], "fill")}})
    st = await trial(client, st["id"])
    assert st["trial"]["status"] == "passed", st["trial"]["problems"]
    assert [i for i in confirm_ids(st) if i.startswith("merged_fill:")], confirm_ids(st)
    assert rows_of(st)[block["table"]] == 6


@pytest.mark.parametrize(("variant", "status", "code"), [
    ("openpyxl", "rejected", "formula_uncached"),
    ("xlsxwriter", "rejected", "formula_full_calc"),
    ("有缓存", "passed", None),
])
async def test_9_3_c05_formulas(client, variant, status, code):
    raw, fn = lab.c05(variant)
    staged = await stage(client, raw, fn)
    st = await trial(client, staged["id"])
    tr = st["trial"]
    assert tr["status"] == status, tr["problems"]
    if code is not None:
        assert problems(st, code), [(p["code"], p["cells"]) for p in tr["problems"]]
        assert problems(st, code)[0]["category"] == "structure"
    else:
        assert tr["receipt"]["formula_cells_accepted"] == 3
        assert [i for i in confirm_ids(st) if i.startswith("formula_cached:")], confirm_ids(st)


async def test_9_3_c06_hidden_rows_cols_and_sheets(client):
    """默认 rejected：hidden_rows（提示可能是筛选）、hidden_cols；隐藏工作表列在 skipped_hidden 且不读；
    回答 include → passed；rows=exclude → passed、行数减少、排除的行列在回执。"""
    raw, fn = lab.c06()
    staged = await stage(client, raw, fn)
    assert [g["sheet"] for g in staged["grids"]] == ["明细"], "隐藏工作表不读"
    assert {(s["sheet"], s["state"]) for s in staged["skipped_sheets"]} == {("草稿", "hidden"), ("配置", "veryHidden")}
    st = await trial(client, staged["id"])
    tr = st["trial"]
    assert tr["status"] == "rejected"
    hr = problems(st, "hidden_rows")
    assert hr and "筛选" in hr[0]["message"], hr
    assert problems(st, "hidden_cols")
    skipped = tr["receipt"]["sheets"]["skipped_hidden"]
    assert {(s["sheet"], s["state"]) for s in skipped} == {("草稿", "hidden"), ("配置", "veryHidden")}
    assert [x["sheet"] for x in tr["receipt"]["ledger"]] == ["明细"]

    q = next(q for q in staged["questions"] if q["id"].startswith("q_hidden:"))
    assert q["default"] is None, "6.1 第 12 条：隐藏行列的问题没有默认值"
    st = await answer(client, staged["id"], {q["id"]: {"value": "include"}})
    st = await trial(client, st["id"])
    assert st["trial"]["status"] == "passed", st["trial"]["problems"]
    full_rows = rows_of(st)["明细"]
    assert full_rows == 10
    assert "hidden:明细" in confirm_ids(st)

    st = await answer(client, staged["id"], {q["id"]: {"value": "exclude"}})
    assert st["recipe"]["sheets"][0]["hidden"]["rows"] == "exclude"
    st = await trial(client, st["id"])
    tr = st["trial"]
    assert tr["status"] == "passed", tr["problems"]
    assert rows_of(st)["明细"] == full_rows - 6 < full_rows
    hidden = tr["receipt"]["hidden"]["明细"]
    assert hidden["rows"] == [3, 4, 5, 7, 9, 11] and hidden["cols"] == ["C"], hidden
    led = tr["receipt"]["ledger"][0]
    assert led["roles"].get("hidden_excluded") == 6 * 4, led
    assert "hidden:明细" in confirm_ids(st)


async def test_9_3_c08_stacked_tables(client):
    """上下两表：两个列表块；passed；标题是区域外文字。"""
    raw, fn = lab.c08("stacked")
    staged = await stage(client, raw, fn)
    assert [b["layout"] for b in blocks_of(staged["recipe"])] == ["list", "list"]
    st = await trial(client, staged["id"])
    assert st["trial"]["status"] == "passed", st["trial"]["problems"]
    outside = {(o["cell"], o["text"], o["kind"]) for o in st["trial"]["receipt"]["outside_text"]}
    assert {("汇总!B1", "一、销售", "text"), ("汇总!B7", "二、费用", "text")} <= outside
    assert sorted(rows_of(st).values()) == [3, 3]


async def test_9_3_c08_blank_row_in_the_middle(client):
    """中间空行：起草 blank_rows=skip + 卡片；passed，blank_rows_skipped=1；改成 stop → rejected（空行下面的数据没有去处）。"""
    raw, fn = lab.c08("gap")
    staged = await stage(client, raw, fn)
    blocks = blocks_of(staged["recipe"])
    assert len(blocks) == 1 and blocks[0]["rows"]["blank_rows"] == "skip"
    assert any("空行" in (c["title"] + c["reason"]) for c in staged["cards"]), staged["cards"]
    st = await trial(client, staged["id"])
    assert st["trial"]["status"] == "passed", st["trial"]["problems"]
    assert st["trial"]["receipt"]["blank_rows_skipped"] == 1
    assert rows_of(st)[blocks[0]["table"]] == 4

    rec = json.loads(json.dumps(staged["recipe"], ensure_ascii=False))
    rec["sheets"][0]["blocks"][0]["rows"]["blank_rows"] = "stop"
    st = await put_recipe(client, staged["id"], rec)
    st = await trial(client, st["id"])
    tr = st["trial"]
    assert tr["status"] == "rejected", tr["problems"]
    structural = [p for p in tr["problems"] if p["category"] == "structure"]
    cells = {c for p in structural for c in p["cells"]}
    assert {"明细!C5", "明细!C6"} <= cells, [(p["code"], p["cells"]) for p in tr["problems"]]


async def test_9_3_c08_side_by_side(client):
    raw, fn = lab.c08("side")
    staged = await stage(client, raw, fn)
    assert [b["layout"] for b in blocks_of(staged["recipe"])] == ["list", "list"]
    st = await trial(client, staged["id"])
    assert st["trial"]["status"] == "passed", st["trial"]["problems"]
    assert sorted(rows_of(st).values()) == [2, 2]


async def test_9_3_c10_drift(client):
    """以 07 起草并启用；08（表头挪到 C7、多「单价」）rejected column_extra；09（表头外观差异）passed +
    diff:header_writing:*；10（「金额」改名「销售额」）rejected column_missing + column_extra。"""
    raw, fn = lab.c10(7)
    staged = await stage(client, raw, fn)
    st = await trial(client, staged["id"])
    assert st["trial"]["status"] == "passed", st["trial"]["problems"]
    resp = await commit(client, st)
    assert resp.status_code == 201, resp.text
    out = resp.json()
    source_id = out["source"]["id"]

    raw, fn = lab.c10(8)
    st = await reupload(client, source_id, raw, fn)
    assert st["trial"]["status"] == "rejected", st["trial"]["problems"]
    assert problems(st, "column_extra"), [(p["code"], p["cells"]) for p in st["trial"]["problems"]]
    await client.delete(f"/api/datasources/imports/{st['id']}")

    raw, fn = lab.c10(9)
    st = await reupload(client, source_id, raw, fn)
    tr = st["trial"]
    assert tr["status"] == "passed", tr["problems"]
    assert "header_writing" in {d["kind"] for d in tr["diff"]}, tr["diff"]
    assert [i for i in confirm_ids(st) if i.startswith("diff:header_writing:")], confirm_ids(st)
    await client.delete(f"/api/datasources/imports/{st['id']}")

    raw, fn = lab.c10(10)
    st = await reupload(client, source_id, raw, fn)
    assert st["trial"]["status"] == "rejected", st["trial"]["problems"]
    codes = {p["code"] for p in st["trial"]["problems"]}
    assert {"column_missing", "column_extra"} <= codes, codes
    assert await current_snapshot(source_id) == out["snapshot_id"]
