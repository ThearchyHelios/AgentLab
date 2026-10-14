"""表助手（data/catalog_assistant.py，POST /api/datasources/{id}/catalog/{table}/assistant）。

- 开始前：表不在 404；起草模型用不了 409 catalog_draft_model_unavailable（说法同起草任务）；对话超过 40 条、一条超过
  4000 字 422。界面上待保存的修改不设上限（宽表上攒到 200 项以上也照常对话）：背景里只列最近的 200 项、写明另有几项，
  码值照全部待保存的修改合并
- 流：text 增量、query（跑过的只读查询，前 10 行，遮罩列写「已遮罩」）、proposal（按 plan_patch 核对过的提案，形状同助手流
  的 catalog_patch：公式不收、列名必须在表结构里、遮罩列不给码值；有效记录条件要是只用这张表的列的一个条件，有起草的
  校验规矩 sqlcheck.valid_filter_problem 时按它）、done（用量、查询条数）、error（中途出错，之后没有 done）
- 交给模型的背景：表结构、这张表的目录（每项的来源和状态）、关联关系（含别的表指过来的）、使用次数、数据源的起草要求、
  最近一次起草留空的列、界面上还没保存的修改；遮罩列只给列名、不给任何值
- 只读查询：只有开了数据剖析的源才给这个工具；经剖析的只读守卫和单条时限；每次请求最多 5 条；最多 50 行交给模型；结果
  先遮再交给模型；没开剖析时提示词里写明不能查数据
- 对话不存服务端；提案不写目录（保存走 PATCH，最多 200 项）

表名、列名、业务数字都是虚构的；号码是 139000000xx 这样的虚构号码。
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from langchain_core.messages import AIMessageChunk, HumanMessage, SystemMessage, ToolMessage

from app.data import catalog, catalog_assistant
from app.data.catalog import make_item
from app.data.engine import CATALOG_DRAFT_GUIDANCE_OPTION
from app.db.base import SessionLocal
from app.db.models import CatalogDraftJob, CatalogDraftTask, DataSource
from app.main import app as _fastapi_app
from tests.fixtures.sources import drop_source

PHONES = [f"139000000{i:02d}" for i in range(1, 31)]


# --------------------------------------------------------------------------
# 假模型：按剧本一轮一轮回
# --------------------------------------------------------------------------


@dataclass
class Turn:
    text: list[str] = field(default_factory=list)
    calls: list[tuple[str, dict]] = field(default_factory=list)
    cut: bool = False
    usage: tuple[int, int] = (100, 20)
    error: BaseException | None = None
    hang: bool = False


class Scripted:
    """bind_tools 记下交给它的工具；astream 每调一次按剧本回一轮，记下这一轮收到的消息。"""

    def __init__(self, turns: list[Turn]) -> None:
        self.turns = list(turns)
        self.seen: list[list[Any]] = []
        self.tools: list[Any] | None = None

    def bind_tools(self, tools: list[Any], **_: Any) -> "Scripted":
        self.tools = tools
        return self

    async def astream(self, messages: list[Any]):
        self.seen.append(list(messages))
        turn = self.turns.pop(0)
        if turn.error is not None:
            raise turn.error
        if turn.hang:
            await asyncio.sleep(3600)
        for piece in turn.text:
            yield AIMessageChunk(content=piece)
        for i, (name, args) in enumerate(turn.calls):
            yield AIMessageChunk(content="", tool_call_chunks=[
                {"name": name, "args": json.dumps(args, ensure_ascii=False), "id": f"c{len(self.seen)}_{i}",
                 "index": i}])
        yield AIMessageChunk(content="", usage_metadata={"input_tokens": turn.usage[0], "output_tokens": turn.usage[1],
                                                         "total_tokens": sum(turn.usage)},
                             response_metadata={"finish_reason": "length" if turn.cut else "stop"})

    def tool_names(self) -> list[str]:
        return [t["function"]["name"] for t in self.tools or []]


class _Unavailable(Exception):
    status_code = 503


# --------------------------------------------------------------------------
# 夹具
# --------------------------------------------------------------------------


def _shop_db(path) -> str:
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE members (id INTEGER PRIMARY KEY, nick TEXT);
        CREATE TABLE orders (id INTEGER PRIMARY KEY, order_no TEXT NOT NULL, status INTEGER NOT NULL,
                             amount REAL, buyer_phone TEXT, created_at TEXT,
                             member_id INTEGER REFERENCES members(id));
    """)
    conn.executemany("INSERT INTO members VALUES (?, ?)", [(i, f"会员{i}") for i in range(1, 6)])
    conn.executemany("INSERT INTO orders VALUES (?, ?, ?, ?, ?, ?, ?)", [
        (i, f"NO{i:04d}", 9 if i % 10 == 0 else 1, 10.0 * i, PHONES[i - 1], f"2026-09-{i:02d}", i % 5 + 1)
        for i in range(1, 31)])
    conn.commit()
    conn.close()
    return str(path)


