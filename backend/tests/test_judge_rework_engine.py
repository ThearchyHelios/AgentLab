"""结论句裁判改造（JR）的内核：判定拆档、补全证据摘录、理由不硬截断。

- 判定拆成 contradicted（证据相矛盾）和 insufficient（证据不足）：只有证据和句子冲突才判 contradicted，
  证据里没有相关信息判 insufficient，并写明缺的是什么（missing）。新判的不再产出 unsupported：
  模型照旧写 unsupported 的，按更严的 contradicted 收下
- 讲方法、讲结构的句子只挂了表名、字段名，裁判以前只拿到名字：表附上字段清单和类型（最多 60 个，
  快照不完整要注明），字段附上类型和所属的表，口径卡指标附上输入取自哪次查询、哪一格和那次查询的 SQL。
  一律只经封存范围内的 loader 读工件，遮罩的列不出现
- 理由上限 120 个字，超长时在句子边界截断加「…」；裁判规则要求 80 个字以内

所有模型调用都是 mock 剧本，不碰真接口。
"""
from __future__ import annotations

import json
import re

import pytest
from langchain_core.messages import AIMessage
from sqlalchemy import delete

from app.db.base import SessionLocal
from app.db.models import DataSource, Setting
from app.engine import judge as judge_mod
from app.engine.evidence import build_catalog, compose_doc, iter_units
from app.engine.judge import (
    JUDGE_ROLE,
    JUDGE_SCHEMA,
    JUDGED,
    SPEND_KEY,
    SQL_CHARS,
    Budget,
    judge_doc,
    prepare,
    source_masks,
    summarize,
)
from app.providers.factory import ModelSpec
from app.providers.mock_model import MockChatModel

PRICED = "claude-sonnet-5"
UNLIMITED = Budget(max_claims=None, max_cost_usd=None, timeout_s=None, daily_max_usd=None)
SPEC = ModelSpec(model=PRICED)

SOURCE = "warehouse"
SCHEMA_ART, Q1_ART, Q2_ART, CARD_ART = "5" * 64, "a" * 64, "b" * 64, "c" * 64
SQL1 = "SELECT COUNT(DISTINCT member_no) AS active_users FROM orders WHERE created_at >= '2026-09-01'"
SQL2 = "SELECT region, SUM(amount) AS gmv FROM orders GROUP BY region"
ORDER_COLS = [("id", "INTEGER"), ("member_no", "TEXT"), ("region", "TEXT"), ("amount", "REAL"),
              ("phone", "TEXT"), ("created_at", "TEXT")]


def schema_snapshot(columns=ORDER_COLS, *, truncated=False) -> dict:
    return {"source": SOURCE, "synced_at": "2026-09-01T00:00:00+00:00", "schema": None,
            "tables": {"orders": {"columns": [{"name": n, "type": t, "nullable": True} for n, t in columns],
                                  "primary_key": ["id"], "comment": None, "is_view": False},
                       "refunds": {"columns": [{"name": "id", "type": "INTEGER"}, {"name": "amount", "type": "REAL"}],
                                   "primary_key": ["id"], "is_view": False}},
            **({"truncated": True, "total": 500} if truncated else {})}


def query_entry(art: str, columns: list[str], tables: list[str]) -> dict:
    return {"kind": "query", "node_id": "fetch", "exec": 1, "artifact": art, "tool": f"db_query__{SOURCE}",
            "source": SOURCE, "columns": columns, "rows": 1, "truncated": False, "schema_artifact": SCHEMA_ART,
            "tables": tables}


def schema_entry(snapshot: dict) -> dict:
    tables = {name: [c["name"] for c in meta["columns"]] for name, meta in snapshot["tables"].items()}
    return {"kind": "schema", "node_id": "fetch", "exec": 1, "artifact": SCHEMA_ART, "source": SOURCE,
            "tables": tables, **({"truncated": True, "total": 500} if snapshot.get("truncated") else {})}


def snaps(snapshot=None, **extra) -> dict:
    out = {SCHEMA_ART: snapshot or schema_snapshot(),
           Q1_ART: {"columns": ["active_users"], "rows": [[321]], "sql": SQL1, "source": SOURCE},
           Q2_ART: {"columns": ["region", "gmv"], "rows": [["华东", 100.0]], "sql": SQL2, "source": SOURCE}}
    out.update(extra)
    return out


