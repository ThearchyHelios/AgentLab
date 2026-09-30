"""面向用户的报错不露 Python 异常类名和内部符号，并且说得出下一步。

以前界面首行红字是「_make_query_tool.<locals>._run() got an unexpected keyword
argument 'query'」，助手输入坞里是「RuntimeError: …」，校验失败是一大段 pydantic
英文。对问数据的业务用户这些都是天书，而且没有下一步。原始异常不丢：放进 detail，
排查的人照样拿得到。
"""
from __future__ import annotations

import json

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app

_INTERNAL = ("Error:", "Error)", "Exception", "Traceback", "<locals>", "Input should be",
             "validation error", "errors.pydantic.dev")


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _clean(text: str) -> None:
    for token in _INTERNAL:
        assert token not in text, f"面向用户的报错里露出了 {token!r}：{text}"


# --------------------------------------------------------------------------
# 翻译规则本身
# --------------------------------------------------------------------------


def test_common_failures_are_put_in_plain_words():
    import httpx

    from app.core.errors import explain, graph_error, raw

    reason, hint = explain(httpx.ConnectError("[Errno 61] Connection refused"))
    assert "无法连接服务" in reason and hint

    req = httpx.Request("GET", "https://x/v1/models")
    denied = httpx.HTTPStatusError("401", request=req, response=httpx.Response(401, request=req))
    assert "API Key" in explain(denied)[0]

    reason, hint = explain(TypeError("_run() got an unexpected keyword argument 'query'"))
    _clean(reason)
    assert "内部" in reason and hint
    assert raw(TypeError("x")) == "TypeError: x", "原始异常要原样留给 detail"

    from app.engine.schema import GraphSpec

    with pytest.raises(Exception) as e:
        GraphSpec.model_validate({"nodes": [{"id": "a", "type": "nope"}]})
    text = graph_error(e.value)
    _clean(text)
    assert "第 1 个节点" in text and "nope" in text


def test_errors_carry_a_category_for_scene_specific_hints():
    """要按场景换说法的调用方（测数据库连接）按类别判断，不比较 explain 的文字。"""
    import asyncio

    import httpx

    from app.core import errors

    req = httpx.Request("GET", "https://x/v1/models")
    cases = [
        (httpx.ConnectError("[Errno 61] Connection refused"), errors.NETWORK),
        (ConnectionRefusedError(61, "Connection refused"), errors.NETWORK),
        (asyncio.TimeoutError(), errors.TIMEOUT),
        (httpx.HTTPStatusError("401", request=req, response=httpx.Response(401, request=req)), errors.AUTH),
        (httpx.HTTPStatusError("429", request=req, response=httpx.Response(429, request=req)), errors.RATE_LIMIT),
        (httpx.HTTPStatusError("502", request=req, response=httpx.Response(502, request=req)), errors.SERVER),
        (TypeError("x() got an unexpected keyword argument 'q'"), errors.BUG),
        (RuntimeError("upstream closed"), None),
    ]
    for exc, want in cases:
        assert errors.classify(exc) == want, (exc, errors.classify(exc))


# --------------------------------------------------------------------------
# 各接口
# --------------------------------------------------------------------------


async def test_a_failing_provider_test_explains_itself(client, monkeypatch):
    import httpx

    import app.api.settings as settings_api

    class Boom:
        async def ainvoke(self, _prompt):
            raise httpx.ConnectError("[Errno 61] Connection refused")

    monkeypatch.setattr(settings_api, "build_chat_model", lambda *_a, **_k: Boom())
    body = (await client.post("/api/providers/test", json={"kind": "mock", "default_model": "m"})).json()
    assert body["ok"] is False
    _clean(body["error"])
    assert "无法连接服务" in body["error"] and body["hint"]
    assert "ConnectError" in body["detail"], "技术细节里要留着原始异常"


async def test_copilot_stream_errors_are_readable(client, monkeypatch):
    import app.api.copilot as copilot

    class Broken:
        async def astream(self, _messages):
            raise RuntimeError("upstream closed")
            yield  # pragma: no cover

    async def _model(*_a, **_k):
        return Broken(), "mock-fast"

    monkeypatch.setattr(copilot, "get_chat_model", _model)
    events = []
    async with client.stream("POST", "/api/copilot/generate-stream", json={"instruction": "x"}) as r:
        async for line in r.aiter_lines():
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))
    err = next(e for e in events if e["op"] == "error")
    _clean(err["message"])
    assert "upstream closed" in err["message"] and "RuntimeError" in err["detail"]


