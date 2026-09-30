"""面向用户的报错：说清发生了什么、为什么、怎么办，不把 Python 异常类名端上去。

以前各处都是 f"{type(e).__name__}: {e}"，界面首行红字是
「_make_query_tool.<locals>._run() got an unexpected keyword argument 'query'」
——对业务用户是天书，也没有下一步。原始异常不丢：调用方把 raw(e) 放进响应或
事件的 detail 字段、写进日志，排查时照样找得到。

这里曾经是两份：接口层一份（测连接这类回「原因 + 怎么办」），执行层一份（节点
报错首行只要原因）。两份各认一套异常类名、各写一套说法，同一个 401 在设置页和
运行记录里是两句话。现在认异常的规则只有下面这一张表，两个入口只在一件事上分叉：
TypeError、KeyError 这类「程序自己的毛病」——

- 接口里冒出来的，是后端的 bug，原文里全是内部符号，说「服务端内部错误」；
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


#: classify() 的返回值：错误属于哪一类。调用方按类别给场景专用的说法（比如测数据库连接时，
#: 网络不通要说主机和端口，不说 API），不要拿 explain() 返回的文字做比较——文字会改，类别不会
AUTH, NOT_FOUND, RATE_LIMIT, SERVER, TIMEOUT, NETWORK = "auth", "not_found", "rate_limit", "server", "timeout", "network"
BAD_JSON, VALIDATION, PERMISSION, FILE_MISSING, OVERFLOW, BUG = (
    "bad_json", "validation", "permission", "file_missing", "overflow", "bug")


def _known(e: BaseException, *, sniff: bool) -> tuple[str, str, str] | None:
    """两种场景说法一样的那些：鉴权、找不到、限流、服务方故障、超时、网络、格式、文件。
    返回 (类别, 原因, 怎么办)。

    sniff：类名认不出时要不要按原文字面猜。SDK 抛出来的裸异常要猜（原文就是
    "Connection refused"）；执行层的异常常常是已经拼好的一句话、里头套着具体原因，
    按字面猜会把那个原因换成笼统的一句。
    """
    names = _names(e)
    low = str(e).lower() if sniff else ""
    status = _status(e)

    if status in (401, 403) or names & _AUTH or "invalid api key" in low or "unauthorized" in low:
        code = f"（{status}）" if status else ""
        return (AUTH, f"鉴权失败{code}：服务方拒绝了当前 API Key",
                "请检查 API Key 是否填写正确、是否过期，以及该账号是否有权使用此模型")
    if status == 404 or "NotFoundError" in names:
        return (NOT_FOUND, "请求的地址或模型不存在（404）",
                "请检查地址路径（常见问题是多了或少了 /v1）和模型名")
    if status == 429 or "RateLimitError" in names:
        return RATE_LIMIT, "请求过于频繁或额度已用完（429）", "请稍后重试；频繁出现时请检查账号额度"
    if isinstance(status, int) and status >= 500:
        return SERVER, f"服务方内部错误（HTTP {status}）", "通常是暂时性故障，请稍后重试"
    if isinstance(e, TimeoutError) or names & _TIMEOUT or "timed out" in low:
        return (TIMEOUT, "等待超时：服务方未在限定时间内响应",
                "服务方可能繁忙或网络不稳定，请稍后重试；持续超时请检查地址和网络")
    if isinstance(e, ConnectionError) or names & _NETWORK or any(s in low for s in _UNREACHABLE):
        return (NETWORK, "无法连接服务：网络不通或服务未启动",
                "请检查地址和端口，确认服务已启动且本机可以访问")
    if isinstance(e, json.JSONDecodeError):
        return BAD_JSON, "返回内容不是合法的 JSON", "地址可能指向了网页而非 API，请检查 Base URL"
    if "ValidationError" in names and callable(getattr(e, "errors", None)):
        return VALIDATION, f"参数有误：{_first_validation(e)}", ""
    if isinstance(e, PermissionError):
        return PERMISSION, "没有权限访问所需的文件或资源", ""
    if isinstance(e, FileNotFoundError):
        return FILE_MISSING, f"找不到文件{'「' + str(e.filename) + '」' if e.filename else ''}", ""
    if isinstance(e, (OverflowError, MemoryError)):
        text = str(e).strip()
        return OVERFLOW, f"数据超出了可处理的范围{'：' + _scrub(text) if text else ''}", ""
    return None


def classify(e: BaseException) -> str | None:
    """错误属于哪一类（上面那组常量），认不出返回 None。和 explain() 的判断是同一套规则。"""
    known = _known(e, sniff=True)
    if known:
        return known[0]
    return BUG if _names(e) & _BUG else None


def explain(e: BaseException) -> tuple[str, str]:
    """接口层：(原因, 怎么办)。原因是一句人话；说不出怎么办时第二项是空串。
    要按错误类别换说法的，用 classify() 拿类别，不要比较这里返回的文字。"""
    known = _known(e, sniff=True)
    if known:
        return known[1], known[2]
    if _names(e) & _BUG:
        return "服务端内部错误", "与你的操作无关，请将技术细节提供给维护者"
    return first_line(e) or "发生了未说明原因的错误", ""


def describe_exception(e: BaseException) -> str:
    """执行层：原因，一句话。「发生了什么」和「怎么办」由节点按场景拼。"""
    text = str(e).strip()
    if isinstance(e, TypeError):
        if m := re.search(r"unexpected keyword argument '(\w+)'", text):
            return f"参数不匹配：不接受名为「{m.group(1)}」的参数"
        if m := re.search(r"missing \d+ required (?:positional |keyword-only )?arguments?: (.+)$", text):
            wanted = "、".join(re.findall(r"'(\w+)'", m.group(1))) or m.group(1)
            return f"缺少必填参数「{wanted}」"
    if isinstance(e, KeyError):
        return f"缺少字段「{e.args[0] if e.args else '?'}」"
    known = _known(e, sniff=False)
    if known:
        return known[1]
    return _scrub(text) or "内部错误（未提供原因）"


def message(e: BaseException, lead: str = "") -> str:
    """一整句：lead + 原因 + 怎么办。给只能放一个字符串的地方（HTTPException 的 detail）。"""
    reason, hint = explain(e)
    text = f"{lead}{reason}" if lead else reason
    return f"{text}。{hint}" if hint else text


def payload(e: BaseException, lead: str = "") -> dict[str, Any]:
    """{ok: false, error, hint, detail}：测试连接这类「失败也是正常结果」的接口用。"""
    reason, hint = explain(e)
    return {"ok": False, "error": f"{lead}{reason}", "hint": hint, "detail": raw(e)}


def not_configured(e: BaseException, *, with_hint: bool = True) -> str:
    """模型接入没配好（ProviderNotConfigured）给用户看的那句话。运行失败原因、助手、设置页都经过这里。

    with_hint=False 只要「缺了什么」、不要「去哪里补」：设置页的测试弹窗本来就在接入的编辑框里，
    助手那边自己另说去哪里。别处抛的同类异常（没有 reason 字段）原话里可能还是内部叫法
    （provider 'x'、base_url），一并换成设置页上的说法。"""
    text = str(e) if with_hint else str(getattr(e, "reason", "") or e)
    text = re.sub(r"provider '([^']*)'", r"模型接入「\1」", text)
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
    if not loc:
        return "格式有误"
    return f"缺少「{loc}」" if err.get("type") == "missing" else f"「{loc}」格式有误"


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
            return f"{where}的类型「{err.get('input')}」无法识别{more}"
        if err.get("type") == "missing":
            return f"{where}缺少「{field}」{more}"
        if _has_cjk(msg):
            return f"{where}：{msg}{more}"
        return f"{where}的「{field or '内容'}」格式有误{more}"
    if _has_cjk(msg):
        return msg + more
    return f"「{'.'.join(str(p) for p in loc) or '整体'}」格式有误{more}"
