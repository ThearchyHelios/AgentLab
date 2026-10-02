"""按配方导入的端到端验收（期 2，WP-7；P2-SPEC 9.7 的 13 步）。

**流程一律经 HTTP 接口**（`AsyncClient(transport=ASGITransport(app=app))`）：暂存、回答、改配方、AI 起草、
试运行、提交、上传新一期、清除原件、发起运行、恢复运行、取工件、试跑工具，全部打真实的路由，管线是
`default_pipeline()` 接的真模块。只有接口不提供的观察点（回收、过期时间、构建 id 的期望值、快照的库哈希、
运行固定的版本）才直接读库或调模块函数，而且只读、不替流程做任何一步。

夹具：tests/fixtures/xlsx 的合成表格（假名、随机数），AI 用假模型（monkeypatch `recipe_ai.resolve_draft_model`），
不调用任何真实模型，不读 .env。

这份文件是验收：断言照规格写，跑出来失败的用例保留失败，不放宽断言、不标 xfail，根因在交付说明里逐条写。
"""
from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import json
import uuid
from pathlib import Path
from typing import Any, Callable

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, update

from app.core.config import settings
from app.data import raw_store, recipe, table_versions
from app.data.recipe_types import prose_problems
from app.db.base import SessionLocal
from app.db.models import DataSource, ImportStaging, Run, SourceSnapshot, TableImport, TableRecipe
from app.engine.runner import run_manager
from app.main import app
from tests.fixtures.xlsx.drift import AUG, DRIFT_CASES, OCT, SEP, DriftCase
from tests.fixtures.xlsx.flow import FLOW_EXPECT, flow_workbook

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
API = "/api/datasources"
FLOW_RECIPE = json.loads((Path(__file__).parent / "fixtures" / "recipes" / "flow_recipe.json").read_text("utf-8"))
#: 基线和各漂移用例共用的随机种子：D00 用例与基线逐字节相同，就是「同一文件再传」
SEED = 5


# ==========================================================================
# 夹具
# ==========================================================================


@pytest.fixture(autouse=True)
def store(monkeypatch, tmp_path):
    """每个测试一个独立的数据目录：上传目录、原件存档、工件、试运行库都在里面（库是会话级的临时库）。"""
    data = tmp_path / "data"
    (data / "uploads").mkdir(parents=True)
    monkeypatch.setattr(settings, "data_dir", data)
    yield data


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest.fixture
async def engine_up(store):
    """发起运行要的引擎（checkpoint 库跟着 data_dir 走，所以依赖 store）。"""
    await run_manager.setup()
    yield
    await run_manager.shutdown()


# ==========================================================================
# 小工具（全部经 HTTP）
# ==========================================================================


def unique(prefix: str = "e2e") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


async def stage(client, name: str, raw: bytes, filename: str, **form: str):
    return await client.post(f"{API}/imports/stage", files={"file": (filename, raw, XLSX)},
                             data={"name": name, **form})


async def reupload(client, source_id: str, raw: bytes, filename: str, **form: str):
    return await client.post(f"{API}/{source_id}/reupload", files={"file": (filename, raw, XLSX)}, data=form)


async def trial(client, sid: str, **body: Any):
    return await client.post(f"{API}/imports/{sid}/trial", json=body)


async def commit(client, st: dict[str, Any], confirmations: list[str] | None = None,
                 acceptances: list[dict[str, str]] | None = None, **extra: Any):
    body: dict[str, Any] = {"trial_id": st["trial"]["trial_id"],
                            "confirmations": all_confirms(st) if confirmations is None else confirmations}
    if acceptances is not None:
        body["acceptances"] = acceptances
    body.update(extra)
    return await client.post(f"{API}/imports/{st['id']}/commit", json=body)


def all_confirms(st: dict[str, Any]) -> list[str]:
    return [i["id"] for i in (st.get("trial") or {}).get("confirm_items") or []]


def rename_wide(rec: dict[str, Any], old: str, new: str) -> dict[str, Any]:
    """把规则起草的宽表名改成 new：表名、写入它的分段（id 一起改）、关系里引用的表名。"""
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


async def query(client, source_name: str, sql: str) -> list[list[Any]]:
    """工具库「试一下」：上传表格查当前版本（POST /api/tools/db_query__名/run）。"""
    resp = await client.post(f"/api/tools/db_query__{source_name}/run", json={"args": {"sql": sql}})
    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert out["ok"], out
    return json.loads(out["result"])["rows"]


async def count(client, source_name: str, table: str, where: str = "") -> int:
    rows = await query(client, source_name, f'SELECT COUNT(*) AS n FROM "{table}"{where}')
    return int(rows[0][0])


async def schema(client, source_id: str, table: str) -> dict[str, Any]:
    resp = await client.get(f"{API}/{source_id}/schema", params={"table": table})
    assert resp.status_code == 200, resp.text
    return resp.json()


async def source_row(client, source_id: str) -> dict[str, Any]:
    resp = await client.get(API)
    assert resp.status_code == 200, resp.text
    return next(s for s in resp.json() if s["id"] == source_id)


async def artifact(client, artifact_id: str) -> dict[str, Any]:
    resp = await client.get(f"/api/artifacts/{artifact_id}")
    assert resp.status_code == 200, resp.text
    return resp.json()["content"]


async def db_get(model, key):
    async with SessionLocal() as session:
        return await session.get(model, key)


def notes_text(trial_or_notes: dict[str, Any]) -> str:
    """说明预览（TrialOut.notes）的全部文字：表说明 + 列说明。"""
    notes = trial_or_notes.get("notes", trial_or_notes)
    parts = []
    for t in (notes or {}).values():
        parts.append(t.get("comment") or "")
        parts.extend((t.get("columns") or {}).values())
    return "\n".join(parts)


async def schema_notes(client, source_id: str, tables: list[str]) -> str:
    parts = []
    for t in tables:
        s = await schema(client, source_id, t)
        parts.append(s.get("comment") or "")
        parts.extend(c.get("comment") or "" for c in s.get("columns") or [])
    return "\n".join(parts)


def _ref(o: dict[str, Any]) -> str:
    """OutsideText 的坐标：cell 已带工作表名（「客流汇总!B3」）时原样用，否则拼上 sheet。"""
    return o["cell"] if "!" in o["cell"] else f"{o['sheet']}!{o['cell']}"


