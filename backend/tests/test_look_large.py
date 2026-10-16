"""大库下的 look、db_schema、目录搜索（docs/design/next-round/03-数据接入与目录维护.md A2）。

3,000 张表的合成库（tests/fixtures/catalog/large.py）：探查范围 ord_*、sal_* 加上 5 张有数据目录的目标表，其余在范围外，
对象索引里只有名字和注释。这组守的是：

- look 的挑表在候选超过 PREFILTER_LIMIT 时只给预筛结果（BM25）、已用到的表、一跳关联：10 条脚本化需求至少 9 条挑中，
  挑选提示词不到 20,000 字；挑中范围外的表按需加入，look 记录写 added_to_scope；
- 点名范围外的表按需加入、留痕（谁、哪一轮、哪一次查看、需求原文）；每次查看最多 ON_DEMAND_PER_LOOK 张，超了照实说；
- 运行时 db_schema__：keyword 用同一个检索；不传 table 只列前 50 张；点名范围外的按需加入，每次运行最多 ON_DEMAND_PER_RUN 张；
- 查询照实列「未按目录检查的表」；
- 目录：GET /catalog/search 找得到范围外的表，POST /catalog/scope/add 加进来（记进规则的显式列出）。

挑选模型用一个按需求和目录行的字面重合打分的假模型（不调真模型）：它只能看到交给它的那份目录，预筛漏掉的它也挑不到。
"""
from __future__ import annotations

import re
import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.data import catalog
from app.db.base import SessionLocal
from app.db.models import DataSource
from app.db.models_sources import SourceObject
from app.look.base import ON_DEMAND_PER_LOOK, ON_DEMAND_PER_RUN, PICK_LIMIT
from app.look.pick import PICK_HUMAN_PROMPT, PICK_SYSTEM_PROMPT, PICK_USED_PROMPT
from app.look.service import LookService
from app.look.tables import TableKind
from app.main import app
from tests.fixtures.catalog import large
from tests.fixtures.sources import drop_source, introspect_now


class OverlapPicker:
    """假的挑选模型：按需求里的二字片段在目录行里出现的次数给每行打分，挑前 PICK_LIMIT 行。记下每次的提示词字数。"""

    def __init__(self) -> None:
        self.prompts: list[int] = []
        self.calls = 0

    async def pick(self, *, need: str, what: str, line: str, index: str, used: str = ""):
        human = PICK_HUMAN_PROMPT.format(need=need, used=PICK_USED_PROMPT.format(items=used) if used else "",
                                         what=what, line=line, index=index)
        self.prompts.append(len(PICK_SYSTEM_PROMPT) + len(human))
        self.calls += 1
        grams = {need[i:i + 2] for i in range(len(need) - 1) if re.match(r"[一-鿿]{2}", need[i:i + 2])}
        source, scored = None, []
        for row in index.splitlines():
            if m := re.match(r"数据源「(.+?)」", row):
                source = m.group(1)
                continue
            if not row.strip() or "｜" not in row and not re.match(r"^[\w.]+", row):
                continue
            name = row.split("｜", 1)[0].strip()
            score = sum(1 for g in grams if g in row)
            if score:
                scored.append((score, source, name))
        scored.sort(key=lambda x: -x[0])
        return [(s, n) for _score, s, n in scored[:PICK_LIMIT]]


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest.fixture(scope="module")
def big_db(tmp_path_factory) -> str:
    return str(large.build(tmp_path_factory.mktemp("large-look") / "large.db"))


@pytest.fixture
async def big(client, big_db):
    """探查范围 ord_*、sal_*（300 张）+ 5 张有目录的目标表（pinned）；对象索引里写上全部注释（SQLite 没有表注释）。"""
    name = f"large_{uuid.uuid4().hex[:8]}"
    r = await client.post("/api/datasources", json={"name": name, "kind": "sqlite", "database": big_db,
                                                    "readonly": True})
    sid = r.json()["id"]
    async with SessionLocal() as session:
        for t in large.TARGETS:
            if t.cataloged:
                await catalog.write_entry(session, sid, t.name, {"label": {"value": t.comment, "source": "human",
                                                                            "status": "confirmed"}},
                                          if_version=0, actor=None)
    r = await client.put(f"/api/datasources/{sid}/introspect-scope",
                         json={"scope": {"mode": "rules", "include": ["ord_*", "sal_*"]}})
    assert r.status_code == 200
    out = await introspect_now(client, sid)
    assert out["table_count"] == 305
    # 探查之后再写注释：刷新对象索引会照 SQLite 的字典（没有注释）改回去
    notes = large.comments()
    async with SessionLocal() as session:
        for obj in (await session.execute(select(SourceObject).where(SourceObject.source_id == sid))).scalars():
            obj.comment = notes.get(obj.name)
        await session.commit()
    yield sid, name
    await drop_source(sid)


