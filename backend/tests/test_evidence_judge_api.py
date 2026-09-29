"""按需裁判接口 POST /api/runs/{id}/evidence/judge（四期）。

探索运行不在节点里花裁判的钱：点开哪句才判哪句。

- 判定作为批注追加在封存之后（evidence.judged 事件，post_seal: true）：封存核对只覆盖 manifest_seq 之前的
  事件，所以追加之后 verify 照旧一致；封存的报告文档一个字都不改
- 只在探索运行里可用：正式运行的裁判在节点内完成，按需裁判返回 409；还没封存的、升级前发起的、封存链断了的
  也不判
- 每次点击的上限、全局每日上限按设置控制；触顶不是报错，照实说「已到上限」和怎么调
- 同一次运行里同一句判过就直接给已有的判定，不再调用、不再花钱（连点两下也一样）
- 裁判只看得到封存范围内的证据：人为插进工件表的、封存之后才追加事件引用的快照，一行都不进摘录；
  数据源遮罩的列同样不给
- 片段接口和证据图带上判定，封存内的和封存后追加的分得开

所有裁判都用 mock 模型给剧本，不调真接口。
"""
from __future__ import annotations

import asyncio
import json
import re
import sqlite3
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient
from langchain_core.messages import AIMessage
from sqlalchemy import delete, select, update

from app.core import artifact_store
from app.core.events import EventType
from app.data.engine import engines
from app.db.base import SessionLocal
from app.db.models import DataSource, Run, RunEvent, Setting
from app.engine import judge as judge_mod
from app.engine import runner as runner_mod
from app.engine.evidence import iter_segments, iter_units
from app.engine.judge import JUDGE_ROLE, SPEND_KEY, daily_spend
from app.engine.runner import run_manager, verify_manifest
from app.main import app
from app.providers.mock_model import MockChatModel

PRICED = "claude-sonnet-5"
METRICS = [{"id": "gmv", "name": "销售额", "unit": "元", "decimals": 1, "expression": "vars.kpi.gmv"}]
#: 一句挂了依据的结论、一句没挂依据的归因、一句连接性的话
TEXT = "## 本周\n\n本周销售额 [[m:gmv]]。[[see:m:gmv]]增长主要来自新客首单。下面看细节。"
CITED, CAUSAL, LINK = "本周销售额 45,678.5元。", "增长主要来自新客首单。", "下面看细节。"


@pytest.fixture(autouse=True)
async def engine_up():
    await run_manager.setup()
    async with SessionLocal() as session:
        await session.execute(delete(Setting).where(Setting.key.in_([SPEND_KEY, "judge", "copilot"])))
        await session.commit()
    yield
    await run_manager.shutdown()


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


def node(nid, ntype, **config):
    return {"id": nid, "type": ntype, "position": {"x": 0, "y": 0}, "data": {"label": nid, "config": config}}


def chain(*nodes) -> dict:
    ids = [n["id"] for n in nodes]
    return {"nodes": list(nodes), "edges": [{"source": a, "target": b} for a, b in zip(ids, ids[1:])]}


def weekly(*, gate=False, **report) -> dict:
    return chain(
        node("start", "input", fields=[{"name": "week", "default": "2026-W37"}]),
        node("fetch", "transform", mode="expression", expression="{'gmv': 45678.5}", assign_to="kpi"),
        node("caliber", "metrics", caliber="周报口径", caliber_version="v2", metrics=METRICS),
        node("write", "report", instructions="写周报", **report),
        *([node("gate", "human", mode="approve", title="确认")] if gate else []),
        node("out", "output", fields=[{"name": "周报", "value": "{{ nodes.write.text }}"}]),
    )


class Models:
    """写作者回 text；裁判按句子里的字给判定（verdicts：{句中片段: 判定}，没对上的是 supported）。
    judges 记下每次交给裁判的请求原文，specs 记下每次要的裁判模型。"""

    def __init__(self, monkeypatch, text=TEXT, verdicts=None, *, model=PRICED, fail_judge=None, delay=0.0):
        self.judges: list[str] = []
        self.specs: list = []
        verdicts = verdicts or {}

        def decide(chat, messages):
            if JUDGE_ROLE in str(messages[0].content):
                request = str(messages[-1].content)
                self.judges.append(request)
                if fail_judge:
                    raise RuntimeError(fail_judge)
                items = [{"unit": uid,
                          "verdict": next((s for frag, s in verdicts.items() if frag in line), "supported"),
                          "rationale": f"判的是：{line.strip()[:16]}", "used": []}
                         for uid, line in re.findall(r"^\[(u\d+)\]([^\n]*)$", request, re.M)]
                return AIMessage(content=json.dumps({"items": items}, ensure_ascii=False))
            return AIMessage(content=text)

        monkeypatch.setattr(MockChatModel, "_decide", decide)

        async def judge_model_of(session, spec):
            self.specs.append(spec)
            if delay:
                await asyncio.sleep(delay)
            return MockChatModel(model_name=spec.model or model), spec.model or model

        monkeypatch.setattr(judge_mod, "get_chat_model", judge_model_of)


