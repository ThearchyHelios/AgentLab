"""数字推演的重算：在一张口径卡上改几个输入的值、排除查询快照里的几行，算出指标会变成多少。

推演永远不是出具。这里只算数：不读库、不写工件、不追加事件。接口（api/whatif.py）把这次运行封存范围内取得到的
工件交进来（Sources），结果原样交回给点开证据面板的人，运行记录和报告一个字都不动。

口径卡怎么算的，这里就怎么算：同一条原式、同一个求值器（expressions.eval_expression），取整用口径卡的同一个函数
（metrics.round_metric，只取整浮点），渲染走 evidence.render_metric。重算前先把每个输入当时的值还原出来，卡上记着的
就用卡上的：

- 标量输入（数、文本、布尔、空值）：卡上记的 value 就是当时代进去的值。cell() 取的格、agent 的单格字段和数组里的
  一个元素也是标量，它们落在查询快照的哪一行记在 locator 里：排除了那一行，这个输入就当缺失，指标标「排除后无法计算」
- 容器输入卡上只记了「[12 项]」这样的大概（metrics._brief），完整值另找：
  - agent 的数组字段：agent 节点封存的产出里的 data；第 k 个元素是快照的第 a + k 行（locator.rows），排除行就去掉对应的元素
  - nodes.* / input.*：那个节点在口径卡执行之前最后一次的产出工件
  - vars.*：有 value_artifact（口径卡当时另存的完整值）才还原得出来；老运行照实说当时没有保存
  - 容器本身是一份查询结果（带 artifact 的 {columns, rows}，或者和某份快照的 rows 一模一样的列表）：排除行就去掉那几行
- 遮罩列取来的输入不给值、也不拿来推演：改一个输入、排除一行，新值和原值一比就能算出遮住的那一格。vars.res.rows[0][2]
  这种按位置取自查询结果的标量没有定位，顺着取值链认出是哪一列（positional_cell，证据面板的指标步骤用同一个认法）
- 落在读不出来（文件不在、与哈希不一致）的查询快照上的输入同样不给值、不推演：那一格遮没遮核对不了，宁可多遮

有的指标不论改什么都推演不了（_Plan.blocked）：卡上没记全输入、用到了遮罩列、最外层的输入还原不了（容器的完整值
找不回来、所在的快照读不出来）。这种不改动也照实交回 unsupported / masked 和原因，输入一个都不让改、不给行段：
界面一打开就只写原因，不写「无变化」，也不摆改了、勾了也推演不出来的输入框和勾选框。

同一张口径卡的指标不能互相引用（算这张卡时卡上的值还不存在），所以只有用到了被改的输入、被排除的行的指标需要重算，
别的原样交回。排除行时有两种例外，都只能说不知道（unsupported），不能报「无变化」：
- 指标的某个输入对不上任何快照（完整值找不回来的容器、加工过的结果、按位置从结果里取的一格、由查询结果算出来的数），
  它可能就是从被排除了行的那份结果来的，重算不了。写它的节点在那份快照交回之前就跑完了的，影响不到它，照常不算
- 决定取哪一格的容器（cell(nodes.q, len(vars.res.rows) - 1, …) 里的 vars.res.rows）少了几行：取到的是哪一格说不准。
  这种容器也就不给可勾的行段（勾了只能说不知道）；决定取哪一格的单格照给，勾掉它确定是排除后无法计算
给了新值的以新值为准：只嵌在给了新值的引用里的输入（cell(…) 那一格给了新值，它行号里的 vars.res.rows）取到哪一格
已无关紧要，它那一行排除了、它说不准受不受影响都不碍事（_Plan.moot）。
"""
from __future__ import annotations

import ast
import copy
import json
import math
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from app.core.errors import describe_exception
from app.engine.evidence import DEFAULT_FORMAT, MISSING, RenderError, is_number, iter_units, render_metric, render_number
from app.engine.expressions import (
    ExpressionError,
    _cell_call,
    _chain_root,
    eval_expression,
    leaf_refs,
    parse_expression,
    substitute,
)
from app.engine.nodes.metrics import _REF_HEAD, _brief, round_metric

NO_LINEAGE = "这个指标生成于系统升级前，口径卡没有记录输入，不能推演"
NO_FULL_VALUE = "当时没有保存这个输入的完整值"
NOT_RESTORABLE = "这类输入当时没有保存完整值"
MASKED_INPUT = "这一列在数据源中设置了遮罩，不能用于推演"
MASKED_TABLE = "这份查询结果里有设置了遮罩的列，不能用于推演"
MASKED_METRIC = "这个指标用到了设置了遮罩的列，不能推演"
SELECTOR = "这个输入决定取哪一行、哪一项，修改它需要重新取数，不能推演"
EXCLUDED = "排除后无法计算"
UNREADABLE = "查询快照无法读取"
TAMPERED = "查询快照与哈希不一致，疑似被修改"
#: 不能改、也没有行可勾的输入（_fixed）
FIXED_CONTAINER = "列表或对象不能直接修改，也没有对应的查询结果可以排除行"
NOT_NUMBER = "不是数值，不能修改"
#: 排除了行，却说不准这个输入受不受影响（_doubt）
DOUBT = "排除行对 {path} 的影响无法确定：{why}"
DOUBT_UNMATCHED = "它对不上任何查询快照，可能是由查询结果加工而来"
DOUBT_DERIVED = "它不是直接取自查询结果的格，可能由查询结果算出"
DOUBT_SELECTOR = "它决定取哪一行、哪一项，排除行后需要重新取数"
NOT_FINITE = "计算结果不是有限的数值（溢出或非数），无法推演"

#: 卡上给容器记的大概（metrics._brief）：「[12 项]」「{3 个字段}」
_BRIEF = re.compile(r"^(?:\[\d+ 项\]|\{\d+ 个字段\})$")
#: 重算时最外层引用换成的占位名。不在 EXPRESSION_ROOTS 里，原式自己写不出这种名字
_PLACEHOLDER = "__whatif_{}"


class WhatIfError(ValueError):
    """推演请求本身不合法：路径不存在、值不是数、行号越界、工件不属于这次运行……接口回 422，消息原样给人看。"""


@dataclass(frozen=True)
class Changes:
    """一次推演的改动：inputs 是输入路径 → 新值；exclude 是查询快照的工件 id → 要排除的行号（从 0 数）。"""

    inputs: Mapping[str, Any] = field(default_factory=dict)
    exclude: Mapping[str, frozenset[int]] = field(default_factory=dict)


