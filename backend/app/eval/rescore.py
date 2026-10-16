"""按新规则重新评分（细案 01 做法 10）：评分规则改了（score.SCORER_VERSION 加一），已经评完的评测不必重跑一遍。

**只读封存，不调模型、不查业务库。** 每一项：期望用存下的那一份（EvalResult.gold：作答前后两次和结论），答案从作答那次运行的
封存里重新读（score.read_answer），文字要点用当时裁判给的判断（EvalResult.judge，不再请裁判），逐题 SQL 差异按同一个比对函数
再比一次（纯计算）。作答前就算不出期望、没作答的，照旧按存下的期望结论。总表跟着重算（runner.refresh_summary）。

明细（作答经过、直接回答的原文）在明细工件里：清理掉了的评测不能重评（接口先拦）。
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select

from app.db.base import SessionLocal
from app.db.models_eval import EvalCase, EvalResult, EvalRun

logger = logging.getLogger(__name__)


async def rescore(eval_run_id: str) -> int:
    """重评一次评测（只这一臂）。交回结论变了的项数。"""
    from app.core import artifact_store
    from app.eval import runner, score
    from app.eval.cases import case_fields

    async with SessionLocal() as session:
        run = await session.get(EvalRun, eval_run_id)
        if run is None:
            return 0
        rows = list((await session.execute(select(EvalResult).where(EvalResult.eval_run_id == eval_run_id)
                                           .order_by(EvalResult.seq))).scalars())
        cases = {c.id: case_fields(c) for c in (await session.execute(
            select(EvalCase).where(EvalCase.id.in_({r.case_id for r in rows if r.case_id})))).scalars()}
    changed = 0
    updates: dict[str, dict[str, Any]] = {}
    for row in rows:
        case = cases.get(row.case_id or "")
        if row.status != "ok" or case is None:
            continue
        details: dict[str, Any] = {}
        if row.details_artifact:
            try:
                details = artifact_store.load(row.details_artifact) or {}
            except ValueError:
                details = {}
        turn = details.get("turn") or {}
        if row.path is None:
            graded = score.score_case(case, row.gold, score.Answer(run_id=None), path=None)
            match = row.sql_match
        else:
            answer = await score.read_answer(row.run_id if row.path == "ran" else None, reply=turn.get("reply"))
            graded = score.score_case(case, row.gold, answer, path=row.path, path_reason=turn.get("reason"),
                                      judgement=(row.judge or {}).get("text"))
            match = await score.sql_match(case, row.gold, answer)
        if graded.verdict != row.verdict:
            changed += 1
        updates[row.id] = {"verdict": graded.verdict, "scores": graded.scores, "sql_match": match,
                           "error": graded.reason if graded.verdict != "pass" else None}
    async with SessionLocal() as session:
        for result_id, values in updates.items():
            row = await session.get(EvalResult, result_id)
            if row is not None:
                for key, value in values.items():
                    setattr(row, key, value)
        await session.flush()
        run = await session.get(EvalRun, eval_run_id)
        await runner.refresh_summary(session, run)
        await session.commit()
    return changed


__all__ = ["rescore"]
