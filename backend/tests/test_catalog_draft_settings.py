"""起草模型（设置里的 catalog_draft 一组）与起草取模型的顺序。

- 设置组 catalog_draft：{provider, model}，经 PUT /api/settings 写、GET 读，没存过补 {provider: null, model: null}；
  provider 不是字符串、点名的接入不存在或已停用 → 422，说法写设置页上的叫法「起草模型 · 接入」「起草模型 · 模型」
- 取模型的顺序（数据目录起草和按配方导入的 AI 起草共用 draft_model.draft_model_choice）：起草模型填了接入或模型就整组
  用它；否则沿用助手的模型设置；再没有就是默认接入。不跨级拼接入和模型
- 数据目录起草不关思考（按接入默认）、额度 32768，服务方嫌大退回 8192；配方 AI 起草只改取模型的顺序，额度、思考不动

接入名、模型名都是虚构的。
"""
from __future__ import annotations

from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete

from app.data import catalog, draft_model
from app.data import recipe_ai as A
from app.db.base import SessionLocal
from app.db.models import Provider, Setting
from app.main import app

LIVE, OFF, GONE = "起草用接入", "起草用停用接入", "起草用不存在的接入"


async def _clean() -> None:
    async with SessionLocal() as session:
        await session.execute(delete(Setting).where(Setting.key.in_([draft_model.CATALOG_DRAFT_SETTING_KEY])))
        await session.execute(delete(Provider).where(Provider.name.in_([LIVE, OFF])))
        await session.commit()


@pytest.fixture
async def providers():
    """一个启用、一个停用的演示类接入（不联网）；用完删掉。"""
    await _clean()
    async with SessionLocal() as session:
        session.add_all([
            Provider(name=LIVE, kind="mock", models=[{"id": "起草模型甲"}], default_model="起草模型甲", enabled=True,
                     extra={}),
            Provider(name=OFF, kind="mock", models=[{"id": "起草模型乙"}], default_model="起草模型乙", enabled=False,
                     extra={}),
        ])
        await session.commit()
    yield
    await _clean()


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


# --------------------------------------------------------------------------
# 设置组的读写与校验
# --------------------------------------------------------------------------


async def test_the_group_is_exposed_with_empty_defaults(client, providers):
    got = (await client.get("/api/settings")).json()
    assert got["catalog_draft"] == {"provider": None, "model": None}


async def test_the_group_round_trips(client, providers):
    r = await client.put("/api/settings", json={"values": {"catalog_draft": {"provider": LIVE, "model": "起草模型甲"}}})
    assert r.status_code == 200, r.text
    assert r.json()["catalog_draft"] == {"provider": LIVE, "model": "起草模型甲"}
    got = (await client.get("/api/settings")).json()["catalog_draft"]
    assert got == {"provider": LIVE, "model": "起草模型甲"}
    # 只写模型、接入留空：照存，接入是 null
    r = await client.put("/api/settings", json={"values": {"catalog_draft": {"provider": "  ", "model": "x"}}})
    assert r.status_code == 200, r.text
    assert r.json()["catalog_draft"] == {"provider": None, "model": "x"}


@pytest.mark.parametrize("value,label", [
    ({"provider": 3}, "起草模型 · 接入"),
    ({"model": ["a"]}, "起草模型 · 模型"),
    ({"temperature": 0}, "temperature"),
    ("起草模型甲", "起草模型"),
])
async def test_bad_values_are_refused_with_the_settings_page_wording(client, providers, value, label):
    r = await client.put("/api/settings", json={"values": {"catalog_draft": value, "ui": {"theme": "dark"}}})
    assert r.status_code == 422, r.text
    assert label in r.json()["detail"]
    got = (await client.get("/api/settings")).json()
    assert got["catalog_draft"] == {"provider": None, "model": None} and got["ui"]["theme"] != "dark"


@pytest.mark.parametrize("name,reason,fix", [
    (GONE, f"模型接入「{GONE}」不存在", "请在「设置 → 模型接入」中核对名称"),
    (OFF, f"模型接入「{OFF}」已停用", "请在「设置 → 模型接入」中启用它"),
])
async def test_a_missing_or_disabled_provider_is_refused_with_the_field(client, providers, name, reason, fix):
    r = await client.put("/api/settings", json={"values": {"catalog_draft": {"provider": name}}})
    assert r.status_code == 422, r.text
    body = r.json()
    assert body["code"] == "settings_provider_invalid" and body["field"] == "catalog_draft.provider"
    assert body["detail"] == f"「起草模型 · 接入」指定的{reason}。{fix}，或换一个接入"
    async with SessionLocal() as session:
        assert await session.get(Setting, "catalog_draft") is None


# --------------------------------------------------------------------------
# 取模型的顺序
# --------------------------------------------------------------------------


class _MemDB:
    """独立的内存库：取模型只看这里的接入和设置，不受别的测试建的接入影响。"""

    async def __aenter__(self):
        from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
        from sqlalchemy.pool import StaticPool

        import app.db.models  # noqa: F401
        from app.db.base import Base

        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.session = async_sessionmaker(self.engine, expire_on_commit=False, class_=AsyncSession)()
        return self.session

    async def __aexit__(self, *exc):
        await self.session.close()
        await self.engine.dispose()


@pytest.fixture
async def mem():
    async with _MemDB() as session:
        yield session


@pytest.fixture
def no_env_keys(monkeypatch):
    for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY", "ANTHROPIC_BASE_URL"):
        monkeypatch.delenv(k, raising=False)


