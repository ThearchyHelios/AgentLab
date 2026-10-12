"""转抄上游口径卡的指标给不给值：在指标依赖图上一次算出的最小不动点（recompute.masked_closure）。

复核第 2 轮发现：转抄的识别逐个往上递归、追够八层（CARD_DEPTH）就按遮罩算。循环里累加自己上一轮产出的口径卡
（nodes.acc.metrics[0].value + cell(nodes.q, 0, 'amount')），写作目录里的产出指回自己，递归一直追到八层，没设任何遮罩的
工作流里也写成「已遮罩」，正式运行判失败、报错说「取自设置了遮罩的列」；每个指标的护栏 nodes.acc 选中整张卡的全部指标，
递归次数约为（指标数 + 1）的八次方，四个指标就要四十秒，事件循环被占住。封存之后循环超过八轮的，证据面板写「已遮罩」，
可验证报告把整份文档只给哈希，说法都不实。

现在：先按转抄以外的输入认每个指标（只按确定取自遮罩列的那一套，写作目录、可验证报告认老文档；证据面板、推演宁可多遮），
再沿转抄的边往下游传，直到不再变化。环（循环里抄自己上一轮的产出、几张卡互相转抄）本身不带来遮罩，环上有一个指标别的
输入取自遮罩格，整个环都不给。写作目录、证据面板、推演、可验证报告共用这一个函数，一次算完、按请求记下，和追了几层无关。

另有几处同属这一判断：上游卡当时的产出读不出来（文件不在、被改过）时转抄的值核对不了，可验证报告照样不给（以前当作认不出
出处，号码随卡出去了）；卡上记着转抄来的清单文字（里面写着号码）的卡不随文件附带，哪怕指标的值只是个字数；推演里转抄的输入
按同一张依赖图认（以前只按值认）；* 展开派生表、WITH 里没起别名的号码表达式（UPPER(phone)）算确定取自遮罩列。

真跑的部分用虚构的 SQLite 库（号码是编的），写作者是 MockChatModel 照剧本写。
"""
from __future__ import annotations

import asyncio
import sqlite3
import threading
import time

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.data.engine import engines
from app.db.base import SessionLocal
from app.db.models import DataSource, RunEvent
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
async def plain(tmp_path, engine_up):
    """没设任何遮罩的销售库：三个月的销售额，合计 425.5。"""
    path = tmp_path / "plain.db"
    db = sqlite3.connect(path)
    db.executescript("CREATE TABLE sales (month INTEGER, amount REAL);")
    db.executemany("INSERT INTO sales VALUES (?, ?)", [(1, 100.0), (2, 250.0), (3, 75.5)])
    db.commit()
    db.close()
    async with SessionLocal() as session:
        row = (await session.execute(select(DataSource).where(DataSource.name == "plainsrc"))).scalar_one_or_none()
        if row is None:
            row = DataSource(name="plainsrc", kind="sqlite", database=str(path), readonly=True, enabled=True)
            session.add(row)
        row.database, row.kind, row.enabled, row.readonly, row.options = str(path), "sqlite", True, True, {}
        await session.commit()
        source_id = row.id
    await engines.invalidate(source_id)
    yield path
    await engines.invalidate(source_id)


#: 循环里累加自己上一轮的产出：第一轮 nodes.acc 还没有，记 0
ACC = "(nodes.acc.metrics[0].value if nodes.acc else 0) + cell(nodes.q, 0, 'amount')"


def loop_graph(rounds: int, cards: list[dict], *, sql: str = "SELECT SUM(amount) AS amount FROM sales",
               source: str = "plainsrc", report: bool = True, **write) -> dict:
    """入口 → 循环（foreach rounds 轮：查询 → 几张口径卡）→ 报告（report 为假时没有）→ 成果。cards 是 (节点 id, 指标) 列表。"""
    start = node("start", "input")
    loop = node("lp", "loop", mode="foreach", items=str(list(range(rounds))))
    query = node("q", "tool", tool=f"db_query__{source}", args={"sql": sql})
    built = [node(c["id"], "metrics", caliber=f"{c['id']} 口径", caliber_version="v1", metrics=c["metrics"])
             for c in cards]
    tail = [node("write", "report", instructions="写一句", **{"on_violation": "fail", **write})] if report else []
    out = node("out", "output", fields=[{"name": "报告", "value": "{{ nodes.write.text }}"}] if report
               else [{"name": "说明", "value": "完成"}])
    edges = [{"source": "start", "target": "lp"}, {"source": "lp", "target": "q", "sourceHandle": "body"}]
    chain = ["q", *(c["id"] for c in cards)]
    edges += [{"source": a, "target": b} for a, b in zip(chain, chain[1:])]
    edges.append({"source": chain[-1], "target": "lp"})
    after = ["lp", *(n["id"] for n in tail), "out"]
    edges += [{"source": a, "target": b, **({"sourceHandle": "done"} if a == "lp" else {})}
              for a, b in zip(after, after[1:])]
    return {"nodes": [start, loop, query, *built, *tail, out], "edges": edges}


def total(expression: str = ACC, metric_id: str = "total", name: str = "累计销售额") -> dict:
    return {"id": metric_id, "name": name, "unit": "元", "decimals": 2, "format": "thousands", "expression": expression}


async def export(client, run_id: str) -> tuple[str, dict]:
    resp = await client.get(f"/api/runs/{run_id}/bundle.html")
    assert resp.status_code == 200, resp.text
    return resp.text, data_of(resp.text)


def cards_of(bundle: dict) -> list[dict]:
    return [a for a in bundle["artifacts"] if a["kind"] == "metric_set"]


# --------------------------------------------------------------------------
# 环本身不带来遮罩：没设遮罩的循环累加照常显示、正式运行成功
# --------------------------------------------------------------------------