ORDERS_NOTES = {
    "label": make_item("订单", "llm"),
    "grain": make_item("每笔订单一行", "human", "confirmed"),
    "kind": make_item("fact", "llm", "rejected"),
    "columns": {
        "status": {"codes": make_item({"1": "已支付"}, "profile", note="数据剖析：全表 30 行，2 个取值")},
        "buyer_phone": {"label": make_item("联系电话", "llm"),
                          "codes": make_item({PHONES[0]: "测试号"}, "human", "confirmed")},
    },
}

_CREATED: list[str] = []


@pytest.fixture(autouse=True)
async def _cleanup():
    yield
    while _CREATED:
        await drop_source(_CREATED.pop())


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=_fastapi_app), base_url="http://test") as c:
        yield c


async def _source(client, tmp_path, *, profile: bool = True, guidance: str | None = "金额一律按元",
                  notes: dict | None = None) -> str:
    options: dict[str, Any] = {"mask_columns": "buyer_phone"}
    if profile:
        options["catalog_profile"] = {"enabled": True}
    if guidance:
        options[CATALOG_DRAFT_GUIDANCE_OPTION] = guidance
    async with SessionLocal() as session:
        row = DataSource(name=f"shop_{uuid.uuid4().hex[:8]}", kind="sqlite", database=_shop_db(tmp_path / "shop.db"),
                         options=options, description="虚构的网店库")
        session.add(row)
        await session.commit()
        sid = row.id
    _CREATED.append(sid)
    r = await client.post(f"/api/datasources/{sid}/introspect")
    assert r.status_code == 200, r.text
    async with SessionLocal() as session:
        await catalog.write_entry(session, sid, "orders", notes if notes is not None else ORDERS_NOTES,
                                  if_version=0, actor=None)
    return sid


def use(monkeypatch, model: Scripted) -> None:
    async def resolve(session):
        return model, "fake-chat"

    monkeypatch.setattr(catalog_assistant, "resolve_model", resolve)


def _url(sid: str, table: str = "orders") -> str:
    return f"/api/datasources/{sid}/catalog/{table}/assistant"


async def _talk(client, sid: str, body: dict, table: str = "orders") -> tuple[list[dict], list[str]]:
    """发一轮，返回 (事件, 原始行)。"""
    events: list[dict] = []
    lines: list[str] = []
    async with client.stream("POST", _url(sid, table), json=body) as r:
        assert r.status_code == 200, await r.aread()
        assert r.headers["content-type"].startswith("text/event-stream")
        async for line in r.aiter_lines():
            lines.append(line)
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))
    return events, lines


def _text(events: list[dict]) -> str:
    return "".join(e["delta"] for e in events if e["type"] == "text")


def _ask(text: str, **extra) -> dict:
    return {"messages": [{"role": "user", "content": text}], **extra}


# --------------------------------------------------------------------------
# 开始前的检查
# --------------------------------------------------------------------------


async def test_refusals_before_the_stream(client, tmp_path, monkeypatch):
    sid = await _source(client, tmp_path)
    use(monkeypatch, Scripted([]))
    assert (await client.post(_url(sid, "ghost"), json=_ask("你好"))).status_code == 404
    assert (await client.post("/api/datasources/nope/catalog/orders/assistant", json=_ask("x"))).status_code == 404
    too_many = {"messages": [{"role": "user", "content": "x"}] * 41}
    assert (await client.post(_url(sid), json=too_many)).status_code == 422
    too_long = _ask("长" * 4001)
    assert (await client.post(_url(sid), json=too_long)).status_code == 422
    wrong_role = {"messages": [{"role": "system", "content": "x"}]}
    assert (await client.post(_url(sid), json=wrong_role)).status_code == 422
    last_is_answer = {"messages": [{"role": "user", "content": "x"}, {"role": "assistant", "content": "y"}]}
    r = await client.post(_url(sid), json=last_is_answer)
    assert r.status_code == 422 and "最后一条" in r.json()["detail"]


async def test_unavailable_draft_model_is_409_with_the_draft_wording(client, tmp_path, monkeypatch):
    sid = await _source(client, tmp_path)

    async def unavailable(session):
        raise catalog.CatalogModelUnavailable("未配置模型接入，无法由模型起草数据目录。请到「设置 → 模型接入」添加")

    monkeypatch.setattr(catalog, "resolve_draft_model", unavailable)
    r = await client.post(_url(sid), json=_ask("你好"))
    assert r.status_code == 409
    assert r.json()["code"] == "catalog_draft_model_unavailable" and "模型接入" in r.json()["detail"]


