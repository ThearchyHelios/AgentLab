"""第二张口径卡转抄上游口径卡里取自遮罩格的指标：转抄来的指标同样不给值。

复核第 1 轮发现：卡 2 上的指标写 nodes.card.metrics[0].value、vars.kpi.metrics[0].value（卡 1 设了 assign_to）或
nodes.card.text（清单文字逐条写着「指标（名称）= 值」），取值链认不出出处（recompute.input_origin 只认查询结果），只按出处认
时直接照给：写作目录写着号码，文档的证据目录里值、渲染、标签都是号码，可验证报告里报告文档和卡 2 的节点完成事件都带着号码。

现在转抄上游口径卡的输入按转抄的那张卡认（recompute.card_input 认出转抄了哪张卡，recompute.masked_closure 在指标依赖图上
一次认，api/evidence.py 的 _CopyGraph、bundle/build.py 的 _Store.metric_word 各自按自己的口径认好转抄以外的输入交进去）：那张
卡在这次运行里有一个指标取自遮罩格，转抄它的指标就写「已遮罩」，转抄的是号码、客户数还是整张清单、数指标个数都一样（转抄的
保守规则，复核第 3 轮：只按值认的写法漏过了由遮罩格算出来的数，见 test_report_masked_copy_any_run）。那张卡上没有取自遮罩格的
指标时照常。写作目录、报告自查、可验证报告同一个判断。

真跑的部分用虚构的 SQLite 会员库（号码是编的），写作者是 MockChatModel 照剧本写。
"""
from __future__ import annotations

import json

import pytest
from httpx import ASGITransport, AsyncClient

from app.engine.evidence import iter_segments
from app.engine.runner import run_manager
from app.main import app
from tests.test_bundle_masked_metrics import data_of, event
from tests.test_report_masked_metrics import (  # noqa: F401  vip：号码设了遮罩的会员库
    PHONES,
    doc_of,
    events,
    finish,
    leaks,
    metric_lines,
    node,
    script,
    vip,
)

#: 卡 1：首位客户手机号（就是号码那一格）、客户数
CARD = [
    {"id": "phone_raw", "name": "首位客户手机号", "format": "plain", "expression": "cell(nodes.q_cust, 0, 'phone')"},
    {"id": "n_cust", "name": "客户数", "unit": "位", "format": "thousands", "expression": "len(vars.cust.rows)"},
]
#: 卡 2：转抄卡 1 的几种写法；后两个只转抄客户数、数指标个数，值里没有号码，可卡 1 上有取自号码列的指标，照样不给
COPIES = [
    {"id": "p_nodes", "name": "转抄手机号", "format": "plain", "expression": "nodes.card.metrics[0].value"},
    {"id": "p_vars", "name": "按变量转抄手机号", "format": "plain", "expression": "vars.kpi.metrics[0].value"},
    {"id": "p_text", "name": "转抄清单", "format": "plain", "expression": "nodes.card.text"},
    {"id": "n_copy", "name": "转抄客户数", "unit": "位", "format": "thousands", "expression": "nodes.card.metrics[1].value"},
    {"id": "n_metrics", "name": "卡 1 的指标个数", "unit": "个", "format": "thousands",
     "expression": "len(nodes.card.metrics)"},
]
WITHHELD = ("m:p_nodes", "m:p_vars", "m:p_text", "m:n_copy", "m:n_metrics")
#: 卡 1 自己的客户数照给、照常引用
CLEAN = "共有 [[m:n_cust]] 客户。"


@pytest.fixture
async def engine_up():
    await run_manager.setup()
    yield
    await run_manager.shutdown()


@pytest.fixture
async def client(engine_up):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


