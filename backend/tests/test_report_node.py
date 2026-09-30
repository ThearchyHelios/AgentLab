"""报告撰写节点：模型只写引用标记，数字由系统从口径卡取出来渲染。

以前叙述是 llm 节点写的自由文本，出具时再按数值回头猜每个数来自哪个指标——
5 个指标都是 0.0 时，报告里所有的 0 全算给第一个；模型顺手写的「45678」没人拦，
直到出口才被判成「未回指」。报告节点把这两件事都挪到写的那一刻：

- 每个数字带着它自己的出处（按 id 引用，同值不同源也各归各的）
- 裸数字、引用不存在当场判违规，让写作者重写一次；还不行，按 on_violation 失败或标注
- 流里看到的就是最终的数字，没有 [[m:gmv]] 这种原始标记
- 自查结果（report.checked）落在封存范围内，证据接口从它出发找文档
"""
from __future__ import annotations

import asyncio

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from sqlalchemy import select

from app.core import artifact_store
from app.core.events import EventType
from app.db.base import SessionLocal
from app.db.models import Run, RunEvent
from app.engine import runner as runner_mod
from app.engine.evidence import DOC_SCHEMA, iter_segments, make_eid
from app.engine.runner import run_manager, verify_manifest

DSML = ('<｜｜DSML｜｜ calls>\n<｜｜DSML｜｜ invoke name="db_query__shop">\n'
        '<｜｜DSML｜｜ parameter name="sql">SELECT 1</｜｜DSML｜｜ parameter>\n'
        '</｜｜DSML｜｜ invoke>\n</｜｜DSML｜｜ calls>')


@pytest.fixture(autouse=True)
async def engine_up():
    await run_manager.setup()
    yield
    await run_manager.shutdown()


def node(nid, ntype, **config):
    return {"id": nid, "type": ntype, "position": {"x": 0, "y": 0},
            "data": {"label": nid, "config": config}}


KPI = "{'gmv': 45678.5, 'gmv_prev': 42010.0, 'orders': 1234}"
STANDARD = [
    {"id": "gmv", "name": "销售额", "unit": "元", "decimals": 1, "expression": "vars.kpi.gmv"},
    {"id": "wow", "name": "环比增幅", "unit": "%", "decimals": 1, "format": "plain",
     "expression": "round((vars.kpi.gmv - vars.kpi.gmv_prev) / vars.kpi.gmv_prev * 100, 1)"},
    {"id": "orders", "name": "订单数", "unit": "单", "expression": "vars.kpi.orders"},
]
CLEAN = "## 本周概览\n\n本周（[[i:week]]）销售额 [[m:gmv]]，环比 [[m:wow]]；订单 [[m:orders]]。[[see:m:gmv,m:wow]]"


def weekly(report=None, *, kpi=KPI, metrics=STANDARD, contract=None, between=(), extra_edges=()):
    """input → 取数 → 口径卡 → 报告 → 出口。between 里的节点插在报告和出口之间。"""
    out = {"fields": [{"name": "周报", "value": "{{ nodes.write.text }}"}]}
    if contract is not None:
        out["contract"] = contract
    nodes = [
        node("start", "input", fields=[{"name": "week", "default": "2026-W37"}]),
        node("fetch", "transform", mode="expression", expression=kpi, assign_to="kpi"),
        node("caliber", "metrics", caliber="周报口径", caliber_version="v2", metrics=metrics),
        node("write", "report", instructions="为 {{ input.week }} 写周报", **(report or {})),
        *between,
        node("out", "output", **out),
    ]
    chain = ["start", "fetch", "caliber", "write", *[n["id"] for n in between], "out"]
    return {"nodes": nodes,
            "edges": [{"source": a, "target": b} for a, b in zip(chain, chain[1:])]
            + [{"source": a, "target": b} for a, b in extra_edges]}