async def test_a_card_that_adds_its_own_last_round_shows_its_value_and_the_formal_run_succeeds(client, monkeypatch,
                                                                                               plain):
    """复核者的探针：数据源没设遮罩，循环三轮，卡 acc 累加自己上一轮的产出，报告「违规处置」为判失败、引用 [[m:total]]。
    写作目录写「累计销售额 = 1,276.50元」，正文写出这个数，运行成功；证据面板的标签、点开这一处都照给；可验证报告附带
    报告文档和口径卡。以前写作目录写「已遮罩（取自设置了遮罩的列，不要引用）」，运行判失败。"""
    seen = script(monkeypatch, "累计销售额 [[m:total]]。")
    row = await finish(loop_graph(3, [{"id": "acc", "metrics": [total()]}]))
    assert row.status == "succeeded", row.error
    assert "累计销售额 = 1,276.50元" in metric_lines(seen[0]), metric_lines(seen[0])
    doc = await doc_of(row.id)
    assert "1,276.50元" in "".join(s["text"] for s in iter_segments(doc)) and "masked" not in doc["catalog"]["m:total"]
    [checked] = [e.data for e in await events(row.id) if e.type == "report.checked"]
    assert checked["ok"] is True and not checked["violations"], checked

    body = (await client.get(f"/api/runs/{row.id}/evidence")).json()
    labels = {e["alias"]: e["label"] for e in body["evidence"] if e["kind"] == "metric"}
    assert labels["m:total"] == "累计销售额 = 1,276.50元", labels
    seg = next(s for s in iter_segments(doc) if s.get("ref") == "m:total")
    chain = (await client.get(f"/api/runs/{row.id}/evidence/segments/{seg['id']}")).json()["chain"]
    assert chain[0]["value"] == 1276.5 and chain[0]["render_ok"], chain[0]
    copied = next(i for i in chain if i.get("step") == "input" and i.get("path") == "nodes.acc.metrics[0].value")
    assert copied["value"] == 851.0 and "masked" not in copied and "mask_unverified" not in copied, copied

    _, bundle = await export(client, row.id)
    [art] = [a for a in bundle["artifacts"] if a["kind"] == "report_doc"]
    assert "b64" in art and "masked" not in art, art
    assert cards_of(bundle) and all("b64" in a and "masked" not in a for a in cards_of(bundle)), cards_of(bundle)
    assert all("canonical" in e for e in bundle["events"] if e["type"] == "node.finished" and e["node_id"] == "acc")


@pytest.mark.parametrize("rounds", [3, 10])
async def test_an_old_doc_from_a_long_loop_without_masks_is_embedded(client, monkeypatch, plain, rounds):
    """封存之后按每次执行往回认：第 k 轮的卡读到的是第 k - 1 轮的产出，一轮一轮连到第一轮为止。循环十轮的老文档（写作前
    不认遮罩，这里把那一步换成什么都不认）照常随文件附带，证据面板照给。以前超过八轮就按遮罩算：面板写「已遮罩」，文档
    整份只给哈希，页面说「证据目录里写着取自遮罩列的指标的值」，总览「有 1 份报告未能核对」。"""
    import app.api.evidence as evidence_api

    async def nothing(*_args, **_kwargs):
        return []

    monkeypatch.setattr(evidence_api, "withheld_metrics", nothing)
    script(monkeypatch, "累计销售额 [[m:total]]。")
    row = await finish(loop_graph(rounds, [{"id": "acc", "metrics": [total()]}]))
    assert row.status == "succeeded", row.error
    body = (await client.get(f"/api/runs/{row.id}/evidence")).json()
    labels = {e["alias"]: e["label"] for e in body["evidence"] if e["kind"] == "metric"}
    assert labels["m:total"] == f"累计销售额 = {425.5 * rounds:,.2f}元", labels
    _, bundle = await export(client, row.id)
    [art] = [a for a in bundle["artifacts"] if a["kind"] == "report_doc"]
    assert "b64" in art and "masked" not in art, art
    assert all("canonical" in e for e in bundle["events"] if e["type"] == "node.finished" and e["node_id"] == "write")


async def test_cards_that_copy_each_other_in_a_loop_show_their_values(client, monkeypatch, plain):
    """几张卡互相转抄（acc 抄 peer 上一轮的、peer 抄 acc 这一轮的），没有哪个指标别的输入取自遮罩格：照给，正式运行成功。"""
    seen = script(monkeypatch, "累计 [[m:total]]，对照 [[m:mirror]]。")
    cards = [
        {"id": "acc", "metrics": [
            total("(nodes.peer.metrics[0].value if nodes.peer else 0) + cell(nodes.q, 0, 'amount')")]},
        {"id": "peer", "metrics": [total("(nodes.acc.metrics[0].value if nodes.acc else 0) + 1", "mirror", "对照值")]},
    ]
    row = await finish(loop_graph(3, cards))
    assert row.status == "succeeded", row.error
    lines = metric_lines(seen[0])
    assert "累计销售额 = 1,278.50元" in lines and "对照值 = 1,279.50元" in lines, lines
    _, bundle = await export(client, row.id)
    assert all("b64" in a and "masked" not in a for a in cards_of(bundle)), cards_of(bundle)


# --------------------------------------------------------------------------
# 环上有一个指标别的输入取自遮罩格：整个环都不给
# --------------------------------------------------------------------------


