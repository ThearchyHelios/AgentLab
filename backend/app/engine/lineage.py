"""口径卡的输入追没追到原始数据：一处认定，三处用。

「原始数据」是查询快照里的格（调用工具节点、合并查询交回的结果；Agent 按出处交来的字段落在的那一格）和运行输入（发起
运行的人填的）。证据面板上的「有出处」说的是另一件事：这个数是口径卡按登记的表达式确定性算出来的、拿代入式复算一致。
两件事分开说，这里只管前一件：

- 运行时（nodes/metrics._input_of）：「只是把调用工具节点的结果原样按 JSON 解析」的数据整形节点（parsed_tool）不改动任何
  一格，vars.<x>.rows[i][j]（row_cell）确定地对到那份查询快照的第 i 行第 j 列，和 cell() 一样按快照核对，记下穿过的整形
  节点（through）；
- 证据面板（api/evidence 的指标步骤）：卡上记的输入里追不到原始数据的（untraced），照实说出处追到哪个节点为止；
- 应用检查（apps/lint）：受管级应用里绑定的指标，按 static_stops 静态地认（只有图、没有运行），追不到的给提示。
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


def traced(item: Mapping[str, Any]) -> bool:
    """卡上记的一个输入追没追到原始数据：落在查询快照的一格上、核对得了快照（cell() 取的格、穿过 JSON 解析按位置取的格、
    Agent 按出处交来的字段），或者是运行输入。"""
    via = item.get("via")
    if via in SNAPSHOT_VIAS:
        return bool(item.get("artifact")) and item.get("status") in _CHECKED
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


def static_stops(nodes: Mapping[str, Mapping[str, Any]], expression: str) -> list[Stop]:
    """静态地认一个指标表达式里追不到原始数据的输入。nodes 是节点 id → {type, config}（apps/tree.node_map 的形状）。

    和运行时同一套认法，只是不看值：cell() 的第一个参数是查询结果（调用工具、合并查询的产出，或原样解析它的整形节点）、
    按位置取的格穿过原样解析的整形节点、运行输入、开了按出处核对字段的 Agent 交来的字段，算追到了；别的（表达式整形、
    沙箱代码、模型调用、转抄别的口径卡……）停在写它的那个节点。表达式写错了的不在这里报（画布的校验报）。"""
    try:
        tree = parse_expression(expression)[0]
    except ExpressionError:
        return []
    out: list[Stop] = []
    for path in leaf_refs(tree):
        stop = _static_stop(nodes, path)
        if stop is not None:
            out.append(stop)
    return out


def _static_stop(nodes: Mapping[str, Mapping[str, Any]], path: str) -> Stop | None:
    type_of = lambda node_id: (nodes.get(node_id) or {}).get("type")  # noqa: E731
    if (parts := cell_parts(path)) is not None:
        writers = _writers(nodes, parts[0])
        bad = [w for w in writers if not _table(nodes, w, type_of)]
        return Stop(path, bad[0]) if bad or not writers else None
    if (at := row_cell(path)) is not None:
        writers = _writers(nodes, at.table)
        node = nodes.get(writers[0]) if len(writers) == 1 else None
        if node is not None and parsed_tool(node.get("type"), node.get("config"), type_of):
            return None
    head = _REF_HEAD.match(path)
    if head and head.group(1) == "input":
        return None
    writers = _writers(nodes, path)
    if not writers:
        return Stop(path, None)
    field = head.group(3).lstrip(".") if head else ""
    for writer in writers:
        node = nodes[writer]
        if node.get("type") == "input":
            continue
        if node.get("type") == "agent" and _cited(node, head.group(1) if head else "", field):
            continue
        return Stop(path, writer)
    return None


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
