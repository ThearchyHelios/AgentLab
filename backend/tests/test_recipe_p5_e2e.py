"""Excel 期 5 的端到端（真管线，经接口）：每月一份的 CSV 明细按期累积成一张表，重叠的那期被拒，同一期再传要确认
替换；带穿插小计的 Excel 规则起草成功、小计核对通过、小计行不进数据。

夹具全部合成（订单号 A001…、门店一…），不调用任何模型（规则起草不完整时的 AI 可用性查询换成「不可用」的桩）。
"""
from __future__ import annotations

import io
import json
import uuid
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from openpyxl import Workbook

from app.core.config import settings
from app.main import app

API = "/api/datasources"
CSV_MIME = "text/csv"


@pytest.fixture(autouse=True)
def store(monkeypatch, tmp_path):
    data = tmp_path / "data"
    (data / "uploads").mkdir(parents=True)
    monkeypatch.setattr(settings, "data_dir", data)
    yield data


@pytest.fixture(autouse=True)
def no_model(monkeypatch):
    from app.data import recipe_ai
    from app.data.recipe_types import AiAvailability

    async def unavailable(session):
        return None, "", AiAvailability(False, reason="验收测试不接模型")

    monkeypatch.setattr(recipe_ai, "resolve_draft_model", unavailable)


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


def _name() -> str:
    return f"p5_{uuid.uuid4().hex[:10]}"


def month_csv(month: int, orders: list[tuple[str, int, int]]) -> tuple[bytes, str]:
    """一个月的订单导出：订单号、下单日期、门店、金额。文件名带统计期（只作旁证，CSV 里没有统计期格）。"""
    lines = ["订单号,下单日期,门店,金额"]
    for oid, day, amount in orders:
        lines.append(f"{oid},2026-{month:02d}-{day:02d},门店{'一二'[day % 2]},{amount}")
    last = 31 if month in (8, 10) else 30
    return ("\n".join(lines) + "\n").encode("utf-8"), f"orders_2026-{month:02d}-01_2026-{month:02d}-{last}.csv"


AUG = [("A001", 1, 120), ("A002", 15, 80), ("A003", 31, 40)]
SEP = [("B001", 3, 70), ("B002", 30, 30)]


async def stage(client, raw: bytes, filename: str, name: str) -> dict[str, Any]:
    resp = await client.post(f"{API}/imports/stage", files={"file": (filename, raw, CSV_MIME)}, data={"name": name})
    assert resp.status_code == 201, resp.text
    return resp.json()


async def answer(client, sid: str, answers: dict[str, Any]) -> dict[str, Any]:
    resp = await client.post(f"{API}/imports/{sid}/answers", json={"answers": answers})
    assert resp.status_code == 200, resp.text
    return resp.json()


