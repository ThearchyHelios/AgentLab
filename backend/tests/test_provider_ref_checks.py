"""保存设置、校验画布、发布时就查出点名了不存在或已停用的模型接入（运行时它们会照实报错，见 test_provider_ref_runtime）。

- PUT /api/settings：引用接入的几项（核查模型、证据裁判、门控模型、助手）写了不存在或已停用的接入 → 422，带 field、
  code 和设置页上的叫法；一组出错整次不落库。留空照旧表示沿用上一级；存着的值没改（写好之后接入才被删、停用）不挡
  这次保存别的项——设置页标着「不存在」，运行时照实报错；
- 画布校验：节点点名的接入不存在或已停用给警告，说清哪个节点、哪个接入；发布前检查、发布、自动修复的复核给错误，
  挡住发布。只查执行时真会去取模型的那几处（分支「让模型分类」、校验「让模型修复」且允许重试、检索「让模型重排」
  时才算；核查节点自己的开关关着不算）；报告没写裁判配置时查全图默认里的那一份；
- 提示按这一处在界面上能不能改：检查器里有接入下拉框的说「在节点中换一个接入」，别处说「在发布前检查中改用」；
- 发布前检查给每一处配一条要人选的修复：改用一个启用的接入，或不指定接入（修完以后执行时落到哪里、只写了模型而
  提供它的接入已停用的那一处，见 test_provider_ref_landing）。

接入名、节点名都是虚构的。
"""
from __future__ import annotations

import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete

from app.db.base import SessionLocal
from app.db.models import Provider, Setting, Workflow, WorkflowVersion
from app.main import app

LIVE, OFF, GONE = "核对用接入", "核对用停用接入", "核对用接人"
SETTING_KEYS = ["verify", "judge", "copilot", "run"]


async def _clean() -> None:
    async with SessionLocal() as session:
        await session.execute(delete(Setting).where(Setting.key.in_(SETTING_KEYS)))
        await session.execute(delete(Provider).where(Provider.name.in_([LIVE, OFF])))
        await session.commit()


@pytest.fixture(autouse=True)
async def providers():
    """一个启用、一个停用的接入（都是演示类，不联网）；用完删掉，免得改变同一进程里别的测试「没点名」时的回退。"""
    await _clean()
    async with SessionLocal() as session:
        live = Provider(name=LIVE, kind="mock", models=[{"id": "核对模型"}], default_model="核对模型", enabled=True,
                        extra={})
        off = Provider(name=OFF, kind="mock", models=[{"id": "核对停用模型"}], default_model="核对停用模型",
                       enabled=False, extra={})
        session.add_all([live, off])
        await session.commit()
        ids = {LIVE: live.id, OFF: off.id}
    yield ids
    await _clean()


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def stored(key: str):
    async with SessionLocal() as session:
        row = await session.get(Setting, key)
        return row.value if row else None


# --------------------------------------------------------------------------
# 保存设置
# --------------------------------------------------------------------------

#: (组, 键, 设置页上的叫法)
FIELDS = [
    ("verify", "provider", "核查模型 · 接入"),
    ("judge", "provider", "裁判模型 · 接入"),
    ("run", "tool_gate_provider", "门控模型 · 接入"),
    ("copilot", "provider", "助手模型 · 接入"),
]


@pytest.mark.parametrize("group,key,label", FIELDS)
async def test_saving_a_provider_that_does_not_exist_is_refused_with_the_field(client, group, key, label):
    r = await client.put("/api/settings", json={"values": {group: {key: GONE}}})
    assert r.status_code == 422, r.text
    body = r.json()
    assert body["code"] == "settings_provider_invalid" and body["field"] == f"{group}.{key}"
    assert body["detail"].startswith(f"「{label}」填写的模型接入「{GONE}」不存在。"), body["detail"]
    assert "请在「设置 → 模型接入」中核对名称，或换一个接入" in body["detail"] and "留空表示" in body["detail"]
    assert await stored(group) is None