@pytest.fixture
async def mem(monkeypatch):
    """单独的内存库：接入和设置只在这个用例里。"""
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
    from sqlalchemy.pool import StaticPool

    from app.db.base import Base

    for key in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY", "ANTHROPIC_BASE_URL"):
        monkeypatch.delenv(key, raising=False)
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)()
    yield session
    await session.close()
    await engine.dispose()


async def _provider(session, kind: str) -> None:
    from app.core.crypto import encrypt
    from app.data import draft_model
    from app.db.models import Provider, Setting

    session.add(Provider(name="起草网关", kind=kind, base_url="http://gw.example.test/v1" if kind != "anthropic" else None,
                         api_key=encrypt("sk-test"), models=[{"id": "模型甲"}], default_model="模型甲", enabled=True,
                         extra={}))
    session.add(Setting(key=draft_model.CATALOG_DRAFT_SETTING_KEY, value={"provider": "起草网关", "model": None}))
    await session.commit()


async def test_model_follows_the_draft_setting_keeps_thinking_and_a_big_budget(mem, monkeypatch):
    from app.providers import factory

    await _provider(mem, "openai_compatible")
    specs: list[Any] = []
    real = factory.build_chat_model

    def spy(provider, spec):
        specs.append(spec)
        return real(provider, spec)

    monkeypatch.setattr(factory, "build_chat_model", spy)
    model, model_id = await catalog_assistant.resolve_model(mem)
    assert model_id == "模型甲" and model.max_tokens == catalog.MODEL_MAX_TOKENS == 32768
    mine = specs[-2:]                      # 前两份是 catalog.resolve_draft_model 查能不能用时建的
    assert [s.max_tokens for s in mine] == [catalog.MODEL_MAX_TOKENS, catalog.MODEL_FALLBACK_MAX_TOKENS]
    assert all(s.thinking is None and s.temperature == 0 and s.max_retries == 0 for s in mine)
    assert all(s.timeout == catalog.MODEL_TIMEOUT_S for s in mine)


@pytest.mark.parametrize("kind", ["openai_compatible", "anthropic"])
async def test_tools_bind_on_real_chat_models(mem, kind):
    """两个工具的定义各家的模型都收（只绑定，不发请求）。"""
    await _provider(mem, kind)
    model, _ = await catalog_assistant.resolve_model(mem)
    tools = [catalog_assistant.QUERY_TOOL_PROMPT, catalog_assistant.PROPOSE_TOOL_PROMPT]
    bound = model.bind_tools(tools)
    given = bound.model.kwargs["tools"]
    names = [t.get("name") or t["function"]["name"] for t in given]
    assert names == ["run_query", "propose_changes"]


# --------------------------------------------------------------------------
# 背景
# --------------------------------------------------------------------------


async def _blank_draft(sid: str) -> None:
    """最近一次结束于 ok 的起草任务记下留空的列；之后一次停止了的不算。"""
    async with SessionLocal() as session:
        done = CatalogDraftJob(id=uuid.uuid4().hex, source_id=sid, status="succeeded",
                               created_at=datetime(2026, 10, 1, tzinfo=timezone.utc))
        stopped = CatalogDraftJob(id=uuid.uuid4().hex, source_id=sid, status="cancelled",
                                  created_at=datetime(2026, 10, 2, tzinfo=timezone.utc))
        session.add_all([done, stopped])
        await session.flush()
        session.add_all([
            CatalogDraftTask(job_id=done.id, seq=0, table_name="orders", status="ok", blank_columns=2,
                             blank_sample=["order_no", "created_at"]),
            CatalogDraftTask(job_id=stopped.id, seq=0, table_name="orders", status="cancelled", blank_columns=0),
        ])
        await session.commit()


