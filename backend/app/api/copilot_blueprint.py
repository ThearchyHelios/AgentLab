"""蓝图助手和填关节的流式接口（docs/design/normal-user-apps.md §2.2、§2.3、§2.11）。

- POST /api/copilot/blueprint-stream：蓝图对话。模型输出 bp_* 操作（BlueprintApplier 落地）、look、bp_ask（这一轮就此结束）；
- POST /api/copilot/fill-joints-stream：按模板填生成关节（sql、metric_expr）。TemplateApplier 只收 fill_joint、只收模板声明过的
  关节；不同 group 的关节并行填。

都复用 api/copilot_gen.Generation。权限按蓝图判（blueprints/store.ensure_access）：共享蓝图要搭建者，且是拥有者、它发布出来
的应用的管理者或管理员；个人蓝图只给拥有者，且管理员开了个人应用。

**调用次数的预算**（N11）：起草蓝图 1 次主调用加最多 2 次查看；填关节每组 1 次调用、各自最多 1 次查看（系统先代为查看
这一组要用的表，不占次数），不同的组并行。并行调用出错（多半是模型服务限流）的组，等别的组做完再按顺序试一次。

**留痕**（P2）：蓝图对话每轮落一条对话记录（kind=blueprint 的 Conversation，问题和回答都进历史；助手反问的那一轮回答
记成反问，meta.asked 同问数据和画布的写法），蓝图的每个 bp_* 操作、每个填上的关节各写一条 blueprint_events。
"""
from __future__ import annotations

import asyncio
import copy
import json
import logging
import time
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import get_session

router = APIRouter(prefix="/api/copilot", tags=["copilot-blueprint"])
logger = logging.getLogger(__name__)

#: 起草蓝图最多查看几次；填关节每组最多查看几次（N11）
BLUEPRINT_LOOK_LIMIT = 2
FILL_LOOK_LIMIT = 1

# ==========================================================================
# 写给模型的文字
# ==========================================================================

BLUEPRINT_SYSTEM_PROMPT = """\
你是应用蓝图助手。用户说想要一个应用，你把他的话整理成蓝图：几张白话卡片，每张是用户能判断对错的一件事。蓝图之后按场景模板
确定性地生成工作流和应用，你不写工作流、不写 SQL。

卡片（路径是 JSON 指针，值照下面的写法）：
- /purpose 目的：一句话，这个应用解决什么问题、给谁用
- 场景：用 bp_template 选模板（见下面的模板清单），选了之后模板会预置几项默认值
- /data 看哪些数，每一项：{"id":"gmv","label":"销售额","unit":"元","table":{"source":"库名","table":"表名"},
  "measure":"怎么算（白话或简单聚合）","date":{"column":"业务日期的列","rule":"按支付日"},"filter":"筛选条件","grain":"粒度"}
  对到已发布的口径卡时写 "reuse"（照抄 look 给的复用写法），不写 table。table 的 source 只能是上面概览里列出的数据源名，
  不要自己起名；填报登记汇总这个应用自己收上来的记录时，写 {"source":"@records","table":"records"}（记录表建好后系统换成真名）
- /cadence 多久更新：{"preset":"daily|weekly|monthly","weekday":1,"day":1,"time":"09:00","tz":"Asia/Shanghai",
  "period":"yesterday|last_iso_week|last_month|today|this_iso_week|this_month"}（weekly 写 weekday，1 是周一；monthly 写 day）
- /people 谁来做，每一步：{"id":"finance","label":"财务会签","who":[{"kind":"user|group|slot|initiator","name":"…"}],
  "policy":{"kind":"any|all|quorum","m":2},"due":"48 小时","on_timeout":{"action":"remind|escalate|reject","to":[…]},
  "threshold":{"field":"amount","op":">","value":5000}}；threshold 只许「字段 比较 常量」，字段要在「填什么」里
- /fields 填什么，每个字段：{"name":"amount","spec":{"type":"number","title":"金额（元）","minimum":0.01,"required":true},
  "sensitive":false}；type 只能是 string、number、integer、boolean、array，x-widget 可写 textarea、select、date 这类控件
- /writeback 写回哪里：{"capability":"能力登记里的写入能力","mapping":{"参数":"form.字段 | metric.指标 id | rows | '常量'"},
  "confirm":true}
- 谁能看（/access）由用户自己在卡片上定，你不填

规则：
- 只提议：你写的每一项都是「待确认」，用户确认了才算数；用户已经确认过的项不要改，除非他这一轮明确要改
- 只改要改的：已有的项用 bp_set 改（写到项上，例如 /data/0/table），列表用 bp_add 加、bp_remove 删
- 对到数据：先 look 数据目录里的表（kind table），对到表和业务日期的列，用 bp_set …/why 写一句为什么对到这张表；
  有已发布的口径能直接用的（kind caliber），优先复用，不重写怎么算
- 缺了只有用户知道的事（按下单日还是支付日、金额线多少、谁来审批），用 bp_ask 问：一次只问一件事、给出候选，这一轮就此结束
- 不要编表名、列名、人名、能力名；看不到的就问"""

