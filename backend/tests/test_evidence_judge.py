"""结论句裁判的内核（engine/judge.py）：证据摘录、裁判调用、结构化输出兜底、预算与每日计数。

数字、实体是确定性的，系统自己能核对；「增长主要来自华东」这种话说得对不对，只能交给另一个模型按
证据判断。这是概率性的判断，所以每一步都要留余地、说实话：

- 一份报告合并成一次调用（超过每批的句数就分批），每句给 supported / partial / contradicted / insufficient /
  not_a_claim
- 模型不支持结构化输出、或者给的不是合法结构时退回 JSON 解析，再失败就记「未裁判」，不编判定
- 裁判模型关掉 thinking，max_tokens 4096
- 预算（每份报告的句数、金额、时长，全局每日金额）每一项都能写 null 表示不限；触顶就停、已判的保留、
  没判的记未裁判并说明是哪个上限——不报错、不让报告失败。金额超了按优先级截断：先判挂了依据的，
  再判有方向词、因果词的
- 每日计数落库，按本地日期滚动
- 给裁判的证据摘录里没有数据源遮罩的列

所有模型调用都是 mock 剧本，不碰真接口。
"""
from __future__ import annotations

import json
import re

import pytest
from langchain_core.messages import AIMessage
from sqlalchemy import delete

from app.core import artifact_store
from app.core.events import EventType
from app.db.base import SessionLocal
from app.db.models import Setting
from app.engine import judge as judge_mod
from app.engine.evidence import build_catalog, compose_doc, iter_units
from app.engine.judge import (
    BATCH_SIZE,
    CHUNK_CHARS,
    EXCERPT_COLS,
    EXCERPT_ROWS,
    JUDGE_ROLE,
    JUDGE_RULES,
    MAX_TOKENS,
    RATIONALE_MAX,
    SPEND_KEY,
    SQL_CHARS,
    Budget,
    daily_spend,
    judge_doc,
    prepare,
    source_masks,
)
from app.providers.factory import ModelSpec
from app.providers.mock_model import MockChatModel

#: 目录里有价格的模型：金额上限对它起作用
PRICED = "claude-sonnet-5"
UNLIMITED = Budget(max_claims=None, max_cost_usd=None, timeout_s=None, daily_max_usd=None)
SPEC = ModelSpec(model=PRICED)


# --------------------------------------------------------------------------
# 夹具：一张口径卡、一次查询（带手机号列）、一次知识库检索、一个运行输入
# --------------------------------------------------------------------------


def _store(content) -> str:
    """按 put_json 的落盘方式写进测试用的临时工件库：取回走真实的 load，复验哈希。"""
    text = artifact_store.canonical_json(content)
    artifact = artifact_store.content_hash(text)
    path = artifact_store._path_of(artifact)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return artifact


CARD = {"kind": "metric_set", "caliber": "周报口径", "caliber_version": "v2", "metrics": [
    {"id": "gmv", "name": "销售额", "unit": "元", "value": 45678.5, "decimals": 1, "format": "thousands",
     "status": "ok", "expression": "vars.kpi.gmv", "rendered": "45,678.5元"},
    {"id": "wow", "name": "环比增幅", "unit": "%", "value": 8.7, "decimals": 1, "format": "plain", "status": "ok",
     "expression": "round((vars.kpi.gmv - vars.kpi.gmv_prev) / vars.kpi.gmv_prev * 100, 1)", "rendered": "8.7%"},
]}
LONG_SQL = "SELECT region, SUM(amount) AS amount, MAX(phone) AS phone FROM orders WHERE " + " AND ".join(
    f"flag_{i} = 1" for i in range(60)) + " GROUP BY region ORDER BY amount DESC"
ROWS = [[f"区域{i}", 1000.0 - i, f"PHONE-{i}"] for i in range(30)]
SNAP = {"columns": ["region", "amount", "phone"], "rows": ROWS, "row_count": len(ROWS), "truncated": False,
        "elapsed_ms": 3, "sql": LONG_SQL, "source": "shop"}
