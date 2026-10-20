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
  这种按位置取自查询结果的标量没有定位，顺着取值链认出是哪一列（positional_cell，证据面板的指标步骤用同一个认法）；
  下标不是字面量的（rows[vars.i][2]）按列认，整行（rows[0]）按整行的列认（wide）。
  转抄上游口径卡的输入（nodes.<卡>.metrics[0].value、nodes.<卡>.text）按那张卡认：调用方在指标依赖图上一次认好
  （masked_closure，证据面板、写作目录、可验证报告同一个函数），交进来（Sources.copied），转抄的那张卡在这次运行的任何一次
  执行里有一个指标取自遮罩格就不给。
  指标本身的原值给不给（withheld_value，证据面板指标步骤的值同一个判断）：用到了遮罩列的一格、一行，原值写「已遮罩」——
  指标的值可能就是那一格，输入里拿掉了、指标上照给，等于没遮；最外层的输入遮没遮核对不了，写「无法核对」。整份结果
  当输入（len(vars.res.rows)）、认不出出处的输入（数据整形算出来再赋给变量的一格），按值认：值里有这次运行查到过的
  遮罩值（Sources.secrets）才不给，数行数的照给。认不出出处、值就是遮罩值的输入同样不给值、不推演
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
#: 转抄上游口径卡的输入，转抄的是取自遮罩列的指标（Sources.copied，和证据面板同一张依赖图）
MASKED_COPY = "转抄的口径卡指标取自设置了遮罩的列，不能用于推演"
#: 认不出出处的输入（数据整形算出来再赋给变量的一格），值和这次运行查到过的遮罩值一样（按值认，Sources.secrets）
MASKED_VALUE_INPUT = "这个输入的值与设置了遮罩的列中的值相同，不能用于推演"
MASKED_VALUE_METRIC = "这个指标的值或输入与设置了遮罩的列中的值相同，不能推演"
#: 按位置取自查询结果的一格（vars.res.rows[0][2]）遮没遮核对不了：取值链上节点的产出读不出来，或者那份结果没存快照、
#: 只带着数据源名（查询当时记下的遮罩在快照里）。和读不出来的快照一样宁可多遮（_positional）
POSITION_UNREADABLE = "查询结果无法读取，核对不了这一格是否取自设置了遮罩的列"
POSITION_NO_SNAPSHOT = "查询结果没有保存快照，核对不了这一格是否取自设置了遮罩的列"
#: recompute（双盲复算的预检、盲值）里整个指标重算不了的说法：同 _Plan.blocked，只是不说「推演」——那里不是推演
NO_LINEAGE_PLAIN = "这个指标生成于系统升级前，口径卡没有记录输入"
MASKED_PLAIN = "这个指标用到了设置了遮罩的列"
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
    - secrets：这次运行查到过的遮罩值（masking._Secrets，没有为 None），用到时才取：认不出出处的输入、整份结果当输入的
      指标，值给不给按它认（withheld_value）。没给的（双盲复算）不按值认
    - copied：卡上转抄上游口径卡的输入给不给值，指标在卡上的位置 → 输入路径 → (种类, 原因)，种类同 Judged.copied。
      调用方按证据面板同一张指标依赖图认好（api/evidence.py 的 copied_inputs → masked_closure）；没给的（双盲复算）当
      认不出出处
    """

    load: Callable[[str], Any]
    queries: tuple[str, ...] = ()
    outputs: Mapping[str, str] = field(default_factory=dict)
    masked: Mapping[str, frozenset[str]] = field(default_factory=dict)
    returned: Mapping[str, int] = field(default_factory=dict)
    finished: Mapping[str, int] = field(default_factory=dict)
    secrets: Callable[[], Any] | None = None
    copied: Mapping[int, Mapping[str, tuple[str | None, str | None]]] = field(default_factory=dict)


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
    #: 认得出取自哪份查询结果（落在快照上，或者顺着取值链认出来）。认不出的按值认遮罩（withheld_value）
    traced: bool = False
    #: 认不出出处，值里却有这次运行查到过的遮罩值（按值认的遮罩）
    by_value: bool = False
    #: 遮着的是一整份结果（或者值里有遮罩值的容器、整张转抄、读到的产出里有指标核对不了的口径卡）：整个指标推演不了，
    #: 可它不一定把遮罩值带进指标的值，指标的原值按值认（len(vars.res.rows) 照给）
    whole: bool = False
    #: 转抄上游口径卡（Sources.copied 里有它）：给不给按依赖图认好了，不再按取值链、按值认
    copy: bool = False

    @property
    def cell(self) -> bool:
        """取自遮罩列的一格、一行（或者值就是遮罩值的一格）：指标的值可能就是它，原值不给。"""
        return self.masked and not (self.table or self.whole)


@dataclass
class _Plan:
    metric: dict[str, Any]
    tree: ast.Expression | None = None
    #: 最外层的引用，按出现顺序：重算时换成占位名的就是这些
    tops: list[str] = field(default_factory=list)
    inputs: dict[str, _Input] = field(default_factory=dict)
    problem: str | None = None
    #: problem 不说「推演」的说法（recompute 给双盲复算用）；和 problem 一样时不填
    plain: str | None = None
    #: 嵌在别的引用里的输入 → 包着它的最外层引用（_refs）
    within: dict[str, set[str]] = field(default_factory=dict)
    #: 指标本身的原值给不给（withheld_value）：None 照给，masked 写「已遮罩」，unverified 写「无法核对」
    held: str | None = None

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

        卡上没记全输入、表达式有误；用到了遮罩列（哪怕只是嵌在下标里的一格），或者值里有这次运行查到过的遮罩值（按值认，
        指标的原值不给时也算：新值一减就是原值）；最外层的输入还原不了——容器的完整值找不回来，或者所在的查询快照读不
        出来。嵌在下标里的输入还原不了不碍事：它取到的那一格的值卡上记着。
        遮罩排在还原不了前面：同一格既遮着、快照又读不出来时，说遮罩（快照修好了它照样遮着）。
        """
        if self.problem:
            return "unsupported", self.problem
        masked = [inp for inp in self.inputs.values() if inp.masked]
        if masked or self.held == "masked":
            return "masked", MASKED_METRIC if any(not inp.by_value for inp in masked) else MASKED_VALUE_METRIC
        for path in self.tops:
            if problem := self.inputs[path].problem:
                return "unsupported", f"无法推演：{path}（{problem}）"
        return None

    @property
    def hides(self) -> bool:
        """指标的原值也不给（held，withheld_value 同一个判断）。成立时一定推演不了（blocked 不为 None），新值本来就是空的。"""
        return self.held is not None

    @property
    def stuck(self) -> str | None:
        """blocked 的原因换成不说「推演」的说法：recompute 交给双盲复算的预检、盲值，那里说「不能推演」驴唇不对马嘴。
        判断和先后与 blocked 一字不差，只换措辞；推演得了的是 None。"""
        blocked = self.blocked
        if blocked is None:
            return None
        if self.problem:
            return self.plain or self.problem
        if blocked[0] == "masked":
            return MASKED_PLAIN
        path = next(p for p in self.tops if self.inputs[p].problem)
        return f"输入 {path} 无法还原（{self.inputs[path].problem}）"


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
    metrics = [(i, m) for i, m in enumerate(card.get("metrics") or [])
               if isinstance(m, dict) and m.get("id") is not None]
    if metric is not None:
        metrics = [(i, m) for i, m in metrics if str(m["id"]) == str(metric)]
        if not metrics:
            raise WhatIfError(f"口径卡中没有指标「{metric}」")
    plans = [_plan(m, store, copied=sources.copied.get(i)) for i, m in metrics]
    _check(changes, plans, store, metric=metric)
    outcomes = [_outcome(p, changes, sources) for p in plans]
    return {"metrics": [result for result, _ in outcomes], "unsupported": _unsupported(plans),
            "maybe_changed": any(moved for _, moved in outcomes)}