async def test_a_loop_with_a_masked_input_is_withheld_as_a_whole(client, monkeypatch, vip):
    """会员库的号码设了遮罩。acc 每轮累加号码除以 7 的余数（输入取自遮罩格，值是算出来的数，宁可多遮），peer 抄 acc、acc 也抄
    peer：两张卡在一个环上，都写「已遮罩」，引用 peer 的正式运行判失败（masked_cell）。同一个循环里只累加自己、没碰号码的
    side 照给。可验证报告里 acc、peer 每一轮的节点完成事件都只给哈希（预览逐条写着「指标（名称）= 值」），side 的照常带
    规范串。"""
    seen = script(monkeypatch, "对照 [[m:mirror]]，轮数 [[m:rounds]]。")
    cards = [
        {"id": "acc", "metrics": [total("(nodes.peer.metrics[0].value if nodes.peer else 0) "
                                        "+ cell(nodes.q, 0, 'phone') % 7", "digits", "号码余数累计")]},
        {"id": "peer", "metrics": [total("(nodes.acc.metrics[0].value if nodes.acc else 0) + 1", "mirror", "对照值")]},
        {"id": "side", "metrics": [{"id": "rounds", "name": "轮数", "unit": "轮", "format": "thousands",
                                    "expression": "(nodes.side.metrics[0].value if nodes.side else 0) + 1"}]},
    ]
    spec = loop_graph(3, cards, sql="SELECT id, phone FROM customers ORDER BY id", source="vip")
    row = await finish(spec)
    assert row.status == "failed" and "m:mirror" in (row.error or "") and "遮罩" in row.error, row.error
    lines = metric_lines(seen[0])
    for name in ("号码余数累计", "对照值"):
        assert f"{name} = 已遮罩（取自设置了遮罩的列，不要引用）" in lines, lines
    assert "轮数 = 3轮" in lines, lines
    [checked] = [e.data for e in await events(row.id) if e.type == "report.checked"][-1:]
    assert [v["ref"] for v in checked["violations"] if v["code"] == "masked_cell"] == ["m:mirror"], checked

    spec = loop_graph(3, cards, sql="SELECT id, phone FROM customers ORDER BY id", source="vip", report=False)
    row = await finish(spec)
    assert row.status == "succeeded", row.error
    _, bundle = await export(client, row.id)
    finished = {n: [e for e in bundle["events"] if e["type"] == "node.finished" and e["node_id"] == n]
                for n in ("acc", "peer", "side")}
    assert all(len(found) == 3 for found in finished.values()), finished
    assert not any("canonical" in e for n in ("acc", "peer") for e in finished[n]), finished
    assert all("canonical" in e for e in finished["side"]), finished["side"]


# --------------------------------------------------------------------------
# 上游卡当时的产出读不出来：转抄的值核对不了，可验证报告照样不给
# --------------------------------------------------------------------------


def chain(*nodes: dict) -> dict:
    return {"nodes": list(nodes), "edges": [{"source": a["id"], "target": b["id"]} for a, b in zip(nodes, nodes[1:])]}


async def bundle_leaks(client, run_id: str) -> dict:
    """导出一份可验证报告：整页里的号码、每份附带的工件里的号码、带规范串的事件里的号码（事件按 序号:类型:节点 记）。"""
    import base64

    html, bundle = await export(client, run_id)
    out: dict = {"html": leaks(html), "artifacts": {}, "events": {}}
    for art in bundle["artifacts"]:
        if "b64" in art and (found := leaks(base64.b64decode(art["b64"]).decode("utf-8", "replace"))):
            out["artifacts"][f"{art['kind']}:{art.get('node_id')}"] = found
    for item in bundle["events"]:
        if "canonical" in item and (found := leaks(item["canonical"])):
            out["events"][f"{item['seq']}:{item['type']}:{item['node_id']}"] = found
    return out


async def _drop_output(run_id: str, node_id: str) -> None:
    """删掉一个节点产出的工件文件：服务端缺文件（missing-card 样本那种），导出时读不出来。"""
    from app.core.artifact_store import _path_of

    async with SessionLocal() as session:
        rows = (await session.execute(select(RunEvent).where(RunEvent.run_id == run_id))).scalars().all()
    for row in rows:
        if row.type == "node.finished" and row.node_id == node_id and (row.data or {}).get("artifact"):
            _path_of(row.data["artifact"]).unlink(missing_ok=True)


@pytest.mark.parametrize("expr", ["nodes.card.metrics[0].value", "vars.kpi.metrics[0].value", "nodes.card.text"])
async def test_a_copy_whose_upstream_output_is_gone_ships_without_the_number(client, monkeypatch, vip, expr):
    """卡 2 转抄卡 1 的号码，导出之前卡 1 的节点产出工件不在了（服务端缺文件）。转抄的值核对不了：卡 2 的节点完成事件只给
    哈希，整个文件里没有号码。以前当作认不出出处、照给，卡 2 的规范串和页面里都带着号码。"""
    spec = chain(
        node("start", "input"),
        node("q_cust", "tool", tool="db_query__vip", args={"sql": "SELECT id, name, phone FROM customers ORDER BY id"}),
        node("card", "metrics", caliber="会员口径", caliber_version="v1", assign_to="kpi", metrics=[
            {"id": "phone_raw", "name": "首位客户手机号", "format": "plain",
             "expression": "cell(nodes.q_cust, 0, 'phone')"}]),
        node("card2", "metrics", caliber="转抄口径", caliber_version="v1", metrics=[
            {"id": "p2", "name": "转抄", "format": "plain", "expression": expr}]),
        node("out", "output", fields=[{"name": "说明", "value": "完成"}]),
    )
    row = await finish(spec)
    assert row.status == "succeeded", row.error
    assert await bundle_leaks(client, row.id) == {"html": [], "artifacts": {}, "events": {}}
    await _drop_output(row.id, "card")
    got = await bundle_leaks(client, row.id)
    assert got == {"html": [], "artifacts": {}, "events": {}}, got
    _, bundle = await export(client, row.id)
    assert "canonical" not in event(bundle, "node.finished", "card2")


async def test_a_copy_of_an_ordinary_metric_whose_upstream_output_is_gone_is_still_judged_safely(client, monkeypatch,
                                                                                                vip):
    """对照：卡 2 转抄的是客户数（没碰号码），卡 1 的产出不在了也核对不了，卡 2 的节点完成事件同样只给哈希（宁可多挡）；
    卡 1 的产出还在时照常带规范串。"""
    spec = chain(
        node("start", "input"),
        node("q_cust", "tool", tool="db_query__vip", args={"sql": "SELECT id, name, phone FROM customers ORDER BY id"}),
        node("parse", "transform", mode="json", template="{{ nodes.q_cust }}", assign_to="cust"),
        node("card", "metrics", caliber="会员口径", caliber_version="v1", metrics=[
            {"id": "n_cust", "name": "客户数", "unit": "位", "format": "thousands", "expression": "len(vars.cust.rows)"}]),
        node("card2", "metrics", caliber="转抄口径", caliber_version="v1", metrics=[
            {"id": "n2", "name": "转抄客户数", "unit": "位", "format": "thousands",
             "expression": "nodes.card.metrics[0].value"}]),
        node("out", "output", fields=[{"name": "说明", "value": "完成"}]),
    )
    row = await finish(spec)
    assert row.status == "succeeded", row.error
    _, bundle = await export(client, row.id)
    assert "canonical" in event(bundle, "node.finished", "card2")
    await _drop_output(row.id, "card")
    _, bundle = await export(client, row.id)
    assert "canonical" not in event(bundle, "node.finished", "card2")