def graph(copies: list[dict] | None = None, card: list[dict] | None = None, **write) -> dict:
    """会员查询（选出号码）→ 数据整形 → 卡 1（assign_to kpi）→ 卡 2（转抄卡 1）→ 报告 → 成果。"""
    nodes = [
        node("start", "input"),
        node("q_cust", "tool", tool="db_query__vip", args={"sql": "SELECT id, name, phone FROM customers ORDER BY id"}),
        node("parse", "transform", mode="json", template="{{ nodes.q_cust }}", assign_to="cust"),
        node("card", "metrics", caliber="会员口径", caliber_version="v1", assign_to="kpi", metrics=card or CARD),
        node("card2", "metrics", caliber="转抄口径", caliber_version="v1", metrics=copies or COPIES),
        node("write", "report", instructions="写一句", **{"on_violation": "flag", **write}),
        node("out", "output", fields=[{"name": "报告", "value": "{{ nodes.write.text }}"}]),
    ]
    return {"nodes": nodes, "edges": [{"source": a["id"], "target": b["id"]} for a, b in zip(nodes, nodes[1:])]}


async def test_the_writer_and_the_doc_see_masked_for_copied_masked_metrics(monkeypatch, vip):
    """写作目录：转抄卡 1 的五种写法（转抄号码、清单文字，也包括只转抄客户数、数指标个数的）都写「已遮罩」并说不要引用——卡 1
    上有取自号码列的指标；卡 1 自己的客户数照给。整段写作目录里没有号码。文档的证据目录同样写「已遮罩」，整份文档没有号码。"""
    seen = script(monkeypatch, CLEAN)
    row = await finish(graph())
    assert row.status == "succeeded", row.error
    lines = metric_lines(seen[0])
    for name in ("转抄手机号", "按变量转抄手机号", "转抄清单", "转抄客户数", "卡 1 的指标个数"):
        assert f"{name} = 已遮罩（取自设置了遮罩的列，不要引用）" in lines, lines
    assert "客户数 = 3位" in lines, lines
    assert leaks(lines) == [], lines
    doc = await doc_of(row.id)
    for alias in WITHHELD:
        entry = doc["catalog"][alias]
        assert (entry["value"], entry["rendered"], entry.get("masked")) == ("已遮罩", "已遮罩", True), entry
    assert "masked" not in doc["catalog"]["m:n_cust"]
    assert leaks(doc) == []
    [checked] = [e.data for e in await events(row.id) if e.type == "report.checked"]
    assert checked["ok"] is True, checked


@pytest.mark.parametrize("alias", WITHHELD)
async def test_citing_a_copied_masked_metric_is_a_masked_cell_violation(monkeypatch, vip, alias):
    """引用转抄来的号码：报告自查记 masked_cell，和引用遮罩格同一套处置；违规处置为判失败的正式运行判失败，说法里不写号码。"""
    script(monkeypatch, f"首位客户的联系方式 [[{alias}]]。")
    row = await finish(graph(on_violation="fail"))
    assert row.status == "failed" and alias in (row.error or "") and "遮罩" in row.error, row.error
    assert leaks(row.error) == []
    [checked] = [e.data for e in await events(row.id) if e.type == "report.checked"][-1:]
    assert [v["ref"] for v in checked["violations"] if v["code"] == "masked_cell"] == [alias], checked["violations"]


async def test_the_bundle_carries_no_copied_number(client, monkeypatch, vip):
    """可验证报告：卡 2 只给 id、它的节点完成事件只给哈希，报告文档照常附带（目录里写的是「已遮罩」），整个文件没有号码；
    证据图的标签同样写「已遮罩」（写作者坚持引用转抄来的客户数、标注照常产出），卡 1 自己的客户数照给。"""
    script(monkeypatch, "共有 [[m:n_cust]] 客户，转抄的客户数 [[m:n_copy]]。")
    row = await finish(graph())
    assert row.status == "succeeded", row.error
    resp = await client.get(f"/api/runs/{row.id}/bundle.html")
    assert resp.status_code == 200, resp.text
    bundle = data_of(resp.text)
    assert leaks(resp.text) == [], "可验证报告里不出现号码"
    assert "canonical" not in event(bundle, "node.finished", "card2")
    [doc] = [a for a in bundle["artifacts"] if a["kind"] == "report_doc"]
    assert "b64" in doc and "masked" not in doc, doc
    cards = {a["node_id"]: a for a in bundle["artifacts"] if a["kind"] == "metric_set"}
    assert cards["card2"].get("masked") is True and "b64" not in cards["card2"], cards
    body = (await client.get(f"/api/runs/{row.id}/evidence")).json()
    labels = {e["alias"]: e["label"] for e in body["evidence"] if e["kind"] == "metric"}
    assert labels["m:n_cust"] == "客户数 = 3位" and labels["m:n_copy"] == "转抄客户数 = 已遮罩", labels
    assert leaks(body) == [], [e for e in body["evidence"] if leaks(e)]


