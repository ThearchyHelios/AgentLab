"""对话里的纠正变成目录修改提案（阶段 4A）。

用户在助手（画布）或问数据页说出一条数据事实（「status=9 表示作废」），模型发一条 catalog_patch 操作；服务端
核对数据源、表、路径和取值格式，附上当前版本、改前和改后的值转给前端——**服务端绝不自动写入**，要人在卡片上点
「保存到数据目录」，带着读到的版本走 /catalog/{table}/patch（记为人工填写、已确认）。

- 纯函数：plan_patch 逐项核对并算出改前、改后；码值是补充，不整份替换；新增关联关系写 relations.new，编号按两端算；
  计算公式不进目录。
- 流：合法的转出、带版本和改前值；不合法的丢弃并记日志；写在 reply 后面的提案也排到 reply 前面转出（前端收到
  reply 就收尾）；只有提案、没有回答时补一句回答；全程不写库。
- 接口：预览只读；保存带 if_version，版本不符 409，X-Actor 记成署名。
"""
from __future__ import annotations

import json
import logging
import uuid
from types import SimpleNamespace
from urllib.parse import quote

import pytest
from httpx import ASGITransport, AsyncClient

from app.data import catalog
from app.data.engine import engines
from app.data.introspect import introspect
from tests.fixtures.catalog import scenic_notes

_CACHE: dict[str, dict] = {}


async def _schema(path: str) -> dict:
    if path not in _CACHE:
        probe = SimpleNamespace(id=f"probe-{uuid.uuid4().hex[:6]}", name="scenic", kind="sqlite", database=path,
                                host=None, port=None, username=None, password=None, options={}, readonly=True,
                                description="", schema_cache={}, origin="manual")
        try:
            _CACHE[path] = await introspect(probe)
        finally:
            await engines.invalidate(probe.id)
    return _CACHE[path]


def _visits() -> dict:
    return scenic_notes.notes()["visits"]


# --------------------------------------------------------------------------
# plan_patch / apply_patch：纯函数
# --------------------------------------------------------------------------


async def test_plan_patch_reports_before_and_after(scenic_db):
    tables = (await _schema(scenic_db))["tables"]
    plan = catalog.plan_patch(_visits(), [
        {"path": "columns.visitor_count.measure", "value": "stock", "reason": "在园人数是存量，不能跨天相加"},
        {"path": "kind", "value": "fact"},
    ], table="visits", tables=tables)
    assert plan.problems == []
    measure, kind = plan.changes
    assert (measure.path, measure.before, measure.before_status, measure.after) == (
        "columns.visitor_count.measure", "flow", "confirmed", "stock")
    assert measure.state == "change" and measure.reason == "在园人数是存量，不能跨天相加"
    assert (kind.path, kind.before, kind.before_status, kind.after, kind.state) == (
        "kind", None, None, "fact", "change")


async def test_codes_are_supplemented_not_replaced(scenic_db):
    tables = (await _schema(scenic_db))["tables"]
    plan = catalog.plan_patch(_visits(), [{"path": "columns.status.codes", "value": {"9": "作废"}}],
                              table="visits", tables=tables)
    [change] = plan.changes
    assert change.before == {"1": "有效", "0": "作废"}
    assert change.after == {"1": "有效", "0": "作废", "9": "作废"}
    # 交回保存的是原样的补充值：409 之后在最新的目录上重新补
    assert change.value == {"9": "作废"}


