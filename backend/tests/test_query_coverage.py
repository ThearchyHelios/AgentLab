"""查询区间里这张表没有数据时，查询结果里照实说（data/coverage.py，接在 tools/datasource 的查询工具上）。

部署上撞过：一张到访事件表只存最新一天（每天整表重灌），周报按运行当天现算的区间取上周、上上周的到访人数，6 列全是 0，
报告照写「上周到访人数为 0 人」。剖析记过日期范围，但对滚动表不可靠，所以运行时现查：

- 结果里有整列是 0 或空的数值列时，把 SQL 收窄到这一列，认出它用到的表和时间谓词；界是字面量的直接解析，按运行当天现算
  的在同一个库里算出来；这一列在表里的范围现查 MIN / MAX，查不成退回剖析记下的范围（照实标「按 X 日的剖析」）；
- 区间和范围完全不重叠才算没有数据：结果和快照里多一项 no_data（给人和模型看的 message），运行事件里一条 query_no_data；
  没查出问题不加这个键，快照和以前一字不差；
- 有数的列、一行都没有的结果、遮罩列、写法对不上（文本列 '20261012' 对文本界 '2026-10-12'）、认不出的一律不判。

库是现造的 SQLite 文件，表名、列名、数字都是虚构的。
"""
from __future__ import annotations

import json
import sqlite3
import uuid

import pytest

from app.core import artifact_store
from app.data import coverage, introspect
from app.data.engine import engines
from app.db.base import SessionLocal
from app.db.models import DataSource
from app.tools.datasource import build_datasource_tools
from app.tools.registry import ToolContext, Versions

#: 这张表只存一天（2020-01-01），按今天现算的上周一定落在它外面
ONLY_DAY = "20200101"
#: 按今天现算的「上周」（SQLite 的写法，和列一样写成 'YYYYMMDD'）
WINDOW = "{c} >= strftime('%Y%m%d', 'now', '-14 days') AND {c} < strftime('%Y%m%d', 'now', '-7 days')"
KPI_SQL = ("SELECT (SELECT COUNT(DISTINCT v.member_id) FROM gate_events v WHERE " + WINDOW.format(c="v.event_date")
           + ") AS 上周到访人数, (SELECT COUNT(*) FROM gate_events v WHERE " + WINDOW.format(c="v.event_date")
           + ") AS 上周到访人次, (SELECT SUM(pv) FROM daily_pv WHERE day >= date('now', '-14 days')) AS 客流PV")


@pytest.fixture
async def plant(tmp_path):
    """gate_events 只有 ONLY_DAY 一天；daily_pv 覆盖最近 30 天、每天 100。"""
    path = tmp_path / f"{uuid.uuid4().hex[:8]}.db"
    db = sqlite3.connect(path)
    db.executescript(
        "CREATE TABLE gate_events (event_date TEXT, member_id INTEGER, gate TEXT);"
        f"INSERT INTO gate_events VALUES ('{ONLY_DAY}', 1, 'A'), ('{ONLY_DAY}', 2, 'B');"
        "CREATE TABLE daily_pv (day TEXT, pv INTEGER);"
        "CREATE TABLE iso_events (paid_on TEXT, amount INTEGER);"
        "INSERT INTO iso_events VALUES ('2020-01-01', 5);")
    for back in range(30):
        db.execute("INSERT INTO daily_pv VALUES (date('now', ?), 100)", (f"-{back} days",))
    db.commit()
    db.close()
    name = f"plant_{uuid.uuid4().hex[:8]}"
    async with SessionLocal() as session:
        row = DataSource(name=name, kind="sqlite", database=str(path), readonly=True, enabled=True)
        session.add(row)
        await session.flush()
        row.schema_cache = await introspect.introspect(row)
        await session.commit()
        source_id = row.id
    yield name, source_id
    await engines.invalidate(source_id)


