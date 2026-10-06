"""写作模型把整篇报告包在一个代码块里（```markdown … ```）：去掉外层围栏，再交给 compose_doc 核对。

不去的话裸数字照样被标出来（代码块里的数字也查），但整篇成了一个代码块，结论句一句都没有：证据裁判、反方审稿
没有句子可审（反方审稿记 challenge_skipped / no_claims），正式运行照样出具，界面上整篇显示成代码。

- 去：去掉首尾空白后整篇正好是一个围栏代码块（``` 或 ~~~，收尾围栏和开头一字不差），信息串为空、markdown 或 md
  （大小写不敏感），围栏外没有别的字；重写那几轮的稿子同样处理
- 不去：信息串是别的语言（sql、json……）、围栏外还有字、有多个代码块、开闭围栏对不上、里面嵌着同样长或更长的同种
  围栏——这些是模型真要贴一段代码，或者分不清哪一行收尾，原样交给 compose_doc
- doc["source"] 记去掉围栏之后的正文：markdown 就是从它渲染出来的，出口契约复核取回的也是这一份文档

模型一律是 MockChatModel 剧本，数据源是现造的 SQLite 文件，区域、金额都是虚构的。
"""
from __future__ import annotations

import asyncio
import json
import re
import sqlite3
import uuid

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from sqlalchemy import delete, select

from app.core import artifact_store
from app.data import introspect
from app.db.base import SessionLocal
from app.db.models import DataSource, Run, RunEvent, Setting
from app.engine import judge as judge_mod
from app.engine import spend, verify_agent
from app.engine.evidence import iter_segments, iter_units
from app.engine.judge import JUDGE_ROLE
from app.engine.judge import SPEND_KEY as JUDGE_SPEND_KEY
from app.engine.nodes import challenge as challenge_mod
from app.engine.nodes import report as report_mod
from app.engine.runner import run_manager, verify_manifest
from app.providers.mock_model import MockChatModel
from tests.fixtures.sources import drop_source


@pytest.fixture(autouse=True)
async def engine_up():
    await run_manager.setup()
    async with SessionLocal() as session:
        await session.execute(delete(Setting).where(Setting.key.in_(
            [spend.SPEND_KEY, JUDGE_SPEND_KEY, "verify", "judge", "copilot"])))
        await session.commit()
    yield
    await run_manager.shutdown()


# --------------------------------------------------------------------------
# 哪些去、哪些不去
# --------------------------------------------------------------------------

BODY = "## 本周概览\n\n华东销售额 [[m:gmv]]，居各区域之首。[[see:m:gmv]]"

UNWRAPPED = [
    pytest.param(f"```markdown\n{BODY}\n```", BODY, id="markdown"),
    pytest.param(f"```\n{BODY}\n```", BODY, id="no-info"),
    pytest.param(f"~~~\n{BODY}\n~~~", BODY, id="tilde"),
    pytest.param(f"~~~md\n{BODY}\n~~~", BODY, id="tilde-md"),
    pytest.param(f"```Markdown\n{BODY}\n```", BODY, id="case-insensitive"),
    pytest.param(f"```MD\n{BODY}\n```", BODY, id="upper-md"),
    pytest.param(f"``` markdown \n{BODY}\n```", BODY, id="spaced-info"),
    pytest.param(f"````markdown\n{BODY}\n````", BODY, id="four-backticks"),
    pytest.param(f"\n\n```markdown\n{BODY}\n```\n\n", BODY, id="blank-lines-around"),
    pytest.param(f"```markdown\r\n{BODY}\r\n```\r\n".replace("\n\n", "\r\n\r\n"), BODY, id="crlf"),
    pytest.param(f"```markdown\n{BODY}\n  ```", BODY, id="indented-close"),
    # 正文里自己的代码块：外层用更长的同种围栏、或者换一种符号，才是一个代码块里套着另一个，里面的原样留下
    pytest.param("````markdown\n## 口径\n\n```sql\nSELECT 1\n```\n\n结论如上。\n````",
                 "## 口径\n\n```sql\nSELECT 1\n```\n\n结论如上。", id="shorter-fence-inside"),
    pytest.param("~~~markdown\n## 口径\n\n```sql\nSELECT 1\n```\n\n结论如上。\n~~~",
                 "## 口径\n\n```sql\nSELECT 1\n```\n\n结论如上。", id="other-fence-inside"),
]