def recompute(metric: dict[str, Any], changes: Changes, sources: Sources) -> dict[str, Any]:
    """重算一个指标，不管改动碰没碰到它，也不校验改动。核对还原是否忠实时用：什么都不改，重算出来应当和卡上记的一样。

    调用方是双盲复算（预检、按盲算的输入代回原式），不是推演：整个指标重算不了时原因不说「推演」（_Plan.stuck）。"""
    plan = _plan(metric, _Store(sources))
    result = _outcome(plan, changes, sources, force=True)[0]
    if (plain := plan.stuck) is not None:
        result["reason"] = plain
    return result


# --------------------------------------------------------------------------
# 还原输入
# --------------------------------------------------------------------------


def _plan(metric: dict[str, Any], store: _Store, *, copied: Mapping[str, tuple[str | None, str | None]] | None = None
          ) -> _Plan:
    expr = str(metric.get("expression") or "").strip()
    items = metric.get("inputs")
    if not expr or not isinstance(items, list):
        # 卡上没记输入（升级前的卡）：认不出出处，原值按值认
        return _Plan(metric, problem=NO_LINEAGE, plain=NO_LINEAGE_PLAIN,
                     held=withheld_value(metric, metric.get("value"), uncertain=True, secrets=store.sources.secrets))
    try:
        tree, _, _ = parse_expression(expr)
    except ExpressionError as e:
        return _Plan(metric, problem=f"表达式有误（{e}）",
                     held=withheld_value(metric, metric.get("value"), uncertain=True, secrets=store.sources.secrets))
    tops, within = _refs(tree)
    remaining = _remaining(metric.get("substituted"), [*tops, *within])
    inputs: dict[str, _Input] = {}
    for item in items:
        if isinstance(item, dict) and isinstance(item.get("path"), str) and item["path"] not in inputs:
            inputs[item["path"]] = _restore(item, remaining, set(within), store, (copied or {}).get(item["path"]))
    plan = _Plan(metric, tree, tops, inputs, within=within)
    if lost := [p for p in tops if p not in inputs]:
        plan.plain = f"口径卡缺少输入 {'、'.join(lost[:3])} 的记录"
        plan.problem = f"{plan.plain}，不能推演"
    # 指标的原值给不给：和证据面板的指标步骤同一个判断（withheld_value），输入按同一套分类交进去
    plan.held = withheld_value(
        metric, metric.get("value"), masked=[p for p, i in inputs.items() if i.cell],
        unverified=[p for p, i in inputs.items() if i.unreadable and not i.whole],
        uncertain=any((i.masked and not i.cell) or (i.unreadable and i.whole) or not i.traced for i in inputs.values()),
        secrets=store.sources.secrets)
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


def _restore(item: dict[str, Any], remaining: set[str], nested: set[str], store: _Store,
             copied: tuple[str | None, str | None] | None = None) -> _Input:
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
    if copied is not None:
        _copied(inp, *copied)
        return inp
    inp.masked = _masked(inp, locator, store)
    inp.traced = inp.snapshot is not None or (item.get("via") in ("tool_cell", "agent_field") and artifact is not None)
    if not inp.masked:
        _positional(inp, store)
    if not inp.masked and not inp.traced:
        _by_value(inp, store)
    return inp


