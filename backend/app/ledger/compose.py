"""组合式问数：口径做骨架，Agent 填缺口（docs/design/next-round/02-口径账本.md 做法 4，拍板 L3）。

问数据的助手看到已发布口径（look 的 caliber，问数据里另给入口字段和「能不能组合」，不给 SQL）之后，可以输出一行

  {"op":"use_caliber","id":"c1","workflow_id":"…","version":3,"card":"card","metrics":["gmv"],"inputs":{"start":"…"}}

选用一份口径。**取数节点不让模型写**：由服务端按那一版确定性地内联实例化（ledger/derive.caliber_block，全站唯一的
派生图生成器）——口径卡读的取数节点照抄，节点 id 加前缀「c1_」，模板里的 {{ input.字段 }} 按 inputs 渲染成字面 SQL
（和运行时同一个渲染器）；口径卡 id 就是 c1，caliber_from 固定到那一版的那张卡、带改名映射 {原 id: c1_原 id}
（metrics 运行时按它改写式子）。取数节点首尾相接、最后一个接口径卡；入口连到第一个取数节点（GraphApplier 接上）。
模型只管把口径卡 c1 接到报告、报告里引用 [[m:指标 id]]。

**为什么不用子图节点。** 子图的证据不交回父图（报告引用不到子图里的数），使用者也用不了子工作流（auth/chat_policy）。
内联实例化只用到 tool 和 metrics 两种已放行的节点，证据照常封存，比对也照常复证（ledger/match 认出这些查询是助手
选用的，标 composed「助手选用」，不标「等价已证明」：选哪份口径是助手凭问法判断的）。

**参数校验**（不合规拒收，原因交回模型改，GraphApplier.refusals）：
- 只能选这一轮 look 能看到的那一版（每个工作流最新的已发布、未退役版本），而且那张卡能组合（derive.load_pinned：卡是
  源头、每个输入都是取数节点第一行的格、取数模板只引用入口字段）；
- inputs 只认口径入口声明的字段，没写的补缺省值，再过 apps/forms 同一套表单校验（类型、可选值、必填、格式）；
- 参数要渲染进 SQL：写在引号里的不能带引号、反斜杠、换行；裸写的只能是数；写在注释里的不能带换行和注释结束符——
  否则渲染出来的就不是那一版口径的 SQL 了；
- metrics 只能是这张卡上的指标 id；id 不能和图里已有的节点撞。

实例化是确定的：同样的版本、同样的操作得到一模一样的节点（测试锁住）。这里只是纯计算：版本快照由 look/calibers 的
CaliberKind 在开流之前用会话读好（GraphApplier.apply 是同步的，流里不碰库）。
"""
from __future__ import annotations

import copy
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from app.ledger.derive import Pinned, caliber_block

#: 操作名
OP = "use_caliber"
#: 拒收时的问题编号（自查交回模型、过程里照实写一行）
REFUSED = "ledger.use_caliber_refused"
#: 操作 id：口径卡的节点 id，取数节点加它做前缀。简短英文小写下划线（同节点 id 的约定）
_OP_ID = re.compile(r"^[a-z][a-z0-9_]{0,23}$")
_NUMBER = re.compile(r"^\s*-?\d+(?:\.\d+)?\s*$")

#: 写给模型的拒收说明（也上界面：过程里那一行）
NOT_LISTED = "只能选用这一轮查看到的已发布口径：「{workflow}」v{version} 不在里面（口径要先 look，版本以查看结果为准）"
WRONG_VERSION = "「{workflow}」这一轮能选用的是 v{latest}（最新的已发布版本），不是 v{version}"
#: card 写错时交回的原因要能一次改对：说清 card 填什么、可填哪几个（节点 id）；写成了自己起的 id 的另说一句
CARD_UNKNOWN = ("「{workflow}」v{version} 里没有口径卡「{card}」：card 要填那一版工作流图里口径卡的节点 id，可填的有 {cards}"
                "（照抄查看结果里 use_caliber 写法中的 card）")
CARD_IS_OP_ID = "；「{card}」是你给这次选用起的 id，不是口径卡的节点 id"
CARD_AMBIGUOUS = "「{workflow}」v{version} 有几张口径卡（节点 id：{cards}），请在 card 里写选用哪一张的节点 id"
NOT_COMPOSABLE = "「{workflow}」v{version} 的口径卡「{card}」不能组合：{reason}"
BAD_ID = "use_caliber 的 id 要写成简短的英文小写加下划线（例如 c1），它就是口径卡的节点 id，当前为「{id}」"
ID_TAKEN = "use_caliber 的 id「{id}」和图里已有的节点「{node}」撞了，请换一个"
NO_METRICS = "use_caliber 的 metrics 要写这张口径卡上要用的指标 id（有：{metrics}）"
METRIC_UNKNOWN = "口径卡「{card}」上没有指标「{metric}」（有：{metrics}）"
FIELD_UNKNOWN = "口径「{workflow}」的入口没有字段「{name}」（有：{fields}）"
INPUTS_NOT_OBJECT = "use_caliber 的 inputs 要写成字段和值的对照（JSON 对象）"
QUOTED_VALUE = "参数「{name}」写在 SQL 的引号里，值里不能有引号、反斜杠或换行"
BARE_VALUE = "参数「{name}」直接写在 SQL 里（没有引号），只能是数，当前为「{value}」"
COMMENT_VALUE = "参数「{name}」写在 SQL 的注释里，值里不能有换行或注释结束符"
TEMPLATE_UNREADABLE = "取数节点的 SQL 模板认不出参数写在哪：{reason}"


