"""运行对比：同一个工作流的两次运行（本周和上周的周报），把每个指标的变化拆清楚——是数据变了、口径变了，
还是模板 / SQL 变了；数据变化再按维度归因。每一项都追得到两边的查询快照。

只读：这里只算，不读库、不写工件、不追加事件。接口（api/compare.py）把两次运行封存范围内的事件、工件的读法和
遮罩准备好交进来（Side），结果原样交回给发请求的人。

怎么对：

- 查询：两次运行各自封存范围内交回过的查询快照，按「节点 + 该节点里第几次查询 + 工具」配对，配不上的再按规范化
  SQL（data/sql_semantics.normalize_sql）配。结构：规范文本和字面量都相同是 same；规范文本相同、只有字面量不同是
  params_only（定时运行每次的日期不同，属于参数变化不是模板变化）；否则 changed（数据源换了也算）。核查类的查询
  （双盲复算、反方审稿发的 role: verify）不进报告，不参与对比。
- 口径卡：同一个口径卡节点按指标 id 配对；循环里执行了几次的取最后一次。表达式、口径版本、小数位、单位、显示格式
  有一样不同就是口径变了：小数位改变取整，单位、比率与否改变数值的含义，都不只是换个写法。
- 每个指标一个分类，按这个次序判：一边有一边没有（added / removed）；口径变了（caliber_and_data，可能连同数据）；
  喂它的查询结构变了、上游节点改了配置（sql_changed，标出、不拆）；输入对不上具体的查询，上游又有变化，说不清变化
  从哪来（incomparable）；只有参数变了（params_only，含运行输入）；都没变时数值相等是 unchanged，不等是 data_only。
  「喂它的查询」按输入的出处认：cell() 那一格、Agent 按出处交来的字段落在哪份快照上就是哪条查询（两边各自的快照
  直接比 SQL）；不落在快照上的，看写它的节点自己查过的库，节点不查库（代码、数据整形……）就看它的配置和画布上的
  全部上游。
- 严格拆分（只对口径变了的）：旧表达式的每个叶子路径都能在这次的口径卡输入里取得值时，
  口径效应 = f_new(x_new) − f_old(x_new)，数据效应 = f_old(x_new) − f_old(x_old)。f_old(x_new) 用推演重算
  （engine/recompute.py）的还原和求值算，取整按旧口径卡的小数位；三个数按十进制精确相减，两效应之和逐位等于
  总变化。取数的 SQL 也变了、用到了遮罩列、单位不同的不拆，说明为什么。
- 维度归因：指标的输入落在查询快照的某一格（或 Agent 数组字段的一段行）、两边快照都没截断时，取快照里不是数值的
  列作维度（遮罩的、日期类型的不算；两边都只有一个值的、两边没有一个值相同的——日期、期次这类每期都不同的列——
  也不拿来配对），按维度键配对两边的行，算每个键的 base / target / delta，按 |delta| 取前 10。只有度量列之和
  等于这个输入的值（两边都等）时才给占比；取的是合计行那一格时，其余各行之和等于它也算，合计行不列进去。
- 遮罩列的值绝不出现在结果里：取自遮罩列的输入不给值，遮罩列不当维度、不当度量，用到遮罩列的指标不拆分（拆出来的
  f_old(x_new) 配上公开的数就能反推出遮住的那一格）。
"""
from __future__ import annotations

import ast
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from app.core.events import EventType
from app.data.sql_semantics import NormalizedSql, diff_facets, facets, normalize_sql
from app.engine import recompute
from app.engine.evidence import DEFAULT_FORMAT, RenderError, is_number, render_number
from app.engine.expressions import ExpressionError, cell_value, leaf_refs, parse_expression
from app.engine.labels import field_label
from app.engine.merge_query import MERGE_SOURCE
from app.engine.nodes.metrics import _REF_HEAD

UNCHANGED = "unchanged"
DATA_ONLY = "data_only"
CALIBER_AND_DATA = "caliber_and_data"
SQL_CHANGED = "sql_changed"
PARAMS_ONLY = "params_only"
ADDED = "added"
REMOVED = "removed"
INCOMPARABLE = "incomparable"
#: summary 里的各类，次序同接口约定
CLASSES = (UNCHANGED, DATA_ONLY, CALIBER_AND_DATA, SQL_CHANGED, PARAMS_ONLY, ADDED, REMOVED, INCOMPARABLE)
#: 这几类才做维度归因：数据（连同参数、口径）变了，两边的行说的是同一回事
_ATTRIBUTED = frozenset({DATA_ONLY, PARAMS_ONLY, CALIBER_AND_DATA})

#: 维度归因最多列几个维度值（按变化的绝对值从大到小）
TOP_KEYS = 10
#: 一条查询的结构差异最多写几条（diff_facets 已按对结果的影响从大到小排）
MAX_DIFFERENCES = 5
#: 表达式、取值写进说明时最多几个字
_SHOWN_MAX = 60
#: 比较 SQL 时两边的叫法（diff_facets 的 labels）
_LABELS = ("基准", "这次")
#: 合并查询的 SQL 在 SQLite 里执行（engine/merge_query.py）
_MERGE_DIALECT = "sqlite"
#: 日期类的列不当维度：每期都不同，按它配对两边的行一个都配不上
_TEMPORAL = frozenset({"date", "datetime", "time"})
_FORMAT_LABEL = {"thousands": "千分位", "plain": "不分组", "percent_of_ratio": "比率按百分数显示"}

NO_CARDS = "两次运行都没有口径卡，没有可以对比的指标"
EDGES_CHANGED = "节点之间的连线有变化"
DEFAULTS_CHANGED = "工作流默认设置有变化"
MASKED_NOTE = "部分列在数据源中设置了遮罩，对比中不显示这些列的值，也不用它们做拆分和归因"
NO_LINEAGE = "口径卡没有记录输入（生成于系统升级前），无法判断变化来源"
DATA_FROM_QUERIES = "取数的 SQL 没有变化，查询结果有变化"
DATA_FROM_UPSTREAM = "模板和取数的 SQL 都没有变化，变化来自上游数据"
SQL_NOT_SPLIT = "取数的 SQL 或上游配置有变化，变化无法在口径与数据之间拆分"
NOT_NUMBERS = "有一边的值不是数值，无法拆分"
SPLIT_MASKED = "用到了设置了遮罩的列，不做拆分"
NO_DIFFERENCE = "查询的写法有变化（选取的列、计算方式等）"
TRUNCATED = "查询结果被截断（{side}只取回了前 {rows} 行），不做维度归因"
MASKED_MEASURE = "度量列「{column}」设置了遮罩，不做维度归因"
UNREADABLE_SNAPSHOT = "查询快照无法读取，不做维度归因"
NOT_A_MEASURE = "度量列「{column}」有不是数值的格，不做维度归因"
NO_SHARE = "各维度值的度量之和与输入的值不相等，变化仅供参考，不给占比"
ZERO_TOTAL = "总变化为 0，不计算占比"
TOTAL_ROW = "输入取自合计行（第 {row} 行），按其余各行归因"
MORE_KEYS = "共 {count} 个维度值，列出变化最大的 {top} 个"
NO_OVERLAP = "列「{columns}」两边没有相同的值（可能是日期、期次这类每期都不同的列），不按它配对"


