"""数字推演：在一次运行的基础上改动输入，推演指标会怎样变化，不改原运行。

POST /api/runs/{run_id}/evidence/whatif：证据面板里点开口径卡上的一个数，改某个输入的值、或者排除它背后查询结果里的
几行，看这个指标和报告里引用它的地方会变成多少。推演永远不是出具：不追加任何事件（last_seq 不变）、不落工件、
不改报告，结果只交回给发请求的人。

只认这次运行封存范围内的工件，和证据接口同一套白名单（api/evidence.py 的 _sealed / _sealed_loader）：口径卡的指标集
从封存的 node.finished 台账找，排除行只认封存范围内交回过的查询快照，nodes.* 的完整值从封存的节点产出工件取，
vars.* 的完整值（value_artifact）只认封存的指标集里写着的。还在跑、停在审批的运行也能推演，结果里 sealed 为假：
数据可能还会变。遮罩列（查询当时记下的加数据源现在设的）不给值、也不能拿来推演，见 engine/recompute.py。

重算本身是纯函数（engine/recompute.py）。这里负责：认请求、找卡、准备工件和遮罩、找报告里引用的地方、标出可能受影响
的下游口径卡。
"""
from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Any

from fastapi import APIRouter, Body

from app.api.coded import CodedHTTPException
from app.api.evidence import (
    REPORT_NOT_FOUND,
    _Masks,
    _merge_masks,
    _recorded_masks,
    _reports,
    _Sealed,
    _sealed,
    _sealed_cards,
    _sealed_loader,
)
from app.core.artifact_store import load
from app.engine import recompute
from app.engine.merge_query import MERGE_SOURCE

router = APIRouter(prefix="/api/runs", tags=["whatif"])

BAD_REQUEST = "whatif_bad_request"
BAD_INPUT = "whatif_bad_input"
CARD_NOT_FOUND = "whatif_card_not_found"
CARD_TAMPERED = "whatif_card_tampered"
METRIC_NOT_FOUND = "whatif_metric_not_found"

BAD_REQUEST_TEXT = ("请求格式有误：card 应为口径卡节点的 ID；report、metric 可省略，填写时为节点 ID 和指标 ID；"
                    "overrides.inputs 应为「输入路径 → 新值」，overrides.exclude_rows 应为「查询快照的工件 ID → 行号列表」")
#: 一次最多改几个输入、排除几份快照里的行、每份最多几行：再多就是请求写错了
MAX_INPUTS = 200
MAX_SNAPSHOTS = 50
MAX_ROWS = 10000


@router.post("/{run_id}/evidence/whatif")
async def evidence_whatif(run_id: str, payload: Any = Body(None)) -> dict[str, Any]:
    """在一次运行的口径卡上推演：请求体 {report?, card, metric?, overrides: {inputs, exclude_rows}}。

    返回 {sealed, seal, card, metrics, segments, unsupported, affected_cards}：每个指标的旧值、新值、变化量和输入，
    报告里引用这些指标的地方会写成什么，不能用于推演的输入和原因，以及引用了这张卡、可能受影响（未重算）的下游口径卡。
    """
    report, card_node, metric, changes = _request(payload)
    sealed = await _sealed(run_id)
    reports = _reports(sealed)
    if report is not None and not any(r.node_id == report for r in reports):
        raise CodedHTTPException(404, "本次运行中没有所选报告撰写节点的文档", REPORT_NOT_FOUND)
    cards = _sealed_cards(sealed)
    artifact, card, started = _card_of(sealed, cards, card_node, reports, report)
    if metric is not None and not any(isinstance(m, dict) and str(m.get("id")) == metric
                                      for m in card.get("metrics") or []):
        raise CodedHTTPException(404, f"口径卡中没有指标「{metric}」", METRIC_NOT_FOUND)
    loader = _loader(sealed, cards)
    sources = recompute.Sources(load=loader, queries=tuple(sealed.queries),
                                outputs=_outputs_before(sealed, started), masked=await _hidden(sealed, loader))
    docs = [(r.node_id, r.doc) for r in reports
            if r.doc is not None and r.hash_ok is True and (report is None or r.node_id == report)]
    try:
        # 纯计算，连同读工件放进线程：快照大的时候不挡住同一进程里别的请求
        result, segments = await asyncio.to_thread(_compute, card, changes, sources, metric, docs, card_node, artifact)
    except recompute.WhatIfError as e:
        raise CodedHTTPException(422, str(e), BAD_INPUT) from None
    changed = any(m.get("changed") for m in result["metrics"])
    return {
        "run_id": run_id,
        "card": {"node_id": card_node, "artifact": artifact, "caliber": card.get("caliber") or "",
                 "version": card.get("caliber_version") or ""},
        "report": report,
        # 封存了、而且封存核对通过：推演依据的数据不会再变。没封存（还在跑、停在审批）或封存核对没过的照样能推演
        "sealed": sealed.trusted,
        "seal": {"sealed": sealed.sealed, "ok": sealed.seal["ok"]},
        "metrics": result["metrics"],
        "segments": segments,
        "unsupported": result["unsupported"],
        # 下游口径卡只在这次推演确实改变了指标时才标：什么都没变，下游也不会变
        "affected_cards": recompute.downstream_cards(cards, card_node) if changed else [],
    }