def answer_all(questions: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """D00 的问题：关系一律登记，占位符存为空值（9.1 的期望配方）。"""
    out = {}
    for q in questions:
        values = [o["value"] for o in q["options"]]
        if q["id"].startswith("q_relation"):
            out[q["id"]] = {"value": "register"}
        elif q["id"] == "q_mode":
            # 期 3：q_mode 的 values[0] 是「按期累积」，配方哈希就不再等于参考配方。期 2 的用例都按每期替换走
            # （P3-SPEC 12.0、1.5）；要累积的用例显式答 accumulate
            out[q["id"]] = {"value": "replace"}
        elif "null" in values:
            out[q["id"]] = {"value": "null"}
        else:
            out[q["id"]] = {"value": values[0]}
    return out


async def first_import(client, name: str, *, seed: int = SEED, start: dt.date = AUG, days: int = 31,
                       variant: str = "D00", signed_by: str = "验收员甲") -> dict[str, Any]:
    """第 1、2 步：stage → 回答 → 改宽表名（PUT）→ trial → commit（勾全部确认项）。返回各步的响应。"""
    raw, fn = flow_workbook(start, days, seed=seed, variant=variant)
    resp = await stage(client, name, raw, fn)
    assert resp.status_code == 201, resp.text
    staged = resp.json()
    sid = staged["id"]
    resp = await client.post(f"{API}/imports/{sid}/answers", json={"answers": answer_all(staged["questions"])})
    assert resp.status_code == 200, resp.text
    answered = resp.json()
    wide = next(t["name"] for t in answered["recipe"]["tables"] if t["name"] not in ("时段客流", "时段客流_表内合计"))
    resp = await client.put(f"{API}/imports/{sid}/recipe", json={"recipe": rename_wide(answered["recipe"], wide, "日客流")})
    assert resp.status_code == 200, resp.text
    put = resp.json()
    resp = await trial(client, sid)
    assert resp.status_code == 200, resp.text
    tried = resp.json()
    assert tried["trial"]["status"] == "passed", tried["trial"]["problems"]
    resp = await commit(client, tried, signed_by=signed_by)
    assert resp.status_code == 201, resp.text
    return {"raw": raw, "file_name": fn, "staged": staged, "answered": answered, "put": put, "trial": tried,
            "commit": resp.json()}


# ==========================================================================
# 第 1、2 步：首次导入
# ==========================================================================


async def test_step01_02_first_import_matches_the_reference_recipe_and_9_1(client):
    name = unique()
    raw, fn = flow_workbook(AUG, 31, seed=101)
    resp = await stage(client, name, raw, fn)
    assert resp.status_code == 201, resp.text
    st = resp.json()
    sid = st["id"]
    # 第 1 步：规则草稿完整、卡片 10 张、不提供 AI
    assert st["kind"] == "first" and st["status"] == "drafting"
    assert st["draft"]["complete"] is True
    assert len(st["cards"]) == 10
    assert all(c.get("reason") for c in st["cards"]), "每张卡片都有理由"
    assert st["ai"]["offered"] is False
    assert st["recipe_problems"] == []
    assert st["sheets"][0]["nonempty"] == FLOW_EXPECT["nonempty"]
    assert st["sheets"][0]["bounds"] == FLOW_EXPECT["bounds"]
    assert st["sheets"][0]["merged"] == FLOW_EXPECT["merged"]
    assert st["sheets"][0]["formulas"] == FLOW_EXPECT["formulas"]
    # 问题没有默认选中
    assert all(q.get("default") in (None, "") for q in st["questions"])

    resp = await client.post(f"{API}/imports/{sid}/answers", json={"answers": answer_all(st["questions"])})
    assert resp.status_code == 200, resp.text
    st = resp.json()
    assert set(st["answers"]) == {q["id"] for q in st["questions"]}
    answered = st["answers"]
    wide = next(t["name"] for t in st["recipe"]["tables"] if t["name"] not in ("时段客流", "时段客流_表内合计"))
    resp = await client.put(f"{API}/imports/{sid}/recipe", json={"recipe": rename_wide(st["recipe"], wide, "日客流")})
    assert resp.status_code == 200, resp.text
    st = resp.json()
    # 期 3（第 8 节遗留项 2）：改名之后的配方仍然体现每一个回答，回答照旧保留，没有被丢弃的
    assert st["recipe_problems"] == [] and st["answers"] == answered and st["answers_dropped"] == []
    ref, ref_problems = recipe.validate_recipe(FLOW_RECIPE, origin="manual")
    mine, mine_problems = recipe.validate_recipe(st["recipe"], origin="manual")
    assert ref_problems == [] and mine_problems == []
    assert recipe.recipe_sha256(mine) == recipe.recipe_sha256(ref), "回答 + 改名之后应与参考配方逐字相同"

    # 第 2 步：trial → passed，回执与 9.1 一致
    resp = await trial(client, sid)
    assert resp.status_code == 200, resp.text
    st = resp.json()
    t = st["trial"]
    assert st["status"] == "trialed" and t["status"] == "passed", t["problems"]
    rc = t["receipt"]
    ledger = rc["ledger"][0]
    assert ledger["nonempty_scan"] == ledger["nonempty_read"] == FLOW_EXPECT["nonempty"], "两遍一致"
    assert ledger["unclaimed"] == 0
    assert {k: v for k, v in ledger["roles"].items() if v} == FLOW_EXPECT["ledger"]
    assert {x["name"]: x["rows"] for x in rc["tables"]} == FLOW_EXPECT["rows"]
    assert rc["placeholders"] == FLOW_EXPECT["placeholders"]
    assert rc["canonicalized_total"] == FLOW_EXPECT["canonicalized"]
    assert [(_ref(o), o["text"], o["kind"]) for o in rc["outside_text"]] == FLOW_EXPECT["outside_text"]
    period = rc["period"]
    assert period["source"] == "cells" and (period["start"], period["end"]) == ("2026-08-01", "2026-08-31")
    assert len(period["texts"]) == 1 and next(iter(period["texts"])).endswith("B2"), period["texts"]
    assert period["annotated"] == {}
    checks = {c["id"]: c for c in t["checks"]}
    assert {k: c["status"] for k, c in checks.items()} == FLOW_EXPECT["checks"]
    assert checks["K1"]["checked"] == 93, "K1 核对 3 × 31 格"
    assert checks["R1"]["checked"] == 31 and checks["R1"]["failed"] == 0
    assert checks["C1"]["checked"] == 1, "C1 一处"
    assert checks["R2"]["checked"] == 31 and "31 天中 0 天相等" in " ".join(checks["R2"]["details"])
    assert set(all_confirms(st)) == set(FLOW_EXPECT["confirms"]), "确认项是精确集合"
    assert t["diff"] is None and t["same_as_import"] is None
    for table, note in t["notes"].items():
        assert prose_problems(note["comment"], known={table, *note["columns"]}) == [], table
    assert "已逐日核对" in t["notes"]["日客流"]["comment"]
    assert "本期原表附有说明文字" not in notes_text(t)
    assert t["notes"]["日客流"]["columns"]["全日客流"] == "单位：人次"

    resp = await commit(client, st, signed_by="验收员甲")
    assert resp.status_code == 201, resp.text
    out = resp.json()
    src = out["source"]
    assert src["name"] == name and src["import_mode"] == "recipe" and src["origin"] == "upload"
    # 当前配方就是参考配方
    resp = await client.get(f"{API}/{src['id']}/recipe")
    assert resp.status_code == 200, resp.text
    assert resp.json()["recipe_sha256"] == recipe.recipe_sha256(ref)
    # 快照里三张表的行数（经工具库试跑，查的是当前版本）
    for table, n in FLOW_EXPECT["rows"].items():
        assert await count(client, name, table) == n, table
    # D11：7-8 时段整行是「·」，31 行保留为空值行
    assert await count(client, name, "时段客流", " WHERE \"时段\" = '7-8' AND \"客流\" IS NULL") == 31
    s = await schema(client, src["id"], "日客流")
    assert s["found"] and "已逐日核对" in (s["comment"] or "")
    col = next(c for c in s["columns"] if c["name"] == "全日客流")
    assert col["comment"] == "单位：人次"
    # 主键：STRICT + PRIMARY KEY（grain）
    assert [c["name"] for c in s["columns"] if c["pk"]] == ["日期"]


# ==========================================================================
# 第 3 步：运行固定版本
# ==========================================================================


def _node(nid, ntype, **config):
    return {"id": nid, "type": ntype, "position": {"x": 0, "y": 0}, "data": {"label": nid, "config": config}}


def _chain(*nodes):
    return {"nodes": list(nodes), "edges": [{"source": a["id"], "target": b["id"]} for a, b in zip(nodes, nodes[1:])]}


async def _wait_run(client, run_id: str, statuses: tuple[str, ...], timeout: float = 30.0) -> dict[str, Any]:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        resp = await client.get(f"/api/runs/{run_id}")
        if resp.status_code == 200 and resp.json()["status"] in statuses and run_manager._tasks.get(run_id) is None:
            return resp.json()
        await asyncio.sleep(0.05)
    raise AssertionError(f"运行 {run_id} 没有进入 {statuses}")


def _rows_of(value: Any) -> list[list[Any]]:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return json.loads(text[text.index("{"):])["rows"]


async def test_step03_run_started_on_d00_keeps_reading_d00_after_reupload_d01_is_enabled(client, engine_up):
    name = unique()
    done = await first_import(client, name, seed=103)
    source_id, s1 = done["commit"]["source"]["id"], done["commit"]["snapshot_id"]
    sql = 'SELECT COUNT(*) AS n FROM "日客流"'
    graph = _chain(
        _node("start", "input"),
        _node("t1", "tool", tool=f"db_query__{name}", args={"sql": sql}),
        _node("gate", "human", mode="approve", title="看一眼"),
        _node("t2", "tool", tool=f"db_query__{name}", args={"sql": sql}),
        _node("out", "output", fields=[{"name": "t1", "value": "{{ nodes.t1 }}"},
                                       {"name": "t2", "value": "{{ nodes.t2 }}"}]),
    )
    resp = await client.post("/api/runs", json={"graph": graph, "input": {}})
    assert resp.status_code == 201, resp.text
    run_id = resp.json()["id"]
    await _wait_run(client, run_id, ("interrupted",))
    await run_manager.wait_idle(run_id)

    # 运行停在审批上：上传新一期 D01 并启用
    raw, fn = flow_workbook(SEP, 30, seed=1031)
    resp = await reupload(client, source_id, raw, fn)
    assert resp.status_code == 201, resp.text
    st = resp.json()
    assert st["trial"]["status"] == "passed", st["trial"]["problems"]
    resp = await commit(client, st)
    assert resp.status_code == 201, resp.text
    s2 = resp.json()["snapshot_id"]
    assert s2 != s1
    assert await count(client, name, "日客流") == 30, "当前版本已是 D01"

    resp = await client.post(f"/api/runs/{run_id}/resume", json={"response": {"approved": True}})
    assert resp.status_code == 200, resp.text
    done_run = await _wait_run(client, run_id, ("succeeded", "failed"))
    assert done_run["status"] == "succeeded", done_run["error"]
    assert _rows_of(done_run["output"]["t1"]) == [[31]]
    assert _rows_of(done_run["output"]["t2"]) == [[31]], "恢复之后查到了新一期，运行没有固定在 D00 的快照上"
    row = await db_get(Run, run_id)
    assert row.data_versions == {source_id: {"snapshot": s1, "name": name}}


# ==========================================================================
# 第 4 步：31 例漂移（30 例 + D28b）
# ==========================================================================


def _match(actual: str, pattern: str) -> bool:
    return actual.startswith(pattern[:-1]) if pattern.endswith("*") else actual == pattern


def _confirm_diff(actual: list[str], expected: tuple[str, ...]) -> tuple[list[str], list[str]]:
    missing = [p for p in expected if not any(_match(a, p) for a in actual)]
    extra = [a for a in actual if not any(_match(a, p) for p in expected)]
    return missing, extra


def _labels(rc: dict[str, Any], segment: str) -> dict[str, Any]:
    return next(x for x in rc.get("labels") or [] if x["segment"] == segment)


def _cell_endswith(cells: list[str], ref: str) -> bool:
    return any(c == ref or c.endswith(f"!{ref}") for c in cells or [])


#: 9.2「其他关键结果」一栏里，单看回执就能断言的部分
def _extra_d23(t):
    assert {c["id"]: c["status"] for c in t["checks"]}["C2"] == "info", "D23：C2 passed → info"


def _extra_d25(t):
    k1 = next(c for c in t["checks"] if c["id"] == "K1")
    assert k1["status"] == "passed" and "保存值与重算一致" in json.dumps(k1, ensure_ascii=False), k1


def _extra_annotated_b3(t):
    ann = t["receipt"]["period"]["annotated"]
    assert any(k.endswith("B3") for k in ann), f"B3 应进 annotated：{ann}"


def _extra_d27(t):
    _extra_annotated_b3(t)
    cells = t["receipt"]["period"]["cells"]
    assert any(c.endswith("B2") for c in cells) and any(c.endswith("B3") for c in cells), f"C1 两处一致：{cells}"
    assert {c["id"]: c["status"] for c in t["checks"]}["C1"] == "passed"


def _extra_d28b(t):
    ann = t["receipt"]["period"]["annotated"]
    assert any(k.endswith("B32") for k in ann), f"B32 应进 annotated：{ann}"
    cells = t["receipt"]["period"]["cells"]
    assert any(c.endswith("B2") for c in cells) and any(c.endswith("B32") for c in cells), f"C1 两处一致：{cells}"


def _extra_d07(t):
    ids = {c["id"]: c["status"] for c in t["checks"]}
    assert ids.get("K1") == "passed" and "G1" not in ids, ids


def _extra_d11(t):
    assert _labels(t["receipt"], "日间")["canonical"][0] == "7-8"


def _extra_d22(t):
    assert "18-22时合计" in _labels(t["receipt"], "夜间合计")["canonical"]


def _extra_d08(t):
    k1 = next(c for c in t["checks"] if c["id"] == "K1")
    assert k1["status"] == "mismatch" and k1["category"] == "structure", k1
    assert _cell_endswith(k1["cells"], "G28"), f"K1 应定位到 G28：{k1['cells']}"


def _extra_d26(t):
    r1 = next(c for c in t["checks"] if c["id"] == "R1")
    assert r1["status"] == "mismatch" and r1["category"] == "data_quality" and r1["acceptable"] is True, r1
    assert t["acceptable"] == ["R1"]


EXTRA: dict[str, Callable[[dict[str, Any]], None]] = {
    "D23": _extra_d23, "D25": _extra_d25, "D17": _extra_annotated_b3, "D27": _extra_d27, "D28b": _extra_d28b,
    "D07": _extra_d07, "D11": _extra_d11, "D22": _extra_d22, "D08": _extra_d08, "D26": _extra_d26,
}


@pytest.mark.parametrize("code", list(DRIFT_CASES))
async def test_step04_drift_case(client, code):
    case: DriftCase = DRIFT_CASES[code]
    name = unique(f"d_{code.lower()}")
    base = await first_import(client, name, seed=SEED)
    source_id = base["commit"]["source"]["id"]
    base_snapshot = base["commit"]["snapshot_id"]
    raw, fn = case.build(SEED)
    resp = await reupload(client, source_id, raw, fn)
    assert resp.status_code == 201, resp.text
    st = resp.json()
    t = st["trial"]
    problems = {p["code"] for p in t["problems"]}
    failing = {c["id"] for c in t["checks"] if c["status"] in ("mismatch", "unverifiable")}
    status_want = {"passed": "passed", "confirm": "passed", "rejected": "rejected", "decision": "needs_decision"}
    assert t["status"] == status_want[case.expect], (code, t["status"], t["problems"],
                                                     [(c["id"], c["status"]) for c in t["checks"]])
    for want in case.codes:
        assert want in problems or want in failing, (code, want, problems, failing)
    if code in EXTRA:
        EXTRA[code](t)

    if case.expect == "rejected":
        # 拒收不动当前快照；也没有「启用」可点（提交 409 trial_not_passed）
        assert (await source_row(client, source_id))["current_snapshot"]["id"] == base_snapshot
        resp = await commit(client, st, confirmations=[])
        assert resp.status_code == 409 and resp.json()["code"] == "trial_not_passed", resp.text
        assert (await client.delete(f"{API}/imports/{st['id']}")).status_code == 204
        return

    assert {x["name"]: x["rows"] for x in t["receipt"]["tables"]} == dict(zip(FLOW_EXPECT["rows"], case.rows))
    assert t["diff"] is not None
    assert {d["kind"] for d in t["diff"]} == set(case.diff_kinds), (code, [(d["kind"], d["label"]) for d in t["diff"]])
    ids = all_confirms(st)
    missing, extra = _confirm_diff(ids, case.confirms)
    assert not missing and not extra, (code, "缺", missing, "多", extra)
    # 差异卡里需确认的项都在确认清单里，并排在差异卡前面
    for d in t["diff"]:
        if d["requires_confirm"]:
            assert d["confirm_id"] in ids, (code, d)
    flags = [d["requires_confirm"] for d in t["diff"]]
    assert flags == sorted(flags, reverse=True), (code, "需确认的差异项应排在前面", flags)

    if code == "D00":
        # 同一文件再传：same_as_import，提交 unchanged，不新建记录
        assert t["same_as_import"] and t["same_as_import"]["id"] == base["commit"]["import_id"]
        resp = await commit(client, st)
        assert resp.status_code == 201, resp.text
        assert resp.json()["unchanged"] is True and resp.json()["import_id"] == base["commit"]["import_id"]
        return

    if case.expect == "decision":
        resp = await commit(client, st)
        assert resp.status_code == 422 and resp.json()["code"] == "acceptance_required", resp.text
        resp = await commit(client, st, acceptances=[{"check_id": "R1", "reason": "合成理由：闸机补录造成的差额"}])
        assert resp.status_code == 201, resp.text
        text = await schema_notes(client, source_id, list(FLOW_EXPECT["rows"]))
    else:
        text = notes_text(t)
        if ids:
            resp = await commit(client, st, confirmations=[])
            assert resp.status_code == 422 and resp.json()["code"] == "confirm_required", resp.text
            for i in ids:
                assert i in resp.json()["detail"], (code, i)
        resp = await commit(client, st)
        assert resp.status_code == 201, resp.text
        assert resp.json()["unchanged"] is False
    for frag in case.note_has:
        assert frag in text, (code, frag)
    for frag in case.note_lacks:
        assert frag not in text, (code, frag)
    assert await count(client, name, "日客流") == case.rows[0]


# ==========================================================================
# 第 5 步：D26 写理由接受
# ==========================================================================


async def test_step05_d26_acceptance_requires_a_reason_and_rewrites_the_note(client):
    name = unique()
    base = await first_import(client, name, seed=105)
    source_id = base["commit"]["source"]["id"]
    raw, fn = DRIFT_CASES["D26"].build(105)
    st = (await reupload(client, source_id, raw, fn)).json()
    t = st["trial"]
    assert t["status"] == "needs_decision" and t["acceptable"] == ["R1"], t["problems"]
    # 说明预览按「全部未接受」生成：R1 不成立，不能写「已逐日核对」
    assert "已逐日核对" not in notes_text(t)
    resp = await commit(client, st)
    assert resp.status_code == 422 and resp.json()["code"] == "acceptance_required", resp.text
    assert "R1" in resp.json()["detail"]
    resp = await commit(client, st, acceptances=[{"check_id": "R1", "reason": "   "}])
    assert resp.status_code == 422 and resp.json()["code"] == "acceptance_required", "空白理由不算理由"
    resp = await commit(client, st, acceptances=[{"check_id": "R1", "reason": "合成理由：第 10 日分区数据补录"}],
                        signed_by="验收员乙")
    assert resp.status_code == 201, resp.text
    s = await schema(client, source_id, "日客流")
    assert "已逐日核对" not in (s["comment"] or "")
    assert "已由用户确认接受" in (s["comment"] or "")
    imports = (await client.get(f"{API}/{source_id}/imports")).json()
    assert imports[0]["overrides"] == 1 and imports[0]["waivers"] == 0
    imp = await db_get(TableImport, resp.json()["import_id"])
    assert imp.overrides[0]["check_id"] == "R1" and imp.overrides[0]["signed_by"] == "验收员乙"


# ==========================================================================
# 第 6 步：统计期人工录入
# ==========================================================================


PERIOD_SEP = {"统计期": {"start": "2026-09-01", "end": "2026-09-30"}}


async def test_step06_human_period_input_end_to_end(client):
    name = unique()
    base = await first_import(client, name, seed=106)
    source_id = base["commit"]["source"]["id"]
    raw, fn = flow_workbook(SEP, 30, seed=106, variant="noperiod")
    resp = await reupload(client, source_id, raw, fn)
    assert resp.status_code == 201, resp.text
    st = resp.json()
    sid = st["id"]
    assert st["trial"]["status"] == "needs_input", st["trial"]["problems"]
    assert any(p["category"] == "input" for p in st["trial"]["problems"])
    # 不需要录入时不收；录入不合法 → 422 context_invalid
    resp = await trial(client, sid, context_inputs={"统计期": {"start": "2026-09-30", "end": "2026-09-01"}})
    assert resp.status_code == 422 and resp.json()["code"] == "context_invalid", resp.text
    resp = await trial(client, sid, context_inputs=PERIOD_SEP, signed_by="录入员甲")
    assert resp.status_code == 200, resp.text
    st = resp.json()
    t = st["trial"]
    assert t["status"] == "passed", t["problems"]
    assert t["receipt"]["period"]["source"] == "human"
    assert t["receipt"]["period"]["signed_by"] == "录入员甲"
    assert (t["receipt"]["period"]["start"], t["receipt"]["period"]["end"]) == ("2026-09-01", "2026-09-30")
    ids = all_confirms(st)
    assert "context_human:统计期" in ids
    assert "本期统计期为人工录入" in notes_text(t)
    resp = await commit(client, st)
    assert resp.status_code == 201, resp.text
    out = resp.json()
    imp = await db_get(TableImport, out["import_id"])
    assert imp.context["source"] == "human" and imp.context["signed_by"] == "录入员甲"
    assert "context_human:统计期" in {c["id"] for c in imp.confirmations}
    # 录入值计入构建哈希：与「同一原件、同一配方、不录入」的构建 id 不同
    rec = (await client.get(f"{API}/{source_id}/recipe")).json()
    raw_sha = hashlib.sha256(raw).hexdigest()
    with_input = table_versions.recipe_build_id(
        source_id, raw_sha, rec["recipe_sha256"], {"context": {"统计期": {"start": "2026-09-01", "end": "2026-09-30"}}})
    without = table_versions.recipe_build_id(source_id, raw_sha, rec["recipe_sha256"], {})
    assert out["build_id"] == with_input != without
    text = await schema_notes(client, source_id, list(FLOW_EXPECT["rows"]))
    assert "本期统计期为人工录入" in text
    # 导入清单（经 table_imports.manifest_artifact 取回）
    manifest = await artifact(client, imp.manifest_artifact)
    assert manifest["period"]["source"] == "human" and manifest["period"]["signed_by"] == "录入员甲"
    assert manifest["receipt"]["period"]["source"] == "human"
    assert manifest["receipt"]["period"]["signed_by"] == "录入员甲"
    assert "context_human:统计期" in {c["id"] for c in manifest["confirmations"]}

    # 能解析出统计期的文件不收录入：422 context_not_needed
    raw2, fn2 = flow_workbook(OCT, 31, seed=1061)
    st2 = (await reupload(client, source_id, raw2, fn2)).json()
    assert st2["trial"]["status"] == "passed", st2["trial"]["problems"]
    resp = await trial(client, st2["id"], context_inputs=PERIOD_SEP)
    assert resp.status_code == 422 and resp.json()["code"] == "context_not_needed", resp.text


# ==========================================================================
# 第 7 步：简单上传对配方源 409
# ==========================================================================


async def test_step07_simple_upload_to_a_recipe_source_is_refused(client):
    name = unique()
    base = await first_import(client, name, seed=107)
    resp = await client.post(f"{API}/upload", files={"file": (base["file_name"], base["raw"], XLSX)},
                             data={"name": name})
    assert resp.status_code == 409 and resp.json()["code"] == "recipe_source", resp.text
    # 再按配方导入同名 → 409 recipe_source_exists
    resp = await stage(client, name, base["raw"], base["file_name"])
    assert resp.status_code == 409 and resp.json()["code"] == "recipe_source_exists", resp.text


# ==========================================================================
# 第 8 步：回收
# ==========================================================================


async def test_step08_open_staging_raw_survives_gc_and_expired_staging_is_cleaned(client):
    name = unique()
    base = await first_import(client, name, seed=108)
    source_id = base["commit"]["source"]["id"]
    d00_sha = hashlib.sha256(base["raw"]).hexdigest()
    raw, fn = flow_workbook(SEP, 30, seed=1081)
    st = (await reupload(client, source_id, raw, fn)).json()
    assert st["status"] == "trialed", st["trial"]["problems"]
    sha = hashlib.sha256(raw).hexdigest()
    row = await db_get(ImportStaging, st["id"])
    trial_path = Path(row.trial_path)
    assert trial_path.is_file() and raw_store.raw_exists(sha)

    async with SessionLocal() as session:
        await table_versions.gc(session)
    async with SessionLocal() as session:
        await table_versions.startup(session)
    assert raw_store.raw_exists(sha), "未结束暂存区的原件不能被回收"
    assert trial_path.is_file(), "未结束暂存区的试运行库不能被清理"
    assert (await client.get(f"{API}/imports/{st['id']}")).json()["status"] == "trialed"

    # 过期：GET 时顺手过期 → 瘦身、删试运行库、原件没人要了随即删掉
    async with SessionLocal() as session:
        await session.execute(update(ImportStaging).where(ImportStaging.id == st["id"])
                              .values(expires_at=dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=1)))
        await session.commit()
    resp = await client.get(f"{API}/imports/{st['id']}")
    assert resp.status_code == 200
    out = resp.json()
    assert out["status"] == "expired" and out["grids"] == [] and out["trial"] is None
    assert not trial_path.exists()
    async with SessionLocal() as session:
        await table_versions.gc(session)
    assert not raw_store.raw_exists(sha), "过期暂存区的原件随后应被回收"
    # 写操作 409 staging_closed
    resp = await trial(client, st["id"])
    assert resp.status_code == 409 and resp.json()["code"] == "staging_closed"
    # 提交过的不受影响
    assert raw_store.raw_exists(d00_sha)
    assert (await source_row(client, source_id))["current_snapshot"]["id"] == base["commit"]["snapshot_id"]
    assert await count(client, name, "日客流") == 31


