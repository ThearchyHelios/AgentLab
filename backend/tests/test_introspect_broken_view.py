"""SQLite 连接关了双引号字符串兼容以后，老库里一个写坏的视图不能拖垮整次结构探查。

老库里常有 `SELECT "yes" AS flag` 这种视图：当年 SQLite 把找不到的双引号名字当字符串，
它一直能用。连接层关掉这项兼容后，这个视图本身查询会报 no such column——这是对的；
但批量反射也因它报错，以前探查吞掉异常、返回「成功、0 张表」，好好的表跟着一起消失，
还被盖上「已同步」。现在批量失败时逐个对象重试，只跳过坏的那个，并说明原因。
"""
from __future__ import annotations

import sqlite3
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy.engine.reflection import Inspector

from app.data import introspect as introspect_mod
from app.data.engine import engines, run_query
from app.data.introspect import introspect


def _make(path, *, table: bool = True, views: int = 1) -> str:
    """建库用的是 Python 自带的 sqlite3 连接（DQS 开着），和当年建出这种老库的情形一样。"""
    conn = sqlite3.connect(path)
    if table:
        conn.execute("CREATE TABLE 时段客流 (id INTEGER PRIMARY KEY, 分区 TEXT, 客流 INTEGER)")
        conn.executemany("INSERT INTO 时段客流 (分区, 客流) VALUES (?, ?)",
                         [("分区甲", 120), ("分区乙", 80)])
        for i in range(views):
            conn.execute(f'CREATE VIEW 旧视图{i} AS SELECT 分区, "是" AS 标记 FROM 时段客流')
    else:
        conn.execute('CREATE VIEW 旧视图 AS SELECT "是" AS 标记')   # 库里只有这一个坏视图
    conn.commit()
    conn.close()
    return str(path)


@pytest.fixture
def make_source():
    made: list[SimpleNamespace] = []

    def make(path: str, *, readonly: bool = True) -> SimpleNamespace:
        src = SimpleNamespace(
            id=f"bv-{uuid.uuid4().hex[:8]}", name="旧库", kind="sqlite", database=path,
            host=None, port=None, username=None, password=None, options={},
            readonly=readonly, description=None, schema_cache={},
        )
        made.append(src)
        return src

    yield make
    for src in made:
        engines._engines.pop(f"{src.id}:", None)


@pytest.mark.parametrize("readonly", [True, False], ids=["只读", "可写"])
async def test_broken_view_is_skipped_tables_still_listed(tmp_path, make_source, readonly):
    src = make_source(_make(tmp_path / "旧库.db"), readonly=readonly)
    payload = await introspect(src)

    assert list(payload["tables"]) == ["时段客流"]
    table = payload["tables"]["时段客流"]
    assert [c["name"] for c in table["columns"]] == ["id", "分区", "客流"]
    assert table["primary_key"] == ["id"]
    assert not payload.get("failed")
    assert list(payload["skipped"]) == ["旧视图0"]
    assert "no such column" in payload["skipped"]["旧视图0"]

    # 表照常可查；坏视图本身查询报错（连接层关 DQS 的本意）
    assert (await run_query(src, "SELECT SUM(客流) FROM 时段客流")).rows == [[200]]
    with pytest.raises(Exception, match="no such column"):
        await run_query(src, "SELECT * FROM 旧视图0")


async def test_only_broken_objects_is_a_failure_not_an_empty_success(tmp_path, make_source):
    """一个对象也取不到：记成失败（不会被盖「已同步」），而不是「成功、0 张表」。"""
    src = make_source(_make(tmp_path / "只剩视图.db", table=False))
    payload = await introspect(src)
    assert payload["tables"] == {}
    assert payload["failed"] is True
    assert "no such column" in payload["error"]


async def test_healthy_database_payload_unchanged(tmp_path, make_source):
    src = make_source(_make(tmp_path / "好库.db", views=0))
    payload = await introspect(src)
    assert list(payload["tables"]) == ["时段客流"]
    assert "skipped" not in payload and not payload.get("failed")


async def test_per_object_retry_gives_up_when_nothing_is_readable(tmp_path, make_source, monkeypatch):
    """批量和逐个都失败（比如连接本身出了问题）：开头几个都取不到就停，不挨个耗时间。"""
    path = tmp_path / "多表.db"
    conn = sqlite3.connect(path)
    for i in range(12):
        conn.execute(f"CREATE TABLE t{i} (a INTEGER)")
    conn.commit()
    conn.close()
    src = make_source(str(path))

    calls: list[str] = []

    def batch_fails(self, *a, **kw):
        raise RuntimeError("模拟：连接中断")

    def single_fails(self, name, *a, **kw):
        calls.append(name)
        raise RuntimeError("模拟：连接中断")

    monkeypatch.setattr(Inspector, "get_multi_columns", batch_fails)
    monkeypatch.setattr(Inspector, "get_columns", single_fails)
    payload = await introspect(src)
    assert len(calls) == introspect_mod._GIVE_UP_AFTER
    assert payload["failed"] is True and "连接中断" in payload["error"]
