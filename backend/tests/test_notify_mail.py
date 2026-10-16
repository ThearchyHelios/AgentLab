"""邮件：通知渠道的第五种格式（notify/mail.py，docs/design/next-round/04-对外集成与触达.md B2）。

全程用假的 SMTP 工厂（mail._smtp_factory），不连任何邮件服务器。守的是：
- 个人邮件渠道只发到拥有者在名录里的账号邮箱；账号没有邮箱时记一条失败的投递、写明原因；个人渠道不收地址；
- 全局、应用的邮件渠道填固定的收件地址（最多 20 个，格式不对的拒收）；邮件服务没启用时不收新的邮件渠道；
- 4xx（临时错误）和连不上按 1/2/4 秒重试，5xx 不重试；报错里绝不出现口令；
- 邮件里只有标题、状态、链接，纯文本 + 转义过的 HTML，带 List-Unsubscribe；投递记录成功写「已交给邮件服务器」（不说已送达）；
- 设置的 mail 一组：口令加密存、读的时候只说配没配；启用时要配全；发件人名称不许换行；只有管理员能改；
- 发测试邮件只发到管理员自己的账号邮箱：关登录、账号没有邮箱时 422 说清怎么办；
- 管理员开了「默认发邮件给待办处理人」：第一次派到待办的人自动有一个个人邮件渠道（默认订阅三项），本人删了不再建回来；
  开关缺省关时不建。

人名、地址、主机都是虚构的。
"""
from __future__ import annotations

import json
import smtplib
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select

from app.core.crypto import encrypt
from app.db.base import SessionLocal
from app.db.models import NotifyChannel, NotifyDelivery, Setting, User
from app.notify import mail, service
from app.notify.core import Message

PASSWORD = "Sm7p-Pa55word-Zq"


class FakeSMTP:
    """假的 SMTP 连接：记下登录、发出的信；box.failures 里排着的异常按顺序在 send_message 上抛。"""

    def __init__(self, box: SimpleNamespace) -> None:
        self.box = box

    def starttls(self, context=None) -> None:
        self.box.tls += 1

    def login(self, username: str, password: str) -> None:
        self.box.logins.append((username, password))
        if self.box.auth_error:
            # 有的服务器会把收到的内容回显在错误里：报错里也不能出现口令
            raise smtplib.SMTPAuthenticationError(535, f"5.7.8 bad credentials {password}".encode())

    def send_message(self, em, from_addr=None, to_addrs=None):
        if self.box.failures:
            raise self.box.failures.pop(0)
        self.box.sent.append(SimpleNamespace(from_addr=from_addr, to=list(to_addrs or []), em=em))
        return {}

    def quit(self) -> None:
        self.box.quits += 1

    def close(self) -> None:
        pass


@pytest.fixture
def smtp(monkeypatch):
    box = SimpleNamespace(sent=[], logins=[], failures=[], connects=[], refuse=0, tls=0, quits=0, auth_error=False)

    def factory(host, port, security, timeout):
        box.connects.append((host, port, security, timeout))
        if box.refuse:
            box.refuse -= 1
            raise ConnectionRefusedError("refused")
        return FakeSMTP(box)

    monkeypatch.setattr(mail, "_smtp_factory", factory)
    monkeypatch.setattr(mail, "RETRY_DELAYS", (0, 0, 0))
    return box


@pytest.fixture(autouse=True)
async def clean():
    """前后各清一遍：同一个进程里别的测试留下的渠道、投递记录不算进来。"""
    await _clean()
    yield
    await service.drain()
    await _clean()


async def _clean() -> None:
    async with SessionLocal() as session:
        await session.execute(delete(NotifyDelivery))
        await session.execute(delete(NotifyChannel))
        await session.execute(delete(Setting).where(Setting.key.in_([mail.GROUP, "notify", service.MAIL_AUTO_KEY])))
        await session.execute(delete(User).where(User.username.like("mail-%")))
        await session.commit()
    from app.core import public_url

    public_url.remember(None)   # 发信时按库里的对外访问地址对齐了缓存：删了那一行，缓存也清掉