async def test_background_has_structure_catalog_and_hides_masked_values(client, tmp_path, monkeypatch):
    sid = await _source(client, tmp_path)
    await _blank_draft(sid)
    async with SessionLocal() as session:
        await catalog.write_entry(session, sid, "members", {"relations": [
            {"id": catalog.relation_id("members", ["id"], "orders", ["member_id"]), "columns": ["id"],
             "to_table": "orders", "to_columns": ["member_id"], "cardinality": "one_to_many", "coverage": None,
             "source": "human", "status": "confirmed"}]}, if_version=0, actor=None)
    model = Scripted([Turn(text=["这张表还缺 order_no 的含义。"])])
    use(monkeypatch, model)
    pending = [{"path": "columns.amount.unit", "value": "元", "reason": "用户说：金额是元"}]
    events, _ = await _talk(client, sid, _ask("看看还缺什么", pending=pending))
    assert _text(events) == "这张表还缺 order_no 的含义。"
    assert events[-1] == {"type": "done", "usage": {"input_tokens": 100, "output_tokens": 20}, "queries": 0}
    [messages] = model.seen
    assert isinstance(messages[0], SystemMessage) and isinstance(messages[-1], HumanMessage)
    system = messages[0].content
    # 规矩
    for rule in ("只改数据目录", "不改业务库", "公式", "拿不准", "原话"):
        assert rule in system
    # 有效记录条件的写法（同起草时的校验规矩），免得提了再被退回
    assert "只能用这张表的列" in system and "不能有子查询" in system and "UPPER、LOWER、TRIM、COALESCE" in system
    # 表结构、目录（来源和状态）、使用次数、起草要求、数据源说明
    for fact in ("order_no", "status", "amount", "member_id → members.id", "主键", "每笔订单一行", "人工填写", "已确认",
                 "模型起草", "已驳回", "还没有被运行用到过", "金额一律按元", "虚构的网店库", "数据剖析：全表 30 行"):
        assert fact in system, fact
    # 别的表指过来的关联关系
    assert "members.id → orders.member_id" in system
    # 最近一次结束于 ok 的起草留空的列（之后停止了的那次不算）
    assert "order_no、created_at" in system
    # 界面上还没保存的修改
    assert "columns.amount.unit" in system and "用户说：金额是元" in system
    # 遮罩列只给列名：码值、取值一个都不给
    assert "buyer_phone" in system
    assert not any(p in system for p in PHONES) and "测试号" not in system
    # 没开剖析的源才写不能查数据；这里开了，给了查询工具
    assert model.tool_names() == ["run_query", "propose_changes"]
    assert "没开剖析" not in system


async def test_background_says_how_many_runs_used_the_table(client, tmp_path, monkeypatch):
    """使用次数的口径同审阅队列：被几次运行用到（同「用过这张表的运行」的 total），查询几次（同目录清单的「使用 N 次」）。"""
    from app.db.models import Artifact, Run

    sid = await _source(client, tmp_path)
    async with SessionLocal() as session:
        name = (await session.get(DataSource, sid)).name
        for i, n in enumerate((2, 1)):
            rid = uuid.uuid4().hex
            session.add(Run(id=rid, graph={}, workflow_name=f"周报{i}", status="succeeded"))
            for _ in range(n):
                session.add(Artifact(id=uuid.uuid4().hex, run_id=rid, kind=catalog.QUERY_SNAPSHOT_KIND,
                                     meta={"source_id": sid, "source": name, "tables": ["orders"]}))
        await session.commit()
    model = Scripted([Turn(text=["好。"])])
    use(monkeypatch, model)
    await _talk(client, sid, _ask("看看"))
    assert "这张表被 2 次运行用到（查询 3 次）。" in model.seen[0][0].content


async def test_long_catalogs_are_cut_with_a_note(client, tmp_path, monkeypatch):
    sid = await _source(client, tmp_path)
    monkeypatch.setattr(catalog_assistant, "CATALOG_ITEMS_SHOWN", 2)
    model = Scripted([Turn(text=["好。"])])
    use(monkeypatch, model)
    await _talk(client, sid, _ask("看看"))
    system = model.seen[0][0].content
    # 表级的项在前；遮罩列的码值本来就不算；剩下的说明还有几项
    assert "- label（中文名）：订单" in system and "- grain（粒度）" in system and "- kind（表类型）" not in system
    assert "还有 3 项没有列出" in system


async def test_without_profiling_there_is_no_query_tool(client, tmp_path, monkeypatch):
    sid = await _source(client, tmp_path, profile=False)
    model = Scripted([Turn(text=["只能根据表结构推断。"])])
    use(monkeypatch, model)
    await _talk(client, sid, _ask("状态 9 是什么"))
    assert model.tool_names() == ["propose_changes"]
    assert "这个数据源没开剖析，不能查数据，只能根据表结构和目录推断" in model.seen[0][0].content


async def test_opening_without_user_messages(client, tmp_path, monkeypatch):
    sid = await _source(client, tmp_path)
    model = Scripted([Turn(text=["order_no 是什么意思？"])])
    use(monkeypatch, model)
    events, _ = await _talk(client, sid, {"messages": []})
    assert _text(events) == "order_no 是什么意思？"
    assert isinstance(model.seen[0][-1], HumanMessage)
    # 界面先显示了一句开场白（助手说的）：照样从使用者这一边开始
    model = Scripted([Turn(text=["好的。"])])
    use(monkeypatch, model)
    await _talk(client, sid, {"messages": [{"role": "assistant", "content": "有两列留空了"},
                                           {"role": "user", "content": "order_no 是订单号"}]})
    kinds = [type(m).__name__ for m in model.seen[0]]
    assert kinds == ["SystemMessage", "HumanMessage", "AIMessage", "HumanMessage"]


