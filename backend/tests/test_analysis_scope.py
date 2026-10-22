"""报告文档里系统写的「分析范围」（engine/analysis_scope.py，00-总方案 §3.6，契约 contract.ScopeBlock）。

- 现象、拆过（按下钻链）、排除（分散、差异可能只是样本波动）、算不了、检验次数
- 没拆：候选维度确定地从数据目录算——拆解所用查询的事实表上标成属性、状态、不遮罩、取值不多的列，加上它关联的维度表；
  拆解按的列对不上目录、读不到目录、认不出事实表时只说核对不了，不乱列
- agent 写的假设不进这一段
"""
from __future__ import annotations

import sqlite3
import uuid

import pytest

from app.analysis.testing import fake_result
from app.data import catalog as data_catalog
from app.data.catalog import make_item
from app.data.catalog_view import CatalogView
from app.data.introspect import introspect
from app.db.base import SessionLocal
from app.db.models import DataSource
from app.engine import analysis_scope as scope
from app.engine.evidence import make_eid
from tests.fixtures.sources import drop_source

SQL = "SELECT o.region, o.week, SUM(o.amount) AS gmv FROM orders o JOIN stores s ON s.id = o.store_id GROUP BY 1, 2"
SNAP = "1" * 64
SNAPSHOTS = {SNAP: {"source": "shop", "sql": SQL, "columns": ["region", "week", "gmv"], "rows": []}}


def codes(*values: str, complete: bool = False) -> dict:
    item = make_item({v: "" for v in values}, "profile", "verified")
    return {**item, "complete": True} if complete else item


def column(label: str, measure: str | None = None, codes_item: dict | None = None) -> dict:
    out = {"label": make_item(label, "human")}
    if measure:
        out["measure"] = make_item(measure, "human")
    if codes_item:
        out["codes"] = codes_item
    return out


NOTES = {
    "orders": {
        "kind": make_item("fact", "human"),
        "columns": {
            "region": column("地区", "attribute", codes("华东", "华南", "华北", "西南")),
            "channel": column("渠道", "attribute", codes("线上", "线下")),
            "status": column("订单状态", "status", codes("1", "2", "9", complete=True)),
            "member_level": column("会员等级", "attribute"),                       # 没有码值：取值多不多说不清
            "customer_id": column("客户编号", "identifier", codes("c1", "c2")),
            "phone": column("手机号", "attribute", codes("1", "2")),               # 遮罩列
            "amount": column("实收金额", "flow"),
            "week": column("周", "attribute", codes(*(f"W{i}" for i in range(60)))),  # 取值太多
        },
        "relations": [{"id": "r1", "columns": ["store_id"], "to_table": "stores", "to_columns": ["id"],
                       "cardinality": "many_to_one", "source": "fk", "status": "verified"}],
    },
    "stores": {
        "kind": make_item("dimension", "human"),
        "columns": {"store_type": column("门店类型", "attribute", codes("旗舰", "社区")),
                    "city": column("城市", "attribute", codes("上海", "杭州"))},
    },
}
SOURCES = {"shop": {"tables": NOTES, "masked": ["phone"]}}


def entry(alias: str, result: dict) -> dict:
    artifact = "a" * 63 + alias[-1]
    return {"alias": alias, "kind": "analysis", "artifact": artifact, "eid": make_eid("analysis", artifact, {}),
            "locator": {}, "result": result}


def data(snapshot: str = SNAP) -> list[dict]:
    return [{"ref": "Q1", "artifact": snapshot, "role": "data", "columns": ["region", "week", "gmv"], "rows": "all"}]


def build(catalog: dict, *, sources=SOURCES, snapshots=SNAPSHOTS):
    def load(artifact):
        if artifact not in snapshots:
            raise ValueError("取不回来")
        return snapshots[artifact]

    return scope.build(catalog, sources=sources, load=load)


def test_no_conclusions_no_scope():
    assert scope.build({"m:gmv": {"kind": "metric"}}, sources=SOURCES) is None


