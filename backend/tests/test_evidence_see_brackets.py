"""依据标记 [[see:…]] 写进括号、夹在标点中间时的渲染（期 5 真实模型验收 M1）。

[[see:]] 渲染成空，按设计挂在句末。核查 agent（双盲复算、反方审稿、审批质询共用）以前被提示「提到某次查询时写
[[see:Q1]]」，模型就把它写进括号、句中：「结果一致（[[see:Q3]]，原报告 SQL 未按 status 过滤）」「合计
[[v:Q1.r1.金额合计]]（[[see:Q1]]）」，渲染后剩下「（，原报告…）」「（）」，正式运行里随封存永久留着。

- 括号（圆括号、方括号的全角半角，一对里混用也算；方头括号【】、六角括号〔〕）里只有依据的，连括号一起去掉；去掉之后
  整对括号当一个依据，外面照下面几条再收拾一遍（括号前悬着的停顿标点、括号前后的空白）。连着写的几个
  （「（[[see:Q1]]）（[[see:m:gmv]]）」）并成一组收拾
- 依据渲染成空、周围的字去掉之后，两边真正挨上的两个字会粘成一串的（「4,919.5（[[see:Q1]]）2 月」，依据两边什么都
  没有的「4,919.5[[see:Q1]]212 笔」也是），补一个空格：粘起来读成另一个数，哪项核对都查不出来。每个依据两边都判，
  找挨上的字时越过一切不显示的：依据、只装着依据的括号、粗体斜体的星号和下划线、删除线的波浪线；依据标记的「]」
  不当数字。一片里最多补一个，挨上的是写作者留着的空白的不补；半角逗号夹着依据、去掉后一个之后同理
  （「212,[[see:Q1]],5」→「212, 5」），右边是紧贴着下文的半角逗号的（「1,200（[[see:Q1]]）,900」）补在逗号后面
- 依据在括号开头、后面跟着「，」「、」「；」「：」的，标点一起去掉；在括号末尾、前面紧挨着标点的同理
- 依据夹在两个分隔标点中间的，去掉后一个；后面就到句末（句末标点、全文结尾）、前面或几个依据之间有「，」「、」
  「；」的，去掉这些标点，到句末只隔着空白的去掉空白；依据后面、句末前面的标点是写作者自己的，不动
- 换行不算句末：列表项末尾的「；」、软换行处的「，」是写作者自己的，依据挂在它后面正合「挂在句末」的规矩，照旧留着
- 括号里、句末前几个依据之间的分隔标点一并去掉；两边都是字时，依据之间的标点可能是分句的标点，照旧留着
- 依据标记本身留在原文里：句子照旧挂着这条依据（see、cites），只是不显示
- 不含依据的括号、没有依据的文档一个字不动：内容哈希和改动之前一样
- 流式渲染分块输出拼起来仍然等于整篇渲染
- 已知限制：依据写在「依据」「见」「取自」「来源」这类引出语后面时，引出语悬空（「依据。」「（来源）」）。渲染时去不掉，
  靠核查 agent 的提示词避免，模型不照做时核查 agent 记一条日志（test_verify_rationale_rules）

数字、表名、地名都是虚构的。
"""
from __future__ import annotations

import random
import re

import pytest

from app.core.artifact_store import canonical_json, content_hash
from app.engine.evidence import (
    StreamRenderer,
    build_catalog,
    compose_doc,
    iter_units,
    render_markers,
    verify_doc,
)

ART = "a" * 64
SNAP_ID = "b" * 64
#: 一次查询的快照：华南 4,919.5 元 212 笔、华东 1,891.8 元 113 笔
SNAP = {"columns": ["区域", "销售额", "订单数"], "rows": [["华南", 4919.5, 212], ["华东", 1891.8, 113]],
        "row_count": 2, "truncated": False, "elapsed_ms": 1, "sql": "SELECT 1", "source": "shop"}
CARD = {"kind": "metric_set", "caliber": "周报口径", "caliber_version": "v1", "artifact": ART,
        "metrics": [{"id": "gmv", "name": "销售额", "unit": "元", "value": 45678.5, "decimals": 1,
                     "format": "thousands", "status": "ok", "expression": "…"},
                    {"id": "yoy", "name": "同比增幅", "unit": "%", "value": 12.5, "decimals": 1,
                     "format": "plain", "status": "ok", "expression": "…"}]}
LEDGER = [{"kind": "metric_set", "node_id": "cal", "exec": 1, "artifact": ART, "caliber": "周报口径",
           "version": "v1", "metrics": ["gmv", "yoy"]},
          {"kind": "query", "node_id": "fetch", "exec": 1, "artifact": SNAP_ID, "call_id": "c1",
           "tool": "db_query__shop", "source": "shop", "columns": SNAP["columns"], "rows": 2, "truncated": False}]
LOADER = {SNAP_ID: SNAP}.__getitem__


@pytest.fixture
def catalog():
    return build_catalog(nodes={"cal": CARD}, ledger=LEDGER, inputs={"week": "2026-W37"})


def render(raw: str, catalog) -> str:
    return render_markers(raw, catalog, loader=LOADER)


def compose(raw: str, catalog) -> dict:
    return compose_doc(raw, catalog, node_id="write", run_id="r1", loader=LOADER)


# --------------------------------------------------------------------------
# 括号里只有依据：连括号一起去掉
# --------------------------------------------------------------------------