# --------------------------------------------------------------------------
# 提案
# --------------------------------------------------------------------------


async def test_proposals_are_checked_and_never_written(client, tmp_path, monkeypatch):
    sid = await _source(client, tmp_path)
    changes = [
        {"path": "columns.status.codes", "value": {"9": "作废"}, "reason": "用户说：9 是作废"},
        {"path": "valid_filter", "value": "status <> 9", "reason": "用户说：统计时排除作废"},
        {"path": "columns.amount.meaning", "value": "实收=单价*数量", "reason": "x"},          # 公式：不收
        {"path": "columns.ghost.label", "value": "幽灵", "reason": "x"},                      # 列不存在：不收
        {"path": "columns.buyer_phone.codes", "value": {PHONES[1]: "客服"}, "reason": "x"},  # 遮罩列：不给码值
        {"path": "grain", "value": "每笔订单一行", "reason": "x"},                             # 已经是这个值：不用提
    ]
    model = Scripted([Turn(text=["好的，"], calls=[("propose_changes", {"changes": changes})]),
                      Turn(text=["已经整理成提案。"])])
    use(monkeypatch, model)
    events, _ = await _talk(client, sid, _ask("9 是作废，统计时排除"))
    assert _text(events) == "好的，已经整理成提案。"
    [proposal] = [e for e in events if e["type"] == "proposal"]
    assert proposal["source_id"] == sid and proposal["table"] == "orders" and proposal["version"] == 1
    assert proposal["source"].startswith("shop_") and proposal["table_label"] == "订单"
    by = {c["path"]: c for c in proposal["changes"]}
    assert set(by) == {"columns.status.codes", "valid_filter"}
    codes = by["columns.status.codes"]
    assert codes["label"] == "列 status 的码值" and codes["reason"] == "用户说：9 是作废"
    assert codes["before"] == {"1": "已支付"} and codes["after"] == {"1": "已支付", "9": "作废"}
    assert codes["value"] == {"9": "作废"} and codes["state"] == "change"
    assert by["valid_filter"]["label"] == "表的有效记录条件" and by["valid_filter"]["before"] is None
    # 没收的几项照实告诉模型（不带遮罩列的任何取值）
    tool = model.seen[1][-1]
    assert isinstance(tool, ToolMessage)
    for said in ("2 项", "公式", "ghost", "遮罩", "已经是这个值"):
        assert said in tool.content, said
    assert not any(p in tool.content for p in PHONES)
    assert events[-1]["type"] == "done" and events[-1]["usage"] == {"input_tokens": 200, "output_tokens": 40}
    async with SessionLocal() as session:
        entry = await catalog.read_entry(session, sid, "orders")
    assert entry.version == 1 and "valid_filter" not in entry.notes


async def test_proposal_codes_build_on_the_pending_ones(client, tmp_path, monkeypatch):
    sid = await _source(client, tmp_path)
    model = Scripted([Turn(calls=[("propose_changes", {"changes": [
        {"path": "columns.status.codes", "value": {"9": "作废"}, "reason": "用户说：9 是作废"}]})]),
        Turn(text=["好。"])])
    use(monkeypatch, model)
    pending = [{"path": "columns.status.codes", "value": {"2": "已退款"}, "reason": "用户说：2 是退款"}]
    events, _ = await _talk(client, sid, _ask("9 是作废", pending=pending))
    [proposal] = [e for e in events if e["type"] == "proposal"]
    [change] = proposal["changes"]
    assert change["value"] == {"2": "已退款", "9": "作废"}
    assert change["after"] == {"1": "已支付", "2": "已退款", "9": "作废"}


async def test_many_pending_changes_do_not_block_the_conversation(client, tmp_path, monkeypatch):
    """宽表上逐列补中文名、含义，待保存的修改攒到 200 项以上：照常对话（不 422）。背景里只列最近的 200 项、写明另有几项；
    码值照全部待保存的修改合并（最早那条码值没列进背景，新提的码值也不能把它丢了）。"""
    sid = await _source(client, tmp_path)
    oldest = {"path": "columns.status.codes", "value": {"2": "已退款"}, "reason": "用户说：2 是退款"}
    labels = [{"path": f"columns.c{i:03d}.label", "value": f"第 {i} 列", "reason": f"用户说：第 {i} 列"}
              for i in range(201)]
    model = Scripted([Turn(calls=[("propose_changes", {"changes": [
        {"path": "columns.status.codes", "value": {"9": "作废"}, "reason": "用户说：9 是作废"}]})]),
        Turn(text=["好。"])])
    use(monkeypatch, model)
    events, _ = await _talk(client, sid, _ask("9 是作废", pending=[oldest, *labels]))
    assert events[-1]["type"] == "done"
    system = model.seen[0][0].content
    # 最近的 200 项列出来了，最早的两项没列，写明另有 2 项
    assert "columns.c200.label" in system and "columns.c001.label" in system
    assert "columns.c000.label" not in system and "用户说：2 是退款" not in system
    assert "另有 2 项" in system
    [proposal] = [e for e in events if e["type"] == "proposal"]
    [change] = proposal["changes"]
    assert change["value"] == {"2": "已退款", "9": "作废"}
    # 再多也收
    model = Scripted([Turn(text=["好。"])])
    use(monkeypatch, model)
    many = [{"path": f"columns.c{i:04d}.meaning", "value": "说明", "reason": "x"} for i in range(1200)]
    events, _ = await _talk(client, sid, _ask("接着看", pending=many))
    assert events[-1]["type"] == "done" and "另有 1000 项" in model.seen[0][0].content


