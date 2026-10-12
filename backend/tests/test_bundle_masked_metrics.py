"""可验证报告里值取自遮罩格的口径卡指标：口径卡的节点完成事件、带着这类原值的老报告文档都只给哈希。

验收第二轮发现（和报告引不引用无关）：
- 口径卡的 node.finished 一律附规范串，它的预览逐条写着「指标（名称）= 值」：没有报告节点的运行（reports、artifacts 都是空的）
  照样把 phone_raw 的号码带进了文件
- 报告文档的证据目录收着上游口径卡的全部指标（没被引用的也收）和它们的值、渲染、标签，report_doc 一律原样附带

现在：
- 口径卡的节点完成事件：卡上有值不给的指标、有输入取自遮罩列（卡本身只给 id）的只给哈希；普通的卡照旧带规范串
- 指标的值给不给，和证据面板指标步骤的值同一个判断（_Store.metric_word 对照 api/evidence.py 的 _metric_masks）
- 现在的报告撰写节点把这类指标写成「已遮罩」，文档照常附带；之前写的、证据目录里还带着原值的老文档整份只给哈希，不附带
  它引用的工件，报告节点的完成事件也只给哈希；核对页照实说明原因，这份报告算未能核对
- 文档里写成「已遮罩」的指标所在的口径卡一律只给 id

真跑的部分用虚构的 SQLite 会员库（号码是编的），手搭的部分照 tests/test_bundle_build.py 的样子。
"""
from __future__ import annotations

import base64
import json
import re

import pytest
from httpx import ASGITransport, AsyncClient

from app.bundle.build import _Store, build_bundle, render_html
from app.core.artifact_store import read_bytes
from app.engine.runner import run_manager
from app.main import app
from tests.test_bundle_build import GRAPH, MASKED_ID, RUN, SNAP_MASKED, STORE, doc_of, stored
from tests.test_bundle_shown import verdicts
from tests.test_masked_metric_values import members  # noqa: F401  夹具：号码设了遮罩的会员库
from tests.test_report_masked_metrics import CITES, CLEAN, METRICS, PHONES, finish, graph, leaks, script, vip  # noqa: F401


@pytest.fixture
async def engine_up():
    await run_manager.setup()
    yield
    await run_manager.shutdown()


@pytest.fixture
async def client(engine_up):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


def data_of(html: str) -> dict:
    m = re.search(r'<script type="application/json" id="bundle-data">(.*?)</script>', html, re.S)
    assert m, "页面里没有数据块"
    return json.loads(m.group(1))


async def export(client, run_id: str) -> tuple[str, dict]:
    resp = await client.get(f"/api/runs/{run_id}/bundle.html")
    assert resp.status_code == 200, resp.text
    return resp.text, data_of(resp.text)


def event(bundle: dict, etype: str, node_id: str | None) -> dict:
    return next(e for e in bundle["events"] if e["type"] == etype and e["node_id"] == node_id)


# --------------------------------------------------------------------------
# 真跑、真导出
# --------------------------------------------------------------------------


async def test_a_run_without_a_report_does_not_carry_the_card_values(client, monkeypatch, vip):
    """没有报告节点的运行：没有报告文档、没有工件，口径卡的节点完成事件只给哈希，文件里一个号码都没有。"""
    row = await finish(graph(report=False))
    assert row.status == "succeeded", row.error
    html, bundle = await export(client, row.id)
    assert bundle["reports"] == [] and bundle["artifacts"] == []
    assert "canonical" not in event(bundle, "node.finished", "card")
    assert leaks(html) == [], "可验证报告里不出现号码"


