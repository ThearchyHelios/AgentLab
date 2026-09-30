"""证据内核：引用标记的解析、目录、渲染、切块切句切片段、裸数字，以及独立复核。

这一层是纯函数，报告节点自查、出口契约复核、证据接口都用它。所以这里守的是
「同样的输入永远得到同样的结果」和「复核不信任文档自己的说法」两件事。
"""
from __future__ import annotations

import copy

import pytest

from app.core.artifact_store import canonical_json, content_hash
from app.engine.evidence import (
    DOC_SCHEMA,
    LATER_REASON,
    MARKER_MAX,
    RenderError,
    StreamRenderer,
    build_catalog,
    catalog_prompt,
    compose_doc,
    describe_violations,
    find_segment,
    input_eid,
    iter_segments,
    make_eid,
    parse_markers,
    render_markers,
    render_number,
    verify_doc,
)

# --------------------------------------------------------------------------
# 夹具：一张口径卡的产出（形状同 metrics 节点），外加运行输入
# --------------------------------------------------------------------------

ART = "a" * 64


def metric(mid, name, value, unit="", decimals=None, fmt="thousands", status="ok"):
    return {"id": mid, "name": name, "unit": unit, "value": value, "decimals": decimals,
            "format": fmt, "status": status, "expression": "…"}


CARD = {
    "kind": "metric_set", "caliber": "周报口径", "caliber_version": "v2", "artifact": ART,
    "metrics": [
        metric("gmv", "销售额", 45678.5, "元", 1),
        metric("wow", "环比增幅", 8.7, "%", 1, "plain"),
        metric("orders", "订单数", 1234, "单"),
        metric("aov", "客单价", 37, "元", 1),
        metric("refund_rate", "退款率", 0.0235, "", 4, "percent_of_ratio"),
        metric("big", "大数", 45678.5),
        metric("gone", "缺数", None, status="missing_input"),
    ],
}
LEDGER = [{"kind": "metric_set", "node_id": "caliber", "exec": 1, "artifact": ART,
           "caliber": "周报口径", "version": "v2", "metrics": [m["id"] for m in CARD["metrics"]]}]
NODES = {"caliber": CARD, "fetch": {"text": "上游笔记 999"}}
INPUTS = {"week": "2026-W37", "region": "华东", "rows": [1, 2]}


@pytest.fixture
def catalog():
    return build_catalog(nodes=NODES, ledger=LEDGER, inputs=INPUTS)


def texts(doc):
    return [s["text"] for s in iter_segments(doc)]


def codes(doc):
    return [v["code"] for v in doc["violations"]]


# --------------------------------------------------------------------------
# 标记语法
# --------------------------------------------------------------------------


def test_markers_parse():
    text = ("销售额 [[m:gmv]]，折合 [[m:gmv|万]]。[[see:m:gmv, m:wow,Q4]] 周期 [[i:week]]，"
            "[[v:Q4.r0.amount]] [[t:orders]] [[c:orders.amount]] [[q:K2|原话。]] "
            "[[table:Q4 cols=a,b rows=0-4]] [[metric:gmv]] [[注意]]")
    found = parse_markers(text)
    assert [(m["kind"], m.get("ref")) for m in found] == [
        ("m", "gmv"), ("m", "gmv"), ("see", None), ("i", "week"), ("v", "Q4.r0.amount"),
        ("t", "orders"), ("c", "orders.amount"), ("q", "K2"), ("table", "Q4"), ("metric", "gmv")]
    assert found[1]["conv"] == "万"
    assert found[2]["refs"] == ["m:gmv", "m:wow", "Q4"]
    assert found[7]["quote"] == "原话。"
    assert found[8]["options"] == {"cols": "a,b", "rows": "0-4"}
    for m in found:
        assert text[m["start"]:m["end"]] == m["raw"]


# --------------------------------------------------------------------------
# 目录与 eid
# --------------------------------------------------------------------------


def test_catalog_entries_and_stable_eids(catalog):
    gmv = catalog["m:gmv"]
    assert gmv["kind"] == "metric" and gmv["node_id"] == "caliber" and gmv["artifact"] == ART
    assert gmv["locator"] == {"metric": "gmv"} and gmv["caliber"] == "周报口径" and gmv["version"] == "v2"
    assert gmv["rendered"] == "45,678.5元" and gmv["label"] == "销售额 = 45,678.5元"
    assert gmv["eid"] == make_eid("metric", ART, {"metric": "gmv"})
    assert gmv["eid"].startswith("ev:metric:") and len(gmv["eid"]) == len("ev:metric:") + 16
    assert catalog["i:week"]["value"] == "2026-W37" and catalog["i:week"]["kind"] == "input"
    assert "i:rows" not in catalog          # 容器型输入没法写进一句话
    # 同一输入，eid 一个字不差
    again = build_catalog(nodes=copy.deepcopy(NODES), ledger=copy.deepcopy(LEDGER), inputs=dict(INPUTS))
    assert {k: v["eid"] for k, v in again.items()} == {k: v["eid"] for k, v in catalog.items()}
    # 内容变了，eid 跟着变
    other = build_catalog(nodes={"caliber": {**CARD, "artifact": "b" * 64}},
                          ledger=[{**LEDGER[0], "artifact": "b" * 64}], inputs=INPUTS)
    assert other["m:gmv"]["eid"] != gmv["eid"]


