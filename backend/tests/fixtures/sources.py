"""测试里建的数据源，用完就删。

数据源表在每个测试进程的测试库里是共用的：留下来的源会出现在助手的数据源清单里（50 张表的合成库还会
触发挑表、多一次模型调用），别的用例的结果就跟着执行顺序变。阶段 1B 就是这样被绊过一次。
删数据源时它的数据目录随外键级联删除（测试库开着 PRAGMA foreign_keys=ON）。
"""
from __future__ import annotations

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
