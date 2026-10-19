"""节点里调工具的公共部分：时限，以及「模型把工具调用写成了文字」的识别。

agent 节点、协作成员、工具节点三处都在调工具。以前时限只有数据层自己那一道
wait_for，而 wait_for 取消之后要等被取消的那一方收完尾：驱动关连接、回滚要排在
数据库那条语句后面。真实运行里一次「查询超过 30s 被中断」实际等了 90 秒——时限
到了、取消也发了，控制权却迟迟回不来，报错写的还是 30。
"""
from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from typing import Any, Awaitable

QUERY_PREFIX = "db_query__"

#: 到了工具自己的时限之后再宽限多久才放弃。工具自己的超时报错（「查询超过 30s
#: 被中断，加上 WHERE…」）比这里的说法更具体，先让它到；收尾卡住了才由这里兜底
GRACE_S = 1.0

#: 沙箱代码工具的时限来自参数（缺省 30 秒），由沙箱自己执行
_SANDBOX_TOOLS = {"python_exec", "shell_exec", "run_code"}
_HTTP_TOOLS = {"http_request", "web_fetch"}


@dataclass(frozen=True)
class Limit:
    """一次工具调用的时限。seconds 随 tool.start 发给界面，界面据此说「已超出 30s 上限」。"""

    seconds: float
    #: 到点由引擎收回控制权。只对「时限到了、收尾却可能卡住」的类型开：沙箱的时限
    #: 里还含着起虚拟机、装依赖，按它掐会误杀冷启动；HTTP 客户端自己掐得准
    enforce: bool
    #: query | declared | sandbox | http
    kind: str

    @property
    def self_timed(self) -> bool:
        """工具自己也按这个时限掐、并且自己收尾（数据层的查询）。引擎放弃它时只撒手、
        不再取消：它那时多半正在收尾，再取消一次会把关连接那一步打断，连接就再也回不到池里。"""
        return self.kind == "query"


def limit_of(tool: Any, name: str, args: dict[str, Any]) -> Limit | None:
    """这次调用的时限。说不出来就是 None——界面上不写上限，引擎也不掐。

    工具自己在 metadata["timeout_s"] 里声明的优先（数字，或者按参数算的函数）。
    """
    meta = (getattr(tool, "metadata", None) or {}) if tool is not None else {}
    declared = meta.get("timeout_s")
    if callable(declared):
        declared = declared(args)
    if not (isinstance(declared, (int, float)) and not isinstance(declared, bool) and declared > 0):
        declared = None
    if name.startswith(QUERY_PREFIX):
        # 数据层自己按这个时限掐、自己收尾。数据源声明了自己的时限就用它，
        # 否则按模块属性现取数据层的缺省——那个数只有这一处定义
        from app.data import guard

        return Limit(float(declared or guard.QueryLimits().timeout_seconds), True, "query")
    if declared:
        return Limit(float(declared), True, "declared")
    if name in _SANDBOX_TOOLS:
        try:
            seconds = float(args.get("timeout") or 30)
        except (TypeError, ValueError):
            seconds = 30.0
        return Limit(seconds, False, "sandbox")
    if name in _HTTP_TOOLS:
        from app.core.config import settings

        return Limit(float(settings.http_tool_timeout), False, "http")
    return None


def limit_fields(limit: Limit | None) -> dict[str, Any]:
    """tool.start 上附带的时限字段。没有时限就什么都不带，老客户端照旧。"""
    if limit is None:
        return {}
    return {"timeout_s": int(limit.seconds) if limit.seconds.is_integer() else limit.seconds}


def _seconds(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:g}"


class ToolTimeout(TimeoutError):
    """引擎等到时限就放弃了。消息直接给模型和人看：等了多久、为什么放弃、怎么办。"""

    def __init__(self, name: str, limit: Limit) -> None:
        self.name = name
        self.limit = limit
        seconds = _seconds(limit.seconds)
        if limit.kind == "query":
            text = (f"查询超过 {seconds} 秒没有返回，已停止等待（数据库可能仍在执行，连接会在后台"
                    "回收）。请添加 WHERE 条件或 LIMIT 缩小范围后重试")
        else:
            text = f"工具 {name} 超过 {seconds} 秒没有返回，已停止等待"
        super().__init__(text)


