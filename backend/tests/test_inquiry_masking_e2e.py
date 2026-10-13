"""审批质询的遮罩收口，端到端（engine/inquiry.py 经核查 agent 的工具边界）：

- 分档写在子查询、WITH 里、外层按档位分组：档位列交给模型、存下、渲染进回答都照常，标签不当遮罩值，SQL 照原样封存；
  以前回答成了「已遮罩有 3 笔，已遮罩有 2 笔。」
- 交给质询 agent 的证据摘录先遮：报告取数把遮罩列改了名字选出来（buyer_phone AS 手机号），摘录里不给这一列；别的列、
  SQL 里写着的遮罩值（换了写法的号码也算）换成「已遮罩」。以前摘录只按列名遮，号码原样交给模型

模型一律是 MockChatModel 剧本，数据源是现造的 SQLite 文件，区域、金额、手机号都是虚构的。
"""
from __future__ import annotations

import pytest

from app.core import artifact_store
from tests.test_inquiry_api import (  # noqa: F401 - engine_up 是自动用上的夹具，client、big_shop、masked_shop 是夹具
    AMOUNTS,
    Models,
    Play,
    ask,
    big_shop,
    client,
    engine_up,
    events,
    fetching,
    masked_shop,
    pause,
    pending,
    report_doc,
    unit_of,
)

BAND = "CASE WHEN amount >= 20000 THEN '大单' ELSE '小单' END"


@pytest.mark.parametrize("sql", [
    pytest.param(f"SELECT 档位, COUNT(*) AS 笔数 FROM (SELECT *, {BAND} AS 档位 FROM sales) GROUP BY 档位 ORDER BY 档位",
                 id="子查询里 * 加分档"),
    pytest.param(f"WITH b AS (SELECT {BAND} AS 档位 FROM sales) SELECT 档位, COUNT(*) AS 笔数 FROM b GROUP BY 档位 "
                 "ORDER BY 档位", id="WITH 里只有分档"),
])
async def test_a_band_grouped_outside_its_derived_source_reads_as_written(monkeypatch, big_shop, client, sql):
    """金额设了遮罩，问两万元上下各有几笔：分档写在派生来源里、外层按档位分组。模型看得到档位、回答里引用的档位和它写的
    「大单」「小单」都照原样，SQL 照原样封存，质询工件里档位列照常显示。"""
    said = "[[v:Q1.r0.档位]]有 [[v:Q1.r0.笔数]] 笔，[[v:Q1.r1.档位]]有 [[v:Q1.r1.笔数]] 笔，大单少于小单。[[see:Q1]]"
    models = Models(monkeypatch, big_shop, writer="华东首单金额 [[v:Q1.r0.金额]]。[[see:Q1]]",
                    plays={"两万元上下": Play([sql], said)})
    paused = await pause(big_shop, graph=fetching(big_shop, AMOUNTS))
    [approval] = await pending(paused.id)
    r = await ask(client, paused.id, report="write", unit=None, question="两万元上下各有几笔？",
                  approval_id=approval.id)
    assert r.status_code == 200, r.text
    item = r.json()["inquiry"]
    assert (item["answer"]["ok"], item["answer"]["text"]) == (True, "大单有 3 笔，小单有 2 笔，大单少于小单。")
    [query] = item["queries"]
    assert query["sql"] == sql, "SQL 里的标签不当遮罩值换"
    [event] = await events(paused.id, "approval.inquiry")
    assert event.data["queries"][0]["sql"] == sql
    snapshot = artifact_store.load(query["artifact"])
    assert snapshot["rows"] == [["大单", 3], ["小单", 2]] and snapshot["masked_at_query"] == []
    seen = [str(m.content) for messages in models.finals for m in messages]
    assert any('r0：档位="大单"，笔数=3' in text for text in seen), "模型看得到档位"
    got = await client.get(f"/api/runs/{paused.id}/inquiries/artifacts/{query['artifact']}")
    assert got.status_code == 200, got.text
    assert (got.json()["content"]["rows"], got.json()["masked"]) == ([["大单", 3], ["小单", 2]], [])


