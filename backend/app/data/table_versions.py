"""上传表格的版本底座：构建、导入记录、快照，以及发布、迁移、回收。

**为什么要有版本。** 以前重传是往同一个 .db 里 DROP + CREATE：第一张表的 DROP/CREATE 先
自动提交，后面失败时库里是新旧混杂的数据；正在查询的连接看到半截的库；运行查到的数据和它
冻结下来的表结构可能不是同一版；原件不留，说不清用的是哪份文件。改成三层，每层不可变：

- 构建（table_builds）：一次解析的产物，id = sha256(源、原件、选项、解析器版本)。库文件写到
  新路径、改成 0444，之后不再动。同一个源重传同一个文件得到同一个构建。
- 导入记录（table_imports）：一次上传。谁、何时、什么文件、原件留没留，都在这里。
- 快照（source_snapshots）：数据源的一个可查询版本 = 若干期导入 + 冻结的表结构。期 1 只做
  「每期替换」，一个快照就是一期，库直接用那次构建的文件。

数据源上只记「当前快照」这一个指针（current_snapshot_id）。发布时先把文件写好、落盘，最后
在一个事务里写三层记录并切指针：任何一步失败，指针不动，旧版完整可查。

**查询怎么拿到版本。** resolve_source 把上传源换成绑定某个快照的 SourceView（库路径、表结构
都来自快照，immutable、带期望哈希）；运行钉住的快照找不到时明确报错，绝不退回当前指针。

**回收。** 任何运行引用的快照、各源当前快照，以及它们用到的构建和原件受保护；其余的删文件、
记录标 retired 留作审计。
"""
from __future__ import annotations

import asyncio
import copy
import dataclasses
import hashlib
import json
import logging
import os
import re
import secrets
import sqlite3
import stat
import weakref
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence
from urllib.parse import unquote, urlsplit

from sqlalchemy import func, or_, select, update
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.errors import explain, first_line
from app.data import introspect as introspect_mod
from app.data import raw_store, tabular
from app.data.engine import engines
from app.db import base as db_base
from app.db.base import new_id, utcnow
from app.db.models import DataSource, Run, SourceSnapshot, TableBuild, TableImport

logger = logging.getLogger(__name__)

#: 解析器版本，进构建 id。解析规则改了（类型推断、名字规则、区域判定）就要改它：同一份原件
#: 按新规则建出来的库不是同一个构建，不能复用旧文件
ENGINE_VER = "tabular/2"
#: 迁移前的老上传：只剩一个 .db，不知道当时按什么规则解析的
LEGACY_ENGINE_VER = "legacy"

#: 未规整的表写进表结构的说明。不含数字和坐标：说明会进模型看得到的文本（H11）。定义在 tabular（见那里）
UNSHAPED_NOTE = tabular.UNSHAPED_NOTE

_CSV_EXTS = (".csv", ".tsv")
_SOURCE_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_TMP = raw_store.TMP_MARK
#: 探查新库时引擎缓存用的一次性键的标记（不含冒号：data/engine.py 按最后一个冒号切出源 id）
_PROBE = "~probe~"
#: 上传库只有 main：options 里的这一项对上传源没有意义，留着只会让探查去找不存在的 schema
_SCHEMA_OPTION = "schema"


class SnapshotMissing(ValueError):
    """要的快照不存在、不属于这个源、已回收或文件丢了。message 直接给人看。"""


class PublishError(RuntimeError):
    """解析成功了，但没能把库文件安全地发布出去。指针没动，旧版照常可查。"""


class ParseFailed(tabular.UnsupportedTable):
    """解析时出了意料之外的错（不是 UnsupportedTable 那种「认得出、读不了」）。

    继承 UnsupportedTable：接口层统一按 400 回给用户，原异常留在 __cause__ 里供日志定位。
    """


@dataclass
class ImportResult:
    import_id: str
    snapshot_id: str
    build_id: str
    build_reused: bool
    #: 解析回执（tabular.LoadReport 的 JSON 形状）
    report: dict[str, Any]


@dataclass
class SourceView:
    """绑定了某个快照的数据源。engine / introspect / guard / 工具只读这些字段，和 DataSource 同名。

    不是 ORM 对象，不进会话：拿它查询不会顺手把快照路径写回数据源。
    """

    id: str
    name: str
    kind: str = "sqlite"
    database: str | None = None
    options: dict[str, Any] = field(default_factory=dict)
    readonly: bool = True
    description: str = ""
    schema_cache: dict[str, Any] = field(default_factory=dict)
    schema_synced_at: datetime | None = None
    enabled: bool = True
    host: str | None = None
    port: int | None = None
    username: str | None = None
    password: str | None = None
    last_check: dict[str, Any] | None = None
    origin: str = "upload"
    current_snapshot_id: str | None = None
    #: 绑定的快照。引擎缓存按「源 id + 快照 id」分开（data/engine.py）
    snapshot_id: str | None = None
    #: 以 immutable=1 打开：文件发布后不会再变，SQLite 可以不加锁、不查日志
    immutable: bool = True
    #: 第一次为这个快照建引擎前核对文件哈希，对不上拒绝查询
    expected_sha256: str | None = None


# --------------------------------------------------------------------------
# id 与路径
# --------------------------------------------------------------------------


def _sha_json(obj: Any) -> str:
    text = json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def parse_options(filename: str, *, header_row: int, mixed: str, raw_mode: bool) -> dict[str, Any]:
    """影响解析结果的全部选项。进构建 id：选项不同，建出来的库就不是同一个。

    文件名只取会影响结果的部分：扩展名决定按 Excel 还是 CSV 读；CSV 的表名取自文件名，
    所以 CSV 带上文件名。Excel 的表名来自工作表名，同一个文件换个名字再传，仍是同一个构建。
    """
    base = (filename or "").replace("\\", "/").rsplit("/", 1)[-1]
    ext = base[base.rfind("."):].lower() if "." in base else ""
    options: dict[str, Any] = {
        "header_row": int(header_row), "mixed": mixed, "raw_mode": bool(raw_mode), "ext": ext,
    }
    if ext in _CSV_EXTS:
        options["file_name"] = base
    return options


