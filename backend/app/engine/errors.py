"""把异常翻成给人看的一句话。

界面上出现过的原样报错：「工具 db_query__shop 执行失败：_make_query_tool.<locals>._run()
got an unexpected keyword argument 'query'」「TypeError: Integer exceeds 64-bit range」。
问数据的业务用户读不懂这些，也不知道下一步该做什么。

这里只管"原因"那半句，不带异常类名和内部符号；"发生了什么"和"怎么办"由调用方
按场景拼。原始异常另外放进事件的 detail 字段、写进日志，排查的人照样拿得到。
"""
from __future__ import annotations

import json
import re

# 供应商 SDK / httpx 的网络类异常。按类名认而不 import：这些包不一定都装了，
# 而这里只需要知道"是不是连不上"
_NETWORK = {"ConnectError", "ConnectTimeout", "APIConnectionError", "RemoteProtocolError",
            "ReadError", "NetworkError"}
_TIMEOUT = {"ReadTimeout", "WriteTimeout", "PoolTimeout", "APITimeoutError", "TimeoutException"}

# 内部符号：`a.b.<locals>._run()`、`<function f at 0x…>`、模块路径。对使用者没有意义
_LOCALS = re.compile(r"[\w.]*<locals>\.?[\w.]*(\(\))?")
_REPR = re.compile(r"<(?:function|bound method|coroutine|object) [^>]*>")
# 包在消息里的异常类名（"连不上：ConnectError: All connection attempts failed"）
_CLASS = re.compile(r"\b[A-Z]\w*(?:Error|Exception|Warning)\b:\s*")


def _names(exc: BaseException) -> set[str]:
    return {cls.__name__ for cls in type(exc).__mro__}


def _scrub(text: str) -> str:
    text = _LOCALS.sub("内部函数", text)
    text = _REPR.sub("内部对象", text)
    text = _CLASS.sub("", text)
    return " ".join(text.split())[:240]


def describe_exception(exc: BaseException) -> str:
    """原因，一句话。常见的几类写成固定说法，其余的去掉内部符号后照实说。"""
    names = _names(exc)
    text = str(exc).strip()

    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        if status in (401, 403):
            return f"鉴权没通过（{status}）：key 无效、过期，或没有这个模型的权限"
        if status == 429:
            return "请求太频繁，或者额度用完了（429）"
        if status >= 500:
            return f"对方服务出错了（{status}），多半是暂时的"

    if isinstance(exc, TypeError):
        if m := re.search(r"unexpected keyword argument '(\w+)'", text):
            return f"参数对不上：它不接受名为「{m.group(1)}」的参数"
        if m := re.search(r"missing \d+ required (?:positional |keyword-only )?arguments?: (.+)$", text):
            wanted = "、".join(re.findall(r"'(\w+)'", m.group(1))) or m.group(1)
            return f"缺少必填参数「{wanted}」"
    if isinstance(exc, TimeoutError) or names & _TIMEOUT:
        return "等待超时：对方没有在限定时间内响应"
    if isinstance(exc, ConnectionError) or names & _NETWORK:
        return "连不上对方服务：网络不通，或者服务没有启动"
    if isinstance(exc, PermissionError):
        return "没有权限访问需要的文件或资源"
    if isinstance(exc, FileNotFoundError):
        return f"找不到文件{'「' + str(exc.filename) + '」' if exc.filename else ''}"
    if isinstance(exc, json.JSONDecodeError):
        return "返回的内容不是合法的 JSON"
    if isinstance(exc, KeyError):
        return f"缺少字段「{exc.args[0] if exc.args else '?'}」"
    if isinstance(exc, (OverflowError, MemoryError)):
        return f"数据超出了能处理的范围{'：' + _scrub(text) if text else ''}"
    return _scrub(text) or "没有给出原因的内部错误"


def raw_detail(exc: BaseException | None) -> str | None:
    """原始异常，给排查的人看。只进事件的 detail 字段和日志，不进首行报错。"""
    if exc is None:
        return None
    text = str(exc).strip()
    return f"{type(exc).__name__}: {text}"[:2000] if text else type(exc).__name__
