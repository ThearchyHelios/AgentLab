"""记录表的文件层（docs/design/normal-user-apps.md N8、§3.7）：一个表版本一个 SQLite 文件，只许追加。

**文件。** data/records/<table_key>.v<version>.db，里面只有一张业务表 records：系统列（records.SYSTEM_COLUMNS）、
业务列（「填什么」卡片生成），外加一列不对外的 _action_key（幂等键，唯一）。查询走连接上的同名临时视图
（view_select），视图不露 _action_key。

**写。** 只有 append 一条路：单进程锁（_WRITE_LOCK）里开一个短事务，先按 action_key 查——同一个动作第二次来交回
第一次写下的那一行，不再写；更正是一次新的追加，带 _supersedes 指向被更正的行，原行不改。_seq 是 SQLite 的 rowid
（INTEGER PRIMARY KEY），只增不减：从不删行，也就不会复用。

**读。** 高水位（high_water）是现在最大的 _seq。一次运行在开始时记下它（engine/runner._data_versions），之后这次
运行查到的都是 _seq ≤ 高水位、在那一刻没被更正过的行：行只追加、不改，所以同一次运行查几遍都是同一份。

这里的函数都是同步的（sqlite3 直连、关 DQS），调用方放进线程。表结构的登记、数据源在 records/tables.py。
"""
from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.core.config import settings
from app.data.engine import open_checked_sqlite
from app.db.base import new_id
from app.records import SYSTEM_COLUMNS

#: 文件里那张业务表的名字。看板的 SQL 写 FROM records
TABLE = "records"
#: 不对外的幂等键列：视图不露它，写 main.records 的 SQL 被只读守卫拒掉
ACTION_KEY_COLUMN = "_action_key"
#: 表单字段类型 → 列类型。多选、文件存 JSON 文字
COLUMN_TYPES = {"string": "TEXT", "number": "REAL", "integer": "INTEGER", "boolean": "INTEGER", "array": "TEXT",
                "file": "TEXT"}
#: 列名最长几个字
NAME_MAX = 64

_KEY = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
#: 列名里不许出现的字符：引号（拼进 SQL 的标识符）、控制字符
_BAD_NAME = re.compile(r"[\"`\[\]\x00-\x1f\x7f]")
#: 所有写入（建表、加列、追加）共用的锁。写都是几毫秒的短事务，不按表分锁也不会互相拖住
_WRITE_LOCK = threading.Lock()


