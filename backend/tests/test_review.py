"""跑完之后的复核层。

核心不变量有两条，其余都是围着它们转的：

1. 干净的运行**一次模型都不调**。复核是补救手段，不是每次问数的固定开销。
2. 复核模型看不到原始数据，所以它写出来的任何新数字都是现编的，必须机械拦住。
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.events import EventType
from app.engine import review as rv
from app.main import app


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def ev(etype, **data):
    return {"type": str(etype), "node_id": data.pop("node_id", ""), "data": data}


def warn(message, code=""):
    return ev(EventType.LOG, level="warn", message=message, code=code)


OK_OUTPUT = {"结果": "管理员有 6 个。"}


# --------------------------------------------------------------------------
# scan：规则层
# --------------------------------------------------------------------------


def test_clean_run_has_no_signals():
    """干净的运行扫不出东西——这是"零成本"的前提。"""
    events = [
        ev(EventType.RUN_STARTED),
        ev(EventType.TOOL_START, tool="db_query__shop"),
        ev(EventType.TOOL_END, tool="db_query__shop"),
        ev(EventType.LOG, level="info", message="随便什么"),
        ev(EventType.RETRIEVE_END, count=5, degraded=False, query="权限"),
        ev(EventType.RUN_FINISHED),
    ]
    assert rv.scan(events, OK_OUTPUT) == []


def test_tool_error_is_broken():
    signals = rv.scan([ev(EventType.TOOL_ERROR, tool="db_query__x", error="no such table")],
                      OK_OUTPUT)
    assert [s.kind for s in signals] == ["tool_error"]
    assert signals[0].severity == rv.BROKEN
    assert "no such table" in signals[0].detail


def test_tool_error_then_success_is_only_degraded():
    """自愈接住了就不该报"答案不可信"。

    参数自动纠正那条路每修一次就发一条 tool.error，不区分的话每次自愈
    都会触发一次复核，还告诉用户答案不可信——而它明明跑通了。
    """
    signals = rv.scan([
        ev(EventType.TOOL_ERROR, tool="db_query__x", error="unexpected keyword 'query'"),
        ev(EventType.TOOL_END, tool="db_query__x"),
    ], OK_OUTPUT)
    assert [s.kind for s in signals] == ["tool_args_fixed"]
    assert signals[0].severity == rv.DEGRADED


def test_retrieve_degraded_and_empty():
    degraded = rv.scan([ev(EventType.RETRIEVE_END, count=3, degraded=True)], OK_OUTPUT)
    assert degraded[0].kind == "retrieve_degraded"
    assert degraded[0].severity == rv.DEGRADED

    empty = rv.scan([ev(EventType.RETRIEVE_END, count=0, query="商家账户")], OK_OUTPUT)
    assert empty[0].kind == "retrieve_empty"
    assert empty[0].severity == rv.BROKEN
    assert "商家账户" in empty[0].detail


def test_specific_retrieve_reason_wins_over_the_generic_one():
    """检索降级会发两条：knowledge.py 先发具体原因的 warn，再发 retrieve.end。

    两边都收就是同一件事报两遍，用户读到一条含糊的加一条具体的，还得自己
    判断是不是两个问题。
    """
    signals = rv.scan([
        warn("default 里有 32 条向量由「local-hashing」建立，和当前的对不上", "retrieve_degraded"),
        ev(EventType.RETRIEVE_END, count=5, degraded=True),
    ], OK_OUTPUT)
    assert len(signals) == 1
    assert "32 条向量" in signals[0].detail


def test_node_and_run_failures_are_broken():
    signals = rv.scan([
        ev(EventType.NODE_FAILED, node_id="agent1", error="boom"),
        ev(EventType.RUN_FAILED, error="超时"),
    ], OK_OUTPUT)
    assert {s.kind for s in signals} == {"node_failed", "run_failed"}
    assert all(s.severity == rv.BROKEN for s in signals)


def test_warn_classified_by_code_not_by_message():
    """分类挂在 code 上。消息是给人读的，随时会改。"""
    assert rv.scan([warn("agent 用满了 8 步还没给出结论。", "step_limit")], OK_OUTPUT)[0] \
        .severity == rv.BROKEN
    assert rv.scan([warn("第 1 次校验失败：不是合法 JSON", "validate_retry")], OK_OUTPUT)[0] \
        .severity == rv.DEGRADED


def test_benign_warns_do_not_trigger_review():
    """流式回退、隔离降级对答案内容零影响。

    为它们叫一次模型，等于每次网络抖一下就多花一次调用，还要用户读一句
    与结论无关的告警。轨迹里有记录就够了。
    """
    events = [
        warn("流式失败，回退非流式：connection reset", "stream_fallback"),
        warn("节点要求 strict 隔离，但 microVM 未就绪，实际用了 seatbelt", "isolation_fallback"),
    ]
    assert rv.scan(events, OK_OUTPUT) == []


def test_warn_without_code_defaults_to_degraded():
    """库里的老事件没有 code。宁可多说一句，也好过把真问题当噪音吞掉。"""
    signals = rv.scan([warn("某个没带 code 的告警")], OK_OUTPUT)
    assert [s.kind for s in signals] == ["warn"]
    assert signals[0].severity == rv.DEGRADED


def test_empty_and_placeholder_output_are_broken():
    assert rv.scan([], None)[0].kind == "empty_output"
    assert rv.scan([], {"结果": ""})[0].kind == "empty_output"

    # run dd9927e6 真的把这句内部管道文案当答案交出去过
    leaked = {"结果": "本轮只执行了第一个工具。看到它的结果之后，再决定这一个还要不要调。"}
    assert rv.scan([], leaked)[0].kind == "placeholder_output"


def test_duplicate_signals_collapse():
    """一次运行里同一条告警发五遍，用户只需要看到一次。"""
    events = [warn("第 1 次校验失败：不是合法 JSON", "validate_retry")] * 5
    assert len(rv.scan(events, OK_OUTPUT)) == 1


def test_scan_accepts_orm_like_rows():
    """事件既可能是 dict 也可能是 ORM 行，两种都要吃得下。"""

    class Row:
        type = str(EventType.TOOL_ERROR)
        node_id = "n1"
        data = {"tool": "t", "error": "e"}

    assert rv.scan([Row()], OK_OUTPUT)[0].kind == "tool_error"


def test_worst_and_should_retry():
    broken = rv.Signal("step_limit", "步数用满", rv.BROKEN)
    degraded = rv.Signal("retrieve_degraded", "检索降级", rv.DEGRADED)
    assert rv.worst([degraded, broken]) == rv.BROKEN
    assert rv.worst([degraded]) == rv.DEGRADED
    assert rv.worst([]) == ""

    assert rv.should_retry([broken]) is True
    # 检索什么都没查到，重跑一遍还是查不到——那不是重跑能解决的
    assert rv.should_retry([rv.Signal("retrieve_empty", "没命中", rv.BROKEN)]) is False
    assert rv.should_retry([degraded]) is False


# --------------------------------------------------------------------------
# check_numbers：不得新增数字
# --------------------------------------------------------------------------


def test_check_numbers_allows_reordering():
    original = "管理员有 6 个，商家有 5 个，占比 45.5%。"
    revised = "占比 45.5%。其中商家 5 个、管理员 6 个。"
    assert rv.check_numbers(original, revised) == []


def test_check_numbers_catches_invented():
    invented = rv.check_numbers("管理员有 6 个。", "管理员有 6 个，商家有 5 个。")
    assert invented == ["5"]


def test_check_numbers_ignores_prose_without_numbers():
    assert rv.check_numbers("管理员有 6 个。", "这次没能查到有效数据，建议重试。") == []


# --------------------------------------------------------------------------
# review：调模型那一段
# --------------------------------------------------------------------------


class FakeModel:
    """最小的 with_structured_output 替身。calls 记录被调了几次。"""

    def __init__(self, reply):
        self.reply = reply
        self.calls = 0

    def with_structured_output(self, schema):
        return self

    async def ainvoke(self, messages):
        self.calls += 1
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


SIGNALS = [rv.Signal("step_limit", "agent 用满了 8 步还没给出结论", rv.BROKEN)]


@pytest.mark.asyncio
async def test_review_annotates_without_rewriting():
    model = FakeModel({"note": "这次没跑完就收尾了。", "answer": "", "retry": True})
    result = await rv.review(model, question="有几个管理员", answer="管理员有 6 个。",
                             signals=SIGNALS)
    assert result.verdict == "annotated"
    assert result.answer is None          # 沿用原答案
    assert result.note == "这次没跑完就收尾了。"
    assert result.retry is True
    assert result.severity == rv.BROKEN


@pytest.mark.asyncio
async def test_review_adopts_clean_rewrite():
    model = FakeModel({
        "note": "步数用满了。", "answer": "管理员有 6 个（这个数可能不完整）。", "retry": False,
    })
    result = await rv.review(model, question="q", answer="管理员有 6 个。", signals=SIGNALS)
    assert result.verdict == "rewritten"
    assert result.answer == "管理员有 6 个（这个数可能不完整）。"
    # 规则层管资格、模型有否决权：step_limit 本来够格重跑，模型说不必就不跑
    assert result.retry is False


@pytest.mark.asyncio
async def test_model_cannot_force_an_unhelpful_retry():
    """检索什么都没查到，重跑一遍还是查不到——模型说要重跑也不能听。

    否则就是白烧一次建图加一次跑图，而用户等的时间翻倍。
    """
    model = FakeModel({"note": "知识库里没有这个。", "answer": "", "retry": True})
    result = await rv.review(
        model, question="q", answer="没查到。",
        signals=[rv.Signal("retrieve_empty", "知识库里没有检索到相关内容", rv.BROKEN)],
    )
    assert result.severity == rv.BROKEN
    assert result.retry is False


@pytest.mark.asyncio
async def test_review_reverts_rewrite_with_invented_numbers():
    """复核手上只有原答案，它写出来的新数字只能是凭空来的。"""
    model = FakeModel({
        "note": "步数用满了。", "answer": "管理员有 6 个，商家有 5 个。", "retry": False,
    })
    result = await rv.review(model, question="q", answer="管理员有 6 个。", signals=SIGNALS)
    assert result.verdict == "reverted"
    assert result.answer is None          # 那份正文一个字都不能用
    assert "5" in result.note             # 但要说清是为什么退的
    assert "步数用满了。" in result.note   # 说明本身还是要给


@pytest.mark.asyncio
async def test_review_survives_model_failure():
    """复核只是加一层说明，它自己挂了不能把已经到手的答案也带走。"""
    model = FakeModel(RuntimeError("502"))
    result = await rv.review(model, question="q", answer="管理员有 6 个。", signals=SIGNALS)
    assert result.verdict == "annotated"
    assert result.answer is None
    assert result.note == SIGNALS[0].detail   # 退回规则层自己的措辞
    assert result.retry is True


@pytest.mark.asyncio
async def test_review_survives_garbage_reply():
    model = FakeModel("这不是 JSON，也不是对象")
    result = await rv.review(model, question="q", answer="管理员有 6 个。", signals=SIGNALS)
    assert result.verdict == "annotated"
    assert result.note


# --------------------------------------------------------------------------
# 接口
# --------------------------------------------------------------------------


async def _make_run(client, output, events):
    """直接落一条运行和它的事件，省掉真跑一遍图。"""
    from app.db.base import SessionLocal
    from app.db.models import Run, RunEvent

    async with SessionLocal() as session:
        run = Run(workflow_id=None, status="succeeded", output=output, graph={})
        session.add(run)
        await session.flush()
        for i, e in enumerate(events):
            session.add(RunEvent(
                run_id=run.id, seq=i + 1, type=e["type"],
                node_id=e["node_id"] or None, ts=0.0, data=e["data"],
            ))
        await session.commit()
        return run.id


@pytest.mark.asyncio
async def test_clean_run_never_touches_the_model(client, monkeypatch):
    """干净运行零成本——这条是整层设计的前提，要用「模型根本没被碰过」来断言。"""
    async def explode(*a, **k):
        raise AssertionError("干净的运行不该调模型")

    monkeypatch.setattr("app.providers.factory.get_chat_model", explode)
    monkeypatch.setattr("app.api.copilot.get_chat_model", explode)

    run_id = await _make_run(client, OK_OUTPUT, [
        ev(EventType.TOOL_START, tool="t"), ev(EventType.TOOL_END, tool="t"),
    ])
    resp = await client.post("/api/copilot/review", json={"run_id": run_id, "question": "q"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["verdict"] == "ok"
    assert body["signals"] == []
    assert body["answer"] is None


@pytest.mark.asyncio
async def test_review_endpoint_reports_signals(client, monkeypatch):
    """没配模型时也得把规则层扫到的东西说出来，而不是静默放行。"""
    from app.providers.factory import ProviderNotConfigured

    async def unconfigured(*a, **k):
        raise ProviderNotConfigured("没有可用的 provider")

    monkeypatch.setattr("app.api.copilot.get_chat_model", unconfigured)

    run_id = await _make_run(client, OK_OUTPUT, [
        ev(EventType.TOOL_ERROR, tool="db_query__x", error="no such table: users"),
    ])
    body = (await client.post(
        "/api/copilot/review", json={"run_id": run_id, "question": "q"}
    )).json()
    assert body["verdict"] == "annotated"
    assert body["severity"] == rv.BROKEN
    assert [s["kind"] for s in body["signals"]] == ["tool_error"]
    assert "no such table" in body["note"]
    assert body["retry"] is True


@pytest.mark.asyncio
async def test_review_endpoint_404(client):
    resp = await client.post("/api/copilot/review", json={"run_id": "没这个", "question": "q"})
    assert resp.status_code == 404
