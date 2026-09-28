"""管理页背后的接口：调试台只看不改、危险操作先说清后果、没保存的配置也能先测。

每一条都对应界面上一个「看起来生效、其实没生效」或者「说的和做的不一样」的地方：
回忆测试会抬高召回计数；标着「需确认」的工具点一下就真跑；数据源的提示指向
表单上不存在的入口；填错一项要保存→关弹窗→点测试→再打开改。
"""
from __future__ import annotations

import sqlite3

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from app.db.base import SessionLocal
from app.db.models import DataSource, Provider, Run, RunEvent, Workflow
from app.main import app
from app.memory import embeddings as emb


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture(autouse=True)
def _local_embedder():
    emb.configure("local")
    yield
    emb.configure("local")


@pytest.fixture
def shop_db(tmp_path):
    path = tmp_path / "shop.db"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, amount REAL)")
    db.execute("INSERT INTO orders VALUES (1, 9.5)")
    db.commit()
    db.close()
    return str(path)


async def _count(model) -> int:
    async with SessionLocal() as session:
        return int((await session.execute(select(func.count()).select_from(model))).scalar_one())


def _no_class_names(text: str) -> None:
    for name in ("Error:", "Exception", "Traceback", "<locals>"):
        assert name not in text, f"面向用户的报错里露出了内部符号 {name!r}：{text}"


# --------------------------------------------------------------------------
# manage-4：调试台和运行时对得上，回忆测试只看不改
# --------------------------------------------------------------------------


async def _use_count(client, scope: str) -> int:
    items = (await client.get("/api/memory", params={"scope": scope})).json()
    return items[0]["use_count"]


async def test_peek_recall_leaves_the_use_count_alone(client):
    r = await client.post("/api/memory", json={"content": "活跃用户指当月有订单的用户", "scope": "peek"})
    assert r.status_code == 201

    r = await client.get("/api/memory/search", params={"q": "活跃用户", "scope": "peek", "peek": "true"})
    assert r.json()["results"], "peek 也得照常召回，否则调试台就没用了"
    assert await _use_count(client, "peek") == 0

    # 不带 peek 的是运行时的语义：计一次，下次排序时用得上
    await client.get("/api/memory/search", params={"q": "活跃用户", "scope": "peek"})
    assert await _use_count(client, "peek") == 1


async def test_kb_status_and_search_agree_on_the_default_alpha(client):
    status = (await client.get("/api/kb/embedding")).json()
    assert status["default_alpha"] == emb.default_alpha()

    body = (await client.get("/api/kb/search", params={"q": "口径"})).json()
    assert body["alpha"] == status["default_alpha"], "不传 alpha 时实际用了多少，要跟结果一起交出去"
    body = (await client.get("/api/kb/search", params={"q": "口径", "alpha": 0.3})).json()
    assert body["alpha"] == 0.3


# --------------------------------------------------------------------------
# manage-5：危险工具在工具库里直接执行，先说清会做什么
# --------------------------------------------------------------------------


async def test_a_dangerous_tool_is_not_run_without_confirm(client):
    from app.core.config import settings

    target = settings.session_dir("playground") / "confirm-probe.txt"
    target.unlink(missing_ok=True)
    args = {"path": "confirm-probe.txt", "content": "hi"}

    r = await client.post("/api/tools/file_write/run", json={"args": args})
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert "confirm-probe.txt" in detail and "审批" in detail, detail
    # 怎么确认（弹窗、再点一次）是界面的事，后端的话不能替它许诺
    assert "再点" not in detail and "点击" not in detail, detail
    assert not target.exists(), "409 之前就已经写下去了"

    r = await client.post("/api/tools/file_write/run", params={"confirm": "true"}, json={"args": args})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert target.exists()

    target.unlink()
    r = await client.post("/api/tools/file_write/run", json={"args": args, "confirm": True})
    assert r.status_code == 200 and target.exists()
    target.unlink()


