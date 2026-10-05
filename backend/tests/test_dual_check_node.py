"""双盲复算节点（engine/nodes/dual_check.py）：真跑一遍图。虚构的 SQLite 订单库埋了退款行、下单日和支付日两个陷阱
（tests/fixtures/dual_check_shop.py），盲算模型是 MockChatModel 剧本。

- 一致：不挂起，产出 agreed，日志 dual_check_agreed；盲算的查询发 tool.end（role: verify），台账只有一条 dual_check，
  用量计入运行；盲算 agent 的任务里没有原 SQL，也没有原流程结果的列名
- 一个都没核成是 unchecked、只核成一部分是 partial，日志 dual_check_unchecked 说清哪几个、为什么
- 不一致：挂起，审批 payload 和对照工件按约定，payload 另带实际生效的 on_reject、摘要里每个指标带选 a、选 b 各会往
  数据目录里写什么的预告；a / b / neither 三种选择的产出和缺口，没选的照实记，驳回、on_reject 为 fail 时失败
- 差异说明只比这一格所在的那一列（一条查询算好几列、分组后取一行的，收窄了再比，HAVING 一并去掉）；分组的列设了
  遮罩时（改了名再分组的也算）不收窄，遮罩列的取值不进审批卡、对照工件、产出和事件
- 只差一个方面时写数据目录（业务日期、有效记录条件），差了不止一处不写，人工确认过的不同说法不覆盖，指标自己的
  筛选范围（取值在运行输入里、用到分组的列）不写，两条 SQL 规范化后一致的不写；人工驳回过的条件不会借着和别的
  指标并成一项写回去，真写的和审批卡上的预告一致
- 全局关闭直通、节点覆盖；设置按发起时的快照；重放时盲算不再执行、事件不重复；报告的写作目录不含盲算的查询
- 触顶、分组结果的维度（只给取值、遮罩列不给）、带着取舍的列名不交给盲算、两个指标用到同一格只盲算一次、遮罩列上的
  盲算结果不采用
"""
from __future__ import annotations

import asyncio
import json
import sqlite3

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select

from app.core import artifact_store
from app.data import catalog
from app.db.base import SessionLocal
from app.db.models import Approval, Run, RunEvent, Setting
from app.engine import spend
from app.engine.launch import launch_run
from app.engine.runner import run_manager
from app.main import app
from tests.fixtures.dual_check_shop import (
    BOTH_WRONG,
    BUYERS,
    BY_ORDER_DATE,
    BY_REGION,
    DEFINITION,
    RIGHT,
    WEEK,
    WITH_REFUNDS,
    Blind,
    add_source,
    build,
)
from tests.fixtures.sources import drop_source

#: 和 RIGHT 等价、写法不同（别名、列名都换了），值也是 330.0：报告的写作目录里若混进了盲算的查询，认得出来
BLIND_TOTAL = f"SELECT SUM(o.amount) AS blind_total FROM orders o WHERE o.status <> 'refunded' AND o.paid_date {WEEK}"
GMV = {"id": "gmv", "name": "销售额", "unit": "元", "decimals": 1, "format": "thousands",
       "expression": "cell(nodes.fetch, 0, 'gmv')"}


@pytest.fixture(autouse=True)
async def engine_up():
    await run_manager.setup()
    yield
    await run_manager.shutdown()


@pytest.fixture(autouse=True)
async def clean_settings():
    async def clear() -> None:
        async with SessionLocal() as session:
            await session.execute(delete(Setting).where(Setting.key.in_(["verify", spend.SPEND_KEY])))
            await session.commit()

    await clear()
    yield
    await clear()


@pytest.fixture
async def shop(tmp_path):
    name, source_id = await add_source(build(tmp_path / "shop.db"))
    yield name, source_id
    await drop_source(source_id)


@pytest.fixture
async def masked_shop(tmp_path):
    """遮了 region、secret_gmv 两列的同一个库。"""
    name, source_id = await add_source(build(tmp_path / "masked.db"), options={"mask_columns": "region, secret_gmv"})
    yield name, source_id
    await drop_source(source_id)


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


def node(nid, ntype, label=None, **config):
    return {"id": nid, "type": ntype, "position": {"x": 0, "y": 0}, "data": {"label": label or nid, "config": config}}


def graph(source: str, sql: str, *, dual: dict | None = None, metrics: list | None = None, report: bool = False):
    """取数 → 口径卡 → 双盲复算 →（报告）→ 成果。成果的叙述里没有数字：不加双盲复算时是完整出具。"""
    nodes = [
        node("start", "input"),
        node("fetch", "tool", tool=f"db_query__{source}", args={"sql": sql}),
        node("card", "metrics", caliber="销售口径", caliber_version="v1", metrics=metrics or [GMV]),
        node("dual", "dual_check", "复核销售额", **{"definitions": {"gmv": DEFINITION}, **(dual or {})}),
    ]
    if report:
        nodes.append(node("write", "report", instructions="写一句周报", on_violation="flag"))
    nodes.append(node("out", "output", fields=[{"name": "结论", "value": "本周销售额见口径卡"}],
                      contract={"metrics_from": "card", "narrative": "本周销售额见口径卡"}))
    return {"nodes": nodes, "edges": [{"source": a["id"], "target": b["id"]} for a, b in zip(nodes, nodes[1:])]}


async def wait(run_id: str, statuses=("succeeded", "failed", "interrupted")) -> Run:
    for _ in range(600):
        await asyncio.sleep(0.05)
        async with SessionLocal() as session:
            row = await session.get(Run, run_id)
        if row.status in statuses and (row.status == "interrupted" or row.manifest_seq is not None):
            await run_manager.wait_idle(run_id)
            return row
    raise AssertionError(f"运行 {run_id} 停在 {row.status}：{row.error}")


async def start(spec: dict, inputs: dict | None = None, **kwargs) -> Run:
    run = await run_manager.start(graph=spec, input_payload=inputs or {}, **kwargs)
    return await wait(run.id)


async def events(run_id: str) -> list[RunEvent]:
    async with SessionLocal() as session:
        return list((await session.execute(
            select(RunEvent).where(RunEvent.run_id == run_id).order_by(RunEvent.seq))).scalars())


def logs(evs: list[RunEvent], code: str) -> list[dict]:
    return [e.data for e in evs if e.type == "log" and e.data.get("code") == code]


async def pending(run_id: str) -> Approval:
    async with SessionLocal() as session:
        return (await session.execute(select(Approval).where(
            Approval.run_id == run_id, Approval.status == "pending"))).scalar_one()


async def decide(client, run_id: str, choices: dict, note: str = "") -> Run:
    approval = await pending(run_id)
    res = await client.post(f"/api/approvals/{approval.id}/decide",
                            json={"approved": True, "value": {"choices": choices, "note": note}})
    assert res.status_code == 200, res.text
    return await wait(run_id, ("succeeded", "failed"))


def dual_output(run: Run, evs: list[RunEvent]) -> dict:
    finished = [e for e in evs if e.type == "node.finished" and e.node_id == "dual"]
    assert finished, "双盲复算节点没有跑完"
    return artifact_store.load(finished[-1].data["artifact"])


async def notes_of(source_id: str, table: str = "orders"):
    async with SessionLocal() as session:
        return await catalog.read_entry(session, source_id, table)


# --------------------------------------------------------------------------
# 一致：不挂起
# --------------------------------------------------------------------------