async def configure(**over) -> None:
    value = mail.stored_value({"enabled": True, "host": "smtp.example.com", "port": 587, "security": "starttls",
                               "username": "agentlab", "password": PASSWORD, "from_addr": "agentlab@example.com",
                               "from_name": "AgentLab", **over})
    async with SessionLocal() as session:
        row = await session.get(Setting, mail.GROUP)
        if row is None:
            session.add(Setting(key=mail.GROUP, value=value))
        else:
            row.value = value
        notify = await session.get(Setting, "notify")
        if notify is None:
            session.add(Setting(key="notify", value={"public_base_url": "https://agentlab.example.com"}))
        else:
            # 别的测试留下的 notify 一组：对外访问地址换成这里要的（退订头按它拼）
            notify.value = {**(notify.value or {}), "public_base_url": "https://agentlab.example.com"}
        await session.commit()


async def person(name: str, email: str | None) -> User:
    async with SessionLocal() as session:
        user = User(username=f"mail-{name}", display_name=name, role="user", email=email)
        session.add(user)
        await session.commit()
        await session.refresh(user)
        return user


async def channel(scope: str, owner_id: str | None = None, to: str = mail.PERSONAL_PLACEHOLDER,
                  events: list[str] | None = None) -> NotifyChannel:
    async with SessionLocal() as session:
        row = NotifyChannel(name=f"邮件-{scope}", scope=scope, owner_id=owner_id, format="email", url=encrypt(to),
                            events=events or ["task.opened"])
        session.add(row)
        await session.commit()
        await session.refresh(row)
        return row


MESSAGE = {"event": "task.opened", "status": "pending", "title": "新的待办：审批 <b>付款</b>",
           "lines": [("状态", "待处理"), ("所在", "应用「报销 & 付款」")],
           "links": [("去处理", "https://agentlab.example.com/apps/inbox/t1")]}


async def _deliveries() -> list[NotifyDelivery]:
    async with SessionLocal() as session:
        return list((await session.execute(select(NotifyDelivery))).scalars())


# --------------------------------------------------------------------------
# 发到谁
# --------------------------------------------------------------------------


async def test_personal_mail_goes_to_the_account_email_only(smtp):
    await configure()
    ana = await person("ana", "ana@example.com")
    row = await channel("user", ana.id)
    sent = await service._push([row], MESSAGE, subject_kind="task", subject_id="t1", event="task.opened",
                               run_id=None, task_id="t1")
    assert sent == 1
    [mail_out] = smtp.sent
    assert mail_out.to == ["ana@example.com"] and mail_out.from_addr == "agentlab@example.com"
    assert smtp.logins == [("agentlab", PASSWORD)] and smtp.tls == 1
    em = mail_out.em
    assert em["Subject"] == "【AgentLab】新的待办：审批 <b>付款</b>"
    assert em["List-Unsubscribe"] == "<https://agentlab.example.com/settings/notify>"
    assert em["Auto-Submitted"] == "auto-generated"
    text = em.get_body(("plain",)).get_content()
    html = em.get_body(("html",)).get_content()
    assert "状态：待处理" in text and "https://agentlab.example.com/apps/inbox/t1" in text
    assert "&lt;b&gt;付款&lt;/b&gt;" in html and "<b>付款</b>" not in html, "HTML 里的内容一律转义"
    assert "报销 &amp; 付款" in html
    [delivery] = await _deliveries()
    assert (delivery.status, delivery.error, delivery.http_status) == ("ok", None, None)


async def test_personal_mail_without_account_email_is_a_failed_delivery_with_the_reason(smtp):
    await configure()
    bo = await person("bo", None)
    row = await channel("user", bo.id)
    await service._push([row], MESSAGE, subject_kind="task", subject_id="t1", event="task.opened", run_id=None,
                        task_id="t1")
    assert smtp.sent == [] and smtp.connects == [], "没有邮箱就不连服务器"
    [delivery] = await _deliveries()
    assert delivery.status == "failed" and delivery.error == mail.NO_EMAIL
    assert "设置 → 身份" in delivery.error


async def test_global_mail_goes_to_the_fixed_addresses(smtp):
    await configure()
    row = await channel("global", to="ops@example.com, lead@example.com")
    await service._push([row], MESSAGE, subject_kind="task", subject_id="t1", event="task.opened", run_id=None,
                        task_id="t1")
    assert smtp.sent[0].to == ["ops@example.com", "lead@example.com"]


