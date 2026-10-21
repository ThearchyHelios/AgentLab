"""分析结论进证据链（docs/design/analysis-round/00-总方案.md §3.3，第 1 波 W1-b）：引擎这一层的纯函数。

- 写作目录按台账编 A1、A2……（只收报告祖先里的），条目带着从工件取回、复验过哈希的整份结论（contract.catalog_result）；
  存进报告文档的目录只留摘要
- [[a:A1]] 渲染判定语（片段种类 analysis，整段涂白、不算裸数字），[[a:A1.键|换算]] 渲染具名标量，[[v:A1.r0.列]]、
  [[table:A1 …]] 取结果表（表头写给人看的列名），[[see:A1]] 当依据
- 受管级别的正式出具、契约没开「单元格引用」时，Agent 现场做的分析和它查的格同一个规矩，分析卡做的照常引用（D10）
- 写作目录的「分析结论」一段：判定语、判定标准、具名的数、结果表前 table_top_k 段、附带说明、算不了的原因、来自哪几个 Q，
  放不下时结果表的各段排在查询的行前面补
- 分析结论的输入查询上的 SQL 检查问题随 A 条目出现；口径卡指标上的可比性照实写进写作目录
- 复算（analysis_recheck）、代入式（analysis_formulas）：用 app.analysis 真算的结论（run 交的输入照分析入口的写法，复算交
  工件原内容），也用 fake_result 测目录和引用的写法
"""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from typing import Any

import pytest

from app import analysis
from app.analysis import contract
from app.analysis.contract import CallError, effective_settings, ledger_entry
from app.analysis.testing import fake_result
from app.core.artifact_store import canonical_json, content_hash
from app.engine.evidence import (
    ANALYSIS_CELLS_REASON,
    StreamRenderer,
    analysis_formulas,
    analysis_recheck,
    build_catalog,
    compose_doc,
    iter_segments,
    render_markers,
    resolve_ref,
    verify_doc,
    writer_catalog,
)
from app.engine.sql_problems import query_problems

#: 虚构零售库 shop：四个地区两周的销售额
SNAPSHOT: dict[str, Any] = {
    "columns": ["region", "week", "gmv"], "column_types": {"region": "text", "week": "text", "gmv": "number"},
    "rows": [["华东", "W41", 820], ["华东", "W42", 700], ["华北", "W41", 600], ["华北", "W42", 560],
             ["华南", "W41", 700], ["华南", "W42", 666], ["西南", "W41", 100], ["西南", "W42", 100]],
    "sql": "SELECT region, week, SUM(gmv) AS gmv FROM orders GROUP BY region, week ORDER BY region, week",
    "source": "shop", "truncated": False, "row_count": 8,
}


class Store(dict):
    """内容寻址的假工件库：put 落一份、交回 sha256；loader 是 artifact_store.load 的约定（取不到 None）。"""

    def put(self, obj: Any) -> str:
        sha = content_hash(canonical_json(obj))
        self[sha] = obj
        return sha

    def load(self, artifact: str) -> Any:
        return self.get(artifact)


#: 运行当天（服务端时区）：真算的结论记进 params.today，复算按它
TODAY = date(2026, 10, 21)
#: 按 SNAPSHOT 真算一份按地区拆的可加拆解
REQUEST: dict[str, Any] = {
    "op": "decompose", "mode": "additive", "hypothesis": "华东拖累",
    "params": {"query": "Q2", "by": ["region"], "period": "week", "base": "W41", "target": "W42", "measure": "gmv",
               "label": "销售额"}}
#: 华东按渠道再拆一层（下钻）：两个渠道之和 820 → 700，和 A1 里华东那一段闭合
CHANNELS: dict[str, Any] = {
    "columns": ["channel", "week", "gmv"], "column_types": {"channel": "text", "week": "text", "gmv": "number"},
    "rows": [["线上", "W41", 500], ["线上", "W42", 410], ["线下", "W41", 320], ["线下", "W42", 290]],
    "sql": "SELECT channel, week, SUM(gmv) AS gmv FROM orders WHERE region = '华东' GROUP BY channel, week",
    "source": "shop", "truncated": False, "row_count": 4,
}


def input_snapshot(content: dict[str, Any], *, ref: str, artifact: str, node_id: str = "fetch") -> dict[str, Any]:
    """照分析入口（tools/analysis.input_snapshot）的写法交给 run：列的类型按列序写成列表，加上出处，遮罩列留空。复算交的是
    工件原内容（类型是 {列: 类型}）：两边算出来要逐字段相同。"""
    types = content.get("column_types") or {}
    return {**content, "ref": ref, "artifact": artifact, "node_id": node_id,
            "column_types": [types.get(c) for c in content["columns"]], "masked": []}


