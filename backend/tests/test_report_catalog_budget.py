"""写作者的出处清单：预算按写作模型的上下文窗口定；超出预算时分优先级、按来源均衡，省略了什么照实说。

2026-10 一次运行：证据台账里上百次查询（4 个 Agent 查询次数悬殊），出处清单写死 12000 字，十万字以上的目录
被截到一万两千字以内，只有前二十来次查询进了写作者的视野，后面只有一句「目录太长，后面的省略了」。查询按编号排，前面的
Agent 占满了预算，有的 Agent 一次都没露面；写作者于是写出「本轮没有查到任何核销结果」，可那个 Agent 查到了上百条。

- 预算：写作模型上下文窗口的 40%（字数按 1 字 ≈ 1 token 保守算），拿不到窗口按 128K；下限 12000、上限 160000
- 第一优先：各 Agent 经按出处核对交来的结构化字段、口径卡指标，一律列出
- 查询按产出它的节点分组、轮流取：先给每次查询前几行，再补到 20 行；每个节点都有代表
- 没展开的查询列出编号和一行摘要（表、行数）；约定只能写 [[see:Qn]] 作依据，数字照样逐字核对
- 清单开头明说「目录有省略」，各节点各省略了多少条

夹具全部合成（tests/fixtures/evidence/store_run.py）。
"""
from __future__ import annotations

import re

import pytest

import app.db.models  # noqa: F401 - 建表（conftest 的 create_all）前要先登记模型
from app.engine.evidence import (
    CATALOG_MAX_CHARS,
    CATALOG_MIN_CHARS,
    DEFAULT_CATALOG_BUDGET,
    build_catalog,
    catalog_budget,
    catalog_prompt,
    writer_catalog,
)
from tests.fixtures.evidence import store_run

LEDGER, STORE, NODES, TITLES = store_run.build()


def load(artifact):
    return STORE.get(artifact)


@pytest.fixture
def catalog():
    return build_catalog(nodes=NODES, ledger=LEDGER)


def sources():
    return [{"node_id": nid, "title": TITLES[nid], "data": NODES[nid]["data"],
             "evidence": NODES[nid]["data_evidence"]} for nid, *_ in store_run.AGENTS]


def written(catalog, budget=DEFAULT_CATALOG_BUDGET, **kw):
    return writer_catalog(catalog, budget=budget, loader=load, fields=sources(), titles=TITLES, **kw)


def group_of(text: str, title: str) -> str:
    """清单里「来自「<名字>」的查询」那一段（到下一个来源或下一节为止）。"""
    start = text.index(f"来自「{title}」的查询")
    rest = text[start + 1:]
    ends = [m.start() for m in re.finditer(r"\n(?:来自「|\n)", rest)]
    return text[start:start + 1 + (ends[0] if ends else len(rest))]


# --------------------------------------------------------------------------
# 预算
# --------------------------------------------------------------------------


def test_budget_follows_the_model_window():
    assert DEFAULT_CATALOG_BUDGET == catalog_budget(None) == 51_200, "拿不到窗口按 128K 的 40%"
    assert catalog_budget(128_000) == 51_200
    assert catalog_budget(200_000) == 80_000
    assert catalog_budget(32_000) == 12_800
    assert catalog_budget(8_000) == CATALOG_MIN_CHARS == 12_000, "很小的窗口维持原来的篇幅"
    assert catalog_budget(1_000_000) == CATALOG_MAX_CHARS == 160_000, "百万窗口也有上限：每写一稿都要重发整份目录"
    assert catalog_budget(0) == catalog_budget(-5) == DEFAULT_CATALOG_BUDGET


def test_the_default_is_far_above_the_old_cut():
    assert DEFAULT_CATALOG_BUDGET >= 4 * 12_000


# --------------------------------------------------------------------------
# 放得下：照旧
# --------------------------------------------------------------------------


def test_a_catalog_that_fits_is_not_trimmed():
    small = build_catalog(nodes={}, ledger=[e for e in LEDGER if e.get("node_id") == "agent_store"])
    got = writer_catalog(small, loader=load)
    assert not got.trimmed and not got.omitted
    assert "目录有省略" not in got.text
    assert got.text == catalog_prompt(small, loader=load), "没有结构化字段时和 catalog_prompt 一字不差"
    assert "Q17：" in got.text and "  r19：" in got.text


# --------------------------------------------------------------------------
# 放不下：分优先级、按来源均衡（那次运行同样形状的场景）
# --------------------------------------------------------------------------


@pytest.mark.parametrize("budget", [DEFAULT_CATALOG_BUDGET, 12_000])
def test_every_agent_stays_in_view_within_the_budget(catalog, budget):
    got = written(catalog, budget)
    text = got.text
    assert got.trimmed and len(text) <= budget, len(text)
    for nid, title, count, table in store_run.AGENTS:
        group = group_of(text, title)
        assert f"共 {count} 次" in group
        assert re.search(r"\n {2}r0：", group), f"「{title}」一次查询都没展开"
    # 每次查询都在清单里：展开的有行，没展开的有编号和摘要
    total = sum(c for *_, c, _ in store_run.AGENTS)
    for n in range(1, total + 1):
        assert re.search(rf"(?:^|\n)\s*- Q{n}：", text), f"Q{n} 不见了"


