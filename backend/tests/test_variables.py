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
        "edges": [
            {"source": e[0], "target": e[1], **({"sourceHandle": e[2]} if len(e) > 2 else {})}
            for e in edges
        ],
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


def test_bare_namespace_roots_are_valid() -> None:
    """{{ input }} 取整个输入 dict，{{ vars }} 取整个变量池——都是合法写法。

    上一版把这三个根跳过了没注册，于是 {{ input }} 被报成"没有任何节点产出"。
    这不是一条无害的多余提示：runs 启动前要过校验，一条假错误意味着整张图
    什么都跑不了。实际把问数据页整个卡住过。
    """
    g = _graph([
        {"id": "start", "type": "input", "config": {"fields": [{"name": "q"}]}},
        {"id": "a", "type": "llm", "config": {
            "prompt": "{{ input }} {{ vars }} {{ nodes }} {{ messages }} {{ last_message }}",
        }},
    ], [("start", "a")])
    bad = [i for i in analyze(g).issues if i.level in ("error", "warning")]
    assert bad == [], [i.message for i in bad]


def test_unknown_root_is_only_a_warning() -> None:
    """认不出的根可能只是我没建模的东西，不该拦住运行。

    能穷举的（vars/input/nodes 的成员）才报 error——那几个命名空间的成员
    全是从图里扫出来的，没扫到就是真没有。
    """
    g = _graph([
        {"id": "a", "type": "llm", "config": {"prompt": "{{ 某个我不认识的东西.x }}"}},
    ])
    issues = [i for i in analyze(g).issues if i.level != "info"]
    assert len(issues) == 1 and issues[0].level == "warning", [i.model_dump() for i in issues]


def test_input_is_open_when_no_fields_are_declared() -> None:
    """入口不声明字段时，input.* 是开放的——运行时传什么键都行。

    问数据页就是这样：问题由系统注入成 input.question，而入口节点里一个
    字段都没有（声明了反而会要求用户再填一次表单）。把它报成"未定义"会让
    整条对话路径跑不起来——实际发生过。
    """
    g = _graph([
        {"id": "start", "type": "input", "config": {}},
        {"id": "a", "type": "llm", "config": {"prompt": "{{ input.question }}"}},
    ], [("start", "a")])
    assert [i for i in analyze(g).issues if i.level != "info"] == []


def test_input_is_closed_once_fields_are_declared() -> None:
    """声明了字段就是闭集，那时候拼错才叫拼错。"""
    g = _graph([
        {"id": "start", "type": "input", "config": {"fields": [{"name": "question"}]}},
        {"id": "a", "type": "llm", "config": {"prompt": "{{ input.quesiton }}"}},
    ], [("start", "a")])
    errs = [i for i in analyze(g).issues if i.level == "error"]
    assert len(errs) == 1 and "input.question" in errs[0].message


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


def test_drilling_into_a_value_is_fine_but_the_variable_must_exist() -> None:
    """往值内部钻不校验（结构是运行期才有的），但第二段必须存在。

    分寸就在这里：退得太松，{{ input.quesiton }} 会退到 input 被当成合法，
    拼写检查整个失效；退得太紧，{{ vars.rows[0].name }} 会被误报。
    """
    g = _graph([
        {"id": "a", "type": "llm", "config": {
            "assign_to": "rows",
            "prompt": "{{ vars.rows[0].name }} {{ nodes.a.text }}",   # 都该通过
        }},
        {"id": "b", "type": "llm", "config": {
            "prompt": "{{ vars.rowz[0].name }}",                      # 第二段拼错了
        }},
    ], [("a", "b")])
    errs = [i for i in analyze(g).issues if i.level == "error"]
    assert len(errs) == 1, [i.message for i in errs]
    assert "vars.rowz" in errs[0].message and errs[0].node_id == "b"


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


# --------------------------------------------------------------------------
# 循环变量
# --------------------------------------------------------------------------
#
# 模板⑦「批量处理（循环）」装好就红着：{{ vars.item }} 被报成"没有任何节点
# 产出"，而那是循环最正常的写法。它是唯一一个内置模板开箱就报错的，
# 新用户点开第一眼看到的就是红字，运行按钮还是灰的。
#
# 根因不在模板，在这里：产出侧从来没登记过循环注入的变量
# （control.py:142 每轮写 vars.<item_var> 和 vars.<item_var>_index）。
# kind="loop" 在 _KIND_ORDER 里一直有位置，只是没人产出过。