async def run_bounded(call: Awaitable[Any], limit: Limit | None, name: str) -> Any:
    """跑一次工具调用；到了时限（加一点宽限）就收回控制权，不等它收尾。

    被放弃的那次调用照样被取消，只是它的收尾（关连接、回滚）挪到后台去做完。
    """
    if limit is None or not limit.enforce:
        return await call
    job = asyncio.ensure_future(call)
    try:
        done, _ = await asyncio.wait({job}, timeout=limit.seconds + GRACE_S)
    except asyncio.CancelledError:
        # 整次运行被取消（用户停止、服务关停）：底下那次调用也不该接着跑
        _abandon(job)
        raise
    if job in done:
        return job.result()
    _abandon(job, cancel=not limit.self_timed)
    raise ToolTimeout(name, limit)


def _abandon(job: asyncio.Future[Any], *, cancel: bool = True) -> None:
    if cancel:
        job.cancel()
    # 结果没人要了；把异常取走，免得事件循环报 "exception was never retrieved"
    job.add_done_callback(lambda f: f.cancelled() or f.exception())


# --------------------------------------------------------------------------
# 模型把工具调用写成了文字
#
# 有的模型（或它前面那层网关）不认工具调用接口、或者节点根本没绑工具，模型就把自己
# 训练时那套调用格式当正文吐出来。这一步没有任何真实调用，数据一次都没查到，而文字
# 看上去像是「在查」。认出来之后先纠正一次，仍然如此就判失败。
# --------------------------------------------------------------------------

_MARKUP = re.compile(
    r"</?\s*[｜|]{1,2}\s*DSML\s*[｜|]{1,2}"                  # DeepSeek 的 DSML
    r"|<[｜|]\s*tool[▁_ ]calls?[▁_ ]begin\s*[｜|]>"          # DeepSeek 的特殊 token
    r"|<\s*(?:tool_calls?|function_calls?)\s*>"
    r"|<\s*invoke\s+name\s*=",
    re.IGNORECASE,
)
# 只有一段 {"tool": …, "sql": …} 不算：正文里引用一段 JSON 很正常。
# 得同时自称是「假设」调的，才是在编
_TOOL_JSON = re.compile(r'\{\s*"tool"\s*:')
_SQL_KEY = re.compile(r'"sql"\s*:')
_PRETEND = re.compile(r"假设(?:调用|使用|执行)了?(?:工具|查询)|假装调用|模拟调用(?:了)?工具")

TOOL_MARKUP_ERROR = (
    "模型以文本形式输出了工具调用的原始标记，未实际调用工具，这一步没有查询到任何数据。"
    "常见原因：节点未绑定工具，或模型、服务不支持工具调用。"
    "请在画布中为该节点绑定所需工具；如已绑定仍出现此问题，请换用支持工具调用的模型"
)

TOOL_MARKUP_NUDGE = (
    "你上一条回复把工具调用写成了文字，系统没有执行它，也就没有拿到任何数据。"
    "要用工具，请通过工具调用接口发起，不要把调用格式写进正文；手上没有能用的工具，"
    "就直接说明查不到、缺什么，不要假设调用的结果。"
)


def leaked_markup(text: str | None) -> str | None:
    """正文里露出来的工具调用标记（截一小段，给警告用）。没有就是 None。"""
    if not text:
        return None
    if m := _MARKUP.search(text):
        return m.group(0)
    if _TOOL_JSON.search(text) and _SQL_KEY.search(text) and (m := _PRETEND.search(text)):
        return m.group(0)
    return None


def markup_warning(snippet: str, who: str = "模型") -> str:
    return f"{who}以文本形式输出了工具调用（{snippet[:40]}…），未实际调用工具"


