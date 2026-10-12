"""值取自遮罩格的口径卡指标：交给写作者的目录、存进报告文档的证据目录里写「已遮罩」，引用它的报告自查记 masked_cell。

验收第二轮发现：口径卡 phone_raw = cell(nodes.q_cust, 0, 'phone')（phone 设了遮罩），写作要求点名 [[m:phone_raw]]，报告自查
判 ok、violations 为空，正文写出号码；证据面板的指标步骤写着「已遮罩」，同一步的 rendered、片段的字、引用的值却都是号码。
报告自查以前只查 kind 为 cell 的引用；写作目录、文档的证据目录收着上游口径卡的全部指标（没被引用的也收），值和渲染原样写着。

现在：
- 指标的值给不给，和证据面板指标步骤的值、推演的原值同一个判断（api/evidence.py 的 _metric_masks → recompute.withheld_value），
  报告撰写节点写作之前按运行状态认好（withheld_metrics）。判「已遮罩」的那几个指标，目录条目的值、渲染、标签都写「已遮罩」
  （withhold_metrics），写作目录里另说不要引用；数值型的普通指标照旧
- 引用这类指标照样解析、正文写「已遮罩」，报告自查记一条 masked_cell（masked_metrics），和引用遮罩格同一套处置：交回重写，
  仍不改按「违规处置」办；出口契约复核按节点产出里记下的同一份重建目录，「已遮罩」对得上，不算文档被改过
- 证据面板点开这一处：值、渲染都是「已遮罩」，渲染一致

真跑的部分用虚构的 SQLite 会员库（号码是编的），写作者是 MockChatModel 照剧本写。
"""
from __future__ import annotations

import asyncio
import json
import sqlite3

import pytest
from httpx import ASGITransport, AsyncClient
from langchain_core.messages import AIMessage
from sqlalchemy import select

from app.core import artifact_store
from app.data.engine import engines
from app.db.base import SessionLocal
from app.db.models import DataSource, Run, RunEvent
from app.engine.evidence import build_catalog, compose_doc, iter_segments, verify_doc, withhold_metrics
from app.engine.runner import run_manager
from app.main import app

CUSTOMERS = [(1, "客户甲", "19900000801"), (2, "客户乙", "19900000802"), (3, "客户丙", "19900000803")]
PHONES = [c[2] for c in CUSTOMERS]
METRICS = [
    {"id": "phone_raw", "name": "首位客户手机号", "format": "plain", "expression": "cell(nodes.q_cust, 0, 'phone')"},
    # 改了名字的遮罩列：SELECT id, phone AS tel
    {"id": "tel_raw", "name": "首位客户联系电话", "format": "plain", "expression": "cell(nodes.q_tel, 0, 'tel')"},
    {"id": "first_id", "name": "首位客户编号", "format": "plain", "expression": "cell(nodes.q_cust, 0, 'id')"},
    # 整份结果当输入、只数行数：值里没有号码，照给（和证据面板、推演一样）
    {"id": "n_cust", "name": "客户数", "unit": "位", "format": "thousands", "expression": "len(vars.cust.rows)"},
]
CLEAN = "共有 [[m:n_cust]] 客户，首位客户编号 [[m:first_id]]。"
CITES = "首位客户编号 [[m:first_id]]，手机号 [[m:phone_raw]]。"


@pytest.fixture
async def engine_up():
    await run_manager.setup()
    yield
    await run_manager.shutdown()


@pytest.fixture
async def client(engine_up):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def set_source(path, options=None) -> str:
    async with SessionLocal() as session:
        row = (await session.execute(select(DataSource).where(DataSource.name == "vip"))).scalar_one_or_none()
        if row is None:
            row = DataSource(name="vip", kind="sqlite", database=str(path), readonly=True, enabled=True)
            session.add(row)
        row.database, row.kind, row.enabled, row.readonly = str(path), "sqlite", True, True
        row.options = dict(options or {})
        await session.commit()
        return row.id


@pytest.fixture
async def vip(tmp_path, engine_up):
    path = tmp_path / "vip.db"
    db = sqlite3.connect(path)
    db.executescript("CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT, phone TEXT);")
    db.executemany("INSERT INTO customers VALUES (?, ?, ?)", CUSTOMERS)
    db.commit()
    db.close()
    source_id = await set_source(path, {"mask_columns": ["phone"]})
    await engines.invalidate(source_id)
    yield path
    await engines.invalidate(source_id)


