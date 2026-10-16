"""组合式问数：use_caliber 选用已发布口径，服务端按那一版确定性内联实例化（next-round 02 做法 4，ledger/compose.py）。

- 实例化是确定的：同一版、同一个操作得到一模一样的节点；取数节点照抄、id 加前缀、模板按参数渲染成字面 SQL，口径卡 id
  就是操作 id、caliber_from 带改名映射；取数首尾相接再接口径卡，入口连到第一个取数节点；
- 参数按口径入口的字段校验（未知字段、缺必填、类型、引号里的值带引号、裸写的值不是数），不合规拒收、原因交回模型；
  只能选这一轮查看到的那一版、能组合的卡、卡上的指标，id 不能撞；
- 问数据的查看对象多了口径：给入口字段和「能不能组合」，不给 SQL；「什么时候该看」换成问数据那一份；使用者不给；
- 走一遍问数据的生成（剧本模型）再真跑出来：比对器把这些数标成 composed「助手选用」，整题 how=composed。
数据是虚构店铺库。
"""
from __future__ import annotations

import json

import pytest
from httpx import ASGITransport, AsyncClient

from app.api import copilot_context
from app.api.copilot_gen import GraphApplier, GraphChecker
from app.db.base import SessionLocal
from app.engine.runner import run_manager
from app.engine.schema import GraphSpec, validate_graph
from app.ledger import compose
from app.ledger import match as ledger_match
from app.ledger import store
from app.look.calibers import CaliberKind
from app.main import app
from tests import ledger_samples as S


@pytest.fixture(autouse=True)
async def engine_up(tmp_path):
    await run_manager.setup()
    await S.fresh_source(tmp_path / "shop.db")
    yield
    await ledger_match.drain()
    await run_manager.shutdown()


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


def use(wf: str, **extra) -> dict:
    return {"op": "use_caliber", "id": "c1", "workflow_id": wf, "version": 1, "metrics": ["gmv"],
            "inputs": {"start": "2026-09-01", "end": "2026-09-08"}, **extra}


async def composer() -> compose.Composer:
    async with SessionLocal() as session:
        kind = await CaliberKind.load(session, compose=True)
    return compose.Composer(list(kind.options))


# --------------------------------------------------------------------------
# 实例化
# --------------------------------------------------------------------------


async def test_instantiation_is_deterministic_and_copies_the_pinned_fetch():
    wf = await S.publish()
    made = (await composer()).compose(use(wf), taken={"start"})
    again = (await composer()).compose(use(wf), taken={"start"})
    assert made.nodes == again.nodes and made.edges == again.edges
    fetch, card = made.nodes
    assert fetch["id"] == "c1_fetch" and fetch["type"] == "tool"
    assert fetch["data"]["config"]["tool"] == S.tool()
    assert fetch["data"]["config"]["args"]["sql"] == S.TEMPLATE.replace("{{ input.start }}", "2026-09-01").replace(
        "{{ input.end }}", "2026-09-08")
    assert card["id"] == "c1" and card["type"] == "metrics" and card["data"]["label"] == "card"
    assert card["data"]["config"]["caliber_from"] == {"workflow_id": wf, "workflow_version": 1, "node_id": "card",
                                                     "nodes": {"fetch": "c1_fetch"}}
    assert made.edges == [{"source": "c1_fetch", "target": "c1"}]
    # 缺省值补上了（store 有缺省 all），不进结果
    assert made.inputs == {"start": "2026-09-01", "end": "2026-09-08", "store": "all"}
    result = made.result()
    assert result["status"] == "ok" and result["card_id"] == "c1" and result["fetches"] == ["c1_fetch"]
    assert result["level_text"] == "受管" and result["metrics"] == [{"id": "gmv", "name": "销售额"}]
    assert "sql" not in json.dumps(result).lower()


