"""分析结论里的占比、变化率、权重按原始小数显示（第 2 波复核 S1，显示规则 4 版）。

复核实测：结果表里占变化的比重、变化率以比率存（0.618556701031），正文引用 [[a:A1.top_share]]、[[v:A1.r0.share]]、
[[table:A1 cols=region,pct,share]] 时照原样写「占比 3.5190412373」；判定语写「占 351.9%」，正文写「占比 3.5190412373」，
读的人把 0.62 当成 62% 的百分之一。现在算子给结果表的、以比率存的列记换算（ResultTable.formats：占比、变化率、权重记 pct，
比率的变化记 pp），结论的 values 里同样记一份（value_formats）；引用、整表、写作目录、证据面板、导出、可验证报告都按它渲染，
实际用的换算记在引用上（导出页、可验证报告照它渲染）。写作者自己写了换算的照他的。3 版及以前组装的文档照当年原样显示，
复核不因此判对不上。

数据全是虚构的（华东、华南、华北、西南，销售额、订单数，虚构零售库 shop）。
"""
from __future__ import annotations

import json
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from app.analysis.contract import CONV_PCT, CONV_POINTS, ledger_entry
from app.core import artifact_store
from app.engine import evidence as ev
from app.engine.evidence import build_catalog, compose_doc, iter_segments, render_cell, resolve_ref, verify_doc, \
    writer_catalog
from app.main import app as asgi
from tests.analysis_samples import analyze, snap
from tests.test_analysis_bundle import Files, GRAPH, RUN, RUN_ID, bundle_of, item, verdicts
from tests.test_analysis_evidence import SNAPSHOT, Store, real_result
from tests.test_analysis_evidence_api import seg_id, sealed_run

WEEKS = [["华东", "2026-W41", 820], ["华东", "2026-W42", 700], ["华北", "2026-W41", 600], ["华北", "2026-W42", 560],
         ["华南", "2026-W41", 700], ["华南", "2026-W42", 666], ["西南", "2026-W41", 100], ["西南", "2026-W42", 100]]
REGIONS = snap("Q2", "SELECT region, week, SUM(amount) AS gmv FROM orders GROUP BY region, week",
               ["region", "week", "gmv"], WEEKS, types={"region": "text", "week": "text", "gmv": "number"})


def run_sync(coro: Any) -> Any:
    from tests.test_analysis_evidence import run_sync as wait

    return wait(coro)


def analyzed(op: str, mode: str | None, **params: Any) -> dict[str, Any]:
    """真算一份结论（同 tests/analysis_samples.analyze：跑一次、复算一遍，逐字段相同）。"""
    return run_sync(analyze(op, mode, snaps=[REGIONS], query="Q2", **params))


def catalog_of(*, result: dict[str, Any] | None = None) -> tuple[Store, dict[str, Any]]:
    """一次查询（Q1）加一份真算的按地区拆的分析结论（A1）：(工件库, 写作目录)。结论带算子记的格式。"""
    store = Store()
    q = store.put(SNAPSHOT)
    res = result or real_result("Q2")
    a = store.put(res)
    ledger = [{"kind": "query", "node_id": "fetch", "exec": 1, "artifact": q, "tool": "db_query__shop",
               "source": "shop", "columns": ["region", "week", "gmv"], "rows": 8, "truncated": False},
              ledger_entry(res, node_id="an", exec_no=1, artifact=a, item="drop")]
    return store, build_catalog(nodes={}, ledger=ledger, loader=store.load)


# --------------------------------------------------------------------------
# 换算的写法：pct、pp
# --------------------------------------------------------------------------


@pytest.mark.parametrize("value, conv, shown", [
    ("0.618556701", "pct", "61.9%"),
    ("3.51904123734", "pct", "351.9%"),
    ("-0.0142977693", "pct", "-1.4%"),
    ("0.0123", "pp", "+1.2 个百分点"),
    ("-0.121944594212", "pp", "-12.2 个百分点"),
    ("0", "pp", "0.0 个百分点"),
    ("0.00004", "pp", "+0.004 个百分点"),           # 不是 0 的不写成 0.0
])
def test_pct_and_percentage_points_are_written_the_way_the_verdict_phrase_writes_them(value, conv, shown):
    """pct 和 phrases.pct 一个写法（一位小数、不带正号）；pp 是百分点（一位小数，不是 0 的 0.0 多给小数位），
    和判定语里「上升 0.4 个百分点」同一个说法。"""
    assert render_cell(value, conv) == shown