@dataclass
class Side:
    """一次运行在对比里要用到的东西，接口从这次运行的封存范围里准备好。

    - run：运行的概要（id、工作流名、版本、类别、起止时间、定时任务），原样放进结果
    - graph：运行时的图快照；events：封存范围内的事件 (seq, type, node_id, data)
    - cards：封存范围内复验过哈希的口径卡 (节点, 工件, 指标集)（api/evidence._sealed_cards）。事件里记着、这里
      没有的，就是读不出来或者被改过
    - load：工件 id → 内容，只认封存范围内的；文件不在返回 None，内容和哈希对不上抛 ValueError
    - masked：查询快照 → 要遮的列（小写）：查询当时记下的加数据源现在设的
    - sources：口径卡那次执行开始的事件序号 → 推演重算还原输入要用的 Sources（节点产出、跑完的时刻都按那一刻）。
      不给就按 load、masked 拼一个最小的：nodes.* 这类容器输入还原不出来
    - dialects：数据源名 → 类型（sqlite / postgres …），规范化 SQL 时按它的方言解析
    - inputs：运行输入（Run.input）
    """

    run: dict[str, Any]
    graph: dict[str, Any]
    events: list[tuple[int, str, str | None, Any]]
    cards: list[tuple[str, str, dict[str, Any]]] = field(default_factory=list)
    load: Callable[[str], Any] = lambda _: None
    masked: Mapping[str, frozenset[str]] = field(default_factory=dict)
    sources: Callable[[int], recompute.Sources] | None = None
    dialects: Mapping[str, str | None] = field(default_factory=dict)
    inputs: dict[str, Any] = field(default_factory=dict)


def compare(base: Side, target: Side) -> dict[str, Any]:
    """对比两次运行（base 是基准，target 是这次）：{target, base, summary, template, metrics, queries, notes}，
    形状见接口约定（api/compare.py）。只读，不碰两次运行的任何东西。"""
    return _Compare(base, target).result()


# --------------------------------------------------------------------------
# 查询：收集、配对、比结构
# --------------------------------------------------------------------------


@dataclass
class _Query:
    """一次运行里交回过的一份查询快照（核查类的不算）。"""

    artifact: str
    node_id: str
    tool: str | None
    #: 这个节点里第几次查询，从 1 数（循环里执行几次的接着数）
    ordinal: int
    call_id: str | None = None
    #: 快照里的 SQL；快照读不出来时取 tool.start 的参数（合并查询取 merge.end 的）
    sql: str | None = None
    snap: dict[str, Any] | None = None
    #: 快照读不出来的原因：missing / tampered
    problem: str | None = None
    source: str | None = None


def _snapshot(load: Callable[[str], Any], artifact: str) -> tuple[dict[str, Any] | None, str | None]:
    try:
        content = load(artifact)
    except ValueError:
        return None, "tampered"
    if isinstance(content, dict) and isinstance(content.get("rows"), list):
        return content, None
    return None, "missing"


def _queries(side: Side) -> list[_Query]:
    """封存范围内交回过的查询快照，按交回的先后。

    调用工具节点、Agent 的查询在 tool.end 里（query_artifact）；合并查询只记在 node.finished 的台账里。同一件快照
    两处都有的按 tool.end 算一次。核查类的查询（role: verify）不进写作目录，这里也不收。"""
    out: list[_Query] = []
    seen: set[tuple[str, str]] = set()
    asked: dict[tuple[str, Any], str] = {}      # (节点, call_id) → tool.start 里的 SQL
    merged: dict[str, str] = {}                  # 合并节点 → merge.end 里的 SQL
    count: dict[str, int] = {}

    def add(node_id: str, artifact: str, tool: Any, call_id: Any, sql: Any, source: Any) -> None:
        count[node_id] = count.get(node_id, 0) + 1
        snap, problem = _snapshot(side.load, artifact)
        own = snap.get("sql") if snap is not None and isinstance(snap.get("sql"), str) else None
        out.append(_Query(artifact=artifact, node_id=node_id, tool=str(tool) if tool else None,
                          ordinal=count[node_id], call_id=str(call_id) if call_id else None,
                          sql=own or (sql if isinstance(sql, str) and sql.strip() else None), snap=snap,
                          problem=problem, source=(snap or {}).get("source") or source))

    for _, etype, node_id, data in side.events:
        if not node_id or not isinstance(data, dict) or data.get("role") == "verify":
            continue
        if etype == EventType.TOOL_START:
            args = data.get("args")
            if isinstance(args, dict) and isinstance(args.get("sql"), str):
                asked[(node_id, data.get("call_id"))] = args["sql"]
        elif etype == EventType.TOOL_END and data.get("query_artifact"):
            artifact = str(data["query_artifact"])
            seen.add((node_id, artifact))
            add(node_id, artifact, data.get("tool"), data.get("call_id"), asked.get((node_id, data.get("call_id"))),
                None)
        elif etype == EventType.MERGE_END and isinstance(data.get("sql"), str):
            merged[node_id] = data["sql"]
        elif etype == EventType.NODE_FINISHED:
            for entry in data.get("evidence") or []:
                if not (isinstance(entry, dict) and entry.get("kind") == "query" and entry.get("artifact")):
                    continue
                artifact = str(entry["artifact"])
                if (node_id, artifact) in seen:
                    # tool.end 已经收过：台账里多记着数据源，读不出快照时用得上
                    for q in out:
                        if q.node_id == node_id and q.artifact == artifact and not q.source:
                            q.source = entry.get("source")
                    continue
                seen.add((node_id, artifact))
                add(node_id, artifact, entry.get("tool"), entry.get("call_id"), merged.get(node_id),
                    entry.get("source"))
    return out


def _pair(base: list[_Query], target: list[_Query],
          text: Callable[[_Query], str | None]) -> list[tuple[_Query | None, _Query | None]]:
    """先按「节点 + 第几次 + 工具」配，配不上的再按规范化 SQL 配（节点换了 ID、同一个模板只有日期不同的也配得上）。
    结果按这次运行的先后排，基准运行多出来的接在后面。"""
    slots = {(q.node_id, q.ordinal, q.tool): i for i, q in enumerate(base)}
    matched: dict[int, int] = {}
    for j, q in enumerate(target):
        i = slots.get((q.node_id, q.ordinal, q.tool))
        if i is not None:
            matched[j] = i
    taken = set(matched.values())
    for j, q in enumerate(target):
        if j in matched or (key := text(q)) is None:
            continue
        i = next((i for i, b in enumerate(base) if i not in taken and text(b) == key), None)
        if i is not None:
            matched[j] = i
            taken.add(i)
    pairs: list[tuple[_Query | None, _Query | None]] = [
        (base[matched[j]] if j in matched else None, q) for j, q in enumerate(target)]
    return pairs + [(b, None) for i, b in enumerate(base) if i not in taken]


@dataclass
class _Shape:
    """一对查询的结构：same / params_only / changed / added / removed，不同的字面量，结构上差在哪。"""

    structure: str
    literals: list[dict[str, str]] = field(default_factory=list)
    differences: list[str] = field(default_factory=list)
    #: 两边的 SQL 都拿得到（拿不到的只能说不知道，不能说变了）
    known: bool = True


# --------------------------------------------------------------------------
# 口径卡
# --------------------------------------------------------------------------


@dataclass
class _Card:
    """一个口径卡节点在一次运行里最后一次执行的指标集。"""

    node_id: str
    artifact: str
    content: dict[str, Any] | None
    #: 指标集读不出来、被改过的说法
    problem: str | None
    #: 那次执行的 node.started 序号：输入按那一刻的节点产出还原
    started: int
    #: 执行了几次
    runs: int
    plans: dict[str, Any] = field(default_factory=dict)
    #: 这张卡上全部指标的输入，按路径（同一次执行里同一条路径的值是同一个）
    inputs: dict[str, Any] = field(default_factory=dict)
    sources: recompute.Sources | None = None

    def metrics(self) -> list[dict[str, Any]]:
        return [m for m in (self.content or {}).get("metrics") or [] if isinstance(m, dict) and m.get("id") is not None]