async def test_disabled_mail_service_records_why(smtp):
    await configure(enabled=False)
    row = await channel("global", to="ops@example.com")
    await service._push([row], MESSAGE, subject_kind="task", subject_id="t1", event="task.opened", run_id=None,
                        task_id="t1")
    [delivery] = await _deliveries()
    assert delivery.status == "failed" and delivery.error == mail.NOT_ENABLED and smtp.connects == []


# --------------------------------------------------------------------------
# 重试与报错
# --------------------------------------------------------------------------


def _cfg() -> mail.MailConfig:
    return mail.config_of(mail.stored_value({"enabled": True, "host": "smtp.example.com", "port": 587,
                                             "security": "starttls", "username": "agentlab", "password": PASSWORD,
                                             "from_addr": "agentlab@example.com"}))


async def test_temporary_errors_and_refused_connections_are_retried(smtp):
    smtp.failures = [smtplib.SMTPDataError(451, b"4.3.0 try again later")]
    smtp.refuse = 1
    out = await mail.send(_cfg(), ["ana@example.com"], Message(title="t"), label="测试")
    assert out.ok is True and out.attempts == 3, "连不上一次、451 一次，第三次交给了邮件服务器"
    assert len(smtp.sent) == 1


async def test_permanent_errors_are_not_retried(smtp):
    smtp.failures = [smtplib.SMTPDataError(550, b"5.7.1 message rejected")]
    out = await mail.send(_cfg(), ["ana@example.com"], Message(title="t"), label="测试")
    assert out.ok is False and out.attempts == 1 and out.retryable is False
    assert "550" in out.error and "smtp.example.com" in out.error


async def test_retries_stop_after_three(smtp):
    smtp.refuse = 10
    out = await mail.send(_cfg(), ["ana@example.com"], Message(title="t"), label="测试")
    assert out.ok is False and out.attempts == 4 and len(smtp.connects) == 4
    assert out.error == "无法连接邮件服务器 smtp.example.com:587"


async def test_errors_never_contain_the_password(smtp, caplog):
    smtp.auth_error = True
    out = await mail.send(_cfg(), ["ana@example.com"], Message(title="t"), label="测试")
    assert out.ok is False and out.attempts == 1, "认证失败（5xx）不重试"
    assert PASSWORD not in (out.error or "") and "535" in out.error
    assert PASSWORD not in caplog.text
    smtp.auth_error = False
    smtp.failures = [smtplib.SMTPSenderRefused(553, f"sender {PASSWORD} refused".encode(), "agentlab@example.com")]
    out = await mail.send(_cfg(), ["ana@example.com"], Message(title="t"), label="测试")
    assert PASSWORD not in (out.error or "")


async def test_partially_refused_recipients_are_said_plainly(smtp, monkeypatch):
    monkeypatch.setattr(FakeSMTP, "send_message", lambda self, em, from_addr=None, to_addrs=None: {"x@example.com": (550, b"no")})
    out = await mail.send(_cfg(), ["ana@example.com", "x@example.com"], Message(title="t"), label="测试")
    assert out.ok is True and out.error == "已交给邮件服务器，其中 1 个收件地址被拒收"


def test_line_breaks_in_titles_never_reach_the_headers():
    em = mail.build(_cfg(), ["ana@example.com"], Message(title="新的待办：审批\r\nBcc: x@example.com"), unsubscribe=None)
    assert em["Subject"] == "【AgentLab】新的待办：审批 Bcc: x@example.com" and em["Bcc"] is None


def test_ssl_security_uses_no_starttls(smtp):
    cfg = mail.config_of(mail.stored_value({"enabled": True, "host": "smtp.example.com", "port": 465,
                                            "security": "ssl", "from_addr": "agentlab@example.com"}))
    out = mail._send_once(cfg, ["ana@example.com"], mail.build(cfg, ["ana@example.com"], Message(title="t"),
                                                               unsubscribe=None))
    assert out.ok and smtp.tls == 0 and smtp.logins == [], "没填用户名不登录"
    assert smtp.connects[0][:3] == ("smtp.example.com", 465, "ssl") and smtp.connects[0][3] == mail.TIMEOUT_S


# --------------------------------------------------------------------------
# 渠道接口
# --------------------------------------------------------------------------