def node(nid, ntype, **config):
    return {"id": nid, "type": ntype, "position": {"x": 0, "y": 0}, "data": {"label": nid, "config": config}}


def graph(*, contract: dict | None = None, report: bool = True, metrics: list | None = None, **write) -> dict:
    """会员查询（选出号码）、改了名字的号码查询 → 数据整形 → 口径卡（metrics，默认 METRICS）→ 报告（report 为假时没有）→ 成果。"""
    out = {"fields": [{"name": "报告", "value": "{{ nodes.write.text }}"}] if report else [{"name": "说明", "value": "完成"}]}
    if contract is not None:
        out["contract"] = contract
    nodes = [
        node("start", "input"),
        node("q_cust", "tool", tool="db_query__vip", args={"sql": "SELECT id, name, phone FROM customers ORDER BY id"}),
        node("q_tel", "tool", tool="db_query__vip", args={"sql": "SELECT id, phone AS tel FROM customers ORDER BY id"}),
        node("parse", "transform", mode="json", template="{{ nodes.q_cust }}", assign_to="cust"),
        node("card", "metrics", caliber="会员口径", caliber_version="v1", metrics=metrics or METRICS),
        *([node("write", "report", instructions="写一句，手机号写 [[m:phone_raw]]", **write)] if report else []),
        node("out", "output", **out),
    ]
    return {"nodes": nodes, "edges": [{"source": a["id"], "target": b["id"]} for a, b in zip(nodes, nodes[1:])]}


def script(monkeypatch, *replies) -> list[list]:
    """写作者依次回这几段，用完了一直回最后一段。返回每次调用收到的消息。"""
    from app.providers import mock_model

    seen: list[list] = []

    def _decide(self, messages):
        seen.append(list(messages))
        return AIMessage(content=replies[min(len(seen), len(replies)) - 1])

    monkeypatch.setattr(mock_model.MockChatModel, "_decide", _decide)
    return seen


async def finish(spec: dict) -> Run:
    run_id = (await run_manager.start(graph=spec, input_payload={})).id
    for _ in range(400):
        await asyncio.sleep(0.05)
        async with SessionLocal() as session:
            row = await session.get(Run, run_id)
        if row.status in ("succeeded", "failed") and row.manifest_seq is not None:
            return row
    raise AssertionError(f"运行 {run_id} 没跑完：{row.status}")


async def events(run_id: str) -> list[RunEvent]:
    async with SessionLocal() as session:
        q = select(RunEvent).where(RunEvent.run_id == run_id).order_by(RunEvent.seq)
        return list((await session.execute(q)).scalars())


def leaks(value) -> list[str]:
    """写着的号码，按千分位写的也算。"""
    raw = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return [p for p in PHONES if p in raw or p in raw.replace(",", "")]


async def doc_of(run_id: str) -> dict:
    [checked] = [e.data for e in await events(run_id) if e.type == "report.checked"][-1:]
    return artifact_store.load(checked["doc_artifact"])


def prompt_of(messages: list) -> str:
    return "\n".join(str(m.content) for m in messages)


def metric_lines(messages: list) -> str:
    """写作目录里口径卡指标的那几行。查询结果的行照旧原样列出（写作者要看得到数据，引用遮罩格由自查拦下），不在这里看。"""
    return "\n".join(line for line in prompt_of(messages).splitlines() if line.startswith("- [[m:"))


# --------------------------------------------------------------------------
# 目录：组装文档的纯函数
# --------------------------------------------------------------------------


def card_catalog() -> dict:
    card = {"kind": "metric_set", "caliber": "会员口径", "caliber_version": "v1", "artifact": "c" * 64, "metrics": [
        {"id": "phone_raw", "name": "首位客户手机号", "value": PHONES[0], "format": "plain"},
        {"id": "first_id", "name": "首位客户编号", "value": 1, "format": "plain"},
        {"id": "gmv", "name": "销售额", "value": 74613.65, "unit": "元", "decimals": 2, "format": "thousands"}]}
    return build_catalog(nodes={"card": card}, ledger=[])