async def _query(name: str, sql: str, events: list | None = None) -> dict:
    async def emit(event_type, **data):
        if events is not None:
            events.append((str(event_type), data))

    async with SessionLocal() as session:
        built = await build_datasource_tools([f"db_query__{name}"], ToolContext(
            data_versions=Versions.CURRENT, run_id="run-coverage", node_id="q_kpi", emit=emit), session)
    got = await built[0].ainvoke({"sql": sql})
    return json.loads(got)


async def test_zero_columns_over_a_window_without_data_are_flagged(plant):
    name, _ = plant
    events: list = []
    result = await _query(name, KPI_SQL, events)
    assert result["rows"][0][:2] == [0, 0]
    [gap] = result["no_data"]
    assert gap["columns"] == ["上周到访人数", "上周到访人次"] and gap["table"] == "gate_events"
    assert gap["column"] == "event_date" and gap["data_range"] == ["2020-01-01", "2020-01-01"] and gap["basis"] == "live"
    start, end = gap["window"]
    assert start < end and start > "2020-01-01"
    assert gap["message"].startswith("「上周到访人数」等 2 列查的是 ") and "只有 2020-01-01 这一天的数据" in gap["message"]
    assert "这些 0 不代表这段时间真的是 0，是这段时间表里没有数据" in gap["message"]
    # 有数的列（客流 PV）不受影响
    assert all("客流PV" not in g["columns"] for g in result["no_data"])
    # 快照里也有；运行事件里有一条 query_no_data
    snapshot = artifact_store.load(result["artifact"])
    assert snapshot["no_data"] == result["no_data"]
    [(etype, data)] = [e for e in events if e[1].get("code") == "query_no_data"]
    assert data["level"] == "warn" and data["no_data"] == result["no_data"]


async def test_literal_windows_are_parsed_directly(plant):
    name, _ = plant
    sql = ("SELECT COUNT(*) AS 到访人次 FROM gate_events WHERE event_date >= '20261012' AND event_date < '20261019'")
    [gap] = (await _query(name, sql))["no_data"]
    assert gap["window"] == ["2026-10-12", "2026-10-18"]          # < 不带时刻：截到前一天


async def test_nothing_is_added_when_the_window_has_data_or_the_column_is_not_zero(plant):
    name, _ = plant
    for sql in (
        f"SELECT COUNT(*) AS n FROM gate_events WHERE event_date = '{ONLY_DAY}'",            # 有数
        "SELECT SUM(pv) AS pv FROM daily_pv WHERE day >= date('now', '-7 days')",              # 有数
        "SELECT COUNT(*) AS n FROM gate_events WHERE event_date >= '20200101'",                  # 区间覆盖了那一天
        "SELECT gate, COUNT(*) AS n FROM gate_events WHERE event_date >= '20261012' GROUP BY gate",   # 一行都没有
    ):
        result = await _query(name, sql)
        assert "no_data" not in result, (sql, result.get("no_data"))
        assert "no_data" not in artifact_store.load(result["artifact"]), "没查出问题：快照和以前一字不差"


async def test_a_text_column_against_a_differently_written_text_bound_is_not_judged(plant):
    """列里存的是 '20200101'，界写的是 '2026-10-12'：库按字符串比，和按日期比对不上，不判。"""
    name, _ = plant
    result = await _query(name, "SELECT COUNT(*) AS n FROM gate_events WHERE event_date >= '2026-10-12'")
    assert "no_data" not in result
    # ISO 写法对 ISO 写法：照常判
    result = await _query(name, "SELECT COUNT(*) AS n FROM iso_events WHERE paid_on >= date('now', '-7 days')")
    [gap] = result["no_data"]
    assert gap["column"] == "paid_on" and gap["data_range"] == ["2020-01-01", "2020-01-01"]


