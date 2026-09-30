"""报告撰写节点的 claims: judge：结论句由另一个模型按证据逐句判断（四期）。

- 正式运行在节点里裁判：写完、核对完之后判，判定写进报告文档的 unit.verdict，随文档一起封存
- 探索运行不在节点里花这笔钱：候选句标「未裁判 · 按需」，点开哪句再判哪句（接口在 B 段）
- 判定只标注、不改写正文；裁判没跑成、触顶了，照样产出报告，没判的记未裁判并留下缺口
  （出口据此不能判完整出具，接线在 B 段）
- rewrite_once（默认关）：把证据不支持的句子和理由交回写作者只改这几句，再判一次，最多一轮；
  改出了新的违规就不采用
- 裁判调用包在 @task 里、追加在节点已有的调用之后：节点「接着跑」时不重复调用、不重复计费；
  升级前发起的运行一样都不加，停在审批上的运行恢复后照常
"""
from __future__ import annotations

import asyncio
import json
import re

import pytest
from langchain_core.messages import AIMessage
from sqlalchemy import delete, select

from app.core import artifact_store
from app.db.base import SessionLocal
from app.db.models import Run, RunEvent, Setting
from app.engine import judge as judge_mod
from app.engine import replay
from app.engine import runner as runner_mod
from app.engine.evidence import CLAIMS_RULE, iter_units
from app.engine.judge import COUNT_KEYS, JUDGE_ROLE, JUDGE_RULE, SPEND_KEY, SUMMARY_KEYS, daily_spend
from app.engine.nodes import report as report_mod
from app.engine.runner import run_manager, verify_manifest
from app.providers.mock_model import MockChatModel

PRICED = "claude-sonnet-5"
METRICS = [{"id": "gmv", "name": "销售额", "unit": "元", "decimals": 1, "expression": "vars.kpi.gmv"}]
#: 一句挂了依据的结论、一句没挂依据的归因、一句连接性的话
TEXT = "本周销售额 [[m:gmv]]。[[see:m:gmv]]增长主要来自新客首单。下面看细节。"
CITED, CAUSAL, LINK = "本周销售额 45,678.5元。", "增长主要来自新客首单。", "下面看细节。"


@pytest.fixture(autouse=True)
async def engine_up():
    await run_manager.setup()
    async with SessionLocal() as session:
        await session.execute(delete(Setting).where(Setting.key.in_([SPEND_KEY, "judge", "copilot"])))
        await session.commit()
    yield
    await run_manager.shutdown()


def node(nid, ntype, **config):
    return {"id": nid, "type": ntype, "position": {"x": 0, "y": 0}, "data": {"label": nid, "config": config}}


def weekly(*, gate=False, **report):
    nodes = [
        node("start", "input", fields=[{"name": "week", "default": "2026-W37"}]),
        node("fetch", "transform", mode="expression", expression="{'gmv': 45678.5}", assign_to="kpi"),
        node("caliber", "metrics", caliber="周报口径", caliber_version="v2", metrics=METRICS),
        *([node("gate", "human", mode="approve", title="确认口径")] if gate else []),
        node("write", "report", instructions="写周报", **report),
        node("out", "output", fields=[{"name": "周报", "value": "{{ nodes.write.text }}"}]),
    ]
    ids = [n["id"] for n in nodes]
    return {"nodes": nodes, "edges": [{"source": a, "target": b} for a, b in zip(ids, ids[1:])]}


class Models:
    """写作者和裁判两份剧本。写作者依次回 writer 里的几段（用完了一直回最后一段）；裁判按句子里的字给判定
    （verdicts：{句中片段: 判定}，没对上的都是 supported）。"""

    def __init__(self, monkeypatch, writer=(TEXT,), verdicts=None, *, judge_model=PRICED, fail_judge=None):
        self.writes: list[list] = []
        self.judges: list[str] = []
        verdicts = verdicts or {}

        def decide(model, messages):
            if JUDGE_ROLE in str(messages[0].content):
                request = str(messages[-1].content)
                self.judges.append(request)
                if fail_judge:
                    raise RuntimeError(fail_judge)
                items = []
                for uid, line in re.findall(r"^\[(u\d+)\]([^\n]*)$", request, re.M):
                    status = next((s for frag, s in verdicts.items() if frag in line), "supported")
                    items.append({"unit": uid, "verdict": status, "rationale": f"判的是：{line[:20]}", "used": []})
                return AIMessage(content=json.dumps({"items": items}, ensure_ascii=False))
            self.writes.append(list(messages))
            return AIMessage(content=writer[min(len(self.writes), len(writer)) - 1])

        monkeypatch.setattr(MockChatModel, "_decide", decide)

        async def judge_model_of(session, spec):
            return MockChatModel(model_name=judge_model), judge_model

        monkeypatch.setattr(judge_mod, "get_chat_model", judge_model_of)