#: 报告取数：手机号改了名字选出来，按号码排序时把号码写进了 SQL
RENAMED = "SELECT buyer_phone AS 手机号, amount AS 金额 FROM sales ORDER BY buyer_phone = '13900000001' DESC, amount DESC"


async def test_the_excerpt_in_the_task_is_masked_before_the_agent_sees_it(monkeypatch, masked_shop, client):
    """针对「金额最高的一笔 1,200。」发起质询：任务里这句挂的证据摘录不给改了名字的遮罩列（手机号），SQL 里写着的号码
    换成「已遮罩」，金额照常。以前「列：金额 | 手机号」「r0：1,200 | 13900000001」…，四个号码都在。"""
    models = Models(monkeypatch, masked_shop, writer="金额最高的一笔 [[v:Q1.r0.金额]]。[[see:Q1]]",
                    plays={"这笔是谁买的": Play(["SELECT COUNT(*) AS 笔数 FROM sales"], "共 [[v:Q1.r0.笔数]] 笔。[[see:Q1]]")})
    paused = await pause(masked_shop, graph=fetching(masked_shop, RENAMED))
    [approval] = await pending(paused.id)
    doc = await report_doc(paused.id)
    r = await ask(client, paused.id, report="write", unit=unit_of(doc, "金额最高的一笔 1,200。"),
                  question="这笔是谁买的？", approval_id=approval.id)
    assert r.status_code == 200, r.text
    [task] = models.tasks
    excerpt = task[task.index("【原报告 Q1】"):]
    assert "1390000000" not in task, excerpt
    columns = next(line for line in excerpt.splitlines() if line.startswith("列："))
    assert "手机号" not in columns and "金额" in columns
    assert "buyer_phone = '已遮罩'" in excerpt and "（有的列按数据源的设置遮罩了，没有给出）" in excerpt
    assert r.json()["inquiry"]["answer"]["ok"] is True


# --------------------------------------------------------------------------
# 摘录分段遮：阈值、没设遮罩的数值格、被问那句里的值照原样
# --------------------------------------------------------------------------


#: 报告按金额阈值取数（12000 正好是某一笔被遮的金额），访问次数没设遮罩
THRESHOLD = ("SELECT id AS 编号, region AS 区域, amount AS 金额, visits AS 访问 FROM sales WHERE amount >= 12000 "
             "ORDER BY id")


async def test_the_excerpt_keeps_thresholds_and_unmasked_numbers(monkeypatch, big_shop, client):
    """针对「华南首单访问 30,000 次。」发起质询：摘录里 SQL 的阈值（12000，恰好是一笔被遮的金额）照原样，访问次数（没设
    遮罩，20000、30000 恰好等于别的订单的金额）照原样，金额列不给。以前对渲染好的整段按值换：「amount >= 已遮罩」
    「r0：已遮罩 | 2 | 华东」，被问的那一格也成了「已遮罩」，任务开头的原句却写着 30,000。"""
    models = Models(monkeypatch, big_shop, writer="华南首单访问 [[v:Q1.r1.访问]] 次。[[see:Q1]]",
                    plays={"访问为什么这么高": Play(["SELECT COUNT(*) AS 笔数 FROM sales"], "共 [[v:Q1.r0.笔数]] 笔。")})
    paused = await pause(big_shop, graph=fetching(big_shop, THRESHOLD))
    [approval] = await pending(paused.id)
    doc = await report_doc(paused.id)
    r = await ask(client, paused.id, report="write", unit=unit_of(doc, "华南首单访问 30,000 次。"),
                  question="访问为什么这么高？", approval_id=approval.id)
    assert r.status_code == 200, r.text
    [task] = models.tasks
    excerpt = task[task.index("【原报告 Q1】"):]
    assert f"SQL：{THRESHOLD}" in excerpt, excerpt
    assert "列：访问 | 编号 | 区域" in excerpt and "r0：20,000 | 2 | 华东" in excerpt and "r1：30,000 | 3 | 华南" in excerpt


