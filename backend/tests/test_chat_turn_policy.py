"""问数据一轮的「决定」（app/chat/turn.py；细案 01 做法 1）。

要不要运行、输入怎么填、要不要带着原图重来、重来时说什么，从问数据页下沉到后端：final 带 next，复核带 retry_instruction，
问数据页照它们走；老服务端没给这两个字段时，问数据页退回它自己那份同样的规则。这里守：

- 各条决定的分支（next_step、fill_input、retry_after_review、retry_instruction 的两种措辞）；
- 前后端对照：frontend/src/store/chat.ts 里留着的那份常量（MAX_ATTEMPTS、fillInput 认问题字段的正则、两种重来的措辞）
  和这里逐字一致——写法同 test_frontend_type_unions.py，两边一改就对不上；
- /copilot/review 按这一轮试过几次给 retry_instruction：可以重来时是那句话，不重来是 null。

问题、字段名都是虚构的。
"""
from __future__ import annotations

import re
import uuid
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from app.chat import turn
from app.main import app

CHAT_TS = Path(__file__).resolve().parents[2] / "frontend" / "src" / "store" / "chat.ts"


def _final(*, issues=(), autorun=True, **extra) -> dict:
    return {"op": "final", "graph": {"nodes": [], "edges": []}, "issues": list(issues), "autorun": autorun, **extra}


ERROR = {"level": "error", "message": "图是空的"}
WARNING = {"level": "warning", "message": "多余的 {{ }}"}


# --------------------------------------------------------------------------
# next_step
# --------------------------------------------------------------------------


def test_next_step_branches():
    assert turn.next_step(_final()) == "run"
    assert turn.next_step(_final(issues=[WARNING])) == "run", "warning 不挡运行"
    assert turn.next_step(_final(autorun=False)) == "ready", "模型说不运行：只建不跑"
    assert turn.next_step(_final(autorun=False, asked={"question": "哪个月？", "options": []})) == "ready"
    assert turn.next_step(_final(issues=[ERROR])) == "fail", "自查过还有 error：判失败，不再整轮重来"
    assert turn.next_step(_final(issues=[ERROR]), self_checked=False) == "retry", "老服务端没自查：重来一次"
    assert turn.next_step(_final(issues=[ERROR], autorun=False)) == "fail", "没过检查比只建不跑优先"
    assert turn.next_step({"op": "reply", "text": "已经查过了"}) == "reply"
    assert turn.next_step({"op": "error", "message": "x"}) == "fail"
    assert turn.next_step(None) == "asked", "流结束时只有反问"
    assert set(turn.NEXT_STEPS) == {"run", "ready", "fail"}


# --------------------------------------------------------------------------
# fill_input
# --------------------------------------------------------------------------


def _graph(*fields: dict) -> dict:
    return {"nodes": [{"id": "start", "type": "input", "data": {"label": "输入", "config": {"fields": list(fields)}}},
                      {"id": "out", "type": "output", "data": {"label": "成果", "config": {}}}], "edges": []}


def test_fill_input_rules():
    assert turn.fill_input(_graph(), "上周销售额", "问：…") == {"question": "上周销售额", "history": "问：…"}
    assert turn.fill_input({"nodes": []}, "上周销售额") == {"question": "上周销售额", "history": ""}
    # 名字像「问题」的优先，其余字段填默认值（没有就空串）
    got = turn.fill_input(_graph({"name": "region", "default": "华东"}, {"name": "topic"}, {"name": "limit"}), "退款率")
    assert got == {"history": "", "region": "华东", "topic": "退款率", "limit": ""}
    got = turn.fill_input(_graph({"name": "门店"}, {"name": "需求说明"}), "退款率")
    assert got["需求说明"] == "退款率" and got["门店"] == ""
    # 没有像问题的：第一个必填的；再没有：第一个
    got = turn.fill_input(_graph({"name": "a"}, {"name": "b", "required": True}), "退款率")
    assert got["b"] == "退款率" and got["a"] == ""
    got = turn.fill_input(_graph({"name": "a"}, {"name": "b"}), "退款率")
    assert got["a"] == "退款率"
    # 大小写不敏感
    assert turn.fill_input(_graph({"name": "UserQuery"}), "退款率")["UserQuery"] == "退款率"


# --------------------------------------------------------------------------
# 复核之后：要不要重来、说什么
# --------------------------------------------------------------------------


BROKEN = {"retry": True, "severity": "broken", "note": "结论不可用",
          "signals": [{"kind": "step_limit", "detail": "agent 用满了 8 步"}, {"kind": "tool_error", "detail": "表不存在"}]}


def test_retry_after_review():
    assert turn.retry_after_review(BROKEN, 0) is True
    assert turn.retry_after_review(BROKEN, 1) is False, "一轮最多自动再试一次"
    assert turn.retry_after_review({**BROKEN, "severity": "degraded"}, 0) is False
    assert turn.retry_after_review({**BROKEN, "retry": False}, 0) is False
    assert turn.retry_after_review(None, 0) is False
    assert turn.review_reason(BROKEN) == "agent 用满了 8 步；表不存在"
    assert turn.review_reason({"signals": [], "note": "结论不可用"}) == "结论不可用"