def test_catalog_respects_metrics_from_and_allowed():
    second = {**CARD, "caliber": "月报口径", "artifact": "c" * 64,
              "metrics": [metric("gmv", "销售额", 1.0), metric("mau", "月活", 9)]}
    nodes = {**NODES, "monthly": second}
    ledger = [*LEDGER, {**LEDGER[0], "node_id": "monthly", "artifact": "c" * 64}]
    both = build_catalog(nodes=nodes, ledger=ledger)
    # 两张卡都有 gmv：不许按先来后到默认给一个，只登记带卡名的写法
    assert "m:gmv" not in both
    assert both["m:caliber.gmv"]["value"] == 45678.5 and both["m:monthly.gmv"]["value"] == 1.0
    assert both["m:mau"]["node_id"] == "monthly"
    only = build_catalog(nodes=nodes, ledger=ledger, metrics_from=["monthly"])
    assert only["m:gmv"]["node_id"] == "monthly" and "m:orders" not in only
    upstream = build_catalog(nodes=nodes, ledger=ledger, allowed={"caliber"})
    assert "m:mau" not in upstream and upstream["m:gmv"]["node_id"] == "caliber"


# --------------------------------------------------------------------------
# 渲染
# --------------------------------------------------------------------------


def test_rendering(catalog):
    raw = ("本周（[[i:week]]）销售额 [[m:gmv]]，环比 [[m:wow]]；订单 [[m:orders]]，客单价 [[m:aov]]。"
           "[[see:m:gmv,m:wow]]\n\n退款率 [[m:refund_rate]]，大数折合 [[m:big|万]]，销售额折合 [[m:gmv|万]]。")
    doc = compose_doc(raw, catalog, node_id="write", run_id="r1")
    assert doc["schema"] == DOC_SCHEMA and doc["node_id"] == "write" and doc["run_id"] == "r1"
    assert doc["markdown"] == ("本周（2026-W37）销售额 45,678.5元，环比 8.7%；订单 1,234单，客单价 37.0元。"
                               "\n\n退款率 2.35%，大数折合 4.57万，销售额折合 4.57万元。")
    assert doc["violations"] == [] and doc["stats"]["numbers_cited"] == 7
    seg = next(s for s in iter_segments(doc) if s["text"] == "45,678.5元")
    assert seg["kind"] == "number" and seg["state"] == "deterministic" and seg["ref"] == "m:gmv"
    assert seg["cite"]["status"] == "resolved" and seg["cite"]["value"] == 45678.5
    assert seg["cite"]["eid"] == catalog["m:gmv"]["eid"] and seg["cite"]["alias"] == "m:gmv"
    week = next(s for s in iter_segments(doc) if s["text"] == "2026-W37")
    assert week["kind"] == "value" and week["ref"] == "i:week"


def _one_metric_catalog(value, unit="", decimals=None, fmt="thousands"):
    card = {**CARD, "metrics": [metric("small", "小值", value, unit, decimals, fmt)]}
    return build_catalog(nodes={"caliber": card}, ledger=LEDGER)


@pytest.mark.parametrize("value, unit, decimals, fmt, conv", [
    (29, "单", None, "thousands", "万"),            # 0.0029万 → 显示成「0万单」
    (45678.5, "元", 1, "thousands", "亿"),          # 0.000456785亿 → 「0亿元」
    (0.004, "", None, "thousands", "int"),
    (-0.004, "", None, "thousands", ".2"),
    (0.00004, "", None, "thousands", "pct"),        # 0.004% 按一位小数是 0.0%
    (0.00004, "", 4, "percent_of_ratio", "万"),     # 比率不能换算（这条本来就解析不了）
])
def test_conversions_never_show_a_nonzero_value_as_zero(value, unit, decimals, fmt, conv):
    """系统渲染的数带着「有出处」的实线：把 29 单换算成「0万单」再标成有出处，比模型
    自己编一个数还糟——读者没有任何理由怀疑它。显示成 0 的非零值一律算解析不了。"""
    cat = _one_metric_catalog(value, unit, decimals, fmt)
    doc = compose_doc(f"数值 [[m:small|{conv}]]。", cat)
    seg = next(s for s in iter_segments(doc) if s.get("ref"))
    assert seg["state"] == "none" and seg["issue"] == "unresolved_ref", seg
    assert seg["cite"]["status"] == "unresolved"
    assert codes(doc) == ["unresolved_ref"]
    assert seg["text"] == f"⟦?m:small|{conv}⟧"
    if fmt != "percent_of_ratio":
        assert "显示为 0" in seg["cite"]["reason"] and "精度不够" in seg["cite"]["reason"]


