"""反方审稿的遮罩收口（engine/nodes/challenge.py）：

- 交给反方 agent 的证据摘录先遮：报告取数把遮罩列改了名字选出来（buyer_phone AS 手机号），摘录里不给这一列（按 SQL
  认，和工具边界同一套规则）；别的列（备注里换了写法的号码）、SQL 里写着的遮罩值换成「已遮罩」。以前摘录只按列名遮，
  四个号码原样交给模型，模型照抄进理由
- 理由正文按值遮（报告快照里的值、agent 查询时遮掉的值、换了写法的号码），端到端；组装之后还认得出遮罩值的（第二道
  防线漏了），整句降为无法判断，理由不记，同审批质询的最后一道关

模型一律是 MockChatModel 剧本，数据源是现造的 SQLite 文件，区域、金额、手机号都是虚构的。
"""
from __future__ import annotations

import json
import sqlite3
import uuid

import pytest

from app.core import artifact_store
from app.data import introspect
from app.data.engine import engines
from app.db.base import SessionLocal
from app.db.models import DataSource
from app.engine import masking
from app.engine.nodes import challenge as challenge_mod
from tests.test_challenge_node import Models, Play, engine_up, finish, graph, output_of  # noqa: F401 - engine_up 是自动用上的夹具

#: 四笔订单，买家手机号设了遮罩；备注没设遮罩，其中一条写着换了写法的号码
SCRIPT = ("CREATE TABLE sales (id INTEGER PRIMARY KEY, region TEXT, buyer_phone TEXT, amount INTEGER, refunded INTEGER,"
          " note TEXT);"
          "INSERT INTO sales VALUES (1, '华东', '13900000001', 1200, 0, '客户 139-0000-0002 代下单'),"
          " (2, '华东', '13900000002', 300, 1, ''), (3, '华南', '13900000003', 900, 0, '例行回访'),"
          " (4, '华北', '13900000004', 400, 0, '');")
SECRETS = ("1390000000", "0000-0002", "139 0000")
MASKED = "已遮罩"


@pytest.fixture
async def masked_shop(tmp_path):
    path = tmp_path / f"{uuid.uuid4().hex[:8]}.db"
    db = sqlite3.connect(path)
    db.executescript(SCRIPT)
    db.commit()
    db.close()
    name = f"shop_{uuid.uuid4().hex[:8]}"
    async with SessionLocal() as session:
        row = DataSource(name=name, kind="sqlite", database=str(path), readonly=True, enabled=True,
                         options={"mask_columns": ["buyer_phone"]})
        session.add(row)
        await session.flush()
        row.schema_cache = await introspect.introspect(row)
        await session.commit()
        source_id = row.id
    yield name
    await engines.invalidate(source_id)


def _clean(text: str) -> bool:
    return not any(s in text for s in SECRETS)


#: 报告取数：手机号改了名字选出来，备注照选，按号码排序时把号码写进了 SQL
REPORT_SQL = ("SELECT buyer_phone AS 手机号, note AS 备注, amount AS 金额 FROM sales "
              "ORDER BY buyer_phone = '13900000001' DESC, amount DESC")
WRITER = ("金额最高的一笔 [[v:Q1.r0.金额]]。[[see:Q1]]",)
CLAIM = "金额最高的一笔"


async def test_the_excerpt_in_the_task_is_masked_before_the_agent_sees_it(monkeypatch, masked_shop):
    """反方 agent 收到的任务里：改了名字的遮罩列（手机号）不给，备注里换了写法的号码、SQL 里写着的号码换成「已遮罩」，
    金额照常。以前「列：金额 | 手机号」「r0：1,200 | 13900000001」…，四个号码都在。"""
    name = masked_shop
    plays = {CLAIM: Play(["SELECT COUNT(*) AS 笔数 FROM sales"], "stands", "共 [[v:Q1.r0.笔数]] 笔。[[see:Q1]]")}
    models = Models(monkeypatch, name, writer=WRITER, plays=plays)
    row = await finish(graph(name, sql=REPORT_SQL), run_class="exploratory")
    assert row.status == "succeeded", row.error
    task = models.tasks()[CLAIM]
    assert "金额最高的一笔 1,200。" in task and "【原报告 Q1】" in task
    excerpt = task[task.index("【原报告 Q1】"):]
    assert _clean(excerpt), excerpt
    columns = next(line for line in excerpt.splitlines() if line.startswith("列："))
    assert "手机号" not in columns and "金额" in columns and "备注" in columns
    assert "客户 已遮罩 代下单" in excerpt and "buyer_phone = '已遮罩'" in excerpt
    assert "（有的列按数据源的设置遮罩了，没有给出）" in excerpt


