"""分析题用的虚构零售库（docs/design/analysis-round/00-总方案.md §3.8、D14）。店名、金额都是编的，和任何真实业务无关。

    python -m tests.fixtures.acceptance.retail_trends <输出 .db 路径> [--today YYYY-MM-DD]

生成「今天」之前 26 周的订单（截止昨天），随机数种子固定：同一个「今天」生成的库逐字节一样。和 retail.py（retail_demo）分开：
那边的测试断言了它的比例、地区排序，在里面埋效应会改掉后面全部的随机序列，60 天也不够放回 8 周以上的历史里看。

相对「上一个整周」（上周一到上周日，下面叫「上周」，它前一周叫「上上周」）埋了这些效应，各放在不互相干扰的指标或段上：

1. 销售额（已支付的金额）上周比上上周下降，主要来自华东：华东线下上周的客单价降到七成，订单数不变。
2. 退款率的辛普森：线上、线下各自的退款率上周都比上上周低，整体却升了——上周起三成线下订单转到了线上，线上的退款率本来
   就高（结构变化为主）。
3. 订单数（不含取消）的常规波动：线下每周有 ±5% 的起伏，上周比上上周少一点，在常规波动范围内，不该叫异常。
4. 取消单激增：平常每周二十来单，上周是三倍多，明显超出常规波动。
5. 线上销售额持续上升：线上订单每周多 4%（上周又加上转过来的那三成），最近十几周周周都涨。
6. 线上销售额上周的变化主要来自订单数，客单价没变。
7. 西南只有一家小店，近四周的退款率看着比华东高，样本太少，差异不足以下结论。
8. 本周还没过完（截止昨天）：拿本周和上周比，不可比。今天是周一时本周还没有数据，那一题的期望算不了。

订单数每家店每周按定好的数生成（不逐笔抽随机），金额在很窄的区间里随机，退款单按每周每个渠道定好的比例挑：各效应不靠运气
成立。tests/test_retail_trends_fixture.py 在几个不同的「今天」上用平台的算子，按题集的期望 SQL 逐个核对。

评测的分析题集（tests/fixtures/eval/analysis_suite.json）按数据源名 SOURCE_NAME 对上这个库：生成之后登记成这个名字的只读
数据源，再导入题集（POST /api/evals/suites/import），题目的期望都按这个库算。
"""
from __future__ import annotations

import argparse
import random
import sqlite3
from datetime import date, timedelta
from pathlib import Path

WEEKS = 26
DAYS = WEEKS * 7
SEED = 20261021
#: 分析题集里写的数据源名
SOURCE_NAME = "retail_trends"
ONLINE, OFFLINE = "线上", "线下"

#: (门店 id, 店名, 地区)：西南只有一家小店（第 7 条效应的小样本）
STORES = [
    (1, "青禾一号店", "华东"), (2, "青禾二号店", "华东"), (3, "青禾三号店", "华东"),
    (4, "青禾四号店", "华南"), (5, "青禾五号店", "华南"), (6, "青禾六号店", "华南"),
    (7, "青禾七号店", "华北"), (8, "青禾八号店", "华北"),
    (9, "青禾九号店", "西南"),
]
SMALL_STORE = 9
#: 每家店每天的线下订单数（平常的水平）
OFFLINE_DAILY = {1: 16, 2: 14, 3: 12, 4: 14, 5: 12, 6: 10, 7: 12, 8: 10, 9: 1}
#: 每家店每天的线上订单数（上周的水平；往前每周少 4%）
ONLINE_DAILY = {1: 7, 2: 6, 3: 5, 4: 6, 5: 5, 6: 5, 7: 5, 8: 4, 9: 0.5}
ONLINE_GROWTH = 1.04
#: 线下每周的起伏（±）；上周比上上周再少这么多
OFFLINE_SWING = 0.05
LAST_WEEK_DIP = 0.975
#: 上周起线下订单转到线上的比例
SHIFT = 0.3
#: 金额的区间（元）；华东线下上周的客单价乘这个数
PRICE = (140.0, 180.0)
EAST_DROP = 0.7
#: 各渠道的退款率：上上周及以前、上周起
REFUND_RATE = {ONLINE: (0.10, 0.09), OFFLINE: (0.04, 0.034)}
#: 每周的取消单数和起伏；上周的倍数
CANCELLED_WEEKLY = 24
CANCEL_SWING = 0.15
CANCEL_SPIKE = 3.2