def test_a_conversion_that_shows_a_nonzero_value_as_zero_still_fails():
    with pytest.raises(ev.RenderError, match="显示为 0"):
        render_cell("0.0000001", "pp")


# --------------------------------------------------------------------------
# 各算子记的格式
# --------------------------------------------------------------------------


def test_the_additive_decomposition_formats_shares_and_rates():
    result = analyzed("decompose", "additive", by=["region"], period="week", base="2026-W41", target="2026-W42",
                      measure="gmv", label="销售额")
    assert result["table"]["formats"] == {"pct": CONV_PCT, "share": CONV_PCT}
    assert result["value_formats"] == {"pct_total": CONV_PCT, "top_share": CONV_PCT, "top_k_share": CONV_PCT}
    # 维度列、量（基期、本期、变化）没有换算
    assert set(result["table"]["formats"]) & {"region", "base", "target", "delta"} == set()


def test_the_ratio_decomposition_formats_weights_as_percent_and_effects_as_points():
    rows = [["华东", "2026-W41", 8, 100], ["华东", "2026-W42", 10, 120], ["华南", "2026-W41", 5, 130],
            ["华南", "2026-W42", 4, 140]]
    rates = snap("Q3", "SELECT region, week, refunds, orders FROM refunds GROUP BY region, week",
                 ["region", "week", "refunds", "orders"], rows,
                 types={"region": "text", "week": "text", "refunds": "number", "orders": "number"})
    result = run_sync(analyze("decompose", "ratio", snaps=[rates], query="Q3", by=["region"], period="week",
                              base="2026-W41", target="2026-W42", numerator="refunds", denominator="orders",
                              label="退款率"))
    # 权重、各段的比率是比率本身 → pct；结构效应、自身效应、合计影响是比率的变化 → pp
    assert result["table"]["formats"] == {"w_base": CONV_PCT, "w_target": CONV_PCT, "r_base": CONV_PCT,
                                          "r_target": CONV_PCT, "mix": CONV_POINTS, "rate": CONV_POINTS,
                                          "total": CONV_POINTS}
    assert result["value_formats"]["r_base_total"] == CONV_PCT and result["value_formats"]["r_target_total"] == CONV_PCT
    assert {result["value_formats"][k] for k in ("delta", "mix_total", "rate_total")} == {CONV_POINTS}
    assert result["value_formats"]["mix_share"] == CONV_PCT


def test_the_factor_decomposition_formats_change_rates_and_shares():
    product = snap("Q4", "SELECT week, gmv, users, price FROM orders", ["week", "gmv", "users", "price"],
                   [["2026-W41", 1000, 100, 10], ["2026-W42", 1188, 110, 10.8]],
                   types={"week": "text", "gmv": "number", "users": "number", "price": "number"})
    result = run_sync(analyze("decompose", "factor", snaps=[product], query="Q4", period="week", base="2026-W41",
                              target="2026-W42", product="gmv", factors=["users", "price"], label="销售额"))
    assert result["table"]["formats"] == {"pct": CONV_PCT, "share": CONV_PCT}
    assert result["value_formats"] == {"pct_total": CONV_PCT, "top_share": CONV_PCT}


def test_the_baseline_formats_change_rates_only_when_it_compares_changes():
    weekly = snap("Q1", "SELECT week, SUM(amount) AS gmv FROM orders GROUP BY week", ["week", "gmv"],
                  [[f"2026-W{k:02d}", 2200 + (k % 5) * 10] for k in range(30, 42)] + [["2026-W42", 2138]],
                  types={"week": "text", "gmv": "number"})
    changes = run_sync(analyze("baseline", None, snaps=[weekly], query="Q1", period="week", measure="gmv",
                               label="销售额", grain="week"))
    assert changes["table"]["formats"] == {"change": CONV_PCT}
    assert {"x", "median", "band_lo", "band_hi"} <= set(changes["value_formats"])
    levels = run_sync(analyze("baseline", None, snaps=[weekly], query="Q1", period="week", measure="gmv",
                              label="销售额", grain="week", basis="level"))
    # 按值本身比时是量，照原样显示（没有换算）
    assert "formats" not in levels["table"] and "value_formats" not in levels