def test_values_that_cannot_be_shown_are_flagged_in_the_catalog_and_prompt():
    cat = _one_metric_catalog(1e-12)
    assert cat["m:small"]["rendered"] == "—" and cat["m:small"]["value"] == 1e-12
    assert "[[m:small]] 小值：按口径卡的格式显示不出来，不要引用" in catalog_prompt(cat)
    doc = compose_doc("小值 [[m:small]]。", cat)
    assert codes(doc) == ["unresolved_ref"] and "显示为 0" in doc["violations"][0]["message"]


def test_zero_guard_only_fires_for_nonzero_values():
    assert render_number(0, conv="万") == "0万"
    assert render_number(0.0, conv="int") == "0"
    assert render_number(-0.0, conv=".2") == "0.00"
    assert render_number(50, unit="单", conv="万") == "0.01万单"     # 四舍五入到 0.01，不是 0
    assert render_number(0.005, conv=".2") == "0.01"
    with pytest.raises(RenderError, match="显示为 0"):
        render_number(1e-12)                          # 不写小数位时最多留 10 位
    with pytest.raises(RenderError, match="显示为 0"):
        render_number(29, decimals=-2)                # 口径卡自己的小数位把它抹成 0 也一样


@pytest.mark.parametrize("value, expected", [
    (1e55, f"{10 ** 55:,}"),
    (9e14 ** 4, f"{6561 * 10 ** 56:,}"),
    (10 ** 70 + 1, f"{10 ** 70 + 1:,}"),              # 超过 60 位有效数字也不能被悄悄舍入
    (-(10 ** 65) - 7, f"{-(10 ** 65) - 7:,}"),
])
def test_huge_values_render_exactly(value, expected):
    assert render_number(value) == expected


def test_huge_values_with_conversions_and_decimals():
    assert render_number(10 ** 40 + 12345, conv="万") == f"{10 ** 36 + 1:,}.23万"
    assert render_number(10 ** 40 + 1, conv="pct") == f"{(10 ** 40 + 1) * 100:,}.0%"
    assert render_number(1e55, decimals=2) == f"{10 ** 55:,}.00"
    assert render_number(12345.678, decimals=-2) == "12,300"
    with pytest.raises(RenderError):
        render_number(10 ** 5000)                     # 大到不像话：说清楚，而不是抛 InvalidOperation
    with pytest.raises(RenderError):
        render_number(1.5, decimals=10 ** 6)


def test_input_eids_are_derived_from_the_value():
    w37 = build_catalog(nodes={}, inputs={"week": "2026-W37"})["i:week"]
    w38 = build_catalog(nodes={}, inputs={"week": "2026-W38"})["i:week"]
    again = build_catalog(nodes={}, inputs={"week": "2026-W37"})["i:week"]
    assert w37["eid"] != w38["eid"] and w37["eid"] == again["eid"]
    assert w37["eid"] == input_eid("week", "2026-W37")
    # 目录条目自己就能复算：值被改过，eid 就对不上
    assert w37["eid"] == make_eid("input", content_hash(canonical_json(w37["value"])), w37["locator"])
    # 同一个值换一个字段名也是另一件证据
    assert build_catalog(nodes={}, inputs={"period": "2026-W37"})["i:period"]["eid"] != w37["eid"]


@pytest.mark.parametrize("empty", [[], ""])
def test_empty_metrics_from_means_every_card(empty):
    """validate_graph 把 metrics_from=[] 当成「所有上游口径卡」，目录要是当成「一张都不要」，
    报告就一个数都写不出来，而画布上什么都不提示。两边必须同一个意思。"""
    full = build_catalog(nodes=NODES, ledger=LEDGER, metrics_from=None)
    assert build_catalog(nodes=NODES, ledger=LEDGER, metrics_from=empty) == full
    assert "m:gmv" in full


