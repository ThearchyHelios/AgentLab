"""从钉住的口径版本确定性地派生只读小图：全站唯一的生成器（docs/design/next-round/00-总方案.md X3）。

**为什么只有一份。** 评测的口径题要算期望（细案 01 做法 8：在同一份数据上把口径重算一遍），口径账本的对账要在同一份
数据上把「这次的取数」和口径各算一次（细案 02 做法 6），召回要把新旧两版口径各算一次（做法 7）。三处要的是同一件事：
从一个不可变的已发布版本里，把口径卡读的取数节点原样拷出来，后面接一张 caliber_from 固定到那一版的口径卡，跑一次探索
运行。各写一份，口径的取法迟早走样——评测量出来的「对」和账本说的「等价」就不是同一个口径。

**派生图长什么样**（assemble）：入口（字段照抄钉住版本的入口：类型、必填、缺省值都不变）→ 各段（Block）首尾相接 →
收尾的卡（对账卡、召回卡，这一波没有）→ 出口（每张口径卡一个字段）。一段 = 那一版口径卡读的取数节点（id、配置原样
照抄；图级 defaults 也照抄，节点上没写的项按同样的规则取值）+ 一张本地口径卡，caliber_from 固定到那一版的那张卡
（名字、版本、指标式、取整、单位都按那一版，metrics 节点的 _pinned_caliber），「缺输入时」照抄原卡。节点一律首尾相接：
同一次运行里先后查，差值只反映口径（或这次的取数）和数据，不反映图的形状。

**能派生的只有可复用的卡**（load_pinned）：口径卡本身是源头（不是转抄别处的 caliber_from）、每个输入都是
cell(nodes.X, 0, '列')、X 是查库的调用工具节点（look/calibers.card_inputs，和蓝图复用口径同一个认法），而且取数节点的
模板只引用入口字段（{{ input.字段 }}）——引用 vars.、nodes.、模板函数（file_text……）的，拷出来就拿不到同样的值，照实
拒收、给原因（NotDerivable）。工作流、版本、节点不在了也照实说。

**已实现的**：caliber_probe —— 一份口径的取数 + 口径卡（评测口径题的期望）；reconcile —— 口径对账（第 3 波，
reconcile_graph）；组合式问数用的 caliber_block(card_id, inputs)（第 3 波）。

**扩展点**（第 3 波的账本按这里扩，不另写生成器）：
- 口径对账（02 做法 6）：assemble("reconcile", [ours, caliber_block(pinned)], tail=[对账卡])。ours 是「这次的取数」
  （执行过的 SQL 原文，节点 id ours_q1…，build 一个 Block 即可，不经 caliber_block），对账卡是本地 metrics 节点（差值、
  相对差，式子引用 cell(nodes.ours_q1, 0, '列') 和口径卡的指标，on_missing=null），出口的契约写 metrics_from。
- 口径召回（02 做法 7）：assemble("recall", [caliber_block(old, prefix="old_"), caliber_block(new, prefix="new_")],
  tail=[召回卡])。同一张图里放两份口径，取数节点 id 会撞：caliber_block 带 prefix 时把拷出来的节点改名，口径卡的
  caliber_from 带上改名映射 nodes={原 id: 新 id}，运行时 metrics._pinned_caliber 按语法树改写式子里的 cell(nodes.X…)
  和 nodes.X（第 3 波起）。
- 组合式问数（02 做法 4，ledger/compose.py）：不经 assemble（拷进的是问数据那张图），只用 caliber_block(prefix=…,
  card_id=…, inputs=…)：取数节点改名、模板按助手给的参数渲染成字面 SQL，口径卡带改名映射。
- 新的派生种类登记进 KINDS，入口字段、出口、首尾相接都走 assemble，不在调用方拼节点。

派生图都是探索运行（正式运行只从已发布未退役的版本发起）；来源（origin_kind）由调用方定：评测 eval、对账 reconcile、
召回 recall，都只能由服务端写（engine/origin.py）。DerivedGraph.origin 是记进运行 origin 的那段派生说明。
"""
from __future__ import annotations

import copy
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

#: 派生图的种类。caliber_probe、reconcile 已实现；recall 归第 4 波（见模块说明的扩展点）
KINDS = ("caliber_probe", "reconcile", "recall")

#: 不能派生的原因的机器码
GONE = "derive.version_gone"
NOT_CARD = "derive.not_caliber_card"
NOT_REUSABLE = "derive.not_reusable"
TEMPLATE = "derive.fetch_template"