# --------------------------------------------------------------------------
# 把写成文字的工具调用读出来
#
# 助手（app/api/copilot_gen）不走工具调用接口：操作（look、add_node……）是正文里一行一行的 JSON。可模型偶尔还是按自己训练时
# 那套调用格式写（部署上撞过：DeepSeek 想先 look 看表，思考里写着「先输出 look」，正文却是一段 DSML），意图和参数都对，
# 只是格式不对。助手按名字把它们读成操作；节点里的 agent 仍然只认、不读（真要调工具得走接口，见上面的纠正）。
#
# 认得的几种写法：
#   DSML（DeepSeek）       <｜DSML｜invoke name="look"><｜DSML｜parameter name="need" string="true">…</｜DSML｜parameter>
#                          </｜DSML｜invoke>，外面可能包一层 function_calls / calls；竖线一到两个、中间可能有空格
#   <invoke>               同上去掉 DSML 前缀（<function_calls><invoke name="…"><parameter name="…">…）
#   <tool_call>            <tool_call>{"name": "look", "arguments": {…}}</tool_call>（function_call 同）
#   DeepSeek 特殊 token    <｜tool▁call▁begin｜>function<｜tool▁sep｜>look ```json {…}```<｜tool▁call▁end｜>，
#                          或者 <｜tool▁call▁begin｜>look<｜tool▁sep｜>{…}<｜tool▁call▁end｜>
# --------------------------------------------------------------------------

_DSML_PREFIX = re.compile(r"<(/?)\s*[｜|]{1,2}\s*DSML\s*[｜|]{1,2}\s*", re.IGNORECASE)
_INVOKE = re.compile(r"<invoke\s+name\s*=\s*[\"']([^\"']*)[\"']\s*>(.*?)</invoke\s*>", re.DOTALL | re.IGNORECASE)
_PARAMETER = re.compile(r"<parameter\s+name\s*=\s*[\"']([^\"']*)[\"']([^>]*)>(.*?)</parameter\s*>",
                        re.DOTALL | re.IGNORECASE)
_PARAMETER_OPEN = re.compile(r"<parameter\b", re.IGNORECASE)
_PARAMETER_CLOSE = re.compile(r"</parameter\s*>", re.IGNORECASE)
_STRING_ATTR = re.compile(r"string\s*=\s*[\"'](true|false)[\"']", re.IGNORECASE)
_JSON_CALL = re.compile(r"<(tool_call|function_call)\s*>(.*?)</\1\s*>", re.DOTALL | re.IGNORECASE)
_TOKEN_CALL = re.compile(r"<[｜|]\s*tool[▁_ ]call[▁_ ]begin\s*[｜|]>(.*?)<[｜|]\s*tool[▁_ ]sep\s*[｜|]>(.*?)"
                         r"<[｜|]\s*tool[▁_ ]call[▁_ ]end\s*[｜|]>", re.DOTALL | re.IGNORECASE)
#: 一次调用的开头：在它之后还没等到结尾的，留着等后面的行
_CALL_OPENER = re.compile(r"<invoke\s+name\s*=|<(?:tool_call|function_call)\s*>"
                          r"|<[｜|]\s*tool[▁_ ]call[▁_ ]begin\s*[｜|]>", re.IGNORECASE)
_FENCE = re.compile(r"^`{3,}[A-Za-z0-9_-]*\s*|\s*`{3,}$")
#: 参数值像 JSON 的（没写 string 属性时按 JSON 读）
_JSONISH = re.compile(r"^\s*(?:[\[{\"]|-?\d|true\b|false\b|null\b)")


@dataclass(frozen=True)
class LeakedCall:
    """写成文字的一次工具调用。args 是 None：参数读不出来（名字照样交出去，由调用方记进诊断）。"""

    name: str
    args: dict[str, Any] | None


def _parameter_value(raw: str, attrs: str) -> Any:
    """DSML 的 string="true" 是原样的字符串，string="false" 是 JSON；没写的看着像 JSON 就按 JSON 读，读不成照原样。"""
    value = raw.strip()
    flag = _STRING_ATTR.search(attrs)
    if flag and flag.group(1).lower() == "true":
        return value
    if flag or _JSONISH.match(value):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def _invoke_call(name: str, body: str) -> LeakedCall:
    return LeakedCall(name.strip(), {key.strip(): _parameter_value(raw, attrs)
                                     for key, attrs, raw in _PARAMETER.findall(body)})