async def test_agreeing_check_passes_without_asking(monkeypatch, shop):
    name, _ = shop
    blind = Blind(monkeypatch, name, {"销售额": (BLIND_TOTAL, "blind_total")})
    run = await start(graph(name, RIGHT))
    assert run.status == "succeeded", run.error
    evs = await events(run.id)
    out = dual_output(run, evs)
    assert out["status"] == "agreed" and "issuance_gaps" not in out
    assert out["counts"] == {"agreed": 1, "disagreed": 0, "unchecked": 0}
    [metric] = out["metrics"]
    assert metric["metric_id"] == "gmv" and metric["status"] == "agreed" and metric["delta_pct"] == 0.0
    assert metric["original_rendered"] == metric["blind_rendered"]
    assert [d["metrics"] for d in logs(evs, "dual_check_agreed")] == [1]
    assert not logs(evs, "dual_check_disagreed") and not [e for e in evs if e.type == "human.requested"]
    # 盲算的查询发 tool.end（role: verify），带查询快照，随运行封存
    ends = [e.data for e in evs if e.type == "tool.end" and e.node_id == "dual"]
    assert ends and all(d.get("role") == "verify" for d in ends)
    assert any(d.get("query_artifact") for d in ends)
    # 台账只记一条 dual_check，绝不写 query：报告的写作目录按台账里的 query 编号
    [finished] = [e for e in evs if e.type == "node.finished" and e.node_id == "dual"]
    kinds = [entry["kind"] for entry in finished.data.get("evidence") or []]
    assert kinds == ["dual_check"]
    entry = finished.data["evidence"][0]
    assert entry["card"] == "card" and entry["status"] == "agreed"
    assert entry["metrics"] == [{"id": "gmv", "status": "agreed"}] and entry["counts"] == out["counts"]
    assert not logs(evs, "dual_check_unchecked")
    # 用量计入运行：图里只有盲算调用了模型
    assert blind.calls and run.usage["calls"] == len(blind.calls)
    assert run.output["_issuance"]["tier"] == "formal" and run.output["_issuance"]["gaps"] == []
    # 盲算 agent 只看得到指标名、口径说明、计算式里是哪一项：看不到原 SQL、取数节点、原流程结果的列名
    [task] = blind.tasks
    assert "指标：销售额" in task and DEFINITION in task and "输入1" in task
    assert "status <> 'refunded'" not in task and "nodes.fetch" not in task and "SUM(" not in task
    assert "gmv" not in task


async def test_grouped_rows_tell_the_blind_agent_which_group(monkeypatch, shop):
    """结果分了组：盲算 agent 要知道算的是哪一组（同一行维度列的取值），但看不到同一行别的数、原流程是按哪一列分的组；
    统计期这类参数在运行输入里。"""
    name, _ = shop
    metric = {**GMV, "name": "华东销售额", "expression": "cell(nodes.fetch, 0, 'gmv')"}
    blind = Blind(monkeypatch, name, {"华东销售额": (BY_REGION, "gmv")})
    spec = graph(name, BY_REGION, metrics=[metric], dual={"definitions": {}})
    spec["nodes"][0]["data"]["config"]["fields"] = [{"name": "week_start"}, {"name": "week_end"}]
    run = await start(spec, {"week_start": "2026-09-01", "week_end": "2026-09-07"})
    assert run.status == "succeeded", run.error
    [task] = blind.tasks
    assert "「华东」" in task and "（未提供" in task and "300" not in task
    assert "region" not in task and "gmv" not in task
    assert "week_start = 2026-09-01" in task and "week_end = 2026-09-07" in task
    out = dual_output(run, await events(run.id))
    assert out["status"] == "agreed" and out["metrics"][0]["status"] == "agreed"


async def test_the_blind_agent_is_not_told_which_date_column_the_original_grouped_by(monkeypatch, shop):
    """原流程按下单日分组、口径卡取其中一天：盲算只知道是哪一天，按哪一列归属要它自己按口径说明判断。告诉它
    「order_date 为 2026-09-02」，它照着算出同一个数，下单日和支付日的陷阱就漏过去了。"""
    name, _ = shop
    by_day = (f"SELECT order_date, SUM(amount) AS gmv FROM orders WHERE status <> 'refunded' AND order_date {WEEK} "
              "GROUP BY order_date ORDER BY order_date")
    blind = Blind(monkeypatch, name, {"销售额": (RIGHT, "gmv")})
    run = await start(graph(name, by_day))
    assert run.status in ("succeeded", "interrupted"), run.error
    [task] = blind.tasks
    assert "2026-09-02 这一天" in task
    assert "order_date" not in task and "gmv" not in task


async def test_column_names_carrying_a_choice_are_not_given_as_hints(monkeypatch, shop):
    """几项输入都来自查询、又没写口径说明时，列名才交给盲算区分计算式里的几项；列名里带着原流程的取舍的（按下单日、
    已支付）不给：「gmv_by_order_date」等于告诉它按下单日归属。看不出取舍的（total）照给。"""
    name, _ = shop
    wide = (f"SELECT SUM(amount) AS gmv_by_order_date, COUNT(*) AS paid_orders, SUM(amount) AS total FROM orders "
            f"WHERE status <> 'refunded' AND order_date {WEEK}")
    aov = {"id": "aov", "name": "客单价", "unit": "元", "decimals": 2, "format": "plain",
           "expression": "(cell(nodes.fetch, 0, 'gmv_by_order_date') + cell(nodes.fetch, 0, 'total')) "
                         "/ cell(nodes.fetch, 0, 'paid_orders')"}
    blind = Blind(monkeypatch, name, {})    # 盲算都交不出数：这里只看任务原文
    await start(graph(name, wide, metrics=[aov], dual={"definitions": {}}))
    assert len(blind.tasks) == 3
    said = "\n".join(blind.tasks)
    assert "gmv_by_order_date" not in said and "paid_orders" not in said
    assert "「total」" in said


async def test_two_metrics_using_one_cell_are_checked_once(monkeypatch, shop):
    name, _ = shop
    share = {"id": "gmv_k", "name": "销售额（千元）", "unit": "千元", "decimals": 2, "format": "thousands",
             "expression": "round(cell(nodes.fetch, 0, 'gmv') / 1000, 2)"}
    blind = Blind(monkeypatch, name, {"销售额": (RIGHT, "gmv")})
    run = await start(graph(name, RIGHT, metrics=[GMV, share]))
    assert run.status == "succeeded", run.error
    assert blind.runs == 1, "同一格盲算了不止一次"
    out = dual_output(run, await events(run.id))
    assert [m["status"] for m in out["metrics"]] == ["agreed", "agreed"]


async def test_hitting_a_limit_counts_as_unchecked(monkeypatch, shop):
    """查询次数用完了还没查到要交的那一格：按无法复算计入缺口；一个指标都没核成，产出是 unchecked，不是 agreed。"""
    name, _ = shop
    Blind(monkeypatch, name, {"销售额": ([BLIND_TOTAL, RIGHT], "gmv")})
    run = await start(graph(name, RIGHT), options={"verify": {"dual_check": {"max_queries": 1}}})
    assert run.status == "succeeded", run.error
    evs = await events(run.id)
    assert [(d["limit"], d["metric"]) for d in logs(evs, "dual_check_limit")] == [("max_queries", "gmv")]
    out = dual_output(run, evs)
    assert out["status"] == "unchecked" and out["metrics"][0]["status"] == "unchecked"
    assert out["counts"] == {"agreed": 0, "disagreed": 0, "unchecked": 1}
    assert out["issuance_gaps"] == ["指标「销售额」无法复算：盲算已达每项最多查询次数（1 次）"]
    [said] = logs(evs, "dual_check_unchecked")
    assert said["metrics"] == [{"id": "gmv", "name": "销售额", "reason": "盲算已达每项最多查询次数（1 次）"}]
    assert not logs(evs, "dual_check_agreed")
    assert not [e for e in evs if e.type == "human.requested"]
    [finished] = [e for e in evs if e.type == "node.finished" and e.node_id == "dual"]
    assert finished.data["evidence"][0]["status"] == "unchecked"
    assert run.output["_issuance"]["tier"] == "degraded"
    assert run.output["_issuance"]["gaps"] == ["双盲复算「复核销售额」：指标「销售额」无法复算：盲算已达每项最多查询次数（1 次）"]


async def test_an_answer_on_the_last_allowed_query_still_counts(monkeypatch, shop):
    """查询次数刚好用完，交回的那一格是真查出来的：照常比对，不按触顶算无法复算。"""
    name, _ = shop
    Blind(monkeypatch, name, {"销售额": (RIGHT, "gmv")})
    run = await start(graph(name, RIGHT), options={"verify": {"dual_check": {"max_queries": 1}}})
    assert run.status == "succeeded", run.error
    evs = await events(run.id)
    out = dual_output(run, evs)
    assert out["status"] == "agreed" and out["metrics"][0]["status"] == "agreed" and "issuance_gaps" not in out
    assert not logs(evs, "dual_check_limit") and not logs(evs, "dual_check_unchecked")
    assert run.output["_issuance"]["tier"] == "formal"


