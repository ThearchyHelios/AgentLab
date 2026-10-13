"""数据目录模型起草的引擎（data/catalog_draft.py）：分段、按失败原因处理、捡回被截断的部分、起草要求、用量。

用假模型复刻实测查明的几种失败（不调用真实模型）：
- 思考加输出用完额度：finish_reason=length 且正文为空（只有思考）、LengthFinishReasonError → 拆成两半再问；
- 只给空壳 → 拆开再问；拆不开的换纯文本问一次，写出来了这次起草后面都走纯文本；
- 被截断的 JSON：已经完整的表、列对象捡回来，只补剩下的；
- 只回一部分表：只补缺的，补一次；
- 服务方嫌额度太大 → 退回 8192，后面都用它；结构化输出不支持 → 后面改走纯文本；
- 网络错误、5xx → 退避重试，最多 2 次，报错写明重试过几次；超时当作这一段太大，拆小再问，不原样重试。

验收后修正的几条（用假模型复现）：宽表的列分段均分、没有很小的尾段；拆不开的空壳纯文本也再问一次；额度用完或超时
累计 2 次后新开始的段列数减半；一段最多调用 8 次；最近 6 次调用 4 次没写出来时提醒、少于 2 次时清掉；记超时和没写
出来的调用；模型留空的列计数；窄表不被宽表的列分段插队；原因写清是哪几段、哪几列。

表名、列名、说明文字都是虚构的。
"""
from __future__ import annotations

import asyncio
import inspect
import json
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Callable

import pytest
from langchain_core.messages import AIMessage

from app.data import catalog
from app.data import catalog_draft as D
from tests.fixtures.catalog.fake_model import answer, requested


# --------------------------------------------------------------------------
# 夹具
# --------------------------------------------------------------------------


def _col(name: str, type_: str = "INTEGER") -> dict:
    return {"name": name, "type": type_, "nullable": True, "comment": None}


def _table(n: int, prefix: str = "c", comment: str | None = None) -> dict:
    return {"qualified": "", "schema": None, "columns": [_col(f"{prefix}{i:03d}") for i in range(1, n + 1)],
            "primary_key": [f"{prefix}001"], "comment": comment, "is_view": False, "foreign_keys": []}


def _source(tables: dict, **kw) -> SimpleNamespace:
    for name, meta in tables.items():
        meta["qualified"] = name
    return SimpleNamespace(id="src", name="shop", kind="sqlite", origin="manual",
                           description=kw.pop("description", ""), schema_cache={"tables": tables})


def _reply(table: str, meta: dict) -> dict:
    """一张表的完整草稿：表级三项 + 每列中文名和度量类型。"""
    return {"label": f"{table}的中文名", "grain": "每条记录一行", "kind": "fact",
            "columns": [{"name": c["name"], "label": f"{c['name']}的中文名", "measure": "attribute"}
                        for c in meta["columns"]]}


@dataclass
class Reply:
    """一次回复。obj 是正常的 JSON 对象；text 给了就原样作正文（截断的 JSON 之类）。"""

    obj: Any = None
    text: str | None = None
    finish: str = "stop"
    thinking: str = ""
    usage: tuple[int, int] = (0, 0)

    def render(self, mode: str, include_raw: bool) -> Any:
        if self.text is not None:
            content = self.text
        else:
            content = json.dumps(self.obj, ensure_ascii=False) if self.obj is not None else ""
        msg = AIMessage(
            content=content,
            additional_kwargs={"reasoning_content": self.thinking} if self.thinking else {},
            response_metadata={"finish_reason": self.finish},
            usage_metadata={"input_tokens": self.usage[0], "output_tokens": self.usage[1],
                            "total_tokens": sum(self.usage)},
        )
        if mode == "text":
            return msg
        parsed = self.obj if self.text is None else None
        if not include_raw:
            return parsed
        return {"raw": msg, "parsed": parsed, "parsing_error": None if parsed is not None else ValueError("坏的")}


@dataclass
class Call:
    mode: str
    text: str
    asked: dict
    n: int


class Scripted:
    """每次调用交给 handler(call) 决定怎么回：返回 Reply，或者抛异常；handler 可以是协程。"""

    def __init__(self, handler: Callable[[Call], Any], name: str = "scripted-model") -> None:
        self.handler, self.model_name = handler, name
        self.calls: list[Call] = []

    def with_structured_output(self, schema: Any, include_raw: bool = False, **_: Any) -> Any:
        owner = self

        class _S:
            async def ainvoke(self, messages):
                return await owner._run(messages, "structured", include_raw)

        return _S()

    async def ainvoke(self, messages: list[Any]) -> Any:
        return await self._run(messages, "text", False)

    async def _run(self, messages: list[Any], mode: str, include_raw: bool) -> Any:
        text = "\n".join(str(getattr(m, "content", m)) for m in messages)
        call = Call(mode=mode, text=text, asked=requested(text), n=len(self.calls))
        self.calls.append(call)
        out = self.handler(call)
        if inspect.isawaitable(out):
            out = await out
        return out.render(mode, include_raw)


def answering(tables: dict, **kw) -> Callable[[Call], Reply]:
    """照请求写：每张表按请它写的那部分给出 _reply 的内容。"""
    def handle(call: Call) -> Reply:
        return Reply(obj={"tables": [answer(_reply(t, tables[t]), t, *call.asked[t]) for t in call.asked]}, **kw)
    return handle


class _Status(Exception):
    def __init__(self, status: int, text: str) -> None:
        super().__init__(text)
        self.status_code = status


class LengthFinishReasonError(Exception):
    """和 openai 的同名异常一样：解析结构化输出时发现 finish_reason=length。"""

    def __init__(self) -> None:
        super().__init__("Could not parse response content as the length limit was reached")
        self.completion = None


class ConnectError(Exception):
    pass


@pytest.fixture(autouse=True)
def _no_wait(monkeypatch):
    """退避不真等：记下要等多久。"""
    waited: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        waited.append(seconds)

    monkeypatch.setattr(D, "_sleep", fake_sleep)
    return waited


def _status(outcomes: dict) -> dict:
    return {t: o.status for t, o in outcomes.items()}


def _hollow(call: Call) -> Reply:
    return Reply(obj={"tables": [{"table": t} for t in call.asked]})


def _exhausted(seg: str = "这一段", budget: int = 32768) -> str:
    return D.REASON_EXHAUSTED.format(budget=budget, seg=seg)


EXHAUSTED = Reply(text="", finish="length", thinking="……")


class Reports:
    """进度回调 call_done 收到的每次调用的结果。"""

    def __init__(self) -> None:
        self.items: list[D.CallReport] = []

    async def __call__(self, report: D.CallReport) -> None:
        self.items.append(report)

    def hooks(self) -> D.Hooks:
        return D.Hooks(call_done=self)

    @property
    def failures(self) -> list[str | None]:
        return [r.failure for r in self.items]


# --------------------------------------------------------------------------
# 思考用完额度、空壳：拆开再问
# --------------------------------------------------------------------------


async def test_thinking_used_up_the_budget_splits_the_segment():
    tables = {"orders_x": _table(24)}
    ok = answering(tables)

    def handle(call: Call) -> Reply:
        if call.n == 0:     # 只想不写：正文为空、只有思考，额度用完
            return Reply(text="", finish="length", thinking="先逐列想一遍……")
        return ok(call)

    model = Scripted(handle)
    got = await D.run_model_draft(model, _source(tables), ["orders_x"])
    o = got["orders_x"]
    assert o.status == "ok" and o.error is None and o.items == 3 + 24 * 2
    assert len(model.calls) == 3
    cols = [f"c{i:03d}" for i in range(1, 25)]
    assert model.calls[1].asked == {"orders_x": ("table+columns", cols[:12])}
    assert model.calls[2].asked == {"orders_x": ("columns", cols[12:])}
    # 后半段在前半段之后问，带着刚起草的表级内容做背景
    assert catalog.CATALOG_DRAFT_PROMPT_LABELS["drafted"] in model.calls[2].text
    assert "orders_x的中文名" in model.calls[2].text