@dataclass
class Sources:
    """还原输入要用到的东西，由调用方从这次运行的封存范围里准备好。

    - load：工件 id → 内容。只认封存范围内的工件，范围外的、文件不在的返回 None，内容和哈希对不上抛 ValueError
      （artifact_store.load 的约定）
    - queries：封存范围内的查询快照，按在事件里第一次出现的顺序。排除行只认这些快照；容器按内容对快照时也按这个顺序
    - outputs：节点 id → 口径卡执行之前它最后一次的产出工件（nodes.*、input.* 的容器、agent 的数组字段从这里还原）
    - masked：查询快照 → 要遮的列（小写）：查询当时记下的加数据源现在设的。只用到 get，调用方可以给一个用到哪份
      才算哪份的映射（快照读不出来时照样按台账记的数据源算）
    - returned：查询快照 → 它第一次交回时的事件序号；finished：节点 → 口径卡执行之前它最后一次跑完的事件序号。
      排除行只可能影响在那份快照交回之后才算出来的值（_doubt）。不知道的一律当可能受影响
    """

    load: Callable[[str], Any]
    queries: tuple[str, ...] = ()
    outputs: Mapping[str, str] = field(default_factory=dict)
    masked: Mapping[str, frozenset[str]] = field(default_factory=dict)
    returned: Mapping[str, int] = field(default_factory=dict)
    finished: Mapping[str, int] = field(default_factory=dict)


class _Store:
    """一次推演里取过的工件：同一份快照、产出只读一次。get 给 (内容, 问题)，问题是 None、missing 或 tampered。"""

    def __init__(self, sources: Sources) -> None:
        self.sources = sources
        self._memo: dict[str, tuple[Any, str | None]] = {}

    def get(self, artifact: Any) -> tuple[Any, str | None]:
        key = str(artifact or "")
        if key not in self._memo:
            try:
                content = self.sources.load(key) if key else None
                self._memo[key] = (content, None if content is not None else "missing")
            except ValueError:
                self._memo[key] = (None, "tampered")
        return self._memo[key]

    def snapshot(self, artifact: Any) -> dict[str, Any] | None:
        content, _ = self.get(artifact)
        return content if isinstance(content, dict) and isinstance(content.get("rows"), list) else None

    def output(self, node_id: Any) -> tuple[Any, str | None]:
        artifact = self.sources.outputs.get(str(node_id)) if node_id else None
        return self.get(artifact) if artifact else (None, "missing")


@dataclass
class _Input:
    item: dict[str, Any]
    path: str
    #: scalar：卡上记的就是当时的值；container：卡上只记了大概，value 是另找来的完整值
    shape: str = "scalar"
    value: Any = None
    #: 完整值找不回来的原因（只有容器会有）
    problem: str | None = None
    #: 落在哪份查询快照上：单格是 row，数组字段是一段行 rows，table 是整份结果（或它的 rows）
    snapshot: str | None = None
    row: int | None = None
    rows: tuple[int, int] | None = None
    table: bool = False
    masked: bool = False
    #: 落在的查询快照读不出来（文件不在、与哈希不一致）：遮没遮核对不了，值不给，也不拿来推演
    unreadable: bool = False
    #: 出现在别的输入的下标、cell() 的行列里：它决定取哪一格，改它要重新取数
    selector: bool = False


@dataclass
class _Plan:
    metric: dict[str, Any]
    tree: ast.Expression | None = None
    #: 最外层的引用，按出现顺序：重算时换成占位名的就是这些
    tops: list[str] = field(default_factory=list)
    inputs: dict[str, _Input] = field(default_factory=dict)
    problem: str | None = None
    #: 嵌在别的引用里的输入 → 包着它的最外层引用（_refs）
    within: dict[str, set[str]] = field(default_factory=dict)

    def settled(self, path: str, changes: Changes) -> bool:
        """包着这个输入的最外层引用都给了新值：它决定取哪一格已无关紧要，那几个引用以新值为准。"""
        outer = self.within.get(path)
        return bool(outer) and all(top in changes.inputs for top in outer)

    def moot(self, path: str, changes: Changes) -> bool:
        """这个输入这次用不上：只嵌在别的引用里，包着它的又都给了新值。它那一行排除了、它说不准受不受影响，都碍不着
        重算。它自己也是最外层引用的不算：那一处照常拿它的值。"""
        return path not in self.tops and self.settled(path, changes)

    @property
    def blocked(self) -> tuple[str, str] | None:
        """整个指标推演不了（不论改什么）：(status, 原因)，推演得了的是 None。

        卡上没记全输入、表达式有误；用到了遮罩列（哪怕只是嵌在下标里的一格）；最外层的输入还原不了——容器的完整值
        找不回来，或者所在的查询快照读不出来。嵌在下标里的输入还原不了不碍事：它取到的那一格的值卡上记着。
        遮罩排在还原不了前面：同一格既遮着、快照又读不出来时，说遮罩（快照修好了它照样遮着）。
        """
        if self.problem:
            return "unsupported", self.problem
        if any(inp.masked for inp in self.inputs.values()):
            return "masked", MASKED_METRIC
        for path in self.tops:
            if problem := self.inputs[path].problem:
                return "unsupported", f"无法推演：{path}（{problem}）"
        return None


# --------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------


def whatif(card: dict[str, Any], changes: Changes, sources: Sources, *, metric: str | None = None) -> dict[str, Any]:
    """在一张口径卡上推演：{metrics: [每个指标的旧值、新值、输入…], unsupported: [不能用于推演的输入和原因],
    maybe_changed: 有没有指标可能因这次改动而变}。

    metric 给了就只推演这一个指标。改动不合法时抛 WhatIfError，一个指标都不算。没碰到改动的指标不重算，
    原值原样交回（status: unchanged）；整个推演不了的指标（_Plan.blocked）不论有没有改动都交回 unsupported / masked
    和原因。unsupported 不论有没有改动都列全（遮罩的、还原不了的、不能改也没有行可勾的；整个指标推演不了的另有一条
    input 为空的），界面一打开就能说清哪些输入不能动。

    maybe_changed 只给调用方决定要不要标下游口径卡：改动碰到了某个指标，它又算出来变了或者说不准（推演不了的指标
    新值不知道）。推演不了的指标 changed 一律为 None，只看它分不出「什么都没改」。
    """
    store = _Store(sources)
    metrics = [m for m in card.get("metrics") or [] if isinstance(m, dict) and m.get("id") is not None]
    if metric is not None:
        metrics = [m for m in metrics if str(m["id"]) == str(metric)]
        if not metrics:
            raise WhatIfError(f"口径卡中没有指标「{metric}」")
    plans = [_plan(m, store) for m in metrics]
    _check(changes, plans, store, metric=metric)
    outcomes = [_outcome(p, changes, sources) for p in plans]
    return {"metrics": [result for result, _ in outcomes], "unsupported": _unsupported(plans),
            "maybe_changed": any(moved for _, moved in outcomes)}


def recompute(metric: dict[str, Any], changes: Changes, sources: Sources) -> dict[str, Any]:
    """重算一个指标，不管改动碰没碰到它，也不校验改动。核对还原是否忠实时用：什么都不改，重算出来应当和卡上记的一样。"""
    return _outcome(_plan(metric, _Store(sources)), changes, sources, force=True)[0]


# --------------------------------------------------------------------------
# 还原输入
# --------------------------------------------------------------------------