#: 派生图自己的入口、出口节点 id（和拷出来的节点撞了就加后缀）
ENTRY_ID = "derived_in"
EXIT_ID = "derived_out"
#: 节点在画布上的横向间距（派生图也能在运行详情里画出来）
_STEP_X = 260

_TEMPLATE_RE = re.compile(r"\{\{(.*?)\}\}", re.DOTALL)
#: 取数节点的模板只许引用入口字段：{{ input.字段 }}，可以带过滤器
_INPUT_REF = re.compile(r"^input\.([^\s.()\[\]]+)$")

TEMPLATE_REASON = ("取数节点「{node}」的模板引用了「{ref}」：只能引用入口字段（{{{{ input.字段 }}}}），别的值（vars.、nodes.、"
                   "模板函数）在派生的图里拿不到同样的值")


class NotDerivable(Exception):
    """不能从这一版派生只读图。message 给人看（照实说哪一环不行），code 给程序认。"""

    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.message = message
        self.code = code


@dataclass(frozen=True)
class Pinned:
    """读好的一版口径：那一版的图、口径卡、它读的取数节点、入口字段。只从不可变的版本快照读。"""

    workflow_id: str
    workflow_name: str
    version: int
    node_id: str
    #: 这一版发布时的级别（governed / published），没发布过的草稿为空
    level: str | None
    #: 这一版是不是「已发布且未退役」（apps/versions.is_published）。派生不拦：升版、退役怎么处置由调用方按
    #: engine/governance.unresolved_caliber_upgrades 和 apps/versions.subgraph_pin_problems 判（同正式运行）
    published: bool
    retired: bool
    card: dict[str, Any]
    #: 口径卡读的取数节点（图里的原始 JSON），按图里的先后
    fetches: tuple[dict[str, Any], ...]
    #: card_inputs 认出的 [(取数节点, 工具, 列)]
    reads: tuple[tuple[str, str, tuple[str, ...]], ...]
    #: 入口节点声明的字段（原始 JSON：name、type、required、default……）
    entry_fields: tuple[dict[str, Any], ...]
    #: 图级 defaults（节点上没写的配置按它取，照抄才和原图取值一致）
    defaults: dict[str, Any]

    @property
    def card_title(self) -> str:
        data = self.card.get("data") if isinstance(self.card.get("data"), dict) else {}
        return str(data.get("label") or self.node_id)

    @property
    def card_config(self) -> dict[str, Any]:
        data = self.card.get("data") if isinstance(self.card.get("data"), dict) else {}
        return data.get("config") if isinstance(data.get("config"), dict) else {}

    @property
    def metrics(self) -> list[dict[str, Any]]:
        """这张卡的指标定义（id、名称、单位、小数位、式子），只读。"""
        return [m for m in self.card_config.get("metrics") or [] if isinstance(m, dict) and m.get("id")]

    @property
    def caliber(self) -> str:
        cfg = self.card_config
        return f"{cfg.get('caliber') or self.card_title} @ {cfg.get('caliber_version') or 'v1'}"

    def ref(self) -> dict[str, Any]:
        """caliber_from 的写法（metrics 节点认的形状）。"""
        return {"workflow_id": self.workflow_id, "workflow_version": self.version, "node_id": self.node_id}

    def describe(self) -> dict[str, Any]:
        """记进运行 origin 的那一份：哪个工作流哪一版的哪张卡。"""
        return {**self.ref(), "workflow_name": self.workflow_name, "card": self.card_title}


@dataclass
class Block:
    """派生图里的一段：照抄（或这次执行过）的取数节点 + 一张口径卡。rename 是拷出来的节点改了名的映射（不带前缀时为空）。
    card_id 为 None 的段没有口径卡（对账里「这次的取数」那一段只有一条查询）。"""

    nodes: list[dict[str, Any]]
    card_id: str | None
    rename: dict[str, str] = field(default_factory=dict)
    #: 这一段出自哪里（记进 DerivedGraph.origin）
    source: dict[str, Any] | None = None


@dataclass(frozen=True)
class DerivedGraph:
    kind: str
    graph: dict[str, Any]
    #: 各段的口径卡节点 id，按段的先后（出口按它们取指标）
    card_ids: tuple[str, ...]
    entry_fields: tuple[dict[str, Any], ...]
    #: 记进运行 origin 的派生说明：{kind, from: [各段的出处]}
    origin: dict[str, Any]


# ==========================================================================
# 读一版口径
# ==========================================================================


def _node_of(graph: Mapping[str, Any], node_id: str) -> dict[str, Any] | None:
    return next((n for n in graph.get("nodes") or [] if isinstance(n, dict) and n.get("id") == node_id), None)


