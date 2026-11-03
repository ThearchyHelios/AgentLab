"""口径草稿的纯逻辑（app/ledger/docdraft.py）。

- 引用定位：跨行、连续空格、全角空格都能还原到原文下标，找不到就是 None；
- 指标归一化：缺名、没有可核对的引用、带 SQL 痕迹整条丢弃；引用太短、chunk 不存在逐条丢弃；顺手删掉取数字段；
- 回复解析：围栏、前后废话、尾随逗号都认，截断的抛 ValueError；
- 对齐：new / duplicate / conflict（单位或定义不同）；
- 合并定义：同名或同义词命中，不动 expression / id，匹配不上进 warnings；
- extract：注入假模型（不调真模型），输入超限按 chunk 边界截断。
"""
from __future__ import annotations

from types import SimpleNamespace

from app.ledger import docdraft

CHUNK = ("本季度销售额按订单创建时间统计，退款订单不计入销售额。"
         "订单数按已支付订单统计，一个订单算一单。")
CHUNKS = {"c1": {"ordinal": 0, "content": CHUNK, "document_id": "doc-1"}}


def metric(name="销售额", quote="销售额按订单创建时间统计", **extra):
    item = {
        "name": name,
        "synonyms": ["营业额"],
        "unit": "元",
        "definition": {"text": "按订单创建时间统计", "citations": [{"chunk": "c1", "quote": quote}]},
    }
    item.update(extra)
    return item


# ==========================================================================
# verify_quote
# ==========================================================================

def test_verify_quote_restores_original_span_across_lines_and_spaces():
    text = "口径说明：\n销售额\u3000按订单创建时间统计，\n\n退款不计入。\n"
    span = docdraft.verify_quote(text, "销售额 按订单创建时间统计， 退款")
    assert span is not None
    start, end = span
    assert text[start:end] == "销售额\u3000按订单创建时间统计，\n\n退款"


def test_verify_quote_ignores_extra_spaces():
    text = "订单   数"
    assert docdraft.verify_quote(text, "订单 数") == (0, 6)
    assert docdraft.verify_quote(text, " 订单\t数 ") == (0, 6)


def test_verify_quote_handles_full_width_space_and_missing_quote():
    text = "销售额\u3000合计"
    assert docdraft.verify_quote(text, "销售额 合计") == (0, 6)
    assert docdraft.verify_quote(text, "毛利率") is None
    assert docdraft.verify_quote(text, "   ") is None


# ==========================================================================
# normalize_metrics
# ==========================================================================

def test_normalize_metrics_keeps_a_verified_metric_and_hands_over_span():
    good, dropped = docdraft.normalize_metrics({"metrics": [metric()]}, CHUNKS)
    assert dropped == []
    [item] = good
    [citation] = item["citations"]
    assert citation["document_id"] == "doc-1"
    assert citation["chunk"] == "c1"
    assert CHUNK[citation["span"][0]:citation["span"][1]] == "销售额按订单创建时间统计"
    assert item["definition"]["citations"] == item["citations"]


def test_normalize_metrics_drops_a_metric_without_a_name():
    good, dropped = docdraft.normalize_metrics({"metrics": [metric(name="  ")]}, CHUNKS)
    assert good == []
    assert dropped[0]["reason"] == "缺少指标名称"


def test_normalize_metrics_drops_short_quotes_and_unknown_chunks_per_citation():
    item = metric(quote="销售额")
    item["definition"]["citations"].append({"chunk": "missing", "quote": "退款订单不计入销售额"})
    good, dropped = docdraft.normalize_metrics({"metrics": [item]}, CHUNKS)
    # 两条引用都不可用：一条太短，一条 chunk 不存在
    assert good == []
    assert "引用" in dropped[0]["reason"]

    # 一条可用、一条不可用：只丢不可用的那条
    item = metric()
    item["definition"]["citations"] += [{"chunk": "missing", "quote": "退款订单不计入销售额"}]
    good, dropped = docdraft.normalize_metrics({"metrics": [item]}, CHUNKS)
    assert dropped == []
    assert len(good[0]["citations"]) == 1


def test_normalize_metrics_drops_sql_traces_and_strips_forbidden_fields():
    traced = metric(notes="select sum(amount) from orders")
    good, dropped = docdraft.normalize_metrics({"metrics": [traced]}, CHUNKS)
    assert good == []
    assert "SQL" in dropped[0]["reason"]

    templated = metric(notes="{{ input.period }}")
    good, dropped = docdraft.normalize_metrics({"metrics": [templated]}, CHUNKS)
    assert good == []
    assert "SQL" in dropped[0]["reason"]

    clean = metric(table="orders", column="amount", sql="", filter={"tables": ["orders"]})
    # sql 是空串，没有 select … from，按字段删除处理而不是整条丢弃
    good, dropped = docdraft.normalize_metrics({"metrics": [clean]}, CHUNKS)
    assert dropped == []
    assert set(good[0]) & set(docdraft.FORBIDDEN_FIELDS) == set()
    assert good[0]["filter"] == {}


# ==========================================================================
# parse_reply
# ==========================================================================