KEPT = [
    pytest.param("```sql\nSELECT region, SUM(amount) AS gmv FROM orders GROUP BY region\n```", id="sql"),
    pytest.param("```python\nprint('华东')\n```", id="python"),
    pytest.param('```json\n{"region": "华东"}\n```', id="json"),
    pytest.param(f"```text\n{BODY}\n```", id="text"),
    pytest.param(f"```markdown 周报\n{BODY}\n```", id="info-with-words"),
    pytest.param(f"以下是本周报告：\n\n```markdown\n{BODY}\n```", id="preamble"),
    pytest.param(f"```markdown\n{BODY}\n```\n\n以上。", id="afterword"),
    pytest.param("```markdown\n## 上半\n```\n\n```markdown\n## 下半\n```", id="two-blocks"),
    pytest.param(f"```markdown\n{BODY}\n~~~", id="mismatched-char"),
    pytest.param(f"````markdown\n{BODY}\n```", id="shorter-close"),
    pytest.param(f"```markdown\n{BODY}\n````", id="longer-close"),
    pytest.param(f"```markdown\n{BODY}", id="unclosed"),
    pytest.param(f"```markdown\n{BODY}\n    ```", id="close-indented-as-code"),
    pytest.param("```markdown\n## 口径\n\n````sql\nSELECT 1\n````\n\n结论如上。\n```", id="longer-fence-inside"),
    pytest.param("```markdown\n## 口径\n\n```sql\nSELECT 1\n```\n\n结论如上。\n```", id="same-fence-inside"),
    pytest.param("```markdown```", id="one-line"),
    pytest.param(BODY, id="plain"),
    pytest.param("", id="empty"),
]


@pytest.mark.parametrize("raw, body", UNWRAPPED)
def test_a_report_wrapped_in_one_markdown_block_is_unwrapped(raw, body):
    assert report_mod._unfenced(raw) == body


@pytest.mark.parametrize("raw", KEPT)
def test_anything_else_is_handed_over_untouched(raw):
    assert report_mod._unfenced(raw) is raw, "不去的一个字都不动：compose_doc 收到的和以前一样，文档哈希也不变"


def test_the_unwrapped_body_reads_as_sentences():
    """去掉围栏后切出的是标题和结论句，不再是一个代码块；裸数字照样被标出来。"""
    from app.engine.evidence import compose_doc

    raw = "```markdown\n## 本周概览\n\n华东销售额 1500，居各区域之首。华南销售额超过华东。\n```"
    wrapped = compose_doc(raw, {}, node_id="write")
    assert [b["type"] for b in wrapped["blocks"]] == ["code"] and wrapped["stats"]["claims"] == 0, "背景：不去时整篇是代码"
    doc = compose_doc(report_mod._unfenced(raw), {}, node_id="write")
    assert [b["type"] for b in doc["blocks"]] == ["heading", "paragraph"]
    assert doc["stats"]["claims"] == 2
    assert [(v["code"], v["text"]) for v in doc["violations"]] == [("uncited_number", "1500")]
    assert doc["source"] == "## 本周概览\n\n华东销售额 1500，居各区域之首。华南销售额超过华东。"


# --------------------------------------------------------------------------
# 报告节点：写作、重写都经过这一步
# --------------------------------------------------------------------------


def node(nid, ntype, label=None, **config):
    return {"id": nid, "type": ntype, "position": {"x": 0, "y": 0}, "data": {"label": label or nid, "config": config}}


def chain(*nodes):
    return {"nodes": list(nodes), "edges": [{"source": a["id"], "target": b["id"]} for a, b in zip(nodes, nodes[1:])]}


KPI = "{'gmv': 45678.5, 'orders': 1234}"
METRICS = [
    {"id": "gmv", "name": "销售额", "unit": "元", "decimals": 1, "expression": "vars.kpi.gmv"},
    {"id": "orders", "name": "订单数", "unit": "单", "expression": "vars.kpi.orders"},
]
CLEAN = "## 本周概览\n\n本周（[[i:week]]）销售额 [[m:gmv]]，订单 [[m:orders]]。[[see:m:gmv,m:orders]]"
RENDERED = "## 本周概览\n\n本周（2026-W37）销售额 45,678.5元，订单 1,234单。"
#: 一个裸数字：写作者自己抄了一遍销售额
BARE = "## 本周概览\n\n本周销售额 45678 元，订单 [[m:orders]]，较上周有所增长。[[see:m:orders]]"


