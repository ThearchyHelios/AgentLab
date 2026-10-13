from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api import (
    artifacts, bundle, catalog, compare, conversations, copilot, datasources, evidence, governance, inquiry, knowledge,
    runs, sandbox, schedules, settings as settings_api, source_versions, table_imports, tools, whatif, workflows,
)
from app.api.coded import CodedHTTPException, coded_handler
from app.core.config import settings
from app.db.base import init_db
from app.engine.runner import run_manager
from app.seed import seed_defaults

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("agentlab")


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    from app.data.engine import sqlite_dqs_self_check

    sqlite_dqs_self_check()  # 写错的双引号列名必须报错；做不到就记日志并让启动失败
    settings.ensure_dirs()
    await init_db()
    await seed_defaults()

    # 把存的 embedding 配置读进来。不读的话重启后会悄悄退回本地哈希向量，
    # 而库里的向量是上次那个模型建的——检索会整体退回关键词，
    # 用户只看到"重启之后搜得不准了"
    from app.db.base import SessionLocal
    from app.memory.embeddings import load_setting

    async with SessionLocal() as _session:
        try:
            await load_setting(_session)
        except Exception as e:  # noqa: BLE001 - key 没了不该拦住整个服务起不来
            logger.warning("embedding 配置没能生效，暂用本地向量：%s", e)

    # 上次被强杀时留下的"处理中"文档。后台任务不会自己回来，留着它只会在
    # 界面上永远转圈——和 runner 对 running 的 run 是同一个道理
    from sqlalchemy import update as _update

    from app.db.models import Document as _Doc

    async with SessionLocal() as _s:
        await _s.execute(
            _update(_Doc).where(_Doc.status == "processing")
            .values(status="failed", error="服务重启，这份文档没有处理完，请重新上传")
        )
        await _s.commit()

    # 上传表格的版本底座：清理上次没发布完的文件、给老上传补初始版本、回收没人用的版本。
    # 出错只记日志：最坏是留下几个多余的文件，不该让服务起不来
    from app.data import table_versions

    async with SessionLocal() as _s:
        try:
            await table_versions.startup(_s)
        except Exception:  # noqa: BLE001
            logger.exception("上传表格的版本整理失败，服务照常启动")

    # 上次没做完的数据目录起草任务：标成中断（没结束的表记停止），不自动续跑——拷来的数据目录（检查脚本、验收副本）
    # 启动时不能替人去调模型。界面上点「重试」接着起草没写好的表
    from app.data import catalog_jobs

    try:
        await catalog_jobs.startup()
    except Exception:  # noqa: BLE001
        logger.exception("数据目录起草任务的启动整理失败，服务照常启动")

    await run_manager.setup()
    from app.sandbox.manager import sandbox_manager as _sbm

    await _sbm.start_reaper()
    # 定时任务的调度循环放在 run_manager 之后起：它到点要发起运行。拷来的数据目录（检查脚本的
    # --stacks）用 AGENTLAB_SCHEDULER=0 关掉，免得替原库把定时任务再执行一遍
    from app import scheduler

    if scheduler.scheduler_enabled():
        await scheduler.start()
    logger.info("AgentLab 就绪 · 数据目录 %s", settings.data_dir)
    try:
        yield
    finally:
        # 先停调度，再停运行：停运行的途中不该再有新运行被发起。起草任务停下来标成中断，下次启动不会再续
        await scheduler.stop()
        await catalog_jobs.manager.shutdown()
        await run_manager.shutdown()
        from app.sandbox.manager import sandbox_manager

        await sandbox_manager.close()
        logger.info("已停止")


app = FastAPI(
    title="AgentLab",
    description="可视化 Agent 编排实验台",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

for router in (
    workflows.router,
    # 这几个也挂在 /api/runs 下面，要在 runs.router 之前登记：以后加的固定路径（/api/runs/compare 之类）
    # 才不会被 runs 的 /{run_id} 当成运行 id 吃掉。/{run_id}/… 的子路径放在哪边都不冲突
    compare.router,
    bundle.router,
    inquiry.router,
    whatif.router,
    runs.router,
    runs.approvals_router,
    evidence.router,
    settings_api.router,
    tools.router,
    tools.custom_router,
    tools.mcp_router,
    knowledge.memory_router,
    knowledge.kb_router,
    knowledge.skills_router,
    sandbox.router,
    copilot.router,
    conversations.router,
    artifacts.router,
    governance.router,
    schedules.router,
    # 按配方导入的 /api/datasources/imports/… 要在 datasources 的通配 /{source_id} 之前登记
    table_imports.router,
    source_versions.router,
    catalog.router,
    datasources.router,
):
    app.include_router(router)


app.add_exception_handler(CodedHTTPException, coded_handler)


@app.exception_handler(ValueError)
async def value_error_handler(_: Request, exc: ValueError) -> JSONResponse:
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.get("/api/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "agentlab"}


# 生产部署：前端构建产物由服务端一并托管（见 app/web.py）。页面回退是通配路由，
# 必须在所有 /api 路由之后注册。没设 AGENTLAB_WEB_DIST 时什么都不挂，开发模式照旧
if settings.web_dist is not None:
    from app.web import mount_web

    mount_web(app, settings.web_dist)
