"""推演和证据面板不露指标本身的原值：指标的输入取自遮罩格（或者指标的值本身就是遮罩格）时，原值写「已遮罩」。

验收发现：phone_raw = cell(nodes.q_cust, 0, 'phone') 推演时判成 masked，输入里置了空，指标的 old、rendered_old 却还是那串
号码；按位置取的 phone_pos 也一样。同一份答复里输入拿掉了、指标上照给，等于没遮。现在：

- 推演（engine/recompute.py）：用到了遮罩列的指标（哪怕只嵌在下标里），old、rendered_old 写「已遮罩」，标 withheld，
  原因照旧写在 reason 里；new、rendered_new 本来就是空的（推演不了）；报告里引用它的地方 rendered_old 同样写「已遮罩」
- 最外层的输入遮没遮核对不了（快照读不出来）的也一样；只嵌在下标里、遮没遮核对不了的那一格不碍事，原值照给
- 证据面板的指标步骤：value 按同一个判断写「已遮罩」（recompute.hides_value），报告上的字（rendered）照旧
- 没用到遮罩列的指标一字不差

前半是纯函数（手写的虚构卡和快照），后半真跑一遍图（虚构的 SQLite 客户库，号码是编的）。
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
from app.engine import recompute
from app.engine.evidence import iter_segments
from app.engine.recompute import Changes, Sources, whatif
from app.engine.runner import run_manager
from app.main import app

# --------------------------------------------------------------------------
# 纯函数：手写的卡和快照
# --------------------------------------------------------------------------

SNAP, OTHER = "a1" * 32, "b2" * 32
STORE = {
    SNAP: {"columns": ["id", "phone", "amount"], "source": "crm", "sql": "SELECT id, phone, amount FROM members",
           "rows": [[1, "19900000201", 120.5], [2, "19900000202", 80.0]]},
    OTHER: {"columns": ["n"], "source": "crm", "sql": "SELECT 2 AS n", "rows": [[2]]},
}
PHONE = "cell(nodes.crm, 0, 'phone')"
AMOUNT = "cell(nodes.crm, 0, 'amount')"


def cell(row, column, value, artifact=SNAP):
    return {"path": f"cell(nodes.crm, {row}, '{column}')", "value": value, "node_id": "crm", "via": "tool_cell",
            "locator": {"row": row, "column": column}, "artifact": artifact, "status": "verified"}


def scalar(path, value):
    return {"path": path, "value": value, "node_id": "fetch", "via": "transform", "status": "ok"}


def card(mid, expression, substituted, value, inputs, unit=""):
    return {"kind": "metric_set", "caliber": "会员口径", "caliber_version": "v1", "metrics": [
        {"id": mid, "name": mid, "unit": unit, "value": value, "format": "plain", "expression": expression,
         "substituted": substituted, "inputs": inputs, "status": "ok"}]}


def sources(**kw):
    base = {"load": STORE.get, "queries": (SNAP, OTHER), "masked": {SNAP: frozenset({"phone"})}}
    return Sources(**{**base, **kw})


def text_of(result) -> str:
    return json.dumps(result, ensure_ascii=False)


def test_the_value_of_a_metric_that_is_a_masked_cell_is_withheld():
    """验收的 phone_raw：指标就是遮罩列的那一格。old、rendered_old 都写「已遮罩」，号码不出现在答复的任何地方。"""
    result = whatif(card("phone_raw", PHONE, "19900000201", 19900000201, [cell(0, "phone", 19900000201)]),
                    Changes(), sources())
    [m] = result["metrics"]
    assert (m["status"], m["old"], m["rendered_old"], m["withheld"]) == ("masked", "已遮罩", "已遮罩", True)
    assert [m[k] for k in ("new", "rendered_new", "delta", "rendered_delta", "changed", "substituted")] == [None] * 6
    assert "遮罩" in m["reason"] and m["inputs"][0]["masked"] is True and m["inputs"][0]["old"] is None
    assert "19900000201" not in text_of(result)


def test_a_metric_computed_from_a_masked_cell_withholds_its_value_too():
    """号码位数这类算出来的数同样不给：改一个别的输入、拿指标的值一减，就能算出遮住的那一格（宁可多遮）。"""
    [m] = whatif(card("mix", f"len({PHONE}) + {AMOUNT}", "len('19900000201') + 120.5", 131.5,
                      [cell(0, "phone", "19900000201"), cell(0, "amount", 120.5)]), Changes(), sources())["metrics"]
    assert (m["status"], m["old"], m["rendered_old"]) == ("masked", "已遮罩", "已遮罩")
    inputs = {i["path"]: i for i in m["inputs"]}
    assert inputs[AMOUNT]["old"] == 120.5 and inputs[PHONE]["old"] is None


def test_a_missing_value_stays_missing():
    """本来就没有值的（缺输入）照旧是空的、显示「—」：缺输入还得看得出来。"""
    [m] = whatif(card("gone", PHONE, None, None, [cell(0, "phone", None)]), Changes(), sources())["metrics"]
    assert m["status"] == "masked" and m["old"] is None and m["rendered_old"] == "—" and m["withheld"] is True


def test_a_top_level_cell_on_an_unreadable_snapshot_withholds_the_value():
    """那一格所在的快照读不出来：遮没遮核对不了，输入不给值，指标的原值同样不给（和代入式一样）。说法写「无法核对」，
    不写「已遮罩」：数据源可能根本没设遮罩，读的人不该以为这里遮着。"""
    [m] = whatif(card("east", AMOUNT, "120.5", 120.5, [cell(0, "amount", 120.5)]), Changes(),
                 sources(load=lambda a: None if a == SNAP else STORE.get(a)))["metrics"]
    assert m["status"] == "unsupported" and m["old"] == m["rendered_old"] == "无法核对" and m["withheld"] is True
    assert "120.5" not in text_of(m)


def test_a_cell_only_choosing_a_row_on_an_unreadable_snapshot_does_not_hide_the_value():
    """只嵌在下标里、遮没遮核对不了的那一格：它只决定取哪一项，取到的值卡上另记着，原值照给、照样推演。"""
    pick = f"vars.kpi.list[{AMOUNT.replace('amount', 'id')}] + vars.kpi.k"
    top = f"vars.kpi.list[{AMOUNT.replace('amount', 'id')}]"
    metric = card("pick", pick, "20 + 2", 22, [scalar(top, 20), cell(0, "id", 1), scalar("vars.kpi.k", 2)])
    [m] = whatif(metric, Changes(), sources(load=lambda a: None if a == SNAP else STORE.get(a)))["metrics"]
    assert m["status"] == "unchanged" and m["old"] == 22 and "withheld" not in m
    [m] = whatif(metric, Changes(inputs={"vars.kpi.k": 5}), sources(load=lambda a: None if a == SNAP else STORE.get(a)))[
        "metrics"]
    assert (m["status"], m["old"], m["new"]) == ("ok", 22, 25)


def test_an_unmasked_metric_is_unchanged():
    result = whatif(card("east", AMOUNT, "120.5", 120.5, [cell(0, "amount", 120.5)]), Changes(), sources())
    [m] = result["metrics"]
    assert (m["status"], m["old"], m["rendered_old"]) == ("unchanged", 120.5, "120.5") and "withheld" not in m


def test_report_segments_of_a_withheld_metric_do_not_carry_the_value():
    """报告里引用了原值不给的指标：推演答复里那一处的 rendered_old 同样写「已遮罩」。"""
    metric = card("phone_raw", PHONE, "19900000201", 19900000201, [cell(0, "phone", 19900000201)])
    result = whatif(metric, Changes(), sources())
    entry = {"kind": "metric", "node_id": "card", "artifact": "c" * 64, "locator": {"metric": "phone_raw"}}
    doc = {"catalog": {"m:phone_raw": entry}, "blocks": [{"units": [{"id": "u0", "segments": [
        {"id": "s0", "kind": "number", "text": "19900000201", "state": "deterministic",
         "cite": {"alias": "m:phone_raw", "kind": "metric", "status": "resolved"}}]}]}]}
    [seg] = recompute.report_segments([("write", doc)], card_node="card", card_artifact="c" * 64,
                                      results=result["metrics"])
    assert (seg["rendered_old"], seg["rendered_new"], seg["changed"]) == ("已遮罩", None, None)


@pytest.mark.parametrize("masked, unverified, expected", [
    ({PHONE}, set(), "masked"),              # 取自遮罩列
    ({"vars.kpi.i"}, set(), "masked"),       # 遮罩列的那一格只嵌在下标里：推演整个按遮罩处理，原值也不给
    (set(), {AMOUNT}, "unverified"),         # 最外层的输入遮没遮核对不了
    (set(), {"vars.kpi.i"}, None),           # 只嵌在下标里、遮没遮核对不了：不碍事
    (set(), set(), None),
])
def test_withheld_value_is_one_rule_for_the_whatif_and_the_evidence_panel(masked, unverified, expected):
    metric = {"expression": f"{AMOUNT} + vars.kpi.list[vars.kpi.i]"}
    assert recompute.withheld_value(metric, 1, masked=masked, unverified=unverified) == expected
    # 表达式读不懂：有一个遮没遮核对不了的就不给
    assert recompute.withheld_value({"expression": "1 +"}, 1, unverified={AMOUNT}) == "unverified"


# --------------------------------------------------------------------------
# 真跑：验收原样的口径卡（单格原值、按位置取、按位置取的位数、编号）
# --------------------------------------------------------------------------

CUSTOMERS = [(1, "客户甲", "19900000301"), (2, "客户乙", "19900000302"), (3, "客户丙", "19900000303")]
PHONES = [c[2] for c in CUSTOMERS]
CARD_METRICS = [
    {"id": "phone_raw", "name": "首位客户手机号（单格原值）", "format": "plain",
     "expression": "cell(nodes.q_cust, 0, 'phone')"},
    {"id": "phone_pos", "name": "首位客户手机号（按位置）", "format": "plain", "expression": "vars.cust.rows[0][2]"},
    {"id": "phone_pos_len", "name": "首位客户手机号位数", "unit": "位", "format": "plain",
     "expression": "len(str(vars.cust.rows[0][2]))"},
    {"id": "id0", "name": "首位客户编号", "format": "plain", "expression": "cell(nodes.q_cust, 0, 'id')"},
]
TEXT = "首位客户编号 [[m:id0]]，号码位数 [[m:phone_pos_len]]。"


@pytest.fixture
async def engine_up():
    await run_manager.setup()
    yield
    await run_manager.shutdown()


@pytest.fixture
async def client(engine_up):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest.fixture
async def members(tmp_path):
    path = tmp_path / "members.db"
    db = sqlite3.connect(path)
    db.executescript("CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT, phone TEXT);")
    db.executemany("INSERT INTO customers VALUES (?, ?, ?)", CUSTOMERS)
    db.commit()
    db.close()
    async with SessionLocal() as session:
        row = (await session.execute(select(DataSource).where(DataSource.name == "members"))).scalar_one_or_none()
        if row is None:
            row = DataSource(name="members", kind="sqlite", database=str(path), readonly=True, enabled=True)
            session.add(row)
        row.database, row.kind, row.enabled, row.readonly = str(path), "sqlite", True, True
        row.options = {"mask_columns": ["phone"]}
        await session.commit()
        source_id = row.id
    await engines.invalidate(source_id)
    yield path
    await engines.invalidate(source_id)


def node(nid, ntype, **config):
    return {"id": nid, "type": ntype, "position": {"x": 0, "y": 0}, "data": {"label": nid, "config": config}}


def graph() -> dict:
    nodes = [
        node("start", "input"),
        node("q_cust", "tool", tool="db_query__members",
             args={"sql": "SELECT id, name, phone FROM customers ORDER BY id LIMIT 5"}),
        node("parse_cust", "transform", mode="json", template="{{ nodes.q_cust }}", assign_to="cust"),
        node("card_c", "metrics", caliber="遮罩取法对照", caliber_version="v1", metrics=CARD_METRICS),
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


def leaks(raw: str) -> list[str]:
    return [p for p in PHONES if p in raw]


async def test_the_whatif_answer_carries_no_masked_value(client, monkeypatch, members):
    """验收原样：推演答复里号码一个都没有（以前在 phone_raw、phone_pos 的 old、rendered_old 里）。"""
    row = await run_graph(monkeypatch)
    res = await client.post(f"/api/runs/{row.id}/evidence/whatif", json={"card": "card_c"})
    assert res.status_code == 200, res.text
    assert leaks(res.text) == [], leaks(res.text)
    metrics = {m["id"]: m for m in res.json()["metrics"]}
    for mid in ("phone_raw", "phone_pos", "phone_pos_len"):
        m = metrics[mid]
        assert (m["status"], m["old"], m["rendered_old"], m["withheld"]) == ("masked", "已遮罩", "已遮罩", True), m
        assert m["new"] is None and m["rendered_new"] is None and "遮罩" in m["reason"]
    assert (metrics["id0"]["status"], metrics["id0"]["old"]) == ("unchanged", 1) and "withheld" not in metrics["id0"]
    # 报告里引用了号码位数：那一处的原值也写「已遮罩」
    [seg] = [s for s in res.json()["segments"] if s["metric"] == "phone_pos_len"]
    assert seg["rendered_old"] == "已遮罩" and seg["rendered_new"] is None


async def test_the_evidence_panel_withholds_the_metric_value_by_the_same_rule(client, monkeypatch, members):
    """证据面板的指标步骤：用到遮罩列的指标 value 写「已遮罩」；报告撰写节点写作时按同一个判断把它在目录里换成了「已遮罩」，
    报告上的字也是「已遮罩」（tests/test_report_masked_metrics.py），渲染一致。没用到的照给。"""
    row = await run_graph(monkeypatch)
    [report] = (await client.get(f"/api/runs/{row.id}/evidence")).json()["reports"]
    doc = artifact_store.load(report["doc_artifact"])
    by_ref = {s["ref"]: s["id"] for s in iter_segments(doc) if s.get("ref")}
    body = (await client.get(f"/api/runs/{row.id}/evidence/segments/{by_ref['m:phone_pos_len']}")).json()
    metric = body["chain"][0]
    assert (metric["step"], metric["value"], metric["rendered"], metric["render_ok"]) == \
        ("metric", "已遮罩", "已遮罩", True), metric
    assert leaks(json.dumps(body, ensure_ascii=False)) == []
    plain = (await client.get(f"/api/runs/{row.id}/evidence/segments/{by_ref['m:id0']}")).json()
    assert plain["chain"][0]["value"] == 1