def test_unresolved_references(catalog):
    doc = compose_doc("销售额 [[m:gmvx]]，缺数 [[m:gone]]，单元格 [[v:Q4.r0.amount]]，"
                      "输入 [[i:nope]]，换算 [[m:wow|pct]]，[[metric:gmv]]。", catalog)
    assert doc["markdown"] == ("销售额 ⟦?m:gmvx⟧，缺数 —，单元格 ⟦?v:Q4.r0.amount⟧，"
                               "输入 ⟦?i:nope⟧，换算 ⟦?m:wow|pct⟧，⟦?metric:gmv⟧。")
    bad = [v for v in doc["violations"] if v["code"] == "unresolved_ref"]
    assert [v["ref"] for v in bad] == ["m:gmvx", "m:gone", "v:Q4.r0.amount", "i:nope", "m:wow|pct",
                                       "metric:gmv"]
    reasons = {v["ref"]: v["message"] for v in bad}
    assert "gmvx" in reasons["m:gmvx"]
    assert "没有值" in reasons["m:gone"]
    assert "查询结果 Q4 不存在" in reasons["v:Q4.r0.amount"]      # 单元格二期起支持，这份目录里没有 Q4
    assert "百分数" in reasons["m:wow|pct"]
    for s in iter_segments(doc):
        if s.get("ref"):
            assert s["state"] == "none" and s["cite"]["status"] == "unresolved" and s["issue"] == "unresolved_ref"
    assert doc["stats"]["unresolved"] == 6 and doc["stats"]["numbers_cited"] == 0


def test_see_refs_are_checked_but_not_rendered(catalog):
    doc = compose_doc("销售额见口径卡。[[see:m:gmv,i:week,gmv,m:nope,t:refunds]]下面看退款。", catalog)
    assert doc["markdown"] == "销售额见口径卡。下面看退款。"
    first = doc["blocks"][0]["units"][0]
    assert [c["alias"] for c in first["see"] if c["status"] == "resolved"] == ["m:gmv", "i:week", "m:gmv"]
    assert first["cites"] == ["m:gmv", "i:week"]
    bad = {v["ref"]: v["message"] for v in doc["violations"]}
    assert set(bad) == {"m:nope", "t:refunds"} and LATER_REASON in bad["t:refunds"]
    assert doc["blocks"][0]["units"][1]["cites"] == []


# --------------------------------------------------------------------------
# 裸数字
# --------------------------------------------------------------------------


def test_bare_numbers_are_violations_with_offsets(catalog):
    doc = compose_doc("销售额 [[m:gmv]]，增长 12%，客户两千五百家。", catalog)
    bare = [v for v in doc["violations"] if v["code"] == "uncited_number"]
    assert [v["text"] for v in bare] == ["12%", "两千五百"]
    for v in bare:
        assert doc["markdown"][v["span"][0]:v["span"][1]] == v["text"]
        assert v["unit"] == "u0" and v["segment"]
    seg = next(s for s in iter_segments(doc) if s["text"] == "12%")
    assert seg["kind"] == "number" and seg["state"] == "none" and seg["issue"] == "uncited_number"
    assert "ref" not in seg
    assert doc["stats"]["uncited_numbers"] == 2 and doc["stats"]["numbers"] == 3


def test_dates_weeks_structural_and_allowed_numbers_pass(catalog):
    doc = compose_doc("2026-09-27 发布，覆盖 2026-W37，2026年9月的数据。前 3 名、第 2 季度、Top 5、"
                      "从 3 个方面看，时间 10:30。客单价 37 元，编号 120。原因有两点：1、价格（2）渠道。",
                      catalog, allow_numbers=["37", "120"])
    assert codes(doc) == [], doc["violations"]
    # 结构性数字限 20 以内：前 30 名就得有出处
    doc = compose_doc("前 30 名。", catalog)
    assert [v["text"] for v in doc["violations"]] == ["30"]


def test_numbers_in_structure_do_not_count_and_cannot_hide(catalog):
    """行首序号按出具校验的规则放过（1–2 位）；再大的序号前端照样显示成 <ol start=100>，
    不能借「这是列表语法」躲过裸数字检查。"""
    raw = ("1. 第一项 [[m:orders]]\n2. 第二项 7 单\n\n| 区域 | 金额 |\n| --- | --- |\n| 甲 | 8 |\n\n"
           "100. 编号从这里开始的列表\n101. 下一项")
    doc = compose_doc(raw, catalog)
    assert [v["text"] for v in doc["violations"]] == ["7", "8", "100", "101"]
    assert doc["blocks"][-1]["start"] == 100
    assert not INTEGRITY & set(codes(doc))
    # 违规指向的是那个序号所在的结构片段，偏移切得回原字
    for v in doc["violations"][2:]:
        assert doc["markdown"][v["span"][0]:v["span"][1]] == v["text"]
        seg = find_segment(doc, v["segment"])[2]
        assert seg["kind"] == "structural" and v["text"] in seg["text"]


