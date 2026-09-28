"""Copilot 的节点手册里要有口径卡、报告撰写和出具契约的写法，以及搭图规则。

手册里没有的节点，Copilot 就不会用：以前手册里连口径卡都没有，只在数据源那一节
提了一句「取数结果要进口径卡」，于是它搭出来的图全是 agent → llm → output，叙述里
的数字没有一个能回指。
"""
from __future__ import annotations

import json

import pytest

from app.api.copilot import NODE_REFERENCE, GenerateIn, _user_message
from app.engine.evidence import render_number


def test_node_reference_covers_calibers_reports_and_contracts():
    ref = NODE_REFERENCE
    for words in (
        ["- metrics：口径卡", "expression", "decimals", "format", "on_missing"],
        ["- report：报告撰写", "[[m:指标id]]", "[[i:", "metrics_from", "on_violation", "不要用 llm"],
        ["report_from", "required", "strict"],
        ["搭图规则", "口径卡 → report → output", "SUM / COUNT", "code 只做格式转换"],
    ):
        assert all(w in ref for w in words), [w for w in words if w not in ref]
    # percent_of_ratio 的小数位按比率算：照「两位小数」写 2 的话 0.0235 显示成 2%，0.004 直接显示不出来
    assert "percent_of_ratio 的 decimals 按比率算：要显示两位百分比（2.35%）写 4" in ref
    assert render_number(0.0235, decimals=4, fmt="percent_of_ratio") == "2.35%"
    assert render_number(0.0235, decimals=2, fmt="percent_of_ratio") == "2%"
    # 成果字段要原样引用报告：前后拼了字就没法逐段对应证据
    assert "{{ nodes.报告节点id.text }}" in ref
    # 老规矩还在
    assert "不要写 max_steps" in ref


def test_report_model_follows_the_same_model_rule():
    assert "llm / agent / supervisor / report 的 config.model" in NODE_REFERENCE


def test_answer_branch_mentions_traceable_reports():
    text = _user_message(GenerateIn(instruction="上周销售额多少", intent="answer"), patch=False)
    assert "report" in text and "口径卡" in text and "report_from" in text
    # 二期起问数也带报告：agent 查过的单元格能被 report 直接 [[v:]] 引用，答案里的数逐个点得开
    assert "input → agent → report → output" in text


# --------------------------------------------------------------------------
# 二期：agent 的结构化字段、单元格引用、cell()、沙箱代码的角色
# --------------------------------------------------------------------------


def test_node_reference_covers_phase_two_evidence():
    ref = NODE_REFERENCE
    for words in (
        ["output_schema", "cite_fields: true", "vars."],                        # agent 交结构化字段
        ["[[v:Q1.r0.", "[[table:Q1 cols=", "rows=0-4"],                          # 报告引用单元格、整表
        ["cell(nodes.", "cell() "],                                             # 口径卡直接取查询结果的一格
        ["evidence_role", "source", "compute"],                                 # 沙箱代码的角色
        ["cells: true"],                                                        # 受管出具要声明 cells
        ["不要用 transform 解析 agent / llm 的文字"],
    ):
        assert all(w in ref for w in words), [w for w in words if w not in ref]
    # 旧的「transform 解析查询结果」「agent 的回答进不了口径卡」不再是推荐写法
    assert "agent 的回答是自由文本，进不了口径卡" not in ref
    assert 'template="{{ nodes.取数节点id }}"' not in ref


def test_answer_branch_builds_agent_report_for_numbers_only():
    text = _user_message(GenerateIn(instruction="上周销售额多少", intent="answer"), patch=False)
    assert "input → agent → report → output" in text and "[[v:Q1.r0." in text
    assert "只问表结构、清单" in text and "不加 report" in text
    assert "不要用 transform 解析 agent / llm 的文字" in text
    assert "output_schema" in text and "cite_fields" in text


def _source(**tables):
    from types import SimpleNamespace

    return SimpleNamespace(name="shop", schema_cache={"tables": {
        name: {"qualified": name, "columns": [{"name": c} for c in cols]} for name, cols in tables.items()}})


def _spec(*nodes):
    from app.engine.schema import GraphSpec

    return GraphSpec.model_validate({"nodes": list(nodes), "edges": [
        {"source": a["id"], "target": b["id"]} for a, b in zip(nodes, nodes[1:])]})


def _node(nid, ntype, **config):
    return {"id": nid, "type": ntype, "data": {"label": nid, "config": config}}