class ComposeRefused(Exception):
    """不实例化这次选用：message 给模型看、也写进过程里那一行。"""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


@dataclass(frozen=True)
class Option:
    """这一轮能选用的一张口径卡：读好的那一版（pinned），或者不能组合的原因（reason）。"""

    workflow_id: str
    workflow_name: str
    version: int
    level: str
    node_id: str
    card: str
    pinned: Pinned | None = None
    reason: str | None = None

    @property
    def composable(self) -> bool:
        return self.pinned is not None and self.reason is None


@dataclass
class Composed:
    """一次选用实例化出来的东西：要加的节点、连线（取数首尾相接、最后接口径卡），交给界面和模型的结果。"""

    op_id: str
    option: Option
    nodes: list[dict[str, Any]]
    edges: list[dict[str, Any]]
    metrics: list[dict[str, Any]]
    inputs: dict[str, Any] = field(default_factory=dict)

    @property
    def fetch_ids(self) -> list[str]:
        return [str(n["id"]) for n in self.nodes if n.get("type") != "metrics"]

    def result(self) -> dict[str, Any]:
        """交给界面（过程里那一行）、记进这一轮的操作流的结果。不放 SQL、不放参数值（参数值助手自己写的，界面不必再给）。"""
        from app.ledger.labels import LEVEL_TEXT

        return {"status": "ok", "workflow_id": self.option.workflow_id, "workflow_name": self.option.workflow_name,
                "version": self.option.version, "level": self.option.level,
                "level_text": LEVEL_TEXT.get(self.option.level, ""), "card": self.option.card, "card_id": self.op_id,
                "fetches": self.fetch_ids, "metrics": [{"id": m["id"], "name": m["name"]} for m in self.metrics],
                "nodes": len(self.nodes)}


def signature(op: Mapping[str, Any]) -> str:
    """一次选用的身份（接着写时重发的同一个操作认得出来）：去掉服务端写上的结果。"""
    return json.dumps({k: v for k, v in op.items() if k != "result"}, ensure_ascii=False, sort_keys=True, default=str)


def _names(items: list[str]) -> str:
    return "、".join(items[:12]) or "无"


def _slot_contexts(pinned: Pinned) -> dict[str, set[str]]:
    """取数 SQL 模板里每个入口字段写在哪：{字段: {quoted / bare / comment}}。认不出的抛 ComposeRefused。"""
    return slot_contexts(pinned.fetches)


def slot_contexts(nodes: Any, *, strict: bool = True) -> dict[str, set[str]]:
    """几个调用工具节点的 args.sql 里每个入口字段写在哪（见 _slot_contexts）。按口径正式出具（ledger/issue）也按它查参数：
    strict=False 时认不出写法的那条（引用了运行时的变量）跳过，不抛——那张图里别的节点怎么写不归出具管。"""
    from app.data.sql_semantics import _NotComparable, _template_slots

    out: dict[str, set[str]] = {}
    for node in nodes:
        if not isinstance(node, dict):
            continue
        args = ((node.get("data") or {}).get("config") or {}).get("args")
        sql = args.get("sql") if isinstance(args, dict) else None
        if not isinstance(sql, str):
            continue
        try:
            spans, _ = _template_slots(sql)
        except _NotComparable as e:
            if not strict:
                continue
            raise ComposeRefused(TEMPLATE_UNREADABLE.format(reason=str(e))) from None
        for _, _, slot in spans:
            out.setdefault(slot.field, set()).add(slot.context)
    return out


def _value_problem(name: str, value: Any, contexts: set[str]) -> str | None:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False) \
        if isinstance(value, (dict, list)) else str(value)
    if "quoted" in contexts and any(ch in text for ch in ("'", "\\", "\n", "\r")):
        return QUOTED_VALUE.format(name=name)
    if "bare" in contexts and not (isinstance(value, (int, float)) and not isinstance(value, bool)) \
            and not (isinstance(value, str) and _NUMBER.match(value)):
        return BARE_VALUE.format(name=name, value=text[:40])
    if "comment" in contexts and ("\n" in text or "\r" in text or "*/" in text):
        return COMMENT_VALUE.format(name=name)
    return None