async def test_a_new_doc_is_embedded_and_the_card_event_ships_only_its_hash(client, monkeypatch, vip):
    """现在的报告撰写节点写的文档（写作者坚持引用了 [[m:phone_raw]]，标注照常产出）：证据目录、正文里都是「已遮罩」，文档照常
    附带，报告节点的完成事件照常带规范串；口径卡只给 id、它的节点完成事件只给哈希。文件里没有号码。"""
    script(monkeypatch, CITES)
    row = await finish(graph(on_violation="flag"))
    assert row.status == "succeeded", row.error
    html, bundle = await export(client, row.id)
    [doc_entry] = [a for a in bundle["artifacts"] if a["kind"] == "report_doc"]
    doc = json.loads(base64.b64decode(doc_entry["b64"]))
    assert doc["catalog"]["m:phone_raw"]["masked"] is True and "手机号 已遮罩" in doc["markdown"]
    [card] = [a for a in bundle["artifacts"] if a["kind"] == "metric_set"]
    assert card.get("masked") is True and "b64" not in card
    assert "canonical" not in event(bundle, "node.finished", "card")
    assert "canonical" in event(bundle, "node.finished", "write") and "canonical" in event(bundle, "report.checked", "write")
    assert leaks(html) == []


async def test_an_old_doc_carrying_masked_values_ships_only_its_hash(client, monkeypatch, vip):
    """升级前的报告撰写节点写的文档（这里把写作前认遮罩的那一步换成什么都不认）：证据目录、正文里写着号码。导出时整份
    只给哈希，不附带它引用的口径卡和快照，报告节点的完成事件也只给哈希；报告自查事件照常带（引用链靠它认文档）。文件里
    没有号码；核对页把这份报告说成未能核对、原因写明，没有一项不通过。"""
    import app.api.evidence as evidence_api

    async def nothing(*_args, **_kwargs):
        return []

    monkeypatch.setattr(evidence_api, "withheld_metrics", nothing)
    script(monkeypatch, CITES)
    row = await finish(graph(on_violation="flag"))
    assert row.status == "succeeded", row.error
    html, bundle = await export(client, row.id)
    assert [{k: v for k, v in a.items() if k != "id"} for a in bundle["artifacts"]] == \
        [{"kind": "report_doc", "node_id": "write", "masked": True}]
    assert "canonical" not in event(bundle, "node.finished", "write")
    assert "canonical" not in event(bundle, "node.finished", "card")
    assert "canonical" in event(bundle, "report.checked", "write")
    assert leaks(html) == []

    got = verdicts(bundle)
    [doc] = [a for a in got["why"]["artifacts"] if a["kind"] == "report_doc"]
    assert doc["result"] == "na" and "取自遮罩列的指标的值" in doc["reason"] and "未随文件附带" in doc["reason"], doc
    [link] = [c for c in got["why"]["chain"] if c["kind"] == "report_doc"]
    assert link["result"] == "na" and "取自遮罩列的指标的值" in link["reason"], link
    assert got["docs"] == [{"node": "write", "trusted": False}] and got["numbers"] == []
    assert "fail" not in got["events"] + got["manifest"] + got["artifacts"] + got["chain"] + got["meta"], got


async def test_an_ordinary_card_keeps_its_event_and_its_numbers(client, monkeypatch, vip):
    """同一个设了号码遮罩的会员库，卡上只有客户编号、客户数（值里没有号码）：卡照常附带，节点完成事件照常带规范串，报告里
    引用它的数字照常核对通过——普通报告的可核对项不因此变少。"""
    script(monkeypatch, CLEAN)
    row = await finish(graph(on_violation="flag", metrics=[m for m in METRICS if m["id"] in ("first_id", "n_cust")]))
    assert row.status == "succeeded", row.error
    html, bundle = await export(client, row.id)
    [card] = [a for a in bundle["artifacts"] if a["kind"] == "metric_set"]
    assert "b64" in card and "masked" not in card
    assert "canonical" in event(bundle, "node.finished", "card")
    got = verdicts(bundle)
    assert [n["result"] for n in got["numbers"]] == ["pass", "pass"], got["numbers"]
    assert leaks(html) == []