# ==========================================================================
# 第 9 步：重放幂等
# ==========================================================================


async def test_step09_replay_is_idempotent(client):
    name = unique()
    base = await first_import(client, name, seed=109)
    source_id = base["commit"]["source"]["id"]
    # 同一文件、同一配方 → 同一构建；same_as_import → unchanged
    st = (await reupload(client, source_id, base["raw"], base["file_name"])).json()
    assert st["trial"]["same_as_import"]["id"] == base["commit"]["import_id"]
    resp = await commit(client, st)
    assert resp.status_code == 201 and resp.json()["unchanged"] is True
    # 同一原件换个文件名：复用构建（D23），但要新建导入记录（文件名不同，不是 unchanged）
    st = (await reupload(client, source_id, base["raw"], "换个名字.xlsx")).json()
    assert st["trial"]["status"] in ("passed",), st["trial"]["problems"]
    assert st["trial"]["same_as_import"] is not None
    resp = await commit(client, st)
    assert resp.status_code == 201, resp.text
    out = resp.json()
    assert out["unchanged"] is False and out["build_reused"] is True
    assert out["build_id"] == base["commit"]["build_id"]

    # 同一录入再传 → 同一构建
    raw, fn = flow_workbook(SEP, 30, seed=109, variant="noperiod")
    builds = []
    for _ in range(2):
        st = (await reupload(client, source_id, raw, fn)).json()
        assert st["trial"]["status"] == "needs_input"
        st = (await trial(client, st["id"], context_inputs=PERIOD_SEP)).json()
        assert st["trial"]["status"] == "passed", st["trial"]["problems"]
        resp = await commit(client, st)
        assert resp.status_code == 201, resp.text
        builds.append(resp.json())
    assert builds[0]["build_id"] == builds[1]["build_id"]
    assert builds[1]["build_reused"] is True