@pytest.fixture
async def client():
    from app.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def test_mail_channels_need_the_mail_service_and_valid_addresses(client, smtp):
    body = {"name": "运维邮件", "scope": "global", "format": "email", "url": "ops@example.com", "events": ["task.opened"]}
    refused = await client.post("/api/notify/channels", json=body)
    assert refused.status_code == 422 and refused.json()["detail"] == mail.NOT_ENABLED
    await configure()
    too_many = ", ".join(f"p{i}@example.com" for i in range(21))
    r = await client.post("/api/notify/channels", json={**body, "url": too_many})
    assert r.status_code == 422 and "最多 20 个" in r.json()["detail"]
    r = await client.post("/api/notify/channels", json={**body, "url": "ops@example.com\nnot-an-address"})
    assert r.status_code == 422 and "not-an-address" in r.json()["detail"]
    made = await client.post("/api/notify/channels", json={**body, "url": "ops@example.com；lead@example.com"})
    assert made.status_code == 201, made.text
    row = made.json()
    assert row["format"] == "email" and row["mail_to"] == ["ops@example.com", "lead@example.com"]
    assert row["url_host"] == "" and row["signed"] is False
    listed = (await client.get("/api/notify/channels")).json()
    assert listed["mail"] == {"enabled": True, "account_email": None}, "关登录时没有账号邮箱"
    tested = (await client.post(f"/api/notify/channels/{row['id']}/test")).json()
    assert tested["ok"] is True and tested["message"].startswith("已交给邮件服务器")
    assert "是否送达以邮件服务器为准" in tested["message"]
    deliveries = (await client.get(f"/api/notify/deliveries?channel_id={row['id']}")).json()["items"]
    assert [(d["event"], d["status"], d["format"]) for d in deliveries] == [("test", "ok", "email")]
    # 只改订阅、不传地址：照旧
    kept = await client.patch(f"/api/notify/channels/{row['id']}", json={"events": ["task.opened", "action.unknown"]})
    assert kept.status_code == 200 and kept.json()["mail_to"] == ["ops@example.com", "lead@example.com"]
    # 换成 Webhook 要填 Webhook 地址
    back = await client.patch(f"/api/notify/channels/{row['id']}", json={"format": "generic"})
    assert back.status_code == 422 and "Webhook 地址" in back.json()["detail"]


async def test_personal_mail_channel_takes_no_address(smtp):
    from tests.fakes import auth_env

    await configure()
    await auth_env.reset()
    try:
        await auth_env.turn_on_local()
        await auth_env.make_user("mail-uma", "user", email="uma@example.com")
        uma = await auth_env.login("mail-uma")
        r = await uma.post("/api/notify/channels", json={"name": "我的邮件", "scope": "user", "format": "email",
                                                         "url": "someone@example.com", "events": ["task.opened"]})
        assert r.status_code == 422 and "账号邮箱" in r.json()["detail"], "个人渠道不能填任意地址（不当中继）"
        r = await uma.post("/api/notify/channels", json={"name": "我的邮件", "scope": "user", "format": "email",
                                                         "events": ["task.opened", "task.digest"]})
        assert r.status_code == 201, r.text
        assert r.json()["mail_to"] == ["uma@example.com"]
        listed = (await uma.get("/api/notify/channels")).json()
        assert listed["mail"] == {"enabled": True, "account_email": "uma@example.com"}
        tested = (await uma.post(f"/api/notify/channels/{r.json()['id']}/test")).json()
        assert tested["ok"] is True and smtp.sent[-1].to == ["uma@example.com"]
    finally:
        await auth_env.reset()


# --------------------------------------------------------------------------
# 设置
# --------------------------------------------------------------------------


async def test_mail_settings_keep_the_password_encrypted_and_hidden(client):
    put = await client.put("/api/settings", json={"values": {"mail": {
        "enabled": True, "host": "smtp.example.com", "port": 465, "security": "ssl", "username": "agentlab",
        "password": PASSWORD, "from_addr": "agentlab@example.com", "from_name": "流程助手"}}})
    assert put.status_code == 200, put.text
    shown = put.json()["mail"]
    assert shown["password"] is None and shown["password_set"] is True and PASSWORD not in json.dumps(put.json())
    async with SessionLocal() as session:
        stored = (await session.get(Setting, "mail")).value
    assert stored["password"] != PASSWORD and PASSWORD not in json.dumps(stored)
    assert mail.config_of(stored).password == PASSWORD
    # 整组回写（口令是 null、带着 password_set）：口令照旧
    again = await client.put("/api/settings", json={"values": {"mail": {**shown, "from_name": "AgentLab"}}})
    assert again.status_code == 200 and again.json()["mail"]["password_set"] is True
    async with SessionLocal() as session:
        assert mail.config_of((await session.get(Setting, "mail")).value).password == PASSWORD
    # 空串表示去掉
    cleared = await client.put("/api/settings", json={"values": {"mail": {"password": ""}}})
    assert cleared.json()["mail"]["password_set"] is False
    assert cleared.json()["mail"]["host"] == "smtp.example.com", "只给一项的不冲掉别的"