def build_id(source_id: str, raw_sha: str, options: dict[str, Any]) -> str:
    """构建 id：sha256 全长。source_id 在里面——两个源传同一个文件是两个构建，互不引用。"""
    return _sha_json({
        "source_id": source_id, "raw_sha256": raw_sha,
        "options_sha256": _sha_json(options), "engine_ver": ENGINE_VER,
    })


def snapshot_id(source_id: str, import_ids: list[str], engine_ver: str = ENGINE_VER) -> str:
    return _sha_json({"source_id": source_id, "imports": list(import_ids), "engine_ver": engine_ver})


def legacy_build_id(source_id: str, file_sha: str) -> str:
    """迁移前老上传的 v0 构建 id。带上 source_id：两个源的老库内容相同，id 也不同。"""
    return hashlib.sha256(f"legacy{source_id}{file_sha}".encode("utf-8")).hexdigest()


def tables_root() -> Path:
    return settings.uploads_dir / "tables"


def build_path(source_id: str, bid: str) -> Path:
    if not _SOURCE_ID.match(source_id or ""):
        raise ValueError("数据源 id 的格式不对")
    return tables_root() / source_id / "builds" / f"{bid}.db"


def _under(path: str | os.PathLike[str], root: Path) -> bool:
    try:
        return Path(os.path.realpath(path)).is_relative_to(os.path.realpath(root))
    except (OSError, ValueError):
        return False


def _sqlite_targets(database: str) -> list[str]:
    """一个 SQLite「数据库」字段可能打开的文件：原字符串，加上按连接串规则还原出的真实路径。

    手工源的 database 原样拼进连接串（data/engine.py build_url）。SQLAlchemy 把 ? 后面拆成
    连接参数，带 uri=true 时 file: 开头的整段按 SQLite URI 打开（百分号转义、mode=rwc 之类）。
    只对原字符串做 realpath，「file:/…/builds/x.db?uri=true」会被当成当前目录下的相对路径，
    「…/x.db?x=/../../别处」会被 .. 带到别处——两种写法实际打开的都是上传目录里的文件。
    """
    targets = [database]
    try:
        url = make_url(f"sqlite:///{database}")
    except Exception:  # noqa: BLE001 - 连接串都拼不出来的，连库也会失败；只按原字符串判断
        return targets
    path = url.database or ""
    targets.append(path)
    # 不带 uri=true 时 SQLite 一般不按 URI 解析，但编译选项 SQLITE_USE_URI 能打开它：一律按两种都算
    if path[:5].lower() == "file:":
        parts = urlsplit(path)
        targets += [unquote(parts.path), unquote(path[5:].split("?", 1)[0].split("#", 1)[0])]
    return [t for t in dict.fromkeys(targets) if t and t != ":memory:"]


#: 文件身份：(st_dev, st_ino)。同一个文件不管怎么写路径，身份都一样
FileId = tuple[int, int]


def _file_id(path: str | os.PathLike[str]) -> FileId | None:
    try:
        st = os.stat(path)
    except (OSError, ValueError):
        return None
    return st.st_dev, st.st_ino


@dataclass(frozen=True)
class _Zone:
    """一组受保护的位置：若干目录（连同其下的一切）和若干单个文件。

    **按文件身份认，不按路径字符串认。** macOS 默认的 APFS 不区分大小写，realpath 也不解析
    firmlink：「…/Uploads/…」「…/DATA/…」「/System/Volumes/Data/Users/…」打开的都是同一个
    文件，按字符串前缀比却都不算在里面。所以对目标路径取 realpath 后，从它自己往上逐级 stat，
    哪一级和受保护目录是同一个 (st_dev, st_ino) 就算落在里面；目标本身和受保护文件同身份
    （包括硬链接）也算。字符串前缀只作兜底：受保护目录还不存在时，没有身份可比。
    """

    dirs: tuple[str, ...] = ()
    dir_ids: frozenset[FileId] = frozenset()
    files: tuple[str, ...] = ()
    file_ids: frozenset[FileId] = frozenset()
    #: 还不存在的受保护文件（-journal 之类，SQLite 用到时才建）：(所在目录的身份, casefold 后的文件名)。
    #: 不区分大小写的文件系统上「AGENTLAB.DB-JOURNAL」就是它；区分大小写的系统上多拒几个怪名字，无妨
    names: frozenset[tuple[FileId, str]] = frozenset()
    #: 目标文件有多个硬链接时，到这些目录里找它的另一个名字
    link_roots: tuple[str, ...] = ()


def _zone(dirs: Sequence[Path] = (), files: Sequence[Path] = (), *, find_links: bool = False) -> _Zone:
    dir_reals = tuple(os.path.realpath(d) for d in dirs)
    file_reals = tuple(os.path.realpath(f) for f in files)
    names = set()
    for real in file_reals:
        parent = _file_id(os.path.dirname(real))
        if parent is not None:
            names.add((parent, os.path.basename(real).casefold()))
    return _Zone(
        dirs=dir_reals, dir_ids=frozenset(i for d in dir_reals if (i := _file_id(d)) is not None),
        files=file_reals, file_ids=frozenset(i for f in file_reals if (i := _file_id(f)) is not None),
        names=frozenset(names), link_roots=dir_reals if find_links else (),
    )


def _linked_into(real: str, leaf: FileId, roots: tuple[str, ...]) -> bool:
    """目标文件是不是受保护目录里某个文件的硬链接。只在链接数大于 1 时才去目录里找，平时不花时间。"""
    try:
        st = os.stat(real)
    except (OSError, ValueError):
        return False
    if not stat.S_ISREG(st.st_mode) or st.st_nlink < 2:
        return False
    for root in roots:
        for folder, _dirs, names in os.walk(root):
            for name in names:
                try:
                    other = os.lstat(os.path.join(folder, name))
                except OSError:
                    continue
                if (other.st_dev, other.st_ino) == leaf:
                    return True
    return False


def _hits(target: str, zone: _Zone) -> bool:
    """一个路径（已按连接串规则还原）会不会打开受保护位置里的文件。"""
    try:
        real = os.path.realpath(target)
    except (OSError, ValueError):
        return False
    if real in zone.files or any(Path(real).is_relative_to(d) for d in zone.dirs):
        return True
    leaf = _file_id(real)
    if leaf is not None and leaf in zone.file_ids:
        return True
    parent = _file_id(os.path.dirname(real))
    if parent is not None and (parent, os.path.basename(real).casefold()) in zone.names:
        return True
    # 从目标自己往上逐级比目录身份：目标和中间几级目录还不存在（mode=rwc 新建）时，靠已存在的祖先认
    node = real
    while True:
        fid = _file_id(node)
        if fid is not None and fid in zone.dir_ids:
            return True
        up = os.path.dirname(node)
        if up == node:
            break
        node = up
    return bool(zone.link_roots) and leaf is not None and _linked_into(real, leaf, zone.link_roots)