async def trial(client, sid: str, **body: Any) -> dict[str, Any]:
    resp = await client.post(f"{API}/imports/{sid}/trial", json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _period(month: int) -> dict[str, Any]:
    last = 31 if month in (8, 10) else 30
    return {"统计期": {"start": f"2026-{month:02d}-01", "end": f"2026-{month:02d}-{last}"}}


async def commit(client, st: dict[str, Any], confirmations: list[str] | None = None):
    ids = [i["id"] for i in st["trial"]["confirm_items"]] if confirmations is None else confirmations
    return await client.post(f"{API}/imports/{st['id']}/commit",
                             json={"trial_id": st["trial"]["trial_id"], "confirmations": ids})


async def rows(client, name: str, sql: str) -> list[list[Any]]:
    resp = await client.post(f"/api/tools/db_query__{name}/run", json={"args": {"sql": sql}})
    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert out["ok"], out
    return json.loads(out["result"])["rows"]


async def first_csv(client, name: str) -> dict[str, Any]:
    raw, fn = month_csv(8, AUG)
    st = await stage(client, raw, fn, name)
    assert st["recipe"]["sheets"][0]["match"]["name"] == "CSV"
    assert not st["recipe_problems"], st["recipe_problems"]
    q = next(q for q in st["questions"] if q["id"].startswith("q_list_period:"))
    # 列表的草稿还不能按期累积，没有 q_mode；「每期怎么更新」这一道题同时定下导入模式和每一行属于哪一期
    assert not any(x["id"] == "q_mode" for x in st["questions"])
    st = await answer(client, st["id"], {q["id"]: {"value": "synthesize"}})
    assert st["recipe"]["mode"] == "accumulate" and not st["recipe_problems"], st["recipe_problems"]
    st = await trial(client, st["id"])
    assert st["trial"]["status"] == "needs_input", st["trial"]["problems"]
    st = await trial(client, st["id"], context_inputs=_period(8))
    assert st["trial"]["status"] == "passed", st["trial"]["problems"]
    ids = [i["id"] for i in st["trial"]["confirm_items"]]
    assert "list_period:列表1" in ids
    resp = await commit(client, st)
    assert resp.status_code == 201, resp.text
    return {**resp.json(), "table": st["recipe"]["sheets"][0]["blocks"][0]["table"]}


async def reupload(client, source_id: str, month: int, orders: list[tuple[str, int, int]],
                   period: dict[str, Any] | None = None) -> dict[str, Any]:
    raw, fn = month_csv(month, orders)
    resp = await client.post(f"{API}/{source_id}/reupload", files={"file": (fn, raw, CSV_MIME)})
    assert resp.status_code == 201, resp.text
    st = resp.json()
    assert st["trial"]["status"] == "needs_input", st["trial"]["problems"]
    return await trial(client, st["id"], context_inputs=period or _period(month))


async def test_monthly_csv_exports_accumulate_into_one_table(client):
    name = _name()
    first = await first_csv(client, name)
    sid, table = first["source"]["id"], first["table"]
    assert await rows(client, name, f'SELECT "订单号", "统计期起", "统计期止" FROM "{table}" ORDER BY rowid') == [
        ["A001", "2026-08-01", "2026-08-31"], ["A002", "2026-08-01", "2026-08-31"], ["A003", "2026-08-01", "2026-08-31"]]
    st = await reupload(client, sid, 9, SEP)
    assert st["trial"]["status"] == "passed", st["trial"]["problems"]
    assert st["trial"]["accumulate"]["action"] == "append"
    resp = await commit(client, st)
    assert resp.status_code == 201, resp.text
    got = await rows(client, name, f'SELECT "统计期起", COUNT(*) FROM "{table}" GROUP BY 1 ORDER BY 1')
    assert got == [["2026-08-01", 3], ["2026-09-01", 2]]
    # 部分重叠的一期被拒收
    st = await reupload(client, sid, 9, SEP, period={"统计期": {"start": "2026-08-15", "end": "2026-09-14"}})
    assert st["trial"]["status"] == "rejected" and any(p["code"] == "period_overlap" for p in st["trial"]["problems"])
    # 同一期再传（数不同）：要确认替换
    st = await reupload(client, sid, 9, [("B001", 3, 75)])
    assert st["trial"]["status"] == "passed" and st["trial"]["accumulate"]["action"] == "replace_period"
    ids = [i["id"] for i in st["trial"]["confirm_items"]]
    replace = "period_replace:2026-09-01~2026-09-30"
    assert replace in ids
    resp = await commit(client, st, [i for i in ids if i != replace])
    assert resp.status_code == 422
    resp = await commit(client, st)
    assert resp.status_code == 201, resp.text
    assert await rows(client, name, f'SELECT COUNT(*), SUM("金额") FROM "{table}"') == [[4, 315]]


async def test_excel_with_subtotals_drafts_checks_and_keeps_them_out(client):
    wb = Workbook()
    ws = wb.active
    ws.title = "门店"
    for row in [("门店", "金额"), ("门店一", 100), ("门店二", 200), ("小计", 300), ("门店三", 150), ("门店四", 150),
                ("小计", 300), ("合计", 600)]:
        ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    name = _name()
    resp = await client.post(f"{API}/imports/stage", files={"file": ("门店.xlsx", buf.getvalue(), "application/octet-stream")},
                             data={"name": name})
    assert resp.status_code == 201, resp.text
    st = resp.json()
    assert not st["draft_problems"] and not st["recipe_problems"], (st["draft_problems"], st["recipe_problems"])
    block = st["recipe"]["sheets"][0]["blocks"][0]
    assert block["rows"]["subtotals"]["pick"] == "小计"
    st = await trial(client, st["id"])
    assert st["trial"]["status"] == "passed", st["trial"]["problems"]
    checks = {c["id"]: c for c in st["trial"]["checks"]}
    assert checks["T2"]["status"] == "passed" and "小计" in checks["T2"]["title"]
    excluded = st["trial"]["receipt"]["rows_excluded"]
    assert any(x["reason"] == "subtotal" and x["rows"] == [[4, 4], [7, 7]] for x in excluded), excluded
    ids = [i["id"] for i in st["trial"]["confirm_items"]]
    assert "subtotals:列表1" in ids
    resp = await commit(client, st)
    assert resp.status_code == 201, resp.text
    table = st["recipe"]["sheets"][0]["blocks"][0]["table"]
    assert await rows(client, name, f'SELECT COUNT(*), SUM("金额") FROM "{table}"') == [[4, 600]]