@pytest.mark.parametrize("group,key,label", FIELDS)
async def test_saving_a_disabled_provider_is_refused_too(client, group, key, label):
    r = await client.put("/api/settings", json={"values": {group: {key: OFF}}})
    assert r.status_code == 422, r.text
    body = r.json()
    assert body["field"] == f"{group}.{key}"
    assert body["detail"].startswith(f"「{label}」填写的模型接入「{OFF}」已停用。"), body["detail"]
    assert "请在「设置 → 模型接入」中启用它，或换一个接入" in body["detail"]
    assert await stored(group) is None


async def test_one_bad_group_keeps_the_whole_request_out_of_the_database(client):
    r = await client.put("/api/settings", json={"values": {
        "run": {"default_memory_scope": "门店", "tool_gate_provider": LIVE},
        "judge": {"provider": LIVE, "model": None},
        "verify": {"provider": GONE},
    }})
    assert r.status_code == 422 and r.json()["field"] == "verify.provider"
    assert await stored("run") is None and await stored("judge") is None and await stored("verify") is None


async def test_blank_means_inherit_and_an_enabled_provider_is_taken_by_name_or_id(client, providers):
    r = await client.put("/api/settings", json={"values": {
        "verify": {"provider": None, "model": "核对模型"},
        "judge": {"provider": "  "},
        "run": {"tool_gate_provider": ""},
    }})
    assert r.status_code == 200, r.text
    r = await client.put("/api/settings", json={"values": {
        "verify": {"provider": LIVE}, "judge": {"provider": providers[LIVE]},
        "run": {"tool_gate_provider": LIVE}, "copilot": {"provider": LIVE, "model": ""},
    }})
    assert r.status_code == 200, r.text
    got = r.json()
    assert got["verify"]["provider"] == LIVE and got["judge"]["provider"] == providers[LIVE]
    assert got["run"]["tool_gate_provider"] == LIVE


async def test_a_stored_provider_removed_later_does_not_block_saving_the_rest_of_its_group(client):
    """写好之后接入才被删掉：存着的值没改，这次保存别的项照常（运行时照实报错，设置页标着「不存在」）。
    改成另一个不存在的名字仍然拦下。"""
    async with SessionLocal() as session:
        session.add(Setting(key="verify", value={"provider": GONE, "daily_max_usd": 2.0}))
        await session.commit()
    r = await client.put("/api/settings", json={"values": {"verify": {"provider": f" {GONE} ", "daily_max_usd": 3}}})
    assert r.status_code == 200, r.text
    assert (await stored("verify"))["daily_max_usd"] == 3
    r = await client.put("/api/settings", json={"values": {"verify": {"provider": "另一个不存在的接入"}}})
    assert r.status_code == 422 and r.json()["field"] == "verify.provider"


async def test_format_problems_are_still_reported_first(client):
    r = await client.put("/api/settings", json={"values": {"verify": {"provider": 3}}})
    assert r.status_code == 422
    assert "需要填写模型接入的名称或模型 ID" in r.json()["detail"]


# --------------------------------------------------------------------------
# 画布校验、发布前检查、发布
# --------------------------------------------------------------------------


def node(nid, ntype, label=None, **config):
    return {"id": nid, "type": ntype, "position": {"x": 0, "y": 0}, "data": {"label": label or nid, "config": config}}


def chain(*nodes, defaults=None):
    graph = {"nodes": list(nodes), "edges": [{"source": a["id"], "target": b["id"]} for a, b in zip(nodes, nodes[1:])]}
    if defaults:
        graph["defaults"] = defaults
    return graph


def writer(ref: str | None, **extra) -> dict:
    return chain(node("start", "input"),
                 node("write", "llm", "写周报", **({"provider": ref} if ref else {}), prompt="写周报", **extra),
                 node("out", "output", fields=[{"name": "周报", "value": "{{ nodes.write.text }}"}]))


def provider_issues(result: dict) -> list[dict]:
    return [i for i in result["issues"] if str(i.get("code") or "").startswith("provider.")]