def entity_catalog(snapshot=None) -> dict:
    snapshot = snapshot or schema_snapshot()
    return build_catalog(nodes={}, ledger=[query_entry(Q1_ART, ["active_users"], ["orders"]),
                                           query_entry(Q2_ART, ["region", "gmv"], ["orders"]), schema_entry(snapshot)])


def excerpts(text: str, *, snapshot=None, loader=None, masked=None) -> dict[str, str]:
    cat = entity_catalog(snapshot)
    doc = compose_doc(text, cat)
    store = snaps(snapshot)
    return prepare(doc, cat, loader=loader or store.get, masked=masked).excerpts


def field_line(excerpt: str) -> str:
    [line] = [x for x in excerpt.splitlines() if x.startswith("字段清单")]
    return line


# --------------------------------------------------------------------------
# 判定拆档：取值、规则、schema
# --------------------------------------------------------------------------


def test_the_schema_offers_contradicted_and_insufficient_instead_of_unsupported():
    item = JUDGE_SCHEMA["properties"]["items"]["items"]
    assert item["properties"]["verdict"]["enum"] == list(JUDGED) == [
        "supported", "partial", "contradicted", "insufficient", "not_a_claim"]
    assert "unsupported" not in JUDGED
    assert "missing" in item["properties"] and "missing" in item["required"]
    assert "80" in item["properties"]["rationale"]["description"]


def test_the_rules_separate_conflict_from_missing_information():
    rules = judge_mod.JUDGE_RULES
    assert "unsupported" not in rules, "新判的一律用新取值"
    assert "contradicted" in rules and "insufficient" in rules and "missing" in rules
    # 只有冲突才判 contradicted；没有相关信息判 insufficient，并写明缺什么
    [conflict] = [line for line in rules.splitlines() if line.startswith("- contradicted")]
    assert "冲突" in conflict
    [lacking] = [line for line in rules.splitlines() if line.startswith("- insufficient")]
    assert "没有" in lacking
    assert "80 个字" in rules and "60 个字" not in rules
    assert "表结构快照不完整" in rules, "快照不完整时「某字段不存在」只能判 insufficient"
    assert "数字已经由系统核对过，不要复算" in rules, "旧规则保留"


def test_count_keys_keep_unsupported_for_old_data():
    assert set(judge_mod.COUNT_KEYS) == {"supported", "partial", "contradicted", "insufficient", "unsupported", "not_a_claim",
                               "unjudged"}


# --------------------------------------------------------------------------
# 判定拆档：调用与收下
# --------------------------------------------------------------------------

RAW = ("活跃用户数由 [[t:orders]] 按 [[c:orders.member_no]] 去重计数得出。"
       "各区销售额按 [[c:orders.region]] 汇总。[[see:Q2]]"
       "华东的销售额是全国最高的，领先第二名一倍。[[see:Q2]]"
       "订单表里没有退款字段。[[see:t:orders]]")


class Judge:
    """裁判剧本：items(units, request) 给出每句的条目；calls 记下每次交给裁判的请求原文。"""

    def __init__(self, monkeypatch, items, *, model_id=PRICED):
        self.calls: list[str] = []

        def decide(model, messages):
            assert JUDGE_ROLE in str(messages[0].content)
            request = str(messages[-1].content)
            self.calls.append("\n".join(str(m.content) for m in messages))
            units = re.findall(r"^\[(u\d+)\]", request, re.M)
            return AIMessage(content=json.dumps({"items": items(units)}, ensure_ascii=False))

        monkeypatch.setattr(MockChatModel, "_decide", decide)

        async def fake_model(session, spec):
            return MockChatModel(model_name=model_id), model_id

        monkeypatch.setattr(judge_mod, "get_chat_model", fake_model)


def ids_of(doc) -> dict[str, str]:
    md = doc["markdown"]
    return {md[u["span"][0]:u["span"][1]].strip(): u["id"] for _, u in iter_units(doc)}


