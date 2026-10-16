"""真实模型验收用的虚构零售库。表名、店名、客户、手机号、金额都是编的，和任何真实业务无关。

    python -m tests.fixtures.acceptance.retail <输出 .db 路径> [--today YYYY-MM-DD]

生成「今天」之前 60 天的订单（截止昨天），随机数种子固定：同一个「今天」生成的库逐字节一样。埋了几个口径陷阱，
验收时用它们检验双盲复算、反方审稿能不能识破：

- 退款行：约 8% 的订单 status = 'refunded'（paid_date 有值）。口径要的销售额不含退款；漏掉这个条件，金额偏高。
- 下单日和支付日：约 15% 的订单隔 1–2 天才支付。口径按支付日（paid_date）归属；按下单日（order_date）统计会把
  跨日、跨周的单算错地方。
- 取消的单：约 3% status = 'cancelled'、paid_date 为空，按支付日统计时自然不在内。
- 华南的销售额每周都明显高于华东（门店多、客单价高）：验收时让报告写「华东销售额最高」这句错话，看反方能不能推翻。

customers.phone 是手机号，验收时把它设成遮罩列，检验质询、证据面板、可验证报告不露原值。append_day 给定时任务
验收用：两次触发之间往「昨天」追加几单，制造「只数据变」。

评测的零售样例题集（tests/fixtures/eval/retail_suite.json）按数据源名 SOURCE_NAME 对上这个库：生成之后登记成这个名字的
只读数据源，再导入题集（POST /api/evals/suites/import），题目的期望都按这个库算。
"""
from __future__ import annotations

import argparse
import random
import sqlite3
from datetime import date, timedelta
from pathlib import Path

DAYS = 60
SEED = 20261006
#: 零售样例题集里写的数据源名
SOURCE_NAME = "retail_demo"

#: (门店 id, 店名, 地区, 城市)：华南门店多、客单价高，华东次之
STORES = [
    (1, "星河一号店", "华东", "临川"), (2, "星河二号店", "华东", "临川"), (3, "星河三号店", "华东", "望江"),
    (4, "星河四号店", "华南", "南屿"), (5, "星河五号店", "华南", "南屿"), (6, "星河六号店", "华南", "海澄"),
    (7, "星河七号店", "华南", "海澄"), (8, "星河八号店", "华北", "北岭"), (9, "星河九号店", "西南", "云坪"),
]
#: 各地区客单价的大致区间（元）
PRICE = {"华东": (60, 260), "华南": (90, 380), "华北": (40, 200), "西南": (30, 180)}
#: 每家店每天的订单数区间
DAILY = {"华东": (4, 8), "华南": (6, 10), "华北": (3, 6), "西南": (2, 5)}
CUSTOMERS = 400

SCHEMA = """
CREATE TABLE stores (id INTEGER PRIMARY KEY, name TEXT NOT NULL, region TEXT NOT NULL, city TEXT NOT NULL);
CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT NOT NULL, phone TEXT NOT NULL, joined_on DATE NOT NULL);
CREATE TABLE orders (
  id INTEGER PRIMARY KEY,
  store_id INTEGER NOT NULL REFERENCES stores(id),
  customer_id INTEGER NOT NULL REFERENCES customers(id),
  order_date DATE NOT NULL,
  paid_date DATE,
  amount REAL NOT NULL,
  status TEXT NOT NULL,
  channel TEXT NOT NULL
);
"""


def _phone(rng: random.Random) -> str:
    # 199 号段加 8 位随机数：看起来像手机号，不对应任何真实号码
    return "199" + "".join(str(rng.randrange(10)) for _ in range(8))


def _orders_for(rng: random.Random, day: date, start_id: int, scale: float = 1.0) -> list[tuple]:
    rows = []
    next_id = start_id
    for store_id, _, region, _ in STORES:
        low, high = DAILY[region]
        for _ in range(max(1, round(rng.randint(low, high) * scale))):
            amount = round(rng.uniform(*PRICE[region]), 2)
            roll = rng.random()
            if roll < 0.03:
                status, paid = "cancelled", None
            else:
                status = "refunded" if roll < 0.11 else "paid"
                lag = rng.choices((0, 1, 2), weights=(85, 10, 5))[0]
                paid = (day + timedelta(days=lag)).isoformat()
            rows.append((next_id, store_id, rng.randint(1, CUSTOMERS), day.isoformat(), paid, amount, status,
                         rng.choice(("online", "offline"))))
            next_id += 1
    return rows


def build(path: Path | str, today: date | None = None) -> Path:
    """生成到 path（已存在就覆盖）。today 缺省取运行这一天；订单截止 today 的前一天，支付日可能落在之后。"""
    path = Path(path)
    if path.exists():
        path.unlink()
    today = today or date.today()
    rng = random.Random(SEED)
    db = sqlite3.connect(path)
    db.executescript(SCHEMA)
    db.executemany("INSERT INTO stores VALUES (?, ?, ?, ?)", STORES)
    first = today - timedelta(days=DAYS)
    db.executemany("INSERT INTO customers VALUES (?, ?, ?, ?)", [
        (i, f"客户{i:04d}", _phone(rng), (first - timedelta(days=rng.randint(0, 400))).isoformat())
        for i in range(1, CUSTOMERS + 1)])
    rows: list[tuple] = []
    for offset in range(DAYS):
        rows += _orders_for(rng, first + timedelta(days=offset), len(rows) + 1)
    db.executemany("INSERT INTO orders VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows)
    db.commit()
    db.close()
    return path


def append_day(path: Path | str, day: date, *, seed: int, scale: float = 0.5) -> int:
    """往 day 这一天追加一批订单（都已支付、当天支付），返回追加的单数。定时任务验收用来制造「只数据变」。"""
    rng = random.Random(seed)
    db = sqlite3.connect(path)
    start = (db.execute("SELECT COALESCE(MAX(id), 0) FROM orders").fetchone()[0] or 0) + 1
    rows = [(*row[:4], day.isoformat(), row[5], "paid", row[7]) for row in _orders_for(rng, day, start, scale)]
    db.executemany("INSERT INTO orders VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows)
    db.commit()
    db.close()
    return len(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description="生成真实模型验收用的虚构零售库")
    parser.add_argument("path")
    parser.add_argument("--today", type=date.fromisoformat, default=None)
    args = parser.parse_args()
    out = build(args.path, args.today)
    print(out)


if __name__ == "__main__":
    main()