@pytest.mark.parametrize("raw, bare", [
    ("45678. 本周销售额创新高", ["45678"]),
    ("段落\n\n98765) 订单", ["98765"]),
    ("12. 序号照旧放过\n13. 下一项", []),
    ("  3) 缩进的序号", []),
    ("```45678\nx\n```", ["45678"]),        # 代码块的语言标签显示在块头上
    ("```py3\nx\n```", []),                 # 和正文同一条规则：v2 / py3 这类标识符不算数字
    ("```\nx\n```", []),
    ("1234567890. 很长的序号", ["1234567890"]),   # 切块认它是列表，复核也得认它是语法
    # 表格第一格不在行首：竖线涂白后不能让「12.」沾行首序号免检的光
    ("| 序号 | 区域 |\n| --- | --- |\n| 12. | 华东 |", ["12"]),
    ("> 3. 引用里的序号", []),
])
def test_ordinals_and_fence_labels_follow_the_prose_rules(catalog, raw, bare):
    doc = compose_doc(raw, catalog)
    assert [v["text"] for v in doc["violations"] if v["code"] == "uncited_number"] == bare
    assert not INTEGRITY & set(codes(doc)), doc["violations"]
    again = verify_doc(copy.deepcopy(doc), catalog)
    assert again["violations"] == doc["violations"]


# --------------------------------------------------------------------------
# 切块、切句、切片段
# --------------------------------------------------------------------------

REPORT = """## 本周概览
本周（[[i:week]]）销售额 [[m:gmv]]，环比 [[m:wow]]。[[see:m:gmv,m:wow]]增长主要来自新客首单。下面看退款情况。

- 订单 [[m:orders]]
  （含预售）
- 客单价 **[[m:aov]]**

| 指标 | 值 |
| --- | :-: |
| 退款率 | [[m:refund_rate]] |
| 大数 | [[m:big|万]] |

> 口径说明：[[i:region]]。
> 第二行。

```
code 块
```

---"""


def test_segments_tile_the_markdown(catalog):
    doc = compose_doc(REPORT, catalog)
    joined = "".join(texts(doc))
    assert joined == doc["markdown"]
    pos = 0
    for seg in iter_segments(doc):
        assert seg["span"][0] == pos and doc["markdown"][seg["span"][0]:seg["span"][1]] == seg["text"]
        pos = seg["span"][1]
    assert pos == len(doc["markdown"])
    assert doc["violations"] == [], doc["violations"]


def test_doc_is_json_and_content_addressable(catalog):
    """报告节点要把文档 put_json 成 report_doc 工件：必须能序列化，同样的原文得到同样的哈希。"""
    from app.core.artifact_store import canonical_json, content_hash

    first = content_hash(canonical_json(compose_doc(REPORT, catalog, node_id="write", run_id="r1")))
    fresh = build_catalog(nodes=copy.deepcopy(NODES), ledger=copy.deepcopy(LEDGER), inputs=dict(INPUTS))
    assert content_hash(canonical_json(compose_doc(REPORT, fresh, node_id="write", run_id="r1"))) == first


def test_blocks_units_and_kinds(catalog):
    doc = compose_doc(REPORT, catalog)
    blocks = doc["blocks"]
    assert [b["type"] for b in blocks] == ["heading", "paragraph", "list", "table", "quote", "code", "hr"]
    assert [b["id"] for b in blocks] == [f"b{i}" for i in range(7)]
    heading, para, lst, table, quote, code, hr = blocks
    assert heading["level"] == 2 and heading["units"][0]["kind"] == "heading"
    h_unit = heading["units"][0]
    assert doc["markdown"][h_unit["span"][0]:h_unit["span"][1]] == "本周概览"
    # 句子按句号切；[[see:]] 挂在它前面那一句；连接性的话单独成一句
    sentences = [doc["markdown"][u["span"][0]:u["span"][1]] for u in para["units"]]
    assert sentences == ["本周（2026-W37）销售额 45,678.5元，环比 8.7%。", "增长主要来自新客首单。",
                         "下面看退款情况。"]
    assert [u["kind"] for u in para["units"]] == ["claim", "claim", "connective"]
    assert para["units"][0]["cites"] == ["i:week", "m:gmv", "m:wow"]
    # 列表每项一个 unit，续行接在同一项里
    assert lst["ordered"] is False and len(lst["units"]) == 2
    first_item = "".join(s["text"] for s in lst["units"][0]["segments"] if s["kind"] != "structural")
    assert first_item == "订单 1,234单\n（含预售）"
    # **[[m:aov]]** 归一成 strong，正文里不留星号
    aov = next(s for s in iter_segments(doc) if s.get("ref") == "m:aov")
    assert aov["strong"] is True and "**" not in doc["markdown"]
    # 表格每格一个 unit，带行列；表头 row = -1
    locs = [(u["loc"]["row"], u["loc"]["col"]) for u in table["units"]]
    assert locs == [(-1, 0), (-1, 1), (0, 0), (0, 1), (1, 0), (1, 1)]
    assert any(s.get("ref") == "m:big|万" for s in table["units"][5]["segments"])
    assert [u["kind"] for u in quote["units"]] == ["claim", "connective"]
    assert code["units"][0]["kind"] == "code" and hr["units"][0]["kind"] == "connective"
    # 编号全局连续
    unit_ids = [u["id"] for b in blocks for u in b["units"]]
    assert unit_ids == [f"u{i}" for i in range(len(unit_ids))]
    seg_ids = [s["id"] for s in iter_segments(doc)]
    assert seg_ids == [f"s{i}" for i in range(len(seg_ids))]
    block, unit, seg = find_segment(doc, aov["id"])
    assert block["id"] == "b2" and unit["id"] == lst["units"][1]["id"] and seg is aov


