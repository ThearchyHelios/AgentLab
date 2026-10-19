from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from app.core.events import EventType
from app.engine.issuance import extract_numbers, trace_numbers
from app.engine.toolcalls import leaked_markup

# --------------------------------------------------------------------------
# 跑完之后的复核层。
#
# 编排跑完只说明「流程走完了」，不说明「答得对」。库里能查到大量 status=succeeded
# 的运行，过程中检索降级了、工具报错了、agent 步数用满了，而交到用户手上的答案
# 对此只字不提——最糟的一次（run dd9927e6）交出去的干脆是一句内部管道文案。
#
# 出具体系（issuance.py）本来就是干这个的，但它挂在输出节点的可选契约上，还要配
# 口径卡节点提供受控指标集；问数据页生成的图基本都是 input → agent → output，
# 走不到那条路。所以这里补一层轻的：
#
#   1. scan()   纯规则扫一遍事件流，零成本。干净的运行到此为止，不调模型。
#   2. review() 只在扫出信号时调一次模型，重组答案并把异常说破。
#   3. 复核模型看不到原始数据，所以它写出来的任何新数字都是凭空来的
#      —— check_numbers() 机械拦截，对不上就整条退回原答案。
# --------------------------------------------------------------------------

BROKEN = "broken"      # 答案不可信
DEGRADED = "degraded"  # 能用，但有缺口要说明
BENIGN = "benign"      # 记在轨迹里就够了，不值得为它叫一次模型

_SEVERITY_RANK = {BENIGN: 0, DEGRADED: 1, BROKEN: 2}


@dataclass(frozen=True)
class Signal:
    """一条「这次运行有什么不对」。detail 同时给模型和用户看，所以写人话。"""

    kind: str
    detail: str
    severity: str = DEGRADED
    #: agent 为什么提前收尾（engine/guards.py：steps / stall / budget_tokens / budget_usd / context）。
    #: 问数据页据此决定给不给「放宽步数重跑」：只有步数用满时放宽步数才对症
    reason: str | None = None

    def as_dict(self) -> dict[str, str]:
        out = {"kind": self.kind, "detail": self.detail, "severity": self.severity}
        return {**out, "reason": self.reason} if self.reason else out