async def test_masked_columns_are_not_probed(plant):
    name, source_id = plant
    async with SessionLocal() as session:
        row = await session.get(DataSource, source_id)
        row.options = {**(row.options or {}), "mask_columns": ["event_date"]}
        await session.commit()
    result = await _query(name, "SELECT COUNT(*) AS n FROM gate_events WHERE event_date >= '20261012'")
    assert "no_data" not in result


async def test_a_failed_probe_falls_back_to_the_profile(plant, monkeypatch):
    """现查不成（超时、出错）：退回剖析记下的范围，照实标「按 X 日的剖析」。"""
    name, _ = plant
    real = coverage._probe

    async def probe(source, sql):
        return None if "MIN(" in sql else await real(source, sql)

    async def facts(table):
        assert table == "gate_events"
        return {"at": "2026-10-01T03:00:00+00:00", "date_ranges": [{"column": "event_date", "min": "20260930",
                                                                     "max": "20260930"}]}

    monkeypatch.setattr(coverage, "_probe", probe)
    async with SessionLocal() as session:
        source = await session.get(DataSource, (await _source_id(name)))
    payload = {"columns": ["n"], "rows": [[0]], "row_count": 1, "column_types": {"n": "number"}}
    [gap] = await coverage.result_gaps(source, "SELECT COUNT(*) AS n FROM gate_events WHERE event_date >= '20261012'",
                                       payload, facts=facts)
    assert gap["basis"] == "profile" and gap["basis_at"] == "2026-10-01"
    assert gap["data_range"] == ["2026-09-30", "2026-09-30"] and "（现查表的范围没有成功，按 2026-10-01 的剖析）" in gap["message"]
    # 剖析也没有：不判
    assert await coverage.result_gaps(source, "SELECT COUNT(*) AS n FROM gate_events WHERE event_date >= '20261012'",
                                      payload, facts=None) == []


async def _source_id(name: str) -> str:
    from sqlalchemy import select

    async with SessionLocal() as session:
        return (await session.execute(select(DataSource.id).where(DataSource.name == name))).scalar_one()


@pytest.mark.parametrize("value, kind, day, shape", [
    ("2026-10-12", None, "2026-10-12", "iso"),
    ("2026-10-12T08:30:00", "datetime", "2026-10-12", "date"),
    ("20261012", None, "2026-10-12", "compact"),
    (20261012, None, "2026-10-12", "number"),
    ("2026-13-01", None, None, None),
    ("2026/10/12", None, None, None),
    ("上周", None, None, None),
])
def test_dates_are_normalised(value, kind, day, shape):
    got = coverage._as_day(value, kind)
    assert (got.day.isoformat() if got else None) == day and (got.shape if got else None) == shape


def test_zero_columns():
    payload = {"columns": ["a", "b", "c", "d"], "column_types": {"a": "number", "b": "number", "d": "text"},
               "rows": [[0, 1, None, "0"], ["0.00", 0, None, "0"]]}
    assert coverage.zero_columns(payload) == ["a", "c"]
    assert coverage.zero_columns({**payload, "rows": []}) == []


