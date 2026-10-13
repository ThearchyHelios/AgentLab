"""数据目录模型起草的引擎（data/catalog_draft.py）：分段、按失败原因处理、捡回被截断的部分、起草要求、用量。

用假模型复刻实测查明的几种失败（不调用真实模型）：
- 思考加输出用完额度：finish_reason=length 且正文为空（只有思考）、LengthFinishReasonError → 拆成两半再问；
- 只给空壳 → 拆开再问；拆不开的换纯文本问一次，写出来了这次起草后面都走纯文本；
- 被截断的 JSON：已经完整的表、列对象捡回来，只补剩下的；
- 只回一部分表：只补缺的，补一次；
- 服务方嫌额度太大 → 退回 8192，后面都用它；结构化输出不支持 → 后面改走纯文本；
- 网络错误、5xx、超时 → 退避重试，最多 2 次，报错写明重试过几次。

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


async def test_splitting_stops_at_three_levels_and_five_columns():
    tables = {"wide40": _table(40)}
    model = Scripted(lambda call: Reply(text="", finish="length", thinking="……"))
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
    tables = {"a_tab": _table(4), "b_tab": _table(4), "c_tab": _table(4)}
    ok = answering(tables)

    def handle(call: Call) -> Reply:
        reply = ok(call)
        keep = {0: {"a_tab"}, 1: {"b_tab"}}.get(call.n, set())
        reply.obj["tables"] = [e for e in reply.obj["tables"] if e["table"] in keep]
        return reply

    model = Scripted(handle)
    got = await D.run_model_draft(model, _source(tables), ["a_tab", "b_tab", "c_tab"])
    assert len(model.calls) == 2
    assert model.calls[1].asked == {"b_tab": ("full", None), "c_tab": ("full", None)}
    assert _status(got) == {"a_tab": "ok", "b_tab": "ok", "c_tab": "failed"}
    assert got["c_tab"].error == "模型的回复里没有这张表"


async def test_columns_the_model_skips_are_asked_once_and_then_left_empty():
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
    assert o.status == "ok" and o.error is None, "补问过仍然没给的列算拿不准、留空"
    assert set(o.notes["columns"]) == {f"c{i:03d}" for i in range(1, 7)}


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


async def test_a_call_past_the_time_limit_is_retried_then_reported(monkeypatch):
    monkeypatch.setattr(catalog, "MODEL_TIMEOUT_S", 0.05)

    async def slow(call: Call) -> Reply:
        await asyncio.sleep(1)
        return Reply(obj={"tables": []})

    model = Scripted(slow)
    got = await D.run_model_draft(model, _source({"t4": _table(4)}), ["t4"])
    assert len(model.calls) == 3
    assert got["t4"].error == "模型 0.05 秒内没有写完这一段（已重试 2 次）"


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
    assert sorted(len(c.asked["wide95"][1]) for c in rest) == [15, 40, 40]
    for call in rest:
        assert call.asked["wide95"][0] == "columns"
        assert "wide95的中文名" in call.text and "虚构的宽表" in call.text, "列分段带着表注释和表级起草结果"
    assert {c for call in rest for c in call.asked["wide95"][1]} == {f"c{i:03d}" for i in range(1, 96)}


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
    assert o.status == "partial" and len(o.notes["columns"]) == 55
    assert o.error == "这张表 95 列，分 4 段起草，第 3 段没写出来（共 40 列）：调用模型出错：网关拒绝。可以只重试这张表"
    compat = (await catalog.draft_with_model(Scripted(handle), _source(tables), ["wide95"]))["wide95"]
    assert compat.covered == frozenset() and compat.error == o.error, "没写全的不算完整起草过，旧的模型项不删"


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

    seen: list[tuple[int, int]] = []

    async def usage(i: int, o: int) -> None:
        seen.append((i, o))

    await D.run_model_draft(Scripted(handle), _source(tables), ["orders_x"], hooks=D.Hooks(usage=usage))
    assert seen == [(100, 32768), (120, 60), (120, 60)]


def test_budget_words_match_the_other_drafts():
    from app.data import recipe_ai

    assert D.BUDGET_WORDS == recipe_ai._BUDGET_WORDS