def _compute(card: dict[str, Any], changes: recompute.Changes, sources: recompute.Sources, metric: str | None,
             docs: list[tuple[str, dict[str, Any]]], card_node: str, artifact: str
             ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    result = recompute.whatif(card, changes, sources, metric=metric)
    segments = recompute.report_segments(docs, card_node=card_node, card_artifact=artifact, results=result["metrics"])
    return result, segments


def _request(payload: Any) -> tuple[str | None, str, str | None, recompute.Changes]:
    """(report, card, metric, 改动)。形状不对一律 422 whatif_bad_request；值对不对（路径在不在、行号越没越界）由重算时核对。"""
    def bad() -> CodedHTTPException:
        return CodedHTTPException(422, BAD_REQUEST_TEXT, BAD_REQUEST)

    if not isinstance(payload, dict):
        raise bad()
    card, report, metric = payload.get("card"), payload.get("report"), payload.get("metric")
    if not isinstance(card, str) or not card.strip():
        raise bad()
    if any(v is not None and (not isinstance(v, str) or not v.strip()) for v in (report, metric)):
        raise bad()
    overrides = payload.get("overrides")
    overrides = {} if overrides is None else overrides
    if not isinstance(overrides, dict):
        raise bad()
    inputs = {} if overrides.get("inputs") is None else overrides["inputs"]
    exclude = {} if overrides.get("exclude_rows") is None else overrides["exclude_rows"]
    if not isinstance(inputs, dict) or not isinstance(exclude, dict) \
            or len(inputs) > MAX_INPUTS or len(exclude) > MAX_SNAPSHOTS:
        raise bad()
    rows: dict[str, frozenset[int]] = {}
    for artifact, picked in exclude.items():
        if not isinstance(picked, list) or len(picked) > MAX_ROWS \
                or not all(isinstance(r, int) and not isinstance(r, bool) for r in picked):
            raise bad()
        if picked:
            rows[str(artifact)] = frozenset(picked)
    return (report.strip() if report else None, card.strip(), metric.strip() if metric else None,
            recompute.Changes(inputs={str(k): v for k, v in inputs.items()}, exclude=rows))


def _card_of(sealed: _Sealed, cards: list[tuple[str, str, dict[str, Any]]], node_id: str, reports: list[Any],
             report: str | None) -> tuple[str, dict[str, Any], int]:
    """要推演的那份指标集：(工件 id, 内容, 那次执行的 node.started 序号)。

    哪些 node.finished 算这张卡的执行，和 _sealed_cards 一样认：台账里的 metric_set 条目；一期以前没有台账的，画布上确实
    是口径卡的节点，产出本身就是指标集。同一张卡在循环里算过几次时，选报告引用的那一份（指定了 report 看那份报告，
    没指定看任意一份），都没引用就取最后一次。内容只认 _sealed_cards 取回、复验过哈希的：执行记录里有、内容却对不上
    哈希的说清是被改过，不说「没有这张卡」。
    """
    calibers = set(sealed.nodes_of("metrics"))
    runs: list[tuple[int, str]] = []
    for seq, etype, nid, data in sealed.events:
        if etype != "node.finished" or nid != node_id or not isinstance(data, dict):
            continue
        arts = [str(e["artifact"]) for e in data.get("evidence") or []
                if isinstance(e, dict) and e.get("kind") == "metric_set" and e.get("artifact")]
        if not arts and nid in calibers and data.get("artifact"):
            arts = [str(data["artifact"])]
        runs.extend((seq, art) for art in arts)
    if not runs:
        raise CodedHTTPException(404, "这次运行中没有这张口径卡的结果：节点不存在、不是口径卡、尚未执行或执行失败",
                                 CARD_NOT_FOUND)
    cited = {e.get("artifact") for r in reports if r.doc is not None and (report is None or r.node_id == report)
             for e in (r.doc.get("catalog") or {}).values()
             if isinstance(e, dict) and e.get("kind") == "metric" and e.get("node_id") == node_id}
    seq, artifact = next(((s, a) for s, a in reversed(runs) if a in cited), runs[-1])
    good = {art: card for nid, art, card in cards if nid == node_id}
    if artifact not in good:
        raise CodedHTTPException(409, "口径卡的指标集与哈希不一致或无法读取，疑似被修改，不能推演", CARD_TAMPERED)
    started = max((s for s, etype, nid, _ in sealed.events if etype == "node.started" and nid == node_id and s < seq),
                  default=seq)
    return artifact, good[artifact], started


def _outputs_before(sealed: _Sealed, started: int) -> dict[str, str]:
    """口径卡那次执行开始之前，每个节点最后一次的产出工件：口径卡当时读到的 nodes.* 就是它们。"""
    out: dict[str, str] = {}
    for seq, etype, node_id, data in sealed.events:
        if seq >= started:
            break
        if etype == "node.finished" and node_id and isinstance(data, dict) and data.get("artifact"):
            out[node_id] = str(data["artifact"])
    return out


def _loader(sealed: _Sealed, cards: list[tuple[str, str, dict[str, Any]]]) -> Any:
    """只认这次运行封存范围内的工件：证据接口的白名单（查询、表结构、检索快照和台账记过的），加上封存的节点产出工件，
    以及封存的指标集里写着的完整值（value_artifact——写在内容寻址、又在封存范围内的指标集里，属于封存链的延伸）。

    未封存、封存核对没过的运行也能推演（结果里 sealed 为假）：白名单按同一套规则算，只是不要求封存完好。
    取回时照旧复验哈希，对不上抛 ValueError（artifact_store.load 的约定），由重算照实说。
    """
    base = _sealed_loader(replace(sealed, seal={**sealed.seal, "sealed": True, "ok": True}))
    extra = {str(data["artifact"]) for _, etype, _, data in sealed.events
             if etype == "node.finished" and isinstance(data, dict) and data.get("artifact")}
    extra |= {str(item["value_artifact"]) for _, _, card in cards
              for metric in card.get("metrics") or [] if isinstance(metric, dict)
              for item in metric.get("inputs") or [] if isinstance(item, dict) and item.get("value_artifact")}
    memo: dict[str, Any] = {}

    def loader(artifact: str) -> Any:
        key = str(artifact)
        if key not in memo:
            memo[key] = load(key) if key in extra else base(key)
        return memo[key]
    return loader


async def _hidden(sealed: _Sealed, loader: Any) -> dict[str, frozenset[str]]:
    """封存范围内每份查询快照要遮的列（小写）：查询当时记下的加数据源现在设的，合并查询按它合并的那几个数据源算。
    和证据面板查询步骤的遮罩同一套（api/evidence.py 的 _query_step）。取不回来、被改过的快照不算：重算也不会用它。
    快照经推演用的同一个 loader 取，读过的重算时不再读盘。"""
    masks = _Masks()
    out: dict[str, frozenset[str]] = {}
    for artifact, info in sealed.queries.items():
        try:
            snap = loader(artifact)
        except ValueError:
            continue
        if not isinstance(snap, dict):
            continue
        if snap.get("source") == MERGE_SOURCE:
            hidden, _ = await _merge_masks(snap, masks)
        else:
            hidden = (await masks.of(info.get("source") or snap.get("source")))[0] | _recorded_masks(snap)
        if hidden:
            out[artifact] = frozenset(hidden)
    return out