# ==========================================================================
# 第 10 步：AI 起草全链路（假模型）
# ==========================================================================

KV_RECIPE = {
    "recipe_format": "agentlab-recipe/2",
    "sheets": [{"id": "s1", "match": {"name": "基本情况"}, "blocks": [{
        "id": "列表1", "layout": "list", "table": "基本情况", "after_title": "基本情况登记表",
        "columns": [{"header": "单位名称", "name": "项目", "type": "TEXT"},
                    {"header": "示例单位甲", "name": "取值", "type": "TEXT"}]}]}],
    "tables": [{"name": "基本情况"}],
}


async def test_step10_ai_draft_full_chain_with_fake_model(client, monkeypatch):
    from langchain_core.messages import AIMessage

    from app.data import recipe_ai
    from app.data.recipe_types import AiAvailability
    from tests.fixtures.xlsx.lab import kv_form

    calls: list[list[Any]] = []

    class FakeModel:
        async def ainvoke(self, messages):
            calls.append(list(messages))
            return AIMessage(content=json.dumps(KV_RECIPE, ensure_ascii=False),
                             usage_metadata={"input_tokens": 2100, "output_tokens": 300, "total_tokens": 2400})

    async def resolve(session):
        return FakeModel(), "fake-model", AiAvailability(True, model="fake-model", provider="假接入")

    monkeypatch.setattr(recipe_ai, "resolve_draft_model", resolve)
    raw, fn = kv_form()
    name = unique()
    resp = await stage(client, name, raw, fn)
    assert resp.status_code == 201, resp.text
    st = resp.json()
    sid = st["id"]
    assert st["draft"]["complete"] is False
    assert st["ai"] == {"offered": True, "available": True, "reason": "", "model": "fake-model", "provider": "假接入"}

    resp = await client.get(f"{API}/imports/{sid}/draft-ai/preview")
    assert resp.status_code == 200, resp.text
    pv = resp.json()
    assert set(pv) >= {"text", "chars", "sha256", "model", "provider"}
    assert pv["chars"] == len(pv["text"]) and pv["text_sha256"] == hashlib.sha256(pv["text"].encode()).hexdigest()
    for number in ("120", "860", "35600"):
        assert number not in pv["text"], f"数字格的值 {number} 不外发"
    assert calls == [], "预览不调用模型"

    # 同意框的门槛：不同意 → 422；sha 对不上 → 409；都不调模型
    resp = await client.post(f"{API}/imports/{sid}/draft-ai", json={"preview_sha256": pv["sha256"]})
    assert resp.status_code == 422 and resp.json()["code"] == "ai_consent_required", resp.text
    resp = await client.post(f"{API}/imports/{sid}/draft-ai", json={"consent": True, "preview_sha256": "0" * 64})
    assert resp.status_code == 409 and resp.json()["code"] == "ai_preview_stale", resp.text
    assert calls == []

    resp = await client.post(f"{API}/imports/{sid}/draft-ai",
                             json={"consent": True, "preview_sha256": pv["sha256"], "signed_by": "验收员丙"})
    assert resp.status_code == 200, resp.text
    st = resp.json()
    assert calls and calls[0][0].content + "\n\n" + calls[0][1].content == pv["text"], "发出去的就是预览的全文"
    assert st["recipe_origin"] == "ai" and st["recipe_problems"] == [] and st["answers"] == {}
    assert st["ai_draft"]["total_tokens"] == 2400 and st["ai_draft"]["error"] is None
    assert st["ai_consents"][-1]["signed_by"] == "验收员丙"
    assert st["ai_consents"][-1]["model"] == "fake-model"

    resp = await trial(client, sid)
    assert resp.status_code == 200, resp.text
    st = resp.json()
    assert st["trial"]["status"] == "passed", st["trial"]["problems"]
    ids = all_confirms(st)
    assert "ai_origin" in ids
    assert any(i.startswith("rename:") for i in ids), ids
    assert any(i.startswith("columns:") for i in ids), ids
    resp = await commit(client, st, confirmations=[i for i in ids if i != "ai_origin"])
    assert resp.status_code == 422 and resp.json()["code"] == "confirm_required" and "ai_origin" in resp.json()["detail"]
    resp = await commit(client, st)
    assert resp.status_code == 201, resp.text
    out = resp.json()
    assert out["source"]["current_recipe"]["origin"] == "ai"
    imp = await db_get(TableImport, out["import_id"])
    manifest = await artifact(client, imp.manifest_artifact)
    assert manifest["ai"]["usage"] and manifest["ai"]["usage"][0]["input_tokens"] == 2100
    assert manifest["ai"]["consents"] and manifest["ai"]["consents"][0]["preview_sha256"] == pv["sha256"]
    assert manifest["recipe"]["origin"] == "ai"
    assert await count(client, name, "基本情况") > 0