def weekly(**report) -> dict:
    """输入 → 取数 → 口径卡 → 报告 → 成果。"""
    return chain(
        node("start", "input", fields=[{"name": "week", "default": "2026-W37"}]),
        node("fetch", "transform", mode="expression", expression=KPI, assign_to="kpi"),
        node("caliber", "metrics", caliber="周报口径", caliber_version="v1", metrics=METRICS),
        node("write", "report", instructions="为 {{ input.week }} 写周报", **report),
        node("out", "output", fields=[{"name": "周报", "value": "{{ nodes.write.text }}"}]),
    )


def script(monkeypatch, *replies) -> list[list]:
    """写作模型依次回这几段，用完了一直回最后一段。返回每次调用收到的消息。"""
    seen: list[list] = []

    def decide(model, messages):
        seen.append(list(messages))
        return AIMessage(content=replies[min(len(seen), len(replies)) - 1])

    monkeypatch.setattr(MockChatModel, "_decide", decide)
    return seen


async def finish(graph, *, run_class="exploratory", options=None) -> Run:
    run = await run_manager.start(graph=graph, input_payload={}, run_class=run_class, options=options)
    for _ in range(600):
        await asyncio.sleep(0.05)
        async with SessionLocal() as session:
            row = await session.get(Run, run.id)
        # 状态先提交、封存随后才写：终态要等 manifest_seq 落下来再看
        if row.status in ("succeeded", "failed") and row.manifest_seq is not None:
            await run_manager.wait_idle(run.id)
            return row
    raise AssertionError(f"运行 {run.id} 没跑完：{row.status}")


async def events(run_id: str, etype: str, node_id: str | None = None) -> list[RunEvent]:
    async with SessionLocal() as session:
        q = select(RunEvent).where(RunEvent.run_id == run_id, RunEvent.type == etype)
        if node_id:
            q = q.where(RunEvent.node_id == node_id)
        return list((await session.execute(q.order_by(RunEvent.seq))).scalars())


async def logs(run_id: str, code: str) -> list[dict]:
    return [e.data for e in await events(run_id, "log") if e.data.get("code") == code]


async def report_of(run_id: str) -> tuple[dict, dict]:
    """(report.checked 的载荷, 取回的文档)。"""
    [checked] = await events(run_id, "report.checked", "write")
    return checked.data, artifact_store.load(checked.data["doc_artifact"])


async def output_of(run_id: str, node_id: str) -> dict:
    finished = await events(run_id, "node.finished", node_id)
    return artifact_store.load(finished[-1].data["artifact"])


def fenced(text: str, info: str = "markdown", fence: str = "```") -> str:
    return f"{fence}{info}\n{text}\n{fence}"


@pytest.mark.parametrize("fence, info", [("```", "markdown"), ("```", ""), ("~~~", "md")])
async def test_the_node_checks_the_body_inside_the_fence(monkeypatch, fence, info):
    """裸数字照样标出、引用照常渲染；文档里没有代码块，source 是去掉围栏之后的正文。"""
    script(monkeypatch, fenced(BARE, info, fence))
    row = await finish(weekly(max_repairs=0))
    assert row.status == "succeeded", row.error
    checked, doc = await report_of(row.id)
    assert [b["type"] for b in doc["blocks"]] == ["heading", "paragraph"]
    assert doc["stats"]["claims"] == 1 and doc["stats"]["headings"] == 1
    assert doc["source"] == BARE
    assert doc["markdown"] == "## 本周概览\n\n本周销售额 45678 元，订单 1,234单，较上周有所增长。"
    assert "```" not in doc["markdown"] and "~~~" not in doc["markdown"]
    bare = [(s["text"], s["state"]) for s in iter_segments(doc) if s.get("issue") == "uncited_number"]
    assert bare == [("45678", "none")], "去掉的只是围栏：自己写的数字照样标成没有出处"
    cited = [(s["text"], s["cite"]["locator"].get("metric")) for s in iter_segments(doc) if s["kind"] == "number"
             and s.get("cite")]
    assert cited == [("1,234单", "orders")]
    assert [v["code"] for v in checked["violations"]] == ["uncited_number"] and checked["ok"] is False
    assert row.output["周报"] == doc["markdown"], "交给下游的是渲染后的正文，不带围栏"