def test_bold_around_markers_becomes_strong_segments(catalog):
    """**销售额 [[m:gmv]]**：数字切成单独的片段后，两边各剩半对星号，前端只能原样显示。
    星号去掉、里面的每一段都标 strong；不含标记的粗体原样留在文字里。"""
    doc = compose_doc("前面 **销售额 [[m:gmv]]** 后面，__[[m:refund_rate]]__，**重点** 不动。", catalog)
    assert doc["markdown"] == "前面 销售额 45,678.5元 后面，2.35%，**重点** 不动。"
    flags = [(s["text"], bool(s.get("strong"))) for s in iter_segments(doc)]
    assert flags == [("前面 ", False), ("销售额 ", True), ("45,678.5元", True), (" 后面，", False),
                     ("2.35%", True), ("，**重点** 不动。", False)]
    assert doc["violations"] == []


def test_sentence_ends_inside_code_links_bold_and_markers_do_not_split(catalog):
    doc = compose_doc("看 `x. y` 和 [链接](http://a.b/?q=1) 还有 **重点。强调** 以及 [[q:K2|原话。]] 结束。第二句。",
                      catalog)
    sentences = [doc["markdown"][u["span"][0]:u["span"][1]] for u in doc["blocks"][0]["units"]]
    assert sentences == ["看 `x. y` 和 [链接](http://a.b/?q=1) 还有 **重点。强调** 以及 ⟦?q:K2|原话。⟧ 结束。",
                         "第二句。"]
    # 链接地址以问号结尾：问号在链接里，不能因为它后面紧跟链接的右括号就在链接后面断句
    doc = compose_doc("见[文档](http://a.b/?) 然后继续。", catalog)
    assert len(doc["blocks"][0]["units"]) == 1


def test_offsets_are_code_points(catalog):
    doc = compose_doc("😀🎉 销售额 [[m:gmv]]，另有 7 单。", catalog)
    seg = next(s for s in iter_segments(doc) if s.get("ref") == "m:gmv")
    assert seg["span"] == [7, 16] and doc["markdown"][7:16] == "45,678.5元"
    assert doc["violations"][0]["span"] == [20, 21] and doc["markdown"][20] == "7"


def test_pipes_inside_markers_do_not_split_table_cells(catalog):
    doc = compose_doc("| a | b |\n| - | - |\n| [[m:gmv|万]] | x |", catalog)
    cells = doc["blocks"][0]["units"]
    assert [u["loc"] for u in cells][2:] == [{"row": 0, "col": 0}, {"row": 0, "col": 1}]
    assert doc["markdown"] == "| a | b |\n| - | - |\n| 4.57万元 | x |"


# --------------------------------------------------------------------------
# verify_doc：独立复核，不信任文档自己的说法
# --------------------------------------------------------------------------


def test_verify_doc_agrees_with_compose(catalog):
    doc = compose_doc(REPORT + "\n\n另有 7 单。", catalog)
    again = verify_doc(copy.deepcopy(doc), catalog)
    assert again["violations"] == doc["violations"] and again["stats"] == doc["stats"]
    assert again["ok"] is False and [v["code"] for v in again["violations"]] == ["uncited_number"]