async def test_every_metric_sharing_a_cell_reports_the_limit(monkeypatch, shop):
    name, _ = shop
    share = {"id": "gmv_k", "name": "销售额（千元）", "unit": "千元", "decimals": 2, "format": "thousands",
             "expression": "round(cell(nodes.fetch, 0, 'gmv') / 1000, 2)"}
    blind = Blind(monkeypatch, name, {"销售额": ([BLIND_TOTAL, RIGHT], "gmv")})
    run = await start(graph(name, RIGHT, metrics=[GMV, share]), options={"verify": {"dual_check": {"max_queries": 1}}})
    assert run.status == "succeeded", run.error
    assert blind.runs == 1
    evs = await events(run.id)
    assert [(d["limit"], d["metric"]) for d in logs(evs, "dual_check_limit")] == [
        ("max_queries", "gmv"), ("max_queries", "gmv_k")]
    [said] = logs(evs, "dual_check_unchecked")
    assert [m["id"] for m in said["metrics"]] == ["gmv", "gmv_k"]


async def test_some_unchecked_metrics_make_a_partial_result(monkeypatch, shop):
    name, _ = shop
    east = {**GMV, "id": "east", "name": "华东销售额", "expression": "cell(nodes.fetch, 0, 'gmv')"}
    north = {**GMV, "id": "north", "name": "华北销售额", "expression": "cell(nodes.fetch, 1, 'gmv')"}
    Blind(monkeypatch, name, {"华东销售额": (BY_REGION, "gmv")})
    run = await start(graph(name, BY_REGION, metrics=[east, north], dual={"definitions": {}}))
    assert run.status == "succeeded", run.error
    evs = await events(run.id)
    out = dual_output(run, evs)
    assert out["status"] == "partial" and [m["status"] for m in out["metrics"]] == ["agreed", "unchecked"]
    assert out["counts"] == {"agreed": 1, "disagreed": 0, "unchecked": 1}
    assert [d["metrics"] for d in logs(evs, "dual_check_agreed")] == [1]
    [said] = logs(evs, "dual_check_unchecked")
    assert [(m["id"], m["name"]) for m in said["metrics"]] == [("north", "华北销售额")] and said["metrics"][0]["reason"]
    assert out["issuance_gaps"] == [f"指标「华北销售额」无法复算：{said['metrics'][0]['reason']}"]


async def test_an_unusable_verify_model_is_tried_once(monkeypatch, shop):
    """核查模型没配好：每个指标都记无法复算、原因照实，后面的盲算不再一个个去试。"""
    from app.engine import verify_agent
    from app.providers.factory import ProviderNotConfigured

    name, _ = shop
    tried: list[str] = []

    async def broken(session, spec):
        tried.append(spec.model or "")
        raise ProviderNotConfigured("核查用的模型接入已停用", "请到「设置 → 模型接入」中启用")

    monkeypatch.setattr(verify_agent, "get_chat_model", broken)
    east = {**GMV, "id": "east", "name": "华东销售额", "expression": "cell(nodes.fetch, 0, 'gmv')"}
    north = {**GMV, "id": "north", "name": "华北销售额", "expression": "cell(nodes.fetch, 1, 'gmv')"}
    run = await start(graph(name, BY_REGION, metrics=[east, north], dual={"definitions": {}}))
    assert run.status == "succeeded", run.error
    evs = await events(run.id)
    out = dual_output(run, evs)
    assert out["status"] == "unchecked" and [m["status"] for m in out["metrics"]] == ["unchecked", "unchecked"]
    assert all("核查模型不可用" in m["reason"] and "已停用" in m["reason"] for m in out["metrics"])
    assert len(out["issuance_gaps"]) == 2 and len(tried) == 1
    [said] = logs(evs, "dual_check_unchecked")
    assert [m["id"] for m in said["metrics"]] == ["east", "north"] and "核查模型不可用" in said["metrics"][0]["reason"]


# --------------------------------------------------------------------------
# 不一致：挂起，按约定的 payload 和工件
# --------------------------------------------------------------------------


async def test_disagreement_pauses_with_the_agreed_payload_and_card(monkeypatch, shop):
    name, _ = shop
    Blind(monkeypatch, name, {"销售额": (RIGHT, "gmv")})
    run = await start(graph(name, BY_ORDER_DATE))
    assert run.status == "interrupted", run.error
    approval = await pending(run.id)
    assert approval.mode == "input" and approval.node_id == "dual"
    payload = approval.payload
    # 约定的六个键，另加 on_reject（实际生效的处置）：审核人在卡上要看得出没采用原流程会不会让运行失败
    assert set(payload) == {"kind", "node_id", "mode", "title", "card_artifact", "summary", "on_reject"}
    assert payload["kind"] == "dual_check" and payload["node_id"] == "dual" and payload["mode"] == "input"
    assert payload["on_reject"] == "degrade"
    [item] = payload["summary"]
    assert set(item) == {"metric_id", "name", "unit", "original_rendered", "blind_rendered", "delta_pct", "diff",
                         "fallback", "catalog"}
    assert (item["metric_id"], item["name"], item["unit"]) == ("gmv", "销售额", "元")
    assert item["original_rendered"].startswith("310") and item["blind_rendered"].startswith("330")
    assert item["delta_pct"] == round((330 - 310) / 310 * 100, 2)
    assert item["fallback"] is False and len(item["diff"]) == 1
    assert "order_date" in item["diff"][0] and "paid_date" in item["diff"][0] and "原流程" in item["diff"][0]
    # 选哪一边会往数据目录里写什么，按发起审批时的目录预告：只差时间列一处，选哪边就记哪一列
    assert item["catalog"] == {
        "a": {"write": True, "source": name, "table": "orders", "item": "business_date",
              "value": {"column": "order_date"}},
        "b": {"write": True, "source": name, "table": "orders", "item": "business_date",
              "value": {"column": "paid_date"}},
    }

    card = artifact_store.load(payload["card_artifact"])
    assert set(card) == {"metrics", "model", "same_model"} and card["same_model"] is False
    [metric] = card["metrics"]
    assert set(metric) == {"metric_id", "name", "unit", "decimals", "original", "blind", "diff"}
    assert metric["decimals"] == 1
    [orig] = metric["original"]["inputs"]
    assert set(orig) == {"path", "value", "sql", "source", "alias"}
    assert (orig["sql"], orig["value"], orig["source"], orig["alias"]) == (BY_ORDER_DATE, 310.0, name, "Q1")
    [mine] = metric["blind"]["inputs"]
    assert set(mine) == {"path", "value", "sql", "source", "alias", "rationale"}
    assert (mine["sql"], mine["value"], mine["source"], mine["alias"]) == (RIGHT, 330.0, name, "Q1")
    assert mine["path"] == orig["path"] == "cell(nodes.fetch, 0, 'gmv')" and "330" in mine["rationale"]
    assert metric["original"]["value"] == 310.0 and metric["blind"]["value"] == 330.0
    assert set(metric["diff"]) == {"equivalent", "items", "fallback", "text_diff"}
    assert metric["diff"]["equivalent"] is False and metric["diff"]["fallback"] is False
    assert [set(i) for i in metric["diff"]["items"]] == [{"aspect", "text"}]
    assert metric["diff"]["items"][0]["aspect"] == "time" and metric["diff"]["text_diff"]

    evs = await events(run.id)
    [said] = logs(evs, "dual_check_disagreed")
    assert said["metrics"] == [{"id": "gmv", "name": "销售额"}] and said["card_artifact"] == payload["card_artifact"]
    assert len([e for e in evs if e.type == "human.requested"]) == 1