async def test_new_relation_gets_its_id_from_both_ends(scenic_db):
    tables = (await _schema(scenic_db))["tables"]
    value = {"columns": ["visit_id"], "to_table": "visits", "to_columns": ["id"], "cardinality": "many_to_one"}
    plan = catalog.plan_patch({}, [{"path": "relations.new", "value": value}], table="channel_visits", tables=tables)
    [change] = plan.changes
    rid = catalog.relation_id("channel_visits", ["visit_id"], "visits", ["id"])
    assert change.path == f"relations.{rid}" and change.before is None
    assert change.after == {**value, "coverage": None}
    # 已有编号、两端对得上：改基数
    notes = catalog.apply_patch({}, [{"path": "relations.new", "value": value}], table="channel_visits", tables=tables)
    again = catalog.plan_patch(notes, [{"path": f"relations.{rid}", "value": {**value, "cardinality": "one_to_one"}}],
                               table="channel_visits", tables=tables)
    assert again.changes[0].before["cardinality"] == "many_to_one" and again.problems == []
    # 编号对不上两端：不认（改指向是另一条关系）
    moved = catalog.plan_patch(notes, [{"path": f"relations.{rid}",
                                        "value": {**value, "to_table": "channels", "to_columns": ["id"]}}],
                               table="channel_visits", tables=tables)
    assert moved.changes == [] and len(moved.problems) == 1


async def test_invalid_changes_are_reported(scenic_db):
    tables = (await _schema(scenic_db))["tables"]
    plan = catalog.plan_patch(_visits(), [
        {"path": "formula", "value": "下单数 / 访问数"},                              # 不是目录的项
        {"path": "columns.visitor_count.measure", "value": "累计值"},                 # 不在可选值内
        {"path": "columns.ghost.meaning", "value": "不存在的列"},                       # 表结构里没有这一列
        {"path": "columns.visitor_count.meaning", "value": "转化率=下单数/访问数"},     # 计算公式不进目录
        {"path": "keys", "value": ["ticket_no", "nope"]},                             # 业务主键里有不存在的列
        {"path": "relations.new", "value": {"columns": ["park_id"], "to_table": "ghost_table", "to_columns": ["id"]}},
        "不是对象",
    ], table="visits", tables=tables)
    assert plan.changes == []
    assert len(plan.problems) == 7
    assert any("口径卡" in p for p in plan.problems)
    # 报错写界面上的叫法，不裸写键名
    assert not any("measure" in p or "meaning" in p for p in plan.problems)


async def test_same_value_states(scenic_db):
    tables = (await _schema(scenic_db))["tables"]
    proposed = scenic_notes.notes("proposed")["visits"]
    plan = catalog.plan_patch(proposed, [{"path": "columns.visitor_count.measure", "value": "flow"}],
                              table="visits", tables=tables)
    # 值没变、只是推断：保存即确认
    assert plan.changes[0].state == "confirm"
    plan = catalog.plan_patch(_visits(), [{"path": "columns.visitor_count.measure", "value": "flow"}],
                              table="visits", tables=tables)
    assert plan.changes[0].state == "same"


async def test_apply_patch_records_human_confirmed(scenic_db):
    tables = (await _schema(scenic_db))["tables"]
    proposed = scenic_notes.notes("proposed")["visits"]
    out = catalog.apply_patch(proposed, [
        {"path": "columns.visitor_count.measure", "value": "stock"},
        {"path": "valid_filter", "value": "status = 1"},   # 值没变：只确认，来源不变
    ], table="visits", tables=tables, at="2026-10-03T00:00:00+00:00")
    measure = out["columns"]["visitor_count"]["measure"]
    assert (measure["value"], measure["source"], measure["status"]) == ("stock", "human", "confirmed")
    assert (out["valid_filter"]["source"], out["valid_filter"]["status"]) == ("llm", "confirmed")
    # 其余项原样
    assert out["columns"]["status"] == proposed["columns"]["status"]
    with pytest.raises(catalog.CatalogInvalid):
        catalog.apply_patch(proposed, [{"path": "kind", "value": "nope"}], table="visits", tables=tables)


# --------------------------------------------------------------------------
# 协议
# --------------------------------------------------------------------------


def test_protocol_describes_catalog_patch():
    from app.api.copilot import _STREAM_PROTOCOL

    assert '"op":"catalog_patch"' in _STREAM_PROTOCOL
    assert "relations.new" in _STREAM_PROTOCOL and "columns.<列名>.<字段>" in _STREAM_PROTOCOL
    # 两条边界：只在用户明确陈述数据事实时提；计算公式进口径卡
    assert "明确" in _STREAM_PROTOCOL and "口径卡" in _STREAM_PROTOCOL


# --------------------------------------------------------------------------
# 流：校验后转出，不写库
# --------------------------------------------------------------------------


