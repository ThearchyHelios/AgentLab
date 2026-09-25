"""条件表达式：写错了要在画图时就说出来，而不是跑到那一步才炸。

开发库里四次运行死在同一句"循环条件写错了：表达式里不允许出现 Set"。条件原文
是 `{{ vars.gate }} != "ok"`——把模板写法带进了表达式。`{{ x }}` 在 Python 里是
"装着一个集合的集合"，而表达式不允许集合，于是跑到循环那一步才报错，前面的步骤
全白跑。Copilot 的提示词还写着"模板语法在任意字符串里用"，模型照做而已。

这里守三件事：这种写法意思无歧义，按 vars.x 理解并提示；真写错的在校验阶段就
拦下（运行前校验会挡住它，Copilot 自查会把它交回去改）；运行时、校验、自查用的
是同一个 parse_expression，不会出现"校验说能跑、跑起来报错"。
"""
from __future__ import annotations

import asyncio

import pytest

from app.db.base import SessionLocal
from app.db.models import Run
from app.engine.expressions import (
    EXPRESSION_ROOTS,
    ExpressionError,
    eval_expression,
    parse_expression,
)
from app.engine.runner import run_manager
from app.engine.schema import GraphSpec, validate_graph
from app.engine.state import template_context

CTX = {"vars": {"gate": "fail", "ok": "fail", "state": {"count": 999}, "items": [1, 2]},
       "input": {}, "nodes": {}}


@pytest.mark.parametrize("expr, expected", [
    # 开发库里真实死过的四条
    ('{{ vars.gate }} != "ok"', True),
    ("{{ vars.ok }} == 'fail'", True),
    ("{{ vars.ok }} == 'ok'", False),
    ("{{ vars.state.count }} <= 1000", True),
])
def test_template_braces_in_an_expression_mean_the_bare_path(expr, expected):
    assert eval_expression(expr, CTX) is expected
    _, unwrapped, _ = parse_expression(expr)
    assert unwrapped, "剥掉了 {{ }} 就得说出来，校验据此给提示"


def test_braces_inside_a_string_literal_are_left_alone():
    """在语法树上剥，不在文本上剥：字符串里的花括号是字面量。"""
    assert eval_expression("contains(vars.gate, '{') or true", CTX) is True


@pytest.mark.parametrize("expr, hint", [
    ("{{ vars.items | length }} > 0", "len(x)"),         # 模板过滤器
    ("vars.gate in {'ok', 'fail'}", "用列表"),           # 集合写法
    ("foo(vars.gate)", "能用的只有"),                    # 不认识的函数
    ("'{{ vars.gate }}' == 'ok'", "不会被渲染"),         # 引号里的模板永远不成立
    ("vars.gate = 'ok'", "=="),                         # 赋值当比较
    ("[x for x in vars.items]", "列表推导式"),
    ("vars.items[0:1]", "切片"),
    ("vars.__class__", "私有属性"),
])
def test_real_mistakes_are_caught_statically_with_a_usable_hint(expr, hint):
    with pytest.raises(ExpressionError, match=hint.replace("(", r"\(").replace(")", r"\)")):
        parse_expression(expr)


def test_the_short_circuited_half_is_checked_too():
    """运行时 and 短路，右边从来走不到；静态检查得把整棵树走一遍。"""
    assert eval_expression("false and true", CTX) is False
    with pytest.raises(ExpressionError):
        parse_expression("false and foo(1)")


def test_names_outside_the_context_are_reported():
    """漏写 vars. 的 gate 运行时是 None，条件永远不成立——不报错，但得提示。"""
    _, _, unknown = parse_expression("gate == 'ok' and len(vars.items) > 0 and true")
    assert unknown == ["gate"]


def test_expression_roots_match_the_runtime_context():
    """EXPRESSION_ROOTS 是 template_context 的键的抄本，两边一旦不一致，提示就会误报。"""
    assert EXPRESSION_ROOTS == set(template_context({}))


# --------------------------------------------------------------------------
# 校验：按节点类型和模式挑字段
# --------------------------------------------------------------------------


def node(nid, ntype, **config):
    return {"id": nid, "type": ntype, "position": {"x": 0, "y": 0},
            "data": {"label": nid, "config": config}}


def issues_of(*nodes):
    chain = [node("start", "input", fields=[]), *nodes, node("out", "output", fields=[])]
    spec = GraphSpec.model_validate({
        "nodes": chain,
        "edges": [{"source": a["id"], "target": b["id"]} for a, b in zip(chain, chain[1:])],
    })
    return [(i.level, i.message) for i in validate_graph(spec).issues]


def test_a_broken_loop_condition_blocks_the_run():
    found = issues_of(node("lp", "loop", mode="while", condition="{{ vars.items | length }} > 0"))
    assert any(level == "error" and "循环条件写错了" in msg and "len(x)" in msg for level, msg in found), found


def test_braces_are_a_warning_not_an_error():
    found = issues_of(
        node("g", "transform", expression="'ok'", assign_to="gate"),
        node("br", "branch", cases=[{"key": "yes", "condition": '{{ vars.gate }} == "ok"'}]),
    )
    assert not [m for lv, m in found if lv == "error"], found
    assert any("是多余的" in m and "vars.gate" in m for _, m in found), found