async def test_length_finish_reason_error_also_splits():
    tables = {"orders_x": _table(24)}
    ok = answering(tables)

    def handle(call: Call) -> Reply:
        if call.n == 0:
            raise LengthFinishReasonError()
        return ok(call)

    model = Scripted(handle)
    got = await D.run_model_draft(model, _source(tables), ["orders_x"])
    assert got["orders_x"].status == "ok" and len(model.calls) == 3


async def test_hollow_shell_splits_the_pack_by_table():
    tables = {"a_tab": _table(6), "b_tab": _table(6)}
    ok = answering(tables)

    def handle(call: Call) -> Reply:
        if call.n == 0:
            return Reply(obj={"tables": [{"table": "a_tab"}, {"table": "b_tab"}]})
        return ok(call)

    model = Scripted(handle)
    got = await D.run_model_draft(model, _source(tables), ["a_tab", "b_tab"])
    assert _status(got) == {"a_tab": "ok", "b_tab": "ok"}
    assert [set(c.asked) for c in model.calls] == [{"a_tab", "b_tab"}, {"a_tab"}, {"b_tab"}]
    assert all(c.asked[t][0] == "full" for c in model.calls[1:] for t in c.asked)


async def test_splitting_stops_at_three_levels_and_five_columns(monkeypatch):
    monkeypatch.setattr(catalog, "MODEL_PART_MAX_CALLS", 99)
    tables = {"wide40": _table(40)}
    model = Scripted(lambda call: EXHAUSTED)
    got = await D.run_model_draft(model, _source(tables), ["wide40"])
    o = got["wide40"]
    assert len(model.calls) == 1 + 2 + 4 + 8, "40 → 20 → 10 → 5 列，最多拆三层"
    assert min(len(c.asked["wide40"][1] or []) for c in model.calls[1:]) == 5
    assert o.status == "failed" and o.items == 0
    assert o.error == "模型的思考和输出用完了额度（32768 个 token），这一段没写出来；已拆小重试仍没写出"

    small = Scripted(lambda call: Reply(text="", finish="length"))
    got = await D.run_model_draft(small, _source({"t9": _table(9)}), ["t9"])
    assert len(small.calls) == 1, "不到 10 列拆不出两段 5 列"
    assert got["t9"].error == "模型的思考和输出用完了额度（32768 个 token），这一段没写出来"


async def test_one_part_makes_at_most_eight_calls(monkeypatch):
    """一段（连同它拆出来的、补问的、重试的）最多调用 8 次：40 列一直用完额度，拆到第 8 次就停，剩下没问的记没写出来、
    原因写清。以前一段 40 列拆到底要 15 次。"""
    monkeypatch.setattr(catalog, "MODEL_GIVE_UP_AFTER", 99)
    assert catalog.MODEL_PART_MAX_CALLS == 8
    model = Scripted(lambda call: EXHAUSTED)
    got = await D.run_model_draft(model, _source({"t40": _table(40)}), ["t40"])
    assert len(model.calls) == 8
    o = got["t40"]
    assert o.status == "failed"
    assert o.error == (_exhausted() + "；每段最多调用 8 次模型，这一段到了次数仍没写完，已停止重试"
                       + D.REASON_SPLIT)

    # 网络错误的重试也算在这 8 次里
    def flaky(call: Call) -> Reply:
        raise ConnectError("All connection attempts failed")

    monkeypatch.setattr(catalog, "MODEL_PART_MAX_CALLS", 2)
    model = Scripted(flaky)
    got = await D.run_model_draft(model, _source({"t4": _table(4)}), ["t4"])
    assert len(model.calls) == 2
    assert got["t4"].error == "每段最多调用 2 次模型，这一段到了次数仍没写完，已停止重试"


async def test_an_unsplittable_hollow_shell_tries_text_once_then_stays_on_text():
    """有的网关的结构化输出只给空壳：拆不开时换纯文本问一次，写出来了，这次起草后面直接走纯文本。

    用宽表看先后：表级那段（拆不开）写完之后才问列分段。"""
    tables = {"wide45": _table(45)}
    ok = answering(tables)

    def handle(call: Call) -> Reply:
        if call.mode == "structured":
            return Reply(obj={"tables": [{"table": t} for t in call.asked]})
        return ok(call)

    model = Scripted(handle)
    got = await D.run_model_draft(model, _source(tables), ["wide45"])
    assert got["wide45"].status == "ok" and len(got["wide45"].notes["columns"]) == 45
    assert [c.mode for c in model.calls] == ["structured", "text", "text", "text"]
    assert catalog.CATALOG_DRAFT_TEXT_PROMPT in model.calls[1].text, "纯文本那条路要说清 JSON 的格式"


def _text_only(handle_text: Callable[[Call], Reply]) -> Callable[[Call], Reply]:
    """结构化输出不支持（这次起草一开始就改走纯文本），纯文本交给 handle_text。"""
    def handle(call: Call) -> Reply:
        if call.mode == "structured":
            raise _Status(400, "This response_format type is unavailable now")
        return handle_text(call)
    return handle


async def test_an_unsplittable_hollow_shell_is_asked_again_in_text_mode_too():
    """已经走纯文本时，拆不开的段（不到 10 列）只给空壳：原样再问一次，写出来就用；仍是空壳才记没写出来。
    以前纯文本模式下不补问，整张表直接记没写出来。"""
    tables = {"t8": _table(8)}
    ok = answering(tables)
    model = Scripted(_text_only(lambda call: _hollow(call) if call.n == 1 else ok(call)))
    got = await D.run_model_draft(model, _source(tables), ["t8"])
    assert [c.mode for c in model.calls] == ["structured", "text", "text"]
    assert got["t8"].status == "ok" and len(got["t8"].notes["columns"]) == 8

    model = Scripted(_text_only(_hollow))
    got = await D.run_model_draft(model, _source(tables), ["t8"])
    assert [c.mode for c in model.calls] == ["structured", "text", "text"], "再问一次，不多问"
    assert got["t8"].status == "failed" and got["t8"].error == catalog.MODEL_EMPTY_REASON


# --------------------------------------------------------------------------
# 截断、缺表、缺列
# --------------------------------------------------------------------------


async def test_truncated_json_keeps_complete_columns_and_asks_only_the_rest():
    tables = {"t12": _table(12)}
    full = json.dumps({"tables": [{"table": "t12", **_reply("t12", tables["t12"])}]}, ensure_ascii=False)
    cut = full[:full.index('"c006"') + 8]          # 第 6 列写到一半
    ok = answering(tables)

    def handle(call: Call) -> Reply:
        if call.n == 0:
            return Reply(text=cut, finish="length")
        return ok(call)

    model = Scripted(handle)
    got = await D.run_model_draft(model, _source(tables), ["t12"])
    o = got["t12"]
    assert o.status == "ok" and set(o.notes["columns"]) == {f"c{i:03d}" for i in range(1, 13)}
    assert o.notes["label"]["value"] == "t12的中文名"
    assert len(model.calls) == 2
    assert model.calls[1].asked == {"t12": ("columns", [f"c{i:03d}" for i in range(6, 13)])}


async def test_truncated_tool_call_arguments_are_salvaged_too():
    """结构化输出走工具调用时，截断的参数在 invalid_tool_calls 里（一段字符串）。"""
    tables = {"t12": _table(12)}
    full = json.dumps({"tables": [{"table": "t12", **_reply("t12", tables["t12"])}]}, ensure_ascii=False)
    cut = full[:full.index('"c004"') + 3]          # 第 4 列的列名写到一半
    ok = answering(tables)

    class ToolModel(Scripted):
        async def _run(self, messages, mode, include_raw):
            if not self.calls:
                self.calls.append(Call(mode, "", {}, 0))
                raw = AIMessage(content="", invalid_tool_calls=[{"name": "catalog_draft", "args": cut, "id": "x",
                                                                  "error": None, "type": "invalid_tool_call"}],
                                response_metadata={"finish_reason": "length"})
                return {"raw": raw, "parsed": None, "parsing_error": ValueError("截断")}
            return await super()._run(messages, mode, include_raw)

    model = ToolModel(ok)
    got = await D.run_model_draft(model, _source(tables), ["t12"])
    assert got["t12"].status == "ok"
    assert model.calls[1].asked == {"t12": ("columns", [f"c{i:03d}" for i in range(4, 13)])}