# --------------------------------------------------------------------------
# 最小不动点本身（recompute.masked_closure，纯函数）
# --------------------------------------------------------------------------


def _metric(expression: str, value=1.0) -> dict:
    return {"id": "m", "name": "m", "value": value, "expression": expression}


def test_a_cycle_on_its_own_brings_no_mask():
    """自己指回自己、两张卡互相转抄：环上没有哪个指标别的输入取自遮罩格，都照给。"""
    from app.engine.recompute import CopyEdge, MetricLinks, masked_closure

    a, b, c = ("a", 0), ("b", 0), ("c", 0)
    got = masked_closure({
        a: MetricLinks(_metric("nodes.a.metrics[0].value + 1"),
                       copies=[CopyEdge("nodes.a.metrics[0].value", (a,), False)]),
        b: MetricLinks(_metric("nodes.c.metrics[0].value"), copies=[CopyEdge("nodes.c.metrics[0].value", (c,), False)]),
        c: MetricLinks(_metric("nodes.b.metrics[0].value + nodes.c"), copies=[
            CopyEdge("nodes.b.metrics[0].value", (b,), False), CopyEdge("nodes.c", (c,), True)]),
    })
    assert {k: v.word for k, v in got.items()} == {a: None, b: None, c: None}
    assert all(kind is None for v in got.values() for kind, _, _ in v.copied.values())


def test_one_masked_input_on_a_cycle_withholds_the_whole_cycle():
    """环上 b 另有一个输入取自遮罩格（phone）：b、c 都不给，转抄它们的输入上写着 phone；转抄 c 的整份指标列表、值只是个数的 d
    同样不给（转抄的保守规则：上游有指标不给，哪种写法转抄都不给，不再按值认），环外没连上的 e 照给。"""
    from app.engine.recompute import CopyEdge, MetricLinks, masked_closure

    b, c, d, e = ("b", 0), ("c", 0), ("d", 0), ("e", 0)
    got = masked_closure({
        b: MetricLinks(_metric("nodes.c.metrics[0].value + cell(nodes.q, 0, 'phone')"),
                       direct={"cell(nodes.q, 0, 'phone')": ("masked", ("phone",))},
                       copies=[CopyEdge("nodes.c.metrics[0].value", (c,), False)]),
        c: MetricLinks(_metric("nodes.b.metrics[0].value"), copies=[CopyEdge("nodes.b.metrics[0].value", (b,), False)]),
        d: MetricLinks(_metric("len(nodes.c.metrics)", 1), copies=[CopyEdge("nodes.c.metrics", (c,), True)]),
        e: MetricLinks(_metric("nodes.e.metrics[0].value"), copies=[CopyEdge("nodes.e.metrics[0].value", (e,), False)]),
    }, secrets=lambda: None)
    assert {k: v.word for k, v in got.items()} == {b: "masked", c: "masked", d: "masked", e: None}
    assert got[c].copied["nodes.b.metrics[0].value"] == ("masked", ("phone",), None)
    assert got[b].copied["nodes.c.metrics[0].value"] == ("masked", ("phone",), None)
    assert got[d].copied["nodes.c.metrics"] == ("masked", ("phone",), None)


def test_a_whole_copy_of_a_card_with_a_withheld_metric_is_withheld_whatever_its_value():
    """整张卡、清单文字、整份指标列表转抄：上游有指标不给时，转抄出来的都不给，值里有没有遮罩值都一样（转抄的保守规则）。以前
    整张卡转抄同整份结果、只按值认：上游的指标由遮罩格算出来（年薪 = 月薪 × 12）时值里认不出，清单文字原样照给。"""
    from app.engine.recompute import CopyEdge, MetricLinks, masked_closure

    up, text, count = ("up", 0), ("text", 0), ("count", 0)
    links = {
        up: MetricLinks(_metric("cell(nodes.q, 0, 'salary') * 12", 97479.0),
                        direct={"cell(nodes.q, 0, 'salary')": ("masked", ("salary",))}),
        text: MetricLinks(_metric("nodes.up.text", "- annual（首位年薪）= 97479.0元"),
                          copies=[CopyEdge("nodes.up.text", (up,), True)]),
        count: MetricLinks(_metric("len(nodes.up.metrics)", 1), copies=[CopyEdge("nodes.up.metrics", (up,), True)]),
    }
    got = masked_closure(links, secrets=lambda: None)
    assert (got[up].word, got[text].word, got[count].word) == ("masked", "masked", "masked")
    assert got[text].copied["nodes.up.text"] == ("masked", ("salary",), None)


def test_an_unreadable_upstream_cannot_be_checked():
    """上游卡当时的产出读不出来（CopyEdge.unreadable）：转抄的输入核对不了；它是最外层的引用时指标的值也核对不了。整张卡
    转抄的同整份结果（table_unverified），指标的值另按值认。之前认好的上游（known）直接用，不再重认。"""
    from app.engine.recompute import CopyEdge, Judged, MetricLinks, masked_closure

    why = "转抄的口径卡结果无法读取"
    a, w, k, known = ("a", 0), ("w", 0), ("k", 0), ("known", 0)
    got = masked_closure({
        a: MetricLinks(_metric("nodes.up.metrics[0].value"),
                       copies=[CopyEdge("nodes.up.metrics[0].value", (), False, why)]),
        w: MetricLinks(_metric("len(nodes.up.text)", 3), copies=[CopyEdge("nodes.up.text", (), True, why)]),
        k: MetricLinks(_metric("nodes.known.metrics[0].value"),
                       copies=[CopyEdge("nodes.known.metrics[0].value", (known,), False)]),
    }, {known: Judged("masked", {}, ("phone",))}, secrets=lambda: None)
    assert got[a].word == "unverified" and got[a].copied["nodes.up.metrics[0].value"] == ("unverified", (), why)
    assert got[w].word is None and got[w].copied["nodes.up.text"] == ("table_unverified", (), why)
    assert got[k].word == "masked" and got[k].copied["nodes.known.metrics[0].value"] == ("masked", ("phone",), None)
    assert known not in got