def script(monkeypatch, *replies):
    """模型依次回这几段，用完了一直回最后一段。返回每次调用收到的消息。"""
    from app.providers import mock_model

    seen: list[list] = []

    def _decide(self, messages):
        seen.append(list(messages))
        return AIMessage(content=replies[min(len(seen), len(replies)) - 1])

    monkeypatch.setattr(mock_model.MockChatModel, "_decide", _decide)
    return seen


def spy_tokens(monkeypatch) -> list:
    """llm.token 不落库，只广播：在广播口上截下来。"""
    captured: list = []
    original = runner_mod.bus.publish

    async def spy(event):
        if str(event.type) == EventType.LLM_TOKEN:
            captured.append(event)
        await original(event)

    monkeypatch.setattr(runner_mod.bus, "publish", spy)
    return captured


async def finish(graph, *, run_class="exploratory", statuses=("succeeded", "failed")) -> Run:
    run = await run_manager.start(graph=graph, input_payload={}, run_class=run_class)
    for _ in range(400):
        await asyncio.sleep(0.05)
        async with SessionLocal() as session:
            row = await session.get(Run, run.id)
            # 状态先提交、封存随后才写：终态要等 manifest_seq 落下来再看
            if row.status in statuses and (row.status == "interrupted" or row.manifest_seq is not None):
                return row
    raise AssertionError(f"run {run.id} 没跑完：{row.status}")


async def events(run_id: str, etype: str | None = None) -> list[RunEvent]:
    async with SessionLocal() as session:
        q = select(RunEvent).where(RunEvent.run_id == run_id)
        if etype:
            q = q.where(RunEvent.type == etype)
        return list((await session.execute(q.order_by(RunEvent.seq))).scalars())


async def report_of(run_id: str) -> tuple[dict, dict]:
    """(report.checked 的载荷, 取回的文档)。"""
    checked = await events(run_id, "report.checked")
    assert len(checked) == 1, [e.data for e in checked]
    return checked[0].data, artifact_store.load(checked[0].data["doc_artifact"])


def numbers(doc: dict) -> list[tuple[str, str | None, str]]:
    return [(s["text"], (s.get("cite") or {}).get("locator", {}).get("metric"), s["state"])
            for s in iter_segments(doc) if s["kind"] == "number"]


# --------------------------------------------------------------------------
# 各归各的
# --------------------------------------------------------------------------


async def test_five_zero_metrics_each_keep_their_own_source(monkeypatch):
    """旧契约按数值回指：5 个 0.0 全算给第一个指标。按 id 引用就没有这个问题。"""
    zeros = [{"id": k, "name": f"指标{k.upper()}", "unit": "元", "decimals": 1,
              "expression": f"vars.kpi.{k}"} for k in "abcde"]
    script(monkeypatch, "各项指标：A [[m:a]]，B [[m:b]]，C [[m:c]]，D [[m:d]]，E [[m:e]]。"
                        "[[see:m:a,m:b,m:c,m:d,m:e]]")
    row = await finish(weekly(kpi="{'a': 0.0, 'b': 0.0, 'c': 0.0, 'd': 0.0, 'e': 0.0}", metrics=zeros))
    assert row.status == "succeeded", row.error
    checked, doc = await report_of(row.id)
    assert numbers(doc) == [("0.0元", k, "deterministic") for k in "abcde"]

    card = (await events(row.id, "node.finished"))
    card_artifact = next(e.data["evidence"][0]["artifact"] for e in card if e.node_id == "caliber")
    eids = [s["cite"]["eid"] for s in iter_segments(doc) if s["kind"] == "number"]
    assert eids == [make_eid("metric", card_artifact, {"metric": k}) for k in "abcde"]
    assert len(set(eids)) == 5
    assert checked["ok"] is True and checked["violations"] == [] and checked["repairs"] == 0
    assert checked["stats"]["numbers_cited"] == 5 and checked["stats"]["uncited_numbers"] == 0