async def _propose_filters(client, sid: str, monkeypatch, filters: list[str]) -> tuple[list[dict], Scripted]:
    """模型一轮提一条有效记录条件（同一条提案里同一个路径只认第一次），最后一轮回答。"""
    turns = [Turn(calls=[("propose_changes", {"changes": [
        {"path": "valid_filter", "value": f, "reason": "用户说：统计时排除作废"}]})]) for f in filters]
    model = Scripted([*turns, Turn(text=["好。"])])
    use(monkeypatch, model)
    events, _ = await _talk(client, sid, _ask("统计时排除作废的单子"))
    return events, model


async def test_valid_filter_proposals_must_be_a_condition_on_this_table(client, tmp_path, monkeypatch):
    """有效记录条件是模型写的 SQL：引用表结构里没有的列、别的表的列、带子查询的不交给界面，原因交回模型；合规的照常提。"""
    sid = await _source(client, tmp_path)
    events, model = await _propose_filters(client, sid, monkeypatch, [
        "status <> 9 AND ghost_col = 1",
        "members.nick IS NOT NULL",
        "member_id IN (SELECT id FROM members)",
        "status <> 9",
    ])
    proposals = [e for e in events if e["type"] == "proposal"]
    assert [[c["value"] for c in p["changes"]] for p in proposals] == [["status <> 9"]]
    said = [[m for m in seen if isinstance(m, ToolMessage)][-1].content for seen in model.seen[1:5]]
    assert "ghost_col" in said[0] and "不在表结构里" in said[0] and "没有可以交给使用者的修改" in said[0]
    assert "members.nick" in said[1]
    assert "子查询" in said[2]
    assert "已把 1 项修改交给使用者确认" in said[3]


async def test_valid_filter_proposals_follow_the_drafting_rules(client, tmp_path, monkeypatch):
    """有起草的校验规矩（sqlcheck.valid_filter_problem：只能用几个函数、不能有聚合……）时按它核对，说法照它的。"""
    from app.data import sqlcheck

    sid = await _source(client, tmp_path)
    calls: list[tuple] = []

    def strict(condition, meta, *, kind, table=None):
        calls.append((condition, [c["name"] for c in meta["columns"]], kind, table))
        return "有效行过滤里只能用 UPPER、LOWER、TRIM、COALESCE 这几个函数，不能用 ABS" if "ABS" in condition else None

    monkeypatch.setattr(sqlcheck, "valid_filter_problem", strict, raising=False)
    events, model = await _propose_filters(client, sid, monkeypatch, ["ABS(amount) > 0", "status <> 9"])
    proposals = [e for e in events if e["type"] == "proposal"]
    assert [[c["value"] for c in p["changes"]] for p in proposals] == [["status <> 9"]]
    assert "不能用 ABS" in [m for m in model.seen[1] if isinstance(m, ToolMessage)][-1].content
    assert calls[0][0] == "ABS(amount) > 0" and calls[0][2:] == ("sqlite", "orders")
    assert "status" in calls[0][1] and "member_id" in calls[0][1]


async def test_nothing_acceptable_means_no_proposal_event(client, tmp_path, monkeypatch):
    sid = await _source(client, tmp_path)
    model = Scripted([Turn(calls=[("propose_changes", {"changes": [{"path": "columns.ghost.label", "value": "x"}]})]),
                      Turn(text=["那一列不存在。"])])
    use(monkeypatch, model)
    events, _ = await _talk(client, sid, _ask("ghost 列叫什么"))
    assert not [e for e in events if e["type"] == "proposal"]
    assert "没有可以交给使用者的修改" in model.seen[1][-1].content


# --------------------------------------------------------------------------
# 只读查询
# --------------------------------------------------------------------------


