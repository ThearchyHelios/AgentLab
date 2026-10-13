"""数据目录起草用的假模型：不发请求，按表名回固定的草稿。

    model = FakeModel({"visits": {"label": "入园记录", "kind": "fact"}}, fail={"orders"})
    await catalog.draft_with_model(model, source, ["visits"])

- 结构化输出（with_structured_output(...).ainvoke）从提示词里认出这一段请它写哪些表、每张表写哪部分（requested：
  整张表、只写表级的项、只写列出的几列、表级的项加几列），返回 {"tables": [{"table": 表名, …}, …]}，只给请它写的
  那部分（整张表给 replies[表名] 的全部；只写几列时只给 replies 里这几列）；
- 直接 ainvoke 是纯文本那条路：同样的内容写成一段正文（AIMessage），前面带一句话；
- fail 里的表所在的那一段抛异常，用来验证「一段失败只影响这一段」；
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

from app.data.catalog import CATALOG_DRAFT_PROMPT_LABELS as _P

#: 表名下一行写着「这次只写…」时，请模型写的是哪一部分（data/catalog_draft.py 按这几句写提示词）
_ONLY_TABLE = _P["only_table"]
_ONLY_COLUMNS = _P["only_columns"].split("{")[0]
_TABLE_AND = _P["table_and_columns"].split("{")[0]


def requested(text: str) -> dict[str, tuple[str, list[str] | None]]:
    """提示词里请模型写哪些表、每张表写哪部分：{表名: (哪部分, 列)}。哪部分是 full（整张表）、table（只写表级的项）、
    columns（只写列出的列，列是表结构描述里列出的那几列）、table+columns（表级的项加「这次只写」那一行点名的列）。"""
    out: dict[str, tuple[str, list[str] | None]] = {}
    for chunk in text.split("\n\n"):
        lines = chunk.split("\n")
        head = lines[0]
        if not head.startswith("表 "):
            continue
        name = head[2:]
        if name.endswith("（视图）"):
            name = name[:-len("（视图）")]
        second = lines[1] if len(lines) > 1 else ""
        listed = [line[2:].split(" ")[0] for line in lines if line.startswith("- ")]
        if second.startswith(_ONLY_TABLE):
            out[name] = ("table", None)
        elif second.startswith(_ONLY_COLUMNS):
            out[name] = ("columns", listed)
        elif second.startswith(_TABLE_AND):
            out[name] = ("table+columns", second.split("：", 1)[1].split("、"))
        else:
            out[name] = ("full", None)
    return out


def answer(reply: dict, name: str, part: str, columns: list[str] | None) -> dict:
    """按请它写的那部分，从 replies[表名] 里取。"""
    if part == "full":
        return {"table": name, **reply}
    out: dict[str, Any] = {"table": name}
    if part in ("table", "table+columns"):
        out.update({k: v for k, v in reply.items() if k != "columns"})
    if part in ("columns", "table+columns"):
        wanted = set(columns or [])
        out["columns"] = [c for c in reply.get("columns") or [] if isinstance(c, dict) and c.get("name") in wanted]
    return out


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
        asked = requested(text)
        names = [t for t in asked if t in self.replies]
        if any(t in self.fail for t in asked):
            raise RuntimeError("网关超时")
        shape = self.structured if mode == "structured" else self.text
        if shape == "error":
            raise NotImplementedError("这条路用不了")
        if shape == "hollow":
            return {"tables": [{"table": t} for t in names]}
        answers = [answer(self.replies[t], t, *asked[t]) for t in names]
        if shape == "single":
            return answers[0] if answers else {}
        return {"tables": answers}


class _Structured:
    def __init__(self, owner: FakeModel) -> None:
        self.owner = owner

    async def ainvoke(self, messages: list[Any]) -> dict:
        return self.owner._reply(messages, "structured")
