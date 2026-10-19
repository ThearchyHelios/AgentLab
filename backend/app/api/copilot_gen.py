"""助手的生成过程（总方案 L1，look 文档 D9）：画布改图、问数据、发布前修复、升级证据、应用润色共用一个循环。

循环只管四件事：读到 look 就停下、执行查看、接着写、计次数和上限。怎么落地（Applier）、怎么自查（Checker）由调用方
注入（app/look/base.py 的协议）：图用 GraphApplier / GraphChecker，应用用 AppApplier / AppChecker。

**接着写时的消息结构**（look 文档 §2.5.5）：
  [system]     和起草时相同，不变
  [human]      这一轮的请求（含历史）
  [assistant]  模型到 look 那一行为止的输出（look 之后的内容已丢弃）
  [human]      查看结果 + 「接着输出操作，已经落地的不要重复」+ 已落地的对象 id
看几次就重复几组 assistant / human。已落地的操作由 Applier 去重：完全相同的 add_edge 忽略、和已落地节点完全相同的
add_node 不转发、plan 只留第一次。

**ask。** 模型输出 {"op":"ask","question":…} 时这一轮就结束：循环里不等用户回答，不再自查修正；已经落地的改动照常
交付（附上自查问题），用户的回答就是下一轮。

**自查修正。** 起草完由 Checker 找出挡路的问题，交回模型改（最多 SELF_CHECK_ROUNDS 轮）；修正请求里带上这一轮的查看
结果（和起草时看到的是同一份文字），修正阶段另给 REPAIR_LOOK_LIMIT 次查看。

copilot.py 原来的读流（_iter_ops）、解析（_parse_op_line）、落地（_apply_op、merge_node_config）搬到这里，copilot.py
照原名再导出。
"""
from __future__ import annotations

import copy
import json
import logging
import re
import time
from collections.abc import AsyncIterator
from contextlib import aclosing
from dataclasses import dataclass, field
from typing import Any

from app.engine.schema import NodeType
from app.look.base import LOOK_LIMIT, REPAIR_LOOK_LIMIT, Applied, Applier, Checker, LookBudget
from app.look.service import LookService
from app.providers.budget import cut_off as _cut_off

logger = logging.getLogger(__name__)

#: 自查最多交回去改几轮。一轮只花一次"只输出改动"的调用；两轮还改不好，多半是需求本身有歧义，该让人看了
SELF_CHECK_ROUNDS = 2
#: 读到这几种操作就停下：done 说完了，look 要查看，ask 要等用户回答（蓝图助手的反问是 bp_ask，normal-user-apps N2）
_ASK_OPS = ("ask", "bp_ask")
_STOP_OPS = ("done", "look", *_ASK_OPS)
#: 不占次数的查看回话（need 没写、次数用完）连着来几次就不再接着写：模型在原地打转，再要下去只是花钱
_IDLE_LOOKS = 2
#: 读到停下的那一行（done、look、ask）之后，最多再收多少字。照约定那一行就是这一次输出的最后一行，后面只剩流末尾那一段
#: 用量（Anthropic 的输出 token、OpenAI 兼容系开了 stream_usage 的用量都在最后一段才报）：读完它，这一次的花费才报得全，
#: 问数据页这一轮的花费、评测按角色的成本才不是下限。超过这么多字说明模型还在往下写闲话：不为它付钱，当场关流，这一次的
#: 用量记成没报全（providers/usage.py 照实记估不出，不当成报全了）
DRAIN_CHARS = 400


# ==========================================================================
# 读流
# ==========================================================================


#: 操作行前后的围栏：```json {"op":…} 这样和围栏写在同一行的，去掉围栏再读
_FENCE_HEAD = re.compile(r"^`{3,}[A-Za-z0-9_-]*\s*")
_FENCE_TAIL = re.compile(r"\s*`{3,}$")
#: 一个操作分成多行写（缩进排版的 JSON）时最多攒多少行、多少字：攒不成一个完整的 JSON 就当跳过
_PENDING_LINES = 200
_PENDING_CHARS = 20000
#: 操作的名字（各入口的都在里面）：原生工具调用叫这些名字的，参数就是操作的字段，照操作读（_tool_ops）
OP_NAMES = frozenset({
    "plan", "add_node", "update_node", "remove_node", "add_edge", "remove_edge", "done", "reply", "look", "ask",
    "catalog_patch", "use_caliber", "question",
    "app_add", "app_update", "app_remove", "app_move", "app_page", "app_set",
    "bp_ask", "bp_set", "bp_add", "bp_remove", "bp_template", "fill_joint",
})


def _op_text(line: str) -> str:
    """一行里要按操作读的那段：去掉首尾空白和同一行上的围栏。只有围栏的行（```、```json）交回空串。"""
    text = line.strip()
    if text.startswith("```"):
        text = _FENCE_HEAD.sub("", text, count=1)
    return _FENCE_TAIL.sub("", text).strip()


def _classify(text: str) -> tuple[dict[str, Any] | None, str | None]:
    """(操作, None) 或 (None, 为什么跳过)。空的交回 (None, None)：不算跳过。"""
    if not text:
        return None, None
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        return None, "不是完整的 JSON"
    if not isinstance(obj, dict):
        return None, "不是 JSON 对象"
    if "op" not in obj:
        return None, "缺 op"
    return obj, None


def _parse_op_line(line: str) -> dict[str, Any] | None:
    """解析一行操作。围栏行、空行、解析失败、缺 op 的一律返回 None——
    模型偶尔犯规不该毁掉整条流，跳过就是（跳过的照实记进 StreamDiagnosis）。和围栏写在同一行的操作去掉围栏再读。"""
    return _classify(_op_text(line))[0]