HITS = {"query": "退款", "collection": "docs", "hits": [
    {"content": "退款主要集中在物流延误的订单。" * 60, "title": "售后周报", "document": "doc-1", "chunk": 0},
    {"content": "第二段原文。", "title": "售后周报", "document": "doc-1", "chunk": 1}]}


@pytest.fixture
def stored():
    card = {**CARD}
    art = _store(card)
    return {"card": art, "snap": _store(SNAP), "masked": _store({**SNAP, "mask_columns": ["phone"]}),
            "hits": _store(HITS)}


def make_catalog(stored, snap="snap"):
    ledger = [
        {"kind": "metric_set", "node_id": "caliber", "exec": 1, "artifact": stored["card"], "caliber": "周报口径",
         "version": "v2", "metrics": ["gmv", "wow"]},
        {"kind": "query", "node_id": "fetch", "exec": 1, "artifact": stored[snap], "via": "b" * 64,
         "call_id": "call_1", "tool": "db_query__shop", "source": "shop", "columns": SNAP["columns"],
         "rows": len(ROWS), "truncated": False},
        {"kind": "retrieval", "node_id": "kb", "exec": 1, "artifact": stored["hits"], "source": "docs", "rows": 2},
    ]
    return build_catalog(nodes={"caliber": {**CARD, "artifact": stored["card"]}}, ledger=ledger,
                         inputs={"week": "2026-W37"})


RAW = ("## 本周概览\n\n"
       "本周销售额 [[m:gmv]]。[[see:m:gmv]]"
       "环比 [[m:wow]]，增长主要来自华东。[[see:m:wow,Q1]]"
       "增长主要来自新客首单。"
       "订单一共是 1234 单，整体平稳。"
       "下面看细节。")


def units_of(doc) -> dict[str, str]:
    """{句子原文: unit id}"""
    md = doc["markdown"]
    return {md[u["span"][0]:u["span"][1]].strip(): u["id"] for _, u in iter_units(doc)}


CITED, CITED2, CAUSAL, NUMBER = ("本周销售额 45,678.5元。", "环比 8.7%，增长主要来自华东。", "增长主要来自新客首单。",
                                 "订单一共是 1234 单，整体平稳。")


@pytest.fixture(autouse=True)
async def clean_settings():
    """每日计数和裁判设置都落在设置表里：每个测试从干净的一天开始。"""
    async with SessionLocal() as session:
        await session.execute(delete(Setting).where(Setting.key.in_([SPEND_KEY, "judge", "copilot"])))
        await session.commit()
    yield


class Judge:
    """裁判模型的剧本。结构化调用和退回的纯文本调用分开记；verdicts 按 unit id 给判定，没写的都是 supported。"""

    def __init__(self, monkeypatch, verdicts=None, *, model_id=PRICED, structured=None, plain=None,
                 latency=0.0, fail=None):
        self.calls: list[dict] = []
        self.specs: list[ModelSpec] = []
        verdicts = verdicts or {}

        def answer(units):
            return json.dumps({"items": [{"unit": u, "verdict": verdicts.get(u, "supported"),
                                          "rationale": f"{u} 的依据", "used": ["m:gmv"]} for u in units]},
                              ensure_ascii=False)

        def decide(model, messages):
            assert JUDGE_ROLE in str(messages[0].content), "这里只该有裁判调用"
            units = re.findall(r"^\[(u\d+)\]", str(messages[-1].content), re.M)
            kind = "structured" if model.response_format else "plain"
            self.calls.append({"kind": kind, "units": units, "messages": list(messages)})
            if fail:
                raise RuntimeError(fail)
            custom = structured if kind == "structured" else plain
            if custom is not None:
                return AIMessage(content=custom(units) if callable(custom) else custom)
            return AIMessage(content=answer(units))

        monkeypatch.setattr(MockChatModel, "_decide", decide)

        async def fake_model(session, spec):
            self.specs.append(spec)
            return MockChatModel(model_name=model_id, latency=latency), model_id

        monkeypatch.setattr(judge_mod, "get_chat_model", fake_model)

    @property
    def judged(self) -> list[list[str]]:
        return [c["units"] for c in self.calls if c["kind"] == "structured"]