@pytest.fixture(autouse=True)
async def clean_settings():
    async with SessionLocal() as session:
        await session.execute(delete(Setting).where(Setting.key.in_([SPEND_KEY, "judge", "copilot"])))
        await session.commit()
    yield


async def test_contradicted_and_insufficient_come_back_with_missing(monkeypatch):
    cat = entity_catalog()
    doc = compose_doc(RAW, cat)
    ids = ids_of(doc)
    method, grouped, ranked, absent = (ids[t] for t in list(ids)[:4])
    script = {
        method: {"verdict": "supported", "rationale": "Q1 的 SQL 写的是 COUNT(DISTINCT member_no)", "missing": ""},
        grouped: {"verdict": "insufficient", "rationale": "摘录里没有这次汇总的 SQL", "missing": "SQL"},
        ranked: {"verdict": "contradicted", "rationale": "Q2 里华东不是最高", "missing": "应当被丢掉"},
        absent: {"verdict": "insufficient", "rationale": "", "missing": ""},
    }
    Judge(monkeypatch, lambda units: [{"unit": u, "used": [], **script[u]} for u in units])
    outcome = await judge_doc(doc, cat, spec=SPEC, budget=UNLIMITED, loader=snaps().get)
    v = outcome["verdicts"]
    assert v[grouped]["status"] == "insufficient" and v[grouped]["missing"] == "SQL"
    assert v[ranked]["status"] == "contradicted" and "missing" not in v[ranked], "只有 insufficient 带 missing"
    assert v[absent]["status"] == "insufficient" and "missing" not in v[absent], "没写缺什么就不编"
    assert v[method]["status"] == "supported" and "missing" not in v[method]
    assert set(v[grouped]) == {"status", "rationale", "judge", "post_seal", "used", "missing", "rule_version"}
    assert outcome["counts"] == {"supported": 1, "partial": 0, "contradicted": 1, "insufficient": 2,
                                 "not_a_claim": 0, "unjudged": 0, "unsupported": 0}


async def test_a_model_still_saying_unsupported_is_taken_as_contradicted(monkeypatch):
    """新判的不再产出 unsupported：模型照旧写 unsupported 的按更严的 contradicted 收下，不当格式错误放过。"""
    cat = entity_catalog()
    doc = compose_doc(RAW, cat)
    Judge(monkeypatch, lambda units: [{"unit": u, "verdict": "unsupported", "rationale": "对不上", "used": []}
                                      for u in units])
    outcome = await judge_doc(doc, cat, spec=SPEC, budget=UNLIMITED, loader=snaps().get)
    assert {x["status"] for x in outcome["verdicts"].values()} == {"contradicted"}
    assert outcome["counts"]["unsupported"] == 0 and outcome["counts"]["contradicted"] == 4


def test_summary_counts_always_carry_every_key():
    """缓存里的旧判定只有旧的五个键：摘要照样补齐 contradicted、insufficient。"""
    outcome = {"counts": {"supported": 1, "partial": 0, "unsupported": 1, "not_a_claim": 0, "unjudged": 0},
               "candidates": 2, "screened": [], "unjudged": {}, "limits_hit": [], "gaps": []}
    summary = summarize(outcome, mode="inline", writer_model="writer-x")
    assert summary["counts"] == {"supported": 1, "partial": 0, "contradicted": 0, "insufficient": 0,
                                 "not_a_claim": 0, "unjudged": 0, "unsupported": 1}
    assert summary["writer_model"] == "writer-x"


# --------------------------------------------------------------------------
# 理由不硬截断
# --------------------------------------------------------------------------


def test_a_rationale_under_the_cap_is_kept_whole():
    text = "摘录里" + "有对应的指标和查询" * 10 + "。"                   # 94 个字：旧规则会截在 60 字的半句中间
    assert 60 < len(text) <= judge_mod.RATIONALE_MAX == 120
    assert judge_mod.clip_text(text) == text


def test_a_long_rationale_is_cut_at_a_sentence_boundary():
    first = "Q2 的 SQL 按 region 汇总，华东 100 元排第一"
    text = first + "；" + "第二名的数字摘录里没有给出，" * 12
    got = judge_mod.clip_text(text)
    assert len(got) <= judge_mod.RATIONALE_MAX and got.endswith("…")
    body = got[:-1]
    assert text.startswith(body) and text[len(body)] in "。；，", f"截在句子边界：{got}"