def spy_tasks(monkeypatch) -> list[str]:
    """报告节点里每调用一次 @task 记一笔名字：证明加进来的 task 只在该出现的地方出现。"""
    from langgraph.func import task as real

    calls: list[str] = []

    def spying(func=None, **kwargs):
        def wrap(fn):
            inner = real(fn, **kwargs)

            def call(*args, **kw):
                calls.append(fn.__name__)
                return inner(*args, **kw)

            return call
        return wrap(func) if func is not None else wrap

    monkeypatch.setattr(report_mod, "task", spying)
    return calls


async def finish(graph, *, run_class="formal", statuses=("succeeded", "failed")) -> Run:
    run = await run_manager.start(graph=graph, input_payload={}, run_class=run_class)
    return await wait(run.id, statuses)


async def wait(run_id, statuses=("succeeded", "failed")) -> Run:
    for _ in range(400):
        await asyncio.sleep(0.05)
        async with SessionLocal() as session:
            row = await session.get(Run, run_id)
        if row.status in statuses and (row.status == "interrupted" or row.manifest_seq is not None):
            return row
    raise AssertionError(f"run {run_id} 没跑完：{row.status}")


async def events(run_id: str, etype: str, node_id: str | None = "write") -> list[RunEvent]:
    async with SessionLocal() as session:
        q = select(RunEvent).where(RunEvent.run_id == run_id, RunEvent.type == etype)
        if node_id:
            q = q.where(RunEvent.node_id == node_id)
        return list((await session.execute(q.order_by(RunEvent.seq))).scalars())


async def report_of(run_id: str) -> tuple[dict, dict, dict]:
    """(report.checked 的载荷, 报告文档, 节点产出)"""
    [checked] = await events(run_id, "report.checked")
    [finished] = await events(run_id, "node.finished")
    return checked.data, artifact_store.load(checked.data["doc_artifact"]), artifact_store.load(finished.data["artifact"])


def verdicts(doc) -> dict[str, dict | None]:
    md = doc["markdown"]
    return {md[u["span"][0]:u["span"][1]].strip(): u.get("verdict") for _, u in iter_units(doc)}


def unit_ids(doc) -> dict[str, str]:
    md = doc["markdown"]
    return {md[u["span"][0]:u["span"][1]].strip(): u["id"] for _, u in iter_units(doc)}


async def judge_ends(run_id: str) -> list[dict]:
    return [e.data for e in await events(run_id, "llm.end") if e.data.get("purpose") == "judge"]


# --------------------------------------------------------------------------
# 正式运行：节点内裁判，判定随文档封存
# --------------------------------------------------------------------------


async def test_a_formal_run_judges_inside_the_node_and_seals_the_verdicts(monkeypatch):
    models = Models(monkeypatch, verdicts={"新客": "contradicted"})
    row = await finish(weekly(claims="judge", judge={"on_unsupported": "withhold"}))
    assert row.status == "succeeded", row.error
    assert len(models.writes) == 1 and len(models.judges) == 1

    checked, doc, output = await report_of(row.id)
    got = verdicts(doc)
    assert got[CITED]["status"] == "supported" and got[CITED]["judge"] == PRICED
    assert got[CITED]["post_seal"] is False and got[CITED]["rationale"]
    assert got[CAUSAL]["status"] == "contradicted"
    assert got[LINK] is None, "连接性的话不送裁判"
    assert CITED in models.judges[0] and CAUSAL in models.judges[0] and LINK not in models.judges[0]

    summary = checked["judge"]
    assert tuple(summary) == SUMMARY_KEYS, "B 段照着这份键读：不能悄悄少一个、多一个"
    assert summary["mode"] == "inline" and summary["model"] == PRICED and summary["on_unsupported"] == "withhold"
    assert summary["counts"] == {"supported": 1, "partial": 0, "contradicted": 1, "insufficient": 0, "not_a_claim": 0,
                                 "unjudged": 0, "unsupported": 0}
    assert summary["complete"] is True and summary["gaps"] == []
    assert checked["claims"] == output["claims"] == "judge" and output["judge"] == summary
    assert doc["judge"]["mode"] == "inline" and doc["judge"]["counts"] == summary["counts"]
    assert {k: doc["stats"][k] for k in COUNT_KEYS} \
        == summary["counts"]

    [end] = await judge_ends(row.id)
    assert end["model"] == PRICED and end["cost_usd"] >= 0
    verdict = await verify_manifest(row.id)
    assert verdict["ok"] is True
    [event] = await events(row.id, "report.checked")
    assert event.seq <= row.manifest_seq, "判定在封存范围内"