def status_of(outcome, doc, text):
    return outcome["verdicts"][units_of(doc)[text]]


# --------------------------------------------------------------------------
# 调用与输出
# --------------------------------------------------------------------------


async def test_one_call_for_the_whole_report_and_the_verdict_shape(monkeypatch, stored):
    judge = Judge(monkeypatch, {"u3": "contradicted"})
    catalog = make_catalog(stored)
    doc = compose_doc(RAW, catalog)
    outcome = await judge_doc(doc, catalog, spec=SPEC, budget=UNLIMITED)
    assert len(judge.calls) == 1, "一份报告合并成一次调用"
    assert sorted(judge.judged[0]) == sorted(units_of(doc)[t] for t in (CITED, CITED2, CAUSAL, NUMBER))
    v = status_of(outcome, doc, CITED)
    assert v["status"] == "supported" and v["judge"] == PRICED and v["post_seal"] is False
    assert v["rationale"] and v["used"] == ["m:gmv"]
    assert outcome["counts"] == {"supported": 3, "partial": 0, "contradicted": 1, "insufficient": 0, "not_a_claim": 0,
                                 "unjudged": 0, "unsupported": 0}
    assert outcome["model"] == PRICED and outcome["calls"] == 1 and outcome["gaps"] == []
    assert units_of(doc)["下面看细节。"] not in outcome["verdicts"], "连接性的话不送裁判"


async def test_the_judge_model_runs_without_thinking(monkeypatch, stored):
    judge = Judge(monkeypatch)
    catalog = make_catalog(stored)
    await judge_doc(compose_doc(RAW, catalog), catalog, spec=ModelSpec(model=PRICED, thinking="adaptive",
                                                                       max_tokens=64000),
                    budget=Budget(max_claims=None, max_cost_usd=None, timeout_s=30, daily_max_usd=None))
    [spec] = judge.specs
    assert spec.thinking == "off" and spec.max_tokens == MAX_TOKENS == 4096
    assert spec.model == PRICED and spec.timeout and spec.timeout <= 30


async def test_structured_output_failure_falls_back_to_json(monkeypatch, stored):
    """结构化输出失败（模型不支持、给的不是 JSON）：退回纯文本调用，从回复里把 JSON 抠出来。"""
    def fenced(units):
        body = {"items": [{"unit": u, "verdict": "partial", "rationale": "只对了一半", "used": []} for u in units]}
        return "判定如下：\n```json\n" + json.dumps(body, ensure_ascii=False) + "\n```"

    judge = Judge(monkeypatch, structured="这不是 JSON", plain=fenced)
    catalog = make_catalog(stored)
    doc = compose_doc(RAW, catalog)
    outcome = await judge_doc(doc, catalog, spec=SPEC, budget=UNLIMITED)
    assert [c["kind"] for c in judge.calls] == ["structured", "plain"]
    assert {v["status"] for v in outcome["verdicts"].values()} == {"partial"}
    assert outcome["gaps"] == []


async def test_an_array_written_as_json_text_is_still_read(monkeypatch, stored):
    """有的模型走工具调用时把嵌套的数组写成一段 JSON 文本（{"items": "[…]"}）：照样认，不为它再花一次调用。"""
    def stringified(units):
        items = [{"unit": u, "verdict": "contradicted", "rationale": "对不上", "used": []} for u in units]
        return json.dumps({"items": json.dumps(items, ensure_ascii=False)}, ensure_ascii=False)

    judge = Judge(monkeypatch, structured=stringified)
    catalog = make_catalog(stored)
    doc = compose_doc(RAW, catalog)
    outcome = await judge_doc(doc, catalog, spec=SPEC, budget=UNLIMITED)
    assert [c["kind"] for c in judge.calls] == ["structured"]
    assert {v["status"] for v in outcome["verdicts"].values()} == {"contradicted"}


