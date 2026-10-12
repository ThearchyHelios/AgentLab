"""证据面板指标步骤的遮罩（期 4 I2a）：取自遮罩列的输入，输入标签和代入式里都不显示它的值。

以前只有查询步骤的结果窗口把遮罩列换成「已遮罩」，指标步骤的输入（「值 ← 路径」）和代入式照样写着原值——同一个
号码，表格里遮着，上面的标签里亮着。现在：

- cell() 取的格、Agent 交来的字段：按定位里的列认
- vars.res.rows[0][2] 这种按位置取自查询结果的标量引用：顺着取值链找到那份查询结果（写变量的节点当时的产出），
  按列号认出是哪一列
- 遮的是「查询当时记下的」加「数据源现在设的」，和查询步骤同一套；数据源事后去掉了遮罩，当时遮着的照样遮
- 取自遮罩列的输入值写「已遮罩」，带上是哪几列（masked），模型报的值、快照里的值一并不给；代入式里那一处写「已遮罩」，
  遮罩列也进 redacted.columns（面板底下那句「遮罩不是安全边界」）
- 没有遮罩的指标和以前一字不差；推演（api/whatif.py）同样认得出按位置取的遮罩列，不给值、不推演

真跑一遍图（虚构的 SQLite 订单库，号码是编的），报告由 MockChatModel 照剧本写。
"""
from __future__ import annotations

import asyncio
import json
import sqlite3

import pytest
from httpx import ASGITransport, AsyncClient
from langchain_core.messages import AIMessage
from sqlalchemy import select

from app.core import artifact_store
from app.data.engine import engines
from app.db.base import SessionLocal
from app.db.models import DataSource, Run
from app.engine.evidence import iter_segments
from app.engine.runner import run_manager
from app.main import app

SQL = "SELECT id, region, phone, amount FROM orders ORDER BY id"
#: 号码写成带横线的文本：全是数字的文本 cell() 会按数读（口径卡在算数），len() 就算不了
ROWS = [(1, "华东", "139-0000-0001", 120.5), (2, "华南", "139-0000-0002", 80.0), (3, "华北", "139-0000-0003", 99.5)]
PHONES = [r[2] for r in ROWS]
#: 号码那一格按位置取：vars.res 是数据整形解析好的查询结果，第 2 列是 phone
FIRST_PHONE = "vars.res.rows[0][2]"
SECOND_PHONE = "vars.res.rows[1][2]"
PHONE_CELL = "cell(nodes.fetch, 0, 'phone')"
METRICS = [
    {"id": "amt", "name": "首单金额", "unit": "元", "decimals": 1, "format": "thousands",
     "expression": "cell(nodes.fetch, 0, 'amount')"},
    {"id": "plen", "name": "号码位数", "unit": "位", "format": "thousands", "expression": f"len({PHONE_CELL})"},
    {"id": "plen2", "name": "号码位数两倍", "unit": "位", "format": "thousands", "expression": f"len({FIRST_PHONE}) * 2"},
    {"id": "mix", "name": "混合", "unit": "分", "decimals": 1, "format": "thousands",
     "expression": f"round(cell(nodes.fetch, 1, 'amount') + len({SECOND_PHONE}), 1)"},
]
TEXT = "首单金额 [[m:amt]]，号码 [[m:plen]]，两倍 [[m:plen2]]，混合 [[m:mix]]。"


@pytest.fixture(autouse=True)
async def engine_up():
    await run_manager.setup()
    yield
    await run_manager.shutdown()


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def set_source(path, options=None):
    async with SessionLocal() as session:
        row = (await session.execute(select(DataSource).where(DataSource.name == "shop"))).scalar_one_or_none()
        if row is None:
            row = DataSource(name="shop", kind="sqlite", database=str(path), readonly=True, enabled=True)
            session.add(row)
        row.database, row.kind, row.enabled, row.readonly = str(path), "sqlite", True, True
        row.options = dict(options or {})
        await session.commit()
        return row.id


