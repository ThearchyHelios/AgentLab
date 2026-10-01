"""上传表格的原件存档：按内容哈希存，只写一次，没有任何下载接口。

**为什么要留原件。** 以前上传接口把文件读进内存、交给解析器，最后只剩一个 .db：
解析规则一改只能让用户重传，报告里也说不出「用的是哪份文件」。原件按 sha256 存下来，
构建 id 里记着它的哈希，版本才说得清来历（硬约束 H12）。

**为什么不给下载。** 原件里常有不该外传的东西：隐藏工作表里的口令、没导入的批注
（硬约束 H10）。导入时这些都被挡在库外，开一个下载口子等于又放了出去。要删掉它，
走「清除原件」（purge_raw），导入记录里留下时间、署名和理由。

**写法。** 先写同目录的临时文件、fsync，再 os.link 到最终路径——link 不会覆盖已有文件，
两个请求同时存同一份内容时，后到的拿到 FileExistsError，校验哈希后直接复用。最终文件
改成 0444：存档之后不该再被任何代码路径改写。
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import secrets
from pathlib import Path

from app.core.config import settings

logger = logging.getLogger(__name__)

_SHA = re.compile(r"^[0-9a-f]{64}$")
#: 临时文件名里的标记。启动清理按它找上次崩溃留下的半成品（table_versions.startup）
TMP_MARK = ".tmp-"


def raw_root() -> Path:
    """原件存档的根目录。按调用时的 settings 现算：测试会换数据目录。"""
    return settings.uploads_dir / "raw"


def raw_path(sha: str) -> Path:
    """哈希 → 存档路径（uploads/raw/<前两位>/<sha>）。只认 64 位小写十六进制，挡住路径穿越。"""
    if not _SHA.match(sha or ""):
        raise ValueError("原件哈希的格式不对")
    return raw_root() / sha[:2] / sha


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def fsync_dir(path: Path) -> None:
    """目录项也要落盘：只 fsync 文件，断电后可能文件内容在、目录里却没有这个名字。"""
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def put_raw(raw: bytes) -> tuple[str, Path]:
    """存一份原件，返回 (sha256, 路径)。同样内容已经存过就校验后复用，不重写。

    已有文件的哈希对不上（磁盘损坏、被人改过）时，用这次的字节原子替换掉它：路径本身就是
    内容的哈希，这次手里的字节才是这个名字应有的内容。
    """
    sha = hashlib.sha256(raw).hexdigest()
    path = raw_path(sha)
    if path.is_file() and sha256_file(path) == sha:
        return sha, path
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".{sha}{TMP_MARK}{secrets.token_hex(6)}"
    try:
        with open(tmp, "wb") as fh:
            fh.write(raw)
            fh.flush()
            os.fsync(fh.fileno())
        try:
            os.link(tmp, path)
        except FileExistsError:
            if sha256_file(path) == sha:
                return sha, path        # 并发的另一个请求刚存好同一份
            logger.warning("原件存档 %s 的内容与文件名不符，已用本次上传的内容替换", sha)
            os.replace(tmp, path)
        os.chmod(path, 0o444)
        fsync_dir(path.parent)
        return sha, path
    finally:
        tmp.unlink(missing_ok=True)


def raw_exists(sha: str) -> bool:
    try:
        return raw_path(sha).is_file()
    except ValueError:
        return False


def purge_raw(sha: str, *, prune: bool = False) -> bool:
    """删掉一份原件。返回是否真的删了文件（本来就不在返回 False）。

    只删文件；导入记录上的 raw_state 和清除记录由调用方写——同一份内容可能被几次导入共用，
    删不删由调用方看过引用之后决定。

    prune：删完后所在的 raw/<前两位>/ 空了就一并删掉。只有持着版本存储锁的调用方能这么做
    （table_versions 的清除原件、回收）：put_raw 也在锁里，先建目录再写临时文件，锁外删目录
    会让并发的存档在两步之间扑空。
    """
    try:
        path = raw_path(sha)
    except ValueError:
        return False
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    try:
        fsync_dir(path.parent)
    except OSError:  # pragma: no cover - 删已经成功，目录落盘失败不改变结论
        pass
    if prune:
        remove_empty_dir(path.parent)
    return True


def remove_empty_dir(path: Path) -> bool:
    """目录空了就删掉，返回删没删。不空、不存在、没权限都不算错：留着一个空目录不影响任何事。"""
    try:
        path.rmdir()
    except OSError:
        return False
    return True
