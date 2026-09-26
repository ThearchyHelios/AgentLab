"""校验和报错写给人看：用界面上的叫法，说清原因和下一步，不露 Python 内部名字。

- 分支 case 的 key 用了保留名 default：和兜底出口撞成同一个 id，跑完两条一起点亮，
  看不出实际走了哪条。重复 key、空 key 更糟——那个 case 根本路由不到。
- 门禁文案写「受管模板不允许 supervisor 节点」「approval=never」「该是个 llm 节点」，
  界面上它们叫「多 Agent 协作」「全部自动放行」「模型调用」，报错引用了用户从没见过的词。
- 运行失败的首行写着「工具 db_query__shop 执行失败：_make_query_tool.<locals>._run()
  got an unexpected keyword argument 'query'」，恢复失败写「当前状态是 succeeded」。
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select

from app.db.base import SessionLocal
from app.db.models import Run, RunEvent
from app.engine import compiler
from app.engine.governance import lint_for_publish
from app.engine.runner import run_manager
from app.engine.schema import GraphSpec, NodeType, validate_graph


def node(nid, ntype, label=None, **config):
    return {"id": nid, "type": ntype, "position": {"x": 0, "y": 0},
            "data": {"label": label or nid, "config": config}}


def _branch(cases: list[dict]) -> GraphSpec:
    return GraphSpec.model_validate({
        "nodes": [
            node("start", "input"),
            node("br", "branch", label="走哪条", mode="expression", cases=cases),
            node("a", "output"), node("b", "output"),
        ],
        "edges": [
            {"source": "start", "target": "br"},
            {"source": "br", "target": "a", "sourceHandle": "fast"},
            {"source": "br", "target": "b", "sourceHandle": "default"},
        ],
    })


def _issues(spec: GraphSpec, level: str) -> list[str]:
    return [i.message for i in validate_graph(spec).issues if i.level == level and i.node_id == "br"]


def test_case_key_default_is_flagged_as_the_reserved_fallback():
    spec = _branch([{"key": "fast", "condition": "true"},
                    {"key": "default", "condition": "true", "label": "协作模式"}])
    warnings = _issues(spec, "warning")
    assert any("default" in m and "保留" in m for m in warnings), warnings
    # 现存的 ⑥ 模板就是这么写的：条件为 true 的 default 等价于兜底，运行语义自洽。
    # 升成 error 会让它和所有从它复制出来的图直接不能跑
    assert validate_graph(spec).ok


def test_duplicate_case_keys_are_an_error():
    spec = _branch([{"key": "fast", "condition": "vars.a"}, {"key": "fast", "condition": "vars.b"}])
    errors = _issues(spec, "error")
    assert any("fast" in m and "重复" in m for m in errors), errors
    assert not validate_graph(spec).ok


def test_blank_case_key_is_an_error():
    spec = _branch([{"key": "fast", "condition": "vars.a"}, {"key": "  ", "condition": "vars.b"}])
    assert any("标识" in m for m in _issues(spec, "error")), _issues(spec, "error")


def test_validation_uses_the_names_on_the_canvas():
    spec = GraphSpec.model_validate({
        "nodes": [node("start", "input"), node("sub", "subgraph"), node("t", "tool"),
                  node("v", "validate"), node("out", "output")],
        "edges": [{"source": "start", "target": "sub"}, {"source": "sub", "target": "t"},
                  {"source": "t", "target": "v"}, {"source": "v", "target": "out"}],
    })
    messages = " ".join(i.message for i in validate_graph(spec).issues)
    assert "子工作流" in messages and "子图节点" not in messages
    assert "「调用工具」" in messages
    assert "结构校验" in messages


def _governed(*nodes) -> str:
    spec = GraphSpec.model_validate({
        "nodes": [node("start", "input"), *nodes],
        "edges": [{"source": "start", "target": n["id"]} for n in nodes],
    })
    return " ".join(i.message for i in lint_for_publish(spec, level="governed").issues)


def test_governance_speaks_the_ui_vocabulary():
    text = _governed(
        node("team", "supervisor", label="协作团队"),
        node("ag", "agent", label="取数员", tools=["web_search"], approval="never"),
        node("bare", "agent", label="空手的"),
        node("c", "code", label="算一下", network=True),
    )
    for internal in ("supervisor", "approval=never", "llm 节点", "contract"):
        assert internal not in text, f"门禁文案里还有内部词 {internal!r}：{text}"
    assert "「协作团队」" in text and "多 Agent 协作" in text
    assert "审批策略" in text and "全部自动放行" in text
    assert "模型调用" in text
    assert "出具契约" in text


# --------------------------------------------------------------------------
# 运行期报错
# --------------------------------------------------------------------------


@pytest.fixture
async def engine_up():
    await run_manager.setup()
    yield
    await run_manager.shutdown()


async def _finish(run_id: str) -> Run:
    for _ in range(300):
        async with SessionLocal() as session:
            row = await session.get(Run, run_id)
            if row.status in ("succeeded", "failed", "cancelled", "interrupted"):
                return row
        await asyncio.sleep(0.03)
    raise AssertionError("等超时了")


RAW = "_make_query_tool.<locals>._run() got an unexpected keyword argument 'query'"


async def test_a_python_error_reads_like_a_sentence(engine_up, monkeypatch):
    async def broken(state, ctx):
        raise TypeError(RAW)

    monkeypatch.setitem(compiler.RUNNERS, NodeType.TRANSFORM, broken)
    graph = {
        "nodes": [node("start", "input"), node("shape", "transform", label="整理数据",
                                               mode="template", template="x")],
        "edges": [{"source": "start", "target": "shape"}],
    }
    run = await run_manager.start(graph=graph, input_payload={})
    row = await _finish(run.id)
    assert row.status == "failed"
    for text in (row.error or "",):
        assert "TypeError" not in text and "<locals>" not in text, text
        assert "query" in text, "原因要说到点子上：哪个参数对不上"
    assert row.error_node_id == "shape"

    async with SessionLocal() as session:
        events = list((await session.execute(
            select(RunEvent).where(RunEvent.run_id == run.id).order_by(RunEvent.seq)
        )).scalars())
    failed_node = next(e for e in events if e.type == "node.failed")
    assert "TypeError" not in failed_node.data["error"]
    assert "TypeError" in failed_node.data["detail"], "原始异常要留着，给排查的人看"
    failed_run = next(e for e in events if e.type == "run.failed")
    assert failed_run.data["node_id"] == "shape"
    assert failed_run.data["label"] == "整理数据"
    assert "TypeError" not in failed_run.data["error"]


async def test_resume_refusal_does_not_leak_the_status_enum(engine_up):
    async with SessionLocal() as session:
        run = Run(workflow_name="x", status="succeeded", graph={"nodes": [], "edges": []}, input={})
        session.add(run)
        await session.commit()
    with pytest.raises(ValueError) as caught:
        await run_manager.resume(run.id, {"approved": True})
    assert "succeeded" not in str(caught.value)
    assert "已完成" in str(caught.value)


def test_exception_wording_helper():
    from app.engine.errors import describe_exception

    assert "query" in describe_exception(TypeError(RAW))
    assert "TypeError" not in describe_exception(TypeError(RAW))
    timeout = describe_exception(TimeoutError())
    assert timeout and "Timeout" not in timeout
    generic = describe_exception(RuntimeError("Integer exceeds 64-bit range"))
    assert "RuntimeError" not in generic and "64-bit" in generic
    nested = describe_exception(RuntimeError("连接失败：ConnectError: All connection attempts failed"))
    assert "Error" not in nested and "All connection attempts failed" in nested


async def test_issuance_event_says_why_it_was_downgraded(engine_up):
    """只有 gaps 时判成降档，事件里却不带 gaps：时间线上只剩一个光秃秃的「降档出具」，
    横幅还说「请对照下方声明」，下方什么都没有。"""
    graph = {
        "nodes": [
            node("start", "input"),
            node("out", "output", fields=[{"name": "结论", "value": "本周 12 单"}],
                 contract={"metrics_from": "没有这个口径卡", "narrative": "{{ output.结论 }}"}),
        ],
        "edges": [{"source": "start", "target": "out"}],
    }
    run = await run_manager.start(graph=graph, input_payload={})
    row = await _finish(run.id)
    assert row.status == "succeeded", row.error
    async with SessionLocal() as session:
        issuance = (await session.execute(
            select(RunEvent).where(RunEvent.run_id == run.id, RunEvent.type == "issuance")
        )).scalars().one()
    assert issuance.data["tier"] == "degraded"
    assert any("没有这个口径卡" in g for g in issuance.data["gaps"]), issuance.data
    assert issuance.data["metrics_checked"] == 0