async def test_missing_tables_are_asked_again_once():
    """缺了的表只补缺的、补一次；补问时整张表还是一项都没有，按只给空壳处理（拆不开就换纯文本问一次）。"""
    tables = {"a_tab": _table(4), "b_tab": _table(4), "c_tab": _table(4)}
    ok = answering(tables)

    def handle(call: Call) -> Reply:
        reply = ok(call)
        keep = {0: {"a_tab"}, 1: {"b_tab"}}.get(call.n, set())
        reply.obj["tables"] = [e for e in reply.obj["tables"] if e["table"] in keep]
        return reply

    model = Scripted(handle)
    got = await D.run_model_draft(model, _source(tables), ["a_tab", "b_tab", "c_tab"])
    assert len(model.calls) == 3
    assert model.calls[1].asked == {"b_tab": ("full", None), "c_tab": ("full", None)}
    assert model.calls[2].asked == {"c_tab": ("full", None)} and model.calls[2].mode == "text"
    assert _status(got) == {"a_tab": "ok", "b_tab": "ok", "c_tab": "failed"}
    assert got["c_tab"].error == "模型的回复里没有这张表"


async def test_a_table_still_missing_after_the_followup_is_split_like_a_hollow_shell():
    """补问时整张表还是一项都没有：照第一次的规则拆小（这里按列拆成两半），不直接记失败。"""
    tables = {"a_tab": _table(4), "b_tab": _table(12)}
    ok = answering(tables)

    def handle(call: Call) -> Reply:
        reply = ok(call)
        if call.n < 2:
            reply.obj["tables"] = [e for e in reply.obj["tables"] if e["table"] == "a_tab"]
        return reply

    model = Scripted(handle)
    got = await D.run_model_draft(model, _source(tables), ["a_tab", "b_tab"])
    assert _status(got) == {"a_tab": "ok", "b_tab": "ok"}
    cols = [f"c{i:03d}" for i in range(1, 13)]
    assert [c.asked for c in model.calls[2:]] == [{"b_tab": ("table+columns", cols[:6])},
                                                  {"b_tab": ("columns", cols[6:])}]


async def test_columns_still_missing_after_the_followup_are_not_written():
    """补问过仍然没列出来的列算没写出来：表是 partial，说清哪部分没写出来（不算拿不准，旧的模型起草项不删）。"""
    tables = {"t8": _table(8)}
    ok = answering(tables)

    def handle(call: Call) -> Reply:
        reply = ok(call)
        for entry in reply.obj["tables"]:
            entry["columns"] = [c for c in entry.get("columns", []) if c["name"] not in ("c007", "c008")]
        return reply

    model = Scripted(handle)
    got = await D.run_model_draft(model, _source(tables), ["t8"])
    assert len(model.calls) == 2 and model.calls[1].asked == {"t8": ("columns", ["c007", "c008"])}
    o = got["t8"]
    assert o.status == "partial"
    assert o.error == ("这张表 8 列，有 2 列（c007、c008）没写出来：模型的回复里漏了这些列，补问一次仍没有给出。"
                       "可以只重试这张表")
    assert set(o.notes["columns"]) == {f"c{i:03d}" for i in range(1, 7)}
    assert o.blank == [], "没写出来的列不算留空"
    compat = (await catalog.draft_with_model(Scripted(handle), _source(tables), ["t8"]))["t8"]
    assert compat.covered == frozenset(), "没写全的不算完整起草过"


async def test_columns_listed_blank_in_the_followup_are_left_blank_not_failed():
    """补问时模型把漏的列列出来了、字段却留空：是拿不准（记留空），不算「漏了这些列」。"""
    tables = {"t8": _table(8)}
    ok = answering(tables)

    def handle(call: Call) -> Reply:
        reply = ok(call)
        entry = reply.obj["tables"][0]
        if call.n == 0:
            entry["columns"] = [c for c in entry["columns"] if c["name"] not in ("c003", "c007")]
        else:
            entry["columns"] = [{"name": c["name"]} for c in entry["columns"]]
        return reply

    model = Scripted(handle)
    got = await D.run_model_draft(model, _source(tables), ["t8"])
    assert model.calls[1].asked == {"t8": ("columns", ["c003", "c007"])}
    o = got["t8"]
    assert o.status == "ok" and o.error is None and o.blank == ["c003", "c007"]


async def test_a_followup_that_comes_back_as_an_empty_shell_leaves_the_table_partial():
    """40 列的表第一次只回了 20 列，补问只给一个空壳：没写出来的 20 列照实记下，表是 partial。"""
    tables = {"t40": _table(40)}
    ok = answering(tables)

    def handle(call: Call) -> Reply:
        if call.n == 0:
            reply = ok(call)
            reply.obj["tables"][0]["columns"] = reply.obj["tables"][0]["columns"][:20]
            return reply
        return Reply(obj={"tables": [{"table": "t40"}]})

    model = Scripted(handle)
    got = await D.run_model_draft(model, _source(tables), ["t40"])
    o = got["t40"]
    assert len(model.calls) == 2
    assert model.calls[1].asked == {"t40": ("columns", [f"c{i:03d}" for i in range(21, 41)])}
    assert o.status == "partial" and len(o.notes["columns"]) == 20
    assert o.error == ("这张表 40 列，有 20 列（c021 到 c040）没写出来：模型的回复里漏了这些列，补问一次仍没有给出。"
                       "可以只重试这张表")


async def test_columns_listed_with_empty_fields_are_left_to_the_model():
    """列出了、字段留空的列是模型拿不准：不补问，也不算没写出来；记成留空（表的状态不因此变）。"""
    tables = {"t8": _table(8)}
    ok = answering(tables)

    def handle(call: Call) -> Reply:
        reply = ok(call)
        for entry in reply.obj["tables"]:
            entry["columns"] = [c if c["name"] not in ("c007", "c008") else {"name": c["name"]}
                                for c in entry.get("columns", [])]
        return reply

    model = Scripted(handle)
    got = await D.run_model_draft(model, _source(tables), ["t8"])
    assert len(model.calls) == 1
    assert got["t8"].status == "ok" and got["t8"].error is None
    assert set(got["t8"].notes["columns"]) == {f"c{i:03d}" for i in range(1, 7)}
    assert got["t8"].blank == ["c007", "c008"]


async def test_blank_columns_are_the_ones_without_a_label_or_meaning():
    """留空的列：请模型写了、中文名和含义却都没给（只给了度量类型、单位，或者给的都没通过校验）。已确认的、库注释已经说清
    的不算：给模型的提示词里它们以此为准，模型不写也不缺。"""
    meta = _table(9)
    meta["columns"][5]["comment"] = "会员等级"            # c006：库注释就是中文名
    meta["columns"][8]["comment"] = "C009"                # c009：注释就是列名，说明不了什么
    tables = {"t9": meta}
    existing = {"t9": {"columns": {"c005": {"label": catalog.make_item("已确认的名字", "human")}}}}

    def handle(call: Call) -> Reply:
        return Reply(obj={"tables": [{"table": "t9", "label": "某表", "columns": [
            {"name": "c001", "label": "第一列"},
            {"name": "c002", "meaning": "第二列的含义"},
            {"name": "c003", "measure": "attribute", "unit": "元"},    # 中文名、含义都没给
            {"name": "c004", "label": "=c001+c002"},                    # 像公式：校验丢掉
            {"name": "c005"}, {"name": "c006"}, {"name": "c007"}, {"name": "c008"}, {"name": "c009"}]}]})

    got = await D.run_model_draft(Scripted(handle), _source(tables), ["t9"], existing=existing)
    o = got["t9"]
    assert o.status == "ok" and o.error is None
    assert o.blank == ["c003", "c004", "c007", "c008", "c009"]


