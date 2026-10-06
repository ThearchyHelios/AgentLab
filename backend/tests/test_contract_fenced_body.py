"""出口契约：报告的正文仍写在代码块里、一句结论都没切出来时记出具缺口（nodes/io.py 的 _fenced_body）。

K5 让报告节点去掉整篇外层的 markdown 围栏（report._unfenced），规格有意不去的写法仍会让正文落在代码块里：围栏外有
前言、只有开头没有收尾、信息串写成 text。这时结论句是 0 句：裁判没有句子可判、反方审稿记 no_claims，以前正式运行
照样完整出具，读的人以为结论都核对过。

- 结论句策略为 judge 或 require_citation（报告节点的，或出具契约自己写的）、结论句 0 句、又有信息串为空 / markdown /
  md / text / plaintext / txt / plain 的代码块，里面写着引用（片段里的引用、只挂依据的 [[see:]]）或者在写正文：记一条
  缺口，降档。缺口不带核查节点的前缀，界面上归在「校验未全部完成」下（run/decode.ts 的 splitIssuanceGaps）
- 在写正文：带句读的中文、成句的英文，或者按正文的规矩会算一句结论（方向词、因果词）；信息串写明是文字的，一行里连着
  六个以上汉字也算。列表不论用什么标记（- 1. 1、 一、 （1） · ●），也不论带不带标记，只看字
- 行为不变：有正常结论句的；代码块是 sql、python 这类代码的；信息串为空、里面是代码的（注释、字符串里的中文标点不算
  句子；有 SQL / Python 开头的行、YAML 的键，或者整块是 diff 时，方向词也不算），信息串写成 text、markdown 而里面
  看得出是代码的同样；信息串为空的块里不带句读、也不像结论的标签和列名；结论句策略为 off 的

模型一律是 MockChatModel 剧本，数据源是现造的 SQLite 文件，区域、金额都是虚构的。
"""
from __future__ import annotations

import time

import pytest
from langchain_core.messages import AIMessage

from app.engine.evidence import compose_doc
from app.engine.nodes.io import GAP_FENCED_BODY, _fenced_body
from app.providers.mock_model import MockChatModel
from tests.test_report_fenced import (  # noqa: F401 - engine_up、shop 是夹具，导入即可用
    EAST,
    SOUTH,
    SQL,
    TEXT,
    Models,
    audited,
    chain,
    engine_up,
    fenced,
    finish,
    logs,
    node,
    output_of,
    report_of,
    shop,
)

#: 写作者把正文包进代码块、又不是整篇只有一个代码块（报告节点不去围栏）的几种写法
PREAMBLE = f"以下是本周的销售报告：\n\n{fenced(TEXT)}"
UNCLOSED = f"```markdown\n{TEXT}"
AS_TEXT = fenced(TEXT, "text")
#: 只用 [[see:]] 挂依据的列表：依据记在句子上（unit 的 see），片段里没有引用；列表项也不带句读
SEE_LIST = "## 本周结论\n\n- 华东销售额居各区域之首 [[see:Q1]]\n- 华南销售额超过华北 [[see:Q1]]"
#: 不带句读、也没挂依据的列表：中文报告的列表项常这样写
BARE_LIST = "## 本周结论\n\n- 华东销售额居各区域之首\n- 华南销售额超过华北"
#: 同样两句，换成中文报告常见的几种编号、项目符号，或者干脆不带标记。不包代码块时第二句是一句结论（「超过」），
#: 第一句是连接性的话：两种句子都要认得出
LINES = ("华东销售额居各区域之首", "华南销售额超过华北")
#: 不带标记、一行一句
UNMARKED = f"## 本周结论\n\n{LINES[0]}\n\n{LINES[1]}"


def listed(first: str, second: str) -> str:
    return f"## 本周结论\n\n{first}{LINES[0]}\n{second}{LINES[1]}"


