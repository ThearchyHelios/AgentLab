"""找模型接入（providers/factory.resolve_provider、get_chat_model）：点名了就只用点名的那个。

以前显式点名的接入不存在时返回 None，get_chat_model 把它当成「一个接入都没配」，悄悄换成演示模型（mock-fast）：
节点上、设置里把接入名写错一个字，或者接入改了名、删了，报告就由演示模型写成假文本，运行照样成功。点名一个已停用的
接入又照常返回、照常调用，和「按模型名找到的接入已停用就报错」对不上。

现在：
- 点名的接入不存在 → ProviderNotConfigured（missing），说清是哪个名字、去「设置 → 模型接入」核对；
- 点名的接入已停用 → ProviderNotConfigured（disabled），说法和按模型名找到停用接入时一致；
- 只有没点名、一个启用的接入都没有时才回退到演示模型（没配 key 的机器上图也能执行完）；没点名、模型名认不出来时
  照旧退回第一个启用的真实接入（用户可能手填了目录里还没有的新模型）。

每个用例一个独立的内存库，接入名、模型名都是虚构的。
"""
from __future__ import annotations

from typing import Any

import pytest

from app.providers import factory
from app.providers.factory import ModelSpec, ProviderNotConfigured, get_chat_model, resolve_provider
from app.providers.mock_model import MockChatModel


class _MemDB:
    """独立的内存库：不碰测试会话共用的库，也不受别的测试建的接入影响（同 test_recipe_ai）。"""

    async def __aenter__(self):
        from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
        from sqlalchemy.pool import StaticPool

        import app.db.models  # noqa: F401 - 建表前要先登记全部模型
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
async def session():
    async with _MemDB() as s:
        yield s


@pytest.fixture(autouse=True)
def no_env_keys(monkeypatch):
    for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY", "ANTHROPIC_BASE_URL"):
        monkeypatch.delenv(k, raising=False)


async def add(session, name: str, *, kind: str = "openai_compatible", models: tuple[str, ...] = ("m-1",),
              enabled: bool = True, **extra: Any):
    from app.core.crypto import encrypt
    from app.db.models import Provider

    row = Provider(name=name, kind=kind, base_url="http://127.0.0.1:9/v1" if kind == "openai_compatible" else None,
                   api_key=encrypt("k-test"), models=[{"id": m} for m in models],
                   default_model=models[0] if models else None, enabled=enabled, extra={}, **extra)
    session.add(row)
    await session.commit()
    return row


# --------------------------------------------------------------------------
# 点名了就只用点名的那个
# --------------------------------------------------------------------------


async def test_a_named_provider_that_does_not_exist_is_an_error_not_the_mock(session):
    await add(session, "甲接入")
    with pytest.raises(ProviderNotConfigured) as e:
        await resolve_provider(session, "甲接人")              # 写错一个字
    assert e.value.code == "missing"
    assert e.value.reason == "模型接入「甲接人」不存在"
    assert str(e.value) == "模型接入「甲接人」不存在。请在「设置 → 模型接入」中核对名称，或换一个接入"
    with pytest.raises(ProviderNotConfigured) as e:
        await get_chat_model(session, ModelSpec(provider="甲接人", model="m-1"))
    assert e.value.code == "missing"


async def test_a_named_provider_is_missing_even_when_no_provider_exists_at_all(session):
    """一个接入都没有、却点了名：点名的那个仍然不存在，不能因为「什么都没配」就退回演示模型。"""
    with pytest.raises(ProviderNotConfigured) as e:
        await get_chat_model(session, ModelSpec(provider="已删除的接入"))
    assert e.value.code == "missing" and "已删除的接入" in str(e.value)


async def test_a_named_provider_that_is_disabled_is_an_error_worded_like_the_model_lookup(session):
    off = await add(session, "停用接入", models=("m-off",), enabled=False)
    await add(session, "备用接入", models=("m-on",))
    for ref in ("停用接入", off.id):
        with pytest.raises(ProviderNotConfigured) as e:
            await resolve_provider(session, ref, "m-off")
        assert e.value.code == "disabled"
        assert str(e.value) == "模型接入「停用接入」已停用。请在「设置 → 模型接入」中启用它，或换一个接入"
    # 和按模型名找到停用接入时同一种说法：「已停用」「启用它」
    with pytest.raises(ProviderNotConfigured) as by_model:
        await resolve_provider(session, None, "m-off")
    assert by_model.value.code == "disabled"
    for words in ("已停用", "请在「设置 → 模型接入」中启用它"):
        assert words in str(by_model.value) and words in str(e.value)