async def test_rewrites_are_unwrapped_too(monkeypatch):
    """第一稿带裸数字、重写稿干净，两稿都包在代码块里：重写一次就通过，封存的是去掉围栏的重写稿。"""
    seen = script(monkeypatch, fenced(BARE), fenced(CLEAN))
    row = await finish(weekly(on_violation="fail"), run_class="formal")
    assert row.status == "succeeded", row.error
    assert len(seen) == 2, "只为裸数字重写一次，围栏本身不算违规"
    repair = seen[1][-1]
    assert isinstance(repair, HumanMessage) and "45678" in str(repair.content)
    [warn] = await logs(row.id, "report_repair")
    assert "1 处" in warn["message"] and "45678" in warn["message"]
    checked, doc = await report_of(row.id)
    assert checked["repairs"] == 1 and checked["ok"] is True and doc["violations"] == []
    assert doc["source"] == CLEAN and doc["markdown"] == RENDERED
    assert [b["type"] for b in doc["blocks"]] == ["heading", "paragraph"] and doc["stats"]["claims"] == 1


@pytest.mark.parametrize("raw", [
    pytest.param("```sql\nSELECT region, SUM(amount) AS gmv FROM orders GROUP BY region\n```", id="sql"),
    pytest.param(f"以下是本周报告：\n\n{fenced(CLEAN)}", id="preamble"),
    pytest.param(f"{fenced('## 上半')}\n\n{fenced('## 下半')}", id="two-blocks"),
])
async def test_the_node_keeps_other_code_blocks_as_written(monkeypatch, raw):
    script(monkeypatch, raw)
    row = await finish(weekly(max_repairs=0))
    assert row.status == "succeeded", row.error
    _, doc = await report_of(row.id)
    assert doc["source"] == raw
    assert "code" in [b["type"] for b in doc["blocks"]]


# --------------------------------------------------------------------------
# 端到端：裁判和反方审稿有句子可审
# --------------------------------------------------------------------------

PRICED = "claude-sonnet-5"
QUERY = "SELECT region AS 区域, SUM(amount) AS 销售额 FROM sales GROUP BY region ORDER BY 销售额 DESC"
NET = ("SELECT region AS 区域, SUM(amount) AS 净销售额 FROM sales WHERE refunded = 0 GROUP BY region "
       "ORDER BY 净销售额 DESC")
TEXT = "华东销售额 [[v:Q1.r0.销售额]]，居各区域之首。[[see:Q1]]华南销售额超过华北。[[see:Q1]]"
EAST, SOUTH = "华东销售额 1,500，居各区域之首。", "华南销售额超过华北。"


@pytest.fixture
async def shop(tmp_path):
    """只读的手工源：华东两笔（其中一笔 300 已退款）、华南一笔 900、华北一笔 400。返回源名。"""
    path = tmp_path / f"{uuid.uuid4().hex[:8]}.db"
    db = sqlite3.connect(path)
    db.executescript(
        "CREATE TABLE sales (id INTEGER PRIMARY KEY, region TEXT, amount INTEGER, refunded INTEGER);"
        "INSERT INTO sales VALUES (1, '华东', 1200, 0), (2, '华东', 300, 1), (3, '华南', 900, 0), (4, '华北', 400, 0);")
    db.commit()
    db.close()
    name = f"fenced_{uuid.uuid4().hex[:8]}"
    async with SessionLocal() as session:
        row = DataSource(name=name, kind="sqlite", database=str(path), readonly=True, enabled=True, options={})
        session.add(row)
        await session.flush()
        row.schema_cache = await introspect.introspect(row)
        await session.commit()
        source_id = row.id
    yield name
    await drop_source(source_id)