@dataclass
class StreamDiagnosis:
    """一轮生成里模型交回了什么：只记长度、个数和原因，不记原文。一个操作都没认出来（copilot_empty）时写进错误详情，看得出
    是只写了思考、格式跑偏、还是用了工具调用；自动重来时据此给一句纠正（correction）。

    finish 是最后一次调用的结束原因（finish_reason / stop_reason）；text_chars、thinking_chars 是正文（不算空白）和思考的
    字数；dropped 是跳过的行按原因数；tool_calls 是原生工具调用的
    名字（按操作读了的也记）；markup 是正文里写成文字的工具调用（<invoke>、<tool_call>、DeepSeek 的 DSML……）有几次——读得出
    的按操作读（ops 里也算），读不出、只认出了标记的记 1；nudged：起草时一个操作都没认出、当场带纠正请它重写过一次
    （Generation._converse）。"""

    calls: int = 0
    finish: str = ""
    text_chars: int = 0
    thinking_chars: int = 0
    ops: int = 0
    tool_calls: list[str] = field(default_factory=list)
    dropped: dict[str, int] = field(default_factory=dict)
    markup: int = 0
    nudged: bool = False

    def drop(self, reason: str) -> None:
        self.dropped[reason] = self.dropped.get(reason, 0) + 1

    def absorb(self, other: StreamDiagnosis) -> None:
        """把一次调用的诊断并进这一轮的（结束原因取最后一次的）。"""
        self.calls += other.calls
        self.finish = other.finish or self.finish
        self.text_chars += other.text_chars
        self.thinking_chars += other.thinking_chars
        self.ops += other.ops
        self.tool_calls.extend(other.tool_calls)
        for reason, n in other.dropped.items():
            self.dropped[reason] = self.dropped.get(reason, 0) + n
        self.markup += other.markup

    def as_dict(self) -> dict[str, Any]:
        return {"calls": self.calls, "finish": self.finish or None, "text_chars": self.text_chars,
                "thinking_chars": self.thinking_chars, "ops": self.ops, "tool_calls": list(self.tool_calls),
                "dropped": dict(self.dropped), "markup": self.markup, "nudged": self.nudged}

    def headline(self) -> str:
        """报错的那一句（「助手本轮未完成：」后面）：按交回了什么照实说。以前一律是「模型没有给出任何修改，也没有说明原因」，
        可模型多半是说了想干什么、只是格式不对（部署上撞过：把 look 写成了工具调用的格式）。"""
        if self.tool_calls or self.markup:
            return "模型把操作写成了工具调用的格式，没有按约定逐行输出，系统认不出"
        if not self.text_chars and self.thinking_chars:
            return "模型只写了思考，没有给出任何操作"
        if not self.text_chars:
            return "模型什么都没有输出"
        if self.dropped:
            return "模型的输出格式不对，没有一行能认出的操作"
        return "模型只说了打算，没有给出任何修改"

    def summary(self) -> str:
        """给人看的一句：「结束原因 stop；正文 0 字，思考 11 字；认出 0 行操作；……」。"""
        parts = [f"结束原因 {self.finish or '未报'}", f"正文 {self.text_chars} 字，思考 {self.thinking_chars} 字",
                 f"认出 {self.ops} 行操作"]
        if self.tool_calls:
            parts.append(f"原生工具调用 {len(self.tool_calls)} 个（{'、'.join(self.tool_calls[:5])}）")
        if self.dropped:
            parts.append(f"跳过 {sum(self.dropped.values())} 行：" + "、".join(f"{n} 行{why}" for why, n in self.dropped.items()))
        if self.markup:
            parts.append("正文里有写成文字的工具调用标记")
        if self.calls > 1:
            parts.append(f"共调用模型 {self.calls} 次")
        if self.nudged:
            parts.append("当场带纠正重写过一次，仍然没有操作")
        return "；".join(parts)

    def correction(self) -> str:
        """自动重来时交给助手的那句纠正（chat/turn.empty_retry_instruction 接在问题后面）。"""
        if self.tool_calls:
            return (f"用了工具调用接口（{'、'.join(self.tool_calls[:3])}），这里不走工具调用；每个操作写成正文里单独一行的 "
                    "JSON，要看表就写一行 {\"op\":\"look\",…}")
        if self.markup:
            return "把操作写成了工具调用的标记，这里不走工具调用；每个操作写成单独一行的 JSON，要看表就写一行 {\"op\":\"look\",…}"
        if not self.text_chars and self.thinking_chars:
            return "只写了思考、没有给出任何操作；这次请直接输出操作（每行一个 JSON），要看表就先输出一行 look"
        if not self.text_chars:
            return "什么都没有输出；这次请直接输出操作（每行一个 JSON），要看表就先输出一行 look"
        if self.dropped:
            return ("输出里没有一行能认出的操作；每个操作单独一行、写成一行完整的 JSON（带 op），不要夹说明文字，"
                    "要看表就先输出一行 look")
        return "没有给出任何操作；请先 look 看表，或者直接给出操作"


class CopilotIncomplete(Exception):
    """模型这一轮没说完：输出被截断，或者一个操作都没给。画布不能按「无需修改」交付。code 是机读码：一个操作都没给的是
    chat/turn.EMPTY_GENERATION（问数据一轮按它自动重来一次），截断的不给。correction 是重来时交给助手的那句纠正（error 的
    reason），diagnostics 是机读的诊断（StreamDiagnosis.as_dict），都只在一个操作都没给时有。"""

    def __init__(self, reason: str, *, hint: str = "", detail: str = "", code: str = "", correction: str = "",
                 diagnostics: dict[str, Any] | None = None) -> None:
        super().__init__(reason)
        self.hint = hint or "画布未修改。可以重试，或把需求拆成几步分别提出"
        self.detail = detail
        self.code = code
        self.correction = correction
        self.diagnostics = diagnostics


async def _drain(stream: Any, already: int) -> None:
    """停下的那一行之后把模型的流读完（内容一概不要），只为收到流末尾才报的用量；读到超过 DRAIN_CHARS 字（模型还在写闲话）
    就不读了，交回调用方关流。already 是缓冲里那一行后面已经收到的字数。"""
    from app.engine.state import message_text as _text
    from app.engine.state import thinking_text

    seen = already
    async for chunk in stream:
        seen += len(_text(chunk)) + len(thinking_text(chunk) or "")
        if seen > DRAIN_CHARS:
            return


def _call_op(name: str, fields: Any, diag: StreamDiagnosis, *, what: str) -> dict[str, Any] | None:
    """一次工具调用 → 操作：名字是操作名的，参数就是操作的字段。名字不认识、参数解析不了的照实记进诊断（what：「工具调用」
    「工具调用标记」）。"""
    if name not in OP_NAMES:
        diag.drop(f"{what}的名字不认识")
        return None
    if not isinstance(fields, dict):
        diag.drop(f"{what}的参数解析不了")
        return None
    return {"op": name, **{k: v for k, v in fields.items() if k != "op"}}


def _tool_ops(calls: dict[int, dict[str, str]], diag: StreamDiagnosis) -> list[dict[str, Any]]:
    """原生工具调用（流里的 tool_call_chunks，按 index 拼好的 {name, args}）→ 操作。以前整个工具调用一声不响就丢了。"""
    out: list[dict[str, Any]] = []
    for index in sorted(calls):
        name, args = calls[index].get("name") or "", calls[index].get("args") or ""
        diag.tool_calls.append(name or "（没有名字）")
        try:
            fields = json.loads(args) if args.strip() else {}
        except json.JSONDecodeError:
            fields = None
        if (op := _call_op(name, fields, diag, what="工具调用")) is not None:
            out.append(op)
    return out


