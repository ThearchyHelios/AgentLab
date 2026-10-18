"""口径卡的输入追没追到原始数据：一处认定，三处用。

「原始数据」是查询快照里的格（调用工具节点、合并查询交回的结果；Agent 按出处交来的字段落在的那一格）和运行输入（发起
运行的人填的）。证据面板上的「有出处」说的是另一件事：这个数是口径卡按登记的表达式确定性算出来的、拿代入式复算一致。
两件事分开说，这里只管前一件：

- 运行时（nodes/metrics._input_of）：「只是把调用工具节点的结果原样按 JSON 解析」的数据整形节点（parsed_tool）不改动任何
  一格，vars.<x>.rows[i][j]（row_cell）确定地对到那份查询快照的第 i 行第 j 列，和 cell() 一样按快照核对，记下穿过的整形
  节点（through）；转抄另一张口径卡的指标（nodes.<卡>.metrics[i].value，metric_copy：对账卡、召回卡的「口径」那一项就是
  这么写的）跨卡往下追——接上那个指标自己的出处，那个指标的输入都追到了原始数据，这一项才算追到（upstream.traced）；
  转抄的转抄一层层接着追，最多 MAX_COPY_DEPTH 层（循环里抄自己上一轮的卡每一轮多一层，到了上限就算没追到）；
- 证据面板（api/evidence 的指标步骤）：卡上记的输入里追不到原始数据的（untraced），照实说出处追到哪个节点为止；
- 应用检查（apps/lint）、受管级发布检查（governance）：按 static_stops 静态地认（只有图、没有运行），追不到的给提示。
  转抄同一张图里另一张口径卡的指标，和运行时一样跨卡按那个指标的式子接着认（最多 MAX_COPY_DEPTH 层）；被抄的卡按
  caliber_from 固定在别处的版本时，静态看不到那一版的图，照旧停在那张卡。

读子工作流出口的字段（nodes.<子图>.output.<字段>，docs/design/orchestration-round/00-总方案.md §2）跨子图往下追，三处同一套：
- 运行时：子图跑完时按出口模板认每个字段的出处（multi.run_subgraph 的 provenance：模板整串就是一个取值链的字段，在子图
  的状态里按口径卡认输入的同一套认；出自子图运行输入的再按子工作流节点的「输入」模板往父图追一层），父图口径卡读这个
  字段时接上它（metrics._subgraph_field），卡上记的就是子图里那一格的出处（via、artifact、locator、status），另记
  subgraph、field——traced / untraced 照常认；子图里追不到原始数据的，父图照样追不到、照样降档；
- 证据面板：卡上记的节点 id 按卡所在的那一层认（api/evidence 的 _scoped），一路给到子图里的那一格；
- 静态：按子工作流节点固定的那一版图（lint_process.subgraph_spec，调用方用 with_subgraphs 放进来）认出口字段的模板，接着
  在子图里认；子图的运行输入按子工作流节点的「输入」模板回到父图认。最多 MAX_SUB_DEPTH 层（和运行时的嵌套上限一样），
  看不到子图的（没放进来、读不到）照旧停在子工作流节点。
"""
from __future__ import annotations

import ast
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from app.engine.expressions import ExpressionError, cell_parts, leaf_refs, parse_expression

#: 整形节点的模板整串就是一个节点的全部产出：{{ nodes.fetch }}（不带过滤器、前后不带别的字）
_WHOLE_NODE = re.compile(r"^\s*\{\{\s*nodes\.([A-Za-z0-9_\-]+)\s*\}\}\s*$")
_REF_HEAD = re.compile(r"^(vars|nodes|input)\.([A-Za-z_一-鿿][\w\-一-鿿]*)(.*)$", re.S)
#: 落在查询快照一格上的取值方式（cell() 取的格、Agent 按出处交来的字段），和 engine/recompute、dual_check 同一组
SNAPSHOT_VIAS = ("tool_cell", "agent_field")
#: 核对得了快照的状态：一致，或者对不上（对不上另有说法，但出处是追到了的）
_CHECKED = ("verified", "mismatch")
#: 直接交回查询结果（带快照）的节点：cell() 的第一个参数指到它们就是快照
_TABLE_NODES = ("tool", "merge")


