"""转抄上游口径卡的保守规则：上游那张卡在这次运行的任何一次执行里有一个指标取自遮罩格，转抄出来的指标就不给值。

复核第 3 轮（91aa34d）找到两处：
- 循环里卡转抄自己上一轮（nodes.acc.metrics[0].value if nodes.acc else cell(nodes.q, 0, 'v')），SQL 带模板，第一轮取号码、
  第二轮取姓名。写作目录（withheld_metrics）只有运行状态里最后一次的产出，自引用在依赖图上成环，环上最后一轮没有直接取自
  遮罩格的输入，照给：完整号码进了报告正文和成果，自查通过、正式运行成功；封存之后按每次执行往回认才写「已遮罩」。
- 整张卡、清单文字、下标不是字面量的转抄（nodes.card.text、nodes.card.metrics[vars.idx].value）只按值认：上游由遮罩格算出来
  的指标值（年薪 = 月薪 × 12）不会原样出现在查询结果里，写作目录、报告、成果、证据面板、可验证报告都带出来。

现在不再按「转抄的是哪一个指标、读到的是哪一次产出」细分：卡上的输入转抄上游口径卡（字面量下标、下标不是字面量、整张卡、
清单文字、整份指标列表都算），只要那张卡在这次运行的任何一次执行里（按台账里每一次执行的指标集认，不只看最后一次）有一个
指标不给，转抄出来的这个输入就不给（recompute.CopyEdge.card）。上游卡一个不给的指标都没有时照常：没设遮罩的工作流、循环
里累加自己上一轮的卡照给。写作目录、报告自查、证据面板、推演、可验证报告同一个判断（recompute.masked_closure）。

真跑的部分用虚构的 SQLite 库（号码、月薪都是编的），写作者是 MockChatModel 照剧本写。
"""
from __future__ import annotations

import base64
import json
import sqlite3

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.data.engine import engines
from app.db.base import SessionLocal
from app.db.models import DataSource
from app.engine.evidence import iter_segments
from app.engine.runner import run_manager
from app.main import app
from tests.test_bundle_masked_metrics import data_of, event
from tests.test_report_masked_metrics import (  # noqa: F401  vip：号码设了遮罩的会员库
    doc_of,
    events,
    finish,
    leaks,
    metric_lines,
    node,
    script,
    vip,
)

MASKED_LINE = "= 已遮罩（取自设置了遮罩的列，不要引用）"


@pytest.fixture
async def engine_up():
    await run_manager.setup()
    yield
    await run_manager.shutdown()


@pytest.fixture
async def client(engine_up):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


#: 虚构的员工表：月薪设了遮罩
STAFF = [(1, "员工甲", 8123.25), (2, "员工乙", 9234.5)]
#: 首位员工的年薪（月薪 × 12）：由遮罩格算出来的数，按值认不出
ANNUAL = STAFF[0][2] * 12


def annual_leaks(value) -> list[str]:
    """写着首位员工年薪的地方（原样、两位小数、千分位都算）。"""
    raw = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    flat = raw.replace(",", "")
    return [n for n in (f"{ANNUAL:.1f}", f"{ANNUAL:.2f}", str(int(ANNUAL))) if n in flat]


@pytest.fixture
async def hr(tmp_path, engine_up):
    path = tmp_path / "hr.db"
    db = sqlite3.connect(path)
    db.executescript("CREATE TABLE staff (id INTEGER PRIMARY KEY, name TEXT, salary REAL);")
    db.executemany("INSERT INTO staff VALUES (?, ?, ?)", STAFF)
    db.commit()
    db.close()
    async with SessionLocal() as session:
        row = (await session.execute(select(DataSource).where(DataSource.name == "hr"))).scalar_one_or_none()
        if row is None:
            row = DataSource(name="hr", kind="sqlite", database=str(path), readonly=True, enabled=True)
            session.add(row)
        row.database, row.kind, row.enabled, row.readonly = str(path), "sqlite", True, True
        row.options = {"mask_columns": ["salary"]}
        await session.commit()
        source_id = row.id
    await engines.invalidate(source_id)
    yield path
    await engines.invalidate(source_id)


def chain(*nodes: dict) -> dict:
    return {"nodes": list(nodes), "edges": [{"source": a["id"], "target": b["id"]} for a, b in zip(nodes, nodes[1:])]}