async def test_output_shape_and_stored_document(monkeypatch):
    script(monkeypatch, CLEAN)
    graph = weekly({"assign_to": "report"})
    row = await finish(graph)
    assert row.status == "succeeded", row.error
    checked, doc = await report_of(row.id)

    assert doc["schema"] == DOC_SCHEMA and doc["run_id"] == row.id and doc["node_id"] == "write"
    text = doc["markdown"]
    assert text == "## 本周概览\n\n本周（2026-W37）销售额 45,678.5元，环比 8.7%；订单 1,234单。"
    assert row.output["周报"] == text
    assert numbers(doc) == [("45,678.5元", "gmv", "deterministic"), ("8.7%", "wow", "deterministic"),
                            ("1,234单", "orders", "deterministic")]

    finished = next(e for e in await events(row.id, "node.finished") if e.node_id == "write")
    payload = artifact_store.load(finished.data["artifact"])
    assert payload["text"] == text and payload["doc_artifact"] == checked["doc_artifact"]
    assert payload["stats"] == checked["stats"] == doc["stats"]
    assert payload["violations"] == [] and payload["repairs"] == 0
    assert payload["model"] and isinstance(payload["duration_ms"], int)
    # assign_to 拿到的是渲染后的正文：下游模板引用它，看到的就是报告本身
    state = await run_manager.checkpointer.aget_tuple({"configurable": {"thread_id": row.id}})
    assert state.checkpoint["channel_values"]["vars"]["report"] == text


async def test_prompt_carries_the_catalog_and_writing_rules(monkeypatch):
    seen = script(monkeypatch, CLEAN)
    row = await finish(weekly({"system": "你是周报撰写人"}))
    assert row.status == "succeeded", row.error
    prompt = "\n".join(str(m.content) for m in seen[0])
    assert "你是周报撰写人" in prompt and "为 2026-W37 写周报" in prompt
    assert "[[m:gmv]] 销售额 = 45,678.5元" in prompt and "[[i:week]] = 2026-W37" in prompt
    assert "任何数字都只能用引用标记写" in prompt


async def test_metrics_from_limits_the_catalog_to_the_named_card(monkeypatch):
    """两张上游口径卡里有同名指标：只写 east 时，m:gmv 就是 east 的那一个，不必写卡名。"""
    seen = script(monkeypatch, "东区销售额 [[m:gmv]]。")
    graph = weekly({"metrics_from": ["east"]}, between=())
    graph["nodes"][2:3] = [
        node("east", "metrics", caliber="东区", metrics=[{"id": "gmv", "unit": "元", "expression": "vars.kpi.gmv"}]),
        node("west", "metrics", caliber="西区", metrics=[{"id": "gmv", "unit": "元", "expression": "vars.kpi.orders"}]),
    ]
    graph["edges"] = [{"source": a, "target": b} for a, b in [
        ("start", "fetch"), ("fetch", "east"), ("fetch", "west"), ("east", "write"), ("west", "write"),
        ("write", "out")]]
    row = await finish(graph)
    assert row.status == "succeeded", row.error
    prompt = "\n".join(str(m.content) for m in seen[0])
    assert "东区" in prompt and "西区" not in prompt
    _, doc = await report_of(row.id)
    assert numbers(doc) == [("45,678.5元", "gmv", "deterministic")]
    assert doc["catalog"]["m:gmv"]["node_id"] == "east"


# --------------------------------------------------------------------------
# 流：看到的就是最终的数字
# --------------------------------------------------------------------------


async def test_llm_tokens_carry_real_numbers_not_markup(monkeypatch):
    """mock 按 6 个字一块吐，标记必然被切在两块中间——渲染器要攒齐了再放。"""
    script(monkeypatch, CLEAN + "\n\n引用错了的也不漏标记：[[m:gmvx]]。")
    tokens = spy_tokens(monkeypatch)
    row = await finish(weekly({"max_repairs": 0}))      # 只看一遍流：修复会再流一遍
    assert row.status == "succeeded", row.error
    streamed = "".join(t.data["delta"] for t in tokens if t.node_id == "write")
    assert "[[" not in streamed and "]]" not in streamed and "m:gmv" not in streamed.replace("?m:gmvx", "")
    assert "45,678.5元" in streamed and "8.7%" in streamed and "2026-W37" in streamed
    assert "⟦?m:gmvx⟧" in streamed           # 解析不了的引用在流里就是占位，和文档里一样
    _, doc = await report_of(row.id)
    assert streamed.strip() == doc["markdown"]