def test_the_sections_follow_the_verdicts():
    region = fake_result(inputs=data())
    channel = fake_result(verdict="dispersed", inputs=[*data(), {"ref": "A1", "artifact": "a" * 63 + "1",
                                                                 "role": "parent"}],
                          params={"by": ["channel"], "label": "销售额"}, hypothesis="华东是不是线上拖累的")
    channel["table"]["columns"][0] = "channel"
    channel["table"]["labels"] = {"channel": "渠道"}
    catalog = {
        "A1": entry("A1", region),
        "A2": entry("A2", fake_result("baseline", None, "outside_band")),
        "A3": entry("A3", channel),
        "A4": entry("A4", fake_result("differ", "rate", "no_evidence")),
        "A5": entry("A5", fake_result("trend", None, None, status="refused", reason_code="history_short")),
        "A6": entry("A6", fake_result("differ", "rate", "differs")),
    }
    block = build(catalog)
    assert [i["alias"] for i in block["phenomena"]] == ["A2"]
    assert [(i["alias"], i["parent"], i["dims"], i["verdict"]) for i in block["decomposed"]] == [
        ("A1", None, ["地区"], "driver"), ("A3", "A1", ["渠道"], "dispersed")]
    assert [i["alias"] for i in block["excluded"]] == ["A3", "A4"]
    assert [(i["alias"], i["reason"]) for i in block["refused"]] == [("A5", "历史只有 5 期，至少要 8 期")]
    assert block["tests"] == 2 and block["corrected"] is False
    assert block["decomposed"][0]["phrase"] == region["phrase"]
    assert "华东是不是线上拖累的" not in str(block), "agent 写的假设不进分析范围"


def test_drill_chains_keep_each_parent_ahead_of_its_children():
    def child(of: str) -> dict:
        return fake_result(inputs=[*data(), {"ref": of, "artifact": "a" * 63 + of[-1], "role": "parent"}])

    catalog = {"A1": entry("A1", fake_result(inputs=data())), "A2": entry("A2", fake_result(inputs=data())),
               "A3": entry("A3", child("A1")), "A4": entry("A4", child("A3"))}
    assert [i["alias"] for i in build(catalog)["decomposed"]] == ["A1", "A3", "A4", "A2"]


def test_candidate_dimensions_come_from_the_fact_table_and_its_dimensions():
    block = build({"A1": entry("A1", fake_result(inputs=data()))})
    # 地区拆过了；会员等级没有码值、客户编号是标识、手机号遮罩、金额是流量、周取值太多：都不是候选
    assert block["not_decomposed"] == ["渠道", "订单状态", "门店类型", "城市"] and block["note"] is None
    assert block["tests"] == 0 and block["corrected"] is None


def test_a_dimension_that_does_not_match_the_catalog_is_reported_not_guessed():
    result = fake_result(inputs=data(), params={"by": ["地区名"], "label": "销售额"})
    result["table"]["columns"][0] = "地区名"
    result["table"]["labels"] = {"地区名": "地区名"}
    block = build({"A1": entry("A1", result)})
    assert block["not_decomposed"] is None and block["note"] == "候选维度核对不了：拆解按的「地区名」对不上数据目录里的列"


@pytest.mark.parametrize("kwargs, note", [
    ({"sources": None}, scope.NOTE_NO_CATALOG),
    ({"sources": {"shop": {"tables": {}, "masked": []}}}, scope.NOTE_NO_NOTES),
    ({"sources": {}}, scope.NOTE_NO_CATALOG),
    ({"snapshots": {}}, scope.NOTE_NO_QUERY),
    ({"sources": {"shop": {"tables": {"orders": {"columns": NOTES["orders"]["columns"]},
                                      "stores": NOTES["stores"]}, "masked": []}}}, scope.NOTE_NO_FACT),
])
def test_what_cannot_be_checked_is_said_plainly(kwargs, note):
    block = build({"A1": entry("A1", fake_result(inputs=data()))}, **kwargs)
    assert block["not_decomposed"] is None and block["note"] == note
    assert [i["alias"] for i in block["decomposed"]] == ["A1"], "核对不了的只是候选维度，别的照写"


