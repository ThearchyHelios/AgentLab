"""启动时等库（db/base.wait_for_database）：和 PostgreSQL 容器一起起、库还没就绪时按退避重试、照实打日志，等够了再报错；
密码不对、库不存在这种等也没用的立刻报错；SQLite 不等。"""

from __future__ import annotations

import logging

import pytest

from app.db.base import make_engine, wait_for_database


class _Dialect:
    def __init__(self, name: str) -> None:
        self.name = name


class _Conn:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, _statement):
        return None


class _Engine:
    """connect() 依次抛 errors 里的错，用完了就连上。"""

    def __init__(self, errors: list[BaseException], dialect: str = "postgresql") -> None:
        self.errors = list(errors)
        self.dialect = _Dialect(dialect)
        self.attempts = 0

    def connect(self):
        self.attempts += 1
        if self.errors:
            raise self.errors.pop(0)
        return _Conn()


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


class _SqlState(Exception):
    def __init__(self, sqlstate: str) -> None:
        super().__init__(f"sqlstate {sqlstate}")
        self.sqlstate = sqlstate


async def test_retries_with_backoff_until_the_database_answers(caplog):
    clock = _Clock()
    engine = _Engine([ConnectionRefusedError("拒绝连接"), OSError("名字解析不了"), _SqlState("57P03")])
    with caplog.at_level(logging.INFO, logger="app.db.base"):
        await wait_for_database(engine, timeout=60, sleep=clock.sleep, clock=clock)
    assert engine.attempts == 4 and clock.slept == [1.0, 2.0, 4.0]
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 3 and all("应用库还没就绪" in w for w in warnings)
    assert "拒绝连接" in warnings[0] and "最多 60 秒" in warnings[0]
    assert any("应用库连上了（第 4 次" in r.getMessage() for r in caplog.records)


async def test_gives_up_after_the_budget_and_raises_the_last_error(caplog):
    clock = _Clock()
    engine = _Engine([ConnectionRefusedError(f"第 {i} 次") for i in range(100)])
    with caplog.at_level(logging.INFO, logger="app.db.base"), pytest.raises(ConnectionRefusedError, match="第 5 次"):
        await wait_for_database(engine, timeout=20, sleep=clock.sleep, clock=clock)
    # 退避 1、2、4、5、5：等到第 17 秒，再等 5 秒就过了 20 秒，不等了，抛第 6 次（下标 5）的错
    assert clock.slept == [1.0, 2.0, 4.0, 5.0, 5.0]
    assert any("不再等了" in r.getMessage() for r in caplog.records if r.levelno == logging.ERROR)


@pytest.mark.parametrize("sqlstate", ["28P01", "28000", "3D000", "42501"])
async def test_errors_waiting_cannot_fix_are_raised_at_once(sqlstate):
    clock = _Clock()
    engine = _Engine([_SqlState(sqlstate)])
    with pytest.raises(_SqlState):
        await wait_for_database(engine, timeout=60, sleep=clock.sleep, clock=clock)
    assert engine.attempts == 1 and clock.slept == []


async def test_sqlstate_on_the_wrapped_cause_counts():
    clock = _Clock()
    wrapper = RuntimeError("SQLAlchemy 包了一层")
    wrapper.__cause__ = _SqlState("28P01")
    with pytest.raises(RuntimeError):
        await wait_for_database(_Engine([wrapper]), timeout=60, sleep=clock.sleep, clock=clock)
    assert clock.slept == []


async def test_sqlite_does_not_wait():
    engine = _Engine([ConnectionRefusedError("不该连")], dialect="sqlite")
    await wait_for_database(engine, timeout=60)
    assert engine.attempts == 0


async def test_a_real_refused_connection_is_treated_as_not_ready_yet():
    """真的 asyncpg：端口上没有服务时（库容器还没起来）算「还没就绪」，按退避重试，到时间才抛。"""
    pytest.importorskip("asyncpg")
    clock = _Clock()
    engine = make_engine("postgresql+asyncpg://agentlab:x@127.0.0.1:1/agentlab")
    try:
        with pytest.raises(OSError):
            await wait_for_database(engine, timeout=4, sleep=clock.sleep, clock=clock)
    finally:
        await engine.dispose()
    assert clock.slept == [1.0, 2.0]


async def test_a_missing_database_on_a_real_server_is_raised_at_once():
    """真的 PostgreSQL（AGENTLAB_TEST_DATABASE_URL）：库不存在（3D000）立刻报错，不等。"""
    from app.core.config import settings

    if settings.db_dialect != "postgresql":
        pytest.skip("要 PostgreSQL")
    base, _, _ = settings.db_url.rpartition("/")
    clock = _Clock()
    engine = make_engine(f"{base}/agentlab_no_such_database")
    try:
        with pytest.raises(Exception) as caught:
            await wait_for_database(engine, timeout=60, sleep=clock.sleep, clock=clock)
    finally:
        await engine.dispose()
    assert clock.slept == [], caught.value
    assert "agentlab_no_such_database" in str(caught.value)
