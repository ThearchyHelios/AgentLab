"""运行来源：这次运行从哪里发起（docs/design/app-platform-and-look.md §2.10 X3）。

以前运行表上只有 schedule_id：问数据和画布试跑都是「裸图 + 探索」，分不出来；应用发起的运行更无从说起。
来源由 launch_run(origin=…) 写进 Run.origin_kind / Run.origin，同时作为 run.origin 事件放进 notes——发起时就定下、
在封存范围内，事后改不了。

客户端（POST /api/runs）只能声明 CLIENT_ORIGINS 里的几种；app_*、hook、schedule 只能由服务端写，客户端声明一律拒绝：
否则谁都能把一次画布试跑说成「应用入口发起的」。
"""
from __future__ import annotations

from typing import Any

#: 全部来源。顺序即界面上筛选的顺序
ORIGIN_KINDS = (
    "chat",          # 问数据
    "canvas",        # 画布试跑
    "launcher",      # 正式运行（手动，画布或版本页上的「正式运行」）
    "schedule",      # 定时任务
    "app_entry",     # 应用入口
    "app_view",      # 应用视图（按需运行）
    "app_backfill",  # 钉住项按 recompute 升版后，按历次触发的输入补算
    "hook",          # 外部系统经入站钩子触发
    "api",           # 直接调接口，没说是哪
    "trial",         # 蓝图的试运行（探索运行，docs/design/normal-user-apps.md N4）
    "personal_app",  # 个人应用的刷新（探索运行，N7）
    # 下一轮（docs/design/next-round/00-总方案.md X1）：都只能由服务端写
    "eval",          # 评测：作答和算期望的运行（探索运行）
    "ledger_issue",  # 问数据的答案按已治理口径正式出具（正式运行）
    "reconcile",     # 口径对账：这次的取数和口径在同一份数据上各算一次（探索运行）
    "recall",        # 口径召回：新旧两版口径在同一份数据上各算一次（探索运行）
)

#: 客户端在 POST /api/runs 里能声明的来源
CLIENT_ORIGINS = ("chat", "canvas", "launcher", "api")

#: 系统派生的运行（docs/design/next-round/00-总方案.md X1）：评测、口径对账、口径召回都是服务端为了量、为了比替人跑的，
#: 不是有人在用。它们是完整的运行（证据、封存都在、点得开），但不进使用统计——探索问题聚类、工具白名单衰减、数据目录的
#: 使用次数和「被哪些运行用过」、审阅队列的使用权重、以后的账本统计——否则一跑评测，治理信号和目录的使用热度就失真了。
#: 记录页默认也不列（筛选里能选，列表照实写有几次没列出）。ledger_issue 是替用户正式出具，照常计入
SYSTEM_ORIGINS = ("eval", "reconcile", "recall")


def counted_runs() -> Any:
    """SQL 条件：计入使用统计的运行（排除 SYSTEM_ORIGINS）。所有使用统计都只算它圈进来的运行，别处不另写一份过滤。

    来源按非空比较（coalesce，同 auth/access.hidden_runs）：加来源这一列之前的老运行 origin_kind 为空，NOT IN 碰上 NULL
    会把它们一起筛掉；外连接没对上运行的行（问数据只回了一句、没有运行的那一轮）同理照常计入。"""
    from sqlalchemy import func

    from app.db.models import Run

    return func.coalesce(Run.origin_kind, "").not_in(SYSTEM_ORIGINS)


def origin_note(kind: str, **data: Any) -> dict[str, Any]:
    """run.origin 事件的数据：kind 加上调用方给的字段（app_id、app_version、spec_hash、entry / view、hook、
    actor、actor_verified、auth_mode……），值为 None 的不写。"""
    if kind not in ORIGIN_KINDS:
        raise ValueError(f"不认识的运行来源：{kind}")
    return {"kind": kind, **{k: v for k, v in data.items() if v is not None}}