def test_verify_doc_catches_tampering(catalog):
    doc = compose_doc("销售额 [[m:gmv]]，订单 [[m:orders]]。", catalog)
    assert verify_doc(doc, catalog)["ok"] is True

    # 1) 数字片段的字被改了：重渲染对不上
    forged = copy.deepcopy(doc)
    seg = next(s for s in iter_segments(forged) if s.get("ref") == "m:gmv")
    shift = len("99,999.9元") - len(seg["text"])
    old_end = seg["span"][1]
    forged["markdown"] = forged["markdown"].replace("45,678.5元", "99,999.9元")
    seg["text"], seg["span"][1] = "99,999.9元", seg["span"][1] + shift
    for s in iter_segments(forged):
        if s["span"][0] >= old_end:
            s["span"] = [s["span"][0] + shift, s["span"][1] + shift]
    assert "render_mismatch" in [v["code"] for v in verify_doc(forged, catalog)["violations"]]

    # 2) 把裸数字塞进文字片段、又不标出来：复核自己去正文里找
    hidden = copy.deepcopy(doc)
    last = list(iter_segments(hidden))[-1]
    last["text"] += "另有 7 单"
    hidden["markdown"] += "另有 7 单"
    last["span"][1] = len(hidden["markdown"])
    assert [v["code"] for v in verify_doc(hidden, catalog)["violations"]] == ["uncited_number"]

    # 3) 片段接不上正文
    broken = copy.deepcopy(doc)
    broken["markdown"] = broken["markdown"] + "尾巴"
    assert "segment_mismatch" in [v["code"] for v in verify_doc(broken, catalog)["violations"]]

    # 3b) 中间一段的字和它记的位置对不上 / 少了一段
    swapped = copy.deepcopy(doc)
    next(s for s in iter_segments(swapped) if s["kind"] == "text")["text"] = "换掉的"
    assert "segment_mismatch" in [v["code"] for v in verify_doc(swapped, catalog)["violations"]]
    dropped = copy.deepcopy(doc)
    dropped["blocks"][0]["units"][0]["segments"].pop(1)
    assert "segment_mismatch" in [v["code"] for v in verify_doc(dropped, catalog)["violations"]]

    # 4) 结构片段里夹带数字
    smuggled = copy.deepcopy(doc)
    first = next(iter_segments(smuggled))
    smuggled["markdown"] = "12345" + smuggled["markdown"]
    for s in iter_segments(smuggled):
        s["span"] = [s["span"][0] + 5 if s is not first else 0, s["span"][1] + 5]
    first["text"] = "12345" + first["text"]
    first["kind"] = "structural"
    assert "structural_text" in [v["code"] for v in verify_doc(smuggled, catalog)["violations"]]

    # 5) 目录里的 eid 被换了
    fake = copy.deepcopy(doc)
    next(s for s in iter_segments(fake) if s.get("ref") == "m:gmv")["cite"]["eid"] = "ev:metric:0000000000000000"
    assert "eid_mismatch" in [v["code"] for v in verify_doc(fake, catalog)["violations"]]

    # 6) 状态标错：没有出处的地方标成「有出处」，界面就会画成确定的实线
    lying = copy.deepcopy(compose_doc("销售额 [[m:gmv]]，另有 7 单。", catalog))
    bare = next(s for s in iter_segments(lying) if s.get("issue") == "uncited_number")
    bare["state"] = "deterministic"
    next(s for s in iter_segments(lying) if s["kind"] == "text")["state"] = "deterministic"
    assert sorted(v["code"] for v in verify_doc(lying, catalog)["violations"]) \
        == ["state_mismatch", "state_mismatch", "uncited_number"]

    # 7) 不认识的文档
    assert [v["code"] for v in verify_doc({**doc, "schema": "x"}, catalog)["violations"]][:1] == ["bad_schema"]


def test_verify_doc_uses_the_catalog_it_is_given(catalog):
    """出口复核拿自己重建的目录：口径卡的值变了，文档里的旧数字就对不上。"""
    doc = compose_doc("销售额 [[m:gmv]]。", catalog)
    changed = build_catalog(
        nodes={"caliber": {**CARD, "metrics": [metric("gmv", "销售额", 1.5, "元", 1)]}},
        ledger=LEDGER, inputs=INPUTS)
    assert [v["code"] for v in verify_doc(doc, changed)["violations"]] == ["render_mismatch"]
    assert [v["code"] for v in verify_doc(doc, {})["violations"]] == ["unresolved_ref"]


# --------------------------------------------------------------------------
# 流式渲染
# --------------------------------------------------------------------------


@pytest.mark.parametrize("cut", range(1, 12))
def test_stream_renderer_never_leaks_raw_markers(catalog, cut):
    raw = "销售额 [[m:gmv]]，折合 [[m:gmv|万]]。[[see:m:gmv]] 未知 [[m:x]] 字面 [[ 和 [ 结束"
    chunks = [raw[i:i + cut] for i in range(0, len(raw), cut)]
    renderer = StreamRenderer(catalog)
    out = [renderer.feed(c) for c in chunks] + [renderer.flush()]
    for piece in out:
        assert "[[m:" not in piece and "[[see:" not in piece, out
    assert "".join(out) == render_markers(raw, catalog)
    assert "".join(out) == "销售额 45,678.5元，折合 4.57万元。 未知 ⟦?m:x⟧ 字面 [[ 和 [ 结束"


@pytest.mark.parametrize("body", [
    "x" * 170,                                      # 比能缓冲的还长：两边都当普通文字
    "x" * (MARKER_MAX - len("[[m:]]")),              # 恰好到上限：两边都当标记
    "x" * (MARKER_MAX - len("[[m:]]") + 1),          # 超出一个字
    "gmv|万" + " " * (MARKER_MAX - len("[[m:gmv|万]]")),
])
def test_stream_output_equals_the_full_render_for_markers_of_any_length(catalog, body):
    raw = f"a [[m:{body}]] b [[see:{body}]] c"
    full = render_markers(raw, catalog)
    for size in (1, 2, 7, 50, len(raw)):
        renderer = StreamRenderer(catalog)
        out = [renderer.feed(raw[i:i + size]) for i in range(0, len(raw), size)] + [renderer.flush()]
        assert "".join(out) == full, (size, out)
    over = len(f"[[m:{body}]]") > MARKER_MAX
    assert (f"[[m:{body}]]" in full) is over        # 超长的不是标记，原样留着
    assert all(len(m["raw"]) <= MARKER_MAX for m in parse_markers(raw))