async def test_a_broken_provider_is_not_called_batch_after_batch(monkeypatch, stored):
    """第一批就调不通（鉴权、网关挂了）：后面几批不再一次次去撞，统一记未裁判。"""
    judge = Judge(monkeypatch, fail="401 鉴权失败")
    catalog = make_catalog(stored)
    raw = "".join(f"第 {i} 个区域的销售额增长来自新客。[[see:m:gmv]]" for i in range(BATCH_SIZE + 5))
    doc = compose_doc(raw, catalog)
    outcome = await judge_doc(doc, catalog, spec=SPEC, budget=UNLIMITED)
    assert len(judge.calls) == 2, "结构化一次、退回纯文本一次，第二批不再调用"
    assert outcome["unjudged"] == {"error": BATCH_SIZE + 5} and "401 鉴权失败" in outcome["gaps"][0]


async def test_both_paths_failing_leaves_the_claims_unjudged(monkeypatch, stored):
    Judge(monkeypatch, structured="不是 JSON", plain="还是不是")
    catalog = make_catalog(stored)
    doc = compose_doc(RAW, catalog)
    outcome = await judge_doc(doc, catalog, spec=SPEC, budget=UNLIMITED)
    assert {(v["status"], v["reason"]) for v in outcome["verdicts"].values()} == {("unjudged", "format")}
    assert outcome["counts"]["unjudged"] == 4 and outcome["unjudged"] == {"format": 4}
    assert outcome["gaps"] and "4 句" in outcome["gaps"][0]


async def test_bad_items_are_not_trusted(monkeypatch, stored):
    """判定不认识、理由太长、编出目录里没有的依据、漏判的句子：各按各的处理，不编判定。"""
    catalog = make_catalog(stored)
    doc = compose_doc(RAW, catalog)
    ids = units_of(doc)

    def messy(units):
        return json.dumps({"items": [
            {"unit": ids[CITED], "verdict": "supported", "rationale": "长" * 200, "used": ["m:gmv", "m:nope", "Q9"]},
            {"unit": ids[CITED2], "verdict": "maybe", "rationale": "不认识的判定", "used": []},
            {"unit": ids[CAUSAL], "verdict": "contradicted", "rationale": "没有新客维度", "used": []},
            {"unit": "u999", "verdict": "supported", "rationale": "不存在的句子", "used": []},
        ]}, ensure_ascii=False)

    Judge(monkeypatch, structured=messy)
    outcome = await judge_doc(doc, catalog, spec=SPEC, budget=UNLIMITED)
    v = outcome["verdicts"]
    assert v[ids[CITED]]["rationale"] == "长" * (RATIONALE_MAX - 1) + "…" and v[ids[CITED]]["used"] == ["m:gmv"]
    assert (v[ids[CITED2]]["status"], v[ids[CITED2]]["reason"]) == ("unjudged", "format")
    assert v[ids[CAUSAL]]["status"] == "contradicted"
    assert (v[ids[NUMBER]]["status"], v[ids[NUMBER]]["reason"]) == ("unjudged", "missing")
    assert "u999" not in v


async def test_a_failed_call_is_unjudged_not_an_error(monkeypatch, stored):
    Judge(monkeypatch, fail="网关超时")
    catalog = make_catalog(stored)
    doc = compose_doc(RAW, catalog)
    events: list = []
    outcome = await judge_doc(doc, catalog, spec=SPEC, budget=UNLIMITED,
                              emit=lambda kind, **data: events.append((str(kind), data)))
    assert {v["reason"] for v in outcome["verdicts"].values()} == {"error"}
    assert "网关超时" in outcome["gaps"][0]
    ends = [d for k, d in events if k == EventType.LLM_END]
    assert ends and ends[-1]["purpose"] == "judge" and "网关超时" in ends[-1]["error"]
    assert any(k == EventType.LOG and d["code"] == "judge_failed" for k, d in events)