async def analyzed(snapshots: dict[str, tuple[dict[str, Any], str]], request: dict[str, Any] | None = None, *,
                   parents: dict[str, Any] | None = None, hypothesis: str | None = None,
                   **params: Any) -> dict[str, Any]:
    """app.analysis.run 真算一份结论。snapshots：{调用里写的名字: (快照内容, 工件 id)}；params 盖在请求的参数上，hypothesis
    换掉请求里 agent 写的假设（不进复算的比较，用来让一份结论的内容独一份）。"""
    request = request or REQUEST
    by_ref = {ref: input_snapshot(content, ref=ref, artifact=sha) for ref, (content, sha) in snapshots.items()}
    asked = {**request, "params": {**request["params"], **params}}
    if hypothesis is not None:
        asked["hypothesis"] = hypothesis
    return dict(await analysis.run(asked, by_ref.get, settings=effective_settings(None), context={"today": TODAY},
                                   parents=(parents or {}).get))


def run_sync(coro: Any) -> Any:
    """在同步的地方等一个协程：没有正在跑的事件循环就直接 asyncio.run，协程测试里调用（已经在循环里）换一个线程跑。"""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with ThreadPoolExecutor(1) as pool:
        return pool.submit(asyncio.run, coro).result()


def real_result(q: str, snapshot: dict[str, Any] | None = None, **params: Any) -> dict[str, Any]:
    """同步的 analyzed：按 SNAPSHOT（工件 q）真算 REQUEST。"""
    return run_sync(analyzed({"Q2": (snapshot or SNAPSHOT, q)}, **params))


def setup(*, item: str | None = "drop", call_id: str | None = None, result: dict[str, Any] | None = None,
          snapshot: dict[str, Any] | None = None) -> tuple[Store, list[dict[str, Any]], str, str]:
    """一次查询（Q1）加一份按地区拆的分析结论（A1）：(工件库, 台账, 查询快照, 分析结论)。"""
    store = Store()
    q = store.put(snapshot or SNAPSHOT)
    res = result or fake_result(inputs=[{"ref": "Q2", "artifact": q, "role": "data",
                                         "columns": ["region", "week", "gmv"], "rows": "all"}])
    a = store.put(res)
    ledger = [{"kind": "query", "node_id": "fetch", "exec": 1, "artifact": q, "tool": "db_query__shop",
               "source": "shop", "columns": ["region", "week", "gmv"], "rows": 8, "truncated": False},
              ledger_entry(res, node_id="an", exec_no=1, artifact=a, item=item, call_id=call_id)]
    return store, ledger, q, a


def catalog_of(**kw: Any) -> tuple[Store, dict[str, Any]]:
    store, ledger, _, _ = setup(**kw)
    return store, build_catalog(nodes={}, ledger=ledger, loader=store.load)


def cites(doc: dict[str, Any]) -> list[tuple[str, str, str | None]]:
    return [(s["kind"], s["text"], s.get("issue")) for s in iter_segments(doc) if s.get("ref")]


# --------------------------------------------------------------------------
# 契约里补的两样：叫法、按形状认分析结论
# --------------------------------------------------------------------------


def test_method_labels_and_result_shapes():
    from app.analysis.contract import OP_LABELS, OPS, is_result, method_label

    assert set(OP_LABELS) == set(OPS)
    assert method_label("decompose", "additive") == "变化拆解（可加）" and method_label("baseline", None) == "波动基线"
    assert method_label("differ", "rate") == "差异检验（比率）" and method_label("odd", "x") == "odd（x）"
    assert is_result(fake_result()) and is_result(fake_result("trend", verdict="rising"))
    assert not is_result(SNAPSHOT) and not is_result({"op": "decompose", "method": 1, "inputs": []})
    assert not is_result(None) and not is_result([fake_result()])


# --------------------------------------------------------------------------
# 写作目录的 A 条目
# --------------------------------------------------------------------------


def test_analyses_are_numbered_by_ledger_order_and_carry_the_whole_result():
    store, ledger, q, a = setup()
    second = fake_result("baseline", verdict="within_band", inputs=[{"ref": "Q1", "artifact": q, "role": "data"}])
    b = store.put(second)
    ledger.append(ledger_entry(second, node_id="trend_agent", exec_no=1, artifact=b, call_id="call-1"))
    catalog = build_catalog(nodes={}, ledger=ledger, loader=store.load)

    assert [k for k, e in catalog.items() if e["kind"] == "analysis"] == ["A1", "A2"]
    first = catalog["A1"]
    assert first["result"] is store[a] and first["item"] == "drop" and "call_id" not in first
    assert first["op"] == "decompose" and first["verdict"] == "driver" and first["inputs"] == [q] and first["rows"] == 4
    assert first["label"].startswith("变化拆解（可加） · 销售额下降")
    assert catalog["A2"]["call_id"] == "call-1" and catalog["A2"]["label"].startswith("波动基线 · ")


def test_only_analyses_from_allowed_nodes_get_a_number():
    store, ledger, q, _ = setup()
    late = fake_result(inputs=[{"ref": "Q1", "artifact": q, "role": "data"}], phrase="下游的结论")
    ledger.insert(0, ledger_entry(late, node_id="late", exec_no=1, artifact=store.put(late), item="x"))
    catalog = build_catalog(nodes={}, ledger=ledger, loader=store.load, allowed={"fetch", "an"})
    assert [k for k, e in catalog.items() if e["kind"] == "analysis"] == ["A1"]
    assert catalog["A1"]["node_id"] == "an"