async def test_choosing_the_original_leaves_no_gap(monkeypatch, shop, client):
    name, source_id = shop
    Blind(monkeypatch, name, {"销售额": (RIGHT, "gmv")})
    run = await start(graph(name, BY_ORDER_DATE))
    done = await decide(client, run.id, {"gmv": "a"}, note="周报一直按下单日统计")
    assert done.status == "succeeded", done.error
    evs = await events(run.id)
    out = dual_output(done, evs)
    assert out["status"] == "resolved" and out["choices"] == {"gmv": "a"} and "issuance_gaps" not in out
    assert out["note"] == "周报一直按下单日统计" and out["metrics"][0]["choice"] == "a"
    assert [d["gaps"] for d in logs(evs, "dual_check_resolved")] == [[]]
    assert logs(evs, "dual_check_resolved")[0]["choices"] == {"gmv": "a"}
    assert done.output["_issuance"]["tier"] == "formal"
    assert len([e for e in evs if e.type == "human.resolved"]) == 1
    # 只差时间列一处：采纳原流程，业务日期记原流程那一列
    entry = await notes_of(source_id)
    item = entry.notes["business_date"]
    assert item["value"] == {"column": "order_date"} and (item["source"], item["status"]) == ("human", "confirmed")
    assert run.id in item["note"] and "复核销售额" in item["note"] and "销售额" in item["note"]
    [wrote] = logs(evs, "dual_check_catalog")
    assert wrote["written"] == [{"source": name, "table": "orders", "item": "business_date"}] and wrote["skipped"] == []


async def test_choosing_the_blind_result_degrades_and_writes_the_catalog(monkeypatch, shop, client):
    name, source_id = shop
    Blind(monkeypatch, name, {"销售额": (RIGHT, "gmv")})
    run = await start(graph(name, BY_ORDER_DATE))
    done = await decide(client, run.id, {"gmv": "b"})
    assert done.status == "succeeded", done.error
    out = dual_output(done, await events(run.id))
    assert out["issuance_gaps"] == ["指标「销售额」两路结果不一致，审核人选择了盲算结果"]
    issuance = done.output["_issuance"]
    assert issuance["tier"] == "degraded"
    assert issuance["gaps"] == ["双盲复算「复核销售额」：指标「销售额」两路结果不一致，审核人选择了盲算结果"]
    assert (await notes_of(source_id)).notes["business_date"]["value"] == {"column": "paid_date"}


async def test_a_dual_check_named_after_its_type_is_named_once_in_the_gap(monkeypatch, shop, client):
    """拖进画布没改名的双盲复算：缺口读成「双盲复算：指标「销售额」两路结果不一致……」，不写两遍「双盲复算」"""
    name, _ = shop
    Blind(monkeypatch, name, {"销售额": (RIGHT, "gmv")})
    spec = graph(name, BY_ORDER_DATE)
    next(n for n in spec["nodes"] if n["id"] == "dual")["data"]["label"] = "双盲复算"
    run = await start(spec)
    done = await decide(client, run.id, {"gmv": "neither"})
    assert done.status == "succeeded", done.error
    assert done.output["_issuance"]["gaps"] == ["双盲复算：指标「销售额」两路结果不一致，审核人判断两边都不对"]


async def test_neither_degrades_and_writes_nothing(monkeypatch, shop, client):
    name, source_id = shop
    Blind(monkeypatch, name, {"销售额": (RIGHT, "gmv")})
    run = await start(graph(name, BY_ORDER_DATE))
    done = await decide(client, run.id, {"gmv": "neither"}, note="两边都没排除测试单")
    assert done.status == "succeeded", done.error
    evs = await events(run.id)
    out = dual_output(done, evs)
    assert out["issuance_gaps"] == ["指标「销售额」两路结果不一致，审核人判断两边都不对"]
    assert done.output["_issuance"]["tier"] == "degraded"
    assert not logs(evs, "dual_check_catalog") and await notes_of(source_id) is None


async def test_a_missing_choice_is_recorded_as_no_choice(monkeypatch, shop, client):
    """没选、选了认不出的：照实记「没有作出选择」，不替审核人说「两边都不对」；没采用原流程，照样降档。"""
    name, source_id = shop
    Blind(monkeypatch, name, {"销售额": (RIGHT, "gmv")})
    run = await start(graph(name, BY_ORDER_DATE))
    done = await decide(client, run.id, {"gmv": "maybe"})
    assert done.status == "succeeded", done.error
    evs = await events(run.id)
    out = dual_output(done, evs)
    assert out["choices"] == {} and "choice" not in out["metrics"][0]
    assert out["issuance_gaps"] == ["指标「销售额」两路结果不一致，审核人没有作出选择"]
    [resolved] = logs(evs, "dual_check_resolved")
    assert resolved["choices"] == {} and resolved["gaps"] == out["issuance_gaps"]
    assert "没有作出选择" in resolved["message"] and "两边都不对" not in resolved["message"]
    assert done.output["_issuance"]["tier"] == "degraded"
    assert not logs(evs, "dual_check_catalog") and await notes_of(source_id) is None


async def test_rejecting_the_approval_fails_the_node(monkeypatch, shop, client):
    name, _ = shop
    Blind(monkeypatch, name, {"销售额": (RIGHT, "gmv")})
    run = await start(graph(name, BY_ORDER_DATE))
    approval = await pending(run.id)
    res = await client.post(f"/api/approvals/{approval.id}/decide", json={"approved": False, "note": "口径要重新对"})
    assert res.status_code == 200, res.text
    done = await wait(run.id, ("succeeded", "failed"))
    assert done.status == "failed" and "人工驳回" in (done.error or "") and "口径要重新对" in done.error


async def test_on_reject_fail_stops_the_run(monkeypatch, shop, client):
    name, source_id = shop
    Blind(monkeypatch, name, {"销售额": (RIGHT, "gmv")})
    run = await start(graph(name, BY_ORDER_DATE, dual={"on_reject": "fail"}))
    # 审批卡上看得出：这一步没采用原流程，运行就会停下
    assert (await pending(run.id)).payload["on_reject"] == "fail"
    done = await decide(client, run.id, {"gmv": "b"})
    assert done.status == "failed" and "审核人没有采用原流程的结果" in (done.error or "") and "销售额" in done.error
    evs = await events(run.id)
    assert [d["gaps"] for d in logs(evs, "dual_check_resolved")] == [
        ["指标「销售额」两路结果不一致，审核人选择了盲算结果"]]
    # 审核人的选择照样写进了目录：那是对数据的判断，和运行停不停无关
    entry = await notes_of(source_id)
    assert entry.notes["business_date"]["value"] == {"column": "paid_date"} and entry.version == 1


async def test_on_reject_fail_lets_the_original_through(monkeypatch, shop, client):
    name, _ = shop
    Blind(monkeypatch, name, {"销售额": (RIGHT, "gmv")})
    run = await start(graph(name, BY_ORDER_DATE, dual={"on_reject": "fail"}))
    done = await decide(client, run.id, {"gmv": "a"})
    assert done.status == "succeeded", done.error


async def test_the_same_choice_twice_writes_the_catalog_once(monkeypatch, shop, client):
    """同样的不一致、同样的选择再来一次：目录里已经是这个说法，不再写，也不升版本。"""
    name, source_id = shop
    Blind(monkeypatch, name, {"销售额": (RIGHT, "gmv")})
    for _ in range(2):
        run = await start(graph(name, BY_ORDER_DATE))
        await decide(client, run.id, {"gmv": "b"})
    [wrote] = logs(await events(run.id), "dual_check_catalog")
    assert wrote["written"] == [] and "无需重复写入" in wrote["skipped"][0]["reason"]
    assert (await notes_of(source_id)).version == 1


# --------------------------------------------------------------------------
# 写数据目录：只差一个方面才写，确认过的不覆盖
# --------------------------------------------------------------------------


async def test_a_refund_filter_becomes_the_valid_row_filter(monkeypatch, shop, client):
    name, source_id = shop
    Blind(monkeypatch, name, {"销售额": (RIGHT, "gmv")})
    run = await start(graph(name, WITH_REFUNDS))
    payload = (await pending(run.id)).payload
    assert "status = 'refunded'" in payload["summary"][0]["diff"][0]
    await decide(client, run.id, {"gmv": "b"})
    item = (await notes_of(source_id)).notes["valid_filter"]
    assert item["value"] == "status <> 'refunded'" and item["status"] == "confirmed" and item["source"] == "human"