def _json_call(body: str) -> LeakedCall:
    """<tool_call>{"name": …, "arguments": {…}}</tool_call>：arguments 也可能是一段 JSON 字符串。"""
    try:
        obj = json.loads(body.strip())
    except ValueError:
        return LeakedCall("", None)
    if not isinstance(obj, dict):
        return LeakedCall("", None)
    function = obj.get("function") if isinstance(obj.get("function"), dict) else {}
    name = str(obj.get("name") or function.get("name") or "")
    args = next((v for v in (obj.get("arguments"), obj.get("parameters"), obj.get("args"),
                             function.get("arguments")) if v is not None), {})
    if isinstance(args, str):
        try:
            args = json.loads(args) if args.strip() else {}
        except ValueError:
            args = None
    return LeakedCall(name, args if isinstance(args, dict) else None)


def _token_call(head: str, body: str) -> LeakedCall:
    """DeepSeek 特殊 token 的两种排法：sep 前是 function、名字在 sep 后第一行；或者 sep 前就是名字。参数可能套着围栏。"""
    head, body = head.strip(), body.strip()
    if head.lower() in ("", "function"):
        name, _, body = body.partition("\n")
    else:
        name = head
    body = _FENCE.sub("", body.strip()).strip()
    try:
        args = json.loads(body) if body else {}
    except ValueError:
        args = None
    return LeakedCall(name.strip(), args if isinstance(args, dict) else None)


#: 流读完了、最后一次调用还差结尾（模型写完参数就停了）时补上的结尾，按开头的写法
_CLOSERS = (("<invoke", "</invoke>"), ("<tool_call", "</tool_call>"), ("<function_call", "</function_call>"),
            ("<", "<｜tool▁call▁end｜>"))


def take_leaked_calls(text: str, *, final: bool = False) -> tuple[list[LeakedCall], str]:
    """从一段写成文字的工具调用里取出已经写完的调用（按出现的先后），交回 (调用, 还没写完的那一段)。还没写完的从最后一个
    没等到结尾的调用开头算起；外层的包装（function_calls、DSML 的 calls）、调用之间的说明文字不留——没有没写完的调用时交回
    空串。流式读的时候一行一行攒进来，每攒一行取一次。

    final：后面不会再来了。最后一次调用只差结尾的（参数都写完整了，模型写完就停了）补上结尾再读一次；参数写到一半的、
    补上也读不出名字和参数的，照样交回——拿半截参数当一次调用，比照实说没写完更糟。"""
    calls, rest = _take(text)
    if final and rest and len(_PARAMETER_OPEN.findall(rest)) == len(_PARAMETER_CLOSE.findall(rest)):
        closer = next(c for prefix, c in _CLOSERS if rest.lower().startswith(prefix))
        more, left = _take(rest + closer)
        if more and not left and all(c.name and c.args is not None for c in more):
            return calls + more, ""
    return calls, rest


def _take(text: str) -> tuple[list[LeakedCall], str]:
    text = _DSML_PREFIX.sub(r"<\1", text)
    found: list[tuple[int, int, LeakedCall]] = []
    for m in _INVOKE.finditer(text):
        found.append((m.start(), m.end(), _invoke_call(m.group(1), m.group(2))))
    for m in _JSON_CALL.finditer(text):
        found.append((m.start(), m.end(), _json_call(m.group(2))))
    for m in _TOKEN_CALL.finditer(text):
        found.append((m.start(), m.end(), _token_call(m.group(1), m.group(2))))
    found.sort(key=lambda f: f[0])
    calls: list[LeakedCall] = []
    end = 0
    for start, stop, call in found:
        if start < end:  # 套在别的调用里面的不另算
            continue
        calls.append(call)
        end = stop
    opener = _CALL_OPENER.search(text, end)
    return calls, text[opener.start():] if opener else ""


def has_markup(text: str) -> bool:
    """这一段里有没有工具调用标记（只认标记本身，不认 leaked_markup 那种「假设调用了」的说法）：流式读的时候据此开始攒。"""
    return bool(_MARKUP.search(text))