@pytest.mark.parametrize(("raw", "shown"), [
    ("先按状态核对口径（[[see:Q1]]）：已付 [[v:Q1.r0.订单数]] 笔。", "先按状态核对口径：已付 212 笔。"),
    ("合计 [[v:Q1.r0.销售额]]（[[see:Q1]]）。", "合计 4,919.5。"),
    ("换口径不改变排序([[see:Q1]])，结论成立。", "换口径不改变排序，结论成立。"),
    ("结果一致（[[see:Q1]])。", "结果一致。"),                                    # 一对括号全角半角混用
    ("结果一致（[[see:Q1]] [[see:m:gmv]]）。", "结果一致。"),
    ("结果一致（[[see:Q1]]、[[see:m:gmv]]）。", "结果一致。"),
    ("结果一致（ [[see:Q1]]， ）。", "结果一致。"),
    ("结果一致 （[[see:Q1]]）。", "结果一致。"),                                  # 括号前的空白一起去掉
    ("前一句 （[[see:Q1]]）后一句", "前一句 后一句"),                              # 后面紧跟着字：留一个空格隔开
    ("末尾（[[see:Q1]]）", "末尾"),
])
def test_a_bracket_holding_only_see_is_dropped(catalog, raw, shown):
    assert render(raw, catalog) == shown
    assert compose(raw, catalog)["markdown"] == shown


def test_indentation_before_a_see_only_bracket_is_kept(catalog):
    assert render("- 第一条\n  （[[see:Q1]]）续行", catalog) == "- 第一条\n  续行"


# --------------------------------------------------------------------------
# 依据在括号开头 / 末尾，紧挨着分隔标点
# --------------------------------------------------------------------------

@pytest.mark.parametrize(("raw", "shown"), [
    # 在括号开头：后面的标点（和空白）一起去掉
    ("结果一致（[[see:Q1]]，原报告未按状态过滤）。", "结果一致（原报告未按状态过滤）。"),
    ("结果一致（[[see:Q1]]、原报告未过滤）。", "结果一致（原报告未过滤）。"),
    ("结果一致（[[see:Q1]]；原报告未过滤）。", "结果一致（原报告未过滤）。"),
    ("结果一致（[[see:Q1]]：原报告未过滤）。", "结果一致（原报告未过滤）。"),
    ("结果一致([[see:Q1]], 原报告未过滤)。", "结果一致(原报告未过滤)。"),
    ("结果一致（[[see:Q1]] 原报告未过滤）。", "结果一致（原报告未过滤）。"),
    ("结果一致（ [[see:Q1]]，[[see:m:gmv]]，原报告未过滤）。", "结果一致（原报告未过滤）。"),
    # 在括号末尾：前面的标点（和空白）一起去掉
    ("复算原查询（同 SQL 同过滤条件，[[see:Q1]]）：华南居首。", "复算原查询（同 SQL 同过滤条件）：华南居首。"),
    ("复算原查询(同 SQL, [[see:Q1]])。", "复算原查询(同 SQL)。"),
    ("复算原查询（同 SQL 同过滤条件 [[see:Q1]]）。", "复算原查询（同 SQL 同过滤条件）。"),
    ("订单（[[v:Q1.r0.订单数]] 笔，[[see:Q1]]）", "订单（212 笔）"),
    ("口径（按支付日；[[see:Q1]]、[[see:m:gmv]]）相同。", "口径（按支付日）相同。"),
])
def test_punctuation_next_to_see_inside_a_bracket_goes_with_it(catalog, raw, shown):
    assert render(raw, catalog) == shown
    assert compose(raw, catalog)["markdown"] == shown


@pytest.mark.parametrize(("raw", "shown"), [
    # 夹在两个分隔标点中间：去掉后一个（括号里外都一样）
    ("口径相同（按支付日，[[see:Q1]]，不含退款）。", "口径相同（按支付日，不含退款）。"),
    ("结果一致，[[see:Q1]]，原报告未过滤。", "结果一致，原报告未过滤。"),
    ("a, [[see:Q1]], b", "a, b"),
    ("a,[[see:Q1]],b", "a, b"),                                       # 半角逗号后面照排版习惯隔一个空格
    # 前面是分隔标点、后面就到句末：去掉前面那个
    ("华南居首，[[see:Q1]]。", "华南居首。"),
    ("华南居首，[[see:Q1]]", "华南居首"),
    ("华南居首，[[see:Q1]] 。", "华南居首。"),
    # 几个依据之间的分隔标点
    ("华南居首 [[see:Q1]]，[[see:m:gmv]]。", "华南居首。"),
    # 后面就到句末、中间只隔着空白：空白一起去掉，不剩「华南居首 。」
    ("华南居首 [[see:Q1]] 。", "华南居首。"),
    ("华南居首 [[see:Q1]] [[see:m:gmv]] 。", "华南居首。"),
])
def test_punctuation_left_dangling_by_see_is_dropped(catalog, raw, shown):
    assert render(raw, catalog) == shown
    assert compose(raw, catalog)["markdown"] == shown


@pytest.mark.parametrize(("raw", "shown"), [
    # 括号前悬着的停顿标点：去掉括号之后和没写括号的「华南居首，[[see:Q1]]。」一样收拾
    ("华南居首，（[[see:Q1]]）。", "华南居首。"),
    ("华南居首、([[see:Q1]])", "华南居首"),
    ("华南居首 （[[see:Q1]]） 。", "华南居首。"),
    ("华南居首，（[[see:Q1]]），华东第二。", "华南居首，华东第二。"),       # 夹在两个逗号中间：去掉后一个
    ("华南居首（（[[see:Q1]]））。", "华南居首。"),                          # 套着的两层只装着依据，一起去掉
    # 换行不算句末：括号里外一样留着写作者的标点
    ("华南居首、（[[see:Q1]]）\n下一行", "华南居首、\n下一行"),
])
def test_a_dropped_bracket_is_tidied_like_a_bare_see(catalog, raw, shown):
    assert render(raw, catalog) == shown
    assert compose(raw, catalog)["markdown"] == shown