def _reaches(path: str | None, zone: _Zone) -> bool:
    if not path or path == ":memory:":
        return False
    return any(_hits(target, zone) for target in _sqlite_targets(path))


def under_uploads(path: str | None) -> bool:
    """手工登记的 SQLite 源会不会打开上传目录里的文件。按文件身份，也按连接串和 URI 的解析规则。

    任何一种解读落进上传目录都算：那里的文件归版本底座管，手工源指过去就绕开了只读和版本。
    大小写不同的写法、firmlink 前缀、符号链接、硬链接都认（见 _Zone）。
    """
    return _reaches(path, _zone([settings.uploads_dir], find_links=True))


#: SQLite 库文件旁边可能出现的日志文件
_DB_SIDE_FILES = ("", "-wal", "-shm", "-journal")


def _app_db_files() -> list[Path]:
    """应用自己的库：元数据库、运行检查点库，以及实际连着的元数据库（测试里和 data_dir 下的不是同一个）。"""
    dbs = [settings.db_path, settings.checkpoint_path]
    try:
        live = db_base.engine.url.database
    except Exception:  # noqa: BLE001 - 取不到就只按配置里的路径判断
        live = None
    if live and live != ":memory:":
        dbs.append(Path(live))
    return [Path(f"{db}{suffix}") for db in dict.fromkeys(dbs) for suffix in _DB_SIDE_FILES]


def reaches_app_files(path: str | None) -> bool:
    """手工登记的 SQLite 源会不会打开 AgentLab 自己的数据文件。判断方式同 under_uploads。

    元数据库里有数据源的连接字段、快照登记和哈希、运行与审批记录：登记成可写的源，一条经审批的
    UPDATE 就能把上传源改成手工源、指向别处，版本底座和「连接字段只由系统维护」一起失效；只读登记
    也会把全部元数据交给模型。检查点库、加密密钥、工件、沙箱策略同理。工作区不在其中：那是模型
    可写的沙箱根目录，登记模型生成的库是正当用法。
    """
    data = settings.data_dir
    return _reaches(path, _zone(
        [data / "artifacts", data / "sandbox-policies"], [*_app_db_files(), data / ".secret_key"]))


def report_to_json(report: Any) -> dict[str, Any]:
    """LoadReport（dataclass）→ 能存进 JSON 列的 dict。"""
    data = dataclasses.asdict(report) if dataclasses.is_dataclass(report) else dict(report or {})
    return json.loads(json.dumps(data, ensure_ascii=False, default=str))


# --------------------------------------------------------------------------
# 快照解析
# --------------------------------------------------------------------------


def upload_options(options: dict[str, Any] | None) -> dict[str, Any]:
    """上传源实际生效的 options：去掉 schema（库里只有 main），查询时限、遮罩列照旧。"""
    return {k: v for k, v in (options or {}).items() if k != _SCHEMA_OPTION}


def bind_snapshot(source: Any, snapshot: SourceSnapshot) -> SourceView:
    """数据源 + 快照 → SourceView：只读、库路径和表结构都取快照里冻结的那份。

    快照没登记文件哈希（记录损坏、被手工改过）时抛 SnapshotMissing：没有哈希就无从核对文件
    是否被改过，不能退回「不核对照样打开」。现有的写入路径（发布、迁移）都会写哈希。
    """
    if not str(snapshot.db_sha256 or "").strip():
        logger.warning("快照没有登记文件哈希，已拒绝：source=%s snapshot=%s", source.id, snapshot.id)
        raise SnapshotMissing(
            f"「{source.name}」的数据版本 {snapshot.id[:12]} 缺少数据文件的哈希登记，"
            "无法核对数据文件是否被修改过，已拒绝查询，请重新上传"
        )
    return SourceView(
        id=source.id, name=source.name, kind="sqlite", database=snapshot.db_path,
        options=upload_options(source.options), readonly=True,
        description=source.description or "",
        schema_cache=copy.deepcopy(snapshot.schema_cache or {}),
        schema_synced_at=snapshot.created_at, enabled=bool(source.enabled),
        last_check=getattr(source, "last_check", None),
        origin="upload", current_snapshot_id=getattr(source, "current_snapshot_id", None),
        snapshot_id=snapshot.id, immutable=True, expected_sha256=str(snapshot.db_sha256).strip(),
    )


async def resolve_source(
    session: AsyncSession, source: DataSource, pinned_snapshot_id: str | None = None
) -> DataSource | SourceView:
    """查询时实际该用的源。手工源原样返回；上传源换成绑定快照的 SourceView。

    钉了快照就只认那一个：它不属于这个源、已回收、文件不在，都抛 SnapshotMissing，
    **绝不退回当前指针**——运行前后两段查的必须是同一版数据，否则报告里的数字对不上来历。
    """
    if (getattr(source, "origin", None) or "manual") != "upload":
        return source
    wanted = pinned_snapshot_id or source.current_snapshot_id
    if not wanted:
        raise SnapshotMissing(f"上传的表格「{source.name}」没有可用的版本，请重新上传")
    snap = await session.get(SourceSnapshot, wanted)
    pinned = bool(pinned_snapshot_id)
    if snap is None or snap.source_id != source.id:
        raise SnapshotMissing(
            f"「{source.name}」的数据版本 {wanted[:12]} 不存在或不属于该数据源"
            + ("，已拒绝查询，不会改用当前版本" if pinned else "，请重新上传")
        )
    if snap.retired_at is not None or not Path(snap.db_path).is_file():
        raise SnapshotMissing(
            f"「{source.name}」的数据版本 {wanted[:12]} 的数据文件已不存在"
            + ("，已拒绝查询，不会改用当前版本" if pinned else "，请重新上传")
        )
    return bind_snapshot(source, snap)


# --------------------------------------------------------------------------
# 锁：发布、回收、启动清理彼此串行
# --------------------------------------------------------------------------

