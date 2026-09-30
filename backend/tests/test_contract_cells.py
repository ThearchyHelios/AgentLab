"""报告直接引用查询单元格 [[v:Qn.r<行>.<列>]]：受管级别的正式运行要在契约里显式声明 cells。

用户拍板：探索运行和已发布级别允许直接引用单元格；受管级别的正式运行，契约里没写
"cells": true 的，单元格引用按 unresolved 计，原因写「受管出具要在契约里声明 cells」。

- 出口复核（io.py）按这条规则独立判，不信报告节点的自查
- 报告节点写作时也照这条规则建目录：不允许时提示里只给 [[see:Qn]]，写了单元格当场就被打回，
  不必等到出口才发现
- 旧 narrative 契约的 matched 里，单元格引用的数字记成 cell: "Q1.r0.gmv"，不是 input: None
"""
from __future__ import annotations

import asyncio
import sqlite3

import pytest
from httpx import ASGITransport, AsyncClient
from langchain_core.messages import AIMessage
from sqlalchemy import select

from app.data.engine import engines
from app.db.base import SessionLocal
from app.db.models import DataSource, Run, RunEvent, Workflow, WorkflowVersion
from app.engine.evidence import CELLS_REASON
from app.engine.runner import run_manager
from app.main import app

CELL_TEXT = "本周销售额 [[v:Q1.r0.gmv]]，按口径卡是 [[m:gmv]]。"


@pytest.fixture(autouse=True)
async def engine_up():
    await run_manager.setup()
    yield
    await run_manager.shutdown()


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest.fixture(autouse=True)
async def shop(tmp_path):
    path = tmp_path / "shop.db"
    db = sqlite3.connect(path)
    db.executescript("CREATE TABLE orders (id INTEGER PRIMARY KEY, week TEXT, amount REAL);")
    db.executemany("INSERT INTO orders VALUES (?, ?, ?)", [(1, "2026-W37", 100.5), (2, "2026-W37", 200.0)])
    db.commit()
    db.close()
    async with SessionLocal() as session:
        row = (await session.execute(select(DataSource).where(DataSource.name == "shop"))).scalar_one_or_none()
        if row is None:
            row = DataSource(name="shop", kind="sqlite", database=str(path), readonly=True, enabled=True)
            session.add(row)
        else:
            row.database, row.kind, row.enabled, row.readonly, row.options = str(path), "sqlite", True, True, {}
        await session.commit()
        source_id = row.id
    await engines.invalidate(source_id)
    yield
    await engines.invalidate(source_id)


def node(nid, ntype, **config):
    return {"id": nid, "type": ntype, "position": {"x": 0, "y": 0}, "data": {"label": nid, "config": config}}


def weekly(*, cells=None, on_violation="flag"):
    contract = {"report_from": "write", "metrics_from": ["card"], "required": ["gmv"], "strict": True}
    if cells is not None:
        contract["cells"] = cells
    nodes = [
        node("start", "input"),
        node("fetch", "tool", tool="db_query__shop",
             args={"sql": "SELECT SUM(amount) AS gmv, COUNT(*) AS orders FROM orders"}),
        node("card", "metrics", caliber="周报口径", caliber_version="v2", metrics=[
            {"id": "gmv", "name": "销售额", "unit": "元", "decimals": 1, "format": "thousands",
             "expression": "cell(nodes.fetch, 0, 'gmv')"}]),
        node("write", "report", instructions="写周报", on_violation=on_violation),
        node("out", "output", fields=[{"name": "周报", "value": "{{ nodes.write.text }}"}], contract=contract),
    ]
    return {"nodes": nodes, "edges": [{"source": a["id"], "target": b["id"]} for a, b in zip(nodes, nodes[1:])]}


class Writer:
    """报告写作者：每次都照 text 写；记下每次看到的提示。"""

    def __init__(self, monkeypatch, text=CELL_TEXT):
        from app.providers import mock_model

        self.prompts: list[str] = []

        def decide(model, messages):
            self.prompts.append("\n".join(str(m.content) for m in messages))
            return AIMessage(content=text)

        monkeypatch.setattr(mock_model.MockChatModel, "_decide", decide)


async def wait(run_id: str) -> Run:
    for _ in range(400):
        await asyncio.sleep(0.05)
        async with SessionLocal() as session:
            row = await session.get(Run, run_id)
        if row.status in ("succeeded", "failed") and row.manifest_seq is not None:
            return row
    raise AssertionError(f"run {run_id} 没跑完：{row.status}")