@pytest.mark.parametrize(("raw", "shown"), [
    # 括号是两边唯一的分隔：去掉之后数字会粘成另一个数，换成一个空格
    ("华东 [[v:Q1.r0.销售额]]（[[see:Q1]]）2 月起持续下滑。", "华东 4,919.5 2 月起持续下滑。"),
    ("合计 [[v:Q1.r0.销售额]]（[[see:Q1]]）2026 年同比持平。", "合计 4,919.5 2026 年同比持平。"),
    ("订单 [[v:Q1.r0.订单数]]([[see:Q1]])2 个区域。", "订单 212 2 个区域。"),
    ("增幅 [[m:yoy]]（[[see:m:yoy]]）3 个区域。", "增幅 12.5% 3 个区域。"),
    ("合计 [[v:Q1.r0.销售额]]（[[see:Q1]]）[[v:Q1.r0.订单数]] 笔。", "合计 4,919.5 212 笔。"),   # 后面的标记渲染出数字
    ("合计 [[v:Q1.r0.销售额]]（[[see:Q1]]）[[see:m:gmv]]2 月。", "合计 4,919.5 2 月。"),       # 越过不显示的依据看
    ("编号 Q1（[[see:Q1]]）x", "编号 Q1 x"),
    ("212,（[[see:Q1]]）5 个区域", "212, 5 个区域"),
    # 半角逗号夹着依据：去掉后一个之后，前一个不能直接贴着数字
    ("212,[[see:Q1]],5 个区域", "212, 5 个区域"),
    # 不会粘的照旧去掉：中文和数字之间、括号前已有空白、后面是中文、隔着全角逗号
    ("居首（[[see:Q1]]）2 月起", "居首2 月起"),
    ("合计 [[v:Q1.r0.销售额]] （[[see:Q1]]）2 月起", "合计 4,919.5 2 月起"),
    ("合计 [[v:Q1.r0.销售额]]（[[see:Q1]]）元", "合计 4,919.5元"),
    ("华南（[[see:Q1]]）居首", "华南居首"),
    ("212，[[see:Q1]]，5 个区域", "212，5 个区域"),
])
def test_dropping_a_bracket_does_not_glue_numbers_together(catalog, raw, shown):
    assert render(raw, catalog) == shown
    assert compose(raw, catalog)["markdown"] == shown


def test_a_glued_number_would_have_slipped_through_the_checks(catalog):
    """粘起来的数没有哪项核对查得出：片段本身对（4,919.5 后面是另一段文字），月份不算裸数字。所以只能在渲染时不粘。"""
    doc = compose("华东 [[v:Q1.r0.销售额]]（[[see:Q1]]）2 月起持续下滑。", catalog)
    assert doc["markdown"] == "华东 4,919.5 2 月起持续下滑。" and doc["violations"] == []
    assert verify_doc(doc, catalog, loader=LOADER)["violations"] == []
    [unit] = [unit for _, unit in iter_units(doc)]
    assert [s["alias"] for s in unit["see"]] == ["Q1"]


# --------------------------------------------------------------------------
# 粘不粘看去掉之后真正挨上的两个字：越过依据、只装着依据的括号、粗体斜体的星号和下划线（复核第 2 轮）
# --------------------------------------------------------------------------

#: 读者看到的字：前端按 Markdown 渲染，星号、两个下划线不显示
_EMPHASIS = re.compile(r"\*+|__+")


def _visible(text: str) -> str:
    return _EMPHASIS.sub("", text)


@pytest.mark.parametrize(("raw", "shown"), [
    # 两个只装着依据的括号连着写（全角半角混用、套着的也一样）：中间什么都不剩，两边会粘的照样隔开
    ("华东 [[v:Q1.r0.销售额]]（[[see:Q1]]）（[[see:m:gmv]]）2 月起持续下滑。", "华东 4,919.5 2 月起持续下滑。"),
    ("订单 [[v:Q1.r0.订单数]]（[[see:Q1]]）（[[see:m:gmv]]）[[v:Q1.r0.订单数]] 笔。", "订单 212 212 笔。"),
    ("增幅 [[m:yoy]]（[[see:m:yoy]]）（[[see:Q1]]）3 个区域。", "增幅 12.5% 3 个区域。"),
    ("增幅 [[m:yoy]]（[[see:m:yoy]]）([[see:Q1]])3 个区域。", "增幅 12.5% 3 个区域。"),
    ("增幅 [[m:yoy]]（[[see:m:yoy]]）（（[[see:Q1]]））3 个区域。", "增幅 12.5% 3 个区域。"),
    ("合计 [[v:Q1.r0.销售额]]（[[see:Q1]]） （[[see:m:gmv]]）2 月", "合计 4,919.5 2 月"),
    # 依据和括号连着写、几个依据之间隔着空白：去掉之后两边会粘的，同样隔开
    ("合计 [[v:Q1.r0.销售额]][[see:Q1]]（[[see:m:gmv]]）2 月。", "合计 4,919.5 2 月。"),
    ("合计 [[v:Q1.r0.销售额]][[see:Q1]] [[see:m:gmv]]2 月。", "合计 4,919.5 2 月。"),
    # 不会粘的照旧：中文和数字之间
    ("华南居首（[[see:Q1]]）（[[see:m:gmv]]）2 月起回升。", "华南居首2 月起回升。"),
])
def test_see_only_brackets_written_together_do_not_glue_numbers(catalog, raw, shown):
    assert render(raw, catalog) == shown
    assert compose(raw, catalog)["markdown"] == shown


@pytest.mark.parametrize(("raw", "shown"), [
    # 连着写的几个只装着依据的括号当一组收拾：括号前悬着的停顿标点、中间的标点一并去掉
    ("华南居首，（[[see:Q1]]）（[[see:m:gmv]]）。", "华南居首。"),
    ("华南居首，[[see:Q1]]（[[see:m:gmv]]）。", "华南居首。"),
    ("华南居首，（[[see:Q1]]）、（[[see:m:gmv]]）。", "华南居首。"),
    ("结果一致（[[see:Q1]]）（[[see:m:gmv]]），原报告未过滤。", "结果一致，原报告未过滤。"),
    ("结果一致（[[see:Q1]]）（[[see:m:gmv]]，原报告未过滤）。", "结果一致（原报告未过滤）。"),
])
def test_see_only_brackets_written_together_are_tidied_as_one(catalog, raw, shown):
    assert render(raw, catalog) == shown
    assert compose(raw, catalog)["markdown"] == shown