async def test_a_safe_tool_runs_straight_away(client):
    r = await client.post("/api/tools/file_list/run", json={"args": {}})
    assert r.status_code == 200 and r.json()["ok"] is True


async def test_the_tool_list_says_whether_runtime_approval_really_applies(client):
    r = await client.post("/api/custom-tools", json={"name": "probe_hook", "kind": "http",
                                                     "config": {"url": "https://example.com"}})
    assert r.status_code == 201
    try:
        tools = {t["id"]: t for t in (await client.get("/api/tools")).json()}
        assert tools["file_write"]["runtime_approval"] is True
        assert tools["file_read"]["runtime_approval"] is False
        # 有副作用，但运行时的审批关卡认不出它——标签不能说它会被审批
        assert tools["probe_hook"]["dangerous"] is True
        assert tools["probe_hook"]["runtime_approval"] is False
    finally:
        await client.delete(f"/api/custom-tools/{r.json()['id']}")


# --------------------------------------------------------------------------
# manage-7：提示只提表单上有的东西
# --------------------------------------------------------------------------


async def test_datasource_hints_only_point_at_fields_the_form_has(client):
    kinds = {k["value"]: k for k in (await client.get("/api/datasources/kinds")).json()["kinds"]}
    for kind in kinds.values():
        assert "options" not in kind["hint"], f"{kind['value']} 的提示指向了表单上没有的 options：{kind['hint']}"
    assert "schema" in kinds["oracle"]["hint"] and "SID" in kinds["oracle"]["hint"]
    # 高级连接参数里有什么，由后端给出：提示里提到的键在这里都找得到
    assert "charset" in [a["key"] for a in kinds["mysql"]["advanced"]]
    assert {"service_name", "sid"} <= {a["key"] for a in kinds["oracle"]["advanced"]}


async def _oracle(name: str, **fields) -> dict:
    """直接写库，好造出新表单之前的存法：service_name 在「数据库」一栏里。"""
    async with SessionLocal() as session:
        row = DataSource(name=name, kind="oracle", host="db", username="u", **fields)
        session.add(row)
        await session.commit()
        return {"id": row.id}


async def _oracle_url(source_id: str) -> str:
    from app.data.engine import build_url

    async with SessionLocal() as session:
        return build_url(await session.get(DataSource, source_id))


async def test_switching_an_old_oracle_source_to_sid_is_not_ignored(client):
    """引擎是 `service_name or database`：老数据的「数据库」一栏有值时，新表单切到
    SID 存下去会被悄悄无视，连的还是原来那个 service_name。"""
    src = await _oracle("ora_sid", database="ORCLPDB1", options={})
    try:
        r = await client.patch(f"/api/datasources/{src['id']}", json={"options": {"sid": "XE"}})
        assert r.status_code == 200, r.text
        assert r.json()["database"] is None and r.json()["options"] == {"sid": "XE"}
        assert (await _oracle_url(src["id"])).endswith("/?sid=XE")

        r = await client.patch(f"/api/datasources/{src['id']}", json={"options": {"service_name": "BI"}})
        assert (await _oracle_url(src["id"])).endswith("/?service_name=BI")
    finally:
        await client.delete(f"/api/datasources/{src['id']}")