# ==========================================================================
# 第 11 步：证据链
# ==========================================================================


async def test_step11_evidence_chain_from_query_snapshot_to_manifest(client, engine_up):
    name = unique()
    base = await first_import(client, name, seed=111)
    snapshot_id = base["commit"]["snapshot_id"]
    graph = _chain(
        _node("start", "input"),
        _node("t1", "tool", tool=f"db_query__{name}", args={"sql": 'SELECT "日期", "全日客流" FROM "日客流" LIMIT 3'}),
        _node("out", "output", fields=[{"name": "t1", "value": "{{ nodes.t1 }}"}]),
    )
    resp = await client.post("/api/runs", json={"graph": graph, "input": {}})
    assert resp.status_code == 201, resp.text
    run = await _wait_run(client, resp.json()["id"], ("succeeded", "failed"))
    assert run["status"] == "succeeded", run["error"]
    value = run["output"]["t1"]
    payload = json.loads(value[value.index("{"):]) if isinstance(value, str) else value
    qs = await artifact(client, payload["artifact"])
    assert qs["data_version"] == snapshot_id
    schema_id = qs.get("schema_artifact") or payload.get("schema_artifact")
    assert schema_id, "查询快照应指向冻结的表结构"
    ss = await artifact(client, schema_id)
    assert ss.get("import_mode") == "recipe"
    assert ss.get("import_manifests") and len(ss["import_manifests"]) == 1
    manifest = await artifact(client, ss["import_manifests"][0])
    snap = await db_get(SourceSnapshot, snapshot_id)
    assert manifest["db_sha256"] == snap.db_sha256
    assert manifest["snapshot_id"] == snapshot_id
    assert manifest["table_hashes"] == base["trial"]["trial"]["receipt"]["table_hashes"]
    assert manifest["file"]["raw_sha256"] == hashlib.sha256(base["raw"]).hexdigest()