# warn 日志按 code 分类，而不是靠匹配中文消息串——消息是给人读的，随时会改，
# 把复核的判断挂在它上面等于给自己埋一颗定时炸弹。各 emit 点带上 code。
#
# 没有 code 的（老运行留在库里的事件）一律按 degraded 处理：宁可多说一句，
# 也好过把真问题当噪音吞掉。
WARN_CODES: dict[str, tuple[str, str]] = {
    # 对答案内容零影响，轨迹里有就够了
    "stream_fallback": ("stream_fallback", BENIGN),
    # 安全属性上的降级，不是答案内容的问题；轨迹里已经说清楚了
    "isolation_fallback": ("isolation_fallback", BENIGN),
    # 答案本身就没写完
    "step_limit": ("step_limit", BROKEN),
    "empty_completion": ("empty_completion", BROKEN),
    # 能用但有缺口
    # 步数用尽，但收尾轮让模型基于已查到的部分收了口：答案是完整的一段话，
    # 只是覆盖面不全——这是缺口不是断裂，所以不判 BROKEN。
    # 连带效果是不再触发自动重跑，那正是想要的：同样配置再跑一遍还撞同一堵墙
    "step_limit_settled": ("step_limit_settled", DEGRADED),
    # 收尾轮自己挂了。影响由同时发出的 step_limit(BROKEN) 承担，这条是说清原因
    "settle_failed": ("settle_failed", DEGRADED),
    "loop_limit": ("loop_limit", DEGRADED),
    "tool_args_fixed": ("tool_args_fixed", DEGRADED),
    "node_retry": ("node_retry", DEGRADED),
    "validate_retry": ("validate_retry", DEGRADED),
    "sandbox_reset": ("sandbox_reset", DEGRADED),
    "retrieve_degraded": ("retrieve_degraded", DEGRADED),
    # 模型把工具调用写成了文字。纠正一次好了的，答案是真查过的，只记缺口；
    # 没好的那次节点已经判失败，node_failed 会按 BROKEN 算
    "tool_markup_leak": ("tool_markup_leak", DEGRADED),
    # 协作团队用完轮数、按降档交付：交来的是成员原话，不是调度者认可的结论
    "team_exhausted": ("team_exhausted", BROKEN),
    # 调度者收了工，但最后派出去的成员失败了，成果取自更早的那份：有缺口，不是断裂
    "team_last_failed": ("team_last_failed", DEGRADED),
    # 校验节点的修复编出了原文没有的值，这次修复被作废
    "repair_invented": ("repair_invented", DEGRADED),
    # 报告撰写节点：写作者写了裸数字或引用了不存在的指标，被要求重写。重写好了的
    # 答案是核对过的，只记缺口；没好的那次按 on_violation 失败或把违规标在报告里
    "report_repair": ("report_repair", DEGRADED),
    # 报告撰写节点的上游没有可引用的口径卡指标：报告里的数都没有出处
    "report_no_evidence": ("report_no_evidence", DEGRADED),
    # 证据目录超出篇幅，有的查询只列了编号和摘要：写作模型没看全证据，报告的覆盖面有缺口
    "report_catalog_trimmed": ("report_catalog_trimmed", DEGRADED),
    # 目录有省略时写了「没有查到」这类缺失断言、重写后仍在：报告照常产出并标注，这几句结论依据不足
    "report_absence_claim": ("report_absence_claim", DEGRADED),
    # 报告直接引用了取自遮罩列的格子、重写后仍在（「违规处置」为标注）：照常产出并标注，另记一条缺口（有出具契约的
    # 成果按缺口降档）
    "report_masked_cell": ("report_masked_cell", DEGRADED),
    # 口径卡的指标集、报告文档没能落进工件库：答案照常，只是点不开证据
    "evidence_store_failed": ("evidence_store_failed", DEGRADED),
    # agent 的结构化字段：模型报的值和查询快照对不上（已按快照取值），或者出处核对不了
    # （记成空值，不兜底成 0）。答案能用，缺口要说清是哪一种
    "agent_field_mismatch": ("agent_field_mismatch", DEGRADED),
    "agent_field_unverified": ("agent_field_unverified", DEGRADED),
    # 结构化抽取两次都被截断（输出额度在推理阶段就用完了）：字段都记空值，答案能用，缺口要说清是额度问题
    "agent_field_truncated": ("agent_field_truncated", DEGRADED),
    # 结论句裁判（claims: judge）：触顶、没跑成的句子记为未裁判，出具不能判完整——是缺口，要说明
    "judge_limit": ("judge_limit", DEGRADED),
    "judge_failed": ("judge_failed", DEGRADED),
    # 裁判模型的输出到了上限被截断（推理模型的思考也算在额度里）：同 judge_failed 是缺口，记成它自己的 kind——说得清
    # 是输出额度的问题，该换的是模型
    "judge_truncated": ("judge_truncated", DEGRADED),
    # 证据不支持的句子交回写作者改过一次（和 report_repair 一样只记缺口）；改写稿冒出新问题没采用的，
    # 原稿里那几句仍是证据不支持
    "report_rewrite": ("report_rewrite", DEGRADED),
    "report_rewrite_rejected": ("report_rewrite_rejected", DEGRADED),
    # 估不出裁判的金额、裁判和写作是同一个模型：配置上的提醒，答案本身没受影响
    "judge_unpriced": ("judge_unpriced", BENIGN),
    "judge_same_model": ("judge_same_model", BENIGN),
    # 双盲复算、反方审稿的核查模型和被核查的模型相同：同 judge_same_model，只关乎独立性，结论本身没受影响
    "dual_check_same_model": ("dual_check_same_model", BENIGN),
    "challenge_same_model": ("challenge_same_model", BENIGN),
    # 核查 agent（engine/verify_agent.py，双盲复算、反方审稿共用）估不出金额：同 judge_unpriced，配置上的提醒
    "verify_unpriced": ("verify_unpriced", BENIGN),
    # 核查没做完、没通过：盲算触顶、两路不一致、指标无法复算；反方审稿触顶、推翻了结论、有句子没能反查、只查了
    # 部分数据源。答案照常交付（出具契约另按缺口降档），是缺口不是断裂，按各自的 kind 记，界面说得清是哪一种
    "dual_check_limit": ("dual_check_limit", DEGRADED),
    "dual_check_disagreed": ("dual_check_disagreed", DEGRADED),
    "dual_check_unchecked": ("dual_check_unchecked", DEGRADED),
    "challenge_limit": ("challenge_limit", DEGRADED),
    "challenge_refuted": ("challenge_refuted", DEGRADED),
    "challenge_unreviewed": ("challenge_unreviewed", DEGRADED),
    "challenge_sources_lost": ("challenge_sources_lost", DEGRADED),
    # 核查 agent 自己触顶、按已有结果收尾：多半和节点自己的 *_limit 说的是同一次触顶，照样是缺口，记成它自己的 kind，
    # 不落成笼统的 warn——步数上限（max_steps）只有它说
    "verify_limit": ("verify_limit", DEGRADED),
    # 核查 agent 的输出到了上限被截断（推理模型的思考也算在额度里）：没交成结果，由节点按无法复算、未能反查记缺口。
    # 同 verify_limit，是缺口不是断裂，记成它自己的 kind：说得清是输出额度的问题，换个模型就好
    "verify_truncated": ("verify_truncated", DEGRADED),
}