async def _row(sid: str) -> DataSource:
    async with SessionLocal() as session:
        row = await session.get(DataSource, sid)
        session.expunge(row)
        return row


async def test_prefiltered_pick_hits_at_least_9_of_10_with_a_short_prompt(big):
    sid, name = big
    picker = OverlapPicker()
    kind = await TableKind.load([await _row(sid)], picker=picker, trail={"actor": "tester", "conversation_id": "c1"})
    looks = LookService([kind])
    hits = 0
    added: list[str] = []
    for target in large.TARGETS:
        result = await looks.look({"op": "look", "need": target.need})
        objects = (result.record or {}).get("objects") or []
        picked = {n for o in objects for n in o.get("picked", [])}
        if target.name in picked:
            hits += 1
        for o in objects:
            assert o.get("prefiltered", 0) <= 120 + 40
            added += o.get("added_to_scope", [])
    assert hits >= 9, f"挑中 {hits}/10"
    assert picker.prompts and max(picker.prompts) < 20_000, f"挑选提示词 {max(picker.prompts)} 字"
    out_of_scope = {t.name for t in large.TARGETS if not t.cataloged}
    assert out_of_scope & set(added), "挑中范围外的表时按需加入，记在 added_to_scope"
    rows = {r.name: r for r in (await _objects(sid)) if r.name in out_of_scope and r.in_scope}
    assert rows and all(r.added_via == "look" and r.added_by == "tester" and r.added_conversation_id == "c1"
                        and r.added_need and r.added_ref for r in rows.values())


async def _objects(sid: str) -> list[SourceObject]:
    async with SessionLocal() as session:
        return list((await session.execute(select(SourceObject).where(SourceObject.source_id == sid))).scalars())


async def test_naming_out_of_scope_tables_adds_them_with_a_per_look_cap(big):
    sid, name = big
    kind = await TableKind.load([await _row(sid)], picker=OverlapPicker(), trail={"actor": "tester"})
    looks = LookService([kind])
    wanted = ["shift_roster", "complaint_ticket", "waybill_track", "pur_hdr_east", "pur_line_east", "pur_log_east"]
    result = await looks.look({"op": "look", "need": "看这几张表的字段",
                               "targets": [{"kind": "table", "source": name, "name": n} for n in wanted]})
    (obj,) = result.record["objects"]
    assert len(obj["added_to_scope"]) == ON_DEMAND_PER_LOOK == 4
    assert set(obj["named"]) == set(obj["added_to_scope"])
    assert "按需加入探查范围" in result.text and f"一次查看最多按需加入 {ON_DEMAND_PER_LOOK} 张" in result.text
    assert sorted(obj["missing"]) == sorted(wanted[4:]), "超了上限的照实写没找到、说清原因"
    assert "biz_date" in result.text, "加进来的表当场给字段"
    row = await _row(sid)
    assert all(n in row.schema_cache["tables"] for n in obj["added_to_scope"]), "落盘，下一轮、运行时都看得到"
    # 下一次查看重新算上限
    again = await looks.look({"op": "look", "need": "再看两张",
                              "targets": [{"kind": "table", "source": name, "name": n} for n in wanted[4:]]})
    assert len(again.record["objects"][0]["added_to_scope"]) == 2


