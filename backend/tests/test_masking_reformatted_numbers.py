"""换了写法的号码（engine/masking.py 的 _Secrets）：按值认得出的长数字（SECRET_DIGITS 位以上）被写成带空格、连字符、
点、括号、全角数字或全角标点、带 +86 / 0086 / 86 前缀的样子，模型写的正文（审批质询的回答）里照样认得出，换成
「已遮罩」；最后一道关（inquiry._leaked）、质询工件里文字格夹着的号码同样认得出。

验收准备时复现过：模型把号码写成 199-1474-2785、199 1474 2785、199.1474.2785、(199)1474-2785、+8619914742785、
全角数字，回答正文、事件、回答文档原样带着，最后一道关也拦不住，正式运行里永久封存。

认法是在正文里找允许夹分隔符的数字串，规范化（全角转半角、去分隔符、去国家码）后在「遮罩值的纯数字写法」集合里
查——集合查找，不现编正则。日期、小数、更长的数里的一截、连字符连着的编号照旧不动。

数据都是虚构的：手机号、金额。
"""
from __future__ import annotations

import time

import pytest

from app.engine import inquiry as inquiry_mod
from app.engine.masking import _masked_view, _Secrets

MASKED = "已遮罩"
PHONE = "19914742785"

#: 同一个号码的各种写法（换了写法的号码）
REFORMATTED = [
    pytest.param("199-1474-2785", id="连字符"),
    pytest.param("199 1474 2785", id="空格"),
    pytest.param("199.1474.2785", id="点"),
    pytest.param("(199)1474-2785", id="括号"),
    pytest.param("(199) 1474 2785", id="括号加空格"),
    pytest.param("+8619914742785", id="+86"),
    pytest.param("+86 199 1474 2785", id="+86 加空格"),
    pytest.param("(+86)19914742785", id="(+86)"),
    pytest.param("0086 199 1474 2785", id="0086"),
    pytest.param("0086-19914742785", id="0086 连字符"),
    pytest.param("86-199-1474-2785", id="86"),
    pytest.param("8619914742785", id="86 连写"),
    pytest.param("１９９１４７４２７８５", id="全角数字"),
    pytest.param("１９９－１４７４－２７８５", id="全角数字和连字符"),
    pytest.param("199　1474　2785", id="全角空格"),
    pytest.param("199．1474．2785", id="全角点"),
    pytest.param("（199）1474-2785", id="全角括号"),
    pytest.param("＋86 199 1474 2785", id="全角加号"),
]


def secrets_of(values: list, *, hidden: str = "phone") -> _Secrets:
    secrets = _Secrets()
    secrets.add({"columns": [hidden], "rows": [[v] for v in values]}, {hidden})
    return secrets


@pytest.mark.parametrize("written", REFORMATTED)
@pytest.mark.parametrize("stored", [PHONE, int(PHONE), "199-1474-2785"], ids=["文本", "整数", "存的就带连字符"])
def test_a_long_number_in_another_shape_is_masked_in_prose(stored, written):
    secrets = secrets_of([stored])
    assert secrets.scrub(f"金额最高的一笔来自 {written}，共 3 笔", prose=True) == f"金额最高的一笔来自 {MASKED}，共 3 笔"
    assert secrets.scrub(f"客户{written}来电", prose=True) == f"客户{MASKED}来电"


@pytest.mark.parametrize("text", [
    # 日期、时间、小数、千分位、更长的数里的一截、连字符连着的编号、别的号码
    "2026-10-06 19:14 下单", "金额 19914.74 元", "共 1,991,474 笔", "号码 1991474278512", "批次 19914742785-2",
    "199-1474-2786", "单号 2026199-1474-2785", "共 199 笔 1474 元 2785 次",
])
def test_other_numbers_and_number_like_text_are_left_alone(text):
    secrets = secrets_of([PHONE])
    assert secrets.scrub(text, prose=True) == text


