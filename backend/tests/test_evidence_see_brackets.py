"""依据标记 [[see:…]] 写进括号、夹在标点中间时的渲染（期 5 真实模型验收 M1）。

[[see:]] 渲染成空，按设计挂在句末。核查 agent（双盲复算、反方审稿、审批质询共用）以前被提示「提到某次查询时写
[[see:Q1]]」，模型就把它写进括号、句中：「结果一致（[[see:Q3]]，原报告 SQL 未按 status 过滤）」「合计
[[v:Q1.r1.金额合计]]（[[see:Q1]]）」，渲染后剩下「（，原报告…）」「（）」，正式运行里随封存永久留着。

- 括号（全角、半角，一对里混用也算）里只有依据的，连括号一起去掉，括号前的空白照 see 前的空白处理
- 依据在括号开头、后面跟着「，」「、」「；」「：」的，标点一起去掉；在括号末尾、前面紧挨着标点的同理
- 依据夹在两个分隔标点中间的，去掉后一个；后面就到句末（句号、换行、全文结尾）、前面或几个依据之间有「，」「、」
  「；」的，去掉这些标点；依据后面、句末前面的标点是写作者自己的，不动
- 括号里、句末前几个依据之间的分隔标点一并去掉；两边都是字时，依据之间的标点可能是分句的标点，照旧留着
- 依据标记本身留在原文里：句子照旧挂着这条依据（see、cites），只是不显示
- 不含依据的括号、没有依据的文档一个字不动：内容哈希和改动之前一样
- 流式渲染分块输出拼起来仍然等于整篇渲染

数字、表名、地名都是虚构的。
"""
from __future__ import annotations

import random

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
    # 前面是分隔标点、后面就到句末：去掉前面那个
    ("华南居首，[[see:Q1]]。", "华南居首。"),
    ("华南居首、华东第二；[[see:Q1]]\n下一行", "华南居首、华东第二\n下一行"),
    ("华南居首，[[see:Q1]]", "华南居首"),
    ("华南居首，[[see:Q1]] 。", "华南居首。"),
    # 几个依据之间的分隔标点
    ("华南居首 [[see:Q1]]，[[see:m:gmv]]。", "华南居首。"),
])
def test_punctuation_left_dangling_by_see_is_dropped(catalog, raw, shown):
    assert render(raw, catalog) == shown
    assert compose(raw, catalog)["markdown"] == shown


@pytest.mark.parametrize(("raw", "shown"), [
    ("华南居首。[[see:Q1]]", "华南居首。"),
    ("华南居首 [[see:Q1]]，华东第二。", "华南居首，华东第二。"),
    # 两个依据中间的逗号后面接着字：它是分句的逗号，不是依据之间的分隔，照旧留着
    ("华南居首 [[see:Q1]]，[[see:m:gmv]]华东第二。", "华南居首，华东第二。"),
    ("华南居首 [[see:Q1]] [[see:m:gmv]] 。", "华南居首 。"),            # 没有悬空的标点：照旧只去掉依据前的空白
    ("华南居首 [[see:Q1]]，", "华南居首，"),                            # 依据后面的逗号是写作者自己的，不动
    ("数据如下：[[see:Q1]]\n| 区域 | 销售额 |", "数据如下：\n| 区域 | 销售额 |"),    # 冒号引出的是下一行
    ("结果见：[[see:Q1]]。", "结果见：。"),          # 冒号前的引出语去不掉：靠提示词让模型写在句末
    ("依据 [[see:Q1]]。", "依据。"),
])
def test_see_outside_brackets_without_dangling_pauses_renders_as_before(catalog, raw, shown):
    assert render(raw, catalog) == shown


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
])
@pytest.mark.parametrize("size", range(1, 9))
def test_stream_tidies_see_in_brackets_like_the_full_render(catalog, raw, size):
    out = _stream(catalog, raw, [size] * len(raw))
    assert "".join(out) == render(raw, catalog), out
    assert all("[[see:" not in piece for piece in out)


def _corpus(n: int) -> list[str]:
    rng = random.Random(20261006)
    pieces = ["[[see:Q1]]", "[[see:m:gmv]]", "[[v:Q1.r0.销售额]]", "[[m:gmv]]", "（", "）", "(", ")", "，", "、",
              "；", "：", ",", ";", " ", "　", "。", "\n", "文字", "元", "[[", "]]", "["]
    return ["".join(rng.choice(pieces) for _ in range(rng.randint(1, 18))) for _ in range(n)]


@pytest.mark.parametrize("raw", _corpus(200))
def test_stream_matches_the_full_render_on_random_brackets(catalog, raw):
    rng = random.Random(raw)
    full = render(raw, catalog)
    for _ in range(3):
        sizes = [rng.randint(1, 12) for _ in range(len(raw))]
        assert "".join(_stream(catalog, raw, sizes)) == full
