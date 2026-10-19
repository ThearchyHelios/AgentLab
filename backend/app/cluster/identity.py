"""这个工作进程是谁：主机 + pid + 启动随机串（OWNER）。

库里认领了的行（运行、问数据的一轮、后台任务、并发名额、同源队）都记着 owner；进程在 cluster_workers 里登记、续租约，
别的进程据此判断「这一行的属主还在不在」。随机串让同一个 pid 被复用（容器里 pid 常是 1）、或者同一个进程里测试一次次起停
服务时，都分得清是哪一次启动认领的。

在导入时定下：建表的缺省值（db 模型的 owner 列）直接取它，不必等服务启动。fork 出来的子进程（uvicorn --workers）各自重新导入
应用、各有各的；万一是 fork 之后没重新导入（multiprocessing 的 fork 方式），pid 对不上时 owner() 重新生成。
"""
from __future__ import annotations

import os
import secrets
import socket

_HOST = (socket.gethostname() or "localhost")[:80]


def _make(pid: int) -> str:
    return f"{_HOST}:{pid}:{secrets.token_hex(4)}"


_PID = os.getpid()
OWNER = _make(_PID)


def owner() -> str:
    """这个进程的 owner id。"""
    global OWNER, _PID
    pid = os.getpid()
    if pid != _PID:
        _PID, OWNER = pid, _make(pid)
    return OWNER


def host() -> str:
    return _HOST


def describe(value: str | None) -> str:
    """写进日志、界面的样子：主机和 pid（随机串不写）。"""
    if not value:
        return "（没有记录）"
    parts = value.split(":")
    return f"{parts[0]} 上的进程 {parts[1]}" if len(parts) >= 2 else value


__all__ = ["OWNER", "describe", "host", "owner"]