def test_sql_columns_missing_from_the_schema_are_sent_back():
    from app.api.copilot import authored_issues

    shop = _source(orders=["id", "week", "amount", "refunded"])
    fetch = _node("fetch", "tool", tool="db_query__shop",
                  args={"sql": "SELECT SUM(amt) AS gmv, COUNT(*) AS n FROM orders WHERE wk = '{{ input.week }}'"})
    spec = _spec(_node("start", "input"), fetch, _node("out", "output", fields=[]))
    [issue] = authored_issues(spec, [shop])
    assert issue["level"] == "error" and issue["code"] == "sql_unknown_column"
    assert issue["node_id"] == "fetch" and issue["field"] == "args.sql"
    assert "amt、wk" in issue["message"] and "amount" in issue["message"] and "db_schema__shop" in issue["message"]

    good = _node("fetch", "tool", tool="db_query__shop",
                 args={"sql": "SELECT SUM(amount) AS gmv, COUNT(*) AS n FROM orders WHERE week = '{{ input.week }}' "
                              "GROUP BY week ORDER BY gmv DESC"})
    assert authored_issues(_spec(_node("start", "input"), good, _node("out", "output", fields=[])), [shop]) == []
    # 结构没探查过、或者表不在结构里：判断不了，不打回
    assert authored_issues(spec, [_source()]) == []
    other = _node("fetch", "tool", tool="db_query__shop", args={"sql": "SELECT amt FROM refunds"})
    assert authored_issues(_spec(_node("start", "input"), other, _node("out", "output", fields=[])), [shop]) == []


def test_sql_the_user_wrote_and_this_round_did_not_touch_is_left_alone():
    from app.api.copilot import authored_issues

    shop = _source(orders=["id", "amount"])
    fetch = _node("fetch", "tool", tool="db_query__shop", args={"sql": "SELECT amt FROM orders"})
    spec = _spec(_node("start", "input"), fetch, _node("out", "output", fields=[]))
    assert authored_issues(spec, [shop], baseline=[fetch]) == []
    assert authored_issues(spec, [shop], baseline=[{**fetch, "data": {"config": {"tool": "db_query__shop"}}}])


def test_parsing_agent_text_is_sent_back():
    from app.api.copilot import authored_issues

    spec = _spec(
        _node("start", "input"),
        _node("query_logs", "agent", prompt="查", tools=["db_query__shop"], assign_to="answer"),
        _node("parse", "transform", mode="json", template="{{ nodes.query_logs.text }}", assign_to="q"),
        _node("card", "metrics", metrics=[{"id": "n", "expression": "vars.q.n"}]),
        _node("out", "output", fields=[]),
    )
    [issue] = authored_issues(spec, [])
    assert issue["code"] == "parse_model_text" and issue["level"] == "error" and issue["node_id"] == "parse"
    assert "cite_fields" in issue["message"] and "不要用整形节点解析 agent / llm 的文字" in issue["message"]


# --------------------------------------------------------------------------
# 端到端：搭完自查，SQL 里猜的列名被打回、改好才自动运行
# --------------------------------------------------------------------------


class _Scripted:
    """第一轮吐 first；收到自查请求（human 里有「上线前自查」）时吐 fix。"""

    def __init__(self, first, fix):
        self.first, self.fix = first, fix
        self.calls: list[str] = []

    async def astream(self, messages):
        from langchain_core.messages import AIMessageChunk

        human = next(text for role, text in messages if role == "human")
        self.calls.append(human)
        for op in (self.fix if "上线前自查" in human else self.first):
            yield AIMessageChunk(content=json.dumps(op, ensure_ascii=False) + "\n")


def _ops(sql):
    return [
        {"op": "plan", "summary": "查销售额"},
        {"op": "add_node", "node": {"id": "start", "type": "input", "label": "输入", "config": {}}},
        {"op": "add_node", "node": {"id": "fetch", "type": "tool", "label": "取数",
                                    "config": {"tool": "db_query__sales", "args": {"sql": sql}}}},
        {"op": "add_edge", "edge": {"source": "start", "target": "fetch"}},
        {"op": "add_node", "node": {"id": "out", "type": "output", "label": "成果",
                                    "config": {"fields": [{"name": "r", "value": "{{ nodes.fetch }}"}]}}},
        {"op": "add_edge", "edge": {"source": "fetch", "target": "out"}},
        {"op": "done", "explanation": "查一下", "run": True},
    ]