# --------------------------------------------------------------------------
# 额度被拒、结构化输出不支持、网络错误、超时
# --------------------------------------------------------------------------


async def test_budget_rejection_falls_back_to_8192_for_the_rest():
    tables = {"wide45": _table(45)}

    def refuse(call: Call) -> Reply:
        raise _Status(400, "max_tokens: 32768 > 8192, which is the maximum allowed number of output tokens")

    big, small = Scripted(refuse), Scripted(answering(tables))
    model = catalog.DraftModel(big, small, "m")
    got = await D.run_model_draft(model, _source(tables), ["wide45"])
    assert got["wide45"].status == "ok"
    assert len(big.calls) == 1 and len(small.calls) == 3, "退回之后，表级那段重来、两个列分段都用小额度"
    assert model.budget == 8192


async def test_exhausted_message_names_the_budget_actually_used():
    def refuse(call: Call) -> Reply:
        raise _Status(400, "max_tokens is too large")

    model = catalog.DraftModel(Scripted(refuse), Scripted(lambda c: Reply(text="", finish="length")), "m")
    got = await D.run_model_draft(model, _source({"t4": _table(4)}), ["t4"])
    assert got["t4"].error == "模型的思考和输出用完了额度（8192 个 token），这一段没写出来"


async def test_structured_output_unsupported_switches_to_text_for_the_rest():
    tables = {"wide45": _table(45)}
    ok = answering(tables)

    def handle(call: Call) -> Reply:
        if call.mode == "structured":
            raise _Status(400, "This response_format type is unavailable now")
        return ok(call)

    model = Scripted(handle)
    got = await D.run_model_draft(model, _source(tables), ["wide45"])
    assert got["wide45"].status == "ok"
    assert [c.mode for c in model.calls] == ["structured", "text", "text", "text"]


@pytest.mark.parametrize("thinking", ["", "先想想每一列是什么……"], ids=["plain", "with-thinking"])
async def test_structured_output_that_comes_back_as_prose_falls_back_to_text(thinking):
    """网关接受了结构化输出的参数却没照做（模型写成了一段说明文字）：换纯文本问，不当成额度用完去拆，也不直接判失败。"""
    tables = {"t40": _table(40)}
    ok = answering(tables)

    def handle(call: Call) -> Reply:
        if call.mode == "structured":
            return Reply(text="好的。这张表记录的是一笔笔业务，各列的含义如下：第一列是编号……", thinking=thinking)
        return ok(call)

    model = Scripted(handle)
    got = await D.run_model_draft(model, _source(tables), ["t40"])
    assert got["t40"].status == "ok" and len(got["t40"].notes["columns"]) == 40
    assert [c.mode for c in model.calls] == ["structured", "text"]
    assert catalog.CATALOG_DRAFT_TEXT_PROMPT in model.calls[1].text


async def test_prose_on_both_paths_is_a_format_failure_not_a_budget_one():
    def handle(call: Call) -> Reply:
        return Reply(text="这张表看起来是业务明细，具体含义需要结合业务确认。", thinking="……")

    model = Scripted(handle)
    got = await D.run_model_draft(model, _source({"t40": _table(40)}), ["t40"])
    assert [c.mode for c in model.calls] == ["structured", "text"], "格式不对不拆小重试"
    assert got["t40"].status == "failed" and got["t40"].error == "模型的回复不是约定的格式"


async def test_a_model_without_structured_output_goes_to_text():
    class TextOnly(Scripted):
        def with_structured_output(self, schema, **_):
            raise NotImplementedError

    tables = {"t4": _table(4)}
    model = TextOnly(answering(tables))
    got = await D.run_model_draft(model, _source(tables), ["t4"])
    assert got["t4"].status == "ok" and [c.mode for c in model.calls] == ["text"]


async def test_network_errors_are_retried_with_backoff(_no_wait):
    tables = {"t4": _table(4)}
    ok = answering(tables)

    def handle(call: Call) -> Reply:
        if call.n < 2:
            raise ConnectError("All connection attempts failed")
        return ok(call)

    model = Scripted(handle)
    got = await D.run_model_draft(model, _source(tables), ["t4"])
    assert got["t4"].status == "ok" and len(model.calls) == 3
    assert _no_wait == list(catalog.MODEL_RETRY_DELAYS_S)


async def test_server_errors_give_up_after_two_retries():
    def handle(call: Call) -> Reply:
        raise _Status(503, "Service Unavailable")

    model = Scripted(handle)
    got = await D.run_model_draft(model, _source({"t4": _table(4)}), ["t4"])
    assert len(model.calls) == 3
    assert got["t4"].status == "failed"
    assert got["t4"].error == "调用模型出错：Service Unavailable（已重试 2 次）"


async def test_other_errors_are_not_retried():
    def handle(call: Call) -> Reply:
        raise RuntimeError("网关拒绝了这次请求")

    model = Scripted(handle)
    got = await D.run_model_draft(model, _source({"t4": _table(4)}), ["t4"])
    assert len(model.calls) == 1 and got["t4"].error == "调用模型出错：网关拒绝了这次请求"


async def test_a_call_past_the_time_limit_is_split_not_retried(monkeypatch, _no_wait):
    """超时当作这一段太大：不原样重试同样大小的段，拆成两半再问（同额度用完）。超时的调用拿不到用量，记 0。"""
    monkeypatch.setattr(catalog, "MODEL_TIMEOUT_S", 0.05)
    tables = {"t24": _table(24)}
    ok = answering(tables, usage=(100, 50))

    async def handle(call: Call) -> Reply:
        if call.n == 0:
            await asyncio.sleep(1)
        return ok(call)

    model, reports = Scripted(handle), Reports()
    got = await D.run_model_draft(model, _source(tables), ["t24"], hooks=reports.hooks())
    cols = [f"c{i:03d}" for i in range(1, 25)]
    assert got["t24"].status == "ok"
    assert [c.asked for c in model.calls] == [{"t24": ("full", None)}, {"t24": ("table+columns", cols[:12])},
                                              {"t24": ("columns", cols[12:])}]
    assert reports.failures == ["timeout", None, None]
    assert [(r.input_tokens, r.output_tokens) for r in reports.items] == [(0, 0), (100, 50), (100, 50)]
    assert _no_wait == [], "不退避重试"


async def test_a_small_part_past_the_time_limit_is_not_retried(monkeypatch):
    monkeypatch.setattr(catalog, "MODEL_TIMEOUT_S", 0.05)

    async def slow(call: Call) -> Reply:
        await asyncio.sleep(1)
        return Reply(obj={"tables": []})

    model = Scripted(slow)
    got = await D.run_model_draft(model, _source({"t4": _table(4)}), ["t4"])
    assert len(model.calls) == 1, "拆不开、也不原样重试"
    assert got["t4"].status == "failed" and got["t4"].error == "模型 0.05 秒内没有写完这一段"


# --------------------------------------------------------------------------
# 分段：宽表拆成表级一段 + 列分段，窄表拼段
# --------------------------------------------------------------------------