def _plan(metric: dict[str, Any], store: _Store) -> _Plan:
    expr = str(metric.get("expression") or "").strip()
    items = metric.get("inputs")
    if not expr or not isinstance(items, list):
        return _Plan(metric, problem=NO_LINEAGE)
    try:
        tree, _, _ = parse_expression(expr)
    except ExpressionError as e:
        return _Plan(metric, problem=f"表达式有误（{e}）")
    tops, within = _refs(tree)
    remaining = _remaining(metric.get("substituted"), [*tops, *within])
    inputs: dict[str, _Input] = {}
    for item in items:
        if isinstance(item, dict) and isinstance(item.get("path"), str) and item["path"] not in inputs:
            inputs[item["path"]] = _restore(item, remaining, set(within), store)
    plan = _Plan(metric, tree, tops, inputs, within=within)
    if lost := [p for p in tops if p not in inputs]:
        plan.problem = f"口径卡缺少输入 {'、'.join(lost[:3])} 的记录，不能推演"
    return plan


def _refs(tree: ast.AST) -> tuple[list[str], dict[str, set[str]]]:
    """表达式引用的值：(最外层的引用, 嵌在别的引用里面的 → 包着它的最外层引用)。和 leaf_refs 按同一套规则认引用，
    只多分一下里外：vars.rows[vars.i] 里的 vars.i、cell(nodes.q, vars.r, 'gmv') 里的 vars.r 是嵌在里面的，它们决定
    取哪一格。嵌了几层的也记最外层那个：重算时只有最外层的引用拿值（_evaluate），给了新值的就是它们。"""
    top: list[str] = []
    within: dict[str, set[str]] = {}

    def add(node: ast.AST, outer: str | None) -> str:
        """记下这个引用，交回它里面的引用归谁：已经嵌在里面的归原来那个最外层引用，最外层的归它自己。"""
        path = ast.unparse(node)
        if outer is not None:
            within.setdefault(path, set()).add(outer)
            return outer
        if path not in top:
            top.append(path)
        return path

    def visit(node: ast.AST, outer: str | None) -> None:
        if _cell_call(node):
            owner = add(node, outer)
            for arg in node.args[1:]:
                visit(arg, owner)
            return
        if isinstance(node, (ast.Attribute, ast.Subscript, ast.Name)) and _chain_root(node):
            owner = add(node, outer)
            cur: ast.AST = node
            while isinstance(cur, (ast.Attribute, ast.Subscript)):
                if isinstance(cur, ast.Subscript):
                    visit(cur.slice, owner)
                cur = cur.value
            return
        for child in ast.iter_child_nodes(node):
            visit(child, outer)

    visit(tree, None)
    return top, within


def _remaining(substituted: Any, paths: list[str]) -> set[str]:
    """代入式里还留着原路径的引用：没被代成字面量的，也就是容器（或卡上记的不是原值）。代入式读不出来就当全都留着。"""
    try:
        return set(leaf_refs(parse_expression(str(substituted))[0])) if substituted else set(paths)
    except ExpressionError:
        return set(paths)


def _restore(item: dict[str, Any], remaining: set[str], nested: set[str], store: _Store) -> _Input:
    path = item["path"]
    inp = _Input(item=item, path=path, value=item.get("value"), selector=path in nested)
    locator = item.get("locator") if isinstance(item.get("locator"), dict) else {}
    artifact = item.get("artifact") if isinstance(item.get("artifact"), str) and item.get("artifact") else None
    if item.get("via") in ("tool_cell", "agent_field") and artifact:
        if _row(locator.get("row")) is not None:
            inp.snapshot, inp.row = artifact, locator["row"]
        elif (span := _span(locator.get("rows"))) is not None:
            inp.snapshot, inp.rows = artifact, span
    # 卡上记的是「[3 项]」、代入式里又还留着原路径：当时的值是容器，卡上只记了大概。真是这几个字的短文本会被代成字面量
    if item.get("value_artifact") or (isinstance(inp.value, str) and _BRIEF.match(inp.value) and path in remaining):
        inp.shape = "container"
        inp.value, inp.problem = _full_value(item, store)
        if inp.problem is None and inp.rows is not None \
                and not (isinstance(inp.value, list) and len(inp.value) == inp.rows[1] - inp.rows[0] + 1):
            inp.snapshot, inp.rows = None, None      # 元素和行对不上号：不按行排除
        elif inp.problem is None and inp.snapshot is None:
            inp.snapshot = _table_of(inp.value, store)
            inp.table = inp.snapshot is not None
    if inp.snapshot is not None:
        _, trouble = store.get(inp.snapshot)
        if trouble or store.snapshot(inp.snapshot) is None:
            # 卡上记着这一格的值，可快照读不出来：这一列遮没遮核对不了（查询当时记下的遮罩在快照里），值不给，
            # 也不拿来推演。数据源现在设的遮罩照样认（_masked），认出来了就按遮罩说
            inp.unreadable = True
            inp.problem = inp.problem or (TAMPERED if trouble == "tampered" else UNREADABLE)
    inp.masked = _masked(inp, locator, store) or _positional_masked(inp, store)
    return inp


def _full_value(item: dict[str, Any], store: _Store) -> tuple[Any, str | None]:
    """容器输入的完整值：(值, 找不回来的原因)。"""
    path = item["path"]
    if ref := item.get("value_artifact"):
        content, problem = store.get(ref)
        if problem == "tampered":
            return None, "保存的完整值与哈希不一致，疑似被修改"
        if not isinstance(content, dict) or "value" not in content:
            return None, "无法读取保存的完整值"
        return content["value"], None
    locator = item.get("locator") if isinstance(item.get("locator"), dict) else {}
    if item.get("via") == "agent_field" and _span(locator.get("rows")) is not None and item.get("field"):
        # agent 的数组字段：assign_to 写进 vars 的、nodes.<agent>.data 下的，都是核对后的 data（值以快照为准）
        output, problem = store.output(item.get("node_id"))
        value = _dig(output.get("data") if isinstance(output, dict) else None, str(item["field"]))
        if isinstance(value, list):
            return value, None
        return None, _output_problem(item.get("node_id"), problem)
    head = _REF_HEAD.match(path)
    root = head.group(1) if head else None
    if root in ("nodes", "input"):
        node_id = head.group(2) if root == "nodes" else item.get("node_id")
        output, problem = store.output(node_id)
        if problem or output is None:
            return None, _output_problem(node_id, problem)
        if not _static(path):
            return None, "取值路径里有变量下标，无法从节点的产出中还原"
        try:
            value = eval_expression(path, {"nodes": {str(node_id): output}} if root == "nodes" else {"input": output})
        except Exception:  # noqa: BLE001 - 取不出来就是还原不了
            value = None
        if isinstance(value, (list, dict)):
            return value, None
        return None, f"无法从节点「{node_id}」当时的产出中还原这个输入"
    if root == "vars":
        return None, NO_FULL_VALUE
    return None, NOT_RESTORABLE


def _output_problem(node_id: Any, problem: str | None) -> str:
    if problem == "tampered":
        return f"节点「{node_id}」的产出与哈希不一致，疑似被修改"
    return f"无法读取节点「{node_id}」当时的产出"