def _cards(side: Side) -> dict[str, _Card]:
    """每个口径卡节点最后一次执行的指标集，按第一次执行的先后。哪些 node.finished 算口径卡的执行，和
    api/evidence._sealed_cards 一样认：台账里的 metric_set 条目；一期以前没有台账的，画布上是口径卡的节点的产出。"""
    verified = {(node_id, artifact): card for node_id, artifact, card in side.cards}
    calibers = {str(n.get("id")) for n in side.graph.get("nodes") or []
                if isinstance(n, dict) and n.get("type") == "metrics"}
    runs: dict[str, list[tuple[int, str]]] = {}
    started: dict[str, list[int]] = {}
    for seq, etype, node_id, data in side.events:
        if not node_id or not isinstance(data, dict):
            continue
        if etype == EventType.NODE_STARTED:
            started.setdefault(node_id, []).append(seq)
        if etype != EventType.NODE_FINISHED:
            continue
        arts = [str(e["artifact"]) for e in data.get("evidence") or []
                if isinstance(e, dict) and e.get("kind") == "metric_set" and e.get("artifact")]
        if not arts and node_id in calibers and data.get("artifact"):
            arts = [str(data["artifact"])]
        runs.setdefault(node_id, []).extend((seq, art) for art in arts)
    out: dict[str, _Card] = {}
    for node_id, done in runs.items():
        if not done:
            continue
        seq, artifact = done[-1]
        content = verified.get((node_id, artifact))
        out[node_id] = _Card(node_id=node_id, artifact=artifact, content=content,
                             problem=None if content is not None else _card_problem(side.load, artifact),
                             started=max((s for s in started.get(node_id, []) if s < seq), default=seq),
                             runs=len(done))
    return out


def _card_problem(load: Callable[[str], Any], artifact: str) -> str:
    try:
        content = load(artifact)
    except ValueError:
        return "指标集与哈希不一致，疑似被修改"
    return "无法读取指标集" if content is None else "指标集的内容不完整"


def _plan_card(side: Side, card: _Card, queries: list[_Query]) -> None:
    """按推演重算的规则还原卡上每个指标的输入：落在哪份快照上、是不是遮罩列、容器的完整值。"""
    if card.content is None:
        return
    sources = side.sources(card.started) if side.sources is not None else recompute.Sources(
        load=side.load, queries=tuple(dict.fromkeys(q.artifact for q in queries)), masked=side.masked)
    store = recompute._Store(sources)
    card.sources = sources
    for metric in card.metrics():
        plan = recompute._plan(metric, store)
        card.plans[str(metric["id"])] = plan
        for path, inp in plan.inputs.items():
            card.inputs.setdefault(path, inp)


# --------------------------------------------------------------------------
# 数：精确相减、渲染
# --------------------------------------------------------------------------


def _exact(value: Any) -> Decimal | None:
    """有限的数换成十进制：浮点按最短的写法（repr），相减不带二进制的误差。"""
    if not recompute._number(value):
        return None
    try:
        return Decimal(repr(value)) if isinstance(value, float) else Decimal(value)
    except (InvalidOperation, ValueError):
        return None


def _plain(d: Decimal, *, whole: bool) -> int | float:
    """十进制换回 JSON 里的数：参与的都是整数时给整数，否则给浮点。"""
    if whole and d == d.to_integral_value():
        return int(d)
    return float(d)


def _places(*metrics: dict[str, Any]) -> int | None:
    """几个指标里最多的小数位；有一个没设的就不定小数位（按值本来的样子写）。"""
    found = [m.get("decimals") for m in metrics]
    if any(not isinstance(d, int) or isinstance(d, bool) for d in found):
        return None
    return max(found)


def _scale(metric: dict[str, Any]) -> tuple[str, bool]:
    """数值的含义：单位，以及是不是比率（percent_of_ratio 的 0.087 和单位为 % 的 8.7 不能直接相减）。"""
    return str(metric.get("unit") or ""), (metric.get("format") or DEFAULT_FORMAT) == "percent_of_ratio"


def _signed(meta: dict[str, Any], value: Any) -> str | None:
    """变化量的写法：+300.0元、-2单、+10.3 个百分点（比率、百分数的变化写百分点，和推演同一个写法）。为 0 写不带
    符号的 0。渲染不出来的给 None。"""
    if not recompute._number(value):
        return None
    if value != 0:
        return recompute._delta(meta, 0, value)[1]
    fmt, unit, places = meta.get("format") or DEFAULT_FORMAT, str(meta.get("unit") or ""), meta.get("decimals")
    try:
        if fmt == "percent_of_ratio":
            return render_number(0, decimals=places, fmt=fmt).rstrip("%") + " 个百分点"
        if unit in ("%", "％"):
            return render_number(0, decimals=places, fmt=fmt) + " 个百分点"
        return render_number(0, unit=unit, decimals=places, fmt=fmt)
    except RenderError:
        return None


def _shown(value: Any) -> str:
    """一个值写进说明：文本、数照写（太长的截断），空值写「空值」，容器写大概。"""
    if value is None:
        return "空值"
    if isinstance(value, (list, dict)):
        return f"[{len(value)} 项]" if isinstance(value, list) else f"{{{len(value)} 个字段}}"
    text = str(value)
    return text if len(text) <= _SHOWN_MAX else text[:_SHOWN_MAX] + "…"


def _expression(metric: dict[str, Any]) -> str:
    """表达式的规范写法：空白、括号写法不同的算同一个。解析不了的照原文。"""
    raw = str(metric.get("expression") or "").strip()
    try:
        return ast.unparse(parse_expression(raw)[0])
    except (ExpressionError, ValueError):
        return raw


# --------------------------------------------------------------------------
# 画布
# --------------------------------------------------------------------------


def _nodes(graph: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(n.get("id")): n for n in graph.get("nodes") or [] if isinstance(n, dict) and n.get("id")}


def _data(node: dict[str, Any]) -> dict[str, Any]:
    return node.get("data") if isinstance(node.get("data"), dict) else {}


def _config(node: dict[str, Any]) -> dict[str, Any]:
    config = _data(node).get("config")
    return config if isinstance(config, dict) else {}


def _edges(graph: dict[str, Any]) -> set[tuple[str, str, str]]:
    """连线：(起点, 终点, 出口)。默认出口的几种写法算同一个（和 autofix 认连线的规则一致）。"""
    out = set()
    for edge in graph.get("edges") or []:
        if isinstance(edge, dict):
            handle = edge.get("sourceHandle")
            out.add((str(edge.get("source")), str(edge.get("target")),
                     "" if handle in (None, "out", "source") else str(handle)))
    return out


def _config_keys(was: dict[str, Any], now: dict[str, Any]) -> tuple[list[str], bool]:
    """一个节点改了哪些配置项：(键名列表, 改的是不是只有名称)。值是字典的往下看一层（args.sql），不展示值本身。"""
    keys: list[str] = []
    if was.get("type") != now.get("type"):
        keys.append("type")
    renamed = _data(was).get("label") != _data(now).get("label")
    a, b = _config(was), _config(now)
    for key in dict.fromkeys([*a, *b]):
        x, y = a.get(key), b.get(key)
        if x == y:
            continue
        if isinstance(x, dict) and isinstance(y, dict):
            keys += [f"{key}.{sub}" for sub in dict.fromkeys([*x, *y]) if x.get(sub) != y.get(sub)]
        else:
            keys.append(key)
    return (keys + ["label"] if renamed else keys), renamed and not keys