# --------------------------------------------------------------------------
# 证据摘录
# --------------------------------------------------------------------------


def request_text(judge) -> str:
    return "\n".join(str(m.content) for m in judge.calls[0]["messages"])


async def test_excerpts_follow_the_rules(monkeypatch, stored):
    judge = Judge(monkeypatch)
    catalog = make_catalog(stored)
    raw = ("本周销售额 [[m:gmv]]。[[see:m:gmv]]"
           "第 25 行的区域排在后面。[[see:Q1.r25]]"
           "整体看华东领先。[[see:Q1]]"
           "退款集中在物流延误的订单。[[see:K1]]"
           "本周是 [[i:week]]，增长来自新客。")
    doc = compose_doc(raw, catalog)
    await judge_doc(doc, catalog, spec=SPEC, budget=UNLIMITED)
    text = request_text(judge)
    assert "数字已经由系统核对过，不要复算" in JUDGE_RULES and JUDGE_RULES in text
    # 指标：名称 = 值，加原式
    assert "销售额 = 45,678.5元" in text and "vars.kpi.gmv" in text
    # 查询：SQL 前 300 字
    assert LONG_SQL[:SQL_CHARS] in text and LONG_SQL[:SQL_CHARS + 20] not in text
    # 被引用的行最多 20 行：点名的第 25 行先给，整份引用的再从头补满
    rows = re.findall(r"^r(\d+)：", text, re.M)
    assert len(rows) == EXCERPT_ROWS and "25" in rows and "0" in rows and "22" not in rows
    # 片段：最多 600 字
    first = HITS["hits"][0]["content"]
    assert first[:CHUNK_CHARS] in text and first[:CHUNK_CHARS + 1] not in text
    assert "week = 2026-W37" in text


async def test_masked_columns_never_reach_the_judge(monkeypatch, stored):
    judge = Judge(monkeypatch)
    catalog = make_catalog(stored)
    doc = compose_doc("整体看华东领先。[[see:Q1]]", catalog)
    await judge_doc(doc, catalog, spec=SPEC, budget=UNLIMITED)
    assert "PHONE-0" in request_text(judge), "对照：没设遮罩时原值在摘录里"

    # 数据源现在设的遮罩（列名不分大小写）
    judge = Judge(monkeypatch)
    await judge_doc(doc, catalog, spec=SPEC, budget=UNLIMITED, masked={"shop": ["Phone"]})
    text = request_text(judge)
    assert "PHONE-" not in text and "区域0" in text
    [header] = re.findall(r"^列：(.*)$", text, re.M)
    assert "phone" not in header.lower() and "region" in header, "表头里也不给遮罩的列"

    # 查询当时记下的遮罩（快照里的 mask_columns）
    judge = Judge(monkeypatch)
    catalog = make_catalog(stored, snap="masked")
    doc = compose_doc("整体看华东领先。[[see:Q1]]", catalog)
    await judge_doc(doc, catalog, spec=SPEC, budget=UNLIMITED)
    assert "PHONE-" not in request_text(judge)


#: 宽表：前 13 列是填充，被引用的列排在第 14、15 列，最后一列是手机号
WIDE_COLS = [f"c{i}" for i in range(13)] + ["refund_rate", "region", "phone"]
WIDE = {"columns": WIDE_COLS, "row_count": 5, "truncated": False, "elapsed_ms": 3, "source": "shop",
        "sql": "SELECT * FROM wide_orders",
        "rows": [[i * 100 + j for j in range(13)] + [f"RATE-{i}", f"区域{i}", f"PHONE-{i}"] for i in range(5)]}


def query_catalog(artifact: str, columns: list[str], *, source: str | None = "shop"):
    ledger = [{"kind": "query", "node_id": "fetch", "exec": 1, "artifact": artifact, "tool": "db_query__shop",
               **({"source": source} if source else {}), "columns": columns, "rows": 5, "truncated": False}]
    return build_catalog(nodes={}, ledger=ledger)