def test_a_long_chain_with_many_cycles_is_solved_in_linear_time():
    """两千个指标，每个转抄前一个、自己、整张前一张卡（护栏），只有最前面那个取自遮罩格：全部不给，一秒以内。以前逐个
    往上递归，次数随护栏选中的指标个数指数增长。"""
    from app.engine.recompute import CopyEdge, MetricLinks, masked_closure

    links = {(0, 0): MetricLinks(_metric("cell(nodes.q, 0, 'phone')"), direct={"cell": ("masked", ("phone",))})}
    for i in range(1, 2000):
        links[(i, 0)] = MetricLinks(_metric("x"), copies=[
            CopyEdge("prev", ((i - 1, 0),), False), CopyEdge("self", ((i, 0),), False),
            CopyEdge("guard", ((i - 1, 0), (i, 0)), True)])
    started = time.perf_counter()
    got = masked_closure(links, secrets=lambda: None)
    assert time.perf_counter() - started < 1.0
    assert all(v.word == "masked" for v in got.values())


# --------------------------------------------------------------------------
# 合成的长循环：写作目录、可验证报告的判断都在一秒内
# --------------------------------------------------------------------------

#: 三张卡在一个循环里互相转抄：acc 抄 acc2、acc2 抄 acc3、acc3 抄 acc
LOOP_CARDS = ("acc", "acc2", "acc3")


def _put(obj) -> str:
    """照工件库的样子落一份（规范 JSON，id 是 sha256），写作目录读快照时按 id 去读。"""
    from app.core.artifact_store import _path_of, canonical_json, content_hash

    text = canonical_json(obj)
    artifact = content_hash(text)
    path = _path_of(artifact)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return artifact


def _snapshot() -> tuple[str, dict]:
    snap = {"columns": ["amount", "phone"], "rows": [[425.5, PHONES[0]]], "sql": "SELECT amount, phone FROM sales",
            "mask_columns": ["phone"], "truncated": False}
    return _put(snap), snap


def _card(card: str, k: int, snap: str, seen: dict, masked: bool) -> dict:
    """一轮里卡 card 的产出：k 个指标，第 i 个转抄自己上一轮的第 i 个、整张自己（护栏）、下一张卡的第 i 个和整张下一张卡，
    再加快照里的一格（masked 时 acc 的第一个指标取号码列）。seen 是这一刻每张卡最后一次的产出（节点 id → 产出）。"""
    other = LOOP_CARDS[(LOOP_CARDS.index(card) + 1) % len(LOOP_CARDS)]
    metrics = []
    for i in range(k):
        column = "phone" if masked and card == "acc" and i == 0 else "amount"
        inputs = []
        value = 425.5
        for source in (card, other):
            before = seen.get(source)
            copied = before["metrics"][i]["value"] if before else None
            value += copied or 0
            inputs += [{"path": f"nodes.{source}.metrics[{i}].value", "value": copied, "node_id": source,
                        "via": "metric", "status": "ok" if before else "missing"},
                       {"path": f"nodes.{source}", "value": "{6 个字段}" if before else None, "node_id": source,
                        "via": "metric", "status": "ok" if before else "missing"}]
        inputs.append({"path": f"cell(nodes.q, 0, '{column}')", "value": 425.5, "node_id": "q", "via": "tool_cell",
                       "artifact": snap, "locator": {"row": 0, "column": column}, "status": "verified"})
        expression = (f"(nodes.{card}.metrics[{i}].value if nodes.{card} else 0) "
                      f"+ (nodes.{other}.metrics[{i}].value if nodes.{other} else 0) + cell(nodes.q, 0, '{column}')")
        metrics.append({"id": f"t{i}", "name": f"累计{i}", "value": value, "unit": "元", "decimals": 2,
                        "format": "thousands", "expression": expression, "inputs": inputs})
    return {"kind": "metric_set", "caliber": f"{card} 口径", "caliber_version": "v1", "metrics": metrics}


def _within(seconds: float, work):
    """在另一个线程里做 work，交回 (结果, 用时)。超过十秒还没做完就判失败：以前的递归卡住的是整个线程，等它没有意义。"""
    box: dict = {}

    def run() -> None:
        started = time.perf_counter()
        try:
            box["value"] = work()
        except BaseException as e:  # noqa: BLE001 - 交回主线程再抛
            box["error"] = e
        box["elapsed"] = time.perf_counter() - started

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(10)
    assert not thread.is_alive(), "十秒还没认完"
    if "error" in box:
        raise box["error"]
    assert box["elapsed"] < seconds, f"用了 {box['elapsed']:.2f} 秒"
    return box["value"]


@pytest.mark.parametrize("masked", [False, True])
def test_the_writer_catalog_judges_a_long_loop_of_copying_cards_within_a_second(monkeypatch, masked):
    """写作目录（withheld_metrics，运行状态里每张卡只有最后一次的产出）：三张卡、各 8 个指标，循环十轮后互相转抄、护栏选中
    整张卡，一秒以内认完，每个指标只认一次（同一张依赖图上认，转抄它的指标不再重认）。没碰号码列的全部照给；acc 的第一个
    指标取号码列时，三张卡在一个环上互相转抄，按整张卡认（转抄的保守规则），全部不给。以前四个指标就要四十秒。"""
    import app.api.evidence as evidence_api
    from app.api.evidence import withheld_metrics

    built: list[str] = []
    original = evidence_api._InputMasks.__init__

    def counting(self, sealed, masks, card_node, artifact, **kwargs):
        built.append(card_node)
        original(self, sealed, masks, card_node, artifact, **kwargs)

    monkeypatch.setattr(evidence_api._InputMasks, "__init__", counting)

    snap, _ = _snapshot()
    seen: dict = {}
    for _ in range(10):
        for card in LOOP_CARDS:
            out = _card(card, 8, snap, seen, masked)
            seen[card] = {**out, "artifact": _put(out), "text": "清单"}
    catalog = {f"m:{card}.t{i}": {"kind": "metric", "node_id": card, "artifact": seen[card]["artifact"],
                                  "locator": {"metric": f"t{i}"}, "value": seen[card]["metrics"][i]["value"]}
               for card in LOOP_CARDS for i in range(8)}
    got = _within(1.0, lambda: asyncio.run(withheld_metrics(catalog, nodes=seen, ledger=[])))
    assert sorted(got) == (sorted(catalog) if masked else []), got
    assert len(built) == 3 * 8, len(built)