def test_a_withheld_metric_shows_masked_everywhere_in_the_catalog():
    """值、渲染、标签都写「已遮罩」，另记 masked；别的指标一字不差。没被引用的也一样（目录收着全部指标）。"""
    raw = card_catalog()
    catalog = withhold_metrics(raw, ["m:phone_raw"], "已遮罩")
    entry = catalog["m:phone_raw"]
    assert (entry["value"], entry["rendered"], entry["label"], entry["masked"]) == \
        ("已遮罩", "已遮罩", "首位客户手机号 = 已遮罩", True)
    assert catalog["m:first_id"] == raw["m:first_id"] and catalog["m:gmv"] == raw["m:gmv"]
    assert raw["m:phone_raw"]["value"] == PHONES[0], "原来的目录不跟着改"
    doc = compose_doc(CLEAN.replace("[[m:n_cust]]", "[[m:gmv]]"), catalog, node_id="write", run_id="r1")
    assert doc["catalog"]["m:phone_raw"]["masked"] is True and leaks(doc) == []


@pytest.mark.parametrize("ref", ["m:phone_raw", "m:phone_raw|万", "m:phone_raw|int"])
def test_citing_a_withheld_metric_is_a_masked_cell_violation_without_its_value(ref):
    """引用它照样解析（换算不再算：「已遮罩」不是数），正文写「已遮罩」，自查记 masked_cell，说法里不写值；违规点名引用、
    指标，写作者知道要去掉哪一处。出口契约复核拿同一份目录重建，渲染对得上，不算文档被改过。"""
    catalog = withhold_metrics(card_catalog(), ["m:phone_raw"], "已遮罩")
    doc = compose_doc(f"首位客户编号 [[m:first_id]]，手机号 [[{ref}]]。", catalog, node_id="write", run_id="r1")
    seg = next(s for s in iter_segments(doc) if s.get("ref") == ref)
    assert seg["cite"]["status"] == "resolved" and seg["text"] == "已遮罩" and seg["cite"]["value"] == "已遮罩", seg
    [v] = [v for v in doc["violations"] if v["code"] == "masked_cell"]
    assert (v["ref"], v["metric"], v["segment"]) == (ref, "phone_raw", seg["id"]) and v["unit"] and v["span"], v
    assert "首位客户手机号" in v["message"] and "遮罩" in v["message"] and "删掉" in v["for_model"], v
    assert "text" not in v and "context" not in v and leaks(doc) == []
    assert not [x for x in doc["violations"] if x["code"] != "masked_cell"], doc["violations"]
    again = verify_doc(doc, catalog)
    assert again["ok"] is True, again["violations"]


def test_a_catalog_without_withheld_metrics_is_unchanged():
    raw = card_catalog()
    assert withhold_metrics(raw, [], "已遮罩") == raw
    doc = compose_doc("手机号 [[m:phone_raw]]。", raw, node_id="write", run_id="r1")
    assert not [v for v in doc["violations"] if v["code"] == "masked_cell"]


# --------------------------------------------------------------------------
# 真跑：写作目录、文档的证据目录
# --------------------------------------------------------------------------


async def test_the_writer_and_the_doc_see_masked_for_metrics_from_masked_cells(monkeypatch, vip):
    """写作目录里 phone_raw、tel_raw（改了名字的遮罩列）写「已遮罩」并说不要引用，号码一个都不出现；客户数、编号照给。
    文档的证据目录里没被引用的 phone_raw、tel_raw 同样写「已遮罩」，整份文档没有号码。"""
    seen = script(monkeypatch, CLEAN)
    row = await finish(graph(on_violation="flag"))
    assert row.status == "succeeded", row.error
    lines = metric_lines(seen[0])
    assert "首位客户手机号 = 已遮罩（取自设置了遮罩的列，不要引用）" in lines, lines
    assert "首位客户联系电话 = 已遮罩（取自设置了遮罩的列，不要引用）" in lines and leaks(lines) == [], lines
    assert "客户数 = 3位" in lines and "首位客户编号 = 1" in lines, lines
    doc = await doc_of(row.id)
    for alias, name in (("m:phone_raw", "首位客户手机号"), ("m:tel_raw", "首位客户联系电话")):
        entry = doc["catalog"][alias]
        assert (entry["value"], entry["rendered"], entry["label"], entry.get("masked")) == \
            ("已遮罩", "已遮罩", f"{name} = 已遮罩", True), entry
    assert doc["catalog"]["m:first_id"]["value"] == 1 and doc["catalog"]["m:n_cust"]["value"] == 3
    assert "masked" not in doc["catalog"]["m:first_id"] and "masked" not in doc["catalog"]["m:n_cust"]
    assert leaks(doc) == []
    finished = next(e.data for e in await events(row.id) if e.type == "node.finished" and e.node_id == "write")
    assert artifact_store.load(finished["artifact"])["masked_metrics"] == ["m:phone_raw", "m:tel_raw"]