# ==========================================================================
# 第 12 步：并发
# ==========================================================================


async def test_step12_two_reuploads_committed_in_turn_second_gets_base_changed(client):
    name = unique()
    base = await first_import(client, name, seed=112)
    source_id = base["commit"]["source"]["id"]
    a = (await reupload(client, source_id, *flow_workbook(SEP, 30, seed=1121))).json()
    b = (await reupload(client, source_id, *flow_workbook(OCT, 31, seed=1122))).json()
    assert a["trial"]["status"] == b["trial"]["status"] == "passed"
    first = await commit(client, a)
    assert first.status_code == 201, first.text
    second = await commit(client, b)
    assert second.status_code == 409 and second.json()["code"] == "base_changed", second.text
    assert (await source_row(client, source_id))["current_snapshot"]["id"] == first.json()["snapshot_id"]
    assert await count(client, name, "日客流") == 30


async def test_step12_two_reuploads_committed_concurrently_one_wins(client):
    name = unique()
    base = await first_import(client, name, seed=113)
    source_id = base["commit"]["source"]["id"]
    a = (await reupload(client, source_id, *flow_workbook(SEP, 30, seed=1131))).json()
    b = (await reupload(client, source_id, *flow_workbook(OCT, 31, seed=1132))).json()
    ra, rb = await asyncio.gather(commit(client, a), commit(client, b))
    codes = sorted([ra.status_code, rb.status_code])
    assert codes == [201, 409], (ra.text, rb.text)
    loser = ra if ra.status_code == 409 else rb
    winner = ra if ra.status_code == 201 else rb
    assert loser.json()["code"] == "base_changed"
    assert (await source_row(client, source_id))["current_snapshot"]["id"] == winner.json()["snapshot_id"]


