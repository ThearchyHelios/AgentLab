"""画布自动排版。

模型生成的图只有拓扑、没有坐标，全堆在原点就没法看；用户手拖过之后点
「自动排版」也得重新摆一遍。这里做的是分层布局（Sugiyama 那条路）的简化版，
四步：

1. **分层**：按最长路径定层。环上的回边先剔掉——不剔的话 loop 节点的回边
   会把整张图压成一层，循环体跑到循环节点左边去。
2. **层内排序**：中位数启发式上下扫，取交叉最少的那一版。并行分支因此会按
   上游的相对位置排好，而不是按 id 字典序乱排（以前 `out_no` 排在 `out_yes`
   上面，因为 "n" < "y"——分支的 yes/no 就反着画）。
3. **定 y**：先均匀堆叠，再按邻居重心迭代松弛。这一步是"同一列的节点和上下游
   对齐"的关键，也是长边能走直、少拐弯的前提。
4. **定 x**：层与层之间留一条走廊，走廊宽度按穿过它的连线在竖直方向的最大
   重叠数算。前端拿这个宽度给每条线分配独立的竖直车道，线才不会叠在一起。
   ——走廊留窄了，前端再聪明也只能把线叠起来。

坐标的约定和前端 `canvas/routing.ts` 是一份契约，改这里要同步改那边：
节点宽 238（NodeCard 写死的），同列节点最小间距 ≥ MIN_ROW_GAP，走廊里
竖直车道间距 LANE。
"""

from __future__ import annotations

from collections import defaultdict, deque
from statistics import median

from app.engine.schema import GraphNode, GraphSpec, NodeType

#: 画布节点的宽度。前端 NodeCard 的 `width: 238` 是写死的，这里必须跟着它——
#: 排版算出来的走廊是给连线用的，宽度估错了走廊就白留。
NODE_W = 238
#: 节点高度估算：标题栏 + 两行摘要。真实高度由前端量，这里只要不低估到让
#: 同列节点撞上就行（前端按实测高度铺端口，能自己吸收这点误差）。
NODE_H = 76
#: 同列相邻节点的最小空隙。除了不撞，还要给横向走线留出能穿过去的空档。
MIN_ROW_GAP = 56
#: 连线离开节点后的第一段水平长度。
STUB = 14
#: 同一条走廊里相邻竖直车道的间距。
LANE = 18
#: 竖直区间重叠多少以内算"同一条道"。共用扇出点的两条线首尾相接，不该各占一条。
LANE_OVERLAP_TOLERANCE = 6.0
#: 走廊宽度下限／上限。上限是防止一个 20 路扇出的节点把整张图撑到屏幕外；
#: 顶到上限时前端会按可用宽度压缩车道间距。
CORRIDOR_MIN = 96
CORRIDOR_MAX = 420
X_ORIGIN = 80

#: 分支节点的兜底出口，和前端 `sourceHandles()` 里的写法一致
BRANCH_FALLBACK = "default"


def _height(node: GraphNode) -> float:
    """节点高度估算。

    多 agent 节点跑起来会在卡片里展开一块协作矩阵（前端 canvas/TeamMatrix）：
    名册有几个人就多几行。排版必须按**展开后**的高度留位置——它会盖住下面那张
    卡，而且运行结束后矩阵不会收起来（那正是要回看的东西），重叠是永久的。
    """
    if node.type == NodeType.SUPERVISOR:
        n = len(node.config.get("agents") or [])
        if n:
            # 标题 34 + 摘要 28 + 矩阵（表头 19 + n 行 21 + 页脚 25 + 内外边距 14）
            # + 正在跑的那个人多半还会带一行当前任务 26
            return 146 + 21 * n + 26
    return NODE_H


def _dedupe_edges(spec: GraphSpec, nodes: dict[str, GraphNode]) -> list:
    """丢掉悬空边和重复边，顺便给每条边定一个稳定的身份。

    自环（source == target）留到最后单独处理，它不参与分层。
    """
    seen: set[tuple[str, str, str]] = set()
    out = []
    for e in spec.edges:
        if e.source not in nodes or e.target not in nodes:
            continue
        key = (e.source, e.target, e.sourceHandle or "")
        if key in seen:
            continue
        seen.add(key)
        out.append(e)
    return out