BLUEPRINT_PROTOCOL_PROMPT = """\
输出格式（严格遵守）：
- 每行一个完整的 JSON 对象，除此之外不输出任何东西——不要 markdown 围栏、不要解释文字
- 可用操作：
  {"op":"plan","summary":"一句话说明打算怎么整理"}          ← 第一行
  {"op":"bp_template","template":{"id":"periodic_board","version":1}}
  {"op":"bp_set","path":"/purpose","value":"…"}
  {"op":"bp_add","path":"/data","value":{"id":"gmv","label":"销售额"}}
  {"op":"bp_remove","path":"/data/1"}
  {"op":"bp_set","path":"/data/0/table/why","value":"一句话说为什么对到这张表"}
  {"op":"bp_ask","question":"销售额按下单日还是支付日算？","options":["按下单日","按支付日"],"path":"/data/0/date"}  ← 问了就停
  {"op":"done","explanation":"两三句话说明整理了什么，还缺什么"}    ← 最后一行
- 这句话不需要改蓝图（只是问个问题）时，第一行也是最后一行输出 {"op":"reply","text":"直接回答的内容"}
- 要看表、口径、能力时输出一行 look（用法见下），输出之后就停下等查看结果，拿到结果再接着写"""

BLUEPRINT_LOOK_WHEN_PROMPT = """\
什么时候看：对「看哪些数」先看数据目录里的表（kind table）；用户说的指标可能已经有人做过时看口径（kind caliber）；
定「写回哪里」时看能力（kind capability），只能选写入能力。只改目的、谁来做、填什么的不看"""

TEMPLATES_HEAD_PROMPT = "场景模板（bp_template 选一个；卡片照约束写）："
PERSONAL_PROMPT = """\
这是使用者给自己做的个人应用：只读、只有自己看得见、自己点刷新。不能有审批步骤（不写「谁来做」），不能写回别的系统（不写
「写回哪里」），只能选下面列出的场景、用模板里「个人应用能用」的那一部分。想给别人用，告诉用户做好之后提交给搭建者审核"""
TEMPLATE_PERSONAL_PROMPT = "\n  个人应用能用：{personal}"
TEMPLATE_LINE_PROMPT = "- {id} v{version} {label}：适用于{applies}\n  卡片：{cards}"
BLUEPRINT_REQUEST_PROMPT = "这是蓝图现在的样子：\n{spec}\n\n{issues}用户说：{instruction}"
BLUEPRINT_ISSUES_PROMPT = "试运行前还要补齐的：\n{lines}\n\n"
ANSWERED_PROMPT = "（这是在回答你上一轮的反问：{question}）"