async def test_a_disabled_provider_named_like_a_model_is_not_swapped_for_the_one_serving_that_model(session):
    """开发数据里真有一条停用的接入，名字恰好就是一个模型 id：点名时撞上它要照实报停用，不能悄悄换成提供这个模型的那一家；
    没点名、只写了模型名时照常按模型名找。"""
    await add(session, "fake-v4.1-flash", models=("fake-v4.1-flash",), enabled=False)
    serving = await add(session, "云端接入", models=("fake-v4.1-flash", "fake-v4-pro"))
    with pytest.raises(ProviderNotConfigured) as e:
        await resolve_provider(session, "fake-v4.1-flash", "fake-v4.1-flash")
    assert e.value.code == "disabled" and "fake-v4.1-flash" in str(e.value)
    assert (await resolve_provider(session, None, "fake-v4.1-flash")).id == serving.id


async def test_a_named_provider_is_found_by_name_or_id_and_used_as_is(session):
    house = await add(session, "本地接入", kind="mock", models=("mock-fast",))
    await add(session, "云端接入", models=("m-cloud",))
    assert (await resolve_provider(session, "本地接入")).id == house.id
    assert (await resolve_provider(session, house.id)).id == house.id
    assert (await resolve_provider(session, " 本地接入 ")).id == house.id     # 两边的空白不算
    # 点名一个演示接入是明确的选择，照用
    model, model_id = await get_chat_model(session, ModelSpec(provider="本地接入"))
    assert isinstance(model, MockChatModel) and model_id == "mock-fast"


async def test_a_blank_name_counts_as_not_named(session):
    cloud = await add(session, "云端接入")
    for ref in ("", "  ", None):
        assert (await resolve_provider(session, ref)).id == cloud.id


# --------------------------------------------------------------------------
# 没点名：保留原来的回退
# --------------------------------------------------------------------------


async def test_without_any_enabled_provider_and_no_name_the_mock_still_runs(session):
    """没配 key 的机器上图也能执行完：没点名、一个启用的接入都没有，才回退到演示模型。停用的接入不算。"""
    model, model_id = await get_chat_model(session, ModelSpec())
    assert isinstance(model, MockChatModel) and model_id == "mock-fast"
    await add(session, "停用接入", models=("m-off",), enabled=False)
    model, model_id = await get_chat_model(session, ModelSpec(model="认不出的模型"))
    assert isinstance(model, MockChatModel) and model_id == "mock-fast"


async def test_without_a_name_an_unknown_model_goes_to_the_first_real_provider(session):
    """手填了目录里还没有的新模型：照旧退回第一个启用的真实接入（演示接入排在后面）。"""
    await add(session, "演示", kind="mock", models=("mock-fast",))
    real = await add(session, "云端接入", models=("m-cloud",))
    assert (await resolve_provider(session, None, "刚发布的新模型")).id == real.id
    assert (await resolve_provider(session, None, None)).id == real.id


async def test_without_a_name_the_model_picks_its_own_provider(session):
    await add(session, "甲接入", models=("m-a",))
    b = await add(session, "乙接入", models=("m-b",))
    assert (await resolve_provider(session, None, "m-b")).id == b.id


async def test_get_chat_model_builds_the_named_provider(session, monkeypatch):
    """点名的接入找得到、启用着：照常建模型，用的就是它。"""
    target = await add(session, "乙接入", models=("m-b",))
    await add(session, "甲接入", models=("m-a",))
    built: list[Any] = []

    def spy(provider, spec):
        built.append((provider.id, spec.model))
        return MockChatModel(model_name=spec.model or "")

    monkeypatch.setattr(factory, "build_chat_model", spy)
    _, model_id = await get_chat_model(session, ModelSpec(provider="乙接入", model="m-b"))
    assert built == [(target.id, "m-b")] and model_id == "m-b"