@pytest.mark.parametrize(("change", "words"), [
    ({"workflow_id": "nope"}, "只能选用这一轮查看到的已发布口径"),
    ({"version": 2}, "能选用的是 v1"),
    ({"card": "other"}, "没有口径卡「other」"),
    ({"id": "C 1"}, "简短的英文小写"),
    ({"metrics": []}, "metrics 要写"),
    ({"metrics": ["profit"]}, "没有指标「profit」"),
    ({"inputs": {"start": "2026-09-01", "end": "2026-09-08", "city": "x"}}, "入口没有字段「city」"),
    ({"inputs": {"start": "2026-09-01"}}, "请填写「end」"),
    ({"inputs": {"start": "2026-09-01' OR '1'='1", "end": "2026-09-08"}}, "引号里，值里不能有引号"),
    ({"inputs": "start=2026-09-01"}, "inputs 要写成"),
])
async def test_bad_choices_are_refused_with_a_reason(change, words):
    wf = await S.publish()
    op = use(wf, **change) if change.get("workflow_id") != "nope" else {**use(wf), **change}
    with pytest.raises(compose.ComposeRefused) as e:
        (await composer()).compose(op, taken={"start"})
    assert words in e.value.message, e.value.message


async def test_bare_slots_take_numbers_only_and_ids_must_not_clash():
    graph = S.caliber_graph(sql="SELECT SUM(amount) AS gmv FROM orders WHERE status = 'paid' AND id > {{ input.after }}")
    # 字段没写类型（按文字收）：表单校验放行，裸写进 SQL 的只能是数
    graph["nodes"][0]["data"]["config"]["fields"] = [{"name": "after", "default": "0"}]
    wf = await S.publish(graph=graph)
    pool = await composer()
    with pytest.raises(compose.ComposeRefused, match="只能是数"):
        pool.compose({**use(wf), "inputs": {"after": "1 OR 1=1"}}, taken=set())
    made = pool.compose({**use(wf), "inputs": {"after": "3"}}, taken=set())
    assert made.nodes[0]["data"]["config"]["args"]["sql"].endswith("id > 3")
    with pytest.raises(compose.ComposeRefused, match="撞了"):
        pool.compose({**use(wf), "inputs": {"after": "3"}}, taken={"c1"})


async def test_a_card_that_reads_model_output_is_listed_but_not_composable():
    graph = S.chain(S.node("start", "input"), S.node("ask", "agent", tools=[S.tool()], assign_to="kpi"),
                    S.node("card", "metrics", caliber="周销售", metrics=[{"id": "gmv", "expression": "vars.kpi.gmv"}]),
                    S.node("out", "output", fields=[{"name": "r", "value": "{{ nodes.card.text }}"}]))
    wf = await S.publish(graph=graph)
    async with SessionLocal() as session:
        kind = await CaliberKind.load(session, compose=True)
    [option] = [o for o in kind.options if o.workflow_id == wf]
    assert not option.composable and "不能复用" in option.reason
    text, _ = await kind.detail([(i, "named") for i in kind.items if i.workflow_id == wf], 4000)
    assert "不能组合" in text
    with pytest.raises(compose.ComposeRefused, match="不能组合"):
        compose.Composer(list(kind.options)).compose(use(wf), taken=set())


# --------------------------------------------------------------------------
# 查看：入口字段、能不能组合，不给 SQL
# --------------------------------------------------------------------------


async def test_the_answer_look_shows_fields_and_how_to_compose_but_no_sql():
    wf = await S.publish()
    async with SessionLocal() as session:
        kind = await CaliberKind.load(session, compose=True)
        plain = await CaliberKind.load(session)
    objects = [(i, "named") for i in kind.items if i.workflow_id == wf]
    text, [record] = await kind.detail(objects, 4000)
    assert "入口字段：start（string，必填）；end（string，必填）；store（string，缺省 all）" in text
    assert '"op": "use_caliber"' in text and f'"workflow_id": "{wf}"' in text and '"card": "card"' in text
    assert "SELECT" not in text.upper() and "orders" not in text
    assert record["kind"] == "caliber" and record["named"]
    # 蓝图助手那一支（不组合）照旧给复用写法，没有组合写法
    plain_text, _ = await plain.detail([(i, "named") for i in plain.items if i.workflow_id == wf], 4000)
    assert "复用写法" in plain_text and "use_caliber" not in plain_text