# 这些 kind 重跑一次大概率能好：原因说得清，且不是「问题本身没问明白」。
# 工具参数错了、步数不够、图没校验过，都属于改一下再来一遍就行的。
RETRYABLE = {"step_limit", "tool_error", "empty_completion", "node_failed"}

#: 串行工具模式下塞给未执行调用的占位说明。它漏到用户面前当过答案，
#: 所以复核要认得出来。从 nodes.llm 导入会绕成循环，这里按内容识别。
_PLACEHOLDER_MARKS = ("本轮只执行了第一个工具",)


def answer_text(output: Mapping[str, Any] | None) -> str:
    """把 output 各字段拼成答案正文。和前端 answerText 同一套规则。"""
    if not output:
        return ""
    parts = [
        value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        for key, value in output.items()
        if not key.startswith("_") and value not in (None, "")
    ]
    return "\n".join(parts).strip()


def _event_field(event: Any, name: str, default: Any = None) -> Any:
    """事件既可能是 ORM 行也可能是 dict，两种都要吃得下。"""
    if isinstance(event, Mapping):
        return event.get(name, default)
    return getattr(event, name, default)


def scan(events: Iterable[Any], output: Mapping[str, Any] | None = None) -> list[Signal]:
    """纯规则扫一遍运行事件，挑出会影响答案可信度的那些。

    不调模型、不碰网络。返回空列表就意味着这次运行干净，可以直接交付。
    """
    signals: list[Signal] = []
    seen: set[tuple[str, str]] = set()

    def add(kind: str, detail: str, severity: str, reason: str | None = None) -> None:
        if severity == BENIGN:
            return
        key = (kind, detail)
        if key in seen:
            return
        seen.add(key)
        kinds.add(kind)
        signals.append(Signal(kind, detail, severity, reason))

    kinds: set[str] = set()

    rows = list(events)

    # 工具报错之后，同一个工具在后面又跑成了，说明自愈接住了——那是缺口不是断裂。
    # 不区分的话，参数自动纠正这条路每修一次就报一次「答案不可信」。
    recovered: set[str] = set()
    failed_at: dict[str, int] = {}
    #: 节点名：node.started 带着画布上的名字，报失败时写名字，不写节点 id
    titles: dict[str, str] = {}
    for i, e in enumerate(rows):
        etype = str(_event_field(e, "type", ""))
        data = _event_field(e, "data") or {}
        if etype == EventType.NODE_STARTED and data.get("label"):
            titles.setdefault(str(_event_field(e, "node_id") or ""), str(data["label"]))
        tool = str(data.get("tool") or "")
        if etype == EventType.TOOL_ERROR and tool:
            failed_at.setdefault(tool, i)
        elif etype == EventType.TOOL_END and tool and tool in failed_at:
            recovered.add(tool)

    #: 每个报告节点最后一次 report.checked：「改过一次」的信号按最终结果定档（_settled）
    finals = _final_checks(rows)
    #: 报过的节点失败 (节点 id, 原话)：子工作流节点跟着子图里那一步失败时认得出是同一件事
    failures: list[tuple[str, str]] = []
    for e in rows:
        etype = str(_event_field(e, "type", ""))
        data = _event_field(e, "data") or {}
        node_id = _event_field(e, "node_id") or ""

        if etype == EventType.TOOL_ERROR:
            tool = str(data.get("tool") or "工具")
            err = str(data.get("error") or "").strip()[:200]
            if tool in recovered:
                add("tool_args_fixed", f"工具 {tool} 第一次调用失败（{err}），重试后成功", DEGRADED)
            else:
                add("tool_error", f"工具 {tool} 调用失败：{err}", BROKEN)

        elif etype == EventType.NODE_FAILED and data.get("reason") == "rejected":
            # 人的驳回（engine/compiler 给被驳回的那一步的 node.failed 带 reason=rejected，总方案 X9）：不是故障，不说「执行
            # 失败」；也不进 RETRYABLE——重跑只会把同一件事再问一遍人。答案没走完是缺口，记 DEGRADED（没有成果时
            # empty_output 另按 BROKEN 说）
            err = str(data.get("error") or "").strip()[:200]
            if any(inner.startswith(f"{node_id}/") and said == err for inner, said in failures):
                continue    # 子工作流里那一步被驳回、子工作流节点跟着收尾：同一件事，只报那一步（同下面的执行失败）
            failures.append((str(node_id), err))
            add("node_rejected", f"节点「{_step_name(str(node_id), titles)}」被驳回：{err}", DEGRADED)

        elif etype == EventType.NODE_FAILED:
            err = str(data.get("error") or "").strip()[:200]
            if any(inner.startswith(f"{node_id}/") and said == err for inner, said in failures):
                # 子工作流里那一步失败了，子工作流节点（「sub」）跟着失败、原话相同：同一件事，只报更具体的那一步（上面已报）
                continue
            failures.append((str(node_id), err))
            add("node_failed", f"节点「{_step_name(str(node_id), titles)}」执行失败：{err}", BROKEN)

        elif etype == EventType.RUN_FAILED:
            err = str(data.get("error") or "").strip()[:200]
            add("run_failed", f"运行失败：{err}", BROKEN)

        elif etype == EventType.RETRIEVE_END:
            if data.get("degraded") and "retrieve_degraded" not in kinds:
                # knowledge.py 在这条事件**之前**已经把每条具体原因发成了 warn
                #（"重排结果解析不出来"、"多少条向量对不上"）。两边都收就是同一件事
                # 报两遍，一条含糊一条具体——这里只兜没有 warn 的情形
                add("retrieve_degraded", "知识库检索能力降级，可能遗漏表述不同的内容", DEGRADED)
            if not data.get("count"):
                q = str(data.get("query") or "").strip()[:60]
                add("retrieve_empty", f"知识库里没有检索到与「{q}」相关的内容", BROKEN)

        elif etype == EventType.LOG and data.get("level") == "warn":
            message = str(data.get("message") or "").strip()
            code = str(data.get("code") or "")
            kind, severity = WARN_CODES.get(code, ("warn", DEGRADED))
            if code in _SETTLED_BY_REPORT and _settled(code, finals.get(str(node_id))):
                # 首稿没过、改过一次之后最终通过了（同一个报告节点最后一次 report.checked）：不是缺口，不进异常清单。
                # 复核模型被别的信号叫起来时，在 notes() 里照实说「重写后已全部通过」
                continue
            reason = data.get("reason")
            if message:
                add(kind, message[:300], severity, reason if isinstance(reason, str) and reason else None)

    text = answer_text(output)
    if not text:
        add("empty_output", "这次运行没有产出任何成果内容", BROKEN)
    elif any(mark in text for mark in _PLACEHOLDER_MARKS):
        add("placeholder_output", "交付的是一句内部流程说明，而不是对问题的回答", BROKEN)
    elif leaked_markup(text):
        # 老运行、或者绕过了节点检查的路径：成果本身就是一段没执行的工具调用标记
        add("markup_output", "交付的是一段未执行的工具调用标记，而不是对问题的回答", BROKEN)

    return signals


