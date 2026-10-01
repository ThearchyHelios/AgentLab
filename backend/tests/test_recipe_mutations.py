"""9.4 的数据变异（WP-7 验收）：在 XML 层改 D00 的字节（tests/fixtures/xlsx/mutate.py），按参考配方重放。

每个用例先把 D00（2026-08-01 起 31 天）按参考配方首次导入并启用，再把变异后的字节作为「上传新一期」重放：
上传新一期自动试运行，回来时已带状态、问题、核对、回执和差异卡。期望照 P2-SPEC 9.4 的表逐条断言，
另加 MUTATIONS 里的「外加」用例 PKDUP（同一分段里一行写 8-9、另一行写 8－9）。

拒收的用例另外断言：不能提交（409 trial_not_passed），当前快照没变（9.7 第 4 步「拒收的确认当前快照没变」）。
夹具全部合成、假名；不调用任何模型。
"""
from __future__ import annotations

import datetime as dt
import json
import re
import uuid
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.config import settings
from app.data import table_versions
from app.data.engine import engines, run_query
from app.db.base import SessionLocal
from app.db.models import DataSource, SourceSnapshot
from app.main import app
from tests.fixtures.xlsx.flow import FLOW_EXPECT, flow_workbook
from tests.fixtures.xlsx.mutate import MUTATIONS, mutate

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
FLOW_RECIPE = json.loads((Path(__file__).parent / "fixtures" / "recipes" / "flow_recipe.json").read_text("utf-8"))
AUG = dt.date(2026, 8, 1)
SHEET = "客流汇总"
TOTALS_TABLE = "时段客流_表内合计"


@pytest.fixture(autouse=True)
def store(monkeypatch, tmp_path):
    """每个测试一个独立的数据目录：上传目录、原件存档、试运行库、工件都在里面。"""
    data = tmp_path / "data"
    (data / "uploads").mkdir(parents=True)
    monkeypatch.setattr(settings, "data_dir", data)
    yield data


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


# --------------------------------------------------------------------------
# 工具
# --------------------------------------------------------------------------


def _cell(ref: str) -> str:
    return f"{SHEET}!{ref}"


def _confirm_ids(st: dict[str, Any]) -> list[str]:
    return [i["id"] for i in st["trial"]["confirm_items"]]


def _problems(trial: dict[str, Any], code: str) -> list[dict[str, Any]]:
    return [p for p in trial["problems"] if p["code"] == code]


def _check(trial: dict[str, Any], cid: str) -> dict[str, Any] | None:
    return next((c for c in trial["checks"] if c["id"] == cid), None)


async def _baseline(client, seed: int) -> dict[str, Any]:
    """D00 按参考配方首次导入并启用：暂存 → PUT 参考配方 → 试运行（passed）→ 勾全部确认项提交。"""
    raw, fn = flow_workbook(AUG, 31, seed=seed)
    name = f"wp7mut_{uuid.uuid4().hex[:10]}"
    resp = await client.post("/api/datasources/imports/stage", files={"file": (fn, raw, XLSX)}, data={"name": name})
    assert resp.status_code == 201, resp.text
    sid = resp.json()["id"]
    resp = await client.put(f"/api/datasources/imports/{sid}/recipe", json={"recipe": FLOW_RECIPE})
    assert resp.status_code == 200, resp.text
    assert resp.json()["recipe_problems"] == []
    resp = await client.post(f"/api/datasources/imports/{sid}/trial", json={})
    assert resp.status_code == 200, resp.text
    st = resp.json()
    trial = st["trial"]
    assert trial["status"] == "passed", trial["problems"]
    assert {t["name"]: t["rows"] for t in trial["receipt"]["tables"]} == FLOW_EXPECT["rows"]
    resp = await client.post(f"/api/datasources/imports/{sid}/commit",
                             json={"trial_id": trial["trial_id"], "confirmations": _confirm_ids(st)})
    assert resp.status_code == 201, resp.text
    out = resp.json()
    return {"raw": raw, "fn": fn, "source_id": out["source"]["id"], "snapshot_id": out["snapshot_id"],
            "import_id": out["import_id"], "table_hashes": trial["receipt"]["table_hashes"]}


async def _replay(client, base: dict[str, Any], case: str) -> dict[str, Any]:
    """变异后的 D00 作为「上传新一期」重放（文件名不变），返回自动试运行后的暂存区。"""
    raw = mutate(base["raw"], case)
    assert raw != base["raw"]
    resp = await client.post(f"/api/datasources/{base['source_id']}/reupload",
                             files={"file": (base["fn"], raw, XLSX)})
    assert resp.status_code == 201, resp.text
    st = resp.json()
    assert st["kind"] == "reupload" and st["trial"] is not None, st
    return st