FILL_SYSTEM_PROMPT = """\
你在给场景模板「{template}」填关节。模板的骨架（节点、连线、别的配置）已经按用户确认的蓝图定好、锁住了，你只能填下面列出的
这几个位置：取数的 SQL（只读的一条 SELECT）、口径卡的指标式（只许 cell() 取格加算术）。别的位置改不了，改了也不会被采纳。

输出格式（严格遵守）：
- 每行一个完整的 JSON 对象，除此之外不输出任何东西
- 可用操作：
  {{"op":"plan","summary":"一句话说明打算怎么写"}}          ← 第一行
  {{"op":"fill_joint","joint":"关节 id","value":"SQL 或指标式"}}   ← 每个关节一行，整段写完
  {{"op":"done","explanation":"一两句话说明写了什么、有什么拿不准的"}}    ← 最后一行
- 要看表结构时输出一行 look（用法见下），输出之后就停下等查看结果；要用的表系统已经代为查看过一次，下面就有
- 拿不准的按最合理的写，在 done 的说明里写清楚；不要问用户"""
FILL_REQUEST_PROMPT = "这一组要填的关节：\n{joints}\n\n说明：\n{brief}\n\n模板的骨架（只读）：\n{graph}"
FILL_JOINT_LINE_PROMPT = "- {id}：{label}（{kind}）"
FILL_SYSTEM_LOOK_NEED = "这一组关节要用的表"
FILL_KIND_PROMPT = {"sql": "SQL", "metric_expr": "指标式"}

#: 交付时写进关节状态的说明（界面上照原话显示）
FILL_FAILED_NOTE = "助手填的没通过检查：{reason}。请补充说明或换个说法再试"
FILL_ERROR_NOTE = "助手这一组没填完：{reason}。可以重试"
FILL_STALE_NOTE = "填的时候卡片又改过了，请重新填"


class BlueprintStreamIn(BaseModel):
    blueprint_id: str
    instruction: str = Field(min_length=1, description="用自然语言说想要什么、要怎么改，或者回答助手的反问")
    provider: str | None = None
    model: str | None = None


class FillJointsIn(BaseModel):
    blueprint_id: str
    #: 只填这几组（不给就填还没填上的那几组）
    groups: list[str] | None = None
    provider: str | None = None
    model: str | None = None


def _sse(obj: dict[str, Any]) -> str:
    return f"data: {json.dumps(obj, ensure_ascii=False, default=str)}\n\n"


