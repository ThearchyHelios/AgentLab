"""自定义工具没保存也能试跑；试跑和运行时走同一套参数校验；工具自己报的失败算失败。

以前编辑器里的「试跑」只能跑已保存的版本：新建时要先保存才能试，改了代码要先
保存再试——而保存就意味着节点里立刻能用上一个还没试过的工具。

另外两件：试跑以前不按参数 schema 校验，运行时却校验，于是试跑通过的参数到了
工作流里被拒；沙箱里代码报错、地址被拦时，结果照样标「成功」，再附一段报错 JSON。
"""
from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from app.db.base import SessionLocal
from app.db.models import CustomTool
from app.main import app
from app.sandbox.base import ExecResult


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


class _FakeSandbox:
    def __init__(self, result: ExecResult) -> None:
        self.result = result
        self.codes: list[str] = []

    async def run(self, code, **_kw):  # noqa: ANN001, ANN003, ANN201
        self.codes.append(code)
        return self.result


def _sandbox(monkeypatch, **result) -> _FakeSandbox:
    import app.tools.custom as custom

    fake = _FakeSandbox(ExecResult(**{"ok": True, "exit_code": 0, "stdout": "", "stderr": "", **result}))
    monkeypatch.setattr(custom, "sandbox_manager", fake)
    return fake


async def _tool_count() -> int:
    async with SessionLocal() as session:
        return int((await session.execute(select(func.count(CustomTool.id)))).scalar_one())


PARAMS = {"type": "object", "properties": {"store": {"type": "string"}, "days": {"type": "integer"}},
          "required": ["store"]}


async def test_an_unsaved_python_tool_can_be_tried(client, monkeypatch):
    fake = _sandbox(monkeypatch, stdout='{"orders": 42}\n')
    before = await _tool_count()
    r = await client.post("/api/custom-tools/test", json={
        "kind": "python", "parameters": PARAMS,
        "config": {"code": "print(json.dumps({'orders': 42}))"},
        "args": {"store": "north", "days": "7"},
    })
    body = r.json()
    assert body["ok"] is True, body
    assert body["result"] == {"orders": 42}
    assert body["duration_ms"] >= 0
    # 参数和运行时一样先过 schema：字符串 "7" 被纠正成整数 7（代码前导里是转义过的 JSON）
    assert '\\"days\\": 7' in fake.codes[0] and '\\"store\\": \\"north\\"' in fake.codes[0]
    assert await _tool_count() == before, "试跑不该落库"


async def test_arguments_that_the_schema_rejects_fail_like_they_would_at_runtime(client, monkeypatch):
    _sandbox(monkeypatch, stdout="1")
    r = await client.post("/api/custom-tools/test", json={
        "kind": "python", "parameters": PARAMS, "config": {"code": "print(1)"}, "args": {"days": 3},
    })
    body = r.json()
    assert body["ok"] is False
    assert "store" in body["error"], "缺了哪个参数得说出来"


async def test_code_that_crashes_is_reported_as_a_failure(client, monkeypatch):
    _sandbox(monkeypatch, ok=False, exit_code=1,
             stderr="Traceback (most recent call last):\n  ...\nNameError: name 'x' is not defined")
    r = await client.post("/api/custom-tools/test", json={
        "kind": "python", "config": {"code": "print(x)"}, "args": {},
    })
    body = r.json()
    assert body["ok"] is False, "沙箱里报错了，不能标成功"
    assert "退出码 1" in body["error"]
    assert "NameError" in body["detail"], "原始报错要留着，用户得照着改代码"
    assert body["hint"]


async def test_an_http_tool_to_a_private_address_is_blocked_and_says_so(client):
    r = await client.post("/api/custom-tools/test", json={
        "kind": "http", "config": {"url": "http://127.0.0.1:9/orders", "method": "GET"}, "args": {},
    })
    body = r.json()
    assert body["ok"] is False
    assert "拦截" in body["error"] or "内网" in body["error"], body


async def test_an_http_tool_renders_its_template_with_the_args(client, monkeypatch):
    import app.tools.custom as custom

    seen: dict = {}

    async def fake_request(method, url, **kw):
        seen.update(method=method, url=url, **kw)
        return {"status": 200, "body": '{"count": 3}'}

    monkeypatch.setattr(custom, "safe_request", fake_request)
    r = await client.post("/api/custom-tools/test", json={
        "kind": "http", "parameters": PARAMS,
        "config": {"url": "https://api.example.com/orders?store={{ store }}", "method": "GET",
                   "parse_json": True},
        "args": {"store": "north"},
    })
    body = r.json()
    assert body["ok"] is True, body
    assert seen["url"] == "https://api.example.com/orders?store=north"
    assert body["result"] == {"status": 200, "data": {"count": 3}}