async def test_step12_same_new_name_committed_concurrently_one_gets_name_taken(client):
    name = unique()
    stagings = []
    for seed in (1141, 1142):
        raw, fn = flow_workbook(AUG, 31, seed=seed)
        st = (await stage(client, name, raw, fn)).json()
        assert st["kind"] == "first"
        st = (await trial(client, st["id"])).json()
        assert st["trial"]["status"] == "passed", st["trial"]["problems"]
        stagings.append(st)
    ra, rb = await asyncio.gather(commit(client, stagings[0]), commit(client, stagings[1]))
    assert sorted([ra.status_code, rb.status_code]) == [201, 409], (ra.text, rb.text)
    loser = ra if ra.status_code == 409 else rb
    assert loser.json()["code"] == "name_taken", loser.text
    async with SessionLocal() as session:
        rows = list((await session.execute(select(DataSource).where(DataSource.name == name))).scalars())
    assert len(rows) == 1


# ==========================================================================
# 第 13 步：清除原件连带暂存区
# ==========================================================================


async def test_step13_purge_raw_discards_open_stagings_on_the_same_raw(client):
    name = unique()
    base = await first_import(client, name, seed=114)
    source_id = base["commit"]["source"]["id"]
    st = (await reupload(client, source_id, base["raw"], base["file_name"])).json()
    assert st["status"] == "trialed"
    row = await db_get(ImportStaging, st["id"])
    trial_path = Path(row.trial_path)
    assert trial_path.is_file()
    resp = await client.post(f"{API}/{source_id}/imports/{base['commit']['import_id']}/purge-raw",
                             json={"reason": "合成理由：原件含敏感信息", "signed_by": "验收员丁"})
    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert out["file_deleted"] is True
    assert out["discarded_stagings"] == [st["id"]]
    got = (await client.get(f"{API}/imports/{st['id']}")).json()
    assert got["status"] == "discarded" and got["grids"] == [] and got["trial"] is None
    assert not trial_path.exists()
    assert not raw_store.raw_exists(hashlib.sha256(base["raw"]).hexdigest())
    # 当前版本照常可查；修改配方要原件 → 409 raw_missing
    assert await count(client, name, "日客流") == 31
    resp = await client.post(f"{API}/{source_id}/redraft", json={})
    assert resp.status_code == 409 and resp.json()["code"] == "raw_missing", resp.text


# ==========================================================================
# 拒收后修复（8.1：D09 这类拒收在期 2 靠配方面板修好）
# ==========================================================================


async def test_rejected_reupload_is_fixed_by_editing_the_recipe_and_committed(client):
    name = unique()
    base = await first_import(client, name, seed=115)
    source_id = base["commit"]["source"]["id"]
    raw, fn = DRIFT_CASES["D09"].build(115)
    st = (await reupload(client, source_id, raw, fn)).json()
    sid = st["id"]
    assert st["trial"]["status"] == "rejected"
    assert "title_not_found" in {p["code"] for p in st["trial"]["problems"]}
    assert st["ai"]["offered"] is False, "上传新一期不提供 AI 起草"
    resp = await client.get(f"{API}/imports/{sid}/draft-ai/preview")
    assert resp.status_code == 409 and resp.json()["code"] == "ai_not_offered"
    # 「修改配方」：在同一个暂存区里改分段标题
    fixed = json.loads(json.dumps(st["recipe"], ensure_ascii=False))
    seg = next(s for s in fixed["sheets"][0]["blocks"][0]["segments"] if s["id"] == "日间")
    seg["locate"]["title"] = "日间分时段客流（人次）"
    resp = await client.put(f"{API}/imports/{sid}/recipe", json={"recipe": fixed})
    assert resp.status_code == 200, resp.text
    st = resp.json()
    assert st["status"] == "drafting"
    # 期 3（P3-SPEC 3.3 (b)，第 8 节遗留项 3）：pick「日间」是现行配方里确认过的、仍逐字出现在新标题里，沿用它
    # 不是新编的文字，静态校验放行；常量列的值不变，不是破坏性变更
    assert st["recipe_problems"] == [], st["recipe_problems"]
    resp = await trial(client, sid)
    st = resp.json()
    assert st["trial"]["status"] == "passed", st["trial"]["problems"]
    ids = all_confirms(st)
    # 6.2：只列签名与现行配方不同的配方类项，导入模式没变就不再出；只改标题、不改 pick，表结构和常量都没变，
    # 没有 breaking:*
    assert "mode" not in ids and [i for i in ids if i.startswith("breaking:")] == [], ids
    resp = await commit(client, st)
    assert resp.status_code == 201, resp.text
    out = resp.json()
    assert out["recipe_id"] != base["commit"]["recipe_id"]
    assert out["source"]["current_recipe"]["seq"] == 2
    async with SessionLocal() as session:
        recipes = {r.seq: r.status for r in (await session.execute(
            select(TableRecipe).where(TableRecipe.source_id == source_id))).scalars()}
    assert recipes == {1: "superseded", 2: "active"}
    assert await count(client, name, "日客流") == 30


async def test_rejected_reupload_fixed_by_picking_a_new_word_still_reports_breaking(client):
    """D09 改标题时另选新标题的候选词（期 2 的修法）：常量「时段类别」整列从「日间」换了值，7.5 的 breaking_changes
    报常量取值变化，必须出 breaking:时段客流 让人确认，别的表不报（期 3 起只改标题可以沿用旧词，见上一个用例）。"""
    name = unique()
    base = await first_import(client, name, seed=119)
    source_id = base["commit"]["source"]["id"]
    raw, fn = DRIFT_CASES["D09"].build(119)
    st = (await reupload(client, source_id, raw, fn)).json()
    sid = st["id"]
    assert st["trial"]["status"] == "rejected"
    fixed = json.loads(json.dumps(st["recipe"], ensure_ascii=False))
    seg = next(s for s in fixed["sheets"][0]["blocks"][0]["segments"] if s["id"] == "日间")
    seg["locate"]["title"] = "日间分时段客流（人次）"
    resp = await client.put(f"{API}/imports/{sid}/recipe", json={"recipe": fixed})
    assert resp.status_code == 200, resp.text
    options = resp.json()["candidates"].get("日间分时段客流（人次）")
    assert options and options[0] != "日间", resp.json()["candidates"]
    seg["const"]["时段类别"]["pick"] = options[0]
    resp = await client.put(f"{API}/imports/{sid}/recipe", json={"recipe": fixed})
    assert resp.status_code == 200, resp.text
    st = resp.json()
    assert st["recipe_problems"] == [] and st["status"] == "drafting"
    resp = await trial(client, sid)
    st = resp.json()
    assert st["trial"]["status"] == "passed", st["trial"]["problems"]
    ids = all_confirms(st)
    assert "mode" not in ids and [i for i in ids if i.startswith("breaking:")] == ["breaking:时段客流"], ids
    label = next(i["label"] for i in st["trial"]["confirm_items"] if i["id"] == "breaking:时段客流")
    assert f"列「时段类别」取值「日间」→「{options[0]}」" in label, label
    resp = await commit(client, st)
    assert resp.status_code == 201, resp.text
    assert resp.json()["source"]["current_recipe"]["seq"] == 2
    assert await count(client, name, "日客流") == 30


# ==========================================================================
# 修改配方（不换文件）、放弃导入
# ==========================================================================