async def test_the_answer_prompt_switches_only_when_calibers_can_be_composed():
    from app.look.service import LookService

    assert copilot_context.LOOK_WHEN_ANSWER_PROMPT != copilot_context.LOOK_WHEN_PROMPT
    assert "use_caliber" in copilot_context.LOOK_WHEN_ANSWER_PROMPT and "含退款" in copilot_context.LOOK_WHEN_ANSWER_PROMPT
    async with SessionLocal() as session:
        composing = LookService([await CaliberKind.load(session, compose=True)])
        plain = LookService([await CaliberKind.load(session)])
    assert copilot_context.look_when(composing) == copilot_context.LOOK_WHEN_ANSWER_PROMPT
    assert copilot_context.look_when(plain) == copilot_context.LOOK_WHEN_PROMPT
    assert copilot_context.composer(plain) is None and copilot_context.composer(composing) is not None


# --------------------------------------------------------------------------
# 落地：GraphApplier
# --------------------------------------------------------------------------


async def test_the_applier_lands_the_nodes_wires_the_entry_and_reports_refusals():
    wf = await S.publish()
    nodes: dict = {}
    edges: list = []
    applier = GraphApplier(nodes, edges, composer=await composer())
    op = use(wf)
    applied = applier.apply(op)
    assert applied.changed and applied.forward and op["result"]["status"] == "ok"
    assert set(nodes) == {"c1_fetch", "c1"} and edges == [{"source": "c1_fetch", "target": "c1", "sourceHandle": None}]
    # 入口后加的：加上时接到第一个取数节点；模型再发一条同样的连线不重复
    applier.apply({"op": "add_node", "node": {"id": "start", "type": "input", "label": "入口", "config": {}}})
    assert {"source": "start", "target": "c1_fetch", "sourceHandle": None} in edges
    assert not applier.apply({"op": "add_edge", "edge": {"source": "start", "target": "c1_fetch"}}).changed
    assert len([e for e in edges if e["target"] == "c1_fetch"]) == 1
    # 接着写时重发同一个选用：不再落地
    applier.continuing()
    assert not applier.apply(use(wf)).changed
    assert "c1" in applier.landed()

    bad = use(wf, id="c2", inputs={"start": "2026-09-01"})
    refused = applier.apply(bad)
    assert not refused.changed and refused.forward and bad["result"]["status"] == "refused"
    checker = GraphChecker(applier)
    first = await checker.issues()
    assert [i["code"] for i in first if i["code"] == compose.REFUSED] == [compose.REFUSED]
    assert "请填写「end」" in first[0]["message"] and "重新输出 use_caliber" in first[0]["for_model"]
    # 交回过一次就不再交
    assert not [i for i in await checker.issues() if i["code"] == compose.REFUSED]

    # 兜底重放到上一轮的图上：拷进来的节点和接线照样在
    applier.replay_onto({"nodes": [{"id": "start", "type": "input", "data": {"config": {}}}], "edges": []})
    assert {"c1_fetch", "c1"} <= set(nodes) and any(e["target"] == "c1_fetch" for e in edges)


async def test_without_a_composer_use_caliber_is_dropped_like_any_unknown_op():
    nodes: dict = {}
    applier = GraphApplier(nodes, [])
    applied = applier.apply({"op": "use_caliber", "id": "c1", "workflow_id": "w", "version": 1})
    assert not applied.changed and not applied.forward and nodes == {}


# --------------------------------------------------------------------------
# 问数据走一遍：生成 → 运行 → 比对标 composed
# --------------------------------------------------------------------------