@pytest.mark.parametrize(("raw", "shown"), [
    # 粗体在括号前、后、把括号包在里面：星号不显示，挨上的是两边的数
    ("销售额 **[[v:Q1.r0.销售额]]**（[[see:Q1]]）2 月起持续下滑。", "销售额 4,919.5 2 月起持续下滑。"),
    ("华东 **[[v:Q1.r0.销售额]]**（[[see:Q1]]）[[v:Q1.r0.订单数]] 笔。", "华东 4,919.5 212 笔。"),
    ("华东 [[v:Q1.r0.销售额]]（[[see:Q1]]）**[[v:Q1.r0.订单数]] 笔**", "华东 4,919.5 212 笔"),
    ("**[[v:Q1.r0.销售额]]（[[see:Q1]]）**2 月", "4,919.5 2 月"),
    ("华东 **[[v:Q1.r0.销售额]]**（[[see:Q1]]）（[[see:m:gmv]]）**[[v:Q1.r0.订单数]]** 笔", "华东 4,919.5 212 笔"),
    ("__[[v:Q1.r0.销售额]]__（[[see:Q1]]）2 月", "4,919.5 2 月"),
    ("*[[v:Q1.r0.销售额]]*（[[see:Q1]]）2 月", "4,919.5 2 月"),
    # 依据前的空白：后面紧跟着的是星号，越过它看
    ("合计 [[v:Q1.r0.销售额]] [[see:Q1]]**2 月**起", "合计 4,919.5 2 月起"),
    # 不会粘的照旧
    ("合计 **[[v:Q1.r0.销售额]]**（[[see:Q1]]）元", "合计 4,919.5元"),
])
def test_bold_around_a_number_does_not_hide_the_glue(catalog, raw, shown):
    assert _visible(render(raw, catalog)) == shown
    assert _visible(compose(raw, catalog)["markdown"]) == shown


def test_bold_still_wraps_the_number_after_the_bracket_is_dropped(catalog):
    doc = compose("销售额 **[[v:Q1.r0.销售额]]**（[[see:Q1]]）2 月起持续下滑。", catalog)
    [number] = [seg for _, unit in iter_units(doc) for seg in unit["segments"] if seg.get("ref")]
    assert number["text"] == "4,919.5" and number.get("strong") is True
    assert render("销售额 **[[v:Q1.r0.销售额]]**（[[see:Q1]]）2 月", catalog) == "销售额 **4,919.5** 2 月"


@pytest.mark.xfail(strict=True, reason="已知限制：粗体把括号包在里面时，补的空格落在收尾的星号前面（「**4,919.5 **2 月」）。"
                                       "前端的行内 Markdown 照样认这对星号，组装出的文档去掉了包着标记的星号，读者看到的一样；"
                                       "挪到星号外面得先知道这对星号从哪开始，流式渲染的一块里不一定有")
def test_the_space_lands_outside_a_bold_that_wraps_the_bracket(catalog):
    assert render("销售额 **[[v:Q1.r0.销售额]]（[[see:Q1]]）**2 月", catalog) == "销售额 **4,919.5** 2 月"


# --------------------------------------------------------------------------
# 依据直接夹在两个数之间：两边没有括号、空白、标点，也不能让两个数并成一个（复核第 3 轮）
# --------------------------------------------------------------------------

@pytest.mark.parametrize(("raw", "shown"), [
    # 依据两边什么都没有：渲染成空之后两边直接挨上
    ("华东 [[v:Q1.r0.销售额]][[see:Q1]][[v:Q1.r0.订单数]] 笔。", "华东 4,919.5 212 笔。"),
    ("华东 [[v:Q1.r0.销售额]][[see:Q1]]2 月起持续下滑。", "华东 4,919.5 2 月起持续下滑。"),
    ("增幅 [[m:yoy]][[see:m:yoy]]3 个区域。", "增幅 12.5% 3 个区域。"),
    ("编号 Q1[[see:Q1]]x 区", "编号 Q1 x 区"),
    ("212,[[see:Q1]]5 个区域", "212, 5 个区域"),
    # 连着写的一串当一个看
    ("华东 [[v:Q1.r0.销售额]][[see:Q1]][[see:m:gmv]]2 月", "华东 4,919.5 2 月"),
    # 不会粘的照旧：中文和数字之间、后面是中文或单位、写作者自己留了空白、句号后面
    ("华南居首[[see:Q1]]2 月起回升。", "华南居首2 月起回升。"),
    ("合计 [[v:Q1.r0.销售额]][[see:Q1]]元", "合计 4,919.5元"),
    ("合计 [[v:Q1.r0.销售额]][[see:Q1]]%", "合计 4,919.5%"),
    ("合计 [[v:Q1.r0.销售额]][[see:Q1]] 2 月", "合计 4,919.5 2 月"),
    ("合计 [[v:Q1.r0.销售额]] [[see:Q1]]2 月", "合计 4,919.5 2 月"),
    ("华南居首。[[see:Q1]]2 月起回升。", "华南居首。2 月起回升。"),
])
def test_a_bare_see_between_two_numbers_does_not_glue_them(catalog, raw, shown):
    assert render(raw, catalog) == shown
    assert compose(raw, catalog)["markdown"] == shown