_locks: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock]" = weakref.WeakKeyDictionary()


def _store_lock() -> asyncio.Lock:
    """版本存储的写锁，按事件循环各一把。

    回收要先看「哪些文件有记录」再删：发布正处在「文件已 link、记录未提交」之间时，回收
    会把它当孤儿删掉；复用一个刚被判为无人引用的构建，也会撞上正在删它的回收。上传不频繁，
    全部串行最省心。按循环分开是因为 asyncio.Lock 一旦在某个循环里等过，就不能换循环用。
    """
    loop = asyncio.get_running_loop()
    lock = _locks.get(loop)
    if lock is None:
        lock = _locks[loop] = asyncio.Lock()
    return lock


#: 同时解析的上传最多几个。解析在线程里做，内存和上传数成正比；不设上限的话，几个大文件
#: 同时上传，内存会叠加起来。超出的排队等前面的解析完
MAX_PARALLEL_PARSES = 2
_parse_slots: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore]" = weakref.WeakKeyDictionary()


def _parse_slot() -> asyncio.Semaphore:
    """解析的并发名额，按事件循环各一份（理由同 _store_lock）。"""
    loop = asyncio.get_running_loop()
    slot = _parse_slots.get(loop)
    if slot is None:
        slot = _parse_slots[loop] = asyncio.Semaphore(MAX_PARALLEL_PARSES)
    return slot


def pin_lock() -> asyncio.Lock:
    """固定数据版本时持有的锁（就是版本存储的写锁）：从读当前指针到把它写进运行记录，全程持有。

    回收按「已提交的运行记录 + 各源当前指针」判断哪些快照还有人用。发起运行（engine/runner.py）
    和运行中途补固定（tools/datasource.py 的 _pin_late）都是先读指针、过一会儿才提交运行记录：
    中间要是有人重传，发布切走指针、紧接着的回收看不到这条还没提交的运行，刚读到的快照就被删了，
    这次运行只能报「固定的数据版本已不存在」。持着这把锁，回收要么在读指针之前做完，要么等运行
    记录提交之后才开始。只包住读指针到提交这一小段，别在里面做慢事（发布和回收也在等它）。
    """
    return _store_lock()


# --------------------------------------------------------------------------
# 发布
# --------------------------------------------------------------------------


def _remove_db_files(path: Path) -> None:
    """删掉一个 SQLite 文件和它可能留下的日志文件。"""
    for suffix in ("", "-journal", "-wal", "-shm"):
        Path(f"{path}{suffix}").unlink(missing_ok=True)


def _prune_build_dirs(final: Path) -> None:
    """删掉构建路径上空了的 builds/ 和 tables/<源 id>/ 两级目录（不空就留着）。"""
    builds = final.parent
    if raw_store.remove_empty_dir(builds):
        raw_store.remove_empty_dir(builds.parent)


def _parse_to_tmp(final: Path, raw: bytes, filename: str, header_row: int, mixed: str,
                  raw_mode: bool) -> tuple[Path, Any]:
    """在线程池里解析到一个临时库。失败时删掉临时文件，异常照原样（或包一层）抛出。"""
    final.parent.mkdir(parents=True, exist_ok=True)
    tmp = final.parent / f"{final.name}{_TMP}{secrets.token_hex(6)}"
    try:
        report = tabular.load_into(str(tmp), raw, filename, header_row=header_row,
                                   mixed=mixed, raw_mode=raw_mode)
    except tabular.UnsupportedTable:
        _remove_db_files(tmp)
        raise
    except Exception as e:
        _remove_db_files(tmp)
        reason = first_line(e) if isinstance(e, ValueError) else explain(e)[0]
        raise ParseFailed(
            f"导入失败：{reason}。请确认文件是未加密的 Excel 或 CSV，且表头行号填写正确"
        ) from e
    except BaseException:
        _remove_db_files(tmp)
        raise
    return tmp, report


_JOURNAL_STUCK = "数据文件的日志未能合并，已停止发布，原数据未受影响"


def _settle_journal(path: Path) -> None:
    """切成回滚日志模式并确认没有残留的 -wal / -journal：发布后以 immutable 打开，SQLite 不会再看日志。

    解析器要是用了 WAL、又有连接没关，切换会报「database is locked」或者原样留在 WAL：
    这时库里的一部分数据还在 -wal 里，按 immutable 打开会静默少数据，只能停下。
    """
    try:
        conn = sqlite3.connect(str(path))
        try:
            mode = conn.execute("PRAGMA journal_mode=DELETE").fetchone()
        finally:
            conn.close()
    except sqlite3.Error as e:
        raise PublishError(_JOURNAL_STUCK) from e
    if not mode or str(mode[0]).lower() != "delete":
        raise PublishError(_JOURNAL_STUCK)
    for suffix in ("-wal", "-journal"):
        if Path(f"{path}{suffix}").exists():
            raise PublishError(_JOURNAL_STUCK)
    Path(f"{path}-shm").unlink(missing_ok=True)


