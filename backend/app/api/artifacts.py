from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from sqlalchemy import select

from app.api.deps import can_read_artifact, current_principal
from app.core import artifact_store

router = APIRouter(prefix="/api/artifacts", tags=["artifacts"])

_MAX_INLINE = 2 * 1024 * 1024


@router.get("/{artifact_id}")
async def get_artifact(artifact_id: str) -> dict[str, Any]:
    """按内容哈希取回一个工件。取回时会复验哈希——内容不可变是这层的全部承诺。

    开了登录：builder 及以上照旧拿原文，只被别人的个人应用刷新、个人蓝图试运行引用的除外（只给拥有者，管理员也一样，
    auth/access.artifact_readable）。user 角色只能取它能读的运行引用的工件（can_read_artifact），而且对它来说遮罩
    就是边界：只给白名单里的几种，按派生遮罩处理后再给（_masked_for_user），别的种类一律不给。"""
    principal = current_principal()
    restricted = principal.mode != "off" and not principal.at_least("builder")
    if principal.mode != "off" and not await can_read_artifact(principal, artifact_id):
        raise HTTPException(403, "没有权限查看这件工件")
    try:
        content = artifact_store.load(artifact_id)
    except ValueError as e:
        # 哈希对不上：明确报给调用方，这正是工件库存在的意义
        raise HTTPException(409, str(e)) from e
    if content is None:
        raise HTTPException(404, "工件不存在，可能已被删除")
    if restricted:
        content = await _masked_for_user(principal, artifact_id, content)
    return {"id": artifact_id, "content": content}


#: user 角色能取的工件种类（白名单）。别的种类——节点的整份产出（node_output，含遮罩列的查询行）、口径卡取值的完整原值
#: （metric_input）、工具快照、双盲复算和反方审稿的记录、质询答复、知识库快照——遮罩管不全，一律不给
_REPORT = "report_doc"
_METRIC_SET = "metric_set"
_RECEIPT = "action_receipt"


async def _masked_for_user(principal: Any, artifact_id: str, content: Any) -> Any:
    """user 角色看到的样子，按工件登记的种类分：
    - 报告文档：写作时就按遮罩写好的（遮罩列的值在正文、目录里本来就是「已遮罩」），照给；
    - 查询快照：照核查 agent 的工具边界那一套遮（engine/masking.py 的 mask_query_result：遮罩列、按 SQL 认出取自遮罩列的
      列、按值认的遮罩值）；端点快照遮登记的遮罩列，响应原文去掉；
    - 指标集：和证据面板同一个判断（api/evidence.py 的 _metric_masks），取自遮罩列的指标的值、输入、代入式写「已遮罩」；
    - 动作回执：响应体按能力登记的遮罩字段遮（同确认卡上的预览）；
    - 别的种类：不给。"""
    from app.capabilities.endpoints import SNAPSHOT_KIND
    from app.data import catalog as data_catalog
    from app.db.base import SessionLocal
    from app.db.models import Artifact

    async with SessionLocal() as session:
        rows = list((await session.execute(
            select(Artifact.kind, Artifact.run_id, Artifact.node_id).where(Artifact.id == artifact_id))).all())
    kinds = {kind for kind, _, _ in rows}
    if kinds & {data_catalog.QUERY_SNAPSHOT_KIND, SNAPSHOT_KIND} and isinstance(content, dict):
        return await _masked_snapshot(content, endpoint=SNAPSHOT_KIND in kinds)
    if _REPORT in kinds:
        return content
    if _METRIC_SET in kinds and isinstance(content, dict):
        return await _masked_metrics(principal, artifact_id, content, rows)
    if _RECEIPT in kinds and isinstance(content, dict):
        return await _masked_receipt(content, rows)
    raise HTTPException(403, "这类工件的原文只对搭建者及以上的角色开放")


async def _masked_snapshot(content: dict[str, Any], *, endpoint: bool) -> dict[str, Any]:
    from app.data.engine import masked_columns
    from app.db.base import SessionLocal
    from app.db.models import DataSource
    from app.engine.masking import mask_query_result

    source = None
    if content.get("source") and not endpoint:
        async with SessionLocal() as session:
            source = (await session.execute(select(DataSource).where(DataSource.name == str(content["source"])))
                      ).scalar_one_or_none()
    hidden = list(dict.fromkeys([*(str(c) for c in content.get("mask_columns") or []),
                                 *(masked_columns(source.options) if source is not None else [])]))
    done = mask_query_result(content, hidden, source.kind if source is not None else None,
                             schema=source.schema_cache if source is not None else None)
    view = dict(done.payload)
    if endpoint:
        view.pop("response", None)
    return view


async def _masked_metrics(principal: Any, artifact_id: str, card: dict[str, Any],
                          rows: list[Any]) -> dict[str, Any]:
    """指标集按它所在的那次运行认遮罩（证据面板的指标步骤同一套）：取自遮罩列的输入写「已遮罩」、代入式里那几处同样换掉，
    指标本身不给值的值和渲染出来的字一并换掉。工件按内容寻址、可能被好几次运行引用，按这个人能读的那一次认。"""
    from app.api.deps import can_read_run
    from app.api.evidence import MASKED, _held_word, _Masks, _metric_masks, _sealed, _withheld
    from app.engine import recompute

    owner = None
    for kind, run_id, node_id in rows:
        if kind == _METRIC_SET and run_id and await can_read_run(principal, run_id):
            owner = (run_id, node_id or "")
            break
    if owner is None:
        raise HTTPException(403, "没有权限查看这件工件")
    sealed = await _sealed(owner[0])
    masks = _Masks(sealed)
    metrics: list[Any] = []
    for metric in card.get("metrics") or []:
        if not isinstance(metric, dict):
            metrics.append(metric)
            continue
        held, word = await _metric_masks(metric, owner[1], artifact_id, sealed, masks, card=card)
        shown = dict(metric)
        shown["inputs"] = [_withheld(item, held[str(item.get("path"))])
                           if isinstance(item, dict) and str(item.get("path")) in held else item
                           for item in metric.get("inputs") or []]
        if withheld := {path: _held_word(found) for path, found in held.items() if found.kind != "table"}:
            shown["substituted"] = recompute.substituted_without(metric, withheld)
        if metric.get("value") is not None and (word is not None or metric.get("masked")):
            shown["value"] = shown["rendered"] = word or MASKED
        metrics.append(shown)
    return {**card, "metrics": metrics}


async def _masked_receipt(content: dict[str, Any], rows: list[Any]) -> dict[str, Any]:
    """动作回执：响应体按写入能力登记的遮罩字段遮。遮罩字段取现在的登记和发起时的治理快照两份的并集：发起之后管理员才
    加上的遮罩，原文同样不给。"""
    from app.capabilities.registry import masked_paths
    from app.db.base import SessionLocal
    from app.db.models import ActionRecord, Capability, Run
    from app.process.actions import masked

    pointers: list[str] = []
    async with SessionLocal() as session:
        for kind, run_id, node_id in rows:
            if kind != _RECEIPT or not run_id:
                continue
            names = set((await session.execute(select(ActionRecord.capability).where(
                ActionRecord.run_id == run_id, ActionRecord.node_id == node_id))).scalars())
            run = await session.get(Run, run_id)
            snapshot = ((run.options or {}).get("capabilities") or {}) if run is not None else {}
            for name in names:
                cap = await session.get(Capability, name)
                pointers += masked_paths(cap) if cap is not None else []
                pointers += [str(p) for p in (snapshot.get(name) or {}).get("masked") or []]
    return {**content, "body": masked(content.get("body"), list(dict.fromkeys(pointers)))}