def _key_label(key: str, node_type: Any) -> str:
    return "节点类型" if key == "type" else field_label(key, node_type)


# --------------------------------------------------------------------------
# 输入的出处有没有变
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class _Change:
    """一个输入的出处在两次运行之间变没变：same / params / sql / template / unknown，加一句说明。"""

    kind: str
    reason: str | None = None


#: 合并几处出处变化时的轻重：模板、SQL 变了 > 说不清 > 只有参数变了 > 没变
_WEIGHT = {"sql": 3, "template": 3, "unknown": 2, "params": 1, "same": 0}


def _worst(changes: Iterable[_Change]) -> _Change:
    found = list(changes)
    return max(found, key=lambda c: _WEIGHT[c.kind]) if found else _Change("same")


def _from_input(path: str, item: dict[str, Any]) -> bool:
    head = _REF_HEAD.match(path)
    return item.get("via") == "input" or (head is not None and head.group(1) == "input")


# --------------------------------------------------------------------------
# 对比
# --------------------------------------------------------------------------


class _Compare:
    def __init__(self, base: Side, target: Side) -> None:
        self.base, self.target = base, target
        self.notes: list[str] = []
        self.masked_seen = False
        self._norm: dict[tuple[str, str | None], NormalizedSql] = {}
        self._shapes: dict[tuple[int, int], _Shape] = {}
        self._ancestors: dict[str, set[str]] = {}
        self.base_queries, self.target_queries = _queries(base), _queries(target)
        self.pairs = _pair(self.base_queries, self.target_queries, self._text)
        self.template, self.changed = self._template()
        self.titles = {**{k: self._title_of(n) for k, n in _nodes(base.graph).items()},
                       **{k: self._title_of(n) for k, n in _nodes(target.graph).items()}}
        self.parents: dict[str, set[str]] = {}
        for source, target_id, _ in _edges(base.graph) | _edges(target.graph):
            self.parents.setdefault(target_id, set()).add(source)

    # ---------------------------------------------------------------- 结果

    def result(self) -> dict[str, Any]:
        metrics = self._metrics()
        queries = [self._query_view(b, t) for b, t in self.pairs]
        self._run_notes()
        if self.masked_seen:
            self.notes.append(MASKED_NOTE)
        summary = {name: 0 for name in CLASSES}
        for m in metrics:
            summary[m["classification"]] += 1
        return {"target": self.target.run, "base": self.base.run, "summary": summary, "template": self.template,
                "metrics": metrics, "queries": queries, "notes": list(dict.fromkeys(self.notes))}

    def _title_of(self, node: dict[str, Any]) -> str:
        return str(_data(node).get("label") or node.get("id"))

    def title(self, node_id: Any) -> str:
        return self.titles.get(str(node_id), str(node_id))

    def _run_notes(self) -> None:
        """运行输入的变化：键名，值短的连值一起写。值可能是一大段文字，太长的只说有变化。"""
        before, after = self.base.inputs or {}, self.target.inputs or {}
        for key in dict.fromkeys([*before, *after]):
            a, b = before.get(key), after.get(key)
            if a == b:
                continue
            short = all(v is None or isinstance(v, (int, float, str, bool)) and len(str(v)) <= 40 for v in (a, b))
            self.notes.append(f"运行输入「{key}」由「{_shown(a)}」改为「{_shown(b)}」" if short
                              else f"运行输入「{key}」有变化")

    # ---------------------------------------------------------------- 查询

    def _dialect(self, source: Any) -> str | None:
        if source == MERGE_SOURCE:
            return _MERGE_DIALECT
        return self.target.dialects.get(str(source)) or self.base.dialects.get(str(source)) if source else None

    def _normalize(self, sql: str, dialect: str | None) -> NormalizedSql:
        key = (sql, dialect)
        if key not in self._norm:
            self._norm[key] = normalize_sql(sql, dialect)
        return self._norm[key]

    def _text(self, q: _Query) -> str | None:
        return self._normalize(q.sql, self._dialect(q.source)).text if q.sql else None

    def shape(self, b: _Query | None, t: _Query | None) -> _Shape:
        if b is None:
            return _Shape("added")
        if t is None:
            return _Shape("removed")
        key = (id(b), id(t))
        if key not in self._shapes:
            self._shapes[key] = self._compare_sql(b, t)
        return self._shapes[key]

    def _compare_sql(self, b: _Query, t: _Query) -> _Shape:
        if b.sql is None or t.sql is None:
            which = "基准运行" if b.sql is None else "这次运行"
            return _Shape("changed", known=False, differences=[f"{which}的查询快照无法读取，无法比较 SQL"])
        differences = []
        if b.source and t.source and b.source != t.source:
            differences.append(f"数据源由「{b.source}」改为「{t.source}」")
        dialect = self._dialect(t.source or b.source)
        nb, nt = self._normalize(b.sql, dialect), self._normalize(t.sql, dialect)
        if nb.text != nt.text:
            # 时间范围的不同排到最后：定时运行每期的日期本来就不同，结构上真正改了的（多了筛选、换了时间列……）
            # 才是要先看的
            found = sorted(diff_facets(facets(b.sql, dialect), facets(t.sql, dialect), _LABELS),
                           key=lambda d: d.aspect == "time_range")
            texts = [d.text for d in found] or [NO_DIFFERENCE]
            return _Shape("changed", differences=(differences + texts)[:MAX_DIFFERENCES])
        if differences:
            return _Shape("changed", differences=differences)
        literals = [{"base": x, "target": y} for x, y in zip(nb.literals, nt.literals) if x != y]
        return _Shape("params_only", literals) if literals else _Shape("same")

    def _query_view(self, b: _Query | None, t: _Query | None) -> dict[str, Any]:
        shape = self.shape(b, t)
        q = t or b
        assert q is not None
        view = {
            "node_id": q.node_id, "node_title": self.title(q.node_id), "ordinal": q.ordinal,
            "structure": shape.structure,
            "base_sql": b.sql if b else None, "target_sql": t.sql if t else None, "literals": shape.literals,
            "base_rows": len(b.snap["rows"]) if b and b.snap else None,
            "target_rows": len(t.snap["rows"]) if t and t.snap else None,
            "base_truncated": bool(b.snap.get("truncated")) if b and b.snap else None,
            "target_truncated": bool(t.snap.get("truncated")) if t and t.snap else None,
            "base_artifact": b.artifact if b else None, "target_artifact": t.artifact if t else None,
            "differences": shape.differences,
        }
        problems = [f"{side}的查询快照{'与哈希不一致，疑似被修改' if one.problem == 'tampered' else '无法读取'}"
                    for side, one in (("基准运行", b), ("这次运行", t)) if one is not None and one.problem]
        if problems:
            view["problem"] = "；".join(problems)
        return view

    def _side_query(self, queries: list[_Query], inp: Any) -> _Query | None:
        """一个输入落在的那份快照，是这次运行里哪一次查询交回的（同一件快照出现几次时先找写它的节点）。"""
        found = [q for q in queries if q.artifact == inp.snapshot]
        node_id = inp.item.get("node_id")
        return next((q for q in found if q.node_id == node_id), found[0] if found else None)

    def _query_change(self, b: _Query | None, t: _Query | None) -> _Change:
        q = t or b
        assert q is not None
        name = f"查询「{self.title(q.node_id)}」"
        if b is None:
            return _Change("sql", f"节点「{self.title(q.node_id)}」这次多了一次查询")
        if t is None:
            return _Change("sql", f"节点「{self.title(q.node_id)}」这次少了一次查询")
        shape = self.shape(b, t)
        if not shape.known:
            return _Change("unknown", f"{name}的快照无法读取，无法判断 SQL 是否有变化")
        if shape.structure == "changed":
            return _Change("sql", f"{name}的 SQL 结构有变化" + (f"：{shape.differences[0]}" if shape.differences else ""))
        if shape.structure == "params_only":
            shown = "，".join(f"{x['base']} → {x['target']}" for x in shape.literals[:2])
            more = f" 等 {len(shape.literals)} 处" if len(shape.literals) > 2 else ""
            return _Change("params", f"{name}的 SQL 只有参数不同（{shown}{more}）")
        return _Change("same")

    def _node_change(self, node_id: str) -> _Change | None:
        """一个节点自己查过的库在两次运行之间变没变；它两边都没查过库返回 None。"""
        own = [(b, t) for b, t in self.pairs if (b and b.node_id == node_id) or (t and t.node_id == node_id)]
        return _worst(self._query_change(b, t) for b, t in own) if own else None

    def _upstream(self, node_id: str) -> set[str]:
        """画布上 node_id 的全部上游（两次运行的图合起来，宁可多算）。"""
        if node_id not in self._ancestors:
            seen: set[str] = set()
            todo = [node_id]
            while todo:
                for parent in self.parents.get(todo.pop(), ()):
                    if parent not in seen:
                        seen.add(parent)
                        todo.append(parent)
            self._ancestors[node_id] = seen
        return self._ancestors[node_id]

    def provenance(self, path: str, b: Any, t: Any) -> _Change:
        """一个输入的出处在两次运行之间变没变（b、t 是推演重算还原出来的输入）。"""
        if b is None or t is None:
            return _Change("unknown", f"输入「{path}」只在一边的口径卡里有记录")
        if b.snapshot or t.snapshot:
            # 落在查询快照上：两边各自的那份快照直接比 SQL，不管全局配对把它们配给了谁
            if not (b.snapshot and t.snapshot):
                return _Change("unknown", f"输入「{path}」只有一边对得上查询快照，无法比较取数")
            qb, qt = self._side_query(self.base_queries, b), self._side_query(self.target_queries, t)
            if qb is None or qt is None:
                return _Change("unknown", f"输入「{path}」落在的查询快照不在封存范围内的查询里，无法比较取数")
            return self._query_change(qb, qt)
        nb, nt = b.item.get("node_id"), t.item.get("node_id")
        if nb != nt:
            return _Change("template", f"输入「{path}」的来源节点由「{self.title(nb)}」换成了「{self.title(nt)}」")
        if _from_input(path, b.item):
            if recompute._same(b.item.get("value"), t.item.get("value")):
                return _Change("same")
            return _Change("params", f"运行输入「{path}」由「{_shown(b.item.get('value'))}」"
                                     f"改为「{_shown(t.item.get('value'))}」")
        if nb and (own := self._node_change(str(nb))) is not None:
            # 写它的节点自己查过库（Agent 没按出处交、调用工具节点的结果整份用）：它查的那几条就是喂它的查询
            if self.changed.get(str(nb)) and own.kind in ("same", "params"):
                return _Change("template", f"上游节点「{self.title(nb)}」的配置有变化")
            return own
        if nb and self.changed.get(str(nb)):
            return _Change("template", f"上游节点「{self.title(nb)}」的配置有变化")
        # 写它的节点不查库（代码、数据整形、模型调用……）：看画布上它的全部上游；写它的节点不知道就看整张图
        scope = self._upstream(str(nb)) if nb else set(self.titles)
        moved = [n for n in scope if self.changed.get(n)]
        changes = [self._query_change(b2, t2) for b2, t2 in self.pairs if (t2 or b2).node_id in scope]
        worst = _worst(changes)
        where = f"来自「{self.title(nb)}」" if nb else "不知道由哪个节点写入"
        if moved or worst.kind in ("sql", "template", "unknown"):
            return _Change("unknown", f"输入「{path}」{where}，对不上具体的查询；它的上游有变化，无法判断变化来源")
        return worst

    # ---------------------------------------------------------------- 模板

    def _template(self) -> tuple[dict[str, Any], dict[str, str]]:
        """两次运行的图快照比较：新增、删除的节点，配置有变化的节点和配置项名（不展示值）。changed 是影响取值的
        变化（新增、删除、类型或配置变了；只改名称的不算），判断输入的出处时用。"""
        before, after = _nodes(self.base.graph), _nodes(self.target.graph)
        nodes: list[dict[str, Any]] = []
        changed: dict[str, str] = {}

        def entry(node_id: str, node: dict[str, Any], change: str, keys: list[str]) -> dict[str, Any]:
            return {"node_id": node_id, "title": self._title_of(node), "change": change, "keys": keys,
                    "labels": [_key_label(k, node.get("type")) for k in keys]}

        for node_id, node in after.items():
            was = before.get(node_id)
            if was is None:
                nodes.append(entry(node_id, node, "added", []))
                changed[node_id] = "added"
                continue
            keys, renamed_only = _config_keys(was, node)
            if keys:
                nodes.append(entry(node_id, node, "config", keys))
                if not renamed_only:
                    changed[node_id] = "config"
        for node_id, node in before.items():
            if node_id not in after:
                nodes.append(entry(node_id, node, "removed", []))
                changed[node_id] = "removed"
        same_edges = _edges(self.base.graph) == _edges(self.target.graph)
        same_defaults = (self.base.graph.get("defaults") or {}) == (self.target.graph.get("defaults") or {})
        if not same_edges:
            self.notes.append(EDGES_CHANGED)
        if not same_defaults:
            self.notes.append(DEFAULTS_CHANGED)
        return {"base_version": self.base.run.get("version"), "target_version": self.target.run.get("version"),
                "same_graph": not nodes and same_edges and same_defaults, "nodes": nodes}, changed

    # ---------------------------------------------------------------- 指标

    def _metrics(self) -> list[dict[str, Any]]:
        before, after = _cards(self.base), _cards(self.target)
        if not before and not after:
            self.notes.append(NO_CARDS)
            return []
        out: list[dict[str, Any]] = []
        for node_id in [*after, *(n for n in before if n not in after)]:
            b, t = before.get(node_id), after.get(node_id)
            for side, card in (("基准运行", b), ("这次运行", t)):
                if card is not None and card.runs > 1:
                    self.notes.append(f"口径卡「{self.title(node_id)}」在{side}中执行了 {card.runs} 次，按最后一次对比")
            if b is not None:
                _plan_card(self.base, b, self.base_queries)
            if t is not None:
                _plan_card(self.target, t, self.target_queries)
            for side, card in (("基准运行", b), ("这次运行", t)):
                if card is not None and card.problem:
                    self.notes.append(f"{side}的口径卡「{self.title(node_id)}」{card.problem}，其中的指标无法对比")
            self._card_notes(node_id, b, t)
            ids = [str(m["id"]) for m in (t.metrics() if t else [])]
            ids += [mid for mid in (str(m["id"]) for m in (b.metrics() if b else [])) if mid not in ids]
            for mid in ids:
                bm = next((m for m in (b.metrics() if b else []) if str(m["id"]) == mid), None)
                tm = next((m for m in (t.metrics() if t else []) if str(m["id"]) == mid), None)
                out.append(self._metric(node_id, b, t, bm, tm))
        return out

    def _card_notes(self, node_id: str, b: _Card | None, t: _Card | None) -> None:
        """口径卡本身的变化（口径名称、固定的来源版本）：不改变指标的分类，写进说明。"""
        if b is None or t is None or b.content is None or t.content is None:
            return
        before, after = b.content.get("caliber") or "", t.content.get("caliber") or ""
        if before != after:
            self.notes.append(f"口径卡「{self.title(node_id)}」的口径名称由「{before}」改为「{after}」")
        src_b, src_t = b.content.get("source"), t.content.get("source")
        if src_b != src_t:
            def version(src: Any) -> str:
                return f"v{src.get('workflow_version')}" if isinstance(src, dict) else "本地定义"
            self.notes.append(f"口径卡「{self.title(node_id)}」固定的口径来源由 {version(src_b)} 改为 {version(src_t)}")

    def _metric(self, card_id: str, b: _Card | None, t: _Card | None, bm: dict[str, Any] | None,
                tm: dict[str, Any] | None) -> dict[str, Any]:
        meta = tm or bm or {}
        mid = str(meta.get("id"))
        caliber = next((c.content.get("caliber") for c in (t, b) if c is not None and c.content), None)
        out: dict[str, Any] = {
            # 画布上的节点名；图里找不到这个节点（老运行的图读不出来）就写口径名
            "card_id": card_id, "card_title": self.title(card_id) if card_id in self.titles else caliber or card_id,
            "metric_id": mid, "name": meta.get("name") or mid, "unit": meta.get("unit") or "",
            "decimals": meta.get("decimals"),
            "base": _value(bm), "target": _value(tm),
            "base_rendered": _rendered(bm), "target_rendered": _rendered(tm),
            "delta": None, "delta_rendered": None, "delta_pct": None,
            "classification": INCOMPARABLE, "reasons": [], "decomposition": None, "decomposition_note": None,
            "inputs": [], "attribution": None,
        }
        bp = b.plans.get(mid) if b is not None and bm is not None else None
        tp = t.plans.get(mid) if t is not None and tm is not None else None
        out["inputs"] = self._inputs(bp, tp)
        # 一边有一边没有：整张卡都没有、卡里没有这个指标；卡读不出来的说不清有没有
        for card, metric, cls, side in ((b, bm, ADDED, "基准运行"), (t, tm, REMOVED, "这次运行")):
            if metric is not None:
                continue
            if card is None:
                out.update(classification=cls, reasons=[f"{side}没有口径卡「{self.title(card_id)}」"])
            elif card.content is None:
                out["reasons"] = [f"{side}的口径卡「{self.title(card_id)}」{card.problem}，无法对比"]
            else:
                out.update(classification=cls, reasons=[f"{side}的口径卡「{self.title(card_id)}」里没有这个指标"])
            return out
        assert bm is not None and tm is not None and b is not None and t is not None
        caliber = _caliber_reasons(bm, tm, b.content or {}, t.content or {})
        self._delta(out, bm, tm)
        lineage = isinstance(bm.get("inputs"), list) and isinstance(tm.get("inputs"), list)
        if caliber:
            out.update(classification=CALIBER_AND_DATA, reasons=caliber)
            out["decomposition"], out["decomposition_note"] = self._decompose(bm, tm, b, t, bp, tp)
        elif not lineage:
            quiet = self.template["same_graph"] and all(self.shape(x, y).structure == "same" for x, y in self.pairs)
            same = recompute._same(bm.get("value"), tm.get("value"))
            out.update(classification=UNCHANGED if quiet and same else INCOMPARABLE,
                       reasons=[] if quiet and same else [NO_LINEAGE])
        else:
            paths = list(dict.fromkeys([*tp.inputs, *bp.inputs]))
            changes = [self.provenance(p, bp.inputs.get(p), tp.inputs.get(p)) for p in paths]
            worst = _worst(changes)
            reasons = list(dict.fromkeys(c.reason for c in changes if c.kind == worst.kind and c.reason))
            if worst.kind in ("sql", "template"):
                out.update(classification=SQL_CHANGED, reasons=reasons, decomposition_note=SQL_NOT_SPLIT)
            elif worst.kind == "unknown":
                out.update(classification=INCOMPARABLE, reasons=reasons)
            elif worst.kind == "params":
                out.update(classification=PARAMS_ONLY, reasons=reasons)
            elif recompute._same(bm.get("value"), tm.get("value")):
                out.update(classification=UNCHANGED)
            else:
                linked = all(inp.snapshot for inp in tp.inputs.values()) and bool(tp.inputs)
                out.update(classification=DATA_ONLY, reasons=[DATA_FROM_QUERIES if linked else DATA_FROM_UPSTREAM])
        for side, metric in (("基准运行", bm), ("这次运行", tm)):
            if metric.get("status") == "missing_input":
                out["reasons"].append(f"{side}这个指标缺少输入，没有值")
        if out["classification"] in _ATTRIBUTED and bp is not None and tp is not None:
            out["attribution"] = self._attribution(bp, tp)
        if any(inp.masked for plan in (bp, tp) if plan is not None for inp in plan.inputs.values()):
            self.masked_seen = True
        return out

    def _delta(self, out: dict[str, Any], bm: dict[str, Any], tm: dict[str, Any]) -> None:
        """总变化：两边都是数、数值的含义相同（单位一样、比率与否一样）才相减，按十进制精确算。"""
        b, t = _exact(bm.get("value")), _exact(tm.get("value"))
        if b is None or t is None or _scale(bm) != _scale(tm):
            return
        whole = all(isinstance(m.get("value"), int) for m in (bm, tm))
        delta = _plain(t - b, whole=whole)
        meta = {**tm, "decimals": _places(bm, tm)}
        out.update(delta=delta, delta_rendered=_signed(meta, delta) if delta != 0 else None,
                   delta_pct=float(round((t - b) / abs(b) * 100, 2)) if b != 0 else None)

    def _decompose(self, bm: dict[str, Any], tm: dict[str, Any], b: _Card, t: _Card, bp: Any,
                   tp: Any) -> tuple[dict[str, Any] | None, str | None]:
        """严格拆分：(拆分, None) 或 (None, 为什么没法拆)。"""
        old, new = _exact(bm.get("value")), _exact(tm.get("value"))
        if old is None or new is None:
            return None, NOT_NUMBERS
        if _scale(bm) != _scale(tm):
            return None, "单位或比率的写法不同，两边的数值不能直接相减，不做拆分"
        try:
            leaves = leaf_refs(parse_expression(str(bm.get("expression") or ""))[0])
        except ExpressionError as e:
            return None, f"旧口径的表达式无法解析（{e}），无法严格拆分"
        if bp is None or not isinstance(bm.get("inputs"), list):
            return None, "基准运行的口径卡没有记录输入（生成于系统升级前），无法确认取数是否有变化，无法严格拆分"
        missing = [p for p in leaves if p not in t.inputs]
        if missing:
            more = f"等 {len(missing)} 个输入" if len(missing) > 1 else ""
            return None, f"旧口径用到的「{missing[0]}」{more}在这次运行的口径卡里没有记录，无法严格拆分"
        used = [t.inputs[p] for p in leaves]
        plans = [p for p in (bp, tp) if p is not None]
        if any(inp.masked for inp in used) or any(inp.masked for plan in plans for inp in plan.inputs.values()):
            return None, SPLIT_MASKED
        for path in leaves:
            change = self.provenance(path, bp.inputs.get(path), t.inputs[path])
            if change.kind in ("sql", "template", "unknown"):
                return None, f"旧口径用到的输入取数也有变化，无法严格拆分：{change.reason}"
        # f_old(x_new)：旧表达式配这次的输入，按推演重算的规则还原、求值，取整按旧口径卡的小数位
        synthetic = {"id": bm.get("id"), "expression": bm.get("expression"), "decimals": bm.get("decimals"),
                     "inputs": [inp.item for inp in used], "substituted": None, "value": None}
        assert t.sources is not None
        plan = recompute._plan(synthetic, recompute._Store(t.sources))
        if plan.problem:
            return None, f"无法严格拆分：{plan.problem}"
        for path in plan.tops:
            if problem := plan.inputs[path].problem:
                return None, f"无法严格拆分：这次运行的输入「{path}」无法还原（{problem}）"
        value, reason = recompute._evaluate(plan, {p: inp.value for p, inp in plan.inputs.items()})
        if reason is not None:
            return None, f"按旧口径在这次的数据上无法计算（{reason}），无法严格拆分"
        middle = _exact(value)
        if middle is None:
            return None, "按旧口径在这次的数据上算出的不是有限的数值，无法严格拆分"
        whole = all(isinstance(v, int) and not isinstance(v, bool) for v in (bm.get("value"), tm.get("value"), value))
        caliber, data = _plain(new - middle, whole=whole), _plain(middle - old, whole=whole)
        meta = {**tm, "decimals": _places(bm, tm)}
        return {"caliber_effect": caliber, "data_effect": data, "caliber_rendered": _signed(meta, caliber),
                "data_rendered": _signed(meta, data)}, None

    def _inputs(self, bp: Any, tp: Any) -> list[dict[str, Any]]:
        """两边的输入按路径对齐：值（遮罩列、核对不了遮没遮的不给），各自落在的查询快照。"""
        b_in = bp.inputs if bp is not None else {}
        t_in = tp.inputs if tp is not None else {}
        out = []
        for path in dict.fromkeys([*t_in, *b_in]):
            b, t = b_in.get(path), t_in.get(path)
            entry: dict[str, Any] = {"path": path, "base": _input_value(b), "target": _input_value(t)}
            if any(inp is not None and inp.masked for inp in (b, t)):
                entry["masked"] = True
            if b is not None and b.snapshot:
                entry["base_artifact"] = b.snapshot
            if t is not None and t.snapshot:
                entry["target_artifact"] = t.snapshot
            out.append(entry)
        return out

    # ---------------------------------------------------------------- 维度归因

    def _attribution(self, bp: Any, tp: Any) -> dict[str, Any] | None:
        """挑一个落在查询快照上、两边都变了的输入做维度归因：先找给得出占比的，没有就取第一个列得出行的，再没有
        就取第一个说明（截断、度量列遮罩……）。一个都挑不出来返回 None。"""
        found: list[dict[str, Any]] = []
        for path, t in tp.inputs.items():
            b = bp.inputs.get(path)
            if b is None or not (b.snapshot and t.snapshot) or recompute._same(b.value, t.value):
                continue
            if self.provenance(path, b, t).kind not in ("same", "params"):
                continue
            result = self._attribute(b, t)
            if result is None:
                continue
            if result["rows"] and all(r["share"] is not None for r in result["rows"]):
                return result
            found.append(result)
        return next((r for r in found if r["rows"]), found[0] if found else None)

    def _attribute(self, b: Any, t: Any) -> dict[str, Any] | None:
        """一个输入的维度归因：{columns, measure, rows, total_delta, note}；归不了因的照实写在 note 里（rows 为空）。
        快照里没有可以当维度的列（只有一行汇总这种）返回 None：没什么可归的。"""
        measure = _locator(b).get("column")
        if not isinstance(measure, str) or _locator(t).get("column") != measure:
            return None
        sb, st = self._snap(self.base_queries, b), self._snap(self.target_queries, t)
        empty = {"columns": [], "measure": measure, "rows": [], "total_delta": None}
        if sb is None or st is None:
            return {**empty, "note": UNREADABLE_SNAPSHOT}
        for side, snap in (("基准运行", sb), ("这次运行", st)):
            if snap.get("truncated"):
                # 截断的结果只是前几行：各键的和不是完整的和，按它归因会把没取回的行的变化算到别的键头上
                return {**empty, "note": TRUNCATED.format(side=side, rows=len(snap["rows"]))}
        hidden = (self.base.masked.get(b.snapshot) or frozenset()) | (self.target.masked.get(t.snapshot) or frozenset())
        if any(str(c).lower() in hidden for c in [*(sb.get("columns") or []), *(st.get("columns") or [])]):
            self.masked_seen = True
        if measure.lower() in hidden:
            return {**empty, "note": MASKED_MEASURE.format(column=measure)}
        rb, rt = _records(sb, b), _records(st, t)
        if rb is None or rt is None:
            return None
        values = (_input_total(b), _input_total(t))
        notes: list[str] = []
        if b.row is not None and t.row is not None and not _adds_up(rb, rt, measure, values):
            # 取的是合计行那一格：其余各行之和等于它（两边都等）时按其余各行归因，合计行不列进去
            kb, kt = [x for x in rb if x[0] != b.row], [x for x in rt if x[0] != t.row]
            if kb and kt and len(kb) < len(rb) and len(kt) < len(rt) and _adds_up(kb, kt, measure, values):
                rb, rt = kb, kt
                notes.append(TOTAL_ROW.format(row=t.row + 1))
        dims, apart = _dimensions(sb, st, rb, rt, measure, hidden)
        if not dims:
            return {**empty, "note": "；".join(apart)} if apart else None
        notes += apart
        groups_b, groups_t = _group(rb, dims, measure), _group(rt, dims, measure)
        if groups_b is None or groups_t is None:
            return {**empty, "note": NOT_A_MEASURE.format(column=measure)}
        total_b, total_t = sum(groups_b.values(), Decimal(0)), sum(groups_t.values(), Decimal(0))
        total = total_t - total_b
        # 占比只在这份结果正好是这个输入的拆分时才有意义：各键之和等于输入的值，两边都等
        share = _equal(values[0], total_b) and _equal(values[1], total_t)
        if not share:
            notes.append(NO_SHARE)
        elif total == 0:
            notes.append(ZERO_TOTAL)
        rows = []
        for key in dict.fromkeys([*groups_b, *groups_t]):
            vb, vt = groups_b.get(key), groups_t.get(key)
            delta = (vt or Decimal(0)) - (vb or Decimal(0))
            rows.append((abs(delta), {"key": list(key), "base": None if vb is None else _plain(vb, whole=False),
                                      "target": None if vt is None else _plain(vt, whole=False),
                                      "delta": _plain(delta, whole=False),
                                      "share": float(delta / total) if share and total != 0 else None}))
        rows.sort(key=lambda r: -r[0])         # 稳定排序：变化一样大的按先出现的
        if len(rows) > TOP_KEYS:
            notes.append(MORE_KEYS.format(count=len(rows), top=TOP_KEYS))
        return {"columns": dims, "measure": measure, "rows": [r for _, r in rows[:TOP_KEYS]],
                "total_delta": _plain(total, whole=False), "note": "；".join(notes) or None}

    def _snap(self, queries: list[_Query], inp: Any) -> dict[str, Any] | None:
        q = self._side_query(queries, inp)
        return q.snap if q is not None else None


