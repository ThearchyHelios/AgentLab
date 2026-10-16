"""带机器码的 HTTP 错误。

detail 仍是给人看的那一句，老前端照旧直接显示；code 是给程序认的稳定标识。
以前前端只能按 detail 的开头几个字认出某一类错误，文案一改就悄悄失灵。
"""
from __future__ import annotations

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

#: 限定的数据源一个都不在了（删了或停用了），见 copilot._sources
DATASOURCE_SCOPE_EMPTY = "datasource_scope_empty"

#: 保存数据源时剖析设置不合规（422）：field 是出错的那一项（table_max_queries……），说不清是哪一项时没有 field
PROFILE_SETTINGS_INVALID = "profile_settings_invalid"
#: 剖析任务开始不了（409，api/catalog.create_profile_job）：未开启、数据源停用、没有表结构、快照被改动、这个源已有排队或
#: 进行中的剖析任务（导入目录、同步起草遇到剖析任务也是它）、按范围挑出 0 张表
PROFILE_DISABLED = "profile_disabled"
DATASOURCE_INACTIVE = "datasource_inactive"
SCHEMA_MISSING = "schema_missing"
PROFILE_BUSY = "profile_busy"
SNAPSHOT_TAMPERED = "snapshot_tampered"
PROFILE_NOTHING = "profile_nothing"
#: 要停止的剖析任务已经结束了（409）
CATALOG_PROFILE_FINISHED = "catalog_profile_finished"
#: 数据目录的后台起草任务（api/catalog.py 的 /catalog/draft-jobs）：同一个源已有排队或进行中的任务（409，界面改去
#: GET /draft-jobs/active 显示那一个）、起草模型用不了（409，不建任务）、要停止的任务已经结束了（409）
CATALOG_DRAFT_BUSY = "catalog_draft_busy"
CATALOG_DRAFT_MODEL_UNAVAILABLE = "catalog_draft_model_unavailable"
CATALOG_DRAFT_FINISHED = "catalog_draft_finished"

# ---------------------------------------------------------------- 集成接口（04 细案 A7）
# 外部系统用访问密钥调的应用、待办、工件接口，以及角色、运行读权限的拒绝：按 code 认是哪种错，detail 照旧是给人看的那句

#: 身份：没登录（401，中间件、auth.require 都回它）、角色不够（403，路由级依赖、接口内部的 ensure_role、开登录时的 /docs）
AUTH_REQUIRED = "auth_required"
ROLE_REQUIRED = "role_required"
#: 运行：这次运行读不了（403，api/deps 的证据、运行接口）；别人的个人应用刷新、个人蓝图试运行只给拥有者（403）
RUN_FORBIDDEN = "run_forbidden"
RUN_PRIVATE = "run_private"
#: 问数据和助手：使用者没开问数据，或者这个接口不在问数据的范围里（403）
CHAT_FORBIDDEN = "chat_forbidden"

#: 应用（api/apps.py）。app_not_found 同入口发起时的 apps/entries.APP_GONE；没有任何授权的人也回它（不该知道应用在不在）
APP_NOT_FOUND = "app_not_found"
APP_VERSION_NOT_FOUND = "app_version_not_found"
APP_INSTANCE_NOT_FOUND = "app_instance_not_found"
#: 草稿里、当前发布版里都没有声明这个钩子（404）；声明了、还没生成密钥（404）
APP_HOOK_NOT_DECLARED = "app_hook_not_declared"
APP_HOOK_NOT_FOUND = "app_hook_not_found"
#: 改草稿、授权时写进来的不合规（422）：岗位绑定、个人应用的草稿（只许改呈现）、授权的人和组
APP_BINDINGS_INVALID = "app_bindings_invalid"
APP_DRAFT_INVALID = "app_draft_invalid"
APP_GRANTS_INVALID = "app_grants_invalid"
#: 页面参数不是 JSON 对象（422）
APP_PARAMS_INVALID = "app_params_invalid"
#: 入口表单、按需视图的参数没通过校验（422，另带 errors：[{path, code, message}]）。同待办回复、入站钩子的表单错误
FORM_INVALID = "form_invalid"
#: 发起实例、刷新视图、补算时引擎不发起运行，又没有更细的码（engine/launch.LaunchRefused 没带 code 的那几种）
RUN_LAUNCH_REFUSED = "run_launch_refused"
# 派生应用草稿时工作流、版本不存在（404）的码在 apps/derive.py（WORKFLOW_GONE、VERSION_GONE）；入口、视图、补算的码
# 在 apps/entries.py、apps/resolve.py、apps/backfill.py，同这里一样都是 app_ 开头

#: 待办（api/tasks.py）。服务层（process/tasks.py 的 TaskRefused）没带码的拒绝按状态补上同一类的码：404 不存在；403
#: 不能这样做（不是管理员也不是应用的管理者，不能改派、催办）；409 待办已经处理过、关闭了、刚被别人回复，或者所属的
#: 运行不再等这条回复——刷新看最新状态；422 回复的内容不对。带了码的照用自己的（task_not_assigned、form_invalid……）
TASK_NOT_FOUND = "task_not_found"
TASK_FORBIDDEN = "task_forbidden"
TASK_CLOSED = "task_closed"
TASK_INVALID = "task_invalid"

#: 工件（api/artifacts.py）：没有权限（403）、这类工件的原文只给搭建者（403）、不存在（404）、内容和哈希对不上（409）
ARTIFACT_FORBIDDEN = "artifact_forbidden"
ARTIFACT_KIND_FORBIDDEN = "artifact_kind_forbidden"
ARTIFACT_NOT_FOUND = "artifact_not_found"
ARTIFACT_TAMPERED = "artifact_tampered"

#: 设置（api/settings.py）：对外访问地址由环境变量 AGENTLAB_PUBLIC_URL 指定时，设置里那一项不能改成别的值（409）
PUBLIC_URL_ENV_LOCKED = "public_url_env_locked"


class CodedHTTPException(HTTPException):
    """field：出错的是哪一项（表单据此把报错落到那一格）。可选，没有就不出现在返回里。"""

    def __init__(self, status_code: int, detail: str, code: str, *, field: str | None = None) -> None:
        super().__init__(status_code, detail)
        self.code = code
        self.field = field


async def coded_handler(_: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, CodedHTTPException)
    content: dict[str, str] = {"detail": exc.detail, "code": exc.code}
    if exc.field:
        content["field"] = exc.field
    return JSONResponse(status_code=exc.status_code, content=content, headers=exc.headers)