async def wait(run_id: str, statuses=("succeeded", "failed")) -> Run:
    for _ in range(400):
        await asyncio.sleep(0.05)
        async with SessionLocal() as session:
            row = await session.get(Run, run_id)
        if row.status in statuses and (row.status == "interrupted" or row.manifest_seq is not None):
            return row
    raise AssertionError(f"run {run_id} 没跑完：{row.status}")


async def finish(graph, *, run_class="exploratory", statuses=("succeeded", "failed")) -> Run:
    run = await run_manager.start(graph=graph, input_payload={}, run_class=run_class)
    row = await wait(run.id, statuses)
    return row


def doc_of(row: Run) -> dict:
    return artifact_store.load(row.output["_evidence"]["doc_artifact"])


def unit_ids(doc: dict) -> dict[str, str]:
    md = doc["markdown"]
    return {md[u["span"][0]:u["span"][1]].strip(): u["id"] for _, u in iter_units(doc)}


def unit_starting(doc: dict, prefix: str) -> str:
    return next(uid for text, uid in unit_ids(doc).items() if text.startswith(prefix))


def seg_in(doc: dict, uid: str) -> str:
    return next(s["id"] for _, u in iter_units(doc) if u["id"] == uid for s in u["segments"] if s["kind"] == "text")


async def judged_events(run_id: str) -> list[RunEvent]:
    async with SessionLocal() as session:
        return list((await session.execute(select(RunEvent).where(
            RunEvent.run_id == run_id, RunEvent.type == EventType.EVIDENCE_JUDGED).order_by(RunEvent.seq))).scalars())


async def judge(client, run_id: str, units, **extra):
    return await client.post(f"/api/runs/{run_id}/evidence/judge", json={"units": units, **extra})


async def save_setting(key: str, value) -> None:
    async with SessionLocal() as session:
        row = await session.get(Setting, key)
        if row is None:
            session.add(Setting(key=key, value=value))
        else:
            row.value = value
        await session.commit()


# --------------------------------------------------------------------------
# 判定落在封存之后，封存核对照旧一致
# --------------------------------------------------------------------------


async def test_an_on_demand_verdict_lands_after_the_seal_and_the_seal_still_holds(client, monkeypatch):
    models = Models(monkeypatch, verdicts={"新客": "unsupported"})
    row = await finish(weekly(claims="judge"))
    assert row.status == "succeeded" and models.judges == [], "探索运行节点里不判"
    doc = doc_of(row)
    ids = unit_ids(doc)

    res = await judge(client, row.id, [ids[CAUSAL]])
    assert res.status_code == 200, res.text
    body = res.json()
    verdict = body["verdicts"][ids[CAUSAL]]
    assert verdict["status"] == "unsupported" and verdict["post_seal"] is True and verdict["judge"] == PRICED
    assert verdict["rationale"] and body["judged"] == [ids[CAUSAL]] and body["reused"] == []
    assert body["report"] == {"node_id": "write", "doc_artifact": row.output["_evidence"]["doc_artifact"]}
    assert body["post_seal"] is True and body["limited"] is False and body["calls"] == 1
    assert len(models.judges) == 1 and CAUSAL in models.judges[0] and CITED not in models.judges[0]

    [event] = await judged_events(row.id)
    assert event.seq > row.manifest_seq and event.node_id == "write" and body["event"] == {"seq": event.seq}
    data = event.data
    assert data["post_seal"] is True and data["report"] == "write"
    assert data["doc_artifact"] == body["report"]["doc_artifact"]
    assert data["verdicts"] == {ids[CAUSAL]: verdict} and data["model"] == PRICED and data["calls"] == 1
    assert set(data["keys"]) == {ids[CAUSAL]} and data["units"] == [ids[CAUSAL]]

    check = await verify_manifest(row.id)
    assert check["ok"] is True and check["sealed_at"] == row.manifest_seq
    assert (await client.get(f"/api/runs/{row.id}/verify")).json()["ok"] is True
    async with SessionLocal() as session:
        after = await session.get(Run, row.id)
    assert after.manifest_seq == row.manifest_seq and after.last_seq == event.seq
    assert doc_of(row) == doc, "封存的文档一个字都不改"
    csv = await client.get(f"/api/runs/{row.id}/evidence/audit", params={"format": "csv"})
    assert csv.headers["x-evidence-seal"] == "ok"