# --------------------------------------------------------------------------
# 指标、输入的几样小事
# --------------------------------------------------------------------------


def _value(metric: dict[str, Any] | None) -> Any:
    """指标的值：有限的数才给，别的（空值、文本、溢出成无穷大的）给 None，写法看 rendered。"""
    value = (metric or {}).get("value")
    return value if recompute._number(value) else None


def _rendered(metric: dict[str, Any] | None) -> str | None:
    """卡上记的写法（报告里就是这几个字）；没记的按卡上的格式渲染。"""
    if metric is None:
        return None
    rendered = metric.get("rendered")
    return rendered if isinstance(rendered, str) else recompute._render(metric, metric.get("value"))


def _caliber_reasons(bm: dict[str, Any], tm: dict[str, Any], bc: dict[str, Any], tc: dict[str, Any]) -> list[str]:
    """口径变了的地方，一处一句；没变返回空列表。"""
    out = []
    if _expression(bm) != _expression(tm):
        out.append(f"表达式由「{_shown(bm.get('expression'))}」改为「{_shown(tm.get('expression'))}」")
    before, after = str(bc.get("caliber_version") or ""), str(tc.get("caliber_version") or "")
    if before != after:
        out.append(f"口径版本由 {before or '未设置'} 改为 {after or '未设置'}")
    if bm.get("decimals") != tm.get("decimals"):
        def places(d: Any) -> str:
            return "未设置" if d is None else str(d)
        out.append(f"小数位由 {places(bm.get('decimals'))} 改为 {places(tm.get('decimals'))}")
    if (bm.get("unit") or "") != (tm.get("unit") or ""):
        out.append(f"单位由「{bm.get('unit') or '无'}」改为「{tm.get('unit') or '无'}」")
    fb, ft = bm.get("format") or DEFAULT_FORMAT, tm.get("format") or DEFAULT_FORMAT
    if fb != ft:
        out.append(f"显示格式由「{_FORMAT_LABEL.get(fb, fb)}」改为「{_FORMAT_LABEL.get(ft, ft)}」")
    return out


