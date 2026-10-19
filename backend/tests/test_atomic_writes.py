"""数据目录里的文件先写临时文件再改名（core/atomic）：读的一方看不到写了一半的，只该生成一次的不会各生成一份。"""

from __future__ import annotations

import os
import stat
import threading

import pytest

from app.core import atomic
from app.db import models  # noqa: F401  建表要先认得模型（单独跑这个文件时）


def _leftovers(folder) -> list[str]:
    return [p.name for p in folder.iterdir() if atomic.TMP_MARK in p.name]


def test_write_replaces_whole_and_leaves_no_temp_file(tmp_path):
    target = tmp_path / "a" / "b.json"
    atomic.write_text(target, "第一版")
    atomic.write_text(target, "第二版")
    assert target.read_text(encoding="utf-8") == "第二版"
    assert _leftovers(target.parent) == []


def test_a_failed_write_keeps_the_old_file(tmp_path, monkeypatch):
    target = tmp_path / "x.txt"
    atomic.write_text(target, "旧的整份")

    def boom(src, dst):
        raise OSError("磁盘满了")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        atomic.write_text(target, "新的")
    assert target.read_text(encoding="utf-8") == "旧的整份"
    assert _leftovers(tmp_path) == []


def test_create_only_once_and_the_loser_reads_the_winner(tmp_path):
    """几个线程同时第一次建同一个密钥文件：只有一个建成，文件内容就是那一个写的，没有临时文件留下。"""
    target = tmp_path / ".secret"
    results: list[tuple[str, bool]] = []
    barrier = threading.Barrier(8)

    def make(i: int) -> None:
        barrier.wait()
        results.append((f"key-{i}", atomic.create_text(target, f"key-{i}", mode=0o600)))

    threads = [threading.Thread(target=make, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    winners = [k for k, made in results if made]
    assert len(winners) == 1 and target.read_text() == winners[0]
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert _leftovers(tmp_path) == []


def test_content_addressed_artifacts_are_written_atomically(tmp_path, monkeypatch):
    """工件按内容寻址：同一份内容写两遍，文件只有一份、内容完整，不留临时文件。"""
    import asyncio

    from app.core import artifact_store
    from app.core.config import settings

    async def twice() -> tuple[str, str]:
        a = await artifact_store.put_json({"区域": "虚构", "值": 1}, kind="test_atomic")
        b = await artifact_store.put_json({"值": 1, "区域": "虚构"}, kind="test_atomic")
        return a, b

    a, b = asyncio.run(twice())
    assert a == b and artifact_store.load(a) == {"区域": "虚构", "值": 1}
    folder = settings.data_dir / "artifacts" / a[:2]
    assert _leftovers(folder) == []


async def test_file_write_tool_overwrites_atomically_and_appends_in_place():
    from types import SimpleNamespace

    from app.tools.builtin.files import file_write

    ctx = SimpleNamespace(sandbox_session=f"atomic-{os.getpid()}")
    assert (await file_write(ctx, path="notes/a.txt", content="第一行\n"))["ok"]
    from app.core.config import settings

    path = settings.session_dir(ctx.sandbox_session) / "notes" / "a.txt"
    inode = path.stat().st_ino
    out = await file_write(ctx, path="notes/a.txt", content="第二行\n", append=True)
    assert path.read_text(encoding="utf-8") == "第一行\n第二行\n" and out["bytes"] == len("第一行\n第二行\n".encode())
    assert path.stat().st_ino == inode, "追加不换文件"
    await file_write(ctx, path="notes/a.txt", content="重写\n")
    assert path.read_text(encoding="utf-8") == "重写\n"
    assert _leftovers(path.parent) == []


def test_create_falls_back_when_the_filesystem_cannot_hard_link(tmp_path, monkeypatch):
    """exFAT、一部分 SMB / FUSE 挂载不支持硬链接（EPERM、ENOTSUP、EXDEV）：退回 O_EXCL 直接建，照样只建一次，不崩。"""
    import errno

    def no_links(src, dst):
        raise OSError(errno.EPERM, "Operation not permitted")

    monkeypatch.setattr(os, "link", no_links)
    target = tmp_path / ".secret"
    assert atomic.create_text(target, "第一把", mode=0o600) is True
    assert atomic.create_text(target, "第二把", mode=0o600) is False
    assert target.read_text() == "第一把" and stat.S_IMODE(target.stat().st_mode) == 0o600
    assert _leftovers(tmp_path) == []


def test_replacing_keeps_the_permissions_of_the_old_file(tmp_path):
    target = tmp_path / "run.sh"
    target.write_text("echo 1")
    target.chmod(0o750)
    atomic.write_text(target, "echo 2")
    assert target.read_text() == "echo 2" and stat.S_IMODE(target.stat().st_mode) == 0o750


def test_appending_keeps_the_same_file(tmp_path):
    """追加写不换文件：inode、权限都不变，原有内容不动（以前整份读出来重写、再改名）。"""
    target = tmp_path / "log.txt"
    target.write_text("一\n")
    target.chmod(0o640)
    before = target.stat().st_ino
    atomic.append_bytes(target, "二\n".encode())
    assert target.read_text() == "一\n二\n"
    assert target.stat().st_ino == before and stat.S_IMODE(target.stat().st_mode) == 0o640
    assert _leftovers(tmp_path) == []


def test_the_data_dir_carries_a_marker_deploy_can_recognise():
    """数据目录里有 .agentlab-data：库在 PostgreSQL、密钥由环境变量给时，deploy.sh 也认得出这是 AgentLab 的数据目录。"""
    from app.core.config import DATA_DIR_MARKER, settings

    settings.ensure_dirs()
    marker = settings.data_dir / DATA_DIR_MARKER
    assert marker.is_file()
    text = marker.read_text(encoding="utf-8")
    settings.ensure_dirs()
    assert marker.read_text(encoding="utf-8") == text, "已有的不覆盖"