@pytest.mark.parametrize("ref,code,state", [(GONE, "provider.missing", "不存在"), (OFF, "provider.disabled", "已停用")])
async def test_validation_warns_about_a_node_naming_a_broken_provider(client, ref, code, state):
    r = await client.post("/api/workflows/validate", json={"graph": writer(ref)})
    assert r.status_code == 200, r.text
    [issue] = provider_issues(r.json())
    assert issue["level"] == "warning" and issue["code"] == code
    assert (issue["node_id"], issue["field"]) == ("write", "provider")
    assert issue["message"].startswith(f"「写周报」（模型调用）指定的模型接入「{ref}」{state}。"), issue["message"]
    assert r.json()["ok"] is True, "只是警告：探索运行仍可发起，执行到那里照实报错"


async def test_validation_is_quiet_for_an_enabled_provider_or_none(client, providers):
    for ref in (LIVE, providers[LIVE], None):
        r = await client.post("/api/workflows/validate", json={"graph": writer(ref)})
        assert provider_issues(r.json()) == [], ref


async def test_every_place_a_graph_names_a_provider_is_checked(client):
    graph = chain(
        node("start", "input"),
        node("team", "supervisor", "协作", provider=OFF, agents=[{"name": "分析师", "provider": GONE, "system": "分析"},
                                                                 {"provider": LIVE, "system": "复核"}]),
        node("write", "report", "写周报", instructions="写周报", claims="judge", judge={"provider": GONE}),
        node("dual", "dual_check", "复核销售额", provider=GONE),
        node("chal", "challenge", "审稿", report="write", provider=OFF),
        node("ask", "llm", "提问", prompt="总结"),        # 没写接入：取全图默认
        node("out", "output", fields=[{"name": "周报", "value": "{{ nodes.write.text }}"}]),
        defaults={"provider": GONE},
    )
    r = await client.post("/api/workflows/validate", json={"graph": graph})
    got = {(i["node_id"], i["field"]): i for i in provider_issues(r.json())}
    assert set(got) == {("team", "provider"), ("team", "agents[0].provider"), ("write", "judge.provider"),
                        ("dual", "provider"), ("chal", "provider"), (None, "defaults.provider")}
    assert got[("team", "agents[0].provider")]["message"].startswith(
        f"「协作」（多 Agent 协作）的成员「分析师」指定的模型接入「{GONE}」不存在")
    assert got[("write", "judge.provider")]["message"].startswith(
        f"「写周报」（报告撰写）的结论句裁判指定的模型接入「{GONE}」不存在")
    assert got[("dual", "provider")]["message"].startswith(f"「复核销售额」（双盲复算）的盲算模型指定的模型接入「{GONE}」")
    assert got[("chal", "provider")]["message"].startswith(f"「审稿」（反方审稿）的反方模型指定的模型接入「{OFF}」已停用")
    assert got[(None, "defaults.provider")]["message"].startswith(f"工作流默认设置指定的模型接入「{GONE}」不存在")


async def test_only_places_that_actually_call_a_model_are_checked(client):
    """分支按表达式判断、校验不让模型修复、检索不让模型重排时根本不取模型；报告不交给模型裁判时裁判的接入用不上；
    每个节点都写了自己的接入时，全图默认的接入没人用。"""
    graph = chain(
        node("start", "input"),
        node("route", "branch", "分流", mode="expression", provider=GONE, cases=[{"key": "a", "condition": "true"}]),
        node("check", "validate", "校验", repair_with_llm=False, provider=GONE, schema={"type": "object"}),
        node("look", "retrieve", "检索", rerank="off", provider=GONE),
        node("write", "report", "写周报", instructions="写周报", provider=LIVE, judge={"provider": GONE}),
        node("out", "output", fields=[{"name": "周报", "value": "{{ nodes.write.text }}"}]),
        defaults={"provider": GONE},
    )
    r = await client.post("/api/workflows/validate", json={"graph": graph})
    assert provider_issues(r.json()) == []
    graph["nodes"][1]["data"]["config"]["mode"] = "llm"
    graph["nodes"][3]["data"]["config"]["rerank"] = "model"
    r = await client.post("/api/workflows/validate", json={"graph": graph})
    assert {i["node_id"] for i in provider_issues(r.json())} == {"route", "look"}
    # 分类的分支不写自己的接入：取全图默认，默认里的接入这才有人用
    del graph["nodes"][1]["data"]["config"]["provider"]
    r = await client.post("/api/workflows/validate", json={"graph": graph})
    assert {(i["node_id"], i["field"]) for i in provider_issues(r.json())} == {("look", "provider"),
                                                                              (None, "defaults.provider")}