def test_a_long_run_without_punctuation_is_not_cut_inside_a_word():
    text = " ".join(["evidence"] * 30)
    got = judge_mod.clip_text(text)
    assert len(got) <= judge_mod.RATIONALE_MAX and got.endswith("…")
    assert got[:-1].rstrip().split(" ")[-1] == "evidence", got


async def test_the_judge_keeps_a_long_rationale_up_to_the_cap(monkeypatch):
    cat = entity_catalog()
    doc = compose_doc("各区销售额按 [[c:orders.region]] 汇总。[[see:Q2]]", cat)
    long = "Q2 的 SQL 是按 region 分组求和，" * 3 + "句子说的汇总方式和 SQL 一致，" * 8
    Judge(monkeypatch, lambda units: [{"unit": u, "verdict": "supported", "rationale": long, "used": []}
                                      for u in units])
    outcome = await judge_doc(doc, cat, spec=SPEC, budget=UNLIMITED, loader=snaps().get)
    [v] = outcome["verdicts"].values()
    assert 60 < len(v["rationale"]) <= judge_mod.RATIONALE_MAX and v["rationale"].endswith("…")
    assert long.startswith(v["rationale"][:-1])


# --------------------------------------------------------------------------
# 补全证据摘录：表
# --------------------------------------------------------------------------


def test_a_table_excerpt_lists_its_fields_and_types():
    ex = excerpts("活跃用户数由 [[t:orders]] 去重计数得出。")
    table = ex["t:orders"]
    line = field_line(table)
    for name, kind in ORDER_COLS:
        assert f"{name} {kind}" in line, line
    assert "COUNT(DISTINCT member_no)" in table, "出现过的查询 SQL 保留"
    assert "表结构快照不完整" not in table


def test_the_field_list_stops_at_sixty():
    columns = [(f"col_{i:02d}", "TEXT") for i in range(75)]
    ex = excerpts("活跃用户数由 [[t:orders]] 去重计数得出。", snapshot=schema_snapshot(columns))
    line = field_line(ex["t:orders"])
    listed = re.findall(r"col_\d\d", line)
    assert judge_mod.FIELDS_MAX == 60 and listed == [f"col_{i:02d}" for i in range(60)]
    assert "另有 15 个" in line


def test_masked_fields_are_never_listed():
    ex = excerpts("活跃用户数由 [[t:orders]] 去重计数得出。", masked={SOURCE: ["Phone"]})
    table = ex["t:orders"]
    assert "phone" not in field_line(table).lower() and "member_no TEXT" in field_line(table)
    assert "遮罩" in table, "说明有字段按遮罩没列出：裁判不能据此说表里没有这个字段"


def test_masks_recorded_in_a_query_snapshot_also_hide_fields():
    """数据源改名、删掉了，现在查不到遮罩：查询当时记下的遮罩照样生效（证据面板同一个口径）。"""
    cat = entity_catalog()
    doc = compose_doc("活跃用户数由 [[t:orders]] 去重计数得出。", cat)
    store = snaps(**{Q1_ART: {"columns": ["active_users"], "rows": [[321]], "sql": SQL1, "source": SOURCE,
                              "mask_columns": ["phone"]}})
    table = prepare(doc, cat, loader=store.get).excerpts["t:orders"]
    assert "phone" not in field_line(table).lower()


def test_a_truncated_snapshot_is_called_out():
    ex = excerpts("活跃用户数由 [[t:orders]] 去重计数得出。", snapshot=schema_snapshot(truncated=True))
    table = ex["t:orders"]
    assert "表结构快照不完整" in table and "id INTEGER" in field_line(table)


def test_only_the_sealed_loader_feeds_the_field_list():
    """schema 快照不在封存范围里（loader 取不到）：一个字段都不给，照实说没有字段清单。"""
    store = snaps()
    ex = excerpts("活跃用户数由 [[t:orders]] 去重计数得出。", loader=lambda a: None if a == SCHEMA_ART else store.get(a))
    table = ex["t:orders"]
    assert "INTEGER" not in table and "member_no TEXT" not in table
    assert "没有" in field_line(table)


