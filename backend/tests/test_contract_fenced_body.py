"""出口契约：报告的正文仍写在代码块里、一句结论都没切出来时记出具缺口（nodes/io.py 的 _fenced_body）。

K5 让报告节点去掉整篇外层的 markdown 围栏（report._unfenced），规格有意不去的写法仍会让正文落在代码块里：围栏外有
前言、只有开头没有收尾、信息串写成 text。这时结论句是 0 句：裁判没有句子可判、反方审稿记 no_claims，以前正式运行
照样完整出具，读的人以为结论都核对过。

- 结论句策略为 judge 或 require_citation（报告节点的，或出具契约自己写的）、结论句 0 句、又有信息串为空 / markdown /
  md / text 的代码块，里面是成句的文字或写着引用：记一条缺口，降档。缺口不带核查节点的前缀，界面上归在「校验未全部
  完成」下（run/decode.ts 的 splitIssuanceGaps）
- 行为不变：有正常结论句的；代码块是 sql、python 这类代码的（信息串为空、里面是代码的也算，代码注释里的中文句子不算）；
  结论句策略为 off 的

模型一律是 MockChatModel 剧本，数据源是现造的 SQLite 文件，区域、金额都是虚构的。
"""
from __future__ import annotations

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
HIDDEN = [pytest.param(PREAMBLE, id="preamble"), pytest.param(UNCLOSED, id="unclosed"),
          pytest.param(AS_TEXT, id="text")]


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
    # 只有列表、没有句读，可写着报告的引用：代码里不会这样写
    pytest.param("以下是周报：\n\n```markdown\n- 华东销售额 [[m:gmv]]\n- 华南销售额 [[m:gmv_s]]\n```", id="markers"),
])
def test_a_report_body_in_a_prose_code_block_is_recognised(raw):
    doc = doc_of(raw)
    assert doc["stats"]["claims"] == 0, "背景：正文在代码块里，一句结论都切不出来"
    assert _fenced_body(doc) is True


@pytest.mark.parametrize("raw", [
    # 代码：信息串写明了语言
    pytest.param("```sql\nSELECT region, SUM(amount) AS gmv FROM orders WHERE amount > 100 GROUP BY region\n```", id="sql"),
    pytest.param("```python\nprint('华东销售额最高，华南次之。')\n```", id="python"),
    pytest.param('```json\n{"区域": "华东，华南"}\n```', id="json"),
    # 信息串为空、里面是代码：没有成句的文字；代码注释里的中文句子不算
    pytest.param("```\nSELECT region, SUM(amount) FROM orders GROUP BY region\n```", id="bare-sql"),
    pytest.param("```\n-- 按支付日统计，排除退款。\nSELECT SUM(amount) FROM orders\n```", id="sql-comment"),
    pytest.param("```\n/* 按支付日统计，排除退款。 */\nSELECT SUM(amount) FROM orders\n```", id="block-comment"),
    pytest.param("```text\n区域  销售额\n华东  1500\n华南  900\n```", id="table-of-numbers"),
    # 有正常的结论句：正文没被藏起来
    pytest.param("华东销售额超过华南。\n\n```markdown\n华东销售额 1500，居各区域之首。\n```", id="claims-outside"),
    # 没有代码块
    pytest.param("以下是本周的销售报告。", id="no-code"),
])
def test_code_or_a_report_with_claims_is_not_taken_for_a_hidden_body(raw):
    assert _fenced_body(doc_of(raw)) is False


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


async def test_without_a_claims_policy_nothing_changes(monkeypatch):
    """结论句策略为 off：出口不核对结论句，同样的报告照旧完整出具（数字都在代码块里、也都有出处）。"""
    writer(monkeypatch, CITED)
    row = await finish(weekly())
    assert row.status == "succeeded", row.error
    issuance = row.output["_issuance"]
    assert issuance["tier"] == "formal" and issuance["gaps"] == [], issuance
    assert "claims" not in issuance