def test_the_trend_formats_the_relative_slope():
    weekly = snap("Q1", "SELECT week, SUM(amount) AS gmv FROM orders GROUP BY week", ["week", "gmv"],
                  [[f"2026-W{k:02d}", 100 + k] for k in range(30, 42)], types={"week": "text", "gmv": "number"})
    result = run_sync(analyze("trend", None, snaps=[weekly], query="Q1", period="week", measure="gmv",
                              label="销售额", grain="week"))
    assert result["value_formats"] == {"rel_slope": CONV_PCT}


def test_the_differ_formats_rates_as_percent_and_differences_as_points():
    groups = snap("Q3", "SELECT region, refunds, orders FROM refunds GROUP BY region", ["region", "refunds", "orders"],
                  [["华东", 100, 2000], ["华南", 120, 1800], ["华北", 90, 2100], ["西南", 40, 1900]],
                  types={"region": "text", "refunds": "number", "orders": "number"})
    result = run_sync(analyze("differ", "rate", snaps=[groups], query="Q3", by=["region"], numerator="refunds",
                              denominator="orders", label="退款率"))
    assert result["table"]["formats"] == {"a_value": CONV_PCT, "b_value": CONV_PCT, "diff": CONV_POINTS,
                                          "ci_lo": CONV_POINTS, "ci_hi": CONV_POINTS}
    assert {result["value_formats"][k] for k in ("a", "b")} == {CONV_PCT}
    assert {result["value_formats"][k] for k in ("diff", "ci_lo", "ci_hi")} == {CONV_POINTS}


def test_a_run_twice_is_byte_for_byte_the_same_and_recompute_matches():
    """格式是结论的一部分：同样的输入两次 run 逐字节相同，复算和封存的逐字段相同（analysis_samples.analyze 顺带核对复算）。"""
    first = analyzed("decompose", "additive", by=["region"], period="week", base="2026-W41", target="2026-W42",
                     measure="gmv", label="销售额")
    second = analyzed("decompose", "additive", by=["region"], period="week", base="2026-W41", target="2026-W42",
                      measure="gmv", label="销售额")
    assert artifact_store.canonical_json(first) == artifact_store.canonical_json(second)
    assert first["table"]["formats"] == second["table"]["formats"]


# --------------------------------------------------------------------------
# 引用：没写换算时取列 / 数的格式，写了照写作者的
# --------------------------------------------------------------------------


def test_citing_a_share_or_rate_without_a_conversion_uses_the_conclusion_format():
    store, catalog = catalog_of()
    doc = compose_doc("华东占 [[a:A1.top_share]]，变化率 [[v:A1.r0.pct]]，占比 [[v:A1.r0.share]]，"
                      "变化量 [[v:A1.r0.delta]]。[[see:A1]]", catalog, loader=store.load)
    shown = {s["ref"]: s["text"] for s in iter_segments(doc) if s.get("ref")}
    assert shown["a:A1.top_share"] == "61.9%"
    assert shown["v:A1.r0.pct"] == "-14.6%" and shown["v:A1.r0.share"] == "61.9%"
    assert shown["v:A1.r0.delta"] == "-120"        # 量照原样
    cites = {s["ref"]: s["cite"] for s in iter_segments(doc) if s.get("ref")}
    # 实际用的换算记在引用上（导出页、可验证报告照它渲染）
    assert cites["a:A1.top_share"]["conv"] == "pct" and cites["v:A1.r0.share"]["conv"] == "pct"
    assert "conv" not in cites["v:A1.r0.delta"]
    assert doc["cell_display"] == 4 and doc["violations"] == []
    assert verify_doc(doc, catalog, loader=store.load)["ok"]


def test_a_writer_supplied_conversion_wins_over_the_conclusion_format():
    store, catalog = catalog_of()
    cite = resolve_ref("a:A1.top_share|.2", catalog, loader=store.load)
    assert cite["rendered"] == "0.62" and cite["conv"] == ".2"
    cell = resolve_ref("v:A1.r0.share|pct", catalog, loader=store.load)
    assert cell["rendered"] == "61.9%"