def test_unreadable_results_are_listed_with_the_reason_and_cannot_be_cited():
    store, ledger, _, a = setup()
    store[a] = {**store[a], "phrase": "被改过的判定"}

    def tampered(artifact: str) -> Any:
        if artifact == a:
            raise ValueError("哈希不一致")
        return store.get(artifact)

    catalog = build_catalog(nodes={}, ledger=ledger, loader=tampered)
    assert "result" not in catalog["A1"] and catalog["A1"]["unreadable"] == "A1 的分析结论与哈希不一致，疑似被修改"
    cite = resolve_ref("a:A1", catalog, loader=tampered)
    assert cite["status"] == "unresolved" and cite["reason"] == "A1 的分析结论与哈希不一致，疑似被修改"

    gone = build_catalog(nodes={}, ledger=ledger, loader=lambda _: None)
    assert gone["A1"]["unreadable"] == "A1 的分析结论已不存在，无法读取"
    shaped = build_catalog(nodes={}, ledger=ledger, loader=lambda art: {"rows": []} if art == a else store.get(art))
    assert shaped["A1"]["unreadable"] == "A1 的工件不是分析结论"


def test_evidence_from_filters_analyses_by_the_node_that_made_them():
    from app.engine.nodes.report import report_catalog
    from app.engine.schema import GraphSpec

    store, ledger, _, _ = setup()

    def node(nid: str, ntype: str, **config: Any) -> dict[str, Any]:
        return {"id": nid, "type": ntype, "position": {"x": 0, "y": 0}, "data": {"label": nid, "config": config}}

    spec = GraphSpec.model_validate({
        "nodes": [node("fetch", "tool", tool="x"), node("an", "tool", tool="x"),
                  node("write", "report", evidence_from=["fetch"])],
        "edges": [{"source": "fetch", "target": "an"}, {"source": "an", "target": "write"}]})
    write = spec.node_map()["write"]
    state = {"nodes": {}, "evidence": ledger, "input": {}}
    from unittest.mock import patch

    with patch("app.engine.evidence.load_artifact", store.load):
        assert set(report_catalog(state, spec, write)) == {"Q1"}
        write.config["evidence_from"] = ["fetch", "an"]
        assert set(report_catalog(state, spec, write)) == {"Q1", "A1"}
        write.config["evidence_from"] = "ancestors"
        assert set(report_catalog(state, spec, write)) == {"Q1", "A1"}


# --------------------------------------------------------------------------
# 引用的解析和渲染
# --------------------------------------------------------------------------


def test_the_judgment_phrase_is_one_system_rendered_segment_and_not_bare_numbers():
    store, catalog = catalog_of()
    doc = compose_doc("[[a:A1]]。[[see:A1]]", catalog, loader=store.load)
    [seg] = [s for s in iter_segments(doc) if s.get("ref")]
    assert seg["kind"] == "analysis" and seg["state"] == "deterministic"
    assert seg["text"] == "销售额下降 194 万（-8.7%），主要来自华东（-120 万，占 62%）"
    assert seg["cite"]["kind"] == "analysis" and seg["cite"]["role"] == "value" and seg["cite"]["locator"] == {}
    # 判定语里的 194、8.7、120、62 都是系统写的：不算裸数字
    assert doc["violations"] == [] and doc["stats"]["uncited_numbers"] == 0
    assert doc["blocks"][0]["units"][0]["cites"] == ["A1"]
    assert verify_doc(doc, catalog, loader=store.load)["ok"]


def test_named_values_render_like_cells_with_the_metric_conversions():
    store, catalog = catalog_of()
    doc = compose_doc("华东占 [[a:A1.top_share|pct]]，总变化 [[a:A1.delta_total]]，最大的是 [[a:A1.top_key]]。",
                      catalog, loader=store.load)
    assert cites(doc) == [("number", "61.9%", None), ("number", "-194", None), ("value", "华东", None)]
    share = next(s for s in iter_segments(doc) if s.get("ref") == "a:A1.top_share|pct")
    assert share["cite"]["locator"] == {"key": "top_share"} and share["cite"]["conv"] == "pct"
    assert share["cite"]["value"] == pytest.approx(0.6186)
    assert verify_doc(doc, catalog, loader=store.load)["ok"]


@pytest.mark.parametrize("ref, reason", [
    ("a:A9", "引用的分析结论 A9 不存在"),
    ("a:A1.nope", "A1 中没有名为「nope」的数"),
    ("a:A1|pct", "判定语是一整句话，不能换算"),
    ("a:A1.top_key|pct", "「华东」不是数值，不能换算为「pct」"),
    ("a:Q1", "分析结论的引用格式无法识别"),
    ("a:A1.top_share.x", "分析结论的引用格式无法识别"),
    ("v:A1.r9.share", "A1 请求第 9 行，但结果表只有 4 行（行号从 0 开始）"),
    ("v:A1.r0.nope", "A1 没有列「nope」，可用的列：region、base、target、delta、pct、share、flag"),
    ("v:A9.r0.share", "引用的分析结论 A9 不存在"),
])
def test_wrong_analysis_references_say_why(ref, reason):
    store, catalog = catalog_of()
    cite = resolve_ref(ref, catalog, loader=store.load)
    assert cite["status"] == "unresolved" and cite["reason"] == reason