async def test_the_adopted_side_without_the_filter_writes_nothing(monkeypatch, shop, client):
    """多出条件的是原流程，审核人却采纳了没有这个条件的盲算：目录里没有对应的写法，照实说。"""
    name, source_id = shop
    Blind(monkeypatch, name, {"销售额": (WITH_REFUNDS, "gmv")})
    run = await start(graph(name, RIGHT))
    done = await decide(client, run.id, {"gmv": "b"})
    [wrote] = logs(await events(done.id), "dual_check_catalog")
    assert wrote["written"] == [] and "被采纳的盲算没有这个条件" in wrote["skipped"][0]["reason"]
    assert await notes_of(source_id) is None


async def test_two_differences_are_not_written(monkeypatch, shop, client):
    name, source_id = shop
    Blind(monkeypatch, name, {"销售额": (RIGHT, "gmv")})
    run = await start(graph(name, BOTH_WRONG))
    assert len((await pending(run.id)).payload["summary"][0]["diff"]) == 2
    done = await decide(client, run.id, {"gmv": "b"})
    [wrote] = logs(await events(done.id), "dual_check_catalog")
    assert wrote["written"] == [] and "不止差一个方面" in wrote["skipped"][0]["reason"]
    assert await notes_of(source_id) is None


async def test_a_confirmed_catalog_entry_is_not_overwritten(monkeypatch, shop, client):
    name, source_id = shop
    async with SessionLocal() as session:
        await catalog.write_entry(session, source_id, "orders", {
            "business_date": catalog.make_item({"column": "order_date"}, "human", "confirmed")}, if_version=0,
            actor="数据组")
    Blind(monkeypatch, name, {"销售额": (RIGHT, "gmv")})
    run = await start(graph(name, BY_ORDER_DATE))
    # 预告按目录里已有的说法算：选原流程是同一个说法、不用再写，选盲算不会覆盖已确认的
    preview = (await pending(run.id)).payload["summary"][0]["catalog"]
    assert preview["a"]["write"] is False and "无需重复写入" in preview["a"]["reason"]
    assert preview["b"]["write"] is False and "已有人工确认过的说法「order_date」" in preview["b"]["reason"]
    done = await decide(client, run.id, {"gmv": "b"})
    [wrote] = logs(await events(done.id), "dual_check_catalog")
    assert wrote["written"] == []
    assert "已有人工确认过的说法「order_date」" in wrote["skipped"][0]["reason"] and "paid_date" in wrote["skipped"][0]["reason"]
    entry = await notes_of(source_id)
    assert entry.version == 1 and entry.notes["business_date"]["value"] == {"column": "order_date"}


async def test_a_rejected_filter_is_not_written_back_along_with_another_metric(monkeypatch, shop, client, tmp_path):
    """目录里「status <> 'refunded'」被人工驳回过。同一次裁决里「销售额」采纳了排除退款的盲算、「订单金额」采纳了排除
    测试单的盲算：审批卡上预告前者不写、后者写「is_test = 0」。真写的和预告一字不差——驳回过的条件不会借着两个指标
    并成一项又写成人工确认。"""
    name, source_id = shop
    db = sqlite3.connect(tmp_path / "shop.db")
    db.execute("INSERT INTO orders VALUES (6, '华东', 105, 40.0, 'paid', '2026-09-03', '2026-09-03', 'online', 1)")
    db.commit()
    db.close()
    async with SessionLocal() as session:
        await catalog.write_entry(session, source_id, "orders", {
            "valid_filter": catalog.make_item("status <> 'refunded'", "human", "rejected")}, if_version=0, actor="数据组")
    original = f"SELECT SUM(amount) AS gmv, SUM(amount) AS total FROM orders WHERE paid_date {WEEK}"
    no_test = f"SELECT SUM(amount) AS total FROM orders WHERE is_test = 0 AND paid_date {WEEK}"
    total = {**GMV, "id": "total", "name": "订单金额", "expression": "cell(nodes.fetch, 0, 'total')"}
    Blind(monkeypatch, name, {"销售额": (RIGHT, "gmv"), "订单金额": (no_test, "total")})
    run = await start(graph(name, original, metrics=[GMV, total], dual={"definitions": {}}))
    assert run.status == "interrupted", run.error
    said = {s["metric_id"]: s["catalog"]["b"] for s in (await pending(run.id)).payload["summary"]}
    assert said["gmv"]["write"] is False and "曾被人工驳回" in said["gmv"]["reason"]
    assert said["total"]["write"] is True and said["total"]["value"] == "is_test = 0"
    done = await decide(client, run.id, {"gmv": "b", "total": "b"})
    assert done.status == "succeeded", done.error
    [wrote] = logs(await events(run.id), "dual_check_catalog")
    assert wrote["written"] == [{"source": name, "table": "orders", "item": "valid_filter"}]
    assert [s["reason"] for s in wrote["skipped"]] == [said["gmv"]["reason"]]
    item = (await notes_of(source_id)).notes["valid_filter"]
    assert item["value"] == said["total"]["value"] and (item["source"], item["status"]) == ("human", "confirmed")
    assert "「订单金额」" in item["note"] and "「销售额」" not in item["note"]


async def test_a_metric_scope_filter_is_not_written_as_a_valid_filter(monkeypatch, shop, client):
    """「华东销售额」原流程按指标名只看华东，盲算漏了地区：审核人选原流程，但 region = '华东' 是这个指标自己的筛选
    范围，写成表的有效记录条件，以后所有查订单表的工作流（包括下一次盲算）都会只看华东。"""
    name, source_id = shop
    east = {**GMV, "id": "east", "name": "华东销售额"}
    Blind(monkeypatch, name, {"华东销售额": (RIGHT, "gmv")})
    original = RIGHT.replace("WHERE ", "WHERE region = '华东' AND ")
    run = await start(graph(name, original, metrics=[east], dual={"definitions": {"east": "华东地区已支付订单的金额"}}))
    [item] = (await pending(run.id)).payload["summary"]
    assert len(item["diff"]) == 1 and "region = '华东'" in item["diff"][0]
    # 预告和真写的是同一套判断：两边选哪个都不写，原因照实
    assert item["catalog"]["a"]["write"] is False and "可能是指标自己的筛选范围" in item["catalog"]["a"]["reason"]
    assert item["catalog"]["b"]["write"] is False and "被采纳的盲算没有这个条件" in item["catalog"]["b"]["reason"]
    done = await decide(client, run.id, {"east": "a"})
    assert done.status == "succeeded", done.error
    [wrote] = logs(await events(run.id), "dual_check_catalog")
    assert wrote["written"] == [] and "可能是指标自己的筛选范围" in wrote["skipped"][0]["reason"]
    assert await notes_of(source_id) is None


async def test_equivalent_sql_with_different_values_points_at_the_data(monkeypatch, shop, client, tmp_path):
    """原流程查完之后库里多了一单：两条 SQL 规范化后一致、值却不同，差异说明写「差在数据」，不写数据目录。"""
    from app.engine.nodes.dual_check import SAME_SQL

    name, source_id = shop

    def late_order() -> None:
        db = sqlite3.connect(tmp_path / "shop.db")
        db.execute("INSERT INTO orders VALUES (6, '华东', 104, 70.0, 'paid', '2026-09-04', '2026-09-04', 'online', 0)")
        db.commit()
        db.close()

    Blind(monkeypatch, name, {"销售额": (BLIND_TOTAL, "blind_total")}, on_query=late_order)
    run = await start(graph(name, RIGHT))
    assert run.status == "interrupted", run.error
    payload = (await pending(run.id)).payload
    [item] = payload["summary"]
    assert item["diff"] == [SAME_SQL] and item["fallback"] is False
    assert item["original_rendered"].startswith("330") and item["blind_rendered"].startswith("400")
    [metric] = artifact_store.load(payload["card_artifact"])["metrics"]
    assert metric["diff"]["equivalent"] is True and metric["diff"]["items"] == [{"aspect": "data", "text": SAME_SQL}]
    done = await decide(client, run.id, {"gmv": "b"})
    [wrote] = logs(await events(done.id), "dual_check_catalog")
    assert wrote["written"] == [] and "规范化后一致" in wrote["skipped"][0]["reason"]
    assert await notes_of(source_id) is None


