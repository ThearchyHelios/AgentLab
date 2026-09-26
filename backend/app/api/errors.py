"""面向用户的报错：说清发生了什么、为什么、怎么办，不把 Python 异常类名端上去。

以前各处都是 f"{type(e).__name__}: {e}"，界面首行红字是
「_make_query_tool.<locals>._run() got an unexpected keyword argument 'query'」
——对业务用户是天书，也没有下一步。原始异常不丢：调用方把 raw(e) 放进响应的
detail 字段或写进日志，排查时照样找得到。

判断按类名走 MRO，不 import 各家 SDK：没装的驱动和 SDK 不该让报错这一层
也跟着 ImportError。
"""
from __future__ import annotations

import re
from typing import Any

_TIMEOUT = {"TimeoutError", "TimeoutException", "APITimeoutError", "ReadTimeout",
            "ConnectTimeout", "PoolTimeout", "WriteTimeout"}
_NETWORK = {"ConnectError", "ConnectionError", "APIConnectionError", "gaierror",
            "ClientConnectorError", "RemoteProtocolError"}
_AUTH = {"AuthenticationError", "PermissionDeniedError", "InvalidPasswordError",
         "InvalidAuthorizationSpecificationError"}
#: 这几类是程序自己的毛病，str(e) 里全是内部符号，原样端上去只会更糊涂
_BUG = {"TypeError", "AttributeError", "KeyError", "NameError", "IndexError",
        "UnboundLocalError", "AssertionError", "RecursionError"}

#: SQLAlchemy 包过的驱动异常开头是「(sqlite3.OperationalError) 」
_WRAPPED = re.compile(r"^\([\w.]+\)\s*")


def raw(e: BaseException) -> str:
    """原始报错，给「技术细节」折叠区和日志用。"""
    return f"{type(e).__name__}: {e}"


def _status(e: BaseException) -> int | None:
    for obj in (e, getattr(e, "response", None)):
        code = getattr(obj, "status_code", None)
        if isinstance(code, int):
            return code
    return None


def first_line(e: BaseException) -> str:
    """异常自己的说明，去掉驱动包装的类名前缀和 SQLAlchemy 的文档链接。"""
    text = str(e).strip()
    line = text.splitlines()[0] if text else ""
    return _WRAPPED.sub("", line)[:300]


def explain(e: BaseException) -> tuple[str, str]:
    """(原因, 怎么办)。原因是一句人话；说不出怎么办时第二项是空串。"""
    names = {c.__name__ for c in type(e).__mro__}
    low = str(e).lower()
    status = _status(e)

    if status in (401, 403) or names & _AUTH or "invalid api key" in low or "unauthorized" in low:
        return "对方拒绝了这把密钥", "核对 API Key 有没有填错、过期，或者这个账号有没有权限"
    if status == 404 or "NotFoundError" in names:
        return "对方说找不到", "核对地址的路径（常见的是少了或多了 /v1）和模型名"
    if status == 429 or "RateLimitError" in names:
        return "请求被对方限流了", "等一会儿再试；频繁出现就检查账号额度"
    if isinstance(status, int) and status >= 500:
        return f"对方服务出错了（HTTP {status}）", "这是对方的问题，稍后再试"
    if names & _TIMEOUT or "timed out" in low:
        return "等了太久没有响应", "对方可能很忙或者网络不稳，稍后再试；一直超时就检查地址和网络"
    if names & _NETWORK or any(s in low for s in (
        "connection refused", "nodename nor servname", "name or service not known",
        "all connection attempts failed", "network is unreachable",
    )):
        return "连不上对方的服务", "核对地址和端口，确认服务已经启动、这台机器访问得到它"
    if "JSONDecodeError" in names:
        return "对方返回的不是 JSON", "地址多半指到了网页而不是 API，核对 Base URL"
    if "ValidationError" in names and callable(getattr(e, "errors", None)):
        return f"参数不对：{_first_validation(e)}", ""
    if names & _BUG:
        return "后端内部出错了", "这不是你操作的问题；把技术细节发给维护者"
    return first_line(e) or "出了一个没有说明的错误", ""


def message(e: BaseException, lead: str = "") -> str:
    """一整句：lead + 原因 + 怎么办。给只能放一个字符串的地方（HTTPException 的 detail）。"""
    reason, hint = explain(e)
    text = f"{lead}{reason}" if lead else reason
    return f"{text}。{hint}" if hint else text


def payload(e: BaseException, lead: str = "") -> dict[str, Any]:
    """{ok: false, error, hint, detail}：测试连接这类「失败也是正常结果」的接口用。"""
    reason, hint = explain(e)
    return {"ok": False, "error": f"{lead}{reason}", "hint": hint, "detail": raw(e)}


def not_configured(e: BaseException) -> str:
    """模型接入没配好时的原话用的是内部叫法（provider 'x'、base_url），换成设置页上的说法。"""
    text = re.sub(r"provider '([^']*)'", r"模型接入「\1」", str(e))
    text = re.sub(r"模型 '([^']*)'", r"模型「\1」", text)
    return text.replace("base_url", "Base URL").rstrip("。")


def _has_cjk(text: str) -> bool:
    return any("一" <= ch <= "鿿" for ch in text)


def _first_validation(e: Any) -> str:
    err = (e.errors() or [{}])[0]
    loc = ".".join(str(p) for p in err.get("loc") or ())
    msg = str(err.get("msg") or "").removeprefix("Value error, ")
    if _has_cjk(msg):
        return msg
    return f"「{loc}」{'缺了' if err.get('type') == 'missing' else '格式不对'}" if loc else "格式不对"


def graph_error(e: BaseException) -> str:
    """工作流结构读不懂时，指到是哪个节点、哪一项。

    pydantic 的原文是一大段英文，还列了全部十几种节点类型和一个文档链接，
    用户从里面找不到自己错在哪。
    """
    errors = e.errors() if callable(getattr(e, "errors", None)) else None
    if not errors:
        return explain(e)[0]
    err = errors[0]
    loc = list(err.get("loc") or ())
    msg = str(err.get("msg") or "").removeprefix("Value error, ")
    more = f"（另有 {len(errors) - 1} 处）" if len(errors) > 1 else ""

    if len(loc) >= 2 and loc[0] in ("nodes", "edges") and isinstance(loc[1], int):
        where = f"第 {loc[1] + 1} 个{'节点' if loc[0] == 'nodes' else '连线'}"
        field = str(loc[2]) if len(loc) > 2 else ""
        if err.get("type") == "enum" and field == "type":
            return f"{where}的类型「{err.get('input')}」不认识{more}"
        if err.get("type") == "missing":
            return f"{where}缺了「{field}」{more}"
        if _has_cjk(msg):
            return f"{where}：{msg}{more}"
        return f"{where}的「{field or '内容'}」格式不对{more}"
    if _has_cjk(msg):
        return msg + more
    return f"「{'.'.join(str(p) for p in loc) or '整体'}」格式不对{more}"