def _step_name(node_id: str, titles: Mapping[str, str]) -> str:
    """失败的那一步给人看的名字。子工作流里的（「子工作流节点 id/…」）一层层接上 node.started 记的名字：「复核子流程 › 算一下」，
    不写内部 id；没记名字的那一层照写 id 的那一段。"""
    if "/" not in node_id:
        return titles.get(node_id) or node_id
    parts = node_id.split("/")
    return " › ".join(titles.get("/".join(parts[:i + 1])) or parts[i] for i in range(len(parts)))


#: 报告节点「改过一次」的信号：按同一个节点最后一次 report.checked 的结果定档（最终通过的不是缺口）
_SETTLED_BY_REPORT = ("report_repair", "report_rewrite")
#: 证据不支持的几种判定（engine/judge 的 CONTRADICTED 加证据不足）：最终还剩这些，改写就没有改好
_UNSUPPORTED = ("contradicted", "unsupported", "insufficient")


def _final_checks(rows: list[Any]) -> dict[str, dict[str, Any]]:
    """每个报告节点最后一次 report.checked 的内容（节点 id → data）。"""
    out: dict[str, dict[str, Any]] = {}
    for e in rows:
        if str(_event_field(e, "type", "")) == EventType.REPORT_CHECKED:
            data = _event_field(e, "data") or {}
            out[str(_event_field(e, "node_id") or "")] = data if isinstance(data, dict) else {}
    return out