def _back_edges(nodes: dict[str, GraphNode], edges: list) -> set[tuple[str, str, str]]:
    """DFS 森林里的回边。剔掉它们，剩下的必然是无环的。

    迭代式而不是递归：一张几百个节点的图不该因为递归深度挂掉。
    """
    adj: dict[str, list] = defaultdict(list)
    for e in edges:
        adj[e.source].append(e)

    WHITE, GREY, BLACK = 0, 1, 2
    state = dict.fromkeys(nodes, WHITE)
    back: set[tuple[str, str, str]] = set()

    for root in nodes:
        if state[root] != WHITE:
            continue
        stack: list[tuple[str, int]] = [(root, 0)]
        state[root] = GREY
        while stack:
            node_id, i = stack[-1]
            out = adj[node_id]
            if i >= len(out):
                state[node_id] = BLACK
                stack.pop()
                continue
            stack[-1] = (node_id, i + 1)
            edge = out[i]
            target = edge.target
            if state[target] == GREY:
                back.add((edge.source, edge.target, edge.sourceHandle or ""))
            elif state[target] == WHITE:
                state[target] = GREY
                stack.append((target, 0))
    return back


def _ranks(
    nodes: dict[str, GraphNode], edges: list, back: set[tuple[str, str, str]],
) -> dict[str, int]:
    """最长路径分层。并行分支会落在同一层，串行的才会被推开。"""
    forward = [e for e in edges if (e.source, e.target, e.sourceHandle or "") not in back]
    succ: dict[str, list[str]] = defaultdict(list)
    indeg: dict[str, int] = dict.fromkeys(nodes, 0)
    for e in forward:
        succ[e.source].append(e.target)
        indeg[e.target] += 1

    rank = dict.fromkeys(nodes, 0)
    queue = deque(sorted(n for n in nodes if indeg[n] == 0))
    while queue:
        node_id = queue.popleft()
        for target in succ[node_id]:
            rank[target] = max(rank[target], rank[node_id] + 1)
            indeg[target] -= 1
            if indeg[target] == 0:
                queue.append(target)
    return rank


def _handle_order(node: GraphNode) -> list[str]:
    """节点出口在画布上从上到下的顺序。分支的 yes/no 就该是这个顺序。

    前端 `sourceHandles()` 决定出口怎么画，这里决定它们怎么排。两份必须一致，
    否则分支标签的上下关系会和连线对不上。
    """
    cfg = node.config
    if node.type == NodeType.BRANCH:
        keys = [
            str(c.get("key"))
            for c in (cfg.get("cases") or [])
            if isinstance(c, dict) and c.get("key")
        ]
        return keys + [BRANCH_FALLBACK]
    if node.type == NodeType.LOOP:
        return ["body", "done"]
    if node.type == NodeType.HUMAN and cfg.get("mode", "approve") == "approve":
        return ["approved", "rejected"]
    return ["out"]


def _initial_orders(
    spec: GraphSpec, nodes: dict[str, GraphNode], rank: dict[str, int], edges: list,
) -> list[list[str]]:
    """第 0 层先按声明顺序（入口排前面），后面的层按上游位置摆一遍。"""
    max_rank = max(rank.values(), default=0)
    orders: list[list[str]] = [[] for _ in range(max_rank + 1)]
    for node in spec.nodes:
        if node.id in rank:
            orders[rank[node.id]].append(node.id)

    preds: dict[str, list[str]] = defaultdict(list)
    for e in edges:
        preds[e.target].append(e.source)

    for r in range(1, max_rank + 1):
        pos = {nid: i for i, nid in enumerate(orders[r - 1])}
        here = {nid: i for i, nid in enumerate(orders[r])}
        def key(nid: str) -> tuple[float, int]:
            ps = [pos[p] for p in preds[nid] if p in pos]
            return (median(ps) if ps else here[nid], here[nid])
        orders[r].sort(key=key)
    return orders


def _crossings(orders: list[list[str]], rank: dict[str, int], edges: list) -> int:
    """相邻两层之间的连线交叉数。只数跨一层的边——跨多层的长边没有虚拟节点，
    数不准，不如不数，免得把排序带偏。"""
    total = 0
    by_rank: dict[int, list] = defaultdict(list)
    for e in edges:
        rs, rt = rank[e.source], rank[e.target]
        if rt == rs + 1:
            by_rank[rs].append(e)
    for r, es in by_rank.items():
        if r + 1 >= len(orders):
            continue
        pos_s = {nid: i for i, nid in enumerate(orders[r])}
        pos_t = {nid: i for i, nid in enumerate(orders[r + 1])}
        pairs = [(pos_s[e.source], pos_t[e.target]) for e in es]
        for i in range(len(pairs)):
            for j in range(i + 1, len(pairs)):
                a, b = pairs[i], pairs[j]
                if (a[0] - b[0]) * (a[1] - b[1]) < 0:
                    total += 1
    return total