async def test_masked_values_the_agent_found_itself_are_masked_in_the_rationale(monkeypatch, masked_shop):
    """端到端：反方 agent 自己查出的号码（查询时在工具边界遮掉、只在内存里）被它换着写法写进理由：审稿记录里的理由正文、
    理由文档都换成「已遮罩」。报告没有选出号码，值只来自 agent 查询时遮掉的那一份。"""
    name = masked_shop
    top = "SELECT buyer_phone AS 电话, amount AS 金额 FROM sales WHERE region = '华南'"
    said = "华南那笔来自 +86 139 0000 0003，金额 [[v:Q1.r0.金额]]。[[see:Q1]]"
    plays = {CLAIM: Play([top], "stands", said)}
    Models(monkeypatch, name, writer=WRITER, plays=plays)
    row = await finish(graph(name, sql="SELECT region AS 区域, amount AS 金额 FROM sales ORDER BY amount DESC"),
                       run_class="exploratory")
    assert row.status == "succeeded", row.error
    record = artifact_store.load((await output_of(row.id, "chal"))["challenge_artifact"])
    [claim] = [c for c in record["claims"] if CLAIM in c["text"]]
    assert claim["verdict"] == "stands" and claim["rationale"] == "华南那笔来自 已遮罩，金额 900。"
    assert claim["rationale_doc"]["markdown"] == claim["rationale"]
    assert "0000 0003" not in json.dumps(record, ensure_ascii=False)


async def test_a_rationale_still_showing_a_masked_value_is_not_kept(monkeypatch, masked_shop):
    """最后一道关：按值遮之后组装出来的理由里还认得出遮罩值（这里让按值换失手），这句降为无法判断，理由不记、不存，
    原值不进审稿记录。同审批质询的回答。"""
    name = masked_shop
    monkeypatch.setattr(masking, "_scrubbed_source", lambda text, secrets: text)
    said = "号码 13900000001 的那笔属实，金额 [[v:Q1.r0.笔数]]。[[see:Q1]]"
    plays = {CLAIM: Play(["SELECT COUNT(*) AS 笔数 FROM sales"], "refuted", said)}
    Models(monkeypatch, name, writer=WRITER, plays=plays)
    row = await finish(graph(name, sql=REPORT_SQL), run_class="exploratory")
    assert row.status == "succeeded", row.error
    record = artifact_store.load((await output_of(row.id, "chal"))["challenge_artifact"])
    [claim] = [c for c in record["claims"] if CLAIM in c["text"]]
    assert claim["verdict"] == "inconclusive" and claim["rationale_doc"] is None
    assert claim["rationale"] == challenge_mod.LEAKED
    assert _clean(json.dumps(record, ensure_ascii=False))
    out = await output_of(row.id, "chal")
    assert out["counts"]["refuted"] == 0, "遮不住的理由不能让出具降档"


# --------------------------------------------------------------------------
# 摘录分段遮：阈值、没设遮罩的数值格照原样，和遮罩列做等值比较的字面量都换
# --------------------------------------------------------------------------


#: 金额、客户名设了遮罩，金额都是五位数；访问次数没设遮罩，第二、三、四单正好等于别的订单的金额
BIG = ("CREATE TABLE sales (id INTEGER PRIMARY KEY, region TEXT, buyer_name TEXT, amount INTEGER, visits INTEGER);"
       "INSERT INTO sales VALUES (1, '华东', '张伟', 10000, 25000), (2, '华东', '李娜', 12000, 20000),"
       " (3, '华南', '王芳', 20000, 30000), (4, '华北', '刘洋', 30000, 50000), (5, '华北', '赵敏', 50000, 9000);")