# --------------------------------------------------------------------------
# 违规：修复一次；还不行按 on_violation
# --------------------------------------------------------------------------


async def test_a_stream_that_breaks_mid_marker_leaks_nothing(monkeypatch):
    """流断在一个标记的半截（「[[m:g」），_invoke_streaming 退回非流式重来。渲染器攒着的半截
    得扔掉：不然收尾 flush 时它会原样漏进 llm.token，和重来的那份拼在一起。"""
    from langchain_core.messages import AIMessageChunk
    from langchain_core.outputs import ChatGenerationChunk

    from app.providers import mock_model

    script(monkeypatch, CLEAN)

    async def broken(self, messages, stop=None, run_manager=None, **kwargs):
        for piece in ("本周（[[i:week]]）销售额 ", "[[m:g"):
            yield ChatGenerationChunk(message=AIMessageChunk(content=piece))
        raise RuntimeError("网关断开")

    monkeypatch.setattr(mock_model.MockChatModel, "_astream", broken)
    tokens = spy_tokens(monkeypatch)
    row = await finish(weekly({"on_violation": "fail"}))
    assert row.status == "succeeded", row.error
    assert [e.data["code"] for e in await events(row.id, "log") if e.data.get("code") == "stream_fallback"] \
        == ["stream_fallback"]
    streamed = "".join(t.data["delta"] for t in tokens if t.node_id == "write")
    # 断之前放出去的是渲染好的字；半截标记一个字符都不能出去。末尾的空格也先攒着：
    # 后面要是 [[see:…]]，渲染时这个空格要去掉
    assert streamed == "本周（2026-W37）销售额", streamed
    checked, doc = await report_of(row.id)
    assert checked["ok"] is True and checked["repairs"] == 0
    assert doc["markdown"] == "## 本周概览\n\n本周（2026-W37）销售额 45,678.5元，环比 8.7%；订单 1,234单。"
    assert row.output["周报"] == doc["markdown"]


async def test_a_bare_number_is_repaired_exactly_once(monkeypatch):
    seen = script(monkeypatch, "本周销售额 45678 元，订单 [[m:orders]]。", CLEAN)
    row = await finish(weekly())
    assert row.status == "succeeded", row.error
    assert len(seen) == 2, "违规之后应当恰好重写一次"
    repair = seen[1][-1]
    assert isinstance(repair, HumanMessage) and "45678" in str(repair.content)
    assert isinstance(seen[1][-2], AIMessage) and "45678" in str(seen[1][-2].content)
    warns = [e.data for e in await events(row.id, "log") if e.data.get("code") == "report_repair"]
    assert len(warns) == 1 and warns[0]["level"] == "warn" and "45678" in warns[0]["message"]
    checked, doc = await report_of(row.id)
    assert checked["repairs"] == 1 and checked["ok"] is True and doc["violations"] == []
    assert "45678" not in doc["markdown"]


async def test_persistent_violation_fails_in_fail_mode(monkeypatch):
    seen = script(monkeypatch, "本周销售额 45678 元，引用 [[m:nope]]。")
    row = await finish(weekly({"on_violation": "fail"}))
    assert row.status == "failed"
    assert row.error_node_id == "write"
    assert "45678" in row.error and "nope" in row.error, row.error
    assert len(seen) == 2                      # 默认修复一次
    # 失败的那份也要留底：自查结果照样进封存，事后看得到它哪里不对
    checked, doc = await report_of(row.id)
    assert checked["ok"] is False and checked["repairs"] == 1 and checked["failed"] is True
    assert {v["code"] for v in doc["violations"]} == {"uncited_number", "unresolved_ref"}