async def export(client, run_id: str) -> tuple[str, dict]:
    resp = await client.get(f"/api/runs/{run_id}/bundle.html")
    assert resp.status_code == 200, resp.text
    return resp.text, data_of(resp.text)


async def bundle_finds(client, run_id: str, find) -> dict:
    """导出一份可验证报告：整页、每份附带的工件、带规范串的事件里 find 找得到的（事件按 序号:类型:节点 记）。"""
    html, bundle = await export(client, run_id)
    out: dict = {"html": find(html), "artifacts": {}, "events": {}}
    for art in bundle["artifacts"]:
        if "b64" in art and (found := find(base64.b64decode(art["b64"]).decode("utf-8", "replace"))):
            out["artifacts"][f"{art['kind']}:{art.get('node_id')}"] = found
    for item in bundle["events"]:
        if "canonical" in item and (found := find(item["canonical"])):
            out["events"][f"{item['seq']}:{item['type']}:{item['node_id']}"] = found
    return out


CLEAN = {"html": [], "artifacts": {}, "events": {}}


# --------------------------------------------------------------------------
# 最小不动点上的边规则（recompute.masked_closure，纯函数）
# --------------------------------------------------------------------------


def _metric(expression: str, value=1.0) -> dict:
    return {"id": "m", "name": "m", "value": value, "expression": expression}


def test_any_withheld_metric_on_the_copied_card_withholds_every_form_of_copy():
    """卡 x 跑了两次：第一次的指标取自号码列，第二次的照给。卡 y 转抄卡 x 第二次的产出，字面量下标、整张卡、清单文字、整份
    指标列表四种写法，都不给，转抄的输入上写着让卡 x 不给的那一列。卡 z 转抄的卡 w 一个不给的指标都没有，照给；没说是哪张卡
    的边（card 为空）照旧只看它读到的那几个指标。"""
    from app.engine.recompute import CopyEdge, MetricLinks, masked_closure

    x1, x2, w1 = ("x1", 0), ("x2", 0), ("w1", 0)
    links = {
        x1: MetricLinks(_metric("cell(nodes.q, 0, 'v')"), direct={"cell(nodes.q, 0, 'v')": ("masked", ("v",))},
                        card="x"),
        x2: MetricLinks(_metric("cell(nodes.q, 0, 'v')"), card="x"),
        w1: MetricLinks(_metric("cell(nodes.q, 0, 'v')"), card="w"),
    }
    forms = {
        "nodes.x.metrics[0].value": False, "nodes.x": True, "nodes.x.text": True, "nodes.x.metrics": True,
    }
    for i, (path, whole) in enumerate(forms.items()):
        links[("y", i)] = MetricLinks(_metric(path), copies=[CopyEdge(path, (x2,), whole, card="x")], card="y")
    links[("z", 0)] = MetricLinks(_metric("nodes.w.text"), copies=[CopyEdge("nodes.w.text", (w1,), True, card="w")],
                                  card="z")
    links[("v", 0)] = MetricLinks(_metric("nodes.x.metrics[0].value"),
                                  copies=[CopyEdge("nodes.x.metrics[0].value", (x2,), False)], card="v")
    got = masked_closure(links, secrets=lambda: None)
    assert (got[x1].word, got[x2].word, got[w1].word) == ("masked", None, None)
    for i, path in enumerate(forms):
        assert got[("y", i)].word == "masked", path
        assert got[("y", i)].copied[path] == ("masked", ("v",), None), path
        assert got[("y", i)].card == "y"
    assert got[("z", 0)].word is None and got[("z", 0)].copied["nodes.w.text"] == (None, (), None)
    assert got[("v", 0)].word is None