def test_reformatted_numbers_are_only_read_in_prose_and_text_cells():
    """SQL 里 199-1474-2785 是一个算式，不当成号码换（SQL 里的号码按字面量的用法换，见 inquiry._scrub_sql）。"""
    secrets = secrets_of([PHONE])
    assert secrets.scrub("SELECT 199-1474-2785 AS x") == "SELECT 199-1474-2785 AS x"
    assert secrets.scrub("备注 199-1474-2785", loose=True) == f"备注 {MASKED}"


def test_a_text_cell_holding_a_reformatted_number_is_masked_whole_or_in_part():
    """快照里别的列（没设遮罩）写着换了写法的号码：整格就是号码的整格遮；质询工件里夹在一段文字里的只换那一截。"""
    secrets = secrets_of([PHONE])
    snapshot = {"columns": ["联系方式", "备注"], "rows": [["199-1474-2785", "客户 (199)1474-2785 来电改址"],
                                                       ["138-0000-0000", "例行回访"]]}
    view, masked = _masked_view(snapshot, set(), secrets)
    assert view["rows"] == [[MASKED, "客户 (199)1474-2785 来电改址"], ["138-0000-0000", "例行回访"]]
    assert masked == ["联系方式"]
    view, masked = _masked_view(snapshot, set(), secrets, embedded=True)
    assert view["rows"] == [[MASKED, f"客户 {MASKED} 来电改址"], ["138-0000-0000", "例行回访"]]
    assert masked == ["联系方式"]


@pytest.mark.parametrize("written", REFORMATTED)
def test_the_last_gate_stops_an_answer_still_showing_a_reformatted_number(written):
    """组装之后还认得出（渲染进单元格引用的一格文字里夹着换了写法的号码）：不交出去。正文里的在组装之前就换掉了。"""
    secrets = secrets_of([PHONE])
    entry = {"alias": "Q1", "kind": "query", "artifact": "a1", "source": "shop", "columns": ["备注"], "rows": 1,
             "truncated": False}
    views = {"a1": _masked_view({"columns": ["备注"], "rows": [[f"客户{written}来电改址"]]}, set(), secrets)[0]}
    with pytest.raises(inquiry_mod._Leak):
        inquiry_mod._compose("回访备注是 [[v:Q1.r0.备注]]。", {"Q1": entry}, run_id="r1", views=views, secrets=secrets,
                             allow=[])
    doc = inquiry_mod._compose(f"来电的是 {written}。", {}, run_id="r1", views={}, secrets=secrets, allow=[])
    assert doc["markdown"] == f"来电的是 {MASKED}。"
    assert inquiry_mod._leaked({"markdown": f"来电的是 {written}。", "blocks": []}, secrets)


def test_a_number_the_asker_wrote_in_another_shape_is_left_alone():
    """提问人自己写的号码（换了写法也一样）已经摆在读者眼前：回答里照原样，别的号码照遮。"""
    secrets = secrets_of([PHONE, "13800000000"])
    secrets.protect(["199-1474-2785 这位客户还买过别的吗？"])
    assert secrets.scrub("199 1474 2785 另有 138-0000-0000 的单", prose=True) == f"199 1474 2785 另有 {MASKED} 的单"


def test_a_threshold_left_in_the_sql_is_shown_in_the_prose_whatever_the_shape():
    """SQL 里照原样留着的阈值，回答正文里复述不遮，换了写法也一样（只免正文）。"""
    secrets = secrets_of([20000, 35000], hidden="amount")
    secrets.show({"20000"})
    assert secrets.scrub("金额不低于 20 000 元，最高一笔 35 000 元", prose=True) == f"金额不低于 20 000 元，最高一笔 {MASKED} 元"


def test_many_masked_numbers_are_matched_without_stalling():
    """遮罩值上万个：集合查找，不现编上万个分支的正则。"""
    secrets = secrets_of([str(19900000000 + i) for i in range(20_000)])
    text = "；".join(f"客户 199-0000-{i:04d} 来电" for i in range(0, 4000, 7)) * 3
    began = time.perf_counter()
    out = secrets.scrub(text, prose=True)
    assert time.perf_counter() - began < 2.0
    assert "199-0000" not in out and out.count(MASKED) == text.count("199-0000")
