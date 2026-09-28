"""画图时就能说破的几件证据链配置问题（validate_graph）。

- agent 写了 output_schema 却没开 cite_fields：Schema 不生效，assign_to 拿到的仍是自由文字
- 口径卡的输入来自沙箱代码、而它没标成取数（evidence_role 不是 source）：算出来的数核对不了出处
- 报告撰写节点没指定模型：和 llm / agent 一样提示会回退到默认 provider
- 钉住别处口径卡的 caliber_from：三项要写全、版本要是具体的数，本地不用再写指标
"""
from __future__ import annotations

from app.engine.schema import GraphSpec, validate_graph


def node(nid, ntype, label=None, **config):
    return {"id": nid, "type": ntype, "position": {"x": 0, "y": 0},
            "data": {"label": label or nid, "config": config}}


def issues(*nodes, node_id=None):
    graph = {"nodes": list(nodes),
             "edges": [{"source": a["id"], "target": b["id"]} for a, b in zip(nodes, nodes[1:])]}
    found = validate_graph(GraphSpec.model_validate(graph)).issues
    return [i for i in found if node_id is None or i.node_id == node_id]


START = node("start", "input")
OUT = node("out", "output", fields=[{"name": "r", "value": "{{ last_message }}"}])
SCHEMA = {"type": "object", "properties": {"gmv": {"type": "number"}}}


def test_output_schema_without_cite_fields_warns():
    bot = node("bot", "agent", prompt="查", tools=["db_query__shop"], output_schema=SCHEMA, model="m")
    [issue] = [i for i in issues(START, bot, OUT, node_id="bot") if "cite_fields" in i.message]
    assert issue.level == "warning" and issue.field == "output_schema"
    assert issue.message.startswith("output_schema 要开 cite_fields 才生效，会多一次抽取调用")

    cited = node("bot", "agent", prompt="查", tools=["db_query__shop"], output_schema=SCHEMA, cite_fields=True,
                 model="m")
    assert not [i for i in issues(START, cited, OUT, node_id="bot") if "cite_fields" in i.message]
    plain = node("bot", "agent", prompt="查", tools=["db_query__shop"], model="m")
    assert not [i for i in issues(START, plain, OUT, node_id="bot") if "cite_fields" in i.message]


def test_caliber_fed_by_a_compute_code_node_warns():
    def graph(**code):
        fetch = node("fetch", "code", "取数脚本", code="print('{\"gmv\": 1}')", assign_to="agg", **code)
        card = node("card", "metrics", caliber="周报口径", metrics=[
            {"id": "gmv", "expression": "vars.agg.gmv"}, {"id": "gmv2", "expression": "nodes.fetch.gmv * 2"}])
        return issues(START, fetch, card, OUT, node_id="card")

    [issue] = [i for i in graph() if "evidence_role" in i.message]
    assert issue.level == "warning" and issue.field == "metrics[0].expression"
    assert "「取数脚本」" in issue.message and "source" in issue.message
    assert [i for i in graph(evidence_role="compute") if "evidence_role" in i.message]
    assert not [i for i in graph(evidence_role="source") if "evidence_role" in i.message]


def test_evidence_role_must_be_source_or_compute():
    fetch = node("fetch", "code", code="print(1)", evidence_role="取数")
    [issue] = [i for i in issues(START, fetch, OUT, node_id="fetch") if i.field == "evidence_role"]
    assert issue.level == "error" and "source" in issue.message and "compute" in issue.message


def test_report_without_a_model_gets_the_same_warning_as_llm():
    card = node("card", "metrics", metrics=[{"id": "gmv", "expression": "1"}])
    write = node("write", "report", instructions="写周报")
    [issue] = [i for i in issues(START, card, write, OUT, node_id="write") if i.field == "model"]
    assert issue.level == "warning" and "没有指定模型" in issue.message


def test_caliber_from_replaces_local_metrics():
    pinned = {"workflow_id": "wf1", "workflow_version": 3, "node_id": "caliber"}
    card = node("card", "metrics", caliber_from=pinned)
    assert not [i for i in issues(START, card, OUT, node_id="card") if i.level == "error"]

    both = node("card", "metrics", caliber_from=pinned, metrics=[{"id": "gmv", "expression": "1"}])
    [issue] = [i for i in issues(START, both, OUT, node_id="card") if i.field == "metrics"]
    assert issue.level == "warning" and "不会生效" in issue.message

    for bad, field in ((["wf1"], "caliber_from"), ({"workflow_id": "wf1", "node_id": "c"}, "caliber_from.workflow_version"),
                       ({"workflow_id": "wf1", "workflow_version": "最新", "node_id": "c"},
                        "caliber_from.workflow_version"),
                       ({"workflow_id": "", "workflow_version": 1, "node_id": "c"}, "caliber_from")):
        found = [i for i in issues(START, node("card", "metrics", caliber_from=bad), OUT, node_id="card")
                 if i.level == "error"]
        assert [i.field for i in found] == [field], (bad, found)

    policy = node("card", "metrics", caliber_from=pinned, upgrade_policy="auto")
    [issue] = [i for i in issues(START, policy, OUT, node_id="card") if i.field == "upgrade_policy"]
    assert issue.level == "error" and "recompute" in issue.message
    ok = node("card", "metrics", caliber_from=pinned, upgrade_policy="dual")
    assert not [i for i in issues(START, ok, OUT, node_id="card") if i.level == "error"]


def test_template_eight_marks_its_fetch_code_as_a_source():
    """模板 ⑧ 的取数代码是「数据底座」：标成 source，口径卡读它不再被提示核对不了出处。"""
    from app.seed import TEMPLATES

    weekly = next(t for t in TEMPLATES if t["name"].startswith("⑧"))
    fetch = next(n for n in weekly["nodes"] if n["id"] == "fetch")
    assert fetch["data"]["config"]["evidence_role"] == "source"
    spec = GraphSpec.model_validate({"nodes": weekly["nodes"], "edges": weekly["edges"]})
    assert not [i for i in validate_graph(spec).issues if "evidence_role" in i.message]