HIDDEN = [pytest.param(PREAMBLE, id="preamble"), pytest.param(UNCLOSED, id="unclosed"),
          pytest.param(AS_TEXT, id="text"), pytest.param(fenced(TEXT, "plaintext"), id="plaintext"),
          pytest.param(f"以下是本周周报：\n\n{fenced(SEE_LIST)}", id="see-only-list"),
          pytest.param(f"以下是本周周报：\n\n{fenced(BARE_LIST)}", id="list-without-punctuation"),
          pytest.param(f"以下是本周周报：\n\n{fenced(listed('1、', '2、'))}", id="digit-and-comma"),
          pytest.param(f"以下是本周周报：\n\n{fenced(listed('一、', '二、'))}", id="chinese-numeral"),
          pytest.param(f"以下是本周周报：\n\n{fenced(listed('（1）', '（2）'))}", id="fullwidth-parens"),
          pytest.param(f"以下是本周周报：\n\n{fenced(listed('· ', '· '))}", id="middle-dot"),
          pytest.param(f"以下是本周周报：\n\n{fenced(listed('● ', '● '))}", id="black-circle"),
          pytest.param(f"以下是本周周报：\n\n{fenced(UNMARKED)}", id="lines-without-markers"),
          # 信息串为空：第二句按正文的规矩是一句结论
          pytest.param(f"以下是本周周报：\n\n{fenced(listed('1、', '2、'), '')}", id="enumerated-no-info")]


def doc_of(raw: str) -> dict:
    return compose_doc(raw, {}, node_id="write")


# --------------------------------------------------------------------------
# 认得出哪些代码块里写的是正文
# --------------------------------------------------------------------------


@pytest.mark.parametrize("raw", [
    pytest.param("以下是本周的销售报告：\n\n```markdown\n华东销售额 1500，居各区域之首。\n```", id="preamble"),
    pytest.param("```markdown\n## 本周概览\n\n华东销售额 1500，居各区域之首。", id="unclosed"),
    pytest.param("```text\n华东销售额 1500，居各区域之首。\n```", id="text"),
    pytest.param("```\n华东销售额 1500，居各区域之首。\n```", id="no-info"),
    pytest.param("```MD\n华东销售额 1500，居各区域之首。\n```", id="upper-md"),
    pytest.param("```markdown\nEast region sales rose sharply this week.\n```", id="english"),
    pytest.param("```markdown\nEast region's sales rose sharply this week.\n```", id="english-apostrophe"),
    # 正文里用半角引号括起一个词：去掉代码字符串的那一步不能把句子吃掉
    pytest.param('```markdown\n华东的"销售额"居各区域之首，华南次之。\n```', id="quoted-word"),
    # 纯文字的另几种写法
    pytest.param("```plaintext\n华东销售额 1500，居各区域之首。\n```", id="plaintext"),
    pytest.param("```txt\n华东销售额 1500，居各区域之首。\n```", id="txt"),
    pytest.param("```Plain\n华东销售额 1500，居各区域之首。\n```", id="plain"),
    # 只有列表、没有句读，可写着报告的引用：代码里不会这样写
    pytest.param("以下是周报：\n\n```markdown\n- 华东销售额 [[m:gmv]]\n- 华南销售额 [[m:gmv_s]]\n```", id="markers"),
    # 只挂了依据（[[see:]] 记在句子上，片段里没有引用），列表项短得不成句也认
    pytest.param("以下是周报：\n\n```markdown\n- East [[see:m:gmv]]\n- South [[see:m:gmv]]\n```", id="see-only"),
    # 不带句读的列表项：一行里连着六个以上汉字，或者按正文的规矩是一句结论
    pytest.param("以下是周报：\n\n```markdown\n## 本周结论\n\n- 华东销售额居各区域之首\n- 华南销售额超过华北\n```",
                 id="bullets"),
    pytest.param("```text\n1. 华东销售额居各区域之首\n2) 华南销售额超过华北\n```", id="numbered"),
    pytest.param("```\n* 华东销售额居各区域之首\n+ 华南销售额超过华北\n• 华北销售额持平\n```", id="bare-bullets"),
    # 中文报告常见的编号、项目符号，或者不带标记：不看标记，只看字
    pytest.param("以下是周报：\n\n```markdown\n1、华东销售额居各区域之首\n2、华南销售额超过华北\n```", id="digit-and-comma"),
    pytest.param("```markdown\n一、华东销售额居各区域之首\n二、华南销售额超过华北\n```", id="chinese-numeral"),
    pytest.param("```text\n（1）华东销售额居各区域之首\n(2) 华南销售额超过华北\n```", id="parens"),
    pytest.param("```markdown\n· 华东销售额居各区域之首\n● 华南销售额超过华北\n▪ 华北销售额持平\n```", id="dots-circles-squares"),
    pytest.param("```markdown\n华东销售额居各区域之首\n\n华南销售额超过华北\n```", id="no-markers"),
    pytest.param("```markdown\n> 华东销售额居各区域之首\n```", id="blockquote"),
    pytest.param("```markdown\n| 区域 | 结论 |\n|---|---|\n| 华东 | 销售额居各区域之首 |\n```", id="table-row"),
    pytest.param("```markdown\n**华东**销售额居各区域之首\n```", id="bold"),
    # 写明是文字的块：一行里连着六个以上汉字就是在写中文，不像结论的也算
    pytest.param("```markdown\n华东销售额居各区域之首\n```", id="six-or-more-in-a-row"),
    # 不到六个字的短句：按正文的规矩是一句结论（方向词）就算
    pytest.param("```text\n- 华北回落\n- 华南领先\n```", id="short-conclusions"),
    # 写明是文字的块不当代码看：# -- 和半角引号都是正文里的符号
    pytest.param("```markdown\n华东排名 #1，销售额居各区域之首。\n```", id="hash-in-prose"),
    pytest.param('```markdown\n负责人说"销售额创新高，下周继续推进。"\n```', id="quoted-sentence"),
    pytest.param("```markdown\n华东领跑 -- 华南次之，华北第三。\n```", id="dash-in-prose"),
    # 信息串为空、看不出是代码：按正文的规矩会算结论的行也算
    pytest.param("```\n1、华东销售额居各区域之首\n2、华南销售额超过华北\n```", id="no-info-enumerated"),
    pytest.param("```\n华北销售额回落\n```", id="no-info-conclusion"),
    # 首字母大写的「Note:」是正文里的标签，不是 YAML 的键
    pytest.param("```markdown\nNote: 以下为初步结论\n- 华南销售额超过华北\n```", id="capitalised-label"),
])
def test_a_report_body_in_a_prose_code_block_is_recognised(raw):
    doc = doc_of(raw)
    assert doc["stats"]["claims"] == 0, "背景：正文在代码块里，一句结论都切不出来"
    assert _fenced_body(doc) is True