@pytest.fixture
async def shop(tmp_path):
    path = tmp_path / "shop.db"
    db = sqlite3.connect(path)
    db.executescript("CREATE TABLE orders (id INTEGER PRIMARY KEY, region TEXT, phone TEXT, amount REAL);")
    db.executemany("INSERT INTO orders VALUES (?, ?, ?, ?)", ROWS)
    db.commit()
    db.close()
    source_id = await set_source(path)
    await engines.invalidate(source_id)
    yield path
    await engines.invalidate(source_id)


def node(nid, ntype, **config):
    return {"id": nid, "type": ntype, "position": {"x": 0, "y": 0}, "data": {"label": nid, "config": config}}


def graph():
    nodes = [
        node("start", "input"),
        node("fetch", "tool", tool="db_query__shop", args={"sql": SQL}),
        node("parse", "transform", mode="json", template="{{ nodes.fetch }}", assign_to="res"),
        node("card", "metrics", caliber="遮罩口径", caliber_version="v1", metrics=METRICS),
        node("write", "report", instructions="写一句", on_violation="flag"),
        node("out", "output", fields=[{"name": "报告", "value": "{{ nodes.write.text }}"}]),
    ]
    return {"nodes": nodes, "edges": [{"source": a["id"], "target": b["id"]} for a, b in zip(nodes, nodes[1:])]}


async def run_graph(monkeypatch) -> Run:
    from app.providers import mock_model

    monkeypatch.setattr(mock_model.MockChatModel, "_decide", lambda self, messages: AIMessage(content=TEXT))
    run_id = (await run_manager.start(graph=graph(), input_payload={})).id
    for _ in range(400):
        await asyncio.sleep(0.05)
        async with SessionLocal() as session:
            row = await session.get(Run, run_id)
        if row.status in ("succeeded", "failed") and row.manifest_seq is not None:
            assert row.status == "succeeded", row.error
            return row
    raise AssertionError(f"运行 {run_id} 没跑完：{row.status}")


async def segment(client, run_id: str, text: str | None = None, *, ref: str | None = None) -> dict:
    """点开报告里的一处：按正文的字找，或者按引用找（ref）。运行之前就设了遮罩的，用到遮罩列的指标在报告里写「已遮罩」
    （报告撰写节点按指标步骤同一个判断换掉，见 tests/test_report_masked_metrics.py），只能按引用找。"""
    graph_body = (await client.get(f"/api/runs/{run_id}/evidence")).json()
    [report] = graph_body["reports"]
    doc = artifact_store.load(report["doc_artifact"])
    seg = next(s for s in iter_segments(doc) if (s.get("ref") == ref if ref else s["text"] == text))
    res = await client.get(f"/api/runs/{run_id}/evidence/segments/{seg['id']}")
    assert res.status_code == 200, res.text
    return res.json()


def inputs_of(body: dict) -> dict[str, dict]:
    return {s["path"]: s for s in body["chain"] if s["step"] == "input"}


def leaks(body: dict) -> list[str]:
    raw = json.dumps(body, ensure_ascii=False)
    return [p for p in PHONES if p in raw]


# --------------------------------------------------------------------------


async def test_a_masked_cell_is_hidden_from_the_input_label_and_the_substituted_expression(client, monkeypatch, shop):
    await set_source(shop, {"mask_columns": ["phone"]})
    row = await run_graph(monkeypatch)
    body = await segment(client, row.id, ref="m:plen")
    metric = body["chain"][0]
    assert metric["step"] == "metric" and metric["expression"] == f"len({PHONE_CELL})"
    assert metric["substituted"] == "len(已遮罩)"
    [cell] = inputs_of(body).values()
    assert cell["value"] == "已遮罩" and cell["masked"] == ["phone"] and cell["via"] == "tool_cell"
    assert "snapshot_value" not in cell and "model_value" not in cell
    # 那一格所在的查询步骤照旧遮着；面板底下的遮罩说明带上这一列
    [query] = [s for s in body["chain"] if s["step"] == "query"]
    assert query["masked"] == ["phone"] and body["redacted"]["columns"] == ["phone"]
    assert leaks(body) == []


