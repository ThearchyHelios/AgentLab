"""查看服务（look 文档 §2.5.3，总方案 L2、L3）：一行 look → 交回给模型的文字 + 发给前端的记录。

处理顺序：
  1. need 没写：回「look 要写 need」，不算次数；次数用完：回「按已查看的结构完成」，不再查看。
  2. targets 按类型分组（旧写法 tables:[{source, table}] 读进来换成 kind=table）。写了 name 的点名直取，不调挑选模型；
     只写了 kind（和 source）的、或者整个没写 targets 的，按 need 在那一类里挑（没写时挑这个入口的第一类）。
  3. 挑选失败或挑出来是空的：交回那一类的目录（status=index），写明原因，请模型点名再看。点名的一个都没认出来时，
     按 need 在那几个来源里挑（同只写类别），没认出来的照样写「没找到」；候选里一个能查看的都没有（库还没探查表
     结构）时没有目录可交，status=failed，照实说原因。
  4. 这一批对象这一轮都已经看过：回「已在上面」，不重新排一遍。
  5. 各类给详情，合计受 LOOK_RESULT_CHARS 约束；记一条记录（id、by、need、status、objects、用时）。

系统代为查看（look 文档 D5：发布前修复、升级证据、应用润色）也走这里，by=system，不占模型的次数，记录照样发、照样存。
查看本身出错（status=failed）不挡生成：照实交回原因，模型按已有的信息接着写。
"""
from __future__ import annotations

import logging
import time
from typing import Any

from app.look.base import LOOK_LIMIT, LOOK_RESULT_CHARS, LookBudget, LookKind, LookResult, LookTarget

logger = logging.getLogger(__name__)

# ==========================================================================
# 写给模型的文字
# ==========================================================================

NEED_MISSING_PROMPT = "look 要写 need：一句话说明要查什么。这一次没有查看，也不算次数"
LIMIT_PROMPT = "这一轮的查看次数已用完（最多 {limit} 次），按已经查看的结构完成，不要再 look"
REPEAT_PROMPT = "这些{what}已经在上面的查看结果里了，不再重复给出，直接用上面的"
HEAD_MODEL_PROMPT = "查看结果（第 {n} 次，这一轮最多 {limit} 次）"
HEAD_SYSTEM_PROMPT = "系统已代为查看（不占你的查看次数）"
NEED_PROMPT = "需求：{need}"
INDEX_PROMPT = ("没能按需求挑出{what}：{reason}。下面是候选的{what}目录，从里面点名（targets 写 name）再 look 一次：\n"
                "{index}")
EMPTY_PROMPT = ("按这个需求没有挑出相关的{what}。这一步确实要用到{what}的话，从下面的目录里点名再 look；"
                "不需要就接着写：\n{index}")
FAILED_PROMPT = "这一次查看出错了：{reason}。按已有的信息接着写"
UNAVAILABLE_PROMPT = "没有可查看的{what}：{reason}。按已有的信息接着写，不要猜{what}的名字和字段"
#: 按需求挑出来是空的：写进 look 事件的 reason，界面上照原话显示
EMPTY_REASON = "按这个需求没有挑出相关的{what}"
UNKNOWN_KIND_PROMPT = "这一轮不能查看这一类对象：{kinds}（能查看的：{allowed}）"
GUIDE_HEAD_PROMPT = (
    "按需查看（look）：要看{labels}的详情时，单独输出一行\n"
    '  {{"op":"look","need":"一句话说要查什么","targets":[{example}]}}\n'
    "  输出这一行就停下，系统把查看结果作为下一条消息交回来，你接着往下写（已经输出的不要重复）。\n"
    "  · need 必填。targets 里写了 name 的直接取那几个；只写 kind（可以加 source）不写 name 的，按 need 在这一类里挑；"
    "整个不写 targets 时按 need 挑{default}。sources 可选：限定在哪几个来源（数据源名、工作流名……）里挑\n"
    "  · kind 只能是：{kinds}\n"
    "  · 每轮最多查看 {limit} 次，一次把要用的都点上；这一轮已经看过的不要再看，这次对话里看过的直接点名看"
)
GUIDE_EXAMPLE_PROMPT = {
    "table": '{"kind":"table","source":"数据源名","name":"表名"}',
    "capability": '{"kind":"capability","name":"能力名"}',
    "workflow_version": '{"kind":"workflow_version","source":"工作流名","name":"v3"}',
}
CONTINUE_PROMPT = "\n\n接着输出操作。已经输出、已经落地的操作不要重复{landed}。"
LANDED_PROMPT = "（已落地：{ids}）"