def test_a_card_copying_its_own_rounds_is_withheld_only_when_some_round_is():
    """卡 acc 跑三轮，每一轮转抄自己上一轮（字面量下标）、整张自己（护栏）。没有哪一轮取自遮罩格：照给（第 3 轮修好的那条）；
    只有第一轮取自号码列：三轮都不给——第三轮读到的第二轮本身也只是转抄来的，按整张卡认，不看读到的是哪一次。"""
    from app.engine.recompute import CopyEdge, MetricLinks, masked_closure

    def rounds(first_masked: bool) -> dict:
        out = {}
        for r in range(3):
            direct = {"cell": ("masked", ("v",))} if first_masked and r == 0 else {}
            prev = ((r - 1, 0),) if r else ()
            out[(r, 0)] = MetricLinks(_metric("nodes.acc.metrics[0].value if nodes.acc else cell"), direct=direct,
                                      copies=[CopyEdge("nodes.acc.metrics[0].value", prev, False, card="acc"),
                                              CopyEdge("nodes.acc", prev, True, card="acc")], card="acc")
        return out

    assert {k: v.word for k, v in masked_closure(rounds(False), secrets=lambda: None).items()} == \
        {(0, 0): None, (1, 0): None, (2, 0): None}
    assert {k: v.word for k, v in masked_closure(rounds(True), secrets=lambda: None).items()} == \
        {(0, 0): "masked", (1, 0): "masked", (2, 0): "masked"}


def test_metrics_judged_earlier_count_for_their_card():
    """之前认好的指标（known，结果不再变）照样算进它所在的卡：卡 x 有一个之前认好的不给的指标，这一次只认转抄卡 x 的 y，也不给；
    之前认好的是照给的，y 照给。"""
    from app.engine.recompute import CopyEdge, Judged, MetricLinks, masked_closure

    y = ("y", 0)
    links = {y: MetricLinks(_metric("nodes.x.metrics[1].value"),
                            copies=[CopyEdge("nodes.x.metrics[1].value", (("x", 1),), False, card="x")], card="y")}
    known = {("x", 0): Judged("masked", {}, ("phone",), card="x"), ("x", 1): Judged(None, {}, (), card="x")}
    got = masked_closure(links, known, secrets=lambda: None)
    assert got[y].word == "masked" and got[y].copied["nodes.x.metrics[1].value"] == ("masked", ("phone",), None)
    known[("x", 0)] = Judged(None, {}, (), card="x")
    assert masked_closure(links, known, secrets=lambda: None)[y].word is None


def test_an_unreadable_copy_of_a_card_with_a_withheld_metric_is_withheld():
    """读到的那次产出读不出来（CopyEdge.unreadable），可卡 x 别的执行里有不给的指标：按整张卡认，不给（不只是核对不了）。"""
    from app.engine.recompute import CopyEdge, MetricLinks, masked_closure

    why = "转抄的口径卡结果无法读取"
    links = {
        ("x", 0): MetricLinks(_metric("cell"), direct={"cell": ("masked", ("phone",))}, card="x"),
        ("y", 0): MetricLinks(_metric("nodes.x.metrics[0].value"),
                              copies=[CopyEdge("nodes.x.metrics[0].value", (), False, why, card="x")], card="y"),
        ("y", 1): MetricLinks(_metric("nodes.w.metrics[0].value"),
                              copies=[CopyEdge("nodes.w.metrics[0].value", (), False, why, card="w")], card="y"),
    }
    got = masked_closure(links, secrets=lambda: None)
    assert got[("y", 0)].word == "masked", got[("y", 0)]
    assert got[("y", 1)].word == "unverified" and got[("y", 1)].copied["nodes.w.metrics[0].value"][2] == why


# --------------------------------------------------------------------------
# 写作目录：循环里转抄自己上一轮，更早一轮取过号码列
# --------------------------------------------------------------------------

#: 第一轮取号码（phone AS v），之后转抄上一轮。护栏写法（nodes.acc 记成整张卡的输入）和只按字面量下标转抄的写法
CARRY = [
    "nodes.acc.metrics[0].value if nodes.acc else cell(nodes.q, 0, 'v')",
    "nodes.acc.metrics[0].value if vars.item == 'name' else cell(nodes.q, 0, 'v')",
]