async def test_a_scalar_taken_by_position_from_a_masked_column_is_hidden(client, monkeypatch, shop):
    """vars.res.rows[0][2]：没有 cell() 的定位，顺着取值链认出取自查询结果的 phone 列。"""
    await set_source(shop, {"mask_columns": ["phone"]})
    row = await run_graph(monkeypatch)
    body = await segment(client, row.id, ref="m:plen2")
    metric = body["chain"][0]
    assert metric["substituted"] == "len(已遮罩) * 2"
    [phone] = inputs_of(body).values()
    assert phone["path"] == FIRST_PHONE and phone["value"] == "已遮罩" and phone["masked"] == ["phone"]
    assert body["redacted"]["columns"] == ["phone"]
    assert leaks(body) == []


async def test_only_the_masked_input_is_hidden_when_inputs_are_mixed(client, monkeypatch, shop):
    await set_source(shop, {"mask_columns": ["phone"]})
    row = await run_graph(monkeypatch)
    body = await segment(client, row.id, ref="m:mix")
    assert body["chain"][0]["substituted"] == "round(80.0 + len(已遮罩), 1)"
    inputs = inputs_of(body)
    amount = inputs["cell(nodes.fetch, 1, 'amount')"]
    assert amount["value"] == 80.0 and "masked" not in amount
    assert inputs[SECOND_PHONE]["value"] == "已遮罩" and inputs[SECOND_PHONE]["masked"] == ["phone"]
    assert leaks(body) == []
    # 没用到遮罩列的指标和以前一样
    plain = await segment(client, row.id, "120.5元")
    assert plain["chain"][0]["substituted"] == "120.5"
    [amt] = inputs_of(plain).values()
    assert amt["value"] == 120.5 and "masked" not in amt


async def test_masks_recorded_at_query_time_stay_after_the_source_drops_them(client, monkeypatch, shop):
    await set_source(shop, {"mask_columns": ["phone"]})
    row = await run_graph(monkeypatch)
    await set_source(shop, {})
    for ref in ("m:plen", "m:plen2", "m:mix"):
        body = await segment(client, row.id, ref=ref)
        assert "已遮罩" in body["chain"][0]["substituted"] and leaks(body) == [], ref


async def test_masks_added_after_the_run_apply_too(client, monkeypatch, shop):
    row = await run_graph(monkeypatch)
    before = await segment(client, row.id, "26位")
    assert before["chain"][0]["substituted"] == "len('139-0000-0001') * 2"
    assert inputs_of(before)[FIRST_PHONE]["value"] == "139-0000-0001" and "masked" not in inputs_of(before)[FIRST_PHONE]
    await set_source(shop, {"mask_columns": "phone"})
    after = await segment(client, row.id, "26位")
    assert after["chain"][0]["substituted"] == "len(已遮罩) * 2" and leaks(after) == []


async def test_without_masks_the_metric_step_is_unchanged(client, monkeypatch, shop):
    row = await run_graph(monkeypatch)
    body = await segment(client, row.id, "93.0分")
    assert body["chain"][0]["substituted"] == "round(80.0 + len('139-0000-0002'), 1)"
    assert all("masked" not in s for s in body["chain"] if s["step"] == "input")
    assert body["redacted"] == {"columns": []}


async def test_the_whatif_withholds_a_masked_scalar_taken_by_position(client, monkeypatch, shop):
    """推演同一套：按位置取自遮罩列的输入不给值，整个指标推演不了，和证据面板说的一致。"""
    await set_source(shop, {"mask_columns": ["phone"]})
    row = await run_graph(monkeypatch)
    res = await client.post(f"/api/runs/{row.id}/evidence/whatif", json={"card": "card", "metric": "plen2"})
    assert res.status_code == 200, res.text
    [plen2] = res.json()["metrics"]
    assert plen2["status"] == "masked" and plen2["substituted"] is None
    [phone] = plen2["inputs"]
    assert phone["path"] == FIRST_PHONE and phone["masked"] is True and phone["old"] is None
    assert not [p for p in PHONES if p in res.text]
    # 改它的值同样不行：遮罩列不能拿来推演
    res = await client.post(f"/api/runs/{row.id}/evidence/whatif",
                            json={"card": "card", "metric": "plen2", "overrides": {"inputs": {FIRST_PHONE: 1}}})
    assert res.status_code == 422 and "遮罩" in res.json()["detail"]