@pytest.mark.parametrize(("raw", "shown"), [
    # 粗体收尾后紧跟依据再跟数字（组装文档时包着标记的星号去掉了，挨上的是两个数）
    ("**华东 [[v:Q1.r0.销售额]]**[[see:Q1]]2 月", "华东 4,919.5 2 月"),
    ("华东 **[[v:Q1.r0.销售额]]**[[see:Q1]][[v:Q1.r0.订单数]] 笔", "华东 4,919.5 212 笔"),
    ("华东 [[v:Q1.r0.销售额]][[see:Q1]]**2 月**起", "华东 4,919.5 2 月起"),
    ("*[[v:Q1.r0.销售额]]*[[see:Q1]]2 月", "4,919.5 2 月"),
])
def test_a_bare_see_after_bold_does_not_glue_the_numbers(catalog, raw, shown):
    assert _visible(render(raw, catalog)) == shown
    assert _visible(compose(raw, catalog)["markdown"]) == shown


def test_the_space_goes_before_the_see_and_outside_the_bold(catalog):
    """补的空格落在依据前面、收尾的星号后面：粗体照样成对，依据照样挂在句子上。一片里有去掉的字时落在头一个去掉的字
    那里（同复核第 2 轮）：粗体在两段依据中间收尾的，空格照样在星号外面。"""
    assert render("**华东 [[v:Q1.r0.销售额]]**[[see:Q1]]2 月", catalog) == "**华东 4,919.5** 2 月"
    assert render("**华东 [[v:Q1.r0.销售额]][[see:Q1]]**（[[see:m:gmv]]）2 月", catalog) == "**华东 4,919.5** 2 月"
    doc = compose("华东 [[v:Q1.r0.销售额]][[see:Q1]][[v:Q1.r0.订单数]] 笔。", catalog)
    [unit] = [unit for _, unit in iter_units(doc)]
    assert [s["alias"] for s in unit["see"]] == ["Q1"]
    assert [seg["text"] for seg in unit["segments"] if seg.get("ref")] == ["4,919.5", "212"]
    assert verify_doc(doc, catalog, loader=LOADER)["violations"] == []


@pytest.mark.parametrize(("raw", "shown"), [
    # 右边是紧贴着下文的半角逗号：去掉依据（和只装着依据的括号）之后读成一个数（「1,200,900」），空格补在逗号后面
    ("分别为 [[v:Q1.r0.订单数]]（[[see:Q1]]）,5 个区域", "分别为 212, 5 个区域"),
    ("分别为 [[v:Q1.r0.销售额]][[see:Q1]],[[v:Q1.r1.销售额]]。", "分别为 4,919.5, 1,891.8。"),
    ("分别为 [[v:Q1.r0.销售额]]【[[see:Q1]]】,[[v:Q1.r1.销售额]]（[[see:Q1]]）。", "分别为 4,919.5, 1,891.8。"),
    ("212[[see:Q1]],5 个区域", "212, 5 个区域"),
    ("分别为 [[v:Q1.r0.销售额]]（[[see:Q1]]）,**[[v:Q1.r1.销售额]]**。", "分别为 4,919.5, **1,891.8**。"),   # 下文包着粗体
    # 不会读成一串的照旧：逗号后面已有空白、是中文，逗号前面是中文
    ("分别为 [[v:Q1.r0.订单数]]（[[see:Q1]]）, 5 个区域", "分别为 212, 5 个区域"),
    ("分别为 [[v:Q1.r0.订单数]]（[[see:Q1]]）,华东第二", "分别为 212,华东第二"),
    ("分别为 [[v:Q1.r0.订单数]][[see:Q1]],[[see:m:gmv]]华东第二", "分别为 212,华东第二"),   # 逗号后面的依据不显示
    ("华南居首（[[see:Q1]]）,5 个区域", "华南居首,5 个区域"),
])
def test_a_half_width_comma_left_against_the_next_number_gets_a_space(catalog, raw, shown):
    assert render(raw, catalog) == shown
    assert _visible(compose(raw, catalog)["markdown"]) == _visible(shown)


def test_a_struck_number_does_not_glue_either(catalog):
    """删除线前端照样画成字（淡色），后面紧跟的数挨着它读成一串：星号、下划线之外，两个波浪线也越过去看。"""
    assert render("~~[[v:Q1.r0.销售额]]~~[[see:Q1]]2 月", catalog) == "~~4,919.5~~ 2 月"
    assert render("~~[[v:Q1.r0.销售额]]~~（[[see:Q1]]）2 月", catalog) == "~~4,919.5~~ 2 月"
    assert render("[[v:Q1.r0.销售额]][[see:Q1]]~~2 月~~", catalog) == "4,919.5 ~~2 月~~"


# --------------------------------------------------------------------------
# 别的括号：【】［］〔〕[] 只装着依据的，和（）一样去掉（复核第 3 轮）
# --------------------------------------------------------------------------