def _loop_graph(*, item_var="item", body_ref="{{ vars.item }}", extra_node=None):
    """input → loop →(body) handle → 回到 loop；loop →(done) out。

    末尾那条回边是关键：它让 loop 的拓扑深度反而大于自己的循环体。
    """
    nodes = [
        {"id": "start", "type": "input", "config": {"fields": [{"name": "items"}]}},
        {"id": "each", "type": "loop", "label": "逐条循环",
         "config": {"mode": "foreach", "items": "{{ input.items }}", "item_var": item_var}},
        {"id": "handle", "type": "llm", "label": "处理单条", "config": {"prompt": body_ref}},
        {"id": "out", "type": "output", "config": {}},
    ]
    if extra_node:
        nodes.append(extra_node)
    edges = [("start", "each"), ("each", "handle", "body"),
             ("handle", "each"), ("each", "out", "done")]
    if extra_node:
        edges.append(("out", extra_node["id"]))
    return _graph(nodes, edges)


def test_loop_item_is_a_known_variable() -> None:
    """循环体里引用 {{ vars.item }} 不该被报成未定义。"""
    report = analyze(_loop_graph())
    paths = {v.path for v in report.variables}
    assert "vars.item" in paths
    assert "vars.item_index" in paths

    errors = [i for i in report.issues if i.level == "error"]
    assert not errors, [i.message for i in errors]


def test_loop_item_follows_the_configured_name() -> None:
    report = analyze(_loop_graph(item_var="row", body_ref="{{ vars.row }}"))
    paths = {v.path for v in report.variables}
    assert {"vars.row", "vars.row_index"} <= paths
    assert not [i for i in report.issues if i.level == "error"]


def test_no_false_ordering_warning_inside_the_loop_body() -> None:
    """循环体的拓扑深度因为回边天然小于循环节点，套用"先后"判定必然误报。

    以前报的是"{{ vars.item }} 由「逐条循环」产出，但那一步在这之后才跑"——
    而那正是循环最正常的写法。
    """
    report = analyze(_loop_graph())
    assert not [i for i in report.issues if "在这之后才跑" in i.message], \
        [i.message for i in report.issues]


def test_loop_variable_outside_the_body_still_warns() -> None:
    """但在循环体**外面**引用它确实取不到值，这条要留着。"""
    outside = {"id": "after", "type": "llm", "label": "循环之后",
               "config": {"prompt": "{{ vars.item }}"}}
    report = analyze(_loop_graph(extra_node=outside))
    warns = [i for i in report.issues if i.node_id == "after"]
    assert warns, [i.message for i in report.issues]
    assert "只在循环体内部有值" in warns[0].message


def test_while_loop_has_no_current_item() -> None:
    """while 模式不遍历列表，自然没有"当前项"（control.py 只在 foreach 分支注入）。"""
    report = analyze(_graph([
        {"id": "lp", "type": "loop",
         "config": {"mode": "while", "condition": "vars.done != True", "item_var": "item"}},
    ]))
    assert "vars.item" not in {v.path for v in report.variables}


# --------------------------------------------------------------------------
# 表达式字段里的裸引用
# --------------------------------------------------------------------------


def test_bare_refs_in_expression_fields_count_as_usage() -> None:
    """表达式字段写的是裸 vars.x，没有 {{ }}——只扫模板语法会认不出来。

    模板⑦里 `(vars.collected or '') + str(vars.one)` 就是这样，
    于是 vars.one 被报成"产出了但没有任何地方引用"，而它明明在用。
    """
    report = analyze(_graph([
        {"id": "a", "type": "llm", "config": {"prompt": "x", "assign_to": "one"}},
        {"id": "b", "type": "transform", "config": {
            "mode": "expression",
            "expression": "(vars.collected or '') + str(vars.one)",
            "assign_to": "collected",
        }},
    ], [("a", "b")]))

    one = next(v for v in report.variables if v.path == "vars.one")
    assert one.refs, "表达式里用了就不该说没人用"
    assert not [i for i in report.issues if "vars.one" in i.message]


def test_branch_case_conditions_count_as_usage() -> None:
    report = analyze(_graph([
        {"id": "a", "type": "llm", "config": {"prompt": "x", "assign_to": "score"}},
        {"id": "b", "type": "branch", "config": {
            "mode": "expression",
            "cases": [{"key": "hi", "condition": "vars.score > 3"}],
        }},
    ], [("a", "b")]))
    assert next(v for v in report.variables if v.path == "vars.score").refs


def test_auto_provided_variables_are_not_nagged_about() -> None:
    """系统自动提供的变量不报"没人用"——作者没声明过它，提醒他等于噪音。

    内置变量（last_message 等）本来就不在这条路上；循环的 item_index 和
    入口字段的 vars.<name> 别名以前会被报，而真正的 input.<name> 用着呢。
    """
    report = analyze(_loop_graph())
    unused = [i.message for i in report.issues if "没有任何地方引用" in i.message]
    assert not any("item_index" in m for m in unused), unused
    assert not any("vars.items" in m for m in unused), unused