@pytest.fixture
async def big_shop(tmp_path):
    path = tmp_path / f"{uuid.uuid4().hex[:8]}.db"
    db = sqlite3.connect(path)
    db.executescript(BIG)
    db.commit()
    db.close()
    name = f"shop_{uuid.uuid4().hex[:8]}"
    async with SessionLocal() as session:
        row = DataSource(name=name, kind="sqlite", database=str(path), readonly=True, enabled=True,
                         options={"mask_columns": ["amount", "buyer_name"]})
        session.add(row)
        await session.flush()
        row.schema_cache = await introspect.introspect(row)
        await session.commit()
        source_id = row.id
    yield name
    await engines.invalidate(source_id)


def _excerpt_of(task: str) -> str:
    return task[task.index("【原报告 Q1】"):]


async def test_thresholds_and_unmasked_numbers_in_the_excerpt_are_kept(monkeypatch, big_shop):
    """报告按金额阈值取数（amount >= 20000，正好等于某一笔被遮的金额），访问次数没设遮罩、第二行正好等于另一笔金额：
    摘录里 SQL 的阈值、访问次数都照原样，金额列不给。以前对渲染好的整段按值换，成了「amount >= 已遮罩」「r1：4 | 华北 |
    已遮罩」，agent 看不到报告按什么取数，没设遮罩的数也被改写。"""
    report = "SELECT id, region, amount, visits FROM sales WHERE amount >= 20000 ORDER BY id"
    claim = "大额订单里访问最多的一笔在华南"
    plays = {claim: Play(["SELECT COUNT(*) AS 笔数 FROM sales"], "stands", "共 [[v:Q1.r0.笔数]] 笔。[[see:Q1]]")}
    models = Models(monkeypatch, big_shop, writer=(f"{claim}，访问 [[v:Q1.r0.visits]] 次。[[see:Q1]]",),
                    plays=plays)
    row = await finish(graph(big_shop, sql=report), run_class="exploratory")
    assert row.status == "succeeded", row.error
    excerpt = _excerpt_of(models.tasks()[claim])
    assert f"SQL：{report}" in excerpt, excerpt
    assert "列：visits | id | region" in excerpt
    assert "r1：50,000 | 4 | 华北" in excerpt and MASKED not in excerpt.split("列：", 1)[1].split("（", 1)[0]


async def test_literals_compared_with_a_masked_column_are_masked_even_when_never_collected(monkeypatch, masked_shop):
    """报告没选出号码，只在条件里排除了两个（一个换了写法）：报告快照里收不到这两个值，摘录的 SQL 照样换掉——和遮罩列
    做等值、IN 比较的字面量不论认不认得出都换。以前按值换只认快照里收到的，号码原样交给 agent。"""
    report = ("SELECT region AS 区域, COUNT(*) AS 笔数 FROM sales WHERE buyer_phone NOT IN ('13900000002', "
              "'139-0000-0003') GROUP BY region ORDER BY 笔数 DESC")
    claim = "笔数最多的区域"
    plays = {claim: Play(["SELECT COUNT(*) AS 笔数 FROM sales"], "stands", "共 [[v:Q1.r0.笔数]] 笔。[[see:Q1]]")}
    models = Models(monkeypatch, masked_shop, writer=(f"{claim}有 [[v:Q1.r0.笔数]] 笔。[[see:Q1]]",), plays=plays)
    row = await finish(graph(masked_shop, sql=report), run_class="exploratory")
    assert row.status == "succeeded", row.error
    task = models.tasks()[claim]
    assert _clean(task), task
    assert f"buyer_phone NOT IN ('{MASKED}', '{MASKED}')" in _excerpt_of(task)


# --------------------------------------------------------------------------
# 最后一道关只看正文和原文：agent 起的列名、照原样留着的阈值不算露了遮罩值
# --------------------------------------------------------------------------


AMOUNTS = "SELECT region AS 区域, amount AS 金额 FROM sales ORDER BY id"
FIRST = "华东首单金额"