@pytest.mark.parametrize(("raw", "shown"), [
    ("合计 [[v:Q1.r0.销售额]]【[[see:Q1]]】。", "合计 4,919.5。"),
    ("合计 [[v:Q1.r0.销售额]]［[[see:Q1]]］。", "合计 4,919.5。"),
    ("合计 [[v:Q1.r0.销售额]]〔[[see:Q1]]〕。", "合计 4,919.5。"),
    ("合计 [[v:Q1.r0.销售额]][[[see:Q1]]]。", "合计 4,919.5。"),
    ("结果一致［[[see:Q1]]]。", "结果一致。"),                                   # 方括号一对里全角半角混用
    ("结果一致【[[see:Q1]]，[[see:m:gmv]]】。", "结果一致。"),
    ("结果一致 【 [[see:Q1]] 】 。", "结果一致。"),
    ("前一句 【[[see:Q1]]】后一句", "前一句 后一句"),
    ("末尾[[[see:Q1]]]", "末尾"),
    # 去掉之后整对括号当一个依据，外面悬着的停顿标点照原规则收拾
    ("华南居首，【[[see:Q1]]】。", "华南居首。"),
    ("华南居首，[[[see:Q1]]]，华东第二。", "华南居首，华东第二。"),
    # 套着的、和别的括号连着写的
    ("华南居首（【[[see:Q1]]】）。", "华南居首。"),
    ("华南居首【[[see:Q1]]】（[[see:m:gmv]]）〔[[see:Q1]]〕。", "华南居首。"),
    # 依据在括号开头、末尾，紧挨着分隔标点
    ("结果一致【[[see:Q1]]，原报告未过滤】。", "结果一致【原报告未过滤】。"),
    ("复算原查询〔同 SQL，[[see:Q1]]〕。", "复算原查询〔同 SQL〕。"),
    ("结果一致[[[see:Q1]]; 原报告未过滤]。", "结果一致[原报告未过滤]。"),
    # 去掉之后两边的数不能并成一个
    ("华东 [[v:Q1.r0.销售额]]【[[see:Q1]]】2 月", "华东 4,919.5 2 月"),
    ("华东 [[v:Q1.r0.销售额]][[[see:Q1]]]2 月", "华东 4,919.5 2 月"),
    ("华东 [[v:Q1.r0.销售额]]〔[[see:Q1]]〕[[v:Q1.r0.订单数]] 笔", "华东 4,919.5 212 笔"),
])
def test_other_brackets_holding_only_see_are_dropped(catalog, raw, shown):
    assert render(raw, catalog) == shown
    assert compose(raw, catalog)["markdown"] == shown


@pytest.mark.parametrize("raw", [
    "【注】原样，［1］、〔2026〕、[a] 也是。",
    "空的【】、［］、〔〕、[] 原样，【，】也是。",
    "【证据 Q1】华南居首。",
])
def test_other_brackets_without_see_are_left_alone(catalog, raw):
    assert render(raw, catalog) == raw
    assert compose(raw, catalog)["markdown"] == raw


@pytest.mark.parametrize(("raw", "shown"), [
    # 依据标记的「]」不显示，不当数字：左边真正挨上的是句号、文首
    ("华南居首。[[see:Q1]]（[[see:m:gmv]]）2 月起回升。", "华南居首。2 月起回升。"),
    ("[[see:Q1]]（[[see:Q1]]）2 月", "2 月"),
    ("华南居首。[[see:Q1]] （[[see:m:gmv]]）2 月起回升。", "华南居首。2 月起回升。"),   # 中间的空白同几个依据之间的
    # 一片里最多补一个空格；挨上的本来就隔着空白的不补
    ("212,[[see:Q1]],[[see:m:gmv]],5 个区域", "212, 5 个区域"),
    ("212,（[[see:Q1]]）,[[see:m:gmv]],5 个区域", "212, 5 个区域"),
    ("合计 [[v:Q1.r0.销售额]]（[[see:Q1]]）（[[see:m:gmv]]） 2 月", "合计 4,919.5 2 月"),
])
def test_a_space_is_put_back_once_and_only_where_it_sticks(catalog, raw, shown):
    assert render(raw, catalog) == shown
    assert compose(raw, catalog)["markdown"] == shown


#: 结构化组合：左边一个渲染出数的标记（可能包着粗体），中间 1–3 段依据、只装着依据的括号（几类括号）、空白、星号，
#: 右边以数字、字母或者渲染出数的标记开头
_LEFTS = ["合计 [[v:Q1.r0.销售额]]", "合计 **[[v:Q1.r0.销售额]]**", "合计 __[[v:Q1.r0.销售额]]__", "增幅 [[m:yoy]]",
          "编号 Q1"]
_MIDDLES = ["（[[see:Q1]]）", "([[see:m:gmv]])", "（（[[see:Q1]]））", "【[[see:Q1]]】", "[[[see:m:gmv]]]", "[[see:Q1]]",
            "**", " ", "，"]
_RIGHTS = ["2 月", "[[v:Q1.r0.订单数]] 笔", "**[[v:Q1.r0.订单数]] 笔**", "x 区"]
#: 写作者原本就隔开了两边的中间段：括号（去掉依据之后还看得见）、空白、标点
_SEPARATING = {"（[[see:Q1]]）", "([[see:m:gmv]])", "（（[[see:Q1]]））", "【[[see:Q1]]】", "[[[see:m:gmv]]]", " ", "，"}


def _combos() -> list[tuple[str, str, str]]:
    """写作者隔开了两边、或者中间写了依据的组合（中间只有星号的，是写作者自己把两个数写在了一起，不归这里管）。"""
    middles = [[m] for m in _MIDDLES]
    middles += [[a, b] for a in _MIDDLES for b in _MIDDLES]
    middles += [[a, b, c] for a in _MIDDLES for b in _MIDDLES for c in _MIDDLES]
    return [(left, "".join(middle), right) for left in _LEFTS for right in _RIGHTS for middle in middles
            if any(m in _SEPARATING or "see" in m for m in middle)]


#: 左边那个数渲染出来的字，后面紧跟着数字、字母，或者半角的逗号、句点再跟数字：读成了另一个数、另一个词
_GLUED = re.compile(r"(?:4,919\.5|12\.5%|Q1)(?=[\dA-Za-z]|[,.]\d)")


def test_two_numbers_never_run_together_once_see_is_dropped(catalog):
    """写作者隔开了的两边（括号、空白、标点），或者中间只隔着依据的，收拾依据之后不能挨成一串；也不会多出两个连着的
    空格。"""
    bad = []
    for left, middle, right in _combos():
        raw = f"{left}{middle}{right}。"
        shown = _visible(render(raw, catalog))
        writer = _visible(re.sub(r"\[\[see:[^\]]*\]\]", "", raw))
        if _GLUED.search(shown) or ("  " in shown and "  " not in writer):
            bad.append((raw, shown))
    assert not bad, bad[:10]


