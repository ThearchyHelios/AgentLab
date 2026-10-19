"""数据目录里的文件一律先写临时文件（同目录、名字带随机后缀）再改名。

为什么：读的一方（同一台机器上的别的进程、共享存储另一头的机器）要么看到旧的完整文件，要么看到新的完整文件，
看不到写了一半的；进程写到一半没了，留下的是一个没人认的临时文件，不是半个正式文件。临时文件名带随机后缀：
两个进程同时写同一个文件（内容寻址的工件撞了同一份内容）各写各的，不会一个把另一个写到一半的截断。

- write_bytes / write_text：写完 os.replace 到目标（目标已有就替换，沿用它的权限）；
- append_bytes：追加写，不走改名（理由见函数说明）；
- create_bytes / create_text：只在目标还没有时建（os.link，已有就不动、返回 False）——给只该生成一次的东西用
  （加密密钥、签名私钥）：两个进程同时第一次启动，后到的读先到的那一份，不会各拿一把。

上传表格的版本库、原件存档、记录表另有自己的写法（data/raw_store、data/table_versions，同样先写临时文件），不走这里。
"""

from __future__ import annotations

import contextlib
import os
import secrets
from pathlib import Path

#: 临时文件名里的记号：.<原名>.tmp-<随机>（同 data/raw_store.TMP_MARK 的写法）
TMP_MARK = ".tmp-"


def _tmp_for(path: Path) -> Path:
    return path.parent / f".{path.name}{TMP_MARK}{secrets.token_hex(6)}"


def _write_tmp(path: Path, data: bytes, *, mode: int | None, fsync: bool) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = _tmp_for(path)
    # 没给 mode 时照普通新建文件（0666 再受 umask 约束）；给了的（密钥 0600）从建出来那一刻起就是那个权限，再 chmod
    # 一次压过 umask
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666 if mode is None else 0o600)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            if fsync:
                os.fsync(fh.fileno())
        if mode is not None:
            os.chmod(tmp, mode)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise
    return tmp


def fsync_dir(folder: Path) -> None:
    """把目录项（新建、改名）落盘。有的平台、文件系统打不开目录或不支持，就算了（同 data/raw_store.fsync_dir）。"""
    try:
        fd = os.open(folder, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def write_bytes(path: Path, data: bytes, *, mode: int | None = None, fsync: bool = False) -> None:
    """原子地写（替换）path。没给 mode、原来已有这个文件时沿用它的权限（改名换掉的是文件本身，权限不跟着走）。"""
    if mode is None:
        with contextlib.suppress(OSError):
            mode = path.stat().st_mode & 0o7777
    tmp = _write_tmp(path, data, mode=mode, fsync=fsync)
    try:
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


def write_text(path: Path, text: str, *, encoding: str = "utf-8", mode: int | None = None,
               fsync: bool = False) -> None:
    write_bytes(path, text.encode(encoding), mode=mode, fsync=fsync)


def create_bytes(path: Path, data: bytes, *, mode: int | None = None, fsync: bool = True) -> bool:
    """path 还没有时原子地建出来，返回 True；已经有了（别人先建了）什么都不动，返回 False。建成后把目录项也落盘。

    硬链接不了的文件系统（exFAT、一部分 SMB / FUSE 挂载报 EPERM、ENOTSUP、EXDEV）退回 O_CREAT | O_EXCL 直接建目标文件：
    仍然只有一个进程建得成，但写的那一小会儿别的进程可能读到不完整的内容——只在头一次生成时有这个窗口。"""
    tmp = _write_tmp(path, data, mode=mode, fsync=fsync)
    try:
        os.link(tmp, path)
    except FileExistsError:
        return False
    except OSError:
        return _create_directly(path, data, mode=mode, fsync=fsync)
    finally:
        with contextlib.suppress(OSError):
            tmp.unlink()
    fsync_dir(path.parent)
    return True


def _create_directly(path: Path, data: bytes, *, mode: int | None, fsync: bool) -> bool:
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666 if mode is None else 0o600)
    except FileExistsError:
        return False
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            if fsync:
                os.fsync(fh.fileno())
        if mode is not None:
            os.chmod(path, mode)
    except BaseException:
        with contextlib.suppress(OSError):
            path.unlink()
        raise
    fsync_dir(path.parent)
    return True


def append_bytes(path: Path, data: bytes, *, fsync: bool = False) -> None:
    """追加写（O_APPEND）：不换文件（inode、权限、别人手里开着的句柄都不变），原有内容一个字节都不动。

    不走「临时文件再改名」：那得把整份读出来重写，文件越大越慢，还会换掉 inode 和权限。代价是追加到一半时读的一方可能
    看到半截新内容——它看到的永远是「原有的整份 + 新内容的一个前缀」，原有的内容不会丢。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o666)
    with os.fdopen(fd, "ab") as fh:
        fh.write(data)
        fh.flush()
        if fsync:
            os.fsync(fh.fileno())


def create_text(path: Path, text: str, *, encoding: str = "utf-8", mode: int | None = None) -> bool:
    return create_bytes(path, text.encode(encoding), mode=mode)