def _settled(code: str, final: Mapping[str, Any] | None) -> bool:
    """「改过一次」之后最终是不是好了：report_repair 看最后一次核对有没有违规（ok、violations 为空）；report_rewrite
    看最后的裁判摘要里还有没有证据不支持的句子。找不到最终结果（老运行、节点失败没写出 report.checked）的当没好。"""
    if not final:
        return False
    if code == "report_repair":
        return final.get("ok") is True and not final.get("violations")
    counts = ((final.get("judge") or {}).get("counts") or {}) if isinstance(final.get("judge"), dict) else {}
    return bool(counts) and not any(int(counts.get(k) or 0) for k in _UNSUPPORTED)


def notes(events: Iterable[Any]) -> list[str]:
    """不是异常、但复核模型被别的信号叫起来时该知道的事（review_prompt 的「已经处理好的」）：报告首稿没过核对、重写后
    已全部通过；裁判认为证据不支持的句子改写后都有了依据。只说处理好了的，没好的照旧是信号（scan）。"""
    rows = list(events)
    finals = _final_checks(rows)
    titles: dict[str, str] = {}
    first: dict[tuple[str, str], dict[str, Any]] = {}
    times: dict[tuple[str, str], int] = {}
    for e in rows:
        etype = str(_event_field(e, "type", ""))
        data = _event_field(e, "data") or {}
        node_id = str(_event_field(e, "node_id") or "")
        if etype == EventType.NODE_STARTED and data.get("label"):
            titles.setdefault(node_id, str(data["label"]))
        if etype == EventType.LOG and data.get("code") in _SETTLED_BY_REPORT:
            key = (node_id, str(data["code"]))
            first.setdefault(key, data)
            times[key] = times.get(key, 0) + 1
    out: list[str] = []
    for (node_id, code), data in first.items():
        if not _settled(code, finals.get(node_id)):
            continue
        name = f"报告「{titles.get(node_id) or node_id}」"
        count = data.get("count")
        if code == "report_repair":
            head = f"首稿有 {count} 处没过核对" if isinstance(count, int) and count > 0 else "首稿没过核对"
            again = f"重写 {times[(node_id, code)]} 次后" if times[(node_id, code)] > 1 else "重写后"
            out.append(f"{name}{head}，{again}已全部通过")
        else:
            head = f"有 {count} 句结论裁判认为证据不支持" if isinstance(count, int) and count > 0 \
                else "有几句结论裁判认为证据不支持"
            out.append(f"{name}{head}，改写后都有了依据")
    return out


