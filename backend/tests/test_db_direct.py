"""直连地址和 PgBouncer 事务模式（AGENTLAB_DATABASE_DIRECT_URL、AGENTLAB_DB_PGBOUNCER）：地址怎么解析、哪些连接走直连、事务模式下
关掉了哪些东西。只建引擎、不连库，两种库都跑。"""
from __future__ import annotations

import pytest
from sqlalchemy.pool import NullPool

from app.core.config import Settings, settings

APP = "postgres://agentlab:secret@pgbouncer.internal:6432/agentlab"
DIRECT = "postgresql://agentlab:secret@db.internal:5432/agentlab"


def test_the_direct_url_is_normalized_and_used_for_session_bound_connections():
    s = Settings(database_url=APP, database_direct_url=DIRECT, _env_file=None)
    assert s.database_url.startswith("postgresql+asyncpg://") and "pgbouncer.internal:6432" in s.database_url
    assert s.database_direct_url.startswith("postgresql+asyncpg://") and "db.internal:5432" in s.database_direct_url
    assert s.session_db_url == s.database_direct_url
    assert "db.internal" in s.checkpoint_conninfo and s.checkpoint_conninfo.startswith("postgresql://"), "断点库缺省走直连"
    plain = Settings(database_url=APP, _env_file=None)
    assert plain.session_db_url == plain.database_url and "pgbouncer.internal" in plain.checkpoint_conninfo, "不设就全用同一个"
    with pytest.raises(ValueError, match="AGENTLAB_DATABASE_DIRECT_URL"):
        Settings(database_url=APP, database_direct_url="mysql://x@y/z", _env_file=None)


def test_transaction_mode_turns_off_client_pooling_and_statement_caches():
    from app.db.base import pg_engine_kwargs

    kw = pg_engine_kwargs(pgbouncer=True)
    assert kw["poolclass"] is NullPool
    args = kw["connect_args"]
    assert args["statement_cache_size"] == 0 and args["prepared_statement_cache_size"] == 0
    names = {args["prepared_statement_name_func"]() for _ in range(3)}
    assert len(names) == 3, "预备语句的名字每次都不一样，不和别的客户端撞名"
    direct = pg_engine_kwargs(pgbouncer=False, pool_size=5)
    assert direct["pool_size"] == 5 and "statement_cache_size" not in direct["connect_args"]


def test_listen_and_cluster_locks_go_direct(monkeypatch):
    from app.cluster.signal import listen_dsn
    from app.db import base

    monkeypatch.setattr(settings, "database_url", Settings(database_url=APP, _env_file=None).database_url)
    monkeypatch.setattr(settings, "database_direct_url", Settings(database_url=APP, database_direct_url=DIRECT,
                                                                  _env_file=None).database_direct_url)
    monkeypatch.setattr(base, "_session_engine", None)
    assert "db.internal:5432" in listen_dsn() and listen_dsn().startswith("postgresql://")
    made = base.session_engine()
    try:
        assert made is not base.engine and made.url.host == "db.internal" and made.url.port == 5432
        assert base.session_engine() is made, "直连那个池子只建一个"
    finally:
        monkeypatch.setattr(base, "_session_engine", None)
        made.sync_engine.dispose()
    monkeypatch.setattr(settings, "database_direct_url", "")
    assert base.session_engine() is base.engine and "pgbouncer.internal" in listen_dsn()


def test_transaction_mode_without_a_direct_url_refuses_to_start(monkeypatch):
    from app.db import base

    monkeypatch.setattr(settings, "database_url", Settings(database_url=APP, _env_file=None).database_url)
    monkeypatch.setattr(settings, "db_pgbouncer", True)
    monkeypatch.setattr(settings, "database_direct_url", "")
    with pytest.raises(RuntimeError, match="AGENTLAB_DATABASE_DIRECT_URL"):
        base.check_pgbouncer()
    monkeypatch.setattr(settings, "database_direct_url", Settings(database_url=APP, database_direct_url=DIRECT,
                                                                  _env_file=None).database_direct_url)
    base.check_pgbouncer()


def test_transaction_mode_with_a_direct_url_that_is_the_same_endpoint_refuses_to_start(monkeypatch):
    """复核 R-b12：直连地址和应用库地址规范化之后连的是同一个主机端口（还是那个 PgBouncer）——同样拒绝，说清楚。"""
    from app.db import base

    monkeypatch.setattr(settings, "database_url", Settings(database_url=APP, _env_file=None).database_url)
    monkeypatch.setattr(settings, "db_pgbouncer", True)
    for same in (APP, "postgresql+asyncpg://other:pw@PGBOUNCER.internal:6432/agentlab?ssl=require"):
        monkeypatch.setattr(settings, "database_direct_url",
                            Settings(database_url=APP, database_direct_url=same, _env_file=None).database_direct_url)
        with pytest.raises(RuntimeError, match="指着同一个地址"):
            base.check_pgbouncer()
    monkeypatch.setattr(settings, "database_direct_url", Settings(
        database_url=APP, database_direct_url="postgresql://agentlab:secret@pgbouncer.internal:5432/agentlab",
        _env_file=None).database_direct_url)
    base.check_pgbouncer()   # 同一台机器上的另一个端口（直连 PostgreSQL 或会话模式）：放行


def test_copying_checkpoints_goes_through_the_direct_url(monkeypatch, tmp_path):
    """复核 R-b12：copy-to-postgres 搬断点缺省走直连地址（设了的话）；单独给了断点库地址就用它。"""
    from app.db import to_postgres

    assert "db.internal:5432" in to_postgres.checkpoint_conninfo(APP, direct_url=DIRECT)
    assert "pgbouncer.internal:6432" in to_postgres.checkpoint_conninfo(APP)
    assert "ckpt.internal" in to_postgres.checkpoint_conninfo(APP, checkpoint_url="postgresql://u:p@ckpt.internal/c",
                                                              direct_url=DIRECT)
    seen: dict = {}

    async def fake_copy(source, target, **kw):
        seen.update(kw, target=target)
        raise to_postgres.CopyRefused("测试：到这里为止")

    monkeypatch.setattr(to_postgres, "copy_to_postgres", fake_copy)
    monkeypatch.setattr(settings, "database_url", Settings(database_url=APP, _env_file=None).database_url)
    monkeypatch.setattr(settings, "database_direct_url",
                        Settings(database_url=APP, database_direct_url=DIRECT, _env_file=None).database_direct_url)
    monkeypatch.setattr(settings, "checkpoint_url", "")
    assert to_postgres.main([str(tmp_path)]) == 2
    assert seen["direct_url"] == settings.database_direct_url, "目标就是应用库：断点走它的直连地址"
    assert to_postgres.main([str(tmp_path), "postgresql://u:p@elsewhere.internal:5432/x"]) == 2
    assert seen["direct_url"] is None, "目标是别的库：不套用应用库的直连地址"