async def test_a_wide_table_is_split_into_a_table_part_and_column_parts():
    tables = {"wide95": _table(95, comment="虚构的宽表")}
    model = Scripted(answering(tables))
    done: list[str] = []

    async def part_done(table: str) -> None:
        done.append(table)

    got = await D.run_model_draft(model, _source(tables), ["wide95"], hooks=D.Hooks(part_done=part_done))
    o = got["wide95"]
    assert o.status == "ok" and o.parts_total == 4 and o.parts_done == 4 and done == ["wide95"] * 4
    assert len(o.notes["columns"]) == 95 and o.notes["label"]["value"] == "wide95的中文名"
    first, *rest = model.calls
    assert first.asked == {"wide95": ("table", None)}
    assert catalog.CATALOG_DRAFT_PROMPT_LABELS["brief_columns"].format(n=95) in first.text
    assert sorted(len(c.asked["wide95"][1]) for c in rest) == [31, 32, 32]
    for call in rest:
        assert call.asked["wide95"][0] == "columns"
        assert "wide95的中文名" in call.text and "虚构的宽表" in call.text, "列分段带着表注释和表级起草结果"
    assert {c for call in rest for c in call.asked["wide95"][1]} == {f"c{i:03d}" for i in range(1, 96)}


@pytest.mark.parametrize("n, sizes", [(164, [33, 33, 33, 33, 32]), (95, [32, 32, 31]), (41, [21, 20]),
                                      (80, [40, 40]), (81, [27, 27, 27])])
async def test_column_parts_are_even_without_a_small_tail(monkeypatch, n, sizes):
    """列分段数 = 列数 / 40 向上取整，各段列数平均（相差不超过 1）：不再留下几列的尾段（实测会思考的模型常对这种小段只给
    空壳）。表级那段照旧在最前面。"""
    monkeypatch.setattr(catalog, "MODEL_CONCURRENCY", 1)
    tables = {"wide": _table(n)}
    model = Scripted(answering(tables))
    got = await D.run_model_draft(model, _source(tables), ["wide"])
    o = got["wide"]
    assert o.status == "ok" and o.parts_total == 1 + len(sizes) == D.parts_for(n)
    assert model.calls[0].asked == {"wide": ("table", None)}
    parts = [c.asked["wide"][1] for c in model.calls[1:]]
    assert [len(p) for p in parts] == sizes
    assert [c for p in parts for c in p] == [f"c{i:03d}" for i in range(1, n + 1)], "按表结构的顺序连续切开"


async def test_a_wide_table_with_one_failed_part_is_partial_and_says_which():
    tables = {"wide95": _table(95)}
    ok = answering(tables)

    def handle(call: Call) -> Reply:
        part, cols = call.asked["wide95"]
        if part == "columns" and "c041" in cols:
            raise RuntimeError("网关拒绝")
        return ok(call)

    model = Scripted(handle)
    got = await D.run_model_draft(model, _source(tables), ["wide95"])
    o = got["wide95"]
    assert o.status == "partial" and len(o.notes["columns"]) == 63
    assert o.error == ("这张表 95 列，分 4 段起草，第 3 段（c033 到 c064，共 32 列）没写出来：调用模型出错：网关拒绝。"
                       "可以只重试这张表")
    compat = (await catalog.draft_with_model(Scripted(handle), _source(tables), ["wide95"]))["wide95"]
    assert compat.covered == frozenset() and compat.error == o.error, "没写全的不算完整起草过，旧的模型项不删"


async def test_reasons_name_each_part_and_say_these_parts(monkeypatch):
    """几段因为同一个原因没写出来：原因里写「这几段」；每段写上列的范围（列名照表结构原样）；拆过的才补「已拆小重试」。"""
    monkeypatch.setattr(catalog, "MODEL_SHRINK_AFTER", 99)
    tables = {"wide95": _table(95)}
    ok = answering(tables)

    def handle(call: Call) -> Reply:
        part, cols = call.asked["wide95"]
        if part == "columns" and cols[0] >= "c033":          # 第 3、4 段（c033 起）一直用完额度
            return EXHAUSTED
        return ok(call)

    got = await D.run_model_draft(Scripted(handle), _source(tables), ["wide95"])
    o = got["wide95"]
    assert o.status == "partial" and len(o.notes["columns"]) == 32
    assert o.error == ("这张表 95 列，分 4 段起草，第 3 段（c033 到 c064，共 32 列）、第 4 段（c065 到 c095，共 31 列）没写出来："
                       + _exhausted("这几段") + D.REASON_SPLIT + "。可以只重试这张表")


async def test_a_part_that_was_never_halved_does_not_say_it_was():
    """被截断后只补剩下的（不是拆小）：补的那次也用完了额度、又拆不开，原因里不写「已拆小重试」。"""
    tables = {"t9": _table(9)}
    full = json.dumps({"tables": [{"table": "t9", **_reply("t9", tables["t9"])}]}, ensure_ascii=False)
    cut = full[:full.index('"c005"') + 8]

    def handle(call: Call) -> Reply:
        return Reply(text=cut, finish="length") if call.n == 0 else EXHAUSTED

    model = Scripted(handle)
    got = await D.run_model_draft(model, _source(tables), ["t9"])
    o = got["t9"]
    assert len(model.calls) == 2 and model.calls[1].asked == {"t9": ("columns", [f"c{i:03d}" for i in range(5, 10)])}
    assert o.status == "partial"
    assert o.error == "这张表 9 列，有 5 列（c005 到 c009）没写出来：" + _exhausted() + "。可以只重试这张表"


@pytest.mark.parametrize("missing, said", [
    (["c004"], "有 1 列（c004）"),
    (["c003", "c007"], "有 2 列（c003、c007）"),
    (["c002", "c004", "c006", "c008"], "有 4 列（c002、c004、c006 等）"),
    (["c003", "c004", "c005", "c006"], "有 4 列（c003 到 c006）"),
])
async def test_columns_not_written_are_named(missing, said):
    tables = {"t8": _table(8)}
    ok = answering(tables)

    def handle(call: Call) -> Reply:
        reply = ok(call)
        for entry in reply.obj["tables"]:
            entry["columns"] = [c for c in entry.get("columns", []) if c["name"] not in missing]
        return reply

    got = await D.run_model_draft(Scripted(handle), _source(tables), ["t8"])
    assert got["t8"].error == f"这张表 8 列，{said}没写出来：{D.REASON_UNANSWERED}。可以只重试这张表"


async def test_tables_are_packed_up_to_40_columns_and_4_tables():
    widths = {"ta": 15, "tb": 15, "tc": 15, "td": 5, "te": 5, "tf": 5, "tg": 5, "th": 41}
    tables = {name: _table(n) for name, n in widths.items()}
    model = Scripted(answering(tables))
    got = await D.run_model_draft(model, _source(tables), list(widths))
    assert set(_status(got).values()) == {"ok"}
    asked = sorted((tuple(sorted(c.asked)), c.asked[next(iter(c.asked))][0]) for c in model.calls)
    assert asked == sorted([
        (("ta", "tb"), "full"), (("tc", "td", "te", "tf"), "full"), (("tg",), "full"),
        (("th",), "table"), (("th",), "columns"), (("th",), "columns"),
    ])


# --------------------------------------------------------------------------
# 报错去重、止损、几次起草轮流用模型
# --------------------------------------------------------------------------


async def test_the_same_reason_is_said_once_even_when_some_parts_were_split(monkeypatch):
    """宽表的表级那段拆不开、列分段拆过：原因只说一次，拆过的在最后补一句。"""
    monkeypatch.setattr(catalog, "MODEL_GIVE_UP_AFTER", 99)
    monkeypatch.setattr(catalog, "MODEL_PART_MAX_CALLS", 99)
    model = Scripted(_hollow)
    got = await D.run_model_draft(model, _source({"wide45": _table(45)}), ["wide45"])
    o = got["wide45"]
    assert o.status == "failed"
    assert o.error == "模型只给了空壳，没有写出可用的内容；已拆小重试仍没写出"