def test_new_excerpt_content_counts_toward_the_estimate():
    cat = entity_catalog()
    doc = compose_doc("活跃用户数由 [[t:orders]] 去重计数得出。", cat)
    store = snaps()
    full = prepare(doc, cat, loader=store.get)
    bare = prepare(doc, cat, loader=lambda a: None if a == SCHEMA_ART else store.get(a))
    assert full.cost(PRICED, full.cands) > bare.cost(PRICED, bare.cands)


# --------------------------------------------------------------------------
# 补全证据摘录：字段
# --------------------------------------------------------------------------


def test_a_column_excerpt_carries_its_type_and_table():
    column = excerpts("活跃用户数按 [[c:orders.member_no]] 去重计数得出。")["c:orders.member_no"]
    assert "类型：TEXT" in column and "所属的表：orders" in column
    assert "COUNT(DISTINCT member_no)" in column, "出现过的查询 SQL 保留"


def test_a_result_column_takes_its_type_from_the_query_snapshot():
    """聚合的别名（gmv）不在表结构里：类型取查询快照记下的列类型。"""
    cat = entity_catalog()
    doc = compose_doc("汇总在 [[c:gmv]] 这一列。[[see:Q2]]", cat)
    store = snaps(**{Q2_ART: {"columns": ["region", "gmv"], "rows": [["华东", 100.0]], "sql": SQL2, "source": SOURCE,
                              "column_types": {"region": "text", "gmv": "number"}}})
    column = prepare(doc, cat, loader=store.get).excerpts["c:gmv"]
    assert "类型：number" in column


# --------------------------------------------------------------------------
# 补全证据摘录：口径卡指标
# --------------------------------------------------------------------------

LONG_SQL = "SELECT COUNT(DISTINCT member_no) AS active_users FROM orders WHERE " + " AND ".join(
    f"flag_{i} = 1" for i in range(80))
CARD = {"kind": "metric_set", "caliber": "周报口径", "caliber_version": "v2", "metrics": [
    {"id": "active", "name": "活跃用户数", "unit": "人", "value": 321, "decimals": 0, "format": "plain", "status": "ok",
     "expression": "cell(nodes.fetch, 0, 'active_users')", "rendered": "321人",
     "inputs": [{"path": "cell(nodes.fetch, 0, 'active_users')", "value": 321, "node_id": None, "via": "tool_cell",
                 "artifact": Q1_ART, "locator": {"row": 0, "column": "active_users"}, "status": "ok"},
                {"path": "vars.target", "value": 300, "node_id": "plan", "via": "transform", "status": "ok"}]}]}


def metric_catalog():
    ledger = [{"kind": "metric_set", "node_id": "caliber", "exec": 1, "artifact": CARD_ART, "caliber": "周报口径",
               "version": "v2", "metrics": ["active"]}, query_entry(Q1_ART, ["active_users"], ["orders"])]
    return build_catalog(nodes={"caliber": {**CARD, "artifact": CARD_ART}}, ledger=ledger)


def test_a_metric_excerpt_names_the_query_and_cell_of_its_input_and_that_sql():
    cat = metric_catalog()
    doc = compose_doc("活跃用户数 [[m:active]]，按会员号去重计数。[[see:m:active]]", cat)
    store = {**snaps(), CARD_ART: CARD}
    metric = prepare(doc, cat, loader=store.get).excerpts["m:active"]
    assert "原式：cell(nodes.fetch, 0, 'active_users')" in metric
    [where] = [line for line in metric.splitlines() if "active_users" in line and "Q1" in line and "SQL" not in line]
    assert "r0" in where, where
    [sql] = [line for line in metric.splitlines() if line.startswith("查询 Q1 的 SQL")]
    assert "COUNT(DISTINCT member_no)" in sql
    assert any("vars.target" in line and "未追溯到查询" in line for line in metric.splitlines())