def header_of(excerpt: str) -> tuple[list[str], str]:
    """(摘录给出的列, 表头那一整行)"""
    [header] = re.findall(r"^列：(.*)$", excerpt, re.M)
    return [c for c in header.split(" | ") if not c.startswith("…")], header


async def test_cited_columns_of_a_wide_table_always_reach_the_judge():
    """宽表只给前几列时，句子引用的列排在后面也一定给：裁判看不到被引用的格子，就只能判不支持。"""
    catalog = query_catalog(_store(WIDE), WIDE_COLS)
    doc = compose_doc("[[v:Q1.r2.region]] 的退款率最高，达到 [[v:Q1.r2.refund_rate]]。"
                      "手机尾号 [[v:Q1.r2.phone]] 的客户下单最多。", catalog)
    excerpt = prepare(doc, catalog, masked={"shop": ["phone"]}).excerpts["Q1"]
    shown, header = header_of(excerpt)
    assert "region" in shown and "refund_rate" in shown, header
    [row] = re.findall(r"^r2：(.*)$", excerpt, re.M)
    cells = dict(zip(shown, row.split(" | "), strict=True))
    assert cells["region"] == "区域2" and cells["refund_rate"] == "RATE-2"
    # 遮罩的列被引用了也不给
    assert "phone" not in header.lower() and "PHONE-" not in excerpt
    # 没遮罩的 15 列给了 EXCERPT_COLS 列，「另有」按实际没给出的列数计
    assert len(shown) == EXCERPT_COLS and header.endswith(f"…另有 {15 - EXCERPT_COLS} 列")

    # 被引用的列比上限还多：一律保留
    cited = "".join(f"[[v:Q1.r1.{c}]]、" for c in WIDE_COLS[:-1])
    doc = compose_doc(f"这一行各列依次是 {cited}都比上周高。", catalog)
    shown, header = header_of(prepare(doc, catalog).excerpts["Q1"])
    assert set(shown) == set(WIDE_COLS[:-1]) and "另有 1 列" in header


async def test_current_masks_apply_when_the_entry_does_not_name_its_source(stored):
    """旧形状的台账条目没记数据源：按快照自己记的数据源找遮罩（和证据面板同一个口径），摘录里照样不给。"""
    catalog = query_catalog(stored["snap"], SNAP["columns"], source=None)
    doc = compose_doc("整体看华东领先。[[see:Q1]]", catalog)
    excerpt = prepare(doc, catalog, masked={"shop": ["phone"]}).excerpts["Q1"]
    assert "PHONE-" not in excerpt and "区域0" in excerpt and "数据源 shop" in excerpt

    # 数据源现在设的遮罩也按快照里的数据源去查
    from app.db.models import DataSource

    catalog = query_catalog(_store({**SNAP, "source": "judge_mask_src"}), SNAP["columns"], source=None)
    async with SessionLocal() as session:
        session.add(DataSource(name="judge_mask_src", kind="sqlite", database="unused.db",
                               options={"mask_columns": "Phone"}))
        await session.commit()
    try:
        assert await source_masks(catalog) == {"judge_mask_src": ["Phone"]}
    finally:
        async with SessionLocal() as session:
            await session.execute(delete(DataSource).where(DataSource.name == "judge_mask_src"))
            await session.commit()


# --------------------------------------------------------------------------
# 预算
# --------------------------------------------------------------------------


