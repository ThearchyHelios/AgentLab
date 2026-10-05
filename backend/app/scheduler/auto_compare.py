"""定时运行结束后的自动对比：和同一定时任务的上一次运行比一次，结果存成工件，工件 id 记在触发记录上
（ScheduleFire.compare_artifact）。

对比本身调运行对比接口的现成函数（api/compare.py 的 default_base、compare_runs），这里只管什么时候比、跟谁比、结果
放哪、怎么说给触发历史和通知听：

- 什么时候：通知扫描（webhook.notify_due）认领到运行进入终态（结束、失败、取消）的那一次之后、推送之前。和推送一样，
  同一个终态只处理一次；失败后被继续执行、又结束的，按新的封存重新比，覆盖旧的结果
- 跟谁比：default_base 的「同一定时任务」那一档，即这个任务里更早结束、封存过的终态运行中最近的一次。它退回「同一
  工作流、同一运行类别」时不比：那不是这个定时任务的上一次，别人手动改的输入、另一个任务的参数都会被算成变化
- 结果：整份对比结果存成工件（kind=run_compare）。不挂在任何运行名下：它不是运行产出的，不该出现在运行的工件列表里。
  没封存的不比（服务重启时还在排队、被改成失败的，永远不会封存）；比不了、出错只记日志，通知照常推送
- 对外：触发历史（Fire.compare）和通知只给基准是哪一次、各类指标的个数，不给具体数字
"""
from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any
from urllib.parse import urlencode

from fastapi import HTTPException
from sqlalchemy import update

from app.core import artifact_store
from app.db.base import SessionLocal
from app.db.models import Run, ScheduleFire

logger = logging.getLogger(__name__)

KIND = "run_compare"
#: 终态，同对比接口（api/evidence._TERMINAL）：只有这几种状态、封存过的运行能参与对比
TERMINAL = ("succeeded", "failed", "cancelled")
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


async def compare_fire(fire_id: str, run_id: str) -> dict[str, Any] | None:
    """这次触发发起的运行刚进入终态：和同一定时任务的上一次比一次，结果记进触发记录。返回给通知用的摘要
    {base_run_id, summary}；没有可比的、比不了时返回 None，触发记录上之前的结果一并清掉（那是上一次封存的）。

    不抛异常：比不了（对比接口拒绝，如基准的封存核对不一致）记一条警告，意外的异常记堆栈，都不影响通知。"""
    artifact: str | None = None
    brief: dict[str, Any] | None = None
    try:
        artifact, brief = await _compare(run_id)
    except HTTPException as e:
        logger.warning("定时任务的运行 %s 自动对比未完成：%s", run_id, e.detail)
    except Exception:  # noqa: BLE001 - 对比出错不能拖住通知，也不能让通知扫描停下
        logger.exception("定时任务的运行 %s 自动对比出错", run_id)
    await _store(fire_id, artifact)
    return brief


async def _compare(run_id: str) -> tuple[str | None, dict[str, Any] | None]:
    # 现成的对比函数在用到时才导入：api/compare.py 连带证据、推演接口，调度模块被导入时不必一起加载
    from app.api import compare as compare_api

    async with SessionLocal() as session:
        run = await session.get(Run, run_id)
    if run is None or run.status not in TERMINAL or not run.manifest_hash:
        return None, None
    found = await compare_api.default_base(run_id)
    if found is None or found[1] != "same_schedule":
        return None, None
    base = found[0]
    result = await compare_api.compare_runs(run_id, base.id)
    brief = _brief(result)
    if brief is None:
        return None, None
    artifact = await artifact_store.put_json(result, kind=KIND, meta={"target_run_id": run_id, "base_run_id": base.id})
    _remember(artifact, brief)
    return artifact, _copy(brief)


async def _store(fire_id: str, artifact: str | None) -> None:
    try:
        async with SessionLocal() as session:
            await session.execute(update(ScheduleFire).where(ScheduleFire.id == fire_id)
                                  .values(compare_artifact=artifact))
            await session.commit()
    except Exception:  # noqa: BLE001 - 任务刚被删掉之类：记不下不影响通知
        logger.warning("触发记录 %s 的自动对比结果没能保存", fire_id)


def _brief(result: Any) -> dict[str, Any] | None:
    """对比结果里给触发历史、通知的那一点：基准是哪一次、各类指标的个数（同对比接口的 summary）。"""
    if not isinstance(result, dict):
        return None
    base, summary = result.get("base"), result.get("summary")
    if not isinstance(base, dict) or not isinstance(base.get("id"), str) or not isinstance(summary, dict):
        return None
    return {"base_run_id": base["id"],
            "summary": {key: value for key, value in summary.items()
                        if isinstance(value, int) and not isinstance(value, bool)}}


def _copy(brief: dict[str, Any]) -> dict[str, Any]:
    return {"base_run_id": brief["base_run_id"], "summary": dict(brief["summary"])}


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
