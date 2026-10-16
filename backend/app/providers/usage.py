"""模型用量计量：助手建图（含挑表、接着写、自查修正）和复核这两段花了多少（细案 01 做法 3）。

**为什么要。** 运行按 llm.end 事件记用量，问数据一轮里另外两块——助手建图、复核——一直不记：这一轮的花费是低报的，
评测也算不出按角色的成本。在每个调用点手写累计（生成、挑表、退回小额度重来、复核各一处）迟早漏掉一处，所以用 LangChain
的回调在模型这一层统一截：和 langchain_core.callbacks.get_usage_metadata_callback 同一个机制（一个登记过的上下文变量，
模型每次调用时把它挂成回调），按模型累计 input / output tokens 和调用次数。

**为什么不直接用 get_usage_metadata_callback。** 它是一个 with 块，块里设上下文变量、出块时按 token 复原。生成过程是一个
async generator：with 块跨着 yield 的话，变量在两次 yield 之间留在调用方的上下文里——调用方这时去发起运行，运行的任务
会继承它，运行里的调用就被重复计一遍；调用方中途不迭代了、在别的任务里收尾，复原还会报「token 不属于这个上下文」。
这里只在每一步 __anext__ 期间设上变量、交出操作之前复原（metered）：生成过程里起的任务（心跳那一层每一步一个任务）
在创建时拷走上下文，照样计到；调用方自己的代码一行都不在计量里。

**金额。** 按 providers/catalog.estimate_cost 算，和运行的 llm.end 一个口径。估不出的调用记 unpriced_calls：模型不在价格
目录里，或者用量没报全而目录价又不是 0。估不出的不当成 0 元，调用方据此写「≥」或「金额无法估算」。用量没报全有两种：
回复没带用量（有的网关流式不给）；读到一半就不读了——用量在流的末尾才报全（Anthropic 的输出 token 在最后一条，OpenAI
兼容系开了 stream_usage 也在最后）。生成过程读到 done、look、ask 那一行之后照样把流读完再关（api/copilot_gen._drain），
流末尾那一段用量收得到；只有模型在那一行之后还往下写超过 DRAIN_CHARS 字的闲话时才当场关流（不为闲话付钱），连接断了也一样，
这一次只拿得到已经报了的部分：照样计次数、计已报的 token，记成估不出，不悄悄少算。

模型名按发起调用时的参数取（invocation_params 的 model / model_name，就是接入里配的模型 id，价格目录按它查），取不到再
看回复里的 model_name。
"""
from __future__ import annotations

import threading
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any
from uuid import UUID

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.tracers.context import register_configure_hook

#: 取不到模型名的调用记在这个名下
UNKNOWN_MODEL = "unknown"