async def formal(client, graph, *, status: str) -> Run:
    """把这张图发布成 status 级别，再从发布版本发起正式运行。"""
    async with SessionLocal() as session:
        wf = Workflow(name=f"周报-{status}", graph=graph, status=status, published_version=1)
        session.add(wf)
        await session.flush()
        session.add(WorkflowVersion(workflow_id=wf.id, version=1, graph=graph))
        await session.commit()
        wf_id = wf.id
    res = await client.post("/api/runs", json={"workflow_id": wf_id, "run_class": "formal"})
    assert res.status_code == 201, res.text
    return await wait(res.json()["id"])


async def checked(run_id: str) -> dict:
    async with SessionLocal() as session:
        [event] = (await session.execute(select(RunEvent).where(
            RunEvent.run_id == run_id, RunEvent.type == "report.checked"))).scalars()
    return event.data


# --------------------------------------------------------------------------
# 出口复核
# --------------------------------------------------------------------------


async def test_governed_formal_run_without_cells_leaves_cell_refs_unresolved(client, monkeypatch):
    Writer(monkeypatch)
    row = await formal(client, weekly(), status="governed")
    assert row.status == "succeeded", row.error
    issuance = row.output["_issuance"]
    assert issuance["mode"] == "citations" and issuance["tier"] == "withheld", issuance
    [unresolved] = issuance["unresolved"]
    assert unresolved["ref"] == "v:Q1.r0.gmv" and CELLS_REASON in unresolved["message"], unresolved
    # 指标照常核对过；单元格那个数不算「有出处」
    assert [m.get("metric") for m in issuance["matched"]] == ["gmv"]


async def test_governed_formal_run_with_cells_declared_passes(client, monkeypatch):
    Writer(monkeypatch)
    row = await formal(client, weekly(cells=True), status="governed")
    assert row.status == "succeeded", row.error
    issuance = row.output["_issuance"]
    assert issuance["tier"] == "formal" and issuance["unresolved"] == [], issuance
    cell = next(m for m in issuance["matched"] if m.get("cell"))
    assert cell["cell"] == "Q1.r0.gmv" and cell["token"] == "300.5" and cell["metric"] is None
    assert "input" not in cell and cell["eid"].startswith("ev:cell:")


@pytest.mark.parametrize("status", ["published"])
async def test_published_formal_runs_allow_cells(client, monkeypatch, status):
    Writer(monkeypatch)
    row = await formal(client, weekly(), status=status)
    assert row.output["_issuance"]["tier"] == "formal", row.output["_issuance"]


async def test_exploratory_runs_are_not_affected(monkeypatch):
    Writer(monkeypatch)
    run = await run_manager.start(graph=weekly(), input_payload={})
    row = await wait(run.id)
    issuance = row.output["_issuance"]
    assert issuance["tier"] == "formal" and issuance["unresolved"] == [], issuance
    assert any(m.get("cell") == "Q1.r0.gmv" for m in issuance["matched"])


# --------------------------------------------------------------------------
# 报告节点写作时就照这条规则
# --------------------------------------------------------------------------


async def test_report_node_refuses_cells_while_writing_for_governed_formal_runs(client, monkeypatch):
    writer = Writer(monkeypatch)
    row = await formal(client, weekly(), status="governed")
    data = await checked(row.id)
    assert data["ok"] is False and data["repairs"] == 1
    assert any(CELLS_REASON in v["message"] for v in data["violations"]), data["violations"]
    # 提示里不再教它写单元格，只给依据写法
    assert "[[v:Q1.r0." not in writer.prompts[0] and "[[see:Q1]]" in writer.prompts[0]


async def test_report_node_with_on_violation_fail_stops_before_the_exit(client, monkeypatch):
    Writer(monkeypatch)
    row = await formal(client, weekly(on_violation="fail"), status="governed")
    assert row.status == "failed" and CELLS_REASON in (row.error or ""), row.error


async def test_report_node_teaches_cells_when_they_are_allowed(client, monkeypatch):
    writer = Writer(monkeypatch)
    row = await formal(client, weekly(cells=True), status="governed")
    data = await checked(row.id)
    assert data["ok"] is True and data["repairs"] == 0, data
    assert "[[v:Q1.r0." in writer.prompts[0]


# --------------------------------------------------------------------------
# report_no_evidence：有任何一种可引用的来源就不报，报的时候说清缺的是哪种
# --------------------------------------------------------------------------