async def test_without_masks_metrics_are_given_as_before(monkeypatch, vip):
    await set_source(vip, {})
    seen = script(monkeypatch, CITES)
    row = await finish(graph(on_violation="flag"))
    assert row.status == "succeeded", row.error
    assert f"首位客户手机号 = {PHONES[0]}" in metric_lines(seen[0])
    doc = await doc_of(row.id)
    entry = doc["catalog"]["m:phone_raw"]
    assert str(entry["value"]) == PHONES[0] and "masked" not in entry, entry
    finished = next(e.data for e in await events(row.id) if e.type == "node.finished" and e.node_id == "write")
    assert "masked_metrics" not in artifact_store.load(finished["artifact"])
    [checked] = [e.data for e in await events(row.id) if e.type == "report.checked"]
    assert checked["ok"] is True and checked["repairs"] == 0


# --------------------------------------------------------------------------
# 真跑：报告自查交回重写、按违规处置办
# --------------------------------------------------------------------------


async def test_citing_the_metric_is_sent_back_and_the_rewrite_passes(monkeypatch, vip):
    """第一稿引用了 [[m:phone_raw]]：自查记 masked_cell、交回重写，重写稿不引用就通过。重写请求、日志里不写号码。"""
    seen = script(monkeypatch, CITES, CLEAN)
    row = await finish(graph(on_violation="flag"))
    assert row.status == "succeeded", row.error
    rows = await events(row.id)
    [checked] = [e.data for e in rows if e.type == "report.checked"]
    assert checked["ok"] is True and checked["repairs"] == 1, checked
    repair = str(seen[1][-1].content)
    assert "m:phone_raw" in repair and "遮罩" in repair and leaks(repair) == [], repair
    [log] = [e.data for e in rows if e.type == "log" and e.data.get("code") == "report_repair"]
    assert "m:phone_raw" in log["message"] and leaks(log) == []


async def test_a_writer_that_insists_is_flagged_without_the_value(monkeypatch, vip):
    """重写后还引用：「违规处置」为标注时照常产出，正文写「已遮罩」；自查结果、时间线、出具缺口都说清，都不写号码；出口
    契约复核不说文档被改过（按节点产出里记下的同一份重建目录）。"""
    script(monkeypatch, CITES)
    row = await finish(graph(on_violation="flag", contract={"report_from": "write"}))
    assert row.status == "succeeded", row.error
    rows = await events(row.id)
    [checked] = [e.data for e in rows if e.type == "report.checked"]
    assert checked["ok"] is False and checked["repairs"] == 1 and not checked["failed"]
    [v] = [v for v in checked["violations"] if v["code"] == "masked_cell"]
    assert v["ref"] == "m:phone_raw" and v["metric"] == "phone_raw" and leaks(v) == []
    doc = await doc_of(row.id)
    assert "手机号 已遮罩" in doc["markdown"] and leaks(doc) == [], doc["markdown"]
    [log] = [e.data for e in rows if e.type == "log" and e.data.get("code") == "report_masked_cell"]
    assert log["refs"] == ["m:phone_raw"] and "指标" in log["message"] and leaks(log) == [], log
    finished = next(e.data for e in rows if e.type == "node.finished" and e.node_id == "write")
    [gap] = artifact_store.load(finished["artifact"])["issuance_gaps"]
    assert "m:phone_raw" in gap and "指标" in gap and leaks(gap) == [], gap
    issuance = row.output["_issuance"]
    assert issuance["tier"] == "degraded", issuance
    assert not [g for g in issuance["gaps"] if "未通过复核" in g], issuance["gaps"]
    assert any("遮罩" in g for g in issuance["gaps"]) and leaks(issuance) == []
    assert leaks(row.output) == []


async def test_a_writer_that_insists_fails_the_node_when_violations_fail(monkeypatch, vip):
    script(monkeypatch, CITES)
    row = await finish(graph(on_violation="fail"))
    assert row.status == "failed" and "遮罩" in (row.error or "") and "m:phone_raw" in row.error, row.error
    assert "指令中去掉" in row.error and "指令遵循能力" not in row.error and leaks(row.error) == []