def _fsync_file(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _publish_file(tmp: Path, final: Path, expected_sha: str | None) -> tuple[str, bool]:
    """把临时库发布到最终路径，返回 (库文件 sha256, 是否复用了已有文件)。

    先落盘再 link：link 不会覆盖已有文件，已发布的版本因此不可能被半个新文件顶掉。
    目标已存在时（同一个构建传过），只有它的哈希等于登记的 db_sha256 才复用——0 字节的
    残骸、被改过的文件都不能冒充。没有登记过（上次发布到一半进程没了）时，和这次刚建的
    临时库逐字节一致也可以接手。临时文件无论成败都删掉。
    """
    try:
        _settle_journal(tmp)
        sha = raw_store.sha256_file(tmp)
        _fsync_file(tmp)
        raw_store.fsync_dir(tmp.parent)
        try:
            os.link(tmp, final)
        except FileExistsError:
            existing = raw_store.sha256_file(final)
            if existing == (expected_sha or sha):
                return existing, True
            raise PublishError(
                "导入失败：服务端已有的同版本数据文件与登记的不一致，可能被修改过。"
                "当前版本未受影响，请联系管理员检查数据目录"
            ) from None
        except OSError as e:
            raise PublishError(f"导入失败：无法写入数据文件（{first_line(e)}），当前版本未受影响") from e
        os.chmod(final, 0o444)
        raw_store.fsync_dir(final.parent)
        return sha, False
    finally:
        _remove_db_files(tmp)


async def _introspect_view(view: SourceView, report: dict[str, Any]) -> dict[str, Any]:
    """探查快照库的结构，给未规整的表注入说明。

    走 introspect 而不是自己读 sqlite_master：表结构的形状必须和手工源的一模一样，下游
    （工具描述、db_schema、表结构快照）才不用分两种写。

    探查用一次性的源 id：引擎缓存要是只按源 id 分键（data/engine.py 加上快照 id 之前），
    用真 id 就会拿到旧版本留在缓存里的引擎，把旧库的结构冻结进新快照；而上传源不能重新
    探查、/schema 又按快照回答，错的结构会一直留到下一次上传。用完即关，不留在缓存里。
    """
    probe = dataclasses.replace(
        view, id=f"{view.id}{_PROBE}{secrets.token_hex(6)}", options=upload_options(view.options))
    try:
        cache = await introspect_mod.introspect(probe)
    finally:
        await engines.invalidate(probe.id)
    unshaped = {str(t.get("name")) for t in report.get("tables") or [] if t.get("unshaped")}
    for name, meta in (cache.get("tables") or {}).items():
        if name in unshaped:
            meta["comment"] = UNSHAPED_NOTE
    return cache


def _check_probe(cache: dict[str, Any], report: dict[str, Any], source_id: str) -> None:
    """冻结进快照之前核对：探查成功，且探到的表正是解析回执里的那些。

    快照的结构一旦冻结就改不了（上传源不能重新探查），所以宁可这次发布失败、旧版照常可查，
    也不留下一个结构为空或者描述的是别的库的版本。
    """
    if cache.get("failed"):
        raise PublishError(
            f"导入失败：读取新数据的表结构时出错（{cache.get('error') or '原因不明'}），当前版本未受影响")
    expected = {str(t.get("name")) for t in report.get("tables") or [] if isinstance(t, dict)}
    probed = set((cache.get("tables") or {}).keys())
    # 表多到探查截断时只看探到的那些是不是都在回执里
    if not (probed <= expected if cache.get("truncated") else probed == expected):
        logger.error("上传表格发布时表结构与解析结果不一致：source=%s 探查=%s 回执=%s",
                     source_id, sorted(probed), sorted(expected))
        raise PublishError("导入失败：新数据的表结构与解析结果对不上，已停止发布，当前版本未受影响")


async def publish_upload(
    session: AsyncSession, source: DataSource, raw: bytes, filename: str, *,
    header_row: int = 1, mixed: str = "reject", raw_mode: bool = False,
) -> ImportResult:
    """解析一份上传、发布成新版本并启用。返回导入回执。

    1. 线程池里解析到临时库；NeedsDecision / UnsupportedTable 原样抛出——不留原件、不动指针。
    2. 发布库文件：落盘、link 到构建路径、0444。
    3. 存原件；一个事务里写构建、导入记录、快照，旧的 active 导入置 superseded，切数据源指针。
       任何一步失败都回滚，并删掉这次新建的文件，指针不动。
    4. 回收一次（失败只记日志）。

    source 可以是还没入库的新 DataSource：失败时它也不会被建出来。
    """
    if not source.id:
        source.id = new_id()
    # 失败回滚后 source 的属性会过期，再读就要回库加载；后面一律用这份本地值
    source_id = source.id
    raw_sha = hashlib.sha256(raw).hexdigest()
    options = parse_options(filename, header_row=header_row, mixed=mixed, raw_mode=raw_mode)
    bid = build_id(source_id, raw_sha, options)
    final = build_path(source_id, bid)
    # 还没入库的新源（接口层每次给一个新 id）：失败时它的 tables/<源 id>/builds/ 目录也不该留下。
    # 被拒收的上传在「422 → 用户选择 → 重传」里每轮都是一个新 id，不收拾的话每轮多一个空目录。
    # 新 id 只有这一个请求在用，删目录不会和别的发布撞上；已有的源目录里有当前版本，不动
    state = sa_inspect(source)
    fresh = state.transient or state.pending

    try:
        async with _parse_slot():
            tmp, parsed = await asyncio.to_thread(
                _parse_to_tmp, final, raw, filename, header_row, mixed, raw_mode)
    except BaseException:
        if fresh:
            await asyncio.to_thread(_prune_build_dirs, final)
        raise
    report = report_to_json(parsed)

    linked = raw_new = False
    try:
        async with _store_lock():
            # 先读后写：读的时候别把调用方挂起的改动（新源、改说明）提前刷进库。
            # 应用库的写锁只在最后提交那一下持有，解析、哈希、fsync 都不占着它
            with session.no_autoflush:
                build = await session.get(TableBuild, bid)
                last_seq = (await session.execute(
                    select(func.max(TableImport.seq)).where(TableImport.source_id == source_id)
                )).scalar() or 0
            db_sha, reused_file = await asyncio.to_thread(
                _publish_file, tmp, final, build.db_sha256 if build is not None else None)
            linked = not reused_file
            raw_new = not raw_store.raw_exists(raw_sha)
            await asyncio.to_thread(raw_store.put_raw, raw)

            now = utcnow()
            if build is None:
                build = TableBuild(
                    id=bid, source_id=source_id, raw_sha256=raw_sha,
                    options_sha256=_sha_json(options), engine_ver=ENGINE_VER, options=options,
                    db_path=str(final), db_sha256=db_sha, report=report, created_at=now,
                )
                session.add(build)
                build_reused = False
            else:
                if not reused_file:
                    # 记录在、文件被回收过：这次重新建了出来
                    build.db_sha256, build.report = db_sha, report
                elif not str(build.db_sha256 or "").strip():
                    # 登记的哈希是空的（记录被改坏了）：复用的文件和这次新建的逐字节一致才走到这里，
                    # 补上它的哈希，免得新快照继承一个空哈希
                    build.db_sha256 = db_sha
                build.db_path, build.retired_at = str(final), None
                build_reused = reused_file
                report = build.report or report

            imp_id = new_id()
            snap_id = snapshot_id(source_id, [imp_id])
            options_now = upload_options(source.options)
            view = SourceView(
                id=source_id, name=source.name, database=str(final), options=options_now,
                description=source.description or "", snapshot_id=snap_id,
                expected_sha256=build.db_sha256,
            )
            cache = await _introspect_view(view, report)
            _check_probe(cache, report, source_id)

            session.add(source)
            session.add(TableImport(
                id=imp_id, source_id=source_id, seq=last_seq + 1, build_id=bid,
                file_name=(filename or "")[:500], file_size=len(raw), raw_sha256=raw_sha,
                raw_state="kept", status="active", created_at=now, activated_at=now,
            ))
            session.add(SourceSnapshot(
                id=snap_id, source_id=source_id, imports=[imp_id], db_path=str(final),
                db_sha256=build.db_sha256, schema_cache=cache, created_at=now,
            ))
            await session.execute(
                update(TableImport)
                .where(TableImport.source_id == source_id, TableImport.status == "active",
                       TableImport.id != imp_id)
                .values(status="superseded")
            )
            source.kind, source.origin, source.readonly = "sqlite", "upload", True
            source.host = source.port = source.username = source.password = None
            if options_now != (source.options or {}):
                source.options = options_now
            # database 只作显示：查询一律从快照解析
            source.database = str(final)
            source.current_snapshot_id = snap_id
            source.schema_cache, source.schema_synced_at = cache, now
            await session.commit()
    except BaseException:
        try:
            await session.rollback()
        except Exception:  # noqa: BLE001 - 回滚失败也要先把文件收拾掉，再抛原来的异常
            logger.exception("上传表格发布失败后回滚出错")
        # 这次新建的文件没有记录指着，留着就是孤儿。复用的文件、早就存在的原件不动
        if linked:
            _remove_db_files(final)
        if raw_new:
            raw_store.purge_raw(raw_sha)
        if fresh:
            _remove_db_files(tmp)
            _prune_build_dirs(final)
        raise
    finally:
        _remove_db_files(tmp)
        # 指针换了（或没换成）：按源 id 清掉缓存的引擎，下次按新的快照重建
        await engines.invalidate(source_id)

    try:
        await gc(session)
    except Exception:  # noqa: BLE001 - 回收失败不影响这次导入，下次发布或重启再收
        logger.exception("上传表格发布后的回收失败")
    return ImportResult(import_id=imp_id, snapshot_id=snap_id, build_id=bid,
                        build_reused=build_reused, report=report)


# --------------------------------------------------------------------------
# 删除数据源
# --------------------------------------------------------------------------


async def retire_source(session: AsyncSession, source_id: str) -> None:
    """数据源删除时调用（不提交）：它的导入记录全部置 retired。文件交给回收，被运行引用的会保留。"""
    await session.execute(
        update(TableImport).where(TableImport.source_id == source_id, TableImport.status != "retired")
        .values(status="retired")
    )


async def purge_import_raw(
    session: AsyncSession, imp: TableImport, *, reason: str, signed_by: str | None
) -> tuple[bool, list[TableImport]]:
    """清除一次导入的原件（提交）。返回 (是否删了文件, 一并标成已清除的其他导入记录)。

    **按内容清除。** 原件按哈希只存一份，同一个文件传过两次、传给过两个源，共用的是同一个
    文件。清除的目的是让含敏感内容的原件从服务器上消失（D13），所以文件一定删；同一份内容
    下其他仍保留原件的导入一并标成已清除，记录里写明是随哪次导入清除的，回执逐条列出。
    持版本存储的锁：发布正在把同一份原件登记给新导入时，不能在它提交前把文件删掉。
    """
    async with _store_lock():
        others = list((await session.execute(
            select(TableImport).where(
                TableImport.raw_sha256 == imp.raw_sha256, TableImport.raw_state == "kept",
                TableImport.id != imp.id,
            ).order_by(TableImport.source_id, TableImport.seq)
        )).scalars())
        record = {
            "at": utcnow().isoformat(), "reason": reason,
            "signed_by": (signed_by or "").strip() or None, "signed_by_verified": False,
        }
        imp.raw_state, imp.purged = "purged", record
        for other in others:
            other.raw_state, other.purged = "purged", {**record, "via_import": imp.id}
        await session.commit()
        deleted = await asyncio.to_thread(lambda: raw_store.purge_raw(imp.raw_sha256, prune=True))
    return deleted, others


# --------------------------------------------------------------------------
# 回收
# --------------------------------------------------------------------------


def _snapshots_in(versions: Any) -> set[str]:
    """runs.data_versions 里引用的快照 id。形状是 {source_id: {"snapshot": id, ...}}，也认直接写 id 的。"""
    found: set[str] = set()
    if not isinstance(versions, dict):
        return found
    for value in versions.values():
        if isinstance(value, str) and value:
            found.add(value)
        elif isinstance(value, dict):
            sid = value.get("snapshot") or value.get("snapshot_id")
            if isinstance(sid, str) and sid:
                found.add(sid)
    return found


def _delete_store_file(path: str) -> None:
    """删一个版本库文件。只删上传目录里的：记录被改坏了也不能删到别处去。"""
    if not path or not _under(path, settings.uploads_dir):
        if path:
            logger.warning("回收跳过了上传目录以外的文件：%s", path)
        return
    try:
        _remove_db_files(Path(path))
    except OSError as e:
        logger.warning("回收没能删掉 %s：%s", path, e)


async def gc(session: AsyncSession) -> None:
    """回收没人用的版本。发布后、删除数据源后、启动时各调一次。

    引用集合 = 任何状态的运行（排队、执行中、等审批、已结束）在 runs.data_versions 里引用的
    快照 ∪ 各源当前的快照。受保护的是这些快照、它们的导入记录、构建库，以及这些导入记录里
    还留着的原件。其余的：记录先标 retired 并提交，再删文件——删到一半出错，下次回收会接着删
    （已 retired、没被保护的文件照样会被处理）。
    """
    async with _store_lock():
        referenced: set[str] = set()
        for (versions,) in (await session.execute(select(Run.data_versions))).all():
            referenced |= _snapshots_in(versions)
        referenced |= {
            sid for (sid,) in (await session.execute(
                select(DataSource.current_snapshot_id).where(DataSource.current_snapshot_id.is_not(None))
            )).all()
        }
        snaps = list((await session.execute(select(SourceSnapshot))).scalars())
        imports = list((await session.execute(select(TableImport))).scalars())
        builds = list((await session.execute(select(TableBuild))).scalars())

        kept_imports: set[str] = {i.id for i in imports if i.status == "active"}
        kept_paths: set[str] = set()
        for snap in snaps:
            if snap.id in referenced:
                kept_imports |= {str(x) for x in snap.imports or []}
                kept_paths.add(snap.db_path)
        kept_builds = {i.build_id for i in imports if i.id in kept_imports}
        kept_raw = {i.raw_sha256 for i in imports
                    if i.id in kept_imports and i.raw_state == "kept" and i.raw_sha256}
        build_paths = {b.db_path for b in builds}

        now = utcnow()
        files: list[str] = []
        raws: set[str] = set()
        touched_sources: set[str] = set()
        for snap in snaps:
            if snap.id in referenced:
                continue
            if snap.retired_at is None:
                snap.retired_at = now
                touched_sources.add(snap.source_id)
            # 期 1 的快照库就是构建库，由下面的构建处理；以后「按期累积」物化出的独立文件在这里删
            if snap.db_path not in build_paths and snap.db_path not in kept_paths:
                files.append(snap.db_path)
        for build in builds:
            if build.id in kept_builds or build.db_path in kept_paths:
                continue
            if build.retired_at is None:
                build.retired_at = now
                touched_sources.add(build.source_id)
            files.append(build.db_path)
        for imp in imports:
            if imp.id in kept_imports:
                continue
            if imp.status != "retired":
                imp.status = "retired"
            if imp.raw_state == "kept" and imp.raw_sha256 and imp.raw_sha256 not in kept_raw:
                imp.raw_state = "purged"
                imp.purged = {"at": now.isoformat(), "reason": "版本已回收，原件随之删除",
                              "signed_by": None, "auto": True}
                raws.add(imp.raw_sha256)
        await session.commit()

        def _delete() -> None:
            for path in files:
                if Path(path).exists():
                    _delete_store_file(path)
            for sha in raws:
                raw_store.purge_raw(sha, prune=True)

        await asyncio.to_thread(_delete)
    # 被删掉文件的快照可能还有引擎开着文件句柄：已删除的文件要等句柄关了才真正释放空间
    for source_id in touched_sources:
        await engines.invalidate(source_id)


# --------------------------------------------------------------------------
# 启动：清理半成品、迁移老上传、回收
# --------------------------------------------------------------------------


def _sweep_tmp() -> int:
    """删掉上次崩溃留下的 *.tmp-* 半成品（库和原件都是先写临时文件再 link）。"""
    removed = 0
    for root in (tables_root(), raw_store.raw_root()):
        if not root.is_dir():
            continue
        for path in root.rglob(f"*{_TMP}*"):
            if path.is_file():
                path.unlink(missing_ok=True)
                removed += 1
    return removed


async def _sweep_orphans(session: AsyncSession) -> None:
    """删掉没有记录的文件：发布时 link 之后、提交之前进程没了，库文件或原件就成了孤儿。"""
    build_ids = {bid for (bid,) in (await session.execute(select(TableBuild.id))).all()}
    kept_raw = {
        sha for (sha,) in (await session.execute(
            select(TableImport.raw_sha256).where(TableImport.raw_state == "kept")
        )).all() if sha
    }

    def _sweep() -> list[Path]:
        orphans: list[Path] = []
        root = tables_root()
        if root.is_dir():
            for path in root.glob("*/builds/*.db"):
                if path.stem not in build_ids:
                    orphans.append(path)
        raw_root = raw_store.raw_root()
        if raw_root.is_dir():
            for path in raw_root.glob("*/*"):
                if path.is_file() and path.name not in kept_raw and _TMP not in path.name:
                    orphans.append(path)
        for path in orphans:
            _remove_db_files(path)
        # 空目录：被拒收的新源留下的 tables/<源 id>/builds/、清除原件后的 raw/<前两位>/。
        # 启动时持着锁、还没开始接请求，删了也不会和正在写的发布撞上（发布会按需重建目录）
        if root.is_dir():
            for builds in root.glob("*/builds"):
                if builds.is_dir() and raw_store.remove_empty_dir(builds):
                    orphans.append(builds)
            for folder in root.iterdir():
                if folder.is_dir() and raw_store.remove_empty_dir(folder):
                    orphans.append(folder)
        if raw_root.is_dir():
            for folder in raw_root.iterdir():
                if folder.is_dir() and raw_store.remove_empty_dir(folder):
                    orphans.append(folder)
        return orphans

    for path in await asyncio.to_thread(_sweep):
        logger.info("清理了没有记录的上传文件（或空目录）：%s", path)


def _legacy_candidate(kind: str | None, database: str | None) -> str | None:
    """老上传的库：路径直接落在 uploads/tables/ 下、文件名以 .db 结尾。返回 realpath。"""
    if (kind or "").lower() != "sqlite" or not database:
        return None
    real = os.path.realpath(database)
    if not real.endswith(".db") or os.path.dirname(real) != os.path.realpath(tables_root()):
        return None
    return real


def _prepare_legacy(path: str) -> tuple[str, int]:
    """老库定稿：合并可能残留的日志，改成 0444，返回 (sha256, 字节数)。"""
    p = Path(path)
    if Path(f"{path}-wal").exists() or Path(f"{path}-journal").exists():
        _settle_journal(p)
    os.chmod(p, 0o444)
    return raw_store.sha256_file(p), p.stat().st_size


async def _migrate_legacy(session: AsyncSession) -> int:
    """给迁移前的上传源补上 v0 版本：构建、导入记录（没有原件）、快照，origin 改成 upload。

    幂等：已经是 upload 的源跳过；每个源一个事务，中途失败不会留下半套记录。

    一个源迁不过去（文件属主不对 chmod 报错、日志合并不了、提交失败）不该挡住别的源和整个
    启动。回滚会让会话里所有 ORM 对象过期，之后再读它们的属性就要回库懒加载——在异步会话里
    这一步直接抛 MissingGreenlet。所以先取纯值，每个源在循环里重新 get，日志只用本地变量。
    """
    candidates = (await session.execute(
        select(DataSource.id, DataSource.name, DataSource.kind, DataSource.database).where(
            DataSource.kind == "sqlite",
            or_(DataSource.origin.is_(None), DataSource.origin != "upload"),
        ).order_by(DataSource.name)
    )).all()
    migrated = 0
    for source_id, name, kind, database in candidates:
        path = _legacy_candidate(kind, database)
        if path is None:
            continue
        if not os.path.isfile(path):
            logger.warning("上传表格「%s」的数据文件不存在，未迁移：%s", name, path)
            continue
        try:
            row = await session.get(DataSource, source_id)
            if row is None or row.origin == "upload":
                continue
            file_sha, size = await asyncio.to_thread(_prepare_legacy, path)
            bid = legacy_build_id(source_id, file_sha)
            now = utcnow()
            options = {"legacy": True}
            if await session.get(TableBuild, bid) is None:
                session.add(TableBuild(
                    id=bid, source_id=source_id, raw_sha256="", options_sha256=_sha_json(options),
                    engine_ver=LEGACY_ENGINE_VER, options=options, db_path=path, db_sha256=file_sha,
                    report={}, created_at=now,
                ))
            last_seq = (await session.execute(
                select(func.max(TableImport.seq)).where(TableImport.source_id == source_id)
            )).scalar() or 0
            imp_id = new_id()
            snap_id = snapshot_id(source_id, [imp_id], LEGACY_ENGINE_VER)
            source_options = upload_options(row.options)
            cache = dict(row.schema_cache or {})
            if not cache.get("tables"):
                # 没有可用的结构缓存：上传源不能再手动探查，迁移时补探一次
                view = SourceView(id=source_id, name=name, database=path, options=source_options,
                                  description=row.description or "", snapshot_id=snap_id,
                                  expected_sha256=file_sha)
                try:
                    cache = await _introspect_view(view, {})
                except Exception as e:  # noqa: BLE001 - 探不到就带着空结构迁过去，重传可修
                    logger.warning("迁移上传表格「%s」时探查结构失败：%s", name, e)
            session.add(TableImport(
                id=imp_id, source_id=source_id, seq=last_seq + 1, build_id=bid, file_name="",
                file_size=size, raw_sha256="", raw_state="absent", status="active",
                created_at=now, activated_at=now,
            ))
            session.add(SourceSnapshot(
                id=snap_id, source_id=source_id, imports=[imp_id], db_path=path, db_sha256=file_sha,
                schema_cache=cache, created_at=now,
            ))
            row.origin, row.readonly, row.current_snapshot_id = "upload", True, snap_id
            row.schema_cache = cache
            if source_options != (row.options or {}):
                row.options = source_options
            await session.commit()
            migrated += 1
        except Exception:  # noqa: BLE001 - 一个源迁不过去不该挡住别的源和整个启动
            try:
                await session.rollback()
            except Exception:  # noqa: BLE001 - 回滚也失败时照样记下原因、接着处理下一个
                logger.exception("迁移上传表格「%s」失败后回滚出错", name)
            logger.exception("迁移上传表格「%s」失败", name)
        finally:
            await engines.invalidate(source_id)
    return migrated


async def _lock_manual_in_uploads(session: AsyncSession) -> int:
    """升级前就指向上传目录、又没迁移成上传源的手工 SQLite 源：强制改成只读（提交）。返回改了几个。

    新建、修改时指向上传目录会被拒（api/datasources.py），但升级前已经登记的这类源（路径在
    uploads/tables 更深的目录里、带连接参数的别名……）迁移认不出来，原样留着可写，就能往归版本底座管的
    目录里写。方案要求迁移时强制只读并标出：这里改只读、逐个记一条警告日志；之后想改回可写，修改接口
    会拒（同样按 under_uploads 判断）。
    """
    rows = list((await session.execute(
        select(DataSource).where(
            DataSource.kind == "sqlite", DataSource.readonly.is_(False),
            or_(DataSource.origin.is_(None), DataSource.origin != "upload"),
        )
    )).scalars())
    locked = [row for row in rows if under_uploads(row.database)]
    for row in locked:
        row.readonly = True
        logger.warning("手工登记的 SQLite 数据源「%s」指向上传表格的存放目录（%s），已强制改为只读",
                       row.name, row.database)
    if locked:
        await session.commit()
        for row in locked:
            await engines.invalidate(row.id)
    return len(locked)


async def _disable_manual_app_files(session: AsyncSession) -> int:
    """升级前登记的、指向 AgentLab 自己数据文件的手工 SQLite 源：停用并改成只读（提交）。返回改了几个。

    新建、修改时会被拒（api/datasources.py，按 reaches_app_files 判断）。这类登记没有正当用途：
    只读也会把元数据库整个交给模型，可写就能改上传源的连接字段和快照登记。只改只读不够，所以停用；
    之后除了改路径和删除，修改接口一律拒。
    """
    rows = list((await session.execute(
        select(DataSource).where(
            DataSource.kind == "sqlite",
            or_(DataSource.enabled.is_(True), DataSource.readonly.is_(False)),
            or_(DataSource.origin.is_(None), DataSource.origin != "upload"),
        )
    )).scalars())
    hit = [row for row in rows if reaches_app_files(row.database)]
    for row in hit:
        row.readonly, row.enabled = True, False
        logger.warning("手工登记的 SQLite 数据源「%s」指向 AgentLab 自己的数据文件（%s），已停用并改为只读",
                       row.name, row.database)
    if hit:
        await session.commit()
        for row in hit:
            await engines.invalidate(row.id)
    return len(hit)


async def startup(session: AsyncSession) -> None:
    """服务启动时调用一次：清理半成品和孤儿文件、迁移老上传、把指向上传目录的手工源改成只读、
    停用指向应用自己数据文件的手工源、回收。"""
    async with _store_lock():
        swept = await asyncio.to_thread(_sweep_tmp)
        if swept:
            logger.info("清理了 %d 个上次未完成的上传临时文件", swept)
        migrated = await _migrate_legacy(session)
        if migrated:
            logger.info("为 %d 个已有的上传表格建立了初始版本", migrated)
        try:
            await _lock_manual_in_uploads(session)
        except Exception:  # noqa: BLE001 - 改不成只读不该挡住启动；下次启动再试
            await session.rollback()
            logger.exception("把指向上传目录的手工数据源改成只读时出错")
        try:
            await _disable_manual_app_files(session)
        except Exception:  # noqa: BLE001 - 同上
            await session.rollback()
            logger.exception("停用指向应用数据文件的手工数据源时出错")
        await _sweep_orphans(session)
    await gc(session)