async def _setup(session, *, draft: dict | None = None, copilot: dict | None = None) -> None:
    from app.core.crypto import encrypt

    for name, model in (("甲接入", "模型甲"), ("乙接入", "模型乙"), ("丙接入", "模型丙")):
        session.add(Provider(name=name, kind="anthropic", api_key=encrypt("sk-test"), models=[{"id": model}],
                             default_model=model, enabled=True, extra={}))
    if draft is not None:
        session.add(Setting(key=draft_model.CATALOG_DRAFT_SETTING_KEY, value=draft))
    if copilot is not None:
        session.add(Setting(key=draft_model.COPILOT_SETTING_KEY, value=copilot))
    await session.commit()


@pytest.mark.parametrize("draft,copilot,expected", [
    (None, None, (None, None, "default")),
    (None, {"provider": "乙接入", "model": ""}, ("乙接入", None, "assistant")),
    ({"provider": None, "model": None}, {"provider": "", "model": "模型乙"}, (None, "模型乙", "assistant")),
    ({"provider": "", "model": "模型丙"}, {"provider": "乙接入", "model": "模型乙"}, (None, "模型丙", "draft")),
    ({"provider": "甲接入", "model": None}, {"provider": "乙接入", "model": "模型乙"}, ("甲接入", None, "draft")),
    ({"provider": 7}, {"provider": "乙接入"}, ("乙接入", None, "assistant")),     # 存坏了的按没填
])
async def test_choice_order(mem, draft, copilot, expected):
    await _setup(mem, draft=draft, copilot=copilot)
    got = await draft_model.draft_model_choice(mem)
    assert (got.provider, got.model, got.origin) == expected


def _spy(monkeypatch) -> list[Any]:
    from app.providers import factory

    specs: list[Any] = []

    class Built:
        def __init__(self, provider, spec):
            self.provider, self.spec = provider.name, spec

    def fake_build(provider, spec):
        specs.append((provider.name, spec))
        return Built(provider, spec)

    monkeypatch.setattr(factory, "build_chat_model", fake_build)
    return specs


async def test_catalog_draft_uses_the_draft_setting_keeps_thinking_and_a_big_budget(mem, no_env_keys, monkeypatch):
    await _setup(mem, draft={"provider": "甲接入", "model": None}, copilot={"provider": "乙接入", "model": "模型乙"})
    specs = _spy(monkeypatch)
    model, model_id = await catalog.resolve_draft_model(mem)
    assert model_id == "模型甲"
    assert [p for p, _ in specs] == ["甲接入", "甲接入"]
    assert [s.max_tokens for _, s in specs] == [catalog.MODEL_MAX_TOKENS, catalog.MODEL_FALLBACK_MAX_TOKENS]
    assert catalog.MODEL_MAX_TOKENS == 32768 and catalog.MODEL_FALLBACK_MAX_TOKENS == 8192
    assert all(s.thinking is None and s.temperature == 0 for _, s in specs), "不关思考，按接入默认"
    assert all(s.timeout == catalog.MODEL_TIMEOUT_S == 420 for _, s in specs)
    assert model.budget == 32768 and model.model_name == "模型甲"


async def test_catalog_draft_falls_back_to_the_assistant_model(mem, no_env_keys, monkeypatch):
    await _setup(mem, copilot={"provider": "", "model": "模型乙"})
    specs = _spy(monkeypatch)
    _, model_id = await catalog.resolve_draft_model(mem)
    assert model_id == "模型乙" and specs[0][0] == "乙接入"


async def test_recipe_ai_draft_follows_the_same_order_but_keeps_its_own_spec(mem, no_env_keys, monkeypatch):
    await _setup(mem, draft={"provider": "", "model": "模型丙"}, copilot={"provider": "乙接入", "model": "模型乙"})
    specs = _spy(monkeypatch)
    model, model_id, avail = await A.resolve_draft_model(mem)
    assert avail.available and model_id == "模型丙" and avail.provider == "丙接入"
    assert [s.max_tokens for _, s in specs] == [A.AI_MAX_TOKENS, A.AI_FALLBACK_MAX_TOKENS]
    assert all(s.thinking == "off" and s.temperature == 0 for _, s in specs), "配方 AI 起草的思考设置不动"


async def test_a_broken_draft_setting_says_where_to_fix_it(mem, no_env_keys):
    """起草模型点名的接入不存在：报错说的是起草模型，不是助手的模型。沿用助手模型时照旧说助手。"""
    from app.core.crypto import encrypt

    mem.add(Provider(name="停用的接入", kind="anthropic", api_key=encrypt("k"), models=[{"id": "模型丁"}],
                     default_model="模型丁", enabled=False, extra={}))
    mem.add(Setting(key=draft_model.CATALOG_DRAFT_SETTING_KEY, value={"provider": "停用的接入"}))
    await mem.commit()
    with pytest.raises(catalog.CatalogModelUnavailable) as e:
        await catalog.resolve_draft_model(mem)
    assert "起草模型" in str(e.value) and "停用的接入" in str(e.value) and "已停用" in str(e.value)
    assert "助手" not in str(e.value)
    _, _, avail = await A.resolve_draft_model(mem)
    assert not avail.available and "起草模型" in avail.reason and "助手" not in avail.reason

    await mem.execute(delete(Setting))
    mem.add(Setting(key=draft_model.COPILOT_SETTING_KEY, value={"provider": "停用的接入"}))
    await mem.commit()
    with pytest.raises(catalog.CatalogModelUnavailable) as e:
        await catalog.resolve_draft_model(mem)
    assert "助手使用的" in str(e.value)


async def test_only_a_demo_provider_is_refused(mem, no_env_keys):
    mem.add(Provider(name="演示", kind="mock", models=[{"id": "mock-fast"}], default_model="mock-fast", enabled=True,
                     extra={}))
    await mem.commit()
    with pytest.raises(catalog.CatalogModelUnavailable) as e:
        await catalog.resolve_draft_model(mem)
    assert "演示" in str(e.value)