def _table_of(value: Any, store: _Store) -> str | None:
    """容器是不是一份查询结果（或它的 rows）：带着封存范围内快照的 artifact，或者 rows 和某份快照一模一样。
    空的不算：空列表和哪份空结果都对得上，排除行也无从谈起。"""
    if isinstance(value, dict) and isinstance(value.get("rows"), list):
        artifact = value.get("artifact")
        if isinstance(artifact, str) and artifact in store.sources.queries:
            return artifact      # 快照读不读得出来由 _restore 核对
        rows = value["rows"]
    elif isinstance(value, list):
        rows = value
    else:
        return None
    if not rows:
        return None
    for artifact in store.sources.queries:
        snap = store.snapshot(artifact)
        if snap is not None and len(snap["rows"]) == len(rows) and snap["rows"] == rows:
            return artifact
    return None


def _masked(inp: _Input, locator: dict[str, Any], store: _Store) -> bool:
    """这个输入取自遮罩列：单格、数组字段看用到的那几列；整份查询结果只要有一列遮着就算。"""
    hidden = store.sources.masked.get(inp.snapshot) if inp.snapshot else None
    if not hidden:
        return False
    if inp.table:
        snap = store.snapshot(inp.snapshot) or {}
        return any(str(c).lower() in hidden for c in snap.get("columns") or [])
    columns = [locator.get("column")] if locator.get("column") else list((locator.get("columns") or {}).values())
    return any(isinstance(c, str) and c.lower() in hidden for c in columns)


def _positional_masked(inp: _Input, store: _Store) -> bool:
    """按位置取自查询结果的一格（vars.res.rows[0][2]）落在遮罩列上：卡上没记定位，顺着取值链找到那份结果、认出是哪一列。
    认不出来的（结果里没带快照的 id、取值链走不通）不算：和以前一样照常推演。"""
    if inp.snapshot is not None or inp.shape != "scalar" or inp.item.get("via") in ("tool_cell", "agent_field"):
        return False
    found = positional_cell(inp.path, inp.item.get("node_id"), lambda node_id: store.output(node_id)[0])
    hidden = store.sources.masked.get(found["artifact"]) if found and found["artifact"] else None
    return bool(hidden) and isinstance(found["column"], str) and found["column"].lower() in hidden


#: 取值链没走通
_GONE = object()


def positional_cell(path: str, node_id: Any, output: Callable[[Any], Any]) -> dict[str, Any] | None:
    """按位置取自一份查询结果的一格：vars.res.rows[0][2]、nodes.parse.rows[1]['amount'] 这种标量引用——查询结果经数据
    整形解析成对象之后按行号、列号（或列名）取。返回 {artifact, source, row, column}：artifact、source 是那份结果里带着的
    （数据源工具、合并查询交回的结果都带，没有的是 None），row 是从 0 数的行号，column 是列名。认不出来的返回 None。

    遮罩要按列认（证据面板的指标步骤、推演都用这个）：cell() 和 Agent 字段有定位，这种引用卡上只记了路径和值。

    output(节点 id) 交回那个节点当时的产出（读不出来给 None）。nodes.X 的根就是 X 的产出；vars.X 是写它的节点（输入记着
    的 node_id）交给变量的那个值——数据整形、合并查询交的是产出本身，Agent、LLM、代码节点交的是产出里的 data，工具节点
    交的是 JSON 文本，都试一遍，取值链走得通的那个就是。下标得是字面量：rows[vars.i] 要另取一个值才知道是哪一行，不认。
    """
    try:
        tree = ast.parse(path, mode="eval").body
    except SyntaxError:
        return None
    keys: list[Any] = []
    while isinstance(tree, (ast.Attribute, ast.Subscript)):
        if isinstance(tree, ast.Attribute):
            keys.append(tree.attr)
        else:
            key = _literal_key(tree.slice)
            if key is _GONE:
                return None
            keys.append(key)
        tree = tree.value
    keys.reverse()
    if not isinstance(tree, ast.Name) or tree.id not in ("vars", "nodes") or len(keys) < 4 \
            or not isinstance(keys[0], str):
        return None
    base = output(keys[0] if tree.id == "nodes" else node_id)
    candidates = [base]
    if isinstance(base, dict) and "data" in base:
        candidates.append(base["data"])
    if isinstance(base, str):
        try:
            candidates.append(json.loads(base))
        except ValueError:
            pass
    for value in candidates:
        if (found := _cell_in(value, keys[1:])) is not None:
            return found
    return None


def _literal_key(node: ast.AST) -> Any:
    """下标里的字面量：整数（含负数）或者文本。别的（变量、算式）交回 _GONE。"""
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, str)) and not isinstance(node.value, bool):
        return node.value
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub) and isinstance(node.operand, ast.Constant) \
            and isinstance(node.operand.value, int) and not isinstance(node.operand.value, bool):
        return -node.operand.value
    return _GONE


def _cell_in(value: Any, keys: list[Any]) -> dict[str, Any] | None:
    """沿取值链往下走，走到一份查询结果（带 rows 列表的对象）、剩下的正好是 rows[行][列] 时交回那一格。"""
    for i, key in enumerate(keys):
        if isinstance(value, dict) and isinstance(value.get("rows"), list) and len(keys) - i == 3 and key == "rows":
            return _cell_at(value, keys[i + 1], keys[i + 2])
        if isinstance(value, dict) and isinstance(key, str):
            value = value.get(key, _GONE)
        elif isinstance(value, list) and isinstance(key, int) and -len(value) <= key < len(value):
            value = value[key]
        else:
            return None
        if value is _GONE:
            return None
    return None


def _cell_at(result: dict[str, Any], row: Any, column: Any) -> dict[str, Any] | None:
    rows = result["rows"]
    if not isinstance(row, int) or not -len(rows) <= row < len(rows):
        return None
    row = row % len(rows)
    record = rows[row]
    columns = [str(c) for c in result.get("columns") or []]
    if isinstance(column, int) and isinstance(record, (list, tuple)) and -len(columns) <= column < len(columns):
        name: str | None = columns[column]
    elif isinstance(column, str) and (column in columns or (isinstance(record, dict) and column in record)):
        name = column
    else:
        name = None
    if name is None:
        return None
    artifact, source = result.get("artifact"), result.get("source")
    return {"artifact": artifact if isinstance(artifact, str) and artifact else None,
            "source": source if isinstance(source, str) and source else None, "row": row, "column": name}


def _static(path: str) -> bool:
    """取值路径的下标全是字面量（nodes.q.rows[0]），没有 nodes.q.rows[vars.i] 这种要另取一个值才知道是哪一项的。"""
    try:
        tree = ast.parse(path, mode="eval")
    except SyntaxError:
        return False
    for node in ast.walk(tree):
        if isinstance(node, ast.Subscript) and not isinstance(node.slice, ast.Constant) and not (
                isinstance(node.slice, ast.UnaryOp) and isinstance(node.slice.operand, ast.Constant)):
            return False
    return True


