"""缺失断言（absence_claim）：写作目录有省略时，「没有查到」「不存在」「缺失」「没有数据」这类结论依据不足。

2026-10 一次运行，写作者只看到上百次查询里的前二十来次，写出「某类数据缺失：本轮没有查到任何结果」这样的断言，
可那个 Agent 实际查到了上百条记录。

- 写作规则：清单有省略、或有来源的结构化结论为空时，不得断言没有数据，只能写「本报告未覆盖」并说明依据不足
- 组装文档：清单有省略时按一组明确的说法认出缺失断言，记 absence_claim，交回重写一次；重写后仍有的只标注、不判失败
  （report.REWRITE_ONLY，和句中的 [[see:]] 同一种处理）
- 只认拿得准的说法：「不存在明显差异」「本报告未覆盖」「不代表没有数据」都不算
"""
from __future__ import annotations

import pytest

from app.engine.evidence import ABSENCE_RULE, build_catalog, compose_doc
from app.engine.nodes.report import REWRITE_ONLY


@pytest.fixture
def catalog():
    card = {"kind": "metric_set", "caliber": "周报口径", "caliber_version": "v1",
            "metrics": [{"id": "gmv", "name": "销售额", "value": 45678.5, "unit": "元", "format": "thousands"}]}
    return build_catalog(nodes={"card": card}, ledger=[{"kind": "metric_set", "node_id": "card", "artifact": "c" * 64}])


def claims(doc):
    return [v for v in doc["violations"] if v["code"] == "absence_claim"]


def test_the_rule_tells_the_writer_what_to_write_instead():
    for phrase in ("没有查到", "不存在", "缺失", "没有数据", "本报告未覆盖", "依据不足"):
        assert phrase in ABSENCE_RULE
    assert "absence_claim" in REWRITE_ONLY and "inline_see" in REWRITE_ONLY


@pytest.mark.parametrize("text, found", [
    ("卡券数据缺失：本轮没有查到核销、兑换、退券的任何结果。", ["数据缺失", "没有查到"]),
    ("卡券核销没有任何记录。", ["没有任何记录"]),
    ("本周门店客流数据为空。", ["数据为空"]),
    ("活动内容无数据。", ["无数据"]),
    ("未查询到会员流失的情况。", ["未查询到"]),
    ("库里不存在相关记录。", ["不存在相关记录"]),
    ("兑换记录均不存在。", ["记录均不存在"]),
    ("暂无数据，查不到核销明细。", ["暂无数据", "查不到"]),
    ("| 方向 | 结论 |\n| --- | --- |\n| 卡券 | 没有查到 |", ["没有查到"]),
])
def test_absence_claims_are_recognised(catalog, text, found):
    doc = compose_doc(text, catalog, absence=True)
    got = claims(doc)
    assert [v["text"] for v in got] == found, doc["violations"]
    for v in got:
        assert doc["markdown"][v["span"][0]:v["span"][1]] == v["text"], "span 指着正文里的那几个字"
        assert v.get("unit") and v.get("context")
        assert "依据不足" in v["message"] and "本报告未覆盖" in v["for_model"]


@pytest.mark.parametrize("text", [
    "本报告未覆盖卡券核销的明细：证据目录有省略，相关查询未展开，依据不足。",
    "两家门店的客单价不存在明显差异。",
    "目录里没展开的查询不代表没有数据。",
    "这并非没有查到，而是没有展开。",
    "本周无数据库变更，无数据源切换。",
    "本周销售额 [[m:gmv]]，缺少口径说明的部分已注明。",
    "```sql\nSELECT * FROM t -- 没有查到\n```",
])
def test_other_wording_is_left_alone(catalog, text):
    assert not claims(compose_doc(text, catalog, absence=True))


def test_only_checked_when_the_catalog_was_trimmed(catalog):
    text = "卡券核销数据缺失：没有查到任何记录。"
    assert not claims(compose_doc(text, catalog))
    assert claims(compose_doc(text, catalog, absence=True))


def test_the_claim_can_be_located_in_the_document(catalog):
    doc = compose_doc("## 卡券\n\n本周销售额 [[m:gmv]]。核销数据缺失。", catalog, absence=True)
    [v] = claims(doc)
    segments = {s["id"]: s for b in doc["blocks"] for u in b["units"] for s in u["segments"]}
    seg = segments[v["segment"]]
    assert seg["kind"] == "text" and seg["span"][0] <= v["span"][0] < seg["span"][1]
    assert v["context"] == "核销数据缺失。"