@pytest.mark.parametrize(("raw", "shown"), [
    ("华南居首。[[see:Q1]]", "华南居首。"),
    ("华南居首 [[see:Q1]]，华东第二。", "华南居首，华东第二。"),
    # 两个依据中间的逗号后面接着字：它是分句的逗号，不是依据之间的分隔，照旧留着
    ("华南居首 [[see:Q1]]，[[see:m:gmv]]华东第二。", "华南居首，华东第二。"),
    ("华南居首 [[see:Q1]]，", "华南居首，"),                            # 依据后面的逗号是写作者自己的，不动
    ("数据如下：[[see:Q1]]\n| 区域 | 销售额 |", "数据如下：\n| 区域 | 销售额 |"),    # 冒号引出的是下一行
    # 换行不算句末：列表项末尾的「；」、软换行处连着两个分句的「，」是写作者自己的
    ("1. 华东居首；[[see:Q1]]\n2. 华南第二；[[see:Q1]]\n3. 华北第三。[[see:Q1]]",
     "1. 华东居首；\n2. 华南第二；\n3. 华北第三。"),
    ("华东居首，[[see:Q1]]\n华南第二。", "华东居首，\n华南第二。"),
    ("华南居首、华东第二；[[see:Q1]]\n下一行", "华南居首、华东第二；\n下一行"),
    ("- 华东居首；（[[see:Q1]]）\n- 华南第二。", "- 华东居首；\n- 华南第二。"),
])
def test_punctuation_the_writer_put_there_is_kept(catalog, raw, shown):
    assert render(raw, catalog) == shown
    assert compose(raw, catalog)["markdown"] == shown


#: 引出语悬空：「依据」「见」「取自」「来源」「出处」后面（可能隔着冒号、空白）紧跟着句末、停顿标点或括号
_DANGLING = re.compile(r"(?:依据|见|取自|来源|出处)[：:]?[ \t]*(?:[。，、；）)！？,;]|$)")


@pytest.mark.xfail(strict=True, reason="已知限制：依据写在引出语后面时，渲染后引出语悬空。引出语在依据前面，渲染时要去掉"
                                       "它，流式渲染得先攒住每个可能是引出语的字；目前靠核查 agent 的提示词避免这种写法，模型"
                                       "不照做时记一条日志（verify_agent._LEAD_IN_SEE）")
@pytest.mark.parametrize("raw", [
    "结果见：[[see:Q1]]。",
    "依据 [[see:Q1]]。",
    "数据取自 [[see:Q1]]。",
    "结果取自 [[see:Q1]]，完整未截断。",
    "按支付日统计（见 [[see:Q1]]）。",
    "按支付日统计（来源：[[see:Q1]]）。",
    "按状态拆开（status 取值见 [[see:Q1]]）。",
])
def test_see_after_a_lead_in_word_leaves_it_dangling(catalog, raw):
    assert not _DANGLING.search(render(raw, catalog)), render(raw, catalog)


@pytest.mark.parametrize("raw", [
    "空括号（）原样保留，半角 () 也是。",
    "说明（含退款，不含测试账号）；另见 (a, b)。",
    "（，）原样，(;) 也是。",
    "甲，乙、丙；丁：戊。",
])
def test_brackets_and_punctuation_without_see_are_left_alone(catalog, raw):
    assert render(raw, catalog) == raw
    assert compose(raw, catalog)["markdown"] == raw


# --------------------------------------------------------------------------
# 依据仍挂在句子上
# --------------------------------------------------------------------------

def test_the_sentence_still_carries_the_see_that_was_in_brackets(catalog):
    doc = compose("先按状态核对口径（[[see:Q1]]）：已付 [[v:Q1.r0.订单数]] 笔。"
                  "结果一致（[[see:m:gmv]]，原报告未过滤）。", catalog)
    assert doc["markdown"] == "先按状态核对口径：已付 212 笔。结果一致（原报告未过滤）。"
    first, second = [unit for _, unit in iter_units(doc)]
    assert [s["alias"] for s in first["see"]] == ["Q1"] and first["cites"] == ["Q1"]
    assert [s["alias"] for s in second["see"]] == ["m:gmv"] and second["cites"] == ["m:gmv"]
    assert doc["stats"]["see"] == 2 and doc["stats"]["uncited_claims"] == 0
    assert verify_doc(doc, catalog, loader=LOADER)["violations"] == []


# --------------------------------------------------------------------------
# 没有依据写进括号的文档：渲染和内容哈希都和改动之前一样
# --------------------------------------------------------------------------

#: 改动之前（基线 58a6759）组装出来的文档的内容哈希
BEFORE = {
    "plain": ("## 本周概览\n\n本周（[[i:week]]）销售额 [[m:gmv]]（含退款，不含测试账号），同比 [[m:yoy]]；"
              "华南 [[v:Q1.r0.销售额]]（[[v:Q1.r0.订单数]] 笔）、华东 [[v:Q1.r1.销售额]]。\n\n"
              "- 空括号（）原样保留，半角 (a, b) 也是\n- 分隔：甲，乙、丙；丁\n\n"
              "| 区域 | 销售额（元） |\n| --- | --- |\n| 华南（含线上） | [[v:Q1.r0.销售额]] |",
              "49e7a6e4e430f0cbc495b5ab2b5e319f146bbee2c7147d663494922aaf6773ba"),
    "see_at_end": ("本周销售额 [[m:gmv]]（含退款）。[[see:m:gmv]]同比 [[m:yoy]]，增幅放缓 [[see:m:yoy]]。\n\n"
                   "华南居首（[[v:Q1.r0.订单数]] 笔），华东第二。[[see:Q1]]\n- 列表项 [[see:Q1]]\n",
                   "1bb0bc31fffa6de8e906d24022dae7fa3cf9b43aeda47d319498f1593d5a3684"),
}