def test_reasons_are_joined_by_their_base():
    """(原因, 第几段)：同样的原因只说一次；同一个原因落在几段上写「这几段」，同一段拆出来的几块仍是「这一段」。"""
    W = D._Why
    ex = D.REASON_EXHAUSTED.format(budget=32768, seg=D.SEG)
    assert D._join([(W(D.REASON_HOLLOW), 1), (W(D.REASON_HOLLOW, split=True), 2)]) == \
        D.REASON_HOLLOW + D.REASON_SPLIT
    assert D._join([(W(ex, split=True), 2), (W(D.REASON_HOLLOW), 1)]) == \
        f"{_exhausted()}；{D.REASON_HOLLOW}{D.REASON_SPLIT}"
    assert D._join([(W(ex), 2), (W(ex), 3)]) == _exhausted("这几段")
    assert D._join([(W(ex), 2), (W(ex, split=True), 2)]) == _exhausted() + D.REASON_SPLIT
    assert D._join([(W("甲"), 1), (W("乙"), 1), (W("丙", split=True), 1)]) == "甲；乙", "没说出来的原因不补后缀"
    halted = D.REASON_HALTED.format(n=catalog.MODEL_GIVE_UP_AFTER)
    assert D._join([(W(D.REASON_HOLLOW, halted=halted), 1), (W(D.REASON_HOLLOW), 2)]) == D.REASON_HOLLOW + halted
    slow = D.REASON_SLOW.format(n=catalog.MODEL_MIN_PART_COLUMNS)
    assert D._join([(W(D.REASON_HOLLOW, halted=halted), 1), (W(D.REASON_HOLLOW, halted=slow), 2),
                    (W(D.REASON_HOLLOW, halted=halted), 3)]) == D.REASON_HOLLOW + halted + slow, "两种止损各说一次"
    assert D._join([(W("调用模型出错：{坏的}"), 1)]) == "调用模型出错：{坏的}", "报错原文里的花括号原样留着"


def _by_table(model: Scripted) -> dict[str, int]:
    out: dict[str, int] = {}
    for call in model.calls:
        for name in call.asked:
            out[name] = out.get(name, 0) + 1
    return out


async def test_after_two_segments_in_a_row_come_to_nothing_the_rest_are_not_split(monkeypatch):
    """模型明显用不了（思考总是吃满额度）：连续 2 段拆到底仍一项没写出，后面的段只问一次、不再拆，报错说明已停止拆小重试。
    有一段写出来了就恢复拆小。一次只起草一段（并发 1），先后是确定的。"""
    monkeypatch.setattr(catalog, "MODEL_CONCURRENCY", 1)
    monkeypatch.setattr(catalog, "MODEL_BATCH_TABLES", 1)
    assert catalog.MODEL_GIVE_UP_AFTER == 2
    tables = {f"t{i}": _table(10) for i in range(1, 5)}
    exhausted = Reply(text="", finish="length", thinking="……")

    model = Scripted(lambda call: exhausted)
    got = await D.run_model_draft(model, _source(tables), list(tables))
    assert _by_table(model) == {"t1": 3, "t2": 3, "t3": 1, "t4": 1}
    budget = _exhausted()
    assert got["t1"].error == budget + D.REASON_SPLIT
    assert got["t3"].error == budget + "；模型连续 2 段都没写出来，已停止拆小重试，建议更换起草模型"

    ok = answering(tables)
    model = Scripted(lambda call: ok(call) if "t3" in call.asked else exhausted)
    got = await D.run_model_draft(model, _source(tables), list(tables))
    assert _by_table(model) == {"t1": 3, "t2": 3, "t3": 1, "t4": 3}, "t3 写出来了，t4 照常拆小"
    assert got["t3"].status == "ok" and got["t4"].error == budget + D.REASON_SPLIT


async def test_the_column_parts_of_a_wide_table_stop_splitting_too(monkeypatch):
    """宽表的列分段一段一段排队起草（一段拆到底再起草下一段），止损在宽表里同样起作用。

    95 列：表级那段拆不开（结构化、纯文本各一次）；第 2 段 32 列拆小、到 8 次封顶；这时已经连续 2 段没写出来，第 3、4 段
    只问一次、换纯文本一次。"""
    monkeypatch.setattr(catalog, "MODEL_CONCURRENCY", 1)
    model = Scripted(_hollow)
    got = await D.run_model_draft(model, _source({"wide95": _table(95)}), ["wide95"])
    assert len(model.calls) == 2 + 8 + 2 + 2
    assert got["wide95"].error == (D.REASON_HOLLOW + "；每段最多调用 8 次模型，这一段到了次数仍没写完，已停止重试"
                                   + D.REASON_SPLIT + "；模型连续 2 段都没写出来，已停止拆小重试，建议更换起草模型")


async def test_a_wide_table_that_never_comes_back_is_bounded_by_the_first_wave():
    """默认并发（3 段同时起草）：294 列（8 个列分段）一直只给空壳，最多是表级那段 2 次、头 3 个列分段各 8 次（封顶）、
    其余 5 段各 2 次。以前所有列分段一下子都排上去、一层一层地拆，要 168 次。"""
    model = Scripted(_hollow)
    got = await D.run_model_draft(model, _source({"wide294": _table(294)}), ["wide294"])
    assert got["wide294"].status == "failed" and got["wide294"].parts_total == 9
    assert len(model.calls) <= 2 + 3 * 8 + 5 * 2


async def test_a_big_draft_does_not_hold_the_model_ahead_of_a_later_one(monkeypatch):
    """全进程的调用名额先来先放：一次起草不把它的所有段一下子都排上去，后开始的那次起草（另一个数据源）能轮上。"""
    monkeypatch.setattr(catalog, "MODEL_CONCURRENCY", 1)
    monkeypatch.setattr(catalog, "MODEL_BATCH_TABLES", 1)
    big = {f"a{i}": _table(4) for i in range(1, 5)}
    small = {"b1": _table(4)}
    order: list[str] = []
    release = asyncio.Event()

    def handler(tables: dict) -> Callable[[Call], Any]:
        ok = answering(tables)

        async def handle(call: Call) -> Reply:
            order.append(next(iter(call.asked)))
            if len(order) == 1:
                await release.wait()
            return ok(call)
        return handle

    first = asyncio.create_task(D.run_model_draft(Scripted(handler(big)), _source(big), list(big)))
    while not order:
        await asyncio.sleep(0)
    second = asyncio.create_task(D.run_model_draft(Scripted(handler(small)), _source(small), ["b1"]))
    for _ in range(20):
        await asyncio.sleep(0)
    release.set()
    got_big, got_small = await asyncio.gather(first, second)
    assert set(_status(got_big).values()) == {"ok"} and _status(got_small) == {"b1": "ok"}
    assert order == ["a1", "b1", "a2", "a3", "a4"]


def _order(model: Scripted) -> list[tuple[str, str]]:
    return [(name, part) for call in model.calls for name, (part, _) in call.asked.items()]


async def test_narrow_tables_are_not_cut_in_by_the_column_parts_of_a_wide_one(monkeypatch):
    """一个任务里按表的先后排队：宽表的表级那段写完，它的列分段排到已经排着的窄表段后面（先来先服务），不插队。
    以前插到队首，窄表要等宽表的列分段全部派完。"""
    monkeypatch.setattr(catalog, "MODEL_CONCURRENCY", 1)
    monkeypatch.setattr(catalog, "MODEL_BATCH_TABLES", 1)
    tables = {"wide": _table(45), "na": _table(4), "nb": _table(4)}
    model = Scripted(answering(tables))
    got = await D.run_model_draft(model, _source(tables), ["wide", "na", "nb"])
    assert set(_status(got).values()) == {"ok"}
    assert _order(model) == [("wide", "table"), ("na", "full"), ("nb", "full"), ("wide", "columns"),
                             ("wide", "columns")]