def _answer_script(wf: str) -> list[dict]:
    from tests.test_copilot_answer_ops import _edge, _node

    return [
        {"op": "plan", "summary": "选用周销售口径"},
        _node("start", "input", {}),
        use(wf),
        _node("write", "report", {"instructions": "回答：{{ input.question }}"}),
        _edge("c1", "write"),
        _node("out", "output", {"fields": [{"name": "答案", "value": "{{ nodes.write.text }}"}]}),
        _edge("write", "out"),
        {"op": "done", "explanation": "按口径周销售回答", "run": True},
    ]


async def test_an_answer_built_with_use_caliber_is_labelled_composed(client, monkeypatch):
    from tests.test_copilot_answer_ops import BODY, _Scripted, _sse, _use

    wf = await S.publish()
    _use(monkeypatch, _Scripted(_answer_script(wf), []))
    ops = await _sse(client, {**BODY, "instruction": "九月第一周卖了多少"})
    [chosen] = [o for o in ops if o["op"] == "use_caliber"]
    assert chosen["result"]["status"] == "ok" and chosen["result"]["workflow_name"] == "周销售"
    assert not [o for o in ops if o["op"] == "add_node" and o["node"]["id"] in ("c1", "c1_fetch")], \
        "拷进来的节点不另外转给界面：过程里只有 use_caliber 那一行"
    [final] = [o for o in ops if o["op"] == "final"]
    assert final["next"] == "run", final["issues"]
    graph = final["graph"]
    assert {n["id"] for n in graph["nodes"]} == {"start", "c1_fetch", "c1", "write", "out"}
    assert not [i for i in validate_graph(GraphSpec.model_validate(graph)).issues if i.level == "error"]

    S.script(monkeypatch, "九月第一周销售额 [[m:gmv]]。")
    run = await run_manager.start(graph=graph, input_payload={"question": "九月第一周卖了多少"}, origin_kind="chat")
    row = await S.wait(run.id)
    assert row.status == "succeeded", row.error
    await ledger_match.drain()
    recorded = await store.latest(row.id)
    content = recorded.content
    assert content["algo"] == store.ALGO == "2"
    [query] = content["queries"]
    assert query["status"] == "composed" and query["node_id"] == "c1_fetch"
    [cand] = query["candidates"]
    assert cand["status"] == "proven" and cand["how"] == "composed" and cand["caliber"] == f"{wf}@1"
    [label] = content["reports"]["write"]["labels"].values()
    assert label["kind"] == "caliber" and label["how"] == "composed" and label["queries"] == [query["artifact"]]
    assert content["whole"]["equivalent"] is True and content["whole"]["how"] == "composed"
    assert recorded.content and (await store.latest(row.id)).trigger == "sealed"
    async with SessionLocal() as session:
        from app.db.models import Artifact

        ref = await session.get(Artifact, (recorded.artifact, row.id))
    assert ref.meta["calibers"] == [f"{wf}@1"] and ref.meta["counts"]["composed"] == 1


async def test_users_do_not_get_calibers_in_the_answer_look(monkeypatch):
    """使用者（开登录后的 user 角色）：问数据里不看口径、不能选用（拷进来的取数 SQL 在他的图里看得到）。"""
    import app.api.copilot as copilot
    from app.api.copilot import GenerateIn

    seen: dict = {}

    async def spy(session, payload, sources, **kw):
        seen["kinds"] = kw.get("kinds")
        raise RuntimeError("到这里就够了")

    monkeypatch.setattr(copilot, "_look_service", spy)
    monkeypatch.setattr("app.api.deps.restricted", lambda: True)
    with pytest.raises(RuntimeError):
        await copilot.answer_ops(GenerateIn(instruction="x", intent="answer")).__anext__()
    assert seen["kinds"] == ("table", "capability")
    monkeypatch.setattr("app.api.deps.restricted", lambda: False)
    with pytest.raises(RuntimeError):
        await copilot.answer_ops(GenerateIn(instruction="x", intent="answer")).__anext__()
    assert seen["kinds"] == ("table", "capability", "caliber")
    with pytest.raises(RuntimeError):
        await copilot.answer_ops(GenerateIn(instruction="x", intent="build")).__anext__()
    assert seen["kinds"] == ("table", "capability")