async def test_the_evidence_panel_says_masked_and_renders_consistently(client, monkeypatch, vip):
    """证据面板点开引用 [[m:phone_raw]] 的那一处：值、渲染都是「已遮罩」，渲染一致（不报「渲染不一致」）；整份答复没有号码。"""
    script(monkeypatch, CITES)
    row = await finish(graph(on_violation="flag"))
    assert row.status == "succeeded", row.error
    doc = await doc_of(row.id)
    seg = next(s for s in iter_segments(doc) if s.get("ref") == "m:phone_raw")
    res = await client.get(f"/api/runs/{row.id}/evidence/segments/{seg['id']}")
    assert res.status_code == 200, res.text
    body = res.json()
    step = body["chain"][0]
    assert (step["value"], step["rendered"], step["render_ok"]) == ("已遮罩", "已遮罩", True), step
    assert leaks(body) == []
    graph_body = (await client.get(f"/api/runs/{row.id}/evidence")).json()
    labels = {e["alias"]: e["label"] for e in graph_body["evidence"] if e["kind"] == "metric"}
    assert labels["m:phone_raw"] == "首位客户手机号 = 已遮罩" and leaks(graph_body) == []


async def test_the_panel_follows_the_doc_when_it_says_a_metric_is_withheld(client, monkeypatch, vip):
    """文档的证据目录里记着这个指标的值不给（写作时认出来的），面板这时却认不出（数据源事后撤了遮罩这类，这里拿客户编号
    装作是）：照文档不给，值写「已遮罩」；渲染按目录里那个字比。没记的照旧给值。"""
    from app.api.evidence import _Masks, _metric_chain, _sealed

    script(monkeypatch, CLEAN)
    row = await finish(graph(on_violation="flag"))
    assert row.status == "succeeded", row.error
    doc = await doc_of(row.id)
    sealed = await _sealed(row.id)
    seg = next(s for s in iter_segments(doc) if s.get("ref") == "m:first_id")
    entry = doc["catalog"]["m:first_id"]
    [step, *_] = await _metric_chain(seg, seg["cite"], entry, doc, sealed, _Masks(sealed))
    assert step["value"] == 1 and step["render_ok"] is True, step
    masked = {**entry, "value": "已遮罩", "rendered": "已遮罩", "masked": True}
    [step, *_] = await _metric_chain({**seg, "text": "已遮罩"}, seg["cite"], masked, doc, sealed, _Masks(sealed))
    assert step["value"] == "已遮罩" and step["render_ok"] is True, step


async def test_a_report_in_a_loop_judges_its_masks_afresh_each_time(monkeypatch, vip):
    """报告撰写节点在循环里跑两次：第一次认出 phone_raw 不给、记进产出，第二次（这里假装数据源撤了遮罩）认不出。第二次建目录
    不能沿用第一次产出里记下的那一份：沿用了，正文写「已遮罩」、产出里却没记，出口契约复核按产出重建目录，对不上就说文档
    被改过。"""
    import app.api.evidence as evidence_api

    calls: list[list[str]] = []
    real = evidence_api.withheld_metrics

    async def first_only(catalog, **kwargs):
        got = await real(catalog, **kwargs)
        calls.append(got)
        return got if len(calls) == 1 else []

    monkeypatch.setattr(evidence_api, "withheld_metrics", first_only)
    script(monkeypatch, CITES)
    base = graph(on_violation="flag", contract={"report_from": "write"})
    nodes = {n["id"]: n for n in base["nodes"]}
    lp = node("lp", "loop", mode="foreach", items="[1, 2]")
    spec = {"nodes": [*base["nodes"][:5], lp, nodes["write"], nodes["out"]],
            "edges": [*[{"source": a["id"], "target": b["id"]} for a, b in zip(base["nodes"][:5], base["nodes"][1:5])],
                      {"source": "card", "target": "lp"},
                      {"source": "lp", "target": "write", "sourceHandle": "body"},
                      {"source": "write", "target": "lp"},
                      {"source": "lp", "target": "out", "sourceHandle": "done"}]}
    row = await finish(spec)
    assert row.status == "succeeded", row.error
    assert len(calls) == 2 and calls[0] == ["m:phone_raw", "m:tel_raw"], calls
    checks = [e.data for e in await events(row.id) if e.type == "report.checked"]
    assert len(checks) == 2, checks
    doc = await doc_of(row.id)
    assert "已遮罩" not in doc["markdown"] and not any(e.get("masked") for e in doc["catalog"].values())
    gaps = row.output["_issuance"]["gaps"]
    assert not [g for g in gaps if "未通过复核" in g], gaps