# --------------------------------------------------------------------------
# 手搭的封存范围：模型报的值、快照里的值，以及遮没遮核对不了的几种
# --------------------------------------------------------------------------

#: 一份带遮罩的快照：phone 在查询当时就设了遮罩（记在快照里）。数据源名是编的，库里没有这个数据源（现在的遮罩为空）
GHOST = "ghost_mask_src"
PHONE_SNAP = {"columns": ["id", "phone"], "rows": [[1, "139-0000-0009"]], "source": GHOST,
              "sql": "SELECT id, phone FROM members", "mask_columns": ["phone"]}
#: 这几条用例里出现过的号码，答复里一个都不能有
SECRETS = ["139-0000-0009", "139-0000-0008", "139-0000-0007", "139-0000-0006", "139-0000-0005", "139-0000-0004"]
CELL = "cell(nodes.fetch, 0, 'phone')"


def card_metric(mid, expression, substituted, inputs, value=13):
    return {"id": mid, "name": mid, "unit": "位", "value": value, "rendered": f"{value}位", "format": "thousands",
            "expression": expression, "substituted": substituted, "recompute_ok": True, "inputs": inputs,
            "status": "ok"}


async def hand_chain(metrics, *, queries=(), outputs=None, metric_id=None):
    """手搭一次封存了的运行（取数 → 数据整形 → 口径卡），直接走指标步骤（evidence._metric_chain）。queries 是封存范围内
    交回过的查询快照，outputs 是口径卡算之前各节点的产出工件。"""
    from app.api import evidence
    from app.core.artifact_store import put_json
    from app.engine.evidence import make_eid

    card = await put_json({"kind": "metric_set", "caliber": "遮罩口径", "caliber_version": "v1", "metrics": metrics},
                          kind="metric_set")
    events = [(i + 1, "node.finished", nid, {"artifact": art}) for i, (nid, art) in enumerate((outputs or {}).items())]
    events += [(len(events) + 1, "node.started", "card", {}),
               (len(events) + 2, "node.finished", "card", {"evidence": [
                   {"kind": "metric_set", "node_id": "card", "artifact": card}]})]
    sealed = evidence._Sealed(run_id="syn-mask", graph=graph(), events=events,
                              seal={"sealed": True, "ok": True, "manifest_seq": len(events), "legacy": False})
    sealed.queries = {a: {"node_id": "fetch", "tool": f"db_query__{GHOST}", "source": GHOST} for a in queries}
    sealed.ledger = {("card", card)}
    mid = metric_id or metrics[0]["id"]
    eid = make_eid("metric", card, {"metric": mid})
    cite = {"alias": f"m:{mid}", "kind": "metric", "locator": {"metric": mid}, "status": "resolved", "eid": eid}
    entry = {"alias": f"m:{mid}", "kind": "metric", "artifact": card, "node_id": "card", "locator": {"metric": mid},
             "eid": eid}
    seg = {"id": "s1", "kind": "number", "text": "13位", "state": "deterministic", "cite": cite}
    return await evidence._metric_chain(seg, cite, entry, {"catalog": {f"m:{mid}": entry}}, sealed, evidence._Masks())


def said(chain) -> list[str]:
    raw = json.dumps(chain, ensure_ascii=False)
    return [p for p in SECRETS if p in raw]


async def test_what_the_model_reported_and_what_the_snapshot_held_are_withheld_too():
    """对不上的输入另记着两边的值：Agent 字段的 model_value（模型报的）、cell() 的 snapshot_value（快照里的）。取自遮罩列
    时一并拿掉——界面上不显示它们，接口答复里同样不能带着。"""
    from app.core.artifact_store import put_json

    snap = await put_json(PHONE_SNAP, kind="query")
    cell = card_metric("plen", f"len({CELL})", "len('139-0000-0008')", [
        {"path": CELL, "value": "139-0000-0008", "node_id": "fetch", "via": "tool_cell", "artifact": snap,
         "locator": {"row": 0, "column": "phone"}, "status": "mismatch", "snapshot_value": "139-0000-0009"}])
    agent = card_metric("alen", "len(vars.kpi.phone)", "len('139-0000-0009')", [
        {"path": "vars.kpi.phone", "value": "139-0000-0009", "node_id": "bot", "via": "agent_field", "field": "phone",
         "artifact": snap, "locator": {"row": 0, "column": "phone"}, "status": "mismatch",
         "model_value": "139-0000-0007"}])
    for metric in (cell, agent):
        chain = await hand_chain([metric], queries=[snap])
        [step] = [s for s in chain if s["step"] == "input"]
        assert step["value"] == "已遮罩" and step["masked"] == ["phone"], metric["id"]
        assert "model_value" not in step and "snapshot_value" not in step, metric["id"]
        assert chain[0]["substituted"] == "len(已遮罩)" and said(chain) == [], (metric["id"], said(chain))


