"""面向用户的报错：说清发生了什么、为什么、怎么办，不把 Python 异常类名端上去。

以前各处都是 f"{type(e).__name__}: {e}"，界面首行红字是
「_make_query_tool.<locals>._run() got an unexpected keyword argument 'query'」
——对业务用户是天书，也没有下一步。原始异常不丢：调用方把 raw(e) 放进响应或
事件的 detail 字段、写进日志，排查时照样找得到。

这里曾经是两份：接口层一份（测连接这类回「原因 + 怎么办」），执行层一份（节点
报错首行只要原因）。两份各认一套异常类名、各写一套说法，同一个 401 在设置页和
运行记录里是两句话。现在认异常的规则只有下面这一张表，两个入口只在一件事上分叉：
TypeError、KeyError 这类「程序自己的毛病」——

- 接口里冒出来的，是后端的 bug，原文里全是内部符号，说「后端内部出错了」；
- 节点里冒出来的，多半是工具被传错了参数、用户代码取错了字段，说清是哪个参数、
  哪个字段，模型和人才知道该改什么。

判断按类名走 MRO，不 import 各家 SDK：没装的驱动和 SDK 不该让报错这一层也跟着
ImportError。
"""
from __future__ import annotations

import json
import re
from typing import Any

_TIMEOUT = {"TimeoutError", "TimeoutException", "APITimeoutError", "ReadTimeout",
            "ConnectTimeout", "PoolTimeout", "WriteTimeout"}
_NETWORK = {"ConnectError", "ConnectionError", "APIConnectionError", "gaierror",
            "ClientConnectorError", "RemoteProtocolError", "ReadError", "NetworkError"}
_AUTH = {"AuthenticationError", "PermissionDeniedError", "InvalidPasswordError",
         "InvalidAuthorizationSpecificationError"}
#: 这几类是程序自己的毛病，str(e) 里全是内部符号，原样端上去只会更糊涂
_BUG = {"TypeError", "AttributeError", "KeyError", "NameError", "IndexError",
        "UnboundLocalError", "AssertionError", "RecursionError"}
_UNREACHABLE = ("connection refused", "nodename nor servname", "name or service not known",
                "all connection attempts failed", "network is unreachable")

#: SQLAlchemy 包过的驱动异常开头是「(sqlite3.OperationalError) 」
_WRAPPED = re.compile(r"^\([\w.]+\)\s*")
# 内部符号：`a.b.<locals>._run()`、`<function f at 0x…>`、模块路径。对使用者没有意义
_LOCALS = re.compile(r"[\w.]*<locals>\.?[\w.]*(\(\))?")
_REPR = re.compile(r"<(?:function|bound method|coroutine|object) [^>]*>")
# 包在消息里的异常类名（"连不上：ConnectError: All connection attempts failed"）
_CLASS = re.compile(r"\b[A-Z]\w*(?:Error|Exception|Warning)\b:\s*")


def raw(e: BaseException) -> str:
    """原始报错，给「技术细节」折叠区、事件的 detail 字段和日志用。"""
    text = str(e).strip()
    return (f"{type(e).__name__}: {text}" if text else type(e).__name__)[:2000]


def raw_detail(e: BaseException | None) -> str | None:
    """同 raw，没有异常时是 None——事件里 detail 字段就不带。"""
    return None if e is None else raw(e)


def first_line(e: BaseException) -> str:
    """异常自己的说明，去掉驱动包装的类名前缀和 SQLAlchemy 的文档链接。"""
    text = str(e).strip()
    line = text.splitlines()[0] if text else ""
    return _WRAPPED.sub("", line)[:300]


def _names(e: BaseException) -> set[str]:
    return {c.__name__ for c in type(e).__mro__}


def _status(e: BaseException) -> int | None:
    for obj in (e, getattr(e, "response", None)):
        code = getattr(obj, "status_code", None)
        if isinstance(code, int):
            return code
    return None