async def test_queries_are_read_only_masked_and_capped(client, tmp_path, monkeypatch):
    sid = await _source(client, tmp_path)
    queries = [
        ("run_query", {"sql": "SELECT status, COUNT(*) AS n FROM orders GROUP BY status ORDER BY status"}),
        ("run_query", {"sql": "SELECT id, buyer_phone AS phone, amount FROM orders ORDER BY id"}),
        ("run_query", {"sql": "DELETE FROM orders"}),
    ]
    model = Scripted([Turn(text=["我查一下。"], calls=queries),
                      Turn(calls=[("run_query", {"sql": "SELECT 1"})] * 3),
                      Turn(text=["9 占一成。"])])
    use(monkeypatch, model)
    events, _ = await _talk(client, sid, _ask("状态分布"))
    shown = [e for e in events if e["type"] == "query"]
    assert len(shown) == 5                                         # 每次请求最多 5 条
    first, second, third = shown[:3]
    assert first["columns"] == ["status", "n"] and first["rows"] == [[1, 27], [9, 3]]
    assert first["row_count"] == 2 and first["error"] is None
    # 遮罩列改了名字也认得出：值写「已遮罩」；界面只给前 10 行
    assert second["columns"] == ["id", "phone", "amount"] and len(second["rows"]) == 10
    assert {row[1] for row in second["rows"]} == {"已遮罩"} and second["row_count"] == 30
    # 只读守卫：写语句不执行
    assert third["error"] and "守卫" in third["error"] and third["rows"] == []
    # 交给模型的：最多 50 行、遮罩值看不到；第 6 条没执行
    second_round = model.seen[1]
    results = [m for m in second_round if isinstance(m, ToolMessage)]
    assert len(results) == 3
    assert json.loads(results[1].content)["rows"][29][1] == "已遮罩"
    assert not any(p in m.content for m in second_round for p in PHONES)
    capped = [m for m in model.seen[2] if isinstance(m, ToolMessage)][-1]
    assert "未执行" in capped.content and "5 条" in capped.content
    assert events[-1] == {"type": "done", "usage": {"input_tokens": 300, "output_tokens": 60}, "queries": 5}
    with sqlite3.connect(tmp_path / "shop.db") as conn:
        assert conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 30


async def test_query_results_given_to_the_model_stop_at_50_rows(client, tmp_path, monkeypatch):
    sid = await _source(client, tmp_path)
    sql = "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n WHERE i < 80) SELECT i FROM n"
    model = Scripted([Turn(calls=[("run_query", {"sql": sql})]), Turn(text=["好。"])])
    use(monkeypatch, model)
    events, _ = await _talk(client, sid, _ask("数一下"))
    [query] = [e for e in events if e["type"] == "query"]
    assert query["row_count"] == 50 and query["truncated"] is True and len(query["rows"]) == 10
    result = json.loads([m for m in model.seen[1] if isinstance(m, ToolMessage)][0].content)
    assert len(result["rows"]) == 50 and result["truncated"] is True


# --------------------------------------------------------------------------
# 中途出错
# --------------------------------------------------------------------------


async def test_errors_end_the_stream_without_done(client, tmp_path, monkeypatch):
    sid = await _source(client, tmp_path)
    use(monkeypatch, Scripted([Turn(error=RuntimeError("接口不认这个模型"))]))
    events, _ = await _talk(client, sid, _ask("你好"))
    assert events[-1]["type"] == "error" and events[-1]["code"] == "assistant_model_error"
    assert "fake-chat" in events[-1]["message"] and not [e for e in events if e["type"] == "done"]
    use(monkeypatch, Scripted([Turn(text=["写到一半"], cut=True)]))
    events, _ = await _talk(client, sid, _ask("你好"))
    assert _text(events) == "写到一半"
    assert events[-1]["type"] == "error" and events[-1]["code"] == "assistant_truncated"
    assert "上限" in events[-1]["message"]


async def test_a_model_without_tools_reports_an_error_event(client, tmp_path, monkeypatch):
    sid = await _source(client, tmp_path)

    class NoTools(Scripted):
        def bind_tools(self, tools, **_):
            raise NotImplementedError("这个模型不支持工具调用")

    use(monkeypatch, NoTools([]))
    events, _ = await _talk(client, sid, _ask("你好"))
    assert events == [{"type": "error", "code": "assistant_failed", "message": events[0]["message"]}]
    assert "工具调用" in events[0]["message"]


async def test_transient_errors_are_retried_before_any_output(client, tmp_path, monkeypatch):
    sid = await _source(client, tmp_path)
    monkeypatch.setattr(catalog_assistant, "RETRY_DELAYS_S", (0.0, 0.0))
    model = Scripted([Turn(error=_Unavailable("服务暂时不可用")), Turn(text=["好了。"])])
    use(monkeypatch, model)
    events, _ = await _talk(client, sid, _ask("你好"))
    assert _text(events) == "好了。" and events[-1]["type"] == "done"
    assert len(model.seen) == 2