async def test_the_bundle_judges_metric_values_like_the_report_writer(client, monkeypatch, members):
    """可验证报告认指标的值给不给，和报告撰写节点的写作目录同一个判断（证据面板指标步骤的那一套，只按出处认：
    _metric_masks 的 by_value=False）：整份结果转成文字、整行、下标不是字面量的一格、cell() 取的号码都不给，数行数的、
    取编号的照给（用例同 tests/test_masked_metric_values.py）。数据整形取出来再赋给变量的号码认不出出处，证据面板按值多遮
    一层，这里照给（已知的缺口）。"""
    from app.api.evidence import MASKED, _mask_fingerprint, _Masks, _metric_masks, _metric_in, _sealed
    from tests.test_masked_metric_values import TEXT, graph as members_graph

    script(monkeypatch, TEXT)
    row = await finish(members_graph())
    assert row.status == "succeeded", row.error
    sealed = await _sealed(row.id)
    entry = next(e for e in sealed.entries if e.get("kind") == "metric_set")
    card = json.loads(read_bytes(entry["artifact"], verify=False))
    store = _Store(lambda a: read_bytes(a, verify=False), await _mask_fingerprint(), sealed.events)
    writer, panel, bundle = {}, {}, {}
    for metric in card["metrics"]:
        _, word = await _metric_masks(metric, "card", entry["artifact"], sealed, _Masks(sealed), by_value=False)
        writer[metric["id"]] = {MASKED: "masked"}.get(word or "", word)
        _, word = await _metric_masks(metric, "card", entry["artifact"], sealed, _Masks(sealed))
        panel[metric["id"]] = {MASKED: "masked"}.get(word or "", word)
        bundle[metric["id"]] = store.metric_word(entry["artifact"], _metric_in(card, metric["id"]), sealed.events)
    assert writer == bundle, (writer, bundle)
    assert bundle == {"n_cust": None, "dump": "masked", "row0": "masked", "last": "masked", "phone_var": None,
                      "phone_raw": "masked", "id0": None}
    assert {k: v for k, v in panel.items() if k != "phone_var"} == {k: v for k, v in bundle.items() if k != "phone_var"}
    assert panel["phone_var"] == "masked"


# --------------------------------------------------------------------------
# 手搭：认法的几条分支
# --------------------------------------------------------------------------

PHONE = SNAP_MASKED["rows"][0][1]
CELL = "cell(nodes.members, 0, 'phone')"
READ = {"path": CELL, "value": PHONE, "node_id": "members", "via": "tool_cell", "artifact": MASKED_ID,
        "locator": {"row": 0, "column": "phone"}, "status": "verified"}
PHONE_METRIC = {"id": "phone_raw", "name": "首位会员手机号", "value": PHONE, "format": "plain", "expression": CELL,
                "substituted": f"'{PHONE}'", "inputs": [READ]}
COUNT_METRIC = {"id": "base", "name": "会员数", "value": 2, "unit": "位", "decimals": 0}


#: 报告文档的证据目录里，现在的报告撰写节点写的 phone_raw（「已遮罩」）。挂在 m:gmv 上：手搭的文档正文引用的是 m:gmv
MASKED_ENTRY = {"m:gmv": {"locator": {"metric": "phone_raw"}, "value": "已遮罩", "rendered": "已遮罩",
                          "label": "首位会员手机号 = 已遮罩", "masked": True}}


def card_of(*metrics) -> dict:
    return {"kind": "metric_set", "caliber": "会员口径", "caliber_version": "v1", "metrics": list(metrics)}


def text_of(card: dict) -> str:
    """metrics 节点产出的 text：逐条「指标（名称）= 值」。"""
    return "指标清单：\n" + "\n".join(f"- {m['id']}（{m['name']}）= {m['value']}{m.get('unit') or ''}"
                                   for m in card["metrics"])