async def test_max_repairs_zero_means_no_rewrite(monkeypatch):
    seen = script(monkeypatch, "本周销售额 45678 元。")
    row = await finish(weekly({"on_violation": "fail", "max_repairs": 0}))
    assert row.status == "failed" and len(seen) == 1


async def test_persistent_violation_is_flagged_in_flag_mode(monkeypatch):
    script(monkeypatch, "本周销售额 45678 元，订单 [[m:orders]]。")
    row = await finish(weekly({"on_violation": "flag"}))
    assert row.status == "succeeded", row.error
    checked, doc = await report_of(row.id)
    assert checked["ok"] is False and checked["repairs"] == 1 and checked["failed"] is False
    assert row.output["周报"] == doc["markdown"]
    bare = [s for s in iter_segments(doc) if s.get("issue") == "uncited_number"]
    assert [(s["text"], s["state"]) for s in bare] == [("45678", "none")]
    finished = next(e for e in await events(row.id, "node.finished") if e.node_id == "write")
    assert [v["code"] for v in artifact_store.load(finished.data["artifact"])["violations"]] \
        == ["uncited_number"]


async def test_on_violation_defaults_follow_the_run_class(monkeypatch):
    """探索运行默认标注、照常产出；正式运行默认失败。"""
    script(monkeypatch, "本周销售额 45678 元。")
    explore = await finish(weekly())
    assert explore.status == "succeeded", explore.error
    formal = await finish(weekly(), run_class="formal")
    assert formal.status == "failed" and "45678" in formal.error


async def test_the_default_follows_the_run_record_in_subgraphs_and_after_resume(monkeypatch):
    """默认值按运行记录上的 run_class 取：子图的线程 id 是「运行 id:节点 id」，审批之后续跑
    是另起的执行上下文——两种情况下正式运行都不能悄悄退成 flag。"""
    from app.db.models import Workflow

    script(monkeypatch, "本周销售额 45678 元。")
    async with SessionLocal() as session:
        wf = Workflow(name="周报子流程", graph=weekly())
        session.add(wf)
        await session.commit()
        wf_id = wf.id
    parent = {"nodes": [node("start", "input", fields=[]), node("sub", "subgraph", workflow_id=wf_id),
                        node("out", "output", fields=[{"name": "r", "value": "{{ nodes.sub.output.周报 }}"}])],
              "edges": [{"source": "start", "target": "sub"}, {"source": "sub", "target": "out"}]}
    explore = await finish(parent)
    assert explore.status == "succeeded", explore.error
    assert "45678" in explore.output["r"]
    formal = await finish(parent, run_class="formal")
    assert formal.status == "failed" and "45678" in formal.error, formal.error

    gated = weekly()
    gated["nodes"].insert(3, node("gate", "human", mode="approve", title="确认口径"))
    chain = ["start", "fetch", "caliber", "gate", "write", "out"]
    gated["edges"] = [{"source": a, "target": b} for a, b in zip(chain, chain[1:])]
    paused = await finish(gated, run_class="formal", statuses=("interrupted", "failed"))
    assert paused.status == "interrupted", paused.error
    await run_manager.resume(paused.id, {"approved": True})
    for _ in range(400):
        await asyncio.sleep(0.05)
        async with SessionLocal() as session:
            resumed = await session.get(Run, paused.id)
            if resumed.status in ("succeeded", "failed") and resumed.manifest_seq is not None:
                break
    assert resumed.status == "failed" and "45678" in resumed.error, resumed.error
    [checked] = [e.data for e in await events(paused.id, "report.checked")]
    assert checked["on_violation"] == "fail"


async def test_numbers_off_does_not_rewrite_but_still_records(monkeypatch):
    seen = script(monkeypatch, "本周销售额 45678 元。")
    row = await finish(weekly({"numbers": "off", "on_violation": "fail"}))
    assert row.status == "succeeded", row.error
    assert len(seen) == 1
    checked, doc = await report_of(row.id)
    # 不拦不等于不记：文档和出口复核照样看得到这个数没有出处
    assert [v["code"] for v in doc["violations"]] == ["uncited_number"]
    assert checked["ok"] is False