@pytest.mark.parametrize("draft, words", [
    ({"kind": "http", "config": {}}, "接口地址"),
    ({"kind": "python", "config": {"code": "  "}}, "代码"),
    ({"kind": "graphql", "config": {}}, "graphql"),
])
async def test_an_incomplete_draft_says_what_is_missing(client, draft, words):
    body = (await client.post("/api/custom-tools/test", json={**draft, "args": {}})).json()
    assert body["ok"] is False and words in body["error"], body


async def test_a_misspelt_argument_name_is_run_but_not_kept_quiet(client, monkeypatch):
    """参数名写错、只有一个候选时，运行时替它改名跑通，但一定会说出来（fix_note）。

    试跑以前把这句说明丢了：{"cuont": 3} 照样 ok，结果里也看不出哪里不对，
    用户照着这份参数去配工作流，错的名字就一直留着。
    """
    fake = _sandbox(monkeypatch, stdout='{"count": 3}')
    r = await client.post("/api/custom-tools/test", json={
        "kind": "python", "config": {"code": "print(json.dumps(args))"},
        "parameters": {"type": "object", "properties": {"count": {"type": "integer"}},
                       "required": ["count"]},
        "args": {"cuont": 3},
    })
    body = r.json()
    assert body["ok"] is True, body
    assert '\\"count\\": 3' in fake.codes[0], "改名之后照样执行"
    assert "cuont" in body["note"] and "count" in body["note"], body

    # 名字写对了就不多嘴
    r = await client.post("/api/custom-tools/test", json={
        "kind": "python", "config": {"code": "print(1)"},
        "parameters": {"type": "object", "properties": {"count": {"type": "integer"}}},
        "args": {"count": 3},
    })
    assert "note" not in r.json(), r.json()


async def test_the_tool_library_run_also_says_when_it_renamed_an_argument(client):
    """工具库的「执行」和试跑是同一种形状，纠正说明也一样要带上。"""
    r = await client.post("/api/tools/memory_recall/run", json={"args": {"query": "口径", "limt": 3}})
    body = r.json()
    assert body["ok"] is True, body
    assert "limt" in body["note"] and "limit" in body["note"], body


@pytest.mark.parametrize("parameters, words", [
    ({"properties": "bad"}, "properties"),
    ({"properties": {"n": "int"}}, "n"),
    ({"properties": {"n": {"type": "int"}}}, "int"),
    ({"properties": {"n": {"type": ["integer", "null"]}}}, "n"),
    ({"properties": {"n": {"type": "integer"}}, "required": "n"}, "required"),
])
async def test_a_malformed_parameter_schema_is_the_users_to_fix(client, monkeypatch, parameters, words):
    """参数定义是用户手写的 JSON。写坏了是配置问题，不能说成「后端内部出错了」。"""
    fake = _sandbox(monkeypatch, stdout="1")
    body = (await client.post("/api/custom-tools/test", json={
        "kind": "python", "config": {"code": "print(1)"}, "parameters": parameters, "args": {},
    })).json()
    assert body["ok"] is False, body
    assert "参数定义" in body["error"] and words in body["error"], body
    assert "内部" not in body["error"] and "维护者" not in (body["hint"] or ""), body
    assert not fake.codes, "参数定义都不对，就别去沙箱里跑了"


async def test_the_saved_tool_trial_follows_the_same_rules(client, monkeypatch):
    _sandbox(monkeypatch, ok=False, exit_code=2, stderr="boom")
    r = await client.post("/api/custom-tools", json={
        "name": "trial_saved", "kind": "python", "parameters": PARAMS, "config": {"code": "print(1)"},
    })
    tool = r.json()
    try:
        body = (await client.post(f"/api/custom-tools/{tool['id']}/test",
                                  json={"args": {"store": "north"}})).json()
        assert body["ok"] is False and "退出码 2" in body["error"]
        body = (await client.post(f"/api/custom-tools/{tool['id']}/test", json={"args": {}})).json()
        assert body["ok"] is False and "store" in body["error"]
    finally:
        await client.delete(f"/api/custom-tools/{tool['id']}")


async def test_at_runtime_a_crash_still_reaches_the_model_as_an_error_payload(monkeypatch):
    """运行时的行为不变：失败以 {"error": …} 的 JSON 交给模型，由它决定怎么办。"""
    import json

    from app.tools.custom import build_custom_tools
    from app.tools.registry import ToolContext

    _sandbox(monkeypatch, ok=False, exit_code=1, stderr="boom")
    async with SessionLocal() as session:
        session.add(CustomTool(name="trial_runtime", kind="python", parameters={},
                               config={"code": "raise SystemExit(1)"}))
        await session.commit()
        try:
            tools = await build_custom_tools(["trial_runtime"], ToolContext(run_id="r", node_id="n"),
                                             session)
            out = json.loads(await tools[0].coroutine(input=""))
            assert out["exit_code"] == 1 and out["error"]
        finally:
            row = (await session.execute(
                select(CustomTool).where(CustomTool.name == "trial_runtime"))).scalar_one()
            await session.delete(row)
            await session.commit()