async def test_the_weekly_report_shape_on_mysql(monkeypatch):
    """部署上那张周报的形状（MySQL）：一条查询用标量子查询一次取几列，上周、上上周按运行当天现算；到访表只有最新一天，
    列存成 VARCHAR 'YYYYMMDD'。界由库算出来是 DATE 类型：MySQL 把文本列转成日期再比，照常判。两段区间各记一条。"""
    from types import SimpleNamespace

    last = ("event_date >= DATE_SUB(CURRENT_DATE, INTERVAL (WEEKDAY(CURRENT_DATE) + 7) DAY) "
            "AND event_date < DATE_SUB(CURRENT_DATE, INTERVAL WEEKDAY(CURRENT_DATE) DAY)")
    before = ("event_date >= DATE_SUB(CURRENT_DATE, INTERVAL (WEEKDAY(CURRENT_DATE) + 14) DAY) "
              "AND event_date < DATE_SUB(CURRENT_DATE, INTERVAL (WEEKDAY(CURRENT_DATE) + 7) DAY)")
    sql = (f"SELECT (SELECT SUM(pv) FROM pv_daily WHERE stat_day >= '2026-10-01') AS `客流PV`, "
           f"(SELECT COUNT(DISTINCT member_id) FROM gate_events WHERE {last}) AS `上周到访人数`, "
           f"(SELECT COUNT(*) FROM gate_events WHERE {last}) AS `上周到访人次`, "
           f"(SELECT COUNT(DISTINCT member_id) FROM gate_events WHERE {before}) AS `上上周到访人数`")
    days = {"(WEEKDAY(CURRENT_DATE) + 14)": "2026-09-28", "(WEEKDAY(CURRENT_DATE) + 7)": "2026-10-05",
            "(WEEKDAY(CURRENT_DATE)) DAY": "2026-10-12"}     # sqlglot 写回 MySQL 时给这一项加了括号
    seen: list[str] = []

    async def probe(source, text):
        seen.append(text)
        if "MIN(" in text:
            return ["20261019", "20261019"], ["text", "text"]
        values = []
        for part in text[len("SELECT "):].split(" AS b")[:-1]:
            values.append(next(v for k, v in days.items() if k in part))
        return values, ["date"] * len(values)

    monkeypatch.setattr(coverage, "_probe", probe)
    source = SimpleNamespace(name="plant", kind="mysql", schema_cache=None, id=None)
    payload = {"columns": ["客流PV", "上周到访人数", "上周到访人次", "上上周到访人数"],
               "column_types": {"客流PV": "number", "上周到访人数": "number", "上周到访人次": "number",
                                "上上周到访人数": "number"},
               "rows": [[3200, 0, 0, 0]], "row_count": 1}
    gaps = await coverage.result_gaps(source, sql, payload)
    by_window = {tuple(g["window"]): g for g in gaps}
    assert set(by_window) == {("2026-10-05", "2026-10-11"), ("2026-09-28", "2026-10-04")}
    assert by_window[("2026-10-05", "2026-10-11")]["columns"] == ["上周到访人数", "上周到访人次"]
    assert by_window[("2026-09-28", "2026-10-04")]["columns"] == ["上上周到访人数"]
    assert all(g["data_range"] == ["2026-10-19", "2026-10-19"] for g in gaps)
    # 表的范围同一张表、同一列只查一次；按运行当天现算的界一条 SELECT 算出来
    assert sum("MIN(" in t for t in seen) == 1
    assert all(t.startswith("SELECT ") for t in seen)


async def test_a_live_range_is_kept_for_a_while(monkeypatch):
    """现查到的范围在进程里记几分钟：一张图里几个取数节点、助手自查几轮查同一张表，不再每次全表 MIN / MAX。"""
    from types import SimpleNamespace

    seen: list[str] = []

    async def probe(source, text):
        seen.append(text)
        return ["20200101", "20200101"], ["text", "text"]

    monkeypatch.setattr(coverage, "_probe", probe)
    monkeypatch.setattr(coverage, "_LIVE", {})
    source = SimpleNamespace(name=f"plant_{uuid.uuid4().hex[:6]}", kind="sqlite", schema_cache=None, id="s1")
    payload = {"columns": ["n"], "rows": [[0]], "row_count": 1, "column_types": {"n": "number"}}
    sql = "SELECT COUNT(*) AS n FROM gate_events WHERE event_date >= '20261012'"
    for _ in range(2):
        assert len(await coverage.result_gaps(source, sql, payload)) == 1
    assert sum("MIN(" in t for t in seen) == 1
    monkeypatch.setattr(coverage, "LIVE_TTL_S", 0.0)
    coverage._LIVE.clear()
    await coverage.result_gaps(source, sql, payload)
    await coverage.result_gaps(source, sql, payload)
    assert sum("MIN(" in t for t in seen) == 3, "过期了就重新现查"
