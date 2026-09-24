"""自动排版。

以前只有"按最长路径分层、同层按 id 字典序竖排"两条规则：并行的分支谁上谁下
是随机的（`out_no` 排在 `out_yes` 上面，因为 "n" < "y"），层间距写死 300，
两个节点扇出到同一个目标时连线还会叠在一起。这些测试守的是重写之后的四条
不变量：并行同层、分支按语义顺序、同列不重叠、走廊按车道需求留宽。
"""

from __future__ import annotations

from app.engine.layout import NODE_W, auto_layout
from app.engine.schema import GraphSpec


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


def _parallel_graph():
    """一个入口扇出三条并行的 agent，再汇进同一个节点。"""
    return _graph(
        [
            {"id": "in", "type": "input"},
            {"id": "a1", "type": "agent"},
            {"id": "a2", "type": "agent"},
            {"id": "a3", "type": "agent"},
            {"id": "join", "type": "llm"},
        ],
        [("in", "a1"), ("in", "a2"), ("in", "a3"), ("a1", "join"), ("a2", "join"), ("a3", "join")],
    )


def test_parallel_branches_share_a_column() -> None:
    """并行的三条支路必须落在同一列：它们之间没有依赖，谁也不该排到谁后面。"""
    spec = auto_layout(_parallel_graph())
    xs = {n.id: n.position.x for n in spec.nodes}
    assert xs["a1"] == xs["a2"] == xs["a3"]
    assert xs["in"] < xs["a1"] < xs["join"]


def test_parallel_branches_are_stacked_without_overlap() -> None:
    spec = auto_layout(_parallel_graph())
    ys = sorted(n.position.y for n in spec.nodes if n.id.startswith("a"))
    assert len(set(ys)) == 3
    # 同列相邻节点的纵向间距要够一个节点高度，否则卡片会叠在一起
    assert ys[1] - ys[0] >= 60
    assert ys[2] - ys[1] >= 60


def test_branch_cases_keep_their_declared_order() -> None:
    """分支的 yes/no 是语义顺序，不能按 id 字典序排（"no" < "yes" 会反过来）。"""
    spec = _graph(
        [
            {"id": "gate", "type": "branch",
             "config": {"cases": [{"key": "yes"}, {"key": "no"}]}},
            {"id": "out_no", "type": "output"},
            {"id": "out_yes", "type": "output"},
        ],
        [("gate", "out_yes", "yes"), ("gate", "out_no", "no")],
    )
    laid = auto_layout(spec)
    pos = {n.id: n.position for n in laid.nodes}
    assert pos["out_yes"].y < pos["out_no"].y
    assert pos["out_yes"].x == pos["out_no"].x


def test_loop_body_stays_right_of_the_loop_node() -> None:
    """回边不能被当成普通边参与分层——那样循环体会跑到循环节点左边去。"""
    spec = _graph(
        [
            {"id": "start", "type": "input"},
            {"id": "each", "type": "loop"},
            {"id": "body", "type": "llm"},
            {"id": "collect", "type": "transform"},
            {"id": "done", "type": "output"},
        ],
        [
            ("start", "each"),
            ("each", "body", "body"),
            ("body", "collect"),
            ("collect", "each"),
            ("each", "done", "done"),
        ],
    )
    laid = auto_layout(spec)
    pos = {n.id: n.position for n in laid.nodes}
    assert pos["start"].x < pos["each"].x < pos["body"].x < pos["collect"].x
    # 循环体出口排在结束出口上面
    assert pos["body"].y < pos["done"].y


def test_corridor_grows_with_lane_demand() -> None:
    """扇出的线越多，走廊越宽——宽度不够，前端的车道就铺不开，线会叠回去。"""
    narrow = auto_layout(_parallel_graph())
    wide = auto_layout(_graph(
        [{"id": "in", "type": "input"}, {"id": "join", "type": "llm"}]
        + [{"id": f"m{i}", "type": "llm"} for i in range(8)],
        [("in", f"m{i}") for i in range(8)] + [(f"m{i}", "join") for i in range(8)],
    ))

    def corridor(spec) -> float:
        xs = sorted({n.position.x for n in spec.nodes})
        return min(b - a for a, b in zip(xs, xs[1:])) - NODE_W

    assert corridor(wide) > corridor(narrow)


def test_long_edges_do_not_widen_a_single_corridor_forever() -> None:
    """走廊有上限。20 路扇出也不该把图撑到屏幕外面去。"""
    spec = auto_layout(_graph(
        [{"id": "in", "type": "input"}]
        + [{"id": f"m{i}", "type": "llm"} for i in range(20)],
        [("in", f"m{i}") for i in range(20)],
    ))
    xs = sorted({n.position.x for n in spec.nodes})
    assert xs[1] - xs[0] <= NODE_W + 460


def test_same_column_never_overlaps() -> None:
    """一整列节点两两之间必须留出至少一个卡片的高度。"""
    spec = auto_layout(_graph(
        [{"id": "in", "type": "input"}] + [{"id": f"n{i}", "type": "llm"} for i in range(12)],
        [("in", f"n{i}") for i in range(12)],
    ))
    ys = sorted(n.position.y for n in spec.nodes if n.id != "in")
    gaps = [b - a for a, b in zip(ys, ys[1:])]
    assert gaps and min(gaps) >= 60


def test_layout_is_stable_and_idempotent() -> None:
    """同一张图排两次必须得到同一份坐标，否则保存后一刷新就跳。"""
    first = auto_layout(_parallel_graph()).model_dump(mode="json")
    second = auto_layout(GraphSpec.model_validate(first)).model_dump(mode="json")
    assert [(n["id"], n["position"]) for n in first["nodes"]] == \
           [(n["id"], n["position"]) for n in second["nodes"]]


def test_layout_does_not_touch_the_graph_itself() -> None:
    """排版只改坐标：节点、边、配置一个都不能丢，也不能多出来。"""
    before = _parallel_graph()
    ids = [(n.id, str(n.type)) for n in before.nodes]
    edges = [(e.source, e.target, e.sourceHandle or "") for e in before.edges]
    after = auto_layout(before)
    assert [(n.id, str(n.type)) for n in after.nodes] == ids
    assert [(e.source, e.target, e.sourceHandle or "") for e in after.edges] == edges


def test_cycle_does_not_hang_or_collapse() -> None:
    """没有任何入口的纯环图也得排完，不能死循环。"""
    spec = _graph(
        [{"id": "x", "type": "llm"}, {"id": "y", "type": "llm"}],
        [("x", "y"), ("y", "x")],
    )
    laid = auto_layout(spec)
    assert {n.id for n in laid.nodes} == {"x", "y"}


def test_dangling_edges_and_empty_graph_are_survivable() -> None:
    assert auto_layout(GraphSpec()).nodes == []
    spec = auto_layout(_graph(
        [{"id": "a", "type": "llm"}],
        [("a", "ghost"), ("ghost", "a")],
    ))
    assert spec.nodes[0].position.x >= 0


def test_self_loop_is_kept() -> None:
    spec = auto_layout(_graph([{"id": "a", "type": "llm"}], [("a", "a")]))
    assert spec.nodes[0].position.x >= 0
    assert len(spec.edges) == 1