@pytest.mark.parametrize("verdict", ["stands", "refuted"])
async def test_a_threshold_in_an_alias_does_not_void_the_verdict(monkeypatch, big_shop, verdict):
    """反方 agent 把阈值写进别名（不低于20000的笔数），20000 恰好是某一笔被遮的金额：结论照常采信，理由照常记。以前最后
    一道关把目录里的列名当成露了遮罩值，整句改判无法判断，「被推翻」也不再让出具降档，日志里多一条误报。"""
    top = "SELECT COUNT(*) AS 不低于20000的笔数 FROM sales WHERE amount >= 20000"
    said = "金额不低于两万的订单有 [[v:Q1.r0.不低于20000的笔数]] 笔，与结论" + ("不矛盾" if verdict == "stands" else "矛盾") \
        + "。[[see:Q1]]"
    Models(monkeypatch, big_shop, writer=(f"{FIRST} [[v:Q1.r0.金额]]。[[see:Q1]]",),
           plays={FIRST: Play([top], verdict, said)})
    row = await finish(graph(big_shop, sql=AMOUNTS), run_class="exploratory")
    assert row.status == "succeeded", row.error
    out = await output_of(row.id, "chal")
    record = artifact_store.load(out["challenge_artifact"])
    [claim] = [c for c in record["claims"] if FIRST in c["text"]]
    assert claim["verdict"] == verdict, claim["rationale"]
    assert claim["rationale"].startswith("金额不低于两万的订单有 3 笔")
    assert out["counts"][verdict] == 1


async def test_a_threshold_the_agent_wrote_in_its_sql_stays_in_the_rationale(monkeypatch, big_shop):
    """理由正文复述 agent 自己 SQL 里照原样留着的阈值（「不低于 20000 元」，恰好等于某一笔被遮的金额）：照原样，同审批质询
    的回答；它随 SQL 存进审稿记录、摆在读者眼前。以前换成「已遮罩」，理由和 SQL 对不上。"""
    top = "SELECT COUNT(*) AS 笔数 FROM sales WHERE amount >= 20000"
    said = "金额不低于 20000 元的订单有 [[v:Q1.r0.笔数]] 笔，与结论不矛盾。[[see:Q1]]"
    Models(monkeypatch, big_shop, writer=(f"{FIRST} [[v:Q1.r0.金额]]。[[see:Q1]]",),
           plays={FIRST: Play([top], "stands", said)})
    row = await finish(graph(big_shop, sql=AMOUNTS), run_class="exploratory")
    assert row.status == "succeeded", row.error
    record = artifact_store.load((await output_of(row.id, "chal"))["challenge_artifact"])
    [claim] = [c for c in record["claims"] if FIRST in c["text"]]
    assert (claim["verdict"], claim["rationale"]) == ("stands", "金额不低于 20000 元的订单有 3 笔，与结论不矛盾。")


async def test_an_alias_naming_a_masked_value_is_renamed_like_the_snapshot(monkeypatch, big_shop):
    """agent 把找的那个值写进了别名（金额为20000的笔数，20000 是一笔被遮的金额，不是阈值）：理由文档的目录列名照快照遮好的
    样子换（同质询的 _renamed_entry），引用照样对得上，结论照常采信，存下的理由文档里没有这个值。以前目录原样传进重新
    组装，最后一道关又把目录拿去认，整句改判无法判断。"""
    top = "SELECT COUNT(*) AS 金额为20000的笔数 FROM sales WHERE amount = 20000"
    said = "金额为两万的订单有 [[v:Q1.r0.金额为20000的笔数]] 笔，与结论不矛盾。[[see:Q1]]"
    Models(monkeypatch, big_shop, writer=(f"{FIRST} [[v:Q1.r0.金额]]。[[see:Q1]]",),
           plays={FIRST: Play([top], "stands", said)})
    row = await finish(graph(big_shop, sql=AMOUNTS), run_class="exploratory")
    assert row.status == "succeeded", row.error
    record = artifact_store.load((await output_of(row.id, "chal"))["challenge_artifact"])
    [claim] = [c for c in record["claims"] if FIRST in c["text"]]
    assert (claim["verdict"], claim["rationale"]) == ("stands", "金额为两万的订单有 1 笔，与结论不矛盾。")
    assert "20000" not in json.dumps(claim["rationale_doc"], ensure_ascii=False)
