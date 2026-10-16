"""从钉住版本派生只读图（app/ledger/derive.py；总方案 X3：评测的口径题、账本的对账与召回共用这一个生成器）。

- load_pinned：只认可复用的源头口径卡；工作流、版本、节点不在、不是口径卡、转抄别处、输入不是查库的格、取数节点引用了
  入口字段以外的值，照实拒收（NotDerivable，带机器码）。
- caliber_probe：入口字段照抄、取数节点 id 和配置原样、口径卡 caliber_from 固定到那一版、「缺输入时」照抄；图能通过
  校验；同样的输入永远得到同一张图。
- 扩展点：带前缀的段改名并写出 caliber_from.nodes 映射；两份口径不带前缀放进一张图会撞 id、直接报错。

工作流、库、表都是虚构的。
"""
from __future__ import annotations

import pytest

from app.db.base import SessionLocal
from app.db.models import Workflow, WorkflowVersion
from app.engine.schema import GraphSpec, validate_graph
from app.ledger import derive


def node(nid, ntype, label=None, **config):
    return {"id": nid, "type": ntype, "position": {"x": 7, "y": 9}, "data": {"label": label or nid, "config": config}}


def chain(*nodes, defaults=None):
    graph = {"nodes": list(nodes), "edges": [{"source": a["id"], "target": b["id"]} for a, b in zip(nodes, nodes[1:])]}
    if defaults:
        graph["defaults"] = defaults
    return graph


FIELDS = [{"name": "start", "required": True, "type": "date"}, {"name": "region", "default": "华东"}]
FETCH_SQL = ("SELECT SUM(amount) AS gmv, COUNT(*) AS n FROM orders WHERE paid_date >= '{{ input.start }}' "
             "AND region = '{{ input.region | trim }}'")


def weekly(fetch_sql: str = FETCH_SQL, **card) -> dict:
    return chain(node("start", "input", fields=FIELDS),
                 node("fetch", "tool", "取数", tool="db_query__shop_x", args={"sql": fetch_sql}, assign_to="rows"),
                 node("card", "metrics", "周销售口径卡", caliber="周销售", caliber_version="v2", on_missing="null",
                      metrics=[{"id": "gmv", "name": "销售额", "expression": "cell(nodes.fetch, 0, 'gmv')"},
                               {"id": "avg", "expression": "cell(nodes.fetch, 0, 'gmv') / cell(nodes.fetch, 0, 'n')"}],
                      **card),
                 node("write", "report", instructions="写周报"),
                 node("out", "output"), defaults={"approval": "dangerous"})


async def publish(graph: dict, *, level: str | None = "published") -> str:
    async with SessionLocal() as session:
        wf = Workflow(name=f"派生_{id(graph)}", graph=graph, status="published", published_version=1, version=1)
        session.add(wf)
        await session.flush()
        session.add(WorkflowVersion(workflow_id=wf.id, version=1, graph=graph, level=level))
        await session.commit()
        return wf.id


async def pinned_of(graph: dict, **kw) -> derive.Pinned:
    wf_id = await publish(graph, **kw)
    async with SessionLocal() as session:
        return await derive.load_pinned(session, wf_id, 1, "card")


async def test_the_probe_copies_the_fetch_and_pins_the_card():
    pinned = await pinned_of(weekly())
    assert pinned.published and not pinned.retired and pinned.reads == (("fetch", "db_query__shop_x", ("gmv", "n")),)
    assert [m["id"] for m in pinned.metrics] == ["gmv", "avg"] and pinned.caliber == "周销售 @ v2"
    probe = derive.caliber_probe(pinned)
    nodes = {n["id"]: n for n in probe.graph["nodes"]}
    assert [n["id"] for n in probe.graph["nodes"]] == [derive.ENTRY_ID, "fetch", "card", derive.EXIT_ID]
    # 入口字段照抄（必填、缺省值、类型都不变）
    assert nodes[derive.ENTRY_ID]["data"]["config"]["fields"] == FIELDS
    # 取数节点 id、配置原样
    original = weekly()["nodes"][1]
    assert nodes["fetch"]["data"] == original["data"] and nodes["fetch"]["type"] == "tool"
    # 口径卡固定到那一版那张卡；「缺输入时」照抄；本地不写指标式
    card = nodes["card"]["data"]["config"]
    assert card == {"caliber_from": {"workflow_id": pinned.workflow_id, "workflow_version": 1, "node_id": "card"},
                    "on_missing": "null"}
    # 图级 defaults 照抄：节点上没写的项按同样的规则取值
    assert probe.graph["defaults"] == {"approval": "dangerous"}
    assert nodes[derive.EXIT_ID]["data"]["config"]["fields"] == [{"name": "周销售口径卡",
                                                                    "value": "{{ nodes.card.text }}"}]
    assert probe.card_ids == ("card",) and probe.origin["kind"] == "caliber_probe"
    assert probe.origin["from"][0]["workflow_version"] == 1
    assert validate_graph(GraphSpec.model_validate(probe.graph)).ok
    # 确定性：同样的版本永远得到同一张图
    assert derive.caliber_probe(pinned).graph == probe.graph
    # 处置写在本地口径卡上，交给和正式运行同一个升版判断
    assert derive.caliber_probe(pinned, upgrade_policy="dual").graph["nodes"][2]["data"]["config"][
        "upgrade_policy"] == "dual"


