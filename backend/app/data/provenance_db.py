"""证据下钻（期 4「推断的来源」）里要碰数据库和数据文件的几步（P4-SPEC 2.3 R12–R16、2.7、2.9）。

识别器（engine/direct_select.py）和清单、格子（data/provenance.py）都是纯函数，不连库；这里收拢剩下的：
取数据文件记录、绑定快照、按主键回查、编译核对、强制重算哈希、行级核对、导入记录上的当前状态。

**为什么回查、行级核对都经 EngineCache**：查询本身就是经它打开快照的（immutable 只读、本进程首次整份核对哈希、
之后每次取引擎和借连接都比文件指纹）。走同一条路，下钻核对过的就是查询读过的那个文件，保证范围也和查询相同
（P4-SPEC 2.6 照实写了这个范围的边界）。编译核对另开一个连接，开前关后各比一次 EngineCache 记下的指纹。

**异常**：SnapshotManifestMismatch、SnapshotTampered、SnapshotMissing 一律原样抛出，由接口一侧按 2.3 的
except 顺序映射成 chain_mismatch / db_tampered / snapshot_gone（api/evidence._snapshot_failure）。这里不吞、
不改判：同一类异常在接口的每一步都要得到同一个结论。

同步的文件操作（编译核对、整份重算哈希）一律放 asyncio.to_thread，不卡事件循环（H9）。
"""
from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.data import engine as engine_mod
from app.data.engine import SnapshotTampered, engines, open_checked_sqlite
from app.data.names import name_key
from app.data.provenance_types import CompileFacts, StateNote, SumEqRule
from app.data.recipe_checks import sum_eq_conditions
from app.data.table_versions import SnapshotMissing, SourceView, bind_snapshot
from app.db.models import SourceSnapshot, TableImport, TableRecipe

#: 数据文件记录在、却已回收或文件不在（绑定之前先认出来，免得取引擎时被当成「文件被改」标红）
_GONE = "数据版本 {sid} 的数据文件已回收或不存在，无法按主键回查"
#: EngineCache 里没有这个快照的指纹（不是快照引擎）：不该出现，按文件不在处理（P4-SPEC 2.9 末尾）
_NOT_PINNED = "数据版本 {sid} 没有核对过的数据文件记录，无法做编译核对"
#: 编译核对前后，文件和 EngineCache 核对过的那份对不上：和借连接时指纹不符同一个说法
_REPLACED = engine_mod._REPLACED


def _q(name: str) -> str:
    """SQL 标识符：一律双引号、内部双引号写两个（和 recipe_checks._q、契约 Recheck 的写法一致）。"""
    return '"' + str(name).replace('"', '""') + '"'


def _fingerprint(path: str) -> engine_mod.Fingerprint | None:
    """文件指纹，口径和 EngineCache 记下的那份完全相同（同一个函数）。单独包一层，测试可以只换这里。"""
    return engine_mod._fingerprint(path)


# --------------------------------------------------------------------------
# 数据文件记录与绑定（R12、R14）
# --------------------------------------------------------------------------


async def snapshot_record(session: AsyncSession, snapshot_id: str) -> SourceSnapshot | None:
    """查询快照的 data_version 对应的数据文件记录（source_snapshots 一行）；没有为 None（R12）。"""
    if not snapshot_id:
        return None
    return await session.get(SourceSnapshot, str(snapshot_id))


def view_of(snapshot: SourceSnapshot, *, source_name: str) -> SourceView:
    """数据文件记录 → 绑定了这个快照的 SourceView，供回查、编译核对、行级核对打开数据文件（R14）。

    用 table_versions.bind_snapshot，和查询解析源的那条路相同（登记哈希为空拒绝；有快照清单的首次绑定时交叉
    核对）。传进去的不是数据源记录，而是一个替身：
    - id = snapshot.source_id：缓存键（cache_key）和查询时相同，首次整份哈希只算一次；
    - name = source_name（查询快照记的数据源名，只用在报错文字里）；description、enabled、options 等给缺省值。
      bind_snapshot 直接读 source.description、bool(source.enabled)，少了会抛 AttributeError。
    所以数据源被删了、改名了也能回查：回查只需要快照文件和它的登记哈希。options 为空，查询时限、遮罩都不进
    这个视图——回查的 SQL 是这里拼的主键查找，遮罩由接口一侧按 R10 先判过。

    记录已回收（retired_at）或文件不在时抛 SnapshotMissing（→ snapshot_gone，不标红）：不先认出来的话，取引擎时
    读不到文件会被 EngineCache 报成 SnapshotTampered，误标成「数据文件被改」。SnapshotManifestMismatch、
    SnapshotMissing（登记哈希为空）原样抛出。
    """
    path = str(snapshot.db_path or "")
    if snapshot.retired_at is not None or not path or not Path(path).is_file():
        raise SnapshotMissing(_GONE.format(sid=str(snapshot.id)[:12]))
    stand_in = SimpleNamespace(
        id=snapshot.source_id, name=source_name, description="", enabled=True, options={},
        current_snapshot_id=None, last_check=None,
    )
    return bind_snapshot(stand_in, snapshot)