def carry_loop(expression: str, items: str = '["phone", "name"]', *, after: list[dict] | None = None,
               report: bool = True, **write) -> dict:
    """入口 → 循环（foreach items：模板查询 SELECT {{ vars.item }} AS v → 卡 acc）→ 循环之后的卡（after）→ 报告 → 成果。"""
    start = node("start", "input")
    lp = node("lp", "loop", mode="foreach", items=items)
    q = node("q", "tool", tool="db_query__vip", args={"sql": "SELECT {{ vars.item }} AS v FROM customers ORDER BY id"})
    acc = node("acc", "metrics", caliber="取值口径", caliber_version="v1", metrics=[
        {"id": "first", "name": "首位客户取值", "format": "plain", "expression": expression}])
    tail = [node(c["id"], "metrics", caliber=f"{c['id']} 口径", caliber_version="v1", metrics=c["metrics"])
            for c in after or []]
    if report:
        tail.append(node("write", "report", instructions="写一句", **{"on_violation": "fail", **write}))
    out = node("out", "output", fields=[{"name": "报告", "value": "{{ nodes.write.text }}"}] if report
               else [{"name": "说明", "value": "完成"}])
    edges = [{"source": "start", "target": "lp"}, {"source": "lp", "target": "q", "sourceHandle": "body"},
             {"source": "q", "target": "acc"}, {"source": "acc", "target": "lp"}]
    rest = ["lp", *(n["id"] for n in tail), "out"]
    edges += [{"source": a, "target": b, **({"sourceHandle": "done"} if a == "lp" else {})}
              for a, b in zip(rest, rest[1:])]
    return {"nodes": [start, lp, q, acc, *tail, out], "edges": edges}


@pytest.mark.parametrize("expression", CARRY)
async def test_a_loop_card_carrying_an_earlier_masked_round_is_withheld_when_writing(monkeypatch, vip, expression):
    """复核者的探针：第一轮取号码、第二轮转抄第一轮。写作目录写「已遮罩」，引用它的正式运行判失败（masked_cell），整段写作
    目录、报错里都没有号码。以前写作目录只看最后一次产出，写着号码，正文、成果都是号码，自查通过、运行成功。"""
    seen = script(monkeypatch, "首位客户取值 [[m:first]]。")
    row = await finish(carry_loop(expression))
    lines = metric_lines(seen[0])
    assert f"首位客户取值 {MASKED_LINE}" in lines and leaks(lines) == [], lines
    assert row.status == "failed" and "m:first" in (row.error or "") and "遮罩" in row.error, row.error
    assert leaks(row.error) == []
    [checked] = [e.data for e in await events(row.id) if e.type == "report.checked"][-1:]
    assert [v["ref"] for v in checked["violations"] if v["code"] == "masked_cell"] == ["m:first"], checked


async def test_a_flagged_report_carrying_an_earlier_masked_round_leaks_nowhere(client, monkeypatch, vip):
    """违规处置为标注：报告照常产出，可正文、文档、成果里都没有号码（写的是「已遮罩」）；证据面板的标签写「已遮罩」；可验证报告
    里没有号码，acc 每一轮的节点完成事件都只给哈希。"""
    script(monkeypatch, "首位客户取值 [[m:first]]。")
    row = await finish(carry_loop(CARRY[0], on_violation="flag"))
    assert row.status == "succeeded", row.error
    doc = await doc_of(row.id)
    assert doc["catalog"]["m:first"]["masked"] is True and leaks(doc) == [], doc["catalog"]["m:first"]
    assert leaks(row.output or {}) == [], row.output
    body = (await client.get(f"/api/runs/{row.id}/evidence")).json()
    labels = {e["alias"]: e["label"] for e in body["evidence"] if e["kind"] == "metric"}
    assert labels["m:first"] == "首位客户取值 = 已遮罩", labels
    assert await bundle_finds(client, row.id, leaks) == CLEAN
    _, bundle = await export(client, row.id)
    finished = [e for e in bundle["events"] if e["type"] == "node.finished" and e["node_id"] == "acc"]
    assert len(finished) == 2 and not any("canonical" in e for e in finished), finished


async def test_a_loop_card_without_any_masked_round_is_shown(monkeypatch, vip):
    """对照：两轮都只取姓名、编号（没碰号码列），转抄上一轮的照给，正式运行成功。"""
    seen = script(monkeypatch, "首位客户取值 [[m:first]]。")
    row = await finish(carry_loop(CARRY[0], items='["name", "id"]'))
    assert row.status == "succeeded", row.error
    assert "首位客户取值 = 客户甲" in metric_lines(seen[0]), metric_lines(seen[0])


# --------------------------------------------------------------------------
# 循环之后的卡转抄循环里的卡：最后一轮没碰号码列，更早一轮碰过
# --------------------------------------------------------------------------

#: 循环之后的卡 pick 转抄 acc 最后一轮的产出（acc 每一轮只取当轮的 v，不转抄自己）
PICK = [{"id": "pick", "metrics": [{"id": "picked", "name": "转抄取值", "format": "plain",
                                    "expression": "nodes.acc.metrics[0].value"}]}]