def worst(signals: Iterable[Signal]) -> str:
    """一组信号里最重的那档。没有信号时返回空串。"""
    ranked = sorted(signals, key=lambda s: _SEVERITY_RANK.get(s.severity, 1), reverse=True)
    return ranked[0].severity if ranked else ""


def should_retry(signals: Iterable[Signal]) -> bool:
    """这些信号里有没有「重跑一次大概率能好」的。"""
    return any(s.kind in RETRYABLE and s.severity == BROKEN for s in signals)


def check_numbers(original: str, revised: str) -> list[str]:
    """复核重写的答案里，有哪些数字是原答案里没有的。

    复核模型手上只有原答案和一份异常清单，没有任何原始数据——它写出来的新数字
    只能是凭空来的。这条是硬拦截，不是提示：叙述层无算术权限是这个项目的地基
    （见 issuance.decide_tier）。

    直接复用出具校验那套回指逻辑，把原答案的数字当成受控指标集。
    """
    controlled = [
        {"id": tok.raw, "value": tok.value} for tok in extract_numbers(original)
    ]
    report = trace_numbers(revised, controlled)
    return [item["token"] for item in report.unmatched]


REVIEW_SYSTEM = (
    "你负责复核一次数据问答的结果。执行过程中检测到了异常，你的任务是让用户"
    "在读这个答案之前就知道它哪里不可靠。\n\n"
    "你会拿到：用户的问题、系统给出的原答案、这次运行的异常清单。\n\n"
    "铁律：\n"
    "1. 你看不到任何原始数据。**不许写出原答案里没有的数字**——一个都不行。"
    "需要改写时只能重组原答案已有的内容。\n"
    "2. 异常必须写进 note，不许淡化或略过。降级要明说，不能悄悄地降。\n"
    "3. 如果异常说明这个答案根本不可信（比如取数工具就没跑通、检索什么都没查到），"
    "就直说答不了、说清缺什么，不要拿一个看起来完整的答案去糊。\n"
    "4. note 写给用户看，一到两句中文，说人话：出了什么事、对这个答案意味着什么。"
    "不要复述事件名或代码。\n\n"
    "输出 JSON：\n"
    '- note：给用户的异常说明（必填）\n'
    '- answer：重写后的答案。原答案本身没问题、只是需要补一句说明时，留空字符串\n'
    '- retry：这次运行是不是「换个配置重跑一遍大概率就好了」（比如步数不够、'
    "工具参数写错）。问题本身没问清楚导致的，填 false"
)

REVIEW_SCHEMA: dict[str, Any] = {
    "title": "review",
    "type": "object",
    "properties": {
        "note": {"type": "string", "description": "给用户的异常说明，一到两句中文"},
        "answer": {"type": "string", "description": "重写后的答案；不需要改写就留空"},
        "retry": {"type": "boolean", "description": "重跑一次是否大概率能好"},
    },
    "required": ["note"],
}


#: 答案带着逐段证据时追加给复核模型的话
LOCKED_NOTE = (
    "\n\n这份答案带有逐段证据（报告里的每个数字都对应着核对过的出处），不能改写："
    "answer 必须留空，只在 note 里说明异常。"
)


def review_prompt(question: str, answer: str, signals: list[Signal], *, locked: bool = False,
                  settled: Iterable[str] = ()) -> str:
    """拼给复核模型的请求。

    只给信号清单，不给原始事件流：几百条事件塞进 prompt 既贵又没用，而信号
    已经是规则层提炼过的结论。settled 是已经处理好的（notes()：首稿没过、重写后已全部通过……）：
    照实告诉复核模型，免得它把「改过一次」写成「仍未全部通过、可能不准」。
    """
    lines = "\n".join(
        f"- [{'严重' if s.severity == BROKEN else '降级'}] {s.detail}" for s in signals
    )
    done = "\n".join(f"- {n}" for n in settled)
    return (
        f"用户的问题：\n{question or '（未记录）'}\n\n"
        f"系统给出的原答案：\n{answer or '（空）'}\n\n"
        f"检测到的异常：\n{lines}"
        + (f"\n\n已经处理好的（不是异常，不要写成缺口或「可能不准」）：\n{done}" if done else "")
        + (LOCKED_NOTE if locked else "")
    )


