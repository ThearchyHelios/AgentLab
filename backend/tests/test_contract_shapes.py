"""普通用户建应用的后端约定（docs/design/normal-user-apps.md 第五部分）：前端类型（typesApp.ts、typesBlueprint.ts）对着的
那些常量和回包形状，在这里钉住，改了就要两边一起改。

- 待办种类有 review（个人应用提交审核），事件类型有 file.extracted，表单错误 code 有 file；
- 记录列表（RecordsPage）每个键都在：next_cursor 没有下一页为 null，state 是 ok / empty / preview；
- 审核待办的 payload 不论蓝图在不在，键都一样；
- 关节状态带模板声明的说法、类型、分组。
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

import app.db.models  # noqa: F401 - 建表前要先登记全部模型（conftest 按 Base.metadata 建）
import app.db.models_apps  # noqa: F401
import app.db.models_blueprints  # noqa: F401
import app.db.models_process  # noqa: F401
from app.core.config import settings
from app.db.base import SessionLocal

RECORDS_PAGE_KEYS = {"table", "version", "columns", "items", "mine_only", "next_cursor", "state"}
REVIEW_PAYLOAD_KEYS = {"kind", "blueprint_id", "app_id", "name", "purpose", "template_id", "submitted_by", "note",
                       "copies"}


@pytest.fixture(autouse=True)
def data_dir(monkeypatch, tmp_path):
    data = tmp_path / "data"
    (data / "uploads").mkdir(parents=True)
    monkeypatch.setattr(settings, "data_dir", data)
    yield data


def test_constants_the_frontend_types_mirror():
    from app.apps.spec import FORM_ERROR_CODES
    from app.core.events import EventType
    from app.db.models_process import TASK_KINDS
    from app.files.store import FILE_ERROR_CODE
    from app.process.tasks import REVIEW_KIND

    assert REVIEW_KIND in TASK_KINDS
    assert EventType.FILE_EXTRACTED == "file.extracted"
    assert FILE_ERROR_CODE == "file" and FILE_ERROR_CODE in FORM_ERROR_CODES


async def test_records_page_always_has_every_key():
    from app.api.deps import Principal
    from app.apps.resolve import _records_list
    from app.db.models_apps import App
    from app.records.listing import page
    from app.records.tables import ensure_table

    async with SessionLocal() as session:
        app = App(name=f"报损-{uuid.uuid4().hex[:4]}", created_by="建立人")
        session.add(app)
        await session.commit()
    viewer = Principal(username="amy", role="admin", verified=True, mode="local")
    async with SessionLocal() as session:
        empty = await page(session, app, principal=viewer, role="manage")
    assert set(empty) == RECORDS_PAGE_KEYS and empty["state"] == "empty" and empty["next_cursor"] is None
    async with SessionLocal() as session:
        await ensure_table(session, app, [{"name": "门店", "type": "string"}])
        await session.commit()
    async with SessionLocal() as session:
        ok = await page(session, app, principal=viewer, role="manage")
    assert set(ok) == RECORDS_PAGE_KEYS and ok["state"] == "ok" and ok["items"] == [] and ok["next_cursor"] is None
    preview = await _records_list(None, SimpleNamespace(draft=True), {"who": "mine"})
    assert set(preview) == RECORDS_PAGE_KEYS and preview["state"] == "preview", "草稿预览不说成「还没有记录表」"


async def test_review_payload_keeps_its_keys_when_the_blueprint_is_gone():
    from app.blueprints.personal import review_payload

    async with SessionLocal() as session:
        got = await review_payload(session, SimpleNamespace(correlation_key="blueprint:gone", app_id="a1"))
    assert set(got) == REVIEW_PAYLOAD_KEYS
    assert got["kind"] == "review" and got["blueprint_id"] == "gone" and got["copies"] == [] and got["name"] is None


def test_joint_states_carry_the_templates_label_kind_and_group():
    from app.blueprints.instantiate import instantiate
    from app.blueprints.store import blueprint_out
    from app.blueprints.templates import get_template

    inst = instantiate(get_template("periodic_board").example())
    defs = {j.id: j for j in inst.joint_defs}
    assert defs and all(state["kind"] == defs[jid].kind and state["group"] == defs[jid].group
                        and state["label"] == (defs[jid].label or jid) for jid, state in inst.joints.items())
    bp = SimpleNamespace(id="b1", name="周报", owner=None, scope="shared", status="drafting", spec={},
                         template_id="periodic_board", template_version=1, source={}, conversation_id=None,
                         workflow_id=None, app_id=None, last_trial_run_id=None, graph_hash=None, joints=inst.joints,
                         copied_from=None, created_at=None, updated_at=None)
    out = blueprint_out(bp, issues=False)["joints"]
    assert all(set(state) <= {"status", "by", "note", "label", "kind", "group"} for state in out.values())
    assert all("value" not in state and "basis" not in state for state in out.values()), "填的值不给前端"