async def test_a_multi_column_query_is_compared_on_its_own_column(monkeypatch, shop, client):
    """原流程一条查询算了好几列，口径卡取其中一列：差异只比这一列是怎么算的，不混进「选出的列」这类差别。"""
    name, source_id = shop
    wide = f"SELECT SUM(amount) AS gmv, COUNT(*) AS n FROM orders WHERE status <> 'refunded' AND order_date {WEEK}"
    Blind(monkeypatch, name, {"销售额": (RIGHT, "gmv")})
    run = await start(graph(name, wide))
    payload = (await pending(run.id)).payload
    [item] = payload["summary"]
    assert len(item["diff"]) == 1 and "order_date" in item["diff"][0] and "paid_date" in item["diff"][0]
    [metric] = artifact_store.load(payload["card_artifact"])["metrics"]
    assert [i["aspect"] for i in metric["diff"]["items"]] == ["time"]
    # 对照里的原 SQL 还是原流程整条查询：给人看的是原文
    assert metric["original"]["inputs"][0]["sql"] == wide
    await decide(client, run.id, {"gmv": "b"})
    assert (await notes_of(source_id)).notes["business_date"]["value"] == {"column": "paid_date"}


async def test_a_grouped_query_is_compared_on_the_row_it_was_taken_from(monkeypatch, shop, client):
    """原流程按地区分组、取华东那一行：收窄成「华东这一组」再比，差异里不出现分组、地区条件这类和这一格无关的差别。"""
    name, source_id = shop
    by_region = (f"SELECT region, SUM(amount) AS gmv FROM orders WHERE status <> 'refunded' AND order_date {WEEK} "
                 "GROUP BY region ORDER BY region")
    east_paid = RIGHT.replace("WHERE ", "WHERE region = '华东' AND ")
    east = {**GMV, "id": "east", "name": "华东销售额"}
    Blind(monkeypatch, name, {"华东销售额": (east_paid, "gmv")})
    run = await start(graph(name, by_region, metrics=[east], dual={"definitions": {}}))
    [item] = (await pending(run.id)).payload["summary"]
    assert item["original_rendered"].startswith("200") and item["blind_rendered"].startswith("300")
    assert len(item["diff"]) == 1 and "order_date" in item["diff"][0] and "paid_date" in item["diff"][0]
    await decide(client, run.id, {"east": "b"})
    assert (await notes_of(source_id)).notes["business_date"]["value"] == {"column": "paid_date"}


async def test_a_having_clause_is_dropped_when_narrowing_to_one_row(monkeypatch, shop, client):
    """原流程分组后另有 HAVING：取的这一行既然在结果里，它所在的组就满足 HAVING，对这一格是噪声。收窄时连 HAVING
    一起去掉，差异只剩真正的那一处（时间列），写得进目录。"""
    name, source_id = shop
    by_region = (f"SELECT region, SUM(amount) AS gmv FROM orders WHERE status <> 'refunded' AND order_date {WEEK} "
                 "GROUP BY region HAVING SUM(amount) > 10 ORDER BY region")
    east_paid = RIGHT.replace("WHERE ", "WHERE region = '华东' AND ")
    east = {**GMV, "id": "east", "name": "华东销售额"}
    Blind(monkeypatch, name, {"华东销售额": (east_paid, "gmv")})
    run = await start(graph(name, by_region, metrics=[east], dual={"definitions": {}}))
    [item] = (await pending(run.id)).payload["summary"]
    assert item["original_rendered"].startswith("200") and item["blind_rendered"].startswith("300")
    assert len(item["diff"]) == 1 and "order_date" in item["diff"][0] and "paid_date" in item["diff"][0]
    await decide(client, run.id, {"east": "b"})
    assert (await notes_of(source_id)).notes["business_date"]["value"] == {"column": "paid_date"}


async def test_a_condition_on_a_grouped_column_is_not_written(monkeypatch, shop, client, tmp_path):
    """原流程按 is_test 分组、口径卡取 is_test = 0 那一行：收窄后原流程多出「is_test = 0」，看着像排除测试单的有效
    记录条件，可它用的是原流程分组的列——是这个指标按组取的范围，不写目录。取值 0 太短、不在任何文字里，只有分组的列
    拦得下它。"""
    name, source_id = shop
    db = sqlite3.connect(tmp_path / "shop.db")
    db.execute("INSERT INTO orders VALUES (6, '华东', 105, 40.0, 'paid', '2026-09-03', '2026-09-03', 'online', 1)")
    db.commit()
    db.close()
    by_flag = (f"SELECT is_test, SUM(amount) AS gmv FROM orders WHERE status <> 'refunded' AND paid_date {WEEK} "
               "GROUP BY is_test ORDER BY is_test")
    Blind(monkeypatch, name, {"销售额": (RIGHT, "gmv")})
    run = await start(graph(name, by_flag, dual={"definitions": {}}))
    [item] = (await pending(run.id)).payload["summary"]
    assert item["original_rendered"].startswith("330") and item["blind_rendered"].startswith("370")
    assert len(item["diff"]) == 1 and "is_test = 0" in item["diff"][0]
    done = await decide(client, run.id, {"gmv": "a"})
    [wrote] = logs(await events(done.id), "dual_check_catalog")
    assert wrote["written"] == [] and "分组的列" in wrote["skipped"][0]["reason"]
    assert await notes_of(source_id) is None


async def test_a_condition_matching_a_run_input_is_not_written(monkeypatch, shop, client):
    """排除退款的条件看着像有效记录条件，可取值正是本次运行的输入（这一期要排除的状态）：是这个指标这一期的筛选范围，
    不是整张表的事实，不写目录。没有这个输入时同样的选择会写（test_a_refund_filter_becomes_the_valid_row_filter）。"""
    name, source_id = shop
    spec = graph(name, RIGHT, dual={"definitions": {}})
    spec["nodes"][0]["data"]["config"]["fields"] = [{"name": "exclude_status"}]
    Blind(monkeypatch, name, {"销售额": (WITH_REFUNDS, "gmv")})
    run = await start(spec, {"exclude_status": "refunded"})
    [item] = (await pending(run.id)).payload["summary"]
    assert len(item["diff"]) == 1 and "refunded" in item["diff"][0]
    done = await decide(client, run.id, {"gmv": "a"})
    [wrote] = logs(await events(done.id), "dual_check_catalog")
    assert wrote["written"] == [] and "本次运行的输入" in wrote["skipped"][0]["reason"]
    assert await notes_of(source_id) is None


async def test_two_differing_inputs_do_not_write_the_catalog(monkeypatch, shop, client):
    """一个指标的两个输入都对不上：审核人选的是整个指标哪边对，拆不出是哪一处，不写目录。"""
    name, source_id = shop
    share = (f"SELECT SUM(CASE WHEN region = '华东' THEN amount ELSE 0 END) AS east, SUM(amount) AS total FROM orders "
             f"WHERE status <> 'refunded' AND order_date {WEEK}")
    east_paid = (f"SELECT SUM(CASE WHEN region = '华东' THEN amount ELSE 0 END) AS east FROM orders "
                 f"WHERE status <> 'refunded' AND paid_date {WEEK}")
    ratio = {"id": "east_share", "name": "华东占比", "unit": "", "decimals": 3, "format": "plain",
             "expression": "cell(nodes.fetch, 0, 'east') / cell(nodes.fetch, 0, 'total')"}
    blind = Blind(monkeypatch, name, {"华东占比|输入1": (east_paid, "east"), "华东占比|输入2": (RIGHT, "gmv")})
    run = await start(graph(name, share, metrics=[ratio], dual={"definitions": {}}))
    assert blind.runs == 2
    [item] = (await pending(run.id)).payload["summary"]
    assert len(item["diff"]) == 2 and all("输入「" in d for d in item["diff"])
    done = await decide(client, run.id, {"east_share": "b"})
    [wrote] = logs(await events(done.id), "dual_check_catalog")
    assert wrote["written"] == [] and "不止差一个方面" in wrote["skipped"][0]["reason"]
    assert await notes_of(source_id) is None