@dataclass
class ReviewResult:
    """复核结论。verdict 是对「系统实际做了什么」的陈述，不是模型的自我评价。"""

    verdict: str          # ok | annotated | rewritten | reverted | failed
    note: str = ""
    answer: str | None = None   # None 表示沿用原答案
    #: 改写之前的那份。改写是有损的，原件得跟着一起落库——否则刷新之后
    #: 只剩一个被重组过的答案，想对照都没得对照
    original: str | None = None
    retry: bool = False
    signals: list[Signal] = None  # type: ignore[assignment]
    severity: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "note": self.note,
            "answer": self.answer,
            "original": self.original,
            "retry": self.retry,
            "severity": self.severity,
            "signals": [s.as_dict() for s in (self.signals or [])],
        }


async def review(
    model: Any, *, question: str, answer: str, signals: list[Signal], locked: bool = False,
    settled: Iterable[str] = (),
) -> ReviewResult:
    """叫一次模型复核。调用方负责确保 signals 非空——空信号根本不该走到这里。

    复核本身失败不能让整轮挂掉：答案已经在手上了，复核只是加一层说明。
    所以这里把异常全兜住，最差也要把原答案原样交出去。

    locked：成果带 _evidence（报告撰写节点产出、逐段对应证据文档）。这种答案只能
    加说明、不能改写——改写之后界面上点开的出处，指向的是一段已经不存在的文字。
    """
    severity = worst(signals)
    rule_retry = should_retry(signals)
    fallback_note = "；".join(s.detail for s in signals if s.severity == BROKEN) or "；".join(
        s.detail for s in signals
    )

    messages = [("system", REVIEW_SYSTEM),
                ("human", review_prompt(question, answer, signals, locked=locked, settled=settled))]
    try:
        raw = await model.with_structured_output(REVIEW_SCHEMA).ainvoke(messages)
        if not isinstance(raw, dict):
            raise TypeError("结构化输出没有返回对象")
    except Exception:  # noqa: BLE001 - 不支持结构化输出的模型退回文本解析
        try:
            from app.api.copilot import _extract_json
            from app.engine.state import message_text

            raw = _extract_json(message_text(await model.ainvoke(messages)))
        except Exception:  # noqa: BLE001
            # 模型这条路彻底不通。规则层已经知道出了什么事，照样说得出话
            return ReviewResult(
                verdict="annotated", note=fallback_note, retry=rule_retry,
                signals=signals, severity=severity,
            )

    note = str(raw.get("note") or "").strip() or fallback_note
    revised = str(raw.get("answer") or "").strip()
    # 规则层管**资格**，模型只有**否决权**。反过来（两边取并）的话，模型就能
    # 越过规则强推重跑——而"检索什么都没查到"这类，重跑一遍还是查不到，
    # 白烧一次建图加一次跑图。模型没表态时按规则走
    retry = rule_retry and bool(raw.get("retry", True))

    if locked or not revised or revised == answer.strip():
        return ReviewResult(
            verdict="annotated", note=note, retry=retry, signals=signals, severity=severity,
        )

    invented = check_numbers(answer, revised)
    if invented:
        # 重写里冒出了原答案没有的数字。整条退回——说明还是要给的，
        # 但那份正文一个字都不能用
        return ReviewResult(
            verdict="reverted",
            note=f"{note}\n\n（复核改写里出现了原答案中没有的数字"
                 f"（{'、'.join(invented[:5])}），已退回原答案。）",
            retry=retry, signals=signals, severity=severity,
        )

    return ReviewResult(
        verdict="rewritten", note=note, answer=revised, original=answer, retry=retry,
        signals=signals, severity=severity,
    )