def _input_value(inp: Any) -> Any:
    """输入在对比里给人看的值：遮罩列、落在读不出来的快照上的不给；容器写卡上记的大概。"""
    if inp is None or inp.masked or (inp.unreadable and inp.shape == "scalar"):
        return None
    if inp.shape == "container":
        return inp.item.get("value")
    return recompute._finite(inp.item.get("value"))


def _locator(inp: Any) -> dict[str, Any]:
    locator = inp.item.get("locator")
    return locator if isinstance(locator, dict) else {}


def _records(snap: dict[str, Any], inp: Any) -> list[tuple[int, dict[str, Any]]] | None:
    """归因用到的行：(行号, {列: 值})。数组字段只用它那一段行，单格用整份结果。"""
    columns = [str(c) for c in snap.get("columns") or []]
    rows = snap.get("rows") or []
    first, last = (inp.rows if inp.rows is not None else (0, len(rows) - 1))
    out = []
    for i in range(first, min(last, len(rows) - 1) + 1):
        row = rows[i]
        if isinstance(row, dict):
            out.append((i, {str(k): v for k, v in row.items()}))
        elif isinstance(row, (list, tuple)) and len(row) == len(columns):
            out.append((i, dict(zip(columns, row))))
        else:
            return None
    return out


def _kind(snap: dict[str, Any], column: str) -> str | None:
    types = snap.get("column_types")
    kind = types.get(column) if isinstance(types, dict) else None
    return kind if isinstance(kind, str) else None


