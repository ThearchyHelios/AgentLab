"""文件（docs/design/normal-user-apps.md N9、§3.8）。

- 上传：按内容寻址存、只读；类型按扩展名的白名单、再核对文件开头；要带上下文（入口、待办、蓝图），字段限定了类型的照办；
- 绑定：提交入口时核对是自己在这个入口传的、还没用过，绑到这次运行，表单值换成登记的引用；重复使用、用别人的被拒；
  回复待办时同样绑到运行；
- 下载：上传的人、读得到绑定那次运行的人能下；别人 403；没提交的别人连在不在都不知道；
- 删除：只能删自己的、还没提交的；清理：24 小时前上传、没提交的删掉；
- file_text：运行里只读绑定的文件，超过上限照实截断，记 file.extracted；
- table_import_period：按配方导入一期，要人确认的地方不替人做主。

夹具里的人名、门店名是假名。
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import uuid
from datetime import timedelta
from urllib.parse import quote

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.api.deps import Principal
from app.core.artifact_store import graph_hash
from app.core.config import settings
from app.db.base import SessionLocal, utcnow
from app.db.models import Run, RunEvent, Workflow, WorkflowVersion
from app.db.models_apps import AppGrant
from app.db.models_blueprints import StoredFile
from app.engine.runner import run_manager
from app.files import store

TEXT = "门店本周报损：一店 3 件，二店 5 件。原因多为过期。" * 3
FILE_FIELD = {"type": "object", "x-widget": "file", "title": "附件", "x-file": {"accept": [".txt", ".csv"]}}


@pytest.fixture(autouse=True)
def data_dir(monkeypatch, tmp_path):
    data = tmp_path / "data"
    (data / "uploads").mkdir(parents=True)
    monkeypatch.setattr(settings, "data_dir", data)
    yield data


@pytest.fixture
async def engine_up(data_dir):
    await run_manager.setup()
    yield
    await run_manager.shutdown()


@pytest.fixture
async def client(engine_up):
    from app.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


def node(nid, ntype, **config):
    return {"id": nid, "type": ntype, "position": {"x": 0, "y": 0}, "data": {"label": nid, "config": config}}


def chain(*nodes):
    return {"nodes": list(nodes), "edges": [{"source": a["id"], "target": b["id"]} for a, b in zip(nodes, nodes[1:])]}


async def wait(run_id: str, statuses=("interrupted", "failed", "succeeded"), timeout=30.0) -> Run:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        async with SessionLocal() as session:
            row = await session.get(Run, run_id)
            if row and row.status in statuses and not run_manager.is_active(row.id):
                return row
        await asyncio.sleep(0.05)
    raise AssertionError(f"运行 {run_id} 没有进入 {statuses}")


def actor(name: str) -> dict[str, str]:
    return {"X-Actor": quote(name)}


GRAPH = chain(node("start", "input", fields=[{"name": "doc", "type": "file"}, {"name": "note"}]),
              node("out", "output", fields=[{"name": "text", "value": "{{ file_text(input.doc, 12) }}"},
                                            {"name": "ref", "value": "{{ input.doc.sha256 }}"}]))


async def published_app(client) -> str:
    """一个已发布的应用：入口「报损」带文件字段，成果读附件的前 12 个字。"""
    async with SessionLocal() as session:
        wf = Workflow(name=f"报损-{uuid.uuid4().hex[:6]}", graph=GRAPH, status="published", published_version=1)
        session.add(wf)
        await session.flush()
        session.add(WorkflowVersion(workflow_id=wf.id, version=1, graph=GRAPH, graph_hash=graph_hash(GRAPH),
                                    level="published", published_at=utcnow()))
        await session.commit()
        wf_id = wf.id
    spec = {"format": "agentlab.app/1", "name": "报损",
            "pins": [{"alias": "main", "workflow_id": wf_id, "version": 1, "graph_hash": graph_hash(GRAPH),
                      "use": "entry"}],
            "entries": [{"id": "report", "label": "报损", "pin": "main", "form": {"$ref": "#/forms/report"},
                         "map": {"doc": "form.doc", "note": "form.note"}}],
            "forms": {"report": {"type": "object", "required": ["doc"],
                                 "properties": {"doc": FILE_FIELD, "note": {"type": "string", "title": "说明"}}}},
            "pages": [{"id": "home", "title": "首页", "body": [
                {"id": "new", "type": "entry_button", "props": {"entry": "report"}}]}]}
    res = await client.post("/api/apps", json={"name": "报损", "spec": spec}, headers=actor("建立人"))
    assert res.status_code == 201, res.text
    app_id = res.json()["id"]
    published = (await client.post(f"/api/apps/{app_id}/publish", json={})).json()
    assert published["ok"], published
    return app_id


async def upload(client, app_id: str, name: str, content: bytes, who: str = "amy", **ctx):
    context = {"app_id": app_id, "entry": "report", "field": "doc", **ctx}
    return await client.post("/api/files", files={"file": (name, content, "application/octet-stream")},
                             data={"context": json.dumps(context)}, headers=actor(who))


async def row_of(file_id: str) -> StoredFile | None:
    async with SessionLocal() as session:
        return await session.get(StoredFile, file_id)


# --------------------------------------------------------------------------
# 上传
# --------------------------------------------------------------------------


async def test_upload_checks_context_type_and_content(client):
    app_id = await published_app(client)
    res = await upload(client, app_id, "报损.txt", TEXT.encode())
    assert res.status_code == 201, res.text
    ref = res.json()
    assert set(ref) == {"file_id", "name", "size", "sha256", "type"} and ref["type"] == "text/plain"
    path = store.blob_path(ref["sha256"])
    assert path.read_bytes() == TEXT.encode() and (path.stat().st_mode & 0o777) == 0o444
    row = await row_of(ref["file_id"])
    assert row.uploaded_by == "amy" and row.context == {"app_id": app_id, "entry": "report", "field": "doc"}

    cases = [
        (await upload(client, app_id, "假的.pdf", b"not a pdf"), 415, "files.content_mismatch"),
        (await upload(client, app_id, "程序.exe", b"MZ\x00"), 415, "files.type_not_allowed"),
        (await upload(client, app_id, "合同.pdf", b"%PDF-1.4 x"), 415, "files.type_not_accepted"),
        (await upload(client, app_id, "空.txt", b""), 422, "files.empty"),
        (await upload(client, app_id, "报损.txt", b"x", entry="ghost"), 404, "files.context_invalid"),
        (await client.post("/api/files", files={"file": ("a.txt", b"x", "text/plain")}, data={"context": "{}"}),
         422, "files.context_invalid"),
    ]
    for res, status, code in cases:
        assert (res.status_code, res.json().get("code")) == (status, code), res.text
    # 没有字段限定的地方（不说是哪个字段）照白名单收 PDF
    pdf = await upload(client, app_id, "合同.pdf", b"%PDF-1.4 x", field="")
    assert pdf.status_code == 201 and pdf.json()["type"] == "application/pdf"


# --------------------------------------------------------------------------
# 提交入口：绑定、重复使用、别人的、file_text
# --------------------------------------------------------------------------


async def test_entry_binds_the_file_once_and_file_text_reads_it(client):
    app_id = await published_app(client)
    ref = (await upload(client, app_id, "报损.txt", TEXT.encode())).json()
    res = await client.post(f"/api/apps/{app_id}/entries/report", json={"values": {"doc": ref, "note": "本周"}},
                            headers=actor("amy"))
    assert res.status_code == 201, res.text
    run_id = res.json()["run_id"]
    done = await wait(run_id, ("succeeded", "failed"))
    assert done.status == "succeeded", done.error
    assert (await row_of(ref["file_id"])).bound_run_id == run_id
    assert done.input["doc"] == ref, "运行输入里是登记的那一份引用（连同 sha256）"
    text = done.output["text"]
    assert text.startswith(TEXT[:12]) and f"共 {len(TEXT)} 字" in text and "只读入了前 12 字" in text
    async with SessionLocal() as session:
        events = list((await session.execute(select(RunEvent).where(RunEvent.run_id == run_id))).scalars())
    extracted = [e.data for e in events if e.type == "file.extracted"]
    assert len(extracted) == 1
    assert extracted[0]["truncated"] is True and extracted[0]["chars"] == 12
    assert extracted[0]["total_chars"] == len(TEXT) and extracted[0]["artifact"]
    # 可验证报告的事件名表认得它（运行时间线的说法在前端 run/decode.ts，check-decode 有一节）
    from pathlib import Path

    from app import bundle as bundle_pkg

    verifier = (Path(bundle_pkg.__file__).parent / "verifier.js").read_text(encoding="utf-8")
    assert "'file.extracted': '读入附件'" in verifier

    again = await client.post(f"/api/apps/{app_id}/entries/report", json={"values": {"doc": ref}},
                              headers=actor("amy"))
    assert again.status_code == 422
    assert again.json()["errors"][0]["path"] == "doc" and "已经提交过" in again.json()["errors"][0]["message"]
    assert again.json()["errors"][0]["code"] == "file"

    bobs = (await upload(client, app_id, "别人的.txt", "二店报损".encode(), who="bob")).json()
    stolen = await client.post(f"/api/apps/{app_id}/entries/report", json={"values": {"doc": bobs}},
                               headers=actor("amy"))
    assert stolen.status_code == 422 and "不是你上传的" in stolen.json()["errors"][0]["message"]
    assert (await row_of(bobs["file_id"])).bound_run_id is None, "被拒的提交不占文件"
    forged = await client.post(f"/api/apps/{app_id}/entries/report",
                               json={"values": {"doc": {**bobs, "file_id": "x"}}}, headers=actor("bob"))
    assert forged.status_code == 422


async def test_file_text_only_reads_files_bound_to_the_run(engine_up):
    """输入里塞一个没绑到这次运行的引用：读不到，照实说。"""
    async with SessionLocal() as session:
        row = await store.save(session, "机密".encode(), "机密.txt", uploaded_by="bob", context={"blueprint_id": "b"})
        ref = store.ref_of(row)
    run = await run_manager.start(graph=GRAPH, input_payload={"doc": ref}, run_class="exploratory")
    done = await wait(run.id, ("succeeded", "failed"))
    assert done.output["text"] == "（附件「机密.txt」没有随这次运行提交，读不到）"


async def test_task_reply_binds_the_file_to_the_run(client):
    from app.db.models import Task
    from app.process import tasks

    form = {"format": "agentlab.form/1", "type": "object", "required": ["doc"], "properties": {"doc": FILE_FIELD}}
    graph = chain(node("start", "input"), node("fill", "human", mode="input", title="补附件", form=form),
                  node("out", "output", fields=[{"name": "text", "value": "{{ file_text(nodes.fill.value.doc) }}"}]))
    run = await run_manager.start(graph=graph, input_payload={})
    assert (await wait(run.id)).status == "interrupted"
    async with SessionLocal() as session:
        task = (await session.execute(select(Task).where(Task.run_id == run.id))).scalars().one()
    ref = (await client.post("/api/files", files={"file": ("说明.txt", "二店冷柜坏了".encode(), "text/plain")},
                             data={"context": json.dumps({"task_id": task.id, "field": "doc"})},
                             headers=actor("amy"))).json()
    amy = Principal(username="amy", role="admin", mode="off")
    other = (await client.post("/api/files", files={"file": ("x.txt", b"x", "text/plain")},
                               data={"context": json.dumps({"task_id": task.id})}, headers=actor("bob"))).json()
    with pytest.raises(tasks.FormInvalid) as refused:
        await tasks.respond(task.id, amy, decision="submit", values={"doc": other})
    assert refused.value.errors[0]["path"] == "doc"
    out = await tasks.respond(task.id, amy, decision="submit", values={"doc": ref})
    assert out["resolved"] is True
    done = await wait(run.id, ("succeeded", "failed"))
    assert done.status == "succeeded", done.error
    assert done.output["text"] == "二店冷柜坏了"
    assert (await row_of(ref["file_id"])).bound_run_id == run.id


# --------------------------------------------------------------------------
# 下载、删除、清理
# --------------------------------------------------------------------------


async def test_download_follows_the_run_and_delete_only_unbound(client, monkeypatch):
    from app.api import files as files_api

    app_id = await published_app(client)
    suffix = uuid.uuid4().hex[:6]
    amy, cat, dan = f"amy{suffix}", f"cat{suffix}", f"dan{suffix}"
    ref = (await upload(client, app_id, "报损.txt", TEXT.encode(), who=amy)).json()
    res = await client.post(f"/api/apps/{app_id}/entries/report", json={"values": {"doc": ref}}, headers=actor(amy))
    await wait(res.json()["run_id"], ("succeeded", "failed"))
    async with SessionLocal() as session:
        session.add(AppGrant(app_id=app_id, principal_kind="user", principal=cat, role="observe"))
        await session.commit()

    def as_(name: str) -> None:
        monkeypatch.setattr(files_api, "current_principal",
                            lambda: Principal(username=name, role="user", verified=True, mode="local"))

    as_(amy)
    got = await client.get(f"/api/files/{ref['file_id']}")
    assert got.status_code == 200 and got.content == TEXT.encode()
    assert "attachment" in got.headers["content-disposition"]
    as_(cat)
    assert (await client.get(f"/api/files/{ref['file_id']}")).status_code == 200, "能看全部实例的人读得到这次运行"
    as_(dan)
    denied = await client.get(f"/api/files/{ref['file_id']}")
    assert denied.status_code == 403 and denied.json()["code"] == "files.forbidden"

    # 删除：提交过的不能删；没提交的只有自己能删，别人连在不在都不知道
    off = Principal(username=None, role="admin", mode="off")
    monkeypatch.setattr(files_api, "current_principal", lambda: off)
    assert (await client.delete(f"/api/files/{ref['file_id']}", headers=actor(amy))).status_code == 409
    loose = (await upload(client, app_id, "草稿.txt", "草稿".encode(), who=amy)).json()
    as_(dan)
    assert (await client.get(f"/api/files/{loose['file_id']}")).status_code == 404
    monkeypatch.setattr(files_api, "current_principal", lambda: off)
    assert (await client.delete(f"/api/files/{loose['file_id']}", headers=actor(dan))).status_code == 404
    assert (await client.delete(f"/api/files/{loose['file_id']}", headers=actor(amy))).status_code == 204
    assert await row_of(loose["file_id"]) is None and not store.blob_path(loose["sha256"]).exists()


async def test_cleanup_removes_old_unbound_uploads(engine_up):
    async with SessionLocal() as session:
        old = await store.save(session, b"old " + uuid.uuid4().hex.encode(), "old.txt", uploaded_by="amy",
                               context={"blueprint_id": "b"})
        fresh = await store.save(session, b"new " + uuid.uuid4().hex.encode(), "new.txt", uploaded_by="amy",
                                 context={"blueprint_id": "b"})
        kept = await store.save(session, b"kept " + uuid.uuid4().hex.encode(), "kept.txt", uploaded_by="amy",
                                context={"blueprint_id": "b"})
        claimed = await store.save(session, b"claim " + uuid.uuid4().hex.encode(), "c.txt", uploaded_by="amy",
                                   context={"blueprint_id": "b"})
        long_ago = utcnow() - timedelta(hours=25)
        old.uploaded_at = kept.uploaded_at = claimed.uploaded_at = long_ago
        kept.bound_run_id = "some-run"
        claimed.bound_run_id = f"{store.CLAIM_PREFIX}stale"
        await session.commit()
        ids = {r.id: r.sha256 for r in (old, fresh, kept, claimed)}
    removed = await store.cleanup()
    assert removed >= 2
    assert await row_of(old.id) is None and not store.blob_path(ids[old.id]).exists()
    assert await row_of(claimed.id) is None, "占着却没发起成功的，过了时限同样清掉"
    assert await row_of(fresh.id) is not None and await row_of(kept.id) is not None
    assert store.blob_path(ids[kept.id]).exists()


# --------------------------------------------------------------------------
# table_import_period：按配方导入一期
# --------------------------------------------------------------------------


async def test_import_period_uses_the_recipe_and_leaves_decisions_to_people(client, monkeypatch):
    from app.data import raw_store, recipe_imports
    from app.db.models import DataSource
    from app.files.imports import import_period_action
    from tests.test_recipe_imports import FakeState, fake_pipeline, raw_bytes, recipe_source

    pipe = fake_pipeline(FakeState())
    monkeypatch.setattr(recipe_imports, "pipeline", lambda: pipe)
    done = await recipe_source(client)
    name, before = done["source"]["name"], done["snapshot_id"]
    run_id = uuid.uuid4().hex

    async def bound_file(raw: bytes, *, run: str | None = run_id) -> dict:
        sha, _ = raw_store.put_addressed(raw, store.files_root())
        async with SessionLocal() as session:
            row = StoredFile(sha256=sha, name="月报导出_2026-09-01_2026-09-30.xlsx", size=len(raw),
                             mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                             uploaded_by="amy", context={"app_id": "a", "entry": "e"}, bound_run_id=run)
            session.add(row)
            await session.commit()
            return store.ref_of(row)

    # 假管线每次都要人勾「导入模式」：不替人勾，照实拒绝，暂存留着
    blocked = await import_period_action({"source": name, "file": await bound_file(raw_bytes())}, run_id=run_id,
                                         on_behalf={"sub": "amy", "act": "cat"}, environment="prod")
    assert blocked.status is None and "要在「数据」页由人确认" in blocked.error and "导入模式" in blocked.error
    # 没有要人判断的地方：按配方导入成新的一期
    monkeypatch.setattr(recipe_imports, "pipeline", lambda: dataclasses.replace(pipe, confirm_items=lambda ctx: []))
    ok = await import_period_action({"source": name, "file": await bound_file(raw_bytes())}, run_id=run_id,
                                    on_behalf={"sub": "amy", "act": "cat"}, environment="prod")
    assert ok.status == 200, ok.error
    async with SessionLocal() as session:
        source = (await session.execute(select(DataSource).where(DataSource.name == name))).scalar_one()
    assert source.current_snapshot_id == ok.body["snapshot_id"] != before
    # 没随这次运行提交的文件、不是按配方导入的源：拒绝
    other = await import_period_action({"source": name, "file": await bound_file(raw_bytes(), run="x")},
                                       run_id=run_id, on_behalf=None, environment="prod")
    assert other.status is None and "没有随这次运行提交" in other.error
    wrong = await import_period_action({"source": "no_such_source", "file": await bound_file(raw_bytes())},
                                       run_id=run_id, on_behalf=None, environment="prod")
    assert wrong.status is None and "不是按配方导入" in wrong.error


def test_form_file_widget_rules():
    from app.apps.forms import input_schema, validate_form, validate_form_schema
    from app.apps.spec import FORM_ERROR_CODES

    form = {"type": "object", "required": ["doc"], "properties": {"doc": FILE_FIELD}}
    assert validate_form_schema(form) == []
    assert any("只有文件控件能用" in p for p in validate_form_schema(
        {"type": "object", "properties": {"x": {"type": "object"}}}))
    assert any("文件控件，类型只能是对象" in p for p in validate_form_schema(
        {"type": "object", "properties": {"x": {"type": "string", "x-widget": "file"}}}))
    assert any("文件的限制" in p or "不是文件控件" in p for p in validate_form_schema(
        {"type": "object", "properties": {"x": {"type": "string", "x-file": {}}}}))
    ref = {"file_id": "f", "name": "a.txt", "size": 3, "sha256": "0" * 64, "type": "text/plain"}
    assert validate_form(form, {"doc": ref}) == []
    assert validate_form(form, {})[0]["code"] == "required"
    assert validate_form(form, {"doc": "a.txt"})[0] == {"path": "doc", "code": "file", "message": "「附件」需要先上传文件"}
    wrong = validate_form(form, {"doc": {**ref, "name": "a.pdf", "type": "application/pdf"}})
    assert wrong and wrong[0]["code"] == "file" and "只收这些类型" in wrong[0]["message"]
    assert "file" in FORM_ERROR_CODES, "文件控件的错误有自己的 code（前端 FormError.code 同一份）"
    schema = input_schema([{"name": "doc", "type": "file", "required": True, "accept": [".pdf"], "max_mb": 5}])
    assert schema["properties"]["doc"] == {"type": "object", "x-widget": "file", "title": "doc",
                                           "x-file": {"accept": [".pdf"], "max_mb": 5}}
    assert schema["required"] == ["doc"] and validate_form_schema(schema) == []


# --------------------------------------------------------------------------
# 试运行的示例表单带附件（复核 M9、L9）
# --------------------------------------------------------------------------


async def test_a_trial_with_an_attachment_binds_the_file_to_the_trial_run(client):
    """流程类试运行填了示例表单：和应用入口提交同一条路——文件核对是这个人在这张蓝图上传的、还没用过，绑到这次试运行，
    表单值换成登记的引用；填报登记的「填什么」可以有文件字段（记录表的文件列存引用）。别处上传的文件不收。"""
    from tests import blueprint_samples as s

    bp = (await client.post("/api/blueprints", json={"source": {"kind": "template", "id": "records"}, "scope": "shared",
                                                     "name": "报损附件"}, headers=actor("amy"))).json()
    await s.edit_spec(bp["id"], purpose=s.item("门店报损，附照片"), people=[], data=[], fields=[
        {"name": "store", "spec": s.item({"type": "string", "title": "门店", "required": True}), "sensitive": False},
        {"name": "doc", "spec": s.item({k: v for k, v in FILE_FIELD.items() if k != "x-file"}), "sensitive": False}])
    got = (await client.get(f"/api/blueprints/{bp['id']}", headers=actor("amy"))).json()
    assert not [i for i in got["issues"] if i["code"] == "bp.template_mismatch"], "填报登记收文件字段"

    up = await client.post("/api/files", files={"file": ("报损.txt", TEXT.encode(), "text/plain")},
                           data={"context": json.dumps({"blueprint_id": bp["id"], "field": "doc"})}, headers=actor("amy"))
    assert up.status_code == 201, up.text
    ref = up.json()
    r = await client.post(f"/api/blueprints/{bp['id']}/trial", json={"inputs": {"store": "一店", "doc": ref}},
                          headers=actor("amy"))
    assert r.status_code == 200, r.text
    run = await wait(r.json()["run_id"], ("succeeded", "failed"))
    assert run.status == "succeeded", run.error
    assert run.input["doc"]["file_id"] == ref["file_id"] and run.input["doc"]["sha256"] == ref["sha256"]
    assert run.input["mode"] == "submit"
    assert (await row_of(ref["file_id"])).bound_run_id == run.id, "绑到这次试运行"

    # 同一个文件再提交一次：已经用过了
    again = await client.post(f"/api/blueprints/{bp['id']}/trial", json={"inputs": {"store": "一店", "doc": ref}},
                              headers=actor("amy"))
    assert again.status_code == 409 and again.json()["code"] == "files.already_bound", again.text
    # 别人的文件：不收
    other = await client.post("/api/files", files={"file": ("别人.txt", TEXT.encode(), "text/plain")},
                              data={"context": json.dumps({"blueprint_id": bp["id"], "field": "doc"})},
                              headers=actor("bob"))
    bad = await client.post(f"/api/blueprints/{bp['id']}/trial", json={"inputs": {"store": "一店", "doc": other.json()}},
                            headers=actor("amy"))
    assert bad.status_code == 422 and bad.json()["code"] == "files.not_yours", bad.text