async def test_copilot_generate_errors_are_readable(client, monkeypatch):
    import app.api.copilot as copilot

    class Garbage:
        def with_structured_output(self, _schema):
            return self

        async def ainvoke(self, _messages):
            raise ValueError("not json")

    async def _model(*_a, **_k):
        return Garbage(), "mock-fast"

    monkeypatch.setattr(copilot, "get_chat_model", _model)
    r = await client.post("/api/copilot/generate", json={"instruction": "x"})
    assert r.status_code == 502
    _clean(r.json()["detail"])


async def test_graph_validation_points_at_the_node(client):
    body = (await client.post("/api/workflows/validate",
                              json={"graph": {"nodes": [{"id": "a", "type": "nope"}]}})).json()
    message = body["issues"][0]["message"]
    _clean(message)
    assert "第 1 个节点的类型「nope」无法识别" in message


async def test_publish_level_uses_ui_words(client):
    wf = (await client.post("/api/workflows", json={"name": "档位", "graph": {}})).json()
    r = await client.post(f"/api/workflows/{wf['id']}/publish", json={"level": "gold"})
    assert r.status_code == 400
    assert "「已发布」" in r.json()["detail"] and "governed" not in r.json()["detail"]
    await client.delete(f"/api/workflows/{wf['id']}")


async def test_a_failing_tool_run_explains_itself(client, monkeypatch):
    import app.api.tools as tools_api

    async def boom(*_a, **_k):
        raise TypeError("_run() got an unexpected keyword argument 'query'")

    monkeypatch.setattr(tools_api, "call_tool", boom)
    body = (await client.post("/api/tools/file_list/run", json={"args": {}})).json()
    assert body["ok"] is False
    _clean(body["error"])
    assert "TypeError" in body["detail"]


async def test_datasource_introspect_failure_is_readable(client):
    r = await client.post("/api/datasources", json={"name": "ghostfile", "kind": "sqlite",
                                                   "database": "/no/such/dir/ghost.db"})
    assert r.status_code == 201
    sid = r.json()["id"]
    try:
        r = await client.post(f"/api/datasources/{sid}/introspect")
        assert r.status_code == 400
        _clean(r.json()["detail"])
        body = (await client.post(f"/api/datasources/{sid}/test")).json()
        assert body["ok"] is False
        _clean(body["error"])
        assert body["detail"], "原始报错要留在 detail"
    finally:
        await client.delete(f"/api/datasources/{sid}")


async def test_embedding_probe_failure_is_readable(client):
    r = await client.get("/api/kb/embedding/probe", params={"base_url": "http://127.0.0.1:9/v1"})
    assert r.status_code == 400
    _clean(r.json()["detail"])


def test_a_broken_document_is_explained_without_class_names():
    pytest.importorskip("pypdf")
    from app.memory.parsing import UnsupportedDocument, extract

    with pytest.raises(UnsupportedDocument) as e:
        extract(b"%PDF-1.4 definitely not a pdf", "broken.pdf")
    _clean(str(e.value))
    assert "broken" not in str(e.value) or "PDF" in str(e.value)


async def test_background_processing_failure_is_readable(monkeypatch):
    from app.api import knowledge as kb_api
    from app.db.base import SessionLocal
    from app.db.models import Document
    from app.memory import kb

    async with SessionLocal() as session:
        doc = await kb.create_document(session, collection="errs", title="t", content="正文",
                                       source="t.txt", mime="text/plain", status="processing")
        doc_id = doc.id

    async def boom(*_a, **_k):
        raise AttributeError("'NoneType' object has no attribute 'encode'")

    monkeypatch.setattr(kb, "process_document", boom)
    await kb_api._process_in_background(doc_id)
    async with SessionLocal() as session:
        doc = await session.get(Document, doc_id)
        assert doc.status == "failed"
        _clean(doc.error)


async def test_governance_hints_do_not_quote_field_names(client):
    wf = (await client.post("/api/workflows", json={"name": "治理", "graph": {}})).json()
    try:
        body = (await client.get("/api/governance/tool-usage", params={"workflow_id": wf["id"]})).json()
        assert "unused" not in body["hint"]
    finally:
        await client.delete(f"/api/workflows/{wf['id']}")
    body = (await client.get("/api/governance/exploratory-clusters")).json()
    assert "promote_candidate" not in body.get("hint", "")


async def test_an_unconfigured_copilot_model_uses_settings_words(client, monkeypatch):
    import app.api.copilot as copilot
    from app.providers.factory import ProviderNotConfigured

    async def _model(*_a, **_k):
        raise ProviderNotConfigured("provider 'gw' 需要填 base_url")

    monkeypatch.setattr(copilot, "get_chat_model", _model)
    r = await client.post("/api/copilot/generate-stream", json={"instruction": "x"})
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert "模型接入「gw」" in detail and "Base URL" in detail and "provider" not in detail, detail