async def test_a_substituted_expression_that_does_not_reproduce_is_not_shown():
    """卡上记的代入式和按卡上的输入重新代出来的对不上（老卡、卡上记的不是原值）：整条代入式不给，宁可不显示，也不能让
    遮着的值从对不上的那一处漏出来。"""
    from app.core.artifact_store import put_json
    from app.engine import recompute

    snap = await put_json(PHONE_SNAP, kind="query")
    odd = card_metric("odd", f"len({CELL})", "len('139-0000-0006')", [
        {"path": CELL, "value": "139-0000-0005", "node_id": "fetch", "via": "tool_cell", "artifact": snap,
         "locator": {"row": 0, "column": "phone"}, "status": "verified"}])
    assert recompute.substituted_without(odd, {CELL}, "已遮罩") is None
    chain = await hand_chain([odd], queries=[snap])
    assert chain[0]["substituted"] is None and said(chain) == [], said(chain)
    # 对得上的照常换掉那一处
    assert recompute.substituted_without({**odd, "substituted": "len('139-0000-0005')"}, {CELL}, "已遮罩") == "len(已遮罩)"


async def test_a_cell_on_a_snapshot_that_cannot_be_read_is_withheld():
    """cell() 落在的快照读不出来（文件不在了）：数据源现在没遮这一列，可查询当时记下的遮罩在快照里，遮没遮核对不了——
    宁可多遮，和推演一致。说法另写，不说「这一列设置了遮罩」（不进 masked）。"""
    gone = "dd" * 32
    metric = card_metric("plen", f"len({CELL})", "len('139-0000-0004')", [
        {"path": CELL, "value": "139-0000-0004", "node_id": "fetch", "via": "tool_cell", "artifact": gone,
         "locator": {"row": 0, "column": "phone"}, "status": "verified"}])
    chain = await hand_chain([metric], queries=[gone])
    [step] = [s for s in chain if s["step"] == "input"]
    # 遮没遮核对不了：值写「无法核对」，不写「已遮罩」（数据源可能根本没设遮罩），代入式里那一处同样
    assert step["value"] == "无法核对" and "masked" not in step
    assert step["mask_unverified"] == "查询快照无法读取，核对不了这一格是否取自设置了遮罩的列"
    assert chain[0]["substituted"] == "len(无法核对)" and said(chain) == []


PARSED = {"columns": ["id", "phone"], "rows": [[1, "139-0000-0004"]]}
POSITIONAL = "vars.res.rows[0][1]"


@pytest.mark.parametrize("parsed, why", [
    # 写变量的节点的产出读不出来：取值链走不通，那一格取自哪一列核对不了
    (None, "查询结果无法读取，核对不了这一格是否取自设置了遮罩的列"),
    # 结果没存快照、只带着数据源名：查询当时记下的遮罩在快照里，核对不了
    ({**PARSED, "source": GHOST}, "查询结果没有保存快照，核对不了这一格是否取自设置了遮罩的列"),
])
async def test_a_positional_cell_that_cannot_be_traced_is_withheld(parsed, why):
    from app.core.artifact_store import put_json

    out = await put_json(parsed, kind="node_output") if parsed is not None else "ee" * 32
    metric = card_metric("plen2", f"len({POSITIONAL}) * 2", "len('139-0000-0004') * 2", [
        {"path": POSITIONAL, "value": "139-0000-0004", "node_id": "parse", "via": "transform", "field": "rows[0][1]",
         "status": "ok"}], value=26)
    chain = await hand_chain([metric], outputs={"parse": out})
    [step] = [s for s in chain if s["step"] == "input"]
    assert step["value"] == "无法核对" and step["mask_unverified"] == why and "masked" not in step
    assert chain[0]["substituted"] == "len(无法核对) * 2" and said(chain) == []