async def _workflow(graph: dict) -> str:
    async with SessionLocal() as session:
        wf = Workflow(name=f"门店周报-{uuid.uuid4().hex[:6]}", graph=graph, version=1)
        session.add(wf)
        await session.flush()
        session.add(WorkflowVersion(workflow_id=wf.id, version=1, graph=graph))
        await session.commit()
        return wf.id


@pytest.mark.parametrize("level", ["published", "governed"])
async def test_publish_check_and_publish_stop_at_a_broken_provider(client, level):
    wf = await _workflow(writer(GONE))
    r = await client.post(f"/api/workflows/{wf}/publish-check", json={"level": level})
    assert r.status_code == 200, r.text
    check = r.json()
    [issue] = provider_issues(check)
    assert issue["level"] == "error" and issue["node_id"] == "write"
    assert issue["fix"] == "provider.missing:write:provider"
    assert check["ok"] is False
    r = await client.post(f"/api/workflows/{wf}/publish", json={"level": level})
    body = r.json()
    assert body["ok"] is False and [i["code"] for i in provider_issues(body)] == ["provider.missing"]
    async with SessionLocal() as session:
        row = await session.get(Workflow, wf)
        assert row.published_version is None and row.status == "draft"


async def test_publish_check_reads_the_unsaved_canvas_and_autofix_keeps_the_problem(client):
    wf = await _workflow(writer(None))
    r = await client.post(f"/api/workflows/{wf}/publish-check", json={"level": "published", "graph": writer(OFF)})
    assert r.json()["ok"] is False and [i["code"] for i in provider_issues(r.json())] == ["provider.disabled"]
    r = await client.post(f"/api/workflows/{wf}/autofix", json={"level": "published", "graph": writer(OFF)})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["ok"] is False
    assert [(i["code"], i["level"]) for i in provider_issues({"issues": out["remaining"]})] == [
        ("provider.disabled", "error")]
    # 换成启用的接入：不再拦
    r = await client.post(f"/api/workflows/{wf}/publish-check", json={"level": "published", "graph": writer(LIVE)})
    assert provider_issues(r.json()) == [] and r.json()["ok"] is True


async def test_handing_only_a_provider_problem_to_the_assistant_says_it_cannot_help(client):
    """修复之后门禁不再拦、只剩点名接入的问题：不说「已没有阻止发布的错误」，说清助手处理不了、该去哪里改。"""
    from app.api.workflows import PROVIDER_LEFT

    wf = await _workflow(writer(None))
    r = await client.post(f"/api/workflows/{wf}/autofix", json={"level": "published", "graph": writer(GONE),
                                                                "assist": True})
    out = r.json()
    assert out["assist"] == {"ok": False, "summary": PROVIDER_LEFT, "questions": []}
    assert out["ok"] is False and [i["code"] for i in provider_issues({"issues": out["remaining"]})] == [
        "provider.missing"]
    r = await client.post(f"/api/workflows/{wf}/autofix", json={"level": "published", "graph": writer(LIVE),
                                                                "assist": True})
    assert r.json()["assist"]["summary"] == "修复后已没有阻止发布的错误，未再交给助手" and r.json()["ok"] is True


async def test_publishing_a_clean_graph_is_unaffected(client):
    wf = await _workflow(writer(LIVE))
    r = await client.post(f"/api/workflows/{wf}/publish", json={"level": "published"})
    assert r.json()["ok"] is True, r.text


@pytest.mark.parametrize("group,key,label", [("run", "tool_gate_provider", "门控模型 · 接入"),
                                             ("copilot", "provider", "助手模型 · 接入")])
async def test_a_provider_name_that_is_not_text_is_refused_with_the_field(client, group, key, label):
    """门控、助手两组没有自己的格式检查：名字写成数字一类以前照样存下，查助手当前用哪个模型时就出错。"""
    r = await client.put("/api/settings", json={"values": {group: {key: 123}}})
    assert r.status_code == 422, r.text
    body = r.json()
    assert body["code"] == "settings_provider_invalid" and body["field"] == f"{group}.{key}"
    assert body["detail"].startswith(f"「{label}」需要填写模型接入的名称，留空表示"), body["detail"]
    assert body["detail"].endswith("当前为「123」")
    assert await stored(group) is None