@pytest.mark.parametrize("masked", [False, True])
def test_the_bundle_judges_a_long_loop_of_copying_cards_within_a_second(masked):
    """可验证报告（按每次执行往回认：第 k 轮的卡读到的是各卡在它之前最后一次的产出）：同样的三张卡、8 个指标、十轮，另有
    一份报告文档，证据目录里写着最后一轮三张卡的全部指标（升级前的老文档那样写着原值）。导出一秒以内。没碰号码列的：文档
    照常附带，每一轮节点完成事件都带规范串。acc 的第一个指标取号码列时：文档整份只给哈希（证据目录里写着取自、转抄自号码列
    的 t0），卡上的 t0 取自或转抄自号码列的那几次执行都只给哈希——只有第一轮的 acc2 照常带规范串：它转抄的 acc3 那时还没跑，
    没连上号码。以前追够八层就按遮罩算：没碰号码列的十轮循环，文档也整份只给哈希。"""
    from app.bundle.build import build_bundle
    from app.core.artifact_store import canonical_json, content_hash

    store: dict[str, bytes] = {}

    def keep(obj) -> str:
        raw = canonical_json(obj).encode()
        store[content_hash(raw)] = raw
        return content_hash(raw)

    snap = keep(_snapshot()[1])
    seen: dict = {}
    events: list = []
    for _ in range(10):
        for card in LOOP_CARDS:
            out = _card(card, 8, snap, seen, masked)
            art = keep(out)
            seen[card] = {**out, "artifact": art, "text": "清单"}
            events.append((len(events) + 1, "node.started", card, {}))
            events.append((len(events) + 1, "node.finished", card, {
                "artifact": keep(seen[card]), "preview": {"artifact": art},
                "evidence": [{"kind": "metric_set", "node_id": card, "artifact": art}]}))
    catalog = {f"m:{card}.t{i}": {"alias": f"m:{card}.t{i}", "kind": "metric", "node_id": card,
                                  "artifact": seen[card]["artifact"], "locator": {"metric": f"t{i}"},
                                  "value": seen[card]["metrics"][i]["value"]} for card in LOOP_CARDS for i in range(8)}
    doc = keep({"schema": "agentlab.report/1", "run_id": "r1", "node_id": "write", "markdown": "", "blocks": [],
                "catalog": catalog})
    events.append((len(events) + 1, "report.checked", "write", {"doc_artifact": doc}))
    graph = {"nodes": [{"id": card, "type": "metrics", "data": {"label": card}} for card in LOOP_CARDS]
             + [{"id": "write", "type": "report", "data": {"label": "write"}}]}
    bundle = _within(1.0, lambda: build_bundle(
        {"id": "r1", "status": "succeeded"}, events, manifest_seq=len(events), manifest_hash="0" * 64,
        read=store.get, exported_at="2026-10-12T00:00:00+00:00", graph=graph, source_masks={}))
    [art] = [a for a in bundle["artifacts"] if a["kind"] == "report_doc"]
    embedded = "b64" in art and "masked" not in art
    assert embedded if not masked else (art.get("masked") is True and "b64" not in art), art
    shown = [("canonical" in e) for e in bundle["events"] if e["type"] == "node.finished"]
    assert len(shown) == 30, shown
    assert shown == ([True] * 30 if not masked else [False, True, False, *[False] * 27]), shown


# --------------------------------------------------------------------------
# * 展开派生表、WITH 里没起别名的号码表达式：算确定取自遮罩列
# --------------------------------------------------------------------------

#: 号码设了遮罩的会员表（号码是编的），外加一张订单表
_MEMBERS_DDL = ("CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT, phone TEXT, amount REAL);"
                "CREATE TABLE orders (oid INTEGER, cid INTEGER, total REAL);")


def _snap_of(sql: str) -> dict:
    db = sqlite3.connect(":memory:")
    db.executescript(_MEMBERS_DDL)
    db.executemany("INSERT INTO customers VALUES (?, ?, ?, ?)",
                   [(i + 1, f"客户{i}", phone, 100.0 * (i + 1)) for i, phone in enumerate(PHONES)])
    db.executemany("INSERT INTO orders VALUES (?, ?, ?)", [(10, 1, 99.5), (11, 2, 15.0)])
    cur = db.execute(sql)
    return {"sql": sql, "columns": [d[0] for d in cur.description], "rows": [list(r) for r in cur.fetchall()]}


@pytest.mark.parametrize("sql, column", [
    ("WITH a AS (SELECT id, LOWER(name), UPPER(phone) FROM customers) SELECT * FROM a ORDER BY id", "UPPER(phone)"),
    ("SELECT * FROM (SELECT id, phone || '' FROM customers) ORDER BY id", "phone || ''"),
    ("SELECT * FROM (SELECT id, SUBSTR(phone, 1, 11) FROM customers)", "SUBSTR(phone, 1, 11)"),
    ("SELECT t.* FROM (SELECT UPPER(phone) FROM customers) t", "UPPER(phone)"),
    ("SELECT a.id, b.* FROM customers a JOIN (SELECT id AS bid, UPPER(phone) FROM customers) b ON a.id = b.bid",
     "UPPER(phone)"),
    ("WITH t AS (SELECT id, phone FROM customers), u AS (SELECT id, UPPER(phone) FROM t) SELECT * FROM u",
     "UPPER(phone)"),
    ("SELECT * FROM (SELECT id, UPPER(phone) FROM customers UNION ALL SELECT oid, total FROM orders)", "UPPER(phone)"),
    ("SELECT * FROM (SELECT id, name || phone, LOWER(name) FROM customers)", "name || phone"),
])
def test_an_unaliased_masked_expression_expanded_by_a_star_is_surely_masked(sql, column):
    """SQLite、MySQL 拿没起别名的表达式的原文当列名，* 展开派生表、WITH、UNION 的一支时原样带出来。认遮罩列的那一套认不出它
    落在哪个位置、整份算，以前确定取自遮罩列的那几列里没有它：写作目录、单元格引用的自查照给了号码。现在按列名再认一遍：
    算确定取自遮罩列（_sure），别的列照旧不算。"""
    from app.api.evidence import _sure

    snap = _snap_of(sql)
    sure, _ = _sure(snap, {"phone"}, "sqlite")
    assert column.lower() in sure, sure
    assert not sure - {column.lower(), "phone"}, sure


