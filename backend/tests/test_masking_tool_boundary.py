"""工具边界先遮（engine/masking.py 的 mask_query_result）的收口：认不准就整份遮，多遮也不改写没设遮罩的数值证据。

- 遮罩分析本身出了意外（认遮罩列、标签列、逐格换值的哪一步都算）：整份结果的每一格都是「已遮罩」，交给模型的、落库的
  都一样，masked_at_query 记全部的列。兜底不再经出意外的那一步（以前兜底又调了一遍逐格换值的那一步，它出意外时兜底跟着
  出意外，结果没交出去，也没有测试证明「整份遮」这句保证）
- 按值遮只换文字格：没设遮罩的数值列（访问次数正好等于某一笔被遮的金额）原样，存下的证据和数据库里的一样，双盲复算照常
  取得到；文字格里整格就是遮罩值、夹着遮罩值的照换，换过的格子记进快照（masked_cells），格子引用指到它们照实说是遮罩。
  占位的短词（未知、暂无……）不当遮罩值，没设遮罩的列、备注里的同一个词照原样

模型一律是 MockChatModel 剧本，数据源是现造的 SQLite 文件，手机号、姓名、金额、访问次数都是虚构的。
"""
from __future__ import annotations

import json

import pytest
from httpx import ASGITransport, AsyncClient

from app.core import artifact_store
from app.data.engine import engines
from app.engine import masking
from app.main import app
from tests.test_verify_agent import Script, q
from tests.test_verify_agent_masking import (  # noqa: F401 - clean_spend 是自动用上的夹具
    MASKED,
    SALES,
    VALUE_SCHEMA,
    _cells,
    _clean,
    _source,
    ask,
    clean_spend,
    masked_shop,
)


# --------------------------------------------------------------------------
# 分析出意外：整份遮
# --------------------------------------------------------------------------


def _broken(*args, **kwargs):
    raise RuntimeError("遮罩分析里的意外")


PAYLOAD = {"sql": "SELECT id, buyer_phone, note FROM sales", "columns": ["id", "buyer_phone", "note"],
           "rows": [[1, "13900000001", "客户来电"], [2, "13900000002", "例行回访"]],
           "column_types": {"id": "number", "buyer_phone": "text", "note": "text"}}


@pytest.mark.parametrize("step", ["_derived_hidden", "_snapshot_labels", "_masked_view"])
def test_a_failing_analysis_masks_every_cell(monkeypatch, step):
    """认遮罩列、认标签列、逐格换值，哪一步出了意外都整份遮：每一格都是「已遮罩」、类型记成文本，整列遮掉的是全部的列。"""
    monkeypatch.setattr(masking, step, _broken)
    done = masking.mask_query_result(json.loads(json.dumps(PAYLOAD)), ["buyer_phone"], "sqlite")
    assert done.payload["rows"] == [[MASKED] * 3, [MASKED] * 3]
    assert done.payload["column_types"] == {"id": "text", "buyer_phone": "text", "note": "text"}
    assert done.columns == ["id", "buyer_phone", "note"]
    assert done.payload["sql"] == PAYLOAD["sql"], "SQL 是模型自己写的，照原样"
    assert not done.secrets.strong, "认不准，不收值"


def test_a_failing_analysis_masks_rows_of_any_shape(monkeypatch):
    """行是字典的结果同样整份遮。"""
    monkeypatch.setattr(masking, "_masked_view", _broken)
    payload = {"sql": "SELECT id, buyer_phone FROM sales", "columns": ["id", "buyer_phone"],
               "rows": [{"id": 1, "buyer_phone": "13900000001"}]}
    done = masking.mask_query_result(payload, ["buyer_phone"], "sqlite")
    assert done.payload["rows"] == [{"id": MASKED, "buyer_phone": MASKED}]