#: 买家姓名设了遮罩；推荐人没设遮罩，写着的恰好是另一位买家的名字
REFERRALS = ("CREATE TABLE sales (id INTEGER PRIMARY KEY, region TEXT, buyer_name TEXT, referrer TEXT, amount INTEGER);"
             "INSERT INTO sales VALUES (1, '华东', '张伟', '李娜', 1200), (2, '华南', '李娜', '张伟', 900),"
             " (3, '华北', '王芳', '刘洋', 400);")


@pytest.fixture
async def referral_shop(tmp_path):
    from app.data.engine import engines
    from tests.test_inquiry_api import _source

    name, source_id = await _source(tmp_path, options={"mask_columns": "buyer_name"}, script=REFERRALS)
    yield name
    await engines.invalidate(source_id)


async def test_values_in_the_asked_sentence_stay_in_the_excerpt_and_the_answer(monkeypatch, referral_shop, client):
    """被问的那句写着「李娜」（推荐人这一格，恰好也是另一笔的买家）：它就在任务开头、摆在审批人眼前，摘录里这一格、回答里
    复述的都照原样（同反方审稿的 _spared）；别的买家名照旧换。以前质询不把这句原文算作已经摆在眼前的，这一格成了
    「已遮罩」，和任务开头写着的原句对不上。"""
    report = "SELECT buyer_name AS 买家, referrer AS 推荐人, amount AS 金额 FROM sales ORDER BY id"
    said = "推荐人是李娜，第二单的推荐人是 [[v:Q1.r1.推荐人]]。[[see:Q1]]"
    models = Models(monkeypatch, referral_shop, writer="首单由 [[v:Q1.r0.推荐人]] 推荐。[[see:Q1]]",
                    plays={"谁推荐的": Play(["SELECT id, referrer AS 推荐人 FROM sales ORDER BY id"], said)})
    paused = await pause(referral_shop, graph=fetching(referral_shop, report))
    [approval] = await pending(paused.id)
    doc = await report_doc(paused.id)
    r = await ask(client, paused.id, report="write", unit=unit_of(doc, "首单由 李娜 推荐。"),
                  question="谁推荐的？", approval_id=approval.id)
    assert r.status_code == 200, r.text
    [task] = models.tasks
    excerpt = task[task.index("【原报告 Q1】"):]
    assert "列：推荐人 | 金额" in excerpt and "r0：李娜 | 1,200" in excerpt and "r1：已遮罩 | 900" in excerpt, excerpt
    item = r.json()["inquiry"]
    assert item["answer"]["text"] == "推荐人是李娜，第二单的推荐人是 已遮罩。"


async def test_literals_compared_with_a_masked_column_are_masked_in_the_excerpt(monkeypatch, masked_shop, client):
    """报告没选出号码，只在条件里排除了两个（一个换了写法）：报告快照里收不到这两个值，摘录的 SQL 照样换掉。"""
    report = ("SELECT region AS 区域, COUNT(*) AS 笔数 FROM sales WHERE buyer_phone NOT IN ('13900000002', "
              "'139-0000-0003') GROUP BY region ORDER BY 笔数 DESC")
    models = Models(monkeypatch, masked_shop, writer="笔数最多的区域有 [[v:Q1.r0.笔数]] 笔。[[see:Q1]]",
                    plays={"按什么排除": Play([], "查不到判断所需的数据。")})
    paused = await pause(masked_shop, graph=fetching(masked_shop, report))
    [approval] = await pending(paused.id)
    doc = await report_doc(paused.id)
    r = await ask(client, paused.id, report="write", unit=unit_of(doc, "笔数最多的区域有 1 笔。"),
                  question="按什么排除的？", approval_id=approval.id)
    assert r.status_code == 200, r.text
    [task] = models.tasks
    assert "13900000002" not in task and "0000-0003" not in task, task
    assert "buyer_phone NOT IN ('已遮罩', '已遮罩')" in task