def test_a_whole_table_is_expanded_with_the_conclusion_formats():
    store, catalog = catalog_of()
    doc = compose_doc("[[table:A1 cols=region,delta,pct,share rows=0-1]]", catalog, loader=store.load)
    assert "| region | 变化 | 变化率 | 占变化的比重 |" in doc["markdown"]
    assert "| 华东 | -120 | -14.6% | 61.9% |" in doc["markdown"]
    assert "| 华北 | -40 | -6.7% | 20.6% |" in doc["markdown"]
    assert verify_doc(doc, catalog, loader=store.load)["ok"]


def test_an_old_document_keeps_the_raw_decimals_and_still_verifies(monkeypatch):
    """3 版及以前组装的文档：引用上是 0.618556701 这样的原样，复核按记下的版本渲染，不判对不上；按新规则重渲染会判对不上。"""
    store, catalog = catalog_of()
    monkeypatch.setattr(ev, "CELL_DISPLAY", 3)
    old = compose_doc("占比 [[a:A1.top_share]]，[[v:A1.r0.share]]。[[see:A1]]", catalog, loader=store.load)
    monkeypatch.undo()
    assert "cell_display" not in old
    shown = {s["ref"]: s["text"] for s in iter_segments(old) if s.get("ref")}
    assert shown["a:A1.top_share"] == "0.618556701" and shown["v:A1.r0.share"] == "0.618556701"
    assert verify_doc(old, catalog, loader=store.load)["ok"]
    # 拿 4 版复核这份老文档：引用上的字还是原样，照样对得上（引用上没记换算，按引用上的字渲染）
    assert {s["ref"]: s["cite"].get("conv") for s in iter_segments(old) if s.get("ref")} == \
        {"a:A1.top_share": None, "v:A1.r0.share": None}
    # 反过来：4 版组装的文档按 3 版复核，引用上的字是 61.9%，老规则渲染出来是原样，判对不上
    now = compose_doc("占比 [[a:A1.top_share]]。[[see:A1]]", catalog, loader=store.load)
    mismatched = verify_doc({**now, "cell_display": 3}, catalog, loader=store.load)
    assert mismatched["ok"] is False
    assert [v["code"] for v in mismatched["violations"]] == ["render_mismatch"]


# --------------------------------------------------------------------------
# 写作目录
# --------------------------------------------------------------------------


def test_the_writer_catalog_shows_the_shares_as_percentages():
    store, catalog = catalog_of()
    text = writer_catalog(catalog, loader=store.load).text
    section = text[text.index("分析结论（"):text.index("查询结果（")]
    assert "top_share=61.9%" in section and "top_share=0.618" not in section
    assert "r0：region=华东，base=820，target=700，delta=-120，pct=-14.6%，share=61.9%" in section
    assert "份额、变化率不写换算也按百分数显示" in section or "不写换算也按百分数显示" in section


# --------------------------------------------------------------------------
# 证据面板接口
# --------------------------------------------------------------------------


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=asgi), base_url="http://test") as c:
        yield c


async def test_the_panel_shows_the_named_value_and_the_table_cells_as_written(client):
    made = await sealed_run("[[a:A1]]。占比 [[a:A1.top_share]]，[[v:A1.r0.share]]。[[see:A1]]")
    body = (await client.get(
        f"/api/runs/{made['run']}/evidence/segments/{seg_id(made['doc'], 'a:A1.top_share')}")).json()
    calc = body["chain"][2]
    top = next(v for v in calc["values"] if v["key"] == "top_share")
    assert top["raw"] == "0.618556701031" and top["rendered"] == "61.9%"
    rows = calc["table"]["rows"]
    share = calc["table"]["columns"].index("share")
    rate = calc["table"]["columns"].index("pct")
    flag = calc["table"]["columns"].index("flag")
    assert [r[share] for r in rows] == ["61.9%", "20.6%", "17.5%", "0.0%"]
    assert rows[0][rate] == "-14.6%" and rows[0][flag] == "—"      # 空格写「—」，不写 null
    assert body["chain"][0]["render_ok"] is True


# --------------------------------------------------------------------------
# 导出页、可验证报告
# --------------------------------------------------------------------------