def parsed_tool(node_type: Any, config: Mapping[str, Any] | None, type_of: Callable[[str], Any]) -> str | None:
    """这个节点只是把调用工具节点的结果原样按 JSON 解析：数据整形、按 JSON 解析（mode=json）、模板整串就是
    {{ nodes.<id> }}、那个节点是调用工具节点。是的话交回那个调用工具节点的 id，不是返回 None。

    只认这一种：表达式、模板拼出来的结构即使带着 rows 和快照 id，行也可能被筛过、换过顺序，第 i 行对不到快照的第 i 行。"""
    cfg = config or {}
    if str(node_type) != "transform" or cfg.get("mode") != "json":
        return None
    m = _WHOLE_NODE.match(str(cfg.get("template") or ""))
    if not m or str(type_of(m.group(1)) or "") != "tool":
        return None
    return m.group(1)


@dataclass(frozen=True)
class RowCell:
    """按位置取的一格：<root>.<name>.rows[row][column]。root 是 vars 或 nodes，row 是从 0 数的行号，column 是列号或列名。"""

    root: str
    name: str
    row: int
    column: int | str

    @property
    def table(self) -> str:
        """那份结果本身的取值链：vars.q"""
        return f"{self.root}.{self.name}"


def row_cell(path: str) -> RowCell | None:
    """路径正好是 vars.q.rows[0][0]、nodes.parse.rows[1]['gmv'] 这种按位置取一格的样子时拆开；行号是非负整数字面量，
    列是非负整数或文本字面量。别的样子（负数下标、下标是变量、多走了一层）返回 None。"""
    try:
        tree = ast.parse(path, mode="eval").body
    except SyntaxError:
        return None
    if not isinstance(tree, ast.Subscript) or not isinstance(tree.value, ast.Subscript):
        return None
    column, row, rows = tree.slice, tree.value.slice, tree.value.value
    if not (isinstance(rows, ast.Attribute) and rows.attr == "rows" and isinstance(rows.value, ast.Attribute)
            and isinstance(rows.value.value, ast.Name) and rows.value.value.id in ("vars", "nodes")):
        return None
    if not (isinstance(row, ast.Constant) and type(row.value) is int and row.value >= 0):
        return None
    if not (isinstance(column, ast.Constant) and ((type(column.value) is int and column.value >= 0)
                                                   or (isinstance(column.value, str) and column.value))):
        return None
    return RowCell(rows.value.value.id, rows.value.attr, row.value, column.value)


#: 转抄另一张口径卡的指标时跨卡往下追的层数上限
MAX_COPY_DEPTH = 8


def metric_copy(path: str) -> tuple[str, str, int] | None:
    """路径正好是 nodes.<卡>.metrics[i].value、vars.<卡的 assign_to>.metrics[i].value 这种转抄一个指标的值的样子时拆开：
    (root, 名字, 第几个指标)。下标是非负整数字面量；别的样子（整张卡、清单文字、指标的名称单位、下标是变量）为 None。"""
    try:
        tree = ast.parse(path, mode="eval").body
    except SyntaxError:
        return None
    if not (isinstance(tree, ast.Attribute) and tree.attr == "value" and isinstance(tree.value, ast.Subscript)):
        return None
    index, metrics = tree.value.slice, tree.value.value
    if not (isinstance(metrics, ast.Attribute) and metrics.attr == "metrics"
            and isinstance(metrics.value, ast.Attribute) and isinstance(metrics.value.value, ast.Name)
            and metrics.value.value.id in ("vars", "nodes")):
        return None
    if not (isinstance(index, ast.Constant) and type(index.value) is int and index.value >= 0):
        return None
    return metrics.value.value.id, metrics.value.attr, index.value