async def test_only_the_configured_metrics_are_checked(monkeypatch, shop):
    name, _ = shop
    wide = f"SELECT SUM(amount) AS gmv, COUNT(*) AS n FROM orders WHERE status <> 'refunded' AND paid_date {WEEK}"
    count = {"id": "orders", "name": "订单数", "unit": "单", "decimals": 0, "expression": "cell(nodes.fetch, 0, 'n')"}
    blind = Blind(monkeypatch, name, {"销售额": (RIGHT, "gmv"), "订单数": (BUYERS, "buyers")})
    run = await start(graph(name, wide, metrics=[GMV, count], dual={"metrics": ["gmv"]}))
    assert run.status == "succeeded", run.error
    out = dual_output(run, await events(run.id))
    assert [m["metric_id"] for m in out["metrics"]] == ["gmv"] and out["status"] == "agreed"
    assert blind.runs == 1 and all("指标：订单数" not in t for t in blind.tasks)


async def test_a_zero_original_compares_by_absolute_error_and_has_no_delta_pct(monkeypatch, shop):
    name, _ = shop
    voids = {"id": "voids", "name": "作废单数", "unit": "单", "decimals": 0, "expression": "cell(nodes.fetch, 0, 'n')"}
    none_void = f"SELECT COUNT(*) AS n FROM orders WHERE status = 'void' AND paid_date {WEEK}"
    refunded = f"SELECT COUNT(*) AS n FROM orders WHERE status = 'refunded' AND paid_date {WEEK}"
    Blind(monkeypatch, name, {"作废单数": (refunded, "n")})
    run = await start(graph(name, none_void, metrics=[voids], dual={"definitions": {}}))
    [item] = (await pending(run.id)).payload["summary"]
    assert item["original_rendered"].startswith("0") and item["blind_rendered"].startswith("1")
    assert item["delta_pct"] is None


def test_zero_originals_use_absolute_error():
    from app.engine.nodes.dual_check import _agree, _delta_pct

    assert _agree(0, 1e-7, 1e-6) and not _agree(0, 0.5, 1e-6)
    assert _agree(100.0, 100.00005, 1e-6) and not _agree(100.0, 100.01, 1e-6)
    assert _delta_pct(0, 5) is None and _delta_pct(310.0, 330.0) == 6.45 and _delta_pct(-10, -5) == 50.0


async def test_the_summary_lists_at_most_three_differences(monkeypatch, shop):
    name, _ = shop
    blind_sql = RIGHT + " AND region <> '华北' AND customer_id <> 999"
    Blind(monkeypatch, name, {"销售额": (blind_sql, "gmv")})
    run = await start(graph(name, BOTH_WRONG))
    payload = (await pending(run.id)).payload
    [item] = payload["summary"]
    [metric] = artifact_store.load(payload["card_artifact"])["metrics"]
    assert len(metric["diff"]["items"]) == 4 and len(item["diff"]) == 3
    assert item["diff"] == [i["text"] for i in metric["diff"]["items"][:3]]


# --------------------------------------------------------------------------
# 遮罩
# --------------------------------------------------------------------------


async def test_a_blind_cell_on_a_masked_column_is_not_used(monkeypatch, masked_shop, client):
    """盲算交的那一格在遮罩列上：值不采用（不进审批卡、对照工件、产出），这个指标按无法复算记。"""
    name, _ = masked_shop
    east = {**GMV, "id": "east", "name": "华东销售额", "expression": "cell(nodes.fetch, 0, 'gmv')"}
    north = {**GMV, "id": "north", "name": "华北销售额", "expression": "cell(nodes.fetch, 1, 'gmv')"}
    east_by_order = BY_ORDER_DATE.replace("WHERE ", "WHERE region = '华东' AND ")
    north_masked = (f"SELECT SUM(amount) + 0.25 AS secret_gmv FROM orders WHERE status <> 'refunded' "
                    f"AND paid_date {WEEK} AND region = '华北'")
    Blind(monkeypatch, name, {"华东销售额": (east_by_order, "gmv"), "华北销售额": (north_masked, "secret_gmv")})
    run = await start(graph(name, BY_REGION, metrics=[east, north], dual={"definitions": {}}))
    assert run.status == "interrupted", run.error
    payload = (await pending(run.id)).payload
    card = artifact_store.load(payload["card_artifact"])
    assert [m["metric_id"] for m in payload["summary"]] == ["east"]
    assert [m["metric_id"] for m in card["metrics"]] == ["east"]
    assert "30.25" not in json.dumps(payload, ensure_ascii=False)
    assert "30.25" not in json.dumps(card, ensure_ascii=False)
    done = await decide(client, run.id, {"east": "a"})
    out = dual_output(done, await events(run.id))
    [_, row] = out["metrics"]
    assert row["status"] == "unchecked" and "遮罩" in row["reason"] and row["blind_rendered"] is None
    assert "30.25" not in json.dumps(out, ensure_ascii=False)


async def test_masked_group_columns_are_not_shown_to_the_blind_agent(monkeypatch, masked_shop):
    name, _ = masked_shop
    first = {**GMV, "id": "first", "name": "首组销售额", "expression": "cell(nodes.fetch, 0, 'gmv')"}
    blind = Blind(monkeypatch, name, {"首组销售额": (RIGHT, "gmv")})
    await start(graph(name, BY_REGION, metrics=[first], dual={"definitions": {}}))
    [task] = blind.tasks
    assert "华东" not in task and "region" not in task


async def test_masked_group_values_stay_out_of_the_card_and_the_catalog_log(monkeypatch, masked_shop, client):
    """原流程按遮罩列（region）分组、口径卡取华东那一行：差异说明收窄到这一行时，不能拿这一行的分组值补等值条件——
    那样「region = '华东'」就进了审批卡、对照工件、事件和目录日志。遮罩的分组列上不收窄，照整条比。"""
    name, _ = masked_shop
    first = {**GMV, "id": "first", "name": "首组销售额", "expression": "cell(nodes.fetch, 0, 'gmv')"}
    Blind(monkeypatch, name, {"首组销售额": (RIGHT, "gmv")})
    run = await start(graph(name, BY_REGION, metrics=[first], dual={"definitions": {}}))
    assert run.status == "interrupted", run.error
    payload = (await pending(run.id)).payload
    card = artifact_store.load(payload["card_artifact"])
    done = await decide(client, run.id, {"first": "a"})
    assert done.status == "succeeded", done.error
    evs = await events(run.id)
    _no_masked_value(payload, card, done, evs)
    # 照整条比：原流程分组、盲算没分组都列出来，不止差一个方面，选了也不写目录
    assert len(payload["summary"][0]["diff"]) > 1
    [wrote] = logs(evs, "dual_check_catalog")
    assert wrote["written"] == [] and "不止差一个方面" in wrote["skipped"][0]["reason"]


async def test_a_renamed_masked_group_column_is_not_narrowed_either(monkeypatch, masked_shop, client):
    """原流程把遮罩列改了名再分组（region AS area）：结果列名 area 不在遮罩里，分组值却还是 region 的取值。收窄时看的
    是分组键用到的列，一样不拿这一行的分组值补等值条件，「华东」不进审批卡、对照工件、产出和事件。"""
    name, _ = masked_shop
    renamed = (f"SELECT region AS area, SUM(amount) AS gmv FROM orders WHERE status <> 'refunded' "
               f"AND paid_date {WEEK} GROUP BY region ORDER BY region")
    first = {**GMV, "id": "first", "name": "首组销售额", "expression": "cell(nodes.fetch, 0, 'gmv')"}
    Blind(monkeypatch, name, {"首组销售额": (RIGHT, "gmv")})
    run = await start(graph(name, renamed, metrics=[first], dual={"definitions": {}}))
    assert run.status == "interrupted", run.error
    payload = (await pending(run.id)).payload
    assert payload["summary"][0]["original_rendered"].startswith("300")
    card = artifact_store.load(payload["card_artifact"])
    done = await decide(client, run.id, {"first": "a"})
    assert done.status == "succeeded", done.error
    _no_masked_value(payload, card, done, await events(run.id))
    # 照整条比：原流程分组、盲算没分组都列出来
    assert len(payload["summary"][0]["diff"]) > 1


