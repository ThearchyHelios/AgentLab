"""代码节点在证据链里的位置：evidence_role 和 code_sha。

沙箱里跑的代码可以是「取数」（读文件、调接口，产出本身就是源数据），也可以是「计算」
（拿上游的数加减乘除）。后者正是叙述层不该有的算术权限，只是换了个地方——所以：

- evidence_role 缺省为 compute，只能写 source / compute
- 产出带 code_sha：实际送进沙箱的那份代码（渲染之后、审批改过之后）的 sha256
- 台账记一条 node_output，工件就是节点产出工件本身
- 报告不能直接引用代码节点的产出：那种数要进口径卡
- 升级前发起的运行一字不改
"""
from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select

from app.core.artifact_store import canonical_json, content_hash
from app.db.base import SessionLocal
from app.db.models import Run, RunEvent
from app.engine import runner as runner_mod
from app.engine.context import NodeContext, NodeError, RunContext
from app.engine.evidence import CODE_REASON, build_catalog, compose_doc
from app.engine.nodes.tools import run_code
from app.engine.runner import run_manager
from app.engine.schema import GraphNode, GraphSpec
from app.sandbox.base import ExecResult

CODE = "print({{ vars.n }} * 2)"


class _FakeSandbox:
    def __init__(self, result: ExecResult) -> None:
        self.result = result
        self.calls: list[str] = []

    async def run(self, code, **kwargs):  # noqa: ANN001, ANN003, ANN201
        self.calls.append(code)
        return self.result


def _ctx(monkeypatch, *, limits=None, stdout="42\n", **config) -> tuple[NodeContext, _FakeSandbox]:
    import app.engine.nodes.tools as tools

    sandbox = _FakeSandbox(ExecResult(stdout=stdout))
    monkeypatch.setattr(tools, "sandbox_manager", sandbox)
    node = GraphNode(id="calc", type="code", data={"label": "计算", "config": {
        "language": "python", "code": CODE, "isolation": "fast", **config}})
    spec = GraphSpec(nodes=[node], edges=[])
    run = RunContext(run_id="r1", thread_id="t1", spec=spec, agent_limits=limits)
    return NodeContext(node=node, run=run), sandbox


def _run(ctx, state=None) -> dict:
    return asyncio.run(run_code(state or {"vars": {"n": 21}}, ctx))


def test_role_defaults_to_compute_and_code_sha_is_of_the_rendered_code(monkeypatch):
    ctx, sandbox = _ctx(monkeypatch, limits={})
    updates = _run(ctx)
    payload = updates["nodes"]["calc"]
    assert sandbox.calls == ["print(21 * 2)"]
    assert payload["code_sha"] == content_hash("print(21 * 2)")
    [entry] = updates["evidence"]
    assert entry == {"kind": "node_output", "node_id": "calc", "exec": 1,
                     "artifact": content_hash(canonical_json(payload)), "code_sha": payload["code_sha"],
                     "role": "compute", "language": "python"}


def test_source_role_is_recorded(monkeypatch):
    ctx, _ = _ctx(monkeypatch, limits={}, evidence_role="source")
    [entry] = _run(ctx)["evidence"]
    assert entry["role"] == "source"


def test_unknown_role_is_refused(monkeypatch):
    ctx, sandbox = _ctx(monkeypatch, limits={}, evidence_role="fetch")
    with pytest.raises(NodeError) as info:
        _run(ctx)
    assert "evidence_role" in str(info.value) and "source" in str(info.value)
    assert sandbox.calls == [], "角色写错了还去跑沙箱"


def test_runs_from_before_the_upgrade_are_untouched(monkeypatch):
    ctx, _ = _ctx(monkeypatch, limits=None, evidence_role="source")
    updates = _run(ctx)
    assert "evidence" not in updates
    assert "code_sha" not in updates["nodes"]["calc"]


def test_code_entries_cannot_be_cited_by_a_report(monkeypatch):
    ctx, _ = _ctx(monkeypatch, limits={}, evidence_role="source")
    updates = _run(ctx)
    catalog = build_catalog(nodes=updates["nodes"], ledger=updates["evidence"])
    assert not [a for a in catalog if a.startswith("Q")]
    doc = compose_doc("总数 [[v:N:calc.text]]。", catalog)
    [violation] = doc["violations"]
    assert violation["code"] == "unresolved_ref" and CODE_REASON in violation["message"]


# --------------------------------------------------------------------------
# 跑一次真的运行：台账里的工件就是 node.finished 里那件节点产出
# --------------------------------------------------------------------------


@pytest.fixture
async def engine_up():
    await run_manager.setup()
    yield
    await run_manager.shutdown()


async def _run_graph(monkeypatch) -> Run:
    import app.engine.nodes.tools as tools

    monkeypatch.setattr(tools, "sandbox_manager", _FakeSandbox(ExecResult(stdout='{"total": 42}\n')))
    graph = {
        "nodes": [
            {"id": "start", "type": "input", "data": {"config": {}}},
            {"id": "calc", "type": "code", "data": {"label": "计算", "config": {
                "language": "python", "code": "print('{\"total\": 42}')", "isolation": "fast"}}},
            {"id": "out", "type": "output", "data": {"config": {"fields": [{"name": "r", "value": "{{ nodes.calc.text }}"}]}}},
        ],
        "edges": [{"source": "start", "target": "calc"}, {"source": "calc", "target": "out"}],
    }
    run = await run_manager.start(graph=graph, input_payload={})
    for _ in range(300):
        await asyncio.sleep(0.05)
        async with SessionLocal() as session:
            row = await session.get(Run, run.id)
        if row.status in ("succeeded", "failed") and row.manifest_seq is not None:
            return row
    raise AssertionError("没跑完")


async def _finished(run_id: str) -> dict:
    async with SessionLocal() as session:
        rows = list((await session.execute(select(RunEvent).where(
            RunEvent.run_id == run_id, RunEvent.type == "node.finished", RunEvent.node_id == "calc"))).scalars())
    return rows[0].data


async def test_ledger_artifact_is_the_node_output_artifact(engine_up, monkeypatch):
    row = await _run_graph(monkeypatch)
    assert row.status == "succeeded", row.error
    finished = await _finished(row.id)
    [entry] = finished["evidence"]
    assert entry["artifact"] == finished["artifact"] and entry["role"] == "compute"


async def test_old_runs_write_no_code_entry(engine_up, monkeypatch):
    async def no_snapshot(session):
        return None

    monkeypatch.setattr(runner_mod, "_agent_limits", no_snapshot)
    row = await _run_graph(monkeypatch)
    assert row.status == "succeeded", row.error
    assert "evidence" not in await _finished(row.id)