def traced(item: Mapping[str, Any]) -> bool:
    """卡上记的一个输入追没追到原始数据：落在查询快照的一格上、核对得了快照（cell() 取的格、穿过 JSON 解析按位置取的格、
    Agent 按出处交来的字段），或者是运行输入；转抄另一张口径卡的指标的，那个指标的输入都追到了（运行时跨卡追好记在
    upstream 里：traced 为真、那张卡的指标集落了工件）。"""
    via = item.get("via")
    if via in SNAPSHOT_VIAS:
        return bool(item.get("artifact")) and item.get("status") in _CHECKED
    if via == "metric":
        up = item.get("upstream")
        return isinstance(up, Mapping) and up.get("traced") is True and bool(up.get("artifact"))
    return via == "input"


def untraced(inputs: Iterable[Any]) -> list[dict[str, Any]]:
    """卡上记的输入里追不到原始数据的。没有值的不算（缺输入另有说法：值都没有，谈不上追到哪里）。"""
    return [item for item in inputs
            if isinstance(item, dict) and item.get("value") is not None and not traced(item)]


# --------------------------------------------------------------------------
# 静态地认（只有图）
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Stop:
    """一个追不到原始数据的输入：路径和停在哪个节点（认不出是哪个节点时为 None）。"""

    path: str
    node_id: str | None


#: 出口字段的模板整串就是一个取值链：{{ nodes.card.metrics[0].value }}（不带过滤器、不调函数、前后不带别的字）
_WHOLE_REF = re.compile(r"^\s*\{\{\s*((?:nodes|vars|input)\.[^{}|()]+?)\s*\}\}\s*$", re.S)


def whole_ref(template: Any) -> str | None:
    """模板整串就是一个取值链时交回它（成果字段的值就是那个值本身，渲染只是转成文字）；别的写法为 None：拼了别的字、
    过了过滤器的值，对不上那一格的值。子工作流出口的出处，运行时（multi._output_provenance）和静态（_exit_template）
    都按这一个判断认。"""
    m = _WHOLE_REF.match(template) if isinstance(template, str) else None
    return m.group(1).strip() if m else None


#: 静态地跨子工作流往下追的层数上限：和运行时子工作流的嵌套上限（multi._MAX_DEPTH）一样
MAX_SUB_DEPTH = 3


@dataclass(frozen=True)
class _Scope:
    """静态地认时所在的那一层图：节点 id → {type, title?, config}。是子工作流时记着父图那一层和它在父图里的节点 id（子图的
    运行输入按那个子工作流节点的「输入」模板回到父图认）。"""

    nodes: Mapping[str, Mapping[str, Any]]
    parent: "_Scope | None" = None
    via: str | None = None
    level: int = 0


def static_stops(nodes: Mapping[str, Mapping[str, Any]], expression: str, *, _depth: int = 0) -> list[Stop]:
    """静态地认一个指标表达式里追不到原始数据的输入。nodes 是节点 id → {type, config}（apps/tree.node_map 的形状）。

    和运行时同一套认法，只是不看值：cell() 的第一个参数是查询结果（调用工具、合并查询的产出，或原样解析它的整形节点）、
    按位置取的格穿过原样解析的整形节点、运行输入、开了按出处核对字段的 Agent 交来的字段，算追到了；转抄同一张图里
    另一张口径卡的指标，按那个指标的式子接着认（它的输入都追到了才算）；读子工作流出口的字段，按那一版子图的出口模板
    接着认（_through_sub）；别的（表达式整形、沙箱代码、模型调用、转抄固定在别处版本的口径卡、看不到的子图……）停在
    写它的那个节点。表达式写错了的不在这里报（画布的校验报）。"""
    return _stops(_Scope(nodes), expression, _depth)


def _stops(scope: _Scope, expression: str, depth: int) -> list[Stop]:
    try:
        tree = parse_expression(expression)[0]
    except ExpressionError:
        return []
    out: list[Stop] = []
    for path in leaf_refs(tree):
        stop = _static_stop(scope, path, depth)
        if stop is not None:
            out.append(stop)
    return out