@pytest.mark.parametrize("sql", [
    "SELECT * FROM (SELECT id, COUNT(phone) FROM customers GROUP BY id)",
    "SELECT * FROM (SELECT id, COUNT(*) FILTER (WHERE phone IS NULL) FROM customers GROUP BY id)",
    "SELECT * FROM (SELECT id, CASE WHEN phone IS NULL THEN '未留' ELSE '已留' END FROM customers)",
    "SELECT * FROM (SELECT id, LOWER(name), amount * 2 FROM customers)",
])
def test_columns_named_like_counts_or_labels_of_the_masked_column_stay_ordinary(sql):
    """按列名认的只算值取自遮罩列的：只数个数的（COUNT(phone)、FILTER）、只装着分档标签的（CASE 写死的字）、没碰号码列的
    照旧不算，指标照给、照常引用。"""
    from app.api.evidence import _sure

    sure, _ = _sure(_snap_of(sql), {"phone"}, "sqlite")
    assert not sure - {"phone"}, sure


async def test_a_metric_from_an_unaliased_masked_expression_is_withheld_everywhere(client, monkeypatch, vip):
    """真跑：WITH 里没起别名的 UPPER(phone)、外层 * 展开，指标 cell(nodes.q, 0, 'UPPER(phone)')。写作目录写「已遮罩」、文档的
    证据目录写「已遮罩」，可验证报告里没有号码。以前三处都写着号码。"""
    seen = script(monkeypatch, "首位客户已登记。")
    sql = "WITH a AS (SELECT id, LOWER(name), UPPER(phone) FROM customers) SELECT * FROM a ORDER BY id"
    row = await finish(chain(
        node("start", "input"),
        node("q", "tool", tool="db_query__vip", args={"sql": sql}),
        node("card", "metrics", caliber="会员口径", caliber_version="v1", metrics=[
            {"id": "p", "name": "首位客户联系方式", "format": "plain", "expression": "cell(nodes.q, 0, 'UPPER(phone)')"}]),
        node("write", "report", instructions="写一句", on_violation="fail"),
        node("out", "output", fields=[{"name": "报告", "value": "{{ nodes.write.text }}"}]),
    ))
    assert row.status == "succeeded", row.error
    lines = metric_lines(seen[0])
    assert "首位客户联系方式 = 已遮罩（取自设置了遮罩的列，不要引用）" in lines and leaks(lines) == [], lines
    doc = await doc_of(row.id)
    assert doc["catalog"]["m:p"]["masked"] is True and leaks(doc) == []
    assert await bundle_leaks(client, row.id) == {"html": [], "artifacts": {}, "events": {}}


# --------------------------------------------------------------------------
# 推演：转抄的输入按同一张依赖图认
# --------------------------------------------------------------------------


async def test_the_whatif_judges_copied_inputs_on_the_same_graph(client, monkeypatch, vip):
    """卡 2 转抄卡 1 的两个指标：号码除以 1000 的余数（值是三位数，按值认不出，输入取自遮罩格）、客户编号，另转抄卡 b 的客户
    编号。推演卡 2：转抄卡 1 的两个输入都不给值、不能改，指标的原值写「已遮罩」，原因写「转抄的口径卡指标取自设置了遮罩的列」
    ——转抄编号的也一样，卡 1 上有取自遮罩格的指标（转抄的保守规则）；转抄卡 b（没有取自遮罩格的指标）的照常推演。以前推演只
    按值认，余数照给、还能改，改一下就看得出原值。证据面板点开卡 2 的指标同样写「已遮罩」。"""
    script(monkeypatch, "卡二的对照 [[m:ten]]，余数 [[m:tail2]]。")
    row = await finish(chain(
        node("start", "input"),
        node("q_cust", "tool", tool="db_query__vip", args={"sql": "SELECT id, name, phone FROM customers ORDER BY id"}),
        node("card", "metrics", caliber="会员口径", caliber_version="v1", metrics=[
            {"id": "tail", "name": "号码尾数", "format": "plain", "expression": "cell(nodes.q_cust, 0, 'phone') % 1000"},
            {"id": "first", "name": "首位编号", "format": "plain", "expression": "cell(nodes.q_cust, 0, 'id')"}]),
        node("card_b", "metrics", caliber="编号口径", caliber_version="v1", metrics=[
            {"id": "first_b", "name": "首位编号", "format": "plain", "expression": "cell(nodes.q_cust, 0, 'id')"}]),
        node("card2", "metrics", caliber="转抄口径", caliber_version="v1", metrics=[
            {"id": "tail2", "name": "转抄尾数", "format": "plain", "expression": "nodes.card.metrics[0].value + 1"},
            {"id": "first2", "name": "转抄编号", "format": "plain", "expression": "nodes.card.metrics[1].value + 1"},
            {"id": "ten", "name": "编号十倍", "format": "plain", "expression": "nodes.card_b.metrics[0].value * 10"}]),
        node("write", "report", instructions="写一句", on_violation="flag"),
        node("out", "output", fields=[{"name": "报告", "value": "{{ nodes.write.text }}"}]),
    ))
    assert row.status == "succeeded", row.error
    res = await client.post(f"/api/runs/{row.id}/evidence/whatif", json={"card": "card2"})
    assert res.status_code == 200, res.text
    body = res.json()
    metrics = {m["id"]: m for m in body["metrics"]}
    tail = metrics["tail2"]
    assert (tail["status"], tail["old"], tail["withheld"]) == ("masked", "已遮罩", True), tail
    [copied] = tail["inputs"]
    assert copied["masked"] is True and copied["old"] is None and copied["editable"] is False, copied
    assert {"metric": "tail2", "input": "nodes.card.metrics[0].value",
            "reason": "转抄的口径卡指标取自设置了遮罩的列，不能用于推演"} in body["unsupported"], body["unsupported"]
    first = metrics["first2"]
    assert (first["old"], first["withheld"]) == ("已遮罩", True) and first["inputs"][0]["editable"] is False, first
    assert {"metric": "first2", "input": "nodes.card.metrics[1].value",
            "reason": "转抄的口径卡指标取自设置了遮罩的列，不能用于推演"} in body["unsupported"], body["unsupported"]
    ten = metrics["ten"]
    assert ten["old"] == 10 and ten["inputs"][0]["editable"] is True and ten["inputs"][0]["old"] == 1, ten
    res = await client.post(f"/api/runs/{row.id}/evidence/whatif",
                            json={"card": "card2", "overrides": {"inputs": {"nodes.card_b.metrics[0].value": 5}}})
    assert res.status_code == 200, res.text
    assert {m["id"]: m for m in res.json()["metrics"]}["ten"]["new"] == 50
    res = await client.post(f"/api/runs/{row.id}/evidence/whatif",
                            json={"card": "card2", "overrides": {"inputs": {"nodes.card.metrics[0].value": 5}}})
    assert res.status_code == 422 and "遮罩" in res.json()["detail"], res.text

    doc = await doc_of(row.id)
    seg = next(s for s in iter_segments(doc) if s.get("ref") == "m:tail2")
    chain_ = (await client.get(f"/api/runs/{row.id}/evidence/segments/{seg['id']}")).json()["chain"]
    assert chain_[0]["value"] == "已遮罩", chain_[0]