@pytest.mark.parametrize("raw", [
    # 代码：信息串写明了语言
    pytest.param("```sql\nSELECT region, SUM(amount) AS gmv FROM orders WHERE amount > 100 GROUP BY region\n```",
                 id="sql"),
    pytest.param("```python\nprint('华东销售额最高，华南次之。')\n```", id="python"),
    pytest.param('```json\n{"区域": "华东，华南"}\n```', id="json"),
    # 信息串为空、里面是代码：没有成句的文字；代码注释里的中文句子不算
    pytest.param("```\nSELECT region, SUM(amount) FROM orders GROUP BY region\n```", id="bare-sql"),
    pytest.param("```\n-- 按支付日统计，排除退款。\nSELECT SUM(amount) FROM orders\n```", id="sql-comment"),
    pytest.param("```\n/* 按支付日统计，排除退款。 */\nSELECT SUM(amount) FROM orders\n```", id="block-comment"),
    pytest.param("```\n/*\n * 按支付日统计，排除退款。\n 已退款的单不计入：见口径说明。\n */\nSELECT SUM(amount) FROM orders\n```",
                 id="multi-line-comment"),
    # 行尾注释（-- // #）、# 开头的注释行，里面写着中文句子也是代码的一部分
    pytest.param("```\nSELECT region, SUM(amount) AS gmv FROM orders GROUP BY region -- 按区域汇总，排除退款\n```",
                 id="trailing-sql-comment"),
    pytest.param("```\n# 按区域汇总，排除退款。\ntotal = sum(rows)  # 单位：元，不含税\n```", id="hash-comments"),
    pytest.param("```\nconst total = sum(rows); // 按区域汇总，排除退款\n```", id="trailing-slash-comment"),
    # 字符串字面量里的中文标点是数据（带 f、N 这类前缀的也是）
    pytest.param('```\nprint("销售额：", total)\nprint(f"订单数：{n}，客单价：{avg}")\n```', id="string-literals"),
    pytest.param("```\nSELECT * FROM orders WHERE note = '已退款，不计入：测试' OR memo = N'作废，重开'\n```",
                 id="sql-strings"),
    # 信息串为空、看得出是 SQL 的块：以 * + - 开头的是运算的续行，后面的中文列名不算正文
    pytest.param("```\nSELECT 单价\n     * 数量 AS 金额\n     + 运费 AS 合计\n     - 折扣金额 AS 实付\nFROM 订单\n```",
                 id="sql-continuation"),
    pytest.param("```text\n区域  销售额\n华东  1500\n华南  900\n```", id="table-of-numbers"),
    # 写明是文字的块里，表格的列名、门店名这类标签不到六个字，又不像结论
    pytest.param("```text\n门店名称  累计销售额\n华东一店  1500\n```", id="labels-under-six"),
    # 标题不算正文：不包代码块时，标题也从不算结论句
    pytest.param("```markdown\n## 华南销售额超过华北\n```", id="heading-only"),
    # 信息串为空、不带句读的行：不像结论的不算（列名、改了的口径）
    pytest.param("## 口径说明\n\n本周报告按支付日期统计。\n\n```\ncolumns:\n  - 订单编号\n  - 支付日期\n```", id="yaml-list"),
    pytest.param("## 口径说明\n\n本周报告按支付日期统计。\n\n```\n- 按下单日期统计\n+ 按支付日期统计\n```", id="diff"),
    pytest.param("```\n- 订单编号\n- 支付日期\n```", id="labels"),
    # 信息串为空、看得出是代码（SQL、YAML 的键、diff）：值里、别名里有方向词也不是结论
    pytest.param("```\nSELECT MAX(amount) AS 最高金额, MIN(amount) AS 最低金额 FROM orders\n```", id="sql-aliases"),
    pytest.param("```\nmetrics:\n  - 退款占比\n  - 销售额增长率\n```", id="yaml-with-conclusion-words"),
    pytest.param("```\n- name: 退款占比\n  unit: 百分比\n```", id="yaml-mappings"),
    pytest.param("```\n- 退款率下降\n+ 退款率上升\n```", id="diff-with-conclusion-words"),
    pytest.param("```\n@@ -1,2 +1,2 @@\n 口径说明\n-销售额按下单日期统计后下降\n+销售额按支付日期统计后上升\n```", id="diff-hunk"),
    # 信息串写成 text、markdown，里面却看得出是代码：照代码看，注释里的句子、别名里的方向词不算
    pytest.param("```text\nSELECT region AS 区域, MAX(amount) AS 最高金额 FROM orders -- 按区域汇总，排除退款\n```",
                 id="sql-labelled-text"),
    pytest.param("```markdown\nmetrics:\n  - 退款占比\n  - 销售额增长率\n```", id="yaml-labelled-markdown"),
    # 有正常的结论句：正文没被藏起来
    pytest.param("华东销售额超过华南。\n\n```markdown\n华东销售额 1500，居各区域之首。\n```", id="claims-outside"),
    # 没有代码块
    pytest.param("以下是本周的销售报告。", id="no-code"),
])
def test_code_or_a_report_with_claims_is_not_taken_for_a_hidden_body(raw):
    assert _fenced_body(doc_of(raw)) is False