def _numeric(values: Iterable[Any], kind: str | None) -> bool:
    """这一列是数值列：查询时记的类型是 number，或者没记类型、值（数字文本也算）全是数。"""
    if kind is not None:
        return kind == "number"
    present = [v for v in values if v is not None]
    return bool(present) and all(is_number(cell_value(v)) for v in present)


def _dimensions(sb: dict[str, Any], st: dict[str, Any], rb: list[tuple[int, dict[str, Any]]],
                rt: list[tuple[int, dict[str, Any]]], measure: str,
                hidden: frozenset[str] | set[str]) -> tuple[list[str] | None, list[str]]:
    """维度列：(列名列表, 说明)。两边都有、不是数值、不是日期类型、没遮罩的列；两边各自都只有一个值的不算（分不出
    维度）；两边没有一个值相同的不拿来配对（日期、期次这类每期都不同的列），写进说明。一个候选都没有返回 None。"""
    common = [c for c in (str(c) for c in sb.get("columns") or []) if c in {str(x) for x in st.get("columns") or []}]
    candidates = []
    for column in common:
        if column == measure or column.lower() in hidden:
            continue
        kinds = (_kind(sb, column), _kind(st, column))
        if any(k in _TEMPORAL for k in kinds):
            continue
        vb, vt = [r.get(column) for _, r in rb], [r.get(column) for _, r in rt]
        if _numeric(vb, kinds[0]) or _numeric(vt, kinds[1]):
            continue
        candidates.append((column, vb, vt))
    if not candidates:
        return None, []
    dims, apart = [], []
    for column, vb, vt in candidates:
        seen_b, seen_t = {_hashable(v) for v in vb}, {_hashable(v) for v in vt}
        if len(seen_b) <= 1 and len(seen_t) <= 1:
            continue
        if seen_b and seen_t and not seen_b & seen_t:
            apart.append(column)
            continue
        dims.append(column)
    return dims, ([NO_OVERLAP.format(columns="、".join(apart))] if apart else [])


