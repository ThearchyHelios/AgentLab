"""结论句预筛：哪些句子送裁判模型、按什么优先级，哪些直接判为连接性。

裁判按调用计费，一份报告里的过渡话、标题、表格里的一格、代码都不值得花钱判。预筛是确定性的
规则（同一份文档永远得到同一个结果），而且只看文档本身：

- 优先级 1：写作者给这句挂了依据（行内引用或 [[see:…]]）
- 优先级 2：没挂依据，但有方向词、因果词（增长、主要来自、导致…）
- 优先级 3：其余有数字、实体这类实质内容、又不是短句的
- 不送：连接性的话、标题、代码、表格单元格；只有过渡词的（「原因如下：」）；短句
预算不够时按优先级截断：先判 1，再判 2，剩下的记未裁判。
"""
from __future__ import annotations

from app.engine.evidence import build_catalog, compose_doc, iter_units
from app.engine.judge import SHORT_CHARS, candidates, claim_priority


def catalog():
    return build_catalog(nodes={"caliber": {
        "kind": "metric_set", "caliber": "周报口径", "caliber_version": "v2", "artifact": "f" * 64,
        "metrics": [{"id": "gmv", "name": "销售额", "value": 45678.5, "unit": "元", "decimals": 1},
                    {"id": "orders", "name": "订单数", "value": 1234, "unit": "单"}]}})


RAW = """## 本周概览

本周销售额 [[m:gmv]]。增长主要来自新客首单。[[see:m:gmv]]原因如下：
下面看细节。订单一共是 1234 单，整体平稳。共 3 单。增长明显。

- 列表里订单 [[m:orders]]
- 过渡说明

| 区域 | 销售额 |
|---|---|
| 华东 | [[m:gmv]] |

```sql
SELECT 1
```
"""


def by_text(doc):
    """{句子原文: unit}，测试按字找句子，不依赖编号。"""
    md = doc["markdown"]
    return {md[u["span"][0]:u["span"][1]].strip(): (b, u) for b, u in iter_units(doc)}


def priorities(doc):
    return {text: claim_priority(u, b, doc["markdown"]) for text, (b, u) in by_text(doc).items()}


def test_the_rules():
    got = priorities(compose_doc(RAW, catalog()))
    assert got["本周销售额 45,678.5元。"] == 1            # 行内引用
    assert got["增长主要来自新客首单。"] == 1              # 句末 [[see:]] 挂的依据
    assert got["列表里订单 1,234单"] == 1                  # 列表项也是句子
    assert got["增长明显。"] == 2                          # 没挂依据，有方向词：短也要判
    assert got["订单一共是 1234 单，整体平稳。"] == 3      # 有数字、不是短句
    assert got["原因如下："] is None                        # 有「原因」但只是过渡
    assert got["共 3 单。"] is None                          # 短句
    assert got["下面看细节。"] is None                       # 连接性
    assert got["过渡说明"] is None
    assert got["本周概览"] is None                           # 标题
    assert got["45,678.5元"] is None                         # 表格单元格：一格值不是一句结论
    assert got["SELECT 1"] is None                           # 代码


def test_it_is_deterministic():
    one, two = compose_doc(RAW, catalog()), compose_doc(RAW, catalog())
    assert priorities(one) == priorities(two)
    assert [c.unit for c in candidates(one)] == [c.unit for c in candidates(two)]


def test_candidates_come_in_priority_order_then_document_order():
    doc = compose_doc(RAW, catalog())
    texts = {u["id"]: t for t, (_, u) in by_text(doc).items()}
    got = [(c.priority, texts[c.unit]) for c in candidates(doc)]
    assert got == [
        (1, "本周销售额 45,678.5元。"), (1, "增长主要来自新客首单。"), (1, "列表里订单 1,234单"),
        (2, "增长明显。"),
        (3, "订单一共是 1234 单，整体平稳。"),
    ]


def test_short_means_visible_characters_not_bytes():
    """短句按看得见的字算（标点、空白不算），数字的字也算进去。"""
    assert SHORT_CHARS == 6
    doc = compose_doc("订单 1234 单。订单总数 1234 单。", catalog())
    got = priorities(doc)
    assert got["订单 1234 单。"] == 3 and got["订单总数 1234 单。"] == 3
    doc = compose_doc("共 12 单。", catalog())
    assert priorities(doc)["共 12 单。"] is None


def test_an_explicit_request_overrides_the_screen_but_not_the_kind():
    """有人点开一句、明确要求判它：预筛放掉的结论句照判（按最低优先级）；标题、连接性的话不是结论，不判。"""
    doc = compose_doc(RAW, catalog())
    units = {t: u["id"] for t, (_, u) in by_text(doc).items()}
    picked = candidates(doc, units=[units["共 3 单。"], units["下面看细节。"], units["本周概览"], "u999"])
    assert [(c.unit, c.priority) for c in picked] == [(units["共 3 单。"], 3)]


def test_cites_are_the_authors_own():
    """自动链接的名字不算依据（三期 D2）：只提到表名的句子不会因此排进优先级 1。"""
    ledger = [{"kind": "schema", "node_id": "fetch", "exec": 1, "artifact": "a" * 64, "source": "shop",
               "schema": None, "synced_at": None, "tables": {"orders": ["amount", "region"]}}]
    cat = build_catalog(nodes={}, ledger=ledger)
    doc = compose_doc("明细在 `orders` 表里，按区域汇总之后东区最高。", cat)
    [(text, prio)] = [(t, p) for t, p in priorities(doc).items()]
    assert prio == 2, (text, prio)
