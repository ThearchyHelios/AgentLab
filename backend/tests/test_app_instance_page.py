"""流程应用的实例页和待办怎么说（apps/resolve.py 的流程组件、process/tasks.py 的待办描述）。

端到端验收（报销审批：财务会签、超时升级给总监）发现的几处「说得不对」：
- 发起人打开实例页，待办表单去取一条不是派给他的待办，红框写「待办不存在」：服务端解析时就判定读者是不是处理人，
  不是的只给这一步的状态（派给了谁、会签进度、时限），不让界面去取；
- 会签进度把登录核实过的审批人写成「自报署名」、没有处理时间：页面解析给的派给谁要带上这两项；
- 同意过的人再打开写「没有派给你」：待办上带这个人的回复，写「你已处理」；
- 走了「已退回」分支的实例写「已完成」：实例的状态说流程走到了哪个成果节点；走到的是从人工节点「驳回」出口一路走到的
  成果节点（否定的结局）时不画成成功的绿勾，时间线上「已驳回」那一步同样；
- 升级之后写「0/3 已同意」，其实总监一人同意就通过：会签人数不算升级加派的那一份，升级后的规则单列；
- 同一个人两笔报销，待办列表里标题一样分不开：待办带上实例标题和开出的时间；
- 停着等审批时印章那一块写「正在运行，完成后刷新页面」，和页头的「等待审批」「自动刷新」对不上：按在等的那一步说；
- 「会签进度 0/2」画了两遍（待办表单一遍、会签组件一遍）：同一页上有绑同一条待办的会签组件时，服务端告诉待办表单不画；
- 等待名单、会签名单写的是用户名：服务端给显示名（people），界面写显示名、悬停给用户名。
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.api.deps import Principal
from app.db.base import SessionLocal, utcnow
from app.db.models import App, AppVersion, Group, GroupMember, Run, Task, User
from app.engine.runner import run_manager
from app.process import tasks


@pytest.fixture(autouse=True)
async def engine_up():
    await run_manager.setup()
    yield
    await run_manager.shutdown()


def node(nid, ntype, label=None, **config):
    return {"id": nid, "type": ntype, "position": {"x": 0, "y": 0}, "data": {"label": label or nid, "config": config}}


def expense() -> dict:
    nodes = [node("start", "input", "发起报销", fields=[{"name": "applicant"}, {"name": "amount", "type": "number"}]),
             node("review", "human", "财务会签", mode="approve", title="报销审批：{{ input.applicant }}",
                  assignee={"kind": "slot", "slot": "finance"}, policy="all", due="PT1H",
                  on_timeout={"action": "escalate", "to_slot": "director"}),
             node("ok", "output", "已通过", fields=[{"name": "结论", "value": "已通过"}]),
             node("no", "output", "已退回", fields=[{"name": "结论", "value": "已退回"}])]
    edges = [{"source": "start", "target": "review"},
             {"source": "review", "target": "ok", "sourceHandle": "approved"},
             {"source": "review", "target": "no", "sourceHandle": "rejected"}]
    return {"nodes": nodes, "edges": edges}


SPEC = {"format": "agentlab.app/1", "name": "报销", "views": {"inst": {"source": {"kind": "instance"}}},
        "pages": [{"id": "instance", "title": "实例", "body": [
            {"id": "hdr", "type": "instance_header"}, {"id": "tl", "type": "instance_timeline"},
            {"id": "form", "type": "task_form", "bind": {"task": "review"}},
            {"id": "signs", "type": "signoffs", "bind": {"task": "review"}},
            {"id": "mine", "type": "task_list", "props": {"scope": "mine"}},
            {"id": "st", "type": "stamp", "bind": {"view": "inst"}}]}]}


def user(name: str) -> Principal:
    return Principal(username=name, role="user", mode="local", verified=True)


async def _wait(run_id: str, statuses: tuple[str, ...], timeout: float = 20.0) -> Run:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        async with SessionLocal() as session:
            row = await session.get(Run, run_id)
            if row and row.status in statuses and not run_manager.is_active(run_id):
                return row
        await asyncio.sleep(0.05)
    raise AssertionError(f"运行 {run_id} 没有进入 {statuses}")


async def _setup() -> tuple[str, str, str, str]:
    """一个应用：财务岗位绑一个组（两个人），总监岗位绑一个人。返回 (应用 id, 财务甲, 财务乙, 总监)。"""
    u = uuid.uuid4().hex[:6]
    a, b, boss, group = f"zhang{u}", f"wang{u}", f"zhao{u}", f"finance{u}"
    async with SessionLocal() as session:
        people = [User(username=n, role="user", display_name=f"财务{n[:1]}") for n in (a, b)]
        session.add_all(people)
        g = Group(name=group)
        session.add(g)
        await session.flush()
        session.add_all([GroupMember(group_id=g.id, user_id=p.id) for p in people])
        app = App(name=f"报销-{u}", published_version=1)
        session.add(app)
        await session.flush()
        session.add(AppVersion(app_id=app.id, version=1, spec=SPEC, spec_hash="x" * 64, bindings={"slots": {
            "finance": [{"kind": "group", "name": group}], "director": [{"kind": "user", "name": boss}]}}))
        await session.commit()
        return app.id, a, b, boss


async def _start(app_id: str, title: str, amount: float = 1280.5) -> str:
    run = await run_manager.start(graph=expense(), input_payload={"applicant": "钱七", "amount": amount},
                                  app_id=app_id, app_version=1, started_by="qianqi", run_class="formal",
                                  origin_kind="app_entry", origin={"entry": "start", "title": title})
    await _wait(run.id, ("interrupted",))
    return run.id


async def _task(run_id: str) -> Task:
    async with SessionLocal() as session:
        return (await session.execute(select(Task).where(Task.run_id == run_id))).scalars().one()


async def _page(app_id: str, run_id: str, reader: Principal) -> dict:
    from app.apps.resolve import resolve_page

    async with SessionLocal() as session:
        app = await session.get(App, app_id)
        version = (await session.execute(select(AppVersion).where(AppVersion.app_id == app_id))).scalars().one()
        run = await session.get(Run, run_id)
        vm = await resolve_page(session, app, page_id="instance", principal=reader, role="use", instance=run,
                                version=version)
    return {n["id"]: n.get("data") for n in vm["tree"]}


async def test_the_initiator_sees_the_step_not_a_missing_task():
    app_id, a, b, _ = await _setup()
    run_id = await _start(app_id, "报销审批·钱七")
    task = await _task(run_id)

    view = await _page(app_id, run_id, user("qianqi"))
    form = view["form"]
    # 发起人不是处理人：不让界面去取待办（取了是 404），给这一步的状态
    assert form["handler"] is False
    assert sorted((x["principal"], x["status"]) for x in form["assignments"]) == \
        sorted([(a, "pending"), (b, "pending")])
    assert form["progress"] == {"policy": "all", "voters": 2, "need": 2, "agreed": 0, "rejected": 0}
    assert form["task"]["due_at"] and form["task"]["instance_title"] == "报销审批·钱七"
    assert tasks.visible(await tasks.detail(task.id, user("qianqi")), user("qianqi")) is False
    # 处理人：给待办卡（界面按 id 取完整的待办）
    assert (await _page(app_id, run_id, user(a)))["form"]["handler"] is True
    # 关登录时人人都算处理人（同待办服务）
    assert (await _page(app_id, run_id, Principal(username=None, role="admin", mode="off")))["form"]["handler"] is True
    hdr = view["hdr"]
    assert (hdr["status_code"], hdr["status_text"]) == ("waiting", "等待审批")
    assert next(s for s in view["tl"]["steps"] if s["node"] == "review")["text"] == "等待审批"
    # 停着等审批：印章那一块按在等的那一步说，不写「正在运行，完成后刷新页面」（页头写着等待审批、自动刷新）
    assert view["st"]["state"] == "running" and view["st"]["message"] == "等待审批，流程走完后自动显示"
    # 同一页上有绑同一条待办的会签组件：待办表单不再画会签进度，会签组件照画
    assert form["signoffs_on_page"] is True and "signoffs_on_page" not in view["signs"]
    # 等待名单写显示名：服务端给用户名 → 显示名，界面悬停给用户名
    assert form["people"] == {a: "财务z", b: "财务w"} and view["signs"]["people"] == form["people"]
    detail = await tasks.detail(task.id, user(a))
    assert detail["people"] == {a: "财务z", b: "财务w"}, "待办卡（待办服务）同一个口径"


async def test_signoffs_say_who_verified_and_when_and_the_voter_sees_their_own_answer():
    app_id, a, b, _ = await _setup()
    run_id = await _start(app_id, "报销审批·钱七")
    task = await _task(run_id)
    assert (await tasks.respond(task.id, user(a), decision="approve"))["resolved"] is False

    signs = (await _page(app_id, run_id, user("qianqi")))["signs"]
    done = next(x for x in signs["assignments"] if x["principal"] == a)
    # 登录核实过的写「已登录核实」，不是「自报署名」；带处理时间
    assert done["status"] == "responded" and done["verified"] is True and done["responded_at"]
    assert done["decision"] == "approve" and done["source"] == "slot:finance"
    assert signs["progress"]["agreed"] == 1 and signs["progress"]["need"] == 2

    # 同意过的人再打开：不能再回复，带上他的回复（写「你已处理：同意」，不写「没有派给你」）
    mine = await tasks.detail(task.id, user(a))
    assert mine["can_respond"] is False and mine["my_response"]["decision"] == "approve"
    assert mine["my_response"]["at"] and mine["my_response"]["verified"] is True
    assert (await tasks.detail(task.id, user(b)))["my_response"] is None
    assert (await _page(app_id, run_id, user(a)))["form"]["my_response"]["decision"] == "approve"


async def test_the_instance_says_which_outcome_it_reached():
    app_id, a, b, _ = await _setup()
    passed = await _start(app_id, "报销审批·钱七")
    task = await _task(passed)
    await tasks.respond(task.id, user(a), decision="approve")
    await tasks.respond(task.id, user(b), decision="approve")
    await _wait(passed, ("succeeded",))
    view = await _page(app_id, passed, user("qianqi"))
    assert (view["hdr"]["status_code"], view["hdr"]["status_text"]) == ("succeeded", "已通过")
    review = next(s for s in view["tl"]["steps"] if s["node"] == "review")
    assert (review["state"], review["text"]) == ("done", "已同意")

    back = await _start(app_id, "报销审批·钱七")
    await tasks.respond((await _task(back)).id, user(b), decision="reject", note="票据不全")
    await _wait(back, ("succeeded",))
    view = await _page(app_id, back, user("qianqi"))
    # 走了退回分支：运行是「已完成」，流程的结论是「已退回」。这是否定的结局（从「驳回」出口走到的成果节点），状态
    # 标记同「未确认（被驳回）」，不画成成功；时间线上驳回的那一步同样
    assert (view["hdr"]["status_code"], view["hdr"]["status_text"]) == ("cancelled", "已退回")
    assert view["tl"]["status_code"] == "cancelled"
    review = next(s for s in view["tl"]["steps"] if s["node"] == "review")
    assert (review["state"], review["text"]) == ("rejected", "已驳回")
    # 判定之后没回复的都撤回了：会签人数按判定那一刻的算，不缩成 1
    assert view["signs"]["progress"] == {"policy": "all", "voters": 2, "need": 2, "agreed": 0, "rejected": 1}
    from app.apps.resolve import list_instances

    async with SessionLocal() as session:
        rows = {r["run_id"]: r for r in await list_instances(session, app_id, mine="qianqi")}
    assert (rows[back]["status_text"], rows[passed]["status_text"]) == ("已退回", "已通过")
    assert (rows[back]["status_code"], rows[passed]["status_code"]) == ("cancelled", "succeeded")


def test_a_negative_outcome_is_read_from_the_graph_and_this_runs_decision():
    """否定的结局按图认（人工节点「驳回」出口一路走到的成果节点，中间不经过别的人工节点），再看这次运行里那个人工节点
    是不是真的驳回了：两条路汇到同一个成果节点时，批准走过来的不算否定。不按成果节点的标题猜。"""
    from types import SimpleNamespace

    from app.apps.resolve import _Trail, instance_state, rejected_outputs

    graph = {"nodes": [node("start", "input", "发起"), node("review", "human", "财务会签", mode="approve"),
                       node("pay", "tool", "付款"), node("note", "tool", "记一笔"),
                       node("done", "output", "办结"), node("back", "output", "打回重填"),
                       node("again", "human", "复核", mode="approve"), node("ok", "output", "复核通过")],
             "edges": [{"source": "start", "target": "review"},
                       {"source": "review", "target": "pay", "sourceHandle": "approved"},
                       {"source": "review", "target": "note", "sourceHandle": "rejected"},
                       {"source": "pay", "target": "done"}, {"source": "note", "target": "done"},
                       {"source": "note", "target": "back"},
                       {"source": "review", "target": "again", "sourceHandle": "rejected"},
                       {"source": "again", "target": "ok", "sourceHandle": "approved"}]}
    # 「复核通过」在另一个人工节点之后：结局由复核的人说了算，不算 review 驳回的结局
    assert rejected_outputs(graph) == {"done": {"review"}, "back": {"review"}}

    def state(decision, reached):
        task = SimpleNamespace(node_id="review", kind="approve", status="resolved", outcome={"decision": decision})
        trail = _Trail(tasks=[task], events=[("node.finished", reached)])
        return instance_state(SimpleNamespace(status="succeeded", graph=graph), trail)

    assert state("reject", "back") == {"code": "cancelled", "text": "打回重填"}
    assert state("expired_reject", "done") == {"code": "cancelled", "text": "办结"}
    assert state("approve", "done") == {"code": "succeeded", "text": "办结"}, "批准走过来的同一个成果节点不算否定"


async def test_outputs_reached_in_parallel_are_not_one_outcome():
    """几个成果节点并行都走到了（「周报」「明细」一起出）：那不是哪一种结局，写「已完成」，不写最后走到的那个节点的名字。"""
    from app.apps.resolve import instance_rows

    graph = {"nodes": [node("start", "input", "发起", fields=[{"name": "week"}]),
                       node("weekly", "output", "周报", fields=[{"name": "周", "value": "{{ input.week }}"}]),
                       node("detail", "output", "明细", fields=[{"name": "周", "value": "{{ input.week }}"}])],
             "edges": [{"source": "start", "target": "weekly"}, {"source": "start", "target": "detail"}]}
    run = await run_manager.start(graph=graph, input_payload={"week": "2026-W41"})
    done = await _wait(run.id, ("succeeded", "failed"))
    assert done.status == "succeeded", done.error
    async with SessionLocal() as session:
        [row] = await instance_rows(session, [await session.get(Run, run.id)])
    assert (row["status_code"], row["status_text"]) == ("succeeded", "已完成")


async def test_after_escalation_the_rule_is_one_director_not_zero_of_three():
    app_id, a, b, boss = await _setup()
    run_id = await _start(app_id, "报销审批·钱七")
    task = await _task(run_id)
    await tasks.respond(task.id, user(a), decision="approve")
    assert await tasks.tick(utcnow() + timedelta(hours=2)) >= 1
    detail = await tasks.detail(task.id, user(boss))
    p = detail["progress"]
    # 升级加派的那一份不算进会签人数：还是 1/2，另写升级给了谁、他一人说了算
    assert (p["voters"], p["need"], p["agreed"]) == (2, 2, 1)
    assert p["escalation"] == {"slot": "director", "assignees": [{"kind": "user", "name": boss}], "decided": None,
                               "by": None}
    assert detail["can_respond"] is True
    assert (await tasks.respond(task.id, user(boss), decision="approve"))["resolved"] is True
    await _wait(run_id, ("succeeded",))
    p = (await tasks.detail(task.id, user(boss)))["progress"]
    assert (p["voters"], p["agreed"]) == (2, 1) and (p["escalation"]["decided"], p["escalation"]["by"]) == \
        ("approve", boss)
    signs = (await _page(app_id, run_id, user("qianqi")))["signs"]
    assert signs["progress"]["escalation"]["by"] == boss


async def test_tasks_with_the_same_title_carry_the_instance_and_when():
    app_id, a, _, _ = await _setup()
    first = await _start(app_id, "报销审批·钱七·第一笔")
    second = await _start(app_id, "报销审批·钱七·第二笔", amount=300)
    listed = [t for t in await tasks.list_tasks(user(a), app_id=app_id)]
    assert [t["title"] for t in listed] == ["报销审批：钱七", "报销审批：钱七"]
    assert {t["run_id"]: t["instance_title"] for t in listed} == {first: "报销审批·钱七·第一笔",
                                                                  second: "报销审批·钱七·第二笔"}
    rows = (await _page(app_id, first, user(a)))["mine"]["items"]
    assert {r["instance_title"] for r in rows} == {"报销审批·钱七·第一笔", "报销审批·钱七·第二笔"}
    assert all(r["created_at"] for r in rows)