class Models:
    """写作者、裁判、反查 agent 三份剧本。写作者回 writer；裁判把收到的每句都判「证据支持」；反查 agent 每句先查一次
    扣除退款后的销售额，再交「经受住」。judges 记下裁判收到的请求，reviewed 记下反查 agent 审过的句子。"""

    def __init__(self, monkeypatch, source: str, writer: str):
        self.judges: list[str] = []
        self.reviewed: list[str] = []

        def decide(model, messages):
            system = str(messages[0].content)
            if JUDGE_ROLE in system:
                request = str(messages[-1].content)
                self.judges.append(request)
                items = [{"unit": uid, "verdict": "supported", "rationale": "与证据一致。", "used": []}
                         for uid, _ in re.findall(r"^\[(u\d+)\]([^\n]*)$", request, re.M)]
                return AIMessage(content=json.dumps({"items": items}, ensure_ascii=False))
            if challenge_mod.CHALLENGE_SYSTEM in system:
                claim = next((c for c in (EAST, SOUTH) if c in str(messages[1].content)), "")
                if model.response_format:
                    self.reviewed.append(claim)
                    return AIMessage(content=json.dumps({"verdict": "stands", "rationale": "没有找到反例。"},
                                                        ensure_ascii=False))
                if not any(isinstance(m, ToolMessage) for m in messages):
                    call = {"name": f"db_query__{source}", "args": {"sql": NET}, "id": f"q-{len(self.reviewed)}"}
                    return AIMessage(content="查一下。", tool_calls=[call])
                return AIMessage(content="查完了。")
            if report_mod.ROLE in system:
                return AIMessage(content=writer)
            raise AssertionError(f"没想到的模型调用：{system[:60]}")

        monkeypatch.setattr(MockChatModel, "_decide", decide)

        async def priced(session, spec):
            return MockChatModel(model_name=PRICED), PRICED

        monkeypatch.setattr(judge_mod, "get_chat_model", priced)
        monkeypatch.setattr(verify_agent, "get_chat_model", priced)


def audited(source: str) -> dict:
    """取数 → 报告（逐句裁判）→ 反方审稿 → 成果（引用模式的出具契约，按报告文档复核）。"""
    return chain(
        node("start", "input"),
        node("fetch", "tool", tool=f"db_query__{source}", args={"sql": QUERY}),
        node("write", "report", "写周报", instructions="写周报", claims="judge"),
        node("chal", "challenge", "审稿", report="write"),
        node("out", "output", fields=[{"name": "周报", "value": "{{ nodes.write.text }}"}],
             contract={"report_from": "write"}),
    )


async def test_the_judge_and_the_challenge_get_sentences_to_review(monkeypatch, shop):
    models = Models(monkeypatch, shop, fenced(TEXT))
    row = await finish(audited(shop), run_class="formal")
    assert row.status == "succeeded", row.error

    checked, doc = await report_of(row.id)
    assert doc["source"] == TEXT and doc["markdown"] == EAST + SOUTH
    assert doc["stats"]["claims"] == 2
    # 单元格引用照常解析：渲染成快照里的值，带着出处
    [cell] = [s for s in iter_segments(doc) if s.get("cite")]
    assert (cell["text"], cell["state"], cell["cite"]["kind"]) == ("1,500", "deterministic", "cell")

    # 裁判：两句都送去判了，判定写进文档
    [request] = models.judges
    assert EAST in request and SOUTH in request
    md = doc["markdown"]
    judged = {md[u["span"][0]:u["span"][1]]: (u.get("verdict") or {}).get("status") for _, u in iter_units(doc)}
    assert judged == {EAST: "supported", SOUTH: "supported"}
    assert checked["judge"]["counts"]["supported"] == 2 and checked["judge"]["complete"] is True

    # 反方审稿：两句都审了，不是「没有可审的结论句」
    assert await logs(row.id, "challenge_skipped") == []
    assert sorted(models.reviewed) == sorted([EAST, SOUTH])
    out = await output_of(row.id, "chal")
    assert out["counts"] == {"stands": 2, "weakened": 0, "refuted": 0, "inconclusive": 0}
    assert sorted(i["text"] for i in out["items"]) == sorted([EAST, SOUTH])

    # 出口契约复核取回的是同一份文档：数字都有出处、两句都判过、没有被推翻的，完整出具
    issuance = row.output["_issuance"]
    assert issuance["tier"] == "formal" and issuance["gaps"] == [], issuance
    assert (await verify_manifest(row.id))["ok"] is True