def built(card: dict, *, entries: dict | None = None, finished: dict | None = None, drop: tuple[str, ...] = (),
          extra_files: dict | None = None, outputs: dict | None = None, source_masks: dict | None = None):
    """一次手搭的运行：会员查询（带遮罩的快照）→ 数据整形（outputs：{节点: 产出工件}）→ 口径卡 → 报告。entries：报告文档
    证据目录里另加的指标条目（{别名: 条目}，值由调用方给，artifact 指向这张卡）；finished 换掉口径卡节点完成事件的内容；
    drop 是导出时服务端已没有文件的工件；source_masks 是数据源现在设的遮罩。"""
    card_id, card_raw = stored(card)
    doc = doc_of(extra_catalog={alias: {"alias": alias, "kind": "metric", "node_id": "card", "artifact": card_id,
                                        **entry} for alias, entry in (entries or {}).items()})
    doc_id, doc_raw = stored(doc)
    files = {**STORE, card_id: card_raw, doc_id: doc_raw, **(extra_files or {})}
    files = {k: v for k, v in files.items() if k not in drop}
    # 数据整形节点的完成事件排在会员查询之后、口径卡之前（seq 3.x），口径卡、报告的序号照旧是 5、6、7
    shaped = [(3, "node.finished", node_id, {"artifact": artifact}) for node_id, artifact in (outputs or {}).items()]
    events = [
        (1, "run.started", None, {"nodes": 7}),
        (2, "tool.end", "members", {"tool": "db_query__crm", "query_artifact": MASKED_ID}),
        (3, "node.finished", "members", {"evidence": [{"kind": "query", "node_id": "members", "artifact": MASKED_ID}]}),
        *shaped,
        (4, "node.started", "card", {}),
        (5, "node.finished", "card", finished if finished is not None else {
            "preview": {"kind": "metric_set", "text": text_of(card), "artifact": card_id},
            "evidence": [{"kind": "metric_set", "node_id": "card", "artifact": card_id}]}),
        (6, "report.checked", "write", {"doc_artifact": doc_id, "ok": True}),
        (7, "node.finished", "write", {"preview": {"text": "…", "doc_artifact": doc_id}}),
        (8, "run.finished", None, {"output": {}}),
    ]
    bundle = build_bundle(RUN, events, manifest_seq=len(events), manifest_hash="0" * 64, graph=GRAPH,
                          source_masks=source_masks or {}, read=files.get, exported_at="2026-10-12T08:00:00+00:00")
    return bundle, card_id, doc_id


def shown(bundle: dict, seq: int) -> bool:
    return "canonical" in next(e for e in bundle["events"] if e["seq"] == seq)


def entry_of(bundle: dict, artifact: str) -> dict | None:
    return next((a for a in bundle["artifacts"] if a["id"] == artifact), None)


def test_a_card_with_a_metric_from_a_masked_cell_ships_its_event_as_a_hash():
    """卡上一个指标就是号码那一格：卡的节点完成事件只给哈希；同样的事件换成只有普通指标的卡，照常带规范串。"""
    bundle, *_ = built(card_of(COUNT_METRIC, PHONE_METRIC), entries={"m:base": {"locator": {"metric": "base"},
                                                                               "value": 2}})
    assert not shown(bundle, 5) and PHONE not in render_html(bundle)
    plain, *_ = built(card_of(COUNT_METRIC), entries={"m:base": {"locator": {"metric": "base"}, "value": 2}})
    assert shown(plain, 5)


@pytest.mark.parametrize("pointer", ["preview", "output"])
def test_a_card_event_without_a_ledger_is_judged_by_what_it_points_to(pointer):
    """升级前的运行：口径卡的节点完成事件没有台账，按预览里记的 artifact、再按节点产出工件（内容就是这张卡加上 text）认。"""
    card = card_of(COUNT_METRIC, PHONE_METRIC)
    card_id, _ = stored(card)
    output = {**card, "text": text_of(card), "artifact": card_id}
    output_id, output_raw = stored(output)
    finished = {"preview": {"kind": "metric_set", "text": text_of(card), "artifact": card_id}} if pointer == "preview" \
        else {"preview": {"kind": "metric_set", "text": text_of(card)}, "artifact": output_id}
    bundle, *_ = built(card, finished=finished, extra_files={output_id: output_raw})
    assert not shown(bundle, 5)