def _hashable(value: Any) -> Any:
    return value if isinstance(value, (str, int, float, bool)) or value is None else repr(value)


def _measure(raw: Any) -> Decimal | None:
    """度量列的一格，十进制：空值按 0（SQL 的 SUM 也不计空值），数字文本按数，别的不是数返回 None。"""
    return Decimal(0) if raw is None else _exact(cell_value(raw))


def _group(rows: list[tuple[int, dict[str, Any]]], dims: list[str],
           measure: str) -> dict[tuple[Any, ...], Decimal] | None:
    """按维度键汇总度量：{键: 和}（去掉了期次列之后同一个键可能有几行）。有不是数值的格返回 None。"""
    out: dict[tuple[Any, ...], Decimal] = {}
    for _, row in rows:
        number = _measure(row.get(measure))
        if number is None:
            return None
        key = tuple(_hashable(row.get(d)) for d in dims)
        out[key] = out.get(key, Decimal(0)) + number
    return out


def _input_total(inp: Any) -> Decimal | None:
    """输入的值（数组字段是各项之和），十进制。不是数的给 None：这时不给占比。"""
    value = inp.value
    if isinstance(value, list):
        parts = [_exact(cell_value(v)) for v in value if v is not None]
        return sum(parts, Decimal(0)) if all(p is not None for p in parts) else None  # type: ignore[arg-type]
    return _exact(cell_value(value))


def _adds_up(rb: list[tuple[int, dict[str, Any]]], rt: list[tuple[int, dict[str, Any]]], measure: str,
             values: tuple[Decimal | None, Decimal | None]) -> bool:
    """度量列之和等于输入的值（两边都等）。"""
    for rows, value in ((rb, values[0]), (rt, values[1])):
        parts = [_measure(row.get(measure)) for _, row in rows]
        if any(p is None for p in parts) or not _equal(value, sum(parts, Decimal(0))):  # type: ignore[arg-type]
            return False
    return True


def _equal(value: Decimal | None, total: Decimal) -> bool:
    """输入的值和度量之和相等：十进制逐位比；浮点算出来的值差在最后几位的也算（相对误差 1e-9 以内）。"""
    if value is None:
        return False
    if value == total:
        return True
    scale = max(abs(value), abs(total))
    return scale != 0 and abs(value - total) <= scale * Decimal("1e-9")