# --------------------------------------------------------------------------
# R15：编译核对
# --------------------------------------------------------------------------


async def compile_facts(view: SourceView, sql: str) -> CompileFacts:
    """在查询读过的同一个文件上编译原 SQL（EXPLAIN，不执行），给出与解析无关的三项事实（R15）：
    授权回调报出读了哪些表、字节码里 ResultRow 和 Yield 各有几个。

    步骤：经 EngineCache 取引擎（首次时整份核对哈希）→ **同步**取 engines.pinned(view)（中间没有 await，不会被 LRU
    挤掉）→ 在线程里：比指纹 → 单开 immutable 只读连接（和 EngineCache 打开快照的方式相同）→ 装授权回调、EXPLAIN
    一次 → 关连接 → 再比指纹。开前关后的指纹都要等于 EngineCache 核对过的那份，单开的连接读的才是核对过的文件；
    不等抛 SnapshotTampered。pinned 为 None（不是快照引擎）抛 SnapshotMissing。

    授权回调只观察、不拦（一律放行）：连接本身只读、immutable，EXPLAIN 不执行语句。结论怎么用由接口判（R15 的
    ①②③），这里只报事实。
    """
    await engines.get(view)
    pinned = engines.pinned(view)
    if pinned is None:
        raise SnapshotMissing(_NOT_PINNED.format(sid=str(view.snapshot_id or "")[:12]))
    return await asyncio.to_thread(_compile, str(view.database or ""), pinned, sql)


def _compile(path: str, pinned: engine_mod.Fingerprint, sql: str) -> CompileFacts:
    if _fingerprint(path) != pinned:
        raise SnapshotTampered(_REPLACED)
    reads: set[str] = set()

    def observe(action: int, arg1: str | None, _arg2: str | None, _db: str | None, _trigger: str | None) -> int:
        # SQLITE_READ 的 arg1 是表名（建表时的写法）。子查询、自连接读到的表都会报出来，改名、CTE 报的和合法写法
        # 一样（P4-SPEC 2.4 末尾），所以这只是兜底，主判据仍是识别器
        if action == sqlite3.SQLITE_READ and arg1:
            reads.add(arg1)
        return sqlite3.SQLITE_OK

    conn = open_checked_sqlite(path, readonly=True, immutable=True)
    try:
        conn.set_authorizer(observe)
        opcodes = [str(row[1]) for row in conn.execute("EXPLAIN " + sql)]
    finally:
        conn.close()
    if _fingerprint(path) != pinned:
        raise SnapshotTampered(_REPLACED)
    return CompileFacts(tables=reads, result_rows=opcodes.count("ResultRow"), yields=opcodes.count("Yield"))


# --------------------------------------------------------------------------
# R16：按主键回查、强制重算哈希
# --------------------------------------------------------------------------


async def recheck(view: SourceView, *, table: str, column: str,
                  pk: dict[str, Any]) -> tuple[list[tuple[int, Any]], str, list[Any]]:
    """按主键参数化回查被引用的那一格（R16）：返回 (行, sql, params)，行是 [(rowid, 值), …]。

    SQL 的写法以契约 Recheck 为准：`SELECT rowid, "列" FROM "表" WHERE "主键1" = ? AND "主键2" = ?`，标识符一律双引号，
    `?` 位置占位符，WHERE 和 params 都按 pk 的键序（调用方按冻结表结构 primary_key 的顺序构造 pk）。面板「技术
    细节」原样显示这里返回的 sql、params，所以不加 LIMIT：多出的行本身就是 recheck_multiple 的证据。

    值经 engine._jsonable 规范化，和查询结果进快照时同一个口径（run_query 逐格调它），调用方直接按类型和值比较。
    经 EngineCache 打开（同一个缓存键、同一份哈希核对），三类异常原样抛出。
    """
    if not pk:
        raise ValueError("回查需要主键")
    names = list(pk)
    sql = (f"SELECT rowid, {_q(column)} FROM {_q(table)} WHERE "
           + " AND ".join(f"{_q(name)} = ?" for name in names))
    params = [pk[name] for name in names]
    engine = await engines.get(view)
    async with engine.connect() as conn:
        result = await conn.exec_driver_sql(sql, tuple(params))
        rows = [(int(r[0]), engine_mod._jsonable(r[1])) for r in result.fetchall()]
    return rows, sql, params