def test_a_single_unlabelled_table_counts_as_the_fact_table():
    notes = {"orders": {k: v for k, v in NOTES["orders"].items() if k != "kind"}}
    sql = "SELECT region, SUM(amount) AS gmv FROM orders GROUP BY region"
    block = build({"A1": entry("A1", fake_result(inputs=data()))}, sources={"shop": {"tables": notes, "masked": []}},
                  snapshots={SNAP: {**SNAPSHOTS[SNAP], "sql": sql}})
    assert block["not_decomposed"] == ["渠道", "订单状态", "手机号"]


def test_without_a_dimension_decomposition_the_candidates_are_not_checked():
    block = build({"A1": entry("A1", fake_result("baseline", None, "within_band", inputs=data()))})
    assert block["not_decomposed"] is None and block["note"] == scope.NOTE_NO_DECOMPOSE


@pytest.fixture
async def shop(tmp_path):
    path = tmp_path / "scope_shop.db"
    db = sqlite3.connect(path)
    db.executescript("CREATE TABLE orders (id INTEGER PRIMARY KEY, region TEXT, channel TEXT, store_id INTEGER, "
                     "phone TEXT, amount REAL, week TEXT);"
                     "CREATE TABLE stores (id INTEGER PRIMARY KEY, store_type TEXT, city TEXT);")
    db.commit()
    db.close()
    async with SessionLocal() as session:
        row = DataSource(name=f"scope_{uuid.uuid4().hex[:6]}", kind="sqlite", database=str(path),
                         options={"mask_columns": "phone"})
        session.add(row)
        await session.commit()
        row.schema_cache = await introspect(row)
        await session.commit()
        await session.refresh(row)
        for table, notes in NOTES.items():
            await data_catalog.write_entry(session, row.id, table, notes, if_version=0, actor=None)
        session.expunge(row)
    yield row
    await drop_source(row.id)


async def test_the_report_reads_the_catalog_through_the_run_view(shop):
    facts = await scope.source_facts([shop.name, "nope"])
    assert set(facts) == {shop.name} and facts[shop.name]["masked"] == ["phone"]
    assert set(facts[shop.name]["tables"]) == {"orders", "stores"}
    assert await scope.source_facts([shop.name], CatalogView(mode="off")) is None

    snapshot = {**SNAPSHOTS[SNAP], "source": shop.name}
    catalog = {"Q1": {"alias": "Q1", "kind": "query", "artifact": SNAP, "source": shop.name},
               "A1": entry("A1", fake_result(inputs=data()))}
    block = await scope.scope_of(catalog, None, load=lambda a: snapshot)
    assert block["not_decomposed"] == ["渠道", "订单状态", "门店类型", "城市"]
    block = await scope.scope_of(catalog, {"mode": "off"}, load=lambda a: snapshot)
    assert block["not_decomposed"] is None and block["note"] == scope.NOTE_NO_CATALOG


def test_an_unmaintained_single_table_catalog_says_so_instead_of_a_column_mismatch():
    """V15（验收复核）：只读一张表、目录还没维护时，_fact_tables 会兜底认出那张表，于是「对不上数据目录里的列」这条
    把「目录没维护」说成了数据对不上。目录一列说明都没有时照实说目录没维护。"""
    from app.engine.analysis_scope import NOTE_NOT_MAINTAINED, _undecomposed

    result = {"op": "decompose", "mode": "additive", "inputs": [{"role": "data", "artifact": "a" * 64}],
              "params": {"by": ["channel"]}, "table": {"columns": ["channel", "base"], "labels": {"channel": "渠道"}}}
    sources = {"源": {"tables": {"orders": {}}, "masked": []}}
    snapshots = {"a" * 64: {"source": "源", "sql": "SELECT channel, SUM(amount) AS amt FROM orders GROUP BY channel",
                            "columns": ["channel", "amt"]}}
    assert _undecomposed([result], {}, sources, snapshots.get) == (None, NOTE_NOT_MAINTAINED)
