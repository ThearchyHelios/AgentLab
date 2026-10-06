"""模型调用的输出额度：认出「输出到了上限被截断」，服务方嫌额度太大时退回小额度重来。

助手改图（api/copilot.py）、核查（engine/verify_agent.py：双盲复算、反方审稿、审批质询共用）和证据裁判（engine/judge.py）
共用这一份。会思考的模型思考也算在输出额度里：经 OpenAI 兼容接入的推理模型（DeepSeek、GLM、Kimi 这一类）收不到
thinking=off，Claude 开了 adaptive thinking 也一样。额度小了，想完就没额度写正文——回复是空的，或者 JSON 只写了一半。
这时要照实说是输出额度不够，不能当成「模型没按格式交」「没查到」。

- cut_off：这一块（流式）或这条回复是不是因为额度用完停下的
- truncated_error：这个异常是不是在说同一件事（openai 结构化输出解析时抛的 LengthFinishReasonError）
- budget_rejected：服务方一开口就嫌额度太大
- error_usage：截断的那次调用花掉的用量
- cut_words：给人看的「某某模型的输出达到上限（N token）被截断」
- BudgetFallback：先按大额度调，被嫌大时按小额度重来，之后都用小的

engine/nodes/llm.py（cite_fields 的抽取）、data/recipe_ai.py 各有一份同样的判断，是在这里之前写的，行为相同。
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

#: 服务方嫌额度太大时报错里会出现的字样：超过模型的输出上限（DeepSeek chat 系只到 8K），
#: 或者输入加额度超过上下文长度（本地 vLLM）
BUDGET_WORDS = ("max_tokens", "max_completion_tokens", "max_output_tokens", "maximum context length")
#: 截断类报错的原话：openai 的 LengthFinishReasonError（结构化输出解析时 finish_reason=length）
LENGTH_WORDS = ("length limit was reached", "finish_reason=length", "finish_reason='length'")


def cut_off(chunk: Any) -> str:
    """这一块是不是在说「额度用完了」：OpenAI 系是 finish_reason=length，Anthropic 是 stop_reason=max_tokens。"""
    meta = getattr(chunk, "response_metadata", None) or {}
    reason = meta.get("finish_reason") or meta.get("stop_reason")
    return str(reason) if reason in ("length", "max_tokens") else ""


def truncated_error(error: BaseException | None) -> bool:
    """这个异常是不是在说「输出被截断」：openai 的 LengthFinishReasonError，或者 langchain、网关转述的同一件事。

    服务方嫌额度太大的 400（报错里也有 max_tokens）不算：那是请求被拒，模型一个字都没写。
    """
    if error is None:
        return False
    if type(error).__name__ == "LengthFinishReasonError":
        return True
    text = str(error).lower()
    return any(word in text for word in LENGTH_WORDS)


def budget_rejected(e: BaseException) -> bool:
    status = getattr(e, "status_code", None) or getattr(getattr(e, "response", None), "status_code", None)
    return status in (400, 422) and any(w in str(e).lower() for w in BUDGET_WORDS)


def error_usage(error: BaseException | None) -> tuple[int, int]:
    """失败的那次调用花掉的 (输入, 输出) token。截断时 openai 的异常带着 completion.usage：额度是真花出去了，要记账，
    不然每日上限会漏掉最贵的那几次。别的异常没有用量，是 (0, 0)。"""
    usage = getattr(getattr(error, "completion", None), "usage", None)
    return int(getattr(usage, "prompt_tokens", 0) or 0), int(getattr(usage, "completion_tokens", 0) or 0)


def cut_words(who: str, limit: int | None) -> str:
    """「核查模型的输出达到上限（16384 token）被截断」。额度不知道（没给 max_tokens、按接入的默认值）就不写数。"""
    return f"{who}的输出达到上限{f'（{limit} token）' if limit else ''}被截断"


class _Switch:
    """几次调用共用的「退回小额度了没有」：核查的工具循环和收尾、裁判的各批。"""

    __slots__ = ("small",)

    def __init__(self) -> None:
        self.small = False


class BudgetFallback:
    """先按大额度调；服务方一开口就嫌额度太大时按小额度重来，之后的调用都用小的。

    只在还没吐出任何东西时重来：这类拒绝都发生在请求刚发出去的时候，重来不会重复输出。流式（astream）、非流式
    （ainvoke）都认。bind_tools、with_structured_output 照样包一层，包出来的和原来的共用同一个开关；shared_with 给了，
    另一份配置建的模型（核查收尾时关掉思考的那个）也和它共用：工具循环里退回过，收尾那次直接用小的，不再先撞一次。

    small 是现成的小额度模型；也可以不给，给 build（无参的协程函数，返回小额度的模型），要退回时才建，平常一个都不多建。
    limits 是 (大额度, 小额度)：给了，max_tokens 就按它报（包过一层的模型身上不一定读得到额度）；不给就读眼下那个模型的
    max_tokens。
    """

    def __init__(self, big: Any, small: Any = None, *, build: Callable[[], Awaitable[Any]] | None = None,
                 limits: tuple[int | None, int | None] | None = None,
                 shared_with: BudgetFallback | None = None) -> None:
        self._big = big
        self._small = small
        self._build = build
        self._limits = limits
        self._switch = shared_with._switch if shared_with is not None else _Switch()

    @property
    def model(self) -> Any:
        """眼下用的那个：退回过、小额度的也建好了就是小的，否则是大的。"""
        return self._small if self._switch.small and self._small is not None else self._big

    @property
    def max_tokens(self) -> Any:
        if self._limits is not None:
            return self._limits[1] if self._switch.small else self._limits[0]
        return getattr(self.model, "max_tokens", None)

    def _can_fall_back(self, used: Any) -> bool:
        """刚才那次用的是大额度、又有小额度可退。大小是同一个模型时重来也是同样的额度，不算可退。"""
        if used is not self._big:
            return False
        return self._build is not None if self._small is None else self._small is not self._big

    async def _small_model(self) -> Any:
        if self._small is None and self._build is not None:
            built = await self._build()
            if self._small is None:         # 并发的几次调用同时要建时，留先建好的那个
                self._small = built
        return self._small

    async def _current(self) -> Any:
        if self._switch.small and (small := await self._small_model()) is not None:
            return small
        return self._big

    async def ainvoke(self, messages: Any, *args: Any, **kwargs: Any) -> Any:
        used = await self._current()
        try:
            return await used.ainvoke(messages, *args, **kwargs)
        except Exception as e:
            if not self._can_fall_back(used) or not budget_rejected(e):
                raise
            self._switch.small = True
        return await (await self._current()).ainvoke(messages, *args, **kwargs)

    async def astream(self, messages: Any, *args: Any, **kwargs: Any):
        used = await self._current()
        started = False
        try:
            async for chunk in used.astream(messages, *args, **kwargs):
                started = True
                yield chunk
            return
        except Exception as e:
            if started or not self._can_fall_back(used) or not budget_rejected(e):
                raise
            self._switch.small = True
        async for chunk in (await self._current()).astream(messages, *args, **kwargs):
            yield chunk

    def _derived(self, make: Callable[[Any], Any]) -> BudgetFallback:
        """对大小两个模型做同一件事（绑工具、要结构化输出），包成共用开关的一层。小的还没建时，建好再做。"""
        small, build = None, None
        if self._small is not None:
            small = make(self._small)
        elif self._build is not None:
            async def build() -> Any:
                return make(await self._small_model())
        return BudgetFallback(make(self._big), small, build=build, limits=self._limits, shared_with=self)

    def bind_tools(self, tools: Any, **kwargs: Any) -> BudgetFallback:
        return self._derived(lambda m: m.bind_tools(tools, **kwargs))

    def with_structured_output(self, schema: Any, **kwargs: Any) -> BudgetFallback:
        return self._derived(lambda m: m.with_structured_output(schema, **kwargs))


__all__ = ["BUDGET_WORDS", "LENGTH_WORDS", "BudgetFallback", "budget_rejected", "cut_off", "cut_words", "error_usage",
           "truncated_error"]