async def no_evidence_logs(run_id: str) -> list[dict]:
    async with SessionLocal() as session:
        rows = (await session.execute(select(RunEvent).where(
            RunEvent.run_id == run_id, RunEvent.type == "log"))).scalars()
        return [e.data for e in rows if e.data.get("code") == "report_no_evidence"]


def chain(*nodes):
    return {"nodes": list(nodes), "edges": [{"source": a["id"], "target": b["id"]} for a, b in zip(nodes, nodes[1:])]}


FETCH = node("fetch", "tool", tool="db_query__shop", args={"sql": "SELECT SUM(amount) AS gmv FROM orders"})
WRITE = node("write", "report", instructions="回答问题", on_violation="flag")
OUT = node("out", "output", fields=[{"name": "答案", "value": "{{ nodes.write.text }}"}])


async def test_a_report_over_queries_only_is_not_warned(monkeypatch):
    """问数据的图（input → 取数 → report）只有查询条目、没有口径卡：单元格就是它的出处，不该报没有证据。"""
    Writer(monkeypatch, "销售额 [[v:Q1.r0.gmv]]。")
    row = await wait((await run_manager.start(graph=chain(node("start", "input"), FETCH, WRITE, OUT),
                                              input_payload={})).id)
    assert row.status == "succeeded", row.error
    assert await no_evidence_logs(row.id) == []


async def test_a_report_with_nothing_upstream_names_every_missing_kind(monkeypatch):
    Writer(monkeypatch, "什么都没有。")
    row = await wait((await run_manager.start(graph=chain(node("start", "input"), WRITE, OUT),
                                              input_payload={})).id)
    [log] = await no_evidence_logs(row.id)
    assert all(kind in log["message"] for kind in ("口径卡指标", "查询结果", "知识库检索", "运行输入")), log


async def test_queries_without_cells_in_a_governed_formal_run_are_named(client, monkeypatch):
    Writer(monkeypatch, "销售额 [[v:Q1.r0.gmv]]。")
    card = node("card", "metrics", caliber="周报口径", metrics=[
        {"id": "gmv", "name": "销售额", "expression": "cell(nodes.fetch, 0, 'gmv')"}])
    out = node("out", "output", fields=[{"name": "答案", "value": "{{ nodes.write.text }}"}],
               contract={"report_from": "write", "metrics_from": ["card"], "required": ["gmv"]})
    row = await formal(client, chain(node("start", "input"), FETCH, WRITE, card, out), status="governed")
    [log] = await no_evidence_logs(row.id)
    assert "只有查询结果" in log["message"] and CELLS_REASON in log["message"], log


# --------------------------------------------------------------------------
# 受管与否在发起时定下来：之后改图、改级别，这次运行都按发起时的算
# --------------------------------------------------------------------------


async def governance_notes(run_id: str) -> list[RunEvent]:
    async with SessionLocal() as session:
        rows = (await session.execute(select(RunEvent).where(
            RunEvent.run_id == run_id, RunEvent.type == "log").order_by(RunEvent.seq))).scalars()
        return [e for e in rows if e.data.get("code") == "run_governance"]


async def set_status(run_id: str, status: str) -> None:
    async with SessionLocal() as session:
        run = await session.get(Run, run_id)
        wf = await session.get(Workflow, run.workflow_id)
        wf.status = status
        await session.commit()


@pytest.mark.parametrize("status, governed, later", [("governed", True, "draft"), ("published", False, "governed")])
async def test_the_governed_decision_is_recorded_at_start_and_does_not_follow_later_edits(
        client, monkeypatch, status, governed, later):
    from app.engine.governance import governed_formal

    Writer(monkeypatch)
    row = await formal(client, weekly(cells=True), status=status)
    [note] = await governance_notes(row.id)
    assert note.data["governed"] is governed and note.data["level"] == "info" and note.node_id is None
    assert note.seq <= row.manifest_seq                  # 在封存范围里
    assert await governed_formal(row.id) is governed
    await set_status(row.id, later)                     # 改图退回草稿 / 事后升成受管
    assert await governed_formal(row.id) is governed


async def test_exploratory_runs_record_nothing(monkeypatch):
    Writer(monkeypatch)
    row = await wait((await run_manager.start(graph=weekly(), input_payload={})).id)
    assert await governance_notes(row.id) == []


async def test_runs_started_before_the_record_existed_follow_the_current_status(client, monkeypatch):
    from app.engine.governance import governed_formal

    Writer(monkeypatch)
    row = await formal(client, weekly(cells=True), status="governed")
    async with SessionLocal() as session:
        for note in await governance_notes(row.id):
            await session.delete(await session.get(RunEvent, note.id))
        await session.commit()
    assert await governed_formal(row.id) is True
    await set_status(row.id, "draft")
    assert await governed_formal(row.id) is False


