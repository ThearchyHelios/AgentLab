"""目录视图接缝（data/catalog_view.py；细案 01 做法 5，总方案 X7）。

目录在四处被读：运行时的 db_schema / db_query（目录同时冻结进表结构快照）、助手查看的表详情、自查的 SQL 检查器、双盲复算。
这里守：四种视图（当前目录、只用已核实和已确认的项、关、目录快照）在四处给出的是同一份；缺省是当前目录、行为不变；运行
从 Run.options 读视图（随发起定下，审批恢复、续跑沿用），冻结快照的 meta 记下不是缺省的视图；lint：这几个消费模块不许
绕过视图直接 read_catalog、直接查 CatalogNote。

库、表、列、目录内容都是虚构的。
"""
from __future__ import annotations

import asyncio
import re
import sqlite3
import uuid
from pathlib import Path

import pytest
from sqlalchemy import select

from app.core.artifact_store import put_json
from app.data import catalog, catalog_view
from app.data.catalog import make_item
from app.data.catalog_view import CatalogSnapshotMissing, CatalogView, CatalogViewInvalid
from app.data.introspect import introspect
from app.db.base import SessionLocal
from app.db.models import Artifact, DataSource, Run
from app.engine.runner import run_manager
from tests.fixtures.sources import drop_source

APP = Path(__file__).resolve().parents[1] / "app"

#: 已确认的一项、推断的一项、驳回的一项：三种视图各留哪些一眼看得出
NOTES = {
    "label": make_item("门店订单", "human"),                         # confirmed
    "grain": make_item("每笔订单一行", "llm"),                         # proposed
    "description": make_item("旧说明", "llm", "rejected"),             # rejected
    "columns": {"amount": {"label": make_item("实收金额", "human")},
                "status": {"label": make_item("订单状态", "llm")}},
}
SNAPSHOT_NOTES = {"label": make_item("快照里的订单", "human")}

VIEWS = {
    "live": CatalogView(),
    "reviewed": CatalogView(statuses="reviewed"),
    "off": CatalogView(mode="off"),
}


@pytest.fixture
async def shop(tmp_path):
    path = tmp_path / "view_shop.db"
    db = sqlite3.connect(path)
    db.executescript("CREATE TABLE orders (id INTEGER PRIMARY KEY, amount REAL NOT NULL, status INTEGER);"
                     "INSERT INTO orders VALUES (1, 30.0, 1), (2, 20.0, 9);")
    db.commit()
    db.close()
    async with SessionLocal() as session:
        row = DataSource(name=f"viewshop_{uuid.uuid4().hex[:6]}", kind="sqlite", database=str(path))
        session.add(row)
        await session.commit()
        row.schema_cache = await introspect(row)
        await session.commit()
        await session.refresh(row)
        await catalog.write_entry(session, row.id, "orders", NOTES, if_version=0, actor=None)
        session.expunge(row)
    yield row
    await drop_source(row.id)


async def _snapshot_view(source) -> CatalogView:
    sid = await put_json({"sources": {source.id: {"tables": {"orders": {"version": 7, "notes": SNAPSHOT_NOTES}}}}},
                         kind=catalog_view.SNAPSHOT_KIND)
    return CatalogView(mode="snapshot", snapshot=sid)


def _says(text: str) -> dict[str, bool]:
    return {word: word in text for word in ("门店订单", "每笔订单一行", "旧说明", "实收金额", "订单状态", "快照里的订单")}


EXPECT = {
    "live": {"门店订单": True, "每笔订单一行": True, "旧说明": False, "实收金额": True, "订单状态": True, "快照里的订单": False},
    "reviewed": {"门店订单": True, "每笔订单一行": False, "旧说明": False, "实收金额": True, "订单状态": False,
                 "快照里的订单": False},
    "off": dict.fromkeys(("门店订单", "每笔订单一行", "旧说明", "实收金额", "订单状态", "快照里的订单"), False),
    "snapshot": {"门店订单": False, "每笔订单一行": False, "旧说明": False, "实收金额": False, "订单状态": False,
                 "快照里的订单": True},
}


async def _all_views(source) -> dict[str, CatalogView]:
    return {**VIEWS, "snapshot": await _snapshot_view(source)}