@pytest.mark.parametrize("value, fragment", [
    ({"enabled": True}, "SMTP 服务器"),
    ({"enabled": True, "host": "smtp.example.com"}, "发件地址"),
    ({"host": "smtp://smtp.example.com"}, "主机名"),
    ({"port": 70000}, "端口"),
    ({"security": "tls"}, "加密方式"),
    ({"from_addr": "not-an-address"}, "发件地址"),
    ({"from_name": "AgentLab\r\nBcc: x@example.com"}, "换行"),
    ({"relay": True}, "不支持「relay」"),
])
async def test_mail_settings_are_validated(client, value, fragment):
    put = await client.put("/api/settings", json={"values": {"mail": value}})
    assert put.status_code == 422, put.text
    assert fragment in put.json()["detail"]


async def test_mail_settings_are_admin_only_and_test_mail_goes_to_the_admin(smtp):
    from tests.fakes import auth_env

    await configure()
    await auth_env.reset()
    try:
        async with auth_env.client() as off:
            r = await off.post("/api/settings/mail/test")
            assert r.status_code == 422 and r.json()["code"] == "mail_test_no_address", "关登录时没有账号邮箱"
        root = await auth_env.turn_on_local()
        r = await root.post("/api/settings/mail/test")
        assert r.status_code == 422 and "没有邮箱地址" in r.json()["detail"]
        async with SessionLocal() as session:
            admin = (await session.execute(select(User).where(User.username == "root"))).scalar_one()
            admin.email = "root@example.com"
            await session.commit()
        r = await root.post("/api/settings/mail/test")
        assert r.status_code == 200 and r.json()["ok"] is True, r.text
        assert "已交给邮件服务器" in r.json()["message"] and smtp.sent[-1].to == ["root@example.com"]
        await auth_env.make_user("mail-bea", "builder")
        bea = await auth_env.login("mail-bea")
        assert (await bea.get("/api/settings")).json()["mail"]["password"] is None, "搭建者读得到，看不到口令"
        assert (await bea.put("/api/settings", json={"values": {"mail": {"enabled": False}}})).status_code == 403
        assert (await bea.post("/api/settings/mail/test")).status_code == 403
    finally:
        await auth_env.reset()


# --------------------------------------------------------------------------
# 默认发邮件给待办处理人
# --------------------------------------------------------------------------


async def test_assignees_get_a_personal_mail_channel_only_when_the_admin_turns_it_on(smtp):
    from tests.fakes import auth_env

    await auth_env.reset()
    try:
        await auth_env.turn_on_local()
        cy = await person("cy", "cy@example.com")
        await configure()
        assert await service.ensure_assignee_mail([cy.username]) == 0, "缺省关：不建"
        await configure(default_for_assignees=True)
        assert await service.ensure_assignee_mail([cy.username]) == 1
        async with SessionLocal() as session:
            [row] = (await session.execute(select(NotifyChannel).where(NotifyChannel.owner_id == cy.id))).scalars()
            assert (row.scope, row.format, row.enabled) == ("user", "email", True)
            assert row.events == ["task.opened", "action.unknown", "review.done"]
            await session.delete(row)
            await session.commit()
        assert await service.ensure_assignee_mail([cy.username]) == 0, "本人删了就是不要：不再建回来"
    finally:
        await auth_env.reset()


async def test_assignee_mail_is_not_created_when_login_is_off(smtp):
    dee = await person("dee", "dee@example.com")
    await configure(default_for_assignees=True)
    assert await service.ensure_assignee_mail([dee.username]) == 0, "关登录没有认证过的账号，个人渠道无从挂起"