@pytest.fixture
async def sales(tmp_path):
    from sqlalchemy import select

    from app.db.base import SessionLocal
    from app.db.models import DataSource

    cache = {"tables": {"orders": {"qualified": "orders", "columns": [
        {"name": "id", "type": "INTEGER"}, {"name": "week", "type": "TEXT"}, {"name": "amount", "type": "REAL"}]}}}
    async with SessionLocal() as session:
        row = (await session.execute(select(DataSource).where(DataSource.name == "sales"))).scalar_one_or_none()
        if row is None:
            row = DataSource(name="sales", kind="sqlite", database=str(tmp_path / "sales.db"), readonly=True)
            session.add(row)
        row.enabled, row.schema_cache = True, cache
        await session.commit()
    yield


async def test_guessed_columns_are_sent_back_and_fixed_before_running(sales, monkeypatch):
    import app.api.copilot as copilot
    from httpx import ASGITransport, AsyncClient

    from app.main import app

    model = _Scripted(_ops("SELECT SUM(sales_amount) AS gmv FROM orders"),
                      [{"op": "update_node", "id": "fetch",
                        "config": {"args": {"sql": "SELECT SUM(amount) AS gmv FROM orders"}}},
                       {"op": "done", "explanation": "改好了"}])

    async def _model(*_a, **_k):
        return model, "mock-fast"

    monkeypatch.setattr(copilot, "get_chat_model", _model)
    events: list[dict] = []
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        async with client.stream("POST", "/api/copilot/generate-stream",
                                 json={"instruction": "上周销售额多少", "intent": "answer"}) as r:
            async for line in r.aiter_lines():
                if line.startswith("data: "):
                    events.append(json.loads(line[6:]))
    checks = [e for e in events if e["op"] == "check"]
    assert [c["status"] for c in checks] == ["repairing", "passed"], checks
    [issue] = checks[0]["issues"]
    assert issue["code"] == "sql_unknown_column" and "sales_amount" in issue["message"]
    assert "sales_amount" in model.calls[1] and "amount" in model.calls[1]
    final = events[-1]
    sql = next(n for n in final["graph"]["nodes"] if n["id"] == "fetch")["data"]["config"]["args"]["sql"]
    assert sql == "SELECT SUM(amount) AS gmv FROM orders" and final["autorun"] is True


def test_model_text_inside_template_quotes_is_sent_back_with_the_json_filter_fix():
    """模板把 agent 的文字放在引号里拼 JSON：打回时给的改法是 | json，不是去换结构化输出。"""
    from app.api.copilot import authored_issues

    spec = _spec(
        _node("start", "input"),
        _node("query_logs", "agent", prompt="查", tools=["db_query__shop"], assign_to="answer"),
        _node("parse", "transform", mode="json", template='{"summary": "{{ nodes.query_logs.text }}"}',
              assign_to="q"),
        _node("out", "output", fields=[]),
    )
    [issue] = authored_issues(spec, [])
    assert issue["code"] == "parse_model_text" and issue["level"] == "error"
    assert "{{ nodes.query_logs.text | json }}" in issue["message"]
    assert "cite_fields" not in issue["message"] and "不要用整形节点解析" not in issue["message"]