async def test_oracle_service_name_is_kept_in_one_place(client):
    # 改无关的字段：老数据挪进 options.service_name，连的还是同一个库
    src = await _oracle("ora_legacy", database="ORCLPDB1", options={"schema": "ODS"})
    try:
        r = await client.patch(f"/api/datasources/{src['id']}", json={"enabled": False})
        body = r.json()
        assert body["database"] is None
        assert body["options"] == {"schema": "ODS", "service_name": "ORCLPDB1"}
        assert (await _oracle_url(src["id"])).endswith("/?service_name=ORCLPDB1")
    finally:
        await client.delete(f"/api/datasources/{src['id']}")

    # 旧表单把 service_name 框绑在「数据库」上，保存时整张表单连 options 一起发：
    # 新填的值得生效，不能被 options 里原样带回来的旧值压住
    src = await _oracle("ora_oldform", database=None, options={"service_name": "BI", "schema": "ODS"})
    try:
        r = await client.patch(f"/api/datasources/{src['id']}", json={
            "database": "BI2", "options": {"service_name": "BI", "schema": "ODS"}})
        assert r.json()["options"] == {"service_name": "BI2", "schema": "ODS"}
        assert (await _oracle_url(src["id"])).endswith("/?service_name=BI2")
    finally:
        await client.delete(f"/api/datasources/{src['id']}")

    # 两处都有值的老数据原样再存一次：引擎原来连哪个，存完还连哪个
    src = await _oracle("ora_both", database="STALE", options={"service_name": "BI"})
    try:
        r = await client.patch(f"/api/datasources/{src['id']}", json={
            "database": "STALE", "options": {"service_name": "BI"}})
        assert r.json()["database"] is None
        assert (await _oracle_url(src["id"])).endswith("/?service_name=BI")
    finally:
        await client.delete(f"/api/datasources/{src['id']}")

    r = await client.post("/api/datasources", json={"name": "ora_new", "kind": "oracle", "host": "db",
                                                   "username": "u", "database": "LEFTOVER",
                                                   "options": {"sid": "XE"}})
    assert r.status_code == 201, r.text
    created = r.json()
    try:
        assert created["database"] is None and created["options"] == {"sid": "XE"}
    finally:
        await client.delete(f"/api/datasources/{created['id']}")


# --------------------------------------------------------------------------
# manage-8：没保存的配置先测，不落库
# --------------------------------------------------------------------------


async def test_testing_an_unsaved_datasource_does_not_save_it(client, shop_db):
    before = await _count(DataSource)

    r = await client.post("/api/datasources/test", json={"name": "draft", "kind": "sqlite",
                                                        "database": shop_db})
    assert r.status_code == 200 and r.json()["ok"] is True, r.text

    r = await client.post("/api/datasources/test", json={"kind": "sqlite",
                                                        "database": "/no/such/dir/x.db"})
    body = r.json()
    assert body["ok"] is False
    _no_class_names(body["error"])
    assert body["hint"] and body["detail"], "人话之外，原始报错也得留着给排查用"

    assert await _count(DataSource) == before, "测试接口往库里写了东西"


async def test_testing_a_mistyped_sqlite_path_does_not_create_the_file(client, tmp_path):
    """可写模式下 SQLite 连一个不存在的路径会当场建一个空库：测试说「连得上」，
    指的却是一个空文件，而且「只测不存」的接口往磁盘上写了东西。"""
    missing = tmp_path / "typo.db"
    r = await client.post("/api/datasources/test", json={"kind": "sqlite", "database": str(missing),
                                                        "readonly": False})
    body = r.json()
    assert body["ok"] is False, body
    assert "打不开这个数据库文件" in body["error"] and body["hint"] and body["detail"]
    assert not missing.exists(), "测连接把一个空库建出来了"

    r = await client.post("/api/datasources/test", json={"kind": "sqlite", "database": "", "readonly": False})
    assert r.json()["ok"] is False, "没填路径时连的是内存库，说「连得上」是假的"

    # 已保存的数据源走的是缓存连接，同样不能顺手建文件
    r = await client.post("/api/datasources", json={"name": "typo_src", "kind": "sqlite",
                                                   "database": str(missing), "readonly": False})
    saved = r.json()
    try:
        body = (await client.post(f"/api/datasources/{saved['id']}/test")).json()
        assert body["ok"] is False and not missing.exists()
    finally:
        await client.delete(f"/api/datasources/{saved['id']}")