async def test_claims_over_max_claims_are_unjudged(monkeypatch, stored):
    judge = Judge(monkeypatch)
    catalog = make_catalog(stored)
    doc = compose_doc(RAW, catalog)
    outcome = await judge_doc(doc, catalog, spec=SPEC,
                              budget=Budget(max_claims=2, max_cost_usd=None, timeout_s=None, daily_max_usd=None))
    ids = units_of(doc)
    assert sorted(judge.judged[0]) == sorted([ids[CITED], ids[CITED2]]), "先判挂了依据的"
    for text in (CAUSAL, NUMBER):
        v = status_of(outcome, doc, text)
        assert (v["status"], v["reason"]) == ("unjudged", "max_claims") and "已达上限" in v["rationale"]
    assert outcome["limits_hit"] == ["max_claims"] and outcome["unjudged"] == {"max_claims": 2}
    assert outcome["gaps"] and "已达上限" in outcome["gaps"][0]


async def test_cost_truncation_follows_priority(monkeypatch, stored):
    catalog = make_catalog(stored)
    doc = compose_doc(RAW, catalog)
    request = prepare(doc, catalog)
    by_text = {t: c for c in request.cands for t, u in units_of(doc).items() if u == c.unit}
    without_p3 = request.cost(PRICED, [by_text[CITED], by_text[CITED2], by_text[CAUSAL]])
    everything = request.cost(PRICED, request.cands)
    assert 0 < without_p3 < everything

    judge = Judge(monkeypatch)
    budget = Budget(max_claims=None, max_cost_usd=(without_p3 + everything) / 2, timeout_s=None, daily_max_usd=None)
    outcome = await judge_doc(doc, catalog, spec=SPEC, budget=budget)
    assert status_of(outcome, doc, NUMBER)["reason"] == "max_cost_usd"
    assert all(status_of(outcome, doc, t)["status"] == "supported" for t in (CITED, CITED2, CAUSAL))
    assert outcome["limits_hit"] == ["max_cost_usd"] and len(judge.judged[0]) == 3

    cited_only = request.cost(PRICED, [by_text[CITED], by_text[CITED2]])
    judge = Judge(monkeypatch)
    budget = Budget(max_claims=None, max_cost_usd=cited_only * 1.001, timeout_s=None, daily_max_usd=None)
    outcome = await judge_doc(doc, catalog, spec=SPEC, budget=budget)
    assert {status_of(outcome, doc, t)["reason"] for t in (CAUSAL, NUMBER)} == {"max_cost_usd"}
    assert {status_of(outcome, doc, t)["status"] for t in (CITED, CITED2)} == {"supported"}


async def test_unlimited_never_truncates(monkeypatch, stored):
    """每一项都写 null：句数再多也全判（按每批上限分批），金额、时长、每日都不拦。"""
    judge = Judge(monkeypatch)
    catalog = make_catalog(stored)
    raw = "".join(f"第 {i} 个区域的销售额增长来自新客。[[see:m:gmv]]" for i in range(BATCH_SIZE + 5))
    doc = compose_doc(raw, catalog)
    async with SessionLocal() as session:          # 今天已经花掉很多：不限就是不限
        session.add(Setting(key=SPEND_KEY, value={"date": judge_mod._today(), "usd": 999.0, "calls": 9}))
        await session.commit()
    outcome = await judge_doc(doc, catalog, spec=SPEC, budget=UNLIMITED)
    assert [len(units) for units in judge.judged] == [BATCH_SIZE, 5]
    assert outcome["counts"]["supported"] == BATCH_SIZE + 5 and outcome["counts"]["unjudged"] == 0
    assert outcome["limits_hit"] == [] and outcome["gaps"] == []