def test_a_q_alias_is_not_an_analysis():
    store, catalog = catalog_of()
    catalog["A2"] = dict(catalog["Q1"])
    assert resolve_ref("a:A2", catalog, loader=store.load)["reason"] == "A2 不是分析结论"


def test_result_table_cells_and_whole_tables_use_the_labels_as_headers():
    store, catalog = catalog_of()
    doc = compose_doc("华东变化 [[v:A1.r0.delta]]。\n\n[[table:A1 cols=region,delta,share rows=0-1]]",
                      catalog, loader=store.load)
    assert "| 地区 | 变化 | 占变化的比重 |" in doc["markdown"]
    assert ("number", "-120", None) in cites(doc) and ("value", "华北", None) in cites(doc)
    cell = next(s for s in iter_segments(doc) if s.get("ref") == "v:A1.r0.delta")
    assert cell["cite"]["kind"] == "cell" and cell["cite"]["alias"] == "A1"
    assert cell["cite"]["locator"] == {"row": 0, "column": "delta"}
    assert doc["violations"] == []
    assert verify_doc(doc, catalog, loader=store.load)["ok"]


def test_header_labels_with_digits_are_not_bare_numbers():
    result = fake_result()
    result["table"]["labels"] = {**result["table"]["labels"], "base": "基期（2026-W41）", "target": "本期（2026-W42）"}
    store, ledger, q, _ = setup(result={**result, "inputs": [{"ref": "Q2", "artifact": "x" * 64, "role": "data"}]})
    catalog = build_catalog(nodes={}, ledger=ledger, loader=store.load)
    doc = compose_doc("[[table:A1 cols=region,base,target rows=0-1]]", catalog, loader=store.load)
    assert "| 地区 | 基期（2026-W41） | 本期（2026-W42） |" in doc["markdown"]
    assert doc["violations"] == [] and verify_doc(doc, catalog, loader=store.load)["ok"]


def test_dimension_values_with_digits_written_as_text_are_names():
    result = fake_result()
    result["table"]["rows"][0][0] = "华东3区"
    store, ledger, _, _ = setup(result=result, snapshot={**SNAPSHOT, "rows": [["华北", "W41", 1]]})
    catalog = build_catalog(nodes={}, ledger=ledger, loader=store.load)
    doc = compose_doc("下降最多的是华东3区。[[see:A1]]", catalog, loader=store.load)
    assert doc["violations"] == []


def test_see_accepts_analyses_rows_and_the_a_prefix():
    store, catalog = catalog_of()
    doc = compose_doc("下降主要来自华东。[[see:A1]] 第一段同理。[[see:A1.r0,a:A1]] 越界。[[see:A1.r9]]",
                      catalog, loader=store.load)
    sees = [c for _, u in __import__("app.engine.evidence", fromlist=["iter_units"]).iter_units(doc) for c in u["see"]]
    assert [(c["alias"], c["status"]) for c in sees] == [("A1", "resolved"), ("A1", "resolved"), ("A1", "resolved"),
                                                        ("A1.r9", "unresolved")]
    assert sees[-1]["reason"] == "A1 只有 4 行，没有第 9 行（行号从 0 开始）"
    assert sees[0]["kind"] == "analysis"


def test_the_doc_keeps_a_summary_and_reloads_the_result_from_the_artifact():
    store, catalog = catalog_of()
    doc = compose_doc("[[a:A1]]，华东占 [[a:A1.top_share|pct]]。", catalog, loader=store.load)
    stored = doc["catalog"]["A1"]
    assert "result" not in stored and stored["artifact"] and stored["rows"] == 4 and stored["verdict"] == "driver"
    # 文档里的摘要照样解析得了：结论按工件取回
    assert resolve_ref("a:A1", doc["catalog"], loader=store.load)["status"] == "resolved"
    assert verify_doc(doc, doc["catalog"], loader=store.load)["ok"]


def test_a_tampered_phrase_in_the_doc_is_a_render_mismatch():
    store, catalog = catalog_of()
    doc = compose_doc("[[a:A1]]。", catalog, loader=store.load)
    seg = next(s for s in iter_segments(doc) if s.get("ref"))
    seg["text"] = seg["cite"]["rendered"] = seg["text"].replace("华东", "华南")
    doc["markdown"] = doc["markdown"].replace("华东", "华南")
    codes = [v["code"] for v in verify_doc(doc, catalog, loader=store.load)["violations"]]
    assert "render_mismatch" in codes


def test_streaming_renders_the_phrase_like_the_whole_text():
    store, catalog = catalog_of()
    text = "结论：[[a:A1]]。华东占 [[a:A1.top_share|pct]]%。[[see:A1]]"
    whole = render_markers(text, catalog, loader=store.load)
    for size in (1, 3, 7):
        renderer = StreamRenderer(catalog, loader=store.load)
        out = "".join(renderer.feed(text[i:i + size]) for i in range(0, len(text), size)) + renderer.flush()
        assert out == whole
    # 具名标量后面重复写的单位去掉（同指标）
    assert "61.9%。" in whole and "61.9%%" not in whole