def _copied(inp: _Input, kind: str | None, reason: str | None) -> None:
    """转抄上游口径卡的输入（Sources.copied，和证据面板同一张依赖图认的）：转抄的那张卡有指标取自遮罩格（哪种写法都一样，
    masked_closure 的 CopyEdge.card），不给值、不推演，指标的原值也不给；读到的上游指标核对不了的，同读不出来的快照，不给值、
    不推演（整张卡转抄的指标的原值另按值认）；照给的照常推演。认得出出处，不再按值认：以前只按值认，转抄来的值不是够长的
    号码就照给、还能改。"""
    inp.traced = inp.copy = True
    if kind == "masked":
        inp.masked = True
    elif kind in ("unverified", "table_unverified"):
        inp.unreadable, inp.whole = True, kind == "table_unverified"
        inp.problem = inp.problem or reason or CARD_UNREADABLE


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


def _positional(inp: _Input, store: _Store) -> None:
    """按位置取自查询结果的一格（vars.res.rows[0][2]）、一行（vars.res.rows[0]）：卡上没记定位，顺着取值链找到那份结果、
    认出是哪一列（input_origin，wide：下标不是字面量的按列认，整行按整行的列认）。

    落在遮罩列上的按遮罩（masked）。遮没遮核对不了的——取值链上节点的产出读不出来，或者那份结果没存快照、只带着数据源名
    ——和读不出来的快照同一个处理：值不给、也不拿来推演（unreadable，原因记在 problem），宁可多遮。认得出、可那份结果
    既没带快照的 id 也没带数据源名的（数据整形改过形状的结果）认不出出处，照常推演，见模块说明。整份结果（vars.res.rows）
    不在这里认：对得上快照的按内容认过了（_table_of），对不上的当认不出出处，指标的原值按值认（withheld_value）。"""
    if inp.snapshot is not None or inp.item.get("via") in ("tool_cell", "agent_field"):
        return
    origin = input_origin(inp.item, lambda node_id: store.output(node_id)[0], wide=True)
    if origin is None or origin.table:
        return
    inp.traced = True
    if origin.unverified is None and origin.artifact:
        hidden = store.sources.masked.get(origin.artifact)
        inp.masked = bool(hidden) and any(c.lower() in hidden for c in origin.columns)
        return
    if origin.unverified is not None or origin.source:
        inp.unreadable = True
        inp.problem = inp.problem or origin.unverified or POSITION_NO_SNAPSHOT


def _by_value(inp: _Input, store: _Store) -> None:
    """认不出出处的输入按值认遮罩（数据整形把 vars.res.rows[0][2] 算出来再赋给 vars.first_phone 这种，取值链认不出）：值里
    有这次运行查到过的遮罩值（Sources.secrets），就当它取自遮罩列，不给值、也不拿来推演。一格的值就是遮罩值的按一格算
    （指标的原值不给）；容器（还原出来的完整值）按整份结果算，指标的原值另按值认。运行输入（input.*）是发起运行的人自己
    填的，不认；完整值找不回来的容器只有卡上记的大概，认不了。"""
    secrets = store.sources.secrets
    value = inp.value if inp.problem is None else None
    if secrets is None or inp.path.startswith("input.") or not maybe_masked_value(value) \
            or not carries_masked(value, secrets()):
        return
    inp.masked = inp.by_value = True
    inp.whole = inp.shape == "container"


@dataclass(frozen=True)
class Origin:
    """一个输入取自哪份查询结果的哪几列（input_origin）。

    - artifact、source：那份查询结果的快照 id 和数据源名，结果里没带的是 None
    - columns：用到的列（列名照原样）。按位置取的一格、取值链上的产出读不出来时认不出是哪一列，为空；整行、整份结果、
      列号不是字面量的（wide）是那份结果的全部列
    - unverified：遮没遮核对不了的原因（取值链上节点的产出读不出来）；核对得了的是 None
    - table：取的是整份结果（vars.res.rows、vars.res，wide 才认）：它不一定把哪一格的值带进指标
    """

    artifact: str | None
    source: str | None
    columns: tuple[str, ...]
    unverified: str | None = None
    table: bool = False


def input_origin(item: dict[str, Any], output: Callable[[Any], Any], *, wide: bool = False) -> Origin | None:
    """卡上的一个输入取自哪份查询结果的哪几列，遮罩按列认时用（推演、证据面板的指标步骤、可验证报告是同一个认法）。

    cell() 的格、Agent 交来的字段按定位里的列认；vars.res.rows[0][2] 这种按位置取的一格顺着取值链认（positional_cell，
    output 同它）。不是取自查询结果的格（常量、运行输入、Agent 没按出处交的字段、加工过的数）是 None。

    wide（推演、证据面板的指标步骤）：取值链伸进查询结果、下标却不全是字面量的也认——rows[vars.i][2] 按第 2 列认，
    rows[0][vars.j] 按全部列认；整行 rows[0] 按整行的列认，整份结果 rows、vars.res 按全部列认、标 table。以前认不出，
    这一格的值就当可以给，指标的原值照给。可验证报告、双盲复算照旧只认字面量下标的一格（它们另有认不出时的处理）。"""
    if item.get("via") in ("tool_cell", "agent_field") and isinstance(item.get("artifact"), str) and item["artifact"]:
        locator = item.get("locator") if isinstance(item.get("locator"), dict) else {}
        columns = [locator.get("column")] if locator.get("column") else list((locator.get("columns") or {}).values())
        return Origin(item["artifact"], None, tuple(c for c in columns if isinstance(c, str) and c))
    found = positional_cell(str(item.get("path") or ""), item.get("node_id"), output, wide=wide)
    if found is None:
        return None
    if found.get("unreadable"):
        return Origin(None, None, (), POSITION_UNREADABLE)
    columns = tuple(found.get("columns") or ([found["column"]] if found.get("column") else []))
    return Origin(found["artifact"], found["source"], columns, table=bool(found.get("table")))