async def test_a_card_recording_a_copied_list_with_a_number_is_not_embedded(client, monkeypatch, vip):
    """卡 2 数卡 1 清单文字的字数（len(nodes.card.text)）：指标的值只是个数，可卡 1 上有取自号码列的指标，转抄的保守规则下
    同样写「已遮罩」（写作者坚持引用、标注照常产出）；卡 2 上记着转抄来的清单文字本身，里面写着号码，卡 2 只给 id，文件里
    没有号码。以前指标的值照给，卡 2 就原样附带了。"""
    script(monkeypatch, "清单字数 [[m:n_len]]。")
    row = await finish(chain(
        node("start", "input"),
        node("q_cust", "tool", tool="db_query__vip", args={"sql": "SELECT id, name, phone FROM customers ORDER BY id"}),
        node("card", "metrics", caliber="会员口径", caliber_version="v1", metrics=[
            {"id": "phone_raw", "name": "首位客户手机号", "format": "plain",
             "expression": "cell(nodes.q_cust, 0, 'phone')"}]),
        node("card2", "metrics", caliber="转抄口径", caliber_version="v1", metrics=[
            {"id": "n_len", "name": "清单字数", "unit": "字", "format": "thousands", "expression": "len(nodes.card.text)"}]),
        node("write", "report", instructions="写一句", on_violation="flag"),
        node("out", "output", fields=[{"name": "报告", "value": "{{ nodes.write.text }}"}]),
    ))
    assert row.status == "succeeded", row.error
    doc = await doc_of(row.id)
    assert doc["catalog"]["m:n_len"]["masked"] is True and doc["catalog"]["m:n_len"]["value"] == "已遮罩"
    _, bundle = await export(client, row.id)
    cards = {a["node_id"]: a for a in cards_of(bundle)}
    assert cards["card2"].get("masked") is True and "b64" not in cards["card2"], cards
    assert await bundle_leaks(client, row.id) == {"html": [], "artifacts": {}, "events": {}}


async def test_a_real_loop_of_eight_metrics_is_judged_within_a_second(client, monkeypatch, plain):
    """真跑：两张卡各 8 个指标，循环十轮，每个指标累加自己上一轮的同一个指标、另一张卡的同一个指标（护栏选中整张卡）。正式
    运行成功、引用的数照常写出；写作目录的判断（withheld_metrics）、可验证报告的构建（build_bundle）各在一秒以内。以前每个
    指标的护栏选中整张卡的全部指标，递归次数约为九的八次方，事件循环被占住、整个服务停住。"""
    import app.api.bundle as bundle_api
    import app.api.evidence as evidence_api

    spent: dict[str, float] = {}
    writer, builder = evidence_api.withheld_metrics, bundle_api.build_bundle

    async def timed_writer(*args, **kwargs):
        started = time.perf_counter()
        try:
            return await writer(*args, **kwargs)
        finally:
            spent["writer"] = max(spent.get("writer", 0.0), time.perf_counter() - started)

    def timed_builder(*args, **kwargs):
        started = time.perf_counter()
        try:
            return builder(*args, **kwargs)
        finally:
            spent["bundle"] = time.perf_counter() - started

    monkeypatch.setattr(evidence_api, "withheld_metrics", timed_writer)
    monkeypatch.setattr(bundle_api, "build_bundle", timed_builder)

    def metrics(card: str, other: str) -> list[dict]:
        return [total(f"(nodes.{card}.metrics[{i}].value if nodes.{card} else 0) "
                      f"+ (nodes.{other}.metrics[{i}].value if nodes.{other} else 0) + cell(nodes.q, 0, 'amount')",
                      f"{card}_t{i}", f"{card} 累计{i}") for i in range(8)]

    seen = script(monkeypatch, "累计 [[m:acc_t0]]，对照 [[m:peer_t7]]。")
    row = await finish(loop_graph(10, [{"id": "acc", "metrics": metrics("acc", "peer")},
                                       {"id": "peer", "metrics": metrics("peer", "acc")}]))
    assert row.status == "succeeded", row.error
    assert "已遮罩" not in metric_lines(seen[0]), metric_lines(seen[0])
    _, bundle = await export(client, row.id)
    [art] = [a for a in bundle["artifacts"] if a["kind"] == "report_doc"]
    assert "b64" in art and "masked" not in art, art
    assert spent["writer"] < 1.0 and spent["bundle"] < 1.0, spent