async def test_a_run_without_a_report_does_not_carry_the_copied_number(client, monkeypatch, vip):
    """没有报告节点的运行：卡 2 的节点完成事件（预览逐条写着「指标（名称）= 值」）只给哈希，文件里没有号码。卡 2 只转抄
    客户数、卡 1 上也没有号码的，卡 2 的节点完成事件照常带规范串。"""
    spec = graph()
    spec["nodes"] = [n for n in spec["nodes"] if n["id"] != "write"]
    spec["nodes"][-1]["data"]["config"]["fields"] = [{"name": "说明", "value": "完成"}]
    spec["edges"] = [{"source": a["id"], "target": b["id"]} for a, b in zip(spec["nodes"], spec["nodes"][1:])]
    row = await finish(spec)
    assert row.status == "succeeded", row.error
    resp = await client.get(f"/api/runs/{row.id}/bundle.html")
    assert resp.status_code == 200, resp.text
    bundle = data_of(resp.text)
    assert "canonical" not in event(bundle, "node.finished", "card2")
    assert leaks(resp.text) == []

    plain = [{**c, "expression": c["expression"].replace("metrics[1]", "metrics[0]")} for c in COPIES
             if c["id"] in ("n_copy", "n_metrics")]
    spec = graph(copies=plain, card=[CARD[1]])
    spec["nodes"] = [n for n in spec["nodes"] if n["id"] != "write"]
    spec["nodes"][-1]["data"]["config"]["fields"] = [{"name": "说明", "value": "完成"}]
    spec["edges"] = [{"source": a["id"], "target": b["id"]} for a, b in zip(spec["nodes"], spec["nodes"][1:])]
    row = await finish(spec)
    assert row.status == "succeeded", row.error
    bundle = data_of((await client.get(f"/api/runs/{row.id}/bundle.html")).text)
    assert "canonical" in event(bundle, "node.finished", "card2")


async def test_an_old_doc_carrying_a_copied_number_ships_only_its_hash(client, monkeypatch, vip):
    """升级前写的文档（写作前不认遮罩，这里把那一步换成什么都不认）：证据目录里写着转抄来的号码。导出时整份只给哈希，报告
    节点的完成事件也只给哈希，文件里没有号码；卡 1 上没有号码、卡 2 只转抄客户数的（目录里没有号码）照常附带。"""
    import app.api.evidence as evidence_api

    async def nothing(*_args, **_kwargs):
        return []

    monkeypatch.setattr(evidence_api, "withheld_metrics", nothing)
    script(monkeypatch, CLEAN)
    row = await finish(graph())
    assert row.status == "succeeded", row.error
    doc = await doc_of(row.id)
    assert leaks(doc["catalog"]["m:p_nodes"]) == [PHONES[0]], "老文档的证据目录里写着转抄来的号码"
    resp = await client.get(f"/api/runs/{row.id}/bundle.html")
    assert resp.status_code == 200, resp.text
    bundle = data_of(resp.text)
    [art] = [a for a in bundle["artifacts"] if a["kind"] == "report_doc"]
    assert art.get("masked") is True and "b64" not in art, art
    assert "canonical" not in event(bundle, "node.finished", "write")
    assert leaks(resp.text) == []

    script(monkeypatch, CLEAN)
    plain = [{**c, "expression": c["expression"].replace("metrics[1]", "metrics[0]")} for c in COPIES
             if c["id"] in ("n_copy", "n_metrics")]
    row = await finish(graph(copies=plain, card=[CARD[1]]))
    assert row.status == "succeeded", row.error
    bundle = data_of((await client.get(f"/api/runs/{row.id}/bundle.html")).text)
    [art] = [a for a in bundle["artifacts"] if a["kind"] == "report_doc"]
    assert "b64" in art and "masked" not in art, art