async def test_db_schema_keyword_list_limit_and_per_run_cap(big):
    from app.tools.datasource import build_datasource_tools
    from app.tools.registry import ToolContext, Versions

    sid, name = big
    async with SessionLocal() as session:
        tools = await build_datasource_tools([f"db_schema__{name}", f"db_query__{name}"],
                                             ToolContext(run_id="run-cap-1", node_id="n1",
                                                         data_versions=Versions.CURRENT), session)
    schema = next(t for t in tools if t.name.startswith("db_schema__"))
    query = next(t for t in tools if t.name.startswith("db_query__"))
    listed = await schema.coroutine()
    assert listed.count("\n  ") == 51 and "另有 2950 张没有列出（其中 2695 张在探查范围外" in listed and "keyword" in listed
    found = await schema.coroutine(keyword="员工排班")
    assert "shift_roster｜员工排班表（探查范围外：table 点名它会按需加入）" in found

    names = [f"pur_{e}_east" for e in ("hdr", "line", "log", "snap", "daily", "monthly", "cfg", "map", "hist")]
    text = await schema.coroutine(table=names)
    assert text.count("按需加入了探查范围") == ON_DEMAND_PER_RUN == 8
    assert f"这次运行最多按需加入 {ON_DEMAND_PER_RUN} 张" in text, "第 9 张照实说上限用完了"
    added = [r for r in await _objects(sid) if r.added_run_id == "run-cap-1"]
    assert len(added) == 8 and all(r.added_via == "db_schema" and r.added_ref == "n1" for r in added)
    # 查询工具看到的是同一份表结构：加进来的表不再列成「未按目录检查的表」，范围外的照实列
    import json

    out = json.loads(await query.coroutine(sql="SELECT a.id FROM pur_hdr_east a JOIN pur_tmp_east b ON a.id = b.id"))
    assert out.get("unchecked_tables") == ["pur_tmp_east"]
    # 删掉了、从来没有的分开说
    assert "没有 no_such_table" in await schema.coroutine(table="no_such_table")


async def test_catalog_search_and_add_to_scope(client, big):
    sid, name = big
    r = await client.get(f"/api/datasources/{sid}/catalog/search", params={"q": "投诉"})
    body = r.json()
    assert body["indexed"] is True and body["total"] >= 1
    hit = next(i for i in body["items"] if i["name"] == "complaint_ticket")
    assert hit["in_scope"] is False and hit["absent_reason"] == "out_of_scope" and hit["comment"] == "客户投诉工单"
    r = await client.get(f"/api/datasources/{sid}/catalog/search", params={"q": "ORD_HDR"})
    assert r.json()["items"][0]["name"].startswith("ord_hdr") and r.json()["items"][0]["in_scope"] is True

    r = await client.post(f"/api/datasources/{sid}/catalog/scope/add",
                          json={"tables": ["complaint_ticket", "nope_table", "ord_hdr_east"]})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["added"] == ["complaint_ticket"] and out["already"] == ["ord_hdr_east"]
    assert [x["name"] for x in out["refused"]] == ["nope_table"]
    row = await _row(sid)
    assert "complaint_ticket" in row.schema_cache["tables"]
    assert "complaint_ticket" in row.options["introspect_scope"]["tables"], "记进规则的显式列出，下次探查也带上"
    listed = (await client.get(f"/api/datasources/{sid}/catalog")).json()
    assert any(t["table_name"] == "complaint_ticket" for t in listed["tables"])
    scope = (await client.get(f"/api/datasources/{sid}/introspect-scope")).json()
    trail = next(t for t in scope["on_demand"] if t["name"] == "complaint_ticket")
    assert trail["via"] == "catalog" and trail["scope_reason"] == "explicit"


async def test_out_of_scope_parent_is_not_called_missing(big):
    from app.data import catalog_profile

    sid, _ = big
    row = await _row(sid)
    from app.data import catalog_profile_jobs

    async with SessionLocal() as session:
        outside = await catalog_profile_jobs.outside_of(session, row)
    tables = row.schema_cache["tables"]
    rel = {"id": "r1", "columns": ["ord_id"], "to_table": "pur_hdr_west", "to_columns": ["id"], "status": "proposed",
           "source": "name"}
    checks = catalog_profile.plan_checks("ord_hdr_east", tables["ord_hdr_east"], {"relations": [rel]},
                                         tables=tables, masked=set(), outside=outside)
    (relation,) = [c for c in checks if c.kind == "relation"]
    assert relation.skip[0] == "out_of_scope" and "探查范围外" in relation.skip[1] and "重新探查" not in relation.skip[1]
    rel["to_table"] = "really_missing"
    checks = catalog_profile.plan_checks("ord_hdr_east", tables["ord_hdr_east"], {"relations": [rel]},
                                         tables=tables, masked=set(), outside=outside)
    assert [c for c in checks if c.kind == "relation"][0].skip[0] == "missing"