async def test_a_card_copying_the_last_round_of_a_card_that_once_took_a_masked_cell_is_withheld_everywhere(
        client, monkeypatch, vip):
    """acc 第一轮取号码、第二轮取姓名；循环之后的卡 pick 转抄 acc 最后一轮（姓名）。按整张卡、每一次执行认：acc 有一次执行的
    指标取自号码列，pick 的指标就不给——写作目录写「已遮罩」、证据面板的标签写「已遮罩」、推演不给值也不让改、可验证报告里
    pick 只给 id。acc 最后一轮自己的指标（姓名）照给。四处是同一个判断。"""
    seen = script(monkeypatch, "首位客户 [[m:first]]。")
    row = await finish(carry_loop("cell(nodes.q, 0, 'v')", after=PICK, on_violation="flag"))
    assert row.status == "succeeded", row.error
    lines = metric_lines(seen[0])
    assert f"转抄取值 {MASKED_LINE}" in lines and "首位客户取值 = 客户甲" in lines, lines
    body = (await client.get(f"/api/runs/{row.id}/evidence")).json()
    labels = {e["alias"]: e["label"] for e in body["evidence"] if e["kind"] == "metric"}
    assert labels["m:picked"] == "转抄取值 = 已遮罩" and labels["m:first"] == "首位客户取值 = 客户甲", labels
    res = await client.post(f"/api/runs/{row.id}/evidence/whatif", json={"card": "pick"})
    assert res.status_code == 200, res.text
    [picked] = res.json()["metrics"]
    assert picked["withheld"] is True and picked["old"] == "已遮罩", picked
    assert picked["inputs"][0]["editable"] is False and picked["inputs"][0]["old"] is None, picked["inputs"]
    _, bundle = await export(client, row.id)
    assert "canonical" not in event(bundle, "node.finished", "pick")
    assert await bundle_finds(client, row.id, leaks) == CLEAN

    # 对照：两轮都没碰号码列，pick 照给、照常推演，节点完成事件带规范串
    seen = script(monkeypatch, "首位客户 [[m:first]]。")
    row = await finish(carry_loop("cell(nodes.q, 0, 'v')", items='["id", "name"]', after=PICK, on_violation="flag"))
    assert row.status == "succeeded", row.error
    assert "转抄取值 = 客户甲" in metric_lines(seen[0]), metric_lines(seen[0])
    res = await client.post(f"/api/runs/{row.id}/evidence/whatif", json={"card": "pick"})
    [picked] = res.json()["metrics"]
    assert picked.get("withheld") is not True and picked["old"] == "客户甲", picked
    _, bundle = await export(client, row.id)
    assert "canonical" in event(bundle, "node.finished", "pick")


async def test_the_bundle_alone_withholds_a_copy_of_a_card_that_once_took_a_masked_cell(client, monkeypatch, vip):
    """可验证报告自己的判断（没有报告文档替它说「已遮罩」）：同上的 acc、pick。没有报告节点的运行，pick 的节点完成事件只给
    哈希，acc 第二轮（取姓名）的照常带规范串；写作前不认遮罩的老文档（这里把那一步换成什么都不认），证据目录里写着 pick 的
    原值，文档整份只给哈希。两轮都没碰号码列的照常附带。"""
    import app.api.evidence as evidence_api

    row = await finish(carry_loop("cell(nodes.q, 0, 'v')", after=PICK, report=False))
    assert row.status == "succeeded", row.error
    _, bundle = await export(client, row.id)
    assert "canonical" not in event(bundle, "node.finished", "pick")
    acc = [e for e in bundle["events"] if e["type"] == "node.finished" and e["node_id"] == "acc"]
    assert ["canonical" in e for e in acc] == [False, True], acc

    async def nothing(*_args, **_kwargs):
        return []

    monkeypatch.setattr(evidence_api, "withheld_metrics", nothing)
    for items, withheld in (('["phone", "name"]', True), ('["id", "name"]', False)):
        script(monkeypatch, "转抄取值 [[m:picked]]。")
        row = await finish(carry_loop("cell(nodes.q, 0, 'v')", items=items, after=PICK, on_violation="flag"))
        assert row.status == "succeeded", row.error
        assert "masked" not in (await doc_of(row.id))["catalog"]["m:picked"], "老文档的证据目录里写着原值"
        _, bundle = await export(client, row.id)
        [art] = [a for a in bundle["artifacts"] if a["kind"] == "report_doc"]
        assert (art.get("masked") is True and "b64" not in art) if withheld else ("b64" in art and "masked" not in art), \
            (items, art)


