"""带机器码的 HTTP 错误。

detail 仍是给人看的那一句，老前端照旧直接显示；code 是给程序认的稳定标识。
以前前端只能按 detail 的开头几个字认出某一类错误，文案一改就悄悄失灵。
"""
from __future__ import annotations

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

#: 限定的数据源一个都不在了（删了或停用了），见 copilot._sources
DATASOURCE_SCOPE_EMPTY = "datasource_scope_empty"


class CodedHTTPException(HTTPException):
    def __init__(self, status_code: int, detail: str, code: str) -> None:
        super().__init__(status_code, detail)
        self.code = code


async def coded_handler(_: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, CodedHTTPException)
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail, "code": exc.code},
        headers=exc.headers,
    )
