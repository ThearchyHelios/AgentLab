"""变量静态分析。

这套模板取不到值时渲染成空字符串（为了让半成品的图也能跑）。代价是拼错一个
变量名不会报错、不会警告、不留痕迹——下游拿到空文本，模型基于空内容一本正经
地编一段回答出来。这些测试守的就是"在跑之前把这件事说破"。
"""

from __future__ import annotations

from app.engine.schema import GraphSpec
from app.engine.variables import analyze


def _graph(nodes, edges=()):
    return GraphSpec.model_validate({
        "nodes": [
            {"id": n["id"], "type": n["type"], "position": {"x": 0, "y": 0},
             "data": {"label": n.get("label", ""), "config": n.get("config", {})}}
            for n in nodes
        ],
        "edges": [{"source": s, "target": t} for s, t in edges],
    })


def test_lists_what_each_node_produces() -> None:
    g = _graph([
        {"id": "start", "type": "input", "label": "输入",
         "config": {"fields": [{"name": "question", "description": "用户的问题"}]}},
        {"id": "q", "type": "llm", "label": "取数", "config": {"assign_to": "sales"}},
    ], [("start", "q")])
    paths = {v.path: v for v in analyze(g).variables}

    assert "input.question" in paths
    assert paths["input.question"].description == "用户的问题"
    assert "vars.sales" in paths
    assert paths["vars.sales"].produced_by == "q"
    # 每个节点的输出都能被引用，不用显式声明
    assert "nodes.start" in paths and "nodes.q" in paths


def test_input_fields_are_also_available_as_vars() -> None:
    """入口节点把每个字段同时写进 vars（run_input 里 input 和 vars 是同一份）。

    不认这一条，{{ vars.topic }} 这种完全合法的写法会被报成"未定义"——
    误报是 linter 最坏的失败模式，它会让人把整个校验一起无视掉。
    """
    g = _graph([
        {"id": "start", "type": "input", "config": {"fields": [{"name": "topic"}]}},
        {"id": "a", "type": "llm", "config": {"prompt": "{{ vars.topic }}"}},
    ], [("start", "a")])
    assert [i for i in analyze(g).issues if i.level != "info"] == []


def test_typo_is_reported_with_a_suggestion() -> None:
    """拼错是这里最常见的错误。只说"不存在"没用，能猜就直说。"""
    g = _graph([
        {"id": "start", "type": "input", "config": {"fields": [{"name": "question"}]}},
        {"id": "a", "type": "llm", "label": "作答",
         "config": {"prompt": "回答：{{ input.quesiton }}"}},
    ], [("start", "a")])
    errs = [i for i in analyze(g).issues if i.level == "error"]

    assert len(errs) == 1
    assert "input.quesiton" in errs[0].message
    assert "input.question" in errs[0].message, "应该给出拼写建议"
    assert errs[0].node_id == "a"
    # 必须说清后果，否则用户不知道为什么"没报错但结果不对"
    assert "空字符串" in errs[0].message


def test_reference_before_production_is_a_warning() -> None:
    """变量确实存在，但那一步在这之后才跑——这时候取到的是空。

    这类最难查：变量名拼对了，表里也有，就是值不对。
    """
    g = _graph([
        {"id": "start", "type": "input", "config": {"fields": [{"name": "q"}]}},
        {"id": "use", "type": "llm", "label": "用它", "config": {"prompt": "{{ vars.later }}"}},
        {"id": "make", "type": "llm", "label": "产出它", "config": {"assign_to": "later"}},
    ], [("start", "use"), ("use", "make")])
    warns = [i for i in analyze(g).issues if i.level == "warning"]

    assert len(warns) == 1
    assert "vars.later" in warns[0].message
    assert "产出它" in warns[0].message, "要说清是谁产出的"
    assert warns[0].node_id == "use"


def test_same_node_reading_its_own_var_is_fine() -> None:
    """循环里节点读自己上一轮写的变量是正常写法，不该报。"""
    g = _graph([
        {"id": "n", "type": "llm", "label": "累加",
         "config": {"assign_to": "acc", "prompt": "上一轮：{{ vars.acc }}"}},
    ])
    assert [i for i in analyze(g).issues if i.level in ("error", "warning")] == []


def test_unused_variable_is_only_info() -> None:
    """产出没人用不一定是错，常常是改图改了一半——提一句，但别拦着跑。"""
    g = _graph([
        {"id": "a", "type": "llm", "label": "算一下", "config": {"assign_to": "unused"}},
    ])
    issues = analyze(g).issues
    assert [i.level for i in issues] == ["info"]
    assert "vars.unused" in issues[0].message


def test_builtins_and_filters_and_indexes_resolve() -> None:
    g = _graph([
        {"id": "a", "type": "llm", "config": {
            "prompt": "{{ last_message }} {{ vars.rows[0].name }} {{ nodes.a.text | json }}",
            "assign_to": "rows",
        }},
    ])
    assert [i for i in analyze(g).issues if i.level != "info"] == []


def test_scans_nested_config_not_just_top_level() -> None:
    """output.fields[].value、code 节点的 files 都是嵌套的，漏扫等于没扫。"""
    g = _graph([
        {"id": "out", "type": "output", "config": {
            "fields": [{"name": "结果", "value": "{{ vars.nope }}"}],
        }},
    ])
    errs = [i for i in analyze(g).issues if i.level == "error"]
    assert len(errs) == 1 and "vars.nope" in errs[0].message
    assert "fields[0].value" in errs[0].path or errs[0].path == "vars.nope"


def test_cycle_does_not_hang() -> None:
    """循环图很常见（loop 的 body 连回 loop 本身），拓扑分析不能死循环。"""
    g = _graph(
        [{"id": "a", "type": "llm"}, {"id": "b", "type": "llm"}],
        [("a", "b"), ("b", "a")],
    )
    assert analyze(g).variables      # 跑得完就行