def test_fields_that_are_not_evaluated_in_this_mode_are_not_checked():
    """foreach 循环的 condition、模板模式整形的 expression 不参与求值，报了就是误报。"""
    found = issues_of(
        node("lp", "loop", mode="foreach", items="[1]", condition="这不是表达式 >>>"),
        node("t", "transform", mode="template", template="x", expression="这也不是 >>>"),
    )
    assert not [m for lv, m in found if "写错了" in m], found


def test_branch_in_llm_mode_is_not_checked_either():
    found = issues_of(node("br", "branch", mode="llm", cases=[{"key": "a", "condition": "随便写的描述"}]))
    assert not [m for lv, m in found if "写错了" in m], found


def test_skip_if_and_metrics_are_checked():
    found = issues_of(node("t", "transform", expression="1", skip_if="vars.x = 1"))
    assert any("跳过条件写错了" in m for _, m in found), found
    found = issues_of(node("m", "metrics", metrics=[{"id": "r", "expression": "foo(1)"}]))
    assert any("指标「r」的表达式写错了" in m for _, m in found), found


def test_empty_while_condition_and_empty_transform_expression():
    found = issues_of(node("lp", "loop", mode="while", condition=""))
    assert any(lv == "warning" and "一次都不会跑" in m for lv, m in found), found
    found = issues_of(node("t", "transform", mode="expression", expression=""))
    assert any(lv == "error" and "没有填表达式" in m for lv, m in found), found


# --------------------------------------------------------------------------
# 真跑一遍：以前死在 Set 上的那种循环
# --------------------------------------------------------------------------


@pytest.fixture
async def engine_up():
    await run_manager.setup()
    yield
    await run_manager.shutdown()


async def test_a_loop_written_with_template_braces_now_runs(engine_up):
    graph = {
        "nodes": [
            node("start", "input", fields=[{"name": "question"}]),
            node("init", "transform", expression="0", assign_to="n"),
            node("lp", "loop", mode="while", condition="{{ vars.n }} < 2", max_iterations=5),
            node("inc", "transform", expression="vars.n + 1", assign_to="n"),
            node("out", "output", fields=[{"name": "结果", "value": "{{ vars.n }}"}]),
        ],
        "edges": [
            {"source": "start", "target": "init"},
            {"source": "init", "target": "lp"},
            {"source": "lp", "target": "inc", "sourceHandle": "body"},
            {"source": "inc", "target": "lp"},
            {"source": "lp", "target": "out", "sourceHandle": "done"},
        ],
    }
    run = await run_manager.start(graph=graph, input_payload={"question": "x"})
    for _ in range(300):
        async with SessionLocal() as session:
            row = await session.get(Run, run.id)
        if row.status in ("succeeded", "failed"):
            break
        await asyncio.sleep(0.05)
    assert row.status == "succeeded", row.error
    assert row.output["结果"] == "2"


# --------------------------------------------------------------------------
# 代码节点：assign_to 拿的是 stdout，结果得 print 出来
# --------------------------------------------------------------------------


def _code(code: str, **cfg):
    return node("c", "code", code=code, **{"assign_to": "state", **cfg})


def test_code_that_hands_over_nothing_is_caught():
    """开发库里的「测试 1」：条件修好之后跑通了，结果却是空的——代码最后一行是裸表达式。"""
    found = issues_of(_code('count = 1\n{"count": count}'))
    assert any(lv == "error" and "没有 print" in m and "vars.state" in m for lv, m in found), found
    # 同一张图的循环体：模板只是填进一个值，代码本身照样没 print
    found = issues_of(_code('state = {{ vars.state | json }}\nstate["count"] += 1\nstate'))
    assert any("没有 print" in m for _, m in found), found
    # 沙箱把空的和大写的 language 都当 python 跑，这里也得这么认
    for language in ("", "Python"):
        found = issues_of(_code('{"count": 1}', language=language))
        assert any("没有 print" in m for _, m in found), (language, found)


@pytest.mark.parametrize("code, cfg", [
    ('import json\nprint(json.dumps({"count": 1}))', {}),
    ("import sys\nsys.stdout.write('ok')", {}),
    ("from sys import stdout\nstdout.write('ok')", {}),
    ("import subprocess\nsubprocess.run(['echo', 'ok'])", {}),   # 子进程写的也是 stdout
    ("import main", {"files": {"main.py": "print('ok')"}}),     # print 在附带文件里
    ("{{ vars.code }}", {}),                  # ⑤号示例：整段代码是上游模型现写的，看不见
    ("exec({{ vars.code | json }})", {}),
    ("x = 1", {"assign_to": ""}),            # 不交给下游就不需要输出
    ("ls -la", {"language": "bash"}),        # bash 的命令本身就会输出，不按 print 判
])
def test_code_that_does_hand_something_over_passes(code, cfg):
    found = issues_of(_code(code, **cfg))
    assert not [m for _, m in found if "没有 print" in m], found