#: 取值链没走通
_GONE = object()
#: 下标不是字面量（rows[vars.i]、rows[len(vars.res.rows) - 1]）：wide 时认作「哪一行、哪一列说不准」
_ANY = object()

#: 口径卡产出里不带指标值的字段：取这些的输入不算转抄了指标的值
_CARD_PLAIN = frozenset({"kind", "caliber", "caliber_version", "artifact", "source"})
#: 一个指标里不带值的字段（名称、单位、表达式、显示格式、状态这类）。值、渲染、输入、代入式都带着值
_METRIC_PLAIN = frozenset({"id", "name", "unit", "expression", "decimals", "format", "status", "recompute_ok",
                           "incomplete", "incomplete_reason", "sql_check_failed", "sql_check_reason", "undefined",
                           "no_data", "no_data_reason", "no_data_sources"})
#: 转抄的上游口径卡当时的产出读不出来（文件不在、与哈希不一致、不知道是谁写的）：转抄来的是不是遮罩值核对不了
CARD_UNREADABLE = "转抄的口径卡结果无法读取，核对不了这个值是否取自设置了遮罩的列"


@dataclass(frozen=True)
class CardInput:
    """卡上的一个输入转抄了上游口径卡的产出（card_input）：node_id 是那张卡的节点，card 是它当时的产出（指标集加清单
    文字），picked 是这个输入带着值的那几个指标（在 card["metrics"] 里的位置）。whole：整张卡、整份指标列表、清单文字、
    下标不是字面量的一个指标——说不准把哪个指标的值带进来。给不给不按 picked 细分：那张卡在这次运行的任何一次执行里有一个
    指标不给，转抄它的输入就不给（masked_closure 的 CopyEdge.card）。

    unreadable：那张卡当时的产出现在读不出来，card 为 None、picked 为空，这里写原因。卡上记着这个输入转抄来了值（不是空的），
    算的时候产出是在的，现在读不出来只能是产出丢了或被改了：同按位置取的一格取值链上的产出读不出来（positional_cell 的
    unreadable），遮没遮核对不了，宁可多遮。以前当作认不出出处，只按出处认的写作目录、可验证报告照给，号码随卡出去了。"""

    node_id: str
    card: dict[str, Any] | None
    picked: tuple[int, ...]
    whole: bool
    unreadable: str | None = None


def card_input(item: dict[str, Any], output: Callable[[Any], Any]) -> CardInput | None:
    """卡上的一个输入是不是转抄上游口径卡的产出：nodes.<卡>.metrics[0].value、vars.<卡的 assign_to>.metrics[0].value、
    nodes.<卡>.text（清单文字逐条写着「指标（名称）= 值」）、整张卡。是的话交回那张卡和带着值的那几个指标，值给不给按
    那张卡认（masked_closure：证据面板、写作目录、推演、可验证报告同一个判断）：那张卡在这次运行里有一个指标取自遮罩格，
    转抄它的这个指标同样不给。

    output 同 input_origin：节点 id → 那个节点当时的产出；nodes.X 是 X 的产出，vars.X 是写它的节点（输入记着的 node_id）
    的产出——口径卡的 assign_to 交给变量的就是它的产出本身。产出不是指标集的（别的节点）交回 None；卡上记着这个输入出自
    口径卡（via 为 metric）、转抄来了值，产出却读不出来的交回 unreadable（见 CardInput）。只取口径名、版本、指标的名称、
    单位这类不带值的字段的，也交回 None。"""
    try:
        tree = ast.parse(str(item.get("path") or ""), mode="eval").body
    except SyntaxError:
        return None
    keys: list[Any] = []
    while isinstance(tree, (ast.Attribute, ast.Subscript)):
        if isinstance(tree, ast.Attribute):
            keys.append(tree.attr)
        else:
            key = _literal_key(tree.slice)
            keys.append(_ANY if key is _GONE else key)
        tree = tree.value
    keys.reverse()
    if not isinstance(tree, ast.Name) or tree.id not in ("vars", "nodes") or not keys or not isinstance(keys[0], str):
        return None
    node_id = keys[0] if tree.id == "nodes" else item.get("node_id")
    rest = keys[1:]
    whole = not rest or rest[0] == "text" or rest[0] is _ANY \
        or (rest[0] == "metrics" and (len(rest) == 1 or rest[1] is _ANY))
    plain = len(rest) > 2 and isinstance(rest[2], str) and rest[2] in _METRIC_PLAIN
    if not whole and (rest[0] != "metrics" or plain):
        # 口径名、版本、工件 id，指标的名称、单位这类，或者产出里本来没有的键：不带指标的值
        return None
    card = output(node_id) if node_id else None
    if not (isinstance(card, dict) and card.get("kind") == "metric_set" and isinstance(card.get("metrics"), list)):
        if node_id and item.get("via") == "metric" and item.get("value") is not None:
            return CardInput(str(node_id), None, (), whole, CARD_UNREADABLE)
        return None
    metrics = card["metrics"]
    if whole:
        return CardInput(str(node_id), card, tuple(range(len(metrics))), True)
    index = rest[1]
    if not isinstance(index, int) or not -len(metrics) <= index < len(metrics):
        return None
    return CardInput(str(node_id), card, (index % len(metrics),), False)