async def _current_snapshot(source_id: str) -> str | None:
    async with SessionLocal() as session:
        row = await session.get(DataSource, source_id)
        return row.current_snapshot_id


async def _assert_rejected(client, base: dict[str, Any], st: dict[str, Any], codes: tuple[str, ...]) -> None:
    """拒收：状态 rejected、期望的问题 code 都在、提交不了、当前快照没变。"""
    trial = st["trial"]
    assert trial["status"] == "rejected", trial["problems"]
    assert st["status"] == "rejected"
    got = {p["code"] for p in trial["problems"]} | {c["id"] for c in trial["checks"] if c["status"] != "passed"}
    for code in codes:
        assert code in got, (code, trial["problems"], trial["checks"])
    # 拒收时不算差异卡和确认项（7.6）
    assert trial["diff"] is None and trial["confirm_items"] == []
    resp = await client.post(f"/api/datasources/imports/{st['id']}/commit",
                             json={"trial_id": trial["trial_id"], "confirmations": []})
    assert resp.status_code == 409 and resp.json()["code"] == "trial_not_passed", resp.text
    assert await _current_snapshot(base["source_id"]) == base["snapshot_id"]


async def _scalar(source_id: str, sql: str) -> Any:
    async with SessionLocal() as session:
        row = await session.get(DataSource, source_id)
        view = await table_versions.resolve_source(session, row)
    try:
        return (await run_query(view, sql)).rows[0][0]
    finally:
        await engines.invalidate(view.id)


def _table_meta(schema_cache: dict[str, Any], table: str) -> dict[str, Any]:
    """schema_cache 里某张表的结构（introspect 的形状：tables = {表名: {comment, columns: [...]}}）。"""
    tables = schema_cache.get("tables") or {}
    assert table in tables, sorted(tables)
    return tables[table]


# --------------------------------------------------------------------------
# 夹具本身：MUTATIONS 照 9.4 抄录
# --------------------------------------------------------------------------


def test_mutation_table_matches_spec():
    expect = {
        "P1": ("passed", ()), "P1b": ("needs_decision", ("K1",)), "P2": ("rejected", ("value_blank",)),
        "P6": ("rejected", ("axis_duplicate",)), "P9": ("rejected", ("K1", "G1")),
        "P20": ("rejected", ("row_unclaimed",)), "P23": ("passed", ()), "P24": ("passed", ()),
        "P25": ("rejected", ("axis_extra_cells",)), "P26": ("rejected", ("axis_coverage",)),
        "PKDUP": ("rejected", ("pk_duplicate", "label_duplicate")),
    }
    assert {k: v[1:] for k, v in MUTATIONS.items()} == expect


# --------------------------------------------------------------------------
# 能通过的变异
# --------------------------------------------------------------------------


async def test_p1_totals_without_formula_pass_by_label_range(client):
    """P1：第 28–30 行去掉公式、保留缓存值 → passed（K1 按标签区间核对通过，derived_form=literal）。"""
    base = await _baseline(client, seed=21)
    st = await _replay(client, base, "P1")
    trial = st["trial"]
    assert trial["status"] == "passed", trial["problems"]
    assert [p for p in trial["problems"] if p["category"] in ("structure", "recipe", "input")] == []
    k1 = _check(trial, "K1")
    assert k1 is not None and k1["status"] == "passed" and k1["checked"] == 93, k1
    # 写死的合计没有公式：不出 G1（9.2 D07 同理）
    assert _check(trial, "G1") is None
    assert trial["receipt"]["derived_form"] == {"夜间合计": "literal"}
    # 值没变：三张表的哈希与 D00 相同
    assert trial["receipt"]["table_hashes"] == base["table_hashes"]
    # 合计从公式变成写死的数：差异卡要求确认（同 9.2 D07 的 derived_form；G1 消失记 checks）
    kinds = {d["kind"] for d in trial["diff"]}
    assert kinds == {"checks", "derived_form"}, trial["diff"]
    assert "diff:derived_form:夜间合计" in _confirm_ids(st)