async def test_draft_test_of_an_existing_source_keeps_the_saved_password(client, shop_db):
    from app.api.datasources import DataSourceTestIn, _draft_source
    from app.core.crypto import decrypt, encrypt

    row = DataSource(name="saved", kind="postgres", host="db", password=encrypt("s3cret"))
    draft = _draft_source(DataSourceTestIn(kind="postgres", host="db2"), row)
    assert decrypt(draft.password) == "s3cret", "编辑时密码框留空表示不改，测连接也得用存着的那个"
    assert draft.host == "db2"
    draft = _draft_source(DataSourceTestIn(kind="postgres", password="new"), row)
    assert decrypt(draft.password) == "new"


async def test_testing_an_unsaved_provider_does_not_save_it(client):
    before = await _count(Provider)

    r = await client.post("/api/providers/test", json={"name": "草稿", "kind": "mock",
                                                      "default_model": "mock-fast"})
    body = r.json()
    assert body["ok"] is True, body
    assert body["reply"]

    # 设置页照着契约里的路径调也得通
    r = await client.post("/api/settings/providers/test", json={"kind": "mock", "default_model": "mock-fast"})
    assert r.json()["ok"] is True

    r = await client.post("/api/providers/test", json={"kind": "openai_compatible", "api_key": "k",
                                                      "default_model": "m"})
    body = r.json()
    assert body["ok"] is False and "Base URL" in body["error"]

    assert await _count(Provider) == before


async def test_an_openai_compatible_provider_needs_a_base_url(client):
    r = await client.post("/api/providers", json={"name": "no-url", "kind": "openai_compatible",
                                                 "api_key": "k"})
    assert r.status_code == 400 and "Base URL" in r.json()["detail"]


async def test_listing_models_of_an_unsaved_provider(client):
    r = await client.post("/api/providers/models", json={"kind": "mock"})
    body = r.json()
    assert body["ok"] is True and "mock-fast" in body["models"]

    # 连不上时照实说，不抛 500
    r = await client.post("/api/providers/models", json={"kind": "openai_compatible",
                                                        "base_url": "http://127.0.0.1:9/v1"})
    body = r.json()
    assert body["ok"] is False
    _no_class_names(body["error"])