class _Scripted:
    def __init__(self, ops):
        self.ops = ops
        self.calls: list[str] = []

    async def astream(self, messages):
        from langchain_core.messages import AIMessageChunk

        self.calls.append(next(text for role, text in messages if role == "system"))
        for op in self.ops:
            yield AIMessageChunk(content=json.dumps(op, ensure_ascii=False) + "\n")


@pytest.fixture
async def scenic_source(scenic_db):
    """库里一个带目录的景区数据源；用完删掉（级联删掉目录），不留在测试库里。"""
    from app.db.base import SessionLocal
    from app.db.models import DataSource

    name = f"scenic_{uuid.uuid4().hex[:8]}"
    async with SessionLocal() as session:
        row = DataSource(name=name, kind="sqlite", database=scenic_db, readonly=True)
        row.schema_cache = await _schema(scenic_db)
        session.add(row)
        await session.commit()
        for table, notes in scenic_notes.notes().items():
            await catalog.write_entry(session, row.id, table, notes, if_version=0, actor="王敏")
        source_id = row.id
    yield SimpleNamespace(id=source_id, name=name)
    async with SessionLocal() as session:
        row = await session.get(DataSource, source_id)
        if row is not None:
            await session.delete(row)
            await session.commit()


async def _stream(monkeypatch, model, source, base_graph=None):
    import app.api.copilot as copilot
    from app.main import app

    async def _model(*_a, **_k):
        return model, "mock-fast"

    monkeypatch.setattr(copilot, "get_chat_model", _model)
    events: list[dict] = []
    body = {"instruction": "status=9 表示作废，统计时要排除", "intent": "answer", "datasource_ids": [source.id]}
    if base_graph:
        body["base_graph"] = base_graph
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        async with client.stream("POST", "/api/copilot/generate-stream", json=body) as r:
            async for line in r.aiter_lines():
                if line.startswith("data: "):
                    events.append(json.loads(line[6:]))
    return events


async def _entry(source_id: str, table: str):
    from app.db.base import SessionLocal

    async with SessionLocal() as session:
        return await catalog.read_entry(session, source_id, table)


async def test_stream_forwards_checked_patch_and_writes_nothing(scenic_source, monkeypatch, caplog):
    before = await _entry(scenic_source.id, "visits")
    patch = {"op": "catalog_patch", "source": scenic_source.name, "table": "visits", "changes": [
        {"path": "columns.status.codes", "value": {"9": "作废"}, "reason": "用户说明 status=9 表示作废"},
        {"path": "valid_filter", "value": "status = 1 AND status <> 9", "reason": "统计时排除作废"},
        {"path": "columns.status.colour", "value": "红"},                       # 不合法：丢弃，其余照转
    ]}
    model = _Scripted([
        {"op": "catalog_patch", "source": "不存在的库", "table": "visits",
         "changes": [{"path": "kind", "value": "fact"}]},                       # 数据源不在范围内：整条丢弃
        {"op": "catalog_patch", "source": scenic_source.name, "table": "ghost",
         "changes": [{"path": "kind", "value": "fact"}]},                       # 表不存在：整条丢弃
        {"op": "reply", "text": "好的，已整理成数据目录的修改建议。"},
        patch,                                                                   # 写在 reply 后面：照样转出，排在前面
    ])
    with caplog.at_level(logging.WARNING, logger="app.api.copilot"):
        events = await _stream(monkeypatch, model, scenic_source)
    ops = [e["op"] for e in events]
    assert ops.count("catalog_patch") == 1 and ops[-1] == "reply"
    assert ops.index("catalog_patch") < ops.index("reply")
    [out] = [e for e in events if e["op"] == "catalog_patch"]
    assert out["source"] == scenic_source.name and out["source_id"] == scenic_source.id
    assert out["table"] == "visits" and out["table_label"] == "入园记录" and out["version"] == before.version
    codes, valid = out["changes"]
    assert codes["path"] == "columns.status.codes" and codes["before"] == {"1": "有效", "0": "作废"}
    assert codes["after"] == {"1": "有效", "0": "作废", "9": "作废"} and codes["value"] == {"9": "作废"}
    assert codes["before_status"] == "confirmed" and codes["reason"] == "用户说明 status=9 表示作废"
    assert valid["before"] == "status = 1" and valid["after"] == "status = 1 AND status <> 9"
    # 丢掉的记了日志
    assert sum("目录修改提案" in r.getMessage() for r in caplog.records) >= 3
    # 服务端没写库
    after = await _entry(scenic_source.id, "visits")
    assert after.version == before.version and after.notes == before.notes
    # 协议进了 system
    assert '"op":"catalog_patch"' in model.calls[0]