def check_inputs(pinned: Pinned, inputs: Any) -> dict[str, Any]:
    """按口径入口的字段校验助手给的参数，补缺省值，交回要渲染进模板的那一份。不合规抛 ComposeRefused（一句一处）。"""
    from app.apps.forms import input_schema, validate_form

    if inputs is None:
        inputs = {}
    if not isinstance(inputs, dict):
        raise ComposeRefused(INPUTS_NOT_OBJECT)
    fields = {str(f.get("name")).strip(): f for f in pinned.entry_fields if isinstance(f.get("name"), str)}
    if unknown := [k for k in inputs if k not in fields]:
        raise ComposeRefused(FIELD_UNKNOWN.format(workflow=pinned.workflow_name, name=unknown[0],
                                                  fields=_names(list(fields))))
    values = {name: copy.deepcopy(f.get("default")) for name, f in fields.items()
              if f.get("default") not in (None, "")}
    values.update({k: v for k, v in inputs.items() if v is not None and v != ""})
    if errors := validate_form(input_schema(list(pinned.entry_fields)), values):
        raise ComposeRefused("；".join(e["message"] for e in errors))
    contexts = _slot_contexts(pinned)
    for name, value in values.items():
        if problem := _value_problem(name, value, contexts.get(name, set())):
            raise ComposeRefused(problem)
    return values


def instantiate(option: Option, op: Mapping[str, Any], *, taken: set[str]) -> Composed:
    """按选用的那一版确定性地实例化（见模块说明）。taken 是图里已有的节点 id。不合规抛 ComposeRefused。"""
    pinned = option.pinned
    if pinned is None or option.reason is not None:
        raise ComposeRefused(NOT_COMPOSABLE.format(workflow=option.workflow_name, version=option.version,
                                                   card=option.card, reason=option.reason or "读不到这一版"))
    op_id = str(op.get("id") or "").strip()
    if not _OP_ID.match(op_id):
        raise ComposeRefused(BAD_ID.format(id=op_id))
    wanted = [str(m) for m in op.get("metrics") or [] if isinstance(m, (str, int))] \
        if isinstance(op.get("metrics"), list) else []
    on_card = {str(m["id"]): m for m in pinned.metrics}
    if not wanted:
        raise ComposeRefused(NO_METRICS.format(metrics=_names(list(on_card))))
    if missing := [m for m in wanted if m not in on_card]:
        raise ComposeRefused(METRIC_UNKNOWN.format(card=option.card, metric=missing[0], metrics=_names(list(on_card))))
    values = check_inputs(pinned, op.get("inputs"))
    block = caliber_block(pinned, prefix=f"{op_id}_", card_id=op_id, inputs=values)
    if clash := next((str(n["id"]) for n in block.nodes if str(n["id"]) in taken), None):
        raise ComposeRefused(ID_TAKEN.format(id=op_id, node=clash))
    nodes = block.nodes
    edges = [{"source": a["id"], "target": b["id"]} for a, b in zip(nodes, nodes[1:])]
    metrics = [{"id": m, "name": str(on_card[m].get("name") or m)} for m in wanted]
    return Composed(op_id=op_id, option=option, nodes=nodes, edges=edges, metrics=metrics, inputs=values)


class Composer:
    """GraphApplier 用的：这一轮能选用的口径卡（CaliberKind 在开流之前读好），按 use_caliber 实例化。"""

    def __init__(self, options: list[Option]) -> None:
        self.options = options

    def pick(self, op: Mapping[str, Any]) -> Option:
        workflow_id = str(op.get("workflow_id") or "").strip()
        version = op.get("version")
        cards = [o for o in self.options if o.workflow_id == workflow_id]
        if not cards:
            named = str(op.get("workflow_name") or workflow_id or "（没写工作流）")
            raise ComposeRefused(NOT_LISTED.format(workflow=named, version=version))
        name = cards[0].workflow_name
        latest = cards[0].version
        if str(version) != str(latest):
            raise ComposeRefused(WRONG_VERSION.format(workflow=name, latest=latest, version=version))
        card = str(op.get("card") or "").strip()
        listed = [f"「{o.node_id}」（口径卡「{o.card}」）" for o in cards]
        if card:
            hit = next((o for o in cards if card in (o.node_id, o.card)), None)
            if hit is None:
                why = CARD_UNKNOWN.format(workflow=name, version=latest, card=card, cards=_names(listed))
                if card == str(op.get("id") or "").strip():
                    why += CARD_IS_OP_ID.format(card=card)
                raise ComposeRefused(why)
            return hit
        if len(cards) > 1:
            raise ComposeRefused(CARD_AMBIGUOUS.format(workflow=name, version=latest, cards=_names(listed)))
        return cards[0]

    def compose(self, op: Mapping[str, Any], *, taken: set[str]) -> Composed:
        return instantiate(self.pick(op), op, taken=taken)


__all__ = ["OP", "REFUSED", "ComposeRefused", "Composed", "Composer", "Option", "check_inputs", "instantiate",
           "signature", "slot_contexts", "value_problem"]

value_problem = _value_problem
