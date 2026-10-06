"""点名的模型接入（K1）收尾：说法与修复候选。

K1 让点名的接入不存在或已停用时照实报错、发布前挡住并给修复。复核留下的几处：
- 核查节点那句「……的盲算模型指定的模型接入」：指定接入的是「盲算模型 · 接入」这一栏，不是模型本身；
- 修复规划：没有候选能落稳时修复为空，问题却仍说「或在「发布前检查」中改用其他接入」；按（编号、节点、字段）认不出修复
  时退到同节点、同编号别处的修复，接口里的 fix 指错；改动波及、但本来就因自己点名的接入坏着的那一处也算落不稳，两处
  互相牵连时谁都给不出修复；
- 「不指定接入」落到演示类接入时，说明不提「演示用、不调用真实模型」——K1 要防的就是悄悄换成演示模型；
- 点名的接入名恰好是某个启用接入的模型：提示只说去启用那条旧接入，用户多半想要的是这个模型；
- 保存设置 422 的说法：下拉框写成「填写的」，留空那半句和设置页的叫法对不上，助手那一项叫法界面上没有；
- 「交给助手」后只剩接入问题时的说明：一句两个冒号、「核对」没有宾语；
- 同一节点同时提示「没有指定模型，将使用默认模型」和「指定的模型接入不存在」，互相矛盾；
- 助手的模型取不到时，提示指到真正能改它的地方（「编排」页助手栏的「助手使用的模型」）。

纯函数的用例用虚构的接入表，不读库；接口的用例建自己的几条接入、用完删掉。接入名、节点名、模型名都是虚构的。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete

from app.db.base import SessionLocal
from app.db.models import Provider, Setting, Workflow, WorkflowVersion
from app.engine import autofix
from app.engine.autofix import apply_fixes, check
from app.engine.governance import publish_issues
from app.engine.provider_refs import issues as provider_ref_issues
from app.engine.schema import GraphSpec
from app.main import app
from app.providers.factory import ProviderNotConfigured, assistant_unavailable, named_provider


def provider(name: str, *models: str, enabled: bool = True, kind: str = "openai_compatible") -> SimpleNamespace:
    return SimpleNamespace(id=f"pv-{name}", name=name, kind=kind, enabled=enabled,
                           models=[{"id": m} for m in models], default_model=models[0] if models else None)


JIA = provider("甲接入", "甲一号", "甲二号")
YI = provider("乙接入", "乙一号")
BING = provider("丙接入", "丙一号", enabled=False)
#: 停用的接入恰好叫「乙一号」——启用的乙接入提供同名的模型（开发数据里的原样：模型名被建成了一条接入）
TRAP = provider("乙一号", "旧一号", enabled=False)
TABLE = [JIA, YI, BING]
DEMO = provider("演示接入", "演示一号", kind="mock")
DEMO_NOTE = "（演示用，不调用真实模型）"
MISSING_FIX, DISABLED_FIX = "请在「设置 → 模型接入」中核对名称", "请在「设置 → 模型接入」中启用它"
IN_CHECK = "，或在「发布前检查」中改用其他接入"


def node(nid, ntype, label=None, **config):
    return {"id": nid, "type": ntype, "position": {"x": 0, "y": 0}, "data": {"label": label or nid, "config": config}}


def flow(*middle, defaults=None) -> dict:
    nodes = [node("start", "input"), *middle,
             node("out", "output", fields=[{"name": "结论", "value": "{{ last_message }}"}])]
    graph = {"nodes": nodes, "edges": [{"source": a["id"], "target": b["id"]} for a, b in zip(nodes, nodes[1:])]}
    if defaults:
        graph["defaults"] = defaults
    return graph


def provider_issues(issues) -> list[dict]:
    return [i for i in map(autofix._as_dict, issues) if str(i.get("code") or "").startswith("provider.")]


def gate(graph: dict, table=TABLE) -> dict[tuple, dict]:
    found = provider_issues(publish_issues(GraphSpec.model_validate(graph), level="published", providers=table))
    return {(i["node_id"], i["field"]): i for i in found}


def checked(graph: dict, table=TABLE) -> dict:
    return check(GraphSpec.model_validate(graph), level="published", providers=table)


def fix_of(graph: dict, fid: str, table=TABLE) -> dict | None:
    return next((f for f in checked(graph, table)["fixes"] if f["id"] == fid), None)


def hints(fix: dict) -> dict:
    return {o["value"]: o.get("hint") for o in fix["options"]}


# --------------------------------------------------------------------------
# 核查节点：指定接入的是「盲算模型 · 接入」这一栏
# --------------------------------------------------------------------------


def test_a_verify_node_names_the_field_that_points_at_the_provider():
    """双盲复算、反方审稿：问题里写检查器上那一栏的叫法（「盲算模型 · 接入」「反方模型」），不说「盲算模型指定的接入」。
    修复的标题仍按角色说（「选择……的盲算模型改用的模型接入」），读得通。"""
    graph = flow(node("write", "report", "写周报", instructions="写周报", provider="甲接入"),
                 node("dual", "dual_check", "复核销售额", provider="戊接入"),
                 node("chal", "challenge", "审稿", report="write", provider="丙接入"),
                 node("chal2", "challenge", "复审", report="write", model="丙一号"))
    got = gate(graph)
    assert got[("dual", "provider")]["message"] == (
        f"「复核销售额」（双盲复算）的「盲算模型 · 接入」指定的模型接入「戊接入」不存在。{MISSING_FIX}，或在节点中换一个接入")
    assert got[("chal", "provider")]["message"] == (
        f"「审稿」（反方审稿）的「反方模型 · 接入」指定的模型接入「丙接入」已停用。{DISABLED_FIX}，或在节点中换一个接入")
    assert got[("chal2", "model")]["message"] == (
        "「复审」（反方审稿）的「反方模型」指定的模型「丙一号」所属的模型接入「丙接入」已停用。"
        f"{DISABLED_FIX}，或在节点中换一个模型")
    assert fix_of(graph, "provider.missing:dual:provider")["label"] == "选择「复核销售额」（双盲复算）的盲算模型改用的模型接入"


# --------------------------------------------------------------------------
# 没有修复时：提示不指到发布前检查，也不借别处的修复
# --------------------------------------------------------------------------


def test_without_a_fix_the_hint_does_not_send_people_to_the_publish_check():
    """协作节点点名的接入不存在，成员只写了停用的丙家的模型、跟随节点的接入：去掉或换掉节点的接入，成员都落不稳，
    修复为空。检查器里又没有这一栏，提示只剩去设置里核对——不说「或在「发布前检查」中改用其他接入」。画布校验的
    警告是同一句。成员换成启用的接入提供的模型，修复就有了，提示照常指到发布前检查。"""
    graph = flow(node("team", "supervisor", "协作", provider="戊接入", goal="分析",
                      agents=[{"name": "研究员", "model": "丙一号", "system": "研究"}]))
    found = checked(graph)
    [issue] = provider_issues(found["issues"])
    assert (issue["node_id"], issue["field"], issue["fix"]) == ("team", "provider", None)
    assert issue["message"] == f"「协作」（多 Agent 协作）指定的模型接入「戊接入」不存在。{MISSING_FIX}"
    [warn] = provider_ref_issues(GraphSpec.model_validate(graph), TABLE, level="warning")
    assert warn.message == issue["message"]

    graph["nodes"][1]["data"]["config"]["agents"][0]["model"] = "甲二号"
    [issue] = provider_issues(checked(graph)["issues"])
    assert issue["fix"] == "provider.missing:team:provider"
    assert issue["message"] == f"「协作」（多 Agent 协作）指定的模型接入「戊接入」不存在。{MISSING_FIX}{IN_CHECK}"


def test_an_issue_without_its_own_fix_does_not_borrow_another_places_fix():
    """全图默认点名停用的丙，节点「提问」只写了丙家的模型（跟随全图默认的接入），节点「复述」自己点名丙：复述那一处有
    修复（改用甲、乙），全图默认那一处没有（换掉或去掉，提问都落不稳）。全图默认那条不能认到复述的修复上去。"""
    graph = flow(node("a", "llm", "提问", model="丙一号", prompt="总结"),
                 node("b", "llm", "复述", provider="丙接入", model="乙一号", prompt="复述"),
                 defaults={"provider": "丙接入"})
    found = checked(graph)
    got = {(i["node_id"], i["field"]): i for i in provider_issues(found["issues"])}
    assert set(got) == {("b", "provider"), (None, "defaults.provider")}
    assert got[("b", "provider")]["fix"] == "provider.disabled:b:provider"
    assert got[(None, "defaults.provider")]["fix"] is None
    assert got[(None, "defaults.provider")]["message"] == f"工作流默认设置指定的模型接入「丙接入」已停用。{DISABLED_FIX}"
    assert [f["id"] for f in found["fixes"]] == ["provider.disabled:b:provider"]


# --------------------------------------------------------------------------
# 本来就因自己点名的接入坏着的那一处，不否决别处的修复
# --------------------------------------------------------------------------


def test_a_place_broken_by_its_own_provider_does_not_veto_a_fix_elsewhere():
    """「提问」点名不存在的戊接入、模型取全图默认的丙一号（只有停用的丙提供），「复述」跟随全图默认。去掉全图默认的模型
    以后，提问照样因为戊接入不存在而失败——这不是这次改动造成的，不拿它否决全图默认的修复；否则两条都没有修复，
    规划器自己卡住。先修全图默认，提问的修复随之出现（不指定接入、改用甲、改用乙）。"""
    graph = flow(node("a", "llm", "提问", provider="戊接入", prompt="总结"),
                 node("b", "llm", "复述", prompt="复述"),
                 defaults={"model": "丙一号"})
    found = checked(graph)
    got = {(i["node_id"], i["field"]): i for i in provider_issues(found["issues"])}
    assert set(got) == {("a", "provider"), (None, "defaults.model")}
    fid = "provider.disabled:graph:defaults.model"
    assert got[(None, "defaults.model")]["fix"] == fid
    assert got[("a", "provider")]["fix"] is None
    assert hints(fix_of(graph, fid))[""] == "去掉这一处指定的模型，执行时改用第一个可用的模型接入「甲接入」"

    out = apply_fixes(graph, [fid], {fid: ""}, level="published", providers=TABLE)
    assert out["applied"] == [fid] and out["rejected"] == [], out["rejected"]
    assert "model" not in (out["graph"].get("defaults") or {})
    [left] = provider_issues(out["remaining"])
    assert (left["node_id"], left["field"], left["fix"]) == ("a", "provider", "provider.missing:a:provider")
    [fix] = [f for f in out["fixes"] if f["id"] == left["fix"]]
    assert [o["value"] for o in fix["options"]] == ["", "甲接入", "乙接入"]


def test_the_place_being_fixed_is_never_waved_through():
    """被修的那一处自己换了接入引用：去掉节点的戊接入以后落到全图默认的戊接入，原因一字不差也不算修好。"""
    graph = flow(node("ask", "llm", "提问", provider="戊接入", prompt="总结"), defaults={"provider": "戊接入"})
    found = checked(graph)
    fid = "provider.missing:ask:provider"
    [fix] = [f for f in found["fixes"] if f["id"] == fid]
    assert "" not in hints(fix)
    out = apply_fixes(graph, [fid], {fid: ""}, level="published", providers=TABLE)
    assert out["applied"] == [], out


# --------------------------------------------------------------------------
# 落到演示类接入时照实说
# --------------------------------------------------------------------------


def test_stop_naming_a_provider_that_lands_on_a_demo_provider_says_so():
    """启用的只有演示类接入：「不指定接入」落到它，说明里和「改用演示接入」那一项一样写明演示用、不调用真实模型。"""
    table = [BING, DEMO]
    lone = flow(node("ask", "llm", "提问", provider="丙接入", model="丙一号", prompt="总结"))
    got = hints(fix_of(lone, "provider.disabled:ask:provider", table))
    assert got[""] == f"去掉这一处指定的接入和模型「丙一号」，执行时改用第一个可用的模型接入「演示接入」{DEMO_NOTE}"
    assert got["演示接入"] == "不提供模型「丙一号」，将改用它的默认模型「演示一号」；演示用的模型接入，不调用真实模型"
    # 有启用的真实接入：去掉以后落到真实接入，不带这句
    assert hints(fix_of(lone, "provider.disabled:ask:provider", [JIA, BING, DEMO]))[""] == (
        "去掉这一处指定的接入和模型「丙一号」，执行时改用第一个可用的模型接入「甲接入」")

    # 跟随节点点名的演示接入、按模型名落到演示接入，也一样
    graph = flow(node("team", "supervisor", "协作", provider="演示接入", goal="分析",
                      agents=[{"name": "分析师", "provider": "丙接入", "system": "分析"}]),
                 node("ask", "llm", "提问", provider="丙接入", model="演示一号", prompt="总结"))
    assert hints(fix_of(graph, "provider.disabled:team:agents[0].provider", table))[""] == (
        f"去掉这一处指定的接入，执行时改用「协作」（多 Agent 协作）指定的接入「演示接入」{DEMO_NOTE}")
    assert hints(fix_of(graph, "provider.disabled:ask:provider", table))[""] == (
        f"去掉这一处指定的接入，执行时改用提供模型「演示一号」的接入「演示接入」{DEMO_NOTE}")


# --------------------------------------------------------------------------
# 点名的接入名恰好是某个启用接入的模型
# --------------------------------------------------------------------------

MODEL_NOTE = "「乙一号」是接入「乙接入」的模型：如需使用该模型，请在模型一栏中填写，接入留空或选择「乙接入」"


def test_a_provider_name_that_is_really_a_model_gets_a_note():
    """停用的接入叫「乙一号」，启用的乙接入提供模型「乙一号」：照提示启用的会是那条旧接入，用户多半想要的是这个模型。
    报错照旧说停用了、去哪里启用，另补一句这是哪个接入的模型、怎么用它。不存在的名字同理；只有停用的接入提供的
    模型名、谁的模型都不是的名字不补。"""
    table = [JIA, YI, BING, TRAP]
    with pytest.raises(ProviderNotConfigured) as e:
        named_provider(table, "乙一号")
    assert e.value.code == "disabled" and e.value.reason == "模型接入「乙一号」已停用"
    assert str(e.value) == f"模型接入「乙一号」已停用。{DISABLED_FIX}，或换一个接入。{MODEL_NOTE}"
    with pytest.raises(ProviderNotConfigured) as e:
        named_provider(table, " 甲二号 ")
    assert str(e.value) == (f"模型接入「甲二号」不存在。{MISSING_FIX}，或换一个接入。"
                            "「甲二号」是接入「甲接入」的模型：如需使用该模型，请在模型一栏中填写，接入留空或选择「甲接入」")
    for name in ("丙一号", "戊接入"):
        with pytest.raises(ProviderNotConfigured) as e:
            named_provider(table, name)
        assert "是接入" not in str(e.value), str(e.value)

    # 画布、发布前检查：同一句补在提示后面
    graph = flow(node("ask", "llm", "提问", provider="乙一号", prompt="总结"))
    [issue] = provider_issues(checked(graph, table)["issues"])
    assert issue["message"] == (f"「提问」（模型调用）指定的模型接入「乙一号」已停用。{DISABLED_FIX}{IN_CHECK}。{MODEL_NOTE}")

    # 助手：指到能直接选这个模型的地方
    with pytest.raises(ProviderNotConfigured) as e:
        named_provider(table, "乙一号")
    assert assistant_unavailable(e.value) == (
        f"助手使用的模型接入「乙一号」已停用。{DISABLED_FIX}，或在「编排」页助手栏的「助手使用的模型」中重新选择。"
        "「乙一号」是接入「乙接入」的模型，可在「助手使用的模型」中直接选择")


def test_the_assistant_hint_points_to_the_dropdown_that_can_change_it():
    """设置页里没有助手的接入这一项：能改的是「编排」页助手栏的「助手使用的模型」（选「跟随默认」或一个模型会清掉存着的
    接入）。表格导入、数据目录的 AI 起草也用助手的模型，同一句。"""
    with pytest.raises(ProviderNotConfigured) as e:
        named_provider(TABLE, "戊接入")
    assert assistant_unavailable(e.value) == (
        f"助手使用的模型接入「戊接入」不存在。{MISSING_FIX}，或在「编排」页助手栏的「助手使用的模型」中重新选择")


# --------------------------------------------------------------------------
# 接口：保存设置、交给助手、画布校验（建自己的几条接入，用完删掉）
# --------------------------------------------------------------------------

LIVE, OFF, GONE = "措辞用接入", "措辞用停用接入", "措辞用接人"
#: 停用的接入，名字恰好是 LIVE 提供的模型
NAMED_LIKE_MODEL = "措辞模型"
SETTING_KEYS = ["verify", "judge", "copilot", "run"]
NAMES = [LIVE, OFF, NAMED_LIKE_MODEL]


async def _clean() -> None:
    async with SessionLocal() as session:
        await session.execute(delete(Setting).where(Setting.key.in_(SETTING_KEYS)))
        await session.execute(delete(Provider).where(Provider.name.in_(NAMES)))
        await session.commit()


@pytest.fixture
async def rows():
    await _clean()
    async with SessionLocal() as session:
        session.add_all([
            Provider(name=LIVE, kind="mock", models=[{"id": NAMED_LIKE_MODEL}], default_model=NAMED_LIKE_MODEL,
                     enabled=True, extra={}),
            Provider(name=OFF, kind="mock", models=[{"id": "措辞停用模型"}], default_model="措辞停用模型",
                     enabled=False, extra={}),
            Provider(name=NAMED_LIKE_MODEL, kind="mock", models=[{"id": "措辞旧模型"}], default_model="措辞旧模型",
                     enabled=False, extra={}),
        ])
        await session.commit()
    yield
    await _clean()


@pytest.fixture
async def client(rows):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def stored(key: str):
    async with SessionLocal() as session:
        row = await session.get(Setting, key)
        return row.value if row else None


#: (组, 键, 接入这一项在界面上的叫法, 模型那一项的叫法；助手只有一个下拉框)
SETTING_FIELDS = [
    ("verify", "provider", "核查模型 · 接入", "核查模型 · 模型"),
    ("judge", "provider", "裁判模型 · 接入", "裁判模型 · 模型"),
    ("run", "tool_gate_provider", "门控模型 · 接入", "门控模型 · 模型"),
    ("copilot", "provider", "助手使用的模型", None),
]


@pytest.mark.parametrize("group,key,label,model_label", SETTING_FIELDS)
async def test_saving_a_broken_provider_is_worded_like_the_settings_page(client, group, key, label, model_label):
    """下拉框里选的接入写「指定的」，不写「填写的」；留空那半句和设置页空选项的叫法对不上，不再说（那一格就在下拉框
    下面，空选项写着留空的意思）。助手那一项叫「助手使用的模型」，界面上没有「助手模型 · 接入」。"""
    r = await client.put("/api/settings", json={"values": {group: {key: GONE}}})
    assert r.status_code == 422, r.text
    assert r.json()["detail"] == f"「{label}」指定的模型接入「{GONE}」不存在。{MISSING_FIX}，或换一个接入"
    r = await client.put("/api/settings", json={"values": {group: {key: OFF}}})
    assert r.json()["detail"] == f"「{label}」指定的模型接入「{OFF}」已停用。{DISABLED_FIX}，或换一个接入"
    # 名字恰好是启用接入的模型：补一句这是哪个接入的模型、在这一组里怎么用它
    r = await client.put("/api/settings", json={"values": {group: {key: NAMED_LIKE_MODEL}}})
    note = (f"「{NAMED_LIKE_MODEL}」是接入「{LIVE}」的模型："
            f"如需使用该模型，请在「{model_label}」中填写，「{label}」留空或选择「{LIVE}」") if model_label else \
        f"「{NAMED_LIKE_MODEL}」是接入「{LIVE}」的模型，可在「助手使用的模型」中直接选择"
    assert r.json()["detail"] == f"「{label}」指定的模型接入「{NAMED_LIKE_MODEL}」已停用。{DISABLED_FIX}，或换一个接入。{note}"
    assert await stored(group) is None


@pytest.mark.parametrize("group,key,label", [("run", "tool_gate_provider", "门控模型 · 接入"),
                                             ("copilot", "provider", "助手使用的模型")])
async def test_a_provider_name_that_is_not_text_is_worded_without_the_blank_half(client, group, key, label):
    r = await client.put("/api/settings", json={"values": {group: {key: 123}}})
    assert r.status_code == 422
    assert r.json()["detail"] == f"「{label}」需要填写模型接入的名称，当前为「123」"


async def test_the_verify_format_check_says_blank_follows_the_judge_model_like_the_page(client):
    r = await client.put("/api/settings", json={"values": {"verify": {"provider": 3}}})
    assert r.status_code == 422
    assert r.json()["detail"] == "「核查模型 · 接入」需要填写模型接入的名称或模型 ID，留空表示跟随证据裁判模型，当前为「3」"


async def _workflow(graph: dict) -> str:
    async with SessionLocal() as session:
        wf = Workflow(name="措辞检查", graph=graph, version=1)
        session.add(wf)
        await session.flush()
        session.add(WorkflowVersion(workflow_id=wf.id, version=1, graph=graph))
        await session.commit()
        return wf.id


async def test_handing_only_provider_problems_to_the_assistant_is_one_sentence_per_step(client):
    """发布弹窗里渲染成「助手：……」：说明自己不再带冒号，「核对」有宾语，不说「节点」（可能是全图默认）。有修复时指到
    各条问题的修复；没有修复时不提。"""
    from app.api.workflows import PROVIDER_LEFT, PROVIDER_LEFT_FIX

    assert "：" not in PROVIDER_LEFT + PROVIDER_LEFT_FIX
    assert PROVIDER_LEFT == ("剩余的问题是所用的模型接入不存在或已停用，无法交给助手处理。"
                             "请在「设置 → 模型接入」中核对名称或启用相应的接入")
    assert PROVIDER_LEFT_FIX == "，或在各条问题的修复中改用其他接入"
    graph = flow(node("ask", "llm", "提问", provider=GONE, prompt="总结"))
    wf = await _workflow(graph)
    r = await client.post(f"/api/workflows/{wf}/autofix", json={"level": "published", "graph": graph, "assist": True})
    assert r.json()["assist"] == {"ok": False, "summary": PROVIDER_LEFT + PROVIDER_LEFT_FIX, "questions": []}

    # 没有一条有修复（成员只写了停用接入的模型，换掉、去掉节点的接入都落不稳）：不指到修复
    graph = flow(node("team", "supervisor", "协作", provider=GONE, goal="分析",
                      agents=[{"name": "研究员", "model": "措辞停用模型", "system": "研究"}]))
    r = await client.post(f"/api/workflows/{wf}/autofix", json={"level": "published", "graph": graph, "assist": True})
    out = r.json()
    assert [i["fix"] for i in provider_issues(out["remaining"])] == [None]
    assert out["assist"] == {"ok": False, "summary": PROVIDER_LEFT, "questions": []}


def unset_model(result: dict) -> list[dict]:
    return [i for i in result["issues"] if i.get("code") == "model.unset"]


async def test_no_model_says_which_provider_and_steps_aside_when_that_provider_is_broken(client):
    """节点点名了接入、没写模型：说用这个接入的默认模型。这个接入本身不存在或已停用时只留接入那一条：一条说会照常
    执行、一条说执行到这里会失败，读者无从判断。全图默认的接入同理。没点名接入的照旧说「将使用默认模型」。"""
    graph = flow(node("a", "llm", "提问", provider=LIVE, prompt="总结"),
                 node("b", "llm", "复述", provider=GONE, prompt="复述"),
                 node("c", "agent", "查数", prompt="查数"),
                 node("d", "report", "写周报", instructions="写周报", provider=""),
                 defaults={"provider": OFF})
    r = await client.post("/api/workflows/validate", json={"graph": graph})
    got = {i["node_id"]: i for i in unset_model(r.json())}
    assert set(got) == {"a"}, got
    assert (got["a"]["level"], got["a"]["field"], got["a"]["message"]) == (
        "warning", "model", f"没有指定模型，将使用接入「{LIVE}」的默认模型")
    assert {(i["node_id"], i["field"]) for i in provider_issues(r.json()["issues"])} == {
        ("b", "provider"), (None, "defaults.provider")}

    # 发布前检查同样只留接入那一条
    wf = await _workflow(graph)
    r = await client.post(f"/api/workflows/{wf}/publish-check", json={"level": "published", "graph": graph})
    assert {i["node_id"] for i in unset_model(r.json())} == {"a"}

    # 全图默认的接入换成启用的：跟随它的节点说用它的默认模型；什么都没点名的照旧
    graph["defaults"] = {"provider": LIVE}
    r = await client.post("/api/workflows/validate", json={"graph": graph})
    said = {i["node_id"]: i["message"] for i in unset_model(r.json())}
    assert said["c"] == said["a"] == f"没有指定模型，将使用接入「{LIVE}」的默认模型"
    graph["defaults"] = {}
    r = await client.post("/api/workflows/validate", json={"graph": graph})
    assert {i["node_id"]: i["message"] for i in unset_model(r.json())}["c"] == "没有指定模型，将使用默认模型"


async def test_the_assistant_model_lookup_explains_a_provider_named_like_a_model(client):
    async with SessionLocal() as session:
        session.add(Setting(key="copilot", value={"provider": NAMED_LIKE_MODEL, "model": ""}))
        await session.commit()
    got = (await client.get("/api/copilot/model")).json()
    assert got["error"] == (
        f"助手使用的模型接入「{NAMED_LIKE_MODEL}」已停用。{DISABLED_FIX}，或在「编排」页助手栏的「助手使用的模型」中重新选择。"
        f"「{NAMED_LIKE_MODEL}」是接入「{LIVE}」的模型，可在「助手使用的模型」中直接选择")
    # 在助手栏重新选「跟随默认」：存着的接入一并清掉，不再报错
    r = await client.put("/api/copilot/model", json={"model": None})
    assert r.json()["error"] is None and (await stored("copilot")) == {"provider": "", "model": ""}
