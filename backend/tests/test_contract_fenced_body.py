"""出口契约：报告的正文仍写在代码块里、一句结论都没切出来时记出具缺口（nodes/io.py 的 _fenced_body）。

K5 让报告节点去掉整篇外层的 markdown 围栏（report._unfenced），规格有意不去的写法仍会让正文落在代码块里：围栏外有
前言、只有开头没有收尾、信息串写成 text。这时结论句是 0 句：裁判没有句子可判、反方审稿记 no_claims，以前正式运行
照样完整出具，读的人以为结论都核对过。

- 结论句策略为 judge 或 require_citation（报告节点的，或出具契约自己写的）、结论句 0 句、又有信息串为空 / markdown /
  md / text / plaintext / txt / plain 的代码块，里面写着引用（片段里的引用、只挂依据的 [[see:]]）或者在写正文：记一条
  缺口，降档。缺口不带核查节点的前缀，界面上归在「校验未全部完成」下（run/decode.ts 的 splitIssuanceGaps）
- 逐行看，先标出代码的行：块里再嵌的代码块（~~~sql）、SQL / Python 的语句连同续行、整段是 YAML 的（写明是文字的块里
  要有值为空的键）、像数据的行（公式、JSON、目录树、流程箭头、函数调用、文件名、逗号隔开的表头），信息串为空、整块是
  diff 的全是代码。正文里另有一段代码、一行像 YAML 键的标签，不妨碍认出别的行
- 在写正文：代码的行只认去掉注释、字符串之后成句的；别的行带句读的中文、成句的英文，或者按正文的规矩会算一句结论
  （方向词、因果词）；信息串写明是文字的，一行里连着六个以上汉字也算。列表不论用什么标记（- + 1. 1、 一、 （1） · ●
  →），也不论带不带标记，只看字
- 行为不变：有正常结论句的；代码块是 sql、python 这类代码的；写文字的块里只有代码、数据、公式的（注释、字符串里的
  中文句子不算，别名、公式里的方向词也不算）；信息串为空的块里不带句读、也不像结论的标签和列名；结论句策略为 off 的
- 取舍：写成 text、markdown 的 diff，+ - 后面又空一格的，和列表分不开，照列表看；信息串为空的块里先列 - 再列 + 的
  要点、整段都是「小写键: 值」的，和 diff、YAML 分不开，照代码看

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


#: + / - 开头的正负面要点：不包代码块时两句都是结论（「增长」「回落」）
PROS_CONS = "+ 华东销售额增长明显\n- 华北销售额回落"
#: 正文后面另贴一段取数的 SQL：块里再嵌一个 ~~~sql 代码块，或者缩进四格
WITH_TILDE = f"{BARE_LIST}\n\n## 取数\n\n~~~sql\nSELECT region, SUM(amount) AS gmv FROM orders GROUP BY region\n~~~"
WITH_INDENTED = f"{BARE_LIST}\n\n## 取数\n\n    select region, sum(amount) as gmv from orders group by region"
#: 列表项前面写着小写的英文标签，看着像 YAML 的键
WITH_LABEL = "## 本周结论\n\n- 华东销售额居各区域之首\n- note: 华南销售额超过华北"


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
          pytest.param(f"以下是本周周报：\n\n{fenced(listed('1、', '2、'), '')}", id="enumerated-no-info"),
          # + / - 开头的正负面要点：写明是文字的块里是列表；信息串为空的块里先列 + 的也不是 diff
          pytest.param(f"以下是本周周报：\n\n{fenced(PROS_CONS)}", id="pros-and-cons"),
          pytest.param(f"以下是本周周报：\n\n{fenced(PROS_CONS, 'text')}", id="pros-and-cons-text"),
          pytest.param(f"以下是本周周报：\n\n{fenced(PROS_CONS, '')}", id="pros-and-cons-no-info"),
          # 正文里另有几行代码、像 YAML 键的标签：只有那几行当代码看，正文照样认得出
          pytest.param(f"以下是本周周报：\n\n{fenced(WITH_TILDE)}", id="tilde-block-inside"),
          pytest.param(f"以下是本周周报：\n\n{fenced(WITH_INDENTED)}", id="indented-sql-inside"),
          pytest.param(f"以下是本周周报：\n\n{fenced(WITH_LABEL)}", id="lowercase-label")]


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
    # + / - 开头的正负面要点：写明是文字的块里是列表，不论哪个在前；信息串为空的块里先列 + 的不是 diff（diff 里一处
    # 改动先列删掉的行），+ 下面缩进的子项也一样
    pytest.param(f"```markdown\n{PROS_CONS}\n```", id="plus-minus-markdown"),
    pytest.param("```text\n- 华北销售额回落\n+ 华东销售额增长明显\n```", id="minus-plus-text"),
    # 取舍：写成 text、markdown 的 diff，+ - 后面又空一格，和列表分不开，照列表看（diff 该写 diff 信息串）
    pytest.param("```text\n- 按下单日期统计\n+ 按支付日期统计\n```", id="spaced-diff-labelled-text"),
    pytest.param(f"```\n{PROS_CONS}\n```", id="plus-minus-no-info"),
    pytest.param("```\n+ 华东销售额增长明显\n  - 主要来自新店\n- 华北销售额回落\n```", id="plus-minus-nested-no-info"),
    # 正文里另有几行代码：块里再嵌的 ~~~sql、缩进的 SQL 只是那几行，不把整块都当代码
    pytest.param(f"```markdown\n{WITH_TILDE}\n```", id="tilde-block-inside"),
    pytest.param(f"```\n{WITH_TILDE}\n```", id="tilde-block-inside-no-info"),
    pytest.param(f"```markdown\n{WITH_INDENTED}\n```", id="indented-sql-inside"),
    pytest.param(f"```\n{WITH_INDENTED}\n```", id="indented-sql-inside-no-info"),
    pytest.param("```text\n- 华南销售额超过华北\n\nselect region from orders\n```", id="sql-in-another-paragraph"),
    pytest.param("```markdown\n~~~sql\nSELECT region FROM orders\n~~~\n\n- 华南销售额超过华北\n```",
                 id="prose-after-nested-block"),
    # 块里再嵌的代码块开了没收：不当围栏，后面的正文照样认
    pytest.param("```markdown\n## 取数\n\n~~~sql\nSELECT region FROM orders\n\n## 本周结论\n\n- 华南销售额超过华北\n```",
                 id="nested-block-left-open"),
    pytest.param("```markdown\n~~~~sql\nSELECT region FROM orders\n~~~\n\n- 华南销售额超过华北\n```", id="nested-close-too-short"),
    # 收尾的围栏比开头长也收得了
    pytest.param("```markdown\n~~~~sql\nSELECT region FROM orders\n~~~~~\n\n- 华南销售额超过华北\n```", id="nested-close-longer"),
    # 写明是文字的块里，顶格的标题另起一段：上一段的 SQL 管不到标题下面的列表
    pytest.param("```markdown\n## 取数\nselect region from orders\n## 本周结论\n- 华南销售额超过华北\n```", id="heading-ends-sql"),
    # 行首的箭头是列表的记号，不是连起流程的箭头
    pytest.param("```markdown\n→ 华东销售额居各区域之首\n→ 华南销售额超过华北\n```", id="arrow-bullets"),
    # 小写英文键开头的行：一段里有不缩进、又不是键的行，不是 YAML；写明是文字的块里，没有值为空的键（下面缩进着
    # 列表、子键）的「键: 一句话」也是正文里的标签
    pytest.param(f"```markdown\n{WITH_LABEL}\n```", id="lowercase-label-after"),
    pytest.param("```\n- note: 华南销售额超过华北\n- 华东销售额居各区域之首\n```", id="lowercase-label-first-no-info"),
    pytest.param("```text\n- top: 华东销售额居各区域之首\n- note: 华南销售额超过华北\n```", id="lowercase-labels-only"),
    pytest.param("```markdown\nsummary: 华南销售额超过华北\n```", id="key-and-conclusion"),
    # 首字母大写的英文词开头是正文里的英文，不是 SQL 的关键字
    pytest.param("```markdown\nFrom 数据看 华南销售额超过华北\n```", id="capitalised-keyword"),
    pytest.param("```\nUpdate 华南销售额超过华北\n```", id="capitalised-keyword-no-info"),
    # def、class 不写成定义的样子，是正文里的词（A 类门店）
    pytest.param("```markdown\nclass A 门店销售额超过 class B 门店\n```", id="class-of-stores"),
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
    # 取舍：信息串为空、整段都是「小写键: 值」的照 YAML 看，值里写着结论也一样（写明是文字的块里照正文看）
    pytest.param("```\n- top: 华东销售额居各区域之首\n- note: 华南销售额超过华北\n```", id="all-keys-no-info"),
    pytest.param("```\n- 退款率下降\n+ 退款率上升\n```", id="diff-with-conclusion-words"),
    pytest.param("```\n@@ -1,2 +1,2 @@\n 口径说明\n-销售额按下单日期统计后下降\n+销售额按支付日期统计后上升\n```", id="diff-hunk"),
    # 写成 text、markdown 的 diff：+ - 紧挨着字写，不是 Markdown 的列表项
    pytest.param("```text\n-退款率下降时提醒\n+退款率上升时提醒\n```", id="diff-labelled-text"),
    pytest.param("```markdown\n@@ -1 +1 @@\n-销售额按下单日期统计后下降\n+销售额按支付日期统计后上升\n```",
                 id="diff-hunk-labelled-markdown"),
    # 有 @@ 块头的，先列加上的行也是 diff
    pytest.param("```\n@@ -1,2 +1,2 @@\n+销售额按支付日期统计后上升\n-销售额按下单日期统计后下降\n```", id="diff-hunk-plus-first"),
    # 代码的行里，-- 紧挨着前面的字也是注释
    pytest.param("```\nSELECT SUM(amount)--按区域汇总，排除退款\nFROM orders\n```", id="sql-comment-without-space"),
    # 信息串写成 text、markdown，里面却看得出是代码：照代码看，注释里的句子、别名里的方向词不算
    pytest.param("```text\nSELECT region AS 区域, MAX(amount) AS 最高金额 FROM orders -- 按区域汇总，排除退款\n```",
                 id="sql-labelled-text"),
    pytest.param("```markdown\nmetrics:\n  - 退款占比\n  - 销售额增长率\n```", id="yaml-labelled-markdown"),
    pytest.param("```text\n# 指标\nmetrics:\n  # 核心指标\n  - 退款占比\n```", id="yaml-with-comments"),
    pytest.param("```\n# 核心指标\nmetrics:\n  - 退款占比\n```", id="yaml-with-comments-no-info"),
    # 写成 text 的 SQL 跨好几行：以语句开头的一段连同续行都是代码，开头的注释行也是；块注释、多行字符串整段不算，
    # 只写着块注释的一行也不把一段断开
    pytest.param("```text\nSELECT region,\n       amount AS 销售额增长率\n  FROM orders\n```", id="sql-continuation-text"),
    pytest.param("```text\nSELECT region,\n/* 别名 */\n       amount AS 销售额增长率\n  FROM orders\n```",
                 id="sql-with-block-comment-text"),
    pytest.param("```text\n-- 按区域汇总，排除退款。\nSELECT region, amount AS 最高金额 FROM orders\n```", id="sql-leading-comment"),
    pytest.param('```\ndef summary():\n    """\n    按支付日统计，排除退款。\n    """\n    return 1\n```', id="docstring"),
    # 像数据、公式的行（等号、JSON、画目录树的制表符、箭头连起的流程、函数调用、文件名、逗号隔开的表头）照代码看：
    # 去掉字符串之后不成句的不算正文，结论词、六字连写只看写正文的行
    pytest.param("```\n退款占比 = 退款金额 / 销售额\n```", id="formula-no-info"),
    pytest.param("```text\n销售额增长率 = (本期销售额 - 上期销售额) / 上期销售额\n```", id="formula-text"),
    pytest.param('```text\n{"区域": "华东", "说明": "按支付日期统计"}\n```', id="json-text"),
    pytest.param('```markdown\n  "说明": "按支付日期统计，排除退款",\n```', id="json-member"),
    pytest.param('```text\n["订单编号", "支付日期", "销售额增长率"]\n```', id="json-array"),
    pytest.param("```text\n报表/\n├── 华东门店销售周报.xlsx\n└── 华南门店销售周报.xlsx\n```", id="tree-text"),
    pytest.param("```text\n周报\n├── 华东门店销售周报\n│   └── 第四周销售明细\n└── 华南门店销售周报\n```", id="tree-of-folders"),
    pytest.param("```text\n上传表格导入 → 字段映射校验 → 生成周报\n```", id="flow-text"),
    pytest.param("```text\nprint('导入完成，请查收')\n```", id="call-with-string-text"),
    pytest.param("```markdown\nrows.groupby(区域).agg(销售额增长率)\n```", id="call-text"),
    pytest.param("```markdown\n华东门店销售周报.xlsx\n华南门店销售周报.xlsx\n```", id="file-names"),
    pytest.param("```text\n订单编号,支付日期,销售额增长率\n```", id="csv-header"),
    # 块里再嵌的代码块照代码看：注释里的句子、别名里的结论词、列着指标名的 YAML 都不算
    pytest.param("```markdown\n## 取数\n\n~~~sql\nSELECT MAX(amount) AS 最高金额 FROM orders -- 按区域汇总，排除退款\n~~~\n```",
                 id="tilde-block-only"),
    pytest.param("```markdown\n## 口径\n\n~~~yaml\n- 退款占比\n- 销售额增长率\n~~~\n```", id="nested-yaml-block"),
    pytest.param("```markdown\n~~~~\n- 退款占比\n- 销售额增长率\n~~~~~\n```", id="nested-yaml-longer-close"),
    # 开了没收的那一行照普通的行看，里面的 SQL 照样认得出
    pytest.param("```markdown\n## 取数\n\n~~~sql\nSELECT MAX(amount) AS 最高金额 FROM orders\n```", id="nested-sql-left-open"),
    # 有正常的结论句：正文没被藏起来
    pytest.param("华东销售额超过华南。\n\n```markdown\n华东销售额 1500，居各区域之首。\n```", id="claims-outside"),
    # 没有代码块
    pytest.param("以下是本周的销售报告。", id="no-code"),
])
def test_code_or_a_report_with_claims_is_not_taken_for_a_hidden_body(raw):
    assert _fenced_body(doc_of(raw)) is False


@pytest.mark.parametrize("lang", ["", "markdown"])
@pytest.mark.parametrize("line", [
    pytest.param("alpha beta " * 4000, id="words"),
    # 逐行认代码、认数据的几条规则：一长串字母后面没有括号、冒号，一长串空格、横线，一长串汉字、逗号、引号
    pytest.param("a" * 40000, id="one-long-word"),
    pytest.param(" " * 20000 + "-" + " " * 20000 + "a", id="spaces-and-a-dash"),
    pytest.param("华" * 40000, id="cjk"),
    pytest.param("a," * 20000, id="commas"),
    pytest.param(' "a' * 13000, id="open-quotes"),
    pytest.param('"""' * 13000, id="triple-quotes"),
    # 好多行围栏：收得了的、开了没收的
    pytest.param("~~~\n" * 13000, id="bare-fences"),
    pytest.param("~~~sql\n" * 13000, id="fences-left-open"),
])
def test_a_long_line_without_a_full_stop_takes_linear_time(lang, line):
    """很长、不带句末标点的一行：逐串数单词、逐行认代码，耗时随行长线性增长。按正则回溯认英文句子时，四万多字的一行要
    十几秒（行长翻倍、耗时翻四倍），出口复核在事件循环上同步执行，整个服务都会卡住。"""
    doc = {"blocks": [{"type": "code", "lang": lang, "units": [
        {"kind": "code", "see": [], "segments": [{"kind": "text", "text": line}]}]}]}
    started = time.perf_counter()
    _fenced_body(doc)
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