async def test_a_call_over_the_time_limit_is_stopped(client, tmp_path, monkeypatch):
    sid = await _source(client, tmp_path)
    monkeypatch.setattr(catalog, "MODEL_TIMEOUT_S", 0.3)
    monkeypatch.setattr(catalog_assistant, "HEARTBEAT_S", 0.1)
    use(monkeypatch, Scripted([Turn(hang=True)]))
    events, lines = await _talk(client, sid, _ask("你好"))
    assert events == [events[-1]] and events[-1]["code"] == "assistant_timeout"
    assert "0.3 秒" in events[-1]["message"]
    # 等模型的时候按拍发 SSE 注释（不是事件），免得中间的代理断开
    assert any(line.startswith(":") for line in lines)


async def test_steps_are_bounded(client, tmp_path, monkeypatch):
    sid = await _source(client, tmp_path)
    monkeypatch.setattr(catalog_assistant, "MAX_ROUNDS", 3)
    again = Turn(calls=[("propose_changes", {"changes": [{"path": "columns.ghost.label", "value": "x"}]})])
    model = Scripted([again, again, again])
    use(monkeypatch, model)
    events, _ = await _talk(client, sid, _ask("你好"))
    assert len(model.seen) == 3
    assert events[-1]["type"] == "error" and events[-1]["code"] == "assistant_rounds"
    # 倒数第二轮的工具结果里提醒模型直接回答
    assert "直接回答" in [m for m in model.seen[2] if isinstance(m, ToolMessage)][-1].content


class _Slow(Scripted):
    """说了半句就一直想：记下这次调用有没有被停掉。"""

    def __init__(self) -> None:
        super().__init__([])
        self.cancelled = False

    async def astream(self, messages: list[Any]):
        self.seen.append(list(messages))
        yield AIMessageChunk(content="我想一想")
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        yield AIMessageChunk(content="……")


async def test_stopping_the_stream_stops_the_model_call():
    """使用者点停止（连接断开、流被关掉）：正在进行的模型调用跟着停，不在后台接着花额度。"""
    from app.data import catalog_profile

    model = _Slow()
    ctx = catalog_assistant.AssistantContext(
        source_id="s", source_name="虚构库", kind="sqlite", table="orders", tables={}, query_source=None, options={},
        masked=set(), can_query=False, settings=catalog_profile.ProfileSettings(), system="系统提示")
    events = catalog_assistant.stream(model, "fake-chat", ctx, [{"role": "user", "content": "你好"}])
    first = await events.__anext__()
    assert json.loads(first[len("data: "):]) == {"type": "text", "delta": "我想一想"}
    await events.aclose()
    assert model.cancelled


# --------------------------------------------------------------------------
# 保存：PATCH 一次最多 200 项
# --------------------------------------------------------------------------


async def test_patch_accepts_up_to_200_changes(client):
    """表助手一次能提很多项（逐列补中文名），保存不再卡在 20 项：预览、保存都收 200 项，201 项 422。"""
    assert catalog.PATCH_MAX_CHANGES == 200
    columns = [f"c{i:03d}" for i in range(200)]
    table = {"qualified": "wide", "primary_key": [], "comment": None, "is_view": False, "foreign_keys": [],
             "unique": [], "columns": [{"name": c, "type": "TEXT", "nullable": True, "comment": None} for c in columns]}
    async with SessionLocal() as session:
        row = DataSource(name=f"wide_{uuid.uuid4().hex[:8]}", kind="sqlite", database=":memory:",
                         schema_cache={"tables": {"wide": table}})
        session.add(row)
        await session.commit()
        sid = row.id
    _CREATED.append(sid)
    base = f"/api/datasources/{sid}/catalog/wide"
    changes = [{"path": f"columns.{c}.label", "value": f"第 {i} 列", "reason": "用户逐列说明"}
               for i, c in enumerate(columns)]
    preview = await client.post(f"{base}/patch/preview", json={"changes": changes})
    assert preview.status_code == 200, preview.text
    assert len(preview.json()["changes"]) == 200 and preview.json()["problems"] == []
    saved = await client.post(f"{base}/patch", json={"changes": changes, "if_version": 0})
    assert saved.status_code == 200, saved.text
    assert len(saved.json()["notes"]["columns"]) == 200
    assert saved.json()["notes"]["columns"]["c199"]["label"]["value"] == "第 199 列"
    too_many = changes + [{"path": "label", "value": "宽表"}]
    assert (await client.post(f"{base}/patch/preview", json={"changes": too_many})).status_code == 422
    assert (await client.post(f"{base}/patch", json={"changes": too_many, "if_version": 1})).status_code == 422
