from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api import (
    artifacts, conversations, copilot, datasources, governance, knowledge, runs,
    sandbox, settings as settings_api, tools, workflows,
)
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

    await run_manager.setup()
    from app.sandbox.manager import sandbox_manager as _sbm

    await _sbm.start_reaper()
    logger.info("AgentLab 就绪 · 数据目录 %s", settings.data_dir)
    try:
        yield
    finally:
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
    runs.router,
    runs.approvals_router,
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
    datasources.router,
):
    app.include_router(router)


@app.exception_handler(ValueError)
async def value_error_handler(_: Request, exc: ValueError) -> JSONResponse:
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.get("/api/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "agentlab"}