def test_a_long_line_without_a_full_stop_takes_linear_time():
    """很长、不带句末标点的一行英文单词：逐串数单词，耗时随行长线性增长。按正则回溯认英文句子时，四万多字的一行要十几
    秒（行长翻倍、耗时翻四倍），出口复核在事件循环上同步执行，整个服务都会卡住。"""
    line = "alpha beta " * 4000
    doc = {"blocks": [{"type": "code", "lang": "", "units": [
        {"kind": "code", "see": [], "segments": [{"kind": "text", "text": line}]}]}]}
    started = time.perf_counter()
    assert _fenced_body(doc) is False
    assert time.perf_counter() - started < 1.0


# --------------------------------------------------------------------------
# 端到端：正式运行（裁判 + 反方审稿 + 出具契约）
# --------------------------------------------------------------------------


def gap(title: str = "写周报") -> str:
    return GAP_FENCED_BODY.format(title=title)


@pytest.mark.parametrize("raw", HIDDEN)
async def test_a_body_left_in_a_code_block_degrades_the_formal_issuance(monkeypatch, shop, raw):
    """正文落在代码块里：裁判没有句子可判（不调用）、反方审稿记 no_claims，出口契约不再完整出具，记一条缺口说清原因。"""
    models = Models(monkeypatch, shop, raw)
    row = await finish(audited(shop), run_class="formal")
    assert row.status == "succeeded", row.error
    _, doc = await report_of(row.id)
    assert doc["source"] == raw and doc["stats"]["claims"] == 0, "背景：报告节点不去这几种围栏"
    assert "code" in [b["type"] for b in doc["blocks"]]
    assert models.judges == [] and models.reviewed == []
    assert [d["reason"] for d in await logs(row.id, "challenge_skipped")] == ["no_claims"]
    issuance = row.output["_issuance"]
    assert issuance["tier"] == "degraded", issuance
    assert issuance["gaps"] == [gap()], issuance["gaps"]