# --------------------------------------------------------------------------
# 视图本身
# --------------------------------------------------------------------------


def test_parse_and_defaults():
    assert CatalogView.parse(None) is catalog_view.LIVE and catalog_view.LIVE.is_default
    assert CatalogView.parse({"mode": "off"}) == CatalogView(mode="off")
    assert catalog_view.describe(None) is None and catalog_view.describe(CatalogView()) is None
    assert catalog_view.describe(CatalogView(mode="off")) == {"mode": "off", "statuses": "all"}
    assert catalog_view.of_options({"catalog_view": {"statuses": "reviewed"}}) == CatalogView(statuses="reviewed")
    for bad in ({"mode": "nope"}, {"statuses": "some"}, {"mode": "snapshot"}, {"mode": "live", "snapshot": "x"},
                {"extra": 1}, "off"):
        with pytest.raises(CatalogViewInvalid):
            CatalogView.parse(bad)


async def test_a_missing_snapshot_is_an_error_not_the_live_catalog(shop):
    with pytest.raises(CatalogSnapshotMissing):
        await catalog_view.read(CatalogView(mode="snapshot", snapshot="0" * 64), shop)


# --------------------------------------------------------------------------
# 四处一致
# --------------------------------------------------------------------------


async def test_runtime_db_schema_follows_the_view(shop):
    from app.tools.datasource import SCHEMA_PREFIX, build_datasource_tools
    from app.tools.registry import ToolContext, Versions

    for name, view in (await _all_views(shop)).items():
        ctx = ToolContext(run_id="", node_id="", data_versions=Versions.CURRENT, catalog_view=view)
        async with SessionLocal() as session:
            [tool] = await build_datasource_tools([f"{SCHEMA_PREFIX}{shop.name}"], ctx, session)
        assert _says(await tool.coroutine(table="orders")) == EXPECT[name], name


async def test_look_table_detail_follows_the_view(shop):
    from app.look.service import LookService
    from app.look.tables import TableKind

    for name, view in (await _all_views(shop)).items():
        looks = LookService([await TableKind.load([shop], catalog_view=view)])
        got = await looks.look({"op": "look", "need": "订单", "targets": [{"kind": "table", "name": "orders"}]})
        assert _says(got.text) == EXPECT[name], name


async def test_sql_checkers_follow_the_view(shop):
    from app.data.sqlcheck import load_checkers

    for name, view in (await _all_views(shop)).items():
        async with SessionLocal() as session:
            checkers = await load_checkers(session, [shop], catalog_view=view)
        if name == "off":
            assert checkers == {}
            continue
        notes = checkers[shop.name]._raw["orders"]
        assert _says(str(notes)) | {"旧说明": False} == EXPECT[name] | {"旧说明": False}, name
        if name == "reviewed":
            # 驳回的项原样留着交给检查器（它自己按驳回处理），推断的去掉
            assert notes["description"]["status"] == "rejected" and "grain" not in notes


async def test_dual_check_blind_catalog_follows_the_view(shop):
    from app.engine.nodes.dual_check import BLIND_NO_CATALOG_PROMPT, _catalog_text, _source_info, _Sources

    for name, view in (await _all_views(shop)).items():
        sources = _Sources(view)
        text = await _catalog_text(sources, shop.name)
        assert _says(text) == EXPECT[name], name
        info = await _source_info(sources, shop.name)
        assert ("orders" in info["notes"]) == (name != "off"), name
        if name == "off":
            # 关目录：表清单（表结构里的事实）照给，目录的说明一句都没有
            assert "orders" in text and "数据目录（" not in text and BLIND_NO_CATALOG_PROMPT not in text


async def test_the_default_is_the_live_catalog_everywhere(shop):
    """不给视图：和以前一样读当前目录。"""
    from app.engine.nodes.dual_check import _catalog_text, _Sources
    from app.look.service import LookService
    from app.look.tables import TableKind
    from app.tools.datasource import _catalog_of

    assert _says(str({t: e.notes for t, e in (await _catalog_of(shop)).items()}))["门店订单"]
    looks = LookService([await TableKind.load([shop])])
    got = await looks.look({"op": "look", "need": "订单", "targets": [{"kind": "table", "name": "orders"}]})
    assert _says(got.text) == EXPECT["live"]
    assert _says(await _catalog_text(_Sources(), shop.name)) == EXPECT["live"]