async def test_any_claim_in_an_explore_report_can_be_judged(client, monkeypatch):
    """报告节点没开 claims: judge 也能按需判：结论句都是候选。"""
    models = Models(monkeypatch)
    row = await finish(weekly())
    ids = unit_ids(doc_of(row))
    body = (await judge(client, row.id, [ids[CITED], ids[CAUSAL]])).json()
    assert {u: v["status"] for u, v in body["verdicts"].items()} == {ids[CITED]: "supported", ids[CAUSAL]: "supported"}
    assert len(models.judges) == 1, "一次点击的几句合成一次调用"
    assert "【m:gmv】" in models.judges[0], "挂的依据摘给裁判"


async def test_sequence_numbers_continue_after_a_restart(client, monkeypatch):
    """进程重启后内存里的序号从 0 起：追加的事件要接在库里最后一条后面，不能和已有的撞号。"""
    from app.core.bus import bus

    Models(monkeypatch)
    row = await finish(weekly())
    ids = unit_ids(doc_of(row))
    bus._seq.pop(row.id, None)
    await judge(client, row.id, [ids[CAUSAL]])
    [event] = await judged_events(row.id)
    async with SessionLocal() as session:
        seqs = [s for (s,) in await session.execute(select(RunEvent.seq).where(RunEvent.run_id == row.id))]
    assert event.seq == max(seqs) and seqs.count(event.seq) == 1 and event.seq > row.manifest_seq


# --------------------------------------------------------------------------
# 同一句不重复判
# --------------------------------------------------------------------------


async def test_asking_again_returns_the_verdict_without_another_call(client, monkeypatch):
    models = Models(monkeypatch, verdicts={"新客": "partial"})
    row = await finish(weekly(claims="judge"))
    ids = unit_ids(doc_of(row))
    first = (await judge(client, row.id, [ids[CAUSAL]])).json()
    spent = (await daily_spend())["calls"]
    again = (await judge(client, row.id, [ids[CAUSAL]])).json()
    assert len(models.judges) == 1 and (await daily_spend())["calls"] == spent, "不再调用、不再计费"
    assert again["verdicts"] == first["verdicts"] and again["reused"] == [ids[CAUSAL]] and again["judged"] == []
    assert again["calls"] == 0 and again["event"] is None and again["message"] is None
    assert len(await judged_events(row.id)) == 1

    # 判过的和没判过的一起点：只判没判过的
    both = (await judge(client, row.id, [ids[CAUSAL], ids[CITED]])).json()
    assert both["reused"] == [ids[CAUSAL]] and both["judged"] == [ids[CITED]]
    assert len(models.judges) == 2 and CAUSAL not in models.judges[1]


async def test_a_double_click_is_judged_once(client, monkeypatch):
    models = Models(monkeypatch, delay=0.05)
    row = await finish(weekly(claims="judge"))
    uid = unit_ids(doc_of(row))[CAUSAL]
    one, two = await asyncio.gather(judge(client, row.id, [uid]), judge(client, row.id, [uid]))
    assert one.status_code == two.status_code == 200
    assert len(models.judges) == 1 and len(await judged_events(row.id)) == 1
    assert sorted([one.json()["reused"], two.json()["reused"]]) == [[], [uid]]


async def test_a_failed_attempt_can_be_retried(client, monkeypatch):
    Models(monkeypatch, fail_judge="网关 502")
    row = await finish(weekly(claims="judge"))
    uid = unit_ids(doc_of(row))[CAUSAL]
    failed = (await judge(client, row.id, [uid])).json()
    assert failed["verdicts"][uid]["status"] == "unjudged" and failed["verdicts"][uid]["reason"] == "error"
    assert "网关 502" in failed["message"] and failed["limited"] is False
    models = Models(monkeypatch, verdicts={"新客": "unsupported"})
    retried = (await judge(client, row.id, [uid])).json()
    assert retried["verdicts"][uid]["status"] == "unsupported" and retried["reused"] == [] and len(models.judges) == 1


# --------------------------------------------------------------------------
# 上限
# --------------------------------------------------------------------------


