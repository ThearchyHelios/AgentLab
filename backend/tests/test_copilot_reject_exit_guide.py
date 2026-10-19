"""助手的节点手册要教「驳回」出口（docs/design/orchestration-round/00-总方案.md §3）。

手册里没写的出口，助手就不会接：驳回之后要通知申请人、退回修改、改走另一条路的，画出来的图只会在驳回时整轮结束。
这里不只查关键词，还拿校验认的那一份（会被驳回的节点类型、出口的 sourceHandle、发布检查的「走不到结束」）来对，
再照手册的写法搭一张图，校验、发布检查都过。
"""
from __future__ import annotations

import re

from app.api.copilot import NODE_REFERENCE
from app.engine.schema import GATE_TYPES, REJECT_HANDLE, GraphSpec, validate_graph


def _reject_entry() -> str:
    start = NODE_REFERENCE.index("「驳回」出口")
    return NODE_REFERENCE[start:NODE_REFERENCE.index("\n- 一个节点连出多条普通边", start)]


def test_the_guide_names_every_node_that_can_be_rejected_and_the_handle():
    entry = _reject_entry()
    assert f'sourceHandle="{REJECT_HANDLE}"' in entry
    # 能接驳回出口的节点类型（schema.GATE_TYPES）一个不漏，按 type 字段写
    assert all(f"{t}" in entry for t in GATE_TYPES), [t for t in GATE_TYPES if str(t) not in entry]
    # 六种关卡都点到：审批、补充输入、编辑稿件、动作确认、代码 / 工具审批、双盲裁决
    for kind in ("审批", "补充输入", "编辑稿件", "动作确认", "代码执行审批", "工具调用审批", "裁决"):
        assert kind in entry, kind


def test_the_guide_says_when_to_connect_it_and_what_happens_without_it():
    entry = _reject_entry()
    # 什么时候接：驳回后要通知、退回修改、走另一条路
    assert "通知" in entry and "退回修改" in entry and "另一条路" in entry
    # 不接时：运行结束、记被驳回（不算失败）；审批模式没设驳回即终止的老规矩照实说
    assert "记为被驳回" in entry and "不算失败" in entry
    assert "stop_on_reject" in entry
    # 驳回那条路必须走到成果节点或结束（发布检查 reject_exit.no_end），绕回去改完再问一次也可以
    assert "成果节点" in entry and "不再往下连" in entry and "发布检查" in entry
    # 驳回不是故障：失败重试、出错时继续不管它
    assert "失败重试" in entry
    # 驳回之后下游取得到谁、备注
    assert "{{ nodes.关卡id.note }}" in entry


def test_the_guide_does_not_leak_internal_names():
    entry = _reject_entry()
    for internal in ("reject_exit", "NodeRejected", "end_reason", "cancelled", "__decision__", "GATE_TYPES"):
        assert internal not in entry, internal


def test_the_old_one_line_rule_is_replaced_not_duplicated():
    # 以前只有一句「human 节点（approve 模式）：sourceHandle 用 approved 和 rejected」：驳回出口说一处，不留两种说法
    assert len(re.findall(r'sourceHandle="rejected"', NODE_REFERENCE)) == 1


def _node(nid: str, ntype: str, **config):
    return {"id": nid, "type": ntype, "position": {"x": 0, "y": 0}, "data": {"label": nid, "config": config}}


def test_a_graph_drawn_the_way_the_guide_says_passes_the_checks():
    """照手册写：审批批准走 approved，驳回接 sourceHandle="rejected" 到「通知申请人」再到一个成果节点；工具的确认被驳回
    退回上游修改（连回起草，改完再问一次）。画布校验、各级发布检查都不报驳回出口的问题。"""
    from app.engine.governance import lint_for_publish

    graph = {
        "nodes": [_node("start", "input"), _node("draft", "llm", prompt="起草"),
                  _node("gate", "human", mode="approve", title="可以吗"),
                  _node("send", "tool", tool="calculator", args={"expression": "1+1"}, approval="always"),
                  _node("tell", "llm", prompt="通知申请人：{{ nodes.gate.note }}"),
                  _node("done", "output", fields=[{"name": "结果", "value": "{{ nodes.send.text }}"}]),
                  _node("back", "output", fields=[{"name": "结果", "value": "{{ nodes.tell.text }}"}])],
        "edges": [{"source": "start", "target": "draft"}, {"source": "draft", "target": "gate"},
                  {"source": "gate", "target": "send", "sourceHandle": "approved"},
                  {"source": "gate", "target": "tell", "sourceHandle": "rejected"},
                  {"source": "tell", "target": "back"},
                  {"source": "send", "target": "done"},
                  {"source": "send", "target": "draft", "sourceHandle": "rejected"}],
    }
    spec = GraphSpec.model_validate(graph)
    codes = {i.code for i in validate_graph(spec).issues if i.level == "error"}
    assert not {c for c in codes if c and "reject" in c}, codes
    for level in ("draft", "published", "governed"):
        issues = lint_for_publish(spec, level=level).issues
        assert not [i for i in issues if (i.code or "").startswith("reject_exit")], level