async def test_the_report_node_and_the_exit_check_agree_when_the_status_changes_mid_run(client, monkeypatch):
    """报告节点写作时按受管拒了单元格，跑到出口前工作流被改回草稿：出口仍按受管判，两边说法一致。"""
    from app.engine import governance

    Writer(monkeypatch)
    real, seen = governance.governed_formal, []

    async def edit_after_first_look(run_id):
        result = await real(run_id)
        seen.append(result)
        if len(seen) == 1:
            await set_status(run_id, "draft")
        return result

    monkeypatch.setattr(governance, "governed_formal", edit_after_first_look)
    row = await formal(client, weekly(), status="governed")
    assert seen == [True, True], seen
    issuance = row.output["_issuance"]
    assert issuance["tier"] == "withheld" and CELLS_REASON in issuance["unresolved"][0]["message"], issuance


# --------------------------------------------------------------------------
# 只引单元格、没有口径卡：契约不用写 metrics_from
#
# 问数据的典型图是 input → 取数 → 报告 → 出口，报告直接引用查过的单元格。受管级别在契约里
# 写 cells: true 就够了（用户拍板的第 5 条），以前校验和门禁无条件要 metrics_from，
# 这张图怎么都发不成受管，Copilot 补上契约也会被判「冒出新的问题」作废
# --------------------------------------------------------------------------


def cells_only(**contract):
    contract = {"report_from": "write", "strict": True, "cells": True, **contract}
    nodes = [
        node("start", "input"),
        node("fetch", "tool", tool="db_query__shop",
             args={"sql": "SELECT SUM(amount) AS gmv, COUNT(*) AS orders FROM orders"}),
        node("write", "report", instructions="写周报", numbers="strict", on_violation="fail",
             claims="require_citation"),
        node("out", "output", fields=[{"name": "周报", "value": "{{ nodes.write.text }}"}], contract=contract),
    ]
    return {"nodes": nodes, "edges": [{"source": a["id"], "target": b["id"]} for a, b in zip(nodes, nodes[1:])]}


def contract_codes(graph: dict, level: str = "governed") -> tuple[list[str], list[str]]:
    from app.engine.governance import lint_for_publish
    from app.engine.schema import GraphSpec, validate_graph

    spec = GraphSpec.model_validate(graph)
    return ([i.code for i in validate_graph(spec).issues if i.level == "error"],
            [i.code for i in lint_for_publish(spec, level=level).issues if i.level == "error"])


def test_a_cells_only_contract_needs_no_metrics_from():
    validate, gate = contract_codes(cells_only())
    assert validate == [], validate
    assert gate == [], gate                 # 受管门禁整张图一条 error 都没有


def test_a_cited_contract_that_lists_required_metrics_still_needs_metrics_from():
    validate, gate = contract_codes(cells_only(required=["gmv"]))
    assert "contract.metrics_from_missing" in validate and "contract.metrics_from_missing" in gate
    validate, gate = contract_codes(cells_only(expected=["gmv"]))
    assert "contract.metrics_from_missing" in validate and "contract.metrics_from_missing" in gate


def test_a_narrative_contract_still_needs_metrics_from():
    graph = cells_only(report_from=None, narrative="{{ nodes.write.text }}")
    graph["nodes"][-1]["data"]["config"]["contract"].pop("report_from")
    validate, _ = contract_codes(graph, level="published")     # 受管级别不收 narrative，只看校验
    assert "contract.metrics_from_missing" in validate


async def test_a_governed_formal_run_over_cells_only_issues_formally(client, monkeypatch):
    Writer(monkeypatch, text="本周销售额 [[v:Q1.r0.gmv]]。")
    row = await formal(client, cells_only(), status="governed")
    assert row.status == "succeeded", row.error
    issuance = row.output["_issuance"]
    assert issuance["mode"] == "citations" and issuance["tier"] == "formal", issuance
    assert issuance["unresolved"] == [] and issuance["gaps"] == [], issuance
    [cell] = [m for m in issuance["matched"] if m.get("cell")]
    assert cell["cell"] == "Q1.r0.gmv" and cell["token"] == "300.5"


def test_a_graph_with_a_caliber_card_still_needs_required_even_without_metrics_from():
    graph = weekly(cells=True)
    contract = graph["nodes"][-1]["data"]["config"]["contract"]
    contract.pop("metrics_from"), contract.pop("required")
    validate, gate = contract_codes(graph)
    assert "contract.required_missing" in gate, gate       # 不写 metrics_from 绕不过必需指标
    assert validate == [], validate