def test_parse_reply_accepts_fences_chatter_and_trailing_commas():
    text = '好的，结果如下：\n```json\n{"metrics": [{"name": "销售额",},],}\n```\n以上。'
    assert docdraft.parse_reply(text) == {"metrics": [{"name": "销售额"}]}


def test_parse_reply_rejects_a_truncated_object():
    try:
        docdraft.parse_reply('{"metrics": [{"name": "销售额"')
    except ValueError as e:
        assert "不完整" in str(e) or "没有" in str(e)
    else:  # pragma: no cover - 只有实现走样时才会到这里
        raise AssertionError("截断的 JSON 应当抛 ValueError")


# ==========================================================================
# alignment
# ==========================================================================

EXISTING = [
    {"workflow_id": "wf-1", "version": 3, "node_id": "card", "name": "销售额",
     "synonyms": ["GMV"], "unit": "元", "definition_text": "按订单创建时间统计"},
    {"workflow_id": "wf-2", "version": 1, "node_id": "card", "name": "订单数",
     "synonyms": [], "unit": "单", "definition_text": "按已支付订单统计"},
]


def test_alignment_reports_new_duplicate_and_conflict():
    draft = [
        {"name": "成交额", "synonyms": ["gmv "], "unit": "元", "definition": {"text": "按订单创建时间统计"}},
        {"name": "订单数 ", "unit": "万元", "definition": {"text": "按已支付订单统计"}},
        {"name": "客单价", "unit": "元", "definition": {"text": "销售额除以订单数"}},
    ]
    result = docdraft.alignment(draft, EXISTING)
    assert result["new"] == ["客单价"]
    assert result["duplicate"] == [{"name": "成交额", "workflow_id": "wf-1", "version": 3, "node_id": "card"}]
    [conflict] = result["conflict"]
    assert conflict["name"] == "订单数"
    assert conflict["workflow_id"] == "wf-2"
    assert "单位不同" in conflict["reason"]


def test_alignment_conflicts_when_only_the_definition_differs():
    draft = [{"name": "销售额", "unit": "元", "definition": {"text": "按发货时间统计"}}]
    [conflict] = docdraft.alignment(draft, EXISTING)["conflict"]
    assert conflict["reason"] == "定义说明不同"


# ==========================================================================
# merge_definition
# ==========================================================================

def test_merge_definition_matches_by_synonym_and_keeps_expression():
    cards = [{"id": "gmv", "name": "销售额", "synonyms": ["GMV"], "expression": "cell(nodes.q, 0, '金额')",
              "definition": {"text": "旧说明", "owner": "财务"}}]
    drafts = [
        {"name": "成交额", "synonyms": ["gmv"], "definition": {"text": "新说明", "scope": "全渠道"}},
        {"name": "客单价", "definition": {"text": "销售额除以订单数"}},
    ]
    merged, warnings = docdraft.merge_definition(cards, drafts)
    [card] = merged
    assert card["id"] == "gmv"
    assert card["expression"] == "cell(nodes.q, 0, '金额')"
    assert card["definition"] == {"text": "新说明", "owner": "财务", "scope": "全渠道"}
    assert len(warnings) == 1 and "客单价" in warnings[0]


# ==========================================================================
# extract
# ==========================================================================

class FakeModel:
    model_name = "fake-model"

    def __init__(self, text: str) -> None:
        self.text = text
        self.messages: list = []

    async def ainvoke(self, messages):
        self.messages = messages
        return SimpleNamespace(content=self.text)


async def test_extract_uses_the_injected_model_and_normalizes_its_reply():
    reply = """```json
    {"metrics": [{"name": "销售额", "unit": "元",
      "definition": {"text": "按订单创建时间统计",
      "citations": [{"chunk": "c1", "quote": "销售额按订单创建时间统计"}]}}],
     "open_questions": ["退款订单是否算销售额？"]}
    ```"""
    model = FakeModel(reply)
    result = await docdraft.extract(None, docs=[{"document_id": "doc-1", "title": "口径说明"}],
                                    chunks=CHUNKS, model=model)
    assert result["model_id"] == "fake-model"
    assert result["truncated"] is False
    assert result["open_questions"] == ["退款订单是否算销售额？"]
    [item] = result["metrics"]
    assert item["citations"][0]["document_id"] == "doc-1"


async def test_extract_truncates_at_chunk_boundaries_and_flags_it():
    first = "第一段：" + "销售额按订单创建时间统计。" * 2000
    second = "第二段：" + "退款订单不计入销售额。" * 2000
    chunks = {
        "c1": {"ordinal": 0, "content": first, "document_id": "doc-1"},
        "c2": {"ordinal": 1, "content": second, "document_id": "doc-1"},
    }
    model = FakeModel('{"metrics": [], "open_questions": []}')
    result = await docdraft.extract(None, docs=[], chunks=chunks, model=model)
    assert result["truncated"] is True
    sent = "\n".join(str(getattr(m, "content", "")) for m in model.messages)
    assert "第二段" not in sent
    assert len(sent) <= docdraft.MAX_INPUT_CHARS