# --------------------------------------------------------------------------
# 只查真会取模型的地方（续）、报告的裁判取自全图默认、提示指到能改的地方
# --------------------------------------------------------------------------


async def test_a_switched_off_verify_node_or_a_validate_without_retries_is_not_checked(client):
    """核查节点自己的开关关着时执行器直接跳过、不取模型；结构校验不许重试（max_retries 为 0）时一次都不修。它们点名的
    接入写错了不提、不挡发布。开关打开、允许重试就照查。"""
    graph = chain(
        node("start", "input"),
        node("write", "report", "写周报", instructions="写周报", provider=LIVE),
        node("dual", "dual_check", "复核销售额", enabled=False, provider=GONE),
        node("chal", "challenge", "审稿", report="write", enabled=False, provider=OFF),
        node("check", "validate", "校验", max_retries=0, provider=GONE, schema={"type": "object"}),
        node("out", "output", fields=[{"name": "周报", "value": "{{ nodes.write.text }}"}]),
    )
    wf = await _workflow(graph)
    r = await client.post(f"/api/workflows/{wf}/publish-check", json={"level": "published", "graph": graph})
    assert provider_issues(r.json()) == []
    r = await client.post("/api/workflows/validate", json={"graph": graph})
    assert provider_issues(r.json()) == []
    graph["nodes"][2]["data"]["config"]["enabled"] = True
    del graph["nodes"][3]["data"]["config"]["enabled"]           # 没写开关：跟随设置，照查
    graph["nodes"][4]["data"]["config"]["max_retries"] = 1
    r = await client.post(f"/api/workflows/{wf}/publish-check", json={"level": "published", "graph": graph})
    assert {i["node_id"] for i in provider_issues(r.json())} == {"dual", "chal", "check"}


async def test_a_report_judged_with_the_graph_default_judge_checks_that_provider_once(client):
    """报告没写自己的裁判配置时执行用全图默认里的那一份（report_judge）：它点名的接入记在全图默认上，几个报告共用
    时只记一条；写了自己的裁判配置的报告不受它影响；不交给模型裁判的报告用不上它。"""
    graph = chain(
        node("start", "input"),
        node("a", "report", "写周报", instructions="写周报", provider=LIVE),             # claims 取全图默认的 judge
        node("b", "report", "写月报", instructions="写月报", provider=LIVE),
        node("c", "report", "写年报", instructions="写年报", provider=LIVE, judge={"provider": LIVE}),
        node("d", "report", "写日报", instructions="写日报", provider=LIVE, claims="off"),
        node("out", "output", fields=[{"name": "周报", "value": "{{ nodes.a.text }}"}]),
        defaults={"claims": "judge", "judge": {"provider": GONE}},
    )
    r = await client.post("/api/workflows/validate", json={"graph": graph})
    [issue] = provider_issues(r.json())
    assert (issue["node_id"], issue["field"], issue["code"]) == (None, "defaults.judge.provider", "provider.missing")
    assert issue["message"].startswith(f"工作流默认设置中的结论句裁判指定的模型接入「{GONE}」不存在。"), issue["message"]
    wf = await _workflow(graph)
    r = await client.post(f"/api/workflows/{wf}/publish-check", json={"level": "published", "graph": graph})
    check = r.json()
    assert check["ok"] is False
    assert [i["fix"] for i in provider_issues(check)] == ["provider.missing:graph:defaults.judge.provider"]