def _config(node: Mapping[str, Any]) -> dict[str, Any]:
    data = node.get("data") if isinstance(node.get("data"), dict) else {}
    return data.get("config") if isinstance(data.get("config"), dict) else {}


def _strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for v in value.values():
            yield from _strings(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from _strings(v)


def template_problem(node_id: str, config: Mapping[str, Any]) -> str | None:
    """取数节点的配置里引用了入口字段以外的东西：给人看的原因；只引用入口字段（或什么都不引用）为 None。"""
    for text in _strings(config):
        for match in _TEMPLATE_RE.finditer(text):
            expr = match.group(1).strip()
            path = expr.split("|")[0].strip()
            if not _INPUT_REF.match(path):
                return TEMPLATE_REASON.format(node=node_id, ref=path or "{{ }}")
    return None


def entry_fields(graph: Mapping[str, Any]) -> tuple[dict[str, Any], ...]:
    """图里入口节点声明的字段（原始 JSON，照抄）。几个入口节点的字段按先后合在一起，同名的取第一个。"""
    out: dict[str, dict[str, Any]] = {}
    for node in graph.get("nodes") or []:
        if not isinstance(node, dict) or node.get("type") != "input":
            continue
        for f in _config(node).get("fields") or []:
            if isinstance(f, dict) and isinstance(f.get("name"), str) and f["name"].strip():
                out.setdefault(f["name"].strip(), copy.deepcopy(f))
    return tuple(out.values())


async def load_pinned(session: AsyncSession, workflow_id: str, version: int, node_id: str) -> Pinned:
    """读 workflow_id@version 里的口径卡 node_id。不能派生的抛 NotDerivable（照实说哪一环不行）。"""
    from app.apps import versions as app_versions
    from app.db.models import Workflow
    from app.look.calibers import card_inputs

    workflow = await session.get(Workflow, str(workflow_id or ""))
    if workflow is None:
        raise NotDerivable("口径所在的工作流不存在，可能已被删除", GONE)
    row = await app_versions.version_row(session, workflow.id, int(version))
    if row is None:
        raise NotDerivable(f"「{workflow.name}」没有 v{version} 这个版本", GONE)
    graph = row.graph or {}
    card = _node_of(graph, node_id)
    if card is None:
        raise NotDerivable(f"「{workflow.name}」v{version} 里没有节点 {node_id}", GONE)
    if card.get("type") != "metrics":
        raise NotDerivable(f"「{workflow.name}」v{version} 里的 {node_id} 不是口径卡", NOT_CARD)
    inputs, reason = card_inputs(graph, node_id)
    if reason is not None:
        title = (card.get("data") or {}).get("label") or node_id
        raise NotDerivable(f"「{workflow.name}」v{version} 的口径卡「{title}」不能复用：{reason}", NOT_REUSABLE)
    wanted = {i["node"] for i in inputs}
    fetches = tuple(copy.deepcopy(n) for n in graph.get("nodes") or []
                    if isinstance(n, dict) and n.get("id") in wanted)
    for node in fetches:
        if problem := template_problem(str(node["id"]), _config(node)):
            raise NotDerivable(f"「{workflow.name}」v{version} 的{problem}", TEMPLATE)
    return Pinned(
        workflow_id=workflow.id, workflow_name=workflow.name, version=int(version), node_id=node_id,
        level=row.level, published=app_versions.is_published(row, current=workflow.published_version),
        retired=row.retired_at is not None, card=copy.deepcopy(card), fetches=fetches,
        reads=tuple((i["node"], i["tool"], tuple(i["columns"])) for i in inputs),
        entry_fields=entry_fields(graph), defaults=copy.deepcopy(graph.get("defaults") or {}))


# ==========================================================================
# 拼图
# ==========================================================================


def _node(nid: str, ntype: str, label: str, **config: Any) -> dict[str, Any]:
    return {"id": nid, "type": ntype, "position": {"x": 0, "y": 0}, "data": {"label": label, "config": config}}


def render_inputs(value: Any, inputs: Mapping[str, Any]) -> Any:
    """配置里每一段字的 {{ input.字段 }} 按 inputs 渲染成字面值（和运行时同一个渲染器 engine/expressions.render_template）。
    load_pinned 已经保证取数节点的模板只引用入口字段。别的值原样。"""
    from app.engine.expressions import render_template

    if isinstance(value, str):
        return render_template(value, {"input": dict(inputs)}) if "{{" in value else value
    if isinstance(value, dict):
        return {k: render_inputs(v, inputs) for k, v in value.items()}
    if isinstance(value, list):
        return [render_inputs(v, inputs) for v in value]
    return value


def caliber_block(pinned: Pinned, *, prefix: str = "", upgrade_policy: str | None = None,
                  card_id: str | None = None, inputs: Mapping[str, Any] | None = None) -> Block:
    """一份口径：拷出取数节点（id、配置原样；prefix 不为空时改名为 prefix + 原 id）+ 一张 caliber_from 固定到那一版的
    口径卡（id 同原卡，带前缀时同样改名；「缺输入时」照抄原卡）。

    upgrade_policy：钉住的版本有了新的已发布版本时这一段怎么处置（recompute / dual / incomparable，没有缺省）。写在
    本地口径卡上，判断走和正式运行同一个 engine/governance.unresolved_caliber_upgrades；不给就不写（有新版本时那个
    判断报「待处置」）。带前缀时口径卡的 caliber_from 带改名映射 nodes（metrics 运行时按它改写式子，见模块说明）。

    组合式问数（ledger/compose.py）另给两项：card_id 是口径卡在新图里的 id（缺省 prefix + 原卡 id）；inputs 给了的话，
    取数节点配置里的 {{ input.字段 }} 按它渲染成字面值——拷进问数据的图里，那张图的入口没有口径的入口字段。"""
    rename = {str(n["id"]): f"{prefix}{n['id']}" for n in pinned.fetches} if prefix else {}
    nodes: list[dict[str, Any]] = []
    for original in pinned.fetches:
        node = copy.deepcopy(original)
        node["id"] = rename.get(str(node["id"]), str(node["id"]))
        node["position"] = {"x": 0, "y": 0}
        if inputs is not None and isinstance(node.get("data"), dict):
            node["data"]["config"] = render_inputs(node["data"].get("config") or {}, inputs)
        nodes.append(node)
    source = pinned.ref()
    if rename:
        source = {**source, "nodes": dict(rename)}
    config: dict[str, Any] = {"caliber_from": source}
    on_missing = pinned.card_config.get("on_missing")
    if on_missing not in (None, ""):
        config["on_missing"] = on_missing
    if upgrade_policy:
        config["upgrade_policy"] = upgrade_policy
    card_id = card_id or f"{prefix}{pinned.node_id}"
    nodes.append(_node(card_id, "metrics", pinned.card_title, **config))
    return Block(nodes=nodes, card_id=card_id, rename=rename, source=pinned.describe())


def _free_id(wanted: str, taken: set[str]) -> str:
    nid, n = wanted, 2
    while nid in taken:
        nid, n = f"{wanted}_{n}", n + 1
    return nid


def assemble(kind: str, blocks: Sequence[Block], *, fields: Sequence[Mapping[str, Any]] = (),
             defaults: Mapping[str, Any] | None = None, tail: Sequence[dict[str, Any]] = (),
             contract: Mapping[str, Any] | None = None) -> DerivedGraph:
    """入口 → 各段（首尾相接）→ tail（对账卡、召回卡）→ 出口。出口每张口径卡（含 tail 里的 metrics 节点）一个字段，
    取那张卡的指标清单；contract 给了就写成出口的出具契约（对账写 metrics_from 指向对账卡，不加报告、不调模型）。几段的
    节点 id 撞了直接报错：两份口径放进同一张图要带前缀（caliber_block 的 prefix）。"""
    if kind not in KINDS:
        raise ValueError(f"不认识的派生图种类：{kind}")
    body: list[dict[str, Any]] = [n for b in blocks for n in b.nodes] + [copy.deepcopy(n) for n in tail]
    ids = [str(n["id"]) for n in body]
    dup = sorted({i for i in ids if ids.count(i) > 1})
    if dup:
        raise ValueError(f"派生图里节点 id 重复：{'、'.join(dup)}（两份口径放进同一张图要带前缀）")
    taken = set(ids)
    entry = _free_id(ENTRY_ID, taken)
    exit_ = _free_id(EXIT_ID, taken | {entry})
    cards = [b.card_id for b in blocks if b.card_id] + [str(n["id"]) for n in tail if n.get("type") == "metrics"]
    titles = {str(n["id"]): str((n.get("data") or {}).get("label") or n["id"]) for n in body}
    outputs, seen = [], set()
    for cid in cards:
        name = _free_id(titles.get(cid, cid), seen)
        seen.add(name)
        outputs.append({"name": name, "value": f"{{{{ nodes.{cid}.text }}}}"})
    exit_config: dict[str, Any] = {"fields": outputs}
    if contract:
        exit_config["contract"] = copy.deepcopy(dict(contract))
    chain = [_node(entry, "input", "入口", fields=[copy.deepcopy(dict(f)) for f in fields]), *body,
             _node(exit_, "output", "出口", **exit_config)]
    for i, node in enumerate(chain):
        node["position"] = {"x": i * _STEP_X, "y": 0}
    graph: dict[str, Any] = {
        "nodes": chain,
        "edges": [{"source": a["id"], "target": b["id"]} for a, b in zip(chain, chain[1:])],
    }
    if defaults:
        graph["defaults"] = copy.deepcopy(dict(defaults))
    origin = {"kind": kind, "from": [b.source for b in blocks if b.source]}
    return DerivedGraph(kind=kind, graph=graph, card_ids=tuple(cards),
                        entry_fields=tuple(copy.deepcopy(dict(f)) for f in fields), origin=origin)


#: 对账图里「这次的取数」节点、对账卡、多行时按键对齐的合并查询的 id（细案 02 做法 6）
OURS_ID = "ours_q1"
RECONCILE_CARD = "reconcile"
ALIGN_ID = "align"


def reconcile_prefix(pinned: Pinned) -> str:
    """对账图里口径那一段的前缀：口径的节点 id 和 ours_q1 / align / reconcile / 入口出口撞了才带 cal_，不撞不带。"""
    reserved = {OURS_ID, ALIGN_ID, RECONCILE_CARD, ENTRY_ID, EXIT_ID}
    clash = any(str(n.get("id")) in reserved for n in pinned.fetches) or pinned.node_id in reserved
    return "cal_" if clash else ""


def ours_block(sql: str, tool: str, *, label: str = "这次的取数") -> Block:
    """对账里「这次的取数」：这次执行过的 SQL 原文，放进一个调用工具节点（id ours_q1）。没有口径卡。"""
    return Block(nodes=[_node(OURS_ID, "tool", label, tool=tool, args={"sql": sql})], card_id=None,
                 source={"kind": "executed", "node_id": OURS_ID})


def reconcile_graph(pinned: Pinned, ours: Block, tail: Sequence[dict[str, Any]], *,
                    inputs: Mapping[str, Any]) -> DerivedGraph:
    """口径对账（细案 02 做法 6）：入口（口径的入口字段，缺省值是这次的参数）→ 这次的取数 → 口径的取数（模板里保留
    {{ input.x }}，运行时按入口渲染）→ 口径卡（caliber_from 固定那一版）→ tail（多行时的对齐合并查询、对账卡）→ 出口
    （契约 metrics_from 指向对账卡；不加报告、不调模型）。两边在同一次运行里先后查，差值在对账卡里算。

    口径的节点 id 和 ours_q1 / align / reconcile 撞了的，口径那一段带前缀 cal_（改名映射照常写进 caliber_from.nodes）。
    同样的输入永远得到同一张图。"""
    block = caliber_block(pinned, prefix=reconcile_prefix(pinned))
    fields = []
    for f in pinned.entry_fields:
        field_ = copy.deepcopy(dict(f))
        if field_.get("name") in inputs:
            field_["default"] = inputs[field_["name"]]
        fields.append(field_)
    card = next((n for n in tail if n.get("type") == "metrics" and n.get("id") == RECONCILE_CARD), None)
    return assemble("reconcile", [ours, block], fields=fields, defaults=pinned.defaults, tail=tail,
                    contract={"metrics_from": RECONCILE_CARD} if card is not None else None)


def caliber_probe(pinned: Pinned, *, upgrade_policy: str | None = None) -> DerivedGraph:
    """口径探针：一份口径的取数 + 口径卡（评测口径题的期望，细案 01 做法 8）。节点 id 和原图一样，入口字段照抄。
    同样的 pinned 永远得到同一张图（确定性）。"""
    return assemble("caliber_probe", [caliber_block(pinned, upgrade_policy=upgrade_policy)],
                    fields=pinned.entry_fields, defaults=pinned.defaults)


__all__ = ["ALIGN_ID", "Block", "DerivedGraph", "ENTRY_ID", "EXIT_ID", "GONE", "KINDS", "NOT_CARD", "NOT_REUSABLE",
           "NotDerivable", "OURS_ID", "Pinned", "RECONCILE_CARD", "TEMPLATE", "assemble", "caliber_block",
           "caliber_probe", "entry_fields", "load_pinned", "ours_block", "reconcile_graph", "reconcile_prefix", "render_inputs",
           "template_problem"]