async def test_numbers_the_contract_allows_are_not_repaired(monkeypatch):
    """出口契约放行的字面量（allow_numbers），报告自查也放行，不必为它白白重写一次。"""
    seen = script(monkeypatch, "共分 37 个门店统计，销售额 [[m:gmv]]。")
    row = await finish(weekly(contract={"metrics_from": ["caliber"], "report_from": "write",
                                        "allow_numbers": ["37"]}))
    assert row.status == "succeeded", row.error
    assert len(seen) == 1
    checked, _ = await report_of(row.id)
    assert checked["ok"] is True


@pytest.mark.parametrize("value", ["sometimes", 3])
async def test_bad_on_violation_is_rejected(monkeypatch, value):
    script(monkeypatch, CLEAN)
    row = await finish(weekly({"on_violation": value}))
    assert row.status == "failed" and "「重写后仍有违规时」" in row.error


async def test_tool_markup_is_nudged_once(monkeypatch):
    seen = script(monkeypatch, DSML, CLEAN)
    row = await finish(weekly())
    assert row.status == "succeeded", row.error
    assert len(seen) == 2
    assert "tool_markup_leak" in [e.data.get("code") for e in await events(row.id, "log")]
    _, doc = await report_of(row.id)
    assert "DSML" not in doc["markdown"]


# --------------------------------------------------------------------------
# 封存
# --------------------------------------------------------------------------


async def test_report_checked_is_inside_the_seal(monkeypatch):
    script(monkeypatch, CLEAN)
    row = await finish(weekly())
    assert row.status == "succeeded", row.error
    checked = (await events(row.id, "report.checked"))[0]
    assert checked.node_id == "write"
    assert row.manifest_seq is not None and checked.seq <= row.manifest_seq
    assert (await verify_manifest(row.id))["ok"] is True
    # 文档取回时复验哈希；事件里的 id 就是它的内容哈希
    assert artifact_store.load(checked.data["doc_artifact"])["node_id"] == "write"


# --------------------------------------------------------------------------
# 目录参数：出口复核要用同一个函数重建，这里单独钉住它的语义
# --------------------------------------------------------------------------


def test_report_catalog_honours_evidence_from_and_ancestry():
    """evidence_from 只管查询、检索这类证据从哪几个节点来；不在上游的节点什么都不收。"""
    from app.engine.nodes.report import report_catalog
    from app.engine.schema import GraphSpec

    spec = GraphSpec.model_validate({
        "nodes": [node("a", "tool", tool="x"), node("b", "tool", tool="x"), node("late", "tool", tool="x"),
                  node("card", "metrics", metrics=[{"id": "gmv", "expression": "1"}]),
                  node("write", "report", evidence_from=["a"])],
        "edges": [{"source": s, "target": t} for s, t in [("a", "card"), ("b", "card"), ("card", "write"),
                                                         ("write", "late")]]})
    state = {
        "nodes": {"card": {"kind": "metric_set", "caliber": "c", "caliber_version": "v1", "artifact": "f" * 64,
                           "metrics": [{"id": "gmv", "name": "销售额", "value": 1, "unit": ""}]}},
        "evidence": [{"kind": "query", "node_id": "a", "artifact": "a" * 64, "rows": 2},
                     {"kind": "query", "node_id": "b", "artifact": "b" * 64, "rows": 3},
                     {"kind": "query", "node_id": "late", "artifact": "c" * 64, "rows": 4}],
        "input": {"week": "2026-W37", "items": [1, 2]},
    }
    write = spec.node_map()["write"]
    catalog = report_catalog(state, spec, write)
    assert set(catalog) == {"m:gmv", "Q1", "i:week"}
    assert catalog["Q1"]["node_id"] == "a"

    write.config["evidence_from"] = "ancestors"
    assert {k: v.get("node_id") for k, v in report_catalog(state, spec, write).items() if k.startswith("Q")} \
        == {"Q1": "a", "Q2": "b"}