async def test_p1b_uncached_totals_need_decision_then_commit_with_null_totals(client):
    """P1b：第 28–30 行保留公式、去掉缓存值 → needs_decision：K1 unverifiable（uncached: 93，可接受）、
    G1 passed；写理由接受后提交，合计表的客流全为 NULL、表结构保留，说明写「未能核对」、不写「一致」。"""
    base = await _baseline(client, seed=22)
    st = await _replay(client, base, "P1b")
    trial = st["trial"]
    assert trial["status"] == "needs_decision", trial["problems"]
    k1 = _check(trial, "K1")
    assert k1 is not None and k1["status"] == "unverifiable", k1
    assert k1["reasons"] == {"uncached": 93} and k1["acceptable"] is True
    g1 = _check(trial, "G1")
    assert g1 is not None and g1["status"] == "passed", g1
    assert trial["acceptable"] == ["K1"]
    # 试运行时的说明预览（还没接受）也不能说「一致」
    preview = trial["notes"][TOTALS_TABLE]["comment"]
    assert "一致" not in preview, preview

    # 不带接受 → 422 acceptance_required；理由是空白 → 同样 422
    confirms = _confirm_ids(st)
    resp = await client.post(f"/api/datasources/imports/{st['id']}/commit",
                             json={"trial_id": trial["trial_id"], "confirmations": confirms})
    assert resp.status_code == 422 and resp.json()["code"] == "acceptance_required", resp.text
    assert "K1" in resp.json()["detail"]
    resp = await client.post(f"/api/datasources/imports/{st['id']}/commit",
                             json={"trial_id": trial["trial_id"], "confirmations": confirms,
                                   "acceptances": [{"check_id": "K1", "reason": "   "}]})
    assert resp.status_code == 422 and resp.json()["code"] == "acceptance_required", resp.text
    assert await _current_snapshot(base["source_id"]) == base["snapshot_id"]

    resp = await client.post(f"/api/datasources/imports/{st['id']}/commit",
                             json={"trial_id": trial["trial_id"], "confirmations": confirms,
                                   "acceptances": [{"check_id": "K1", "reason": "合成理由：导出工具未保存计算结果"}]})
    assert resp.status_code == 201, resp.text
    out = resp.json()
    assert out["snapshot_id"] != base["snapshot_id"]
    sid = base["source_id"]
    # 合计表：行数照旧 93，客流全为 NULL；明细表不受影响
    assert await _scalar(sid, f'SELECT COUNT(*) FROM "{TOTALS_TABLE}"') == 93
    assert await _scalar(sid, f'SELECT COUNT("客流") FROM "{TOTALS_TABLE}"') == 0
    assert await _scalar(sid, 'SELECT COUNT(*) FROM "时段客流"') == FLOW_EXPECT["rows"]["时段客流"]
    async with SessionLocal() as session:
        snap = await session.get(SourceSnapshot, out["snapshot_id"])
        cache = snap.schema_cache
    meta = _table_meta(cache, TOTALS_TABLE)
    cols = [c["name"] for c in meta["columns"]]
    assert cols == ["日期", "合计项", "起始小时", "结束小时", "客流"], cols
    comment = meta.get("comment") or ""
    assert "本期原表未保存计算结果，合计值为空，未能核对" in comment, comment
    assert "一致" not in comment, comment
    for c in meta["columns"]:
        assert "一致" not in (c.get("comment") or ""), c


async def test_p23_dimension_cut_rows_still_reads_everything(client):
    """P23：<dimension> 截到第 27 行 → passed，table_hashes 与 D00 完全相同。"""
    base = await _baseline(client, seed=23)
    st = await _replay(client, base, "P23")
    trial = st["trial"]
    assert trial["status"] == "passed", trial["problems"]
    assert trial["receipt"]["table_hashes"] == base["table_hashes"]
    assert {t["name"]: t["rows"] for t in trial["receipt"]["tables"]} == FLOW_EXPECT["rows"]
    # 陈旧的 <dimension> 不影响任何东西：内容与上一期逐格相同，没有差异
    assert trial["diff"] == [], trial["diff"]


async def test_p24_dimension_cut_columns_still_reads_everything(client):
    """P24：<dimension> 截到倒数第二个日期列 → passed，table_hashes 与 D00 完全相同。"""
    base = await _baseline(client, seed=24)
    st = await _replay(client, base, "P24")
    trial = st["trial"]
    assert trial["status"] == "passed", trial["problems"]
    assert trial["receipt"]["table_hashes"] == base["table_hashes"]
    assert {t["name"]: t["rows"] for t in trial["receipt"]["tables"]} == FLOW_EXPECT["rows"]
    assert trial["diff"] == [], trial["diff"]


# --------------------------------------------------------------------------
# 拒收的变异
# --------------------------------------------------------------------------


