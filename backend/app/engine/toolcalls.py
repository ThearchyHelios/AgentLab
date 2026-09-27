"""节点里调工具的公共部分：时限，以及「模型把工具调用写成了文字」的识别。

agent 节点、协作成员、工具节点三处都在调工具。以前时限只有数据层自己那一道
wait_for，而 wait_for 取消之后要等被取消的那一方收完尾：驱动关连接、回滚要排在
数据库那条语句后面。真实运行里一次「查询超过 30s 被中断」实际等了 90 秒——时限
到了、取消也发了，控制权却迟迟回不来，报错写的还是 30。
"""
from __future__ import annotations

import asyncio
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
            text = (f"查询超过 {seconds}s 没有返回，已放弃等待（数据库那边可能还在跑，连接会在后台"
                    "收回）。加上 WHERE 条件或 LIMIT 缩小范围再查")
        else:
            text = f"工具 {name} 超过 {seconds}s 没有返回，已放弃等待"
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
    r"<[｜|]{1,2}\s*DSML\s*[｜|]{1,2}"                      # DeepSeek 的 DSML
    r"|<[｜|]\s*tool[▁_ ]calls?[▁_ ]begin\s*[｜|]>"          # DeepSeek 的特殊 token
    r"|<\s*(?:tool_call|function_call)\s*>"
    r"|<\s*invoke\s+name\s*=",
    re.IGNORECASE,
)
# 只有一段 {"tool": …, "sql": …} 不算：正文里引用一段 JSON 很正常。
# 得同时自称是「假设」调的，才是在编
_TOOL_JSON = re.compile(r'\{\s*"tool"\s*:')
_SQL_KEY = re.compile(r'"sql"\s*:')
_PRETEND = re.compile(r"假设(?:调用|使用|执行)了?(?:工具|查询)|假装调用|模拟调用(?:了)?工具")

TOOL_MARKUP_ERROR = (
    "模型输出了工具调用的原始标记，但没有真正调用工具，这一步一次都没查到数据。"
    "常见原因：节点没有绑定工具，或者模型、服务不支持工具调用。"
    "到画布里给这个节点绑定要用的工具；绑定了还这样，就换一个支持工具调用的模型"
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
    return f"{who}把工具调用写成了文字（{snippet[:40]}…），没有真正调用工具"