def _stream_corpus(n: int) -> list[str]:
    import random

    rng = random.Random(20260929)
    pieces = ["[[m:gmv]]", "[[m:gmv|万]]", "[[see:m:wow]]", "[[", "]]", "[", "]", "\n", "文字", " ",
              "[[m:" + "y" * 150, "[[see:" + "z" * 160 + "]]", "[[i:week]]", "[[m:x" + "q" * 155 + "]]"]
    return ["".join(rng.choice(pieces) for _ in range(rng.randint(1, 12))) for _ in range(n)]


@pytest.mark.parametrize("raw", _stream_corpus(120))
def test_stream_renderer_matches_render_markers_on_random_input(catalog, raw):
    import random

    rng = random.Random(raw)
    for _ in range(3):
        renderer, out, i = StreamRenderer(catalog), [], 0
        while i < len(raw):
            step = rng.randint(1, 40)
            out.append(renderer.feed(raw[i:i + step]))
            i += step
        out.append(renderer.flush())
        assert "".join(out) == render_markers(raw, catalog)


def test_stream_renderer_releases_a_long_unclosed_bracket(catalog):
    renderer = StreamRenderer(catalog)
    first = renderer.feed("开头 [[" + "很长的正文" * 40)
    assert first.startswith("开头 [[")     # 不会一直憋着不发
    assert renderer.feed("\n下一行") and renderer.flush() == ""


# --------------------------------------------------------------------------
# 给报告节点用的辅助
# --------------------------------------------------------------------------


def test_catalog_prompt_and_violation_text(catalog):
    prompt = catalog_prompt(catalog)
    assert "[[m:gmv]]" in prompt and "45,678.5元" in prompt and "周报口径 @ v2" in prompt
    assert "[[i:week]]" in prompt and "2026-W37" in prompt
    assert "[[m:gone]]" in prompt and "没有值" in prompt
    doc = compose_doc("增长 12%，[[m:gmvx]]。", catalog)
    text = describe_violations(doc["violations"])
    assert "12%" in text and "gmvx" in text


# --------------------------------------------------------------------------
# 性质：什么样的原文进来，片段都铺满正文，组装和复核的结论一致，自己不制造完整性问题
# --------------------------------------------------------------------------

AWKWARD = [
    "", "   \n\n  ", "[[see:m:gmv]]", "---", "```py\nx = 1", "```\n```", "| a | |\n|---|---|\n| | [[m:gmv]] |",
    "- a\n  - b [[m:orders]]\n    续行\n- c", "3. 三\n4. 四", "#  标题 [[m:gmv]]  ", "## ", "[[m:orders]]. 第一句。",
    "a\r\nb\r\n", "开头 [[ 没闭合", "**加粗 [[m:gmv]] 里面**。", "> a\n>\n> b [[m:gmv]]", "[[see:m:gmv]] 开头。",
    "e.g. 3.5 和 v2. 结束", "表格 | 不是 | 表格", "| x |\n| y |", "__[[m:gmv]]__ 与 ** [[m:wow]] **",
    "`code 12` 和 [link](http://x.y/12)", "多个\n\n\n\n段落", "[[m:gmv|万]]|[[m:gmv]]", "1) 一\n2) 二",
]


def _random_corpus(n: int) -> list[str]:
    import random

    rng = random.Random(20260928)
    alphabet = [*"我们。！？\n- *#>|`[]:0123456789 .,，、（）_", "[[m:gmv]]", "[[see:m:wow]]", "[[i:week]]",
                "[[m:refund_rate]]", "**", "__", "```", "---", "| a |", "\n\n", "[[v:Q1.r0.x]]"]
    return ["".join(rng.choice(alphabet) for _ in range(rng.randint(1, 40))) for _ in range(n)]


INTEGRITY = {"segment_mismatch", "structural_text", "state_mismatch", "render_mismatch", "eid_mismatch", "bad_schema"}


@pytest.mark.parametrize("raw", AWKWARD + _random_corpus(400))
def test_composition_invariants(catalog, raw):
    doc = compose_doc(raw, catalog)
    assert "".join(texts(doc)) == doc["markdown"]
    again = verify_doc(copy.deepcopy(doc), catalog)
    assert again["violations"] == doc["violations"] and again["stats"] == doc["stats"]
    assert not INTEGRITY & set(codes(doc)), doc["violations"]