async def test_p2_blank_value_is_rejected_at_d15(client):
    """P2：D15 改成空 → rejected value_blank（D15）。"""
    base = await _baseline(client, seed=25)
    st = await _replay(client, base, "P2")
    await _assert_rejected(client, base, st, ("value_blank",))
    hits = _problems(st["trial"], "value_blank")
    assert len(hits) == 1 and hits[0]["category"] == "structure"
    assert hits[0]["cells"] == [_cell("D15")], hits[0]


async def test_p6_duplicate_axis_date_is_rejected(client):
    """P6：R4 改成「8月15日」（与 Q4 重复）→ rejected axis_duplicate。"""
    base = await _baseline(client, seed=26)
    st = await _replay(client, base, "P6")
    await _assert_rejected(client, base, st, ("axis_duplicate",))
    hit = _problems(st["trial"], "axis_duplicate")[0]
    assert hit["category"] == "structure"
    assert {_cell("Q4"), _cell("R4")} <= set(hit["cells"]), hit


async def test_p9_wrong_formula_range_fails_k1_and_g1(client):
    """P9：C28 改成 =SUM(C22:C24)，缓存值自洽 → rejected：K1 mismatch（C28）且 G1 mismatch。"""
    base = await _baseline(client, seed=27)
    st = await _replay(client, base, "P9")
    await _assert_rejected(client, base, st, ("K1", "G1"))
    trial = st["trial"]
    k1, g1 = _check(trial, "K1"), _check(trial, "G1")
    for c in (k1, g1):
        assert c["status"] == "mismatch" and c["category"] == "structure" and c["acceptable"] is False, c
        assert c["cells"] == [_cell("C28")], c
        assert c["failed"] == 1, c
    # 只有核对没过：执行器本身没有结构问题
    assert [p for p in trial["problems"] if p["category"] in ("structure", "recipe")] == []


async def test_p20_unclaimed_data_row_32_is_rejected(client):
    """P20：第 32 行加「备用分区（人次）」和两个数，<dimension> 不更新 → rejected row_unclaimed（第 32 行）。"""
    base = await _baseline(client, seed=28)
    st = await _replay(client, base, "P20")
    await _assert_rejected(client, base, st, ("row_unclaimed",))
    hits = _problems(st["trial"], "row_unclaimed")
    assert len(hits) == 1, hits
    assert hits[0]["category"] == "structure"
    assert all(re.fullmatch(rf"{SHEET}![A-Z]+32", c) for c in hits[0]["cells"]), hits[0]
    assert _cell("B32") in hits[0]["cells"]


async def test_p25_extra_total_column_right_of_axis_is_rejected(client):
    """P25：AH4「合计」、AH 列各数据行写 999，<dimension> 不更新 → rejected axis_extra_cells（AH4）。"""
    base = await _baseline(client, seed=29)
    st = await _replay(client, base, "P25")
    await _assert_rejected(client, base, st, ("axis_extra_cells",))
    hit = _problems(st["trial"], "axis_extra_cells")[0]
    assert hit["category"] == "structure" and _cell("AH4") in hit["cells"], hit
    assert hit["fix"] == "ignore_cells"


async def test_p26_missing_last_date_column_fails_coverage(client):
    """P26：删掉 AG 列所有格，B2 仍写到 31 日 → rejected axis_coverage。"""
    base = await _baseline(client, seed=30)
    st = await _replay(client, base, "P26")
    await _assert_rejected(client, base, st, ("axis_coverage",))
    hit = _problems(st["trial"], "axis_coverage")[0]
    assert hit["category"] == "structure"
    assert "30" in hit["message"] and "31" in hit["message"], hit


async def test_pkdup_mixed_dash_labels_report_pk_and_label_duplicates(client):
    """9.4 外加：同一分段里一行写 8-9、另一行写 8－9 → rejected pk_duplicate（两处坐标），label_duplicate
    也出现（4.2 第 3 步：定位失败的分段跳过，其余照常，一次报全）。"""
    base = await _baseline(client, seed=31)
    st = await _replay(client, base, "PKDUP")
    await _assert_rejected(client, base, st, ("pk_duplicate", "label_duplicate"))
    trial = st["trial"]
    pk = _problems(trial, "pk_duplicate")
    assert len(pk) == 1 and pk[0]["category"] == "structure", pk
    # 两处坐标：B11（8-9）和 B12（8－9）那两行同一日期的格子
    assert {_cell("C11"), _cell("C12")} <= set(pk[0]["cells"]), pk[0]
    assert "C11" in pk[0]["message"] and "C12" in pk[0]["message"], pk[0]["message"]
    dup = _problems(trial, "label_duplicate")
    assert dup and _cell("B12") in dup[0]["cells"], dup