@pytest.mark.parametrize("masked_run, expected", [(True, False), (False, True)])
def test_an_unreadable_card_event_follows_the_run(masked_run, expected):
    """卡的文件读不出来：不知道卡上有没有遮罩，运行里有带遮罩的快照时只给哈希（同成果节点），没有的照常带。"""
    card = card_of(COUNT_METRIC, PHONE_METRIC)
    card_id, _ = stored(card)
    store = _Store({}.get, {}, [])
    from app.bundle.build import _card_shown

    data = {"preview": {"text": text_of(card)}, "evidence": [{"kind": "metric_set", "artifact": card_id}]}
    assert _card_shown(data, store, masked_run, []) is expected


def test_an_old_doc_carrying_a_masked_value_ships_only_its_hash():
    """证据目录里写着 phone_raw 的号码（没被引用也一样）：整份只给哈希，不附带它引用的工件，报告节点的完成事件只给哈希。
    同一张卡，证据目录里这个指标写成「已遮罩」的照常附带。"""
    raw = {"m:phone_raw": {"locator": {"metric": "phone_raw"}, "value": PHONE, "rendered": PHONE,
                           "label": f"首位会员手机号 = {PHONE}"}}
    bundle, _, doc_id = built(card_of(COUNT_METRIC, PHONE_METRIC), entries=raw)
    assert bundle["artifacts"] == [{"id": doc_id, "kind": "report_doc", "node_id": "write", "masked": True}]
    assert not shown(bundle, 7) and shown(bundle, 6)
    assert PHONE not in render_html(bundle)
    bundle, card_id, doc_id = built(card_of(COUNT_METRIC, PHONE_METRIC), entries=MASKED_ENTRY)
    assert "b64" in entry_of(bundle, doc_id) and shown(bundle, 7)
    assert entry_of(bundle, card_id) == {"id": card_id, "kind": "metric_set", "node_id": "card", "masked": True}
    assert PHONE not in render_html(bundle)


def test_an_old_doc_with_only_ordinary_values_is_embedded():
    """证据目录里只有普通指标的值（同一张卡上有号码那个指标，目录里没收）：照常附带。"""
    bundle, _, doc_id = built(card_of(COUNT_METRIC, PHONE_METRIC),
                              entries={"m:base": {"locator": {"metric": "base"}, "value": 2}})
    assert "b64" in entry_of(bundle, doc_id) and shown(bundle, 7)


@pytest.mark.parametrize("value, withheld", [(PHONE, True), (2, False)])
def test_an_old_doc_whose_card_is_missing_is_judged_by_value(value, withheld):
    """目录里的口径卡导出时读不出来：认不出这个指标取自哪里，按值认——值就是这次运行查到过的遮罩值才只给哈希。"""
    card = card_of(COUNT_METRIC, PHONE_METRIC)
    card_id, _ = stored(card)
    bundle, _, doc_id = built(card, entries={"m:x": {"locator": {"metric": "phone_raw"}, "value": value}},
                              drop=(card_id,))
    assert (entry_of(bundle, doc_id).get("masked") is True) is withheld, entry_of(bundle, doc_id)


def test_a_doc_masking_a_metric_forces_its_card_to_ship_only_its_id():
    """文档的证据目录里写成「已遮罩」的指标，导出时这里认不出它取自遮罩列（这里是卡上没记输入）：卡上记着的原值照样不随
    文件附带，整张卡只给 id。"""
    plain = {k: v for k, v in PHONE_METRIC.items() if k != "inputs"}
    card = card_of(COUNT_METRIC, plain)
    assert _Store({}.get, {}, []).metric_word("x", plain, []) is None
    bundle, card_id, _ = built(card, entries=MASKED_ENTRY)
    assert entry_of(bundle, card_id) == {"id": card_id, "kind": "metric_set", "node_id": "card", "masked": True}
    assert PHONE not in render_html(bundle)
    # 对照：目录里同一个指标照写原值（老文档、真没遮）的，这张卡照常附带
    bundle, card_id, _ = built(card, entries={"m:gmv": {"locator": {"metric": "base"}, "value": 2}})
    assert "b64" in entry_of(bundle, card_id)