@pytest.mark.parametrize("name", list(BEFORE))
def test_documents_without_see_in_brackets_hash_as_before(catalog, name):
    raw, digest = BEFORE[name]
    assert content_hash(canonical_json(compose(raw, catalog))) == digest


# --------------------------------------------------------------------------
# 流式渲染：分块输出拼起来仍然等于整篇渲染
# --------------------------------------------------------------------------

def _stream(catalog, raw: str, sizes: list[int]) -> list[str]:
    renderer, out, i = StreamRenderer(catalog, loader=LOADER), [], 0
    for size in sizes:
        if i >= len(raw):
            break
        out.append(renderer.feed(raw[i:i + size]))
        i += size
    if i < len(raw):
        out.append(renderer.feed(raw[i:]))
    out.append(renderer.flush())
    return out


@pytest.mark.parametrize("raw", [
    "结果一致（[[see:Q1]]，原报告未按状态过滤），合计 [[v:Q1.r0.销售额]]（[[see:Q1]]）。下一句",
    "前一句 （[[see:Q1]]）后一句，复算原查询（同 SQL，[[see:Q1]]）：华南居首，[[see:Q1]]。",
    "口径相同（按支付日，[[see:Q1]]，不含退款）。a, [[see:Q1]], b\n华南、[[see:Q1]]\n末尾（[[see:Q1]]）",
    "华东 [[v:Q1.r0.销售额]]（[[see:Q1]]）2 月，合计 [[v:Q1.r0.销售额]]（[[see:Q1]]）[[see:m:gmv]][[v:Q1.r0.订单数]] 笔"
    "，212,[[see:Q1]],5 个",
    "华南居首，（[[see:Q1]]）。华南居首 [[see:Q1]] 。1. 华东居首；[[see:Q1]]\n2. 华南第二，（[[see:Q1]]），华东第二",
    # 粗体、连着写的括号：往两边找挨上的字要越过星号、下划线，流式渲染得把它们和依据一起攒着
    "销售额 **[[v:Q1.r0.销售额]]**（[[see:Q1]]）2 月，华东 [[v:Q1.r0.销售额]]（[[see:Q1]]）（[[see:m:gmv]]）"
    "**[[v:Q1.r0.订单数]] 笔**",
    "华南居首。[[see:Q1]]（[[see:m:gmv]]）2 月，212,[[see:Q1]],[[see:m:gmv]],5 个，合计 [[v:Q1.r0.销售额]] [[see:Q1]]"
    "__2 月__",
    "**[[v:Q1.r0.销售额]]（[[see:Q1]]）**2 月；华南居首，（[[see:Q1]]）（[[see:m:gmv]]）。order_（[[see:Q1]]）_x",
    # 依据直接夹在两个数之间、别的括号（复核第 3 轮）：方括号、波浪线也得和依据一起攒着
    "华东 [[v:Q1.r0.销售额]][[see:Q1]][[v:Q1.r0.订单数]] 笔，[[v:Q1.r0.销售额]][[see:Q1]][[see:m:gmv]]2 月，"
    "**华东 [[v:Q1.r0.销售额]]**[[see:Q1]]2 月",
    "合计 [[v:Q1.r0.销售额]]【[[see:Q1]]】2 月，[[v:Q1.r0.销售额]][[[see:Q1]]]2 月，华南居首，［[[see:Q1]]]。"
    "结果一致【[[see:Q1]]，原报告未过滤】〔[[see:m:gmv]]〕[[v:Q1.r0.订单数]] 笔",
    "~~[[v:Q1.r0.销售额]]~~[[see:Q1]]2 月，[[v:Q1.r0.销售额 ]][[see:Q1]][[v:Q1.r0.订单数]]，a[0] 与 [b] 原样]",
    "分别为 [[v:Q1.r0.销售额]]（[[see:Q1]]）,[[v:Q1.r1.销售额]]，212[[see:Q1]],5 个，[[m:gmv]][[see:Q1]],[[m:gmv]] 元",
])
@pytest.mark.parametrize("size", range(1, 9))
def test_stream_tidies_see_in_brackets_like_the_full_render(catalog, raw, size):
    out = _stream(catalog, raw, [size] * len(raw))
    assert "".join(out) == render(raw, catalog), out
    assert all("[[see:" not in piece for piece in out)


def _corpus(n: int) -> list[str]:
    rng = random.Random(20261006)
    pieces = ["[[see:Q1]]", "[[see:m:gmv]]", "[[v:Q1.r0.销售额]]", "[[m:gmv]]", "（", "）", "(", ")", "，", "、",
              "；", "：", ",", ";", " ", "　", "。", "\n", "文字", "元", "[[", "]]", "[", "2", "x", "%", "！",
              "**", "*", "__", "_", "（[[see:Q1]]）", "【", "】", "［", "］", "〔", "〕", "]", "~~", "【[[see:Q1]]】",
              "[[[see:m:gmv]]]"]
    return ["".join(rng.choice(pieces) for _ in range(rng.randint(1, 18))) for _ in range(n)]


@pytest.mark.parametrize("raw", _corpus(200))
def test_stream_matches_the_full_render_on_random_brackets(catalog, raw):
    rng = random.Random(raw)
    full = render(raw, catalog)
    for _ in range(3):
        sizes = [rng.randint(1, 12) for _ in range(len(raw))]
        assert "".join(_stream(catalog, raw, sizes)) == full


def test_stream_matches_the_full_render_on_the_structured_combos(catalog):
    """结构化组合（数、依据、只装着依据的括号、空白、星号、下划线）逐字、每 4 个字一块地送进去，拼起来和整篇渲染一样。"""
    bad = []
    for left, middle, right in _combos():
        raw = f"{left}{middle}{right}。"
        full = render(raw, catalog)
        bad += [(raw, out) for size in (1, 4) if "".join(out := _stream(catalog, raw, [size] * len(raw))) != full]
    assert not bad, bad[:5]
