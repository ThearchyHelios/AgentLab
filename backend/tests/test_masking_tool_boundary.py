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