class RecordsRefused(ValueError):
    """这一次写入不收：code 是问题编号（records.*，白话在 app/plain.py），message 直接给人看。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class Column:
    """一个业务列：名字（就是表单字段名）、中文名、类型（COLUMN_TYPES 的键）、必填、敏感（进数据源的遮罩列）。"""

    name: str
    label: str = ""
    type: str = "string"
    required: bool = False
    sensitive: bool = False

    def out(self) -> dict[str, Any]:
        return {"name": self.name, "label": self.label or self.name, "type": self.type, "required": self.required,
                "sensitive": self.sensitive}


def column_problem(name: Any) -> str | None:
    """列名合不合规：返回原因，合规返回 None。下划线开头的留给系统列。"""
    if not isinstance(name, str) or not name.strip():
        return "字段名不能为空"
    if name != name.strip():
        return f"字段名「{name}」前后不能有空格"
    if len(name) > NAME_MAX:
        return f"字段名「{name[:20]}…」超过 {NAME_MAX} 个字"
    if name.startswith("_"):
        return f"字段名「{name}」不能以下划线开头：下划线开头的留给系统列"
    if _BAD_NAME.search(name):
        return f"字段名「{name}」里不能有引号、方括号或控制字符"
    return None


def parse_columns(raw: Any) -> list[Column]:
    """records_tables.columns（或调用方交来的列定义）→ Column。类型不认识的、名字不合规的抛 RecordsRefused。"""
    out: list[Column] = []
    seen: set[str] = set()
    for item in raw or []:
        if not isinstance(item, dict):
            raise RecordsRefused("records.column_invalid", "列定义需要是对象")
        name = item.get("name")
        if problem := column_problem(name):
            raise RecordsRefused("records.column_invalid", problem)
        if name.lower() in seen:
            raise RecordsRefused("records.column_invalid", f"字段名「{name}」重复了")
        seen.add(name.lower())
        kind = str(item.get("type") or "string")
        if kind not in COLUMN_TYPES:
            raise RecordsRefused("records.column_invalid", f"字段「{name}」的类型「{kind}」不能存进记录表")
        out.append(Column(name=name, label=str(item.get("label") or name), type=kind,
                          required=bool(item.get("required")), sensitive=bool(item.get("sensitive"))))
    return out


# --------------------------------------------------------------------------
# 路径
# --------------------------------------------------------------------------


def records_root() -> Path:
    """记录表文件的根目录。按调用时的 settings 现算：测试会换数据目录。"""
    return settings.data_dir / "records"


def table_path(table_key: str, version: int) -> Path:
    """data/records/<table_key>.v<version>.db。键只认字母、数字、下划线、连字符，挡住路径穿越。"""
    if not _KEY.match(table_key or ""):
        raise ValueError("记录表的键格式不对")
    if not isinstance(version, int) or version < 1:
        raise ValueError("记录表的版本号需要是正整数")
    return records_root() / f"{table_key}.v{version}.db"


def _q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _lit(value: str) -> str:
    """SQL 文字字面量。只用于临时视图里拼看的人的名字：视图定义不能带参数。"""
    return "'" + str(value).replace("'", "''") + "'"


def _rw(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise RecordsRefused("records.table_missing", "记录表的数据文件不见了，请联系管理员")
    return open_checked_sqlite(str(path), readonly=False)


def _ro(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise RecordsRefused("records.table_missing", "记录表的数据文件不见了，请联系管理员")
    return open_checked_sqlite(str(path), readonly=True)


# --------------------------------------------------------------------------
# 建表、加列
# --------------------------------------------------------------------------


def create_file(path: Path, columns: list[Column]) -> None:
    """新建一个表版本的文件。路径上已有文件时：没有任何行（上次建了一半、登记没提交）就重建，有行就拒绝——
    有数据的文件绝不覆盖。"""
    with _WRITE_LOCK:
        if path.exists():
            if _row_count(path) > 0:
                raise RecordsRefused("records.file_exists", f"记录表文件 {path.name} 已经有数据，不能重建")
            path.unlink()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.tmp-{new_id()[:12]}")
        conn = open_checked_sqlite(str(tmp), readonly=False)
        try:
            business = "".join(f", {_q(c.name)} {COLUMN_TYPES[c.type]}" for c in columns)
            conn.execute(
                f"CREATE TABLE {_q(TABLE)} ("
                "_seq INTEGER PRIMARY KEY, _id TEXT NOT NULL UNIQUE, _submitted_by TEXT, _submitted_at TEXT NOT NULL, "
                "_run_id TEXT, _supersedes TEXT, _schema_version INTEGER NOT NULL, "
                f"{ACTION_KEY_COLUMN} TEXT UNIQUE{business})")
            conn.execute(f"CREATE INDEX ix_records_supersedes ON {_q(TABLE)} (_supersedes)")
            conn.execute(f"CREATE INDEX ix_records_submitted_by ON {_q(TABLE)} (_submitted_by)")
            conn.commit()
        finally:
            conn.close()
        os.replace(tmp, path)


def add_columns(path: Path, columns: list[Column]) -> None:
    """兼容的改动（只加可选字段）：在同一个版本上 ALTER TABLE ADD COLUMN。已有的列跳过（重做一次也没事）。"""
    if not columns:
        return
    with _WRITE_LOCK:
        conn = _rw(path)
        try:
            have = {str(r[1]).lower() for r in conn.execute(f"PRAGMA table_info({_q(TABLE)})")}
            for c in columns:
                if c.name.lower() not in have:
                    conn.execute(f"ALTER TABLE {_q(TABLE)} ADD COLUMN {_q(c.name)} {COLUMN_TYPES[c.type]}")
            conn.commit()
        finally:
            conn.close()


def _row_count(path: Path) -> int:
    try:
        conn = open_checked_sqlite(str(path), readonly=True)
    except sqlite3.Error:
        return 0
    try:
        return int(conn.execute(f"SELECT count(*) FROM {_q(TABLE)}").fetchone()[0])
    except sqlite3.Error:
        return 0
    finally:
        conn.close()


# --------------------------------------------------------------------------
# 追加
# --------------------------------------------------------------------------


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


_TYPE_TEXT = {"number": "数", "integer": "整数", "boolean": "是或否", "array": "多选（列表）", "file": "文件"}
_INTEGER = re.compile(r"[+-]?\d+")
_NUMBER = re.compile(r"[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?")


def coerce(column: Column, value: Any) -> Any:
    """一个值按列类型收好，能存进 SQLite 的样子。模板渲染出来的一律是文字（"12.5"、"true"、JSON），在这里收；
    收不成抛 RecordsRefused（records.value_invalid），说清是哪个字段。空值（None、空串）交回 None。"""
    if value is None or (isinstance(value, str) and not value.strip() and column.type != "string"):
        return None
    if isinstance(value, str) and column.type == "string":
        return value
    bad = RecordsRefused("records.value_invalid",
                         f"字段「{column.label or column.name}」应为{_TYPE_TEXT.get(column.type, '文字')}，"
                         f"实际是「{str(value)[:80]}」")
    if column.type == "string":
        if isinstance(value, (dict, list)):
            return json.dumps(value, ensure_ascii=False)
        return str(value)
    if column.type in ("number", "integer"):
        number: Any = None
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            number = value
        elif isinstance(value, str):
            text = value.strip()
            number = int(text) if _INTEGER.fullmatch(text) else float(text) if _NUMBER.fullmatch(text) else None
        if number is None or (isinstance(number, float) and not math.isfinite(number)):
            raise bad
        if column.type == "integer":
            if isinstance(number, float) and not number.is_integer():
                raise bad
            return int(number)
        return number
    if column.type == "boolean":
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, str) and value.strip().lower() in ("true", "false"):
            return int(value.strip().lower() == "true")
        raise bad
    # 多选、文件：存 JSON 文字
    parsed = value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except ValueError:
            raise bad from None
    if column.type == "array" and not isinstance(parsed, list):
        raise bad
    if column.type == "file":
        from app.apps.spec import FILE_REF_KEYS

        if not (isinstance(parsed, dict) and isinstance(parsed.get("file_id"), str)
                and set(parsed) <= set(FILE_REF_KEYS)):
            raise bad
    return json.dumps(parsed, ensure_ascii=False, sort_keys=True)


@dataclass(frozen=True)
class Appended:
    seq: int
    row_id: str
    submitted_at: str
    #: 同一个动作键已经写过：交回的是第一次写下的那一行，这次没有再写
    duplicate: bool
    values: dict[str, Any]

    def receipt(self) -> dict[str, Any]:
        return {"_seq": self.seq, "_id": self.row_id, "_submitted_at": self.submitted_at,
                "duplicate": self.duplicate}


def append(path: Path, columns: list[Column], values: Any, *, version: int, run_id: str | None, actor: str | None,
           action_key: str, supersedes: str | None = None) -> Appended:
    """写一行。同一个 action_key 第二次来交回第一次的那一行（duplicate=True），不再写。

    values 只认表里的业务列：多出来的字段、必填没填、类型收不成都拒（RecordsRefused），一行都不写。
    supersedes 是被更正的那一行的 _id：它得在这张表里、而且还没被更正过（更正要改最新的那一行）。"""
    if not action_key:
        raise RecordsRefused("records.action_key_missing", "写记录表需要动作键，只能经调用工具节点写入")
    if not isinstance(values, dict):
        raise RecordsRefused("records.value_invalid", "要写入的内容需要是一组字段和值")
    by_name = {c.name: c for c in columns}
    unknown = [k for k in values if k not in by_name]
    if unknown:
        raise RecordsRefused("records.column_unknown",
                             f"记录表里没有这些字段：{'、'.join(map(str, unknown[:5]))}。表单改过的话，请重新发布应用")
    row = {c.name: coerce(c, values.get(c.name)) for c in columns}
    missing = [c.label or c.name for c in columns if c.required and row[c.name] in (None, "")]
    if missing:
        raise RecordsRefused("records.required_missing", f"必填的字段没有值：{'、'.join(missing)}")
    with _WRITE_LOCK:
        conn = _rw(path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            done = conn.execute(f"SELECT _seq, _id, _submitted_at FROM {_q(TABLE)} WHERE {ACTION_KEY_COLUMN} = ?",
                                (action_key,)).fetchone()
            if done is not None:
                conn.rollback()
                return Appended(seq=int(done[0]), row_id=str(done[1]), submitted_at=str(done[2]), duplicate=True,
                                values=_row_values(path, columns, int(done[0])))
            if supersedes:
                target = conn.execute(f"SELECT _seq FROM {_q(TABLE)} WHERE _id = ?", (supersedes,)).fetchone()
                if target is None:
                    raise RecordsRefused("records.supersede_missing", "要更正的那一行不在这张记录表里")
                later = conn.execute(f"SELECT 1 FROM {_q(TABLE)} WHERE _supersedes = ? LIMIT 1",
                                     (supersedes,)).fetchone()
                if later is not None:
                    raise RecordsRefused("records.supersede_twice", "这一行已经被更正过了，请在最新的那一行上更正")
            row_id, at = new_id(), _now()
            names = ["_id", "_submitted_by", "_submitted_at", "_run_id", "_supersedes", "_schema_version",
                     ACTION_KEY_COLUMN, *row]
            params = [row_id, actor, at, run_id, supersedes or None, version, action_key, *row.values()]
            cur = conn.execute(f"INSERT INTO {_q(TABLE)} ({', '.join(_q(n) for n in names)}) "
                               f"VALUES ({', '.join('?' for _ in names)})", params)
            seq = int(cur.lastrowid or 0)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()
    return Appended(seq=seq, row_id=row_id, submitted_at=at, duplicate=False, values=row)


def _row_values(path: Path, columns: list[Column], seq: int) -> dict[str, Any]:
    conn = _ro(path)
    try:
        names = [c.name for c in columns]
        got = conn.execute(f"SELECT {', '.join(_q(n) for n in names) or '1'} FROM {_q(TABLE)} WHERE _seq = ?",
                           (seq,)).fetchone()
        return dict(zip(names, got)) if got is not None and names else {}
    finally:
        conn.close()


# --------------------------------------------------------------------------
# 读
# --------------------------------------------------------------------------


def high_water(path: Path) -> int:
    """现在最大的 _seq；空表为 0。"""
    conn = _ro(path)
    try:
        got = conn.execute(f"SELECT max(_seq) FROM {_q(TABLE)}").fetchone()
        return int(got[0] or 0)
    finally:
        conn.close()


def lookup(path: Path, columns: list[Column], action_key: str) -> dict[str, Any] | None:
    """按动作键找写下的那一行（查询能力 records_lookup 用）：没有为 None。"""
    conn = _ro(path)
    try:
        got = conn.execute(f"SELECT _seq, _id, _submitted_at FROM {_q(TABLE)} WHERE {ACTION_KEY_COLUMN} = ?",
                           (action_key,)).fetchone()
    finally:
        conn.close()
    if got is None:
        return None
    return {"_seq": int(got[0]), "_id": str(got[1]), "_submitted_at": str(got[2]), "duplicate": True}


def view_select(columns: list[Column], *, high_water: int | None, scope: str, viewer: str | None,
                schema: str = "main") -> str:
    """临时视图（和看板、记录列表）看到的那一份：

    - _seq ≤ 高水位（None 为不限，现在有的都算）；
    - 没被更正过：在高水位之内没有一行指向它（_supersedes）。按高水位判，不靠可改的标记——原行永远不改；
    - 行级过滤：scope=all 全部，mine 只看 _submitted_by = viewer 的，none 一行都不给。

    不露 _action_key。列名照 SYSTEM_COLUMNS + 业务列的顺序。"""
    cols = ", ".join(f"r.{_q(n)}" for n in (*SYSTEM_COLUMNS, *(c.name for c in columns)))
    src = f"{_q(schema)}.{_q(TABLE)}"
    where = []
    if high_water is not None:
        seq = int(high_water)
        where.append(f"r._seq <= {seq}")
        newer = f" AND s._seq <= {seq}"
    else:
        newer = ""
    where.append(f"NOT EXISTS (SELECT 1 FROM {src} AS s WHERE s._supersedes = r._id{newer})")
    if scope == "mine":
        where.append(f"r._submitted_by = {_lit(viewer)}" if viewer else "0")
    elif scope != "all":
        where.append("0")
    return f"SELECT {cols} FROM {src} AS r WHERE {' AND '.join(where)}"


def view_prelude(columns: list[Column], *, high_water: int | None, scope: str, viewer: str | None) -> tuple[str, ...]:
    """查询连接上建同名临时视图的两条语句。SQLite 解析不带库名的表名时先找 temp 库，于是 FROM records 查到的就是
    这份视图；直接写 main.records 的 SQL 由只读守卫拒掉（data/engine.run_query 的 forbid_schemas）。"""
    return (f"DROP VIEW IF EXISTS temp.{_q(TABLE)}",
            f"CREATE TEMP VIEW {_q(TABLE)} AS "
            + view_select(columns, high_water=high_water, scope=scope, viewer=viewer))


def rows(path: Path, columns: list[Column], *, scope: str, viewer: str | None, history: bool = False,
         before: int | None = None, limit: int = 50) -> tuple[list[dict[str, Any]], bool]:
    """记录列表（records_list、GET /api/apps/{id}/records）：现在的数据，按看的人过滤，_seq 倒序。

    history=False 只给当前有效的行；True 连被更正过的也给，标 superseded。before 是游标（只要 _seq 更小的）。
    交回 (行, 后面还有没有)。"""
    limit = max(1, min(int(limit), 200))
    names = [c.name for c in columns]
    src = f"main.{_q(TABLE)}"
    where = ["1"]
    params: list[Any] = []
    if scope == "mine":
        where.append("r._submitted_by = ?")
        params.append(viewer)
    elif scope != "all":
        return [], False
    if not history:
        where.append(f"NOT EXISTS (SELECT 1 FROM {src} AS s WHERE s._supersedes = r._id)")
    if before is not None:
        where.append("r._seq < ?")
        params.append(int(before))
    cols = ", ".join(f"r.{_q(n)}" for n in (*SYSTEM_COLUMNS, *names))
    sql = (f"SELECT {cols}, EXISTS (SELECT 1 FROM {src} AS s WHERE s._supersedes = r._id) "
           f"FROM {src} AS r WHERE {' AND '.join(where)} ORDER BY r._seq DESC LIMIT ?")
    conn = _ro(path)
    try:
        got = conn.execute(sql, [*params, limit + 1]).fetchall()
    finally:
        conn.close()
    out: list[dict[str, Any]] = []
    width = len(SYSTEM_COLUMNS)
    for row in got[:limit]:
        system = dict(zip(SYSTEM_COLUMNS, row[:width]))
        values = {n: _shown(c, v) for n, c, v in zip(names, columns, row[width:width + len(names)])}
        out.append({**system, "superseded": bool(row[-1]), "values": values})
    return out, len(got) > limit


def row_by_id(path: Path, columns: list[Column], row_id: str) -> dict[str, Any] | None:
    """按 _id 取一行（下载附件时核对行级可见用）。"""
    names = [c.name for c in columns]
    cols = ", ".join(_q(n) for n in (*SYSTEM_COLUMNS, *names))
    conn = _ro(path)
    try:
        got = conn.execute(f"SELECT {cols} FROM {_q(TABLE)} WHERE _id = ?", (row_id,)).fetchone()
    finally:
        conn.close()
    if got is None:
        return None
    width = len(SYSTEM_COLUMNS)
    return {**dict(zip(SYSTEM_COLUMNS, got[:width])),
            "values": {n: _shown(c, v) for n, c, v in zip(names, columns, got[width:])}}


def _shown(column: Column, value: Any) -> Any:
    """存的样子 → 给界面的样子：是或否交回布尔，多选、文件解开 JSON。"""
    if value is None:
        return None
    if column.type == "boolean":
        return bool(value)
    if column.type in ("array", "file") and isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value