async def test_a_reshaped_result_without_a_snapshot_or_a_source_is_shown_as_is():
    """数据整形改过形状的结果（既没带快照的 id，也没带数据源名）认不出出处：照常显示。已知的缺口，写在交付说明里。"""
    from app.core.artifact_store import put_json

    out = await put_json(PARSED, kind="node_output")
    metric = card_metric("plen2", f"len({POSITIONAL}) * 2", "len('139-0000-0004') * 2", [
        {"path": POSITIONAL, "value": "139-0000-0004", "node_id": "parse", "via": "transform", "field": "rows[0][1]",
         "status": "ok"}], value=26)
    chain = await hand_chain([metric], outputs={"parse": out})
    [step] = [s for s in chain if s["step"] == "input"]
    assert step["value"] == "139-0000-0004" and "mask_unverified" not in step and "masked" not in step


@pytest.mark.parametrize("parsed, withheld", [
    (None, True),
    ({**PARSED, "source": GHOST}, True),
    (PARSED, False),
])
def test_the_whatif_withholds_the_same_untraceable_cells(parsed, withheld):
    """推演同一个认法：取值链走不通、结果没存快照的那一格不给值、不推演；认不出出处的照常推演。"""
    from app.engine import recompute

    store = {"out": parsed}
    metric = card_metric("plen2", f"len({POSITIONAL}) * 2", "len('139-0000-0004') * 2", [
        {"path": POSITIONAL, "value": "139-0000-0004", "node_id": "parse", "via": "transform", "field": "rows[0][1]",
         "status": "ok"}], value=26)
    sources = recompute.Sources(load=store.get, outputs={"parse": "out"} if parsed is not None else {})
    [result] = recompute.whatif({"metrics": [metric]}, recompute.Changes(), sources)["metrics"]
    [inp] = result["inputs"]
    if withheld:
        assert inp["old"] is None and inp.get("withheld") is True and result["status"] == "unsupported"
        assert result["substituted"] is None and "139-0000-0004" not in json.dumps(result, ensure_ascii=False)
    else:
        assert inp["old"] == "139-0000-0004" and result["status"] == "unchanged"


async def test_the_writer_output_is_read_once_for_all_cells_taken_by_position(monkeypatch):
    """一个指标按位置取了好几格：写变量的节点的产出（可能是几 MB 的查询结果）一次请求里只读一次，快照也只读一次。"""
    from app.api import evidence
    from app.core.artifact_store import put_json

    snap = await put_json({**PHONE_SNAP, "rows": [[1, "139-0000-0009"], [2, "139-0000-0008"]]}, kind="query")
    out = await put_json({"columns": ["id", "phone"], "rows": [[1, "139-0000-0009"], [2, "139-0000-0008"]],
                          "artifact": snap, "source": GHOST}, kind="node_output")
    paths = ["vars.res.rows[0][1]", "vars.res.rows[1][1]", "vars.res.rows[0][0]"]
    metric = card_metric("many", " + ".join(f"len(str({p}))" for p in paths), "len('139-0000-0009') + 0", [
        {"path": p, "value": v, "node_id": "parse", "via": "transform", "field": p[9:], "status": "ok"}
        for p, v in zip(paths, ["139-0000-0009", "139-0000-0008", 1])], value=27)
    reads: list[str] = []
    real = evidence.load
    monkeypatch.setattr(evidence, "load", lambda artifact: (reads.append(artifact), real(artifact))[1])
    chain = await hand_chain([metric], outputs={"parse": out})
    inputs = {s["path"]: s for s in chain if s["step"] == "input"}
    assert inputs[paths[0]]["masked"] == ["phone"] and inputs[paths[1]]["masked"] == ["phone"]
    assert inputs[paths[2]]["value"] == 1 and "masked" not in inputs[paths[2]]
    assert reads.count(out) == 1 and reads.count(snap) == 1, reads