async def test_hitting_the_click_limit_is_a_result_not_an_error(client, monkeypatch):
    models = Models(monkeypatch)
    await save_setting("judge", {"click_max_cost_usd": 1e-9})
    row = await finish(weekly(claims="judge"))
    uid = unit_ids(doc_of(row))[CAUSAL]
    res = await judge(client, row.id, [uid])
    assert res.status_code == 200
    body = res.json()
    assert body["limited"] is True and body["limits_hit"] == ["max_cost_usd"]
    assert body["unjudged"] == {"max_cost_usd": 1}
    assert body["message"].startswith("已到上限") and "这次点击" in body["message"]
    assert any("设置" in how and "不限" in how for how in body["adjust"])
    verdict = body["verdicts"][uid]
    assert verdict["status"] == "unjudged" and verdict["reason"] == "max_cost_usd" and verdict["post_seal"] is True
    assert models.judges == [] and body["calls"] == 0 and body["budget"]["max_cost_usd"] == 1e-9
    assert body["event"] is None and await judged_events(row.id) == [], "没问模型就没有判定可记"


async def test_the_daily_limit_applies_to_clicks_too(client, monkeypatch):
    models = Models(monkeypatch)
    today = (await daily_spend())["date"]
    await save_setting(SPEND_KEY, {"date": today, "usd": 2.0, "calls": 9, "unpriced_calls": 0})
    row = await finish(weekly(claims="judge"))
    body = (await judge(client, row.id, [unit_ids(doc_of(row))[CAUSAL]])).json()
    assert body["limits_hit"] == ["daily_max_usd"] and "每日" in body["message"] and models.judges == []
    assert body["spend"]["usd"] == 2.0 and body["spend"]["daily_max_usd"] == 2.0


async def test_unlimited_click_budget_judges(client, monkeypatch):
    models = Models(monkeypatch)
    await save_setting("judge", {"click_max_cost_usd": None, "daily_max_usd": None})
    row = await finish(weekly(claims="judge"))
    body = (await judge(client, row.id, [unit_ids(doc_of(row))[CAUSAL]])).json()
    assert body["limited"] is False and len(models.judges) == 1
    assert body["budget"]["max_cost_usd"] is None and body["budget"]["daily_max_usd"] is None
    assert body["spend"]["calls"] == 1 and body["spend"]["daily_max_usd"] is None


async def test_the_sentence_limit_keeps_what_was_judged(client, monkeypatch):
    models = Models(monkeypatch)
    row = await finish(weekly(claims="judge", judge={"max_claims": 1}))
    ids = unit_ids(doc_of(row))
    body = (await judge(client, row.id, [ids[CITED], ids[CAUSAL]])).json()
    statuses = {u: v["status"] for u, v in body["verdicts"].items()}
    assert statuses == {ids[CITED]: "supported", ids[CAUSAL]: "unjudged"} and len(models.judges) == 1
    assert body["limits_hit"] == ["max_claims"] and body["verdicts"][ids[CAUSAL]]["reason"] == "max_claims"
    [event] = await judged_events(row.id)
    assert set(event.data["verdicts"]) == {ids[CITED], ids[CAUSAL]}, "问过模型的这一次连同没判的一起记下"
    # 没判的那句还能再点
    again = (await judge(client, row.id, [ids[CAUSAL]])).json()
    assert again["judged"] == [ids[CAUSAL]] and again["reused"] == []


# --------------------------------------------------------------------------
# 用哪个模型
# --------------------------------------------------------------------------


async def test_the_judge_model_follows_node_then_settings(client, monkeypatch):
    models = Models(monkeypatch)
    await save_setting("judge", {"model": "judge-from-settings"})
    row = await finish(weekly(claims="judge"))
    await judge(client, row.id, [unit_ids(doc_of(row))[CAUSAL]])
    [spec] = models.specs
    assert spec.model == "judge-from-settings" and spec.thinking == "off" and spec.max_tokens == 4096

    row = await finish(weekly(claims="judge", judge={"model": "judge-from-node"}))
    body = (await judge(client, row.id, [unit_ids(doc_of(row))[CAUSAL]])).json()
    assert models.specs[-1].model == "judge-from-node" and body["model"] == "judge-from-node"


# --------------------------------------------------------------------------
# 什么时候不判
# --------------------------------------------------------------------------


async def test_formal_runs_are_judged_inside_the_node_not_on_demand(client, monkeypatch):
    models = Models(monkeypatch)
    row = await finish(weekly(claims="judge", judge={"max_cost_usd": 0.05}), run_class="formal")
    assert row.status == "succeeded", row.error
    calls = len(models.judges)
    res = await judge(client, row.id, [unit_ids(doc_of(row))[CAUSAL]])
    assert res.status_code == 409 and res.json()["code"] == "evidence_judge_formal"
    assert "正式运行的裁判在节点内完成" in res.json()["detail"]
    assert len(models.judges) == calls and await judged_events(row.id) == []


