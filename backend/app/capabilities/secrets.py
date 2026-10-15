"""「名字 → 密钥值」的字典（请求头、环境变量）：存库加密、接口只回掩码、提交掩码值表示保持原值（A6 第 6 条）。

自定义工具的 headers、MCP 的 env 和 headers、端点各环境的 headers 都按这一套：界面拿到的永远是掩码，用户只改了别的
字段、原样提交回来时，掩码对上原值的那一项保持原值——不然每保存一次，密钥就被掩码本身覆盖掉。
"""
from __future__ import annotations

import re
from typing import Any

from app.core import crypto

#: HTTP 请求头名能用的字符（RFC 9110 的 token）
_HEADER_NAME = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")


def seal(values: dict[str, Any] | None, previous: dict[str, Any] | None = None) -> dict[str, str]:
    """提交上来的字典 → 存库的样子：值加密；正好等于原值掩码的那一项保持原值（原值是升级前的明文也一样加密）。"""
    before = previous or {}
    out: dict[str, str] = {}
    for key, value in (values or {}).items():
        text = "" if value is None else str(value)
        old = before.get(key)
        if old and text and text == crypto.mask(old):
            text = old
        out[str(key)] = crypto.encrypt(text) or ""
    return out


def header_problem(values: dict[str, Any] | None, *, check_values: bool = True) -> str | None:
    """请求头（名字 → 明文值）哪里不合法，没问题交回 None。名字不能为空、只能是 HTTP 的 token：存成 {"": ""} 时，这一组
    头的每个请求都在发出之前就失败，报出来的只是一句 LocalProtocolError。值不能换行、只能是可打印的英文字符（中文、全角
    字符编不进请求头）；值里写了 {{ 参数 }} 模板的（自定义工具）由调用方关掉值的检查。"""
    for name, value in (values or {}).items():
        key = str(name)
        if not key.strip():
            return "请求头名称不能为空"
        if not _HEADER_NAME.match(key):
            return f"请求头名称「{key}」不合法：只能用英文字母、数字和 - _ . 这类符号，不能有空格、冒号或中文"
        if check_values and any(not (" " <= ch <= "~" or ch == "\t") for ch in str(value or "")):
            return f"请求头「{key}」的值不合法：只能用英文字母、数字、空格和常用符号，不能换行，也不能用中文或全角字符"
    return None


def masked(values: dict[str, Any] | None) -> dict[str, str]:
    """给接口返回的样子：每个值都换成掩码。"""
    return {str(k): crypto.mask(v) if v else "" for k, v in (values or {}).items()}


def reveal(values: dict[str, Any] | None) -> dict[str, str]:
    """真正发请求、起进程时用的明文。密钥换过（解不开）的值给空串：带着密文去请求只会让对方回一句认证失败。"""
    return {str(k): (crypto.decrypt(v) or "") if v else "" for k, v in (values or {}).items()}


def is_sealed(values: dict[str, Any] | None) -> bool:
    """每个非空值都已经是密文。启动时据此判断存量明文要不要加密一遍。"""
    return all(not v or (isinstance(v, str) and v.startswith(crypto._PREFIX)) for v in (values or {}).values())


async def seal_existing() -> int:
    """启动时把存量的明文加密一遍：自定义工具的请求头、MCP 的 env 和请求头。encrypt 本身幂等（已经是密文的原样交回），
    重复执行不改任何东西。交回改了几行。"""
    from sqlalchemy import select

    from app.db.base import SessionLocal
    from app.db.models import CustomTool, McpServer

    changed = 0
    async with SessionLocal() as session:
        for tool in (await session.execute(select(CustomTool))).scalars():
            headers = (tool.config or {}).get("headers")
            if isinstance(headers, dict) and not is_sealed(headers):
                tool.config = {**(tool.config or {}), "headers": seal(headers)}
                changed += 1
        for server in (await session.execute(select(McpServer))).scalars():
            if not is_sealed(server.env) or not is_sealed(server.headers):
                server.env, server.headers = seal(server.env), seal(server.headers)
                changed += 1
        if changed:
            await session.commit()
    return changed