def held_graph(*, report: bool = True) -> dict:
    """会员查询 → 卡 1（号码、客户编号）→ 数据整形把整张卡赋给变量 k2 → 卡 2 按变量转抄卡 1 的客户编号 → 报告 → 成果。"""
    tail = [node("write", "report", instructions="写一句", on_violation="flag"),
            node("out", "output", fields=[{"name": "报告", "value": "{{ nodes.write.text }}"}])] if report \
        else [node("out", "output", fields=[{"name": "说明", "value": "完成"}])]
    return chain(
        node("start", "input"),
        node("q_cust", "tool", tool="db_query__vip", args={"sql": "SELECT id, name, phone FROM customers ORDER BY id"}),
        node("card", "metrics", caliber="会员口径", caliber_version="v1", metrics=[
            {"id": "phone_raw", "name": "首位客户手机号", "format": "plain", "expression": "cell(nodes.q_cust, 0, 'phone')"},
            {"id": "first_id", "name": "首位客户编号", "format": "plain", "expression": "cell(nodes.q_cust, 0, 'id')"}]),
        node("hold", "transform", expression="nodes.card", assign_to="k2"),
        node("card2", "metrics", caliber="转抄口径", caliber_version="v1", metrics=[
            {"id": "id2", "name": "转抄编号", "format": "plain", "expression": "vars.k2.metrics[1].value"}]),
        *tail,
    )


async def test_a_copy_through_a_variable_set_by_another_node_is_judged_on_the_card_itself(client, monkeypatch, vip):
    """数据整形把整张卡赋给变量（nodes.card → vars.k2），卡 2 按变量转抄卡 1 的客户编号（没碰号码列）：按卡 1 本身认（读到的
    产出上记着的是卡 1 的指标集），卡 1 上有取自号码列的指标，写作目录、证据面板都写「已遮罩」；没有报告的运行里，可验证报告
    同样只给卡 2 的哈希。以前按写变量的节点（数据整形）认，那个节点没有指标集，整张卡的规则落了空。"""
    seen = script(monkeypatch, "转抄编号 [[m:id2]]。")
    row = await finish(held_graph())
    assert row.status == "succeeded", row.error
    assert f"转抄编号 {MASKED_LINE}" in metric_lines(seen[0]), metric_lines(seen[0])
    body = (await client.get(f"/api/runs/{row.id}/evidence")).json()
    labels = {e["alias"]: e["label"] for e in body["evidence"] if e["kind"] == "metric"}
    assert labels["m:id2"] == "转抄编号 = 已遮罩", labels
    row = await finish(held_graph(report=False))
    assert row.status == "succeeded", row.error
    _, bundle = await export(client, row.id)
    assert "canonical" not in event(bundle, "node.finished", "card2")


# --------------------------------------------------------------------------
# 整张卡、清单文字、下标不是字面量的转抄：上游的指标由遮罩格算出来
# --------------------------------------------------------------------------

#: 卡 2 转抄卡 1（首位年薪 = 月薪 × 12，值按值认不出）的几种写法
WHOLE_COPIES = [
    "nodes.card.metrics[vars.idx].value",
    "nodes.card.text",
    "len(nodes.card.text)",
    "len(nodes.card.metrics)",
    "nodes.card.metrics[0].value",
]


def annual_graph(expression: str, **write) -> dict:
    """员工查询（选出月薪）→ 下标变量 idx = 0 → 卡 1（首位年薪）→ 卡 2（转抄卡 1）→ 报告 → 成果。"""
    return chain(
        node("start", "input"),
        node("idx", "transform", expression="0", assign_to="idx"),
        node("q", "tool", tool="db_query__hr", args={"sql": "SELECT id, name, salary FROM staff ORDER BY id"}),
        node("card", "metrics", caliber="薪酬口径", caliber_version="v1", metrics=[
            {"id": "annual", "name": "首位年薪", "unit": "元", "decimals": 2, "format": "thousands",
             "expression": "cell(nodes.q, 0, 'salary') * 12"}]),
        node("card2", "metrics", caliber="转抄口径", caliber_version="v1", metrics=[
            {"id": "c2", "name": "抄录", "format": "plain", "expression": expression}]),
        node("write", "report", instructions="写一句", **{"on_violation": "fail", **write}),
        node("out", "output", fields=[{"name": "报告", "value": "{{ nodes.write.text }}"}]),
    )


