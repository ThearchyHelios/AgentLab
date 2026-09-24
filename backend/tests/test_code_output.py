"""代码节点的输出契约。

`print()` 是代码节点唯一的产出通道，而 print 一定会补一个换行。以前
`assign_to` 拿到的是**原始 stdout**，于是 `print('ok')` 写进去的是 `"ok\n"`：

    分支条件  vars.verdict == 'ok'    → False（走了 default）
    循环条件  vars.verdict == 'fail'  → False（循环正常退出）

同一个变量、同一个表达式，两个控制节点得出相反的结论——循环那边看着是
"成功了"，分支那边判成"失败"，最后交出来的是一份自相矛盾的结果。这不是
个例：只要有人写 `print(x)` 再拿它比字符串，就必然踩上。

真实用例见运行 d47bfcb03dc747658c2d63f8755992b9：agent 明明返回了
`{"ok": true, "result": "1+1 = 2"}`，judge 节点也 print 出了 ok，
流程却走到"已重试 2 次仍未成功，流程终止"。
"""

from __future__ import annotations

import asyncio

from app.engine.context import NodeContext, RunContext
from app.engine.nodes.tools import run_code
from app.engine.schema import GraphNode, GraphSpec
from app.sandbox.base import ExecResult


class _FakeSandbox:
    """只把预设的 ExecResult 递回来，不真起沙箱。"""

    def __init__(self, result: ExecResult) -> None:
        self.result = result

    async def run(self, code, **kwargs):  # noqa: ANN001, ANN003, ANN201
        self.calls = (code, kwargs)
        return self.result


def _ctx(monkeypatch, result: ExecResult, **config) -> NodeContext:
    import app.engine.nodes.tools as tools

    monkeypatch.setattr(tools, "sandbox_manager", _FakeSandbox(result))
    node = GraphNode(
        id="judge", type="code",
        data={"label": "判定", "config": {"language": "python", "code": "print('ok')",
                                          "isolation": "fast", **config}},
    )
    spec = GraphSpec(nodes=[node], edges=[])
    return NodeContext(node=node, run=RunContext(run_id="r1", thread_id="t1", spec=spec))


def _run(state, ctx) -> dict:
    return asyncio.run(run_code(state, ctx))


def test_assigned_variable_has_no_print_newline(monkeypatch) -> None:
    """print('ok') 之后，变量里就该是 'ok'，不是 'ok\\n'。

    这一条是 d47bfcb0 那个自相矛盾结果的直接原因：分支比 'ok' 比不中，
    于是从"重试成功"的图里走出了"重试失败"的结局。
    """
    ctx = _ctx(monkeypatch, ExecResult(stdout="ok\n"), assign_to="verdict")
    updates = _run({"vars": {}}, ctx)

    assert updates["vars"]["verdict"] == "ok"
    # 分支/循环的条件表达式就是拿它比的，两边必须一致
    assert (updates["vars"]["verdict"] == "ok") is True


def test_assigned_variable_is_trimmed_but_keeps_inner_lines(monkeypatch) -> None:
    """只削掉首尾的空白，多行输出里面的换行是内容，不能动。"""
    ctx = _ctx(monkeypatch, ExecResult(stdout="第一行\n第二行\n"), assign_to="report")
    updates = _run({"vars": {}}, ctx)

    assert updates["vars"]["report"] == "第一行\n第二行"


def test_json_stdout_still_becomes_a_parsed_object(monkeypatch) -> None:
    """stdout 是 JSON 时仍然解析成对象——这是"下游直接用字段"的既有约定。"""
    ctx = _ctx(monkeypatch, ExecResult(stdout='{"ok": true, "n": 2}\n'), assign_to="data")
    updates = _run({"vars": {}}, ctx)

    assert updates["vars"]["data"] == {"ok": True, "n": 2}


def test_node_text_matches_the_assigned_variable(monkeypatch) -> None:
    """`{{ nodes.x.text }}` 和 `{{ vars.x }}` 是同一个东西的两种写法。

    以前一个带换行一个也带换行，至少还一致；修的时候只修一半的话，两者
    会给出不同的值——那比原来的毛病更难查。
    """
    ctx = _ctx(monkeypatch, ExecResult(stdout="ok\n"), assign_to="verdict")
    updates = _run({"vars": {}}, ctx)

    payload = updates["nodes"]["judge"]
    assert payload["text"] == updates["vars"]["verdict"] == "ok"
    # 原始 stdout 仍然原样留着：要查"到底跑了什么"时只能看它
    assert payload["stdout"] == "ok\n"


def test_empty_output_assigns_empty_string_not_whitespace(monkeypatch) -> None:
    ctx = _ctx(monkeypatch, ExecResult(stdout="\n\n"), assign_to="verdict")
    updates = _run({"vars": {}}, ctx)

    assert updates["vars"]["verdict"] == ""