async def test_patch_only_gets_a_reply(scenic_source, monkeypatch):
    """只发了提案、没回答也没改图：补一句回答收尾，不当成「模型没给出任何修改」。"""
    model = _Scripted([{"op": "catalog_patch", "source": scenic_source.name, "table": "visits",
                        "changes": [{"path": "columns.visitor_count.measure", "value": "stock"}]}])
    events = await _stream(monkeypatch, model, scenic_source)
    assert [e["op"] for e in events if e["op"] not in ("model", "heartbeat", "context")] == ["catalog_patch", "reply"]
    assert "数据目录" in events[-1]["text"]


async def test_patch_alongside_graph_ops(scenic_source, monkeypatch):
    """和图操作一起出现：提案照转，图照常收尾。"""
    model = _Scripted([
        {"op": "plan", "summary": "查入园人数"},
        {"op": "catalog_patch", "source": scenic_source.name, "table": "visits",
         "changes": [{"path": "columns.visitor_count.measure", "value": "stock"}]},
        {"op": "add_node", "node": {"id": "start", "type": "input", "label": "输入", "config": {}}},
        {"op": "add_node", "node": {"id": "out", "type": "output", "label": "成果", "config": {"fields": []}}},
        {"op": "add_edge", "edge": {"source": "start", "target": "out"}},
        {"op": "done", "explanation": "", "run": False},
    ])
    events = await _stream(monkeypatch, model, scenic_source)
    ops = [e["op"] for e in events]
    assert "catalog_patch" in ops and ops[-1] == "final"
    assert ops.index("catalog_patch") < ops.index("final")


# --------------------------------------------------------------------------
# 接口：预览（只读）与保存
# --------------------------------------------------------------------------


async def test_preview_and_save(scenic_source):
    from app.main import app

    base = f"/api/datasources/{scenic_source.id}/catalog/visits"
    changes = [{"path": "columns.status.codes", "value": {"9": "作废"}, "reason": "作废单"}]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.post(f"{base}/patch/preview", json={"changes": changes})
        assert r.status_code == 200, r.text
        preview = r.json()
        version = preview["version"]
        assert preview["changes"][0]["after"] == {"1": "有效", "0": "作废", "9": "作废"}
        assert preview["problems"] == []
        # 预览不写库
        assert (await _entry(scenic_source.id, "visits")).version == version

        r = await client.post(f"{base}/patch", json={"changes": changes, "if_version": version},
                              headers={"X-Actor": quote("王敏")})
        assert r.status_code == 200, r.text
        detail = r.json()
        assert detail["version"] == version + 1 and detail["updated_by"] == "王敏"
        codes = detail["notes"]["columns"]["status"]["codes"]
        assert codes["value"]["9"] == "作废" and (codes["source"], codes["status"]) == ("human", "confirmed")

        # 拿旧版本再存：409，什么都不写
        r = await client.post(f"{base}/patch", json={"changes": changes, "if_version": version})
        assert r.status_code == 409 and "刚被修改过" in r.json()["detail"]
        # 重新预览：已经是这个值了
        again = (await client.post(f"{base}/patch/preview", json={"changes": changes})).json()
        assert again["version"] == version + 1 and again["changes"][0]["state"] == "same"

        # 不合法的取值：422，原话说清
        r = await client.post(f"{base}/patch", json={"changes": [{"path": "kind", "value": "nope"}],
                                                       "if_version": version + 1})
        assert r.status_code == 422 and "表类型" in r.json()["detail"]
