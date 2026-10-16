"""评测里的两种模型判断：结论裁判、文字要点（细案 01 做法 10、11；评测-D4）。都只在变体打开了裁判时做，都是概率性的判断，
单列「模型判断」，不进准确率；花费都记在评测那本账上（engine/spend.eval_spend），不占裁判自己的每日额度。

- 结论裁判（judge_claims）：走按需裁判同一条路——api/evidence 的 _judge_now（证据面板上点「请模型判断」就是它）：只取封存
  范围内的工件、遮罩列不给裁判、判定作为 evidence.judged 追加在封存之后（post_seal）、报告文档一个字不改。送哪几句由裁判
  自己的预筛定（judge.candidates：结论句，挂了依据的在前）。花费经 judge.charged_to 记在评测那本账上、按评测的每日上限。
  结果按 supported / partial / contradicted / insufficient / 未判 数句数。
- 文字要点（judge_points）：问法、答案原文和要点交给裁判模型（设置里的证据裁判模型，和按需裁判同一个取值链），逐条说答到
  了（covered）、答到一部分（partial）、没答到（missing），附一句理由。答案原文只是这次运行的成果（遮过的），要点是人写的。
  不放查询结果、不放 SQL：要点问的是「说没说到」，不是「数对不对」（数由确定性的评分判）。

判断出错（模型没配好、超时、格式不对）一律照实记「没有判」和原因，不挡评分。
"""
from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping, Sequence
from typing import Any

logger = logging.getLogger(__name__)

#: 结论裁判按句数分（judge.COUNT_KEYS 里评测关心的几种，unsupported 是旧数据，并进 contradicted）
CLAIM_KEYS = ("supported", "partial", "contradicted", "insufficient", "unjudged")
#: 文字要点一次最多交给裁判多少字的答案
ANSWER_CHARS = 6000
#: 文字要点的输出额度、时限
POINTS_MAX_TOKENS = 2048
POINTS_TIMEOUT_S = 90

POINTS_SYSTEM_PROMPT = """\
你是评测的裁判。下面给出一个问题、对它的回答，以及期望回答里应当说到的几条要点。逐条判断回答有没有说到这条要点：
- covered：说到了（意思一致即可，不要求字面相同）
- partial：说到了一部分，或者说得含糊
- missing：没有说到，或者说的和要点相反
只看回答里写了什么，不要自己补充事实，也不要判断回答里的数对不对（数另有确定性的核对）。
只输出一个 JSON 数组，每条要点一项：[{"point": 要点序号（从 1 起）, "verdict": "covered|partial|missing", "reason": "一句话理由"}]"""
POINTS_HUMAN_PROMPT = "问题：{question}\n\n回答：\n{answer}\n\n要点：\n{points}"


async def judge_claims(run_id: str, *, daily_max_usd: float | None) -> dict[str, Any]:
    """按需裁判这次作答的报告（结论句）。{counts, model, cost_usd, calls, reason}；判不了的照实写 reason。"""
    from fastapi import HTTPException

    from app.api import evidence
    from app.engine import judge as judging
    from app.engine.spend import eval_spend

    empty = {k: 0 for k in CLAIM_KEYS}
    try:
        async with evidence._JUDGING.hold(run_id):
            sealed = await evidence._sealed(run_id)
            if not sealed.sealed or not sealed.trusted or not sealed.ledger_on:
                return {"counts": empty, "reason": "作答的运行没有可信的封存，不判"}
            reports = evidence._reports(sealed)
            if not reports:
                return {"counts": empty, "reason": "答案没有报告文档，没有结论句可判"}
            chosen = evidence._choose(reports, None, sealed)
            if chosen.doc is None or chosen.hash_ok is False:
                return {"counts": empty, "reason": "报告文档读不出来或被改过，不判"}
            units = [c.unit for c in judging.candidates(chosen.doc)][:evidence.JUDGE_MAX_UNITS]
            if not units:
                return {"counts": empty, "reason": "报告里没有要判的结论句"}
            with judging.charged_to(eval_spend, daily_max_usd):
                result = await evidence._judge_now(sealed, chosen, units)
    except HTTPException as e:
        return {"counts": empty, "reason": str(e.detail)}
    except Exception as e:  # noqa: BLE001 - 裁判是附加的一步：出错照实记，不挡评分
        from app.core.errors import describe_exception

        logger.exception("评测：结论裁判出错（运行 %s）", run_id)
        return {"counts": empty, "reason": f"结论裁判出错：{describe_exception(e)}"}
    counts = dict(empty)
    for verdict in (result.get("verdicts") or {}).values():
        status = (verdict or {}).get("status")
        status = "contradicted" if status == judging.UNSUPPORTED else status
        if status in counts:
            counts[status] += 1
        elif status != "not_a_claim":
            counts["unjudged"] += 1
    return {"counts": counts, "units": len(units), "model": result.get("model"), "priced": result.get("priced"),
            "cost_usd": float(result.get("cost_usd") or 0.0), "calls": int(result.get("calls") or 0),
            "limited": bool(result.get("limited")), "reason": result.get("message")}