def test_retry_instruction_two_wordings():
    with_base = turn.retry_instruction("上周销售额", "表不存在", True)
    assert with_base.startswith("这个工作流上一次运行没有成功：表不存在。\n\n请针对这个原因改它")
    assert with_base.endswith("改完继续回答原来的问题：上周销售额")
    scratch = turn.retry_instruction("上周销售额", "表不存在", False)
    assert scratch == "上周销售额\n\n（上一次没有成功：表不存在。请针对这个原因调整，重新给出完整的工作流。）"


# --------------------------------------------------------------------------
# 前后端对照
# --------------------------------------------------------------------------


def _ts() -> str:
    return CHAT_TS.read_text(encoding="utf-8")


def test_max_attempts_matches_the_frontend():
    found = re.search(r"const MAX_ATTEMPTS = (\d+)", _ts())
    assert found and int(found.group(1)) == turn.MAX_ATTEMPTS


def test_the_question_field_pattern_matches_the_frontend():
    found = re.search(r"/(question\|[^/]+)/i\.test", _ts())
    assert found, "chat.ts 的 fillInput 里找不到认问题字段的正则"
    assert found.group(1) == turn.QUESTION_FIELD.pattern and turn.QUESTION_FIELD.flags & re.IGNORECASE


def _template(source: str) -> str:
    """一段 TS 里由 + 连起来的模板字符串 → 一个 Python format 模板（${x} → {x}，\\n → 换行）。"""
    pieces = re.findall(r"`([^`]*)`", source)
    text = "".join(pieces).replace("\\n", "\n")
    return re.sub(r"\$\{(\w+)\}", r"{\1}", text)


def test_the_retry_wordings_match_the_frontend():
    body = re.search(r"function retryInstruction\([^)]*\): string \{(.*?)\n\}", _ts(), re.S)
    assert body, "chat.ts 里找不到 retryInstruction"
    with_base, scratch = re.split(r"\n\s*: ", body.group(1).split("hasBase", 1)[1].split("?", 1)[1], maxsplit=1)
    assert _template(with_base) == turn.RETRY_WITH_BASE
    assert _template(scratch) == turn.RETRY_FROM_SCRATCH


# --------------------------------------------------------------------------
# /copilot/review 给 retry_instruction
# --------------------------------------------------------------------------


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def _run(output: dict, graph: dict, code: str) -> str:
    from app.core.events import EventType
    from app.db.base import SessionLocal
    from app.db.models import Run, RunEvent

    async with SessionLocal() as session:
        run = Run(id=uuid.uuid4().hex, status="succeeded", output=output, graph=graph)
        session.add(run)
        await session.flush()
        session.add(RunEvent(run_id=run.id, seq=1, type=EventType.LOG, node_id="a", ts=0.0,
                             data={"level": "warn", "code": code, "message": "agent 用满了 8 步还没给出结论"}))
        await session.commit()
        return run.id


async def test_review_says_whether_and_how_to_retry(client, monkeypatch):
    import app.api.copilot as copilot
    from app.providers.factory import ProviderNotConfigured

    async def unconfigured(*_a, **_k):
        raise ProviderNotConfigured("没有可用的 provider")

    # 没配模型：规则层照样判得出不可用、该重来
    monkeypatch.setattr(copilot, "get_chat_model", unconfigured)
    graph = _graph({"name": "question"})
    run_id = await _run({"answer": "大概 3 家"}, graph, "step_limit")
    body = (await client.post("/api/copilot/review", json={"run_id": run_id, "question": "有几家门店"})).json()
    assert body["severity"] == "broken" and body["retry"] is True
    assert body["retry_instruction"] == turn.retry_instruction("有几家门店", turn.review_reason(body), True)
    # 这一轮已经重来过一次：不再给
    body = (await client.post("/api/copilot/review",
                              json={"run_id": run_id, "question": "有几家门店", "attempt": 1})).json()
    assert body["retry_instruction"] is None
    # 运行的图是空的：措辞换成从零再建
    bare = await _run({"answer": "大概 3 家"}, {}, "step_limit")
    body = (await client.post("/api/copilot/review", json={"run_id": bare, "question": "有几家门店"})).json()
    assert body["retry_instruction"].startswith("有几家门店\n\n（上一次没有成功：")
    # 干净的运行：不重来
    from app.db.base import SessionLocal
    from app.db.models import Run

    async with SessionLocal() as session:
        clean = Run(status="succeeded", output={"answer": "3 家"}, graph=graph)
        session.add(clean)
        await session.commit()
        clean_id = clean.id
    body = (await client.post("/api/copilot/review", json={"run_id": clean_id, "question": "有几家门店"})).json()
    assert body["verdict"] == "ok" and body["retry_instruction"] is None