def test_metric_input_sql_is_capped_and_needs_the_sealed_loader():
    cat = metric_catalog()
    doc = compose_doc("活跃用户数 [[m:active]]，按会员号去重计数。[[see:m:active]]", cat)
    long = {**snaps(**{Q1_ART: {"columns": ["active_users"], "rows": [[321]], "sql": LONG_SQL, "source": SOURCE}}),
            CARD_ART: CARD}
    metric = prepare(doc, cat, loader=long.get).excerpts["m:active"]
    [sql] = [line for line in metric.splitlines() if line.startswith("查询 Q1 的 SQL")]
    assert LONG_SQL[:SQL_CHARS] in sql and LONG_SQL[:SQL_CHARS + 10] not in sql and sql.endswith("…")

    sealed_only = {CARD_ART: CARD}                           # 查询快照不在封存范围里
    metric = prepare(doc, cat, loader=sealed_only.get).excerpts["m:active"]
    assert "SELECT" not in metric and "Q1" in metric, "位置照给，SQL 不给"


def test_a_masked_input_column_is_not_named():
    cat = metric_catalog()
    doc = compose_doc("活跃用户数 [[m:active]]，按会员号去重计数。[[see:m:active]]", cat)
    store = {**snaps(), CARD_ART: CARD}
    metric = prepare(doc, cat, loader=store.get, masked={SOURCE: ["active_users"]}).excerpts["m:active"]
    [where] = [line for line in metric.splitlines() if line.startswith("输入 cell(")]
    assert "r0" in where and "active_users" not in where.split("：", 1)[1]


# --------------------------------------------------------------------------
# 遮罩：表、字段的数据源也要查
# --------------------------------------------------------------------------


async def test_source_masks_cover_the_sources_of_tables():
    name = "jr_mask_src"
    cat = build_catalog(nodes={}, ledger=[{**schema_entry(schema_snapshot()), "source": name}])
    assert cat and all(e["kind"] in ("table", "column") for e in cat.values())
    async with SessionLocal() as session:
        session.add(DataSource(name=name, kind="sqlite", database="unused.db", options={"mask_columns": "phone"}))
        await session.commit()
    try:
        assert await source_masks(cat) == {name: ["phone"]}
    finally:
        async with SessionLocal() as session:
            await session.execute(delete(DataSource).where(DataSource.name == name))
            await session.commit()


# --------------------------------------------------------------------------
# 断点里的旧判定：规则换了就不复用
# --------------------------------------------------------------------------


def test_the_request_key_carries_the_rules_version(monkeypatch):
    """报告节点按 request_key 认断点里缓存的判定：规则、取值换了（旧的 unsupported），旧判定不能原样复用。"""
    cat = entity_catalog()
    doc = compose_doc(RAW, cat)
    request = prepare(doc, cat, loader=snaps().get)
    before = judge_mod.request_key(request, SPEC, UNLIMITED)
    assert before == judge_mod.request_key(request, SPEC, UNLIMITED), "同一份输入同一个指纹"
    monkeypatch.setattr(judge_mod, "RULES_VERSION", judge_mod.RULES_VERSION + 1)
    assert judge_mod.request_key(request, SPEC, UNLIMITED) != before


async def test_a_new_verdict_carries_the_rules_version(monkeypatch):
    """新判的判定带上规则版本（和 request_key 里的同一个常量）：按需裁判据此认出早于当前规则的旧判定。"""
    cat = entity_catalog()
    doc = compose_doc("各区销售额按 [[c:orders.region]] 汇总。[[see:Q2]]", cat)
    Judge(monkeypatch, lambda units: [{"unit": u, "verdict": "supported", "rationale": "有依据", "used": []}
                                      for u in units])
    outcome = await judge_doc(doc, cat, spec=SPEC, budget=UNLIMITED, loader=snaps().get)
    [v] = outcome["verdicts"].values()
    assert v["rule_version"] == judge_mod.RULES_VERSION


def test_which_verdicts_are_older_than_the_current_rules():
    current = judge_mod.RULES_VERSION
    assert judge_mod.outdated({"status": "supported"}), "没有规则版本：改造前判的"
    assert judge_mod.outdated({"status": "unsupported", "rule_version": current}), "旧取值一律算旧"
    assert judge_mod.outdated({"status": "partial", "rule_version": current - 1})
    assert not judge_mod.outdated({"status": "contradicted", "rule_version": current})
    assert not judge_mod.outdated({"status": "unjudged", "reason": "error"}), "没判成的谈不上新旧"