NODE_NS = {"__name__": "node-check"}
try:
    import shutil
    import subprocess
    from pathlib import Path

    import app.bundle
    import app.report_export
    NODE = shutil.which("node")
except Exception:  # pragma: no cover
    NODE = None

RUNTIME = Path(app.report_export.__file__).parent / "runtime.js" if NODE else None
VERIFIER = Path(app.bundle.__file__).parent / "verifier.js" if NODE else None
DOM = Path(__file__).parent.joinpath("test_analysis_pages.py").read_text(encoding="utf-8").split('DOM = r"""', 1)[1] \
    .split('"""', 1)[0]


def run_node(script: str, path: Path, payload: dict[str, Any]) -> Any:
    done = subprocess.run([NODE, "-e", DOM + script, str(path)], input=json.dumps(payload), capture_output=True,
                          text=True, timeout=120)
    assert done.returncode == 0, done.stderr[-3000:]
    return json.loads(done.stdout)


RUN_RUNTIME = r"""
document.getElementById('report-data').textContent = input.data
new Function(fs.readFileSync(process.argv[1], 'utf8'))()
const out = {}
for (const seg of input.segs) {
  const el = new El_('button')
  for (const [k, v] of Object.entries(seg)) el.setAttribute(`data-${k}`, v)
  el.textContent = seg.text
  for (const f of document.getElementById('report').listeners.click || []) f({ target: el })
  out[seg.seg] = document.getElementById('detail-body').textContent
}
process.stdout.write(JSON.stringify(out))
"""

RUN_VERIFIER = r"""
document.getElementById('bundle-data').textContent = input.data
document.getElementById('seal-hash').textContent = input.seal
new Function(fs.readFileSync(process.argv[1], 'utf8'))()
setTimeout(() => {
  const reports = document.getElementById('reports')
  const out = { opened: {} }
  for (const btn of find(reports, (n) => n.attrs['data-num'])) {
    for (const f of btn.listeners.click || []) f()
    out.opened[btn.attrs['data-num']] = document.getElementById('detail-body').textContent
  }
  out.verified = document.documentElement.attrs['data-verified'] ?? null
  process.stdout.write(JSON.stringify(out))
}, 200)
"""


@pytest.mark.skipif(NODE is None, reason="没有 node，无法执行页面上的脚本")
async def test_the_export_page_shows_percentages_in_the_calculation_step():
    made = await sealed_run("[[a:A1]]。占比 [[a:A1.top_share]]，[[v:A1.r0.share]]。[[see:A1]]")
    async with AsyncClient(transport=ASGITransport(app=asgi), base_url="http://test") as c:
        html = (await c.get(f"/api/runs/{made['run']}/report.html")).text
    import re

    data = re.search(r'<script type="application/json" id="report-data">(.*?)</script>', html, re.S).group(1)
    phrase = seg_id(made["doc"], "a:A1")
    out = run_node(RUN_RUNTIME, RUNTIME, {"data": data, "segs": [
        {"seg": phrase, "kind": "analysis", "state": "deterministic", "text": made["result"]["phrase"]}]})
    body = out[phrase].replace(" ", "").replace(" ", "")
    # 具名的数、结果表的格按结论记的格式写：top_share 61.9%、变化率 -14.6%，没有原始小数、没有 null
    assert "top_share61.9%" in body and "top_k_share82.5%" in body
    assert "占变化的比重" in body and "-14.6%" in body
    assert "0.618556701" not in body and "null" not in body


@pytest.mark.skipif(NODE is None, reason="没有 node，无法执行页面上的脚本")
def test_the_verifiable_report_renders_the_conclusion_format():
    bundle, _, _ = bundle_of("## 异动\n\n[[a:A1]]。华东占 [[a:A1.top_share]]，占比 [[v:A1.r0.share]]。[[see:A1]]")
    out = run_node(RUN_VERIFIER, VERIFIER, {"data": json.dumps(bundle), "seal": bundle["meta"]["manifest_hash"]})
    assert out["verified"] == ""
    share = next(text for text in out["opened"].values() if "具名的数「top_share」" in text)
    assert "正文显示61.9%（与按显示规则渲染的结果一致）" in share
    cell = next(text for text in out["opened"].values() if "结果表 · 第 1 行 · share" in text)
    assert "正文显示61.9%" in cell