async def test_the_hint_points_to_where_the_provider_can_actually_be_changed(client):
    """检查器里有接入下拉框的（双盲复算、反方审稿、报告自己的结论句裁判）说「在节点中换一个接入」；节点自己的接入、
    协作成员、全图默认在界面上没有下拉框，说「在发布前检查中改用」——那里有这一处的修复。"""
    graph = chain(
        node("start", "input"),
        node("ask", "llm", "提问", provider=GONE, prompt="总结"),
        node("write", "report", "写周报", instructions="写周报", provider=LIVE, claims="judge", judge={"provider": OFF}),
        node("dual", "dual_check", "复核销售额", provider=GONE),
        node("team", "supervisor", "协作", provider=LIVE, agents=[{"name": "分析师", "provider": OFF, "system": "分析"}]),
        node("out", "output", fields=[{"name": "周报", "value": "{{ nodes.write.text }}"}]),
    )
    r = await client.post("/api/workflows/validate", json={"graph": graph})
    got = {(i["node_id"], i["field"]): i["message"] for i in provider_issues(r.json())}
    in_node, in_check = "或在节点中换一个接入", "或在「发布前检查」中改用其他接入"
    assert got[("ask", "provider")].endswith(f"。请在「设置 → 模型接入」中核对名称，{in_check}"), got
    assert got[("dual", "provider")].endswith(f"。请在「设置 → 模型接入」中核对名称，{in_node}"), got
    assert got[("write", "judge.provider")].endswith(f"。请在「设置 → 模型接入」中启用它，{in_node}"), got
    assert got[("team", "agents[0].provider")].endswith(f"。请在「设置 → 模型接入」中启用它，{in_check}"), got


# --------------------------------------------------------------------------
# 发布前检查的修复：改用一个启用的接入，或不指定接入
# --------------------------------------------------------------------------


async def _fix(client, wf: str, graph: dict, fix_id: str, value) -> dict:
    r = await client.post(f"/api/workflows/{wf}/autofix", json={"level": "published", "graph": graph,
                                                                "apply": [fix_id], "choices": {fix_id: value}})
    assert r.status_code == 200, r.text
    return r.json()


def config_of(graph: dict, node_id: str) -> dict:
    return next(n for n in graph["nodes"] if n["id"] == node_id)["data"]["config"]


async def test_each_broken_provider_gets_a_choice_to_switch_or_stop_naming_one(client):
    """多数节点的检查器里没有接入下拉框：发布前检查里的这条修复是界面上唯一能改它的地方。候选只列启用的接入，不替人选；
    选的接入不提供这一处写着的模型时，模型一并去掉（拿 A 家的模型名去调 B 家必然失败），候选的说明里写清。"""
    graph = writer(GONE, model="核对旧模型")
    wf = await _workflow(graph)
    r = await client.post(f"/api/workflows/{wf}/publish-check", json={"level": "published", "graph": graph})
    check = r.json()
    [issue] = provider_issues(check)
    [fix] = [f for f in check["fixes"] if f["id"] == issue["fix"]]
    assert (fix["kind"], fix["node_id"], fix["field"]) == ("choice", "write", "provider")
    assert fix["label"] == "选择「写周报」（模型调用）改用的模型接入"
    assert "default" not in fix and not any(k.startswith("_") for k in fix)
    values = [o["value"] for o in fix["options"]]
    assert values[0] == "" and fix["options"][0]["label"] == "不指定接入"
    assert LIVE in values and OFF not in values and GONE not in values
    live = next(o for o in fix["options"] if o["value"] == LIVE)
    assert "不提供模型「核对旧模型」，将改用它的默认模型「核对模型」" in live["hint"] and "演示用" in live["hint"]

    out = await _fix(client, wf, graph, fix["id"], LIVE)
    assert out["applied"] == [fix["id"]] and out["rejected"] == [] and out["ok"] is True, out["rejected"]
    assert config_of(out["graph"], "write")["provider"] == LIVE and "model" not in config_of(out["graph"], "write")
    assert [(c["node_id"], c["field"], c["before"], c["after"]) for c in out["changes"]] == [
        ("write", "provider", GONE, LIVE), ("write", "model", "核对旧模型", None)]
    assert provider_issues({"issues": out["remaining"]}) == []

    # 不指定接入：哪一级都没写接入，按模型名找；「核对旧模型」没有哪个接入提供，执行时会落到第一个可用的接入去调它，
    # 所以模型一并去掉，说明里写清改用哪里（详见 test_provider_ref_landing）
    clear = fix["options"][0]
    assert clear["hint"].startswith("去掉这一处指定的接入和模型「核对旧模型」，执行时改用第一个可用的模型接入「"), clear
    out = await _fix(client, wf, graph, fix["id"], "")
    assert out["applied"] == [fix["id"]] and out["ok"] is True, out["rejected"]
    config = config_of(out["graph"], "write")
    assert "provider" not in config and "model" not in config
    assert [(c["field"], c["before"], c["after"]) for c in out["changes"]] == [("provider", GONE, None),
                                                                              ("model", "核对旧模型", None)]