@pytest.mark.parametrize("info, code", [
    pytest.param("", "SELECT region, SUM(amount) AS gmv FROM orders GROUP BY region -- 按区域汇总，排除退款",
                 id="sql-trailing-comment"),
    pytest.param("", "columns:\n  - 订单编号\n  - 支付日期", id="yaml-list"),
    pytest.param("", "- 按下单日期统计\n+ 按支付日期统计", id="diff"),
    pytest.param("", "退款占比 = 退款金额 / 销售额", id="formula"),
    pytest.param("text", "销售额增长率 = (本期销售额 - 上期销售额) / 上期销售额", id="formula-text"),
    pytest.param("text", '{"区域": "华东", "说明": "按支付日期统计"}', id="json-text"),
    pytest.param("text", "报表/\n├── 华东门店销售周报.xlsx\n└── 华南门店销售周报.xlsx", id="tree-text"),
])
async def test_code_in_a_report_without_claims_is_issued_as_before(monkeypatch, info, code):
    """一句结论都没有的报告（只有口径说明）贴了一段代码、公式或数据：行尾注释里写着中文句子的 SQL、列着中文列名的
    YAML、改口径的 diff（这三种不带信息串），带「占比」「增长率」的公式，写成 text 的 JSON、目录树。那不是藏起来的
    正文，照常完整出具，不记这条缺口。"""
    writer(monkeypatch, f"## 口径说明\n\n本周报告按支付日期统计。\n\n```{info}\n{code}\n```")
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
