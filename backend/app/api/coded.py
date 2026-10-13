"""带机器码的 HTTP 错误。

detail 仍是给人看的那一句，老前端照旧直接显示；code 是给程序认的稳定标识。
以前前端只能按 detail 的开头几个字认出某一类错误，文案一改就悄悄失灵。
"""
from __future__ import annotations

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

#: 限定的数据源一个都不在了（删了或停用了），见 copilot._sources
DATASOURCE_SCOPE_EMPTY = "datasource_scope_empty"

#: 保存数据源时剖析设置不合规（422）：field 是出错的那一项（max_queries……），说不清是哪一项时没有 field
PROFILE_SETTINGS_INVALID = "profile_settings_invalid"
#: 剖析开始不了（409）的五种情况，见 api/catalog.profile_catalog_tables
PROFILE_DISABLED = "profile_disabled"
DATASOURCE_INACTIVE = "datasource_inactive"
SCHEMA_MISSING = "schema_missing"
PROFILE_BUSY = "profile_busy"
SNAPSHOT_TAMPERED = "snapshot_tampered"
#: 数据目录的后台起草任务（api/catalog.py 的 /catalog/draft-jobs）：同一个源已有排队或进行中的任务（409，界面改去
#: GET /draft-jobs/active 显示那一个）、起草模型用不了（409，不建任务）、要停止的任务已经结束了（409）
CATALOG_DRAFT_BUSY = "catalog_draft_busy"
CATALOG_DRAFT_MODEL_UNAVAILABLE = "catalog_draft_model_unavailable"
CATALOG_DRAFT_FINISHED = "catalog_draft_finished"


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