async def test_the_writer_is_told_that_claims_will_be_judged(monkeypatch):
    models = Models(monkeypatch)
    await finish(weekly(claims="judge"))
    prompt = "\n".join(str(m.content) for m in models.writes[0])
    assert CLAIMS_RULE in prompt and JUDGE_RULE in prompt


async def test_explore_runs_leave_it_to_the_reader(monkeypatch):
    """探索运行默认按需裁判：节点里不调用，候选句标「未裁判 · 按需」，连接性的话什么都不标。"""
    models = Models(monkeypatch)
    row = await finish(weekly(claims="judge"), run_class="exploratory")
    assert row.status == "succeeded", row.error
    assert models.judges == []
    checked, doc, output = await report_of(row.id)
    got = verdicts(doc)
    for text in (CITED, CAUSAL):
        assert got[text]["status"] == "unjudged" and got[text]["reason"] == "on_demand"
        assert got[text]["judge"] is None and got[text]["post_seal"] is False
    assert got[LINK] is None
    assert checked["judge"]["mode"] == "on_demand" and checked["judge"]["gaps"] == []
    assert tuple(checked["judge"]) == SUMMARY_KEYS and checked["judge"]["limits"] is None
    assert output["judge"] == checked["judge"] and await judge_ends(row.id) == []


async def test_a_failed_judge_leaves_a_gap_not_a_failed_report(monkeypatch):
    Models(monkeypatch, fail_judge="网关超时")
    row = await finish(weekly(claims="judge"))
    assert row.status == "succeeded", row.error
    checked, doc, output = await report_of(row.id)
    assert {verdicts(doc)[t]["reason"] for t in (CITED, CAUSAL)} == {"error"}
    summary = checked["judge"]
    assert summary["complete"] is False and summary["counts"]["unjudged"] == 2
    assert summary["gaps"] and "网关超时" in summary["gaps"][0]
    assert output["judge"]["gaps"] == summary["gaps"]
    [end] = await judge_ends(row.id)
    assert "网关超时" in end["error"]


async def test_hitting_a_limit_is_reported_not_raised(monkeypatch):
    models = Models(monkeypatch)
    row = await finish(weekly(claims="judge", judge={"max_claims": 1}))
    assert row.status == "succeeded", row.error
    checked, doc, _ = await report_of(row.id)
    assert verdicts(doc)[CITED]["status"] == "supported", "先判挂了依据的"
    assert (verdicts(doc)[CAUSAL]["status"], verdicts(doc)[CAUSAL]["reason"]) == ("unjudged", "max_claims")
    assert checked["judge"]["limits_hit"] == ["max_claims"] and "已达上限" in checked["judge"]["gaps"][0]
    assert CAUSAL not in models.judges[0]


async def test_the_node_budget_overrides_the_settings_and_null_means_unlimited(monkeypatch):
    """节点 judge 里写了的键盖过设置；写 null 就是不限（不是「没写」）。"""
    async with SessionLocal() as session:
        session.add(Setting(key="judge", value={"report_max_claims": 1, "report_max_cost_usd": 1e-9}))
        await session.commit()
    Models(monkeypatch)
    row = await finish(weekly(claims="judge", judge={"max_cost_usd": None}))
    checked, doc, _ = await report_of(row.id)
    assert verdicts(doc)[CITED]["status"] == "supported"
    assert verdicts(doc)[CAUSAL]["reason"] == "max_claims", "max_claims 没写，取设置里的 1"
    assert checked["judge"]["limits"]["max_cost_usd"] is None and checked["judge"]["limits"]["max_claims"] == 1


async def test_judging_yourself_is_called_out(monkeypatch):
    Models(monkeypatch, judge_model="mock-fast")
    row = await finish(weekly(claims="judge"))
    assert row.status == "succeeded", row.error
    [warn] = [e.data for e in await events(row.id, "log") if e.data.get("code") == "judge_same_model"]
    assert "mock-fast" in warn["message"] and "难以发现写作模型自身的错误" in warn["message"]