async def test_a_masked_group_column_of_the_blind_query_is_not_narrowed_either(monkeypatch, masked_shop, client):
    """盲算自己按遮罩列分组、交了华东那一行：同样不拿这一行的分组值改写盲算的 SQL。"""
    name, _ = masked_shop
    Blind(monkeypatch, name, {"销售额": (BY_REGION, "gmv")})
    run = await start(graph(name, BY_ORDER_DATE))
    assert run.status == "interrupted", run.error
    payload = (await pending(run.id)).payload
    assert payload["summary"][0]["blind_rendered"].startswith("300")
    card = artifact_store.load(payload["card_artifact"])
    done = await decide(client, run.id, {"gmv": "b"})
    assert done.status == "succeeded", done.error
    _no_masked_value(payload, card, done, await events(run.id))


def _no_masked_value(payload: dict, card: dict, done: Run, evs: list[RunEvent], value: str = "华东") -> None:
    """遮罩列上的取值不出现在审批 payload、对照工件、节点产出和节点发的事件里。查询本身的 tool.end、模型的往来按
    设计带着原始结果（证据面板显示时再遮），不在此列。"""
    seen = {"payload": payload, "card": card, "output": dual_output(done, evs),
            "events": [e.data for e in evs if (e.node_id == "dual" or e.type.startswith("human.") or e.type == "log")
                       and not e.type.startswith(("tool.", "llm."))]}
    for where, content in seen.items():
        assert value not in json.dumps(content, ensure_ascii=False), where


# --------------------------------------------------------------------------
# 设置：全局开关、节点覆盖、发起时的快照
# --------------------------------------------------------------------------


async def save_verify(values: dict) -> None:
    async with SessionLocal() as session:
        row = await session.get(Setting, "verify")
        if row is None:
            session.add(Setting(key="verify", value=values))
        else:
            row.value = values
        await session.commit()


async def launch(spec: dict) -> Run:
    async with SessionLocal() as session:
        run = await launch_run(session, graph=spec, actor="测试")
    return await wait(run.id)


async def test_globally_disabled_passes_through(monkeypatch, shop):
    name, _ = shop
    blind = Blind(monkeypatch, name, {"销售额": (RIGHT, "gmv")})
    await save_verify({"dual_check": {"enabled": False}})
    run = await launch(graph(name, BY_ORDER_DATE))
    assert run.status == "succeeded", run.error
    assert run.options["verify"]["dual_check"]["enabled"] is False
    evs = await events(run.id)
    assert dual_output(run, evs) == {"status": "skipped", "reason": "disabled"}
    assert [d["reason"] for d in logs(evs, "dual_check_skipped")] == ["disabled"]
    assert blind.calls == [] and run.output["_issuance"]["tier"] == "formal"


async def test_the_node_can_override_the_global_switch(monkeypatch, shop):
    name, _ = shop
    blind = Blind(monkeypatch, name, {"销售额": (RIGHT, "gmv")})
    await save_verify({"dual_check": {"enabled": False}})
    run = await launch(graph(name, RIGHT, dual={"enabled": True}))
    assert run.status == "succeeded" and dual_output(run, await events(run.id))["status"] == "agreed"
    assert blind.runs == 1

    await save_verify({"dual_check": {"enabled": True}})
    off = await launch(graph(name, RIGHT, dual={"enabled": False}))
    assert dual_output(off, await events(off.id)) == {"status": "skipped", "reason": "disabled"}
    assert blind.runs == 1


async def test_no_query_inputs_pass_through(monkeypatch, shop):
    name, _ = shop
    blind = Blind(monkeypatch, name, {})
    constant = {"id": "target", "name": "目标", "unit": "元", "expression": "300"}
    run = await start(graph(name, RIGHT, metrics=[constant], dual={"definitions": {}}))
    evs = await events(run.id)
    assert dual_output(run, evs) == {"status": "skipped", "reason": "no_inputs"}
    assert [d["reason"] for d in logs(evs, "dual_check_skipped")] == ["no_inputs"] and blind.calls == []
    assert run.output["_issuance"]["tier"] == "formal" and run.output["_issuance"]["gaps"] == []


async def test_settings_changed_while_waiting_do_not_apply(monkeypatch, shop, client):
    """发起时是开着的：停在审批上的时候把双盲复算关了，恢复后照旧按发起时的设置裁决，不会走成直接放行。"""
    name, _ = shop
    Blind(monkeypatch, name, {"销售额": (RIGHT, "gmv")})
    run = await launch(graph(name, BY_ORDER_DATE))
    assert run.status == "interrupted" and run.options["verify"]["dual_check"]["enabled"] is True
    await save_verify({"dual_check": {"enabled": False}})
    done = await decide(client, run.id, {"gmv": "b"})
    assert done.status == "succeeded", done.error
    evs = await events(run.id)
    out = dual_output(done, evs)
    assert out["status"] == "resolved" and out["issuance_gaps"]
    assert not logs(evs, "dual_check_skipped")
    started = [e.data for e in evs if e.type == "run.started"]
    assert len(started) == 2 and all(s["options"]["verify"]["dual_check"]["enabled"] is True for s in started)


async def test_the_node_reads_the_snapshot_not_the_current_settings(monkeypatch, shop):
    name, _ = shop
    blind = Blind(monkeypatch, name, {"销售额": (RIGHT, "gmv")})
    run = await start(graph(name, BY_ORDER_DATE), options={"verify": {"dual_check": {"enabled": False}}})
    assert dual_output(run, await events(run.id)) == {"status": "skipped", "reason": "disabled"}
    assert blind.calls == []


# --------------------------------------------------------------------------
# 重放、写作目录
# --------------------------------------------------------------------------


async def test_resuming_does_not_run_the_blind_agent_again(monkeypatch, shop, client):
    """照 test_replay_side_effects：恢复后节点从头重放，盲算 agent 不再执行，事件不重复。"""
    name, _ = shop
    blind = Blind(monkeypatch, name, {"销售额": (RIGHT, "gmv")})
    run = await start(graph(name, BY_ORDER_DATE))
    calls_before = list(blind.calls)
    before = await events(run.id)
    verify_starts = [e for e in before if e.type == "tool.start" and e.node_id == "dual"]
    llm_ends = [e for e in before if e.type == "llm.end" and e.node_id == "dual"]
    assert verify_starts and llm_ends

    done = await decide(client, run.id, {"gmv": "a"})
    assert done.status == "succeeded", done.error
    assert blind.calls == calls_before, f"恢复之后盲算又调用了模型：{blind.calls[len(calls_before):]}"
    evs = await events(run.id)
    assert [e.seq for e in evs if e.type == "tool.start" and e.node_id == "dual"] == [e.seq for e in verify_starts]
    assert [e.seq for e in evs if e.type == "llm.end" and e.node_id == "dual"] == [e.seq for e in llm_ends]
    for code in ("dual_check_disagreed", "dual_check_resolved", "dual_check_catalog"):
        assert len(logs(evs, code)) == 1, code
    assert len([e for e in evs if e.type == "human.requested"]) == 1
    assert len([e for e in evs if e.type == "human.resolved"]) == 1
    assert len([e for e in evs if e.type == "node.finished" and e.node_id == "dual"]) == 1


async def test_the_report_catalog_leaves_out_the_blind_queries(monkeypatch, shop):
    name, _ = shop
    blind = Blind(monkeypatch, name, {"销售额": (BLIND_TOTAL, "blind_total")}, writer="本周销售额 [[m:gmv]]。")
    run = await start(graph(name, RIGHT, report=True))
    assert run.status == "succeeded", run.error
    [prompt] = blind.writer_prompts
    assert "blind_total" not in prompt and "Q2" not in prompt, "盲算的查询进了报告的写作目录"
    assert "Q1" in prompt and "m:gmv" in prompt
    evs = await events(run.id)
    blind_queries = [e.data["query_artifact"] for e in evs
                     if e.type == "tool.end" and e.node_id == "dual" and e.data.get("query_artifact")]
    ledger = [entry for e in evs if e.type == "node.finished" for entry in e.data.get("evidence") or []]
    assert blind_queries and not any(entry.get("artifact") in blind_queries for entry in ledger)