class Meter(BaseCallbackHandler):
    """一次计量：按模型累计用量。线程安全（同步模型的回调可能在线程里来）。"""

    #: 同步模型在线程里调用时也要回调
    run_inline = True

    def __init__(self) -> None:
        super().__init__()
        self._lock = threading.Lock()
        self._pending: dict[UUID, str] = {}
        self._models: dict[str, dict[str, int]] = {}

    # ------------------------------------------------------------------ 回调

    def _started(self, run_id: UUID, serialized: dict[str, Any] | None, kwargs: dict[str, Any]) -> None:
        params = kwargs.get("invocation_params") or {}
        model = params.get("model") or params.get("model_name") or params.get("model_id")
        if not model and isinstance(serialized, dict):
            model = (serialized.get("kwargs") or {}).get("model") or (serialized.get("kwargs") or {}).get("model_name")
        with self._lock:
            self._pending[run_id] = str(model) if model else ""

    def on_chat_model_start(self, serialized: dict[str, Any], messages: Any, *, run_id: UUID,
                            **kwargs: Any) -> None:
        self._started(run_id, serialized, kwargs)

    def on_llm_start(self, serialized: dict[str, Any], prompts: Any, *, run_id: UUID, **kwargs: Any) -> None:
        self._started(run_id, serialized, kwargs)

    def on_llm_end(self, response: Any, *, run_id: UUID, **kwargs: Any) -> None:
        message = None
        try:
            message = response.generations[0][0].message
        except (AttributeError, IndexError, TypeError):
            pass
        usage = getattr(message, "usage_metadata", None) or None
        meta = getattr(message, "response_metadata", None) or {}
        with self._lock:
            model = self._pending.pop(run_id, "") or str(meta.get("model_name") or meta.get("model") or "") \
                or UNKNOWN_MODEL
            row = self._models.setdefault(model, {"input_tokens": 0, "output_tokens": 0, "calls": 0, "unreported": 0})
            row["calls"] += 1
            if usage:
                row["input_tokens"] += int(usage.get("input_tokens") or 0)
                row["output_tokens"] += int(usage.get("output_tokens") or 0)
            else:
                row["unreported"] += 1

    def on_llm_error(self, error: BaseException, *, run_id: UUID, **kwargs: Any) -> None:
        """流读到一半停了（生成过程在最后一行之后读到太多闲话就关流，或者连接断了）：LangChain 照样交来已经收到的那几段。
        有收到东西的照计一次、记已报的 token，算用量没报全；一段都没收到的（请求刚发出去就被拒了）不计。"""
        from langchain_core.outputs import ChatGenerationChunk

        response = kwargs.get("response")
        partial = None
        try:
            first = response.generations[0][0] if response is not None and response.generations[0] else None
            partial = first.message if isinstance(first, ChatGenerationChunk) else None
        except (AttributeError, IndexError, TypeError):
            partial = None
        with self._lock:
            model = self._pending.pop(run_id, "")
            if partial is None:
                return
            meta = getattr(partial, "response_metadata", None) or {}
            model = model or str(meta.get("model_name") or meta.get("model") or "") or UNKNOWN_MODEL
            row = self._models.setdefault(model, {"input_tokens": 0, "output_tokens": 0, "calls": 0, "unreported": 0})
            row["calls"] += 1
            row["unreported"] += 1
            reported = getattr(partial, "usage_metadata", None) or {}
            row["input_tokens"] += int(reported.get("input_tokens") or 0)
            row["output_tokens"] += int(reported.get("output_tokens") or 0)

    # ------------------------------------------------------------------ 合并、汇总

    def merge(self, other: Meter) -> None:
        """把另一次计量并进来（嵌套的计量出块时并进外层）。"""
        with other._lock:
            rows = {k: dict(v) for k, v in other._models.items()}
        with self._lock:
            for model, row in rows.items():
                mine = self._models.setdefault(model, dict.fromkeys(row, 0))
                for key, value in row.items():
                    mine[key] = mine.get(key, 0) + value

    def summary(self) -> dict[str, Any]:
        """{by_model: {模型: {input_tokens, output_tokens, calls, cost_usd, unpriced_calls}}, input_tokens, output_tokens,
        calls, cost_usd, unpriced_calls}。cost_usd 只是估得出的那部分。"""
        from app.providers.catalog import estimate_cost, find_pricing

        with self._lock:
            rows = {k: dict(v) for k, v in self._models.items()}
        by_model: dict[str, dict[str, Any]] = {}
        for model, row in sorted(rows.items()):
            pricing = find_pricing(model)
            free = pricing is not None and not any(pricing.values())
            if pricing is None:
                unpriced = row["calls"]
            else:
                unpriced = 0 if free else row["unreported"]
            by_model[model] = {"input_tokens": row["input_tokens"], "output_tokens": row["output_tokens"],
                               "calls": row["calls"],
                               "cost_usd": round(estimate_cost(model, row["input_tokens"], row["output_tokens"]), 6),
                               "unpriced_calls": unpriced}
        total = {key: sum(r[key] for r in by_model.values())
                 for key in ("input_tokens", "output_tokens", "calls", "unpriced_calls")}
        return {"by_model": by_model, **total, "cost_usd": round(sum(r["cost_usd"] for r in by_model.values()), 6)}


#: 计量挂在这个上下文变量上（inheritable：模型调用时 LangChain 按它把 Meter 挂成回调，子任务拷走上下文时一并带走）
_CURRENT: ContextVar[Meter | None] = ContextVar("agentlab_usage_meter", default=None)
register_configure_hook(_CURRENT, inheritable=True)


def meter() -> Meter:
    """新的一次计量。用 metering（一段代码）或 metered（一个生成过程）把它挂上。"""
    return Meter()


def current() -> Meter | None:
    return _CURRENT.get()


@contextmanager
def metering(m: Meter) -> Iterator[Meter]:
    """这一段代码里的模型调用计进 m。只给不跨 yield 的一段用（复核那一次调用）；生成过程用 metered。

    嵌套的：出块时并进外层的那一次，外层照样看得到。复原用 set 而不是按 token reset：在别的任务里收尾也不报错。"""
    outer = _CURRENT.get()
    _CURRENT.set(m)
    try:
        yield m
    finally:
        _CURRENT.set(outer)
        if outer is not None and outer is not m:
            outer.merge(m)


async def metered(source: AsyncIterator[Any], m: Meter) -> AsyncIterator[Any]:
    """迭代一个生成过程，它的模型调用计进 m：每一步 __anext__ 期间挂上，交出操作之前复原（见模块开头）。

    开始时外层已经有一次计量的（评测把一整题的助手用量记成一份）：结束时把 m 并进去，同 metering。"""
    enclosing = _CURRENT.get()
    iterator = source.__aiter__()
    try:
        while True:
            outer = _CURRENT.get()
            _CURRENT.set(m)
            try:
                item = await iterator.__anext__()
            except StopAsyncIteration:
                return
            finally:
                _CURRENT.set(outer)
            yield item
    finally:
        if enclosing is not None and enclosing is not m:
            enclosing.merge(m)


def empty() -> dict[str, Any]:
    """一次调用都没有时的汇总（同 Meter().summary()）。"""
    return Meter().summary()


__all__ = ["Meter", "UNKNOWN_MODEL", "current", "empty", "meter", "metered", "metering"]