def test_omitted_queries_are_listed_with_a_summary(catalog):
    got = written(catalog)
    assert got.omitted, "这个场景放不下全部展开"
    for alias in got.omitted:
        node = catalog[alias]["node_id"]
        table = next(t for nid, _, _, t in store_run.AGENTS if nid == node)
        assert re.search(rf"\n {{2}}- {alias}：{table}，30 行", got.text), alias
    # 没展开的查询没有行
    first = got.omitted[0]
    block = got.text[got.text.index(f"- {first}："):].split("\n", 2)
    assert not block[1].lstrip().startswith("r0：")


def test_the_head_says_what_was_left_out_per_source(catalog):
    got = written(catalog)
    head = got.text[:800]
    assert "目录有省略" in head
    for row in got.nodes:
        assert f"{row['title']} {row['total']} 次" in head and f"省略 {row['omitted']} 次" in head, row
    assert sum(r["omitted"] for r in got.nodes) == len(got.omitted)
    assert [r["node_id"] for r in got.nodes] == [nid for nid, *_ in store_run.AGENTS]


def test_how_to_use_an_omitted_query_is_spelled_out(catalog):
    text = written(catalog).text
    assert "[[see:Qn]]" in text and "本报告未覆盖" in text
    assert "不要凭摘要写" in text and "数字照样逐字核对" in text
    for phrase in ("没有查到", "不存在", "缺失", "没有数据"):
        assert phrase in text


def test_structured_fields_come_first_and_point_at_global_cells(catalog):
    got = written(catalog, 12_000)
    text = got.text
    fields_at = text.index("各 Agent 交来的结构化结论")
    assert fields_at < text.index("来自「"), "结构化结论排在查询前面"
    member_q3 = store_run.global_alias("agent_member", 3)
    store_q3 = store_run.global_alias("agent_store", 3)
    campaign_q5 = store_run.global_alias("agent_campaign", 5)
    assert f"[[v:{member_q3}.r0.cnt]]" in text and f"[[v:{store_q3}.r0.store_name]]" in text
    assert f"[[table:{campaign_q5} cols=store_name rows=0-4]]" in text
    assert "churn_rate：抽取时没有找到这个值" in text
    # 抽取失败的那个 Agent：结构化结论为空，不能据此断言没有数据
    block = text[text.index("「卡券核销」：", fields_at):]
    assert block.startswith("「卡券核销」：结构化结论为空") and "本报告未覆盖" in block.split("\n", 1)[0]
    assert got.empty_sources == ["卡券核销"]


def test_queries_the_fields_cite_are_expanded_first(catalog):
    """结构化字段引用到的那几次查询先展开：写作者照抄字段的引用标记时，看得到那几行。"""
    got = written(catalog, 12_000)
    for nid in ("agent_member", "agent_store", "agent_campaign"):
        for local in (3, 5):
            alias = store_run.global_alias(nid, local)
            assert alias not in got.omitted, alias
            assert re.search(rf"\n- {alias}：.*\n {{2}}r0：", got.text), alias


def test_a_field_value_reads_like_the_cell_it_cites(catalog):
    """字段后面写的值和引用之后报告里显示的一样（[[v:]] 的渲染规则）。"""
    text = written(catalog).text
    q3 = STORE[store_run.artifact_of("agent_member", 3)]
    assert f"total_cnt = {q3['rows'][0][2]:,}：[[v:Q3.r0.cnt]]" in text


# --------------------------------------------------------------------------
# 报告节点：窗口从哪来
# --------------------------------------------------------------------------


async def test_the_window_comes_from_the_provider_entry_then_the_catalog():
    """模型接入里给这个模型登记的 context 优先，其次模型目录；都没有返回 None（预算按保守值）。"""
    from sqlalchemy import delete

    from app.db.base import SessionLocal
    from app.db.models import Provider
    from app.engine.nodes.report import model_window
    from app.providers.factory import ModelSpec

    async with SessionLocal() as session:
        row = Provider(name="fa-window-test", kind="mock", enabled=True, default_model="mock-wide",
                       models=[{"id": "mock-wide", "context": 64_000}, {"id": "mock-text", "context": "96000"},
                               {"id": "mock-bare"}])
        session.add(row)
        await session.commit()
        try:
            assert await model_window(session, ModelSpec(model="mock-wide"), "mock-wide") == 64_000
            assert await model_window(session, ModelSpec(model="mock-text"), "mock-text") == 96_000
            # 接入里没登记窗口：按模型目录（deepseek-chat 登记了 128K）；目录里也没有就是 None
            assert await model_window(session, ModelSpec(model="deepseek-chat"), "deepseek-chat") == 128_000
            assert await model_window(session, ModelSpec(model="mock-bare"), "mock-bare") is None
            assert catalog_budget(await model_window(session, ModelSpec(model="mock-wide"), "mock-wide")) == 25_600
        finally:
            await session.execute(delete(Provider).where(Provider.id == row.id))
            await session.commit()
