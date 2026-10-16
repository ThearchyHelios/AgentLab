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
# 应用平台（docs/design/app-platform-and-look.md）。权限按 §3.12 在这里挂路由级依赖（api/deps.require_role），
# 关登录时全部放行
from app.api import (
    actions as actions_api, apps as apps_api, auth as auth_api, capabilities as capabilities_api, hooks as hooks_api,
    identity as identity_api, ledger as ledger_api, notify as notify_api, tasks as tasks_api,
)
# 下一轮第 2 波（docs/design/next-round）：评测、访问密钥
from app.api import access_keys as access_keys_api, evals as evals_api
# 普通用户建应用（docs/design/normal-user-apps.md §3.10）：蓝图、模板、文件、个人应用。权限在接口里按蓝图的范围和拥有者判
from app.api import (
    blueprints as blueprints_api, copilot_blueprint, files as files_api, personal as personal_api,
    templates as templates_api,
)
from app.api.coded import CodedHTTPException, coded_handler
from app.api.deps import chat_scope, evidence_scope, private_run_scope, require_role, role_by_method, run_scope
from app.auth.middleware import AuthMiddleware
from app.core.config import settings
from app.db.base import init_db
from app.engine.runner import run_manager
from app.seed import seed_defaults
from app.web_security import SecurityHeaders

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

    # 记级别之前发布过的老版本：有正式运行凭据的补记为已发布，免得子工作流、口径卡钉住它们的正式运行被误判成「没发布过」
    from app.apps.versions import backfill_legacy_levels
    from app.db.base import SessionLocal as _Sessions

    async with _Sessions() as _s:
        if levelled := await backfill_legacy_levels(_s):
            logger.info("给 %d 个老的发布版本补记了级别（有正式运行凭据）", levelled)
        await _s.commit()
    await _platform_startup()

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

    # 上次没做完的数据目录起草、剖析任务：标成中断（没结束的表记停止），不自动续跑——拷来的数据目录（检查脚本、验收副本）
    # 启动时不能替人去调模型、查业务库。界面上点「重试」接着做没做完的表。剖析设置里按整次剖析算的旧预算顺带迁移掉
    from app.data import catalog_jobs, catalog_profile_jobs

    try:
        await catalog_jobs.startup()
    except Exception:  # noqa: BLE001
        logger.exception("数据目录起草任务的启动整理失败，服务照常启动")
    try:
        await catalog_profile_jobs.startup()
    except Exception:  # noqa: BLE001
        logger.exception("数据剖析任务的启动整理失败，服务照常启动")
    # 后台探查任务（next-round 03 A1）同一个规矩：没做完的标中断，读完的表物化进表结构，不自动续跑
    from app.data import introspect_jobs

    try:
        await introspect_jobs.startup()
    except Exception:  # noqa: BLE001
        logger.exception("探查任务的启动整理失败，服务照常启动")

    await run_manager.setup()
    # 流程：存量的待审批补建待办（派给任何人，幂等）；上次进程没了时已发出、没拿到结果的写入收成结果未知、安排核实
    from app.process import actions as _actions, tasks as _tasks

    await _tasks.startup()
    await _actions.startup()
    # 发布单：上次进程没了时还在执行的单改成停下（停在没做完的那一步、写明执行中断），这样能接着做
    from app.blueprints import publish_plan as _plans

    try:
        if stopped := await _plans.startup():
            logger.info("%d 份发布单执行中断，已改成停下，可以接着做", stopped)
    except Exception:  # noqa: BLE001
        logger.exception("发布单的启动整理失败，服务照常启动")
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
        # 先停调度，再停运行：停运行的途中不该再有新运行被发起。起草、剖析任务停下来标成中断，下次启动不会再续
        await scheduler.stop()
        await catalog_jobs.manager.shutdown()
        await catalog_profile_jobs.manager.shutdown()
        await introspect_jobs.manager.shutdown()
        await run_manager.shutdown()
        from app.sandbox.manager import sandbox_manager

        await sandbox_manager.close()
        logger.info("已停止")


async def _platform_startup() -> None:
    """应用平台的启动整理（BE-1）：登录设置、对外访问地址读进缓存；自定义工具请求头、MCP env 和请求头里的存量明文加密
    一遍（幂等）；存量的内网 MCP 服务改为绑定登记端点（只在升级后第一次启动时做）；能力登记同步一次。后三样出错只记
    日志：最坏是登记暂时不全，不该让服务起不来。"""
    from app.auth import current_mode, load_settings
    from app.capabilities import registry as capability_registry
    from app.capabilities.secrets import seal_existing
    from app.core import public_url
    from app.db.base import SessionLocal

    await load_settings(force=True)
    logger.info("登录方式：%s", current_mode())
    # 对外访问地址（core/public_url.py）同样读进缓存：中间件核对来源时不查库
    if address := await public_url.refresh():
        logger.info("对外访问地址：%s（%s）", address, "环境变量" if public_url.env_locked() else "设置")
    try:
        if sealed := await seal_existing():
            logger.info("已加密 %d 处存量的明文密钥（自定义工具请求头、MCP 环境变量和请求头）", sealed)
    except Exception:  # noqa: BLE001
        logger.exception("存量密钥加密失败，服务照常启动")
    try:
        from app.tools.mcp_manager import adopt_intranet_servers

        if adopted := await adopt_intranet_servers():
            logger.info("已为 %d 个指向内网的存量 MCP 服务登记端点并绑定：%s", len(adopted),
                        "、".join(a["server"] for a in adopted))
    except Exception:  # noqa: BLE001
        logger.exception("存量 MCP 服务的端点登记失败，服务照常启动")
    try:
        async with SessionLocal() as session:
            await capability_registry.sync_all(session)
    except Exception:  # noqa: BLE001
        logger.exception("能力登记同步失败，服务照常启动")