async def test_redraft_on_the_current_raw_without_changes_is_unchanged(client):
    name = unique()
    base = await first_import(client, name, seed=116)
    source_id = base["commit"]["source"]["id"]
    resp = await client.post(f"{API}/{source_id}/redraft", json={})
    assert resp.status_code == 201, resp.text
    st = resp.json()
    assert st["kind"] == "redraft" and st["status"] == "drafting" and st["draft"] is None
    assert st["ai"]["offered"] is False and st["recipe_problems"] == []
    assert st["file"]["name"] == base["file_name"]
    cur = (await client.get(f"{API}/{source_id}/recipe")).json()
    mine, _ = recipe.validate_recipe(st["recipe"], origin="manual")
    assert recipe.recipe_sha256(mine) == cur["recipe_sha256"], "工作配方 = 现行配方"
    st = (await trial(client, st["id"])).json()
    assert st["trial"]["status"] == "passed", st["trial"]["problems"]
    assert st["trial"]["same_as_import"]["id"] == base["commit"]["import_id"]
    resp = await commit(client, st)
    assert resp.status_code == 201 and resp.json()["unchanged"] is True, resp.text


async def test_discard_deletes_an_unreferenced_raw_and_closes_the_staging(client):
    raw, fn = flow_workbook(AUG, 31, seed=117)
    st = (await stage(client, unique(), raw, fn)).json()
    sha = hashlib.sha256(raw).hexdigest()
    assert raw_store.raw_exists(sha)
    resp = await client.delete(f"{API}/imports/{st['id']}")
    assert resp.status_code == 204, resp.text
    assert not raw_store.raw_exists(sha), "放弃即删原件（没有别的引用）"
    got = (await client.get(f"{API}/imports/{st['id']}")).json()
    assert got["status"] == "discarded" and got["grids"] == [] and got["recipe"] is not None
    resp = await client.delete(f"{API}/imports/{st['id']}")
    assert resp.status_code == 409 and resp.json()["code"] == "staging_closed"
    resp = await client.post(f"{API}/imports/{st['id']}/answers", json={"answers": {}})
    assert resp.status_code == 409 and resp.json()["code"] == "staging_closed"


# ==========================================================================
# 差异卡的种类与界面对得上（7.6 的取值表就是前端 DIFF_KIND_LABEL 的键）
# ==========================================================================

#: P2-SPEC 7.6 差异卡表格里的全部 kind（前端 frontend/src/lib/terms.ts 的 DIFF_KIND_LABEL 照它逐个给了中文名）
SPEC_DIFF_KINDS = frozenset({
    "period", "rows", "placeholders", "checks", "file_name", "full_calc", "context_text", "context_source_added",
    "column_order", "label_writing", "header_writing", "canon_values", "row_order", "block_order", "derived_form",
    "axis_form", "outside_moved", "outside_added", "outside_removed", "outside_changed", "ignored_columns", "hidden",
})


def _move_note_down(raw: bytes) -> bytes:
    """D28 的表下注释从 B32 挪到 B33（文字一字不改，只是位置变了）。"""
    import re

    from tests.fixtures.xlsx import patch_parts, sheet_part
    from tests.fixtures.xlsx.flow import SHEET

    def edit(xml: str) -> str:
        xml = xml.replace('<row r="32"', '<row r="33"').replace('r="B32"', 'r="B33"')
        return re.sub(r'<dimension ref="([A-Z]+\d+):([A-Z]+)32"/>', r'<dimension ref="\1:\g<2>33"/>', xml)

    return patch_parts(raw, {sheet_part(raw, SHEET): edit})


async def test_outside_text_only_moved_is_an_info_item_of_a_spec_kind(client):
    """7.6：区域外文字先按全文配对，只是位置变了的算 info、不要求确认。差异项的 kind 必须是 7.6 表里的取值
    （界面按它取中文名，表外的 kind 会把英文键名原样露在差异卡上）。"""
    name = unique()
    raw, fn = flow_workbook(SEP, 30, seed=118, variant="D28")
    resp = await stage(client, name, raw, fn)
    assert resp.status_code == 201, resp.text
    st = resp.json()
    st = (await client.post(f"{API}/imports/{st['id']}/answers", json={"answers": answer_all(st["questions"])})).json()
    st = (await trial(client, st["id"])).json()
    assert st["trial"]["status"] == "passed", st["trial"]["problems"]
    resp = await commit(client, st)
    assert resp.status_code == 201, resp.text
    source_id = resp.json()["source"]["id"]

    st = (await reupload(client, source_id, _move_note_down(raw), fn)).json()
    t = st["trial"]
    assert t["status"] == "passed", t["problems"]
    kinds = {d["kind"] for d in t["diff"]}
    assert kinds <= SPEC_DIFF_KINDS, f"差异卡出现了 7.6 表外的 kind：{sorted(kinds - SPEC_DIFF_KINDS)}"
    assert kinds == {"outside_moved"}, kinds
    assert not any(d["requires_confirm"] for d in t["diff"]), "只是挪了位置：info，不要求确认"
    assert "outside_digits:客流汇总!B33" in all_confirms(st), "本期类：含数字的区域外文字每期都要确认"


async def test_reported_total_table_is_marked_everywhere_the_model_and_judge_read(client):
    """AU-5：原表写明的合计表（时段客流_表内合计）的「不要相加」不只在 db_schema 查单表时看得到：查询工具描述、
    db_schema 的表清单、写作目录、裁判摘录都要标出，而且不含数字。明细表不标。"""
    from app.core.artifact_store import load as load_artifact
    from app.data.engine import engines
    from app.engine import judge
    from app.engine.evidence import build_catalog, catalog_prompt, extract_numbers, schema_ledger_entry
    from app.tools.datasource import REPORTED_TOTAL_MARK, build_datasource_tools
    from app.tools.registry import ToolContext, Versions

    name = unique()
    done = await first_import(client, name)
    sid = done["commit"]["source"]["id"]
    async with SessionLocal() as session:
        src = await session.get(DataSource, sid)
        assert src.schema_cache["tables"]["时段客流_表内合计"]["kind"] == "reported_total"
        assert "kind" not in src.schema_cache["tables"]["时段客流"]
        tools = {t.name: t for t in await build_datasource_tools(
            [f"db_query__{name}", f"db_schema__{name}"], ToolContext(data_versions=Versions.CURRENT), session)}
    try:
        query, schema = tools[f"db_query__{name}"], tools[f"db_schema__{name}"]
        assert "时段客流_表内合计 是原表写明的合计" in query.description and "不要与明细表相加" in query.description
        listing = await schema.coroutine()
        assert f"时段客流_表内合计{REPORTED_TOTAL_MARK}" in listing
        assert f"时段客流{REPORTED_TOTAL_MARK}" not in listing.replace(f"时段客流_表内合计{REPORTED_TOTAL_MARK}", "")
        assert extract_numbers(REPORTED_TOTAL_MARK) == []
        payload = json.loads(await query.coroutine(sql='SELECT COUNT(*) AS n FROM "时段客流_表内合计"'))
        frozen = schema_ledger_entry(payload["schema_artifact"], node_id="fetch", exec_no=1)
        catalog = build_catalog(nodes={}, ledger=[frozen])
        prompt = catalog_prompt(catalog, loader=load_artifact)
        assert "时段客流_表内合计（原表写明的合计：不要彼此相加，也不要与明细相加）" in prompt
        excerpt = judge._excerpt("t:时段客流_表内合计", catalog["t:时段客流_表内合计"], [], load_artifact, None, catalog)
        assert "表的说明：原表写明的合计" in excerpt
    finally:
        await engines.invalidate(sid)