def _streaming(gen: Any) -> Any:
    from fastapi.responses import StreamingResponse

    return StreamingResponse(gen, media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


async def _model(session: AsyncSession, provider: str | None, model: str | None) -> tuple[Any, str, Any]:
    """按操作流写的模型（同改图助手的额度和退回），和挑选模型的规格。没配接入 400。"""
    from app.api import copilot

    request = copilot.GenerateIn(instruction="-", provider=provider, model=model)
    try:
        op_model, model_id = await copilot._op_model(session, request)
    except copilot.ProviderNotConfigured as e:
        raise HTTPException(400, copilot._unconfigured(e)) from e
    spec = await copilot.copilot_model_spec(session, request, max_tokens=copilot.copilot_context.PICK_MAX_TOKENS)
    return op_model, model_id, spec


def _templates_text(*, personal: bool = False) -> str:
    """模板清单（给助手选场景）。个人蓝图只列能做成个人应用的那几个，并写明能用哪一部分。"""
    from app.blueprints.templates import list_templates

    lines = [PERSONAL_PROMPT, TEMPLATES_HEAD_PROMPT] if personal else [TEMPLATES_HEAD_PROMPT]
    for t in list_templates():
        if personal and t.personal is None:
            continue
        lines.append(TEMPLATE_LINE_PROMPT.format(id=t.id, version=t.version, label=t.label, applies=t.applies_when,
                                                 cards="；".join(f"{k} {v}" for k, v in t.cards.items()))
                     + (TEMPLATE_PERSONAL_PROMPT.format(personal=t.personal) if personal else ""))
    return "\n".join(lines)


def _actor(x_actor: str | None) -> tuple[str | None, bool]:
    from app.api.deps import actor_of, actor_verified

    return actor_of(x_actor), actor_verified()


# ==========================================================================
# 蓝图对话
# ==========================================================================


async def _conversation(session: AsyncSession, blueprint: Any, actor: str | None, title: str) -> Any:
    """这张蓝图的对话（kind=blueprint）：没有就建一段挂上去。对话按人隔离的规则照旧（api/conversations）。"""
    from app.api.deps import ensure_conversation_access
    from app.db.models import Conversation

    if blueprint.conversation_id:
        row = await ensure_conversation_access(session, blueprint.conversation_id)
        if row is not None:
            return row
    from app.api.conversations import title_from

    row = Conversation(kind="blueprint", title=blueprint.name or title_from(title), created_by=actor)
    session.add(row)
    await session.flush()
    blueprint.conversation_id = row.id
    await session.commit()
    return row


async def _open_turn(session: AsyncSession, conversation: Any, question: str) -> str:
    from sqlalchemy import func, select

    from app.db.base import utcnow
    from app.db.models import ConversationTurn

    seq = int((await session.execute(select(func.coalesce(func.max(ConversationTurn.seq), -1))
                                     .where(ConversationTurn.conversation_id == conversation.id))).scalar_one()) + 1
    turn = ConversationTurn(conversation_id=conversation.id, seq=seq, question=question, status="running")
    session.add(turn)
    conversation.last_active_at = utcnow()
    await session.commit()
    return turn.id


async def _close_turn(turn_id: str, *, status: str, answer: str = "", error: str = "",
                      meta: dict[str, Any] | None = None) -> None:
    """回填这一轮（另开短会话：流里请求的会话已经不能用了）。"""
    from app.db.base import SessionLocal
    from app.db.models import ConversationTurn

    async with SessionLocal() as session:
        turn = await session.get(ConversationTurn, turn_id)
        if turn is None:
            return
        turn.status, turn.answer, turn.error = status, answer, error
        if meta is not None:
            turn.meta = meta
        await session.commit()


def _open_ask(spec: dict[str, Any]) -> tuple[int, dict[str, Any]] | None:
    """上一轮的反问还没回答：这一轮用户的话就是在回答它。只看最后一个（更早的反问后面已经有过一轮了）。"""
    asks = spec.get("asks") or []
    if asks and not asks[-1].get("answer"):
        return len(asks) - 1, asks[-1]
    return None


def _as_bp_ask(op: dict[str, Any]) -> dict[str, Any]:
    """模型写成了通用的 ask（画布、问数据的写法）也认：换成 bp_ask 记进蓝图的反问。"""
    options = op.get("options") if isinstance(op.get("options"), list) else []
    return {"op": "bp_ask", "question": str(op.get("question") or ""),
            "options": [str(o) for o in options if isinstance(o, (str, int, float))][:6],
            **({"path": op["path"]} if isinstance(op.get("path"), str) else {})}


@router.post("/blueprint-stream")
async def blueprint_stream(payload: BlueprintStreamIn, session: AsyncSession = Depends(get_session),
                           x_actor: str | None = Header(default=None)) -> Any:
    """蓝图对话：SSE 推送，形状同 /generate-stream。

    事件：model；模型的 plan、bp_* 操作（已经落到服务端维护的那一份蓝图上，来源记 assistant）、look、thinking、heartbeat；
    bp_ask（反问，这一轮就此结束）；自查的 check；最后 final {blueprint, explanation, issues, asked?}——blueprint 是存好的
    蓝图（typesBlueprint.Blueprint），issues 是这一轮没采纳的操作（越界、和卡片冲突）。只回答了一句的发 reply 收尾。
    """
    from app.api import copilot
    from app.blueprints import store
    from app.blueprints.generation import BlueprintApplier, BlueprintChecker
    from app.blueprints.ops import blueprint_issues
    from app.look.calibers import CaliberKind
    from app.look.capabilities import CapabilityKind
    from app.look.pick import Picker
    from app.look.service import LookService
    from app.look.tables import TableKind

    row = await store.load(session, payload.blueprint_id)
    actor, verified = _actor(x_actor)
    op_model, model_id, pick_spec = await _model(session, payload.provider, payload.model)
    conversation = await _conversation(session, row, actor, payload.instruction)
    history = await copilot._history_section(session, conversation.id)
    # 用户这一轮的话是在回答上一轮的反问：先记下回答（来源 user），助手看到的就是答过的那一份
    instruction = payload.instruction.strip()
    if (open_ask := _open_ask(row.spec or {})) is not None:
        await store.apply_ops_and_save(session, row, [{"op": "bp_set", "path": f"/asks/{open_ask[0]}/answer",
                                                       "value": instruction[:500]}], actor, verified, "user")
        instruction = f"{instruction}\n{ANSWERED_PROMPT.format(question=open_ask[1]['question'])}"
    turn_id = await _open_turn(session, conversation, payload.instruction)
    blueprint_id = row.id
    spec = copy.deepcopy(row.spec or {})
    picker = Picker(pick_spec)
    personal = row.scope == "personal"
    calibers = await CaliberKind.load(session, picker=picker)
    # 个人蓝图不写回别的系统：能力只列只读的（同使用者的问数据，P4）
    looks = LookService([await TableKind.load(await copilot._sources(session, None), picker=picker), calibers,
                         await CapabilityKind.load(picker=picker, read_only=personal)])
    system = "\n\n".join(part for part in (
        BLUEPRINT_SYSTEM_PROMPT, _templates_text(personal=personal), await looks.overview(), BLUEPRINT_PROTOCOL_PROMPT,
        looks.guide(), BLUEPRINT_LOOK_WHEN_PROMPT) if part)
    missing = [i for i in blueprint_issues(spec, for_="trial") if i["level"] == "error"]
    human = copilot._with_history(BLUEPRINT_REQUEST_PROMPT.format(
        spec=json.dumps(spec, ensure_ascii=False), instruction=instruction,
        issues=BLUEPRINT_ISSUES_PROMPT.format(lines="\n".join(f"- {i['path']} {i['message']}" for i in missing))
        if missing else ""), history)
    applier = BlueprintApplier(spec, calibers=calibers)
    checker = BlueprintChecker(applier, template_id=row.template_id)
    started = time.monotonic()
    gen = copilot.Generation(op_model, system=system, applier=applier, checker=checker, looks=looks, started=started,
                             look_limit=BLUEPRINT_LOOK_LIMIT)

    async def event_stream():
        from app.db.base import SessionLocal
        from app.db.models_blueprints import Blueprint

        yield _sse({"op": "model", "model": model_id})
        try:
            async for op in copilot._with_heartbeat(gen.draft(human), started=started):
                yield _sse(op)
            if applier.replied is not None and not applier.effective and gen.asked is None:
                yield _sse(applier.replied)
                await _close_turn(turn_id, status="done", answer=str(applier.replied.get("text") or ""),
                                  meta={"looks": looks.records})
                return
            if gen.asked is None and gen.done is None and not applier.effective:
                raise copilot.CopilotIncomplete("模型没有给出任何修改，也没有说明原因",
                                                hint="蓝图未修改。可以重试，或把需求描述得更具体")
            async for op in copilot._with_heartbeat(gen.repair(), phase="repairing", started=started):
                yield _sse(op)
            remaining = await checker.issues() if gen.asked is None else []
            yield _sse({"op": "check", "status": "failed" if remaining else "passed", "repaired": gen.repaired,
                        **({"issues": remaining} if remaining else {})})
            asked = _as_bp_ask(gen.asked) if gen.asked is not None else None
            async with SessionLocal() as own:
                target = await own.get(Blueprint, blueprint_id)
                saved = await store.apply_ops_and_save(own, target, [*applier.effective, *([asked] if asked else [])],
                                                       actor, verified, "assistant", partial=True)
                out = await store.blueprint_view(own, target)
            conflicts = [{"code": r["code"], "level": "warning", "path": r["op"].get("path"),
                          "message": f"助手提的这一处没有采纳：{r['message']}"} for r in saved.refused]
            answer = applier.explanation or (asked["question"] if asked else "")
            await _close_turn(turn_id, status="done", answer=answer, meta={
                "looks": looks.records, "calls": gen.calls,
                **({"asked": {"question": asked["question"], "options": asked["options"]}} if asked else {})})
            yield _sse({"op": "final", "blueprint": out, "explanation": applier.explanation,
                        "issues": [*applier.refusals(), *conflicts],
                        **({"asked": {"question": asked["question"], "options": asked["options"]}} if asked else {})})
        except copilot.CopilotIncomplete as e:
            await _close_turn(turn_id, status="error", error=str(e))
            yield _sse({"op": "error", "message": f"助手本轮未完成：{e}", "hint": e.hint, "detail": e.detail})
        except Exception as e:  # noqa: BLE001
            reason, hint = copilot.explain_error(e)
            logger.exception("蓝图助手这一轮没完成")
            await _close_turn(turn_id, status="error", error=reason)
            yield _sse({"op": "error", "message": f"助手本轮未完成：{reason}", "hint": hint,
                        "detail": copilot.raw_error(e)})

    return _streaming(event_stream())


# ==========================================================================
# 填关节
# ==========================================================================


def _slim(graph: dict[str, Any]) -> dict[str, Any]:
    """给模型看的骨架：节点的 id、类型、标题、配置和连线，不带坐标。"""
    return {"nodes": [{"id": n["id"], "type": n["type"], "label": (n.get("data") or {}).get("label"),
                       "config": (n.get("data") or {}).get("config")} for n in graph.get("nodes") or []],
            "edges": [{k: e.get(k) for k in ("source", "target", "sourceHandle") if e.get(k)}
                      for e in graph.get("edges") or []]}


def _group_tables(spec: dict[str, Any], joints: list[Any], graph: dict[str, Any]) -> list[dict[str, str]]:
    """这一组要用的表（系统代为查看）：组里取数节点查的库 × 「看哪些数」里对到这个库的表。"""
    from app.blueprints.instantiate import node_config

    sources = {str((node_config(graph, j.node_id) or {}).get("tool") or "").removeprefix("db_query__")
               for j in joints if j.kind == "sql"}
    out: list[dict[str, str]] = []
    for item in spec.get("data") or []:
        table = (item.get("table") or {}).get("value")
        if isinstance(table, dict) and table.get("source") in sources:
            target = {"kind": "table", "source": table["source"], "name": table["table"]}
            if target not in out:
                out.append(target)
    for source in sources:
        if not any(t["source"] == source for t in out):
            out.append({"kind": "table", "source": source})
    return out


@router.post("/fill-joints-stream")
async def fill_joints_stream(payload: FillJointsIn, session: AsyncSession = Depends(get_session),
                             x_actor: str | None = Header(default=None)) -> Any:
    """按模板填生成关节：SSE 推送。不同 group 的关节并行填，每组各自一个 Generation，事件都带 group。

    事件：model；每组的 group {status: started}、系统代为查看的 look（by=system）、plan、fill_joint、look、check、
    group {status: filled | failed, issues?, message?}；最后 final {blueprint, groups, problems}——blueprint 是存好的蓝图，
    图和关节状态都更新了；失败的组照实报，留占位（标「待填」），不挡别的组。"""
    from app.api import copilot
    from app.blueprints import store
    from app.blueprints.generation import JOINT_REFUSED, TemplateApplier, TemplateChecker
    from app.blueprints.instantiate import filled_from, generated_groups, instantiate
    from app.blueprints.templates import get_template
    from app.data.sqlcheck import load_checkers
    from app.look.pick import Picker
    from app.look.service import LookService
    from app.look.tables import TableKind

    row = await store.load(session, payload.blueprint_id)
    if not ((row.spec or {}).get("template") or None):
        raise HTTPException(409, "这张蓝图还没选场景，没有要填的关节：先说说要做什么，或者从场景模板开始")
    actor, verified = _actor(x_actor)
    op_model, model_id, pick_spec = await _model(session, payload.provider, payload.model)
    spec = copy.deepcopy(row.spec)
    # 填报登记要按记录表的结构写 SQL：起草这边的那一版还没建的先建（和试运行同一个入口；草稿不影响线上那一版）
    from app.api.coded import CodedHTTPException
    from app.records.store import RecordsRefused

    try:
        ctx = await store.instantiate_context(session, row, create_records=True)
    except RecordsRefused as e:
        await session.rollback()
        raise CodedHTTPException(422, e.message, e.code) from e
    await session.commit()
    keep = filled_from(spec, row.joints or {}, graph=ctx.get("graph"), records=ctx.get("records"))
    inst = instantiate(spec, filled=keep, **ctx)
    groups = generated_groups(inst)
    wanted = [g for g in (payload.groups or []) if g in groups] if payload.groups else \
        [g for g, joints in groups.items() if any(inst.joints[j.id]["status"] != "filled" for j in joints)]
    if payload.groups and not wanted:
        raise HTTPException(400, f"这张蓝图没有这些关节组：{'、'.join(payload.groups)}（有的是：{'、'.join(groups) or '没有'}）")
    sources = await copilot._sources(session, None)
    checkers = await load_checkers(session, sources)
    tables = await TableKind.load(sources, picker=Picker(pick_spec))
    template = get_template(str(spec["template"]["id"]))
    system = FILL_SYSTEM_PROMPT.format(template=template.label if template else spec["template"]["id"]) \
        + await copilot.copilot_context.section(LookService([tables])) + "\n\n" + LookService([tables]).guide()
    blueprint_id = row.id
    started = time.monotonic()

    queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
    results: dict[str, dict[str, Any]] = {}

    async def fill(group: str) -> None:
        joints = groups[group]
        applier = TemplateApplier(copy.deepcopy(inst.graph), inst.joint_defs, joints)
        checker = TemplateChecker(applier, skeleton=inst.skeleton, sources=sources, checkers=checkers)
        looks = LookService([tables])
        gen = copilot.Generation(op_model, system=system, applier=applier, checker=checker, looks=looks,
                                 started=started, look_limit=FILL_LOOK_LIMIT)
        await queue.put({"op": "group", "group": group, "status": "started", "joints": [j.id for j in joints]})
        try:
            request = FILL_REQUEST_PROMPT.format(
                joints="\n".join(FILL_JOINT_LINE_PROMPT.format(id=j.id, label=j.label, kind=FILL_KIND_PROMPT[j.kind])
                                 for j in joints),
                brief=inst.briefs.get(group, ""), graph=json.dumps(_slim(inst.skeleton), ensure_ascii=False))
            targets = _group_tables(spec, joints, inst.graph)
            if targets:
                looked = await looks.look({"op": "look", "need": FILL_SYSTEM_LOOK_NEED, "targets": targets},
                                          by="system")
                if looked.record is not None:
                    await queue.put({**looked.record, "group": group})
                request = f"{request}\n\n{looked.text}"
            async for op in gen.draft(request):
                await queue.put({**op, "group": group})
            async for op in gen.repair():
                await queue.put({**op, "group": group})
            # 越界被拒的不算这一组没填好：没采纳的那一处骨架原样留着，照实列给人看（refusals）
            remaining = [i for i in await checker.issues() if i["code"] != JOINT_REFUSED]
            results[group] = {"values": dict(applier.values), "issues": remaining, "error": None,
                              "refusals": applier.refusals(), "calls": gen.calls}
            await queue.put({"op": "group", "group": group, "status": "failed" if remaining else "filled",
                             **({"issues": remaining} if remaining else {})})
        except Exception as e:  # noqa: BLE001 - 一组失败不挡别的组，照实报
            reason, _ = copilot.explain_error(e)
            logger.exception("填关节的一组没完成：%s", group)
            results[group] = {"values": {}, "issues": [], "error": reason, "refusals": [], "calls": gen.calls}
            await queue.put({"op": "group", "group": group, "status": "failed", "message": reason})

    async def run_all() -> None:
        try:
            await asyncio.gather(*(fill(g) for g in wanted))
            # 并行调用出错的组（多半是模型服务限流）退回串行：等别的组做完，再按顺序各试一次
            for g in [g for g in wanted if results.get(g, {}).get("error")]:
                await queue.put({"op": "group", "group": g, "status": "retrying"})
                await fill(g)
        finally:
            await queue.put(None)

    async def drained():
        while (item := await queue.get()) is not None:
            yield item

    async def event_stream():
        from app.core.artifact_store import graph_hash
        from app.db.base import SessionLocal
        from app.db.models_blueprints import Blueprint
        from app.plain import with_plain

        yield _sse({"op": "model", "model": model_id})
        task = asyncio.create_task(run_all())
        try:
            async for item in copilot._with_heartbeat(drained(), phase="building", started=started):
                yield _sse(item)
            await task
            filled = dict(keep)
            notes: dict[str, str] = {}
            for g in wanted:
                result = results.get(g) or {"values": {}, "issues": [], "error": "没有结果"}
                ids = [j.id for j in groups[g]]
                if result["error"] is None and not result["issues"]:
                    filled.update({jid: result["values"][jid] for jid in ids if jid in result["values"]})
                    continue
                why = (FILL_ERROR_NOTE.format(reason=result["error"]) if result["error"] else
                       FILL_FAILED_NOTE.format(reason=result["issues"][0].get("message") or ""))
                notes.update({jid: why for jid in ids if jid not in keep})
            async with SessionLocal() as own:
                target = await own.get(Blueprint, blueprint_id)
                ctx_now = await store.instantiate_context(own, target)
                done = instantiate(target.spec or {}, filled=filled, **ctx_now)
                for g in wanted:
                    if done.basis.get(g) != inst.basis.get(g):
                        # 填的时候卡片又改过：这一组按旧的说明填的，不算数
                        filled = {k: v for k, v in filled.items() if k not in {j.id for j in groups[g]}}
                        notes.update({j.id: FILL_STALE_NOTE for j in groups[g]})
                        done = instantiate(target.spec or {}, filled=filled, **ctx_now)
                joints = {jid: ({**state, "note": notes[jid]} if jid in notes and state["status"] != "filled" else state)
                          for jid, state in done.joints.items()}
                for g in wanted:
                    for j in groups[g]:
                        state = joints.get(j.id) or {}
                        await store.record_event(own, target, op="fill_joint", actor=actor, verified=verified, data={
                            "joint": j.id, "group": g, "by": "assistant", "status": state.get("status"),
                            **({"note": state["note"]} if state.get("note") else {})})
                target.graph = done.graph
                target.graph_hash = graph_hash(done.graph)
                target.joints = joints
                target.updated_by = actor
                await own.commit()
                out = await store.blueprint_view(own, target)
            yield _sse({"op": "final", "blueprint": out,
                        "groups": {g: {"status": "filled" if not (results.get(g) or {}).get("error")
                                       and not (results.get(g) or {}).get("issues") else "failed",
                                       "calls": (results.get(g) or {}).get("calls", 0),
                                       "refusals": (results.get(g) or {}).get("refusals", [])} for g in wanted},
                        "problems": [with_plain(p) for p in done.problems]})
        except Exception as e:  # noqa: BLE001
            reason, hint = copilot.explain_error(e)
            logger.exception("填关节没完成")
            yield _sse({"op": "error", "message": f"助手本轮未完成：{reason}", "hint": hint,
                        "detail": copilot.raw_error(e)})
        finally:
            if not task.done():
                task.cancel()

    return _streaming(event_stream())