def _parse_points(text: str, n: int) -> list[dict[str, Any]] | None:
    match = re.search(r"\[.*\]", text or "", re.DOTALL)
    if match is None:
        return None
    try:
        items = json.loads(match.group(0))
    except ValueError:
        return None
    if not isinstance(items, list):
        return None
    out: dict[int, dict[str, Any]] = {}
    for item in items:
        if not isinstance(item, Mapping):
            continue
        index = item.get("point")
        verdict = item.get("verdict")
        if isinstance(index, int) and 1 <= index <= n and verdict in ("covered", "partial", "missing"):
            out[index] = {"point": index, "verdict": verdict, "reason": str(item.get("reason") or "")[:200]}
    if len(out) != n:
        return None
    return [out[i] for i in range(1, n + 1)]


async def judge_points(question: str, answer: str, expectations: Sequence[Mapping[str, Any]], *,
                       daily_max_usd: float | None, per_run_left: float | None = None) -> dict[str, Any]:
    """文字要点：{expectations: {期望 id: {points: [{point, verdict, reason}], reason?}}, model, cost_usd, calls, reason?}。
    先按目录价估一次、在评测那本账上预留，加上会超过每日上限（或这次评测剩下的钱）就不判、照实写。"""
    from langchain_core.messages import HumanMessage, SystemMessage

    from app.core.errors import describe_exception
    from app.db.base import SessionLocal
    from app.engine.judge import judge_model_spec
    from app.engine.spend import eval_spend
    from app.providers import usage as usage_meter
    from app.providers.catalog import estimate_cost, find_pricing
    from app.providers.factory import get_chat_model

    texts = [e for e in expectations if e.get("kind") == "text" and e.get("points")]
    if not texts:
        return {"expectations": {}}
    if not (answer or "").strip():
        return {"expectations": {}, "reason": "答案是空的，文字要点没有判"}
    try:
        async with SessionLocal() as session:
            spec = await judge_model_spec(session, None)
            model, model_id = await get_chat_model(session, spec.model_copy(update={
                "max_tokens": POINTS_MAX_TOKENS, "timeout": POINTS_TIMEOUT_S}))
    except Exception as e:  # noqa: BLE001 - 裁判模型没配好：照实写没有判
        return {"expectations": {}, "reason": f"裁判模型用不了：{describe_exception(e)}"}
    priced = find_pricing(model_id) is not None
    out: dict[str, Any] = {}
    meter = usage_meter.meter()
    reason = None
    for exp in texts:
        points = [str(p) for p in exp.get("points") or []]
        human = POINTS_HUMAN_PROMPT.format(question=question, answer=answer[:ANSWER_CHARS],
                                           points="\n".join(f"{i}. {p}" for i, p in enumerate(points, start=1)))
        estimate = estimate_cost(model_id, (len(POINTS_SYSTEM_PROMPT) + len(human)) // 2, 60 * len(points)) if priced \
            else 0.0
        if per_run_left is not None and priced and estimate > per_run_left:
            out[exp["id"]] = {"reason": "这次评测的金额上限不够再判文字要点"}
            continue
        day = await eval_spend.charge(estimate, limit=daily_max_usd if priced else None, priced=priced)
        if day is None:
            out[exp["id"]] = {"reason": "评测今天的金额已到上限，文字要点没有判"}
            continue
        before = meter.summary()["cost_usd"]
        try:
            with usage_meter.metering(meter):
                reply = await model.ainvoke([SystemMessage(POINTS_SYSTEM_PROMPT), HumanMessage(human)])
            text = reply.content if isinstance(reply.content, str) else json.dumps(reply.content, ensure_ascii=False)
            parsed = _parse_points(text, len(points))
            out[exp["id"]] = {"points": parsed} if parsed else {"reason": "裁判的回复格式不对，文字要点没有判"}
        except Exception as e:  # noqa: BLE001
            logger.exception("评测：判文字要点出错")
            reason = f"判文字要点出错：{describe_exception(e)}"
            out[exp["id"]] = {"reason": reason}
        spent = meter.summary()["cost_usd"] - before
        await eval_spend.settle(day, (spent if priced else 0.0) - estimate)
        if per_run_left is not None:
            per_run_left -= spent
    summary = meter.summary()
    return {"expectations": out, "model": model_id, "usage": summary, "cost_usd": summary["cost_usd"],
            "calls": summary["calls"], **({"reason": reason} if reason else {})}


__all__ = ["CLAIM_KEYS", "POINTS_HUMAN_PROMPT", "POINTS_SYSTEM_PROMPT", "judge_claims", "judge_points"]
