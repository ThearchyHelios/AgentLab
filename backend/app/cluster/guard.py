"""SQLite 只支持单进程：服务启动时拿数据目录上的文件锁（data/.agentlab.lock，flock 独占、不等），拿不到就拒绝启动。

**为什么拒绝而不是凑合。** SQLite 应用库下，运行的序号、并发名额、同源队、调度当班……这些在库里协调的东西照样能用，但
上传表格的版本库、填报记录表、断点库都是数据目录里的 SQLite 文件，两个进程一起写同一个文件靠的是文件锁，换不成库里的锁；
uvicorn --workers 2 起来的第二个进程以前也只是悄悄地不写上传版本（503），别的照跑、各算各的，最难查。现在照实说：第二个进程
起不来，报错写明「SQLite 只支持单进程；要多进程把应用库换成 PostgreSQL」。

进程退出（含强杀）时操作系统放掉文件锁，重启（dev.sh 的 --reload 先停旧的再起新的）不受影响。同一个进程里重复取（测试里
一次次起停服务）是幂等的；数据目录换了就放掉旧的、对新目录重取。没有 fcntl 的平台（Windows）只记一条警告。
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

from app.core.config import settings

logger = logging.getLogger(__name__)

LOCK_FILE = ".agentlab.lock"

SINGLE_PROCESS = ("应用库是 SQLite 时只支持单进程：数据目录 {dir} 已经被另一个 AgentLab 进程使用（{hint}）。"
                  "SQLite 不能多进程部署——不要给 uvicorn 加 --workers、不要让两个服务指着同一个数据目录；"
                  "要多进程，把应用库换成 PostgreSQL（AGENTLAB_DATABASE_URL，见 README.zh-CN.md「用 PostgreSQL」「多进程」）")


class SingleProcessOnly(RuntimeError):
    """SQLite 下第二个进程启动：拒绝。"""


_held: tuple[Path, int] | None = None


def _path() -> Path:
    return settings.data_dir / LOCK_FILE


def acquire() -> None:
    """SQLite 时取数据目录上的独占锁，拿不到抛 SingleProcessOnly。PostgreSQL 时什么都不做（可以多进程）。"""
    global _held
    if settings.db_dialect != "sqlite":
        return
    if settings.workers > 1:
        raise SingleProcessOnly(SINGLE_PROCESS.format(dir=settings.data_dir, hint=f"AGENTLAB_WORKERS={settings.workers}"))
    path = _path()
    if _held is not None and _held[0] == path:
        return
    release()
    try:
        import fcntl
    except ImportError:
        logger.warning("这个平台没有 fcntl，无法确认数据目录只被一个进程使用：SQLite 只支持单进程部署")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        holder = ""
        try:
            holder = path.read_text(encoding="utf-8").strip()
        except OSError:
            pass
        raise SingleProcessOnly(SINGLE_PROCESS.format(
            dir=settings.data_dir, hint=f"占着它的是 {holder}" if holder else "另一个进程占着它")) from None
    try:
        os.ftruncate(fd, 0)
        os.write(fd, f"pid {os.getpid()}".encode())
    except OSError:
        pass
    _held = (path, fd)


def release() -> None:
    global _held
    if _held is not None:
        try:
            os.close(_held[1])
        except OSError:
            pass
        _held = None


__all__ = ["LOCK_FILE", "SINGLE_PROCESS", "SingleProcessOnly", "acquire", "release"]
