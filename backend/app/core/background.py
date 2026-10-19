"""进程里「发出去就不管」的后台任务（推送通知、评测收尾的清理）：留着引用免得被回收，测试和关停时等它们做完。"""

from __future__ import annotations

import asyncio
from typing import Any


async def drain(tasks: set[asyncio.Future[Any]]) -> None:
    """等 tasks 里本事件循环上的任务都做完。

    已经做完的直接从集合里摘掉，不等它们自己的 discard 回调：done 回调排在任务所在的事件循环上，那个循环要是已经不转了（测试里
    每个测试一个事件循环，前一个测试末尾刚做完的推送）回调就永远不跑、任务一直留在集合里。以前的写法 `while tasks: await
    gather(*tasks)` 碰上这种集合，gather 拿到的全是做完了的、立刻返回，await 不让出事件循环，就在这里空转——CPU 打满、整个
    进程卡死（PostgreSQL 全量里 test_personal_apps 收尾时出现过）。别的事件循环上还没做完的等不到（不在这个循环上），不等；
    那个循环已经关了的一并摘掉。"""
    loop = asyncio.get_running_loop()
    while True:
        for task in list(tasks):
            if task.done() or (task.get_loop() is not loop and task.get_loop().is_closed()):
                tasks.discard(task)
        mine = [task for task in tasks if task.get_loop() is loop]
        if not mine:
            return
        await asyncio.gather(*mine, return_exceptions=True)


__all__ = ["drain"]