def _scrub(text: str) -> str:
    text = _LOCALS.sub("内部函数", text)
    text = _REPR.sub("内部对象", text)
    text = _CLASS.sub("", text)
    return " ".join(text.split())[:240]


def _known(e: BaseException, *, sniff: bool) -> tuple[str, str] | None:
    """两种场景说法一样的那些：鉴权、找不到、限流、对方故障、超时、网络、格式、文件。

    sniff：类名认不出时要不要按原文字面猜。SDK 抛出来的裸异常要猜（原文就是
    "Connection refused"）；执行层的异常常常是已经拼好的一句话、里头套着具体原因，
    按字面猜会把那个原因换成笼统的一句。
    """
    names = _names(e)
    low = str(e).lower() if sniff else ""
    status = _status(e)

    if status in (401, 403) or names & _AUTH or "invalid api key" in low or "unauthorized" in low:
        code = f"（{status}）" if status else ""
        return (f"鉴权没通过{code}：对方拒绝了这把密钥",
                "核对 API Key 有没有填错、过期，或者这个账号有没有这个模型的权限")
    if status == 404 or "NotFoundError" in names:
        return "对方说找不到（404）", "核对地址的路径（常见的是少了或多了 /v1）和模型名"
    if status == 429 or "RateLimitError" in names:
        return "请求太频繁，或者额度用完了（429）", "等一会儿再试；频繁出现就检查账号额度"
    if isinstance(status, int) and status >= 500:
        return f"对方服务出错了（HTTP {status}）", "这是对方的问题，多半是暂时的，稍后再试"
    if isinstance(e, TimeoutError) or names & _TIMEOUT or "timed out" in low:
        return ("等待超时：对方没有在限定时间内响应",
                "对方可能很忙或者网络不稳，稍后再试；一直超时就检查地址和网络")
    if isinstance(e, ConnectionError) or names & _NETWORK or any(s in low for s in _UNREACHABLE):
        return ("连不上对方的服务：网络不通，或者服务没有启动",
                "核对地址和端口，确认服务已经启动、这台机器访问得到它")
    if isinstance(e, json.JSONDecodeError):
        return "返回的内容不是合法的 JSON", "地址多半指到了网页而不是 API，核对 Base URL"
    if "ValidationError" in names and callable(getattr(e, "errors", None)):
        return f"参数不对：{_first_validation(e)}", ""
    if isinstance(e, PermissionError):
        return "没有权限访问需要的文件或资源", ""
    if isinstance(e, FileNotFoundError):
        return f"找不到文件{'「' + str(e.filename) + '」' if e.filename else ''}", ""
    if isinstance(e, (OverflowError, MemoryError)):
        text = str(e).strip()
        return f"数据超出了能处理的范围{'：' + _scrub(text) if text else ''}", ""
    return None


def explain(e: BaseException) -> tuple[str, str]:
    """接口层：(原因, 怎么办)。原因是一句人话；说不出怎么办时第二项是空串。"""
    known = _known(e, sniff=True)
    if known:
        return known
    if _names(e) & _BUG:
        return "后端内部出错了", "这不是你操作的问题；把技术细节发给维护者"
    return first_line(e) or "出了一个没有说明的错误", ""


def describe_exception(e: BaseException) -> str:
    """执行层：原因，一句话。「发生了什么」和「怎么办」由节点按场景拼。"""
    text = str(e).strip()
    if isinstance(e, TypeError):
        if m := re.search(r"unexpected keyword argument '(\w+)'", text):
            return f"参数对不上：它不接受名为「{m.group(1)}」的参数"
        if m := re.search(r"missing \d+ required (?:positional |keyword-only )?arguments?: (.+)$", text):
            wanted = "、".join(re.findall(r"'(\w+)'", m.group(1))) or m.group(1)
            return f"缺少必填参数「{wanted}」"
    if isinstance(e, KeyError):
        return f"缺少字段「{e.args[0] if e.args else '?'}」"
    known = _known(e, sniff=False)
    if known:
        return known[0]
    return _scrub(text) or "没有给出原因的内部错误"


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