def _sweep(
    orders: list[list[str]], neigh: dict[str, list[str]], down: bool,
) -> list[list[str]]:
    """一轮中位数扫描。没邻居的节点按当前位置排，不被扫到队尾去。"""
    result = [list(ids) for ids in orders]
    seq = range(1, len(result)) if down else range(len(result) - 2, -1, -1)
    for r in seq:
        ref_rank = r - 1 if down else r + 1
        pos = {nid: i for i, nid in enumerate(result[ref_rank])}
        here = {nid: i for i, nid in enumerate(result[r])}

        def key(nid: str) -> tuple[float, int]:
            ps = [pos[p] for p in neigh[nid] if p in pos]
            return (median(ps) if ps else here[nid], here[nid])

        result[r].sort(key=key)
    return result


def _minimize_crossings(
    orders: list[list[str]], rank: dict[str, int], edges: list,
    preds: dict[str, list[str]], succs: dict[str, list[str]],
) -> list[list[str]]:
    best = [list(ids) for ids in orders]
    best_score = _crossings(best, rank, edges)
    for iteration in range(6):
        orders = _sweep(orders, preds if iteration % 2 == 0 else succs, iteration % 2 == 0)
        score = _crossings(orders, rank, edges)
        if score < best_score:
            best, best_score = [list(ids) for ids in orders], score
        if best_score == 0:
            break
    return best


def _apply_handle_order(
    orders: list[list[str]], spec: GraphSpec, nodes: dict[str, GraphNode],
    rank: dict[str, int],
) -> None:
    """分支/循环的出口有语义顺序，交叉再少也得让 yes 排在 no 上面。

    做法是"占位重排"：这些后继本来占着层里的哪几个格子，就把它们按出口顺序
    填回那几个格子——层里其他节点的相对次序不受影响。
    """
    outgoing: dict[str, list] = defaultdict(list)
    for e in spec.edges:
        outgoing[e.source].append(e)

    for node in spec.nodes:
        handles = _handle_order(node)
        if len(handles) < 2 or node.id not in nodes:
            continue
        outs = outgoing.get(node.id, [])
        if not outs:
            continue
        groups: list[str] = []
        for handle in handles:
            for e in outs:
                if e.target not in nodes or e.target == node.id:
                    continue
                if (e.sourceHandle or handles[0]) == handle and e.target not in groups:
                    groups.append(e.target)
        # 同一层里按出口顺序重排；跨层的没法重排，跳过
        by_rank: dict[int, list[str]] = defaultdict(list)
        for target in groups:
            by_rank[rank[target]].append(target)
        for r, targets in by_rank.items():
            if len(targets) < 2 or r >= len(orders):
                continue
            slots = sorted(orders[r].index(t) for t in targets if t in orders[r])
            if len(slots) != len(targets):
                continue
            for slot, target in zip(slots, targets):
                orders[r][slot] = target


def _stack(orders: list[list[str]], nodes: dict[str, GraphNode], row_gap: float) -> dict[str, float]:
    """每层先均匀堆叠，整层以 y=0 为中心。"""
    y: dict[str, float] = {}
    for ids in orders:
        heights = [_height(nodes[i]) for i in ids]
        total = sum(heights) + row_gap * (len(ids) - 1)
        cursor = -total / 2
        for node_id, h in zip(ids, heights):
            y[node_id] = cursor + h / 2
            cursor += h + row_gap
    return y


def _relax(
    y: dict[str, float], orders: list[list[str]], nodes: dict[str, GraphNode],
    preds: dict[str, list[str]], succs: dict[str, list[str]], row_gap: float,
    rounds: int = 6,
) -> None:
    """按邻居重心松弛，再把同层节点推开到互不重叠。

    推开的正向扫描会把整层往下带，所以每层收尾要整体平移回重心——平移不改变
    层内间距，重叠约束依然成立。
    """
    for iteration in range(rounds):
        down = iteration % 2 == 0
        neigh = preds if down else succs
        seq = range(len(orders)) if down else range(len(orders) - 1, -1, -1)
        for r in seq:
            ids = orders[r]
            if not ids:
                continue
            desired = []
            for node_id in ids:
                ys = [y[p] for p in neigh[node_id] if p in y]
                desired.append(sum(ys) / len(ys) if ys else y[node_id])
            cursor: float | None = None
            for i, node_id in enumerate(ids):
                if cursor is None:
                    cursor = desired[i]
                else:
                    gap = _height(nodes[ids[i - 1]]) / 2 + row_gap + _height(nodes[node_id]) / 2
                    cursor = max(desired[i], cursor + gap)
                y[node_id] = cursor
            shift = sum(desired) / len(desired) - sum(y[i] for i in ids) / len(ids)
            for node_id in ids:
                y[node_id] += shift