async def test_a_missing_item_says_it_may_have_been_deleted(client):
    """404 多半是别的标签页里删掉了，或者链接过期了。只说「不存在」，人会以为
    是自己输错了什么；统一说一句「可能已被删除」，下一步就清楚了。"""
    gone = "no-such-id"
    calls = [
        ("get", f"/api/workflows/{gone}", None),
        ("patch", f"/api/workflows/{gone}", {"name": "x"}),
        ("delete", f"/api/workflows/{gone}", None),
        ("post", f"/api/workflows/{gone}/duplicate", None),
        ("get", f"/api/workflows/{gone}/versions/1", None),
        ("post", f"/api/workflows/{gone}/versions/1/restore", None),
        ("post", f"/api/workflows/{gone}/publish", {}),
        ("get", "/api/governance/tool-usage", None),
        ("post", "/api/copilot/from-run", {"run_id": gone}),
        ("post", "/api/copilot/review", {"run_id": gone}),
        ("get", f"/api/conversations/{gone}", None),
        ("get", f"/api/kb/documents/{gone}", None),
        ("delete", f"/api/kb/documents/{gone}", None),
        ("delete", f"/api/skills/{gone}", None),
        ("get", f"/api/artifacts/{'0' * 64}", None),
    ]
    for method, url, body in calls:
        kwargs = {"json": body} if body is not None else {}
        if url.endswith("tool-usage"):
            kwargs = {"params": {"workflow_id": gone}}
        r = await client.request(method.upper(), url, **kwargs)
        assert r.status_code == 404, (method, url, r.status_code, r.text)
        detail = r.json()["detail"]
        assert "可能已被删除" in detail, (method, url, detail)
        assert gone not in detail, f"把 id 原样念给用户听没用：{detail}"


async def test_an_unconfigured_provider_on_the_run_path_uses_settings_words(monkeypatch):
    """运行路径上的「缺少 API Key」「接入已停用」和助手、设置页一样经过统一的翻译：写「模型接入」、
    写去「设置 → 模型接入」哪里补，不露 provider、base_url 和英文单引号。以前运行失败原因里原样是
    「provider 'x' 缺少 API Key」。"""
    import asyncio

    from app.core.crypto import encrypt
    from app.db.base import SessionLocal
    from app.db.models import Provider, Run
    from app.engine.runner import run_manager

    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        monkeypatch.delenv(name, raising=False)

    def graph(model: str) -> dict:
        nodes = [
            {"id": "start", "type": "input", "data": {"config": {"fields": [{"name": "q"}]}}},
            {"id": "ask", "type": "llm", "data": {"label": "提问", "config": {"model": model, "prompt": "hi"}}},
            {"id": "out", "type": "output", "data": {"config": {"fields": [{"name": "结果", "value": "{{ last_message }}"}]}}},
        ]
        return {"nodes": nodes, "edges": [{"source": "start", "target": "ask"}, {"source": "ask", "target": "out"}]}

    async def error_of(model: str) -> str:
        run = await run_manager.start(graph=graph(model), input_payload={"q": "x"})
        for _ in range(400):
            async with SessionLocal() as session:
                row = await session.get(Run, run.id)
                if row and row.status in ("failed", "succeeded", "cancelled"):
                    assert row.status == "failed", row.status
                    return row.error or ""
            await asyncio.sleep(0.05)
        raise AssertionError("运行没有按时结束")

    async with SessionLocal() as session:
        keyless = Provider(name="无钥接入", kind="anthropic", api_key=None, models=[{"id": "claude-nokey"}],
                           default_model="claude-nokey", enabled=True, extra={})
        disabled = Provider(name="停用接入", kind="openai", api_key=encrypt("k"), models=[{"id": "gpt-off"}],
                            default_model="gpt-off", enabled=False, extra={})
        session.add_all([keyless, disabled])
        await session.commit()
        ids = [keyless.id, disabled.id]
    await run_manager.setup()
    try:
        error = await error_of("claude-nokey")
        assert "模型接入「无钥接入」缺少 API Key" in error and "「设置 → 模型接入」" in error, error
        error = await error_of("gpt-off")
        assert "模型「gpt-off」所属的模型接入「停用接入」已停用" in error, error
        assert "「设置 → 模型接入」中启用" in error, error
        for text in (error,):
            assert "provider" not in text and "'" not in text, text
    finally:
        await run_manager.shutdown()
        async with SessionLocal() as session:
            for pid in ids:
                row = await session.get(Provider, pid)
                if row is not None:
                    await session.delete(row)
            await session.commit()