async def test_after_two_exhausted_or_timed_out_calls_new_parts_are_halved(monkeypatch):
    """额度用完或超时累计 2 次，之后新开始的段列数上限减半（40 → 20 → 10，最低 10）；已经开始的段照旧（按原因拆小）。
    每次调用的结果里带着当前的上限（part_columns）。"""
    monkeypatch.setattr(catalog, "MODEL_CONCURRENCY", 1)
    monkeypatch.setattr(catalog, "MODEL_BATCH_TABLES", 1)
    tables = {f"t{i}": _table(40) for i in range(1, 4)}
    ok = answering(tables)

    def handle(call: Call) -> Reply:
        (_, (part, cols)), = call.asked.items()
        width = 40 if part == "full" else len(cols)
        return EXHAUSTED if width > 10 else ok(call)        # 这个模型一次只写得出 10 列

    model, reports = Scripted(handle), Reports()
    got = await D.run_model_draft(model, _source(tables), list(tables), hooks=reports.hooks())
    assert set(_status(got).values()) == {"ok"}
    asked = {name: [(part, len(cols or [])) for c in model.calls for n, (part, cols) in c.asked.items() if n == name]
             for name in tables}
    # t1：整张 40 列开始，用完额度拆成 20、20，又各拆成 10、10
    assert asked["t1"][0] == ("full", 0) and len(asked["t1"]) == 7
    # 第 2 次用完额度后上限是 20：t2 一开始就按 20 列切成两段（各自再拆一次）
    assert asked["t2"][0] == ("table+columns", 20) and ("columns", 20) in asked["t2"]
    # 第 4 次之后上限是 10：t3 一开始就切成 4 段 10 列，一次都没用完额度
    assert asked["t3"] == [("table+columns", 10), ("columns", 10), ("columns", 10), ("columns", 10)]
    caps = [r.part_columns for r in reports.items]
    assert list(dict.fromkeys(caps)) == [40, 20, 10]
    assert caps[:2] == [40, 20], "第 2 次用完额度的那次调用之后就是 20"
    assert min(caps) == 10 == catalog.MODEL_MIN_PART_COLUMNS


async def test_the_notice_comes_when_four_of_six_calls_fail_and_goes_when_fewer_than_two(monkeypatch):
    """最近 6 次调用有 4 次以上没写出来：提醒（写明次数和原因，建议换起草模型、减少表或停止）；之后最近 6 次里没写出来
    的少于 2 次就清掉。"""
    monkeypatch.setattr(catalog, "MODEL_CONCURRENCY", 1)
    monkeypatch.setattr(catalog, "MODEL_BATCH_TABLES", 1)
    tables = {f"t{i}": _table(4) for i in range(1, 10)}
    ok = answering(tables)

    def handle(call: Call) -> Reply:
        name = next(iter(call.asked))
        if name in ("t1", "t2"):
            raise RuntimeError("网关拒绝了这次请求")
        if name == "t3":
            return EXHAUSTED
        if name == "t4":
            return _hollow(call)
        return ok(call)

    reports = Reports()
    got = await D.run_model_draft(Scripted(handle), _source(tables), list(tables), hooks=reports.hooks())
    assert reports.failures == ["error", "error", "exhausted", "hollow", "hollow"] + [None] * 5
    notices = [r.notice for r in reports.items]
    assert notices[:3] == [None] * 3
    assert notices[3] == ("最近 4 次调用有 4 次没写出来（额度用完 1 次、只给空壳 1 次、出错 2 次）。可以在「设置 → 偏好设置」"
                          "的「起草模型」换一个模型、减少这次起草的表，或者停止这次起草")
    assert notices[4].startswith("最近 5 次调用有 5 次没写出来")
    assert notices[7].startswith("最近 6 次调用有 3 次没写出来"), "还有 2 次以上没写出来：留着，次数照实写"
    assert notices[8].startswith("最近 6 次调用有 2 次没写出来")
    assert notices[9] is None, "最近 6 次只有 1 次没写出来：清掉"
    assert got["t9"].status == "ok"


# --------------------------------------------------------------------------
# 复核后的修正：每个原始段一共 8 次、一直超时的模型几轮就停、止损后纯文本不再原样再问
# --------------------------------------------------------------------------


def _width(call: Call, tables: dict) -> int:
    """这次调用请模型写几列（只写表级的项是 0）。"""
    return sum(len(tables[name]["columns"]) if part == "full" else len(cols or [])
               for name, (part, cols) in call.asked.items())


async def test_the_chunks_of_a_part_cut_by_the_smaller_cap_share_its_eight_calls(monkeypatch):
    """每个原始段（连同段大小减半后按上限分开的各块、拆出来的、补问的、重试的）一共最多调用 8 次。以前分开的每一块各有
    8 次：上限缩到 10 列以后一直只给空壳，4 张窄表拼成的 40 列段调用了 20 次，单张 40 列的表也是 20 次。"""
    monkeypatch.setattr(catalog, "MODEL_CONCURRENCY", 1)
    monkeypatch.setattr(catalog, "MODEL_GIVE_UP_AFTER", 99)        # 不让止损先停下来
    tables = {"w40": _table(40), **{f"n{i}": _table(10) for i in range(1, 5)}, "x40": _table(40)}

    def handle(call: Call) -> Reply:
        return EXHAUSTED if "w40" in call.asked else _hollow(call)

    model, reports = Scripted(handle), Reports()
    got = await D.run_model_draft(model, _source(tables), list(tables), hooks=reports.hooks())
    first = sum(1 for c in model.calls if "w40" in c.asked)
    assert first == 8 and reports.items[first - 1].part_columns == 10, "w40 一直用完额度，上限缩到 10 列"
    packed = [c for c in model.calls if set(c.asked) & {"n1", "n2", "n3", "n4"}]
    assert len(packed) == 8, "拼成的 40 列段按 10 列分开成 4 块，4 块一共 8 次"
    assert all(len(c.asked) == 1 and _width(c, tables) <= 10 for c in packed)
    assert {got[f"n{i}"].status for i in range(1, 5)} == {"failed"}
    assert "每段最多调用 8 次模型，这一段到了次数仍没写完，已停止重试" in {got[f"n{i}"].error for i in range(1, 5)}, \
        "次数用完时还没问的块记「每段最多调用 8 次」"
    wide = [c for c in model.calls if "x40" in c.asked]
    assert len(wide) == 8 and _width(wide[0], tables) == 10, "单张 40 列的表按 10 列分开，各块一共 8 次"
    assert len(reports.items) == len(model.calls)


class AlwaysSlow:
    """每次调用都超时（立刻抛 TimeoutError，和到了单次调用的时限走同一条路），fits(列数) 为真的照常写。

    记下每次调用在第几轮：每次调用一样长（一个时限），一次调用开始时，已经结束的调用里最晚的那一轮的下一轮——
    轮数乘以时限就是用掉的时间。"""

    def __init__(self, tables: dict, fits: Callable[[int], bool] = lambda n: False) -> None:
        self.tables, self.fits, self.ok = tables, fits, answering(tables)
        self.rounds: list[int] = []
        self._ended: list[int] = []

    async def __call__(self, call: Call) -> Reply:
        r = 1 + max(self._ended, default=0)
        self.rounds.append(r)
        for _ in range(5):
            await asyncio.sleep(0)
        self._ended.append(r)
        if self.fits(_width(call, self.tables)):
            return self.ok(call)
        raise TimeoutError()


SLOW_STOP = "；拆到每段 10 列仍然超时，已停止拆小重试，建议更换起草模型"


async def test_a_model_that_always_times_out_stops_after_three_rounds_not_seven():
    """每次调用都超时、和段大小无关：超时拆出来的两半同时问（不再等带表级项的前一半写完才问后一半），拆到每段 10 列的
    调用接连超时 2 次就停止拆小，拆出来还没问的块不再问。40 列的表 3 轮、6 次调用就结束（时限 420 秒时约 21 分钟）；
    以前 40→20→10→5 一层一层串着问，8 次调用约 7 轮（约 49 分钟）。"""
    tables = {"t40": _table(40)}
    slow = AlwaysSlow(tables)
    model, reports = Scripted(slow), Reports()
    got = await D.run_model_draft(model, _source(tables), ["t40"], hooks=reports.hooks())
    assert [_width(c, tables) for c in model.calls] == [40, 20, 20, 10, 10, 10], "拆到每段 10 列仍超时就不再往下拆"
    assert slow.rounds == [1, 2, 2, 3, 3, 3], "两半同时问"
    assert reports.failures == ["timeout"] * 6 and len(reports.items) == len(model.calls)
    o = got["t40"]
    assert o.status == "failed" and o.error == "模型 420 秒内没有写完这一段" + D.REASON_SPLIT + SLOW_STOP