@pytest.fixture
def models_endpoint():
    """一个本地的 /models：记下每次请求带了哪些头，拿来看钥匙发给了谁。"""
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    seen: list[dict[str, str]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - http.server 的约定
            seen.append({k.lower(): v for k, v in self.headers.items()})
            body = json.dumps({"data": [{"id": "m1"}]}).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", seen
    finally:
        server.shutdown()
        server.server_close()


async def test_listing_models_never_sends_the_env_openai_key_to_another_host(
    client, models_endpoint, monkeypatch
):
    base, seen = models_endpoint
    monkeypatch.setenv("OPENAI_API_KEY", "sk-env-secret")
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL"):
        monkeypatch.delenv(name, raising=False)

    # 表单上还没填 Key 就点「拉取模型」：兼容网关是别人家的服务，环境里那把
    # OpenAI 的钥匙不能顺手发过去——真正调用时（factory）也从不这么做
    r = await client.post("/api/providers/models", json={"kind": "openai_compatible",
                                                        "base_url": f"{base}/v1"})
    assert r.json()["models"] == ["m1"]
    assert "authorization" not in seen[-1], seen[-1]

    r = await client.post("/api/providers/models", json={"kind": "openai_compatible", "api_key": "sk-typed",
                                                        "base_url": f"{base}/v1"})
    assert seen[-1]["authorization"] == "Bearer sk-typed"

    # 官方 OpenAI 本来就会退回环境变量，和真正调用时一致
    r = await client.post("/api/providers/models", json={"kind": "openai", "base_url": f"{base}/v1"})
    assert seen[-1]["authorization"] == "Bearer sk-env-secret"


async def test_listing_models_authenticates_like_the_real_call(client, models_endpoint):
    base, seen = models_endpoint

    await client.post("/api/providers/models", json={"kind": "anthropic", "api_key": "k1", "base_url": base})
    assert seen[-1]["x-api-key"] == "k1" and "authorization" not in seen[-1]

    await client.post("/api/providers/models", json={"kind": "anthropic", "api_key": "k2", "base_url": base,
                                                     "extra": {"auth_style": "bearer"}})
    assert seen[-1]["authorization"] == "Bearer k2" and "x-api-key" not in seen[-1]

    # 编辑已有接入时，请求头回显的是 ***：拉模型得换回存着的真值
    r = await client.post("/api/providers", json={
        "name": "gw-headers", "kind": "openai_compatible", "base_url": f"{base}/v1", "api_key": "k3",
        "extra": {"headers": {"X-Tenant": "acme-real"}},
    })
    assert r.status_code in (200, 201), r.text
    saved = r.json()
    try:
        await client.post("/api/providers/models", json={
            "id": saved["id"], "kind": "openai_compatible", "base_url": f"{base}/v1",
            "extra": {"headers": {"X-Tenant": "***"}},
        })
        assert seen[-1]["x-tenant"] == "acme-real"
        assert seen[-1]["authorization"] == "Bearer k3", "Key 框留空表示沿用已保存的"
    finally:
        await client.delete(f"/api/providers/{saved['id']}")


# --------------------------------------------------------------------------
# manage-17：记忆看得出是哪来的，能原地改
# --------------------------------------------------------------------------


async def test_memory_items_say_where_they_came_from_and_can_be_edited(client):
    async with SessionLocal() as session:
        run = Run(workflow_name="周报", status="succeeded", graph={"nodes": [
            {"id": "remember_table", "type": "memory", "data": {"label": "记下口径", "config": {}}},
        ], "edges": []})
        session.add(run)
        await session.commit()
        run_id = run.id

    r = await client.post("/api/memory", json={"content": "毛利率按含税收入算", "scope": "src",
                                               "meta": {"run_id": run_id, "node_id": "remember_table"}})
    from_run = r.json()
    assert from_run["source"] == {"kind": "run", "run_id": run_id, "node_id": "remember_table",
                                  "node_label": "记下口径", "workflow_name": "周报", "run_exists": True}
    assert from_run["created_at"]

    r = await client.post("/api/memory", json={"content": "我叫张三", "scope": "src"})
    assert r.json()["source"]["kind"] == "manual"

    listed = {m["id"]: m for m in (await client.get("/api/memory", params={"scope": "src"})).json()}
    assert listed[from_run["id"]]["source"]["node_label"] == "记下口径"

    r = await client.patch(f"/api/memory/{from_run['id']}",
                           json={"content": "毛利率按不含税收入算", "importance": 0.8})
    assert r.status_code == 200, r.text
    edited = r.json()
    assert edited["content"] == "毛利率按不含税收入算" and edited["importance"] == 0.8
    assert edited["source"]["run_id"] == run_id, "改内容不该把来源抹掉"
    from app.db.models import MemoryItem
    from app.memory.embeddings import embed_text, from_blob

    async with SessionLocal() as session:
        row = await session.get(MemoryItem, from_run["id"])
        stored = from_blob(row.embedding, row.embed_dim)
    fresh = await embed_text("毛利率按不含税收入算")
    assert stored is not None and list(stored) == list(fresh), "改完内容，向量得跟着重算"

    assert (await client.patch("/api/memory/nope", json={"content": "x"})).status_code == 404
    # 只有空白的内容和空串是一回事：存下去就是一条空记忆
    for blank in ("", "   ", "\n\t"):
        r = await client.patch(f"/api/memory/{from_run['id']}", json={"content": blank})
        assert r.status_code == 422, (blank, r.text)
    assert (await client.post("/api/memory", json={"content": "  ", "scope": "src"})).status_code == 422


async def test_memory_items_do_not_offer_a_time_that_moves_on_recall(client):
    """updated_at 每次召回都会跟着动（记计数也是一次写），当「修改于」显示就是错的。

    界面要的两个时间是「记下于」和「上次召回」，各自有字段。
    """
    r = await client.post("/api/memory", json={"content": "库存周转按月末库存算", "scope": "when"})
    item = r.json()
    assert item["created_at"] and "last_used_at" in item
    assert "updated_at" not in item
    await client.get("/api/memory/search", params={"q": "库存周转", "scope": "when"})
    listed = (await client.get("/api/memory", params={"scope": "when"})).json()[0]
    assert listed["last_used_at"] and "updated_at" not in listed


# --------------------------------------------------------------------------
# manage-6：知识库收什么格式，一处定义
# --------------------------------------------------------------------------


async def test_kb_formats_come_from_the_parser(client):
    formats = (await client.get("/api/kb/formats")).json()
    assert ".pptx" in formats["extensions"]
    assert ".csv" not in formats["extensions"] and ".csv" in formats["tabular"]
    assert formats["accept"] == ",".join(formats["extensions"])

    from app.memory.parsing import extract

    for ext in formats["text"]:
        assert extract("你好".encode(), f"a{ext}") == "你好"


async def test_a_csv_upload_is_pointed_at_datasources(client):
    r = await client.post("/api/kb/upload", files={"file": ("sales.csv", b"a,b\n1,2", "text/csv")})
    assert r.status_code == 415
    detail = r.json()["detail"]
    assert "数据源" in detail and "传表格" in detail, detail


# --------------------------------------------------------------------------
# studio-05：取一个历史版本的图
# --------------------------------------------------------------------------


def _graph(field: str) -> dict:
    return {"nodes": [{"id": "a", "type": "input", "position": {"x": 0, "y": 0},
                       "data": {"label": "入口", "config": {"fields": [{"name": field}]}}}],
            "edges": []}


async def test_a_published_version_can_be_fetched_with_its_input_fields(client):
    wf = (await client.post("/api/workflows", json={"name": "版本", "graph": _graph("q")})).json()
    await client.patch(f"/api/workflows/{wf['id']}", json={"graph": _graph("q2")})

    r = await client.get(f"/api/workflows/{wf['id']}/versions/1")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["version"] == 1 and body["graph"] == _graph("q")
    assert [f["name"] for f in body["input_fields"]] == ["q"]
    assert body["graph_hash"]

    assert (await client.get(f"/api/workflows/{wf['id']}/versions/9")).status_code == 404
    assert (await client.get("/api/workflows/nope/versions/1")).status_code == 404
    await client.delete(f"/api/workflows/{wf['id']}")


# --------------------------------------------------------------------------
# runs-15：只从跑通的运行提取模板
# --------------------------------------------------------------------------


async def _run_with(status: str) -> str:
    graph = {"nodes": [{"id": "a", "type": "input", "data": {"label": "入口", "config": {}}}],
             "edges": []}
    async with SessionLocal() as session:
        run = Run(workflow_name="提取", status=status, graph=graph)
        session.add(run)
        await session.flush()
        session.add(RunEvent(run_id=run.id, seq=1, type="node.started", node_id="a", ts=0.0, data={}))
        await session.commit()
        return run.id


async def test_only_a_succeeded_run_can_become_a_template(client):
    for status in ("failed", "cancelled", "interrupted", "running"):
        r = await client.post("/api/copilot/from-run", json={"run_id": await _run_with(status)})
        assert r.status_code == 409, (status, r.text)
        assert "没有跑完" in r.json()["detail"]

    before = await _count(Workflow)
    r = await client.post("/api/copilot/from-run", json={"run_id": await _run_with("succeeded")})
    assert r.status_code == 200, r.text
    assert await _count(Workflow) == before + 1
    await client.delete(f"/api/workflows/{r.json()['workflow_id']}")