async def test_a_run_paused_at_an_approval_is_not_judged(client, monkeypatch):
    models = Models(monkeypatch)
    row = await finish(weekly(claims="judge", gate=True), statuses=("interrupted",))
    res = await judge(client, row.id, ["u1"])
    assert res.status_code == 409 and res.json()["code"] == "evidence_judge_unsealed"
    assert models.judges == [] and await judged_events(row.id) == []


async def test_runs_from_before_the_upgrade_are_not_judged(client, monkeypatch):
    async def no_snapshot(session):
        return None

    monkeypatch.setattr(runner_mod, "_agent_limits", no_snapshot)
    models = Models(monkeypatch)
    row = await finish(weekly())
    res = await judge(client, row.id, [unit_ids(doc_of(row))[CAUSAL]])
    assert res.status_code == 409 and res.json()["code"] == "evidence_judge_legacy"
    assert models.judges == [] and await judged_events(row.id) == []


async def test_a_broken_seal_is_not_judged(client, monkeypatch):
    models = Models(monkeypatch)
    row = await finish(weekly(claims="judge"))
    async with SessionLocal() as session:
        event = (await session.execute(select(RunEvent).where(
            RunEvent.run_id == row.id, RunEvent.type == "node.finished", RunEvent.node_id == "fetch"))).scalar_one()
        event.data = {**event.data, "tampered": True}
        await session.commit()
    res = await judge(client, row.id, [unit_ids(doc_of(row))[CAUSAL]])
    assert res.status_code == 409 and res.json()["code"] == "evidence_seal_broken" and models.judges == []


@pytest.mark.parametrize("meanwhile", [
    # 裁判的这几秒里有人点了「继续运行」（失败的运行可以接着跑）：运行又活了
    {"status": "running", "error": None},
    # 接着跑完、重新封存了：manifest_seq 和清单哈希都换了，这次点击依据的那份封存已经不是最新的
    {"manifest_seq": 10_000, "manifest_hash": "0" * 64},
])
async def test_a_run_continued_during_the_judge_call_records_no_event(client, monkeypatch, meanwhile):
    """判定只接在点击开始时的那份封存后面。运行在裁判期间被接着跑了，再追加就落进一次活着的运行中间，
    还会被封进下一份清单：不记进运行记录，判定照样交回给点的人，并说明为什么没记。"""
    models = Models(monkeypatch, verdicts={"新客": "unsupported"})
    row = await finish(weekly(claims="judge"))
    uid = unit_ids(doc_of(row))[CAUSAL]
    ask_model = judge_mod.get_chat_model

    async def continued_while_judging(session, spec):
        async with SessionLocal() as other:
            await other.execute(update(Run).where(Run.id == row.id).values(**meanwhile))
            await other.commit()
        return await ask_model(session, spec)

    monkeypatch.setattr(judge_mod, "get_chat_model", continued_while_judging)
    res = await judge(client, row.id, [uid])
    assert res.status_code == 200, res.text
    body = res.json()
    assert len(models.judges) == 1 and body["calls"] == 1, "模型问过了"
    assert body["verdicts"][uid]["status"] == "unsupported" and body["judged"] == [uid], "判定照样交回"
    assert body["event"] is None and "接着跑" in body["message"] and "没有记进运行记录" in body["message"]
    assert await judged_events(row.id) == []
    async with SessionLocal() as session:
        after = await session.get(Run, row.id)
    assert after.last_seq == row.last_seq, "运行记录一条都没多"


async def test_a_continue_that_wins_the_row_between_check_and_write_gets_no_event(client, monkeypatch):
    """核对过状态、还没写入的那一下，接着跑的条件更新（runner._claim）先提交了：追加判定的条件更新落空，
    照样不记——核对和写入是同一行上的条件更新，不是先读后写。"""
    from app.core.bus import bus
    from app.core.config import settings

    Models(monkeypatch, verdicts={"新客": "unsupported"})
    row = await finish(weekly(claims="judge"))
    uid = unit_ids(doc_of(row))[CAUSAL]
    set_seq = bus.set_seq

    def claimed_in_between(run_id, value):
        # _append_judged 读完运行、分配序号之前正好调用它：在这里用另一条连接把运行占成 running
        if run_id == row.id:
            db = sqlite3.connect(settings.db_path, timeout=5)
            try:
                db.execute("UPDATE runs SET status = 'running' WHERE id = ?", (row.id,))
                db.commit()
            finally:
                db.close()
        set_seq(run_id, value)

    monkeypatch.setattr(bus, "set_seq", claimed_in_between)
    body = (await judge(client, row.id, [uid])).json()
    assert body["verdicts"][uid]["status"] == "unsupported" and body["event"] is None
    assert "没有记进运行记录" in body["message"] and await judged_events(row.id) == []
    async with SessionLocal() as session:
        after = await session.get(Run, row.id)
    assert after.status == "running" and after.last_seq == row.last_seq