@dataclass(frozen=True)
class CopyEdge:
    """指标依赖图上的一条转抄边（masked_closure）：卡上路径为 path 的输入转抄了上游口径卡的产出，upstream 是它读到的那一次
    产出里带着值的那几个指标（键由调用方定，和 MetricLinks 的键同一套）。whole、unreadable 同 CardInput；读不出来的
    upstream 为空。

    card：转抄的那张卡（节点 id）。转抄的保守规则按整张卡认：这张卡在这次运行的任何一次执行里有一个指标不给（调用方把它每一
    次执行的每个指标都放进图里，MetricLinks.card 记着是哪张卡），转抄出来的这个输入就不给——不管下标是不是字面量、转抄的
    是一个指标、整张卡还是清单文字，也不管读到的是哪一次执行的产出。以前按读到的那一次产出、带着值的那几个指标细分：写作
    目录只有运行状态里最后一次的产出，循环里抄自己上一轮的卡最后一轮没碰号码列就照给，可它转抄来的正是第一轮的号码；整张
    卡、清单文字只按值认，由遮罩格算出来的数（年薪 = 月薪 × 12）原样照给。没给 card 的只看 upstream。"""

    path: str
    upstream: tuple[Any, ...]
    whole: bool
    unreadable: str | None = None
    card: Any = None


@dataclass
class MetricLinks:
    """指标依赖图上的一个指标（masked_closure 的输入）：metric 是卡上的那个指标（认表达式的最外层引用、值）；direct 是
    转抄以外的输入里不给值的，路径 → (种类, 遮罩列)，种类同 api/evidence.py 的 _Held（masked、table、unverified、value）；
    uncertain：有认不出出处、指标的值要按值认的输入；copies：转抄上游口径卡的输入；card：这个指标所在的口径卡（节点 id），
    转抄这张卡的边按它认（CopyEdge.card）。"""

    metric: dict[str, Any]
    direct: dict[str, tuple[str, tuple[str, ...]]] = field(default_factory=dict)
    uncertain: bool = False
    copies: list[CopyEdge] = field(default_factory=list)
    card: Any = None


@dataclass(frozen=True)
class Judged:
    """masked_closure 认出来的一个指标：word 是指标的值给不给（None 照给，masked 不给，unverified 核对不了，同
    withheld_value）；copied 是每个转抄的输入：路径 → (种类, 遮罩列, 原因)，种类 None 照给，masked 是转抄的那张卡有指标
    不给（哪种写法都一样），unverified 是读到的那几个指标核对不了，table_unverified 是整张卡转抄、读到的那次产出里有指标
    核对不了（同整份查询结果：指标的值另按值认），原因只在上游产出读不出来时有；columns 是让它不给的遮罩列，转抄它的输入上
    写这几列；card 同 MetricLinks.card，之后再认的指标转抄这张卡时按它算进去。"""

    word: str | None
    copied: Mapping[str, tuple[str | None, tuple[str, ...], str | None]] = field(default_factory=dict)
    columns: tuple[str, ...] = ()
    card: Any = None


#: 指标的值给不给，从松到严：照给、核对不了、不给
_STRICTNESS = {None: 0, "unverified": 1, "masked": 2}