async def rehash(view: SourceView) -> bool:
    """在线程里整份重算数据文件的 sha256，等于 view.expected_sha256 返回 True（R16 的强制重算）。

    不经 EngineCache 的指纹捷径：回查的值和快照对不上时才调。EngineCache 只在首次和指纹变化时重算，原地改写同样
    字节数、再还原修改时间的改动在指纹不变期间认不出（P4-SPEC 2.6）；这里强制读一遍，才能把「文件确实被改了」
    和「这条 SQL 的含义与判据认定的不同」分开。文件读不出来、没有登记哈希时返回 False：核对不了就不能当没变。
    """
    expected = str(view.expected_sha256 or "").strip().lower()
    if not expected:
        return False
    try:
        actual = await asyncio.to_thread(engine_mod._file_sha256, str(view.database or ""))
    except OSError:
        return False
    return actual.lower() == expected


# --------------------------------------------------------------------------
# 2.7：关系核对的行级状态
# --------------------------------------------------------------------------


async def row_status(view: SourceView, rule: SumEqRule, snapshot_rowid: int) -> str | None:
    """这一行的关系核对「total = parts 之和」成不成立：passed / mismatch / unverifiable；判不了为 None（2.7）。

    条件取自 recipe_checks.sum_eq_conditions，和导入时的核对（_check_sum_eq）是同一段代码生成的：面板说「这一天
    成立」，和导入时的结论是同一个判断。先判含空值（unverifiable），再判不等（mismatch），其余 passed，与导入时
    「含空值的行不参与比较、计入无法核对」一致。

    snapshot_rowid 是**快照库**的 rowid（cell_source.rowid，并集时是并集 rowid），不是该期构建库的 part_rowid：
    SQL 跑在快照库上。各期的行是逐字节拷进并集库的，同一个条件照样成立（P3-SPEC 2.7）。

    规则里的列在快照库这张表里不存在（不该出现：关系取自格子所在那一期的配方，目标表结构只会多出退役列），或者
    没有这一行时返回 None，不报错。三类数据文件异常原样抛出。
    """
    _nonnull, anynull, mismatch = sum_eq_conditions(rule.total, list(rule.parts), dict(rule.types))
    sql = (f"SELECT CASE WHEN {anynull} THEN 'unverifiable' WHEN {mismatch} THEN 'mismatch' ELSE 'passed' END "
           f"FROM {_q(rule.table)} WHERE rowid = ?")
    engine = await engines.get(view)
    async with engine.connect() as conn:
        present = {name_key(str(r[0])) for r in
                   (await conn.exec_driver_sql("SELECT name FROM pragma_table_xinfo(?)", (rule.table,))).fetchall()}
        if not present or any(name_key(c) not in present for c in [rule.total, *rule.parts]):
            return None
        row = (await conn.exec_driver_sql(sql, (int(snapshot_rowid),))).first()
    return str(row[0]) if row is not None else None


# --------------------------------------------------------------------------
# 导入记录上的当前状态（数据版本一节标「当前状态」的那几项）
# --------------------------------------------------------------------------


async def import_states(session: AsyncSession, import_ids: list[str]) -> dict[str, dict[str, Any]]:
    """各期导入记录上的当前状态：{import_id: {raw_state, purged, revoked, recipe_seq}}。

    - raw_state：kept / purged / absent（table_imports.raw_state）；
    - purged、revoked：StateNote{at, signed_by, reason}，没有记录为 None。清除原件、作废接受都只记在导入记录上，
      不在封存范围内，界面标「当前状态」（P4-SPEC 0.2 补充 2）；
    - recipe_seq：这一期配方记录的 seq（table_recipes.seq，经 table_imports.recipe_id 找），取不到为 None。

    找不到的 import_id 不出现在结果里（调用方按 None 处理）。只取需要的几列：导入记录上还有核对摘要等大字段。
    """
    ids = [str(i) for i in dict.fromkeys(import_ids) if i]
    if not ids:
        return {}
    rows = (await session.execute(
        select(TableImport.id, TableImport.raw_state, TableImport.purged, TableImport.revoked, TableImport.recipe_id)
        .where(TableImport.id.in_(ids)))).all()
    recipe_ids = sorted({str(r.recipe_id) for r in rows if r.recipe_id})
    seqs: dict[str, int] = {}
    if recipe_ids:
        seqs = {str(rid): int(seq) for rid, seq in (await session.execute(
            select(TableRecipe.id, TableRecipe.seq).where(TableRecipe.id.in_(recipe_ids)))).all()}
    return {str(r.id): {
        "raw_state": r.raw_state or None,
        "purged": _note(r.purged),
        "revoked": _note(r.revoked),
        "recipe_seq": seqs.get(str(r.recipe_id)) if r.recipe_id else None,
    } for r in rows}


def _note(record: Any) -> StateNote | None:
    """table_imports.purged / revoked 的记录 {at, reason, signed_by, …} → StateNote；空的、不是 dict 的为 None。"""
    if not isinstance(record, dict) or not record:
        return None

    def text(key: str) -> str | None:
        value = record.get(key)
        return str(value) if value not in (None, "") else None

    return StateNote(at=text("at"), signed_by=text("signed_by"), reason=text("reason"))