async def test_the_panel_withholds_the_copied_number(client, monkeypatch, vip):
    """证据面板点开引用转抄号码的那一处（写作者坚持引用、标注照常产出）：值写「已遮罩」，整份答复没有号码。"""
    script(monkeypatch, "首位客户的联系方式 [[m:p_vars]]。")
    row = await finish(graph())
    assert row.status == "succeeded", row.error
    doc = await doc_of(row.id)
    seg = next(s for s in iter_segments(doc) if s.get("ref") == "m:p_vars")
    res = await client.get(f"/api/runs/{row.id}/evidence/segments/{seg['id']}")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["chain"][0]["value"] == "已遮罩", body["chain"][0]
    assert leaks(body) == [], json.dumps(body, ensure_ascii=False)[:600]


# --------------------------------------------------------------------------
# 手搭：认转抄、循环里抄自己上一轮的产出
# --------------------------------------------------------------------------

#: 一张卡转抄自己上一轮的产出（循环里写成这样）：写作目录只有最后一次的产出，指回自己，是一个环
LOOP_PATH = "nodes.loop_card.metrics[0].value"
LOOPING = {"kind": "metric_set", "caliber": "循环口径", "caliber_version": "v1", "artifact": "c" * 64, "metrics": [
    {"id": "p", "name": "上一轮的号码", "value": PHONES[0], "format": "plain", "expression": LOOP_PATH,
     "inputs": [{"path": LOOP_PATH, "value": PHONES[0], "node_id": "loop_card", "via": "metric", "status": "ok"}]}]}


def test_recognizing_which_card_metrics_an_input_copies():
    """认转抄：带值的字段（值、渲染、输入、整个指标）算转抄了那个指标；整张卡、清单文字、下标不是字面量的算整份；只取名称、
    单位、口径名的不算；产出不是指标集的节点不算。卡上记着出自口径卡（via 为 metric）、转抄来了值，那张卡的产出却读不出来
    的，交回读不出来的原因（unreadable）；转抄来的是空值（第一轮还没有上一轮）、不出自口径卡的不算。"""
    from app.engine.recompute import CARD_UNREADABLE, card_input

    def output(node_id):
        return LOOPING if node_id == "loop_card" else {"rows": [[1]], "columns": ["a"]} if node_id == "q" else None

    def picked(path, node_id=None, **item):
        got = card_input({"path": path, "node_id": node_id, **item}, output)
        if got is None:
            return None
        return (got.picked, got.whole) if got.unreadable is None else (got.unreadable, got.whole)

    assert picked("nodes.loop_card.metrics[0].value") == ((0,), False)
    assert picked("nodes.loop_card.metrics[-1]['rendered']") == ((0,), False)
    assert picked("nodes.loop_card.metrics[0].inputs[0].value") == ((0,), False)
    assert picked("vars.kpi.metrics[0].value", node_id="loop_card") == ((0,), False)
    assert picked("nodes.loop_card.text") == ((0,), True)
    assert picked("nodes.loop_card") == ((0,), True)
    assert picked("nodes.loop_card.metrics") == ((0,), True)
    assert picked("nodes.loop_card.metrics[vars.i].value") == ((0,), True)
    assert picked("nodes.loop_card.metrics[0].name") is None
    assert picked("nodes.loop_card.metrics[0].unit") is None
    assert picked("nodes.loop_card.caliber") is None
    assert picked("nodes.loop_card.metrics[3].value") is None
    assert picked("nodes.q.rows[0][0]") is None
    # 那张卡的产出读不出来
    assert picked("nodes.gone.metrics[0].value", via="metric", value=PHONES[0]) == (CARD_UNREADABLE, False)
    assert picked("vars.kpi.metrics[0].value", node_id="gone", via="metric", value=1) == (CARD_UNREADABLE, False)
    assert picked("nodes.gone.text", via="metric", value="清单") == (CARD_UNREADABLE, True)
    assert picked("nodes.gone.metrics[0].value", via="metric", value=None) is None
    assert picked("nodes.gone.metrics[0].value", via="transform", value=1) is None
    assert picked("nodes.gone.caliber", via="metric", value="口径") is None
    assert picked("nodes.gone.metrics[0].name", via="metric", value="名称") is None