#: 都是能跑的 SQL：关键字、运算符、排序规则名、窗口名、表达式后面不写 AS 的别名，都不是列
VALID_SQL = [
    "SELECT id FROM orders ORDER BY status COLLATE NOCASE",
    "SELECT id FROM orders WHERE status COLLATE NOCASE = 'x'",
    "SELECT id FROM orders ORDER BY region COLLATE RTRIM",
    "SELECT id FROM orders WHERE status = 'x' COLLATE utf8mb4_bin",
    "SELECT created_at AT TIME ZONE 'UTC' FROM orders",
    "SELECT id FROM orders WHERE status ISNULL",
    "SELECT id FROM orders WHERE status NOTNULL",
    "SELECT id FROM orders WHERE region GLOB 'E*'",
    "SELECT id FROM orders WHERE region REGEXP '^E'",
    "SELECT id FROM orders WHERE region RLIKE '^E'",
    "SELECT id FROM orders WHERE region SIMILAR TO 'E%'",
    "SELECT id FROM orders WHERE region SOUNDS LIKE 'x'",
    "SELECT amount DIV 2 FROM orders",
    "SELECT amount MOD 2 FROM orders",
    "SELECT id FROM orders WHERE amount > 0 XOR status = 'x'",
    "SELECT region, SUM(amount) FROM orders GROUP BY GROUPING SETS ((region), ())",
    "SELECT FIRST_VALUE(amount) IGNORE NULLS OVER (ORDER BY id) FROM orders",
    "SELECT FIRST_VALUE(amount) RESPECT NULLS OVER (ORDER BY id) FROM orders",
    "SELECT RANK() OVER (ORDER BY amount RANGE BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW EXCLUDE TIES) FROM orders",
    "SELECT RANK() OVER (ORDER BY amount GROUPS BETWEEN 1 PRECEDING AND CURRENT ROW EXCLUDE NO OTHERS) FROM orders",
    "SELECT id, SUM(amount) OVER w FROM orders WINDOW w AS (PARTITION BY region)",
    "SELECT SUM(amount) OVER w AS total FROM orders WINDOW w AS (PARTITION BY region)",
    "SELECT id FROM orders WHERE amount > 0 MINUS SELECT id FROM orders",
    "SELECT id FROM orders WHERE created_at >= NOW() - INTERVAL 7 DAYS",
    "SELECT id FROM orders WHERE status = ANY (ARRAY['a'])",
    "SELECT id FROM orders WHERE MATCH (status) AGAINST ('x' IN BOOLEAN MODE)",
    "SELECT id FROM orders WHERE created_at OVERLAPS created_at",
    "SELECT id FROM orders WHERE status = 'x' FOR UPDATE SKIP LOCKED",
    "SELECT id FROM orders WHERE status = 'x' FOR SHARE NOWAIT",
    "SELECT id FROM orders WHERE status = 'x' LOCK IN SHARE MODE",
    "SELECT SQL_CALC_FOUND_ROWS id FROM orders",
    "SELECT DISTINCTROW id FROM orders",
    "SELECT id FROM orders WHERE region = N'x'",
    "SELECT CONVERT(region USING utf8mb4) FROM orders",
    "SELECT id FROM orders WHERE status = 'x' START WITH id = 1 CONNECT BY PRIOR id = customer_id",
    "SELECT [Total Sales] FROM orders",
    "SELECT id FROM orders WHERE region = @wanted",
    "SELECT id FROM orders WHERE created_at > DATEADD(dd, -7, GETDATE())",
    "SELECT DATEDIFF(wk, created_at, GETDATE()) FROM orders",
    # 表达式后面不写 AS 的别名：前面是 END、NULL、TRUE、CURRENT_DATE 这种「一个值到此为止」的词
    "SELECT CASE WHEN status = 'x' THEN 1 ELSE 0 END flag FROM orders",
    "SELECT CASE WHEN status = 'x' THEN 1 ELSE 0 END flag, id FROM orders",
    "SELECT NULL placeholder, id FROM orders",
    "SELECT TRUE flag, id FROM orders",
    "SELECT CURRENT_DATE today, id FROM orders",
]


@pytest.mark.parametrize("sql", VALID_SQL)
def test_valid_sql_is_not_sent_back(sql):
    from app.api.copilot import sql_unknown_columns

    shop = _source(orders=["id", "customer_id", "amount", "status", "created_at", "region"])
    assert sql_unknown_columns(sql, shop) is None, sql_unknown_columns(sql, shop)


@pytest.mark.parametrize("sql, unknown", [
    # 别名照样认得出来，打错的列照样打回
    ("SELECT CASE WHEN statsu = 'x' THEN 1 ELSE 0 END flag FROM orders", ["statsu"]),
    ("SELECT id FROM orders ORDER BY regoin COLLATE NOCASE", ["regoin"]),
    ("SELECT id, SUM(amt) OVER w FROM orders WINDOW w AS (PARTITION BY region)", ["amt"]),
    ("SELECT id FROM orders WHERE statsu ISNULL", ["statsu"]),
    # 日期部分的缩写只在 DATEADD 这类函数的第一个参数上放过
    ("SELECT DATEADD(dd, 1, created_at) FROM orders WHERE wk = 1", ["wk"]),
])
def test_typos_next_to_those_constructs_are_still_caught(sql, unknown):
    from app.api.copilot import sql_unknown_columns

    shop = _source(orders=["id", "customer_id", "amount", "status", "created_at", "region"])
    found = sql_unknown_columns(sql, shop)
    assert found is not None and found[0] == unknown, found
