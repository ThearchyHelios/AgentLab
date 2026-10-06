"""定时运行结束后的自动对比：和同一定时任务上一次成功的运行比一次，结果存成工件，工件 id 记在触发记录上
（ScheduleFire.compare_artifact）。

对比本身调运行对比接口的现成函数（api/compare.py 的 compare_runs，不改它），这里只管比哪些、跟谁比、什么时候比、
结果放哪、怎么说给触发历史和通知听：

- 比哪些：成功结束、封存完的运行。失败、取消的运行指标不全（口径卡常常没执行），和上一次比只会得出「移除 N 个」，
  收到通知的人会以为报告删了指标
- 跟谁比：同一定时任务、同一工作流、比这次先发起的运行里成功结束并封存的最近一次。规则在对比接口的 pick_base，
  运行详情「与上一次对比」的默认基准用的是同一个函数：触发历史和运行详情里两个同名的入口必须比同一对运行。失败的那次
  不当基准：拿它当基准，每个指标都成了「新增」。按发起先后挑，不按结束时间：老的失败运行事后被接着跑完，结束得比
  后一次晚，它的上一次仍是它前面那次。紧挨着的上一次不是成功的（失败、取消、还没结束）时 previous 为 False，通知和
  触发历史写明比的是哪一次。这个任务的第一次不比（不像运行详情那样退回同一工作流的别的运行：那不是「上一次」）
- 和封存绑定：运行的封存要盖住它最后一条结束事件才算封存完（sealed_now）。runner 先写终态、再另起会话封存，失败后
  接着跑完的运行在两步之间还带着失败时的清单哈希，拿它去比只比得到旧封存范围里的事件。结果里记下两边的清单哈希：
  哪一边后来变了，触发历史就不再给这份对比（api/schedules._fire_outs）；对比途中变了，结果不记
- 什么时候：通知扫描（webhook.notify_due）挑出这次触发之后、认领之前，在锁外比，比完（或等过时限）再认领、推送。
  比到一半服务停了，这次触发还没认领，下次启动照常比、照常推；同一份封存已经比过的，直接用记下的结果
- 时限：一次对比最多等 WAIT_S 秒，同一时刻最多 SLOTS 个在算（读快照、重算都在线程里，不能一起占满接口请求也要用的
  线程池）。超时、比不了（对比接口拒绝，如基准的封存核对不一致）、出错只记日志，通知照常推送、不带对比
- 结果：整份对比结果连同两边的清单哈希存成工件（kind=run_compare）。不挂在任何运行名下：它不是运行产出的，不该出现
  在运行的工件列表里
- 对外：触发历史（Fire.compare）和通知只给基准是哪一次、各类指标的个数，不给具体数字
"""
from __future__ import annotations

import asyncio
import logging
import threading
import weakref
from typing import Any
from urllib.parse import urlencode

from fastapi import HTTPException
from sqlalchemy import func, select, update

from app.core import artifact_store
from app.core.events import EventType
from app.db.base import SessionLocal
from app.db.models import Run, RunEvent, ScheduleFire

logger = logging.getLogger(__name__)

KIND = "run_compare"
#: 终态，同对比接口（api/evidence._TERMINAL）：触发历史里两边都要在终态，对比页才比得了
TERMINAL = ("succeeded", "failed", "cancelled")
#: 结束事件：封存要盖住这次运行的最后一条，才是这一次结束时封的
_ENDS = (EventType.RUN_FINISHED.value, EventType.RUN_FAILED.value, EventType.RUN_CANCELLED.value)
#: 一次对比最多等多久（秒），排队等空位的时间也算在里面。等不到就不等了：通知不带对比照常推送
WAIT_S = 20.0
#: 同一时刻最多几个对比在算
SLOTS = 2
#: 各类指标的叫法，和对比页一致（frontend/src/lib/terms.ts 的 RUN_COMPARE_TEXT.classLabel）
CLASS_LABEL = {
    "data_only": "只数据变",
    "caliber_and_data": "口径与数据都变",
    "sql_changed": "SQL 结构变",
    "params_only": "只参数变",
    "added": "新增",
    "removed": "移除",
    "incomparable": "无法比较",
    "unchanged": "未变",
}
#: 变化三分法的三类，0 个也写：「SQL 结构变 0 个」本身就是要告诉人的事
CORE = ("data_only", "caliber_and_data", "sql_changed")
#: 其余各类有才写，次序同对比页的摘要
OTHERS = ("params_only", "added", "removed", "incomparable", "unchanged")
#: 两边都没有指标时的说法：不写一串「0 个」，免得读成「什么都没变」
NO_METRICS = "两次运行都没有指标"

#: 读过的对比工件：工件 id → 摘要。工件按内容寻址、内容不会变，记下来不会过期；触发历史、任务列表每次打开都要读，
#: 不必每次都去盘上读一份几十 KB 的 JSON。只记读成功的，满了丢最早的。读盘在线程里、记下新算的在事件循环里，
#: 两边都动这个字典，加一把线程锁
_BRIEFS: dict[str, dict[str, Any]] = {}
_BRIEFS_MAX = 512
_BRIEFS_LOCK = threading.Lock()
#: 对比的空位，按事件循环分开（同 service.launch_lock）：测试每个用例一个新循环，跨循环用同一个信号量会报错
_SLOTS: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore]" = weakref.WeakKeyDictionary()