@pytest.mark.parametrize("expression", WHOLE_COPIES)
async def test_every_form_of_copying_a_derived_masked_metric_is_withheld_when_writing(monkeypatch, hr, expression):
    """复核者的探针：卡 1 的年薪由月薪（设了遮罩）算出，卡 2 用五种写法转抄卡 1。写作目录都写「已遮罩」、整段写作目录里没有
    年薪，引用它的正式运行判失败。以前下标不是字面量的、清单文字照给，写作目录、正文、成果都是年薪；数字数、数指标个数的
    也照给（值是个数，可卡 2 上记着的清单文字里写着年薪）。"""
    seen = script(monkeypatch, "抄录 [[m:c2]]。")
    row = await finish(annual_graph(expression))
    lines = metric_lines(seen[0])
    assert f"抄录 {MASKED_LINE}" in lines and annual_leaks(lines) == [], lines
    assert row.status == "failed" and "m:c2" in (row.error or "") and "遮罩" in row.error, row.error
    assert annual_leaks(row.error) == []


@pytest.mark.parametrize("expression", WHOLE_COPIES[:3])
async def test_a_flagged_report_copying_a_derived_masked_metric_leaks_nowhere(client, monkeypatch, hr, expression):
    """违规处置为标注：报告照常产出，文档、成果里没有年薪；证据面板的标签写「已遮罩」；可验证报告里卡 2 只给 id、它的节点完成
    事件只给哈希，整个文件里没有年薪。以前卡 2 的指标集、报告文档、两个节点完成事件都带着年薪。"""
    script(monkeypatch, "抄录 [[m:c2]]。")
    row = await finish(annual_graph(expression, on_violation="flag"))
    assert row.status == "succeeded", row.error
    doc = await doc_of(row.id)
    assert doc["catalog"]["m:c2"]["masked"] is True and annual_leaks(doc) == [], doc["catalog"]["m:c2"]
    assert annual_leaks(row.output or {}) == [], row.output
    body = (await client.get(f"/api/runs/{row.id}/evidence")).json()
    labels = {e["alias"]: e["label"] for e in body["evidence"] if e["kind"] == "metric"}
    assert labels["m:c2"] == "抄录 = 已遮罩", labels
    seg = next(s for s in iter_segments(doc) if s.get("ref") == "m:c2")
    chain_ = (await client.get(f"/api/runs/{row.id}/evidence/segments/{seg['id']}")).json()["chain"]
    assert chain_[0]["value"] == "已遮罩" and annual_leaks(chain_) == [], chain_
    _, bundle = await export(client, row.id)
    cards = {a["node_id"]: a for a in bundle["artifacts"] if a["kind"] == "metric_set"}
    assert "card2" not in cards or (cards["card2"].get("masked") is True and "b64" not in cards["card2"]), cards
    assert "canonical" not in event(bundle, "node.finished", "card2")
    assert await bundle_finds(client, row.id, annual_leaks) == CLEAN


async def test_copying_a_card_whose_metrics_are_all_shown_is_unchanged(client, monkeypatch, hr):
    """对照：卡 1 只有首位员工的编号（不碰月薪列），卡 2 数它的指标个数，照给、正式运行成功，可验证报告里卡 2 的节点完成事件
    带规范串。"""
    seen = script(monkeypatch, "抄录 [[m:c2]]。")
    spec = annual_graph("len(nodes.card.metrics)")
    spec["nodes"][3]["data"]["config"]["metrics"] = [
        {"id": "first_id", "name": "首位员工编号", "format": "plain", "expression": "cell(nodes.q, 0, 'id')"}]
    row = await finish(spec)
    assert row.status == "succeeded", row.error
    assert "抄录 = 1" in metric_lines(seen[0]), metric_lines(seen[0])
    _, bundle = await export(client, row.id)
    assert "canonical" in event(bundle, "node.finished", "card2")
