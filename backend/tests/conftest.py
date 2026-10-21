"""把整个测试会话关进一个临时目录。

不这么做的话，任何一个直接用 SessionLocal 的测试都会往开发库
（data/agentlab.db）里写行——写进去的 run 会出现在运行列表里，和真实运行
混在一起，而且带着测试的假数据。这不是假想：test_emit_order 第一版就这么
干了，几行 emit-order-* 的假运行直接排在了列表最前面。

必须在 app.* 被 import 之前设好环境变量：settings 是模块级单例，engine 在
app.db.base 导入时就按 db_url 建好了。conftest 早于测试模块加载，所以这里
是唯一还来得及的地方。
"""

from __future__ import annotations

import atexit
import os
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentlab-tests-"))
os.environ["AGENTLAB_DATA_DIR"] = str(_TMP)
# 下面这几项也都要在任何 app.* 导入之前设（两种库同一个顺序）：PostgreSQL 分支要先导入 app.db.base 装连接池，settings 在那一刻
# 就按当时的环境定下来了——以前这两项写在那之后，PostgreSQL 上 AGENTLAB_SCHEDULER 没生效（scheduler_on 成了 True）
# 别把真实的 key 带进测试环境
os.environ.setdefault("AGENTLAB_SECRET_KEY", "test-only-not-a-real-key")
# 定时任务的调度循环不在测试里起：走 lifespan 的测试不该到点替人发起运行。要测调度的直接调它
os.environ.setdefault("AGENTLAB_SCHEDULER", "0")


def _postgres_database(admin_url: str) -> str:
    """AGENTLAB_TEST_DATABASE_URL 给的是管理用的库：每个 xdist 进程在同一台服务器上建一个自己的空库（库名加进程号后缀，
    已有的同名库先删掉），进程退出时删掉。返回这个库的地址。scripts/test-postgres.sh 起临时容器、设好这个变量。
    这里导入的 app.db.urls 只用 SQLAlchemy 的地址解析，不碰 settings（settings 要等环境变量都设好）。"""
    import psycopg
    from sqlalchemy.engine import make_url

    from app.db.urls import libpq_conninfo, normalize_database_url

    url = make_url(normalize_database_url(admin_url))
    name = f"{url.database}_{os.environ.get('PYTEST_XDIST_WORKER', 'main')}"
    admin = libpq_conninfo(url.render_as_string(hide_password=False))

    def run(sql: str) -> None:
        with psycopg.connect(admin, autocommit=True) as conn:
            conn.execute(sql)

    run(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    run(f'CREATE DATABASE "{name}"')

    def drop() -> None:
        try:
            run(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        except Exception:  # noqa: BLE001 - 服务器先走了（容器已删）就算了
            pass

    atexit.register(drop)
    return url.set(database=name).render_as_string(hide_password=False)


# 应用库：缺省数据目录里的 SQLite。设了 AGENTLAB_TEST_DATABASE_URL 就跑在 PostgreSQL 上。不管哪种，都盖掉环境和 .env
# 里可能有的 AGENTLAB_DATABASE_URL / AGENTLAB_CHECKPOINT_URL：测试绝不能连到真在用的库上
_TEST_PG = os.environ.get("AGENTLAB_TEST_DATABASE_URL", "").strip()
os.environ["AGENTLAB_CHECKPOINT_URL"] = ""
os.environ["AGENTLAB_DATABASE_URL"] = _postgres_database(_TEST_PG) if _TEST_PG else ""
if _TEST_PG:
    # 每个测试一个事件循环（pytest-asyncio 的缺省），asyncpg 的连接不能跨循环用：引擎先按不用连接池建，
    # 再换成按事件循环分的池子（tests/pgpool.py），一个测试里的查库照样复用连接
    os.environ["AGENTLAB_DB_POOL_SIZE"] = "0"

    from app.db.base import engine as _app_engine  # noqa: E402
    from tests.pgpool import install as _loop_local_pool  # noqa: E402

    _loop_local_pool(_app_engine)

import pytest  # noqa: E402

# 整个 JSON 值做比较、去重、分组、排序时编译就报错（tests/jsonguard.py）：PostgreSQL 的 json 不支持，SQLite 的测试也拦下
from app.db.base import engine as _guarded_engine  # noqa: E402
from tests.jsonguard import install as _json_guard  # noqa: E402

_json_guard(_guarded_engine.sync_engine.dialect)


@pytest.fixture(scope="session", autouse=True)
def _prepare_schema():
    """建表。测试库是全新的临时文件，什么都没有。"""
    import asyncio

    from app.db.base import Base, engine

    async def create() -> None:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.get_event_loop_policy().new_event_loop().run_until_complete(create())
    yield


def pytest_report_header(config) -> str:
    where = "PostgreSQL（AGENTLAB_TEST_DATABASE_URL）" if _TEST_PG else "SQLite"
    return f"测试数据目录：{_TMP}；应用库：{where}"


def pytest_collection_modifyitems(config, items) -> None:
    """标了 sqlite_only 的测试只在应用库是 SQLite 时跑：它们断言的是 SQLite 自己的东西（数据目录里的库文件、PRAGMA）。"""
    if not _TEST_PG:
        return
    skip = pytest.mark.skip(reason="只对 SQLite 应用库成立")
    for item in items:
        if "sqlite_only" in item.keywords:
            item.add_marker(skip)


@pytest.fixture
async def scratch_source():
    """测试里建的 SQLite 数据源：source_id = await scratch_source("shop", 库文件路径, options={...})。同名的旧源先删掉（连同
    它的数据目录、剖析和现查缓存，tests/fixtures/sources.sqlite_source），测试结束时再删一遍、清掉现查缓存（forget_source）。
    分析相关的用例都建名叫 shop 的源：别的用例留下的数据目录、5 分钟内现查过的表范围会改掉这次的判定。"""
    from tests.fixtures.sources import forget_source, sqlite_source

    made: list[str] = []

    async def make(name: str, path, **kw) -> str:
        made.append(await sqlite_source(name, path, **kw))
        return made[-1]

    yield make
    for source_id in made:
        await forget_source(source_id)


@pytest.fixture(scope="session")
def scenic_db(tmp_path_factory) -> str:
    """合成的景区业务库（tests/fixtures/catalog/scenic.py）：50 张表、真外键与只能靠命名推断的关系并存。

    每个测试进程建一次，只读使用；要改库内容的测试自己调 scenic.build(tmp_path / "x.db") 另建一个。
    """
    from tests.fixtures.catalog import scenic

    return str(scenic.build(tmp_path_factory.mktemp("catalog") / "scenic.db"))