def masked_closure(links: Mapping[Any, MetricLinks], known: Mapping[Any, Judged] | None = None,
                   secrets: Callable[[], Any] | None = None) -> dict[Any, Judged]:
    """转抄上游口径卡的指标值给不给：在指标依赖图上一次算出的最小不动点。证据面板、写作目录、推演、可验证报告都按这一个
    函数认（各自按自己的口径认好转抄以外的输入，见 MetricLinks.direct），不各写一套。

    先按转抄以外的输入认每个指标（取自遮罩格的、核对不了的），再沿转抄的边往下游传，一直传到不再变化：
    - 转抄的保守规则（CopyEdge.card）：转抄的那张卡在这次运行的任何一次执行里有一个指标不给，转抄它的输入就是 masked，
      字面量下标、下标不是字面量、整张卡、清单文字、整份指标列表都一样，读到的那次产出读不出来的也一样。那张卡一个不给的
      指标都没有时照常
    - 读到的那几个指标（upstream）有不给的，同样 masked；只是核对不了的，转抄它的输入核对不了（整张卡、清单文字这类是
      table_unverified，指标的值另按值认）
    环（循环里一张卡抄自己上一轮的产出、几张卡互相转抄）本身不带来遮罩：从「都照给」起往上收，环上没有哪个指标别的输入取自
    遮罩格，整个环照给；有一个取自遮罩格，整个环都不给。以前逐个往上递归、追够八层就按遮罩算：没设任何遮罩的工作流里，
    循环里累加上一轮结果的口径卡也成了「已遮罩」，引用它的正式运行判失败。

    links：这次要认的指标（键 → MetricLinks），转抄的边指到的上游要么也在里面，要么在 known 里（之前认好的，结果不再变），
    都不在的（卡上没记输入的老指标、写死的数）照给；一张卡的指标可以一部分在 known 里、一部分在 links 里，按 card 合起来认。
    secrets 同 withheld_value，整份结果当输入、认不出出处的输入要按值认时才取。每个指标的结论只会往严里变（最多两次），每张卡
    「有指标不给」只会从无到有、遮罩列只增不减，每变一次只重认转抄它的那几个：总的工作量和边数成正比。"""
    known = known or {}
    words: dict[Any, str | None] = {key: None for key in links}
    columns: dict[Any, tuple[str, ...]] = {key: () for key in links}
    below: dict[Any, set[Any]] = {}
    readers: dict[Any, set[Any]] = {}
    for key, node in links.items():
        for edge in node.copies:
            for up in edge.upstream:
                if up in links:
                    below.setdefault(up, set()).add(key)
            if edge.card is not None:
                readers.setdefault(edge.card, set()).add(key)
    # 被转抄的卡里有指标不给的：卡 → 让它们不给的遮罩列（没有不给的卡不在里面）。之前认好的先算进去
    cards: dict[Any, tuple[str, ...]] = {}
    for found in known.values():
        if found.word == "masked" and found.card is not None and found.card in readers:
            cards[found.card] = tuple(dict.fromkeys((*cards.get(found.card, ()), *found.columns)))

    def state(key: Any) -> tuple[str | None, tuple[str, ...]]:
        if key in links:
            return words[key], columns[key]
        found = known.get(key)
        return (found.word, found.columns) if found is not None else (None, ())

    def judge(key: Any) -> Judged:
        node = links[key]
        masked = [p for p, (kind, _) in node.direct.items() if kind in ("masked", "value")]
        unverified = [p for p, (kind, _) in node.direct.items() if kind == "unverified"]
        uncertain = node.uncertain or any(kind == "table" for kind, _ in node.direct.values())
        because = [c for kind, cols in node.direct.values() if kind in ("masked", "table") for c in cols]
        copied: dict[str, tuple[str | None, tuple[str, ...], str | None]] = {}
        for edge in node.copies:
            ups = [state(up) for up in edge.upstream]
            if (edge.card is not None and edge.card in cards) or any(word == "masked" for word, _ in ups):
                cols = (*cards.get(edge.card, ()), *(c for word, cs in ups if word == "masked" for c in cs))
                copied[edge.path] = ("masked", tuple(dict.fromkeys(cols)), None)
            elif edge.unreadable is not None:
                copied[edge.path] = ("table_unverified" if edge.whole else "unverified", (), edge.unreadable)
            elif any(word == "unverified" for word, _ in ups):
                copied[edge.path] = ("table_unverified" if edge.whole else "unverified", (), None)
            else:
                copied[edge.path] = (None, (), None)
            kind, cols, _ = copied[edge.path]
            if kind == "masked":
                masked.append(edge.path)
                because.extend(cols)
            elif kind == "unverified":
                unverified.append(edge.path)
            elif kind is not None:
                uncertain = True
        word = withheld_value(node.metric, node.metric.get("value"), masked=masked, unverified=unverified,
                              uncertain=uncertain, secrets=secrets)
        return Judged(word, copied, tuple(dict.fromkeys(because)) if word == "masked" else (), node.card)

    queue = list(links)
    waiting = set(queue)
    while queue:
        key = queue.pop()
        waiting.discard(key)
        got = judge(key)
        # 只往严里收：withheld_value 先看取自遮罩格的、再看核对不了的，上游变严时这里也不会松回去
        word = got.word if _STRICTNESS[got.word] >= _STRICTNESS[words[key]] else words[key]
        cols = tuple(dict.fromkeys((*columns[key], *got.columns))) if word == "masked" else ()
        if (word, cols) == (words[key], columns[key]):
            continue
        words[key], columns[key] = word, cols
        downs = set(below.get(key, ()))
        card = links[key].card
        if word == "masked" and card is not None and card in readers:
            merged = tuple(dict.fromkeys((*cards.get(card, ()), *cols)))
            if card not in cards or cards[card] != merged:
                cards[card] = merged
                downs |= readers[card]
        for down in downs:
            if down not in waiting:
                waiting.add(down)
                queue.append(down)
    out: dict[Any, Judged] = {}
    for key in links:
        got = judge(key)
        out[key] = Judged(words[key], got.copied, columns[key], links[key].card)
    return out