async def test_missing_runs_and_reports(client, monkeypatch):
    Models(monkeypatch)
    res = await judge(client, "nope", ["u1"])
    assert res.status_code == 404 and res.json()["code"] == "run_not_found"
    row = await finish(chain(node("start", "input"), node("out", "output", fields=[{"name": "a", "value": "x"}])))
    res = await judge(client, row.id, ["u1"])
    assert res.status_code == 404 and res.json()["code"] == "evidence_report_not_found"


@pytest.mark.parametrize("payload", [{}, {"units": []}, {"units": "u1"}, {"units": [1]}, {"units": [""]},
                                     {"units": [f"u{i}" for i in range(201)]}, {"units": ["u1"], "report": 3}])
async def test_bad_requests_are_422(client, monkeypatch, payload):
    Models(monkeypatch)
    row = await finish(weekly())
    res = await client.post(f"/api/runs/{row.id}/evidence/judge", json=payload)
    assert res.status_code == 422 and res.json()["code"] == "evidence_judge_bad_units"


async def test_sentences_that_are_not_claims_are_skipped(client, monkeypatch):
    models = Models(monkeypatch)
    row = await finish(weekly(claims="judge"))
    ids = unit_ids(doc_of(row))
    body = (await judge(client, row.id, [ids["本周"], ids[LINK], "u999"])).json()
    assert body["skipped"] == {ids["本周"]: "not_a_claim", ids[LINK]: "not_a_claim", "u999": "not_found"}
    assert body["verdicts"] == {} and models.judges == [] and body["event"] is None
    assert "不是结论句" in body["message"]


# --------------------------------------------------------------------------
# 片段接口、证据图带上判定
# --------------------------------------------------------------------------


async def test_the_segment_shows_the_verdict_and_whether_it_can_still_be_asked(client, monkeypatch):
    Models(monkeypatch, verdicts={"新客": "unsupported"})
    row = await finish(weekly(claims="judge"))
    doc = doc_of(row)
    ids = unit_ids(doc)
    before = (await client.get(f"/api/runs/{row.id}/evidence/segments/{seg_in(doc, ids[CAUSAL])}")).json()
    assert before["unit"]["verdict"]["status"] == "unjudged" and before["unit"]["verdict"]["reason"] == "on_demand"
    assert before["unit"]["on_demand"] == {"available": True, "reason": None, "message": None}

    await judge(client, row.id, [ids[CAUSAL]])
    after = (await client.get(f"/api/runs/{row.id}/evidence/segments/{seg_in(doc, ids[CAUSAL])}")).json()
    assert after["unit"]["verdict"]["status"] == "unsupported" and after["unit"]["verdict"]["post_seal"] is True
    assert after["unit"]["on_demand"]["available"] is False and after["unit"]["on_demand"]["reason"] == "judged"
    assert "模型" in after["note"] and "后续版本" not in after["note"]
    other = (await client.get(f"/api/runs/{row.id}/evidence/segments/{seg_in(doc, ids[CITED])}")).json()
    assert other["unit"]["on_demand"]["available"] is True
    assert "挂了依据" in other["note"] and "模型" in other["note"] and "后续版本" not in other["note"]
    link = (await client.get(f"/api/runs/{row.id}/evidence/segments/{seg_in(doc, ids[LINK])}")).json()
    assert link["unit"]["on_demand"]["reason"] == "not_a_claim" and "verdict" not in link["unit"]

    graph = (await client.get(f"/api/runs/{row.id}/evidence")).json()
    [report] = graph["reports"]
    assert report["claims"] == "judge" and report["judge"]["mode"] == "on_demand"
    assert report["post_seal_verdicts"] == {ids[CAUSAL]: after["unit"]["verdict"]}
    audit = (await client.get(f"/api/runs/{row.id}/evidence/audit")).json()
    assert audit["reports"][0]["post_seal_verdicts"] == report["post_seal_verdicts"]