def _copied_metric(nodes: Mapping[str, Mapping[str, Any]], path: str) -> tuple[str, str] | None:
    """path 转抄同一张图里一张口径卡的第 i 个指标时：(写它的卡, 那个指标的式子)。卡按 caliber_from 固定在别处版本的、
    认不出是哪张卡的、下标越界的为 None。"""
    copy = metric_copy(path)
    if copy is None:
        return None
    root, name, index = copy
    writers = _writers(nodes, f"{root}.{name}")
    if len(writers) != 1:
        return None
    card = nodes.get(writers[0]) or {}
    cfg = card.get("config") or {}
    if card.get("type") != "metrics" or isinstance(cfg.get("caliber_from"), dict):
        return None
    metrics = cfg.get("metrics") or []
    if index >= len(metrics) or not isinstance(metrics[index], dict) or not metrics[index].get("expression"):
        return None
    return writers[0], str(metrics[index]["expression"])


def _static_stop(scope: _Scope, path: str, depth: int = 0) -> Stop | None:
    nodes = scope.nodes
    type_of = lambda node_id: (nodes.get(node_id) or {}).get("type")  # noqa: E731
    if (copied := _copied_metric(nodes, path)) is not None:
        card_id, expression = copied
        if depth < MAX_COPY_DEPTH and not _stops(scope, expression, depth + 1):
            return None
        return Stop(path, card_id)
    if (parts := cell_parts(path)) is not None:
        writers = _writers(nodes, parts[0])
        bad = [w for w in writers if not _table(nodes, w, type_of) and not _sub_table(scope, w, parts[0])]
        return Stop(path, bad[0]) if bad or not writers else None
    if (at := row_cell(path)) is not None:
        writers = _writers(nodes, at.table)
        node = nodes.get(writers[0]) if len(writers) == 1 else None
        if node is not None and parsed_tool(node.get("type"), node.get("config"), type_of):
            return None
    head = _REF_HEAD.match(path)
    if head and head.group(1) == "input":
        return None if _from_parent(scope, head.group(2), depth) else Stop(path, scope.via)
    writers = _writers(nodes, path)
    if not writers:
        return Stop(path, None)
    field = head.group(3).lstrip(".") if head else ""
    for writer in writers:
        node = nodes[writer]
        if node.get("type") == "input":
            if head and head.group(1) == "vars" and not _from_parent(scope, head.group(2), depth):
                return Stop(path, scope.via)
            continue
        if node.get("type") == "agent" and _cited(node, head.group(1) if head else "", field):
            continue
        if node.get("type") == "subgraph" and head and _through_sub(scope, writer, head, depth):
            continue
        return Stop(path, writer)
    return None


def _from_parent(scope: _Scope, name: str, depth: int) -> bool:
    """运行输入的一个字段算不算原始数据：最外层的是（发起运行的人填的）；子工作流的运行输入是父图按子工作流节点的「输入」
    模板渲染出来的，那个模板整串就是一个取值链、在父图里追得到原始数据，才算（同运行时 multi._lifted）。"""
    if scope.parent is None:
        return True
    templates = ((scope.parent.nodes.get(scope.via or "") or {}).get("config") or {}).get("input")
    path = whole_ref(templates.get(name)) if isinstance(templates, dict) and name else None
    return path is not None and _static_stop(scope.parent, path, depth + 1) is None


def _sub_scope(scope: _Scope, node_id: str) -> _Scope | None:
    """子工作流节点 node_id 引用的那一版图（lint_process.subgraph_spec 读好放进来的），看不到、超过层数上限的为 None。"""
    if scope.level >= MAX_SUB_DEPTH:
        return None
    from app.engine.lint_process import subgraph_spec

    sub = subgraph_spec((scope.nodes.get(node_id) or {}).get("config") or {})
    if sub is None:
        return None
    nodes = {n.id: {"type": str(n.type), "title": n.title, "config": n.config} for n in sub.nodes}
    return _Scope(nodes, parent=scope, via=node_id, level=scope.level + 1)