def positional_cell(path: str, node_id: Any, output: Callable[[Any], Any], *, wide: bool = False) -> dict[str, Any] | None:
    """按位置取自一份查询结果的一格：vars.res.rows[0][2]、nodes.parse.rows[1]['amount'] 这种标量引用——查询结果经数据
    整形解析成对象之后按行号、列号（或列名）取。返回 {artifact, source, row, column}：artifact、source 是那份结果里带着的
    （数据源工具、合并查询交回的结果都带，没有的是 None），row 是从 0 数的行号，column 是列名。认不出来的返回 None。

    遮罩要按列认（证据面板的指标步骤、推演都用这个）：cell() 和 Agent 字段有定位，这种引用卡上只记了路径和值。

    output(节点 id) 交回那个节点当时的产出（读不出来给 None）。nodes.X 的根就是 X 的产出；vars.X 是写它的节点（输入记着
    的 node_id）交给变量的那个值——数据整形、合并查询交的是产出本身，Agent、LLM、代码节点交的是产出里的 data，工具节点
    交的是 JSON 文本，都试一遍，取值链走得通的那个就是。下标得是字面量：rows[vars.i] 要另取一个值才知道是哪一行，不认。

    wide（见 input_origin）：rows 之后的下标可以不是字面量，整行、整份结果也认，另交回 columns（用到的列）和 table
    （整份结果）；row、column 认不出时为 None。走到 rows 之前的下标仍得是字面量（vars.r[vars.k].rows 说不清是哪一份）。

    路径就是按位置取一格的样子（以 .rows[行号][列] 结尾），可那个节点的产出读不出来（文件不在、与哈希不一致、不知道是
    谁写的）时返回 {"unreadable": True}：这一格取自哪一列核对不了，调用方宁可多遮。卡上记着这个输入的值，算的时候
    取值链是走得通的，现在读不出来只能是产出丢了或被改了。wide 时伸进了行的（rows 后面还有下标）都算；整份结果不算。
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
                if not wide:
                    return None
                key = _ANY
            keys.append(key)
        tree = tree.value
    keys.reverse()
    if not isinstance(tree, ast.Name) or tree.id not in ("vars", "nodes") or not keys \
            or not isinstance(keys[0], str) or (not wide and len(keys) < 4):
        return None
    base = output(keys[0] if tree.id == "nodes" else node_id)
    if base is None:
        if wide:
            positional = "rows" in keys[1:-1]
        else:
            positional = keys[-3] == "rows" and isinstance(keys[-2], int) and isinstance(keys[-1], (int, str))
        return {"unreadable": True} if positional else None
    candidates = [base]
    if isinstance(base, dict) and "data" in base:
        candidates.append(base["data"])
    if isinstance(base, str):
        try:
            candidates.append(json.loads(base))
        except ValueError:
            pass
    for value in candidates:
        if (found := (_cells_in if wide else _cell_in)(value, keys[1:])) is not None:
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


def _cells_in(value: Any, keys: list[Any]) -> dict[str, Any] | None:
    """wide 的 _cell_in：走到一份查询结果，剩下的是 rows、rows[行]、rows[行][列…]，或者什么都不剩（整份结果）。"""
    for i, key in enumerate(keys):
        if isinstance(value, dict) and isinstance(value.get("rows"), list) and key == "rows":
            return _cells_at(value, keys[i + 1:])
        if isinstance(value, dict) and isinstance(key, str):
            value = value.get(key, _GONE)
        elif isinstance(value, list) and isinstance(key, int) and -len(value) <= key < len(value):
            value = value[key]
        else:
            return None
        if value is _GONE:
            return None
    if isinstance(value, dict) and isinstance(value.get("rows"), list):
        return _cells_at(value, None)
    return None


def _cells_at(result: dict[str, Any], rest: list[Any] | None) -> dict[str, Any] | None:
    """一份查询结果里 rest（rows 后面的下标）取到的那几列：整份结果（rest 为 None 或空）、整行、一格，列号认不出的算全部列。
    字面量的行号越界、列名对不上的认不出（None）：卡上记着的值不是从这里取的。"""
    rows = result["rows"]
    columns = [str(c) for c in result.get("columns") or []]
    artifact, source = result.get("artifact"), result.get("source")
    out = {"artifact": artifact if isinstance(artifact, str) and artifact else None,
           "source": source if isinstance(source, str) and source else None,
           "row": None, "column": None, "columns": columns, "table": not rest}
    if not rest:
        return out
    row = rest[0]
    if row is not _ANY:
        if not isinstance(row, int) or not -len(rows) <= row < len(rows):
            return None
        out["row"] = row % len(rows)
    if len(rest) == 1 or rest[1] is _ANY:
        return out
    column = rest[1]
    record = rows[out["row"]] if out["row"] is not None else None
    if isinstance(column, int) and -len(columns) <= column < len(columns):
        name: str | None = columns[column]
    elif isinstance(column, str) and (column in columns or (isinstance(record, dict) and column in record)):
        name = column
    else:
        return None
    return {**out, "column": name, "columns": [name]}


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
        if any(i.masked and not i.by_value for i in found):
            raise WhatIfError(f"输入「{path}」取自设置了遮罩的列，不能用于推演")
        if any(i.masked for i in found):
            raise WhatIfError(f"输入「{path}」的值与设置了遮罩的列中的值相同，不能用于推演")
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
    if plan.hides or any(inp.masked or inp.unreadable for inp in plan.inputs.values()):
        substituted = None      # 代入式里代进去的就是遮罩列（或者遮没遮核对不了）的那一格
    out = {
        "id": metric.get("id"), "name": metric.get("name") or metric.get("id"), "unit": metric.get("unit") or "",
        "decimals": metric.get("decimals"), "format": metric.get("format") or DEFAULT_FORMAT,
        "old": _finite(old), "new": _finite(new), "delta": delta,
        "rendered_old": _render(metric, old), "rendered_new": _render(metric, new) if known else None,
        "rendered_delta": rendered_delta,
        "changed": (not _same(old, new)) if known else None,
        "status": status, "reason": reason, "substituted": substituted,
        "inputs": [_input_view(inp, values, notes, blocked=blocked) for inp in plan.inputs.values()],
    }
    if plan.hides:
        # 指标的值就是（或者算自）遮罩列的那一格：原值同样写「已遮罩」，遮没遮核对不了的写「无法核对」。以前输入里
        # 拿掉了、指标的 old 照给，同一个号码换个地方照样看得到；原因在 reason 里（推演不了的原因）。本来就没有值的
        # 照旧是空的，缺输入还得看得出来
        out["withheld"] = True
        if old is not None:
            out["old"] = out["rendered_old"] = _masked_word() if plan.held == "masked" else _unverified_word()
    return out


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
            reason = (MASKED_VALUE_INPUT if inp.by_value else MASKED_COPY if inp.copy else MASKED_TABLE if inp.table
                      else MASKED_INPUT) if inp.masked \
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


def withheld_value(metric: dict[str, Any], value: Any, *, masked: Iterable[str] = (), unverified: Iterable[str] = (),
                   uncertain: bool = False, secrets: Callable[[], Any] | None = None) -> str | None:
    """指标本身的值给不给（推演的原值、证据面板指标步骤的值，两处同一个判断）：None 照给；"masked" 写「已遮罩」；
    "unverified" 写「无法核对」。masked 是取自遮罩列的一格、一行（或者值就是遮罩值的一格）的输入的路径，unverified 是
    遮没遮核对不了的输入的路径，uncertain 是有没有整份结果当输入、认不出出处的输入。

    - 有输入取自遮罩列的一格、一行：不给，哪怕它只嵌在下标里（推演把整个指标按遮罩处理，_Plan.blocked）。指标的值可能
      就是那一格（cell(nodes.q, 0, 'phone')），也可能是算出来的数，宁可多遮：改一个别的输入、拿指标的值一减，就能算出
      遮住的那一格
    - 最外层的输入遮没遮核对不了：不给，它的值就在指标值里。只嵌在下标里的不算：它只决定取哪一格，取到的值卡上另记着。
      认不出最外层引用（表达式有误）时，有一个遮没遮核对不了就不给。说法和遮着的分开：读的人不该以为这里设了遮罩
    - 整份结果当输入、认不出出处的输入（uncertain）：按值认，值里有这次运行查到过的遮罩值（secrets()，masking._Secrets）
      才不给。整份结果不一定把哪一格带进指标：len(vars.res.rows) 数行数，照给；str(vars.res.rows)、max(r[2] for r in …)
      值里就是号码，不给。以前整份结果有一列遮着就不给，推演写「已遮罩」、证据面板的指标步骤照写行数，同一个数两样说法
    """
    if any(True for _ in masked):
        return "masked"
    unverified = set(unverified)
    if unverified:
        try:
            tops, _ = _refs(parse_expression(str(metric.get("expression") or ""))[0])
        except ExpressionError:
            return "unverified"
        if unverified & set(tops):
            return "unverified"
    if uncertain and secrets is not None and maybe_masked_value(value) and carries_masked(value, secrets()):
        return "masked"
    return None


def maybe_masked_value(value: Any) -> bool:
    """值有没有可能是按值认得出的遮罩值（masking 的门槛：够长的数、两个字以上的文字），容器一律算。不可能的不必去取这次
    运行的遮罩值（要把封存的查询快照读一遍）。"""
    if value is None or isinstance(value, bool):
        return False
    if isinstance(value, (list, tuple, dict)):
        return True
    from app.engine.masking import _forms, _strong

    return any(_strong(form) for form in _forms(value))


def carries_masked(value: Any, secrets: Any) -> bool:
    """值里有没有按值认得出的遮罩值（secrets 是 masking._Secrets，None 为没有）：整个值就是一个遮罩值（数写法不同也算），
    或者文字、容器（按 JSON 写出来）里写着一个（换了写法的长号码也认）。"""
    if secrets is None or value is None or isinstance(value, bool):
        return False
    if isinstance(value, (list, tuple, dict)):
        return bool(secrets.spans(json.dumps(value, ensure_ascii=False, default=str), loose=True))
    if secrets.hit(value):
        return True
    return isinstance(value, str) and bool(secrets.spans(value, loose=True))


def _masked_word() -> str:
    """「已遮罩」：证据面板、推演同一个说法。api.evidence 导入了这个模块，用到时再导入。"""
    from app.api.evidence import MASKED

    return MASKED


def _unverified_word() -> str:
    """「无法核对」：遮没遮核对不了、不给的值（和「已遮罩」分开：读的人不该以为这里设了遮罩）。同 _masked_word。"""
    from app.api.evidence import UNVERIFIED_VALUE

    return UNVERIFIED_VALUE


def substituted_without(metric: dict[str, Any], paths: Iterable[str] | Mapping[str, str],
                        token: str = "") -> str | None:
    """卡上的代入式，只把 paths 这几个输入代成 token（证据面板给取自遮罩列的输入写「已遮罩」），别的照卡上代。paths 也可以
    是 {路径: 代成的字}，各写各的（遮没遮核对不了的那一处写「无法核对」）。

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
    tokens = dict(paths) if isinstance(paths, Mapping) else {p: token for p in paths}
    swapped = ast.fix_missing_locations(_Bind(tokens).visit(copy.deepcopy(tree)))
    return substitute(swapped, {p: v for p, v in values.items() if p not in tokens})