@pytest.mark.parametrize("graph, code, words", [
    (chain(node("start", "input"), node("fetch", "transform", mode="expression", expression="{}", assign_to="k"),
           node("card", "metrics", metrics=[{"id": "x", "expression": "vars.k.x"}]), node("out", "output")),
     derive.NOT_REUSABLE, "输入不全是查库结果"),
    (chain(node("start", "input"), node("fetch", "tool", tool="db_query__shop_x", args={"sql": "SELECT 1 AS v"}),
           node("card", "metrics", metrics=[{"id": "x", "expression": "cell(nodes.fetch, 1, 'v')"}]),
           node("out", "output")), derive.NOT_REUSABLE, "别的行号"),
    (chain(node("start", "input"), node("card", "metrics", caliber_from={"workflow_id": "w", "workflow_version": 1,
                                                                         "node_id": "c"}), node("out", "output")),
     derive.NOT_REUSABLE, "转抄"),
    (chain(node("start", "input"), node("card", "report"), node("out", "output")), derive.NOT_CARD, "不是口径卡"),
    (weekly("SELECT SUM(amount) AS gmv, COUNT(*) AS n FROM orders WHERE region = '{{ vars.region }}'"),
     derive.TEMPLATE, "vars.region"),
    (weekly("SELECT SUM(amount) AS gmv, COUNT(*) AS n FROM {{ nodes.pick.table }}"), derive.TEMPLATE, "nodes.pick"),
    (weekly("SELECT SUM(amount) AS gmv, COUNT(*) AS n FROM orders -- {{ file_text('x') }}"), derive.TEMPLATE,
     "file_text"),
])
async def test_cards_that_cannot_be_derived_say_why(graph, code, words):
    with pytest.raises(derive.NotDerivable) as caught:
        await pinned_of(graph)
    assert caught.value.code == code and words in caught.value.message


async def test_missing_workflows_versions_and_nodes():
    wf_id = await publish(weekly())
    async with SessionLocal() as session:
        for args, words in (((("nope", 1, "card")), "不存在"), (((wf_id, 9, "card")), "没有 v9"),
                            (((wf_id, 1, "ghost")), "没有节点 ghost")):
            with pytest.raises(derive.NotDerivable, match=words) as caught:
                await derive.load_pinned(session, *args)
            assert caught.value.code == derive.GONE


async def test_draft_versions_load_but_say_they_are_not_published():
    pinned = await pinned_of(weekly(), level=None)
    async with SessionLocal() as session:
        wf = await session.get(Workflow, pinned.workflow_id)
        wf.published_version = None
        await session.commit()
        again = await derive.load_pinned(session, pinned.workflow_id, 1, "card")
    assert again.published is False


async def test_prefixed_blocks_rename_and_map_for_two_calibers_in_one_graph():
    """召回（第 3 波）要把新旧两版放进同一张图：带前缀的段改名，口径卡写出改名映射。"""
    pinned = await pinned_of(weekly())
    old = derive.caliber_block(pinned, prefix="old_")
    new = derive.caliber_block(pinned, prefix="new_")
    assert [n["id"] for n in old.nodes] == ["old_fetch", "old_card"] and old.rename == {"fetch": "old_fetch"}
    assert old.nodes[-1]["data"]["config"]["caliber_from"]["nodes"] == {"fetch": "old_fetch"}
    graph = derive.assemble("recall", [old, new], fields=pinned.entry_fields)
    assert graph.card_ids == ("old_card", "new_card") and len(graph.origin["from"]) == 2
    names = [f["name"] for f in graph.graph["nodes"][-1]["data"]["config"]["fields"]]
    assert names == ["周销售口径卡", "周销售口径卡_2"]
    # 不带前缀放两份：节点 id 撞了，直接报错，不悄悄拼出一张错图
    with pytest.raises(ValueError, match="重复"):
        derive.assemble("recall", [derive.caliber_block(pinned), derive.caliber_block(pinned)])
    with pytest.raises(ValueError, match="不认识"):
        derive.assemble("nope", [old])


def test_template_problem_allows_only_entry_fields():
    assert derive.template_problem("q", {"args": {"sql": "SELECT '{{ input.start }}', '{{ input.x | trim }}'"}}) is None
    assert derive.template_problem("q", {"args": {"sql": "SELECT 1"}, "limit": 5}) is None
    assert "vars.a" in derive.template_problem("q", {"args": {"sql": "{{ vars.a }}"}})
    assert "input" in derive.template_problem("q", {"args": {"sql": "{{ input }}"}})