# --------------------------------------------------------------------------
# rewrite_once
# --------------------------------------------------------------------------

REWRITTEN = "本周销售额 [[m:gmv]]。[[see:m:gmv]]新客首单的贡献要等新客维度的数据再看。下面看细节。"


async def test_rewrite_once_hands_back_only_the_unsupported_sentences(monkeypatch):
    models = Models(monkeypatch, writer=(TEXT, REWRITTEN),
                    verdicts={"主要来自新客": "contradicted", "要等新客维度": "not_a_claim"})
    row = await finish(weekly(claims="judge", judge={"rewrite_once": True}))
    assert row.status == "succeeded", row.error
    assert len(models.writes) == 2 and len(models.judges) == 2

    request = str(models.writes[1][-1].content)
    assert CAUSAL in request and "判的是：" in request and CITED not in request, "只交回不支持的那几句和理由"
    # 第二次只判改过的句子：没改的那句直接用第一次的判定
    assert "要等新客维度" in models.judges[1] and CITED not in models.judges[1]

    checked, doc, output = await report_of(row.id)
    assert "新客首单的贡献要等新客维度的数据再看。" in doc["markdown"] and CAUSAL not in doc["markdown"]
    got = verdicts(doc)
    assert got[CITED]["status"] == "supported" and got["新客首单的贡献要等新客维度的数据再看。"]["status"] == "not_a_claim"
    rewrite = checked["judge"]["rewrite"]
    assert rewrite["applied"] is True and rewrite["sentences"] == [CAUSAL] and checked["judge"]["reused"] == 1
    # 封存的是改写稿：units 按句子认封存文档里的编号，交回去的那句已经改掉了，对不上就是 null——
    # 不能拿原稿的编号去指改写稿里碰巧同号的另一句；改写稿里新出来的句子在 changed 里
    ids = unit_ids(doc)
    assert rewrite["units"] == [None]
    assert rewrite["changed"] == [ids["新客首单的贡献要等新客维度的数据再看。"]]
    assert output["text"] == doc["markdown"]
    [log] = [e.data for e in await events(row.id, "log") if e.data.get("code") == "report_rewrite"]
    assert "1 句" in log["message"]
    assert len(await judge_ends(row.id)) == 2


async def test_rewrite_once_is_off_by_default(monkeypatch):
    models = Models(monkeypatch, writer=(TEXT, REWRITTEN), verdicts={"主要来自新客": "contradicted"})
    row = await finish(weekly(claims="judge"))
    assert row.status == "succeeded", row.error
    assert len(models.writes) == 1 and len(models.judges) == 1
    checked, doc, _ = await report_of(row.id)
    assert verdicts(doc)[CAUSAL]["status"] == "contradicted" and checked["judge"].get("rewrite") is None


async def test_a_rewrite_that_breaks_the_numbers_is_not_used(monkeypatch):
    models = Models(monkeypatch, writer=(TEXT, "本周销售额 [[m:gmv]]。[[see:m:gmv]]新客贡献了 45678 元。下面看细节。"),
                    verdicts={"主要来自新客": "contradicted"})
    row = await finish(weekly(claims="judge", judge={"rewrite_once": True}))
    assert row.status == "succeeded", row.error
    assert len(models.writes) == 2 and len(models.judges) == 1, "改坏了的那一稿不再花钱判"
    checked, doc, output = await report_of(row.id)
    assert CAUSAL in doc["markdown"] and "45678" not in doc["markdown"]
    assert verdicts(doc)[CAUSAL]["status"] == "contradicted"
    rewrite = checked["judge"]["rewrite"]
    assert rewrite["applied"] is False and "45678" in rewrite["reason"]
    # 封存的是原稿：units 就是原稿（也就是封存文档）里的编号
    assert rewrite["units"] == [unit_ids(doc)[CAUSAL]] and rewrite["changed"] == []


def with_schema(monkeypatch):
    """报告的目录里加一张表：实体层有了核对的底，反引号里的名字才会被认出是编造的。"""
    from app.engine.evidence import _entity_entries

    real = report_mod.report_catalog

    def catalog_with_schema(state, spec, node):
        return {**real(state, spec, node), **_entity_entries(
            [{"artifact": "d" * 64, "source": "shop", "tables": {"orders": ["order_id", "amount"]}}], [])}

    monkeypatch.setattr(report_mod, "report_catalog", catalog_with_schema)