def _looping(masked: bool) -> tuple[dict, dict[str, bytes], str | None]:
    """循环里抄自己上一轮产出的卡；masked 时另有一个输入取自号码列（第一轮还没有上一轮时取它）。交回 (卡, 工件, 快照 id)。"""
    from app.core.artifact_store import _path_of, canonical_json, content_hash

    if not masked:
        return LOOPING, {}, None
    snap = {"columns": ["id", "phone"], "rows": [[1, PHONES[0]]], "sql": "SELECT id, phone FROM customers",
            "mask_columns": ["phone"], "truncated": False}
    text = canonical_json(snap)
    snap_id = content_hash(text)
    _path_of(snap_id).parent.mkdir(parents=True, exist_ok=True)
    _path_of(snap_id).write_text(text, encoding="utf-8")
    cell = "cell(nodes.q, 0, 'phone')"
    metric = {**LOOPING["metrics"][0], "expression": f"{LOOP_PATH} if nodes.loop_card else {cell}", "inputs": [
        *LOOPING["metrics"][0]["inputs"],
        {"path": cell, "value": PHONES[0], "node_id": "q", "via": "tool_cell", "artifact": snap_id,
         "locator": {"row": 0, "column": "phone"}, "status": "verified"}]}
    return {**LOOPING, "metrics": [metric]}, {snap_id: text.encode()}, snap_id


@pytest.mark.parametrize("masked", [False, True])
async def test_a_card_copying_its_own_last_round_is_judged_on_the_cycle(masked):
    """一张卡转抄自己上一轮的产出：写作目录（只有最后一次的产出，指回自己）和可验证报告（这里同样只有这一份产出）都在依赖
    图上认这个环。环本身不带来遮罩：没有别的输入时照给（以前追够八层就按遮罩算）；另有一个输入取自号码列时，整个环都不给。"""
    from app.api.evidence import MASKED, _Masks, _metric_masks, _Sealed
    from app.bundle.build import _Store
    from app.core.artifact_store import canonical_json, content_hash

    looping, files, _ = _looping(masked)
    view = _Sealed(run_id="", graph={}, events=[], seal={"sealed": False, "ok": None, "manifest_seq": None,
                                                          "legacy": False})
    metric = looping["metrics"][0]
    _, word = await _metric_masks(metric, "loop_card", looping["artifact"], view, _Masks(view), card=looping,
                                  output=lambda node_id: looping if node_id == "loop_card" else None,
                                  by_value=False, sure=True)
    assert word == (MASKED if masked else None)
    raw = canonical_json(looping).encode()
    out_id = content_hash(raw)
    store = _Store({out_id: raw, **files}.get, {}, [(1, "node.finished", "loop_card", {"artifact": out_id})])
    card = store.content(out_id)
    assert store.metric_word(looping["artifact"], card["metrics"][0], store._events, sure=True) == \
        ("masked" if masked else None)
    assert store.metric_word(looping["artifact"], card["metrics"][0], store._events) == ("masked" if masked else None)