async def test_three_parts_that_always_time_out_end_in_four_rounds():
    """首轮同时起草的 3 段都一直超时：拆到每段 10 列仍超时就一起停下，不再各自拆满 8 次。12 次调用、4 轮；以前 24 次
    调用、约 8 轮（止损要等一整段结束才计数，首轮的 3 段都跑满）。"""
    tables = {f"t{i}": _table(40) for i in range(1, 4)}
    slow = AlwaysSlow(tables)
    model = Scripted(slow)
    got = await D.run_model_draft(model, _source(tables), list(tables))
    assert len(model.calls) == 12 and max(slow.rounds) == 4
    assert {o.status for o in got.values()} == {"failed"}
    assert all(o.error.endswith(SLOW_STOP) for o in got.values())


async def test_timeouts_that_go_away_at_ten_columns_are_still_split_down_and_written():
    """超时和段大小有关（40、20 列超时，10 列写得完）：照旧拆到 10 列写出来，不因为前面超时多就停下。"""
    tables = {"t40": _table(40)}
    slow = AlwaysSlow(tables, fits=lambda n: n <= 10)
    model = Scripted(slow)
    got = await D.run_model_draft(model, _source(tables), ["t40"])
    assert got["t40"].status == "ok" and len(got["t40"].notes["columns"]) == 40
    assert len(model.calls) == 7 and max(slow.rounds) == 4


async def test_a_halted_part_that_could_be_split_is_not_asked_again_in_text_mode(monkeypatch):
    """止损以后本来能拆小的段只给空壳：已经走纯文本的不再原样再问（约定只对拆不开的段再问一次）；结构化输出的照旧换
    纯文本问一次，看看是不是结构化输出的问题（见 test_the_column_parts_of_a_wide_table_stop_splitting_too）。"""
    monkeypatch.setattr(catalog, "MODEL_CONCURRENCY", 1)
    monkeypatch.setattr(catalog, "MODEL_BATCH_TABLES", 1)
    tables = {f"t{i}": _table(10) for i in range(1, 5)}
    model = Scripted(_text_only(_hollow))
    got = await D.run_model_draft(model, _source(tables), list(tables))
    # t1：结构化不支持 1 次、纯文本 1 次、拆成两半各问一次再原样再问一次；t2 同样（少了结构化那次）；之后止损
    assert _by_table(model) == {"t1": 6, "t2": 5, "t3": 1, "t4": 1}
    assert got["t3"].error == (catalog.MODEL_EMPTY_REASON
                               + "；模型连续 2 段都没写出来，已停止拆小重试，建议更换起草模型")


# --------------------------------------------------------------------------
# 起草要求、用量
# --------------------------------------------------------------------------


async def test_guidance_goes_into_the_prompt_after_the_rules():
    tables = {"t4": _table(4)}
    model = Scripted(answering(tables))
    src = _source(tables, description="虚构的景区票务库")
    await D.run_model_draft(model, src, ["t4"],
                            background=D.Background(guidance="金额一律按元写单位", note="这次重点看会员相关的列"))
    text = model.calls[0].text
    block = ("使用者补充的背景和要求：\n数据源说明：虚构的景区票务库\n起草要求：金额一律按元写单位\n"
             "本次说明：这次重点看会员相关的列")
    assert block in text
    rule = "它和这里的规矩冲突时，以这里的规矩为准"
    assert rule in catalog.CATALOG_DRAFT_SYSTEM
    assert text.index(rule) < text.index(block) < text.index("请为下面") < text.index("表 t4")

    plain = Scripted(answering(tables))
    await D.run_model_draft(plain, _source(tables), ["t4"])
    assert "使用者补充的背景和要求：" not in plain.calls[0].text, "都没有就不出现这一段（系统提示词里那条规矩照旧在）"
    only_note = Scripted(answering(tables))
    await D.run_model_draft(only_note, _source(tables), ["t4"], background=D.Background(note="只看金额"))
    assert "使用者补充的背景和要求：\n本次说明：只看金额\n\n请为下面" in only_note.calls[0].text


async def test_the_prompt_asks_not_to_draft_every_column_while_thinking():
    assert "思考时不必把要输出的内容逐列草拟一遍" in catalog.CATALOG_DRAFT_SYSTEM


async def test_usage_is_added_up_through_the_hooks():
    tables = {"orders_x": _table(24)}
    ok = answering(tables, usage=(120, 60))

    def handle(call: Call) -> Reply:
        if call.n == 0:
            return Reply(text="", finish="length", thinking="……", usage=(100, 32768))
        return ok(call)

    reports = Reports()
    await D.run_model_draft(Scripted(handle), _source(tables), ["orders_x"], hooks=reports.hooks())
    assert [(r.input_tokens, r.output_tokens, r.failure) for r in reports.items] == \
        [(100, 32768, "exhausted"), (120, 60, None), (120, 60, None)]
    assert {r.notice for r in reports.items} == {None} and {r.part_columns for r in reports.items} == {40}


def test_budget_words_match_the_other_drafts():
    from app.data import recipe_ai

    assert D.BUDGET_WORDS == recipe_ai._BUDGET_WORDS


# --------------------------------------------------------------------------
# 从正文里取 JSON
# --------------------------------------------------------------------------


@pytest.mark.parametrize("text, tables, truncated", [
    # 前面有一句话、带括号的说明、围栏
    ('按 {table} 的格式写好了：\n```json\n{"tables": [{"table": "a", "label": "甲"}]}\n```', ["a"], False),
    ('[说明] 草稿：{"tables": [{"table": "a"}]} 以上', ["a"], False),
    # 外面多包了一层
    ('{"result": {"tables": [{"table": "a"}, {"table": "b"}]}}', ["a", "b"], False),
    # 截断在字符串、数字、字面量、键名中间：只收写完了的
    ('{"tables": [{"table": "a", "label": "甲"}, {"table": "b", "label": "乙', ["a", "b"], True),
    ('{"tables": [{"table": "a", "columns": [{"name": "x", "label": "某"}, {"name": "y", "len": 12', ["a"], True),
    ('{"tables": [{"table": "a", "keys": ["x", "y"', ["a"], True),
    ('{"tables": [{"table": "a"}, {"tab', ["a"], True),
    ('{"tables": [{"table": "a", "flag": tr', ["a"], True),
])
def test_json_is_taken_from_the_text(text, tables, truncated):
    reply = D._parse_text(text, cut=False)
    assert reply is not None and [e["table"] for e in reply.entries] == tables
    assert reply.truncated is truncated


def test_cut_values_are_dropped_and_complete_ones_kept():
    text = '{"tables": [{"table": "a", "label": "甲", "keys": ["x", "y"], "columns": [{"name": "x", "label": "某"}, ' \
           '{"name": "y", "label": "半'
    entry = D._parse_text(text, cut=True).entries[0]
    assert entry == {"table": "a", "label": "甲", "keys": ["x", "y"], "columns": [{"name": "x", "label": "某"}]}
    cut_keys = D._parse_text('{"tables": [{"table": "a", "label": "甲", "keys": ["x", "y"', cut=True).entries[0]
    assert cut_keys == {"table": "a", "label": "甲"}, "没写完的业务主键整项不要，不能只留前半"


@pytest.mark.parametrize("text", ["好的，我来写", "{坏的 json}", ""])
def test_not_json_is_not_a_reply(text):
    assert D._parse_text(text, cut=False) is None