async def test_a_team_node_and_its_member_are_fixed_one_place_at_a_time(client):
    """同一个协作节点上自己的接入和成员的接入各有一条修复：只修成员那一处时，节点自己那一处还在不算没修好。"""
    graph = chain(
        node("start", "input"),
        node("team", "supervisor", "协作", provider=GONE, goal="分析本周销售",
             agents=[{"name": "分析师", "provider": GONE, "system": "分析"}]),
        node("out", "output", fields=[{"name": "结论", "value": "{{ last_message }}"}]),
    )
    wf = await _workflow(graph)
    r = await client.post(f"/api/workflows/{wf}/publish-check", json={"level": "published", "graph": graph})
    issues = provider_issues(r.json())
    member = "provider.missing:team:agents[0].provider"
    assert [(i["field"], i["fix"]) for i in issues] == [("provider", "provider.missing:team:provider"),
                                                        ("agents[0].provider", member)]
    assert {f["id"] for f in r.json()["fixes"]} >= {"provider.missing:team:provider", member}
    out = await _fix(client, wf, graph, member, LIVE)
    assert out["applied"] == [member], out["rejected"]
    config = config_of(out["graph"], "team")
    assert config["agents"][0]["provider"] == LIVE and config["provider"] == GONE
    assert [i["field"] for i in provider_issues({"issues": out["remaining"]})] == ["provider"]
    assert out["ok"] is False


async def test_the_graph_default_provider_has_its_own_fix(client):
    graph = chain(node("start", "input"), node("ask", "llm", "提问", prompt="总结"),
                  node("out", "output", fields=[{"name": "结论", "value": "{{ last_message }}"}]),
                  defaults={"provider": OFF, "model": "核对停用模型"})
    wf = await _workflow(graph)
    r = await client.post(f"/api/workflows/{wf}/publish-check", json={"level": "published", "graph": graph})
    [issue] = provider_issues(r.json())
    assert (issue["node_id"], issue["code"], issue["fix"]) == (
        None, "provider.disabled", "provider.disabled:graph:defaults.provider")
    out = await _fix(client, wf, graph, issue["fix"], LIVE)
    assert out["applied"] == [issue["fix"]] and out["ok"] is True, out["rejected"]
    assert out["graph"]["defaults"] == {"provider": LIVE}
    assert [(c["node_id"], c["field"], c["before"], c["after"]) for c in out["changes"]] == [
        (None, "defaults.provider", OFF, LIVE), (None, "defaults.model", "核对停用模型", None)]


async def test_stop_naming_a_provider_is_not_offered_when_the_graph_default_is_broken_too(client):
    """节点不再指定接入就改取全图默认：全图默认的接入也坏了，修完照样失败，「不指定接入」这一项不给；直接把这个值发给
    接口也不采用。换成启用的接入照常给。"""
    graph = chain(node("start", "input"), node("ask", "llm", "提问", provider=GONE, prompt="总结"),
                  node("out", "output", fields=[{"name": "结论", "value": "{{ last_message }}"}]),
                  defaults={"provider": OFF})
    wf = await _workflow(graph)
    r = await client.post(f"/api/workflows/{wf}/publish-check", json={"level": "published", "graph": graph})
    [issue] = provider_issues(r.json())          # 唯一的节点写了自己的接入，全图默认的没人用，不提
    [fix] = [f for f in r.json()["fixes"] if f["id"] == issue["fix"]]
    values = [o["value"] for o in fix["options"]]
    assert "" not in values and LIVE in values, values
    out = await _fix(client, wf, graph, issue["fix"], "")
    assert out["applied"] == [] and [x["fix_id"] for x in out["rejected"]] == [issue["fix"]]
    assert config_of(out["graph"], "ask")["provider"] == GONE and out["changes"] == []