async def test_sealed_verdicts_of_a_formal_run_are_shown_as_sealed(client, monkeypatch):
    Models(monkeypatch, verdicts={"新客": "unsupported"})
    row = await finish(weekly(claims="judge", judge={"max_cost_usd": 0.05}), run_class="formal")
    doc = doc_of(row)
    body = (await client.get(f"/api/runs/{row.id}/evidence/segments/{seg_in(doc, unit_ids(doc)[CAUSAL])}")).json()
    assert body["unit"]["verdict"]["status"] == "unsupported" and body["unit"]["verdict"]["post_seal"] is False
    assert body["unit"]["on_demand"]["reason"] == "formal"
    graph = (await client.get(f"/api/runs/{row.id}/evidence")).json()
    assert graph["reports"][0]["post_seal_verdicts"] == {} and graph["reports"][0]["judge"]["mode"] == "inline"


async def test_a_forged_event_cannot_overwrite_a_sealed_verdict(client, monkeypatch):
    """封存后追加的判定只填补没判过的句子：封存内判过的，后面追加什么都盖不掉。"""
    Models(monkeypatch, verdicts={"新客": "unsupported"})
    row = await finish(weekly(claims="judge", judge={"max_cost_usd": 0.05}), run_class="formal")
    doc = doc_of(row)
    uid = unit_ids(doc)[CAUSAL]
    doc_artifact = row.output["_evidence"]["doc_artifact"]
    await run_manager.note(row.id, EventType.EVIDENCE_JUDGED, node_id="write", report="write",
                           doc_artifact=doc_artifact, post_seal=True,
                           verdicts={uid: {"status": "supported", "rationale": "改好了", "judge": "x", "post_seal": True}})
    body = (await client.get(f"/api/runs/{row.id}/evidence/segments/{seg_in(doc, uid)}")).json()
    assert body["unit"]["verdict"]["status"] == "unsupported"


async def test_appended_verdicts_always_read_as_post_seal(client, monkeypatch):
    """追加的判定不管载荷里怎么写，展示出来都是「封存后追加」：只有随文档封存的判定才算封存内的。"""
    Models(monkeypatch)
    row = await finish(weekly(claims="judge"))
    doc = doc_of(row)
    uid = unit_ids(doc)[CAUSAL]
    await run_manager.note(row.id, EventType.EVIDENCE_JUDGED, node_id="write", report="write",
                           doc_artifact=row.output["_evidence"]["doc_artifact"],
                           verdicts={uid: {"status": "supported", "rationale": "x", "judge": "x", "post_seal": False},
                                     "u999": {"status": "supported", "rationale": "x", "judge": "x"}})
    body = (await client.get(f"/api/runs/{row.id}/evidence/segments/{seg_in(doc, uid)}")).json()
    assert body["unit"]["verdict"]["status"] == "supported" and body["unit"]["verdict"]["post_seal"] is True
    graph = (await client.get(f"/api/runs/{row.id}/evidence")).json()
    assert set(graph["reports"][0]["post_seal_verdicts"]) == {uid}, "文档里没有的句子不认"


# --------------------------------------------------------------------------
# 裁判只看得到封存范围内的证据
# --------------------------------------------------------------------------

SOURCE = "warehouse"
TOOL = f"db_query__{SOURCE}"
SQL = "SELECT region, SUM(amount) AS gmv, COUNT(*) AS order_cnt FROM orders GROUP BY region ORDER BY gmv DESC"
EAST, WEST = "东区销售额 [[v:Q1.r0.gmv]]。[[see:Q1]]", "西区销售额 [[v:Q1.r1.gmv]]。[[see:Q1]]"


@pytest.fixture
async def warehouse(tmp_path):
    from app.data.introspect import introspect

    path = tmp_path / "warehouse.db"
    db = sqlite3.connect(path)
    db.executescript("CREATE TABLE orders (id INTEGER PRIMARY KEY, region TEXT, amount REAL);")
    db.executemany("INSERT INTO orders VALUES (?, ?, ?)", [(1, "east", 100.5), (2, "west", 200.0), (3, "east", 300.0)])
    db.commit()
    db.close()
    probe = SimpleNamespace(id=f"probe-{path.name}", name=SOURCE, kind="sqlite", database=str(path), host=None,
                            port=None, username=None, password=None, options={}, readonly=True, schema_cache={})
    cache = await introspect(probe)
    await engines.invalidate(probe.id)
    async with SessionLocal() as session:
        row = (await session.execute(select(DataSource).where(DataSource.name == SOURCE))).scalar_one_or_none()
        if row is None:
            row = DataSource(name=SOURCE, kind="sqlite", database=str(path), readonly=True, enabled=True)
            session.add(row)
        row.database, row.kind, row.enabled, row.readonly, row.schema_cache = str(path), "sqlite", True, True, cache
        row.options = {}
        await session.commit()
        source_id = row.id
    await engines.invalidate(source_id)
    yield source_id
    await engines.invalidate(source_id)


