"""测试里建的数据源，用完就删。

数据源表在每个测试进程的测试库里是共用的：留下来的源会出现在助手的数据源清单里（50 张表的合成库还会
触发挑表、多一次模型调用），别的用例的结果就跟着执行顺序变。阶段 1B 就是这样被绊过一次。
删数据源时它的数据目录随外键级联删除（测试库开着 PRAGMA foreign_keys=ON）。

分析相关的用例都建名叫 shop 的源（sqlite_source / forget_source，tests/conftest.py 的 scratch_source 夹具按它们建、按它们删）：
别的用例留下的数据目录（金额标成了流量，窗口长度不同就从有条件可比变成不可比）、coverage 的现查缓存里 5 分钟内按同一个
源 id 记下的表范围（另一份库的日期范围），都会改掉这次的判定。所以建之前先把同名的旧源连同这两样删干净，用完再删一遍。
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import delete, select

from app.data.engine import engines
from app.db.base import SessionLocal
from app.db.models import DataSource


async def drop_source(source_id: str | None = None, *, name: str | None = None) -> None:
    """按 id 或名字删一个数据源，并作废它的连接池。不存在就什么都不做。"""
    async with SessionLocal() as session:
        if source_id is None:
            source_id = (await session.execute(
                select(DataSource.id).where(DataSource.name == name))).scalar_one_or_none()
            if source_id is None:
                return
        await session.execute(delete(DataSource).where(DataSource.id == source_id))
        await session.commit()
    await engines.invalidate(source_id)


async def sqlite_source(name: str, path: Any, *, options: dict[str, Any] | None = None, schema: bool = False) -> str:
    """建一个指向这份 SQLite 库文件的只读数据源（启用），交回 id。同名的旧源先删掉（forget_source：连同它的数据目录、剖析
    和现查缓存），这次的源是新的 id。schema 为真时顺手探好表结构（要写数据目录的用例用）。用完调 forget_source。"""
    await forget_source(name=name)
    async with SessionLocal() as session:
        row = DataSource(name=name, kind="sqlite", database=str(path), readonly=True, enabled=True,
                         options=dict(options or {}))
        session.add(row)
        await session.flush()
        if schema:
            from app.data.introspect import introspect

            row.schema_cache = await introspect(row)
        await session.commit()
        source_id = row.id
    await engines.invalidate(source_id)
    return source_id


async def forget_source(source_id: str | None = None, *, name: str | None = None) -> None:
    """按 id 或名字删一个数据源（drop_source：数据目录、剖析随外键级联删除，连接池作废），再清掉 coverage 现查缓存里它的
    表范围（data/coverage 的 _LIVE，按数据源 id 记、5 分钟有效：同一个进程里后面的用例会拿到这份旧范围）。不存在就什么都
    不做。"""
    from app.data import coverage

    if source_id is None:
        async with SessionLocal() as session:
            source_id = (await session.execute(
                select(DataSource.id).where(DataSource.name == name))).scalar_one_or_none()
        if source_id is None:
            return
    await drop_source(source_id)
    for key in [k for k in coverage._LIVE if k and k[0] == source_id]:
        coverage._LIVE.pop(key, None)


async def introspect_now(client, source_id: str, *, expect: int = 202) -> dict:
    """探查结构并等它做完（探查是后台任务：POST /introspect 回 202 和任务，next-round 03 A1），返回数据源此刻的样子
    （DataSourceOut）。expect 不是 202 时只发请求、断言状态码，返回响应体。"""
    from app.data import introspect_jobs

    r = await client.post(f"/api/datasources/{source_id}/introspect")
    assert r.status_code == expect, r.text
    if expect != 202:
        return r.json()
    job_id = r.json()["job"]["id"]
    await introspect_jobs.manager.wait(job_id, timeout=120)
    r = await client.get(f"/api/datasources/{source_id}/introspect-jobs/{job_id}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["job"]["status"] in ("succeeded", "partial"), body["job"]
    return body["source"]