@pytest.mark.parametrize("step", ["_derived_hidden", "_masked_view"])
async def test_the_model_and_the_snapshot_see_only_masks_when_the_analysis_fails(monkeypatch, masked_shop, step):
    """端到端：分析出了意外，交给模型的工具结果、落库的快照、通用接口给的都只有「已遮罩」，masked_at_query 是全部的列；
    格子引用照实说是遮罩列，不采信。"""
    monkeypatch.setattr(masking, step, _broken)
    name = masked_shop
    sql = "SELECT id, region, buyer_phone AS 手机号, amount FROM sales ORDER BY id"
    script = Script(monkeypatch, [q(name, sql, "c1")], {"value": {"alias": "Q1", "row": 0, "column": "amount"},
                                                        "rationale": "x"})
    out, _ = await ask(name)
    seen = json.loads(script.tool_results()["c1"].split("\n", 1)[1])
    assert seen["rows"] == [[MASKED] * 4] * 4, seen
    snapshot = artifact_store.load(out.queries[0]["artifact"])
    assert snapshot["rows"] == [[MASKED] * 4] * 4
    assert snapshot["masked_at_query"] == ["id", "region", "手机号", "amount"]
    assert _clean(json.dumps(snapshot, ensure_ascii=False))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        got = await client.get(f"/api/artifacts/{out.queries[0]['artifact']}")
    assert got.status_code == 200 and _clean(got.text) and "1200" not in got.text
    assert out.result["value"] is None
    assert out.cells[0]["reason"] == "Q1 的「amount」取自设置了遮罩的列，查询时已遮罩，不采信"


# --------------------------------------------------------------------------
# 多遮不改写没设遮罩的数值证据；占位的短词不当遮罩值
# --------------------------------------------------------------------------


#: 金额设了遮罩，都是五位数；访问次数没设遮罩，第二、三单正好等于别的订单的金额（20000、30000）。联系表的访问次数存成
#: 文字（'20000'），客户名设了遮罩、里面有占位的「未知」，区域和备注里也写着「未知」
BIG = ("CREATE TABLE sales (id INTEGER PRIMARY KEY, region TEXT, amount INTEGER, visits INTEGER, buyer_name TEXT,"
       " note TEXT);"
       "INSERT INTO sales VALUES (1, '华东', 10000, 25000, '张伟', '原因未知'), (2, '华东', 12000, 20000, '未知', ''),"
       " (3, '华南', 20000, 30000, '李娜', '客户未知来源'), (4, '未知', 30000, 22000, '王芳', ''),"
       " (5, '华北', 50000, 9000, '刘洋', '');"
       "CREATE TABLE contacts (id INTEGER PRIMARY KEY, visits_text TEXT, remark TEXT);"
       "INSERT INTO contacts VALUES (1, '20000', '上月消费 30000 元'), (2, '8', '例行回访');")


@pytest.fixture
async def big_shop(tmp_path):
    name, source_id = await _source(tmp_path, {"mask_columns": "amount, buyer_name"}, script=BIG)
    yield name
    await engines.invalidate(source_id)


async def test_an_unmasked_numeric_column_is_kept_even_when_it_equals_a_masked_value(monkeypatch, big_shop):
    """访问次数没设遮罩，第二、三单正好等于别的订单被遮的金额：交给模型的、落库的都是数据库里的原数，类型照旧是数，
    引用它照常取值（双盲复算取得到 20000）。以前按值整格换成「已遮罩」，快照里不留记录，引用时说成「不是数值」。"""
    name = big_shop
    sql = "SELECT id, amount, visits FROM sales ORDER BY id"
    script = Script(monkeypatch, [q(name, sql, "c1")], {"value": {"alias": "Q1", "row": 1, "column": "visits"},
                                                        "rationale": "x"})
    out, _ = await ask(name)
    seen = json.loads(script.tool_results()["c1"].split("\n", 1)[1])
    assert seen["rows"] == [[1, MASKED, 25000], [2, MASKED, 20000], [3, MASKED, 30000], [4, MASKED, 22000],
                            [5, MASKED, 9000]]
    snapshot = artifact_store.load(out.queries[0]["artifact"])
    assert snapshot["rows"] == seen["rows"] and snapshot["column_types"]["visits"] == "number"
    assert snapshot["masked_at_query"] == ["amount"] and "masked_cells" not in snapshot
    assert out.result["value"] == 20000 and out.cells[0]["status"] == "resolved"