async def test_the_daily_cap_resets_the_next_day(monkeypatch, stored):
    catalog = make_catalog(stored)
    doc = compose_doc(RAW, catalog)
    cost = prepare(doc, catalog).cost(PRICED, prepare(doc, catalog).cands)
    budget = Budget(max_claims=None, max_cost_usd=None, timeout_s=None, daily_max_usd=cost * 1.05)
    day = {"today": "2026-09-28"}
    monkeypatch.setattr(judge_mod, "_today", lambda: day["today"])

    judge = Judge(monkeypatch)
    first = await judge_doc(doc, catalog, spec=SPEC, budget=budget)
    assert first["counts"]["unjudged"] == 0
    spent = await daily_spend()
    assert spent["date"] == "2026-09-28" and spent["usd"] == pytest.approx(cost) and spent["calls"] == 1

    second = await judge_doc(doc, catalog, spec=SPEC, budget=budget)
    assert {v["reason"] for v in second["verdicts"].values()} == {"daily_max_usd"}
    assert second["limits_hit"] == ["daily_max_usd"] and len(judge.calls) == 1, "触顶就不再调用"

    day["today"] = "2026-09-29"
    third = await judge_doc(doc, catalog, spec=SPEC, budget=budget)
    assert third["counts"]["unjudged"] == 0 and len(judge.calls) == 2
    assert (await daily_spend())["date"] == "2026-09-29"


async def test_the_time_limit_stops_a_slow_judge(monkeypatch, stored):
    Judge(monkeypatch, latency=2.0)
    catalog = make_catalog(stored)
    doc = compose_doc(RAW, catalog)
    outcome = await judge_doc(doc, catalog, spec=SPEC,
                              budget=Budget(max_claims=None, max_cost_usd=None, timeout_s=0.2, daily_max_usd=None))
    assert {v["reason"] for v in outcome["verdicts"].values()} == {"timeout_s"}
    assert outcome["limits_hit"] == ["timeout_s"] and outcome["gaps"]


async def test_a_model_without_a_price_ignores_money_limits(monkeypatch, stored):
    """目录里没有价格的模型估不出金额：金额上限对它不起作用，并照实说「按令牌估不出金额」。"""
    judge = Judge(monkeypatch, model_id="house-judge-model")
    catalog = make_catalog(stored)
    doc = compose_doc(RAW, catalog)
    events: list = []
    outcome = await judge_doc(doc, catalog, spec=ModelSpec(model="house-judge-model"),
                              budget=Budget(max_claims=None, max_cost_usd=1e-9, timeout_s=None, daily_max_usd=1e-9),
                              emit=lambda kind, **data: events.append((str(kind), data)))
    assert len(judge.calls) == 1 and outcome["counts"]["unjudged"] == 0
    assert outcome["priced"] is False and any("无法按 token 数估算金额" in n for n in outcome["notes"])
    [log] = [d for k, d in events if k == EventType.LOG and d.get("code") == "judge_unpriced"]
    assert "无法按 token 数估算金额" in log["message"]


# --------------------------------------------------------------------------
# 复用与按需
# --------------------------------------------------------------------------


async def test_known_sentences_are_not_judged_again(monkeypatch, stored):
    judge = Judge(monkeypatch, {"u2": "contradicted"})
    catalog = make_catalog(stored)
    doc = compose_doc(RAW, catalog)
    first = await judge_doc(doc, catalog, spec=SPEC, budget=UNLIMITED)
    known = {first["keys"][u]: v for u, v in first["verdicts"].items()}
    again = await judge_doc(compose_doc(RAW, catalog), catalog, spec=SPEC, budget=UNLIMITED, known=known)
    assert len(judge.calls) == 1 and again["reused"] == 4 and again["calls"] == 0
    assert again["verdicts"] == first["verdicts"]


async def test_on_demand_judges_just_the_requested_sentence_after_the_seal(monkeypatch, stored):
    judge = Judge(monkeypatch)
    catalog = make_catalog(stored)
    doc = compose_doc(RAW + "共 3 单。", catalog)
    ids = units_of(doc)
    outcome = await judge_doc(doc, catalog, spec=SPEC, budget=UNLIMITED, post_seal=True,
                              units=[ids[CAUSAL], ids["共 3 单。"], ids["本周概览"]])
    assert sorted(judge.judged[0]) == sorted([ids[CAUSAL], ids["共 3 单。"]]), "点名要判的短句照判"
    assert all(v["post_seal"] is True for v in outcome["verdicts"].values())
    assert outcome["skipped"] == {ids["本周概览"]: "not_a_claim"}