def queried() -> dict:
    return chain(node("start", "input"), node("fetch", "tool", tool=TOOL, args={"sql": SQL}),
                 node("write", "report", instructions="写周报"),
                 node("out", "output", fields=[{"name": "周报", "value": "{{ nodes.write.text }}"}]))


async def reseal(run_id: str) -> None:
    from app.core.artifact_store import manifest_hash

    async with SessionLocal() as session:
        run = await session.get(Run, run_id)
        rows = [tuple(r) for r in await session.execute(
            select(RunEvent.seq, RunEvent.type, RunEvent.node_id, RunEvent.data)
            .where(RunEvent.run_id == run_id, RunEvent.seq <= run.manifest_seq).order_by(RunEvent.seq))]
        run.manifest_hash = manifest_hash(rows)
        await session.commit()


async def unseal_queries(run_id: str) -> str:
    """把查询快照从封存的事件里摘掉、按改过的事件重新封存：快照还在工件库和工件表里，封存链完好，
    只是没有哪条封存的事件引用它（等于「人为插进工件表」）。返回那件快照的 id。"""
    async with SessionLocal() as session:
        rows = list((await session.execute(select(RunEvent).where(
            RunEvent.run_id == run_id, RunEvent.type.in_(["tool.end", "node.finished"])))).scalars())
        artifact = None
        for event in rows:
            data = dict(event.data or {})
            if event.type == "tool.end":
                artifact = data.pop("query_artifact", None) or artifact
                data.pop("schema_artifact", None)
            elif data.get("evidence"):
                data["evidence"] = [e for e in data["evidence"] if e.get("kind") not in ("query", "schema")]
            event.data = data
        await session.commit()
    await reseal(run_id)
    return artifact


def excerpt_lines(request: str) -> tuple[str | None, list[str]]:
    header = re.search(r"^列：(.*)$", request, re.M)
    return (header.group(1) if header else None), re.findall(r"^r\d+：.*$", request, re.M)


async def test_only_sealed_snapshots_reach_the_judge(client, monkeypatch, warehouse):
    models = Models(monkeypatch, text=EAST + WEST)
    control = await finish(queried())
    assert control.status == "succeeded", control.error
    await judge(client, control.id, [unit_starting(doc_of(control), "东区")])
    assert "【Q1】查询结果" in models.judges[0] and "SQL：SELECT region" in models.judges[0]
    assert excerpt_lines(models.judges[0])[1], "封存链完好时照常摘被引用的行"

    row = await finish(queried())
    doc = doc_of(row)
    artifact = await unseal_queries(row.id)
    assert artifact and artifact_store.load(artifact) is not None, "快照本身还在工件库里"
    assert (await verify_manifest(row.id))["ok"] is True
    res = await judge(client, row.id, [unit_starting(doc, "东区")])
    assert res.status_code == 200, res.text
    assert "【Q1】" not in models.judges[1] and "SELECT" not in models.judges[1]
    assert excerpt_lines(models.judges[1]) == (None, [])

    # 封存之后才追加的事件引用了它，也不算
    async with SessionLocal() as session:
        run = await session.get(Run, row.id)
        seq = run.last_seq
    from app.core.bus import bus

    bus.set_seq(row.id, seq)
    await run_manager.note(row.id, "tool.end", node_id="fetch", tool=TOOL, query_artifact=artifact)
    await judge(client, row.id, [unit_starting(doc, "西区")])
    assert "【Q1】" not in models.judges[2] and "SELECT" not in models.judges[2]


async def test_masked_columns_never_reach_the_judge(client, monkeypatch, warehouse):
    models = Models(monkeypatch, text=EAST)
    row = await finish(queried())
    async with SessionLocal() as session:
        source = await session.get(DataSource, warehouse)
        source.options = {"mask_columns": ["order_cnt"]}
        await session.commit()
    await judge(client, row.id, [unit_starting(doc_of(row), "东区")])
    header, lines = excerpt_lines(models.judges[0])
    assert header is not None and "order_cnt" not in header and "gmv" in header
    assert lines and all(line.count("|") == 1 for line in lines), lines
    assert "遮罩" in models.judges[0]