@pytest.mark.parametrize("raw", [
    pytest.param(TEXT, id="plain"),
    pytest.param(fenced(TEXT), id="whole-report-unwrapped"),
    pytest.param(f"华东销售额 [[v:Q1.r0.销售额]]，居各区域之首。[[see:Q1]]\n\n{SQL}\n\n华南销售额超过华北。[[see:Q1]]",
                 id="sql-block"),
])
async def test_a_report_with_claims_is_issued_as_before(monkeypatch, shop, raw):
    """有正常的结论句（整篇外层围栏已由报告节点去掉的也是）、代码块是 SQL 的：判过、审过，照常完整出具，不记这条缺口。"""
    models = Models(monkeypatch, shop, raw)
    row = await finish(audited(shop), run_class="formal")
    assert row.status == "succeeded", row.error
    assert sorted(models.reviewed) == sorted([EAST, SOUTH])
    issuance = row.output["_issuance"]
    assert issuance["tier"] == "formal" and issuance["gaps"] == [], issuance


# --------------------------------------------------------------------------
# 结论句策略：require_citation 同样记，off 不管
# --------------------------------------------------------------------------

KPI = "{'gmv': 45678.5, 'orders': 1234}"
METRICS = [{"id": "gmv", "name": "销售额", "unit": "元", "decimals": 1, "expression": "vars.kpi.gmv"}]
#: 前言 + 代码块包住正文，正文里的引用都解析得了
CITED = "以下是本周周报：\n\n```markdown\n本周销售额 [[m:gmv]]，较上周有所增长。[[see:m:gmv]]\n```"


def weekly(*, claims: str | None = None, contract_claims: str | None = None) -> dict:
    """输入 → 取数 → 口径卡 → 报告（claims 是报告节点的结论句策略）→ 成果（引用模式的出具契约）。"""
    return chain(
        node("start", "input"),
        node("fetch", "transform", mode="expression", expression=KPI, assign_to="kpi"),
        node("caliber", "metrics", caliber="周报口径", caliber_version="v1", metrics=METRICS),
        node("write", "report", "写周报", instructions="写周报", max_repairs=0,
             **({"claims": claims} if claims else {})),
        node("out", "output", fields=[{"name": "周报", "value": "{{ nodes.write.text }}"}],
             contract={"report_from": "write", "metrics_from": ["caliber"],
                       **({"claims": contract_claims} if contract_claims else {})}),
    )


def writer(monkeypatch, text: str) -> None:
    monkeypatch.setattr(MockChatModel, "_decide", lambda self, messages: AIMessage(content=text))


@pytest.mark.parametrize("claims, contract_claims", [
    pytest.param("require_citation", None, id="report-node"),
    pytest.param(None, "require_citation", id="contract"),
])
async def test_require_citation_records_the_gap_too(monkeypatch, claims, contract_claims):
    """要求结论句挂依据（报告节点或出具契约写的）：同样数不出结论句，记同一条缺口。"""
    writer(monkeypatch, CITED)
    row = await finish(weekly(claims=claims, contract_claims=contract_claims))
    assert row.status == "succeeded", row.error
    issuance = row.output["_issuance"]
    assert issuance["tier"] == "degraded" and issuance["gaps"] == [gap()], issuance
    assert issuance["claims"]["uncited_claims"] == 0


@pytest.mark.parametrize("code", [
    pytest.param("SELECT region, SUM(amount) AS gmv FROM orders GROUP BY region -- 按区域汇总，排除退款",
                 id="sql-trailing-comment"),
    pytest.param("columns:\n  - 订单编号\n  - 支付日期", id="yaml-list"),
    pytest.param("- 按下单日期统计\n+ 按支付日期统计", id="diff"),
])
async def test_code_in_a_report_without_claims_is_issued_as_before(monkeypatch, code):
    """一句结论都没有的报告（只有口径说明）贴了一段不带信息串的代码：行尾注释里写着中文句子的 SQL、列着中文列名的
    YAML、改口径的 diff。那是代码，不是藏起来的正文，照常完整出具，不记这条缺口。"""
    writer(monkeypatch, f"## 口径说明\n\n本周报告按支付日期统计。\n\n```\n{code}\n```")
    row = await finish(weekly(claims="require_citation"))
    assert row.status == "succeeded", row.error
    issuance = row.output["_issuance"]
    assert issuance["tier"] == "formal" and issuance["gaps"] == [], issuance


async def test_without_a_claims_policy_nothing_changes(monkeypatch):
    """结论句策略为 off：出口不核对结论句，同样的报告照旧完整出具（数字都在代码块里、也都有出处）。"""
    writer(monkeypatch, CITED)
    row = await finish(weekly())
    assert row.status == "succeeded", row.error
    issuance = row.output["_issuance"]
    assert issuance["tier"] == "formal" and issuance["gaps"] == [], issuance
    assert "claims" not in issuance
