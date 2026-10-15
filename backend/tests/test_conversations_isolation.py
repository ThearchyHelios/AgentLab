"""对话按人隔离（D-d）与应用编辑器的助手对话（D-e 的归属核对）。

- 新建对话记下建立人（actor_of：关登录时是自报的署名，开登录时是认证过的用户名）；
- 开登录后使用者只列得出、读得到、改得动、删得掉自己建的对话；建立人为空的老对话只有搭建者以上看得到；
- 应用编辑器的助手对话（kind=app）只对能管理这个应用的人开放；
- ensure_conversation_access：接受 conversation_id 的接口（助手的生成流等）按同一套规则核对，不存在的当没有历史；
- 关登录时一切照旧。
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException
from sqlalchemy import delete, select

from app.api.conversations import ensure_conversation_access
from app.api.deps import Principal, reset_principal, set_principal
from app.db.base import SessionLocal
from app.db.models import App, AppGrant, Conversation, ConversationTurn
from tests.fakes import auth_env


#: 这些测试里出现的人：收尾时只删他们建的对话、本文件建的应用，不碰同一进程里别的测试的数据
PEOPLE = ("uma", "ula", "bob", "bea", "张三")


async def _clear(*app_ids: str, conversation_ids: tuple[str, ...] = ()) -> None:
    async with SessionLocal() as s:
        mine = Conversation.created_by.in_(PEOPLE) | Conversation.id.in_(list(conversation_ids))
        ids = list((await s.execute(select(Conversation.id).where(mine))).scalars())
        await s.execute(delete(ConversationTurn).where(ConversationTurn.conversation_id.in_(ids)))
        await s.execute(delete(Conversation).where(Conversation.id.in_(ids)))
        await s.execute(delete(AppGrant).where(AppGrant.app_id.in_(list(app_ids))))
        await s.execute(delete(App).where(App.id.in_(list(app_ids))))
        await s.commit()


@pytest.fixture
async def world():
    """开账号密码登录、开问数据；使用者 uma、ula，搭建者 bob、bea；一段关登录时留下的老对话，一个 bob 建的应用。"""
    await auth_env.reset()
    await _clear()
    async with SessionLocal() as s:
        legacy = Conversation(kind="chat", title="老对话")
        app_row = App(name="报销", created_by="bob")
        s.add_all([legacy, app_row])
        await s.flush()
        app_conv = Conversation(kind="app", app_id=app_row.id, title="应用润色", created_by="bob")
        s.add(app_conv)
        await s.commit()
        ids = {"legacy": legacy.id, "app": app_row.id, "app_conv": app_conv.id}
    admin = await auth_env.turn_on_local()
    assert (await admin.put("/api/auth/settings", json={"user_can_chat": True})).status_code == 200
    for name, role in (("uma", "user"), ("ula", "user"), ("bob", "builder"), ("bea", "builder")):
        await auth_env.make_user(name, role)
    clients = {name: await auth_env.login(name) for name in ("uma", "ula", "bob", "bea")}
    try:
        yield {"admin": admin, **clients, "ids": ids}
    finally:
        await _clear(ids["app"], conversation_ids=(ids["legacy"],))
        await auth_env.reset()


async def _create(client, title: str) -> str:
    r = await client.post("/api/conversations", json={"kind": "chat", "title": title})
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def test_a_user_only_sees_their_own_conversations(world):
    uma, ula, bob = world["uma"], world["ula"], world["bob"]
    mine = await _create(uma, "我的")
    theirs = await _create(ula, "别人的")
    legacy = world["ids"]["legacy"]

    got = (await uma.get("/api/conversations")).json()
    assert [c["id"] for c in got] == [mine] and got[0]["created_by"] == "uma", "只列自己建的，老对话不列"
    assert (await uma.get(f"/api/conversations/{mine}")).status_code == 200
    for other in (theirs, legacy):
        assert (await uma.get(f"/api/conversations/{other}")).status_code == 403
        assert (await uma.patch(f"/api/conversations/{other}", json={"title": "改"})).status_code == 403
        assert (await uma.post(f"/api/conversations/{other}/turns", json={"question": "接着问"})).status_code == 403
        assert (await uma.delete(f"/api/conversations/{other}")).status_code == 403
    turn = (await ula.post(f"/api/conversations/{theirs}/turns", json={"question": "我的问题"})).json()
    assert (await uma.patch(f"/api/conversations/{theirs}/turns/{turn['id']}", json={"answer": "x"})).status_code == 403

    assert (await uma.patch(f"/api/conversations/{mine}", json={"title": "改名"})).status_code == 200
    assert (await uma.post(f"/api/conversations/{mine}/turns", json={"question": "问"})).status_code == 201
    assert (await uma.delete(f"/api/conversations/{mine}")).status_code == 204

    # 搭建者以上照旧：看得到使用者的和老对话
    seen = {c["id"] for c in (await bob.get("/api/conversations")).json()}
    assert {theirs, legacy} <= seen
    assert (await bob.get(f"/api/conversations/{legacy}")).status_code == 200


async def test_app_assistant_conversations_need_manage(world):
    ids = world["ids"]
    path = f"/api/conversations/{ids['app_conv']}"
    assert (await world["bob"].get(path)).status_code == 200, "应用的建立人"
    assert (await world["admin"].get(path)).status_code == 200
    assert (await world["bea"].get(path)).status_code == 403, "没有 manage 的搭建者"
    assert ids["app_conv"] not in {c["id"] for c in (await world["bea"].get(
        "/api/conversations", params={"kind": "app"})).json()}
    assert [c["id"] for c in (await world["bob"].get(
        "/api/conversations", params={"kind": "app", "app_id": ids["app"]})).json()] == [ids["app_conv"]]
    async with SessionLocal() as s:
        s.add(AppGrant(app_id=ids["app"], principal_kind="user", principal="bea", role="manage"))
        await s.commit()
    assert (await world["bea"].get(path)).status_code == 200, "有 manage 授权的搭建者"
    assert (await world["uma"].get(path)).status_code == 403


async def test_ensure_conversation_access_for_other_endpoints(world):
    ids = world["ids"]
    mine = await _create(world["uma"], "我的")
    uma = Principal(username="uma", role="user", verified=True, mode="local")
    token = set_principal(uma)
    try:
        async with SessionLocal() as s:
            assert await ensure_conversation_access(s, None) is None
            assert await ensure_conversation_access(s, "nope") is None, "不存在的当没有历史，不报错"
            assert (await ensure_conversation_access(s, mine)).id == mine
            for other in (ids["legacy"], ids["app_conv"]):
                with pytest.raises(HTTPException) as e:
                    await ensure_conversation_access(s, other)
                assert e.value.status_code == 403
    finally:
        reset_principal(token)
    # api/deps.py 里那一份是同一个函数（给 api/copilot.py 这类接口调）
    from app.api import deps

    token = set_principal(Principal(username="bob", role="builder", verified=True, mode="local"))
    try:
        async with SessionLocal() as s:
            assert (await deps.ensure_conversation_access(s, ids["legacy"])).id == ids["legacy"]
    finally:
        reset_principal(token)


async def test_login_off_records_the_self_reported_name_and_changes_nothing_else():
    await auth_env.reset()
    async with SessionLocal() as s:
        legacy = Conversation(kind="chat", title="老对话")
        s.add(legacy)
        await s.commit()
        legacy_id = legacy.id
    try:
        async with auth_env.client(**{"X-Actor": "%E5%BC%A0%E4%B8%89"}) as c:
            created = (await c.post("/api/conversations", json={"kind": "chat"})).json()
            assert created["created_by"] == "张三"
            seen = {x["id"] for x in (await c.get("/api/conversations")).json()}
            assert {legacy_id, created["id"]} <= seen, "关登录时都看得到"
            assert (await c.get(f"/api/conversations/{legacy_id}")).status_code == 200
        async with SessionLocal() as s:
            assert await ensure_conversation_access(s, created["id"]) is not None
    finally:
        await _clear(conversation_ids=(legacy_id,))
