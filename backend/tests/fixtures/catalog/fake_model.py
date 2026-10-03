"""数据目录起草用的假模型：不发请求，按表名回固定的草稿。

    model = FakeModel({"visits": {"label": "入园记录", "kind": "fact"}}, fail={"orders"})
    await catalog.draft_with_model(model, source, ["visits"])

- 结构化输出（with_structured_output）返回自己；ainvoke 从提示词里认出这一批有哪些表（「表 <表名>」），
  返回 {"tables": [{"table": 表名, **replies[表名]}, …]}；
- fail 里的表所在的那一批抛异常，用来验证「一批失败只影响这一批」；
- calls 记下每次收到的提示词全文，用来核对交给模型的内容（只有结构，没有数据）。
"""
from __future__ import annotations

from typing import Any


class FakeModel:
    def __init__(self, replies: dict[str, dict], fail: set[str] = frozenset()) -> None:
        self.replies, self.fail, self.calls = replies, set(fail), []

    def with_structured_output(self, schema: Any, **_: Any) -> "FakeModel":
        assert schema["title"]
        return self

    async def ainvoke(self, messages: list[Any]) -> dict:
        text = "\n".join(str(getattr(m, "content", m)) for m in messages)
        self.calls.append(text)
        names = [t for t in self.replies if f"表 {t}\n" in text or f"表 {t}（" in text]
        if any(t in self.fail for t in names):
            raise RuntimeError("网关超时")
        return {"tables": [{"table": t, **self.replies[t]} for t in names]}