def _exit_template(sub: _Scope, key: str) -> str | None:
    """子图出口字段 key 的模板整串所写的取值链（whole_ref）；没有这个字段、几个成果节点写法不一样、不是整串一个
    取值链的为 None。"""
    found = {whole_ref(f.get("value", "")) for node in sub.nodes.values() if node.get("type") == "output"
             for f in (node.get("config") or {}).get("fields") or [] if isinstance(f, dict) and f.get("name") == key}
    return found.pop() if len(found) == 1 else None


def _sub_key(head: re.Match[str], node: Mapping[str, Any]) -> str | None:
    """nodes.<子图>.output.<字段> / vars.<子图的 assign_to>.<字段> 读的出口字段名；不是正好一个字段的为 None。"""
    root, name, rest = head.group(1), head.group(2), head.group(3)
    key = re.sub(r"\[\s*['\"]([^'\"\]]+)['\"]\s*\]", r".\1", rest).strip(".")
    if root == "nodes":
        if not key.startswith("output."):
            return None
        key = key[len("output."):]
    elif root != "vars" or str((node.get("config") or {}).get("assign_to") or "").strip() != name:
        return None
    return key if key and "." not in key and "[" not in key else None


def _through_sub(scope: _Scope, writer: str, head: re.Match[str], depth: int) -> bool:
    """读子工作流出口的一个字段：那个字段在子图里追得到原始数据（出口模板整串就是一个取值链，接着在子图里认）。"""
    key = _sub_key(head, scope.nodes.get(writer) or {})
    sub = _sub_scope(scope, writer) if key else None
    inner = _exit_template(sub, key) if sub is not None and key else None
    return inner is not None and _static_stop(sub, inner, depth + 1) is None


def _sub_table(scope: _Scope, writer: str, source: str) -> bool:
    """cell() 的第一个参数是子工作流出口原样交出的一份查询结果（出口字段的模板整串就是 {{ nodes.<查询节点> }}）。"""
    head = _REF_HEAD.match(source)
    if not head or (scope.nodes.get(writer) or {}).get("type") != "subgraph":
        return False
    key = _sub_key(head, scope.nodes.get(writer) or {})
    sub = _sub_scope(scope, writer) if key else None
    inner = _exit_template(sub, key) if sub is not None and key else None
    if inner is None:
        return False
    writers = _writers(sub.nodes, inner)
    type_of = lambda node_id: (sub.nodes.get(node_id) or {}).get("type")  # noqa: E731
    return bool(writers) and all(_table(sub.nodes, w, type_of) or _sub_table(sub, w, inner) for w in writers)


def _writers(nodes: Mapping[str, Mapping[str, Any]], source: str) -> list[str]:
    """取值链的根是谁写的：nodes.X 是 X；vars.X 是 assign_to 为 X 的节点（入口节点声明了字段 X 的也算）。"""
    head = _REF_HEAD.match(source)
    if not head:
        return []
    root, name = head.group(1), head.group(2)
    if root == "nodes":
        return [name] if name in nodes else []
    if root == "input":
        return [n for n, node in nodes.items() if node.get("type") == "input"]
    out = []
    for node_id, node in nodes.items():
        cfg = node.get("config") or {}
        if str(cfg.get("assign_to") or "").strip() == name:
            out.append(node_id)
        elif node.get("type") == "input" and any(isinstance(f, dict) and str(f.get("name") or "").strip() == name
                                                 for f in cfg.get("fields") or []):
            out.append(node_id)
    return out


def _table(nodes: Mapping[str, Mapping[str, Any]], node_id: str, type_of: Callable[[str], Any]) -> bool:
    node = nodes.get(node_id) or {}
    return node.get("type") in _TABLE_NODES or parsed_tool(node.get("type"), node.get("config"), type_of) is not None


def _cited(node: Mapping[str, Any], root: str, field: str) -> bool:
    """开了按出处核对字段的 Agent 交来的字段（vars.X.字段，或 nodes.<agent>.data.字段）：逐格核对过快照，是原始数据。"""
    cfg = node.get("config") or {}
    if not (cfg.get("output_schema") and cfg.get("cite_fields") is True):
        return False
    return root == "vars" or field.startswith("data.") or field.startswith("data[")