def summary_text(summary: dict[str, Any]) -> str:
    """计数摘要：「只数据变 3 个、口径与数据都变 1 个、SQL 结构变 0 个」，其余各类有才接在后面。只有个数。"""
    counts = {key: value for key, value in summary.items() if isinstance(value, int) and not isinstance(value, bool)}
    if not any(counts.values()):
        return NO_METRICS
    parts = [f"{CLASS_LABEL[key]} {counts.get(key, 0)} 个" for key in CORE]
    parts += [f"{CLASS_LABEL[key]} {counts[key]} 个" for key in OTHERS if counts.get(key)]
    return "、".join(parts)


def page_url(base_url: str, target: str, base: str) -> str:
    """对比页的地址（对外访问地址 + /runs/compare?target=…&base=…），和界面上运行详情跳过去的是同一个页面。"""
    return f"{base_url}/runs/compare?{urlencode({'target': target, 'base': base})}"


async def sealed_now(session: Any, run: Run) -> bool:
    """这次运行的封存是不是这一次结束时封的：有清单哈希，而且封存范围盖住了最后一条结束事件。

    runner 先在一个事务里写终态和结束事件，再另起会话封存。失败后接着跑完（或接着跑之后停在审批上被放弃）的运行，在
    两步之间还带着失败时的清单哈希，只看「有没有哈希」会把旧的封存当成这一次的。没有 manifest_seq 的是加这一列之前
    封存的老运行，只能看有没有哈希。"""
    if not run.manifest_hash:
        return False
    if run.manifest_seq is None:
        return True
    last = await session.scalar(select(func.max(RunEvent.seq)).where(
        RunEvent.run_id == run.id, RunEvent.type.in_(_ENDS)))
    return last is None or run.manifest_seq >= last


async def compare_fire(fire_id: str, run_id: str) -> dict[str, Any] | None:
    """这次触发发起的运行成功结束、封存完了：和同一定时任务上一次成功的运行比一次，结果记进触发记录。返回给通知用的
    摘要 {base_run_id, summary, previous, base_started_at, …}；不该比、没有可比的、比不了时返回 None。

    最多等 WAIT_S 秒（含排队）。超时、对比接口拒绝（基准的封存核对不一致等）记一条警告，意外的异常记堆栈，都不影响
    通知。取消不吞：扫描被取消（停机）时这次触发还没认领，下次照常比。"""
    try:
        return await asyncio.wait_for(_compare(fire_id, run_id), timeout=WAIT_S)
    except TimeoutError:
        logger.warning("定时任务的运行 %s 自动对比超过 %g 秒仍未完成，通知不带对比照常推送", run_id, WAIT_S)
    except HTTPException as e:
        logger.warning("定时任务的运行 %s 自动对比未完成：%s", run_id, e.detail)
    except Exception:  # noqa: BLE001 - 对比出错不能拖住通知，也不能让通知扫描停下
        logger.exception("定时任务的运行 %s 自动对比出错", run_id)
    return None


async def _compare(fire_id: str, run_id: str) -> dict[str, Any] | None:
    # 挑基准、对比都用对比接口现成的函数，在用到时才导入：api/compare.py 连带证据、推演接口，调度模块被导入时不必
    # 一起加载
    from app.api import compare as compare_api

    # 挑基准、核对封存、看有没有比过都只是读库，不占对比的空位：前面排着慢的对比时，第一次运行这类没什么可比的
    # 不必跟着排队、等到超时
    async with SessionLocal() as session:
        target = await session.get(Run, run_id)
        stored = await session.scalar(select(ScheduleFire.compare_artifact).where(ScheduleFire.id == fire_id))
        if target is None or target.status != "succeeded" or not target.schedule_id or not target.workflow_id \
                or not await sealed_now(session, target):
            return None
        found = await compare_api.pick_base(session, target, schedule_only=True)
        if found is None:
            return None
        base, previous = found.run, found.previous
        if not await sealed_now(session, base):
            # 基准正好处在写完终态、还没封存的那一刻，或者封存没补上就停了机：拿它比只比得到旧封存里的事件
            logger.warning("作为基准的运行 %s 还没有封存到它的结束事件，运行 %s 这次不比", base.id, run_id)
            return None
        sides = ((target.id, target.manifest_hash), (base.id, base.manifest_hash))
    (target_id, target_seal), (base_id, base_seal) = sides
    if stored:
        # 同一份封存已经比过（比完还没认领就停了机）：直接用
        known = await asyncio.to_thread(brief_of, stored)
        if known is not None and _sides_of(known) == sides:
            return _copy(known)
    async with _slots():
        result = await compare_api.compare_runs(target_id, base_id)
    payload = {"target_manifest": target_seal, "base_manifest": base_seal, "previous": previous, "result": result}
    brief = _brief(payload)
    if brief is None or _sides_of(brief) != sides:
        return None
    artifact = await artifact_store.put_json(payload, kind=KIND,
                                             meta={"target_run_id": target_id, "base_run_id": base_id})
    if not await _store(fire_id, artifact, sides):
        logger.warning("运行 %s 或作为基准的运行 %s 在对比途中重新封存（或触发记录已删除），这次的对比结果不记",
                       target_id, base_id)
        return None
    _remember(artifact, brief)
    return _copy(brief)