def parse_targets(line: dict[str, Any]) -> list[LookTarget]:
    """look 行里的 targets（L2）。旧写法 tables:[{source, table}]（look 文档 §2.5.2 的初稿）读进来换成 kind=table。"""
    out: list[LookTarget] = []
    for t in line.get("targets") or []:
        if isinstance(t, dict) and isinstance(t.get("kind"), str):
            target: LookTarget = {"kind": t["kind"].strip()}
            for key in ("source", "name"):
                if isinstance(t.get(key), (str, int)) and str(t[key]).strip():
                    target[key] = str(t[key]).strip()
            out.append(target)
    for t in line.get("tables") or []:
        if isinstance(t, str) and t.strip():
            out.append({"kind": "table", "name": t.strip()})
        elif isinstance(t, dict) and isinstance(t.get("table", t.get("name")), str):
            target = {"kind": "table", "name": str(t.get("table", t.get("name"))).strip()}
            if isinstance(t.get("source"), str) and t["source"].strip():
                target["source"] = t["source"].strip()
            out.append(target)
    return out


def _sources(line: dict[str, Any]) -> list[str] | None:
    raw = line.get("sources")
    names = [str(s).strip() for s in raw if isinstance(s, str) and s.strip()] if isinstance(raw, list) else []
    return names or None


