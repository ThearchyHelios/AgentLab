"""登记的端点与平台执行器、密钥收口（应用平台 A6 第 2、3、6、7 条）。

- 端点的认证头只回掩码，提交掩码值表示保持原值，乐观锁；
- 只有登记、开了内网访问的端点能连内网，开了也照样拦元数据地址和高危端口；模型给的地址照旧过完整 SSRF 检查；
- 路径参数逐段编码代入，. 和 .. 不收；只认登记的 base_url；
- 探索运行走测试环境，没有测试环境时写入演练（不发送）；正式运行走生产环境；
- 带幂等头；service_on_behalf 带 EdDSA 身份断言，公钥在 /api/auth/jwks；
- 超时 → 结果未知；连接没建立 → 确定的失败；
- 声明了表格的读取落成查询快照同一个形状的端点快照，台账照 query 处理；
- 运行里的写入不从自定义工具直接发；工具页试运行、工具库执行写入能力一律演练（不发送，交回将要发送的请求）；
- 试运行、测试连接按 HTTP 状态照实说：2xx 才算成功，能连上但 404 / 401 和连不上分开说；
- 认证头名字为空、不合法的存不进去（422 说人话），存量的坏头发请求时照实说是哪一项，不是 LocalProtocolError；
- 自定义工具请求头、MCP env 和请求头：加密存、掩码回、启动时存量明文加密一遍（幂等）；MCP http 地址过 SSRF 检查或绑定端点。
"""
from __future__ import annotations

import json

import httpx
import jwt
import pytest
from sqlalchemy import delete, select

from app.capabilities import endpoints as ep
from app.capabilities import registry
from app.core import crypto
from app.core.artifact_store import load
from app.db.base import SessionLocal
from app.db.models import Artifact, Capability, CustomTool, Endpoint, McpServer, Run
from app.engine.evidence import query_entry_fields
from app.tools.net import UnsafeUrlError, assert_safe_url
from app.tools.registry import ToolContext, Versions, call_tool
from tests.fakes import auth_env

BASE = "http://127.0.0.1:9"