# --------------------------------------------------------------------------
# 运行：从 Run.options 读视图，冻结快照记下
# --------------------------------------------------------------------------


def _graph(source: str) -> dict:
    def node(nid, ntype, **config):
        return {"id": nid, "type": ntype, "position": {"x": 0, "y": 0}, "data": {"label": nid, "config": config}}

    return {"nodes": [node("start", "input"),
                      node("q", "tool", tool=f"db_query__{source}", args={"sql": "SELECT SUM(amount) AS 金额 FROM orders"},
                           assign_to="rows"),
                      node("out", "output", fields=[{"name": "rows", "value": "{{ vars.rows }}"}])],
            "edges": [{"source": "start", "target": "q"}, {"source": "q", "target": "out"}]}


async def _frozen(options: dict | None, source) -> tuple[dict, dict]:
    from app.core.artifact_store import load
    from app.engine.evidence import SCHEMA_SNAPSHOT
    from app.engine.launch import launch_run

    async with SessionLocal() as session:
        run = await launch_run(session, graph=_graph(source.name), input_payload={}, actor="测试",
                               origin_kind="canvas", options=options)
        run_id = run.id
    for _ in range(200):
        async with SessionLocal() as session:
            status = (await session.get(Run, run_id)).status
        if status in ("succeeded", "failed", "cancelled"):
            break
        await asyncio.sleep(0.05)
    assert status == "succeeded", status
    async with SessionLocal() as session:
        ref = (await session.execute(select(Artifact).where(
            Artifact.run_id == run_id, Artifact.kind == SCHEMA_SNAPSHOT))).scalars().first()
    assert ref is not None
    return load(ref.id), ref.meta or {}


async def test_a_run_reads_its_view_from_options_and_the_freeze_says_so(shop):
    await run_manager.setup()
    try:
        content, meta = await _frozen(None, shop)
        assert "orders" in content.get("catalog", {}), "缺省：当前目录照旧冻结"
        assert "catalog_view" not in meta, "缺省视图不记，meta 和以前一样"

        content, meta = await _frozen({"catalog_view": {"mode": "off"}}, shop)
        assert "catalog" not in content, "关目录：快照里没有目录"
        assert meta["catalog_view"] == {"mode": "off", "statuses": "all"}

        content, meta = await _frozen({"catalog_view": {"statuses": "reviewed"}}, shop)
        frozen = content["catalog"]["orders"]["notes"]
        assert "grain" not in frozen and frozen["label"]["value"] == "门店订单"
        assert meta["catalog_view"] == {"mode": "live", "statuses": "reviewed"}
    finally:
        await run_manager.shutdown()


def test_of_run_reads_the_run_options():
    from types import SimpleNamespace

    from app.engine.runner import RUN_OPTIONS_KEY

    run = SimpleNamespace(extra={RUN_OPTIONS_KEY: {"catalog_view": {"mode": "off"}}})
    assert catalog_view.of_run(run) == CatalogView(mode="off")
    assert catalog_view.of_run(SimpleNamespace(extra={})) == catalog_view.LIVE


# --------------------------------------------------------------------------
# lint：消费模块只经视图读目录
# --------------------------------------------------------------------------

#: 给模型、给自查读目录的模块（四处消费 + 它们的帮手）。要读目录一律经 data/catalog_view.py
CONSUMERS = ("tools/datasource.py", "look/tables.py", "data/sqlcheck.py", "engine/nodes/dual_check.py",
             "engine/verify_agent.py", "api/copilot_context.py")
FORBIDDEN = re.compile(r"\bread_catalogs?\s*\(|\bread_entry\s*\(|\bCatalogNote\b")


@pytest.mark.parametrize("module", CONSUMERS)
def test_consumers_never_read_the_catalog_directly(module):
    text = (APP / module).read_text(encoding="utf-8")
    code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    hits = [m.group(0) for m in FORBIDDEN.finditer(code)]
    assert not hits, f"{module} 绕过了目录视图：{hits}"