def _lane_count(intervals: list[tuple[float, float]]) -> int:
    """竖直走线至少要几条车道。

    就是区间图上的首次适应染色，按起点排序即最优。前端 `routing.ts` 用的是
    同一套贪心，所以两边算出来的车道数一致——否则走廊留窄了，线还是会叠。
    """
    lanes: list[float] = []
    for start, end in sorted((min(a, b), max(a, b)) for a, b in intervals):
        for i, lane_end in enumerate(lanes):
            # 首尾相接（共用扇出点）不算冲突：那正是一条总线的样子。
            # 真叠在一起（重叠超过 TOLERANCE）才换一条道。
            if start >= lane_end - LANE_OVERLAP_TOLERANCE:
                lanes[i] = max(lane_end, end)
                break
        else:
            lanes.append(end)
    return len(lanes)


def _corridor_width(
    boundary: int, rank: dict[str, int], edges: list, y: dict[str, float],
) -> float:
    """第 boundary 层和第 boundary+1 层之间要留多宽。

    穿过这条走廊的边在竖直方向最多重叠几条，就至少要几条车道。
    """
    intervals = [
        (y[e.source], y[e.target])
        for e in edges
        if rank[e.source] <= boundary < rank[e.target]
    ]
    lanes = _lane_count(intervals)
    if lanes <= 1:
        return CORRIDOR_MIN
    width = STUB * 2 + LANE * (lanes - 1) + 28
    return float(min(CORRIDOR_MAX, max(CORRIDOR_MIN, width)))


def auto_layout(
    spec: GraphSpec, *, x_gap: int | None = None, y_gap: int | None = None,
) -> GraphSpec:
    """给整张图重新分配坐标，返回同一个 spec（原地改坐标）。

    `x_gap` / `y_gap` 是旧接口留下的旋钮，现在只当"走廊最窄多少、同列最少隔
    多少"用：实际间距由拓扑算出来的车道需求决定，不再是固定 300/150。
    """
    if not spec.nodes:
        return spec

    nodes = spec.node_map()
    edges = _dedupe_edges(spec, nodes)
    # 自环不参与分层：它不跨层，留着只会让"自己的前驱是自己"这种噪音渗进排序
    edges = [e for e in edges if e.source != e.target]

    back = _back_edges(nodes, edges)
    rank = _ranks(nodes, edges, back)

    preds: dict[str, list[str]] = defaultdict(list)
    succs: dict[str, list[str]] = defaultdict(list)
    for e in edges:
        preds[e.target].append(e.source)
        succs[e.source].append(e.target)

    orders = _initial_orders(spec, nodes, rank, edges)
    orders = _minimize_crossings(orders, rank, edges, preds, succs)
    _apply_handle_order(orders, spec, nodes, rank)

    row_gap = float(y_gap) if y_gap else MIN_ROW_GAP
    corridor_min = max(CORRIDOR_MIN, float(x_gap) - NODE_W) if x_gap else CORRIDOR_MIN

    y = _stack(orders, nodes, row_gap)
    _relax(y, orders, nodes, preds, succs, row_gap)

    # 每个节点都有层，也就都在这两个表里；.get 的兜底是防"层算漏了"这种
    # 不该发生的情况——真漏了也把节点摆在图末尾，而不是让它掉在原点压住别人
    x: dict[str, float] = {}
    cursor = float(X_ORIGIN)
    for r, ids in enumerate(orders):
        for node_id in ids:
            x[node_id] = cursor
        if r < len(orders) - 1:
            width = _corridor_width(r, rank, edges, y)
            cursor += NODE_W + max(width, corridor_min)

    for node in spec.nodes:
        node.position.x = round(x.get(node.id, cursor), 1)
        node.position.y = round(y.get(node.id, 0.0), 1)
    return spec