async def test_text_cells_masked_by_value_are_recorded_and_cited_as_masked(monkeypatch, big_shop):
    """存成文字的数（'20000'）、备注里夹着的金额（30000）照样按值换：交给模型的、落库的都换掉，换过的格子记进快照
    （masked_cells），引用到它们照实说是遮罩，不说成「不是数值」。"""
    name = big_shop
    steps = [q(name, "SELECT amount FROM sales ORDER BY id", "c1"),
             q(name, "SELECT id, visits_text, remark FROM contacts ORDER BY id", "c2")]
    script = Script(monkeypatch, steps, lambda messages: {"value": {"alias": "Q2", "row": 0, "column": "visits_text"},
                                                          "rationale": "x"})
    out, _ = await ask(name)
    seen = json.loads(script.tool_results()["c2"].split("\n", 1)[1])
    assert seen["rows"] == [[1, MASKED, f"上月消费 {MASKED} 元"], [2, "8", "例行回访"]]
    assert "masked_cells" not in seen, "记录只在快照里，不交给模型"
    snapshot = artifact_store.load(out.queries[1]["artifact"])
    assert snapshot["rows"] == seen["rows"] and snapshot["masked_cells"] == [[0, 1], [0, 2]]
    assert snapshot["masked_at_query"] == []
    assert out.result["value"] is None
    assert out.cells[0]["reason"] == "Q2 第 0 行「visits_text」与设置了遮罩的值相同，查询时已遮罩，不采信"


async def test_a_partly_masked_text_cell_is_cited_as_masked(monkeypatch, big_shop):
    """引用指到文字里换掉了一截的格子：同样照实说是遮罩。"""
    name = big_shop
    steps = [q(name, "SELECT amount FROM sales ORDER BY id", "c1"),
             q(name, "SELECT id, remark FROM contacts ORDER BY id", "c2")]
    Script(monkeypatch, steps, {"value": {"alias": "Q2", "row": 0, "column": "remark"}, "rationale": "x"})
    out, _ = await ask(name)
    assert out.result["value"] is None
    assert out.cells[0]["reason"] == "Q2 第 0 行「remark」里有与设置了遮罩的值相同的部分，查询时已遮罩，不采信"


async def test_placeholder_words_are_not_masked_elsewhere(monkeypatch, big_shop):
    """客户名设了遮罩，里面有占位的「未知」：客户名整列照旧遮；没设遮罩的区域里的「未知」、备注里的「原因未知」「客户
    未知来源」照原样。以前「未知」当遮罩值收了，这几格都被换掉。"""
    name = big_shop
    sql = "SELECT id, buyer_name, region, note FROM sales ORDER BY id"
    script = Script(monkeypatch, [q(name, sql, "c1")], {"value": {"alias": "Q1", "row": 0, "column": "id"},
                                                        "rationale": "x"})
    out, _ = await ask(name)
    seen = json.loads(script.tool_results()["c1"].split("\n", 1)[1])
    assert [row[1] for row in seen["rows"]] == [MASKED] * 5
    assert [row[2:] for row in seen["rows"]] == [["华东", "原因未知"], ["华东", ""], ["华南", "客户未知来源"],
                                                 ["未知", ""], ["华北", ""]]
    assert out.masked_values.hit("张伟") and not out.masked_values.hit("未知")


def test_placeholders_are_never_collected():
    secrets = masking._Secrets()
    secrets.add({"columns": ["name"], "rows": [["未知"], ["N/A"], ["暂无"], ["张伟"], ["NULL"]]}, {"name"})
    assert secrets.strong == {"张伟"} and secrets.weak == {"张伟"}
    assert secrets.scrub("原因未知，暂无记录，N/A，张伟") == f"原因未知，暂无记录，N/A，{MASKED}"
