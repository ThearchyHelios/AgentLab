"""数据目录起草用的假模型：不发请求，按表名回固定的草稿。

    model = FakeModel({"visits": {"label": "入园记录", "kind": "fact"}}, fail={"orders"})
    await catalog.draft_with_model(model, source, ["visits"])

- 结构化输出（with_structured_output(...).ainvoke）从提示词里认出这一批有哪些表（「表 <表名>」），
  返回 {"tables": [{"table": 表名, **replies[表名]}, …]}；
- 直接 ainvoke 是纯文本那条路：同样的内容写成一段正文（AIMessage），前面带一句话；
- fail 里的表所在的那一批抛异常，用来验证「一批失败只影响这一批」；
- calls 记下每次收到的提示词全文，用来核对交给模型的内容（只有结构，没有数据）；modes 记下每次走的是
  structured 还是 text。

structured、text 模拟实测见过的几种回复（两条路各自设）：

- "tables"：正常，{"tables": [...]}；
- "hollow"：退化的空壳，表名对上了、一个字段都没有：{"tables": [{"table": 表名}, …]}；
- "single"：只给第一张表的对象，没有 tables 外壳：{"table": 表名, …}；
- "error"：这条路用不了（网关不支持工具调用之类），一调就抛异常。
"""
from __future__ import annotations

import json
from typing import Any


class FakeModel:
    def __init__(self, replies: dict[str, dict], fail: set[str] = frozenset(), *,
                 structured: str = "tables", text: str = "tables") -> None:
        self.replies, self.fail, self.calls = replies, set(fail), []
        self.structured, self.text = structured, text
        self.modes: list[str] = []

    def with_structured_output(self, schema: Any, **_: Any) -> "_Structured":
        assert schema["title"]
        return _Structured(self)

    async def ainvoke(self, messages: list[Any]) -> Any:
        from langchain_core.messages import AIMessage

        reply = self._reply(messages, "text")
        return AIMessage(content="草稿如下：" + json.dumps(reply, ensure_ascii=False))

    def _reply(self, messages: list[Any], mode: str) -> dict:
        text = "\n".join(str(getattr(m, "content", m)) for m in messages)
        self.calls.append(text)
        self.modes.append(mode)
        names = [t for t in self.replies if f"表 {t}\n" in text or f"表 {t}（" in text]
        if any(t in self.fail for t in names):
            raise RuntimeError("网关超时")
        shape = self.structured if mode == "structured" else self.text
        if shape == "error":
            raise NotImplementedError("这条路用不了")
        if shape == "hollow":
            return {"tables": [{"table": t} for t in names]}
        if shape == "single":
            return {"table": names[0], **self.replies[names[0]]} if names else {}
        return {"tables": [{"table": t, **self.replies[t]} for t in names]}


class _Structured:
    def __init__(self, owner: FakeModel) -> None:
        self.owner = owner

    async def ainvoke(self, messages: list[Any]) -> dict:
        return self.owner._reply(messages, "structured")
