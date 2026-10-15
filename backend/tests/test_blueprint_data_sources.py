"""「看哪些数」对到的数据源和表在不在（blueprints/store.data_source_problems）。

卡片的白名单只核形状、不查库：以前对到一个不存在的源（模型自己起的「本应用」），要到试运行发起时才报「绑定的工具
不存在」，用户还找不到在哪改。现在：
- 卡片上就照实说是哪一项、怎么改（bp.data_source_unknown / bp.data_table_unknown，带白话），挡住试运行和发布；
- 本应用的记录表写 @records，只有填报登记认，表名固定为 records；
- 模型自查（BlueprintChecker）拿得到这类问题，交回它改。

库和数字都是编的。
"""
from __future__ import annotations

import sqlite3
import uuid

import pytest
from httpx import ASGITransport, AsyncClient

from app.blueprints.ops import BpRefused, apply_bp_op
from app.blueprints.spec import empty_blueprint
from app.blueprints.store import SELF_RECORDS, data_source_problems
from app.data.introspect import introspect
from app.db.base import SessionLocal
from app.db.models import DataSource
from app.engine.runner import run_manager
from app.main import app
from tests import blueprint_samples as s


@pytest.fixture(autouse=True)
async def engine_up():
    await run_manager.setup()
    yield
    await run_manager.shutdown()


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def _source(tmp_path, *, explored: bool = True) -> str:
    path = tmp_path / f"shop_{uuid.uuid4().hex[:6]}.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, amount REAL, paid_at TEXT)")
    name = f"shop_{uuid.uuid4().hex[:6]}"
    async with SessionLocal() as session:
        row = DataSource(name=name, kind="sqlite", database=str(path), readonly=True, enabled=True)
        session.add(row)
        await session.commit()
        if explored:
            row.schema_cache = await introspect(row)
            await session.commit()
    return name


def _spec(*tables: dict) -> dict:
    spec = empty_blueprint()
    spec["data"] = [{"id": f"m{i}", "label": s.item(f"指标{i}"), "table": s.item(t)} for i, t in enumerate(tables)]
    return spec


def test_the_apps_own_records_table_has_a_fixed_name():
    spec = empty_blueprint()
    spec = apply_bp_op(spec, {"op": "bp_add", "path": "/data", "value": {"id": "total"}}, source="user")
    spec = apply_bp_op(spec, {"op": "bp_set", "path": "/data/0/table",
                              "value": {"source": SELF_RECORDS, "table": "生产日报记录"}}, source="assistant")
    assert spec["data"][0]["table"]["value"] == {"source": SELF_RECORDS, "table": "records"}
    with pytest.raises(BpRefused):
        apply_bp_op(spec, {"op": "bp_set", "path": "/data/0/table", "value": {"source": "", "table": "x"}}, source="user")


async def test_unknown_sources_and_tables_are_named_on_the_card(tmp_path):
    known = await _source(tmp_path)
    bare = await _source(tmp_path, explored=False)
    spec = _spec({"source": "本应用", "table": "生产日报记录"}, {"source": known, "table": "orders"},
                 {"source": known, "table": "refunds"}, {"source": bare, "table": "anything"},
                 {"source": SELF_RECORDS, "table": "records"})
    async with SessionLocal() as session:
        board = await data_source_problems(session, spec, template_id="periodic_board")
        records = await data_source_problems(session, spec, template_id="records")
    by_path = {i["path"]: i for i in board}
    # 不存在的源：说是哪一项、怎么改
    assert by_path["/data/0/table"]["code"] == "bp.data_source_unknown" and "「本应用」不存在" in by_path["/data/0/table"]["message"]
    # 对得上的不报；表结构探查过、表不在里面的照实说；没探查过的不猜
    assert "/data/1/table" not in by_path and "/data/3/table" not in by_path
    assert by_path["/data/2/table"]["code"] == "bp.data_table_unknown" and "refunds" in by_path["/data/2/table"]["message"]
    # 本应用的记录表只有填报登记认
    assert by_path["/data/4/table"]["code"] == "bp.data_source_unknown"
    records_paths = {i["path"]: i for i in records}
    assert "/data/4/table" not in records_paths
    assert "本应用的记录表" in records_paths["/data/0/table"]["message"], "填报登记提示改选本应用的记录表"
    assert all(i["level"] == "error" for i in board + records)


async def test_a_trial_on_an_unknown_source_is_refused_with_the_card_problem(client, tmp_path):
    source = await s.register_source(s.make_source(tmp_path))
    wf_id = await s.published_workflow(s.board_graph(source), f"周报-{uuid.uuid4().hex[:4]}")
    bp = (await client.post("/api/blueprints", json={"source": {"kind": "workflow_version", "id": wf_id},
                                                     "scope": "shared"})).json()
    await s.edit_spec(bp["id"], purpose=s.item("每周看销售额"), cadence=s.item(s.WEEKLY),
                      data=_spec({"source": "本应用", "table": "orders"})["data"])
    got = (await client.get(f"/api/blueprints/{bp['id']}")).json()
    issue = next(i for i in got["issues"] if i["code"] == "bp.data_source_unknown")
    assert issue["path"] == "/data/0/table" and issue["plain"]["who"] == "you"
    r = await client.post(f"/api/blueprints/{bp['id']}/trial", json={"inputs": s.PERIOD})
    assert r.status_code == 409, r.text
    assert "本应用" in r.text, "挡在发起之前，照实说是哪一项，不是「绑定的工具不存在」"


async def test_the_assistants_self_check_sees_unknown_sources():
    from app.blueprints.generation import BlueprintApplier, BlueprintChecker

    spec = _spec({"source": "本应用", "table": "生产日报记录"})
    checker = BlueprintChecker(BlueprintApplier(spec), template_id="records")
    issues = await checker.issues()
    assert any(i["code"] == "bp.data_source_unknown" for i in issues)
