"""口径一次起草的纯逻辑（app/ledger/draftplan.py）。

- SQL 只收单条只读 SELECT：多语句、注释后藏的第二条、写操作、目录外的表一律拒；
- 指标表达式必须是 cell(nodes.fetch, 0, '列名')；引用核不上的整条丢弃；
- build_graph 两次调用逐字一致，且能过 GraphSpec / validate_graph；
- plan 注入假模型（不发真实请求），材料里有表目录和原文。
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.engine.schema import GraphSpec, validate_graph
from app.ledger import draftplan

CHUNK = "销售额按已支付订单的支付时间归期统计，退款订单不计入销售额。"
CHUNKS = {"c1": {"ordinal": 1, "content": CHUNK, "document_id": "d1"}}
TABLES = {"orders", "customers"}

METRIC = {
    "id": "m1",
    "name": "销售额",
    "synonyms": ["gmv"],
    "unit": "元",
    "decimals": 2,
    "expression": "cell(nodes.fetch, 0, 'gmv')",
    "definition": {"text": "已支付订单的销售额", "scope": "全部", "exclusions": [], "owner": "运营",
                   "effective_from": "2026-01-01",
                   "citations": [{"chunk": "c1", "quote": "销售额按已支付订单的支付时间归期统计"}]},
    "open_questions": [],
}


def raw(**extra):
    body = {
        "entry_fields": [{"name": "start", "type": "string", "required": True, "default": "2026-09-01",
                          "description": "开始日期"}],
        "sql": "SELECT SUM(amount) AS gmv FROM orders WHERE paid_at >= '{{ input.start }}'",
        "metrics": [dict(METRIC)],
        "open_questions": [],
    }
    body.update(extra)
    return body


def sql_problem(sql: str) -> str | None:
    plan, problems = draftplan.normalize_plan(raw(sql=sql), CHUNKS, allowed_tables=TABLES)
    assert plan["sql"] == ""
    reasons = [p["reason"] for p in problems if p["name"] == "sql"]
    assert reasons, problems
    return reasons[0]


# ==========================================================================
# normalize_plan：SQL 的硬规则
# ==========================================================================

def test_a_read_only_select_over_the_catalog_is_kept():
    plan, problems = draftplan.normalize_plan(raw(), CHUNKS, allowed_tables=TABLES)
    assert plan["sql"].endswith("'{{ input.start }}'")
    assert [m["id"] for m in plan["metrics"]] == ["m1"]
    assert plan["entry_fields"][0]["name"] == "start"
    assert not [p for p in problems if p["name"] == "sql"]


def test_multiple_statements_and_a_hidden_second_statement_are_refused():
    assert "只能是一条语句" in sql_problem(
        "SELECT SUM(amount) AS gmv FROM orders; DROP TABLE orders")
    # 注释后藏的第二条也解析成第二条语句
    assert "只能是一条语句" in sql_problem(
        "SELECT SUM(amount) AS gmv FROM orders; -- 收尾\nDELETE FROM orders")


def test_writes_and_ddl_are_refused():
    assert "只读 SELECT" in sql_problem("DELETE FROM orders")
    assert "只读 SELECT" in sql_problem("UPDATE orders SET amount = 0")
    assert "只读 SELECT" in sql_problem("DROP TABLE orders")


def test_tables_outside_the_catalog_are_refused():
    reason = sql_problem("SELECT * FROM secrets")
    assert "secrets" in reason and "数据目录" in reason
    # CTE 名字不是表；真正的表没在目录里照样拒
    reason = sql_problem("WITH x AS (SELECT * FROM secrets) SELECT * FROM x")
    assert "secrets" in reason


def test_a_cte_over_a_catalog_table_is_allowed():
    plan, problems = draftplan.normalize_plan(
        raw(sql="WITH paid AS (SELECT amount FROM orders) SELECT SUM(amount) AS gmv FROM paid"),
        CHUNKS, allowed_tables=TABLES)
    assert plan["sql"].startswith("WITH paid")
    assert not [p for p in problems if p["name"] == "sql"]


def test_an_undeclared_template_field_invalidates_the_sql():
    plan, problems = draftplan.normalize_plan(
        raw(sql="SELECT SUM(amount) AS gmv FROM orders WHERE paid_at >= '{{ input.nope }}'"),
        CHUNKS, allowed_tables=TABLES)
    assert plan["sql"] == ""
    assert any("nope" in p["reason"] for p in problems)


# ==========================================================================
# normalize_plan：指标
# ==========================================================================

def test_a_bad_expression_drops_only_that_metric():
    plan, problems = draftplan.normalize_plan(
        raw(metrics=[{**METRIC, "expression": "SUM(amount)"}, {**dict(METRIC), "id": "m2"}]),
        CHUNKS, allowed_tables=TABLES)
    assert [m["id"] for m in plan["metrics"]] == ["m2"]
    assert any(p["name"] == "销售额" and "cell(nodes.fetch, 0" in p["reason"] for p in problems)


def test_a_metric_whose_quotes_do_not_match_the_source_is_dropped():
    plan, problems = draftplan.normalize_plan(
        raw(metrics=[{**METRIC, "definition": {**METRIC["definition"],
                                               "citations": [{"chunk": "c1", "quote": "原文里没有这句"}]}}]),
        CHUNKS, allowed_tables=TABLES)
    assert plan["metrics"] == []
    assert any(p["name"] == "销售额" and "引用" in p["reason"] for p in problems)
    assert any(p["name"] == "metrics" for p in problems)


def test_metric_ids_are_unique_and_entry_fields_are_normalized():
    plan, _problems = draftplan.normalize_plan(
        raw(entry_fields=[{"name": "start", "type": "date", "required": 1, "default": "2026-09-01"}],
            metrics=[dict(METRIC), {**dict(METRIC), "name": "订单数"}]),
        CHUNKS, allowed_tables=TABLES)
    assert [m["id"] for m in plan["metrics"]] == ["m1", "m2"]
    assert plan["entry_fields"][0]["type"] == "string"
    assert plan["entry_fields"][0]["required"] is True


# ==========================================================================
# build_graph
# ==========================================================================

def test_build_graph_is_deterministic_and_passes_validation():
    plan, _ = draftplan.normalize_plan(raw(), CHUNKS, allowed_tables=TABLES)
    plan["datasource"] = "shop"
    first = draftplan.build_graph(plan, name="销售额口径", question="上周销售额")
    second = draftplan.build_graph(plan, name="销售额口径", question="上周销售额")
    assert first == second
    assert [node["id"] for node in first["nodes"]] == ["start", "fetch", "card", "write", "out"]
    fetch = next(node for node in first["nodes"] if node["id"] == "fetch")
    assert fetch["data"]["config"] == {"tool": "db_query__shop", "args": {"sql": plan["sql"]}}
    card = next(node for node in first["nodes"] if node["id"] == "card")
    assert card["data"]["config"]["metrics"][0]["id"] == "m1"
    contract = next(node for node in first["nodes"] if node["id"] == "out")["data"]["config"]["contract"]
    assert contract["metrics_from"] == "card" and contract["required"] == ["m1"]
    report = next(node for node in first["nodes"] if node["id"] == "write")["data"]["config"]
    assert report["numbers"] == "strict" and report["metrics_from"] == ["card"]
    result = validate_graph(GraphSpec.model_validate(first))
    assert result.ok, [issue.message for issue in result.issues if issue.level == "error"]


# ==========================================================================
# build_graph：多组
# ==========================================================================

START = {"name": "start", "type": "string", "required": True, "default": "2026-09-01",
         "description": "开始日期"}


def block(sql: str, metrics: list[dict], fields: list[dict] | None = None) -> dict:
    return {"sql": sql, "entry_fields": [dict(f) for f in ([START] if fields is None else fields)],
            "metrics": [dict(m) for m in metrics]}


def test_build_graph_gives_each_group_its_own_fetch_node_and_rewrites_expressions():
    """一张草稿图挂多个取数节点：第 i 组指标读 fetch_i，指标 id 跨组重排，出口要求全部指标。"""
    first = block("SELECT SUM(amount) AS gmv FROM orders",
                  [dict(METRIC), {**METRIC, "id": "m2", "name": "订单数",
                                  "expression": "cell(nodes.fetch, 0, 'cnt')"}])
    second = block("SELECT COUNT(DISTINCT customer_id) AS buyers FROM orders",
                   [{**METRIC, "id": "m1", "name": "买家数",
                     "expression": "cell(nodes.fetch, 0, 'buyers')"}])
    graph = draftplan.build_graph([first, second], name="销售口径", question="上周", datasource="shop")
    assert draftplan.build_graph([first, second], name="销售口径", question="上周",
                                 datasource="shop") == graph
    assert [node["id"] for node in graph["nodes"]] == ["start", "fetch_1", "fetch_2", "card",
                                                       "write", "out"]
    assert [edge["source"] for edge in graph["edges"]] == ["start", "fetch_1", "fetch_2", "card",
                                                           "write"]
    nodes = {node["id"]: node for node in graph["nodes"]}
    assert nodes["fetch_1"]["data"]["config"] == {
        "tool": "db_query__shop", "args": {"sql": first["sql"]}}
    assert nodes["fetch_2"]["data"]["config"]["args"]["sql"] == second["sql"]
    metrics = nodes["card"]["data"]["config"]["metrics"]
    assert [m["id"] for m in metrics] == ["m1", "m2", "m3"]
    assert [m["expression"] for m in metrics] == ["cell(nodes.fetch_1, 0, 'gmv')",
                                                  "cell(nodes.fetch_1, 0, 'cnt')",
                                                  "cell(nodes.fetch_2, 0, 'buyers')"]
    contract = nodes["out"]["data"]["config"]["contract"]
    assert contract == {"report_from": "write", "metrics_from": "card",
                        "required": ["m1", "m2", "m3"]}
    result = validate_graph(GraphSpec.model_validate(graph))
    assert result.ok, [issue.message for issue in result.issues if issue.level == "error"]


def test_build_graph_leaves_expressions_that_already_carry_an_index_alone():
    """已经写成 nodes.fetch_2 的式子不能再被改坏（改成了 nodes.fetch_1_2 这类）。"""
    first = block("SELECT SUM(amount) AS gmv FROM orders",
                  [{**METRIC, "expression": "cell(nodes.fetch_2, 0, 'gmv')"}])
    second = block("SELECT 1 AS gmv FROM orders", [dict(METRIC)])
    graph = draftplan.build_graph([first, second], name="x")
    metrics = next(node for node in graph["nodes"] if node["id"] == "card")["data"]["config"]["metrics"]
    assert metrics[0]["expression"] == "cell(nodes.fetch_2, 0, 'gmv')"
    assert metrics[1]["expression"] == "cell(nodes.fetch_2, 0, 'gmv')"


def test_build_graph_accepts_one_block_as_a_single_group():
    """老调用传一份草案（含 datasource）也要能建图：取数节点仍叫 fetch。"""
    graph = draftplan.build_graph({**block("SELECT 1 AS gmv FROM orders", [dict(METRIC)]),
                                   "datasource": "shop"}, name="销售额口径")
    assert [node["id"] for node in graph["nodes"]] == ["start", "fetch", "card", "write", "out"]
    fetch = next(node for node in graph["nodes"] if node["id"] == "fetch")
    assert fetch["data"]["config"]["tool"] == "db_query__shop"
    card = next(node for node in graph["nodes"] if node["id"] == "card")["data"]["config"]
    assert card["metrics"][0]["expression"] == "cell(nodes.fetch, 0, 'gmv')"


def test_build_graph_merges_entry_fields_and_widens_conflicts():
    """同名字段在各组里声明不一致时按更保守的一边合并，不抛错：一个字段的差异不该让整份草稿作废。"""
    same = block("SELECT 1 AS gmv FROM orders", [dict(METRIC)])
    merged = draftplan.build_graph([same, block("SELECT 2 AS cnt FROM orders", [{**METRIC, "id": "m2"}])],
                                   name="x")
    fields = next(node for node in merged["nodes"] if node["id"] == "start")["data"]["config"]["fields"]
    assert [f["name"] for f in fields] == ["start"]

    not_required = block("SELECT 1 AS gmv FROM orders", [dict(METRIC)],
                         fields=[{**START, "required": False}])
    fields, notes = draftplan.merge_entry_fields([same, not_required])
    assert fields[0]["required"] is True
    assert any("start" in note and "必填" in note for note in notes)

    typed = block("SELECT 1 AS gmv FROM orders", [dict(METRIC)],
                  fields=[{**START, "type": "integer"}])
    fields, notes = draftplan.merge_entry_fields([same, typed])
    assert fields[0]["type"] == "string"
    assert any("start" in note and "类型" in note for note in notes)
    assert draftplan.build_graph([same, typed], name="x")["nodes"]



# ==========================================================================
# plan：注入假模型
# ==========================================================================

class FakeModel:
    model_name = "fake-plan-model"

    def __init__(self, text: str) -> None:
        self.text = text
        self.messages: list = []

    async def ainvoke(self, messages):
        self.messages = messages
        return SimpleNamespace(content=self.text)


async def test_plan_hands_the_model_the_table_catalog_and_the_source_text():
    model = FakeModel(json.dumps({**raw(), "model_id": "ignored"}, ensure_ascii=False))
    result = await draftplan.plan(
        None, docs=[{"document_id": "d1", "title": "销售口径.md"}], chunks=CHUNKS,
        guidance="只看已支付", datasource={"name": "shop", "type": "sqlite"},
        tables_text="表 orders（订单）\n  amount  金额", model=model)
    assert result["model_id"] == "fake-plan-model"
    assert result["sql"].startswith("SELECT SUM(amount)")
    assert result["metrics"][0]["name"] == "销售额"
    human = model.messages[-1].content
    assert "表 orders" in human and "amount  金额" in human
    assert CHUNK in human and "只看已支付" in human


async def test_plan_refuses_when_no_draft_model_is_available(monkeypatch):
    from app.data import recipe_ai
    from app.ledger import docdraft

    async def resolve(_session):
        return None, "", SimpleNamespace(available=False, reason="还没有接入可用的起草模型")

    monkeypatch.setattr(recipe_ai, "resolve_draft_model", resolve)
    with pytest.raises(docdraft.DraftUnavailable, match="起草模型"):
        await draftplan.plan(None, docs=[], chunks={})


async def test_plan_salvages_metrics_when_the_reply_is_cut_off():
    """SQL 把输出额度吃光、JSON 在指标数组里断掉：抢回完整的指标和 SQL，并照实说「回复被截断」。"""
    text = ('{"entry_fields": [], "sql": "SELECT SUM(amount) AS gmv FROM orders", "metrics": ['
            + json.dumps(raw()["metrics"][0], ensure_ascii=False) + ', {"id": "m2", "name": "写到这里就断了')
    result = await draftplan.plan(None, docs=[], chunks={}, model=FakeModel(text))
    assert result["salvaged"] is True
    assert len(result["metrics"]) == 1 and result["metrics"][0]["name"] == "销售额"
    assert result["sql"].startswith("SELECT SUM(amount)")
    assert "截断" in result["diagnostics"]


async def test_plan_explains_what_came_back_when_metrics_is_not_a_list():
    """指标为空时不能只说「没有一条通过校验」：要说出模型到底交回了什么形状。"""
    model = FakeModel(json.dumps({"sql": "SELECT 1", "metrics": {}}, ensure_ascii=False))
    result = await draftplan.plan(None, docs=[], chunks={}, model=model)
    assert result["metrics"] == []
    assert "metrics 是对象" in result["diagnostics"]
    assert "sql" in result["diagnostics"]


async def test_plan_passes_uncovered_through():
    """一次只起草能共用一条 SQL 的一组，其余的写进 uncovered，下一轮再起草。"""
    model = FakeModel(json.dumps({**raw(), "uncovered": ["月活买家"]}, ensure_ascii=False))
    result = await draftplan.plan(None, docs=[], chunks={}, model=model)
    assert result["uncovered"] == ["月活买家"]


async def test_plan_tells_the_model_which_metrics_were_already_drafted():
    """续跑时把已经起草过的指标写进提示词，模型才会换下一组，不会重复起草前几轮。"""
    model = FakeModel(json.dumps(raw(), ensure_ascii=False))
    await draftplan.plan(None, docs=[], chunks={}, model=model, covered=["销售额", "订单数"])
    human = model.messages[-1].content
    assert "已经起草过的指标" in human
    assert "销售额、订单数" in human
    assert "不要重复起草" in model.messages[0].content


async def test_plan_reports_document_and_catalog_truncation_separately():
    """文档被截（doc_truncated）和表目录被截（catalog_truncated）要分开说：改文档、改目录是两件事。"""
    text = json.dumps(raw(), ensure_ascii=False)
    long_tables = "t" * (draftplan.MAX_TABLES_CHARS + 100)
    result = await draftplan.plan(None, docs=[], chunks={}, model=FakeModel(text), tables_text=long_tables)
    assert result["catalog_truncated"] is True
    assert result["doc_truncated"] is False
    assert result["truncated"] is True

    chunks = {"c1": {"ordinal": 1, "content": CHUNK, "document_id": "d1"},
              "c2": {"ordinal": 2, "content": "长" * (draftplan.MAX_INPUT_CHARS + 100), "document_id": "d1"}}
    result = await draftplan.plan(None, docs=[], chunks=chunks, model=FakeModel(text), tables_text="表 orders")
    assert result["doc_truncated"] is True
    assert result["catalog_truncated"] is False
    assert result["truncated"] is True

    result = await draftplan.plan(None, docs=[], chunks=CHUNKS, model=FakeModel(text), tables_text="表 orders")
    assert result["doc_truncated"] is False
    assert result["catalog_truncated"] is False
    assert result["truncated"] is False


async def test_plan_tells_the_model_which_conditions_are_fixed():
    """固定条件要原样交到模型手里，并明确说「写死在 SQL 里、不要做成入口」。"""
    model = FakeModel(json.dumps(raw(), ensure_ascii=False))
    await draftplan.plan(None, docs=[], chunks={}, model=model, fixed="line_code = 'FOAM01'")
    human = model.messages[-1].content
    assert "固定条件" in human and "FOAM01" in human
    assert "不要" in human and "入口" in human


def test_too_many_entry_fields_is_reported_not_silently_trimmed():
    """入口超过 4 个说明把不常变的条件也做成了参数：照实报出来，但不悄悄删（删了 SQL 还引用着）。"""
    fields = [{"name": "start", "type": "string", "required": True, "default": "2026-09-01"}]
    fields += [{"name": f"dim{i}", "type": "string", "required": False} for i in range(4)]
    plan, problems = draftplan.normalize_plan(raw(entry_fields=fields), CHUNKS, allowed_tables={"orders"})
    assert len(plan["entry_fields"]) == 5
    assert any(p["name"] == "入口字段" and "上限" in p["reason"] for p in problems)