async def test_a_rewrite_that_makes_up_a_name_is_not_used(monkeypatch):
    """改写稿冒出一个编造的表名（只标注、不拦的那类违规）：同样是原稿没有的新违规，不采用。"""
    with_schema(monkeypatch)
    models = Models(monkeypatch, writer=(TEXT, "本周销售额 [[m:gmv]]。[[see:m:gmv]]新客首单的贡献要看 `refund_detail` 表。"
                                               "下面看细节。"),
                    verdicts={"主要来自新客": "contradicted"})
    row = await finish(weekly(claims="judge", judge={"rewrite_once": True}))
    assert row.status == "succeeded", row.error
    assert len(models.writes) == 2 and len(models.judges) == 1
    checked, doc, _ = await report_of(row.id)
    assert CAUSAL in doc["markdown"] and "refund_detail" not in doc["markdown"]
    rewrite = checked["judge"]["rewrite"]
    assert rewrite["applied"] is False and "refund_detail" in rewrite["reason"]
    [log] = [e.data for e in await events(row.id, "log") if e.data.get("code") == "report_rewrite_rejected"]
    assert "refund_detail" in log["message"]


async def test_a_rewrite_that_trades_one_violation_for_another_is_not_used(monkeypatch):
    """去掉一处违规、又冒出另一处：总数没变，可冒出来的是原稿没有的，一样不采用。"""
    draft = "本周销售额 [[m:gmv]]。[[see:m:gmv]]增长主要来自新客首单，共 1200 单。下面看细节。"
    swapped = "本周销售额 [[m:gmv]]。[[see:m:gmv]]新客首单约占 3500 单。下面看细节。"
    models = Models(monkeypatch, writer=(draft, draft, swapped), verdicts={"主要来自新客": "contradicted"})
    row = await finish(weekly(claims="judge", on_violation="flag", max_repairs=1, judge={"rewrite_once": True}))
    assert row.status == "succeeded", row.error
    assert len(models.writes) == 3 and len(models.judges) == 1
    checked, doc, _ = await report_of(row.id)
    assert "1200" in doc["markdown"] and "3500" not in doc["markdown"]
    rewrite = checked["judge"]["rewrite"]
    assert rewrite["applied"] is False and "3500" in rewrite["reason"] and "1200" not in rewrite["reason"]


def test_new_violations_are_counted_one_by_one():
    """改写稿的违规逐个和原稿抵消：原样留着的不算新的，同一个裸数字多写一处、只标注的那类，都算新的。"""
    number = {"code": "uncited_number", "text": "1200", "message": "数字「1200」没有出处"}
    name = {"code": "unknown_entity", "text": "refund_detail", "message": "可能是编造的名字"}
    see = {"code": "unresolved_ref", "ref": "Q9", "message": "依据 [[see:Q9]] 解析不了"}
    assert report_mod._new_violations([number, name, see], [see, number, name]) == []
    assert report_mod._new_violations([number], [number, number]) == [number]
    assert report_mod._new_violations([number], [name]) == [name]
    assert report_mod._new_violations([number, see], []) == []


async def test_a_second_round_that_hits_a_limit_names_the_configured_limit(monkeypatch):
    """第二轮用的是第一轮剩下的钱，可说给人看的上限是节点上配的那个数，不是剩下的零头。"""
    Models(monkeypatch, writer=(TEXT, REWRITTEN), verdicts={"主要来自新客": "contradicted"})
    # 每句估 1 分钱、回复不带用量（实际就按估算记）：第一轮两句花掉 2 分，第二轮只剩半分，一句也装不下
    monkeypatch.setattr(judge_mod.JudgeRequest, "cost", lambda self, model_id, units: 0.01 * len(units))
    monkeypatch.setattr(judge_mod, "_usage_of", lambda message, model_id: {"input_tokens": 0, "output_tokens": 0})
    row = await finish(weekly(claims="judge", judge={"rewrite_once": True, "max_cost_usd": 0.025}))
    assert row.status == "succeeded", row.error
    checked, doc, _ = await report_of(row.id)
    v = verdicts(doc)["新客首单的贡献要等新客维度的数据再看。"]
    assert (v["status"], v["reason"]) == ("unjudged", "max_cost_usd")
    assert "$0.025" in v["rationale"] and "0.005" not in v["rationale"], v["rationale"]
    summary = checked["judge"]
    assert summary["rewrite"]["applied"] is True and summary["limits"]["max_cost_usd"] == 0.025
    assert "$0.025" in summary["gaps"][0] and "0.005" not in summary["gaps"][0], summary["gaps"]
    [log] = [e.data for e in await events(row.id, "log") if e.data.get("code") == "judge_limit"]
    assert "$0.025" in log["message"] and "0.005" not in log["message"]