class Recorder:
    """假的外部系统：记下收到的每个请求，按路径回预设的响应；raise_ 设了就抛那个网络异常。"""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.raise_: Exception | None = None
        #: 按路径改回的状态码（测 4xx、404 时用）
        self.status: dict[str, int] = {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.raise_ is not None:
            raise self.raise_
        if (code := self.status.get(request.url.path)) is not None:
            return httpx.Response(code, json={"error": "nope"})
        if request.url.path.startswith("/orders"):
            return httpx.Response(200, json={"items": [{"id": "A1", "amount": 12.5, "phone": "13900000001"},
                                                       {"id": "A2", "amount": 7, "phone": "13900000002"}]})
        return httpx.Response(201, json={"ok": True})


@pytest.fixture
async def erp(monkeypatch):
    recorder = Recorder()
    real = httpx.AsyncClient

    def client(**kwargs):
        return real(transport=httpx.MockTransport(recorder), **kwargs)

    monkeypatch.setattr(ep, "_client", client)
    c = auth_env.client()
    r = await c.post("/api/endpoints", json={"name": "erp", "environments": {
        "prod": {"base_url": BASE, "headers": {"Authorization": "Bearer prod-secret-token"}, "allow_intranet": True}}})
    assert r.status_code == 201, r.text
    endpoint = r.json()
    for body in (
        {"name": "erp_orders", "kind": "http", "parameters": {"type": "object", "properties": {"q": {"type": "string"}}},
         "config": {"endpoint": endpoint["id"], "method": "GET", "path": "/orders", "query": {"q": "{{ q }}"}}},
        {"name": "erp_order", "kind": "http",
         "parameters": {"type": "object", "properties": {"order_id": {"type": "string"}}},
         "config": {"endpoint": endpoint["id"], "method": "GET", "path": "/orders/{{ order_id }}"}},
        {"name": "erp_pay", "kind": "http",
         "parameters": {"type": "object", "properties": {"amount": {"type": "number"}}},
         "config": {"endpoint": endpoint["id"], "method": "POST", "path": "/payments"}},
    ):
        r = await c.post("/api/custom-tools", json=body)
        assert r.status_code == 201, r.text
    try:
        yield {"client": c, "endpoint": endpoint, "recorder": recorder}
    finally:
        async with SessionLocal() as s:
            await s.execute(delete(CustomTool).where(CustomTool.name.in_(["erp_orders", "erp_order", "erp_pay"])))
            await s.execute(delete(Endpoint))
            await s.execute(delete(Capability))
            await s.commit()
            await registry.refresh_cache(s)


async def test_endpoint_headers_are_masked_and_kept(erp):
    c, endpoint = erp["client"], erp["endpoint"]
    shown = endpoint["environments"]["prod"]["headers"]["Authorization"]
    assert "prod-secret-token" not in shown
    r = await c.patch(f"/api/endpoints/{endpoint['id']}", json={"version": 1, "environments": {
        "prod": {"base_url": BASE, "headers": {"Authorization": shown}, "allow_intranet": True},
        "test": {"base_url": "http://127.0.0.1:10", "headers": {}}}})
    assert r.status_code == 200, r.text
    async with SessionLocal() as s:
        row = await s.get(Endpoint, endpoint["id"])
    stored = row.environments["prod"]["headers"]["Authorization"]
    assert stored.startswith("enc::") and crypto.decrypt(stored) == "Bearer prod-secret-token", "提交掩码值保持原值"
    assert (await c.patch(f"/api/endpoints/{endpoint['id']}", json={"version": 1})).status_code == 409


async def test_intranet_only_through_registered_endpoints():
    with pytest.raises(UnsafeUrlError):
        await assert_safe_url("http://127.0.0.1:8080/x")
    assert await assert_safe_url("http://127.0.0.1:8080/x", allow_intranet=True)
    for url in ("http://169.254.169.254/latest/meta-data", "http://100.100.100.200/", "http://127.0.0.1:22/"):
        with pytest.raises(UnsafeUrlError):
            await assert_safe_url(url, allow_intranet=True)
    # 没绑定端点的 http 工具拿模型给的内网地址：照旧拦
    from app.tools.custom import _run_http

    blocked = await _run_http(CustomTool(name="t", kind="http", config={"url": "http://127.0.0.1:8080/"}), {})
    assert "内网" in blocked["error"]


async def test_tabular_read_becomes_an_evidence_snapshot(erp):
    recorder = erp["recorder"]
    async with SessionLocal() as s:
        cap = await s.get(Capability, "erp_orders")
        version = cap.version
    r = await erp["client"].put("/api/capabilities/erp_orders/governance", json={
        "version": version, "tabular": {"rows": "/items", "columns": {"单号": "/id", "金额": "/amount", "电话": "/phone"}},
        "masked": ["/phone"], "credential_mode": "service_on_behalf"})
    assert r.status_code == 200, r.text
    async with SessionLocal() as s:
        run = Run(workflow_name="t", run_class="formal", started_by="alice")
        s.add(run)
        await s.commit()
        run_id = run.id
    ctx = ToolContext(run_id=run_id, node_id="n1", data_versions=Versions.CURRENT)
    async with SessionLocal() as s:
        out = json.loads(await call_tool("erp_orders", {"q": "x y"}, ctx, session=s))
    assert set(out) == {"artifact", "source", "columns", "rows", "truncated", "request"}
    assert out["columns"] == ["单号", "金额", "电话"] and out["rows"][0] == ["A1", 12.5, "13900000001"]
    fields = query_entry_fields(out)
    assert fields and fields["rows"] == 2 and fields["source"] == "erp"
    assert registry.is_platform_query("erp_orders")
    assert await registry.snapshot_owned(out["artifact"], run_id, "erp_orders")
    assert not await registry.snapshot_owned(out["artifact"], "other-run", "erp_orders")
    snap = load(out["artifact"])
    assert snap["mask_columns"] == ["电话"] and snap["response"]["items"][0]["id"] == "A1"
    async with SessionLocal() as s:
        kinds = set((await s.execute(select(Artifact.kind).where(Artifact.id == out["artifact"]))).scalars())
    assert kinds == {"endpoint_snapshot"}

    sent = recorder.requests[-1]
    assert str(sent.url) == f"{BASE}/orders?q=x+y" and sent.headers["authorization"] == "Bearer prod-secret-token"
    claims = jwt.decode(sent.headers["x-agentlab-assertion"],
                        jwt.PyJWK((await erp["client"].get("/api/auth/jwks")).json()["keys"][0]).key,
                        algorithms=["EdDSA"], audience="erp", issuer="agentlab")
    assert claims["sub"] == "alice", "替谁办的写在断言里，正式运行的发起人"


async def test_path_params_are_encoded_segment_by_segment(erp):
    ctx = ToolContext(run_id="playground", node_id="t", data_versions=Versions.CURRENT)
    async with SessionLocal() as s:
        await call_tool("erp_order", {"order_id": "a/b?c#d"}, ctx, session=s)
        assert erp["recorder"].requests[-1].url.raw_path == b"/orders/a%2Fb%3Fc%23d"
        failed = await call_tool("erp_order", {"order_id": ".."}, ctx, session=s)
    assert "不能是" in json.dumps(failed, ensure_ascii=False)


async def test_environments_simulation_idempotency_and_unknown(erp):
    recorder = erp["recorder"]
    async with SessionLocal() as s:
        cap = await s.get(Capability, "erp_pay")
        before = len(recorder.requests)
        dry = await ep.endpoint_request(s, cap, {"amount": 3}, environment="test", idem_key="k:1")
        assert dry.environment == "simulated" and dry.body["simulated"] and len(recorder.requests) == before, \
            "没有测试环境：演练，不发送"
        sent = await ep.endpoint_request(s, cap, {"amount": 3}, environment="prod", idem_key="k:1")
        assert sent.status == 201 and recorder.requests[-1].headers["idempotency-key"] == "k:1"
        assert json.loads(recorder.requests[-1].content) == {"amount": 3}
        recorder.raise_ = httpx.ReadTimeout("slow")
        lost = await ep.endpoint_request(s, cap, {"amount": 3}, environment="prod", idem_key="k:2")
        assert lost.unknown and lost.status is None
        recorder.raise_ = httpx.ConnectError("refused")
        refused = await ep.endpoint_request(s, cap, {"amount": 3}, environment="prod", idem_key="k:3")
        assert not refused.unknown and refused.error, "连接没建立：请求没发出去，是确定的失败"
        endpoint = await s.get(Endpoint, erp["endpoint"]["id"])
        assert ep.pick_environment(endpoint, formal=True, effect="write") == "prod"
        assert ep.pick_environment(endpoint, formal=False, effect="write") == "simulated"
        assert ep.pick_environment(endpoint, formal=False, effect="read") == "prod"


async def test_writes_inside_a_run_go_through_the_action_flow(erp):
    async with SessionLocal() as s:
        run = Run(workflow_name="t", run_class="formal")
        s.add(run)
        await s.commit()
        run_id = run.id
    before = len(erp["recorder"].requests)
    async with SessionLocal() as s:
        out = await call_tool("erp_pay", {"amount": 1}, ToolContext(run_id=run_id, node_id="n",
                                                                    data_versions=Versions.CURRENT), session=s)
    assert "动作确认" in json.loads(out)["error"] and len(erp["recorder"].requests) == before


async def test_custom_tool_headers_are_sealed_and_masked():
    c = auth_env.client()
    r = await c.post("/api/custom-tools", json={"name": "crm_api", "kind": "http", "parameters": {},
                                                "config": {"url": "https://crm.example.com", "headers": {"X-Key": "abcdefgh12345"}}})
    assert r.status_code == 201
    shown = r.json()["config"]["headers"]["X-Key"]
    assert "abcdefgh12345" not in shown
    r = await c.patch(f"/api/custom-tools/{r.json()['id']}", json={**r.json(), "description": "改了说明"})
    assert r.status_code == 200
    async with SessionLocal() as s:
        row = (await s.execute(select(CustomTool).where(CustomTool.name == "crm_api"))).scalar_one()
        assert crypto.decrypt(row.config["headers"]["X-Key"]) == "abcdefgh12345"
        await s.delete(row)
        await s.commit()


async def test_existing_plaintext_is_sealed_once():
    from app.capabilities.secrets import seal_existing

    async with SessionLocal() as s:
        s.add(CustomTool(name="legacy", kind="http", config={"url": "https://x.example.com", "headers": {"K": "plain"}}))
        s.add(McpServer(name="legacy_mcp", transport="stdio", command="x", env={"TOKEN": "plain-token"}))
        await s.commit()
    try:
        assert await seal_existing() == 2
        assert await seal_existing() == 0, "幂等"
        async with SessionLocal() as s:
            tool = (await s.execute(select(CustomTool).where(CustomTool.name == "legacy"))).scalar_one()
            server = (await s.execute(select(McpServer).where(McpServer.name == "legacy_mcp"))).scalar_one()
        assert tool.config["headers"]["K"].startswith("enc::") and crypto.decrypt(server.env["TOKEN"]) == "plain-token"
    finally:
        async with SessionLocal() as s:
            await s.execute(delete(CustomTool).where(CustomTool.name == "legacy"))
            await s.execute(delete(McpServer).where(McpServer.name == "legacy_mcp"))
            await s.commit()


async def test_mcp_http_address_and_secrets(erp):
    c = auth_env.client()
    r = await c.post("/api/mcp/servers", json={"name": "inner", "transport": "http", "url": "http://127.0.0.1:9100/mcp"})
    assert r.status_code == 422, "内网地址不经登记的端点不放行"
    r = await c.post("/api/mcp/servers", json={"name": "inner", "transport": "http", "url": "/mcp",
                                               "endpoint_id": erp["endpoint"]["id"], "headers": {"X-Token": "tok-123456789"}})
    assert r.status_code == 201, r.text
    row = r.json()
    assert "tok-123456789" not in row["headers"]["X-Token"]
    async with SessionLocal() as s:
        server = await s.get(McpServer, row["id"])
    from app.tools.mcp_manager import check_url

    config = await check_url(server)
    assert config["url"] == f"{BASE}/mcp"
    assert config["headers"] == {"Authorization": "Bearer prod-secret-token", "X-Token": "tok-123456789"}
    await c.delete(f"/api/mcp/servers/{row['id']}")


def test_run_compare_reads_endpoint_requests():
    from app.engine.compare import _compare_request, _Query

    def q(source, path, query):
        snap = {"source": source, "columns": [], "rows": [], "request": {"method": "GET", "path": path, "query": query}}
        return _Query(artifact="a", node_id="n", tool="erp_orders", ordinal=1, call_id=None, sql=None, snap=snap,
                      problem=None, source=source, inputs=None)

    assert _compare_request(q("erp", "/orders", {"d": "1"}), q("erp", "/orders", {"d": "1"})).structure == "same"
    assert _compare_request(q("erp", "/orders", {"d": "1"}), q("erp", "/orders", {"d": "2"})).structure == "params_only"
    assert _compare_request(q("erp", "/orders", {}), q("erp", "/refunds", {})).structure == "changed"


async def test_evidence_panel_masks_an_endpoint_snapshot_by_column():
    """证据面板认端点快照：按列名遮（落快照时记下的加能力现在的遮罩字段），不按数据源找、不把整张表当成「数据源不在了」
    遮掉。能力登记里查不到的只按快照记下的遮。"""
    from app.api.evidence import _mask_sets, _Masks

    snap = {"capability": "erp_orders", "source": "erp", "columns": ["单号", "电话", "金额"],
            "rows": [["A1", "13800000000", 12]], "mask_columns": ["电话"]}
    sets = await _mask_sets(snap, "erp", _Masks())
    assert sets.hidden == {"电话"} and sets.sure == {"电话"} and not sets.gone


async def test_header_names_are_checked_when_saved_and_when_sent(erp):
    c, endpoint = erp["client"], erp["endpoint"]
    for headers, words in (({"": ""}, "名称不能为空"), ({"X Token": "1"}, "不合法"), ({"X-Token": "第一\n第二"}, "不能换行")):
        r = await c.patch(f"/api/endpoints/{endpoint['id']}", json={"version": 1, "environments": {
            "prod": {"base_url": BASE, "headers": headers, "allow_intranet": True}}})
        assert r.status_code == 422 and "生产环境的认证头有误" in r.json()["detail"] and words in r.json()["detail"], r.text
    r = await c.post("/api/endpoints", json={"name": "erp2", "idempotency_header": "Idem Key", "environments": {
        "prod": {"base_url": BASE}}})
    assert r.status_code == 422 and "幂等请求头" in r.json()["detail"]
    # 加校验之前存进去的 {"": ""}：发请求、测连接都照实说是认证头的事，请求没有发出，也不当成结果未知
    async with SessionLocal() as s:
        row = await s.get(Endpoint, endpoint["id"])
        row.environments = {"prod": {**row.environments["prod"], "headers": {"": ""}}}
        await s.commit()
        before = len(erp["recorder"].requests)
        cap = await s.get(Capability, "erp_pay")
        result = await ep.endpoint_request(s, cap, {"amount": 1}, environment="prod", idem_key="k:9")
    assert not result.unknown and "认证头有误" in result.error and "名称不能为空" in result.error
    probe = (await c.post(f"/api/endpoints/{endpoint['id']}/test?env=prod")).json()
    assert probe["ok"] is False and probe["reachable"] is False and "认证头有误" in probe["message"]
    assert len(erp["recorder"].requests) == before, "坏头的请求一个都没发"


async def test_probe_tells_reachable_apart_from_ok(erp):
    c, endpoint, recorder = erp["client"], erp["endpoint"], erp["recorder"]
    ok = (await c.post(f"/api/endpoints/{endpoint['id']}/test?env=prod")).json()
    assert ok["ok"] is True and ok["status"] == 201 and "连接成功" in ok["message"]
    recorder.status["/"] = 404
    missing = (await c.post(f"/api/endpoints/{endpoint['id']}/test?env=prod")).json()
    assert (missing["ok"], missing["reachable"], missing["status"]) == (False, True, 404)
    assert "能连上" in missing["message"] and "404" in missing["message"] and "成功" not in missing["message"], \
        "能连上但路径不存在，不能说成连接成功"
    recorder.status["/"] = 401
    denied = (await c.post(f"/api/endpoints/{endpoint['id']}/test?env=prod")).json()
    assert denied["ok"] is False and "认证" in denied["message"]
    recorder.raise_ = httpx.ConnectError("refused")
    down = (await c.post(f"/api/endpoints/{endpoint['id']}/test?env=prod")).json()
    assert (down["ok"], down["reachable"]) == (False, False) and "无法连接" in down["message"]


async def test_trying_a_write_capability_rehearses_instead_of_sending(erp):
    c, endpoint, recorder = erp["client"], erp["endpoint"], erp["recorder"]
    tool = next(t for t in (await c.get("/api/custom-tools")).json() if t["name"] == "erp_pay")
    before = len(recorder.requests)
    trial = (await c.post(f"/api/custom-tools/{tool['id']}/test", json={"args": {"amount": 3}})).json()
    assert trial["ok"] is True and trial["rehearsal"] is True, trial
    req = trial["result"]["request"]
    assert (req["method"], req["url"], req["body"]) == ("POST", f"{BASE}/payments", {"amount": 3})
    assert trial["result"]["environment"] == "prod", "端点没有测试环境：写正式运行要发往的生产环境"
    assert req["headers"] == ["Authorization", "Idempotency-Key"], "只交回请求头的名字"
    assert "prod-secret-token" not in json.dumps(trial, ensure_ascii=False)
    draft = (await c.post("/api/custom-tools/test", json={**tool, "args": {"amount": 3}})).json()
    assert draft["rehearsal"] is True, "没保存的草稿同样只演练"
    # 工具库执行：同样演练，没有副作用也就不先要确认
    run = await c.post("/api/tools/erp_pay/run", json={"args": {"amount": 3}})
    assert run.status_code == 200 and run.json()["rehearsal"] is True and run.json()["result"]["request"]["method"] == "POST"
    assert len(recorder.requests) == before, "写入能力在工具页一个请求都没发"
    # 读取照旧真的发，对方回 4xx 时照实说失败和状态码
    recorder.status["/orders"] = 400
    read = (await c.post("/api/tools/erp_orders/run?confirm=true", json={"args": {"q": "x"}})).json()
    assert read["ok"] is False and "HTTP 400" in read["error"] and "nope" in read["detail"]
    orders = next(t for t in (await c.get("/api/custom-tools")).json() if t["name"] == "erp_orders")
    tried = (await c.post(f"/api/custom-tools/{orders['id']}/test", json={"args": {"q": "x"}})).json()
    assert tried["ok"] is False and "HTTP 400" in tried["error"] and not tried.get("rehearsal")
    assert len(recorder.requests) == before + 2


async def test_a_draft_trial_sends_the_draft_not_the_saved_tool(erp):
    """工具页试运行的草稿按草稿的配置拼请求，读写按已保存的治理和草稿的方法里更严的一个认。以前按名字找已保存的那一版：
    把已保存的 GET /orders 改成草稿 POST /payments 去试，真发出去的是 GET /orders，结果写「成功」。"""
    c, recorder = erp["client"], erp["recorder"]
    orders = next(t for t in (await c.get("/api/custom-tools")).json() if t["name"] == "erp_orders")
    before = len(recorder.requests)
    post = {**orders, "config": {**orders["config"], "method": "POST", "path": "/payments", "query": {}}}
    trial = (await c.post("/api/custom-tools/test", json={**post, "args": {"q": "x"}})).json()
    assert trial["ok"] is True and trial["rehearsal"] is True, trial
    req = trial["result"]["request"]
    assert (req["method"], req["url"]) == ("POST", f"{BASE}/payments"), req
    assert len(recorder.requests) == before, "草稿改成了写入：已保存的那一版是只读也只演练，一个请求都不发"

    # 草稿只改了路径和查询参数（还是读取）：真的发，发的是草稿的
    moved = {**orders, "config": {**orders["config"], "path": "/orders/open", "query": {"status": "{{ q }}"}}}
    read = (await c.post("/api/custom-tools/test", json={**moved, "args": {"q": "x"}})).json()
    assert read["ok"] is True and not read.get("rehearsal"), read
    [sent] = recorder.requests[before:]
    assert (sent.method, sent.url.path, dict(sent.url.params)) == ("GET", "/orders/open", {"status": "x"})

    # 还没保存过的绑定端点的读取：同样按草稿发（以前按名字找不到已保存的工具，报没有绑定端点）
    fresh = (await c.post("/api/custom-tools/test", json={**moved, "name": "erp_orders_new", "args": {"q": "y"}})).json()
    assert fresh["ok"] is True, fresh
    assert recorder.requests[-1].url.path == "/orders/open" and len(recorder.requests) == before + 2

    # 保存过的那一行和登记对得上：照治理的走（管理员把 POST 的查询接口治理成只读，是他的决定）
    async with SessionLocal() as s:
        from app.tools.custom import _effect_of

        pay_cap = await s.get(Capability, "erp_pay")
        pay = (await s.execute(select(CustomTool).where(CustomTool.name == "erp_pay"))).scalar_one()
        pay_cap.effect = "read"
        assert _effect_of(pay, pay_cap) == "read"
        draft = CustomTool(name="erp_pay", kind="http", config={**pay.config, "method": "GET"})
        assert _effect_of(draft, pay_cap) == "read"
        pay_cap.effect = None
        assert _effect_of(draft, pay_cap) == "write", "草稿改成了 GET：已保存的那一版是写入，取更严的"
        await s.rollback()


async def test_custom_tool_header_names_are_checked():
    c = auth_env.client()
    r = await c.post("/api/custom-tools", json={"name": "bad_headers", "kind": "http", "parameters": {},
                                                "config": {"url": "https://crm.example.com", "headers": {"": "x"}}})
    assert r.status_code == 422 and "名称不能为空" in r.json()["detail"]
    r = await c.post("/api/custom-tools", json={"name": "bad_headers", "kind": "http", "parameters": {},
                                                "config": {"url": "https://crm.example.com",
                                                           "headers": {"Authorization": "Bearer {{ 令牌 }}"}}})
    assert r.status_code == 201, "值里的模板不查"
    async with SessionLocal() as s:
        await s.execute(delete(CustomTool).where(CustomTool.name == "bad_headers"))
        await s.commit()