# --------------------------------------------------------------------------
# 受管级别的出具：Agent 现场做的分析和它查的格同一个规矩，分析卡做的总能引用（D10）
# --------------------------------------------------------------------------


@pytest.mark.parametrize("ref", ["a:A1", "a:A1.top_share|pct", "v:A1.r0.share", "table:A1 cols=region,share"])
def test_agent_analyses_follow_the_cells_rule_when_cells_are_off(ref):
    store, catalog = catalog_of(item=None, call_id="call-7")
    cite = resolve_ref(ref, catalog, cells_allowed=False, loader=store.load)
    assert cite["status"] == "unresolved" and cite["reason"] == ANALYSIS_CELLS_REASON
    assert resolve_ref(ref, catalog, cells_allowed=True, loader=store.load)["status"] == "resolved"


@pytest.mark.parametrize("ref", ["a:A1", "a:A1.top_share|pct", "v:A1.r0.share", "table:A1 cols=region,share"])
def test_card_analyses_are_always_citable(ref):
    store, catalog = catalog_of(item="drop")
    assert resolve_ref(ref, catalog, cells_allowed=False, loader=store.load)["status"] == "resolved"


def test_query_cells_keep_their_reason_and_see_stays_allowed():
    store, catalog = catalog_of(item=None, call_id="call-7")
    from app.engine.evidence import CELLS_REASON, resolve_support

    assert resolve_ref("v:Q1.r0.gmv", catalog, cells_allowed=False, loader=store.load)["reason"] == CELLS_REASON
    assert resolve_support("A1", catalog, loader=store.load)["status"] == "resolved"
    doc = compose_doc("下降主要来自华东。[[see:A1]]", catalog, cells_allowed=False, loader=store.load)
    assert doc["violations"] == []


# --------------------------------------------------------------------------
# 写作目录
# --------------------------------------------------------------------------


def test_writer_catalog_lists_the_analysis_with_how_to_cite_it():
    store, catalog = catalog_of()
    text = writer_catalog(catalog, loader=store.load).text
    section = text[text.index("分析结论（"):text.index("查询结果（")]
    assert "判定语单独写 [[a:A1]]" in section and "[[see:A1]]" in section
    assert "- A1：变化拆解（可加），来自 Q1" in section
    assert "判定语 [[a:A1]] = 销售额下降 194 万（-8.7%），主要来自华东（-120 万，占 62%）" in section
    assert "判定标准：一段占总变化一半以上算主要来源" in section
    assert "具名的数（写 [[a:A1.键]]）：base_total=2,220，target_total=2,026，delta_total=-194" in section
    assert "结果表（共 4 段）：列 region（地区）、base（基期）" in section
    assert "r0：region=华东，base=820，target=700，delta=-120" in section
    assert "hypothesis" not in section and "地区拖累" not in section   # agent 写的假设不进写作目录
    # 分析结论排在查询前面
    assert text.index("分析结论（") < text.index("- Q1：")


def test_writer_catalog_shows_only_the_top_segments_and_says_how_many_are_left():
    result = fake_result(settings={**fake_result()["settings"], "table_top_k": 3})
    store, catalog = catalog_of(result=result)
    text = writer_catalog(catalog, loader=store.load).text
    assert "r2：region=华南" in text and "r3：" not in text.split("查询结果（")[0]
    assert "…其余 1 段（这里只列出前 3 段；后面的行也能按行号引用）" in text


def test_refused_results_say_why_and_warn_off_judgments():
    result = fake_result("baseline", status="refused", reason_code="history_short",
                         caveats=[{"code": "weekday_effect", "text": "日数据有星期效应"}])
    store, catalog = catalog_of(result=result)
    text = writer_catalog(catalog, loader=store.load).text
    assert "判定语 [[a:A1]] = 变化拆解没有做成：历史只有 5 期，至少要 8 期" in text
    assert "算不了：历史只有 5 期，至少要 8 期。这条结论不能当「异常」「主要来自」「呈上升趋势」这类判断的依据" in text
    assert "附带说明：日数据有星期效应" in text
    assert "结果表（共" not in text and "具名的数（写" not in text


def test_agent_analyses_are_see_only_when_cells_are_off():
    store, catalog = catalog_of(item=None, call_id="call-7")
    text = writer_catalog(catalog, cells_allowed=False, loader=store.load).text
    assert "只能在句末写 [[see:A1]] 作依据" in text
    assert "具名的数（写" not in text and "r0：region=华东" not in text
    assert "这次出具不允许直接引用 Agent 现场做的分析：标着只能当依据的几条" in text
    card_store, card = catalog_of(item="drop")
    shown = writer_catalog(card, cells_allowed=False, loader=card_store.load).text
    assert "具名的数（写" in shown and "标着只能当依据的几条" not in shown