# --------------------------------------------------------------------------
# 没有口径卡、又没写 cells: true：受管门禁要拦下，发布弹窗里一键选
#
# 放开 metrics_from / required 之后留下的口子：这种契约以前被 metrics_from 挡着，现在能过门禁，
# 可受管正式运行里单元格引用一律不认，报告撰写节点（numbers strict、on_violation fail）每次都失败。
# 写不写 cells 要作者定（模型不许替人打开），所以修复是 choice：写 cells: true，或交给 Copilot 改成口径卡
# --------------------------------------------------------------------------


def _plan(graph: dict, level: str = "governed") -> dict[str, dict]:
    from app.engine.autofix import plan_fixes
    from app.engine.governance import publish_issues
    from app.engine.schema import GraphSpec

    spec = GraphSpec.model_validate(graph)
    return {f["id"]: f for f in plan_fixes(spec, publish_issues(spec, level=level), level=level)}


def test_a_governed_graph_without_cards_must_declare_cells():
    graph = cells_only()
    graph["nodes"][-1]["data"]["config"]["contract"].pop("cells")
    _, gate = contract_codes(graph)
    assert gate == ["contract.cells_undeclared"], gate
    _, gate = contract_codes(cells_only(cells=False))
    assert gate == ["contract.cells_undeclared"], gate
    _, gate = contract_codes(graph, level="published")      # 已发布级别照样允许直接引用单元格
    assert "contract.cells_undeclared" not in gate


def test_a_graph_with_a_caliber_card_does_not_need_cells():
    _, gate = contract_codes(weekly())                       # 有口径卡：数可以走 [[m:]]，cells 是可选的
    assert "contract.cells_undeclared" not in gate, gate


def test_the_fix_is_a_choice_and_picking_cells_is_not_loosening():
    from app.engine.autofix import HANDOFF, apply_fixes

    graph = cells_only()
    graph["nodes"][-1]["data"]["config"]["contract"].pop("cells")
    fid = "contract.cells_undeclared:out"
    fix = _plan(graph)[fid]
    assert fix["kind"] == "choice" and fix["field"] == "contract.cells"
    assert [o["value"] for o in fix["options"]] == [True, HANDOFF]
    assert [o.get("handoff") is True for o in fix["options"]] == [False, True]
    assert apply_fixes(graph, [fid], level="governed")["applied"] == []      # 不替人选
    out = apply_fixes(graph, [fid], {fid: True}, level="governed")
    assert out["applied"] == [fid] and out["rejected"] == [], out["rejected"]
    assert out["graph"]["nodes"][-1]["data"]["config"]["contract"]["cells"] is True and out["ok"] is True
    out = apply_fixes(graph, [fid], {fid: HANDOFF}, level="governed")
    assert out["handoff"] == [fid] and out["applied"] == []


def test_turning_cells_on_unasked_is_still_loosening():
    from app.engine.autofix import forbidden_changes

    before = cells_only()
    before["nodes"][-1]["data"]["config"]["contract"].pop("cells")
    after = cells_only()
    [why] = forbidden_changes(before, after)
    assert "单元格引用" in why
    assert forbidden_changes(before, after, chosen={("out", "contract.cells")}) == []


def test_only_a_newly_raised_human_choice_error_is_let_through():
    from app.engine.autofix import judge, worse

    no_contract = {"level": "error", "code": "governed.no_contract", "node_id": "out", "message": "没有契约"}
    cells = {"level": "error", "code": "contract.cells_undeclared", "node_id": "out", "field": "contract.cells",
             "message": "要写 cells"}
    other = {"level": "error", "code": "contract.strict_off", "node_id": "out", "message": "strict 关了"}
    assert judge([no_contract], [cells]) is None                  # 补上契约、cells 留给人：算更好
    assert worse([], [cells]) is None
    assert "出现了新的问题" in (judge([no_contract], [other]) or "")  # 别的新 error 照样作废
    assert judge([no_contract], [cells, other]) is not None
    # 原来就有的 cells 照常计数：修它的那一步，改完还在就是没修好
    assert "仍然存在" in (judge([cells], [cells], target=("contract.cells_undeclared", "out")) or "")
    assert judge([cells], [], target=("contract.cells_undeclared", "out")) is None
    assert judge([cells], [cells]) == "修改后错误没有减少"             # 原来就有、改完还在：Copilot 兜底不算修好
