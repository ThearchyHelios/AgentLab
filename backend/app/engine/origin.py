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
)

#: 客户端在 POST /api/runs 里能声明的来源
CLIENT_ORIGINS = ("chat", "canvas", "launcher", "api")


def origin_note(kind: str, **data: Any) -> dict[str, Any]:
    """run.origin 事件的数据：kind 加上调用方给的字段（app_id、app_version、spec_hash、entry / view、hook、
    actor、actor_verified、auth_mode……），值为 None 的不写。"""
    if kind not in ORIGIN_KINDS:
        raise ValueError(f"不认识的运行来源：{kind}")
    return {"kind": kind, **{k: v for k, v in data.items() if v is not None}}