def test_drill_downs_and_inputs_outside_the_catalog_are_named():
    store, ledger, q, a = setup()
    child = fake_result(inputs=[{"ref": "Q9", "artifact": q, "role": "data"},
                                {"ref": "A3", "artifact": a, "role": "parent"},
                                {"ref": "Q8", "artifact": "f" * 64, "role": "data"}])
    ledger.append(ledger_entry(child, node_id="an", exec_no=2, artifact=store.put(child), item="drill"))
    catalog = build_catalog(nodes={}, ledger=ledger, loader=store.load)
    text = writer_catalog(catalog, loader=store.load).text
    assert "- A2：变化拆解（可加），来自 Q1，下钻自 A1，另有 1 份输入不在这份目录里" in text


def test_unreadable_results_are_listed_as_not_citable():
    store, ledger, _, a = setup()
    catalog = build_catalog(nodes={}, ledger=ledger, loader=lambda art: None if art == a else store.get(art))
    assert "- A1：变化拆解（可加），来自 Q1（A1 的分析结论已不存在，无法读取，不要引用）" in \
        writer_catalog(catalog, loader=store.load).text


def test_trimmed_catalogs_keep_the_judgments_and_fill_result_tables_before_query_rows():
    store, ledger, q, _ = setup()
    many = [{"kind": "query", "node_id": "fetch", "exec": i + 2, "artifact": store.put({**SNAPSHOT, "sql": f"-- {i}"}),
             "tool": "db_query__shop", "source": "shop", "columns": SNAPSHOT["columns"], "rows": 8} for i in range(40)]
    catalog = build_catalog(nodes={}, ledger=[*ledger, *many], loader=store.load)
    full = writer_catalog(catalog, loader=store.load, budget=10 ** 6)
    assert not full.trimmed
    trimmed = writer_catalog(catalog, loader=store.load, budget=len(full.text) // 3)
    assert trimmed.trimmed and len(trimmed.text) <= trimmed.budget
    assert "判定语 [[a:A1]] = " in trimmed.text and "r0：region=华东，base=820" in trimmed.text
    assert trimmed.omitted                                   # 查询有省略，分析结论的结果表没被省掉

    # 更紧：判定、判定标准、具名的数照样列出，结果表退成一句，开头照实说
    tight = writer_catalog(catalog, loader=store.load, budget=2000)
    assert len(tight.text) <= 2000 and "判定语 [[a:A1]] = " in tight.text and "具名的数（写" in tight.text
    assert "r0：region=华东，base=820" not in tight.text
    assert "结果表共 4 段，列 region（地区）" in tight.text and "要列出就单独一行写 [[table:A1]]" in tight.text
    assert "另有 1 条分析结论，判定和具名的数都列出了，其中 1 条没列出结果表的各段" in tight.text


def test_metric_comparability_is_spoken_in_the_writer_catalog():
    card = {"kind": "metric_set", "caliber": "周报", "caliber_version": "v1", "artifact": "c" * 64, "metrics": [
        {"id": "wow", "name": "环比", "value": -0.087, "unit": "", "comparability": {
            "artifact": "d" * 64, "verdict": "incomparable", "phrase": "两期不可比：本期只有 3 天数据",
            "reasons": ["本期只有 3 天数据"]}},
        {"id": "yoy", "name": "同比", "value": 0.02, "unit": "", "comparability": {
            "artifact": "e" * 64, "verdict": "conditional", "phrase": "两期有条件可比：两段的周末天数不同",
            "reasons": ["两段的周末天数不同：4 天对 6 天"]}},
        {"id": "gmv", "name": "销售额", "value": 2026, "unit": "", "comparability": {
            "artifact": "f" * 64, "verdict": "comparable", "phrase": "两期可比", "reasons": []}}]}
    catalog = build_catalog(nodes={"card": card}, ledger=[])
    assert catalog["m:wow"]["comparability"]["verdict"] == "incomparable"
    text = writer_catalog(catalog).text
    assert "环比 = -0.087（两期不可比：本期只有 3 天数据。这个比较不能当结论，引用时须如实说明两期不可比）" in text
    assert "同比 = 0.02（两期有条件可比：两段的周末天数不同：4 天对 6 天。引用时须如实说明这几点）" in text
    assert "- [[m:gmv]] 销售额 = 2,026\n" in text + "\n"


def test_sql_check_problems_of_the_input_queries_follow_the_analysis():
    bad = {**SNAPSHOT, "checks": [{"code": "fanout_sum", "level": "error",
                                   "message": "一对多关联后求和，金额被重复计算。改成先汇总再关联"}]}
    store, ledger, q, a = setup(snapshot=bad)
    found = query_problems(a, loader=store.load)
    assert [p.artifact for p in found] == [q] and found[0].problems == ["一对多关联后求和，金额被重复计算"]
    catalog = build_catalog(nodes={}, ledger=ledger, loader=store.load)
    text = writer_catalog(catalog, loader=store.load).text
    section = text[text.index("- A1："):text.index("查询结果（")]
    assert "（这次分析的输入查询未通过 SQL 检查：一对多关联后求和，金额被重复计算。其中的数存疑" in section


# --------------------------------------------------------------------------
# 复算与代入式
# --------------------------------------------------------------------------


def test_recheck_with_the_real_operators_says_a_sealed_result_holds():
    """真算的结论按封存的输入复算（load 交工件原内容），逐字段相同；agent 写的假设不比。"""
    store = Store()
    q = store.put(SNAPSHOT)
    sealed = real_result(q)
    assert sealed["phrase"] == "销售额下降 194（-8.7%），主要来自华东（-120，占 61.9%）"
    out = analysis_recheck(sealed, store.get)
    assert out == {"status": "same", "method": "decompose.additive@1", "text": "复算一致：按封存的输入重新计算，结论逐项相同"}
    assert analysis_recheck({**sealed, "hypothesis": "别的假设"}, store.get)["status"] == "same"


def test_recheck_follows_the_parent_of_a_drill_down_by_hash():
    store = Store()
    q, c = store.put(SNAPSHOT), store.put(CHANNELS)
    top = real_result(q)
    a = store.put(top)
    child = asyncio.run(analyzed({"Q3": (CHANNELS, c)}, parents={"A1": top}, query="Q3", by=["channel"],
                                 parent={"of": "A1", "key": ["华东"]}))
    assert [i["role"] for i in child["inputs"]] == ["parent", "data"] and child["inputs"][0]["artifact"] == a
    assert analysis_recheck(child, store.get, load_result=store.get)["status"] == "same"
    # 上一层取不回：照实说取不回哪一份，不说两遍「复算」
    out = analysis_recheck(child, store.get, load_result=lambda sha: None)
    assert out == {"status": "failed", "method": "decompose.additive@1", "text": "复算失败：取不回上一层的分析结论 A1"}


def test_recheck_says_what_differs_and_what_is_missing():
    store = Store()
    q = store.put(SNAPSHOT)
    sealed = real_result(q)
    # 封存的那份和按输入算出来的不一样（判定语被改过、哈希也就是另一份）：点名哪几项不同
    out = analysis_recheck({**sealed, "phrase": "销售额下降，主要来自华南"}, store.get)
    assert out["status"] == "different" and out["fields"] == ["phrase"]
    assert out["text"] == "复算不一致：判定语和封存的不同"
    # 输入快照取不回（不在封存范围、哈希对不上的调用方都交 None）
    out = analysis_recheck(sealed, lambda sha: None)
    assert out == {"status": "failed", "method": "decompose.additive@1", "text": "复算失败：取不回 Q2 的查询快照"}
    # 输入快照换了一份（同一个 sha256 交回了别的内容）：复算照样算，结果和封存的不同
    other = {**SNAPSHOT, "rows": [["华东", "W41", 820], ["华东", "W42", 650], *SNAPSHOT["rows"][2:]]}
    assert analysis_recheck(sealed, lambda sha: other)["status"] == "different"


def test_recheck_names_both_versions_when_the_method_was_upgraded(monkeypatch):
    store = Store()
    q = store.put(SNAPSHOT)
    sealed = real_result(q)
    monkeypatch.setitem(contract.METHODS, ("decompose", "additive"), "decompose.additive@2")
    out = analysis_recheck(sealed, store.get)
    assert out == {"status": "same", "method": "decompose.additive@1", "method_now": "decompose.additive@2",
                   "text": "运行时用的是 decompose.additive@1，现在是 decompose.additive@2，复算结果相同"}
    out = analysis_recheck({**sealed, "phrase": "别的判定"}, store.get)
    assert out["status"] == "different" and out["fields"] == ["phrase"]
    assert out["text"] == "运行时用的是 decompose.additive@1，现在是 decompose.additive@2，复算结果不同"

    def refuse(result, load, *, load_result=None):
        raise CallError("unknown_mode", "变化拆解要写模式：additive / ratio / factor")

    monkeypatch.setattr(analysis, "recompute", refuse)
    out = analysis_recheck(sealed, store.get)
    assert out["status"] == "failed" and out["method_now"] == "decompose.additive@2"
    assert out["text"] == ("运行时用的是 decompose.additive@1，现在是 decompose.additive@2：现在的方法按封存的输入复算不了"
                           "（变化拆解要写模式：additive / ratio / factor）")


def test_recheck_errors_are_said_plainly(monkeypatch):
    store = Store()
    q = store.put(SNAPSHOT)
    sealed = real_result(q)
    seen: list[Any] = []

    def broken(result, load, *, load_result=None):
        seen.append(load(q))
        raise ZeroDivisionError("division by zero")

    monkeypatch.setattr(analysis, "recompute", broken)
    out = analysis_recheck(sealed, store.get)
    assert out["status"] == "failed" and out["text"].startswith("复算失败：") and seen == [SNAPSHOT]
    monkeypatch.setattr(analysis, "recompute", lambda result, load, *, load_result=None: None)
    assert analysis_recheck(sealed, store.get)["text"] == "复算失败：复算没有交回结论"


def test_formulas_substitute_the_key_numbers():
    store = Store()
    q = store.put(SNAPSHOT)
    formulas = {f["label"]: f["text"] for f in analysis_formulas(real_result(q))}
    assert formulas == {
        "总变化": "总变化 = 本期合计 − 基期合计 = 2,026 − 2,220 = -194",
        "变化率": "变化率 = 总变化 ÷ 基期合计 = -194 ÷ 2,220 = -8.7%",
        "占比": "华东的占比 = 华东的变化 ÷ 总变化 = -120 ÷ （-194） = 61.9%",
    }
    assert analysis_formulas(fake_result(status="refused", reason_code="history_short")) == []
    assert analysis_formulas(fake_result(values={})) == []


def test_formulas_for_factor_ratio_and_baseline():
    store = Store()
    factor_snap = {"columns": ["week", "gmv", "orders", "aov"],
                   "rows": [["W41", 1000, 10, 100], ["W42", 1320, 12, 110]],
                   "sql": "SELECT week, SUM(amount) AS gmv, COUNT(*) AS orders, ROUND(AVG(amount), 2) AS aov "
                          "FROM orders GROUP BY week", "source": "shop", "truncated": False}
    factor = asyncio.run(analyzed({"Q1": (factor_snap, store.put(factor_snap))},
                                  {"op": "decompose", "mode": "factor", "params": {}}, query="Q1", period="week",
                                  base="W41", target="W42", product="gmv", factors=["orders", "aov"], label="销售额"))
    texts = {f["label"]: f["text"] for f in analysis_formulas(factor)}
    assert texts["总变化"] == "总变化 = 本期合计 − 基期合计 = 1,320 − 1,000 = 320"
    assert texts["占比"].startswith("orders的占比 = orders的贡献 ÷ 总变化 = 210.1449167") and texts["占比"].endswith(" = 65.7%")

    ratio_snap = {"columns": ["channel", "week", "refunds", "orders"],
                  "rows": [["线上", "W41", 50, 1000], ["线上", "W42", 80, 1000], ["线下", "W41", 20, 1000],
                           ["线下", "W42", 26, 1000]],
                  "sql": "SELECT channel, week, SUM(refund_flag) AS refunds, COUNT(*) AS orders FROM orders "
                         "GROUP BY 1, 2", "source": "shop", "truncated": False}
    ratio = asyncio.run(analyzed({"Q1": (ratio_snap, store.put(ratio_snap))},
                                 {"op": "decompose", "mode": "ratio", "params": {}}, query="Q1", by="channel",
                                 period="week", base="W41", target="W42", numerator="refunds", denominator="orders",
                                 label="退款率"))
    assert [f["text"] for f in analysis_formulas(ratio)] == [
        "结构效应占比 = 结构效应合计 ÷ 比率的变化 = 0 ÷ 0.018 = 0.0%",
        "自身效应占比 = 自身效应合计 ÷ 比率的变化 = 0.018 ÷ 0.018 = 100.0%"]

    weeks = [f"2026-{m:02d}-{d:02d}" for m, d in ((6, 8), (6, 15), (6, 22), (6, 29), (7, 6), (7, 13), (7, 20), (7, 27),
                                                 (8, 3), (8, 10))]
    series = {"columns": ["week", "orders"], "column_types": {"week": "text", "orders": "number"},
              "rows": [[w, v] for w, v in zip(weeks, [100, 102, 98, 101, 99, 103, 100, 97, 102, 130])],
              "sql": "SELECT week, COUNT(*) AS orders FROM orders GROUP BY 1", "source": "shop", "truncated": False}
    baseline = asyncio.run(analyzed({"Q1": (series, store.put(series))}, {"op": "baseline", "mode": None, "params": {}},
                                    query="Q1", period="week", measure="orders", label="订单数"))
    assert baseline["params"]["basis"] == "change" and baseline["status"] == "ok"
    band, z = [f["text"] for f in analysis_formulas(baseline)]
    assert band == "常规波动范围 = 中位数 ± 2 倍稳健标准差 = -9.0% 至 9.0%（中位数 0.01%，过去 8 期）"
    assert z == "稳健 z = （本期 − 中位数）÷ 稳健标准差 = （27.5% − 0.01%）÷ 稳健标准差 = 6.1072657902"
    assert analysis_recheck(baseline, store.get)["status"] == "same"


# --------------------------------------------------------------------------
# 血缘（D16）：分析卡的结果不是口径卡的原料
# --------------------------------------------------------------------------


def test_metrics_reading_analysis_results_are_not_traced_to_raw_data():
    """口径卡读 nodes.<分析卡>.results 的输入：运行时（traced）、静态（static_stops）都认作追不到原始数据，停在分析卡。
    分析结论的输入只认台账里的查询快照，复算即血缘；写死 tool_cell、agent_field 的几处不用改。"""
    from app.engine.lineage import static_stops, traced, untraced

    item = {"path": "nodes.an.results.drop.values.top_share", "value": "0.6186", "node_id": "an", "via": "analysis"}
    assert not traced(item) and untraced([item]) == [item]
    stops = static_stops({"an": {"type": "analysis", "config": {}}, "fetch": {"type": "tool", "config": {}}},
                         "nodes.an.results.drop.values.top_share * 100")
    assert [(s.path, s.node_id) for s in stops] == [("nodes.an.results.drop.values.top_share", "an")]