def _dig(data: Any, key: str) -> Any:
    for part in key.split("."):
        data = data.get(part) if isinstance(data, dict) else None
    return data


def _row(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _span(rows: Any) -> tuple[int, int] | None:
    if isinstance(rows, list) and len(rows) == 2 and all(_row(r) is not None for r in rows) and rows[0] <= rows[1]:
        return rows[0], rows[1]
    return None


# --------------------------------------------------------------------------
# 校验改动
# --------------------------------------------------------------------------


def _number(value: Any) -> bool:
    """算得动的数：bool 不算；无穷大、非数不算；浮点放不下的大整数（10 ** 400）也不算——math.isfinite 对它抛
    OverflowError，JSON 写得出来，界面却只能读成 Infinity。"""
    if not is_number(value):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _check(changes: Changes, plans: list[_Plan], store: _Store, *, metric: str | None) -> None:
    known: dict[str, list[_Input]] = {}
    for plan in plans:
        for path, inp in plan.inputs.items():
            known.setdefault(path, []).append(inp)
    for path, value in changes.inputs.items():
        found = known.get(path)
        if not found:
            raise WhatIfError(f"指标「{metric}」没有输入「{path}」" if metric is not None else f"口径卡里没有输入「{path}」")
        if not is_number(value):
            raise WhatIfError(f"输入「{path}」的新值需要是数值，当前为{_shown(value)}")
        if not _number(value):
            raise WhatIfError(f"输入「{path}」的新值超出可计算的范围，需要是有限的数值")
        if any(i.masked for i in found):
            raise WhatIfError(f"输入「{path}」取自设置了遮罩的列，不能用于推演")
        if unreadable := next((i for i in found if i.unreadable), None):
            raise WhatIfError(f"输入「{path}」所在的{unreadable.problem}，不能用于推演")
        if any(i.selector for i in found):
            raise WhatIfError(f"输入「{path}」决定取哪一行、哪一项，修改它需要重新取数，不能推演")
        if found[0].shape != "scalar":
            raise WhatIfError(f"输入「{path}」是列表或对象，不能直接修改，可以在查询结果里勾选要排除的行")
        if not (found[0].value is None or _number(found[0].value)):
            raise WhatIfError(f"输入「{path}」不是数值，不能修改")
    for artifact, rows in changes.exclude.items():
        if artifact not in store.sources.queries:
            raise WhatIfError(f"工件 {str(artifact)[:12]}… 不属于这次运行，或不在封存范围内，不能排除其中的行")
        _, problem = store.get(artifact)
        if problem == "tampered":
            raise WhatIfError("查询快照与哈希不一致，疑似被修改，不能用于推演")
        snap = store.snapshot(artifact)
        if snap is None:
            raise WhatIfError("无法读取查询快照，不能排除其中的行")
        total = len(snap["rows"])
        for row in sorted(rows):
            if not 0 <= row < total:
                raise WhatIfError(f"行号 {row} 超出范围：这份查询结果只有 {total} 行（行号从 0 开始）")


def _shown(value: Any) -> str:
    """不合法的新值写给人看：空值写「空值」，文本照原样，别的按 JSON 的写法——不露出 None、单引号这些 Python 写法。"""
    if value is None:
        return "空值"
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return f"「{text if len(text) <= 40 else text[:40] + '…'}」"


# --------------------------------------------------------------------------
# 重算
# --------------------------------------------------------------------------


def _apply(inp: _Input, changes: Changes) -> tuple[Any, list[int] | None, bool]:
    """改动之后这个输入的值：(值, 起了作用的被排除行, 是不是改了值)。给了新值的以新值为准，不看排除。"""
    if inp.path in changes.inputs:
        return changes.inputs[inp.path], None, True
    excluded = changes.exclude.get(inp.snapshot) if inp.snapshot else None
    if not excluded:
        return inp.value, None, False
    if inp.row is not None:
        return (None, [inp.row], False) if inp.row in excluded else (inp.value, None, False)
    if inp.rows is not None:
        # 数组字段只用到快照里的一段行：段外的行排除了也影响不到它
        first, last = inp.rows
        hit = sorted(r for r in excluded if first <= r <= last)
        if not hit:
            return inp.value, None, False
        if not isinstance(inp.value, list):
            # 完整值找不回来（inp.problem）：用到的行少了，它一定变了，变成什么不知道。照样算碰到了它，指标因这个
            # 输入还原不了而推演不了（_Plan.blocked），不会拿原值算出一个「无变化」
            return None, hit, False
        return [v for k, v in enumerate(inp.value) if first + k not in excluded], hit, False
    if inp.table:
        rows = inp.value["rows"] if isinstance(inp.value, dict) else inp.value
        hit = sorted(r for r in excluded if r < len(rows))
        if not hit:
            return inp.value, None, False
        kept = [r for i, r in enumerate(rows) if i not in excluded]
        return ({**inp.value, "rows": kept} if isinstance(inp.value, dict) else kept), hit, False
    return inp.value, None, False


def _outcome(plan: _Plan, changes: Changes, sources: Sources, *,
             force: bool = False) -> tuple[dict[str, Any], bool]:
    """(这个指标的推演结果, 它会不会因这次改动而变：碰到了它，又算出来变了或者说不准)。"""
    values: dict[str, Any] = {}
    notes: dict[str, list[int]] = {}
    touched = False
    for path, inp in plan.inputs.items():
        values[path], excluded, overridden = _apply(inp, changes)
        if excluded:
            notes[path] = excluded
        touched = touched or overridden or bool(excluded)
    doubt = _doubt(plan, changes, sources)
    # 说不准受不受影响的也算碰到了；卡上没记全输入的指标不知道它用了什么，有任何改动都说不准它变不变
    touched = touched or doubt is not None or (plan.problem is not None and bool(changes.inputs or changes.exclude))
    if blocked := plan.blocked:
        # 整个推演不了：不改动也照实说，不拿原值报「无变化」。碰没碰到它只决定下游要不要标
        status, reason = blocked
        result = _result(plan, status, None, reason, values, notes)
    elif not touched and not force:
        result = _result(plan, "unchanged", plan.metric.get("value"), None, values, notes)
    else:
        status, new, reason = _compute(plan, values, notes, changes, doubt)
        result = _result(plan, status, new, reason, values, notes)
    return result, touched and result["changed"] is not False


def _doubt(plan: _Plan, changes: Changes, sources: Sources) -> str | None:
    """排除了行，这个指标却有输入说不准受不受影响：返回原因；没排除行、或者输入都对得上号的返回 None。

    对得上号的输入（cell() 那一格、agent 的单格字段和数组字段、整份查询结果）排除行怎么影响它是确定的，_apply 照办；
    数组字段的完整值找不回来时也知道它用到哪几行，排除了那几行就算碰到了它（指标因它还原不了而推演不了）。
    别的输入——完整值找不回来的容器、对不上任何快照的容器（加工过的结果）、不是直接取自快照的数（按位置从整份结果里
    取的一格、transform 由查询结果算出来的数）——都可能来自被排除了行的那份结果，又重算不了，只能说不知道，不能拿
    原值算出一个「无变化」或者一个不对应任何一份数据的数。给了新值的以新值为准，只嵌在给了新值的引用里的也就无关紧要
    （_Plan.moot）；写它的节点在那份快照交回之前就跑完了的，影响不到它（_after）。
    """
    if not changes.exclude:
        return None
    for path, inp in plan.inputs.items():
        if path in changes.inputs or inp.snapshot is not None or plan.moot(path, changes):
            continue
        if any(_after(snapshot, inp, sources) for snapshot in changes.exclude):
            why = inp.problem or (DOUBT_UNMATCHED if inp.shape == "container" else DOUBT_DERIVED)
            return DOUBT.format(path=path, why=why)
    return None


def _after(snapshot: str, inp: _Input, sources: Sources) -> bool:
    """这个输入可能是在这份快照交回之后才算出来的：写它的节点不早于快照交回跑完（合并查询的快照就记在它自己跑完的
    那条事件里），或者两样有一样不知道。"""
    writer = inp.item.get("node_id")
    returned = sources.returned.get(snapshot)
    finished = sources.finished.get(str(writer)) if writer else None
    return returned is None or finished is None or finished >= returned


def _compute(plan: _Plan, values: dict[str, Any], notes: dict[str, list[int]], changes: Changes,
             doubt: str | None) -> tuple[str, Any, str | None]:
    """(status, 新值, 原因)。整个推演不了的（_Plan.blocked）到不了这里。排除后缺了的、说不准受不受影响的先说，
    都没有才求值。只嵌在给了新值的引用里的输入（_Plan.moot）不论排除了什么都不碍事：那几个引用以新值为准。"""
    hit = [(path, inp) for path, inp in plan.inputs.items()
           if path in notes and path not in changes.inputs and not plan.moot(path, changes)]
    for path, inp in hit:
        if inp.row is not None:
            return "excluded", None, f"{EXCLUDED}：{path} 所在的第 {inp.row + 1} 行已排除"
    for path, inp in hit:
        if inp.selector and not plan.settled(path, changes):
            # 嵌在下标、cell() 行列里的容器少了几行：外面那个引用取到的是哪一格跟着变，卡上记的值不再作数。
            # 包着它的引用都给了新值时，它只剩自己作为最外层引用的那一处（排除了哪几行是确定的），照常算
            return "unsupported", None, DOUBT.format(path=path, why=DOUBT_SELECTOR)
    if doubt:
        return "unsupported", None, doubt
    value, reason = _evaluate(plan, values)
    if reason is None and is_number(value) and not _number(value):
        # 溢出成无穷大、算出非数、乘出浮点放不下的整数：口径卡渲染成「—」，这里也说清算不出来（JSON 里也放不下）
        reason = NOT_FINITE
    return ("ok", value, None) if reason is None else ("error", None, reason)


class _Bind(ast.NodeTransformer):
    """把最外层的引用换成占位名，值从上下文里按名字取：和口径卡当时在完整的上下文里取到的是同一个值。
    认引用的规则和 expressions._Substitute 一样（取值链、整个 cell() 调用）。"""

    def __init__(self, names: dict[str, str]) -> None:
        self.names = names

    def _swap(self, node: ast.AST) -> ast.AST | None:
        name = self.names.get(ast.unparse(node))
        return None if name is None else ast.copy_location(ast.Name(id=name, ctx=ast.Load()), node)

    def visit_Call(self, node: ast.Call) -> ast.AST:
        if _cell_call(node) and (swapped := self._swap(node)) is not None:
            return swapped
        return self.generic_visit(node)

    def _chain(self, node: ast.AST) -> ast.AST:
        if _chain_root(node) is None:
            return self.generic_visit(node)
        swapped = self._swap(node)
        return swapped if swapped is not None else node

    visit_Attribute = visit_Subscript = visit_Name = _chain


def _evaluate(plan: _Plan, values: dict[str, Any]) -> tuple[Any, str | None]:
    """按原式求值，报错的说法和口径卡一致。取整和口径卡共用 metrics.round_metric（只取整浮点）。"""
    names = {path: _PLACEHOLDER.format(i) for i, path in enumerate(plan.tops)}
    bound = ast.fix_missing_locations(_Bind(names).visit(copy.deepcopy(plan.tree)))
    ctx = {names[p]: values.get(p) for p in plan.tops}
    try:
        value = eval_expression(ast.unparse(bound), ctx)
    except ExpressionError as e:
        return None, f"表达式有误（{e}）"
    except ZeroDivisionError:
        return None, "除以零"
    except Exception as e:  # noqa: BLE001 - None 参与算术、类型不对：和口径卡一样说人话
        if any(v is None for v in ctx.values()) or "NoneType" in str(e):
            return None, "有输入为空值，无法计算"
        if isinstance(e, TypeError):
            return None, "输入的类型不正确，无法计算"
        return None, f"无法计算（{describe_exception(e)}）"
    # 取整和口径卡是同一个函数：卡上怎么取整的，推演就怎么取整
    return round_metric(value, plan.metric.get("decimals")), None


# --------------------------------------------------------------------------
# 结果
# --------------------------------------------------------------------------

#: 这几种状态下新值是确定的（算出来了，或者确定算不出来）；unsupported、masked 是不知道
_KNOWN = frozenset({"unchanged", "ok", "excluded", "error"})


def _result(plan: _Plan, status: str, new: Any, reason: str | None, values: dict[str, Any],
            notes: dict[str, list[int]]) -> dict[str, Any]:
    metric = plan.metric
    old = metric.get("value")
    known = status in _KNOWN
    blocked = plan.blocked is not None
    delta, rendered_delta = _delta(metric, old, new) if known else (None, None)
    substituted = metric.get("substituted") if status == "unchanged" \
        else substitute(plan.tree, values) if status == "ok" and plan.tree is not None else None
    if any(inp.masked or inp.unreadable for inp in plan.inputs.values()):
        substituted = None      # 代入式里代进去的就是遮罩列（或者遮没遮核对不了）的那一格
    return {
        "id": metric.get("id"), "name": metric.get("name") or metric.get("id"), "unit": metric.get("unit") or "",
        "decimals": metric.get("decimals"), "format": metric.get("format") or DEFAULT_FORMAT,
        "old": _finite(old), "new": _finite(new), "delta": delta,
        "rendered_old": _render(metric, old), "rendered_new": _render(metric, new) if known else None,
        "rendered_delta": rendered_delta,
        "changed": (not _same(old, new)) if known else None,
        "status": status, "reason": reason, "substituted": substituted,
        "inputs": [_input_view(inp, values, notes, blocked=blocked) for inp in plan.inputs.values()],
    }


def _render(metric: dict[str, Any], value: Any, conv: str | None = None) -> str:
    """按卡上的格式渲染：和口径卡的 rendered、报告里的字同一套规则。渲染不出来的显示「—」（metrics._rendered 同样）。"""
    try:
        return render_metric({**metric, "value": value}, conv)
    except RenderError:
        return MISSING


def _finite(value: Any) -> Any:
    """无穷大、非数、浮点放不下的整数换成 None：老卡上可能记着（口径卡渲染成「—」），JSON 里放不下或界面读不准。"""
    return None if is_number(value) and not _number(value) else value


def _same(a: Any, b: Any) -> bool:
    if is_number(a) and is_number(b):
        return a == b or (isinstance(a, float) and isinstance(b, float) and math.isnan(a) and math.isnan(b))
    return a == b


def _delta(metric: dict[str, Any], old: Any, new: Any) -> tuple[Any, str | None]:
    """变化量和它的写法。比率、百分数的变化写「个百分点」：8.7% 到 19.0% 是多了 10.3 个百分点，不是 10.3%。"""
    if not (_number(old) and _number(new)):
        return None, None
    decimals = metric.get("decimals")
    places = decimals if isinstance(decimals, int) and not isinstance(decimals, bool) else None
    # 变化量按卡上的小数位取整，规则同指标本身（round_metric）：不然 8.7 到 19.0 会写成 +10.299999999999999
    delta = round_metric(new - old, places)
    if delta == 0:
        return delta, None
    fmt = metric.get("format") or DEFAULT_FORMAT
    unit = str(metric.get("unit") or "")
    try:
        if fmt == "percent_of_ratio":
            text = render_number(abs(delta), decimals=places, fmt=fmt).rstrip("%") + " 个百分点"
        elif unit in ("%", "％"):
            text = render_number(abs(delta), decimals=places, fmt=fmt) + " 个百分点"
        else:
            text = render_number(abs(delta), unit=unit, decimals=places, fmt=fmt)
    except RenderError:
        return delta, None
    return delta, ("+" if delta > 0 else "-") + text


def _kind(inp: _Input) -> str:
    if inp.shape == "container":
        return "container"
    value = inp.item.get("value")
    return "missing" if value is None else "bool" if isinstance(value, bool) \
        else "number" if is_number(value) else "text"


def _editable(inp: _Input, *, blocked: bool) -> bool:
    """推演里能改的输入：数值（或原本缺失）的标量，不是遮罩列、不决定取哪一格、还原得出。整个指标推演不了时一个都不算：
    改了也推演不出来。"""
    return not blocked and inp.shape == "scalar" and not inp.masked and not inp.selector and inp.problem is None \
        and (inp.value is None or _number(inp.value))


def _row_span(inp: _Input) -> tuple[int, int] | None:
    """这个输入实际用到的快照行（含两端）：单格一行、数组字段一段、整份结果全部。排除别的行影响不到它，界面据此只让
    这几行可勾。遮罩的、还原不了的（完整值找不回来、快照读不出来，都记在 problem 里）不给：排除它们的行什么也推演
    不出来。决定取哪一格的数组字段、整份结果也不给：少了几行，外面那个引用取到哪一格说不准（_compute），勾了只能
    说不知道，「不支持推演的输入」里又说它不能用。决定取哪一格的单格照给：勾掉它，确定是排除后无法计算。"""
    if inp.snapshot is None or inp.masked or inp.problem is not None:
        return None
    if inp.row is not None:
        return inp.row, inp.row
    if inp.selector:
        return None
    if inp.rows is not None:
        return inp.rows
    rows = inp.value.get("rows") if isinstance(inp.value, dict) else inp.value
    return (0, len(rows) - 1) if inp.table and isinstance(rows, list) and rows else None


def _input_view(inp: _Input, values: dict[str, Any], notes: dict[str, list[int]], *, blocked: bool) -> dict[str, Any]:
    """一个输入在推演里的样子：原值、新值、排除了哪几行、用到快照的哪几行（row_span）。遮罩列取来的、落在读不出来的
    快照上的不给值。容器写大概（「[12 项]」）。整个指标推演不了时（blocked）一个都不让改、不给行段。"""
    item = inp.item
    span = None if blocked else _row_span(inp)
    view: dict[str, Any] = {"path": inp.path, "via": item.get("via"), "node_id": item.get("node_id"),
                            **({"artifact": inp.snapshot} if inp.snapshot else {}),
                            **({"locator": item["locator"]} if isinstance(item.get("locator"), dict) else {}),
                            **({"row_span": list(span)} if span else {}),
                            "kind": _kind(inp), "editable": _editable(inp, blocked=blocked)}
    if inp.masked:
        return {**view, "old": None, "new": None, "changed": False, "masked": True}
    if inp.unreadable and inp.shape == "scalar":
        return {**view, "old": None, "new": None, "changed": False, "withheld": True}
    if inp.shape == "container":
        old = _outline(inp.value, inp.table) if inp.problem is None else item.get("value")
        new = _outline(values.get(inp.path), inp.table) if inp.problem is None else None
        # 完整值找不回来的数组排除了它用到的行：少了几项是确定的，只是新的样子不知道（new 为空）
        changed = not _same(old, new) if inp.problem is None else inp.path in notes
    else:
        old, new = _finite(item.get("value")), _finite(values.get(inp.path))
        changed = not _same(old, new)
    view.update(old=old, new=new, changed=changed)
    if inp.path in notes:
        view["excluded_rows"] = notes[inp.path]
    return view


def _outline(value: Any, table: bool) -> Any:
    """容器的大概。整份查询结果写行数（「[12 项]」），排除了几行看得出来；别的照卡上的写法。"""
    if table and isinstance(value, dict) and isinstance(value.get("rows"), list):
        return _brief(value["rows"])
    return _brief(value) if isinstance(value, (list, dict)) else value


def _unsupported(plans: list[_Plan]) -> list[dict[str, Any]]:
    """不能用于推演的输入和原因：遮罩的、完整值找不回来的（含快照读不出来的）、决定取哪一格的、既不能改也没有行可勾的。

    整个指标推演不了的（_Plan.blocked）先有一条 input 为空的，原因和指标上的一样：界面据此只写原因，不画输入框、
    勾选框。之后只列卡住它的输入（遮罩的、还原不了的），别的输入能不能改已无从谈起；卡本身没记输入的只有这一条。
    """
    out: list[dict[str, Any]] = []
    for plan in plans:
        metric_id = plan.metric.get("id")
        if blocked := plan.blocked:
            out.append({"metric": metric_id, "input": None, "reason": blocked[1]})
        if plan.problem:
            continue
        for inp in plan.inputs.values():
            if blocked and not (inp.masked or inp.problem):
                continue
            reason = (MASKED_TABLE if inp.table else MASKED_INPUT) if inp.masked \
                else f"无法推演：{inp.problem}" if inp.problem \
                else SELECTOR if inp.selector else _fixed(inp)
            if reason:
                out.append({"metric": metric_id, "input": inp.path, "reason": reason})
    return out


def _fixed(inp: _Input) -> str | None:
    """还原得出、没遮罩，却既不能改值、也没有行可勾的输入：说清为什么，不让人对着一个改不了的值干看。"""
    if inp.shape == "container":
        return None if _row_span(inp) is not None else FIXED_CONTAINER
    value = inp.item.get("value")
    return None if value is None or is_number(value) else NOT_NUMBER


def substituted_without(metric: dict[str, Any], paths: Iterable[str], token: str) -> str | None:
    """卡上的代入式，只把 paths 这几个输入代成 token（证据面板给取自遮罩列的输入写「已遮罩」），别的照卡上代。

    按卡上记的输入值重新代一遍：先核对一个都不换时代出来的和卡上记的一字不差，对不上（卡上记的不是原值、老卡的写法）
    就整条不给，交回 None——宁可不显示代入式，也不能让遮着的值从对不上的那一处漏出来。卡上没代成字面量的（容器、太长的
    文本）照旧留原路径。嵌在别的引用里的输入不单独换：外面那个引用代成了它的值，里面那一格本来就不出现。
    """
    expr, recorded = metric.get("expression"), metric.get("substituted")
    if not isinstance(expr, str) or not isinstance(recorded, str):
        return None
    try:
        tree, _, _ = parse_expression(expr)
        kept = set(leaf_refs(parse_expression(recorded)[0]))
    except ExpressionError:
        return None
    values = {str(i["path"]): i.get("value") for i in metric.get("inputs") or []
              if isinstance(i, dict) and isinstance(i.get("path"), str) and i["path"] not in kept}
    if substitute(tree, values) != recorded:
        return None
    hidden = set(paths)
    swapped = ast.fix_missing_locations(_Bind({p: token for p in hidden}).visit(copy.deepcopy(tree)))
    return substitute(swapped, {p: v for p, v in values.items() if p not in hidden})


# --------------------------------------------------------------------------
# 报告里引用的地方、下游口径卡
# --------------------------------------------------------------------------


def report_segments(reports: Iterable[tuple[str, dict[str, Any]]], *, card_node: str, card_artifact: str,
                    results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """报告里引用了这些指标的片段，推演之后会写成什么：[{report, unit, sid, metric, rendered_old, rendered_new, changed}]。

    只认指向这张卡这一份指标集的引用（目录条目的 node_id、artifact）：循环里同一张卡算过几次，报告引用的是哪一次
    就是哪一次。新值按片段自己的换算（|万、|pct……）渲染，和报告里的字同一套规则。指标推演不了（unsupported、masked）
    时 rendered_new、changed 为 None：不知道会变成什么。缺输入、当时就显示「—」的引用也列上：推演里补上了值，它会变。
    """
    by_id = {str(r.get("id")): r for r in results}
    out: list[dict[str, Any]] = []
    for report_node, doc in reports:
        catalog = doc.get("catalog") or {}
        for _, unit in iter_units(doc):
            for seg in unit.get("segments") or []:
                cite = seg.get("cite") or {}
                entry = catalog.get(cite.get("alias")) or {}
                if entry.get("kind") != "metric" or entry.get("node_id") != card_node \
                        or entry.get("artifact") != card_artifact or not _cites(seg, cite, entry):
                    continue
                result = by_id.get(str((entry.get("locator") or {}).get("metric")))
                if result is None:
                    continue
                new = None if result.get("rendered_new") is None else _render(result, result.get("new"), cite.get("conv"))
                out.append({"report": report_node, "unit": unit.get("id"), "sid": seg.get("id"), "metric": result["id"],
                            "rendered_old": seg.get("text"), "rendered_new": new,
                            "changed": None if new is None else new != seg.get("text")})
    return out


def _cites(seg: dict[str, Any], cite: dict[str, Any], entry: dict[str, Any]) -> bool:
    """片段引用了这个指标：解析成功的引用，或者指标在、只是这次缺输入没有值（显示「—」）的那种。"""
    if cite.get("kind") != "metric":
        return False
    if seg.get("state") == "deterministic" and cite.get("status") == "resolved":
        return True
    return cite.get("status") == "unresolved" and entry.get("value") is None and entry.get("status") == "missing_input"


def downstream_cards(cards: Iterable[tuple[str, str, dict[str, Any]]], node_id: str, *,
                     edges: Iterable[tuple[Any, Any]] = ()) -> list[dict[str, Any]]:
    """引用了这张口径卡的下游口径卡：[{node_id, artifact, caliber, version, metrics, direct}]。

    只标「可能受影响」，不重算：下游卡怎么用这张卡的值（取第几个指标、和别的什么一起算）要整条链重放才说得准。
    认引用看下游指标的输入从哪个节点来（node_id，或者 nodes.<卡> 这样的路径）：直接来自这张卡的标 direct。
    经别的卡间接用到的也标；给了画布上的边（edges）时，从这张卡顺着边走得到的节点也算上游——下游卡经 transform 之类
    的中间节点用到卡上的值（输入写的是 vars.t.g，来自中间节点），同样可能受影响。循环的回边会让循环里的节点都走得到，
    宁可多标。同一张卡执行过几次的按最后一次。
    """
    latest: dict[str, tuple[str, dict[str, Any]]] = {}
    for nid, artifact, card in cards:
        latest[str(nid)] = (artifact, card)
    latest.pop(str(node_id), None)

    def uses(card: dict[str, Any], upstream: set[str]) -> list[str]:
        found = []
        for metric in card.get("metrics") or []:
            for item in (metric.get("inputs") or []) if isinstance(metric, dict) else []:
                head = _REF_HEAD.match(str(item.get("path") or "")) if isinstance(item, dict) else None
                origins = {str(item.get("node_id") or "")} | ({head.group(2)} if head and head.group(1) == "nodes" else set())
                if origins & upstream:
                    found.append(metric.get("id"))
                    break
        return found

    upstream = {str(node_id)} | _reach(edges, str(node_id))
    flagged: dict[str, dict[str, Any]] = {}
    grew = True
    while grew:
        # 标出来的卡本身也成了上游：经它间接用到的再标一轮，直到没有新的
        grew = False
        for nid, (artifact, card) in latest.items():
            if nid in flagged or not (metrics := uses(card, upstream)):
                continue
            flagged[nid] = {"node_id": nid, "artifact": artifact, "caliber": card.get("caliber") or "",
                            "version": card.get("caliber_version") or "", "metrics": metrics,
                            "direct": bool(uses(card, {str(node_id)}))}
            upstream.add(nid)
            grew = True
    return [flagged[nid] for nid in latest if nid in flagged]


def _reach(edges: Iterable[tuple[Any, Any]], start: str) -> set[str]:
    """从 start 顺着边走得到的节点（不含 start 自己，除非绕回来）。"""
    after: dict[str, list[str]] = {}
    for a, b in edges:
        after.setdefault(str(a), []).append(str(b))
    seen: set[str] = set()
    todo = [start]
    while todo:
        for nxt in after.get(todo.pop(), []):
            if nxt not in seen:
                seen.add(nxt)
                todo.append(nxt)
    return seen