# --------------------------------------------------------------------------
# 重放安全
# --------------------------------------------------------------------------


async def test_continuing_after_a_failure_does_not_judge_or_charge_twice(monkeypatch):
    models = Models(monkeypatch)
    real = report_mod._store
    broken = {"on": True}

    async def flaky(doc, ctx):
        if broken["on"]:
            raise RuntimeError("落盘时断了")
        return await real(doc, ctx)

    monkeypatch.setattr(report_mod, "_store", flaky)
    run = await run_manager.start(graph=weekly(claims="judge"), input_payload={}, run_class="formal")
    row = await wait(run.id)
    assert row.status == "failed" and "落盘时断了" in (row.error or ""), row.error
    assert len(models.judges) == 1
    before = await daily_spend()
    await run_manager.wait_idle(run.id)

    broken["on"] = False
    await run_manager.continue_failed(run.id)
    row = await wait(run.id)
    assert row.status == "succeeded", row.error
    assert len(models.judges) == 1, "接着跑时裁判又调了一次：裁判的结果没进 checkpoint"
    assert await daily_spend() == before, "接着跑时每日计数又记了一次"
    assert len(await judge_ends(row.id)) == 1
    _, doc, _ = await report_of(row.id)
    assert verdicts(doc)[CITED]["status"] == "supported"


async def test_a_changed_draft_is_judged_afresh_when_continuing(monkeypatch):
    """接着跑时写作者重写了一稿（写作调用不进 checkpoint），缓存的判定是上一稿的：不能拿来用。"""
    models = Models(monkeypatch, writer=(TEXT, "本周销售额 [[m:gmv]]。[[see:m:gmv]]增长主要来自老客复购。下面看细节。"))
    real = report_mod._store
    broken = {"on": True}

    async def flaky(doc, ctx):
        if broken["on"]:
            raise RuntimeError("落盘时断了")
        return await real(doc, ctx)

    monkeypatch.setattr(report_mod, "_store", flaky)
    run = await run_manager.start(graph=weekly(claims="judge"), input_payload={}, run_class="formal")
    assert (await wait(run.id)).status == "failed"
    await run_manager.wait_idle(run.id)
    broken["on"] = False
    await run_manager.continue_failed(run.id)
    row = await wait(run.id)
    assert row.status == "succeeded", row.error
    assert len(models.judges) == 2 and "老客复购" in models.judges[1]
    _, doc, _ = await report_of(row.id)
    assert verdicts(doc)["增长主要来自老客复购。"]["status"] == "supported"


async def test_runs_from_before_the_upgrade_never_judge(monkeypatch):
    """升级前发起的运行（没有护栏快照）：claims 一律按 off，节点里一个 task 都不多调用，产出一个键都不多。"""
    async def no_snapshot(session):
        return None

    monkeypatch.setattr(runner_mod, "_agent_limits", no_snapshot)
    models = Models(monkeypatch)
    calls = spy_tasks(monkeypatch)
    row = await finish(weekly(claims="judge"))
    assert row.status == "succeeded", row.error
    assert models.judges == [] and calls == []
    checked, doc, output = await report_of(row.id)
    assert "judge" not in checked and "claims" not in checked and "judge" not in output and "judge" not in doc
    assert all(v is None for v in verdicts(doc).values())


async def test_runs_paused_at_an_approval_resume_with_tasks_in_place(monkeypatch):
    """停在审批上的运行恢复：审批节点照旧只问一次；报告节点在恢复之后才第一次执行，裁判 task 只调一次、
    排在节点所有已有调用之后。没开裁判的报告节点一个 task 都不调用（和升级前一样）。"""
    assert replay.PROTOCOL == 2, "没有改动已有 task 的顺序，重放协议不升版"
    for claims, expected in (("require_citation", []), ("judge", ["judge_step"])):
        models = Models(monkeypatch)
        calls = spy_tasks(monkeypatch)
        paused = await finish(weekly(gate=True, claims=claims), statuses=("interrupted", "failed"))
        assert paused.status == "interrupted", paused.error
        await run_manager.resume(paused.id, {"approved": True})
        row = await wait(paused.id)
        assert row.status == "succeeded", row.error
        assert calls == expected, (claims, calls)
        assert len(models.judges) == len(expected)
        assert len(await events(row.id, "human.requested", "gate")) == 1
        checked, _, _ = await report_of(row.id)
        assert ("judge" in checked) is (claims == "judge")