def _markup_ops(calls: list[Any], diag: StreamDiagnosis) -> list[dict[str, Any]]:
    """写成文字的工具调用（engine/toolcalls.take_leaked_calls 读出来的）→ 操作，和原生工具调用同一个规矩。"""
    out: list[dict[str, Any]] = []
    for call in calls:
        diag.markup += 1
        if (op := _call_op(call.name, call.args, diag, what="工具调用标记")) is not None:
            diag.ops += 1
            out.append(op)
    return out


async def _iter_ops(model: Any, messages: list[Any], *, lines: list[str] | None = None,
                    diag: StreamDiagnosis | None = None, raw: list[str] | None = None):
    """流式读模型输出，按行切出操作。done、look、ask 之后即停——后面就算有闲话也不要了；look 之后停下也就是
    「读到这一行就停止本次输出」。停下之后不马上关流：交出那一行、调用方接着要下一条时先把流读完（_drain，内容丢掉，最多
    DRAIN_CHARS 字），流末尾那一段用量才收得到——以前读到这一行就关流，输出 token 一个都拿不到，问数据这一轮的花费多半是下限
    （第 1 波遗留）。

    除了操作，还把模型的思考原样转出去（thinking_text：Claude 的 thinking 块、OpenAI 兼容系的 reasoning_content），
    不把非 JSON 行当成思考。lines 给了的话，认出来的操作行原样记进去：接着写时作为 assistant 消息交回模型。

    格式跑偏的几种写法照样认（以前一声不响就丢了）：一个操作分成多行写的 JSON（以「{」开头的一行读不成，就接着往后攒，
    攒到读得成为止；中途来了一行自己就读得成的，攒着的那段算跳过）、和围栏写在同一行的操作、原生工具调用里名字是操作名
    的（_tool_ops，流读完再按先后交出）、写成文字的工具调用（DSML、<invoke>、<tool_call>……：从有标记的那一行起攒，一次调用
    写完就按名字读成操作，见 engine/toolcalls.take_leaked_calls）。diag 给了的话，交回了什么（长度、个数、跳过的原因）记进去，
    不记原文。raw 给了的话，流读完时把正文的开头一段（最多 4000 字）记进去：一个操作都没认出时当场交回模型纠正用。

    没等到 done 就因为额度用完停下的，抛 CopilotIncomplete：截断之前的操作照常转出去，由调用方决定整轮作废。
    """
    from app.engine.state import message_text as _text
    from app.engine.state import thinking_text
    from app.engine.toolcalls import has_markup, leaked_markup, take_leaked_calls

    diag = diag if diag is not None else StreamDiagnosis()
    diag.calls += 1
    buffer = ""
    cut = ""
    pending: list[str] = []
    markup = ""                     # 写成文字的工具调用里还没写完的那一段
    markup_before = diag.markup
    calls: dict[int, dict[str, str]] = {}
    head = ""                       # 正文的开头一段：认写成文字的工具调用标记、交给 raw 用，不进诊断

    def read(line: str) -> dict[str, Any] | None:
        """一行 → 操作（认出来了）或 None；多行的 JSON 攒在 pending 里。"""
        text = _op_text(line)
        if pending:
            op, _ = _classify(text)
            if op is not None:
                diag.drop("不是完整的 JSON")         # 自己就是一个操作的一行来了：攒着的那段读不成了
                pending.clear()
                return _single(op, None)
            pending.append(text)
            joined = "\n".join(pending)
            op, why = _classify(joined)
            if op is not None:
                pending.clear()
                return _single(op, None)
            if len(pending) > _PENDING_LINES or len(joined) > _PENDING_CHARS:
                diag.drop("不是完整的 JSON")
                pending.clear()
            return None
        op, why = _classify(text)
        if why == "不是完整的 JSON" and text.startswith("{"):
            pending.append(text)                    # 可能是分成多行写的一个操作：接着往后攒
            return None
        return _single(op, why)

    def _single(op: dict[str, Any] | None, why: str | None) -> dict[str, Any] | None:
        if why:
            diag.drop(why)
        if op is not None:
            diag.ops += 1
        return op

    def take(line: str) -> list[dict[str, Any]]:
        """一行 → 这一行读出来的操作。写成文字的工具调用从有标记的那一行起攒着，一次调用写完才交出（一行里可能写完好几个）；
        攒着的时候来的行都归它，外层包装和调用之间的说明文字不算跳过。"""
        nonlocal markup
        if markup or (not pending and has_markup(line)):
            leaked, markup = take_leaked_calls(markup + line + "\n")
            return _markup_ops(leaked, diag)
        op = read(line)
        return [op] if op else []

    def record(op: dict[str, Any]) -> None:
        if lines is not None:
            lines.append(json.dumps(op, ensure_ascii=False))

    async with aclosing(model.astream(messages)) as stream:
        async for chunk in stream:
            reasoning = thinking_text(chunk)
            if reasoning:
                diag.thinking_chars += len(reasoning)
                yield {"op": "thinking", "delta": reasoning}
            for part in getattr(chunk, "tool_call_chunks", None) or []:
                slot = calls.setdefault(int(part.get("index") or 0), {"name": "", "args": ""})
                slot["name"] = slot["name"] or str(part.get("name") or "")
                slot["args"] += str(part.get("args") or "")
            meta = getattr(chunk, "response_metadata", None) or {}
            if meta.get("finish_reason") or meta.get("stop_reason"):
                diag.finish = str(meta.get("finish_reason") or meta.get("stop_reason"))

            text = _text(chunk)
            diag.text_chars += sum(1 for c in text if not c.isspace())
            if len(head) < 4000:
                head += text[:4000 - len(head)]
            buffer += text
            while "\n" in buffer:
                line, buffer = buffer.split("\n", 1)
                for op in take(line):
                    record(op)
                    yield op
                    if op.get("op") in _STOP_OPS:
                        await _drain(stream, len(buffer))
                        return
            cut = _cut_off(chunk) or cut
    if raw is not None:
        raw.append(head)
    tail = take(buffer)
    if markup:
        # 流读完了，最后一次调用还差结尾：补上结尾再读一次，还读不出的算跳过
        leaked, markup = take_leaked_calls(markup, final=True)
        tail += _markup_ops(leaked, diag)
        if markup:
            diag.drop("工具调用标记没写完")
    if diag.markup == markup_before and leaked_markup(head):
        diag.markup += 1            # 认出了标记、一次调用都读不出来
    if pending:
        diag.drop("不是完整的 JSON")
        pending.clear()
    tools = _tool_ops(calls, diag)
    diag.ops += len(tools)
    for op in tail + tools:
        record(op)
        yield op
        if op.get("op") in _STOP_OPS:
            return
    if cut:
        # 截断的流和说完了的流读起来一模一样：不在这里拦下，上面就会拿原图（或半张图）
        # 去自查、交付，界面上写「无需修改」
        limit = getattr(model, "max_tokens", None)
        raise CopilotIncomplete(
            "模型的输出超出了长度上限，修改没有写完",
            detail=f"finish_reason={cut}" + (f"，本轮输出上限 {limit} token" if limit else ""))