# --------------------------------------------------------------------------
# 报告里引用的地方、下游口径卡
# --------------------------------------------------------------------------


def report_segments(reports: Iterable[tuple[str, dict[str, Any]]], *, card_node: str, card_artifact: str,
                    results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """报告里引用了这些指标的片段，推演之后会写成什么：[{report, unit, sid, metric, rendered_old, rendered_new, changed}]。

    只认指向这张卡这一份指标集的引用（目录条目的 node_id、artifact）：循环里同一张卡算过几次，报告引用的是哪一次
    就是哪一次。新值按片段自己的换算（|万、|pct……）渲染，和报告里的字同一套规则。指标推演不了（unsupported、masked）
    时 rendered_new、changed 为 None：不知道会变成什么。缺输入、当时就显示「—」的引用也列上：推演里补上了值，它会变。
    原值不给的指标（withheld），rendered_old 同样写「已遮罩」或「无法核对」。
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
                # 原值不给的指标（_Plan.hides）：报告里引用它的地方同样写「已遮罩」（遮没遮核对不了的写「无法核对」），
                # 推演的答复里不出现它的值
                old = result.get("rendered_old") if result.get("withheld") and result.get("old") is not None \
                    else seg.get("text")
                out.append({"report": report_node, "unit": unit.get("id"), "sid": seg.get("id"), "metric": result["id"],
                            "rendered_old": old, "rendered_new": new,
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