def _slots() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    slots = _SLOTS.get(loop)
    if slots is None:
        slots = _SLOTS[loop] = asyncio.Semaphore(SLOTS)
    return slots


async def _store(fire_id: str, artifact: str, sides: tuple[tuple[str, str | None], ...]) -> bool:
    """把工件 id 记进触发记录。条件写：两边都还是成功结束、清单哈希还是比的时候那一份，才记。"""
    same = [select(Run.id).where(Run.id == run_id, Run.status == "succeeded", Run.manifest_hash == seal).exists()
            for run_id, seal in sides]
    async with SessionLocal() as session:
        result = await session.execute(update(ScheduleFire).where(ScheduleFire.id == fire_id, *same)
                                       .values(compare_artifact=artifact))
        await session.commit()
        return result.rowcount == 1


def _sides_of(brief: dict[str, Any]) -> tuple[tuple[str, str | None], ...]:
    return (brief["target_run_id"], brief["target_manifest"]), (brief["base_run_id"], brief["base_manifest"])


def _brief(payload: Any) -> dict[str, Any] | None:
    """对比工件里给触发历史、通知的那一点：两边是哪一次、比的时候两边的清单哈希、基准是不是紧挨着的上一次、基准
    什么时候开始的、各类指标的个数（同对比接口的 summary）。形状不对的（这一版之前的工件、被改过的）返回 None。"""
    if not isinstance(payload, dict) or not isinstance(payload.get("result"), dict):
        return None
    result = payload["result"]
    target, base, summary = result.get("target"), result.get("base"), result.get("summary")
    if not isinstance(target, dict) or not isinstance(base, dict) or not isinstance(summary, dict):
        return None
    keys = (target.get("id"), base.get("id"), payload.get("target_manifest"), payload.get("base_manifest"))
    if not all(isinstance(key, str) and key for key in keys):
        return None
    started = base.get("started_at")
    return {"target_run_id": keys[0], "base_run_id": keys[1], "target_manifest": keys[2], "base_manifest": keys[3],
            "previous": payload.get("previous") is not False,
            "base_started_at": started if isinstance(started, str) else None,
            "summary": {key: value for key, value in summary.items()
                        if isinstance(value, int) and not isinstance(value, bool)}}


def public(brief: dict[str, Any]) -> dict[str, Any]:
    """接口给前端的那一份（Fire.compare）：基准是哪一次、各类指标的个数、基准是不是紧挨着的上一次。"""
    return {"base_run_id": brief["base_run_id"], "summary": dict(brief["summary"]), "previous": brief["previous"]}


def _copy(brief: dict[str, Any]) -> dict[str, Any]:
    return {**brief, "summary": dict(brief["summary"])}


def _remember(artifact: str, brief: dict[str, Any]) -> None:
    with _BRIEFS_LOCK:
        if artifact not in _BRIEFS and len(_BRIEFS) >= _BRIEFS_MAX:
            _BRIEFS.pop(next(iter(_BRIEFS)))
        _BRIEFS[artifact] = brief


def brief_of(artifact: str) -> dict[str, Any] | None:
    """读一份对比工件的摘要。文件不在、内容和哈希对不上、读不出来（后两种另记一条警告）、形状不对的返回 None，
    不抛异常：一份对比读不出来不能让整页触发历史打不开。"""
    with _BRIEFS_LOCK:
        known = _BRIEFS.get(artifact)
    if known is not None:
        return _copy(known)
    try:
        brief = _brief(artifact_store.load(artifact))
    except ValueError:
        logger.warning("对比工件 %s… 内容与哈希不符，触发历史中不显示这次对比", artifact[:12])
        return None
    except OSError as e:   # 权限、磁盘出错
        logger.warning("对比工件 %s… 读取失败（%s），触发历史中不显示这次对比", artifact[:12], type(e).__name__)
        return None
    if brief is None:
        return None
    _remember(artifact, brief)
    return _copy(brief)


async def briefs(artifacts: list[str]) -> dict[str, dict[str, Any]]:
    """几份对比工件的摘要：工件 id → 摘要，读不出来的不在里面。读盘在线程里做。"""
    wanted = list(dict.fromkeys(a for a in artifacts if a))
    if not wanted:
        return {}

    def read() -> dict[str, dict[str, Any]]:
        out = {}
        for artifact in wanted:
            brief = brief_of(artifact)
            if brief is not None:
                out[artifact] = brief
        return out

    return await asyncio.to_thread(read)