# ==========================================================================
# 图的落地
# ==========================================================================

_NODE_TYPES = frozenset(t.value for t in NodeType)

#: 协作成员条目里写 "remove": true 就把这个成员拿掉。不复用 null：null 在合并里
#: 的意思是「删这个字段」，成员本身不是一个字段
_REMOVE_MEMBER = "remove"


def _member_key(member: dict[str, Any], index: int) -> str:
    # 和 engine/nodes/multi.py 的叫法一致：没起名字的成员按位置叫 agentN
    return str(member.get("name") or f"agent{index}")


def _merge_fields(old: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """按字段合并：写了的覆盖，写 null 的删掉，没写的原样保留。返回新 dict。"""
    out = dict(old)
    for key, value in patch.items():
        if value is None:
            out.pop(key, None)
        else:
            out[key] = value
    return out


def _merge_members(old: list[Any], patch: list[Any]) -> list[Any]:
    """协作成员按 name 合并。原有成员保持原来的次序，新名字接在后面。"""
    merged: list[Any] = list(old)
    where = {_member_key(m, i): i for i, m in enumerate(merged) if isinstance(m, dict)}
    dropped: set[int] = set()
    for i, entry in enumerate(patch):
        if not isinstance(entry, dict):
            continue
        key = _member_key(entry, i)
        if entry.get(_REMOVE_MEMBER) is True:
            if key in where:
                dropped.add(where[key])
            continue
        fields = {k: v for k, v in entry.items() if k != _REMOVE_MEMBER}
        if key in where:
            merged[where[key]] = _merge_fields(merged[where[key]], fields)
        else:
            where[key] = len(merged)
            merged.append({k: v for k, v in fields.items() if v is not None})
    return [m for i, m in enumerate(merged) if i not in dropped]


def merge_node_config(
    node_type: str, old: dict[str, Any], patch: dict[str, Any]
) -> dict[str, Any]:
    """update_node 的 config 怎么落到节点上。前端 store/studio.ts 必须是同一套规则。

    以前是整体替换：模型只想改一句提示词，照协议也得把整份 config 抄一遍，
    漏抄的字段就没了。真出过事——改写提示词时漏了 tools，三个要查库的 agent
    全丢了工具，一次库都没查，最后把一堆工具调用的原始标记当答案交了出去。
    改成按字段合并之后，漏写等于不改；真要删一个字段得显式写 null。
    """
    if node_type == NodeType.SUPERVISOR.value and isinstance(patch.get("agents"), list):
        members = old.get("agents") if isinstance(old.get("agents"), list) else []
        patch = {**patch, "agents": _merge_members(members, patch["agents"])}
    return _merge_fields(old, patch)


def _apply_op(nodes: dict[str, dict[str, Any]], edges: list[dict[str, Any]], op: dict[str, Any]) -> bool:
    """把一个操作应用到服务端维护的图状态。返回是否真的改了图。"""
    kind = op.get("op")
    if kind == "add_node":
        node = op.get("node") or {}
        # 类型不认识的节点不进图、也不转给前端：前端按类型查节点定义，查不到
        # 以前是整站白屏，连带没保存的编辑一起丢
        if not node.get("id") or node.get("type") not in _NODE_TYPES:
            return False
        nodes[node["id"]] = {
            "id": node["id"],
            "type": node["type"],
            "position": {"x": 0, "y": 0},
            "data": {"label": node.get("label") or "", "config": node.get("config") or {}},
        }
        return True
    if kind == "update_node":
        node = nodes.get(op.get("id") or "")
        if not node:
            return False
        data = node.setdefault("data", {})
        if op.get("label") is not None:
            data["label"] = op["label"]
        if isinstance(op.get("config"), dict):
            data["config"] = merge_node_config(
                str(node.get("type") or ""), data.get("config") or {}, op["config"])
        return True
    if kind == "remove_node":
        node_id = op.get("id")
        if node_id not in nodes:
            return False
        nodes.pop(node_id)
        edges[:] = [e for e in edges if e["source"] != node_id and e["target"] != node_id]
        return True
    if kind == "add_edge":
        edge = op.get("edge") or {}
        if not edge.get("source") or not edge.get("target"):
            return False
        edges.append(
            {
                "source": edge["source"],
                "target": edge["target"],
                "sourceHandle": edge.get("sourceHandle"),
            }
        )
        return True
    if kind == "remove_edge":
        before = len(edges)
        edges[:] = [
            e
            for e in edges
            if not (
                e["source"] == op.get("source")
                and e["target"] == op.get("target")
                and (op.get("sourceHandle") is None or e.get("sourceHandle") == op.get("sourceHandle"))
            )
        ]
        return len(edges) != before
    return False


class GraphApplier:
    """图的落地：_apply_op 改服务端维护的图（nodes、edges 原地改，调用方手里的同一份），外加这一轮要记下的东西。

    - seen：起草时收到的图操作（含 plan、done），兜底重放到上一轮的图上时用（replay_onto）
    - tried / effective：试过的、真改了图的操作（发布前修复、升级按它们复核）
    - replied：模型说这句话不需要工作流时的 reply；之后只再收目录修改提案
    - questions：发布前修复、升级里模型要人拿主意的 question（questions=True 时 reply 也算在里面）
    - skipped_types：模型编出来的节点类型，收尾时说出来

    patches：目录修改提案转不转（只有用户亲口说了数据事实的画布、问数据才转）。

    composer：问数据里能选用已发布口径时（ledger/compose.Composer，next-round 02 做法 4）。use_caliber 由它按那一版确定性
    实例化成取数节点和口径卡，这里落地：节点、连线照 add_node / add_edge 记进 seen（兜底重放照样重放得出来），入口连到
    第一个取数节点（入口还没加的话，等入口加了再接）；转给界面的只是那一条 use_caliber，带上 result（选用了哪一版、生成了
    哪几个节点）。不合规的拒收：result 写原因、照样转给界面（过程里照实写一行），原因记进 refused，自查时交回模型改
    （GraphChecker.issues）。没有 composer 的入口（画布、发布前修复、升级）use_caliber 不认，照旧当认不出的操作丢掉。
    """

    def __init__(self, nodes: dict[str, dict[str, Any]], edges: list[dict[str, Any]], *, patches: bool = True,
                 questions: bool = False, composer: Any = None) -> None:
        self.nodes = nodes
        self.edges = edges
        self.patches = patches
        self.questions_mode = questions
        self.composer = composer
        #: 选用口径实例化出来的段：[(第一个取数节点, 全部节点 id)]，接入口用
        self.composed: list[tuple[str, list[str]]] = []
        #: 已经落地的 use_caliber（ledger/compose.signature），接着写时重发的同一个不再落地
        self._composed_ops: set[str] = set()
        #: 系统接上的连线（入口 → 取数）：模型再发一条一模一样的不重复加
        self._wired: list[dict[str, Any]] = []
        #: 拒收的 use_caliber：[{op, message}]，自查时交回模型
        self.refused: list[dict[str, Any]] = []
        self._reported = 0
        self.seen: list[dict[str, Any]] = []
        self.tried: list[dict[str, Any]] = []
        self.effective: list[dict[str, Any]] = []
        self.skipped_types: list[str] = []
        self.questions: list[dict[str, Any]] = []
        self.replied: dict[str, Any] | None = None
        self.plan: dict[str, Any] | None = None
        self.explanation = ""
        # 建完要不要跑，由模型在 done.run 里表态
        self.autorun = True
        self._landed: list[str] = []
        self._continuing = False
        self._repairing = False

    def apply(self, op: dict[str, Any]) -> Applied:
        kind = op.get("op")
        if kind == "catalog_patch":
            # 目录修改提案不进图：转给调用方核对后做成卡片（人点「保存」才写）
            return Applied(False, self.patches and not self._repairing)
        if kind in ("question", "reply") and self.questions_mode:
            self.questions.append(op)
            return Applied(False, False)
        if self._repairing and kind == "reply":
            return Applied(False, False)
        if self.replied is not None:
            return Applied(False, False)
        if kind == "reply":
            # 这句话不需要工作流：先压着，等模型说完（只再收它后面的提案）由调用方转出、收尾
            self.replied = op
            return Applied(False, False)
        if kind == "plan":
            if self.plan is not None and self._continuing:
                return Applied(False, False)
            self.plan = self.plan or op
            self.seen.append(op)
            return Applied(False, True)
        if kind == "done":
            self.explanation = str(op.get("explanation", ""))
            # 用户要的是流程本身时（"设计一个每天跑的工作流"），建完就跑等于替他多花一次钱
            self.autorun = op.get("run") is not False
            self.seen.append(op)
            return Applied(False, True)
        if kind == "use_caliber" and self.composer is not None:
            return self._use_caliber(op)
        if kind == "add_edge" and self._was_wired(op.get("edge") or {}):
            # 入口 → 取数这条系统已经接上了
            return Applied(False, False)
        if self._continuing and self._repeats(op):
            return Applied(False, False)
        self.seen.append(op)
        self.tried.append(op)
        changed = _apply_op(self.nodes, self.edges, op)
        if changed and kind == "add_node":
            self._wire()
        if changed:
            self.effective.append(op)
            nid = (op.get("node") or {}).get("id") if kind == "add_node" else op.get("id")
            if isinstance(nid, str) and nid not in self._landed and kind in ("add_node", "update_node"):
                self._landed.append(nid)
        elif kind == "add_node":
            bad = (op.get("node") or {}).get("type")
            if bad and bad not in _NODE_TYPES:
                self.skipped_types.append(str(bad))
        return Applied(changed, changed)

    def _use_caliber(self, op: dict[str, Any]) -> Applied:
        """选用一份已发布口径（见类说明）。op 原地写上 result，原样转给界面。"""
        from app.ledger.compose import ComposeRefused, signature

        sig = signature(op)
        if sig in self._composed_ops:
            return Applied(False, False)
        try:
            composed = self.composer.compose(op, taken=set(self.nodes))
        except ComposeRefused as e:
            op["result"] = {"status": "refused", "reason": e.message}
            self.refused.append({"op": {k: v for k, v in op.items() if k != "result"}, "message": e.message})
            return Applied(False, True, e.message)
        self._composed_ops.add(sig)
        primitive = [{"op": "add_node", "node": {"id": n["id"], "type": n["type"], "label": n["data"].get("label") or "",
                                                 "config": n["data"].get("config") or {}}} for n in composed.nodes]
        primitive += [{"op": "add_edge", "edge": e} for e in composed.edges]
        for item in primitive:
            self.seen.append(item)
            self.tried.append(item)
            if _apply_op(self.nodes, self.edges, item):
                self.effective.append(item)
        ids = [str(n["id"]) for n in composed.nodes]
        self.composed.append((ids[0], ids))
        if composed.op_id not in self._landed:
            self._landed.append(composed.op_id)
        self._wire()
        op["result"] = composed.result()
        return Applied(True, True)

    def _wire(self) -> None:
        """选用口径生成的取数节点还没有上游、图里已经有入口的：系统把入口连到第一个取数节点。"""
        entry = next((nid for nid, n in self.nodes.items() if n.get("type") == "input"), None)
        if entry is None:
            return
        for first, _ in self.composed:
            if first in self.nodes and not any(e["target"] == first for e in self.edges):
                edge = {"source": entry, "target": first, "sourceHandle": None}
                self.edges.append(edge)
                self._wired.append(edge)

    def _was_wired(self, edge: dict[str, Any]) -> bool:
        return any(w["source"] == edge.get("source") and w["target"] == edge.get("target") for w in self._wired)

    def unreported(self) -> list[dict[str, Any]]:
        """还没交回过模型的拒收的选用，取走即算交回过（同 AppApplier.unreported）。"""
        out = self.refused[self._reported:]
        self._reported = len(self.refused)
        return out

    def _repeats(self, op: dict[str, Any]) -> bool:
        """接着写时和已落地完全相同的操作（look §2.5.5）：已有的同一条连线、和已落地节点一模一样的节点、重发的同一个操作。"""
        kind = op.get("op")
        if kind == "add_edge":
            edge = op.get("edge") or {}
            return any(e["source"] == edge.get("source") and e["target"] == edge.get("target")
                       and e.get("sourceHandle") == edge.get("sourceHandle") for e in self.edges)
        if kind == "add_node":
            node = op.get("node") or {}
            have = self.nodes.get(str(node.get("id") or ""))
            return have is not None and have.get("type") == node.get("type") \
                and (have.get("data") or {}).get("label") == (node.get("label") or "") \
                and (have.get("data") or {}).get("config") == (node.get("config") or {})
        return op in self.effective

    def landed(self) -> list[str]:
        return list(self._landed)

    def continuing(self) -> None:
        self._continuing = True

    def repairing(self) -> None:
        self._repairing = True

    def replay_onto(self, graph: dict[str, Any]) -> None:
        """一个操作都没落地、而上一轮有图时：模型把这一轮当成「改上一张图」，只发了改动操作，而累加器是从空图起步的。
        与其报一句「图是空的」，不如按它本来的意思做——把这些操作重放到上一轮那张图上。"""
        self.nodes.clear()
        self.nodes.update({n["id"]: copy.deepcopy(n) for n in graph.get("nodes") or [] if n.get("id")})
        # 原地改，不能重新绑定：调用方手里拿的是同一份
        self.edges[:] = [{k: v for k, v in e.items() if k in ("source", "target", "sourceHandle") and v}
                         for e in graph.get("edges") or []]
        for op in self.seen:
            _apply_op(self.nodes, self.edges, op)
        self._wire()


class GraphChecker:
    """图的自查：按运行时同一套规则（copilot._blocking_issues）挑出挡路的问题，交回模型改。

    known 给了（升级证据）：只把这一轮冒出来的问题交回去（图上原来就有的不归这一轮管），一个修改都没落地时不修。
    """

    def __init__(self, applier: GraphApplier, *, scope: set[str] | None = None, sources: list[Any] | None = None,
                 baseline: list[dict[str, Any]] | None = None, level: str | None = None,
                 defaults: dict[str, Any] | None = None, checkers: dict[str, Any] | None = None,
                 known: set[Any] | None = None, chat_user: bool = False) -> None:
        self.applier = applier
        #: 图要以开登录后 user 角色的身份在问数据里发起（D-c）：使用者不能用的节点、工具也算挡路
        self.chat_user = chat_user
        self.scope = scope
        self.sources = sources
        self.baseline = baseline
        self.level = level
        self.defaults = defaults
        self.checkers = checkers
        self.known = known

    def blocking(self) -> list[dict[str, Any]] | None:
        from app.api.copilot import _blocking_issues

        return _blocking_issues(self.applier.nodes, self.applier.edges, self.scope, sources=self.sources,
                                baseline=self.baseline, level=self.level, defaults=self.defaults,
                                checkers=self.checkers, chat_user=self.chat_user)

    async def issues(self) -> list[dict[str, Any]]:
        errors = self.blocking()
        refused = _refused_issues(self.applier)
        if self.known is None:
            return [*refused, *(errors or [])]
        from app.engine.autofix import _sig

        if not self.applier.effective:
            return []
        return [e for e in errors or [] if _sig(e) not in self.known]

    def repair_request(self, issues: list[dict[str, Any]], looks_text: str) -> str:
        from app.api.copilot import _repair_request

        return _repair_request(self.applier.nodes, self.applier.edges, issues, looks=looks_text)


# ==========================================================================
# 应用的落地
# ==========================================================================

#: 选用口径被拒时交回模型的说法
_REFUSED_FOR_MODEL = ("上一次的 use_caliber 被拒收：{message}。按原因改正后重新输出 use_caliber（id 可以沿用）；这份口径对不上"
                      "问法的话就不要选用，交给 agent 查")


def _refused_issues(applier: Any) -> list[dict[str, Any]]:
    """拒收的 use_caliber 还没交回过模型的那几条，形状同自查的问题（error 级：不改就少了这一块）。"""
    if not isinstance(applier, GraphApplier):
        return []
    from app.ledger.compose import REFUSED

    return [{"level": "error", "node_id": None, "edge_id": None, "field": None, "code": REFUSED,
             "message": f"没有选用口径：{r['message']}", "for_model": _REFUSED_FOR_MODEL.format(message=r["message"])}
            for r in applier.unreported()]


APP_REPAIR_PROMPT = (
    "这是你刚改好的应用草稿：\n{spec}\n\n"
    "应用检查（和发布同一套规则）发现下面这些问题：\n{issues}\n\n"
    "只修这些问题，别的不要动。用应用操作输出修正；岗位绑定要人来做，不要替人绑。最后一行输出 done。{looks}"
)
REPAIR_LOOKS_PROMPT = "\n\n这一轮查看过的内容（和起草时看到的是同一份）：\n{text}"

#: 应用检查里只有人能处理的问题：钉住哪个版本、升版怎么处置、岗位和核实人绑给谁（绑的人在不在名录里）、授权、通知
#: 渠道。不交回模型改，照常列在交付的问题里
APP_HUMAN_ONLY = frozenset({
    "app.pin_unpublished", "app.pin_retired", "app.pin_hash", "app.upgrade_policy", "app.slot_unbound",
    "app.slot_principal_unknown", "app.reconciler_unbound", "app.acl_empty", "app.acl_principal_unknown",
    "app.notify_channel_missing", "app.process_unsafe",
    "app.view_source_empty", "subgraph_version_unpublished", "subgraph_version_retired",
    # 个人应用（docs/design/normal-user-apps.md N7、N6）：视图来源和应用范围对不上、图违反使用者的约束要回到蓝图改，
    # 隐藏哪块是作者的决定——润色只改呈现，改不了视图来源，也不该替人取消隐藏
    "app.view_source_personal", "app.hidden_bound", "personal.policy",
    # 看板表格逐行列出记录表（N8）：公开不公开是作者的决定，助手不替人把表格换掉
    "app.records_rows_exposed",
})


def _components(items: list[Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for item in items or []:
        if isinstance(item, dict):
            out.append(item)
            out += _components(item.get("children") or [])
    return out


def app_component(spec: dict[str, Any], component_id: str) -> dict[str, Any] | None:
    """spec 里按 id 找组件（各页的组件树，含子组件）。"""
    for page in spec.get("pages") or []:
        for item in _components(page.get("body") or [] if isinstance(page, dict) else []):
            if item.get("id") == component_id:
                return item
    return None


class AppApplier:
    """应用的落地：app/apps/ops.apply_app_op 改 spec（原地改），外加接着写时的去重。

    plan、done 记下说明；reply 记下（这句话不需要改应用）；图操作、目录修改提案在这里一概不收。

    润色只改呈现（D-h，app/apps/ops.polish_problem）：组件操作、页面、待办外观（task_ui）、表单的外观键照收；改钉住的
    版本、入口、视图、岗位、表单的数据约束一律拒（Applied(False, False, problem)）。拒掉的原因记在 refused 里：自查时
    交回模型（AppChecker.issues），交付时列给人看（refusals），不悄悄吞掉。
    """

    def __init__(self, spec: dict[str, Any]) -> None:
        self.spec = spec
        self.effective: list[dict[str, Any]] = []
        self.plan: dict[str, Any] | None = None
        self.replied: dict[str, Any] | None = None
        self.explanation = ""
        #: 越界被拒的操作：[{op, path, message}]，按到来的顺序
        self.refused: list[dict[str, Any]] = []
        self._reported = 0
        self._landed: list[str] = []
        self._continuing = False
        self._repairing = False

    def apply(self, op: dict[str, Any]) -> Applied:
        from app.apps import ops as app_ops

        kind = str(op.get("op") or "")
        if kind == "plan":
            if self.plan is not None and self._continuing:
                return Applied(False, False)
            self.plan = self.plan or op
            return Applied(False, True)
        if kind == "done":
            self.explanation = str(op.get("explanation", ""))
            return Applied(False, True)
        if kind == "reply":
            if self.replied is None and not self._repairing:
                self.replied = op
            return Applied(False, False)
        if not kind.startswith("app_"):
            return Applied(False, False)
        if self._continuing and self._repeats(op):
            return Applied(False, False)
        if problem := app_ops.polish_problem(self.spec, op):
            path = f"{op.get('path')}.{op.get('key')}" if op.get("key") else str(op.get("path") or "")
            self.refused.append({"op": kind, "path": path, "message": problem})
            return Applied(False, False, problem)
        applied = app_ops.apply_app_op(self.spec, op)
        if applied.changed:
            self.effective.append(op)
            target = op.get("id") or (op.get("component") or {}).get("id") or (op.get("page") or {}).get("id") \
                or op.get("key")
            if isinstance(target, str) and target not in self._landed:
                self._landed.append(target)
        return applied

    def _repeats(self, op: dict[str, Any]) -> bool:
        """接着写时：重发的同一个操作；加一个 spec 里已经一模一样存在的组件。"""
        if op in self.effective:
            return True
        if op.get("op") == "app_add":
            component = op.get("component") or {}
            have = app_component(self.spec, str(component.get("id") or ""))
            return have is not None and have == component
        return False

    def landed(self) -> list[str]:
        return list(self._landed)

    def continuing(self) -> None:
        self._continuing = True

    def repairing(self) -> None:
        self._repairing = True

    def unreported(self) -> list[dict[str, Any]]:
        """还没交回过模型的越界操作，取走即算交回过：同一条不在后面几轮自查里反复交回。"""
        out = self.refused[self._reported:]
        self._reported = len(self.refused)
        return out

    def refusals(self) -> list[dict[str, Any]]:
        """交付时列给人看的：形状同应用检查的问题（警告级），说明助手想改什么、为什么没改。"""
        return [{"message": f"助手想改的这一处没有采纳：{r['message']}", "level": "warning", "code": POLISH_REFUSED,
                 "path": r["path"] or None} for r in self.refused]


#: 助手润色越界被拒（D-h）。不是应用检查的编号：不进发布检查，只在润色交付时列出来
POLISH_REFUSED = "app.polish_refused"


class AppChecker:
    """应用的自查：app/apps/lint.lint_app 的 error，去掉只有人能处理的（APP_HUMAN_ONLY），交回模型改。"""

    def __init__(self, applier: AppApplier, *, bindings: dict[str, Any] | None = None, scope: str = "shared") -> None:
        self.applier = applier
        self.bindings = bindings or {}
        #: 应用的范围（App.scope）：个人应用按个人应用的规则查（apps/lint.lint_app 的 scope）
        self.scope = scope

    async def lint(self) -> list[dict[str, Any]]:
        """全部问题（各级都在），交付时列出来。另开短会话：流里请求的会话已经不能用了。"""
        from app.apps import lint as app_lint
        from app.db.base import SessionLocal

        async with SessionLocal() as session:
            return list(await app_lint.lint_app(session, self.applier.spec, self.bindings, scope=self.scope))

    async def issues(self) -> list[dict[str, Any]]:
        """交回模型改的：应用检查的 error（去掉只有人能处理的），加上这之前越界被拒、还没交回过的操作。"""
        refused = [{"message": r["message"], "level": "error", "code": POLISH_REFUSED, "path": r["path"] or None}
                   for r in self.applier.unreported()]
        return [*refused,
                *(i for i in await self.lint() if i.get("level") == "error" and i.get("code") not in APP_HUMAN_ONLY)]

    def repair_request(self, issues: list[dict[str, Any]], looks_text: str) -> str:
        lines = "\n".join(f"- {i.get('path') or ''} {i.get('message') or ''}".strip() for i in issues)
        return APP_REPAIR_PROMPT.format(spec=json.dumps(self.applier.spec, ensure_ascii=False, indent=2),
                                        issues=lines,
                                        looks=REPAIR_LOOKS_PROMPT.format(text=looks_text) if looks_text else "")


# ==========================================================================
# 生成过程
# ==========================================================================

#: 起草时一次调用读完、到这时还没有一个能落地的操作（只写了思考、写成了工具调用的格式、夹着说明文字、只写了 plan、查看完
#: 接着写却什么都没认出……）：同一轮里把它的原话交回去、附上这句纠正（correction 是 StreamDiagnosis.correction），请它按格式
#: 重写一次。只补一次，还不行就照实报 copilot_empty。
#: 部署上撞过：模型想先 look，却写成了 DSML，整轮当场报「没有给出任何修改」，用户点重试还是同样的几率
FORMAT_NUDGE_PROMPT = "上面这次输出里没有一行能认出的操作：{correction}。请按输出格式重新给出这一轮的操作。"
#: 交回去的原话最多多少字
NUDGE_ECHO_CHARS = 2000


class Generation:
    """一轮生成：起草（draft）→ 自查修正（repair）。两段之间调用方可以插自己的判断（只回答了一句、兜底重放……）。

    model 是按操作流写的模型（copilot._op_model）；looks 是这一轮的查看服务（app/look/service.py），起草和修正共用，
    看过的对象、交回过的文字都在里面。asked：模型问了用户一个问题（这一轮结束）。
    """

    def __init__(self, model: Any, *, system: str, applier: Applier, checker: Checker | None = None,
                 looks: LookService | None = None, started: float | None = None, look_limit: int = LOOK_LIMIT) -> None:
        self.model = model
        #: 起草阶段最多查看几次。蓝图、填关节按调用次数的预算收得更紧（normal-user-apps N11）
        self.look_limit = look_limit
        self.system = system
        self.applier = applier
        self.checker = checker
        self.looks = looks if looks is not None else LookService([])
        self.started = time.monotonic() if started is None else started
        self.asked: dict[str, Any] | None = None
        self.done: dict[str, Any] | None = None
        self.repaired = 0
        self.repair_error: Exception | None = None
        #: 这一轮调了几次主模型（含接着写、修正），验收时统计
        self.calls = 0
        #: 起草阶段模型交回了什么（只记长度、个数和原因）：一个操作都没认出来时写进错误详情
        self.diagnosis = StreamDiagnosis()
        #: 起草阶段交出过能落地的操作（改动、回答、反问、收尾……；plan、look 不算：它们不改任何东西，查看完什么都没写的这一轮
        #: 照样是空的）。一次调用读完还没有时当场纠正一次（FORMAT_NUDGE_PROMPT）
        self.moved = False

    def empty(self, message: str = "", *, hint: str = "") -> CopilotIncomplete:
        """一个操作都没给（copilot_empty）的报错：说法按诊断照实写（StreamDiagnosis.headline），带诊断（detail、
        diagnostics）和重来时交给助手的纠正（correction）。"""
        from app.chat.turn import EMPTY_GENERATION

        return CopilotIncomplete(message or self.diagnosis.headline(), hint=hint, code=EMPTY_GENERATION,
                                 detail=self.diagnosis.summary(), correction=self.diagnosis.correction(),
                                 diagnostics=self.diagnosis.as_dict())

    async def draft(self, request: str) -> AsyncIterator[dict[str, Any]]:
        async for op in self._converse(request, LookBudget(self.look_limit), repair=False):
            yield op

    async def repair(self, rounds: int = SELF_CHECK_ROUNDS) -> AsyncIterator[dict[str, Any]]:
        """按 Checker 自查，挡路的问题交回模型改，改完再查。修不成（报错、截断）就停，照实发一条 check error，原因记在
        repair_error；改到第几轮记在 repaired。模型问了用户（asked）的这一轮不修。"""
        if self.checker is None or self.asked is not None:
            return
        budget = LookBudget(REPAIR_LOOK_LIMIT)
        self.applier.repairing()
        for round_no in range(1, rounds + 1):
            errors = await self.checker.issues()
            if not errors:
                break
            yield {"op": "check", "status": "repairing", "round": round_no, "issues": errors}
            request = self.checker.repair_request(errors, self.looks.looked())
            try:
                async for op in self._converse(request, budget, repair=True):
                    yield op
            except Exception as e:  # noqa: BLE001 - 修不成就照实交付，调用方列出剩下的问题
                from app.core.errors import explain, raw

                self.repair_error = e
                yield {"op": "check", "status": "error", "message": f"自查修正失败：{explain(e)[0]}",
                       "detail": raw(e)}
                break
            self.repaired = round_no
            if self.asked is not None:
                break

    def _elapsed(self) -> int:
        return int((time.monotonic() - self.started) * 1000)

    async def _converse(self, request: str, budget: LookBudget, *, repair: bool) -> AsyncIterator[dict[str, Any]]:
        """一段对话：请模型写，读到 look 就停下、查看、把结果作为下一条消息交回，再请它接着写，直到 done / ask / 流结束。

        起草时一次调用读完、到这时还没有一个能落地的操作（格式跑偏，包括查看完接着写的那一次），先不收场：把它的原话交回去、
        附一句纠正，请它当场重写一次（FORMAT_NUDGE_PROMPT，每轮一次）。以前直接报「没有给出任何修改」，画布上没有自动重来，
        点重试也是同样的几率。"""
        messages: list[tuple[str, str]] = [("system", self.system), ("human", request)]
        idle = 0
        while True:
            lines: list[str] = []
            raw: list[str] = []
            look: dict[str, Any] | None = None
            self.calls += 1
            call = StreamDiagnosis()
            try:
                async with aclosing(_iter_ops(self.model, messages, lines=lines, diag=call, raw=raw)) as ops:
                    async for op in ops:
                        kind = op.get("op")
                        if kind not in ("thinking", "plan", "look"):
                            self.moved = True
                        if kind == "thinking":
                            yield op
                        elif kind == "look":
                            look = op
                        elif kind in _ASK_OPS:
                            self.asked = op
                            yield op
                        elif repair and kind in ("plan", "done"):
                            # 修补轮自己的开场和收尾，不是新方案，不覆盖原来的说明
                            continue
                        else:
                            if kind == "done":
                                self.done = op
                            if self.applier.apply(op).forward:
                                yield op
            finally:
                if not repair:
                    self.diagnosis.absorb(call)
            if call.markup and call.ops:
                logger.info("助手把 %d 处操作写成了工具调用的格式，已按操作读出 %d 个", call.markup, call.ops)
            if look is None:
                if repair or self.moved or self.diagnosis.nudged:
                    return
                # 还没有能落地的操作：当场纠正一次。说过话的把原话交回去；什么都没写的（只有思考）把纠正接在最后那条消息后面
                logger.info("助手起草时一个操作都没认出（%s），当场纠正一次", call.summary())
                self.diagnosis.nudged = True
                nudge = FORMAT_NUDGE_PROMPT.format(correction=call.correction())
                said = raw[0].strip() if raw else ""
                messages = [*messages, ("ai", said[:NUDGE_ECHO_CHARS]), ("human", nudge)] if said \
                    else [*messages[:-1], ("human", f"{messages[-1][1]}\n\n{nudge}")]
                continue
            # 查看可能要等挑选模型：心跳的阶段换成「正在查看」
            yield {"op": "heartbeat", "phase": "looking", "elapsed_ms": self._elapsed()}
            result = await self.looks.look(look, budget)
            if result.record is not None:
                yield result.record
            if not result.counted:
                idle += 1
                if idle >= _IDLE_LOOKS:
                    return
            messages = [*messages, ("ai", "\n".join(lines)),
                        ("human", result.text + self.looks.continue_prompt(self.applier.landed()))]
            self.applier.continuing()


async def generate(model: Any, system: str, request: str, applier: Applier, checker: Checker | None = None,
                   looks: LookService | None = None, *, rounds: int = SELF_CHECK_ROUNDS) -> AsyncIterator[dict[str, Any]]:
    """附录 C 的生成过程协议：起草（含查看）→ 自查修正，产出一串操作和事件。中间不需要插判断的调用方直接用它。"""
    gen = Generation(model, system=system, applier=applier, checker=checker, looks=looks)
    async for op in gen.draft(request):
        yield op
    async for op in gen.repair(rounds):
        yield op
