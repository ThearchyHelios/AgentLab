"""测试里另开一条连接直接改应用库：模拟别处（另一个进程、恢复、继续运行）抢在中间提交。

应用库是 SQLite 时用 sqlite3 连数据目录里的 agentlab.db（同以前各处的写法）；是 PostgreSQL 时（AGENTLAB_TEST_DATABASE_URL，
见 conftest）用 psycopg 连同一个库。SQL 一律用 ? 占位，两种库写成同一句；锁等 5 秒（同以前 sqlite3 的 timeout=5）：
另一条连接要等的锁正好被调用方拿着的话，等满报错，不会把测试卡死。
"""

from __future__ import annotations

import re
from typing import Any

from app.core.config import settings


def is_postgres() -> bool:
    return settings.db_dialect == "postgresql"


def _pg_conninfo() -> str:
    from app.db.urls import libpq_conninfo

    return libpq_conninfo(settings.db_url)


def execute(*statements: str | tuple[str, tuple[Any, ...]]) -> None:
    """在另一条连接上依次执行、一起提交。每一项是 SQL，或者 (SQL, 参数)。"""
    items = [(s, ()) if isinstance(s, str) else (s[0], tuple(s[1])) for s in statements]
    if is_postgres():
        import psycopg

        with psycopg.connect(_pg_conninfo(), options="-c lock_timeout=5000") as conn:
            for sql, params in items:
                conn.execute(re.sub(r"\?", "%s", sql), params)
            conn.commit()
        return
    import sqlite3

    db = sqlite3.connect(settings.db_path, timeout=5)
    try:
        for sql, params in items:
            db.execute(sql, params)
        db.commit()
    finally:
        db.close()


def query(sql: str, *params: Any) -> list[tuple[Any, ...]]:
    """在另一条连接上读：看别人这时提交了什么（不是调用方事务里的）。"""
    if is_postgres():
        import psycopg

        with psycopg.connect(_pg_conninfo(), options="-c lock_timeout=5000") as conn:
            return [tuple(r) for r in conn.execute(re.sub(r"\?", "%s", sql), params).fetchall()]
    import sqlite3

    db = sqlite3.connect(settings.db_path, timeout=5)
    try:
        return [tuple(r) for r in db.execute(sql, params).fetchall()]
    finally:
        db.close()


def refuse_inserts(table: str, condition: str, *, name: str) -> tuple[list[str], list[str]]:
    """建一个触发器：往 table 插入满足 condition（用 NEW.列 写）的行时报错，报的是完整性错误（IntegrityError），同 SQLite
    的 RAISE(ABORT)。返回 (建的语句, 删的语句)，各是一串语句，逐条在应用库的连接上执行。

    PostgreSQL 要先建一个触发器函数（plpgsql）再挂上去，错误码给 23000（integrity_constraint_violation）。"""
    if is_postgres():
        create = [f"CREATE FUNCTION {name}_fn() RETURNS trigger AS $$ BEGIN IF {condition} THEN "
                  f"RAISE EXCEPTION 'refused by test' USING ERRCODE = 'integrity_constraint_violation'; END IF; "
                  f"RETURN NEW; END $$ LANGUAGE plpgsql",
                  f"CREATE TRIGGER {name} BEFORE INSERT ON {table} FOR EACH ROW EXECUTE FUNCTION {name}_fn()"]
        drop = [f"DROP TRIGGER IF EXISTS {name} ON {table}", f"DROP FUNCTION IF EXISTS {name}_fn()"]
        return create, drop
    return ([f"CREATE TRIGGER {name} BEFORE INSERT ON {table} WHEN {condition} "
             f"BEGIN SELECT RAISE(ABORT, 'refused by test'); END"], [f"DROP TRIGGER IF EXISTS {name}"])