@pytest.mark.parametrize("path, value, parsed", [
    # 整行转成文字：只认字面量下标一格的那一套认不出，按位置认得出整行的列
    ("vars.res.rows[0]", ["林一", PHONE, 320.0], {"columns": SNAP_MASKED["columns"], "rows": SNAP_MASKED["rows"],
                                                "artifact": MASKED_ID, "source": "crm"}),
    # 整份结果转成文字：结果里带着号码列，值里就是号码
    ("vars.res.rows", [["林一", PHONE, 320.0]], {"columns": SNAP_MASKED["columns"], "rows": SNAP_MASKED["rows"],
                                               "artifact": MASKED_ID, "source": "crm"}),
])
def test_a_card_whose_value_carries_a_masked_row_ships_only_its_id(path, value, parsed):
    """卡上的指标是整行、整份结果转成的文字：卡只给 id、节点完成事件只给哈希，页面里没有号码（以前只认字面量下标的一格，
    卡原样嵌了进去）。"""
    metric = {"id": "who", "name": "首位会员", "value": str(value), "format": "plain", "expression": f"str({path})",
              "substituted": "…", "inputs": [{"path": path, "value": value, "node_id": "parse", "via": "transform",
                                              "status": "ok"}]}
    card = card_of(COUNT_METRIC, metric)
    parse_id, parse_raw = stored(parsed)
    finished = {"preview": {"kind": "metric_set", "text": text_of(card)},
                "evidence": [{"kind": "metric_set", "node_id": "card", "artifact": stored(card)[0]}]}
    bundle, card_id, _ = built(card, finished=finished, extra_files={parse_id: parse_raw},
                               entries={"m:gmv": {"locator": {"metric": "base"}, "value": 2}},
                               outputs={"parse": parse_id})
    assert entry_of(bundle, card_id) == {"id": card_id, "kind": "metric_set", "node_id": "card", "masked": True}
    assert not shown(bundle, 5) and PHONE not in render_html(bundle)


def test_an_old_doc_is_judged_by_the_source_when_the_snapshot_is_gone():
    """号码那一格所在的快照导出时读不出来（这次运行查到过的遮罩值也就收不到）：按这份快照的数据源现在设的遮罩认，phone_raw
    照样算取自遮罩列，带着号码的老文档只给哈希。"""
    raw = {"m:phone_raw": {"locator": {"metric": "phone_raw"}, "value": PHONE}}
    bundle, _, doc_id = built(card_of(COUNT_METRIC, PHONE_METRIC), entries=raw, drop=(MASKED_ID,),
                              source_masks={"crm": ["phone"]})
    assert entry_of(bundle, doc_id).get("masked") is True, entry_of(bundle, doc_id)


def test_an_ordinary_metric_that_merely_equals_a_masked_value_stays_checkable():
    """写死在数据整形里的热线号码碰巧和遮罩列里的号码一样（认不出出处，只是值一样，按值认得出）：不算取自遮罩列。卡照常
    附带、节点完成事件照常带规范串，证据目录里写着这个值的文档照常附带——普通报告的可核对项不因此变少（真实的情形见
    tests/test_inquiry_api.py 里「单笔目标 20000」那一条：目标碰巧等于遮罩金额列里的一笔）。证据面板为了展示按值多遮一层，
    这里不跟。"""
    metric = {"id": "hotline", "name": "服务热线", "value": PHONE, "format": "plain", "expression": "vars.kpi.hotline",
              "substituted": f"'{PHONE}'", "inputs": [{"path": "vars.kpi.hotline", "value": PHONE, "node_id": "calc",
                                                      "via": "transform", "status": "ok"}]}
    card = card_of(COUNT_METRIC, metric)
    kpi_id, kpi_raw = stored({"hotline": PHONE})
    bundle, card_id, doc_id = built(card, extra_files={kpi_id: kpi_raw}, outputs={"calc": kpi_id},
                                    entries={"m:gmv": {"locator": {"metric": "hotline"}, "value": PHONE}})
    assert "b64" in entry_of(bundle, card_id) and shown(bundle, 5)
    assert "b64" in entry_of(bundle, doc_id) and shown(bundle, 7)