SCHEMA = """
CREATE TABLE stores (id INTEGER PRIMARY KEY, name TEXT NOT NULL, region TEXT NOT NULL);
CREATE TABLE orders (
  id INTEGER PRIMARY KEY,
  store_id INTEGER NOT NULL REFERENCES stores(id),
  order_date DATE NOT NULL,
  channel TEXT NOT NULL,
  amount REAL NOT NULL,
  status TEXT NOT NULL
);
"""


def _spread(count: int, days: list[date]) -> list[date]:
    """一周里的 count 单按天摊开：尽量平均，多出来的几单从这一周的头几天起各加一单。"""
    if not days or count <= 0:
        return []
    base, extra = divmod(count, len(days))
    return [d for i, d in enumerate(days) for _ in range(base + (1 if i < extra else 0))]


def build(path: Path | str, today: date | None = None) -> Path:
    """生成到 path（已存在就覆盖）。today 缺省取运行这一天；订单截止 today 的前一天。"""
    path = Path(path)
    if path.exists():
        path.unlink()
    today = today or date.today()
    rng = random.Random(SEED)
    first, last = today - timedelta(days=DAYS), today - timedelta(days=1)
    monday = today - timedelta(days=today.weekday())
    # 第 j 周：本周 j = 0、上周 j = 1……最早的那一周可能不完整。每周的起伏先抽好（个数和「今天」是星期几无关）
    oldest = (monday - (first - timedelta(days=first.weekday()))).days // 7
    swing = {j: 1 + rng.uniform(-OFFLINE_SWING, OFFLINE_SWING) for j in range(oldest, -1, -1)}
    cancel = {j: 1 + rng.uniform(-CANCEL_SWING, CANCEL_SWING) for j in range(oldest, -1, -1)}
    swing[1] = swing[2] * LAST_WEEK_DIP
    cancel[1] = CANCEL_SPIKE

    def days_of(j: int) -> list[date]:
        start = monday - timedelta(days=7 * j)
        return [d for d in (start + timedelta(days=k) for k in range(7)) if first <= d <= last]

    def price(store: int, channel: str, j: int) -> float:
        amount = rng.uniform(*PRICE)
        if j == 1 and channel == OFFLINE and _REGION[store] == "华东":
            amount *= EAST_DROP
        return round(amount, 2)

    rows: list[list] = []
    for j in range(oldest, -1, -1):
        days = days_of(j)
        if not days:
            continue
        share = len(days) / 7
        week: dict[str, list[list]] = {ONLINE: [], OFFLINE: []}
        for store, _, _ in STORES:
            online = round(7 * share * ONLINE_DAILY[store] * ONLINE_GROWTH ** (1 - j))
            offline = round(7 * share * OFFLINE_DAILY[store] * swing[j])
            if j <= 1:
                moved = round(offline * SHIFT)
                online, offline = online + moved, offline - moved
            for channel, count in ((ONLINE, online), (OFFLINE, offline)):
                for day in _spread(count, days):
                    row = [store, day.isoformat(), channel, price(store, channel, j), "paid"]
                    week[channel].append(row)
                    rows.append(row)
        # 退款：每个渠道按这一周的比例挑（西南那家小店另算：每周一单线下退款）
        for channel, pool in week.items():
            usual = [r for r in pool if r[0] != SMALL_STORE]
            rate = REFUND_RATE[channel][1 if j <= 1 else 0]
            for row in rng.sample(usual, round(len(usual) * rate)):
                row[4] = "refunded"
        small = [r for r in week[OFFLINE] if r[0] == SMALL_STORE]
        if small:
            small[0][4] = "refunded"
        # 取消单：另外的单（不占上面的订单数），店、渠道、日子随机
        for _ in range(round(CANCELLED_WEEKLY * share * cancel[j])):
            store = rng.choice([s for s, _, _ in STORES if s != SMALL_STORE])
            channel = OFFLINE if rng.random() < 0.6 else ONLINE
            rows.append([store, rng.choice(days).isoformat(), channel, price(store, channel, j), "cancelled"])
    rows.sort(key=lambda r: (r[1], r[0], r[2], r[4], r[3]))
    db = sqlite3.connect(path)
    db.executescript(SCHEMA)
    db.executemany("INSERT INTO stores VALUES (?, ?, ?)", STORES)
    db.executemany("INSERT INTO orders VALUES (?, ?, ?, ?, ?, ?)", [(i, *r) for i, r in enumerate(rows, start=1)])
    db.commit()
    db.close()
    return path


_REGION = {store: region for store, _, region in STORES}


def main() -> None:
    parser = argparse.ArgumentParser(description="生成分析题用的虚构零售库")
    parser.add_argument("path")
    parser.add_argument("--today", type=date.fromisoformat, default=None)
    args = parser.parse_args()
    print(build(args.path, args.today))


if __name__ == "__main__":
    main()