class LookService:
    """一轮生成里的查看：各类对象（LookKind）、这一轮的记录、看过的对象。起草和自查修正共用一份。"""

    def __init__(self, kinds: list[LookKind]) -> None:
        self.kinds: dict[str, LookKind] = {k.kind: k for k in kinds}
        self.records: list[dict[str, Any]] = []
        self.texts: list[str] = []
        self._seen: set[str] = set()
        self._n = 0

    @property
    def default_kind(self) -> str | None:
        return next(iter(self.kinds), None)

    async def overview(self) -> str:
        """system 里的概览：每类各自几行，没有内容的类不占地方。"""
        blocks = []
        for kind in self.kinds.values():
            lines = await kind.overview()
            if lines:
                blocks.append("\n".join(lines))
        return "\n\n".join(blocks)

    def guide(self) -> str:
        """写给模型的 look 用法（只列这个入口能看的类）。什么时候该看由调用方另写（各入口不一样）。"""
        if not self.kinds:
            return ""
        labels = "、".join(k.label for k in self.kinds.values())
        default = self.kinds[self.default_kind].label if self.default_kind else ""
        return GUIDE_HEAD_PROMPT.format(labels=labels, example=GUIDE_EXAMPLE_PROMPT.get(self.default_kind or "", ""),
                                        default=default, kinds="、".join(self.kinds), limit=LOOK_LIMIT)

    def looked(self) -> str:
        """这一轮所有查看交回过的文字，修正请求里原样带上（look 文档 D8：和起草时看到的是同一份）。"""
        return "\n\n".join(self.texts)

    def continue_prompt(self, landed: list[str]) -> str:
        return CONTINUE_PROMPT.format(landed=LANDED_PROMPT.format(ids="、".join(landed)) if landed else "")

    async def look(self, line: dict[str, Any], budget: LookBudget | None = None, *, by: str = "model") -> LookResult:
        need = str(line.get("need") or "").strip()
        if not need:
            return LookResult(NEED_MISSING_PROMPT, None, counted=False)
        if budget is not None and budget.used >= budget.limit:
            return LookResult(LIMIT_PROMPT.format(limit=budget.limit), None, counted=False)
        if budget is not None:
            budget.used += 1
        self._n += 1
        started = time.monotonic()
        record: dict[str, Any] = {"op": "look", "id": f"L{self._n}", "by": by, "need": need, "status": "ok"}
        head = HEAD_SYSTEM_PROMPT if by == "system" else HEAD_MODEL_PROMPT.format(
            n=budget.used if budget else self._n, limit=budget.limit if budget else LOOK_LIMIT)
        try:
            body, objects, keys = await self._run(line, need, record)
        except Exception as e:  # noqa: BLE001 - 查看是辅助步骤，出错不挡生成（P3）
            from app.core.errors import explain

            logger.exception("查看出错：%s", need)
            record.update(status="failed", reason=explain(e)[0])
            body, objects, keys = FAILED_PROMPT.format(reason=record["reason"]), [], set()
        if record["status"] == "ok" and keys and keys <= self._seen and not any(o.get("missing") for o in objects):
            # 这一批这一轮都看过了：不重排一遍，也不发事件（模型没看到新东西），编号留给下一次真正的查看
            self._n -= 1
            what = "、".join(dict.fromkeys(self.kinds[k.split(":", 1)[0]].label for k in keys))
            return LookResult(REPEAT_PROMPT.format(what=what), None, counted=True)
        self._seen |= keys
        record["elapsed_ms"] = int((time.monotonic() - started) * 1000)
        record["objects"] = objects
        text = "\n".join([head, NEED_PROMPT.format(need=need), "", body])
        self.records.append(record)
        self.texts.append(text)
        return LookResult(text, record)

    async def _run(self, line: dict[str, Any], need: str,
                   record: dict[str, Any]) -> tuple[str, list[dict[str, Any]], set[str]]:
        """真正查看：(交回的正文, objects[], 这一次给出详情的对象的身份)。status / reason 写进 record。"""
        targets = parse_targets(line)
        sources = _sources(line)
        unknown = [t["kind"] for t in targets if t["kind"] not in self.kinds]
        named: dict[str, list[LookTarget]] = {}
        picks: dict[str, list[str] | None] = {}
        for t in targets:
            if t["kind"] not in self.kinds:
                continue
            if t.get("name"):
                named.setdefault(t["kind"], []).append(t)
            else:
                # 只写了类别：按 need 在这一类里挑；写了 source 的只在那几个来源里挑
                scope = picks.setdefault(t["kind"], [])
                if scope is not None and t.get("source"):
                    scope.append(t["source"])
                elif not t.get("source"):
                    picks[t["kind"]] = None
        if not targets and self.default_kind:
            picks[self.default_kind] = None
        parts: list[str] = []
        objects: list[dict[str, Any]] = []
        keys: set[str] = set()
        indexed: list[str] = []
        empty: list[str] = []
        unavailable: list[str] = []
        budget = LOOK_RESULT_CHARS
        for kind_name in dict.fromkeys([*named, *picks]):
            kind = self.kinds[kind_name]
            found, missing = await kind.resolve(named.get(kind_name, [])) if kind_name in named else ([], [])
            fallback = bool(missing) and not found and kind_name not in picks
            if fallback:
                # 点名的一个都没认出来（常见的是拿说明里的中文词当表名）：按 need 在那几个来源里挑，和只写类别时一样，
                # 不让模型为了拿到真名再 look 一次。没认出来的照样写进「没找到」，不拿挑出来的冒充点名的
                picks[kind_name] = list(dict.fromkeys(str(t["source"]) for t in missing if t.get("source"))) or None
            if kind_name in picks:
                within = picks[kind_name] or sources
                chosen, reason = await kind.pick(need, within)
                if reason is not None or not chosen:
                    index = await kind.index(within)
                    if not index and reason is not None:
                        # 候选里一个能查看的都没有（库还没探查表结构）：没有目录可交，照实说原因，算查看没完成
                        unavailable.append(UNAVAILABLE_PROMPT.format(what=kind.label, reason=reason))
                        record["reason"] = reason
                    elif index or not fallback:
                        # 点名的来源本身不在这一轮里（目录是空的）：「没找到数据源」已经说清楚了，不再补一段空目录
                        indexed.append(INDEX_PROMPT.format(what=kind.label, reason=reason, index=index) if reason
                                       else EMPTY_PROMPT.format(what=kind.label, index=index))
                        if reason is not None:
                            record["reason"] = reason
                        else:
                            empty.append(kind.label)
                found += [o for o in chosen if kind.key(o) not in {kind.key(f) for f in found}]
            if not found and not missing:
                continue
            text, blocks = await kind.detail(found, budget, missing=missing)
            budget = max(budget - len(text), 0)
            parts.append(text)
            objects += blocks
            keys |= {kind.key(o) for o in found}
        if unknown:
            parts.append(UNKNOWN_KIND_PROMPT.format(kinds="、".join(dict.fromkeys(unknown)),
                                                    allowed="、".join(self.kinds)))
        parts += indexed + unavailable
        if unavailable and not indexed and not keys:
            record["status"] = "failed"
        elif indexed and not keys:
            # 一个对象都没给出详情、交回的是目录：挑选失败（有原因）或按需求没挑出来（返回空）
            record["status"] = "index"
            record.setdefault("reason", EMPTY_REASON.format(what="、".join(empty)))
        else:
            # 给出了详情：status 是 ok，reason 只在不是 ok 时带
            record.pop("reason", None)
        return "\n\n".join(p for p in parts if p), objects, keys