app = FastAPI(
    title="AgentLab",
    description="可视化 Agent 编排实验台",
    version="0.1.0",
    lifespan=lifespan,
)

# 身份（app/auth/middleware.py）：按请求认出是谁、核对 CSRF。加在 CORS 之前，CORS 就在它外面一层：
# 跨域预检由 CORS 先答，不经过登录检查
app.add_middleware(AuthMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
# 安全响应头（app/web_security.py）：最后加、在最外层，CORS 预检和登录检查自己回的 401 / 403 也带上
app.add_middleware(SecurityHeaders)

# 路由级权限（§3.12）。关登录时这些依赖全部放行；开了登录，中间件先挡掉没登录的，这里再按角色和运行挡
_BUILDER = [require_role("builder")]
_ADMIN = [require_role("admin")]
_USER = [require_role("user")]
for router, guard in (
    (workflows.router, _BUILDER),
    # 这几个也挂在 /api/runs 下面，要在 runs.router 之前登记：以后加的固定路径（/api/runs/compare 之类）
    # 才不会被 runs 的 /{run_id} 当成运行 id 吃掉。/{run_id}/… 的子路径放在哪边都不冲突。
    # 运行对比、可验证报告、质询、假设分析交的是查询原文，只给 builder 及以上；别人的个人应用刷新、个人蓝图试运行只给
    # 拥有者（private_run_scope），管理员也不例外
    (compare.router, [*_BUILDER, private_run_scope]),
    (bundle.router, [*_BUILDER, private_run_scope]),
    (inquiry.router, [*_BUILDER, private_run_scope]),
    (whatif.router, [*_BUILDER, private_run_scope]),
    # 口径账本按运行读的接口（/api/runs/{run_id}/ledger…）：和证据同一道门
    (ledger_api.runs_router, [evidence_scope]),
    (runs.router, [run_scope]),
    (runs.approvals_router, _BUILDER),
    (evidence.router, [evidence_scope]),
    # 模型接入、运行默认值：搭建者只读，改动只给管理员
    (settings_api.router, [role_by_method(read="builder", write="admin")]),
    (tools.router, _BUILDER),
    (tools.custom_router, _BUILDER),
    # MCP 的 stdio 只有管理员能建、能改（api/tools.py 里按传输方式判断）
    (tools.mcp_router, _BUILDER),
    (knowledge.memory_router, _BUILDER),
    (knowledge.kb_router, _BUILDER),
    (knowledge.skills_router, _BUILDER),
    (sandbox.router, _BUILDER),
    (copilot.router, [chat_scope]),
    (conversations.router, [chat_scope]),
    # 工件：user 角色只能取能读的运行引用的、遮罩后的（api/artifacts.py）
    (artifacts.router, _USER),
    (governance.router, _BUILDER),
    (schedules.router, _BUILDER),
    # 问数据页选数据源用的名单（只有名字、说明、种类）：开了问数据的使用者也能读，同样要在通配 /{source_id} 之前登记
    (datasources.names_router, [chat_scope]),
    # 按配方导入的 /api/datasources/imports/… 要在 datasources 的通配 /{source_id} 之前登记
    (table_imports.router, _BUILDER),
    (source_versions.router, _BUILDER),
    (catalog.router, _BUILDER),
    (datasources.router, _BUILDER),
    # 应用平台。登录、登出、OIDC、JWKS 匿名可用，设置在接口里要管理员；入站钩子只认签名
    (auth_api.router, []),
    (identity_api.router, _ADMIN),
    # 选人用的名录（用户名、显示名、是否停用、组名）：岗位绑定、应用授权由搭建者做，只读
    (identity_api.directory_router, _BUILDER),
    (identity_api.candidates_router, _USER),
    # 能力登记、端点：搭建者只读，改动在接口里要管理员
    (capabilities_api.router, _BUILDER),
    # 普通用户建应用：个人应用的接口共用 /api/apps 前缀，要在 apps_api 之前登记；蓝图助手共用 /api/copilot 前缀，
    # 在接口里按蓝图的范围判（共享要 builder，个人要拥有者且开了 apps.user_personal_apps）
    (personal_api.router, _USER),
    (blueprints_api.router, _USER),
    (templates_api.router, _USER),
    (files_api.router, _USER),
    (copilot_blueprint.router, _USER),
    # 应用、待办、生效记录、通知：各自按应用授权、派给谁再判断
    (apps_api.router, _USER),
    (tasks_api.router, _USER),
    (actions_api.router, _USER),
    (hooks_api.router, []),
    (notify_api.router, _USER),
    (ledger_api.router, _BUILDER),
    # 下一轮第 2 波：评测只给搭建者；访问密钥登录即可，接口里再按本人 / 管理员判
    (evals_api.router, _BUILDER),
    (access_keys_api.router, _USER),
):
    app.include_router(router, dependencies=guard)


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
