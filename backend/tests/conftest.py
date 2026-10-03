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

import os
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentlab-tests-"))
os.environ["AGENTLAB_DATA_DIR"] = str(_TMP)
# 别把真实的 key 带进测试环境
os.environ.setdefault("AGENTLAB_SECRET_KEY", "test-only-not-a-real-key")

import pytest  # noqa: E402


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
    return f"测试数据目录：{_TMP}"


@pytest.fixture(scope="session")
def scenic_db(tmp_path_factory) -> str:
    """合成的景区业务库（tests/fixtures/catalog/scenic.py）：50 张表、真外键与只能靠命名推断的关系并存。

    每个测试进程建一次，只读使用；要改库内容的测试自己调 scenic.build(tmp_path / "x.db") 另建一个。
    """
    from tests.fixtures.catalog import scenic

    return str(scenic.build(tmp_path_factory.mktemp("catalog") / "scenic.db"))
