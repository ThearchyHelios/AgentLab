"""SQL 差异说明的执行比对夹具：虚构业务库的表结构、按固定种子生成的几组数据、SQL 对的语料。

为什么要执行：explain_sql_diff 判「等价」靠的是两条 SQL 规范化之后的形式一字不差，规范化的每一步都必须保持语义。
光看规范化的代码保证不了这一点（sqlglot 的 merge_subqueries 把外连接可空一侧的常量列并进外层、unnest_subqueries 把
OR 里的标量子查询改成 CROSS JOIN，都是复验时真踩到的），所以拿真实的库来守门：判等价的两条在每组数据上结果都得
一样，结果不一样的绝不能判等价。

数据刻意带上会让写法差异暴露出来的东西：空值（金额、状态、时间、关联键）、重复的行、边界值（金额正好 100、时间
正好在月初零点、月末 23:59:59）、已删除的行、退款（一单多退、退款单关联不上订单）、悬空的外键、空表。金额都是
2 的负幂次的倍数，不同的求和先后得到的浮点数一样；比较结果时浮点数另按 6 位小数取整（乘 0.9 这类每行都有舍入）。

语料（PAIRS，SQLite，拿去执行）每一项 (编号, A, B, 预期)：same 是已知等价的改写（别名、CTE 改名、关联先后、条件
左右、子查询和 CTE 互换……），编号以 hard: 开头的是要按语义推理才看得出等价的（条件挪进相关子查询、冗余条件），规范化
不保证认得出、认不出时只许给兜底说明；diff 是不许判等价的（换时间列、少一个条件、去重与否、外连接与内连接，以及只在
某个方言、某种排序规则下才等价的）。第 2 轮复验的反例在 _SAME_HARD、_SAME_R2、_DIFF_R2（执行）和 REGRESSIONS_R2、
DIALECT_SAME_R2、KNOWN_UNRECOGNIZED（各方言，只查结论）里；第 3 轮的在 _DIFF_R3、_SAME_R3、_SAME_TYPED_R3（要给表结构才认得出，Pair.typed）、_SAME_SCHEMA_R3（要靠
表结构里的主键才看得出等价，Pair.needs_schema）、_SAME_HARD_R3（执行）和 REGRESSIONS_R3、DIALECT_SAME_R3（只查
结论）里。LIMIT 都带能定出唯一顺序的排序，没有 ORDER BY 的 LIMIT 不进语料（EXISTS 里的 LIMIT 1 OFFSET 1 只看有没有
第二行，不看先后）。过去复验报过的反例都收进来了：第 5 轮回退的「只在 IN 子查询里用到的 CTE」在 PAIRS 里；复验者留在 /private/tmp 的脚本里、在这几组数据上
结果不同的对是 VALIDATOR_DIFF（也进 PAIRS 执行）；旧测试（五轮复核修正按复验报告补的用例，各方言，用到的列这个库里
不全有）里报过不同的对是 REGRESSIONS、意思一样的对是 DIALECT_SAME，这两份不执行，只查判没判等价。
"""
from __future__ import annotations

import random
import sqlite3
from dataclasses import dataclass

DDL = {
    "users": "id INTEGER PRIMARY KEY, name TEXT, city TEXT, vip INTEGER, grade INTEGER, referrer_id INTEGER, "
             "created_at TEXT, is_deleted INTEGER",
    "stores": "id INTEGER PRIMARY KEY, name TEXT, city TEXT, region TEXT, owner_id INTEGER",
    "orders": "id INTEGER PRIMARY KEY, user_id INTEGER, store_id INTEGER, parent_id INTEGER, referrer_id INTEGER, "
              "amount REAL, discount REAL, tax REAL, status TEXT, region TEXT, created_at TEXT, paid_at TEXT, "
              "is_deleted INTEGER",
    "orders_archive": "id INTEGER PRIMARY KEY, user_id INTEGER, store_id INTEGER, parent_id INTEGER, "
                      "referrer_id INTEGER, amount REAL, discount REAL, tax REAL, status TEXT, region TEXT, "
                      "created_at TEXT, paid_at TEXT, is_deleted INTEGER",
    "order_items": "id INTEGER PRIMARY KEY, order_id INTEGER, product_id INTEGER, qty INTEGER, price REAL",
    "refunds": "id INTEGER PRIMARY KEY, order_id INTEGER, user_id INTEGER, amount REAL, state TEXT, refunded_at TEXT",
    "payments": "id INTEGER PRIMARY KEY, order_id INTEGER, user_id INTEGER, amount REAL, method TEXT, paid_at TEXT",
    "shipments": "id INTEGER PRIMARY KEY, order_id INTEGER, carrier TEXT, shipped_at TEXT",
    # 一张叫 nums 的实表：SQLite 里不写 RECURSIVE 的递归 CTE（WITH nums(n) AS (… FROM nums …)）引用的是它自己，换个名字
    # 引用的就是这张表了
    "nums": "n INTEGER",
}


def schema_cache() -> dict:
    """数据源 schema_cache 的形状（{"tables": {表: {"columns": [{"name", "type", "nullable"}], "primary_key": […]}}}，
    和 data/introspect.py 探查出来的一样），explain_sql_diff 的 schema。SQLAlchemy 读 SQLite 的 INTEGER PRIMARY KEY
    时 nullable 照样是 True（PRAGMA table_info 的 notnull 是 0），主键另记在 primary_key 里。"""
    tables = {}
    for name, ddl in DDL.items():
        cols, pk = [], []
        for part in ddl.split(", "):
            col, typ = part.split(" ")[:2]
            cols.append({"name": col, "type": typ, "nullable": True})
            if "PRIMARY KEY" in part:
                pk.append(col)
        tables[name] = {"columns": cols, "primary_key": pk}
    return {"tables": tables}


AMOUNTS = [0, 0.5, 10, 12.5, 99.5, 100, 100.25, 250, -5, None]
DATES = ["2026-08-31", "2026-08-31 23:59:59", "2026-09-01", "2026-09-01 08:00:00", "2026-09-15", "2026-09-30",
         "2026-09-30 23:59:59", "2026-10-01", "2026-10-01 00:00:00", None]
STATUSES = ["paid", "paid", "open", "done", "void", "shipped", None]
CITIES = ["SH", "BJ", "虚构市", None]


def _rows(seed: int) -> dict[str, list[tuple]]:
    rng = random.Random(seed)
    pick = rng.choice
    n_users = rng.randint(4, 12)
    users = [(i, f"u{i}", pick(CITIES), pick([0, 1, 1, None]), pick([1, 2, 3, 4, 5, None]),
              pick([None, None] + list(range(1, n_users + 1))), pick(DATES), pick([0, 0, 0, 1, None]))
             for i in range(1, n_users + 1)]
    n_stores = rng.randint(2, 5)
    stores = [(i, f"s{i}", pick(CITIES), pick(["east", "west", None]), pick([None] + list(range(1, n_users + 1))))
              for i in range(1, n_stores + 1)]
    user_ids = list(range(1, n_users + 1)) + [99, None]
    orders: list[tuple] = []
    for i in range(1, rng.randint(8, 36) + 1):
        parent = pick([None, None, None] + [o[0] for o in orders[-5:]])
        orders.append((i, pick(user_ids), pick(list(range(1, n_stores + 1)) + [None]), parent,
                       pick([None] + list(range(1, n_users + 1))), pick(AMOUNTS), pick([0, 0.5, 2, None]),
                       pick([0, 1, 2.5, None]), pick(STATUSES), pick(["east", "west", None]), pick(DATES),
                       pick(DATES), pick([0, 0, 1, None])))
    # 重复的行：同一个用户同一笔金额再来一单
    for o in rng.sample(orders, min(3, len(orders))):
        orders.append((len(orders) + 1, *o[1:]))
    archive = [o for o in orders if rng.random() < 0.5]
    items, refunds, payments, shipments = [], [], [], []
    for o in orders:
        for _ in range(rng.randint(0, 3)):
            items.append((len(items) + 1, o[0], rng.randint(1, 4), pick([0, 1, 2, 5, None]), pick([0.5, 10, 12.5, None])))
        for _ in range(pick([0, 0, 1, 1, 2])):
            refunds.append((len(refunds) + 1, o[0], pick([o[1], o[1], None]), pick(AMOUNTS),
                            pick(["done", "pending", "rejected", None]), pick(DATES)))
        for _ in range(rng.randint(0, 2)):
            payments.append((len(payments) + 1, o[0], pick([o[1], None]), pick(AMOUNTS), pick(["card", "cash", None]),
                             pick(DATES)))
        if rng.random() < 0.6:
            shipments.append((len(shipments) + 1, o[0], pick(["sf", "ems", None]), pick(DATES)))
    # 关联不上订单的退款、付款
    refunds.append((len(refunds) + 1, pick([None, 999]), pick(user_ids), pick(AMOUNTS), "done", pick(DATES)))
    payments.append((len(payments) + 1, None, pick(user_ids), pick(AMOUNTS), "cash", pick(DATES)))
    nums = [(n,) for n in rng.sample([1, 2, 3, 4, 10, 20], rng.randint(1, 4))]
    return {"users": users, "stores": stores, "orders": orders, "orders_archive": archive, "order_items": items,
            "refunds": refunds, "payments": payments, "shipments": shipments, "nums": nums}


def _edge_rows() -> dict[str, list[tuple]]:
    """一组极端数据：退款、付款、发货都是空表，订单只有几行，状态、时间全是空值或边界值。"""
    users = [(1, "u1", None, None, None, None, None, None), (2, "u2", "SH", 1, 3, 1, "2026-09-01", 0)]
    stores = [(1, "s1", None, None, None)]
    orders = [(1, 1, 1, None, None, 100, None, None, None, None, "2026-09-01", "2026-10-01", None),
              (2, 1, None, 1, 2, None, 0, 0, "paid", "east", None, None, 0),
              (3, None, 1, None, None, 100, 0, 1, "paid", None, "2026-09-30 23:59:59", "2026-09-01", 1)]
    return {"users": users, "stores": stores, "orders": orders, "orders_archive": [], "order_items": [],
            "refunds": [], "payments": [], "shipments": [], "nums": []}


def _tricky_rows() -> dict[str, list[tuple]]:
    """一组专挑写法差异的数据（第 2、3 轮复验的反例在这上面现形）：金额 0.1、0.2、0.3（浮点数 0.1 + 0.2 ≠ 0.3）、
    100 * 0.07；状态里有像数字的文本（'3.0'、'12'）、大小写不同的 'PAID'，还有 'State'（SQLite 把找不到这一列的
    "State" 读成字符串，和 "state" 不同）；用户名是像数字的文本（'0100' 取两次负是 100）；城市 'SH'、'sh'；id 16 的
    订单（0x10）；nums 表有几行。"""
    users = [(1, "0100", "SH", 1, 3, None, "2026-09-01", 0), (2, "5", "sh", 0, 1, 1, "2026-09-02", 0),
             (3, "alice", "Sh", None, None, 2, None, 1), (4, "Bob", None, 1, 5, None, "2026-08-31 23:59:59", None)]
    stores = [(1, "s1", "SH", "east", 1), (2, "s2", "sh", None, None), (3, "3.0", "12", "west", 2)]
    amounts = [0.1, 0.2, 0.3, 0.3, 0.30000000000000004, 7.0, 100 * 0.07, 12.5, None, 100.0, 0, 0.5]
    statuses = ["paid", "PAID", "3.0", "12", None, "open", "void", "paid", "State"]
    orders = []
    for i in range(1, 21):
        orders.append((i, (i % 4) + 1 if i % 7 else None, (i % 3) + 1, i - 1 if i % 5 == 0 else None, (i % 2) + 1,
                       amounts[i % len(amounts)], [0, 0.5, None][i % 3], [0, 1, None][i % 3],
                       statuses[i % len(statuses)], ["east", "west", None][i % 3], DATES[i % len(DATES)],
                       DATES[(i * 3) % len(DATES)], [0, 1, None][i % 3]))
    refunds = [(1, 1, 2, 0.1, "done", "2026-09-05"), (2, 16, 1, 0.2, None, None), (3, 3, None, 0.3, "done", "2026-09-30"),
               (4, 99, 3, 7.0, "pending", "2026-10-01")]
    payments = [(1, 1, 2, 0.3, "card", "2026-09-02"), (2, 16, 1, 0.1, "cash", None)]
    items = [(1, 1, 1, 2, 0.1), (2, 16, 2, None, 0.2), (3, 3, 1, 1, None)]
    shipments = [(1, 1, "sf", "2026-09-03"), (2, 16, None, None)]
    return {"users": users, "stores": stores, "orders": orders, "orders_archive": orders[:5], "order_items": items,
            "refunds": refunds, "payments": payments, "shipments": shipments, "nums": [(1,), (2,), (10,)]}


#: 生成数据用的种子（每个种子一组），外加一组极端数据、一组专挑写法差异的数据
SEEDS = (7, 11, 23, 42, 2026)


def build_databases() -> list[sqlite3.Connection]:
    """每组数据一个内存库。"""
    out = []
    for rows in [_rows(seed) for seed in SEEDS] + [_edge_rows(), _tricky_rows()]:
        conn = sqlite3.connect(":memory:")
        for name, ddl in DDL.items():
            conn.execute(f"CREATE TABLE {name} ({ddl})")
            if rows[name]:
                marks = ", ".join("?" * len(rows[name][0]))
                conn.executemany(f"INSERT INTO {name} VALUES ({marks})", rows[name])
        out.append(conn)
    return out


def _norm(value):
    return round(value, 6) if isinstance(value, float) else value


def run(conn: sqlite3.Connection, sql: str) -> list[tuple]:
    """执行结果按行排序（行的先后不算结果的一部分），浮点数按 6 位小数取整。"""
    rows = [tuple(_norm(v) for v in row) for row in conn.execute(sql).fetchall()]
    return sorted(rows, key=lambda r: tuple((v is None, str(type(v)), v if v is not None else 0) for v in r))


@dataclass(frozen=True)
class Pair:
    id: str
    a: str
    b: str
    #: same：已知等价的改写；diff：不许判等价——语义不同，或者只在某个方言、某种排序规则、某种列类型下才等价
    #: （比如 SQLite 里 status > 'b' AND status > 'o' 和 status > 'o' 一样，PostgreSQL 的语言排序规则下不一定）
    expect: str
    #: 要靠表结构里的约束（主键不为空）才看得出等价：没给表结构时可以说出具体的不同（那时确实不知道它不会为空）
    needs_schema: bool = False
    #: 要给表结构才认得出等价（列的类型、外层表有没有这一列）：没给表结构时只许给兜底说明，认出的比例不算进
    #: 「不给表结构」那一项
    typed: bool = False


PR = ("WITH paid AS (SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id), "
      "refunded AS (SELECT user_id, SUM(amount) AS total FROM refunds GROUP BY user_id) ")

_SAME = [
    # ---- 别名、大小写、空白、注释
    ("alias", "SELECT SUM(o.amount) FROM orders o WHERE o.status = 'paid'",
     "SELECT SUM(x.amount) FROM orders AS x WHERE x.status = 'paid'"),
    ("bare-vs-qualified", "SELECT SUM(o.amount) FROM orders o WHERE o.status = 'paid' AND o.paid_at >= '2026-09-01'",
     "SELECT SUM(amount) FROM orders WHERE paid_at >= '2026-09-01' AND status = 'paid'"),
    ("case-comments", "SELECT store_id, SUM(amount) FROM orders WHERE status = 'paid' GROUP BY store_id",
     "select   STORE_ID , -- 门店\n sum( Amount ) /* 金额 */ from ORDERS\n where status='paid' group by store_id"),
    ("quoted-names", 'SELECT SUM("amount") FROM "orders" WHERE "status" = \'paid\'',
     "SELECT SUM(amount) FROM orders WHERE status = 'paid'"),
    ("output-alias", "SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id",
     "SELECT o.user_id AS uid, SUM(o.amount) AS s FROM orders o GROUP BY o.user_id"),
    # ---- 条件左右、方向、先后、括号、等价写法
    ("flip-compare", "SELECT COUNT(*) FROM orders WHERE amount > 100 AND paid_at >= '2026-09-01' AND store_id = 3",
     "SELECT COUNT(*) FROM orders WHERE 3 = store_id AND '2026-09-01' <= paid_at AND 100 < amount"),
    ("and-order-parens", "SELECT COUNT(*) FROM orders WHERE (status = 'paid') AND ((amount > 0)) AND is_deleted = 0",
     "SELECT COUNT(*) FROM orders WHERE is_deleted = 0 AND amount > 0 AND status = 'paid'"),
    ("or-order", "SELECT COUNT(*) FROM orders WHERE status = 'paid' OR status = 'done'",
     "SELECT COUNT(*) FROM orders WHERE status = 'done' OR status = 'paid'"),
    ("between", "SELECT SUM(amount) FROM orders WHERE paid_at BETWEEN '2026-09-01' AND '2026-09-30'",
     "SELECT SUM(amount) FROM orders WHERE paid_at >= '2026-09-01' AND paid_at <= '2026-09-30'"),
    ("not-eq", "SELECT COUNT(*) FROM orders WHERE NOT status = 'void'",
     "SELECT COUNT(*) FROM orders WHERE status <> 'void'"),
    ("bang-eq", "SELECT COUNT(*) FROM orders WHERE status != 'void'",
     "SELECT COUNT(*) FROM orders WHERE status <> 'void'"),
    ("not-null", "SELECT COUNT(*) FROM orders WHERE paid_at IS NOT NULL",
     "SELECT COUNT(*) FROM orders WHERE NOT (paid_at IS NULL)"),
    ("not-gt", "SELECT COUNT(*) FROM orders WHERE NOT amount > 100",
     "SELECT COUNT(*) FROM orders WHERE amount <= 100"),
    ("in-order", "SELECT COUNT(*) FROM orders WHERE status IN ('paid', 'done')",
     "SELECT COUNT(*) FROM orders WHERE status IN ('done', 'paid', 'done')"),
    ("in-single", "SELECT COUNT(*) FROM orders WHERE status IN ('paid')",
     "SELECT COUNT(*) FROM orders WHERE status = 'paid'"),
    ("mul-commute", "SELECT SUM(price * qty) FROM order_items WHERE price + 1 > 10",
     "SELECT SUM(qty * price) FROM order_items WHERE 1 + price > 10"),
    ("paren-arith", "SELECT COUNT(*) FROM orders WHERE (amount + tax) > 10",
     "SELECT COUNT(*) FROM orders WHERE amount + tax > 10"),
    ("count-1", "SELECT COUNT(1) FROM orders WHERE status = 'paid'",
     "SELECT count(*) FROM orders WHERE status = 'paid'"),
    ("count-1-having", "SELECT store_id FROM orders GROUP BY store_id HAVING COUNT(1) > 2",
     "SELECT store_id FROM orders GROUP BY store_id HAVING 2 < COUNT(*)"),
    ("having-flip", "SELECT store_id FROM orders GROUP BY store_id HAVING COUNT(*) >= 5",
     "SELECT store_id FROM orders GROUP BY store_id HAVING 5 <= COUNT(*)"),
    ("scalar-flip", "SELECT SUM(amount) FROM orders WHERE amount > (SELECT AVG(amount) FROM orders WHERE is_deleted = 0)",
     "SELECT SUM(amount) FROM orders WHERE (SELECT AVG(amount) FROM orders WHERE 0 = is_deleted) < amount"),
    ("count-distinct-paren", "SELECT COUNT(DISTINCT(user_id)) FROM orders",
     "SELECT COUNT(DISTINCT user_id) FROM orders"),
    ("expr-commute", "SELECT store_id, COUNT(*) * 100 AS x FROM orders GROUP BY store_id",
     "SELECT store_id, 100 * COUNT(*) AS y FROM orders GROUP BY store_id"),
    # ---- 分组、排序、取行
    ("group-ordinal", "SELECT store_id, SUM(amount) AS s FROM orders GROUP BY 1",
     "SELECT store_id, SUM(amount) AS s FROM orders GROUP BY store_id"),
    ("group-order", "SELECT region, store_id, SUM(amount) FROM orders GROUP BY region, store_id HAVING SUM(amount) > 10",
     "SELECT region, store_id, SUM(amount) AS s FROM orders GROUP BY store_id, region HAVING s > 10"),
    ("group-alias", "SELECT o.region, SUM(o.amount) FROM orders o GROUP BY o.region HAVING SUM(o.amount) > 100",
     "SELECT region AS r, SUM(amount) AS s FROM orders GROUP BY r HAVING s > 100"),
    ("group-expr-ordinal", "SELECT DATE(paid_at) AS d, SUM(amount) FROM orders GROUP BY DATE(paid_at)",
     "SELECT DATE(paid_at) AS d, SUM(amount) FROM orders GROUP BY 1"),
    ("limit-offset0", "SELECT id, amount FROM orders ORDER BY amount DESC, id LIMIT 3 OFFSET 0",
     "SELECT o.id, o.amount FROM orders o ORDER BY o.amount DESC, o.id ASC LIMIT 3"),
    ("order-alias", "SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id ORDER BY total DESC, user_id LIMIT 3",
     "SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id ORDER BY SUM(amount) DESC, user_id LIMIT 3"),
    ("order-ordinal", "SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id ORDER BY 2 DESC, 1 LIMIT 3",
     "SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id ORDER BY total DESC, user_id LIMIT 3"),
    ("distinct-vs-group", "SELECT user_id, store_id FROM orders GROUP BY user_id, store_id",
     "SELECT DISTINCT user_id, store_id FROM orders"),
    ("distinct-group-redundant", "SELECT DISTINCT user_id FROM orders GROUP BY user_id",
     "SELECT user_id FROM orders GROUP BY user_id"),
    ("distinct-subq-vs-group", "SELECT COUNT(*) FROM (SELECT DISTINCT user_id FROM orders WHERE status = 'paid') t",
     "SELECT COUNT(*) FROM (SELECT user_id FROM orders WHERE status = 'paid' GROUP BY user_id) AS u"),
    ("window-alias", "SELECT o.id, ROW_NUMBER() OVER (PARTITION BY o.user_id ORDER BY o.paid_at, o.id) AS rn FROM orders o",
     "SELECT x.id, ROW_NUMBER() OVER (PARTITION BY x.user_id ORDER BY x.paid_at, x.id) AS k FROM orders x"),
    ("window-partition-order", "SELECT id, SUM(amount) OVER (PARTITION BY user_id, store_id) AS s FROM orders",
     "SELECT id, SUM(amount) OVER (PARTITION BY store_id, user_id) AS s FROM orders"),
    ("case-alias", "SELECT SUM(CASE WHEN o.status = 'paid' THEN o.amount ELSE 0 END) FROM orders o",
     "SELECT SUM(CASE WHEN x.status = 'paid' THEN x.amount ELSE 0 END) FROM orders AS x"),
    # ---- 连接
    ("join-swap", "SELECT s.name, SUM(o.amount) FROM orders o JOIN stores s ON s.id = o.store_id GROUP BY s.name",
     "SELECT s.name, SUM(o.amount) FROM stores s INNER JOIN orders o ON o.store_id = s.id GROUP BY s.name"),
    ("comma-join", "SELECT COUNT(*) FROM orders o, stores s WHERE o.store_id = s.id AND s.city = 'SH'",
     "SELECT COUNT(*) FROM orders o JOIN stores s ON s.id = o.store_id WHERE s.city = 'SH'"),
    ("on-vs-where", "SELECT COUNT(*) FROM orders o JOIN stores s ON s.id = o.store_id WHERE s.city = 'SH'",
     "SELECT COUNT(*) FROM orders o JOIN stores s ON s.id = o.store_id AND s.city = 'SH'"),
    ("cross-where", "SELECT COUNT(*) FROM orders o CROSS JOIN stores s WHERE s.id = o.store_id",
     "SELECT COUNT(*) FROM orders o JOIN stores s ON s.id = o.store_id"),
    ("three-way", "SELECT COUNT(*) FROM orders o JOIN stores s ON s.id = o.store_id JOIN users u ON u.id = o.user_id "
                  "WHERE u.vip = 1 AND s.city = 'SH'",
     "SELECT COUNT(*) FROM users u JOIN orders o ON o.user_id = u.id JOIN stores s ON o.store_id = s.id "
     "WHERE s.city = 'SH' AND u.vip = 1"),
    ("multi-on", "SELECT COUNT(*) FROM orders o JOIN refunds r ON r.order_id = o.id AND r.user_id = o.user_id",
     "SELECT COUNT(*) FROM refunds r JOIN orders o ON o.user_id = r.user_id AND o.id = r.order_id"),
    ("left-outer-word", "SELECT o.id, r.amount FROM orders o LEFT OUTER JOIN refunds r ON r.order_id = o.id",
     "SELECT o.id, r.amount FROM orders o LEFT JOIN refunds r ON o.id = r.order_id"),
    ("left-right-mirror", "SELECT o.id, r.amount FROM orders o LEFT JOIN refunds r ON r.order_id = o.id",
     "SELECT o.id, r.amount FROM refunds r RIGHT JOIN orders o ON o.id = r.order_id"),
    ("left-chain-reorder", "SELECT COUNT(*) FROM orders o LEFT JOIN refunds r ON r.order_id = o.id "
                           "LEFT JOIN users u ON u.id = o.user_id",
     "SELECT COUNT(*) FROM orders o LEFT JOIN users u ON u.id = o.user_id LEFT JOIN refunds r ON r.order_id = o.id"),
    ("left-rejected-vs-inner", "SELECT COUNT(*) FROM orders o LEFT JOIN refunds r ON r.order_id = o.id WHERE r.amount > 10",
     "SELECT COUNT(*) FROM refunds r JOIN orders o ON o.id = r.order_id WHERE r.amount > 10"),
    ("left-not-null-vs-inner", "SELECT SUM(o.amount) FROM orders o LEFT JOIN users u ON u.id = o.user_id "
                               "WHERE u.city IS NOT NULL",
     "SELECT SUM(o.amount) FROM orders o JOIN users u ON u.id = o.user_id WHERE u.city IS NOT NULL"),
    ("anti-join-alias", "SELECT COUNT(*) FROM orders o LEFT JOIN refunds r ON r.order_id = o.id WHERE r.id IS NULL",
     "SELECT COUNT(*) FROM orders x LEFT JOIN refunds y ON x.id = y.order_id WHERE y.id IS NULL"),
    ("using-vs-on", "SELECT COUNT(*) FROM orders o JOIN refunds r USING (user_id)",
     "SELECT COUNT(*) FROM orders o JOIN refunds r ON r.user_id = o.user_id"),
    ("count-distinct-join", "SELECT COUNT(DISTINCT o.user_id) FROM orders o JOIN stores s ON s.id = o.store_id "
                            "WHERE s.city = 'SH'",
     "SELECT COUNT(DISTINCT a.user_id) FROM stores b JOIN orders a ON a.store_id = b.id WHERE b.city = 'SH'"),
    # ---- 自连接
    ("self-join-swap", "SELECT COUNT(*) FROM orders o JOIN orders c ON c.parent_id = o.id WHERE c.amount > o.amount",
     "SELECT COUNT(*) FROM orders x JOIN orders y ON x.id = y.parent_id WHERE x.amount < y.amount"),
    ("self-join-names-exchanged", "SELECT COUNT(*) FROM users a JOIN users b ON b.id = a.referrer_id "
                                  "WHERE a.vip = 1 AND b.vip = 0",
     "SELECT COUNT(*) FROM users b JOIN users a ON a.id = b.referrer_id WHERE b.vip = 1 AND a.vip = 0"),
    ("self-join-flip-op", "SELECT COUNT(*) FROM orders a JOIN orders b ON a.user_id = b.user_id WHERE a.id < b.id",
     "SELECT COUNT(*) FROM orders a JOIN orders b ON a.user_id = b.user_id WHERE b.id > a.id"),
    ("self-join-symmetric", "SELECT COUNT(*) FROM orders a JOIN orders b ON a.user_id = b.user_id "
                            "WHERE a.status = 'paid' AND b.status = 'void'",
     "SELECT COUNT(*) FROM orders b JOIN orders a ON b.user_id = a.user_id WHERE b.status = 'paid' AND a.status = 'void'"),
    ("self-join-3way", "SELECT COUNT(*) FROM users a JOIN users b ON b.id = a.referrer_id JOIN users c "
                       "ON c.id = b.referrer_id WHERE c.vip = 1 AND a.vip = 0",
     "SELECT COUNT(*) FROM users x JOIN users y ON y.id = x.referrer_id JOIN users z ON z.id = y.referrer_id "
     "WHERE x.vip = 0 AND z.vip = 1"),
    # ---- CTE、子查询
    ("cte-rename", "WITH paid AS (SELECT * FROM orders WHERE status = 'paid') "
                   "SELECT store_id, SUM(amount) FROM paid GROUP BY store_id",
     "WITH p2 AS (SELECT * FROM orders WHERE status = 'paid') SELECT store_id, SUM(amount) FROM p2 GROUP BY store_id"),
    ("cte-inline", "WITH paid AS (SELECT * FROM orders WHERE status = 'paid') "
                   "SELECT store_id, SUM(amount) FROM paid GROUP BY store_id",
     "SELECT store_id, SUM(amount) FROM orders WHERE status = 'paid' GROUP BY store_id"),
    ("cte-vs-subquery", "WITH t AS (SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id) "
                        "SELECT COUNT(*) FROM t WHERE t.total > 100",
     "SELECT COUNT(*) FROM (SELECT user_id, SUM(amount) AS s FROM orders GROUP BY user_id) AS q WHERE q.s > 100"),
    ("cte-def-order", "WITH a AS (SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id), "
                      "b AS (SELECT user_id, SUM(amount) AS total FROM refunds GROUP BY user_id) "
                      "SELECT SUM(a.total - b.total) FROM a JOIN b ON a.user_id = b.user_id",
     "WITH b AS (SELECT user_id, SUM(amount) AS total FROM refunds GROUP BY user_id), "
     "a AS (SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id) "
     "SELECT SUM(a.total - b.total) FROM b JOIN a ON a.user_id = b.user_id"),
    ("cte-vs-subq-swap", PR + "SELECT SUM(p.total) - SUM(r.total) FROM paid p JOIN refunded r ON r.user_id = p.user_id",
     "SELECT SUM(a.total) - SUM(b.total) FROM (SELECT user_id, SUM(amount) AS total FROM refunds GROUP BY user_id) b "
     "JOIN (SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id) a ON a.user_id = b.user_id"),
    ("cte-names-swapped", PR + "SELECT SUM(p.total - r.total) FROM paid p JOIN refunded r ON r.user_id = p.user_id",
     "WITH paid AS (SELECT user_id, SUM(amount) AS total FROM refunds GROUP BY user_id), "
     "refunded AS (SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id) "
     "SELECT SUM(r.total - p.total) FROM refunded r JOIN paid p ON p.user_id = r.user_id"),
    ("cte-condition-flipped", PR + "SELECT COUNT(*) FROM paid p JOIN refunded r ON r.user_id = p.user_id "
                                   "WHERE p.total - r.total > 100",
     "WITH rf AS (SELECT user_id, SUM(amount) AS total FROM refunds GROUP BY user_id), "
     "pd AS (SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id) "
     "SELECT COUNT(*) FROM rf JOIN pd ON pd.user_id = rf.user_id WHERE 100 < pd.total - rf.total"),
    ("cte-dead", "WITH unused AS (SELECT * FROM refunds), t AS (SELECT user_id, SUM(amount) AS s FROM orders "
                 "GROUP BY user_id) SELECT COUNT(*) FROM t WHERE s > 10",
     "WITH t AS (SELECT user_id, SUM(amount) AS s FROM orders GROUP BY user_id) SELECT COUNT(*) FROM t WHERE s > 10"),
    ("cte-chain-vs-nested", "WITH a AS (SELECT * FROM orders WHERE status = 'paid'), b AS (SELECT user_id, "
                            "SUM(amount) AS s FROM a GROUP BY user_id) SELECT COUNT(*) FROM b WHERE s > 100",
     "SELECT COUNT(*) FROM (SELECT user_id, SUM(amount) AS s FROM (SELECT * FROM orders WHERE status = 'paid') AS a "
     "GROUP BY user_id) AS b WHERE b.s > 100"),
    ("cte-filter-then-group", "WITH base AS (SELECT * FROM orders WHERE is_deleted = 0), per_user AS (SELECT user_id, "
                              "SUM(amount) AS total FROM base GROUP BY user_id) "
                              "SELECT COUNT(*) FROM per_user WHERE total > 100",
     "WITH totals AS (SELECT user_id, SUM(amount) AS total FROM orders WHERE is_deleted = 0 GROUP BY user_id) "
     "SELECT COUNT(*) FROM totals WHERE total > 100"),
    ("cte-filter-inner-vs-outer", "WITH p AS (SELECT * FROM orders WHERE status = 'paid') "
                                  "SELECT store_id, SUM(amount) FROM p GROUP BY store_id",
     "WITH p AS (SELECT * FROM orders) SELECT store_id, SUM(amount) FROM p WHERE status = 'paid' GROUP BY store_id"),
    ("cte-join-inline", "WITH p AS (SELECT * FROM orders WHERE status = 'paid') "
                        "SELECT SUM(p.amount) FROM users u JOIN p ON p.user_id = u.id WHERE u.vip = 1",
     "SELECT SUM(o.amount) FROM orders o JOIN users u ON o.user_id = u.id WHERE o.status = 'paid' AND u.vip = 1"),
    ("subq-join-inline", "SELECT COUNT(*) FROM users u JOIN (SELECT * FROM orders WHERE status = 'paid') o "
                         "ON o.user_id = u.id",
     "SELECT COUNT(*) FROM users u JOIN orders o ON o.user_id = u.id WHERE o.status = 'paid'"),
    ("cte-group-city", "WITH p AS (SELECT * FROM orders WHERE status = 'paid') "
                       "SELECT s.city, SUM(p.amount) FROM p JOIN stores s ON s.id = p.store_id GROUP BY s.city",
     "SELECT s.city, SUM(o.amount) FROM orders o JOIN stores s ON s.id = o.store_id WHERE o.status = 'paid' "
     "GROUP BY s.city"),
    ("cte-join-in-cte", "WITH t AS (SELECT o.amount, s.city FROM orders o JOIN stores s ON s.id = o.store_id) "
                        "SELECT city, SUM(amount) FROM t GROUP BY city",
     "SELECT s.city, SUM(o.amount) FROM orders o JOIN stores s ON s.id = o.store_id GROUP BY s.city"),
    ("cte-passthrough-group", "SELECT store_id, SUM(amount) FROM orders GROUP BY store_id",
     "WITH t AS (SELECT store_id, amount FROM orders) SELECT store_id, SUM(amount) FROM t GROUP BY store_id"),
    ("cte-computed-col", "WITH t AS (SELECT id, (amount * 0.5) AS net FROM orders) SELECT SUM(net) FROM t",
     "WITH t AS (SELECT id, amount * 0.5 AS net FROM orders) SELECT SUM(t.net) FROM t"),
    ("cte-date-inline", "WITH t AS (SELECT DATE(paid_at) AS d, amount FROM orders) "
                        "SELECT SUM(amount) FROM t WHERE d >= '2026-09-01'",
     "SELECT SUM(amount) FROM orders WHERE DATE(paid_at) >= '2026-09-01'"),
    ("cte-grouped-wrapped", "SELECT store_id, SUM(amount) AS s FROM orders GROUP BY store_id",
     "SELECT t.store_id, t.s FROM (SELECT store_id, SUM(amount) AS s FROM orders GROUP BY store_id) t"),
    ("cte-grouped-star", "WITH t AS (SELECT store_id, SUM(amount) AS s FROM orders GROUP BY store_id) SELECT * FROM t",
     "SELECT * FROM (SELECT store_id, SUM(amount) AS s FROM orders GROUP BY store_id) x"),
    ("cte-having-vs-where", "WITH t AS (SELECT user_id, SUM(amount) AS s FROM orders GROUP BY user_id "
                            "HAVING SUM(amount) > 100) SELECT COUNT(*) FROM t",
     "WITH t AS (SELECT user_id, SUM(amount) AS s FROM orders GROUP BY user_id) SELECT COUNT(*) FROM t WHERE s > 100"),
    ("cte-join-subq-mix", "WITH paid AS (SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id) "
                          "SELECT SUM(p.total - r.total) FROM paid p JOIN (SELECT user_id, SUM(amount) AS total "
                          "FROM refunds GROUP BY user_id) r ON r.user_id = p.user_id",
     "WITH refunded AS (SELECT user_id, SUM(amount) AS total FROM refunds GROUP BY user_id) "
     "SELECT SUM(p.total - r.total) FROM refunded r JOIN (SELECT user_id, SUM(amount) AS total FROM orders "
     "GROUP BY user_id) p ON p.user_id = r.user_id"),
    ("cte-three-reorder", "WITH p AS (SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id), "
                          "r AS (SELECT user_id, SUM(amount) AS total FROM refunds GROUP BY user_id), "
                          "v AS (SELECT id FROM users WHERE vip = 1) "
                          "SELECT SUM(p.total - r.total) FROM p JOIN r ON r.user_id = p.user_id JOIN v ON v.id = p.user_id",
     "WITH v AS (SELECT id FROM users WHERE vip = 1), r AS (SELECT user_id, SUM(amount) AS total FROM refunds "
     "GROUP BY user_id), p AS (SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id) "
     "SELECT SUM(p.total - r.total) FROM v JOIN p ON p.user_id = v.id JOIN r ON p.user_id = r.user_id"),
    ("cte-self-join-swapped", "WITH t AS (SELECT user_id, referrer_id, SUM(amount) AS total FROM orders "
                              "GROUP BY user_id, referrer_id) SELECT COUNT(*) FROM t a JOIN t b "
                              "ON b.user_id = a.referrer_id WHERE a.total > b.total",
     "WITH t AS (SELECT user_id, referrer_id, SUM(amount) AS total FROM orders GROUP BY user_id, referrer_id) "
     "SELECT COUNT(*) FROM t b JOIN t a ON a.referrer_id = b.user_id WHERE b.total < a.total"),
    ("two-passthrough-ctes", "WITH paid AS (SELECT * FROM orders WHERE status = 'paid'), open AS (SELECT * FROM orders "
                             "WHERE status = 'open') SELECT SUM(p.amount) - SUM(o.amount) FROM paid p JOIN open o "
                             "ON o.user_id = p.user_id",
     "WITH op AS (SELECT * FROM orders WHERE status = 'open'), pd AS (SELECT * FROM orders WHERE status = 'paid') "
     "SELECT SUM(x.amount) - SUM(y.amount) FROM op y JOIN pd x ON x.user_id = y.user_id"),
    ("passthrough-cte-own-table", "WITH paid AS (SELECT * FROM orders WHERE status = 'paid') SELECT COUNT(*) "
                                  "FROM orders o JOIN paid p ON p.parent_id = o.id WHERE o.amount > p.amount",
     "WITH pp AS (SELECT * FROM orders WHERE status = 'paid') SELECT COUNT(*) FROM pp c JOIN orders m "
     "ON c.parent_id = m.id WHERE c.amount < m.amount"),
    # ---- 条件里的子查询：IN、EXISTS、标量
    ("in-cte", "SELECT COUNT(*) FROM orders WHERE user_id IN (SELECT id FROM users WHERE vip = 1)",
     "WITH v AS (SELECT id FROM users WHERE vip = 1) SELECT COUNT(*) FROM orders WHERE user_id IN (SELECT id FROM v)"),
    ("in-cte-regression", "WITH p AS (SELECT * FROM orders WHERE status = 'paid') "
                          "SELECT COUNT(*) FROM users u WHERE u.id IN (SELECT user_id FROM p)",
     "SELECT COUNT(*) FROM users u WHERE u.id IN (SELECT user_id FROM orders WHERE status = 'paid')"),
    ("in-alias", "SELECT SUM(o.amount) FROM orders o WHERE o.user_id IN (SELECT u.id FROM users u WHERE u.vip = 1)",
     "SELECT SUM(amount) FROM orders WHERE user_id IN (SELECT id AS uid FROM users WHERE vip = 1)"),
    ("in-and-order", "SELECT COUNT(*) FROM orders WHERE user_id IN (SELECT id FROM users WHERE vip = 1 AND city = 'SH')",
     "SELECT COUNT(*) FROM orders WHERE user_id IN (SELECT id FROM users WHERE city = 'SH' AND vip = 1)"),
    ("in-join-swap", "SELECT COUNT(*) FROM orders WHERE user_id IN (SELECT u.id FROM users u JOIN stores s "
                     "ON s.owner_id = u.id)",
     "SELECT COUNT(*) FROM orders WHERE user_id IN (SELECT u.id FROM stores s JOIN users u ON u.id = s.owner_id)"),
    ("in-distinct", "SELECT COUNT(*) FROM orders WHERE user_id IN (SELECT DISTINCT user_id FROM refunds)",
     "SELECT COUNT(*) FROM orders WHERE user_id IN (SELECT user_id FROM refunds)"),
    ("exists-alias", "SELECT o.id FROM orders o WHERE NOT EXISTS (SELECT 1 FROM refunds r WHERE r.order_id = o.id)",
     "SELECT orders.id FROM orders WHERE NOT EXISTS (SELECT 1 FROM refunds WHERE refunds.order_id = orders.id)"),
    ("exists-star", "SELECT COUNT(*) FROM orders o WHERE NOT EXISTS (SELECT 1 FROM refunds r WHERE r.order_id = o.id)",
     "SELECT COUNT(*) FROM orders o WHERE NOT EXISTS (SELECT * FROM refunds r WHERE r.order_id = o.id)"),
    ("exists-and-order", "SELECT COUNT(*) FROM orders o WHERE EXISTS (SELECT 1 FROM orders c WHERE c.parent_id = o.id "
                         "AND c.status = 'paid')",
     "SELECT COUNT(*) FROM orders AS m WHERE EXISTS (SELECT 1 FROM orders AS k WHERE k.status = 'paid' "
     "AND m.id = k.parent_id)"),
    ("exists-cte-vs-subq", "WITH v AS (SELECT id FROM users WHERE vip = 1) SELECT COUNT(*) FROM orders o "
                           "WHERE EXISTS (SELECT 1 FROM v WHERE v.id = o.user_id)",
     "SELECT COUNT(*) FROM orders o WHERE EXISTS (SELECT 1 FROM (SELECT id FROM users WHERE vip = 1) v "
     "WHERE v.id = o.user_id)"),
    ("exists-names-exchanged", "SELECT COUNT(*) FROM orders o WHERE EXISTS (SELECT 1 FROM orders o2 "
                               "WHERE o2.parent_id = o.id)",
     "SELECT COUNT(*) FROM orders o2 WHERE EXISTS (SELECT 1 FROM orders o WHERE o.parent_id = o2.id)"),
    ("exists-outer-by-name", "SELECT COUNT(*) FROM orders WHERE EXISTS (SELECT 1 FROM orders c WHERE c.parent_id = orders.id)",
     "SELECT COUNT(*) FROM orders o WHERE EXISTS (SELECT 1 FROM orders c WHERE c.parent_id = o.id)"),
    ("correlated-scalar", "SELECT COUNT(*) FROM orders o WHERE o.amount > (SELECT AVG(i.amount) FROM orders i "
                          "WHERE i.user_id = o.user_id)",
     "SELECT COUNT(*) FROM orders x WHERE (SELECT AVG(y.amount) FROM orders y WHERE x.user_id = y.user_id) < x.amount"),
    ("correlated-select-list", "SELECT o.id, (SELECT COUNT(*) FROM refunds r WHERE r.order_id = o.id) AS n FROM orders o",
     "SELECT x.id, (SELECT COUNT(*) FROM refunds y WHERE x.id = y.order_id) AS cnt FROM orders x"),
    ("scalar-vs-cte", "SELECT COUNT(*) FROM orders WHERE amount > (SELECT AVG(amount) FROM orders)",
     "WITH a AS (SELECT AVG(amount) AS m FROM orders) SELECT COUNT(*) FROM orders WHERE amount > (SELECT m FROM a)"),
    ("in-outer-and-sub", "SELECT COUNT(*) FROM orders o WHERE o.status = 'paid' AND o.user_id IN "
                         "(SELECT p.user_id FROM orders p WHERE p.status = 'paid' AND p.amount > 50)",
     "SELECT COUNT(*) FROM orders a WHERE a.user_id IN (SELECT b.user_id FROM orders b WHERE b.amount > 50 "
     "AND b.status = 'paid') AND a.status = 'paid'"),
    ("cte-exists-renamed", "WITH p AS (SELECT * FROM orders WHERE status = 'paid') SELECT COUNT(*) FROM p "
                           "WHERE EXISTS (SELECT 1 FROM p p2 WHERE p2.parent_id = p.id)",
     "WITH q AS (SELECT * FROM orders WHERE status = 'paid') SELECT COUNT(*) FROM q AS m WHERE EXISTS "
     "(SELECT 1 FROM q AS c WHERE c.parent_id = m.id)"),
    ("cte-correlated-vs-subq", "WITH t AS (SELECT o.* FROM orders o WHERE EXISTS (SELECT 1 FROM refunds r "
                               "WHERE r.order_id = o.id)) SELECT COUNT(*) FROM t",
     "SELECT COUNT(*) FROM (SELECT x.* FROM orders x WHERE EXISTS (SELECT 1 FROM refunds y WHERE y.order_id = x.id)) AS q"),
    ("cte-in-and-in", "WITH p AS (SELECT * FROM orders WHERE status = 'paid') SELECT SUM(p.amount) FROM p "
                      "WHERE p.user_id IN (SELECT user_id FROM orders WHERE amount > 50)",
     "SELECT SUM(o.amount) FROM orders o WHERE o.status = 'paid' AND o.user_id IN (SELECT user_id FROM orders "
     "WHERE amount > 50)"),
    ("subq-from-subq-alias", "SELECT SUM(o.amount) FROM orders o WHERE o.user_id IN (SELECT s.user_id FROM "
                             "(SELECT user_id, SUM(amount) AS total FROM payments GROUP BY user_id) s WHERE s.total > 100)",
     "SELECT SUM(o.amount) FROM orders o WHERE o.user_id IN (SELECT agg.user_id FROM "
     "(SELECT user_id, SUM(amount) AS total FROM payments GROUP BY user_id) agg WHERE agg.total > 100)"),
    # ---- UNION
    ("union-branch-order", "SELECT user_id, amount FROM orders WHERE amount > 1 UNION ALL SELECT user_id, amount FROM refunds",
     "SELECT user_id, amount FROM refunds UNION ALL SELECT user_id, amount FROM orders WHERE 1 < amount"),
    ("union-wrapped", "SELECT user_id FROM orders UNION SELECT user_id FROM refunds",
     "SELECT * FROM (SELECT user_id FROM orders UNION SELECT user_id FROM refunds) t"),
    ("union-cte-vs-subq", "WITH t AS (SELECT user_id FROM orders UNION ALL SELECT user_id FROM refunds) "
                          "SELECT COUNT(*) FROM t",
     "SELECT COUNT(*) FROM (SELECT user_id FROM orders UNION ALL SELECT user_id FROM refunds) AS q"),
    ("union-aliases", "SELECT o.user_id, o.amount FROM orders o WHERE o.status = 'paid' UNION ALL "
                      "SELECT r.user_id, -r.amount FROM refunds r",
     "SELECT x.user_id, x.amount FROM orders x WHERE x.status = 'paid' UNION ALL SELECT y.user_id, -y.amount FROM refunds y"),
    ("union-output-alias", "SELECT user_id AS u FROM orders UNION SELECT user_id FROM refunds",
     "SELECT user_id AS uid FROM orders UNION SELECT user_id FROM refunds"),
    ("union-cte-filter", "WITH p AS (SELECT * FROM orders WHERE status = 'paid') SELECT user_id FROM p WHERE amount > 10 "
                         "UNION ALL SELECT user_id FROM p WHERE amount < 0",
     "SELECT user_id FROM orders WHERE status = 'paid' AND amount > 10 UNION ALL "
     "SELECT user_id FROM orders WHERE status = 'paid' AND amount < 0"),
    ("archive-union-reorder", "SELECT id, amount FROM orders UNION SELECT id, amount FROM orders_archive",
     "SELECT id, amount FROM orders_archive UNION SELECT id, amount FROM orders"),
    # ---- 过滤挪了一层、分组键上的条件
    ("group-key-filter-moved", "WITH t AS (SELECT user_id, SUM(amount) AS total FROM orders WHERE user_id > 3 "
                               "GROUP BY user_id) SELECT COUNT(*) FROM t WHERE total > 100",
     "WITH t AS (SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id) "
     "SELECT COUNT(*) FROM t AS z WHERE 100 < z.total AND z.user_id > 3"),
    ("preserved-side-constant", "SELECT d.flag, s.id FROM (SELECT id, 1 AS flag FROM users) d LEFT JOIN stores s "
                                "ON s.owner_id = d.id",
     "SELECT 1 AS flag, s.id FROM users u LEFT JOIN stores s ON s.owner_id = u.id"),
    ("orders-items-join", "SELECT o.id, SUM(i.qty * i.price) FROM orders o JOIN order_items i ON i.order_id = o.id "
                          "GROUP BY o.id",
     "SELECT x.id, SUM(y.price * y.qty) FROM order_items y JOIN orders x ON x.id = y.order_id GROUP BY x.id"),
    ("payments-left", "SELECT o.id, COALESCE(SUM(p.amount), 0) FROM orders o LEFT JOIN payments p ON p.order_id = o.id "
                      "GROUP BY o.id",
     "SELECT a.id, COALESCE(SUM(b.amount), 0) FROM orders a LEFT JOIN payments b ON a.id = b.order_id GROUP BY a.id"),
    ("shipments-exists", "SELECT COUNT(*) FROM orders o WHERE EXISTS (SELECT 1 FROM shipments s WHERE s.order_id = o.id "
                         "AND s.carrier = 'sf')",
     "SELECT COUNT(*) FROM orders o WHERE EXISTS (SELECT * FROM shipments AS t WHERE 'sf' = t.carrier "
     "AND o.id = t.order_id)"),
]

_DIFF = [
    # ---- 时间列、时间范围
    ("time-column", "SELECT SUM(amount) FROM orders WHERE paid_at >= '2026-09-01' AND paid_at < '2026-10-01'",
     "SELECT SUM(amount) FROM orders WHERE created_at >= '2026-09-01' AND created_at < '2026-10-01'"),
    ("time-one-side", "SELECT SUM(amount) FROM orders WHERE paid_at >= '2026-09-01' AND paid_at < '2026-10-01'",
     "SELECT SUM(amount) FROM orders"),
    ("time-range-end", "SELECT SUM(amount) FROM orders WHERE paid_at >= '2026-09-01' AND paid_at < '2026-10-01'",
     "SELECT SUM(amount) FROM orders WHERE paid_at BETWEEN '2026-09-01' AND '2026-09-30'"),
    ("time-range-op", "SELECT COUNT(*) FROM orders WHERE paid_at >= '2026-09-01'",
     "SELECT COUNT(*) FROM orders WHERE paid_at > '2026-09-01'"),
    ("time-in-cte", "WITH t AS (SELECT DATE(paid_at) AS d, amount FROM orders) SELECT SUM(amount) FROM t "
                    "WHERE d >= '2026-09-01'",
     "WITH t AS (SELECT DATE(created_at) AS d, amount FROM orders) SELECT SUM(amount) FROM t WHERE d >= '2026-09-01'"),
    ("time-date-fn", "SELECT COUNT(*) FROM orders WHERE DATE(paid_at) = '2026-09-01'",
     "SELECT COUNT(*) FROM orders WHERE paid_at = '2026-09-01'"),
    # ---- 有效行过滤
    ("filter-deleted", "SELECT SUM(amount) FROM orders WHERE is_deleted = 0 AND paid_at >= '2026-09-01'",
     "SELECT SUM(amount) FROM orders WHERE paid_at >= '2026-09-01'"),
    ("filter-exclusion", "SELECT SUM(amount) FROM orders WHERE is_deleted <> 1", "SELECT SUM(amount) FROM orders"),
    ("filter-status-value", "SELECT SUM(amount) FROM orders WHERE status = 'paid'",
     "SELECT SUM(amount) FROM orders WHERE status = 'done'"),
    ("filter-status-in", "SELECT SUM(amount) FROM orders WHERE status = 'paid'",
     "SELECT SUM(amount) FROM orders WHERE status IN ('paid', 'shipped')"),
    ("filter-not-vs-neq-null", "SELECT COUNT(*) FROM orders WHERE status <> 'void'",
     "SELECT COUNT(*) FROM orders WHERE status <> 'void' OR status IS NULL"),
    ("filter-threshold", "SELECT COUNT(*) FROM orders WHERE amount > 100", "SELECT COUNT(*) FROM orders WHERE amount >= 100"),
    ("filter-and-vs-or", "SELECT COUNT(*) FROM orders WHERE status = 'paid' AND amount > 10",
     "SELECT COUNT(*) FROM orders WHERE status = 'paid' OR amount > 10"),
    ("filter-in-cte-dropped", "WITH v AS (SELECT * FROM orders WHERE is_deleted = 0) SELECT COUNT(DISTINCT v.user_id) FROM v",
     "SELECT COUNT(DISTINCT user_id) FROM orders"),
    ("filter-cte-unused", "WITH p AS (SELECT * FROM orders WHERE status = 'paid') SELECT SUM(amount) FROM orders",
     "SELECT SUM(amount) FROM orders WHERE status = 'paid'"),
    ("filter-cte-status", "WITH p AS (SELECT * FROM orders WHERE status = 'paid') "
                          "SELECT store_id, SUM(amount) FROM p GROUP BY store_id",
     "WITH p AS (SELECT * FROM orders WHERE status = 'open') SELECT store_id, SUM(amount) FROM p GROUP BY store_id"),
    ("in-cte-regression", "WITH p AS (SELECT * FROM orders WHERE status = 'paid') "
                          "SELECT COUNT(*) FROM users u WHERE u.id IN (SELECT user_id FROM p)",
     "WITH p AS (SELECT * FROM orders WHERE status = 'open') "
     "SELECT COUNT(*) FROM users u WHERE u.id IN (SELECT user_id FROM p)"),
    ("exists-cte-regression", "WITH p AS (SELECT * FROM orders WHERE status = 'paid') "
                              "SELECT COUNT(*) FROM users u WHERE EXISTS (SELECT 1 FROM p WHERE p.user_id = u.id)",
     "WITH p AS (SELECT * FROM orders WHERE status = 'open') "
     "SELECT COUNT(*) FROM users u WHERE EXISTS (SELECT 1 FROM p WHERE p.user_id = u.id)"),
    ("scalar-cte-regression", "WITH p AS (SELECT * FROM orders WHERE status = 'paid') "
                              "SELECT COUNT(*) FROM users u WHERE (SELECT COUNT(*) FROM p WHERE p.user_id = u.id) > 0",
     "WITH p AS (SELECT * FROM orders WHERE status = 'open') "
     "SELECT COUNT(*) FROM users u WHERE (SELECT COUNT(*) FROM p WHERE p.user_id = u.id) > 0"),
    ("in-subquery-column", "SELECT SUM(o.amount) FROM orders o WHERE o.user_id IN (SELECT u.id FROM users u WHERE u.vip = 1)",
     "SELECT SUM(o.amount) FROM orders o WHERE o.user_id IN (SELECT u.referrer_id FROM users u WHERE u.vip = 1)"),
    ("in-subquery-filter", "SELECT COUNT(*) FROM orders WHERE user_id IN (SELECT id FROM users WHERE vip = 1)",
     "SELECT COUNT(*) FROM orders WHERE user_id IN (SELECT id FROM users)"),
    ("not-in-vs-not-exists", "SELECT COUNT(*) FROM orders o WHERE o.id NOT IN (SELECT order_id FROM refunds)",
     "SELECT COUNT(*) FROM orders o WHERE NOT EXISTS (SELECT 1 FROM refunds r WHERE r.order_id = o.id)"),
    ("in-vs-not-in", "SELECT COUNT(*) FROM orders WHERE user_id IN (SELECT user_id FROM refunds)",
     "SELECT COUNT(*) FROM orders WHERE user_id NOT IN (SELECT user_id FROM refunds)"),
    ("exists-child-vs-parent", "SELECT COUNT(*) FROM orders o WHERE EXISTS (SELECT 1 FROM orders c WHERE c.parent_id = o.id)",
     "SELECT COUNT(*) FROM orders o WHERE EXISTS (SELECT 1 FROM orders c WHERE c.id = o.parent_id)"),
    ("correlated-earlier", "SELECT COUNT(*) FROM orders o WHERE EXISTS (SELECT 1 FROM orders p "
                           "WHERE p.user_id = o.user_id AND p.created_at < o.created_at)",
     "SELECT COUNT(*) FROM orders o WHERE EXISTS (SELECT 1 FROM orders p WHERE p.user_id = o.user_id "
     "AND p.created_at > o.created_at)"),
    ("scalar-subquery-filter", "SELECT SUM(amount) FROM orders WHERE amount > (SELECT AVG(amount) FROM orders "
                               "WHERE is_deleted = 0)",
     "SELECT SUM(amount) FROM orders WHERE amount > (SELECT AVG(amount) FROM orders)"),
    ("unnest-or-trap", "SELECT id FROM orders WHERE status = 'paid' OR amount > (SELECT amount FROM refunds WHERE id = 999999)",
     "SELECT orders.id FROM orders CROSS JOIN (SELECT amount FROM refunds WHERE id = 999999) AS q "
     "WHERE orders.status = 'paid' OR orders.amount > q.amount"),
    ("unnest-scalar-trap", "SELECT id FROM orders WHERE amount > (SELECT amount FROM refunds ORDER BY id LIMIT 1)",
     "SELECT orders.id FROM orders CROSS JOIN (SELECT amount FROM refunds) AS q WHERE orders.amount > q.amount"),
    # ---- 聚合、去重
    ("count-distinct", "SELECT COUNT(DISTINCT user_id) FROM orders", "SELECT COUNT(user_id) FROM orders"),
    ("count-star-vs-col", "SELECT COUNT(*) FROM orders", "SELECT COUNT(user_id) FROM orders"),
    ("count-star-vs-distinct", "SELECT COUNT(*) FROM orders", "SELECT COUNT(DISTINCT id) FROM orders WHERE amount > 0"),
    ("sum-vs-avg", "SELECT SUM(amount) FROM orders", "SELECT AVG(amount) FROM orders"),
    ("sum-column", "SELECT SUM(amount) FROM orders", "SELECT SUM(discount) FROM orders"),
    ("sum-vs-coalesce", "SELECT SUM(amount) FROM refunds", "SELECT COALESCE(SUM(amount), 0) FROM refunds"),
    ("max-vs-min", "SELECT user_id, MAX(paid_at) FROM orders GROUP BY user_id",
     "SELECT user_id, MIN(paid_at) FROM orders GROUP BY user_id"),
    ("expression-factor", "WITH t AS (SELECT id, amount * 0.5 AS net FROM orders) SELECT SUM(net) FROM t",
     "WITH t AS (SELECT id, amount * 0.25 AS net FROM orders) SELECT SUM(net) FROM t"),
    ("expression-swap-sub", PR + "SELECT SUM(p.total) - SUM(r.total) FROM paid p JOIN refunded r ON r.user_id = p.user_id",
     PR + "SELECT SUM(r.total) - SUM(p.total) FROM paid p JOIN refunded r ON r.user_id = p.user_id"),
    ("cte-which-total", PR + "SELECT COUNT(*) FROM paid p JOIN refunded r ON r.user_id = p.user_id WHERE p.total > 100",
     PR + "SELECT COUNT(*) FROM paid p JOIN refunded r ON r.user_id = p.user_id WHERE r.total > 100"),
    ("cte-swapped-defs", PR + "SELECT SUM(p.total - r.total) FROM paid p JOIN refunded r ON r.user_id = p.user_id",
     "WITH paid AS (SELECT user_id, SUM(amount) AS total FROM refunds GROUP BY user_id), "
     "refunded AS (SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id) "
     "SELECT SUM(p.total - r.total) FROM paid p JOIN refunded r ON r.user_id = p.user_id"),
    ("distinct-dropped", "SELECT DISTINCT user_id FROM orders", "SELECT user_id FROM orders"),
    ("distinct-in-subq", "SELECT COUNT(*) FROM (SELECT DISTINCT user_id FROM orders) t",
     "SELECT COUNT(*) FROM (SELECT user_id FROM orders) t"),
    ("count-distinct-vs-subq", "SELECT COUNT(DISTINCT user_id) FROM orders",
     "SELECT COUNT(*) FROM (SELECT DISTINCT user_id FROM orders) t"),
    ("window-partition", "SELECT id, SUM(amount) OVER (PARTITION BY user_id) AS s FROM orders",
     "SELECT id, SUM(amount) OVER (PARTITION BY store_id) AS s FROM orders"),
    ("window-rank-vs-rownum", "SELECT id, RANK() OVER (ORDER BY amount) AS r FROM orders",
     "SELECT id, ROW_NUMBER() OVER (ORDER BY amount, id) AS r FROM orders"),
    ("window-filter-stage", "SELECT COUNT(*) FROM (SELECT id, status, ROW_NUMBER() OVER (PARTITION BY user_id "
                            "ORDER BY paid_at, id) AS rn FROM orders) t WHERE t.rn = 1 AND t.status = 'paid'",
     "SELECT COUNT(*) FROM (SELECT id, status, ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY paid_at, id) AS rn "
     "FROM orders WHERE status = 'paid') t WHERE t.rn = 1"),
    ("limit-filter-stage", "SELECT COUNT(*) FROM (SELECT * FROM orders ORDER BY id LIMIT 5) t WHERE t.status = 'paid'",
     "SELECT COUNT(*) FROM (SELECT * FROM orders WHERE status = 'paid' ORDER BY id LIMIT 5) t"),
    ("case-default", "SELECT SUM(CASE WHEN status = 'paid' THEN amount ELSE 0 END) FROM orders",
     "SELECT SUM(CASE WHEN status = 'paid' THEN amount END) FROM orders"),
    ("output-columns-order", "SELECT user_id, SUM(amount) FROM orders GROUP BY user_id",
     "SELECT SUM(amount), user_id FROM orders GROUP BY user_id"),
    ("output-extra-column", "SELECT user_id, SUM(amount) FROM orders GROUP BY user_id",
     "SELECT user_id, SUM(amount), COUNT(*) FROM orders GROUP BY user_id"),
    # ---- 分组、HAVING、取行
    ("group-key", "SELECT region, SUM(amount) FROM orders GROUP BY region",
     "SELECT region, status, SUM(amount) FROM orders GROUP BY region, status"),
    ("group-none", "SELECT SUM(amount) FROM orders", "SELECT region, SUM(amount) FROM orders GROUP BY region"),
    ("group-grain", "SELECT COUNT(*) FROM (SELECT user_id FROM orders GROUP BY user_id) t",
     "SELECT user_id, COUNT(*) FROM orders GROUP BY user_id"),
    ("having-dropped", "SELECT region, SUM(amount) FROM orders GROUP BY region HAVING SUM(amount) > 100",
     "SELECT region, SUM(amount) FROM orders GROUP BY region"),
    ("having-threshold", "SELECT store_id FROM orders GROUP BY store_id HAVING COUNT(*) > 2",
     "SELECT store_id FROM orders GROUP BY store_id HAVING COUNT(*) > 3"),
    ("limit-count", "SELECT id FROM orders ORDER BY id LIMIT 3", "SELECT id FROM orders ORDER BY id LIMIT 5"),
    ("limit-vs-none", "SELECT id FROM orders ORDER BY id LIMIT 3", "SELECT id FROM orders ORDER BY id"),
    ("limit-offset", "SELECT id FROM orders ORDER BY id LIMIT 3", "SELECT id FROM orders ORDER BY id LIMIT 3 OFFSET 2"),
    ("limit-order-dir", "SELECT id FROM orders ORDER BY amount DESC, id LIMIT 3",
     "SELECT id FROM orders ORDER BY amount, id LIMIT 3"),
    ("limit-in-cte", "WITH t AS (SELECT * FROM orders ORDER BY id LIMIT 5) SELECT SUM(amount) FROM t",
     "WITH t AS (SELECT * FROM orders ORDER BY id LIMIT 8) SELECT SUM(amount) FROM t"),
    ("order-only", "SELECT id, amount FROM orders ORDER BY amount DESC, id", "SELECT id, amount FROM orders"),
    # ---- 连接
    ("left-vs-inner", "SELECT SUM(o.amount) FROM orders o LEFT JOIN refunds r ON r.order_id = o.id",
     "SELECT SUM(o.amount) FROM orders o JOIN refunds r ON r.order_id = o.id"),
    ("left-join-key", "SELECT SUM(o.amount) FROM orders o LEFT JOIN refunds r ON r.order_id = o.id",
     "SELECT SUM(o.amount) FROM orders o LEFT JOIN refunds r ON r.user_id = o.user_id"),
    ("join-dropped", "SELECT SUM(o.amount) FROM orders o LEFT JOIN refunds r ON r.order_id = o.id",
     "SELECT SUM(amount) FROM orders"),
    ("inner-join-added", "SELECT SUM(o.amount) FROM orders o JOIN stores s ON s.id = o.store_id",
     "SELECT SUM(amount) FROM orders"),
    ("join-filter-city", "SELECT SUM(o.amount) FROM orders o JOIN stores s ON s.id = o.store_id WHERE s.city = 'SH'",
     "SELECT SUM(o.amount) FROM orders o JOIN stores s ON s.id = o.store_id WHERE s.city = 'BJ'"),
    ("left-keep-side", "SELECT COUNT(*) FROM orders o LEFT JOIN refunds r ON r.order_id = o.id",
     "SELECT COUNT(*) FROM refunds r LEFT JOIN orders o ON r.order_id = o.id"),
    ("right-keep-side", "SELECT COUNT(*) FROM orders o LEFT JOIN refunds r ON r.order_id = o.id",
     "SELECT COUNT(*) FROM orders o RIGHT JOIN refunds r ON r.order_id = o.id"),
    ("left-on-vs-where", "SELECT COUNT(*) FROM orders o LEFT JOIN refunds r ON r.order_id = o.id AND r.state = 'done'",
     "SELECT COUNT(*) FROM orders o LEFT JOIN refunds r ON r.order_id = o.id WHERE r.state = 'done'"),
    ("left-is-null-not-rejected", "SELECT COUNT(*) FROM orders o LEFT JOIN refunds r ON r.order_id = o.id "
                                  "WHERE r.amount > 5 OR r.id IS NULL",
     "SELECT COUNT(*) FROM orders o JOIN refunds r ON r.order_id = o.id WHERE r.amount > 5 OR r.id IS NULL"),
    ("left-coalesce-not-rejected", "SELECT COUNT(*) FROM orders o LEFT JOIN refunds r ON r.order_id = o.id "
                                   "WHERE COALESCE(r.amount, 0) = 0",
     "SELECT COUNT(*) FROM orders o JOIN refunds r ON r.order_id = o.id WHERE COALESCE(r.amount, 0) = 0"),
    ("nullable-side-constant", "SELECT o.id, r.flag FROM orders o LEFT JOIN (SELECT order_id, 1 AS flag FROM refunds) r "
                               "ON r.order_id = o.id",
     "SELECT o.id, 1 AS flag FROM orders o LEFT JOIN refunds r ON r.order_id = o.id"),
    ("nullable-side-coalesce", "SELECT o.id, r.a FROM orders o LEFT JOIN (SELECT order_id, COALESCE(amount, 0) AS a "
                               "FROM refunds) r ON r.order_id = o.id",
     "SELECT o.id, COALESCE(r.amount, 0) AS a FROM orders o LEFT JOIN refunds r ON r.order_id = o.id"),
    ("nullable-nested-constant", "SELECT o.id, d.flag FROM orders o LEFT JOIN (SELECT x.order_id, x.flag FROM "
                                 "(SELECT order_id, 1 AS flag FROM refunds) x) d ON d.order_id = o.id",
     "SELECT o.id, 1 AS flag FROM orders o LEFT JOIN refunds r ON r.order_id = o.id"),
    ("right-side-constant", "SELECT d.flag, s.id FROM (SELECT id, 1 AS flag FROM users) d RIGHT JOIN stores s "
                            "ON s.owner_id = d.id",
     "SELECT 1 AS flag, s.id FROM users u RIGHT JOIN stores s ON s.owner_id = u.id"),
    ("full-side-constant", "SELECT d.flag, s.id FROM (SELECT id, 1 AS flag FROM users) d FULL JOIN stores s "
                           "ON s.owner_id = d.id",
     "SELECT 1 AS flag, s.id FROM users u FULL JOIN stores s ON s.owner_id = u.id"),
    ("preserved-side-constant", "SELECT d.flag, s.id FROM (SELECT id, 1 AS flag FROM users) d LEFT JOIN stores s "
                                "ON s.owner_id = d.id",
     "SELECT d.flag, s.id FROM (SELECT id, 2 AS flag FROM users) d LEFT JOIN stores s ON s.owner_id = d.id"),
    ("self-join-direction", "SELECT COUNT(*) FROM orders o JOIN orders c ON c.parent_id = o.id WHERE c.amount > o.amount",
     "SELECT COUNT(*) FROM orders x JOIN orders y ON x.id = y.parent_id WHERE x.amount > y.amount"),
    ("self-join-roles", "SELECT COUNT(*) FROM users a JOIN users b ON b.id = a.referrer_id WHERE a.vip = 1",
     "SELECT COUNT(*) FROM users a JOIN users b ON b.id = a.referrer_id WHERE b.vip = 1"),
    ("table-swap", "SELECT SUM(amount) FROM orders WHERE is_deleted = 0 AND paid_at >= '2026-09-01'",
     "SELECT SUM(amount) FROM orders_archive WHERE is_deleted = 0 AND paid_at >= '2026-09-01'"),
    ("two-columns-same-name", "SELECT SUM(o.amount) FROM orders o JOIN refunds r ON r.order_id = o.id",
     "SELECT SUM(r.amount) FROM orders o JOIN refunds r ON r.order_id = o.id"),
    ("join-multiplies", "SELECT SUM(o.amount) FROM orders o WHERE EXISTS (SELECT 1 FROM refunds r WHERE r.order_id = o.id)",
     "SELECT SUM(o.amount) FROM orders o JOIN refunds r ON r.order_id = o.id"),
    ("passthrough-which-cte", "WITH paid AS (SELECT * FROM orders WHERE status = 'paid'), open AS (SELECT * FROM orders "
                              "WHERE status = 'open') SELECT SUM(p.amount) FROM paid p JOIN open o ON o.user_id = p.user_id",
     "WITH paid AS (SELECT * FROM orders WHERE status = 'paid'), open AS (SELECT * FROM orders WHERE status = 'open') "
     "SELECT SUM(o.amount) FROM paid p JOIN open o ON o.user_id = p.user_id"),
    ("cte-self-join-direction", "WITH t AS (SELECT user_id, referrer_id, SUM(amount) AS total FROM orders "
                                "GROUP BY user_id, referrer_id) SELECT COUNT(*) FROM t a JOIN t b "
                                "ON b.user_id = a.referrer_id WHERE a.total > b.total",
     "WITH t AS (SELECT user_id, referrer_id, SUM(amount) AS total FROM orders GROUP BY user_id, referrer_id) "
     "SELECT COUNT(*) FROM t a JOIN t b ON b.user_id = a.referrer_id WHERE b.total > a.total"),
    ("extra-cte-join", "WITH t AS (SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id) "
                       "SELECT COUNT(*) FROM t WHERE total > 100",
     "WITH vip AS (SELECT id FROM users WHERE vip = 1), t AS (SELECT user_id, SUM(amount) AS total FROM orders "
     "GROUP BY user_id) SELECT COUNT(*) FROM vip JOIN t ON t.user_id = vip.id WHERE t.total > 100"),
    # ---- UNION
    ("union-vs-union-all", "SELECT user_id FROM orders UNION SELECT user_id FROM refunds",
     "SELECT user_id FROM orders UNION ALL SELECT user_id FROM refunds"),
    ("union-branch-filter", "SELECT user_id FROM orders WHERE status = 'paid' UNION ALL SELECT user_id FROM refunds",
     "SELECT user_id FROM orders UNION ALL SELECT user_id FROM refunds"),
    ("union-branch-dup", "SELECT user_id FROM orders UNION ALL SELECT user_id FROM refunds",
     "SELECT user_id FROM orders UNION ALL SELECT user_id FROM refunds UNION ALL SELECT user_id FROM refunds"),
    ("union-distinct-branch", "SELECT DISTINCT user_id FROM orders UNION ALL SELECT user_id FROM refunds",
     "SELECT user_id FROM orders UNION ALL SELECT user_id FROM refunds"),
    ("except-order", "SELECT user_id FROM orders EXCEPT SELECT user_id FROM refunds",
     "SELECT user_id FROM refunds EXCEPT SELECT user_id FROM orders"),
    ("intersect-vs-union", "SELECT user_id FROM orders INTERSECT SELECT user_id FROM refunds",
     "SELECT user_id FROM orders UNION SELECT user_id FROM refunds"),
    # ---- 写法上看着一样
    ("in-vs-exists-null", "SELECT COUNT(*) FROM users u WHERE u.id NOT IN (SELECT user_id FROM orders)",
     "SELECT COUNT(*) FROM users u WHERE NOT EXISTS (SELECT 1 FROM orders o WHERE o.user_id = u.id)"),
    ("coalesce-filter", "SELECT COUNT(*) FROM orders WHERE COALESCE(is_deleted, 0) = 0",
     "SELECT COUNT(*) FROM orders WHERE is_deleted = 0"),
    ("integer-division", "SELECT SUM(amount) / COUNT(*) FROM orders", "SELECT AVG(amount) FROM orders"),
    ("cast-date-sqlite", "SELECT COUNT(*) FROM orders WHERE paid_at >= '2026-09-01'",
     "SELECT COUNT(*) FROM orders WHERE paid_at >= CAST('2026-09-01' AS DATE)"),
    ("string-compare-redundant", "SELECT COUNT(*) FROM orders WHERE status > 'b' AND status > 'o'",
     "SELECT COUNT(*) FROM orders WHERE status > 'o'"),
    ("text-number-compare", "SELECT COUNT(*) FROM orders WHERE region > 10 AND region > 9",
     "SELECT COUNT(*) FROM orders WHERE region > 10"),
    ("arith-equality", "SELECT COUNT(*) FROM orders WHERE amount + 1 = 101", "SELECT COUNT(*) FROM orders WHERE amount = 100"),
    # ---- 别名和列同名（压测撞到的）：SELECT 列表里的名字指表的列，不指前面列的别名；去重的 UNION 留先出现的值
    ("select-alias-shadows-column", "SELECT user_id AS amount, amount * 2 AS twice FROM orders",
     "SELECT user_id AS amount, user_id * 2 AS twice FROM orders"),
    ("select-alias-shadows-column-2", "SELECT store_id AS region, region FROM orders WHERE status = 'paid'",
     "SELECT store_id AS region, store_id FROM orders WHERE status = 'paid'"),
    ("having-alias-shadows-column", "SELECT user_id, SUM(amount) AS amount FROM orders GROUP BY user_id "
     "HAVING SUM(amount) > 10",
     "SELECT user_id, SUM(amount) AS amount FROM orders GROUP BY user_id HAVING amount > 10"),
    ("union-dedup-branch-order", "SELECT tax FROM orders WHERE status = 'paid' UNION SELECT referrer_id FROM orders",
     "SELECT referrer_id FROM orders UNION SELECT tax FROM orders WHERE status = 'paid'"),
]


# ---------------------------------------------------------------------------- 过去的反例
# 下面三份是从旧的 tests/test_sql_semantics.py（五轮复核修正里按复验报告补的用例）和复验者留在 /private/tmp 的
# 脚本里收集来的 SQL 对，原样收进来：名字是出处（旧测试的函数名、复验脚本:用例名）。

#: 旧测试里两边写法不同、意思一样的对（各方言），新做法认得出是等价的
DIALECT_SAME: list[tuple[str, str, str, str]] = [
    ("names_given_to_derived_tables_are_no_difference", "mysql",
     "SELECT SUM(o.amount) FROM orders o JOIN (SELECT order_id, COUNT(*) AS n FROM refunds GROUP BY order_id) r ON r.order_id = o.id WHERE r.n > 1",
     "SELECT SUM(o.amount) FROM orders o JOIN (SELECT order_id, COUNT(*) AS n FROM refunds GROUP BY order_id) `退款` ON o.id = `退款`.order_id WHERE `退款`.n > 1"),
    ("derived_tables_reading_the_same_table_written_differently_ar", "mysql",
     "WITH paid AS (SELECT * FROM orders WHERE status = 'paid'), open AS (SELECT * FROM orders WHERE status = 'open') SELECT SUM(p.amount) FROM paid p JOIN open o ON o.user_id = p.user_id",
     "WITH settled AS (SELECT * FROM orders WHERE status = 'paid'), pending AS (SELECT * FROM orders WHERE status = 'open') SELECT SUM(s.amount) FROM settled s JOIN pending q ON q.user_id = s.user_id"),
    ("names_given_to_derived_tables_are_no_difference", "postgres",
     "SELECT SUM(amount) FROM orders WHERE user_id IN (WITH t AS (SELECT id FROM users WHERE grade >= 3) SELECT id FROM t)",
     "SELECT SUM(amount) FROM orders WHERE user_id IN (WITH g AS (SELECT id FROM users WHERE grade >= 3) SELECT g.id FROM g)"),
    ("the_same_output_computation_written_differently_is_no_differ", "postgres",
     "SELECT order_id, RANK() OVER (PARTITION BY user_id ORDER BY amount DESC) AS k FROM orders",
     "SELECT o.order_id, RANK() OVER (PARTITION BY o.user_id ORDER BY o.amount DESC) FROM orders o"),
    ("a_self_join_tells_its_two_aliases_apart", "postgres",
     "WITH base AS (SELECT user_id, referrer_id, SUM(amount) AS total FROM orders GROUP BY user_id, referrer_id) SELECT COUNT(*) FROM base b1 JOIN base b2 ON b2.user_id = b1.referrer_id WHERE b1.total > b2.total",
     "WITH base AS (SELECT user_id, referrer_id, SUM(amount) AS total FROM orders GROUP BY user_id, referrer_id) SELECT COUNT(*) FROM base y JOIN base x ON x.referrer_id = y.user_id WHERE y.total < x.total"),
    ("the_same_outer_join_written_differently_is_no_difference", "postgres",
     "WITH paid AS (SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id), refunded AS (SELECT user_id, SUM(amount) AS total FROM refunds GROUP BY user_id) SELECT COUNT(*) FROM paid p LEFT JOIN refunded r ON r.user_id = p.user_id",
     "WITH paid AS (SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id), refunded AS (SELECT user_id, SUM(amount) AS total FROM refunds GROUP BY user_id) SELECT COUNT(*) FROM refunded r RIGHT JOIN paid p ON p.user_id = r.user_id"),
    ("names_given_to_derived_tables_are_no_difference", "postgres",
     "WITH user_totals AS (SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id) SELECT COUNT(*) FROM user_totals WHERE total > 100",
     "SELECT COUNT(*) FROM (SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id) AS agg WHERE agg.total > 100"),
    ("the_same_rows_written_differently_are_no_difference", "sqlite",
     "SELECT COUNT(*) FROM (SELECT user_id FROM orders WHERE status = 'paid' UNION ALL SELECT referrer_id FROM orders WHERE status = 'paid') t",
     "WITH ids AS (SELECT user_id FROM orders WHERE status = 'paid' UNION ALL SELECT referrer_id FROM orders WHERE status = 'paid') SELECT COUNT(*) FROM ids"),
    ("swapping_the_order_of_an_inner_join_is_no_difference", "sqlite",
     "SELECT COUNT(*) FROM (SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id) p JOIN (SELECT user_id, SUM(amount) AS total FROM refunds GROUP BY user_id) r ON r.user_id = p.user_id WHERE p.total > r.total",
     "SELECT COUNT(*) FROM (SELECT user_id, SUM(amount) AS total FROM refunds GROUP BY user_id) r JOIN (SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id) p ON p.user_id = r.user_id WHERE p.total > r.total"),
    ("the_same_outer_join_written_differently_is_no_difference", "sqlite",
     "SELECT COUNT(*) FROM orders o LEFT JOIN refunds r ON r.order_id = o.id",
     "SELECT COUNT(*) FROM refunds r RIGHT JOIN orders o ON o.id = r.order_id"),
    ("the_same_outer_join_written_differently_is_no_difference", "sqlite",
     "SELECT COUNT(*) FROM orders o LEFT JOIN refunds r ON r.order_id = o.id LEFT JOIN users u ON u.id = o.user_id",
     "SELECT COUNT(*) FROM orders o LEFT JOIN users u ON u.id = o.user_id LEFT JOIN refunds r ON r.order_id = o.id"),
    ("the_same_outer_join_written_differently_is_no_difference", "sqlite",
     "SELECT COUNT(*) FROM orders o LEFT JOIN refunds r ON r.order_id = o.id WHERE o.status = 'paid'",
     "SELECT COUNT(*) FROM orders AS o LEFT OUTER JOIN refunds AS r ON o.id = r.order_id WHERE o.status = 'paid'"),
    ("the_same_outer_join_written_differently_is_no_difference", "sqlite",
     "SELECT COUNT(*) FROM orders o LEFT JOIN refunds r ON r.order_id = o.id WHERE r.state = 'done'",
     "SELECT COUNT(*) FROM orders o JOIN refunds r ON r.order_id = o.id WHERE r.state = 'done'"),
    ("the_same_correlated_subquery_written_differently_is_no_diffe", "sqlite",
     "SELECT COUNT(*) FROM orders o WHERE EXISTS (SELECT 1 FROM orders c WHERE c.parent_id = o.id)",
     "SELECT COUNT(*) FROM orders x WHERE EXISTS (SELECT 1 FROM orders y WHERE x.id = y.parent_id)"),
    ("the_same_correlated_subquery_written_differently_is_no_diffe", "sqlite",
     "SELECT COUNT(*) FROM orders o WHERE NOT EXISTS (SELECT 1 FROM refunds r WHERE r.order_id = o.id)",
     "SELECT COUNT(*) FROM orders WHERE NOT EXISTS (SELECT 1 FROM refunds WHERE refunds.order_id = orders.id)"),
    ("a_self_join_tells_its_two_aliases_apart", "sqlite",
     "SELECT COUNT(*) FROM orders o1 JOIN orders o2 ON o2.id = o1.parent_id WHERE o1.amount > o2.amount",
     "SELECT COUNT(*) FROM orders c JOIN orders p ON c.parent_id = p.id WHERE c.amount > p.amount"),
    ("the_same_item_on_equivalent_layers_is_no_difference", "sqlite",
     "SELECT SUM(amount) FROM orders WHERE status = 'paid' AND user_id IN (SELECT user_id FROM orders WHERE status = 'paid')",
     "SELECT SUM(o.amount) FROM orders o WHERE o.user_id IN (SELECT x.user_id FROM orders x WHERE x.status = 'paid') AND o.status = 'paid'"),
    ("swapping_the_order_of_an_inner_join_is_no_difference", "sqlite",
     "SELECT SUM(o.amount) FROM orders o JOIN refunds r ON r.order_id = o.id WHERE o.amount > r.amount",
     "SELECT SUM(o.amount) FROM refunds r JOIN orders o ON o.id = r.order_id WHERE o.amount > r.amount"),
    ("names_given_to_derived_tables_are_no_difference", "sqlite",
     "SELECT SUM(o.amount) FROM orders o WHERE o.amount > (SELECT AVG(o2.amount) AS avg_amount FROM orders o2 WHERE o2.is_deleted = 0)",
     "SELECT SUM(amount) FROM orders WHERE amount > (SELECT AVG(amount) FROM orders WHERE is_deleted = 0)"),
    ("the_same_meaning_written_differently_is_no_difference", "sqlite",
     "SELECT SUM(o.amount) FROM orders o WHERE o.paid_at >= '2026-09-01' AND o.is_deleted = 0",
     "select sum(amount) from orders where 0 = is_deleted and '2026-09-01' <= paid_at"),
    ("names_given_to_derived_tables_are_no_difference", "sqlite",
     "SELECT SUM(o.amount) FROM orders o WHERE o.user_id IN (SELECT s.user_id FROM (SELECT user_id, SUM(amount) AS total FROM payments GROUP BY user_id) s WHERE s.total > 100)",
     "SELECT SUM(o.amount) FROM orders o WHERE o.user_id IN (SELECT agg.user_id FROM (SELECT user_id, SUM(amount) AS total FROM payments GROUP BY user_id) agg WHERE agg.total > 100)"),
    ("names_given_to_derived_tables_are_no_difference", "sqlite",
     "SELECT SUM(o.amount) FROM orders o WHERE o.user_id IN (SELECT u.id AS uid FROM users u WHERE u.vip = 1)",
     "SELECT SUM(amount) FROM orders WHERE user_id IN (SELECT id FROM users WHERE vip = 1)"),
    ("the_same_meaning_written_differently_is_no_difference", "sqlite",
     "SELECT SUM(o.amount) FROM orders o WHERE o.user_id IN (SELECT u.id FROM users u WHERE u.vip = 1)",
     "SELECT SUM(amount) FROM orders WHERE user_id IN (SELECT id FROM users WHERE vip = 1)"),
    ("swapping_the_order_of_an_inner_join_is_no_difference", "sqlite",
     "SELECT SUM(o.amount) FROM orders o, refunds r WHERE r.order_id = o.id",
     "SELECT SUM(o.amount) FROM refunds r, orders o WHERE r.order_id = o.id"),
    ("the_same_meaning_written_differently_is_no_difference", "sqlite",
     "SELECT SUM(o.amount) FROM orders o, stores s WHERE o.store_id = s.id AND s.city = '虚构市'",
     "SELECT SUM(orders.amount) FROM orders INNER JOIN stores ON stores.id = orders.store_id WHERE stores.city = '虚构市'"),
    ("the_same_meaning_written_differently_is_no_difference", "sqlite",
     "SELECT amount FROM orders ORDER BY 1 DESC LIMIT 10 OFFSET 0",
     "SELECT o.amount FROM orders o ORDER BY o.amount DESC LIMIT 10"),
    ("the_same_meaning_written_differently_is_no_difference", "sqlite",
     "SELECT o.id FROM orders o WHERE NOT EXISTS (SELECT 1 FROM refunds r WHERE r.order_id = o.id)",
     "SELECT orders.id FROM orders WHERE NOT EXISTS (SELECT 1 FROM refunds WHERE refunds.order_id = orders.id)"),
    ("the_same_output_computation_written_differently_is_no_differ", "sqlite",
     "SELECT region, SUM(amount) * 1.0 / COUNT(*) AS avg_amt FROM orders GROUP BY region",
     "SELECT o.region, 1.0 * SUM(o.amount) / COUNT(*) FROM orders o GROUP BY 1"),
    ("the_same_rows_written_differently_are_no_difference", "sqlite",
     "SELECT region, SUM(amount) FROM orders GROUP BY region",
     "WITH t AS (SELECT region, SUM(amount) AS s FROM orders GROUP BY region) SELECT region, s FROM t"),
    ("the_same_rows_written_differently_are_no_difference", "sqlite",
     "SELECT user_id FROM orders GROUP BY user_id",
     "SELECT DISTINCT user_id FROM orders"),
    ("the_same_rows_written_differently_are_no_difference", "sqlite",
     "SELECT user_id, COUNT(*) FROM orders GROUP BY user_id",
     "SELECT user_id, n FROM (SELECT user_id, COUNT(*) AS n FROM orders GROUP BY user_id) t"),
    ("names_given_to_derived_tables_are_no_difference", "sqlite",
     "WITH base AS (SELECT * FROM orders WHERE is_deleted = 0), per_user AS (SELECT user_id, SUM(amount) AS total FROM base GROUP BY user_id) SELECT COUNT(*) FROM per_user WHERE total > 100",
     "WITH totals AS (SELECT user_id, SUM(amount) AS total FROM orders WHERE is_deleted = 0 GROUP BY user_id) SELECT COUNT(*) FROM totals WHERE total > 100"),
    ("names_given_to_derived_tables_are_no_difference", "sqlite",
     "WITH c AS (SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id), d AS (SELECT user_id, COUNT(*) AS n FROM refunds GROUP BY user_id), a AS (SELECT * FROM c WHERE total > 100), b AS (SELECT * FROM d WHERE n > 1) SELECT COUNT(*) FROM b JOIN a ON a.user_id = b.user_id",
     "SELECT COUNT(*) FROM (SELECT * FROM (SELECT user_id, COUNT(*) AS n FROM refunds GROUP BY user_id) x WHERE n > 1) b JOIN (SELECT * FROM (SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id) y WHERE total > 100) a ON a.user_id = b.user_id"),
    ("names_given_to_derived_tables_are_no_difference", "sqlite",
     "WITH c AS (SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id), d AS (SELECT user_id, SUM(amount) AS total FROM refunds GROUP BY user_id), a AS (SELECT * FROM c), b AS (SELECT * FROM d) SELECT COUNT(*) FROM b JOIN a ON a.user_id = b.user_id WHERE a.total > b.total",
     "SELECT COUNT(*) FROM (SELECT * FROM (SELECT user_id, SUM(amount) AS total FROM refunds GROUP BY user_id) x) b JOIN (SELECT * FROM (SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id) y) a ON a.user_id = b.user_id WHERE a.total > b.total"),
    ("derived_tables_reading_the_same_table_written_differently_ar", "sqlite",
     "WITH paid AS (SELECT * FROM orders WHERE status = 'paid') SELECT SUM(p.amount) FROM paid p JOIN refunds r ON r.order_id = p.id",
     "SELECT SUM(o.amount) FROM orders o JOIN refunds r ON r.order_id = o.id WHERE o.status = 'paid'"),
    ("derived_tables_reading_the_same_table_written_differently_ar", "sqlite",
     "WITH paid AS (SELECT * FROM orders WHERE status = 'paid'), open AS (SELECT * FROM orders WHERE status = 'open') SELECT COUNT(*) FROM paid p JOIN open o ON o.user_id = p.user_id WHERE p.amount > o.amount",
     "WITH paid AS (SELECT * FROM orders WHERE status = 'paid'), open AS (SELECT * FROM orders WHERE status = 'open') SELECT COUNT(*) FROM open o JOIN paid p ON p.user_id = o.user_id WHERE o.amount < p.amount"),
    ("swapping_the_order_of_an_inner_join_is_no_difference", "sqlite",
     "WITH paid AS (SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id), refunded AS (SELECT user_id, SUM(amount) AS total FROM refunds GROUP BY user_id) SELECT COUNT(*) FROM paid p JOIN refunded r ON r.user_id = p.user_id WHERE p.total > r.total",
     "WITH paid AS (SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id), refunded AS (SELECT user_id, SUM(amount) AS total FROM refunds GROUP BY user_id) SELECT COUNT(*) FROM refunded r JOIN paid p ON p.user_id = r.user_id WHERE p.total > r.total"),
    ("the_same_output_computation_written_differently_is_no_differ", "sqlite",
     "WITH paid AS (SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id), refunded AS (SELECT user_id, SUM(amount) AS total FROM refunds GROUP BY user_id) SELECT SUM(p.total) - SUM(r.total) FROM paid p JOIN refunded r ON r.user_id = p.user_id",
     "WITH paid AS (SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id), refunded AS (SELECT user_id, SUM(amount) AS total FROM refunds GROUP BY user_id) SELECT SUM(p.total) - SUM(r.total) FROM refunded r JOIN paid p ON p.user_id = r.user_id"),
    ("swapping_the_order_of_an_inner_join_is_no_difference", "sqlite",
     "WITH paid AS (SELECT user_id, SUM(amount) AS total FROM orders WHERE status = 'paid' GROUP BY user_id), open AS (SELECT user_id, SUM(amount) AS total FROM orders WHERE status = 'open' GROUP BY user_id) SELECT COUNT(*) FROM paid p JOIN open o ON o.user_id = p.user_id WHERE p.total > o.total",
     "WITH paid AS (SELECT user_id, SUM(amount) AS total FROM orders WHERE status = 'paid' GROUP BY user_id), open AS (SELECT user_id, SUM(amount) AS total FROM orders WHERE status = 'open' GROUP BY user_id) SELECT COUNT(*) FROM open o JOIN paid p ON p.user_id = o.user_id WHERE o.total < p.total"),
    ("names_given_to_derived_tables_are_no_difference", "sqlite",
     "WITH paid AS (SELECT user_id, SUM(amount) AS total FROM orders WHERE status = 'paid' GROUP BY user_id), refunded AS (SELECT user_id, SUM(amount) AS total FROM refunds GROUP BY user_id) SELECT COUNT(*) FROM paid p JOIN refunded r ON r.user_id = p.user_id WHERE p.total > r.total",
     "WITH r AS (SELECT user_id, SUM(amount) AS total FROM refunds GROUP BY user_id), p AS (SELECT user_id, SUM(amount) AS total FROM orders WHERE status = 'paid' GROUP BY user_id) SELECT COUNT(*) FROM p JOIN r ON p.user_id = r.user_id WHERE p.total > r.total"),
    ("filtering_at_an_equivalent_stage_is_no_difference", "sqlite",
     "WITH t AS (SELECT * FROM orders ORDER BY amount DESC LIMIT 10) SELECT SUM(amount) FROM t WHERE status = 'paid'",
     "WITH top10 AS (SELECT * FROM orders ORDER BY amount DESC LIMIT 10) SELECT SUM(t.amount) FROM top10 t WHERE t.status = 'paid'"),
    ("derived_tables_reading_the_same_table_written_differently_ar", "sqlite",
     "WITH t AS (SELECT * FROM orders WHERE status = 'paid') SELECT SUM(a.amount) FROM t a JOIN t b ON b.user_id = a.referrer_id",
     "WITH t AS (SELECT * FROM orders WHERE status = 'paid') SELECT SUM(y.amount) FROM t x JOIN t y ON y.referrer_id = x.user_id"),
    ("derived_columns_renamed_on_one_side_are_paired", "sqlite",
     "WITH t AS (SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id) SELECT COUNT(*) FROM t WHERE total > 100",
     "WITH x AS (SELECT user_id, SUM(amount) AS sum_amt FROM orders GROUP BY user_id) SELECT COUNT(*) FROM x WHERE sum_amt > 100"),
    ("a_shifted_number_inside_a_condition_subquery_is_no_differenc", "sqlite",
     "WITH t AS (SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id) SELECT COUNT(*) FROM users u WHERE u.vip = 1 AND u.id IN (SELECT user_id FROM t WHERE total > 100)",
     "WITH vip AS (SELECT id FROM users WHERE vip = 1), t AS (SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id) SELECT COUNT(*) FROM vip WHERE vip.id IN (SELECT user_id FROM t WHERE total > 100)"),
    ("swapped_join_order_of_two_derived_tables_is_no_difference", "sqlite",
     "WITH t AS (SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id), u AS (SELECT user_id, COUNT(*) AS cnt FROM refunds GROUP BY user_id) SELECT COUNT(*) FROM t JOIN u ON u.user_id = t.user_id WHERE t.total > u.cnt",
     "WITH t AS (SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id), u AS (SELECT user_id, COUNT(*) AS cnt FROM refunds GROUP BY user_id) SELECT COUNT(*) FROM u JOIN t ON t.user_id = u.user_id WHERE t.total > u.cnt"),
    ("derived_columns_renamed_on_one_side_are_paired", "sqlite",
     "WITH t AS (SELECT user_id, SUM(amount) AS total, COUNT(*) AS n FROM orders GROUP BY user_id) SELECT COUNT(*) FROM t WHERE total > 100 AND n > 1",
     "WITH t AS (SELECT user_id, SUM(amount) AS s, COUNT(*) AS c FROM orders GROUP BY user_id) SELECT COUNT(*) FROM t WHERE s > 100 AND c > 1"),
    ("the_same_derived_column_written_differently_is_no_difference", "sqlite",
     "WITH t AS (SELECT user_id, amount * 0.9 AS net FROM orders) SELECT SUM(net) FROM t",
     "WITH x AS (SELECT user_id, 0.9 * amount AS net FROM orders) SELECT SUM(x.net) FROM x"),
    ("derived_columns_renamed_on_one_side_are_paired", "sqlite",
     "WITH t AS (SELECT user_id, amount, DATE(paid_at) AS d FROM orders) SELECT SUM(amount) FROM t WHERE d >= '2026-09-01'",
     "WITH t AS (SELECT user_id, amount, DATE(paid_at) AS day FROM orders) SELECT SUM(amount) FROM t WHERE day >= '2026-09-01'"),
    ("the_same_derived_column_written_differently_is_no_difference", "sqlite",
     "WITH t AS (SELECT user_id, amount, paid_at AS ts FROM orders) SELECT SUM(amount) FROM t",
     "WITH t AS (SELECT user_id, amount, created_at AS ts FROM orders) SELECT SUM(amount) FROM t"),
    ("names_given_to_derived_tables_are_no_difference", "sqlite",
     "WITH user_totals AS (SELECT user_id, SUM(amount) AS total FROM orders WHERE is_deleted = 0 GROUP BY user_id) SELECT COUNT(*) FROM user_totals WHERE total > 100",
     "WITH t AS (SELECT user_id, SUM(amount) AS total FROM orders WHERE is_deleted = 0 GROUP BY user_id) SELECT COUNT(*) FROM t WHERE t.total > 100"),
    ("the_same_meaning_written_differently_is_no_difference", "sqlite",
     "WITH v AS (SELECT * FROM orders WHERE is_deleted = 0) SELECT COUNT(DISTINCT v.order_id) FROM v",
     "SELECT COUNT(DISTINCT order_id) FROM orders WHERE is_deleted = 0"),
    ("the_same_item_on_equivalent_layers_is_no_difference", "sqlite",
     "WITH v AS (SELECT * FROM orders WHERE is_deleted = 0) SELECT SUM(amount) FROM v WHERE is_deleted = 0",
     "SELECT SUM(amount) FROM orders WHERE is_deleted = 0"),
    ("names_given_to_derived_tables_are_no_difference", "sqlite",
     "WITH vip AS (SELECT id FROM users WHERE grade >= 3) SELECT SUM(amount) FROM orders WHERE user_id IN (SELECT id FROM vip)",
     "WITH good_users AS (SELECT id FROM users WHERE grade >= 3) SELECT SUM(amount) FROM orders WHERE user_id IN (SELECT g.id FROM good_users g)"),
]

#: 过去五轮复验、旧测试里报过的不同（各方言）：绝不能判等价
REGRESSIONS: list[tuple[str, str, str, str]] = [
    ("which_pass_through_derived_table_a_column_comes_from_is_repo", "mysql",
     "SELECT COUNT(*) FROM (SELECT * FROM orders WHERE status = 'paid') p JOIN (SELECT * FROM orders WHERE status = 'open') o ON o.user_id = p.user_id GROUP BY p.store_id",
     "SELECT COUNT(*) FROM (SELECT * FROM orders WHERE status = 'paid') p JOIN (SELECT * FROM orders WHERE status = 'open') o ON o.user_id = p.user_id GROUP BY o.store_id"),
    ("which_pass_through_derived_table_a_column_comes_from_is_repo", "mysql",
     "SELECT COUNT(*) FROM (SELECT * FROM orders WHERE status = 'paid') p JOIN (SELECT * FROM orders WHERE status = 'open') o ON o.user_id = p.user_id WHERE p.amount > 100",
     "SELECT COUNT(*) FROM (SELECT * FROM orders WHERE status = 'paid') p JOIN (SELECT * FROM orders WHERE status = 'open') o ON o.user_id = p.user_id WHERE o.amount > 100"),
    ("which_pass_through_derived_table_a_column_comes_from_is_repo", "mysql",
     "SELECT COUNT(*) FROM (SELECT * FROM orders WHERE status = 'paid') p JOIN (SELECT * FROM orders WHERE status = 'open') o ON o.user_id = p.user_id WHERE p.created_at >= '2026-09-01'",
     "SELECT COUNT(*) FROM (SELECT * FROM orders WHERE status = 'paid') p JOIN (SELECT * FROM orders WHERE status = 'open') o ON o.user_id = p.user_id WHERE o.created_at >= '2026-09-01'"),
    ("dedup_union_and_how_many_rows_come_out_are_compared", "mysql",
     "SELECT COUNT(*) FROM (SELECT DISTINCT user_id FROM orders WHERE status = 'paid') t",
     "SELECT COUNT(*) FROM (SELECT user_id FROM orders WHERE status = 'paid') t"),
    ("dedup_union_and_how_many_rows_come_out_are_compared", "mysql",
     "SELECT COUNT(*) FROM (SELECT DISTINCT user_id FROM orders WHERE status = 'paid') t",
     "SELECT COUNT(*) FROM orders WHERE status = 'paid'"),
    ("dedup_union_and_how_many_rows_come_out_are_compared", "mysql",
     "SELECT COUNT(*) FROM (SELECT id FROM orders WHERE status = 'paid' UNION ALL SELECT id FROM orders WHERE status = 'paid') t",
     "SELECT COUNT(*) FROM orders WHERE status = 'paid'"),
    ("derived_tables_reading_the_same_table_keep_apart", "mysql",
     "SELECT COUNT(*) FROM (SELECT p.user_id FROM (SELECT * FROM orders WHERE status = 'paid') p JOIN (SELECT * FROM orders WHERE status = 'open') o ON o.user_id = p.user_id WHERE p.amount > 100) x",
     "SELECT COUNT(*) FROM (SELECT p.user_id FROM (SELECT * FROM orders WHERE status = 'paid') p JOIN (SELECT * FROM orders WHERE status = 'open') o ON o.user_id = p.user_id WHERE o.amount > 100) x"),
    ("dedup_union_and_how_many_rows_come_out_are_compared", "mysql",
     "SELECT COUNT(*) FROM (SELECT user_id FROM orders UNION SELECT referrer_id FROM orders) t",
     "SELECT COUNT(*) FROM (SELECT user_id FROM orders UNION ALL SELECT referrer_id FROM orders) t"),
    ("the_same_item_on_another_layer_is_counted_once_per_layer", "mysql",
     "SELECT COUNT(*) FROM (SELECT user_id FROM orders WHERE status = 'paid' UNION SELECT referrer_id FROM orders WHERE status = 'paid') t",
     "SELECT COUNT(*) FROM (SELECT user_id FROM orders WHERE status = 'paid' UNION SELECT referrer_id FROM orders) t"),
    ("which_side_an_outer_join_keeps_is_compared", "mysql",
     "SELECT COUNT(*) FROM (SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id) p LEFT JOIN (SELECT user_id, SUM(amount) AS total FROM refunds GROUP BY user_id) r ON r.user_id = p.user_id",
     "SELECT COUNT(*) FROM (SELECT user_id, SUM(amount) AS total FROM refunds GROUP BY user_id) r LEFT JOIN (SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id) p ON p.user_id = r.user_id"),
    ("the_same_item_on_another_layer_is_counted_once_per_layer", "mysql",
     "SELECT COUNT(*) FROM orders o JOIN refunds r ON r.order_id = o.id WHERE EXISTS (SELECT 1 FROM orders o2 JOIN refunds r2 ON r2.order_id = o2.id WHERE o2.user_id = o.user_id)",
     "SELECT COUNT(*) FROM orders o WHERE EXISTS (SELECT 1 FROM orders o2 JOIN refunds r2 ON r2.order_id = o2.id WHERE o2.user_id = o.user_id)"),
    ("a_correlated_subquery_on_the_outer_table_tells_inner_from_ou", "mysql",
     "SELECT COUNT(*) FROM orders o WHERE EXISTS (SELECT 1 FROM orders c WHERE c.parent_id = o.id)",
     "SELECT COUNT(*) FROM orders o WHERE EXISTS (SELECT 1 FROM orders c WHERE c.id = o.parent_id)"),
    ("a_correlated_subquery_on_the_outer_table_tells_inner_from_ou", "mysql",
     "SELECT COUNT(*) FROM orders o WHERE EXISTS (SELECT 1 FROM orders p WHERE p.user_id = o.user_id AND p.created_at < o.created_at)",
     "SELECT COUNT(*) FROM orders o WHERE EXISTS (SELECT 1 FROM orders p WHERE p.user_id = o.user_id AND o.created_at < p.created_at)"),
    ("the_same_item_on_another_layer_is_counted_once_per_layer", "mysql",
     "SELECT COUNT(*) FROM orders o WHERE o.status = 'paid' AND EXISTS (SELECT 1 FROM orders x WHERE x.user_id = o.user_id AND x.status = 'paid' AND x.id <> o.id)",
     "SELECT COUNT(*) FROM orders o WHERE EXISTS (SELECT 1 FROM orders x WHERE x.user_id = o.user_id AND x.status = 'paid' AND x.id <> o.id)"),
    ("each_dialect_gives_the_same_three_findings", "mysql",
     "SELECT COUNT(DISTINCT o.order_id) AS `订单数` FROM orders o WHERE o.paid_at >= '2026-09-01' AND o.is_deleted <> 1",
     "SELECT COUNT(o.order_id) AS `订单数` FROM orders o WHERE o.created_at >= '2026-09-01'"),
    ("dedup_union_and_how_many_rows_come_out_are_compared", "mysql",
     "SELECT COUNT(t.user_id) FROM (SELECT user_id FROM orders GROUP BY user_id) t",
     "SELECT COUNT(user_id) FROM orders GROUP BY user_id"),
    ("dedup_union_and_how_many_rows_come_out_are_compared", "mysql",
     "SELECT DISTINCT region FROM orders",
     "SELECT region FROM orders"),
    ("derived_tables_reading_the_same_table_keep_apart", "mysql",
     "SELECT SUM(a.amount) FROM orders a JOIN orders b ON b.user_id = a.referrer_id",
     "SELECT SUM(a.amount) FROM orders a JOIN orders b ON b.referrer_id = a.user_id"),
    ("the_same_output_computation_written_differently_is_no_differ", "mysql",
     "SELECT SUM(amount) FROM orders",
     "SELECT IFNULL(SUM(amount), 0) FROM orders"),
    ("the_same_item_on_another_layer_is_counted_once_per_layer", "mysql",
     "SELECT SUM(amount) FROM orders WHERE paid_at >= '2026-09-01' AND user_id IN (SELECT user_id FROM orders WHERE paid_at >= '2026-09-01')",
     "SELECT SUM(amount) FROM orders WHERE user_id IN (SELECT user_id FROM orders WHERE paid_at >= '2026-09-01')"),
    ("the_same_item_on_another_layer_is_counted_once_per_layer", "mysql",
     "SELECT SUM(amount) FROM orders WHERE status = 'paid' AND user_id IN (SELECT user_id FROM orders WHERE status = 'paid')",
     "SELECT SUM(amount) FROM orders WHERE user_id IN (SELECT user_id FROM orders WHERE status = 'paid')"),
    ("the_same_item_on_another_layer_is_counted_once_per_layer", "mysql",
     "SELECT SUM(o.amount) FROM orders o WHERE o.amount > (SELECT AVG(amount) FROM orders)",
     "SELECT AVG(o.amount) FROM orders o WHERE o.amount > (SELECT AVG(amount) FROM orders)"),
    ("which_pass_through_derived_table_a_column_comes_from_is_repo", "mysql",
     "SELECT SUM(p.amount) FROM (SELECT * FROM orders WHERE status = 'paid') p JOIN (SELECT * FROM orders WHERE status = 'open') o ON o.user_id = p.user_id",
     "SELECT SUM(o.amount) FROM (SELECT * FROM orders WHERE status = 'paid') p JOIN (SELECT * FROM orders WHERE status = 'open') o ON o.user_id = p.user_id"),
    ("how_a_derived_column_is_computed_is_reported", "mysql",
     "SELECT SUM(x.total) FROM (SELECT p.user_id, p.total FROM (SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id) p JOIN (SELECT user_id, SUM(amount) AS total FROM refunds GROUP BY user_id) r ON r.user_id = p.user_id) x",
     "SELECT SUM(x.total) FROM (SELECT p.user_id, r.total FROM (SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id) p JOIN (SELECT user_id, SUM(amount) AS total FROM refunds GROUP BY user_id) r ON r.user_id = p.user_id) x"),
    ("a_correlated_subquery_on_the_outer_table_tells_inner_from_ou", "mysql",
     "SELECT o.id, (SELECT COUNT(*) FROM orders c WHERE c.parent_id = o.id) AS n FROM orders o",
     "SELECT o.id, (SELECT COUNT(*) FROM orders c WHERE c.id = o.parent_id) AS n FROM orders o"),
    ("which_pass_through_derived_table_a_column_comes_from_is_repo", "mysql",
     "SELECT p.id FROM (SELECT * FROM orders WHERE status = 'paid') p JOIN (SELECT * FROM orders WHERE status = 'open') o ON o.user_id = p.user_id ORDER BY p.created_at DESC LIMIT 10",
     "SELECT p.id FROM (SELECT * FROM orders WHERE status = 'paid') p JOIN (SELECT * FROM orders WHERE status = 'open') o ON o.user_id = p.user_id ORDER BY o.created_at DESC LIMIT 10"),
    ("the_same_item_on_another_layer_is_counted_once_per_layer", "mysql",
     "SELECT user_id FROM orders WHERE status = 'paid' UNION SELECT referrer_id FROM orders WHERE status = 'paid'",
     "SELECT user_id FROM orders WHERE status = 'paid' UNION SELECT referrer_id FROM orders"),
    ("the_same_item_on_another_layer_is_counted_once_per_layer", "mysql",
     "SELECT user_id, COUNT(*) FROM orders WHERE user_id IN (SELECT user_id FROM orders GROUP BY user_id HAVING COUNT(*) > 1) GROUP BY user_id",
     "SELECT COUNT(*) FROM orders WHERE user_id IN (SELECT user_id FROM orders GROUP BY user_id HAVING COUNT(*) > 1)"),
    ("the_same_item_on_another_layer_is_counted_once_per_layer", "mysql",
     "WITH a AS (SELECT * FROM orders WHERE is_deleted = 0 AND status = 'paid'), b AS (SELECT * FROM orders WHERE is_deleted = 0 AND status = 'open') SELECT COUNT(*) FROM a JOIN b ON b.user_id = a.user_id",
     "WITH a AS (SELECT * FROM orders WHERE is_deleted = 0 AND status = 'paid'), b AS (SELECT * FROM orders WHERE status = 'open') SELECT COUNT(*) FROM a JOIN b ON b.user_id = a.user_id"),
    ("time_columns_inside_derived_tables", "mysql",
     "WITH a AS (SELECT user_id, MAX(paid_at) AS ts FROM orders GROUP BY user_id), b AS (SELECT user_id, MAX(refunded_at) AS ts FROM refunds GROUP BY user_id) SELECT COUNT(*) FROM a JOIN b ON b.user_id = a.user_id WHERE a.ts >= '2026-09-01'",
     "WITH a AS (SELECT user_id, MAX(refunded_at) AS ts FROM refunds GROUP BY user_id), b AS (SELECT user_id, MAX(paid_at) AS ts FROM orders GROUP BY user_id) SELECT COUNT(*) FROM a JOIN b ON b.user_id = a.user_id WHERE a.ts >= '2026-09-01'"),
    ("derived_tables_reading_the_same_table_keep_apart", "mysql",
     "WITH base AS (SELECT * FROM orders WHERE is_deleted = 0), paid AS (SELECT * FROM base WHERE status = 'paid'), open AS (SELECT * FROM base WHERE status = 'open') SELECT COUNT(*) FROM paid p JOIN open o ON o.user_id = p.user_id WHERE p.amount > 100",
     "WITH base AS (SELECT * FROM orders WHERE is_deleted = 0), paid AS (SELECT * FROM base WHERE status = 'paid'), open AS (SELECT * FROM base WHERE status = 'open') SELECT COUNT(*) FROM paid p JOIN open o ON o.user_id = p.user_id WHERE o.amount > 100"),
    ("an_outer_join_turned_inner_by_where_is_reported", "mysql",
     "WITH done AS (SELECT * FROM refunds WHERE state = 'done') SELECT SUM(o.amount) FROM orders o LEFT JOIN done d ON d.order_id = o.id",
     "WITH done AS (SELECT * FROM refunds) SELECT SUM(o.amount) FROM orders o LEFT JOIN done d ON d.order_id = o.id WHERE d.state = 'done'"),
    ("a_correlated_subquery_on_the_outer_table_tells_inner_from_ou", "mysql",
     "WITH p AS (SELECT * FROM orders WHERE status = 'paid') SELECT COUNT(*) FROM p WHERE EXISTS (SELECT 1 FROM orders c WHERE c.parent_id = p.id)",
     "WITH p AS (SELECT * FROM orders WHERE status = 'paid') SELECT COUNT(*) FROM p WHERE EXISTS (SELECT 1 FROM orders c WHERE c.id = p.parent_id)"),
    ("which_pass_through_derived_table_a_column_comes_from_is_repo", "mysql",
     "WITH paid AS (SELECT * FROM orders WHERE status = 'paid'), open AS (SELECT * FROM orders WHERE status = 'open') SELECT COUNT(*) FROM paid p JOIN open o ON o.user_id = p.user_id GROUP BY p.store_id",
     "WITH paid AS (SELECT * FROM orders WHERE status = 'paid'), open AS (SELECT * FROM orders WHERE status = 'open') SELECT COUNT(*) FROM paid p JOIN open o ON o.user_id = p.user_id GROUP BY o.store_id"),
    ("which_pass_through_derived_table_a_column_comes_from_is_repo", "mysql",
     "WITH paid AS (SELECT * FROM orders WHERE status = 'paid'), open AS (SELECT * FROM orders WHERE status = 'open') SELECT COUNT(*) FROM paid p JOIN open o ON o.user_id = p.user_id WHERE p.amount > 100",
     "WITH paid AS (SELECT * FROM orders WHERE status = 'paid'), open AS (SELECT * FROM orders WHERE status = 'open') SELECT COUNT(*) FROM paid p JOIN open o ON o.user_id = p.user_id WHERE o.amount > 100"),
    ("which_pass_through_derived_table_a_column_comes_from_is_repo", "mysql",
     "WITH paid AS (SELECT * FROM orders WHERE status = 'paid'), open AS (SELECT * FROM orders WHERE status = 'open') SELECT COUNT(*) FROM paid p JOIN open o ON o.user_id = p.user_id WHERE p.created_at >= '2026-09-01'",
     "WITH paid AS (SELECT * FROM orders WHERE status = 'paid'), open AS (SELECT * FROM orders WHERE status = 'open') SELECT COUNT(*) FROM paid p JOIN open o ON o.user_id = p.user_id WHERE o.created_at >= '2026-09-01'"),
    ("derived_tables_reading_the_same_table_keep_apart", "mysql",
     "WITH paid AS (SELECT * FROM orders WHERE status = 'paid'), open AS (SELECT * FROM orders WHERE status = 'open') SELECT SUM(d) FROM (SELECT p.amount - o.amount AS d FROM paid p JOIN open o ON o.user_id = p.user_id) x",
     "WITH paid AS (SELECT * FROM orders WHERE status = 'paid'), open AS (SELECT * FROM orders WHERE status = 'open') SELECT SUM(d) FROM (SELECT o.amount - p.amount AS d FROM paid p JOIN open o ON o.user_id = p.user_id) x"),
    ("which_pass_through_derived_table_a_column_comes_from_is_repo", "mysql",
     "WITH paid AS (SELECT * FROM orders WHERE status = 'paid'), open AS (SELECT * FROM orders WHERE status = 'open') SELECT SUM(p.amount) FROM paid p JOIN open o ON o.user_id = p.user_id",
     "WITH paid AS (SELECT * FROM orders WHERE status = 'paid'), open AS (SELECT * FROM orders WHERE status = 'open') SELECT SUM(o.amount) FROM paid p JOIN open o ON o.user_id = p.user_id"),
    ("which_pass_through_derived_table_a_column_comes_from_is_repo", "mysql",
     "WITH paid AS (SELECT * FROM orders WHERE status = 'paid'), open AS (SELECT * FROM orders WHERE status = 'open') SELECT p.id FROM paid p JOIN open o ON o.user_id = p.user_id ORDER BY p.created_at DESC LIMIT 10",
     "WITH paid AS (SELECT * FROM orders WHERE status = 'paid'), open AS (SELECT * FROM orders WHERE status = 'open') SELECT p.id FROM paid p JOIN open o ON o.user_id = p.user_id ORDER BY o.created_at DESC LIMIT 10"),
    ("derived_tables_reading_the_same_table_keep_apart", "mysql",
     "WITH paid AS (SELECT user_id, referrer_id, amount FROM orders WHERE status = 'paid'), open AS (SELECT user_id, referrer_id, amount FROM orders WHERE status = 'open') SELECT SUM(p.amount) FROM paid p JOIN open o ON p.user_id = o.referrer_id",
     "WITH paid AS (SELECT user_id, referrer_id, amount FROM orders WHERE status = 'paid'), open AS (SELECT user_id, referrer_id, amount FROM orders WHERE status = 'open') SELECT SUM(p.amount) FROM paid p JOIN open o ON p.referrer_id = o.user_id"),
    ("the_same_item_on_another_layer_is_counted_once_per_layer", "mysql",
     "WITH pu AS (SELECT user_id, COUNT(*) AS n FROM orders WHERE status = 'paid' GROUP BY user_id) SELECT SUM(o.amount) FROM orders o JOIN pu ON pu.user_id = o.user_id WHERE o.status = 'paid'",
     "WITH pu AS (SELECT user_id, COUNT(*) AS n FROM orders WHERE status = 'paid' GROUP BY user_id) SELECT SUM(o.amount) FROM orders o JOIN pu ON pu.user_id = o.user_id"),
    ("how_a_derived_column_is_computed_is_reported", "mysql",
     "WITH r AS (SELECT user_id, amount, RANK() OVER (ORDER BY amount DESC) AS k FROM orders) SELECT SUM(amount) FROM r WHERE k <= 10",
     "WITH r AS (SELECT user_id, amount, ROW_NUMBER() OVER (ORDER BY amount DESC) AS k FROM orders) SELECT SUM(amount) FROM r WHERE k <= 10"),
    ("derived_tables_reading_the_same_table_keep_apart", "mysql",
     "WITH t AS (SELECT * FROM orders WHERE status = 'paid') SELECT SUM(a.amount) FROM t a JOIN t b ON b.user_id = a.referrer_id",
     "WITH t AS (SELECT * FROM orders WHERE status = 'paid') SELECT SUM(a.amount) FROM t a JOIN t b ON b.referrer_id = a.user_id"),
    ("derived_tables_reading_the_same_table_keep_apart", "mysql",
     "WITH t AS (SELECT * FROM orders WHERE status = 'paid') SELECT SUM(a.amount) FROM t a JOIN t b ON b.user_id = a.referrer_id",
     "WITH t AS (SELECT * FROM orders WHERE status = 'paid') SELECT SUM(b.amount) FROM t a JOIN t b ON b.user_id = a.referrer_id"),
    ("derived_tables_reading_the_same_table_keep_apart", "mysql",
     "WITH t AS (SELECT * FROM orders WHERE status = 'paid') SELECT SUM(p.amount) FROM t p JOIN orders c ON c.parent_id = p.id",
     "WITH t AS (SELECT * FROM orders WHERE status = 'paid') SELECT SUM(c.amount) FROM t p JOIN orders c ON c.parent_id = p.id"),
    ("how_a_derived_column_is_computed_is_reported", "mysql",
     "WITH t AS (SELECT DATE_FORMAT(paid_at, '%Y-%m') AS m, amount FROM orders) SELECT m, SUM(amount) FROM t GROUP BY m",
     "WITH t AS (SELECT DATE_FORMAT(created_at, '%Y-%m') AS m, amount FROM orders) SELECT m, SUM(amount) FROM t GROUP BY m"),
    ("how_a_derived_column_is_computed_is_reported", "mysql",
     "WITH t AS (SELECT user_id, CASE WHEN status = 'paid' THEN amount ELSE 0 END AS paid_amt FROM orders) SELECT SUM(paid_amt) FROM t",
     "WITH t AS (SELECT user_id, CASE WHEN status IN ('paid', 'shipped') THEN amount ELSE 0 END AS paid_amt FROM orders) SELECT SUM(paid_amt) FROM t"),
    ("a_time_column_computed_in_a_cte_is_seen_through", "mysql",
     "WITH t AS (SELECT user_id, amount, DATE(paid_at) AS d FROM orders) SELECT SUM(amount) FROM t WHERE d >= '2026-09-01'",
     "WITH t AS (SELECT user_id, amount, DATE(created_at) AS d FROM orders) SELECT SUM(amount) FROM t WHERE d >= '2026-09-01'"),
    ("which_pass_through_derived_table_a_column_comes_from_is_repo", "postgres",
     "SELECT COUNT(*) FROM (SELECT * FROM orders WHERE status = 'paid') p JOIN (SELECT * FROM orders WHERE status = 'open') o ON o.user_id = p.user_id GROUP BY p.store_id",
     "SELECT COUNT(*) FROM (SELECT * FROM orders WHERE status = 'paid') p JOIN (SELECT * FROM orders WHERE status = 'open') o ON o.user_id = p.user_id GROUP BY o.store_id"),
    ("which_pass_through_derived_table_a_column_comes_from_is_repo", "postgres",
     "SELECT COUNT(*) FROM (SELECT * FROM orders WHERE status = 'paid') p JOIN (SELECT * FROM orders WHERE status = 'open') o ON o.user_id = p.user_id WHERE p.amount > 100",
     "SELECT COUNT(*) FROM (SELECT * FROM orders WHERE status = 'paid') p JOIN (SELECT * FROM orders WHERE status = 'open') o ON o.user_id = p.user_id WHERE o.amount > 100"),
    ("which_pass_through_derived_table_a_column_comes_from_is_repo", "postgres",
     "SELECT COUNT(*) FROM (SELECT * FROM orders WHERE status = 'paid') p JOIN (SELECT * FROM orders WHERE status = 'open') o ON o.user_id = p.user_id WHERE p.created_at >= '2026-09-01'",
     "SELECT COUNT(*) FROM (SELECT * FROM orders WHERE status = 'paid') p JOIN (SELECT * FROM orders WHERE status = 'open') o ON o.user_id = p.user_id WHERE o.created_at >= '2026-09-01'"),
    ("dedup_union_and_how_many_rows_come_out_are_compared", "postgres",
     "SELECT COUNT(*) FROM (SELECT DISTINCT user_id FROM orders WHERE status = 'paid') t",
     "SELECT COUNT(*) FROM (SELECT user_id FROM orders WHERE status = 'paid') t"),
    ("dedup_union_and_how_many_rows_come_out_are_compared", "postgres",
     "SELECT COUNT(*) FROM (SELECT DISTINCT user_id FROM orders WHERE status = 'paid') t",
     "SELECT COUNT(*) FROM orders WHERE status = 'paid'"),
    ("dedup_union_and_how_many_rows_come_out_are_compared", "postgres",
     "SELECT COUNT(*) FROM (SELECT id FROM orders WHERE status = 'paid' UNION ALL SELECT id FROM orders WHERE status = 'paid') t",
     "SELECT COUNT(*) FROM orders WHERE status = 'paid'"),
    ("derived_tables_reading_the_same_table_keep_apart", "postgres",
     "SELECT COUNT(*) FROM (SELECT p.user_id FROM (SELECT * FROM orders WHERE status = 'paid') p JOIN (SELECT * FROM orders WHERE status = 'open') o ON o.user_id = p.user_id WHERE p.amount > 100) x",
     "SELECT COUNT(*) FROM (SELECT p.user_id FROM (SELECT * FROM orders WHERE status = 'paid') p JOIN (SELECT * FROM orders WHERE status = 'open') o ON o.user_id = p.user_id WHERE o.amount > 100) x"),
    ("dedup_union_and_how_many_rows_come_out_are_compared", "postgres",
     "SELECT COUNT(*) FROM (SELECT user_id FROM orders UNION SELECT referrer_id FROM orders) t",
     "SELECT COUNT(*) FROM (SELECT user_id FROM orders UNION ALL SELECT referrer_id FROM orders) t"),
    ("the_same_item_on_another_layer_is_counted_once_per_layer", "postgres",
     "SELECT COUNT(*) FROM (SELECT user_id FROM orders WHERE status = 'paid' UNION SELECT referrer_id FROM orders WHERE status = 'paid') t",
     "SELECT COUNT(*) FROM (SELECT user_id FROM orders WHERE status = 'paid' UNION SELECT referrer_id FROM orders) t"),
    ("filtering_at_an_equivalent_stage_is_no_difference", "postgres",
     "SELECT COUNT(*) FROM (SELECT user_id, ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY paid_at) AS rn FROM orders) t WHERE t.rn = 1 AND t.user_id > 100",
     "SELECT COUNT(*) FROM (SELECT user_id, ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY paid_at) AS rn FROM orders WHERE user_id > 100) t WHERE t.rn = 1"),
    ("how_a_derived_column_is_computed_is_reported", "postgres",
     "SELECT COUNT(*) FROM (SELECT user_id, amount - fee AS net FROM orders) a WHERE a.net > 100",
     "SELECT COUNT(*) FROM (SELECT user_id, amount + fee AS net FROM orders) b WHERE b.net > 100"),
    ("filtering_before_or_after_a_window_or_limit_is_reported", "postgres",
     "SELECT COUNT(*) FROM (SELECT user_id, status, ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY paid_at) AS rn FROM orders) t WHERE t.rn = 1 AND t.status = 'paid'",
     "SELECT COUNT(*) FROM (SELECT user_id, status, ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY paid_at) AS rn FROM orders WHERE status = 'paid') t WHERE t.rn = 1"),
    ("the_same_item_on_another_layer_is_counted_once_per_layer", "postgres",
     "SELECT COUNT(*) FROM orders o JOIN refunds r ON r.order_id = o.id WHERE EXISTS (SELECT 1 FROM orders o2 JOIN refunds r2 ON r2.order_id = o2.id WHERE o2.user_id = o.user_id)",
     "SELECT COUNT(*) FROM orders o WHERE EXISTS (SELECT 1 FROM orders o2 JOIN refunds r2 ON r2.order_id = o2.id WHERE o2.user_id = o.user_id)"),
    ("an_outer_join_turned_inner_by_where_is_reported", "postgres",
     "SELECT COUNT(*) FROM orders o RIGHT JOIN refunds r ON r.order_id = o.id",
     "SELECT COUNT(*) FROM orders o RIGHT JOIN refunds r ON r.order_id = o.id WHERE o.status = 'paid'"),
    ("which_side_an_outer_join_keeps_is_compared", "postgres",
     "SELECT COUNT(*) FROM orders o RIGHT JOIN refunds r ON r.order_id = o.id",
     "SELECT COUNT(*) FROM refunds r RIGHT JOIN orders o ON o.id = r.order_id"),
    ("a_correlated_subquery_on_the_outer_table_tells_inner_from_ou", "postgres",
     "SELECT COUNT(*) FROM orders o WHERE EXISTS (SELECT 1 FROM orders c WHERE c.parent_id = o.id)",
     "SELECT COUNT(*) FROM orders o WHERE EXISTS (SELECT 1 FROM orders c WHERE c.id = o.parent_id)"),
    ("a_correlated_subquery_on_the_outer_table_tells_inner_from_ou", "postgres",
     "SELECT COUNT(*) FROM orders o WHERE EXISTS (SELECT 1 FROM orders p WHERE p.user_id = o.user_id AND p.created_at < o.created_at)",
     "SELECT COUNT(*) FROM orders o WHERE EXISTS (SELECT 1 FROM orders p WHERE p.user_id = o.user_id AND o.created_at < p.created_at)"),
    ("the_same_item_on_another_layer_is_counted_once_per_layer", "postgres",
     "SELECT COUNT(*) FROM orders o WHERE o.status = 'paid' AND EXISTS (SELECT 1 FROM orders x WHERE x.user_id = o.user_id AND x.status = 'paid' AND x.id <> o.id)",
     "SELECT COUNT(*) FROM orders o WHERE EXISTS (SELECT 1 FROM orders x WHERE x.user_id = o.user_id AND x.status = 'paid' AND x.id <> o.id)"),
    ("each_dialect_gives_the_same_three_findings", "postgres",
     "SELECT COUNT(DISTINCT o.order_id) AS \"订单数\" FROM orders o WHERE o.paid_at >= '2026-09-01' AND o.is_deleted <> 1",
     "SELECT COUNT(o.order_id) AS \"订单数\" FROM orders o WHERE o.created_at >= '2026-09-01'"),
    ("dedup_union_and_how_many_rows_come_out_are_compared", "postgres",
     "SELECT COUNT(t.user_id) FROM (SELECT user_id FROM orders GROUP BY user_id) t",
     "SELECT COUNT(user_id) FROM orders GROUP BY user_id"),
    ("dedup_union_and_how_many_rows_come_out_are_compared", "postgres",
     "SELECT DISTINCT region FROM orders",
     "SELECT region FROM orders"),
    ("derived_tables_reading_the_same_table_keep_apart", "postgres",
     "SELECT SUM(a.amount) FROM orders a JOIN orders b ON b.user_id = a.referrer_id",
     "SELECT SUM(a.amount) FROM orders a JOIN orders b ON b.referrer_id = a.user_id"),
    ("how_a_derived_column_is_computed_is_reported", "postgres",
     "SELECT SUM(amount) FROM (SELECT user_id, amount, ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY paid_at) AS rn FROM orders) t WHERE rn = 1",
     "SELECT SUM(amount) FROM (SELECT user_id, amount, ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY paid_at DESC) AS rn FROM orders) t WHERE rn = 1"),
    ("the_same_item_on_another_layer_is_counted_once_per_layer", "postgres",
     "SELECT SUM(amount) FROM orders WHERE paid_at >= '2026-09-01' AND user_id IN (SELECT user_id FROM orders WHERE paid_at >= '2026-09-01')",
     "SELECT SUM(amount) FROM orders WHERE user_id IN (SELECT user_id FROM orders WHERE paid_at >= '2026-09-01')"),
    ("the_same_item_on_another_layer_is_counted_once_per_layer", "postgres",
     "SELECT SUM(amount) FROM orders WHERE status = 'paid' AND user_id IN (SELECT user_id FROM orders WHERE status = 'paid')",
     "SELECT SUM(amount) FROM orders WHERE user_id IN (SELECT user_id FROM orders WHERE status = 'paid')"),
    ("an_outer_join_turned_inner_by_where_is_reported", "postgres",
     "SELECT SUM(o.amount) FROM orders o LEFT JOIN refunds r ON r.order_id = o.id WHERE o.status = 'paid'",
     "SELECT SUM(o.amount) FROM refunds r LEFT JOIN orders o ON o.id = r.order_id WHERE o.status = 'paid'"),
    ("the_same_item_on_another_layer_is_counted_once_per_layer", "postgres",
     "SELECT SUM(o.amount) FROM orders o WHERE o.amount > (SELECT AVG(amount) FROM orders)",
     "SELECT AVG(o.amount) FROM orders o WHERE o.amount > (SELECT AVG(amount) FROM orders)"),
    ("names_given_to_derived_tables_are_no_difference", "postgres",
     "SELECT SUM(o.amount) FROM orders o, LATERAL (SELECT COUNT(*) AS n FROM refunds r WHERE r.order_id = o.id) l WHERE l.n > 0",
     "SELECT SUM(o.amount) FROM orders o CROSS JOIN LATERAL (SELECT COUNT(*) AS n FROM refunds r WHERE r.order_id = o.id) AS x WHERE x.n > 0"),
    ("which_pass_through_derived_table_a_column_comes_from_is_repo", "postgres",
     "SELECT SUM(p.amount) FROM (SELECT * FROM orders WHERE status = 'paid') p JOIN (SELECT * FROM orders WHERE status = 'open') o ON o.user_id = p.user_id",
     "SELECT SUM(o.amount) FROM (SELECT * FROM orders WHERE status = 'paid') p JOIN (SELECT * FROM orders WHERE status = 'open') o ON o.user_id = p.user_id"),
    ("how_an_output_column_is_computed_is_compared", "postgres",
     "SELECT SUM(r.total) / SUM(p.total) FROM (SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id) p JOIN (SELECT user_id, SUM(amount) AS total FROM refunds GROUP BY user_id) r ON r.user_id = p.user_id",
     "SELECT SUM(p.total) / SUM(r.total) FROM (SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id) p JOIN (SELECT user_id, SUM(amount) AS total FROM refunds GROUP BY user_id) r ON r.user_id = p.user_id"),
    ("time_columns_inside_derived_tables", "postgres",
     "SELECT SUM(x.amount) FROM (SELECT amount, CAST(paid_at AS DATE) AS day FROM orders) x WHERE x.day BETWEEN '2026-09-01' AND '2026-09-30'",
     "SELECT SUM(y.amount) FROM (SELECT amount, CAST(created_at AS DATE) AS day FROM orders) y WHERE y.day BETWEEN '2026-09-01' AND '2026-09-30'"),
    ("a_lateral_subquery_is_named_by_its_alias", "postgres",
     "SELECT o.id FROM orders o, LATERAL (SELECT amount FROM refunds r WHERE r.order_id = o.id ORDER BY amount DESC LIMIT 1) l",
     "SELECT o.id FROM orders o CROSS JOIN LATERAL (SELECT amount FROM refunds r WHERE r.order_id = o.id ORDER BY amount DESC LIMIT 2) AS x"),
    ("a_correlated_subquery_on_the_outer_table_tells_inner_from_ou", "postgres",
     "SELECT o.id, (SELECT COUNT(*) FROM orders c WHERE c.parent_id = o.id) AS n FROM orders o",
     "SELECT o.id, (SELECT COUNT(*) FROM orders c WHERE c.id = o.parent_id) AS n FROM orders o"),
    ("a_correlated_lateral_subquery_on_the_outer_table_is_reported", "postgres",
     "SELECT o.id, x.s FROM orders o CROSS JOIN LATERAL (SELECT SUM(c.amount) AS s FROM orders c WHERE c.parent_id = o.id) x",
     "SELECT o.id, x.s FROM orders o CROSS JOIN LATERAL (SELECT SUM(c.amount) AS s FROM orders c WHERE c.id = o.parent_id) x"),
    ("which_pass_through_derived_table_a_column_comes_from_is_repo", "postgres",
     "SELECT p.id FROM (SELECT * FROM orders WHERE status = 'paid') p JOIN (SELECT * FROM orders WHERE status = 'open') o ON o.user_id = p.user_id ORDER BY p.created_at DESC LIMIT 10",
     "SELECT p.id FROM (SELECT * FROM orders WHERE status = 'paid') p JOIN (SELECT * FROM orders WHERE status = 'open') o ON o.user_id = p.user_id ORDER BY o.created_at DESC LIMIT 10"),
    ("the_same_item_on_another_layer_is_counted_once_per_layer", "postgres",
     "SELECT user_id FROM orders WHERE status = 'paid' UNION SELECT referrer_id FROM orders WHERE status = 'paid'",
     "SELECT user_id FROM orders WHERE status = 'paid' UNION SELECT referrer_id FROM orders"),
    ("the_same_item_on_another_layer_is_counted_once_per_layer", "postgres",
     "SELECT user_id, COUNT(*) FROM orders WHERE user_id IN (SELECT user_id FROM orders GROUP BY user_id HAVING COUNT(*) > 1) GROUP BY user_id",
     "SELECT COUNT(*) FROM orders WHERE user_id IN (SELECT user_id FROM orders GROUP BY user_id HAVING COUNT(*) > 1)"),
    ("how_a_derived_column_is_computed_is_reported", "postgres",
     "WITH RECURSIVE t(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM t WHERE n < 10) SELECT SUM(n) FROM t",
     "WITH RECURSIVE t(n) AS (SELECT 1 UNION ALL SELECT n + 2 FROM t WHERE n < 10) SELECT SUM(n) FROM t"),
    ("the_same_item_on_another_layer_is_counted_once_per_layer", "postgres",
     "WITH a AS (SELECT * FROM orders WHERE is_deleted = 0 AND status = 'paid'), b AS (SELECT * FROM orders WHERE is_deleted = 0 AND status = 'open') SELECT COUNT(*) FROM a JOIN b ON b.user_id = a.user_id",
     "WITH a AS (SELECT * FROM orders WHERE is_deleted = 0 AND status = 'paid'), b AS (SELECT * FROM orders WHERE status = 'open') SELECT COUNT(*) FROM a JOIN b ON b.user_id = a.user_id"),
    ("derived_tables_reading_the_same_table_keep_apart", "postgres",
     "WITH base AS (SELECT * FROM orders WHERE is_deleted = 0), paid AS (SELECT * FROM base WHERE status = 'paid'), open AS (SELECT * FROM base WHERE status = 'open') SELECT COUNT(*) FROM paid p JOIN open o ON o.user_id = p.user_id WHERE p.amount > 100",
     "WITH base AS (SELECT * FROM orders WHERE is_deleted = 0), paid AS (SELECT * FROM base WHERE status = 'paid'), open AS (SELECT * FROM base WHERE status = 'open') SELECT COUNT(*) FROM paid p JOIN open o ON o.user_id = p.user_id WHERE o.amount > 100"),
    ("a_self_join_tells_its_two_aliases_apart", "postgres",
     "WITH base AS (SELECT user_id, referrer_id, SUM(amount) AS total FROM orders GROUP BY user_id, referrer_id) SELECT COUNT(*) FROM base b1 JOIN base b2 ON b2.user_id = b1.referrer_id WHERE b1.total > b2.total",
     "WITH base AS (SELECT user_id, referrer_id, SUM(amount) AS total FROM orders GROUP BY user_id, referrer_id) SELECT COUNT(*) FROM base b1 JOIN base b2 ON b2.user_id = b1.referrer_id WHERE b2.total > b1.total"),
    ("a_correlated_subquery_on_the_outer_table_tells_inner_from_ou", "postgres",
     "WITH p AS (SELECT * FROM orders WHERE status = 'paid') SELECT COUNT(*) FROM p WHERE EXISTS (SELECT 1 FROM orders c WHERE c.parent_id = p.id)",
     "WITH p AS (SELECT * FROM orders WHERE status = 'paid') SELECT COUNT(*) FROM p WHERE EXISTS (SELECT 1 FROM orders c WHERE c.id = p.parent_id)"),
    ("which_pass_through_derived_table_a_column_comes_from_is_repo", "postgres",
     "WITH paid AS (SELECT * FROM orders WHERE status = 'paid'), open AS (SELECT * FROM orders WHERE status = 'open') SELECT COUNT(*) FROM paid p JOIN open o ON o.user_id = p.user_id GROUP BY p.store_id",
     "WITH paid AS (SELECT * FROM orders WHERE status = 'paid'), open AS (SELECT * FROM orders WHERE status = 'open') SELECT COUNT(*) FROM paid p JOIN open o ON o.user_id = p.user_id GROUP BY o.store_id"),
    ("which_pass_through_derived_table_a_column_comes_from_is_repo", "postgres",
     "WITH paid AS (SELECT * FROM orders WHERE status = 'paid'), open AS (SELECT * FROM orders WHERE status = 'open') SELECT COUNT(*) FROM paid p JOIN open o ON o.user_id = p.user_id WHERE p.amount > 100",
     "WITH paid AS (SELECT * FROM orders WHERE status = 'paid'), open AS (SELECT * FROM orders WHERE status = 'open') SELECT COUNT(*) FROM paid p JOIN open o ON o.user_id = p.user_id WHERE o.amount > 100"),
    ("which_pass_through_derived_table_a_column_comes_from_is_repo", "postgres",
     "WITH paid AS (SELECT * FROM orders WHERE status = 'paid'), open AS (SELECT * FROM orders WHERE status = 'open') SELECT COUNT(*) FROM paid p JOIN open o ON o.user_id = p.user_id WHERE p.created_at >= '2026-09-01'",
     "WITH paid AS (SELECT * FROM orders WHERE status = 'paid'), open AS (SELECT * FROM orders WHERE status = 'open') SELECT COUNT(*) FROM paid p JOIN open o ON o.user_id = p.user_id WHERE o.created_at >= '2026-09-01'"),
    ("a_time_column_of_a_pass_through_derived_table_names_the_real", "postgres",
     "WITH paid AS (SELECT * FROM orders WHERE status = 'paid'), open AS (SELECT * FROM orders WHERE status = 'open') SELECT COUNT(*) FROM paid p JOIN open o ON o.user_id = p.user_id WHERE p.created_at::date >= '2026-09-01'",
     "WITH paid AS (SELECT * FROM orders WHERE status = 'paid'), open AS (SELECT * FROM orders WHERE status = 'open') SELECT COUNT(*) FROM paid p JOIN open o ON o.user_id = p.user_id WHERE o.created_at::date >= '2026-09-01'"),
    ("derived_tables_reading_the_same_table_keep_apart", "postgres",
     "WITH paid AS (SELECT * FROM orders WHERE status = 'paid'), open AS (SELECT * FROM orders WHERE status = 'open') SELECT SUM(d) FROM (SELECT p.amount - o.amount AS d FROM paid p JOIN open o ON o.user_id = p.user_id) x",
     "WITH paid AS (SELECT * FROM orders WHERE status = 'paid'), open AS (SELECT * FROM orders WHERE status = 'open') SELECT SUM(d) FROM (SELECT o.amount - p.amount AS d FROM paid p JOIN open o ON o.user_id = p.user_id) x"),
    ("which_pass_through_derived_table_a_column_comes_from_is_repo", "postgres",
     "WITH paid AS (SELECT * FROM orders WHERE status = 'paid'), open AS (SELECT * FROM orders WHERE status = 'open') SELECT SUM(p.amount) FROM paid p JOIN open o ON o.user_id = p.user_id",
     "WITH paid AS (SELECT * FROM orders WHERE status = 'paid'), open AS (SELECT * FROM orders WHERE status = 'open') SELECT SUM(o.amount) FROM paid p JOIN open o ON o.user_id = p.user_id"),
    ("which_pass_through_derived_table_a_column_comes_from_is_repo", "postgres",
     "WITH paid AS (SELECT * FROM orders WHERE status = 'paid'), open AS (SELECT * FROM orders WHERE status = 'open') SELECT p.id FROM paid p JOIN open o ON o.user_id = p.user_id ORDER BY p.created_at DESC LIMIT 10",
     "WITH paid AS (SELECT * FROM orders WHERE status = 'paid'), open AS (SELECT * FROM orders WHERE status = 'open') SELECT p.id FROM paid p JOIN open o ON o.user_id = p.user_id ORDER BY o.created_at DESC LIMIT 10"),
    ("how_a_derived_column_is_computed_is_reported", "postgres",
     "WITH paid AS (SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id), refunded AS (SELECT user_id, SUM(amount) AS total FROM refunds GROUP BY user_id) , net AS (SELECT p.user_id, p.total AS v FROM paid p JOIN refunded r ON r.user_id = p.user_id) SELECT COUNT(*) FROM net WHERE v > 100",
     "WITH paid AS (SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id), refunded AS (SELECT user_id, SUM(amount) AS total FROM refunds GROUP BY user_id) , net AS (SELECT p.user_id, r.total AS v FROM paid p JOIN refunded r ON r.user_id = p.user_id) SELECT COUNT(*) FROM net WHERE v > 100"),
    ("which_side_an_outer_join_keeps_is_compared", "postgres",
     "WITH paid AS (SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id), refunded AS (SELECT user_id, SUM(amount) AS total FROM refunds GROUP BY user_id) SELECT COUNT(*) FROM paid p LEFT JOIN refunded r ON r.user_id = p.user_id",
     "WITH paid AS (SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id), refunded AS (SELECT user_id, SUM(amount) AS total FROM refunds GROUP BY user_id) SELECT COUNT(*) FROM refunded r LEFT JOIN paid p ON p.user_id = r.user_id"),
    ("derived_tables_reading_the_same_table_keep_apart", "postgres",
     "WITH paid AS (SELECT user_id, referrer_id, amount FROM orders WHERE status = 'paid'), open AS (SELECT user_id, referrer_id, amount FROM orders WHERE status = 'open') SELECT SUM(p.amount) FROM paid p JOIN open o ON p.user_id = o.referrer_id",
     "WITH paid AS (SELECT user_id, referrer_id, amount FROM orders WHERE status = 'paid'), open AS (SELECT user_id, referrer_id, amount FROM orders WHERE status = 'open') SELECT SUM(p.amount) FROM paid p JOIN open o ON p.referrer_id = o.user_id"),
    ("the_same_item_on_another_layer_is_counted_once_per_layer", "postgres",
     "WITH pu AS (SELECT user_id, COUNT(*) AS n FROM orders WHERE status = 'paid' GROUP BY user_id) SELECT SUM(o.amount) FROM orders o JOIN pu ON pu.user_id = o.user_id WHERE o.status = 'paid'",
     "WITH pu AS (SELECT user_id, COUNT(*) AS n FROM orders WHERE status = 'paid' GROUP BY user_id) SELECT SUM(o.amount) FROM orders o JOIN pu ON pu.user_id = o.user_id"),
    ("derived_tables_reading_the_same_table_keep_apart", "postgres",
     "WITH t AS (SELECT * FROM orders WHERE status = 'paid') SELECT SUM(a.amount) FROM t a JOIN t b ON b.user_id = a.referrer_id",
     "WITH t AS (SELECT * FROM orders WHERE status = 'paid') SELECT SUM(a.amount) FROM t a JOIN t b ON b.referrer_id = a.user_id"),
    ("derived_tables_reading_the_same_table_keep_apart", "postgres",
     "WITH t AS (SELECT * FROM orders WHERE status = 'paid') SELECT SUM(a.amount) FROM t a JOIN t b ON b.user_id = a.referrer_id",
     "WITH t AS (SELECT * FROM orders WHERE status = 'paid') SELECT SUM(b.amount) FROM t a JOIN t b ON b.user_id = a.referrer_id"),
    ("derived_tables_reading_the_same_table_keep_apart", "postgres",
     "WITH t AS (SELECT * FROM orders WHERE status = 'paid') SELECT SUM(p.amount) FROM t p JOIN orders c ON c.parent_id = p.id",
     "WITH t AS (SELECT * FROM orders WHERE status = 'paid') SELECT SUM(c.amount) FROM t p JOIN orders c ON c.parent_id = p.id"),
    ("time_columns_inside_derived_tables", "postgres",
     "WITH t AS (SELECT DATE(paid_at) AS d, amount FROM orders) SELECT SUM(amount) FROM t WHERE d >= '2026-09-01'",
     "WITH t AS (SELECT CAST(paid_at AS DATE) AS d, amount FROM orders) SELECT SUM(amount) FROM t WHERE d >= '2026-09-01'"),
    ("the_same_derived_column_written_differently_is_no_difference", "postgres",
     "WITH t AS (SELECT DATE(paid_at) AS d, amount FROM orders) SELECT d, SUM(amount) FROM t GROUP BY d",
     "WITH t AS (SELECT CAST(paid_at AS DATE) AS d, amount FROM orders) SELECT d, SUM(amount) FROM t GROUP BY d"),
    ("a_time_column_computed_in_a_cte_is_seen_through", "postgres",
     "WITH t AS (SELECT user_id, amount, DATE(paid_at) AS d FROM orders) SELECT SUM(amount) FROM t WHERE d >= '2026-09-01'",
     "WITH t AS (SELECT user_id, amount, DATE(created_at) AS d FROM orders) SELECT SUM(amount) FROM t WHERE d >= '2026-09-01'"),
    ("same_named_derived_tables_with_swapped_definitions_are_told_", "postgres",
     "WITH t1 AS (SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id), t2 AS (SELECT user_id, SUM(amount) AS total FROM refunds GROUP BY user_id) SELECT COUNT(*) FROM t1 JOIN t2 ON t2.user_id = t1.user_id WHERE t1.total > 100",
     "WITH t1 AS (SELECT user_id, SUM(amount) AS total FROM refunds GROUP BY user_id), t2 AS (SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id) SELECT COUNT(*) FROM t1 JOIN t2 ON t2.user_id = t1.user_id WHERE t1.total > 100"),
    ("which_pass_through_derived_table_a_column_comes_from_is_repo", "sqlite",
     "SELECT COUNT(*) FROM (SELECT * FROM orders WHERE status = 'paid') p JOIN (SELECT * FROM orders WHERE status = 'open') o ON o.user_id = p.user_id GROUP BY p.store_id",
     "SELECT COUNT(*) FROM (SELECT * FROM orders WHERE status = 'paid') p JOIN (SELECT * FROM orders WHERE status = 'open') o ON o.user_id = p.user_id GROUP BY o.store_id"),
    ("which_pass_through_derived_table_a_column_comes_from_is_repo", "sqlite",
     "SELECT COUNT(*) FROM (SELECT * FROM orders WHERE status = 'paid') p JOIN (SELECT * FROM orders WHERE status = 'open') o ON o.user_id = p.user_id WHERE p.amount > 100",
     "SELECT COUNT(*) FROM (SELECT * FROM orders WHERE status = 'paid') p JOIN (SELECT * FROM orders WHERE status = 'open') o ON o.user_id = p.user_id WHERE o.amount > 100"),
    ("which_pass_through_derived_table_a_column_comes_from_is_repo", "sqlite",
     "SELECT COUNT(*) FROM (SELECT * FROM orders WHERE status = 'paid') p JOIN (SELECT * FROM orders WHERE status = 'open') o ON o.user_id = p.user_id WHERE p.created_at >= '2026-09-01'",
     "SELECT COUNT(*) FROM (SELECT * FROM orders WHERE status = 'paid') p JOIN (SELECT * FROM orders WHERE status = 'open') o ON o.user_id = p.user_id WHERE o.created_at >= '2026-09-01'"),
    ("dedup_union_and_how_many_rows_come_out_are_compared", "sqlite",
     "SELECT COUNT(*) FROM (SELECT DISTINCT user_id FROM orders WHERE status = 'paid') t",
     "SELECT COUNT(*) FROM (SELECT user_id FROM orders WHERE status = 'paid') t"),
    ("dedup_union_and_how_many_rows_come_out_are_compared", "sqlite",
     "SELECT COUNT(*) FROM (SELECT DISTINCT user_id FROM orders WHERE status = 'paid') t",
     "SELECT COUNT(*) FROM orders WHERE status = 'paid'"),
    ("dedup_union_and_how_many_rows_come_out_are_compared", "sqlite",
     "SELECT COUNT(*) FROM (SELECT id FROM orders WHERE status = 'paid' UNION ALL SELECT id FROM orders WHERE status = 'paid') t",
     "SELECT COUNT(*) FROM orders WHERE status = 'paid'"),
    ("derived_tables_reading_the_same_table_keep_apart", "sqlite",
     "SELECT COUNT(*) FROM (SELECT p.user_id FROM (SELECT * FROM orders WHERE status = 'paid') p JOIN (SELECT * FROM orders WHERE status = 'open') o ON o.user_id = p.user_id WHERE p.amount > 100) x",
     "SELECT COUNT(*) FROM (SELECT p.user_id FROM (SELECT * FROM orders WHERE status = 'paid') p JOIN (SELECT * FROM orders WHERE status = 'open') o ON o.user_id = p.user_id WHERE o.amount > 100) x"),
    ("dedup_union_and_how_many_rows_come_out_are_compared", "sqlite",
     "SELECT COUNT(*) FROM (SELECT user_id FROM orders UNION SELECT referrer_id FROM orders) t",
     "SELECT COUNT(*) FROM (SELECT user_id FROM orders UNION ALL SELECT referrer_id FROM orders) t"),
    ("the_same_item_on_another_layer_is_counted_once_per_layer", "sqlite",
     "SELECT COUNT(*) FROM (SELECT user_id FROM orders WHERE status = 'paid' UNION SELECT referrer_id FROM orders WHERE status = 'paid') t",
     "SELECT COUNT(*) FROM (SELECT user_id FROM orders WHERE status = 'paid' UNION SELECT referrer_id FROM orders) t"),
    ("which_derived_table_a_column_comes_from_is_reported", "sqlite",
     "SELECT COUNT(*) FROM (SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id) p JOIN (SELECT user_id, SUM(amount) AS total FROM refunds GROUP BY user_id) r ON r.user_id = p.user_id WHERE p.total - r.total > 100",
     "SELECT COUNT(*) FROM (SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id) p JOIN (SELECT user_id, SUM(amount) AS total FROM refunds GROUP BY user_id) r ON r.user_id = p.user_id WHERE r.total - p.total > 100"),
    ("which_derived_table_a_column_comes_from_is_reported", "sqlite",
     "SELECT COUNT(*) FROM (SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id) p JOIN (SELECT user_id, SUM(amount) AS total FROM refunds GROUP BY user_id) r ON r.user_id = p.user_id WHERE p.total > 100",
     "SELECT COUNT(*) FROM (SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id) p JOIN (SELECT user_id, SUM(amount) AS total FROM refunds GROUP BY user_id) r ON r.user_id = p.user_id WHERE r.total > 100"),
    ("other_aggregate_differences", "sqlite",
     "SELECT COUNT(*) FROM orders",
     "SELECT COUNT(DISTINCT order_id) FROM orders"),
    ("the_same_item_on_another_layer_is_counted_once_per_layer", "sqlite",
     "SELECT COUNT(*) FROM orders o JOIN refunds r ON r.order_id = o.id WHERE EXISTS (SELECT 1 FROM orders o2 JOIN refunds r2 ON r2.order_id = o2.id WHERE o2.user_id = o.user_id)",
     "SELECT COUNT(*) FROM orders o WHERE EXISTS (SELECT 1 FROM orders o2 JOIN refunds r2 ON r2.order_id = o2.id WHERE o2.user_id = o.user_id)"),
    ("an_outer_join_turned_inner_by_where_is_reported", "sqlite",
     "SELECT COUNT(*) FROM orders o LEFT JOIN (SELECT * FROM refunds WHERE state = 'done') r ON r.order_id = o.id",
     "SELECT COUNT(*) FROM orders o LEFT JOIN (SELECT * FROM refunds) r ON r.order_id = o.id WHERE r.state = 'done'"),
    ("which_side_an_outer_join_keeps_is_compared", "sqlite",
     "SELECT COUNT(*) FROM orders o LEFT JOIN refunds r ON r.order_id = o.id",
     "SELECT COUNT(*) FROM refunds r LEFT JOIN orders o ON o.id = r.order_id"),
    ("only_null_rejecting_where_conditions_turn_an_outer_join_inne", "sqlite",
     "SELECT COUNT(*) FROM orders o LEFT JOIN refunds r ON r.order_id = o.id WHERE COALESCE(r.amount, 0) < 100",
     "SELECT COUNT(*) FROM orders o LEFT JOIN refunds r ON r.order_id = o.id"),
    ("which_side_an_outer_join_keeps_is_compared", "sqlite",
     "SELECT COUNT(*) FROM orders o LEFT JOIN refunds r ON r.order_id = o.id WHERE r.order_id IS NULL",
     "SELECT COUNT(*) FROM refunds r LEFT JOIN orders o ON o.id = r.order_id WHERE r.order_id IS NULL"),
    ("only_null_rejecting_where_conditions_turn_an_outer_join_inne", "sqlite",
     "SELECT COUNT(*) FROM orders o LEFT JOIN refunds r ON r.order_id = o.id WHERE r.state = 'done' OR r.id IS NULL",
     "SELECT COUNT(*) FROM orders o JOIN refunds r ON r.order_id = o.id WHERE r.state = 'done' OR r.id IS NULL"),
    ("a_correlated_subquery_on_the_outer_table_tells_inner_from_ou", "sqlite",
     "SELECT COUNT(*) FROM orders o WHERE EXISTS (SELECT 1 FROM orders c WHERE c.parent_id = o.id)",
     "SELECT COUNT(*) FROM orders o WHERE EXISTS (SELECT 1 FROM orders c WHERE c.id = o.parent_id)"),
    ("a_correlated_subquery_on_the_outer_table_tells_inner_from_ou", "sqlite",
     "SELECT COUNT(*) FROM orders o WHERE EXISTS (SELECT 1 FROM orders p WHERE p.user_id = o.user_id AND p.created_at < o.created_at)",
     "SELECT COUNT(*) FROM orders o WHERE EXISTS (SELECT 1 FROM orders p WHERE p.user_id = o.user_id AND o.created_at < p.created_at)"),
    ("a_difference_inside_a_condition_subquery_is_said_once", "sqlite",
     "SELECT COUNT(*) FROM orders o WHERE NOT EXISTS (SELECT 1 FROM refunds r WHERE r.order_id = o.id)",
     "SELECT COUNT(*) FROM orders o WHERE NOT EXISTS (SELECT 1 FROM refunds r WHERE r.order_id = o.id AND r.state = 'done')"),
    ("the_same_item_on_another_layer_is_counted_once_per_layer", "sqlite",
     "SELECT COUNT(*) FROM orders o WHERE o.status = 'paid' AND EXISTS (SELECT 1 FROM orders x WHERE x.user_id = o.user_id AND x.status = 'paid' AND x.id <> o.id)",
     "SELECT COUNT(*) FROM orders o WHERE EXISTS (SELECT 1 FROM orders x WHERE x.user_id = o.user_id AND x.status = 'paid' AND x.id <> o.id)"),
    ("a_self_join_tells_its_two_aliases_apart", "sqlite",
     "SELECT COUNT(*) FROM orders o1 JOIN orders o2 ON o2.id = o1.parent_id WHERE o1.amount > o2.amount",
     "SELECT COUNT(*) FROM orders o1 JOIN orders o2 ON o2.id = o1.parent_id WHERE o2.amount > o1.amount"),
    ("each_dialect_gives_the_same_three_findings", "sqlite",
     "SELECT COUNT(DISTINCT o.order_id) AS \"订单数\" FROM orders o WHERE o.paid_at >= '2026-09-01' AND o.is_deleted <> 1",
     "SELECT COUNT(o.order_id) AS \"订单数\" FROM orders o WHERE o.created_at >= '2026-09-01'"),
    ("distinct_count_versus_plain_count", "sqlite",
     "SELECT COUNT(DISTINCT order_id) FROM orders",
     "SELECT COUNT(order_id) FROM orders"),
    ("side_labels_in_chinese_have_no_stray_spaces", "sqlite",
     "SELECT COUNT(DISTINCT order_id) FROM orders WHERE paid_at >= '2026-09-01'",
     "SELECT COUNT(order_id) FROM orders WHERE created_at >= '2026-09-01'"),
    ("distinct_count_versus_plain_count", "sqlite",
     "SELECT COUNT(order_id) FROM orders",
     "SELECT COUNT(DISTINCT order_id) FROM orders"),
    ("dedup_union_and_how_many_rows_come_out_are_compared", "sqlite",
     "SELECT COUNT(t.user_id) FROM (SELECT user_id FROM orders GROUP BY user_id) t",
     "SELECT COUNT(user_id) FROM orders GROUP BY user_id"),
    ("dedup_union_and_how_many_rows_come_out_are_compared", "sqlite",
     "SELECT DISTINCT region FROM orders",
     "SELECT region FROM orders"),
    ("derived_tables_reading_the_same_table_keep_apart", "sqlite",
     "SELECT SUM(a.amount) FROM orders a JOIN orders b ON b.user_id = a.referrer_id",
     "SELECT SUM(a.amount) FROM orders a JOIN orders b ON b.referrer_id = a.user_id"),
    ("how_an_output_column_is_computed_is_compared", "sqlite",
     "SELECT SUM(amount) - SUM(fee) FROM orders",
     "SELECT SUM(amount), SUM(fee) FROM orders"),
    ("other_aggregate_differences", "sqlite",
     "SELECT SUM(amount) FROM orders",
     "SELECT AVG(amount) FROM orders"),
    ("the_same_output_computation_written_differently_is_no_differ", "sqlite",
     "SELECT SUM(amount) FROM orders",
     "SELECT ROUND(SUM(amount), 2) FROM orders"),
    ("how_an_output_column_is_computed_is_compared", "sqlite",
     "SELECT SUM(amount) FROM orders",
     "SELECT SUM(amount) / 100 FROM orders"),
    ("time_filter_on_one_side_only", "sqlite",
     "SELECT SUM(amount) FROM orders",
     "SELECT SUM(amount) FROM orders WHERE paid_at >= '2026-09-01' AND paid_at < '2026-10-01'"),
    ("an_exclusion_on_one_side", "sqlite",
     "SELECT SUM(amount) FROM orders",
     "SELECT SUM(amount) FROM orders WHERE refund_id IS NOT NULL"),
    ("a_kept_condition_on_one_side", "sqlite",
     "SELECT SUM(amount) FROM orders",
     "SELECT SUM(amount) FROM orders WHERE status = 'paid'"),
    ("which_rows_are_taken_is_compared", "sqlite",
     "SELECT SUM(amount) FROM orders ORDER BY 1 DESC",
     "SELECT SUM(amount) FROM orders"),
    ("an_exclusion_on_one_side", "sqlite",
     "SELECT SUM(amount) FROM orders WHERE is_deleted <> 1",
     "SELECT SUM(amount) FROM orders"),
    ("a_different_source_table_is_said_once", "sqlite",
     "SELECT SUM(amount) FROM orders WHERE is_deleted = 0 AND paid_at >= '2026-09-01'",
     "SELECT SUM(amount) FROM orders_archive WHERE is_deleted = 0 AND paid_at >= '2026-09-01'"),
    ("a_different_source_table_is_said_once", "sqlite",
     "SELECT SUM(amount) FROM orders WHERE is_deleted = 0 AND paid_at >= '2026-09-01'",
     "SELECT SUM(amount) FROM orders_archive WHERE is_deleted = 1 AND paid_at >= '2026-09-01'"),
    ("time_filter_on_one_side_only", "sqlite",
     "SELECT SUM(amount) FROM orders WHERE paid_at >= '2026-09-01' AND paid_at < '2026-10-01'",
     "SELECT SUM(amount) FROM orders"),
    ("time_column_differs_with_catalog_names", "sqlite",
     "SELECT SUM(amount) FROM orders WHERE paid_at >= '2026-09-01' AND paid_at < '2026-10-01'",
     "SELECT SUM(amount) FROM orders WHERE created_at >= '2026-09-01' AND created_at < '2026-10-01'"),
    ("only_literals_differ_is_still_a_different_range", "sqlite",
     "SELECT SUM(amount) FROM orders WHERE paid_at >= '2026-09-01' AND paid_at < '2026-10-01'",
     "SELECT SUM(amount) FROM orders WHERE paid_at >= '2026-09-08' AND paid_at < '2026-10-08'"),
    ("same_time_column_different_range", "sqlite",
     "SELECT SUM(amount) FROM orders WHERE paid_at >= '2026-09-01' AND paid_at < '2026-10-01'",
     "SELECT SUM(amount) FROM orders WHERE paid_at BETWEEN '2026-09-01' AND '2026-09-30'"),
    ("the_same_item_on_another_layer_is_counted_once_per_layer", "sqlite",
     "SELECT SUM(amount) FROM orders WHERE paid_at >= '2026-09-01' AND user_id IN (SELECT user_id FROM orders WHERE paid_at >= '2026-09-01')",
     "SELECT SUM(amount) FROM orders WHERE user_id IN (SELECT user_id FROM orders WHERE paid_at >= '2026-09-01')"),
    ("two_conditions_on_the_same_column_are_paired", "sqlite",
     "SELECT SUM(amount) FROM orders WHERE status = 'paid'",
     "SELECT SUM(amount) FROM orders WHERE status IN ('paid', 'shipped')"),
    ("the_same_item_on_another_layer_is_counted_once_per_layer", "sqlite",
     "SELECT SUM(amount) FROM orders WHERE status = 'paid' AND user_id IN (SELECT user_id FROM orders WHERE status = 'paid')",
     "SELECT SUM(amount) FROM orders WHERE user_id IN (SELECT user_id FROM orders WHERE status = 'paid')"),
    ("an_extra_source_written_in_from_is_named_correctly", "sqlite",
     "SELECT SUM(o.amount) FROM orders o JOIN refunds r ON r.order_id = o.id",
     "SELECT SUM(o.amount) FROM users u JOIN refunds r ON r.user_id = u.id JOIN orders o ON o.id = r.order_id"),
    ("two_columns_that_read_the_same_are_told_apart", "sqlite",
     "SELECT SUM(o.amount) FROM orders o JOIN refunds r ON r.order_id = o.id",
     "SELECT SUM(r.amount) FROM orders o JOIN refunds r ON r.order_id = o.id"),
    ("join_differences", "sqlite",
     "SELECT SUM(o.amount) FROM orders o LEFT JOIN refunds r ON r.order_id = o.id",
     "SELECT SUM(amount) FROM orders"),
    ("join_differences", "sqlite",
     "SELECT SUM(o.amount) FROM orders o LEFT JOIN refunds r ON r.order_id = o.id",
     "SELECT SUM(o.amount) FROM orders o JOIN refunds r ON r.order_id = o.id"),
    ("join_differences", "sqlite",
     "SELECT SUM(o.amount) FROM orders o LEFT JOIN refunds r ON r.order_id = o.id",
     "SELECT SUM(o.amount) FROM orders o LEFT JOIN refunds r ON r.user_id = o.user_id"),
    ("the_same_item_on_another_layer_is_counted_once_per_layer", "sqlite",
     "SELECT SUM(o.amount) FROM orders o WHERE o.amount > (SELECT AVG(amount) FROM orders)",
     "SELECT AVG(o.amount) FROM orders o WHERE o.amount > (SELECT AVG(amount) FROM orders)"),
    ("an_extra_source_written_in_from_is_named_correctly", "sqlite",
     "SELECT SUM(o.amount) FROM orders o WHERE o.status = 'paid'",
     "SELECT SUM(o.amount) FROM stores s JOIN orders o ON o.store_id = s.id WHERE o.status = 'paid'"),
    ("an_extra_source_written_in_from_is_named_correctly", "sqlite",
     "SELECT SUM(o.amount) FROM orders o WHERE o.user_id IN (SELECT id FROM users WHERE vip = 1)",
     "SELECT SUM(o.amount) FROM users u JOIN orders o ON o.user_id = u.id WHERE u.vip = 1"),
    ("a_real_difference_inside_a_subquery_is_still_reported", "sqlite",
     "SELECT SUM(o.amount) FROM orders o WHERE o.user_id IN (SELECT u.id FROM users u WHERE u.vip = 1)",
     "SELECT SUM(o.amount) FROM orders o WHERE o.user_id IN (SELECT u.referrer_id FROM users u WHERE u.vip = 1)"),
    ("which_pass_through_derived_table_a_column_comes_from_is_repo", "sqlite",
     "SELECT SUM(p.amount) FROM (SELECT * FROM orders WHERE status = 'paid') p JOIN (SELECT * FROM orders WHERE status = 'open') o ON o.user_id = p.user_id",
     "SELECT SUM(o.amount) FROM (SELECT * FROM orders WHERE status = 'paid') p JOIN (SELECT * FROM orders WHERE status = 'open') o ON o.user_id = p.user_id"),
    ("which_derived_table_a_column_comes_from_is_reported", "sqlite",
     "SELECT SUM(p.total - r.total) FROM (SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id) p JOIN (SELECT user_id, SUM(amount) AS total FROM refunds GROUP BY user_id) r ON r.user_id = p.user_id",
     "SELECT SUM(r.total - p.total) FROM (SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id) p JOIN (SELECT user_id, SUM(amount) AS total FROM refunds GROUP BY user_id) r ON r.user_id = p.user_id"),
    ("which_derived_table_a_column_comes_from_is_reported", "sqlite",
     "SELECT SUM(p.total) FROM (SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id) p JOIN (SELECT user_id, SUM(amount) AS total FROM refunds GROUP BY user_id) r ON r.user_id = p.user_id",
     "SELECT SUM(r.total) FROM (SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id) p JOIN (SELECT user_id, SUM(amount) AS total FROM refunds GROUP BY user_id) r ON r.user_id = p.user_id"),
    ("other_aggregate_differences", "sqlite",
     "SELECT SUM(paid_amount) FROM orders",
     "SELECT SUM(amount) FROM orders"),
    ("limit_difference", "sqlite",
     "SELECT amount FROM orders LIMIT 10",
     "SELECT amount FROM orders"),
    ("limit_difference", "sqlite",
     "SELECT amount FROM orders LIMIT 10",
     "SELECT amount FROM orders LIMIT 100"),
    ("which_rows_are_taken_is_compared", "sqlite",
     "SELECT amount FROM orders LIMIT 10 OFFSET 0",
     "SELECT amount FROM orders LIMIT 10 OFFSET 10"),
    ("which_rows_are_taken_is_compared", "sqlite",
     "SELECT amount FROM orders ORDER BY 1 DESC LIMIT 10",
     "SELECT amount FROM orders ORDER BY 1 ASC LIMIT 10"),
    ("which_rows_are_taken_is_compared", "sqlite",
     "SELECT amount FROM orders ORDER BY amount DESC LIMIT 10",
     "SELECT amount FROM orders LIMIT 10"),
    ("a_correlated_subquery_on_the_outer_table_tells_inner_from_ou", "sqlite",
     "SELECT o.id, (SELECT COUNT(*) FROM orders c WHERE c.parent_id = o.id) AS n FROM orders o",
     "SELECT o.id, (SELECT COUNT(*) FROM orders c WHERE c.id = o.parent_id) AS n FROM orders o"),
    ("how_an_output_column_is_computed_is_compared", "sqlite",
     "SELECT order_id, SUM(amount) OVER (PARTITION BY user_id) AS s FROM orders",
     "SELECT order_id, SUM(amount) OVER (PARTITION BY store_id) AS s FROM orders"),
    ("which_pass_through_derived_table_a_column_comes_from_is_repo", "sqlite",
     "SELECT p.id FROM (SELECT * FROM orders WHERE status = 'paid') p JOIN (SELECT * FROM orders WHERE status = 'open') o ON o.user_id = p.user_id ORDER BY p.created_at DESC LIMIT 10",
     "SELECT p.id FROM (SELECT * FROM orders WHERE status = 'paid') p JOIN (SELECT * FROM orders WHERE status = 'open') o ON o.user_id = p.user_id ORDER BY o.created_at DESC LIMIT 10"),
    ("which_derived_table_a_column_comes_from_is_reported", "sqlite",
     "SELECT p.user_id FROM (SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id) p JOIN (SELECT user_id, SUM(amount) AS total FROM refunds GROUP BY user_id) r ON r.user_id = p.user_id GROUP BY p.user_id HAVING MAX(p.total) > 10",
     "SELECT p.user_id FROM (SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id) p JOIN (SELECT user_id, SUM(amount) AS total FROM refunds GROUP BY user_id) r ON r.user_id = p.user_id GROUP BY p.user_id HAVING MAX(r.total) > 10"),
    ("which_derived_table_a_column_comes_from_is_reported", "sqlite",
     "SELECT p.user_id FROM (SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id) p JOIN (SELECT user_id, SUM(amount) AS total FROM refunds GROUP BY user_id) r ON r.user_id = p.user_id ORDER BY p.total DESC LIMIT 10",
     "SELECT p.user_id FROM (SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id) p JOIN (SELECT user_id, SUM(amount) AS total FROM refunds GROUP BY user_id) r ON r.user_id = p.user_id ORDER BY r.total DESC LIMIT 10"),
    ("group_by_difference", "sqlite",
     "SELECT region, SUM(amount) FROM orders GROUP BY region",
     "SELECT region, city, SUM(amount) FROM orders GROUP BY region, city"),
    ("having_difference", "sqlite",
     "SELECT region, SUM(amount) FROM orders GROUP BY region HAVING SUM(amount) > 100",
     "SELECT region, SUM(amount) FROM orders GROUP BY region"),
    ("the_same_rows_written_differently_are_no_difference", "sqlite",
     "SELECT user_id FROM orders WHERE status = 'paid' UNION SELECT referrer_id FROM orders",
     "SELECT * FROM (SELECT user_id FROM orders WHERE status = 'paid' UNION SELECT referrer_id FROM orders) t"),
    ("the_same_item_on_another_layer_is_counted_once_per_layer", "sqlite",
     "SELECT user_id FROM orders WHERE status = 'paid' UNION SELECT referrer_id FROM orders WHERE status = 'paid'",
     "SELECT user_id FROM orders WHERE status = 'paid' UNION SELECT referrer_id FROM orders"),
    ("the_same_item_on_another_layer_is_counted_once_per_layer", "sqlite",
     "SELECT user_id, COUNT(*) FROM orders WHERE user_id IN (SELECT user_id FROM orders GROUP BY user_id HAVING COUNT(*) > 1) GROUP BY user_id",
     "SELECT COUNT(*) FROM orders WHERE user_id IN (SELECT user_id FROM orders GROUP BY user_id HAVING COUNT(*) > 1)"),
    ("the_same_item_on_another_layer_is_counted_once_per_layer", "sqlite",
     "WITH a AS (SELECT * FROM orders WHERE is_deleted = 0 AND status = 'paid'), b AS (SELECT * FROM orders WHERE is_deleted = 0 AND status = 'open') SELECT COUNT(*) FROM a JOIN b ON b.user_id = a.user_id",
     "WITH a AS (SELECT * FROM orders WHERE is_deleted = 0 AND status = 'paid'), b AS (SELECT * FROM orders WHERE status = 'open') SELECT COUNT(*) FROM a JOIN b ON b.user_id = a.user_id"),
    ("derived_tables_reading_the_same_table_keep_apart", "sqlite",
     "WITH base AS (SELECT * FROM orders WHERE is_deleted = 0), paid AS (SELECT * FROM base WHERE status = 'paid'), open AS (SELECT * FROM base WHERE status = 'open') SELECT COUNT(*) FROM paid p JOIN open o ON o.user_id = p.user_id WHERE p.amount > 100",
     "WITH base AS (SELECT * FROM orders WHERE is_deleted = 0), paid AS (SELECT * FROM base WHERE status = 'paid'), open AS (SELECT * FROM base WHERE status = 'open') SELECT COUNT(*) FROM paid p JOIN open o ON o.user_id = p.user_id WHERE o.amount > 100"),
    ("a_correlated_subquery_on_the_outer_table_tells_inner_from_ou", "sqlite",
     "WITH p AS (SELECT * FROM orders WHERE status = 'paid') SELECT COUNT(*) FROM p WHERE EXISTS (SELECT 1 FROM orders c WHERE c.parent_id = p.id)",
     "WITH p AS (SELECT * FROM orders WHERE status = 'paid') SELECT COUNT(*) FROM p WHERE EXISTS (SELECT 1 FROM orders c WHERE c.id = p.parent_id)"),
    ("a_cte_defined_but_not_used_does_not_count", "sqlite",
     "WITH paid AS (SELECT * FROM orders WHERE status = 'paid') SELECT SUM(amount) FROM paid",
     "WITH paid AS (SELECT * FROM orders WHERE status = 'paid') SELECT SUM(amount) FROM orders"),
    ("which_pass_through_derived_table_a_column_comes_from_is_repo", "sqlite",
     "WITH paid AS (SELECT * FROM orders WHERE status = 'paid'), open AS (SELECT * FROM orders WHERE status = 'open') SELECT COUNT(*) FROM paid p JOIN open o ON o.user_id = p.user_id GROUP BY p.store_id",
     "WITH paid AS (SELECT * FROM orders WHERE status = 'paid'), open AS (SELECT * FROM orders WHERE status = 'open') SELECT COUNT(*) FROM paid p JOIN open o ON o.user_id = p.user_id GROUP BY o.store_id"),
    ("which_pass_through_derived_table_a_column_comes_from_is_repo", "sqlite",
     "WITH paid AS (SELECT * FROM orders WHERE status = 'paid'), open AS (SELECT * FROM orders WHERE status = 'open') SELECT COUNT(*) FROM paid p JOIN open o ON o.user_id = p.user_id WHERE p.amount > 100",
     "WITH paid AS (SELECT * FROM orders WHERE status = 'paid'), open AS (SELECT * FROM orders WHERE status = 'open') SELECT COUNT(*) FROM paid p JOIN open o ON o.user_id = p.user_id WHERE o.amount > 100"),
    ("which_pass_through_derived_table_a_column_comes_from_is_repo", "sqlite",
     "WITH paid AS (SELECT * FROM orders WHERE status = 'paid'), open AS (SELECT * FROM orders WHERE status = 'open') SELECT COUNT(*) FROM paid p JOIN open o ON o.user_id = p.user_id WHERE p.created_at >= '2026-09-01'",
     "WITH paid AS (SELECT * FROM orders WHERE status = 'paid'), open AS (SELECT * FROM orders WHERE status = 'open') SELECT COUNT(*) FROM paid p JOIN open o ON o.user_id = p.user_id WHERE o.created_at >= '2026-09-01'"),
    ("derived_tables_reading_the_same_table_keep_apart", "sqlite",
     "WITH paid AS (SELECT * FROM orders WHERE status = 'paid'), open AS (SELECT * FROM orders WHERE status = 'open') SELECT SUM(d) FROM (SELECT p.amount - o.amount AS d FROM paid p JOIN open o ON o.user_id = p.user_id) x",
     "WITH paid AS (SELECT * FROM orders WHERE status = 'paid'), open AS (SELECT * FROM orders WHERE status = 'open') SELECT SUM(d) FROM (SELECT o.amount - p.amount AS d FROM paid p JOIN open o ON o.user_id = p.user_id) x"),
    ("which_pass_through_derived_table_a_column_comes_from_is_repo", "sqlite",
     "WITH paid AS (SELECT * FROM orders WHERE status = 'paid'), open AS (SELECT * FROM orders WHERE status = 'open') SELECT SUM(p.amount) FROM paid p JOIN open o ON o.user_id = p.user_id",
     "WITH paid AS (SELECT * FROM orders WHERE status = 'paid'), open AS (SELECT * FROM orders WHERE status = 'open') SELECT SUM(o.amount) FROM paid p JOIN open o ON o.user_id = p.user_id"),
    ("which_pass_through_derived_table_a_column_comes_from_is_repo", "sqlite",
     "WITH paid AS (SELECT * FROM orders WHERE status = 'paid'), open AS (SELECT * FROM orders WHERE status = 'open') SELECT p.id FROM paid p JOIN open o ON o.user_id = p.user_id ORDER BY p.created_at DESC LIMIT 10",
     "WITH paid AS (SELECT * FROM orders WHERE status = 'paid'), open AS (SELECT * FROM orders WHERE status = 'open') SELECT p.id FROM paid p JOIN open o ON o.user_id = p.user_id ORDER BY o.created_at DESC LIMIT 10"),
    ("how_a_derived_column_is_computed_is_reported", "sqlite",
     "WITH paid AS (SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id), refunded AS (SELECT user_id, SUM(amount) AS total FROM refunds GROUP BY user_id) , net AS (SELECT p.user_id, p.total - r.total AS v FROM paid p JOIN refunded r ON r.user_id = p.user_id) SELECT SUM(v) FROM net",
     "WITH paid AS (SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id), refunded AS (SELECT user_id, SUM(amount) AS total FROM refunds GROUP BY user_id) , net AS (SELECT p.user_id, r.total - p.total AS v FROM paid p JOIN refunded r ON r.user_id = p.user_id) SELECT SUM(v) FROM net"),
    ("which_derived_table_a_column_comes_from_is_reported", "sqlite",
     "WITH paid AS (SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id), refunded AS (SELECT user_id, SUM(amount) AS total FROM refunds GROUP BY user_id) SELECT COUNT(*) FROM paid p JOIN refunded r ON r.user_id = p.user_id WHERE p.total - r.total > 100",
     "WITH paid AS (SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id), refunded AS (SELECT user_id, SUM(amount) AS total FROM refunds GROUP BY user_id) SELECT COUNT(*) FROM paid p JOIN refunded r ON r.user_id = p.user_id WHERE r.total - p.total > 100"),
    ("which_derived_table_a_column_comes_from_is_reported", "sqlite",
     "WITH paid AS (SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id), refunded AS (SELECT user_id, SUM(amount) AS total FROM refunds GROUP BY user_id) SELECT COUNT(*) FROM paid p JOIN refunded r ON r.user_id = p.user_id WHERE p.total > 100",
     "WITH paid AS (SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id), refunded AS (SELECT user_id, SUM(amount) AS total FROM refunds GROUP BY user_id) SELECT COUNT(*) FROM paid p JOIN refunded r ON r.user_id = p.user_id WHERE r.total > 100"),
    ("a_reversed_comparison_between_two_derived_tables_is_still_re", "sqlite",
     "WITH paid AS (SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id), refunded AS (SELECT user_id, SUM(amount) AS total FROM refunds GROUP BY user_id) SELECT COUNT(*) FROM paid p JOIN refunded r ON r.user_id = p.user_id WHERE p.total > r.total",
     "WITH paid AS (SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id), refunded AS (SELECT user_id, SUM(amount) AS total FROM refunds GROUP BY user_id) SELECT COUNT(*) FROM paid p JOIN refunded r ON r.user_id = p.user_id WHERE r.total > p.total"),
    ("an_extra_source_written_in_from_is_named_correctly", "sqlite",
     "WITH paid AS (SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id), refunded AS (SELECT user_id, SUM(amount) AS total FROM refunds GROUP BY user_id) SELECT COUNT(*) FROM paid p JOIN refunded r ON r.user_id = p.user_id WHERE p.total > r.total",
     "WITH vip AS (SELECT id FROM users WHERE vip = 1), paid AS (SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id), refunded AS (SELECT user_id, SUM(amount) AS total FROM refunds GROUP BY user_id) SELECT COUNT(*) FROM vip v JOIN refunded r ON r.user_id = v.id JOIN paid p ON p.user_id = r.user_id WHERE p.total > r.total"),
    ("unused_ctes_take_part_in_pairing", "sqlite",
     "WITH paid AS (SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id), refunded AS (SELECT user_id, SUM(amount) AS total FROM refunds GROUP BY user_id) SELECT COUNT(*) FROM users u JOIN paid p ON p.user_id = u.id",
     "WITH paid AS (SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id), refunded AS (SELECT user_id, SUM(amount) AS total FROM refunds GROUP BY user_id) SELECT COUNT(*) FROM users u JOIN refunded r ON r.user_id = u.id"),
    ("which_derived_table_a_column_comes_from_is_reported", "sqlite",
     "WITH paid AS (SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id), refunded AS (SELECT user_id, SUM(amount) AS total FROM refunds GROUP BY user_id) SELECT SUM(p.total - r.total) FROM paid p JOIN refunded r ON r.user_id = p.user_id",
     "WITH paid AS (SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id), refunded AS (SELECT user_id, SUM(amount) AS total FROM refunds GROUP BY user_id) SELECT SUM(r.total - p.total) FROM paid p JOIN refunded r ON r.user_id = p.user_id"),
    ("how_an_output_column_is_computed_is_compared", "sqlite",
     "WITH paid AS (SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id), refunded AS (SELECT user_id, SUM(amount) AS total FROM refunds GROUP BY user_id) SELECT SUM(p.total) - SUM(r.total) FROM paid p JOIN refunded r ON r.user_id = p.user_id",
     "WITH paid AS (SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id), refunded AS (SELECT user_id, SUM(amount) AS total FROM refunds GROUP BY user_id) SELECT SUM(r.total) - SUM(p.total) FROM paid p JOIN refunded r ON r.user_id = p.user_id"),
    ("which_derived_table_a_column_comes_from_is_reported", "sqlite",
     "WITH paid AS (SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id), refunded AS (SELECT user_id, SUM(amount) AS total FROM refunds GROUP BY user_id) SELECT SUM(p.total) FROM paid p JOIN refunded r ON r.user_id = p.user_id",
     "WITH paid AS (SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id), refunded AS (SELECT user_id, SUM(amount) AS total FROM refunds GROUP BY user_id) SELECT SUM(r.total) FROM paid p JOIN refunded r ON r.user_id = p.user_id"),
    ("renamed_and_reordered_derived_tables_still_tell_their_column", "sqlite",
     "WITH paid AS (SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id), refunded AS (SELECT user_id, SUM(amount) AS total FROM refunds GROUP BY user_id) SELECT SUM(p.total) FROM paid p JOIN refunded r ON r.user_id = p.user_id",
     "WITH ref_sum AS (SELECT user_id, SUM(amount) AS total FROM refunds GROUP BY user_id), pay_sum AS (SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id) SELECT SUM(r.total) FROM ref_sum r JOIN pay_sum p ON p.user_id = r.user_id"),
    ("which_derived_table_a_column_comes_from_is_reported", "sqlite",
     "WITH paid AS (SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id), refunded AS (SELECT user_id, SUM(amount) AS total FROM refunds GROUP BY user_id) SELECT p.user_id FROM paid p JOIN refunded r ON r.user_id = p.user_id GROUP BY p.user_id HAVING MAX(p.total) > 10",
     "WITH paid AS (SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id), refunded AS (SELECT user_id, SUM(amount) AS total FROM refunds GROUP BY user_id) SELECT p.user_id FROM paid p JOIN refunded r ON r.user_id = p.user_id GROUP BY p.user_id HAVING MAX(r.total) > 10"),
    ("which_derived_table_a_column_comes_from_is_reported", "sqlite",
     "WITH paid AS (SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id), refunded AS (SELECT user_id, SUM(amount) AS total FROM refunds GROUP BY user_id) SELECT p.user_id FROM paid p JOIN refunded r ON r.user_id = p.user_id ORDER BY p.total DESC LIMIT 10",
     "WITH paid AS (SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id), refunded AS (SELECT user_id, SUM(amount) AS total FROM refunds GROUP BY user_id) SELECT p.user_id FROM paid p JOIN refunded r ON r.user_id = p.user_id ORDER BY r.total DESC LIMIT 10"),
    ("same_named_derived_tables_that_differ_only_in_conditions_say", "sqlite",
     "WITH paid AS (SELECT user_id, SUM(amount) AS total FROM orders WHERE status = 'paid' GROUP BY user_id), open AS (SELECT user_id, SUM(amount) AS total FROM orders WHERE status = 'open' GROUP BY user_id) SELECT COUNT(*) FROM paid p JOIN open o ON o.user_id = p.user_id WHERE p.total > 100",
     "WITH paid AS (SELECT user_id, SUM(amount) AS total FROM orders WHERE status = 'open' AND is_deleted = 0 GROUP BY user_id), open AS (SELECT user_id, SUM(amount) AS total FROM orders WHERE status = 'paid' AND is_deleted = 0 GROUP BY user_id) SELECT COUNT(*) FROM paid p JOIN open o ON o.user_id = p.user_id WHERE p.total > 100"),
    ("two_same_shape_derived_tables_are_paired_by_their_conditions", "sqlite",
     "WITH paid AS (SELECT user_id, SUM(amount) AS total FROM orders WHERE status = 'paid' GROUP BY user_id), open AS (SELECT user_id, SUM(amount) AS total FROM orders WHERE status = 'open' GROUP BY user_id) SELECT COUNT(*) FROM paid p JOIN open o ON o.user_id = p.user_id WHERE p.total > o.total",
     "WITH unpaid AS (SELECT user_id, SUM(amount) AS total FROM orders WHERE status = 'open' AND is_deleted = 0 GROUP BY user_id), settled AS (SELECT user_id, SUM(amount) AS total FROM orders WHERE status = 'paid' AND is_deleted = 0 GROUP BY user_id) SELECT COUNT(*) FROM unpaid u JOIN settled s ON s.user_id = u.user_id WHERE u.total < s.total"),
    ("derived_tables_reading_the_same_table_keep_apart", "sqlite",
     "WITH paid AS (SELECT user_id, referrer_id, amount FROM orders WHERE status = 'paid'), open AS (SELECT user_id, referrer_id, amount FROM orders WHERE status = 'open') SELECT SUM(p.amount) FROM paid p JOIN open o ON p.user_id = o.referrer_id",
     "WITH paid AS (SELECT user_id, referrer_id, amount FROM orders WHERE status = 'paid'), open AS (SELECT user_id, referrer_id, amount FROM orders WHERE status = 'open') SELECT SUM(p.amount) FROM paid p JOIN open o ON p.referrer_id = o.user_id"),
    ("the_same_item_on_another_layer_is_counted_once_per_layer", "sqlite",
     "WITH pu AS (SELECT user_id, COUNT(*) AS n FROM orders WHERE status = 'paid' GROUP BY user_id) SELECT SUM(o.amount) FROM orders o JOIN pu ON pu.user_id = o.user_id WHERE o.status = 'paid'",
     "WITH pu AS (SELECT user_id, COUNT(*) AS n FROM orders WHERE status = 'paid' GROUP BY user_id) SELECT SUM(o.amount) FROM orders o JOIN pu ON pu.user_id = o.user_id"),
    ("filtering_before_or_after_a_window_or_limit_is_reported", "sqlite",
     "WITH t AS (SELECT * FROM orders ORDER BY amount DESC LIMIT 10) SELECT SUM(amount) FROM t WHERE paid_at >= '2026-09-01'",
     "WITH t AS (SELECT * FROM orders WHERE paid_at >= '2026-09-01' ORDER BY amount DESC LIMIT 10) SELECT SUM(amount) FROM t"),
    ("filtering_before_or_after_a_window_or_limit_is_reported", "sqlite",
     "WITH t AS (SELECT * FROM orders ORDER BY amount DESC LIMIT 10) SELECT SUM(amount) FROM t WHERE status = 'paid'",
     "WITH t AS (SELECT * FROM orders WHERE status = 'paid' ORDER BY amount DESC LIMIT 10) SELECT SUM(amount) FROM t"),
    ("derived_tables_reading_the_same_table_keep_apart", "sqlite",
     "WITH t AS (SELECT * FROM orders WHERE status = 'paid') SELECT SUM(a.amount) FROM t a JOIN t b ON b.user_id = a.referrer_id",
     "WITH t AS (SELECT * FROM orders WHERE status = 'paid') SELECT SUM(a.amount) FROM t a JOIN t b ON b.referrer_id = a.user_id"),
    ("derived_tables_reading_the_same_table_keep_apart", "sqlite",
     "WITH t AS (SELECT * FROM orders WHERE status = 'paid') SELECT SUM(a.amount) FROM t a JOIN t b ON b.user_id = a.referrer_id",
     "WITH t AS (SELECT * FROM orders WHERE status = 'paid') SELECT SUM(b.amount) FROM t a JOIN t b ON b.user_id = a.referrer_id"),
    ("derived_tables_reading_the_same_table_keep_apart", "sqlite",
     "WITH t AS (SELECT * FROM orders WHERE status = 'paid') SELECT SUM(p.amount) FROM t p JOIN orders c ON c.parent_id = p.id",
     "WITH t AS (SELECT * FROM orders WHERE status = 'paid') SELECT SUM(c.amount) FROM t p JOIN orders c ON c.parent_id = p.id"),
    ("time_columns_inside_derived_tables", "sqlite",
     "WITH t AS (SELECT amount, DATE(paid_at) AS d FROM orders) SELECT SUM(amount) FROM t WHERE d >= '2026-09-01'",
     "WITH t AS (SELECT amount, DATE(paid_at, '+8 hours') AS d FROM orders) SELECT SUM(amount) FROM t WHERE d >= '2026-09-01'"),
    ("a_shifted_derived_number_is_no_difference_in_any_aspect", "sqlite",
     "WITH t AS (SELECT store_id, user_id, SUM(amount) AS total FROM orders GROUP BY store_id, user_id) SELECT store_id FROM t WHERE user_id IN (SELECT id FROM users WHERE vip = 1) GROUP BY store_id HAVING MAX(total) > 100",
     "WITH vip AS (SELECT id FROM users WHERE vip = 1), t AS (SELECT store_id, user_id, SUM(amount) AS total FROM orders GROUP BY store_id, user_id) SELECT t.store_id FROM vip JOIN t ON t.user_id = vip.id GROUP BY t.store_id HAVING MAX(t.total) > 100"),
    ("how_a_derived_column_is_computed_is_reported", "sqlite",
     "WITH t AS (SELECT strftime('%Y-%m', paid_at) AS m, amount FROM orders) SELECT m, SUM(amount) FROM t GROUP BY m",
     "WITH t AS (SELECT strftime('%Y-%m', created_at) AS m, amount FROM orders) SELECT m, SUM(amount) FROM t GROUP BY m"),
    ("a_shifted_derived_number_is_no_difference_in_any_aspect", "sqlite",
     "WITH t AS (SELECT user_id, DATE(paid_at) AS day, amount FROM orders) SELECT SUM(amount) FROM t WHERE day >= '2026-09-01' AND user_id IN (SELECT id FROM users WHERE vip = 1)",
     "WITH vip AS (SELECT id FROM users WHERE vip = 1), t AS (SELECT user_id, DATE(paid_at) AS day, amount FROM orders) SELECT SUM(t.amount) FROM vip JOIN t ON t.user_id = vip.id WHERE t.day >= '2026-09-01'"),
    ("a_shifted_derived_number_is_no_difference_in_any_aspect", "sqlite",
     "WITH t AS (SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id) SELECT AVG(total) FROM t WHERE user_id IN (SELECT id FROM users WHERE vip = 1)",
     "WITH vip AS (SELECT id FROM users WHERE vip = 1), t AS (SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id) SELECT AVG(t.total) FROM vip JOIN t ON t.user_id = vip.id"),
    ("a_changed_column_inside_a_renamed_derived_table_is_said_once", "sqlite",
     "WITH t AS (SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id) SELECT COUNT(*) FROM t WHERE total > 100",
     "WITH vip AS (SELECT id FROM users WHERE vip = 1), x AS (SELECT user_id, SUM(paid_amount) AS total FROM orders GROUP BY user_id) SELECT COUNT(*) FROM vip JOIN x ON x.user_id = vip.id WHERE total > 100"),
    ("derived_columns_renamed_on_one_side_are_paired", "sqlite",
     "WITH t AS (SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id) SELECT COUNT(*) FROM t WHERE total > 100",
     "WITH x AS (SELECT user_id, SUM(amount - discount) AS net_total FROM orders GROUP BY user_id) SELECT COUNT(*) FROM x WHERE net_total > 100"),
    ("a_shifted_derived_number_does_not_split_one_condition_in_two", "sqlite",
     "WITH t AS (SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id) SELECT COUNT(*) FROM t WHERE total > 100 AND user_id IN (SELECT id FROM users WHERE vip = 1)",
     "WITH vip AS (SELECT id FROM users WHERE vip = 1), t AS (SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id) SELECT COUNT(*) FROM vip JOIN t ON t.user_id = vip.id WHERE t.total > 100"),
    ("a_paired_derived_join_with_another_name_says_both_names", "sqlite",
     "WITH t AS (SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id) SELECT COUNT(*) FROM users u JOIN t ON t.user_id = u.id",
     "WITH totals AS (SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id) SELECT COUNT(*) FROM users u JOIN totals ON totals.user_id = u.referrer_id"),
    ("an_extra_derived_table_on_one_side_is_not_paired_with_anothe", "sqlite",
     "WITH t AS (SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id) SELECT COUNT(*) FROM users u JOIN t ON t.user_id = u.id",
     "WITH vip AS (SELECT id FROM users WHERE vip = 1), t AS (SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id) SELECT COUNT(*) FROM users u JOIN vip ON vip.id = u.id JOIN t ON t.user_id = u.referrer_id"),
    ("a_shifted_derived_join_is_paired_with_its_counterpart", "sqlite",
     "WITH t AS (SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id) SELECT COUNT(*) FROM users u JOIN t ON t.user_id = u.id WHERE t.total > 100",
     "WITH vip AS (SELECT id FROM users WHERE vip = 1), t AS (SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id) SELECT COUNT(*) FROM users u JOIN vip ON vip.id = u.id JOIN t ON t.user_id = u.id WHERE t.total > 100"),
    ("a_shifted_derived_number_is_no_difference_in_any_aspect", "sqlite",
     "WITH t AS (SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id) SELECT user_id FROM t WHERE user_id IN (SELECT id FROM users WHERE vip = 1) ORDER BY total DESC LIMIT 10",
     "WITH vip AS (SELECT id FROM users WHERE vip = 1), t AS (SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id) SELECT t.user_id FROM vip JOIN t ON t.user_id = vip.id ORDER BY t.total DESC LIMIT 10"),
    ("a_shifted_derived_number_is_no_difference_in_any_aspect", "sqlite",
     "WITH t AS (SELECT user_id, SUM(amount) AS total, COUNT(*) AS n FROM orders GROUP BY user_id) SELECT n, COUNT(*) FROM t WHERE user_id IN (SELECT id FROM users WHERE vip = 1) GROUP BY n",
     "WITH vip AS (SELECT id FROM users WHERE vip = 1), t AS (SELECT user_id, SUM(amount) AS total, COUNT(*) AS n FROM orders GROUP BY user_id) SELECT t.n, COUNT(*) FROM vip JOIN t ON t.user_id = vip.id GROUP BY t.n"),
    ("how_a_derived_column_is_computed_is_reported", "sqlite",
     "WITH t AS (SELECT user_id, amount * 0.9 AS net FROM orders) SELECT * FROM t",
     "WITH t AS (SELECT user_id, amount * 0.8 AS net FROM orders) SELECT * FROM t"),
    ("how_a_derived_column_is_computed_is_reported", "sqlite",
     "WITH t AS (SELECT user_id, amount * 0.9 AS net FROM orders) SELECT SUM(net) FROM t",
     "WITH t AS (SELECT user_id, amount * 0.8 AS net FROM orders) SELECT SUM(net) FROM t"),
    ("a_time_column_computed_in_a_cte_is_seen_through", "sqlite",
     "WITH t AS (SELECT user_id, amount, DATE(paid_at) AS d FROM orders) SELECT SUM(amount) FROM t WHERE d >= '2026-09-01'",
     "WITH t AS (SELECT user_id, amount, DATE(created_at) AS d FROM orders) SELECT SUM(amount) FROM t WHERE d >= '2026-09-01'"),
    ("which_rows_are_taken_is_compared", "sqlite",
     "WITH top AS (SELECT amount FROM orders ORDER BY amount DESC LIMIT 10) SELECT SUM(amount) FROM top",
     "WITH top AS (SELECT amount FROM orders ORDER BY amount DESC LIMIT 100) SELECT SUM(amount) FROM top"),
    ("limits_inside_derived_tables_are_paired_by_structure", "sqlite",
     "WITH top AS (SELECT user_id, amount FROM orders ORDER BY amount DESC LIMIT 10) SELECT SUM(amount) FROM top",
     "WITH vip AS (SELECT id FROM users WHERE vip = 1 LIMIT 5), top AS (SELECT user_id, amount FROM orders ORDER BY amount DESC LIMIT 10) SELECT SUM(top.amount) FROM top JOIN vip ON vip.id = top.user_id"),
    ("a_real_difference_next_to_renamed_derived_tables_is_reported", "sqlite",
     "WITH user_totals AS (SELECT user_id, SUM(amount) AS total FROM orders WHERE is_deleted = 0 GROUP BY user_id) SELECT COUNT(*) FROM user_totals WHERE total > 100",
     "WITH t AS (SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id) SELECT COUNT(*) FROM t WHERE total > 100"),
]

#: 过去复验者自己造的 SQL 对里，在这几组数据上结果不同的（SQLite，进执行比对）
VALIDATOR_DIFF: list[tuple[str, str, str]] = [
    ("cases.py:A29a",
     "SELECT COUNT(*) FROM (SELECT DISTINCT user_id FROM orders WHERE status = 'paid') t",
     "SELECT COUNT(DISTINCT user_id) FROM orders WHERE status = 'paid'"),
    ("cases.py:A29b",
     "SELECT COUNT(*) FROM (SELECT DISTINCT user_id FROM orders WHERE status = 'paid') t",
     "SELECT COUNT(user_id) FROM orders WHERE status = 'paid'"),
    ("real1.py:W08 distinct in subq vs none",
     "SELECT COUNT(*) FROM (SELECT DISTINCT user_id FROM orders) t",
     "SELECT COUNT(*) FROM (SELECT user_id FROM orders) t"),
    ("exec_check3.py:E4",
     "SELECT COUNT(*) FROM (SELECT DISTINCT user_id, store_id FROM orders) t",
     "SELECT COUNT(*) FROM (SELECT DISTINCT user_id FROM orders) t"),
    ("batch4.py:N1 nested 3-level DISTINCT in middle",
     "SELECT COUNT(*) FROM (SELECT user_id FROM (SELECT DISTINCT user_id, store_id FROM orders) a) b",
     "SELECT COUNT(*) FROM (SELECT user_id FROM (SELECT user_id, store_id FROM orders) a) b"),
    ("exec_check.py:F2",
     "SELECT COUNT(*) FROM (SELECT user_id FROM orders EXCEPT SELECT user_id FROM refunds) t",
     "SELECT COUNT(*) FROM (SELECT user_id FROM refunds EXCEPT SELECT user_id FROM orders) t"),
    ("real1.py:W09 grain",
     "SELECT COUNT(*) FROM (SELECT user_id FROM orders GROUP BY user_id) t",
     "SELECT user_id, COUNT(*) FROM orders GROUP BY user_id"),
    ("exec_check3.py:F3",
     "SELECT COUNT(*) FROM (SELECT user_id FROM orders INTERSECT SELECT user_id FROM refunds) t",
     "SELECT COUNT(*) FROM (SELECT user_id FROM orders EXCEPT SELECT user_id FROM refunds) t"),
    ("exec_check.py:F1",
     "SELECT COUNT(*) FROM (SELECT user_id FROM orders UNION SELECT user_id FROM refunds) t",
     "SELECT COUNT(*) FROM (SELECT user_id FROM orders UNION ALL SELECT user_id FROM refunds) t"),
    ("cases.py:mysql",
     "SELECT COUNT(*) FROM (SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id) p LEFT JOIN (SELECT user_id, SUM(amount) AS total FROM refunds GROUP BY user_id) r ON r.user_id = p.user_id",
     "SELECT COUNT(*) FROM (SELECT user_id, SUM(amount) AS total FROM refunds GROUP BY user_id) r LEFT JOIN (SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id) p ON p.user_id = r.user_id"),
    ("cases.py:postgres",
     "SELECT COUNT(*) FROM (SELECT user_id, status, ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY paid_at) AS rn FROM orders) t WHERE t.rn = 1 AND t.status = 'paid'",
     "SELECT COUNT(*) FROM (SELECT user_id, status, ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY paid_at) AS rn FROM orders WHERE status = 'paid') t WHERE t.rn = 1"),
    ("batch3.py:K2 extra time col on one side; shared paid_at uppe",
     "SELECT COUNT(*) FROM orders WHERE paid_at >= '2026-09-01' AND paid_at < '2026-10-01'",
     "SELECT COUNT(*) FROM orders WHERE created_at >= '2026-01-01' AND paid_at >= '2026-09-01' AND paid_at < '2026-11-01'"),
    ("p123c.py:Z08 real diff in 3-table chain same order (wording)",
     "SELECT COUNT(*) FROM orders o JOIN refunds r ON r.order_id = o.id JOIN users u ON u.id = o.user_id",
     "SELECT COUNT(*) FROM orders o JOIN users u ON u.id = o.user_id JOIN refunds r ON r.user_id = u.id"),
    ("p123c.py:Z07 real diff in 3-table chain (wording)",
     "SELECT COUNT(*) FROM orders o JOIN refunds r ON r.order_id = o.id JOIN users u ON u.id = o.user_id",
     "SELECT COUNT(*) FROM users u JOIN orders o ON o.user_id = u.id JOIN refunds r ON r.user_id = u.id"),
    ("p123d.py:Z10 real diff + permutation (tables)",
     "SELECT COUNT(*) FROM orders o JOIN refunds r ON r.order_id = o.id JOIN users u ON u.id = o.user_id",
     "SELECT COUNT(*) FROM users u JOIN refunds r ON r.user_id = u.id JOIN orders o ON o.id = r.order_id"),
    ("real1.py:W03 join swap + LEFT vs INNER",
     "SELECT COUNT(*) FROM orders o LEFT JOIN refunds r ON r.order_id = o.id",
     "SELECT COUNT(*) FROM refunds r JOIN orders o ON o.id = r.order_id"),
    ("cases.py:postgres",
     "SELECT COUNT(*) FROM orders o RIGHT JOIN refunds r ON r.order_id = o.id",
     "SELECT COUNT(*) FROM refunds r RIGHT JOIN orders o ON o.id = r.order_id"),
    ("batch2.py:C4 amount above own-user avg vs below (correlated ",
     "SELECT COUNT(*) FROM orders o WHERE o.amount > (SELECT AVG(x.amount) FROM orders x WHERE x.user_id = o.user_id)",
     "SELECT COUNT(*) FROM orders o WHERE (SELECT AVG(x.amount) FROM orders x WHERE x.user_id = o.user_id) > o.amount"),
    ("real1.py:W01 alias + value",
     "SELECT COUNT(*) FROM orders o WHERE o.status = 'paid'",
     "SELECT COUNT(*) FROM orders x WHERE x.status = 'done'"),
    ("real1.py:W10 extra cond in outer vs IN subq",
     "SELECT COUNT(*) FROM orders o WHERE o.status = 'paid' AND o.user_id IN (SELECT p.user_id FROM orders p WHERE p.status = 'paid')",
     "SELECT COUNT(*) FROM orders o WHERE o.user_id IN (SELECT p.user_id FROM orders p WHERE p.status = 'paid')"),
    ("exec_check3.py:SJ2",
     "SELECT COUNT(*) FROM orders o1 JOIN orders o2 ON o2.user_id = o1.user_id AND o2.created_at > o1.created_at WHERE o1.amount > 100",
     "SELECT COUNT(*) FROM orders o1 JOIN orders o2 ON o2.user_id = o1.user_id AND o2.created_at > o1.created_at WHERE o2.amount > 100"),
    ("real2.py:X02 table self-join role reversed, WHERE",
     "SELECT COUNT(*) FROM users a JOIN users b ON b.id = a.referrer_id WHERE a.vip = 1",
     "SELECT COUNT(*) FROM users a JOIN users b ON a.id = b.referrer_id WHERE a.vip = 1"),
    ("real1.py:W04 self-join direction reversed",
     "SELECT COUNT(*) FROM users a JOIN users b ON b.id = a.referrer_id WHERE a.vip = 1",
     "SELECT COUNT(*) FROM users a JOIN users b ON b.id = a.referrer_id WHERE b.vip = 1"),
    ("batch4.py:V1 COUNT(DISTINCT user_id) vs DISTINCT COUNT(user_",
     "SELECT COUNT(DISTINCT user_id) FROM orders",
     "SELECT DISTINCT COUNT(user_id) FROM orders"),
    ("compare.py:count-distinct-vs-sub",
     "SELECT COUNT(DISTINCT user_id) FROM orders WHERE status='paid'",
     "SELECT COUNT(*) FROM (SELECT DISTINCT user_id FROM orders WHERE status='paid') t"),
    ("batch2.py:E5 COUNT(t.user_id) over DISTINCT subquery vs COUN",
     "SELECT COUNT(t.user_id) FROM (SELECT DISTINCT user_id FROM orders) t",
     "SELECT COUNT(user_id) FROM orders"),
    ("batch6.py:Q3s GROUP BY alias shadowing a column (sqlite)",
     "SELECT DATE(created_at) AS created_at, COUNT(*) FROM orders GROUP BY created_at",
     "SELECT DATE(created_at) AS created_at, COUNT(*) FROM orders GROUP BY DATE(created_at)"),
    ("exec_check2.py:SQ3 sqlite",
     "SELECT DATE(created_at) AS created_at, COUNT(*) FROM orders GROUP BY created_at ORDER BY 1",
     "SELECT DATE(created_at) AS created_at, COUNT(*) FROM orders GROUP BY DATE(created_at) ORDER BY 1"),
    ("fp4.py:outer-date-vs-cast",
     "SELECT DATE(paid_at) AS d, COUNT(*) FROM orders GROUP BY DATE(paid_at)",
     "SELECT CAST(paid_at AS DATE) AS d, COUNT(*) FROM orders GROUP BY CAST(paid_at AS DATE)"),
    ("regress2.py:group-date-forms2",
     "SELECT DATE(paid_at) AS d, SUM(amount) FROM orders GROUP BY DATE(paid_at)",
     "SELECT CAST(paid_at AS DATE) AS d, SUM(amount) FROM orders GROUP BY CAST(paid_at AS DATE)"),
    ("regress2.py:group-date-forms",
     "SELECT DATE(paid_at), SUM(amount) FROM orders GROUP BY DATE(paid_at)",
     "SELECT CAST(paid_at AS DATE) AS d, SUM(amount) FROM orders GROUP BY 1"),
    ("batch2.py:E6 outer SELECT DISTINCT vs no DISTINCT",
     "SELECT DISTINCT user_id FROM orders WHERE status = 'paid'",
     "SELECT user_id FROM orders WHERE status = 'paid'"),
    ("eq2.py:T01 distinct col order",
     "SELECT DISTINCT user_id, store_id FROM orders",
     "SELECT DISTINCT store_id, user_id FROM orders"),
    ("exec_check4.py:F3 (sets)",
     "SELECT GROUP_CONCAT(user_id) FROM (SELECT user_id FROM orders INTERSECT SELECT user_id FROM refunds) t",
     "SELECT GROUP_CONCAT(user_id) FROM (SELECT user_id FROM orders EXCEPT SELECT user_id FROM refunds) t"),
    ("real1.py:W14 self-join direction reversed renamed",
     "SELECT SUM(a.amount) FROM orders a JOIN orders b ON b.id = a.parent_id",
     "SELECT SUM(y.amount) FROM orders x JOIN orders y ON y.id = x.parent_id"),
    ("batch1.py:B4 real-table self-join control (should report)",
     "SELECT SUM(a.amount) FROM orders a JOIN orders b ON b.user_id = a.referrer_id",
     "SELECT SUM(b.amount) FROM orders a JOIN orders b ON b.user_id = a.referrer_id"),
    ("batch4.py:N3 LIMIT two levels down, filter at top vs inside ",
     "SELECT SUM(amount) FROM (SELECT * FROM (SELECT * FROM orders ORDER BY amount DESC LIMIT 10) a) b WHERE status = 'paid'",
     "SELECT SUM(amount) FROM (SELECT * FROM (SELECT * FROM orders WHERE status = 'paid' ORDER BY amount DESC LIMIT 10) a) b"),
    ("cases.py:postgres",
     "SELECT SUM(amount) FROM (SELECT user_id, amount, ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY paid_at) AS rn FROM orders) t WHERE rn = 1",
     "SELECT SUM(amount) FROM (SELECT user_id, amount, ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY paid_at DESC) AS rn FROM orders) t WHERE rn = 1"),
    ("fp2.py:cast-date-where",
     "SELECT SUM(amount) FROM orders WHERE CAST(paid_at AS DATE) >= '2026-09-01'",
     "SELECT SUM(amount) FROM orders WHERE DATE(paid_at) >= '2026-09-01'"),
    ("batch3.py:L2 table swapped AND SUM moved from order amount t",
     "SELECT SUM(o.amount) FROM orders o JOIN refunds r ON r.order_id = o.id",
     "SELECT SUM(r.amount) FROM orders_archive o JOIN refunds r ON r.order_id = o.id"),
    ("exec_check.py:L1",
     "SELECT SUM(o.amount) FROM orders o JOIN refunds r ON r.order_id = o.id WHERE o.amount > 100",
     "SELECT SUM(o.amount) FROM orders_archive o JOIN refunds r ON r.order_id = o.id WHERE r.amount > 100"),
    ("cases.py:postgres",
     "SELECT SUM(o.amount) FROM orders o LEFT JOIN refunds r ON r.order_id = o.id WHERE o.status = 'paid'",
     "SELECT SUM(o.amount) FROM refunds r LEFT JOIN orders o ON o.id = r.order_id WHERE o.status = 'paid'"),
    ("exec_check2.py:C2 (sum of qualifying orders)",
     "SELECT SUM(o.amount), GROUP_CONCAT(o.id) FROM orders o WHERE EXISTS (SELECT 1 FROM orders p WHERE p.user_id = o.user_id AND p.created_at < o.created_at)",
     "SELECT SUM(o.amount), GROUP_CONCAT(o.id) FROM orders o WHERE EXISTS (SELECT 1 FROM orders p WHERE p.user_id = o.user_id AND o.created_at < p.created_at)"),
    ("exec_check3.py:SJ1",
     "SELECT SUM(o1.amount) FROM orders o1 JOIN orders o2 ON o2.user_id = o1.user_id AND o2.created_at > o1.created_at",
     "SELECT SUM(o2.amount) FROM orders o1 JOIN orders o2 ON o2.user_id = o1.user_id AND o2.created_at > o1.created_at"),
    ("cases.py:postgres",
     "SELECT SUM(r.total) / SUM(p.total) FROM (SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id) p JOIN (SELECT user_id, SUM(amount) AS total FROM refunds GROUP BY user_id) r ON r.user_id = p.user_id",
     "SELECT SUM(p.total) / SUM(r.total) FROM (SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id) p JOIN (SELECT user_id, SUM(amount) AS total FROM refunds GROUP BY user_id) r ON r.user_id = p.user_id"),
    ("batch4.py:U1 UNION branch signs swapped (control)",
     "SELECT SUM(x) FROM (SELECT amount AS x FROM orders UNION ALL SELECT -amount FROM refunds) t",
     "SELECT SUM(x) FROM (SELECT -amount AS x FROM orders UNION ALL SELECT amount FROM refunds) t"),
    ("fp6.py:time-derived-renamed",
     "SELECT SUM(x.amount) FROM (SELECT amount, DATE(paid_at) AS day FROM orders) x WHERE x.day BETWEEN '2026-09-01' AND '2026-09-30'",
     "WITH y AS (SELECT amount, CAST(paid_at AS DATE) AS d FROM orders) SELECT SUM(amount) FROM y WHERE d BETWEEN '2026-09-01' AND '2026-09-30'"),
    ("cases.py:mysql",
     "SELECT SUM(x.total) FROM (SELECT p.user_id, p.total FROM (SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id) p JOIN (SELECT user_id, SUM(amount) AS total FROM refunds GROUP BY user_id) r ON r.user_id = p.user_id) x",
     "SELECT SUM(x.total) FROM (SELECT p.user_id, r.total FROM (SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id) p JOIN (SELECT user_id, SUM(amount) AS total FROM refunds GROUP BY user_id) r ON r.user_id = p.user_id) x"),
    ("batch3.py:J1 ORDER BY DESC NULLS LAST vs NULLS FIRST LIMIT 1",
     "SELECT id FROM orders ORDER BY amount DESC NULLS LAST LIMIT 10",
     "SELECT id FROM orders ORDER BY amount DESC NULLS FIRST LIMIT 10"),
    ("exec_check.py:J1",
     "SELECT id FROM orders ORDER BY amount DESC NULLS LAST LIMIT 2",
     "SELECT id FROM orders ORDER BY amount DESC NULLS FIRST LIMIT 2"),
    ("batch3.py:H3 window frame 6 vs 2 preceding (control)",
     "SELECT id, SUM(amount) OVER (ORDER BY created_at ROWS BETWEEN 6 PRECEDING AND CURRENT ROW) AS s FROM orders",
     "SELECT id, SUM(amount) OVER (ORDER BY created_at ROWS BETWEEN 2 PRECEDING AND CURRENT ROW) AS s FROM orders"),
    ("batch3.py:H1 named WINDOW partition user_id vs store_id",
     "SELECT id, SUM(amount) OVER w AS s FROM orders WINDOW w AS (PARTITION BY user_id)",
     "SELECT id, SUM(amount) OVER w AS s FROM orders WINDOW w AS (PARTITION BY store_id)"),
    ("exec_check.py:H1",
     "SELECT id, SUM(amount) OVER w AS s FROM orders WINDOW w AS (PARTITION BY user_id) ORDER BY id",
     "SELECT id, SUM(amount) OVER w AS s FROM orders WINDOW w AS (PARTITION BY store_id) ORDER BY id"),
    ("exec_check3.py:C3",
     "SELECT o.id, (SELECT COUNT(*) FROM orders c WHERE c.parent_id = o.id) AS n FROM orders o ORDER BY o.id",
     "SELECT o.id, (SELECT COUNT(*) FROM orders c WHERE c.id = o.parent_id) AS n FROM orders o ORDER BY o.id"),
    ("eq1.py:E05 column order",
     "SELECT store_id, user_id, SUM(amount) FROM orders GROUP BY store_id, user_id",
     "SELECT SUM(amount), user_id, store_id FROM orders GROUP BY user_id, store_id"),
    ("real1.py:W07 union vs union all wrapped",
     "SELECT user_id FROM orders UNION SELECT user_id FROM refunds",
     "SELECT * FROM (SELECT user_id FROM orders UNION ALL SELECT user_id FROM refunds) t"),
    ("batch2.py:F4 outer UNION vs UNION ALL",
     "SELECT user_id FROM orders UNION SELECT user_id FROM refunds",
     "SELECT user_id FROM orders UNION ALL SELECT user_id FROM refunds"),
    ("compare.py:union-root-extra-branch-cond",
     "SELECT user_id FROM orders WHERE status='paid' UNION SELECT referrer_id FROM orders WHERE status='paid'",
     "SELECT user_id FROM orders WHERE status='paid' UNION SELECT referrer_id FROM orders"),
    ("fp1.py:select-order",
     "SELECT user_id, SUM(amount) AS s, COUNT(*) AS n FROM orders GROUP BY user_id",
     "SELECT COUNT(*) AS cnt, o.user_id, SUM(o.amount) AS total FROM orders o GROUP BY o.user_id"),
    ("eq2.py:T02 distinct vs group by",
     "SELECT user_id, store_id FROM orders GROUP BY user_id, store_id",
     "SELECT DISTINCT store_id, user_id FROM orders"),
    ("compare.py:multi-cte-same-cond",
     "WITH a AS (SELECT * FROM orders WHERE is_deleted = 0 AND status='paid'), b AS (SELECT * FROM orders WHERE is_deleted = 0 AND status='open') SELECT COUNT(*) FROM a JOIN b ON b.user_id = a.user_id",
     "WITH a AS (SELECT * FROM orders WHERE is_deleted = 0 AND status='paid'), b AS (SELECT * FROM orders WHERE status='open') SELECT COUNT(*) FROM a JOIN b ON b.user_id = a.user_id"),
    ("cases.py:mysql",
     "WITH a AS (SELECT user_id, MAX(paid_at) AS ts FROM orders GROUP BY user_id), b AS (SELECT user_id, MAX(refunded_at) AS ts FROM refunds GROUP BY user_id) SELECT COUNT(*) FROM a JOIN b ON b.user_id = a.user_id WHERE a.ts >= '2026-09-01'",
     "WITH a AS (SELECT user_id, MAX(refunded_at) AS ts FROM refunds GROUP BY user_id), b AS (SELECT user_id, MAX(paid_at) AS ts FROM orders GROUP BY user_id) SELECT COUNT(*) FROM a JOIN b ON b.user_id = a.user_id WHERE a.ts >= '2026-09-01'"),
    ("cases.py:sqlite",
     "WITH a AS (SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id), b AS (SELECT user_id, SUM(amount) AS total FROM refunds GROUP BY user_id) SELECT SUM(a.total) FROM a JOIN b ON b.user_id = a.user_id",
     "WITH a AS (SELECT user_id, SUM(amount) AS total FROM refunds GROUP BY user_id), b AS (SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id) SELECT SUM(a.total) FROM a JOIN b ON b.user_id = a.user_id"),
    ("cases.py:mysql",
     "WITH done AS (SELECT * FROM refunds WHERE state = 'done') SELECT SUM(o.amount) FROM orders o LEFT JOIN done d ON d.order_id = o.id",
     "WITH done AS (SELECT * FROM refunds) SELECT SUM(o.amount) FROM orders o LEFT JOIN done d ON d.order_id = o.id WHERE d.state = 'done'"),
    ("real2.py:X05 condition moved into CTE + extra in outer",
     "WITH p AS (SELECT * FROM orders WHERE is_deleted = 0) SELECT SUM(amount) FROM p WHERE status = 'paid'",
     "WITH p AS (SELECT * FROM orders) SELECT SUM(amount) FROM p WHERE status = 'paid'"),
    ("real1.py:W02 CTE renamed + agg func",
     "WITH p AS (SELECT * FROM orders WHERE status = 'paid') SELECT SUM(p.amount) FROM p",
     "WITH q AS (SELECT * FROM orders WHERE status = 'paid') SELECT AVG(q.amount) FROM q"),
    ("batch4.py:O1 LEFT JOIN keep side swapped (control)",
     "WITH paid AS (SELECT * FROM orders WHERE status = 'paid'), open AS (SELECT * FROM orders WHERE status = 'open') SELECT COUNT(*) FROM paid p LEFT JOIN open o ON o.user_id = p.user_id",
     "WITH paid AS (SELECT * FROM orders WHERE status = 'paid'), open AS (SELECT * FROM orders WHERE status = 'open') SELECT COUNT(*) FROM open o LEFT JOIN paid p ON p.user_id = o.user_id"),
    ("batch4.py:O2 LEFT JOIN, WHERE on keep side vs nullable side",
     "WITH paid AS (SELECT * FROM orders WHERE status = 'paid'), open AS (SELECT * FROM orders WHERE status = 'open') SELECT COUNT(*) FROM paid p LEFT JOIN open o ON o.user_id = p.user_id WHERE p.amount > 100",
     "WITH paid AS (SELECT * FROM orders WHERE status = 'paid'), open AS (SELECT * FROM orders WHERE status = 'open') SELECT COUNT(*) FROM paid p LEFT JOIN open o ON o.user_id = p.user_id WHERE o.amount > 100"),
    ("real1.py:W12 sealed renamed CTEs, used the other one",
     "WITH paid AS (SELECT * FROM orders WHERE status = 'paid'), open AS (SELECT * FROM orders WHERE status = 'open') SELECT SUM(p.amount) FROM paid p JOIN open o ON o.user_id = p.user_id",
     "WITH a1 AS (SELECT * FROM orders WHERE status = 'open'), b1 AS (SELECT * FROM orders WHERE status = 'paid') SELECT SUM(x.amount) FROM b1 y JOIN a1 x ON x.user_id = y.user_id"),
    ("exec_check3.py:A5",
     "WITH paid AS (SELECT * FROM orders WHERE status = 'paid'), open AS (SELECT * FROM orders WHERE status = 'open') SELECT p.store_id, COUNT(*) FROM paid p JOIN open o ON o.user_id = p.user_id GROUP BY p.store_id ORDER BY 1",
     "WITH paid AS (SELECT * FROM orders WHERE status = 'paid'), open AS (SELECT * FROM orders WHERE status = 'open') SELECT o.store_id, COUNT(*) FROM paid p JOIN open o ON o.user_id = p.user_id GROUP BY o.store_id ORDER BY 1"),
    ("batch4.py:T2 DATE(created_at) AS d in paid/open CTEs, p.d vs",
     "WITH paid AS (SELECT user_id, DATE(created_at) AS d FROM orders WHERE status = 'paid'), open AS (SELECT user_id, DATE(created_at) AS d FROM orders WHERE status = 'open') SELECT COUNT(*) FROM paid p JOIN open o ON o.user_id = p.user_id WHERE p.d >= '2026-09-01'",
     "WITH paid AS (SELECT user_id, DATE(created_at) AS d FROM orders WHERE status = 'paid'), open AS (SELECT user_id, DATE(created_at) AS d FROM orders WHERE status = 'open') SELECT COUNT(*) FROM paid p JOIN open o ON o.user_id = p.user_id WHERE o.d >= '2026-09-01'"),
    ("cases.py:postgres",
     "WITH paid AS (SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id), refunded AS (SELECT user_id, SUM(amount) AS total FROM refunds GROUP BY user_id) , net AS (SELECT p.user_id, p.total AS v FROM paid p JOIN refunded r ON r.user_id = p.user_id) SELECT COUNT(*) FROM net WHERE v > 100",
     "WITH paid AS (SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id), refunded AS (SELECT user_id, SUM(amount) AS total FROM refunds GROUP BY user_id) , net AS (SELECT p.user_id, r.total AS v FROM paid p JOIN refunded r ON r.user_id = p.user_id) SELECT COUNT(*) FROM net WHERE v > 100"),
    ("p123.py:1k renamed WHERE",
     "WITH paid AS (SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id), refunded AS (SELECT user_id, SUM(amount) AS total FROM refunds GROUP BY user_id) SELECT COUNT(*) FROM paid p JOIN refunded r ON r.user_id = p.user_id WHERE p.total > 100",
     "WITH a1 AS (SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id), b1 AS (SELECT user_id, SUM(amount) AS total FROM refunds GROUP BY user_id) SELECT COUNT(*) FROM a1 x JOIN b1 y ON y.user_id = x.user_id WHERE y.total > 100"),
    ("cases.py:postgres",
     "WITH paid AS (SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id), refunded AS (SELECT user_id, SUM(amount) AS total FROM refunds GROUP BY user_id) SELECT COUNT(*) FROM paid p LEFT JOIN refunded r ON r.user_id = p.user_id",
     "WITH paid AS (SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id), refunded AS (SELECT user_id, SUM(amount) AS total FROM refunds GROUP BY user_id) SELECT COUNT(*) FROM refunded r LEFT JOIN paid p ON p.user_id = r.user_id"),
    ("p123.py:1l renamed diff",
     "WITH paid AS (SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id), refunded AS (SELECT user_id, SUM(amount) AS total FROM refunds GROUP BY user_id) SELECT SUM(p.total - r.total) FROM paid p JOIN refunded r ON r.user_id = p.user_id",
     "WITH a1 AS (SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id), b1 AS (SELECT user_id, SUM(amount) AS total FROM refunds GROUP BY user_id) SELECT SUM(y.total - x.total) FROM a1 x JOIN b1 y ON y.user_id = x.user_id"),
    ("p123.py:1j renamed SUM",
     "WITH paid AS (SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id), refunded AS (SELECT user_id, SUM(amount) AS total FROM refunds GROUP BY user_id) SELECT SUM(p.total) FROM paid p JOIN refunded r ON r.user_id = p.user_id",
     "WITH a1 AS (SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id), b1 AS (SELECT user_id, SUM(amount) AS total FROM refunds GROUP BY user_id) SELECT SUM(y.total) FROM a1 x JOIN b1 y ON y.user_id = x.user_id"),
    ("p123b.py:Z04 diff survives join swap",
     "WITH paid AS (SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id), refunded AS (SELECT user_id, SUM(amount) AS total FROM refunds GROUP BY user_id), vip AS (SELECT id FROM users WHERE vip = 1) SELECT SUM(p.total) FROM paid p JOIN refunded r ON r.user_id = p.user_id",
     "WITH paid AS (SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id), refunded AS (SELECT user_id, SUM(amount) AS total FROM refunds GROUP BY user_id), vip AS (SELECT id FROM users WHERE vip = 1) SELECT SUM(r.total) FROM refunded r JOIN paid p ON p.user_id = r.user_id"),
    ("p123b.py:Z05 diff survives names swapped",
     "WITH paid AS (SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id), refunded AS (SELECT user_id, SUM(amount) AS total FROM refunds GROUP BY user_id), vip AS (SELECT id FROM users WHERE vip = 1) SELECT SUM(p.total) FROM paid p JOIN refunded r ON r.user_id = p.user_id",
     "WITH refunded AS (SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id), paid AS (SELECT user_id, SUM(amount) AS total FROM refunds GROUP BY user_id), v AS (SELECT id FROM users WHERE vip = 1) SELECT SUM(x.total) FROM paid x JOIN refunded y ON y.user_id = x.user_id"),
    ("cases.py:mysql",
     "WITH r AS (SELECT user_id, amount, RANK() OVER (ORDER BY amount DESC) AS k FROM orders) SELECT SUM(amount) FROM r WHERE k <= 10",
     "WITH r AS (SELECT user_id, amount, ROW_NUMBER() OVER (ORDER BY amount DESC) AS k FROM orders) SELECT SUM(amount) FROM r WHERE k <= 10"),
    ("real1.py:W13 same name CTE different meaning",
     "WITH t AS (SELECT * FROM orders WHERE status = 'paid') SELECT SUM(t.amount) FROM t",
     "WITH t AS (SELECT * FROM orders WHERE status = 'open') SELECT SUM(t.amount) FROM t"),
    ("real1.py:W06 time col via renamed CTE",
     "WITH t AS (SELECT DATE(paid_at) AS d, amount FROM orders) SELECT SUM(amount) FROM t WHERE d >= '2026-09-01'",
     "WITH x AS (SELECT DATE(created_at) AS d, amount FROM orders) SELECT SUM(amount) FROM x WHERE d >= '2026-09-01'"),
    ("exec_check4.py:E3",
     "WITH t AS (SELECT DISTINCT user_id, amount FROM orders) SELECT SUM(amount) FROM t",
     "SELECT SUM(amount) FROM orders"),
    ("batch3.py:H2 named WINDOW in CTE + outer filter",
     "WITH t AS (SELECT id, amount, ROW_NUMBER() OVER w AS rn FROM orders WINDOW w AS (PARTITION BY user_id ORDER BY created_at)) SELECT SUM(amount) FROM t WHERE rn = 1",
     "WITH t AS (SELECT id, amount, ROW_NUMBER() OVER w AS rn FROM orders WINDOW w AS (PARTITION BY user_id ORDER BY created_at DESC)) SELECT SUM(amount) FROM t WHERE rn = 1"),
    ("cases.py:mysql",
     "WITH t AS (SELECT user_id, CASE WHEN status = 'paid' THEN amount ELSE 0 END AS paid_amt FROM orders) SELECT SUM(paid_amt) FROM t",
     "WITH t AS (SELECT user_id, CASE WHEN status IN ('paid', 'shipped') THEN amount ELSE 0 END AS paid_amt FROM orders) SELECT SUM(paid_amt) FROM t"),
    ("real2.py:X03 extra outer cond also in CTE layer",
     "WITH t AS (SELECT user_id, SUM(amount) AS s FROM orders WHERE status = 'paid' GROUP BY user_id) SELECT COUNT(*) FROM t JOIN orders o ON o.user_id = t.user_id WHERE o.status = 'paid'",
     "WITH t AS (SELECT user_id, SUM(amount) AS s FROM orders WHERE status = 'paid' GROUP BY user_id) SELECT COUNT(*) FROM t JOIN orders o ON o.user_id = t.user_id"),
    ("p123b.py:Z06 problem2 variant: extra CTE after t, used via I",
     "WITH t AS (SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id) SELECT COUNT(*) FROM users u JOIN t ON t.user_id = u.id",
     "WITH t AS (SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id), vip AS (SELECT id FROM users WHERE vip = 1) SELECT COUNT(*) FROM users u JOIN t ON t.user_id = u.id WHERE u.id IN (SELECT id FROM vip)"),
    ("real2.py:X01 CTE self-join role reversed",
     "WITH t AS (SELECT user_id, referrer_id, SUM(amount) AS total FROM orders GROUP BY user_id, referrer_id) SELECT SUM(a.total) FROM t a JOIN t b ON b.user_id = a.referrer_id",
     "WITH t AS (SELECT user_id, referrer_id, SUM(amount) AS total FROM orders GROUP BY user_id, referrer_id) SELECT SUM(a.total) FROM t a JOIN t b ON a.user_id = b.referrer_id"),
    ("cases.py:postgres",
     "WITH t1 AS (SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id), t2 AS (SELECT user_id, SUM(amount) AS total FROM refunds GROUP BY user_id) SELECT COUNT(*) FROM t1 JOIN t2 ON t2.user_id = t1.user_id WHERE t1.total > 100",
     "WITH t1 AS (SELECT user_id, SUM(amount) AS total FROM refunds GROUP BY user_id), t2 AS (SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id) SELECT COUNT(*) FROM t1 JOIN t2 ON t2.user_id = t1.user_id WHERE t1.total > 100"),
    ("batch5.py:S1 filter on LIMIT-CTE column vs on another CTE's ",
     "WITH top AS (SELECT * FROM orders ORDER BY amount DESC LIMIT 10), allo AS (SELECT * FROM orders) SELECT COUNT(*) FROM top t JOIN allo a ON a.user_id = t.user_id WHERE t.amount > 100",
     "WITH top AS (SELECT * FROM orders ORDER BY amount DESC LIMIT 10), allo AS (SELECT * FROM orders) SELECT COUNT(*) FROM top t JOIN allo a ON a.user_id = t.user_id WHERE a.amount > 100"),
    ("exec_check.py:S1",
     "WITH top AS (SELECT * FROM orders ORDER BY amount DESC LIMIT 2), allo AS (SELECT * FROM orders) SELECT COUNT(*) FROM top t JOIN allo a ON a.user_id = t.user_id WHERE t.amount > 100",
     "WITH top AS (SELECT * FROM orders ORDER BY amount DESC LIMIT 2), allo AS (SELECT * FROM orders) SELECT COUNT(*) FROM top t JOIN allo a ON a.user_id = t.user_id WHERE a.amount > 100"),
    ("exec_check.py:E7",
     "WITH u AS (SELECT DISTINCT user_id FROM refunds) SELECT SUM(o.amount) FROM orders o JOIN u ON u.user_id = o.user_id",
     "WITH u AS (SELECT user_id FROM refunds) SELECT SUM(o.amount) FROM orders o JOIN u ON u.user_id = o.user_id"),
]


# ---------------------------------------------------------------------------- 第 2 轮复验的反例
# 第 2 轮复验（重做之后的第一版）报的 blocker / major 和几位复验者留在 /private/tmp 的脚本里的对，改写到这个库的
# 表上（列名换成这里有的），SQLite 能执行的进执行比对；只在 PostgreSQL / MySQL 才有的写法进 REGRESSIONS_R2 只查结论。

#: 意思一样、但要按语义推理才看得出的（条件挪进相关子查询、冗余条件、写法不同的等价条件……）：规范化不保证认得出，
#: 认不出时只许给兜底说明，不许给具体说法（复验专挑这类对查「说出不存在的差异」）
_SAME_HARD = [
    # 条件挪进相关子查询（EXISTS 里引用外层的列）：B 是有这个条件的，不能说「B 没有」
    ("r2-exists-moved-time", "SELECT COUNT(*) FROM orders o WHERE o.paid_at >= '2026-09-01' AND EXISTS "
     "(SELECT 1 FROM refunds r WHERE r.order_id = o.id)",
     "SELECT COUNT(*) FROM orders o WHERE EXISTS (SELECT 1 FROM refunds r WHERE r.order_id = o.id "
     "AND o.paid_at >= '2026-09-01')"),
    ("r2-exists-moved-filter", "SELECT COUNT(*) FROM orders o WHERE o.status = 'paid' AND EXISTS "
     "(SELECT 1 FROM refunds r WHERE r.order_id = o.id)",
     "SELECT COUNT(*) FROM orders o WHERE EXISTS (SELECT 1 FROM refunds r WHERE r.order_id = o.id AND o.status = 'paid')"),
    ("r2-scalar-moved-filter", "SELECT COUNT(*) FROM orders o WHERE o.status = 'paid' AND o.amount > "
     "(SELECT AVG(r.amount) FROM refunds r WHERE r.order_id = o.id)",
     "SELECT COUNT(*) FROM orders o WHERE o.amount > (SELECT AVG(r.amount) FROM refunds r WHERE r.order_id = o.id "
     "AND o.status = 'paid')"),
    # refunds 没有 status 列：子查询里的 status 是外层 orders 的
    ("r2-in-moved-outer-column", "SELECT COUNT(*) FROM orders WHERE status = 'paid' AND id IN (SELECT order_id FROM refunds)",
     "SELECT COUNT(*) FROM orders WHERE id IN (SELECT order_id FROM refunds WHERE status = 'paid')"),
    # 一段时间拆成两段、IN 和 OR、多写一个下界：留下的行一样
    ("r2-time-split-range", "SELECT COUNT(*) FROM orders WHERE paid_at >= '2026-09-01' AND paid_at < '2026-10-01'",
     "SELECT COUNT(*) FROM orders WHERE (paid_at >= '2026-09-01' AND paid_at < '2026-09-16') OR "
     "(paid_at >= '2026-09-16' AND paid_at < '2026-10-01')"),
    ("r2-time-in-vs-or", "SELECT COUNT(*) FROM orders WHERE paid_at IN ('2026-09-01', '2026-09-30')",
     "SELECT COUNT(*) FROM orders WHERE paid_at = '2026-09-01' OR paid_at = '2026-09-30'"),
    ("r2-time-redundant-lower", "SELECT COUNT(*) FROM orders WHERE paid_at >= '2026-09-01' AND paid_at < '2026-10-01'",
     "SELECT COUNT(*) FROM orders WHERE paid_at >= '2026-09-01' AND paid_at < '2026-10-01' AND paid_at > '2026-08-01'"),
    ("r2-in-vs-or", "SELECT COUNT(*) FROM orders WHERE status IN ('paid', 'done')",
     "SELECT COUNT(*) FROM orders WHERE status = 'paid' OR status = 'done'"),
    ("r2-redundant-not-null", "SELECT SUM(tax) FROM orders WHERE tax > 1 AND tax IS NOT NULL",
     "SELECT SUM(tax) FROM orders WHERE tax > 1"),
    ("r2-redundant-not-null-join", "SELECT SUM(o.amount) FROM orders o JOIN users u ON u.id = o.user_id "
     "WHERE o.user_id IS NOT NULL",
     "SELECT SUM(o.amount) FROM orders o JOIN users u ON u.id = o.user_id"),
    ("r2-redundant-not-null-in", "SELECT COUNT(*) FROM orders WHERE store_id IN (SELECT id FROM stores "
     "WHERE region = 'east') AND store_id IS NOT NULL",
     "SELECT COUNT(*) FROM orders WHERE store_id IN (SELECT id FROM stores WHERE region = 'east')"),
    ("r2-count-col-vs-not-null-filter", "SELECT COUNT(tax) FROM orders", "SELECT COUNT(*) FROM orders WHERE tax IS NOT NULL"),
    ("r2-count-distinct-not-null", "SELECT COUNT(DISTINCT user_id) FROM orders WHERE user_id IS NOT NULL",
     "SELECT COUNT(DISTINCT user_id) FROM orders"),
    ("r2-coalesce-gt", "SELECT COUNT(*) FROM orders WHERE COALESCE(amount, 0) > 10",
     "SELECT COUNT(*) FROM orders WHERE amount > 10"),
    ("r2-left-coalesce-reject", "SELECT o.id FROM orders o LEFT JOIN users u ON u.id = o.user_id "
     "WHERE COALESCE(u.grade, 0) > 1",
     "SELECT o.id FROM orders o JOIN users u ON u.id = o.user_id WHERE COALESCE(u.grade, 0) > 1"),
    ("r2-left-count-nullable-side", "SELECT COUNT(r.id) FROM orders o LEFT JOIN refunds r ON r.order_id = o.id",
     "SELECT COUNT(r.id) FROM orders o JOIN refunds r ON r.order_id = o.id"),
    ("r2-having-count-gt-1-vs-ge-2", "SELECT user_id FROM orders GROUP BY user_id HAVING COUNT(*) > 1",
     "SELECT user_id FROM orders GROUP BY user_id HAVING COUNT(*) >= 2"),
    ("r2-having-noop", "SELECT store_id, SUM(amount) FROM orders GROUP BY store_id HAVING COUNT(*) > 0",
     "SELECT store_id, SUM(amount) FROM orders GROUP BY store_id"),
    ("r2-scalar-max-commute", "SELECT max(amount, tax) FROM orders", "SELECT max(tax, amount) FROM orders"),
    ("r2-case-vs-coalesce", "SELECT SUM(CASE WHEN discount IS NULL THEN 0 ELSE discount END) FROM orders",
     "SELECT SUM(COALESCE(discount, 0)) FROM orders"),
]

#: 意思一样、规范化认得出的改写（第 2 轮新增的几步）
_SAME_R2 = [
    ("r2-one-eq-one", "SELECT COUNT(*) FROM orders WHERE 1 = 1 AND status = 'paid'",
     "SELECT COUNT(*) FROM orders WHERE status = 'paid'"),
    ("r2-not-is-not-null", "SELECT COUNT(*) FROM orders WHERE NOT (paid_at IS NOT NULL)",
     "SELECT COUNT(*) FROM orders WHERE paid_at IS NULL"),
    ("r2-not-like", "SELECT COUNT(*) FROM orders WHERE status NOT LIKE 'p%'",
     "SELECT COUNT(*) FROM orders WHERE NOT status LIKE 'p%'"),
    ("r2-iif-vs-case", "SELECT SUM(IIF(status = 'paid', amount, 0)) FROM orders",
     "SELECT SUM(CASE WHEN status = 'paid' THEN amount ELSE 0 END) FROM orders"),
    ("r2-case-else-null", "SELECT SUM(CASE WHEN status = 'paid' THEN amount END) FROM orders",
     "SELECT SUM(CASE WHEN status = 'paid' THEN amount ELSE NULL END) FROM orders"),
    ("r2-min-distinct", "SELECT MIN(DISTINCT amount), MAX(DISTINCT tax) FROM orders",
     "SELECT MIN(amount), MAX(tax) FROM orders"),
    ("r2-exists-limit-1", "SELECT COUNT(*) FROM users u WHERE EXISTS (SELECT 1 FROM orders o WHERE o.user_id = u.id LIMIT 1)",
     "SELECT COUNT(*) FROM users u WHERE EXISTS (SELECT 1 FROM orders o WHERE o.user_id = u.id)"),
    ("r2-in-subquery-distinct", "SELECT COUNT(*) FROM orders WHERE user_id IN (SELECT DISTINCT user_id FROM refunds)",
     "SELECT COUNT(*) FROM orders WHERE user_id IN (SELECT user_id FROM refunds)"),
    ("r2-using-vs-on-select", "SELECT user_id FROM orders o LEFT JOIN refunds r USING (user_id)",
     "SELECT o.user_id FROM orders o LEFT JOIN refunds r ON o.user_id = r.user_id"),
    ("r2-multi-use-cte-correlated", "WITH p AS (SELECT * FROM orders WHERE status = 'paid') SELECT COUNT(*) FROM p "
     "WHERE EXISTS (SELECT 1 FROM p AS p2 WHERE p2.parent_id = p.id)",
     "SELECT COUNT(*) FROM orders o WHERE o.status = 'paid' AND EXISTS (SELECT 1 FROM orders c "
     "WHERE c.parent_id = o.id AND c.status = 'paid')"),
    ("r2-outer-where-into-having", "WITH t AS (SELECT user_id, SUM(amount) AS s FROM orders GROUP BY user_id "
     "HAVING SUM(amount) > 100) SELECT COUNT(*) FROM t",
     "SELECT COUNT(*) FROM (SELECT user_id, SUM(amount) AS s FROM orders GROUP BY user_id) t WHERE t.s > 100"),
]

#: 不许判等价的（第 2 轮复验报的 blocker 改写到这个库上；在这几组数据上大多结果不同）
_DIFF_R2 = [
    # 数字常量不折叠：SQLite 按浮点数算，0.1 + 0.2 ≠ 0.3；三项以上的加乘不换结合
    ("r2-float-fold", "SELECT COUNT(*) FROM orders WHERE amount = 0.1 + 0.2", "SELECT COUNT(*) FROM orders WHERE amount = 0.3"),
    ("r2-float-fold-chain", "SELECT COUNT(*) FROM orders WHERE amount + 0.1 + 0.2 = 0.3",
     "SELECT COUNT(*) FROM orders WHERE amount + 0.3 = 0.3"),
    ("r2-float-fold-mul", "SELECT id, amount * 0.1 * 3 FROM orders", "SELECT id, amount * 0.3 FROM orders"),
    ("r2-float-fold-compare", "SELECT COUNT(*) FROM orders WHERE amount < 3 * 0.1",
     "SELECT COUNT(*) FROM orders WHERE amount < 0.3"),
    ("r2-float-fold-007", "SELECT COUNT(*) FROM orders WHERE amount >= 100 * 0.07",
     "SELECT COUNT(*) FROM orders WHERE amount >= 7.00"),
    # 取两次负会把文本转成数
    ("r2-double-negation", "SELECT id, -(-name) FROM users", "SELECT id, name FROM users"),
    ("r2-double-negation-compare", "SELECT COUNT(*) FROM users WHERE -(-name) = 100",
     "SELECT COUNT(*) FROM users WHERE name = 100"),
    # 列名表：CTE 自己写的列名决定了外面的名字指哪一列
    ("r2-cte-column-list-swap", "WITH p(uid, total) AS (SELECT user_id, SUM(amount) FROM orders GROUP BY user_id) "
     "SELECT COUNT(*) FROM p WHERE total > 100",
     "WITH p(total, uid) AS (SELECT user_id, SUM(amount) FROM orders GROUP BY user_id) "
     "SELECT COUNT(*) FROM p WHERE total > 100"),
    ("r2-cte-column-list-swap-2", "WITH t(user_id, amount) AS (SELECT amount, user_id FROM orders) SELECT SUM(amount) FROM t",
     "WITH t(amount, user_id) AS (SELECT amount, user_id FROM orders) SELECT SUM(amount) FROM t"),
    # SQLite 不写 RECURSIVE 也是递归：A 引用的是自己，B 引用的是实表 nums
    ("r2-recursive-without-keyword", "WITH nums(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM nums WHERE n < 5) "
     "SELECT COUNT(*) FROM nums",
     "WITH seq(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM nums WHERE n < 5) SELECT COUNT(*) FROM seq"),
    # ORDER BY 有并列再取行、拼串、窗口函数：结果看行出来的先后，调换来源、DISTINCT 改 GROUP BY 都会变
    ("r2-distinct-group-limit-ties", "SELECT DISTINCT user_id, status FROM orders ORDER BY user_id LIMIT 4",
     "SELECT user_id, status FROM orders GROUP BY user_id, status ORDER BY user_id LIMIT 4"),
    ("r2-group-key-order-limit-ties", "SELECT user_id, store_id, COUNT(*) FROM orders GROUP BY user_id, store_id "
     "ORDER BY COUNT(*) DESC LIMIT 2",
     "SELECT user_id, store_id, COUNT(*) FROM orders GROUP BY store_id, user_id ORDER BY COUNT(*) DESC LIMIT 2"),
    ("r2-cross-join-swap-limit-ties", "SELECT o.id, u.id FROM orders o CROSS JOIN users u WHERE u.id = o.user_id "
     "ORDER BY o.amount LIMIT 3",
     "SELECT o.id, u.id FROM users u CROSS JOIN orders o WHERE u.id = o.user_id ORDER BY o.amount LIMIT 3"),
    ("r2-group-concat-join-order", "SELECT u.city, GROUP_CONCAT(o.id) FROM users u CROSS JOIN orders o "
     "WHERE o.user_id = u.id GROUP BY u.city",
     "SELECT u.city, GROUP_CONCAT(o.id) FROM orders o CROSS JOIN users u WHERE o.user_id = u.id GROUP BY u.city"),
    ("r2-group-concat-cross-vs-comma", "SELECT u.city, GROUP_CONCAT(o.id) FROM users u CROSS JOIN orders o "
     "WHERE o.user_id = u.id GROUP BY u.city",
     "SELECT u.city, GROUP_CONCAT(o.id) FROM users u, orders o WHERE o.user_id = u.id GROUP BY u.city"),
    ("r2-row-number-join-order", "SELECT o.id, ROW_NUMBER() OVER () AS rn FROM users u CROSS JOIN orders o "
     "WHERE o.user_id = u.id",
     "SELECT o.id, ROW_NUMBER() OVER () AS rn FROM orders o CROSS JOIN users u WHERE o.user_id = u.id"),
    ("r2-row-number-ties-join-order", "SELECT o.id, ROW_NUMBER() OVER (ORDER BY o.status) AS rn FROM users u "
     "JOIN orders o ON o.user_id = u.id",
     "SELECT o.id, ROW_NUMBER() OVER (ORDER BY o.status) AS rn FROM orders o JOIN users u ON o.user_id = u.id"),
    ("r2-group-concat-subquery-order", "SELECT GROUP_CONCAT(id) FROM (SELECT id FROM orders ORDER BY amount DESC, id) t",
     "SELECT GROUP_CONCAT(id) FROM (SELECT id FROM orders) t"),
    # SQLite 里 a IN (b) 是 a = +b：右边那一列不带类型亲和
    ("r2-in-single-column", "SELECT COUNT(*) FROM orders WHERE status IN (store_id)",
     "SELECT COUNT(*) FROM orders WHERE status = store_id"),
    ("r2-not-in-single-column", "SELECT COUNT(*) FROM orders WHERE status NOT IN (store_id)",
     "SELECT COUNT(*) FROM orders WHERE status <> store_id"),
    # 解析时丢了写法的：类型名（STRING 是数值亲和）、0x10 和 x'10'
    ("r2-cast-string", "SELECT store_id, CAST(store_id AS STRING) FROM orders",
     "SELECT store_id, CAST(store_id AS TEXT) FROM orders"),
    ("r2-cast-bytea", "SELECT id, CAST(status AS BLOB) FROM orders", "SELECT id, CAST(status AS BYTEA) FROM orders"),
    ("r2-hex-literal", "SELECT COUNT(*) FROM orders WHERE id = 0x10", "SELECT COUNT(*) FROM orders WHERE id = x'10'"),
    # SELECT * 的列先后
    ("r2-star-cte-column-order", "WITH c AS (SELECT user_id, amount FROM orders) SELECT * FROM c JOIN users "
     "ON users.id = c.user_id",
     "WITH c AS (SELECT amount, user_id FROM orders) SELECT * FROM c JOIN users ON users.id = c.user_id"),
    ("r2-star-join-reorder", "SELECT * FROM orders o LEFT JOIN refunds r ON r.order_id = o.id JOIN users u ON u.id = o.user_id",
     "SELECT * FROM orders o JOIN users u ON u.id = o.user_id LEFT JOIN refunds r ON r.order_id = o.id"),
    # EXISTS 里：ORDER BY 有聚合就是聚合查询（总出一行）；DISTINCT 在 OFFSET 之前算（SQLite 恰好不管这个 DISTINCT）
    ("r2-exists-order-by-aggregate", "SELECT COUNT(*) FROM orders o WHERE EXISTS (SELECT 1 FROM refunds r "
     "WHERE r.order_id = o.id ORDER BY COUNT(*))",
     "SELECT COUNT(*) FROM orders o WHERE EXISTS (SELECT 1 FROM refunds r WHERE r.order_id = o.id)"),
    ("r2-exists-distinct-offset", "SELECT COUNT(*) FROM orders o WHERE EXISTS (SELECT DISTINCT r.state FROM refunds r "
     "WHERE r.amount > 0 LIMIT 1 OFFSET 1)",
     "SELECT COUNT(*) FROM orders o WHERE EXISTS (SELECT r.state FROM refunds r WHERE r.amount > 0 LIMIT 1 OFFSET 1)"),
    # HAVING 不往 WHERE 挪：不分大小写的排序规则下同一组里的值各不一样
    ("r2-having-key-vs-where", "SELECT city, COUNT(*) FROM users GROUP BY city HAVING city = 'SH'",
     "SELECT city, COUNT(*) FROM users WHERE city = 'SH' GROUP BY city"),
    # 两边对调、子查询配错位置（复验说成自相矛盾的话的几对）
    ("r2-except-sides-swapped", "SELECT user_id FROM orders WHERE paid_at >= '2026-09-01' EXCEPT SELECT user_id FROM refunds",
     "SELECT user_id FROM refunds EXCEPT SELECT user_id FROM orders WHERE created_at >= '2026-09-01'"),
    ("r2-in-not-in-swapped", "WITH p AS (SELECT user_id FROM orders WHERE status = 'paid' AND user_id IS NOT NULL), "
     "v AS (SELECT user_id FROM orders WHERE status = 'void' AND user_id IS NOT NULL) SELECT COUNT(*) FROM users "
     "WHERE id IN (SELECT user_id FROM p) AND id NOT IN (SELECT user_id FROM v)",
     "WITH p AS (SELECT user_id FROM orders WHERE status = 'paid' AND user_id IS NOT NULL), "
     "v AS (SELECT user_id FROM orders WHERE status = 'void' AND user_id IS NOT NULL) SELECT COUNT(*) FROM users "
     "WHERE id IN (SELECT user_id FROM v) AND id NOT IN (SELECT user_id FROM p)"),
    ("r2-not-in-filter", "WITH p AS (SELECT user_id FROM orders WHERE status = 'paid'), v AS (SELECT user_id FROM orders "
     "WHERE status = 'void' AND user_id IS NOT NULL) SELECT COUNT(*) FROM users WHERE id IN (SELECT user_id FROM p) "
     "AND id NOT IN (SELECT user_id FROM v)",
     "WITH p AS (SELECT user_id FROM orders WHERE status = 'paid'), v AS (SELECT user_id FROM orders "
     "WHERE status = 'open' AND user_id IS NOT NULL) SELECT COUNT(*) FROM users WHERE id IN (SELECT user_id FROM p) "
     "AND id NOT IN (SELECT user_id FROM v)"),
    ("r2-exists-not-exists-swapped", "SELECT COUNT(*) FROM orders o WHERE EXISTS (SELECT 1 FROM refunds r "
     "WHERE r.order_id = o.id AND r.state = 'done') AND NOT EXISTS (SELECT 1 FROM refunds r WHERE r.order_id = o.id "
     "AND r.state = 'pending')",
     "SELECT COUNT(*) FROM orders o WHERE EXISTS (SELECT 1 FROM refunds r WHERE r.order_id = o.id AND r.state = 'pending') "
     "AND NOT EXISTS (SELECT 1 FROM refunds r WHERE r.order_id = o.id AND r.state = 'done')"),
    ("r2-in-left-column-swapped", "SELECT COUNT(*) FROM users WHERE id IN (SELECT user_id FROM orders WHERE status = 'paid') "
     "AND referrer_id IN (SELECT user_id FROM orders WHERE status = 'void')",
     "SELECT COUNT(*) FROM users WHERE id IN (SELECT user_id FROM orders WHERE status = 'void') "
     "AND referrer_id IN (SELECT user_id FROM orders WHERE status = 'paid')"),
    # 换了表：各自的列是各自表的（复验报的 detail 归错了表）
    ("r2-table-swap-time", "SELECT COUNT(*) FROM orders WHERE paid_at >= '2026-09-01'",
     "SELECT COUNT(*) FROM users WHERE created_at >= '2026-09-01'"),
    ("r2-table-swap-join", "SELECT SUM(o.amount) FROM orders o JOIN stores s ON s.id = o.store_id WHERE s.city = 'SH'",
     "SELECT SUM(o.amount) FROM orders o JOIN users u ON u.id = o.user_id WHERE u.city = 'SH'"),
    ("r2-table-swap-time-2", "SELECT SUM(amount) FROM refunds WHERE refunded_at >= '2026-09-01'",
     "SELECT SUM(amount) FROM payments WHERE paid_at >= '2026-09-01'"),
]


# ---------------------------------------------------------------------------- 第 3 轮复验的反例
# 第 2 轮补充之后的复验报的 blocker / major（复验者的表 visits / patients / doctors 改写成这里的 orders / users /
# stores：visits.pid → orders.user_id，visits.did → orders.store_id，visits.fee → orders.amount，refunds.vid →
# refunds.order_id），以及本轮自己压测时找到的同类写法。

#: 不许判等价的（在这几组数据上大多结果不同）
_DIFF_R3 = [
    # 外连接改内连接要 WHERE 真把没关联上的行筛掉：NULL NOT IN (空集) 是真，SQLite 里 x NOT IN () 也是真
    ("r3-left-not-in-subquery", "SELECT o.id FROM orders o LEFT JOIN users u ON u.id = o.user_id WHERE u.city NOT IN "
     "(SELECT s.city FROM stores s WHERE s.region = 'nowhere')",
     "SELECT o.id FROM orders o JOIN users u ON u.id = o.user_id WHERE u.city NOT IN "
     "(SELECT s.city FROM stores s WHERE s.region = 'nowhere')"),
    ("r3-left-not-in-empty-list", "SELECT o.id FROM orders o LEFT JOIN users u ON u.id = o.user_id WHERE u.city NOT IN ()",
     "SELECT o.id FROM orders o JOIN users u ON u.id = o.user_id WHERE u.city NOT IN ()"),
    ("r3-left-not-exists", "SELECT o.id FROM orders o LEFT JOIN users u ON u.id = o.user_id WHERE NOT EXISTS "
     "(SELECT 1 FROM stores s WHERE s.owner_id = u.id)",
     "SELECT o.id FROM orders o JOIN users u ON u.id = o.user_id WHERE NOT EXISTS "
     "(SELECT 1 FROM stores s WHERE s.owner_id = u.id)"),
    ("r3-left-is-not-1", "SELECT o.id FROM orders o LEFT JOIN users u ON u.id = o.user_id WHERE u.vip IS NOT 1",
     "SELECT o.id FROM orders o JOIN users u ON u.id = o.user_id WHERE u.vip IS NOT 1"),
    ("r3-left-not-in-subquery-or", "SELECT o.id FROM orders o LEFT JOIN users u ON u.id = o.user_id WHERE u.grade > 2 "
     "OR u.city NOT IN (SELECT s.city FROM stores s WHERE s.region = 'nowhere')",
     "SELECT o.id FROM orders o JOIN users u ON u.id = o.user_id WHERE u.grade > 2 "
     "OR u.city NOT IN (SELECT s.city FROM stores s WHERE s.region = 'nowhere')"),
    # 列名表改名时 WHERE 里还按原来的列别名引用（SQLite 的 WHERE 认列别名）
    ("r3-cte-column-list-where-alias", "WITH t(a, b) AS (SELECT amount AS b, id AS a FROM orders WHERE b > 50) SELECT a, b FROM t",
     "WITH t(a, b) AS (SELECT amount AS x, id AS b FROM orders WHERE b > 50) SELECT a, b FROM t"),
    ("r3-cte-column-list-union-where-alias", "WITH t(a, b) AS (SELECT amount AS b, id AS a FROM orders WHERE b > 50 "
     "UNION ALL SELECT amount, id FROM refunds) SELECT a, b FROM t",
     "WITH t(a, b) AS (SELECT amount AS x, id AS b FROM orders WHERE b > 50 UNION ALL SELECT amount, id FROM refunds) "
     "SELECT a, b FROM t"),
    # EXISTS 写成 SELECT 1 时 WHERE 里还按选出来的列别名引用
    ("r3-exists-alias-where", "SELECT o.id FROM orders o WHERE EXISTS (SELECT r.amount * 2 AS a2 FROM refunds r "
     "WHERE r.order_id = o.id AND a2 > 100)",
     "SELECT o.id FROM orders o WHERE EXISTS (SELECT r.amount AS a2 FROM refunds r WHERE r.order_id = o.id AND a2 > 100)"),
    ("r3-exists-alias-distinct", "SELECT o.id FROM orders o WHERE EXISTS (SELECT DISTINCT r.amount AS a2 FROM refunds r "
     "WHERE r.order_id = o.id AND a2 > 100)",
     "SELECT o.id FROM orders o WHERE EXISTS (SELECT r.state AS a2 FROM refunds r WHERE r.order_id = o.id AND a2 > 100)"),
    # ORDER BY 里单独一个名字先认列别名：USING 改写 ON 时不能把它写成左表的列
    ("r3-using-order-by-alias", "SELECT -o.id AS id FROM orders o JOIN users USING (id) ORDER BY id LIMIT 2",
     "SELECT -o.id AS id FROM orders o JOIN users u ON o.id = u.id ORDER BY o.id LIMIT 2"),
    ("r3-using-order-by-alias-left", "SELECT -o.id AS id, u.name FROM orders o LEFT JOIN users u USING (id) "
     "ORDER BY id LIMIT 3",
     "SELECT -o.id AS id, u.name FROM orders o LEFT JOIN users u ON o.id = u.id ORDER BY o.id LIMIT 3"),
    # 子查询选出来的两列同名：SQLite 认第一个
    ("r3-duplicate-output-name", "SELECT * FROM (SELECT user_id AS k, SUM(amount) AS k FROM orders GROUP BY user_id) g "
     "WHERE g.k > 3",
     "SELECT * FROM (SELECT user_id AS k, SUM(amount) AS k FROM orders GROUP BY user_id HAVING SUM(amount) > 3) g"),
    ("r3-having-ungrouped-count", "SELECT COUNT(*) FROM orders WHERE status = 'nope' HAVING COUNT(*) > 0",
     "SELECT COUNT(*) FROM orders WHERE status = 'nope'"),
    # SQLite 的 % 先把两边转成整数，MOD() 按浮点数算：12.5 % 2 是 0，MOD(12.5, 2) 是 0.5
    ("r3-mod-vs-percent", "SELECT COUNT(*) FROM orders WHERE amount % 2 = 0",
     "SELECT COUNT(*) FROM orders WHERE MOD(amount, 2) = 0"),
    # 子查询的别名遮住了外层的别名：refunds 没有 status 列，SQLite 往外层找，o.status 是父单的、c.status 是子单的
    ("r3-shadowed-alias", "SELECT COUNT(*) FROM orders o JOIN orders c ON c.parent_id = o.id WHERE EXISTS "
     "(SELECT 1 FROM refunds AS o WHERE o.amount > 0 AND o.status = 'paid')",
     "SELECT COUNT(*) FROM orders o JOIN orders c ON c.parent_id = o.id WHERE EXISTS "
     "(SELECT 1 FROM refunds AS c WHERE c.amount > 0 AND c.status = 'paid')"),
    # orders 没有 state 列：SQLite 把双引号里的读成字符串，'State' 和 'state' 不同（refunds 有 state 列不算数）
    ("r3-double-quoted-not-in-scope", 'SELECT COUNT(*) FROM orders WHERE status = "State"',
     'SELECT COUNT(*) FROM orders WHERE status = "state"'),
    ("r3-double-quoted-not-in-outer-scope", 'SELECT COUNT(*) FROM users u WHERE EXISTS (SELECT 1 FROM orders o '
     'WHERE o.user_id = u.id AND o.status = "State")',
     'SELECT COUNT(*) FROM users u WHERE EXISTS (SELECT 1 FROM orders o WHERE o.user_id = u.id AND o.status = "state")'),
    # 自己关联自己，关联的方向不同（复验报的是说法自相矛盾，等价的结论本来就是对的）
    ("r3-self-join-roles", "SELECT a.id, b.id FROM orders a LEFT JOIN orders b ON b.parent_id = a.id",
     "SELECT a.id, b.id FROM orders a LEFT JOIN orders b ON a.parent_id = b.id"),
    # 真正的差异只有时间列：分组键、LEFT JOIN 那两处写法不同却不影响结果，不能多说
    ("r3-time-plus-group-key-equal", "SELECT u.id, SUM(o.amount) FROM orders o JOIN users u ON u.id = o.user_id "
     "WHERE o.paid_at >= '2026-09-01' GROUP BY u.id",
     "SELECT o.user_id, SUM(o.amount) FROM orders o JOIN users u ON u.id = o.user_id "
     "WHERE o.created_at >= '2026-09-01' GROUP BY o.user_id"),
    ("r3-time-plus-left-coalesce", "SELECT COALESCE(SUM(r.amount), 0) FROM orders o LEFT JOIN refunds r "
     "ON r.order_id = o.id WHERE o.paid_at >= '2026-09-01'",
     "SELECT COALESCE(SUM(r.amount), 0) FROM orders o JOIN refunds r ON r.order_id = o.id "
     "WHERE o.created_at >= '2026-09-01'"),
    # 下面几对看起来像本轮新认的等价改写，其实不是（守住新改写的边界）
    ("r3-left-count-star-not-null-blind", "SELECT COUNT(*) FROM orders o LEFT JOIN refunds r ON r.order_id = o.id",
     "SELECT COUNT(*) FROM orders o JOIN refunds r ON r.order_id = o.id"),
    ("r3-left-coalesce-inside-sum", "SELECT SUM(COALESCE(r.amount, 0)) FROM orders o LEFT JOIN refunds r ON r.order_id = o.id",
     "SELECT SUM(COALESCE(r.amount, 0)) FROM orders o JOIN refunds r ON r.order_id = o.id"),
    ("r3-left-grouped-sum", "SELECT o.user_id, SUM(r.amount) FROM orders o LEFT JOIN refunds r ON r.order_id = o.id "
     "GROUP BY o.user_id",
     "SELECT o.user_id, SUM(r.amount) FROM orders o JOIN refunds r ON r.order_id = o.id GROUP BY o.user_id"),
    ("r3-left-sum-other-side", "SELECT SUM(r.amount), SUM(o.amount) FROM orders o LEFT JOIN refunds r ON r.order_id = o.id",
     "SELECT SUM(r.amount), SUM(o.amount) FROM orders o JOIN refunds r ON r.order_id = o.id"),
    ("r3-left-extra-count-star", "SELECT COUNT(*) FROM orders o LEFT JOIN refunds r ON r.order_id = o.id",
     "SELECT COUNT(*) FROM orders o"),
    ("r3-left-extra-sum", "SELECT SUM(o.amount) FROM orders o LEFT JOIN refunds r ON r.order_id = o.id",
     "SELECT SUM(o.amount) FROM orders o"),
    ("r3-left-extra-used-in-where", "SELECT DISTINCT o.user_id FROM orders o LEFT JOIN refunds r ON r.order_id = o.id "
     "WHERE r.id IS NULL",
     "SELECT DISTINCT o.user_id FROM orders o"),
    ("r3-inner-extra-distinct", "SELECT DISTINCT o.user_id FROM orders o JOIN refunds r ON r.order_id = o.id",
     "SELECT DISTINCT o.user_id FROM orders o"),
    ("r3-in-group-by-aggregate", "SELECT COUNT(*) FROM users WHERE id IN (SELECT MAX(user_id) FROM orders GROUP BY store_id)",
     "SELECT COUNT(*) FROM users WHERE id IN (SELECT MAX(user_id) FROM orders)"),
    ("r3-in-group-by-having", "SELECT COUNT(*) FROM users WHERE id IN (SELECT user_id FROM orders GROUP BY user_id "
     "HAVING COUNT(*) > 1)",
     "SELECT COUNT(*) FROM users WHERE id IN (SELECT user_id FROM orders)"),
    ("r3-in-union-limit", "SELECT COUNT(*) FROM users WHERE id IN (SELECT user_id FROM orders UNION "
     "SELECT user_id FROM refunds ORDER BY 1 LIMIT 3)",
     "SELECT COUNT(*) FROM users WHERE id IN (SELECT user_id FROM orders UNION ALL SELECT user_id FROM refunds "
     "ORDER BY 1 LIMIT 3)"),
    ("r3-exists-group-by-offset", "SELECT COUNT(*) FROM users u WHERE EXISTS (SELECT o.status FROM orders o "
     "WHERE o.user_id = u.id GROUP BY o.status LIMIT 1 OFFSET 1)",
     "SELECT COUNT(*) FROM users u WHERE EXISTS (SELECT o.status FROM orders o WHERE o.user_id = u.id LIMIT 1 OFFSET 1)"),
    ("r3-exists-aggregate-ungrouped", "SELECT COUNT(*) FROM users u WHERE EXISTS (SELECT COUNT(*) FROM orders o "
     "WHERE o.user_id = u.id)",
     "SELECT COUNT(*) FROM users u WHERE EXISTS (SELECT o.id FROM orders o WHERE o.user_id = u.id)"),
    ("r3-group-key-equal-left-join", "SELECT u.id, COUNT(*) FROM users u LEFT JOIN orders o ON o.user_id = u.id "
     "GROUP BY u.id",
     "SELECT o.user_id, COUNT(*) FROM users u LEFT JOIN orders o ON o.user_id = u.id GROUP BY o.user_id"),
    ("r3-not-null-on-nullable-side", "SELECT o.id, r.id FROM orders o LEFT JOIN refunds r ON r.order_id = o.id "
     "WHERE r.id IS NOT NULL",
     "SELECT o.id, r.id FROM orders o LEFT JOIN refunds r ON r.order_id = o.id"),
    ("r3-limit-negative-vs-zero", "SELECT id FROM orders ORDER BY id LIMIT -1", "SELECT id FROM orders ORDER BY id LIMIT 0"),
    ("r3-order-dup-key-direction", "SELECT id, user_id FROM orders ORDER BY user_id, id DESC LIMIT 5",
     "SELECT id, user_id FROM orders ORDER BY user_id, id LIMIT 5"),
]

#: 意思一样、本轮规范化认得出的改写（一部分要给表结构：要知道两列声明的类型一样，相等才是一样）
_SAME_R3 = [
    # EXISTS、IN 只看有没有、在不在：子查询里去重、分组、UNION 还是 UNION ALL 都不影响
    ("r3-exists-distinct", "SELECT o.id FROM orders o WHERE EXISTS (SELECT DISTINCT r.amount FROM refunds r "
     "WHERE r.order_id = o.id)",
     "SELECT o.id FROM orders o WHERE EXISTS (SELECT r.amount FROM refunds r WHERE r.order_id = o.id)"),
    ("r3-not-exists-distinct", "SELECT o.id FROM orders o WHERE NOT EXISTS (SELECT DISTINCT r.amount FROM refunds r "
     "WHERE r.order_id = o.id)",
     "SELECT o.id FROM orders o WHERE NOT EXISTS (SELECT r.amount FROM refunds r WHERE r.order_id = o.id)"),
    ("r3-exists-group-by", "SELECT o.id FROM orders o WHERE EXISTS (SELECT r.state FROM refunds r "
     "WHERE r.order_id = o.id GROUP BY r.state)",
     "SELECT o.id FROM orders o WHERE EXISTS (SELECT r.state FROM refunds r WHERE r.order_id = o.id)"),
    ("r3-exists-union-vs-union-all", "SELECT COUNT(*) FROM users u WHERE EXISTS (SELECT o.id FROM orders o "
     "WHERE o.user_id = u.id UNION SELECT r.id FROM refunds r WHERE r.user_id = u.id)",
     "SELECT COUNT(*) FROM users u WHERE EXISTS (SELECT o.id FROM orders o WHERE o.user_id = u.id "
     "UNION ALL SELECT r.id FROM refunds r WHERE r.user_id = u.id)"),
    ("r3-in-union-vs-union-all", "SELECT COUNT(*) FROM users WHERE id IN (SELECT user_id FROM orders UNION "
     "SELECT user_id FROM refunds)",
     "SELECT COUNT(*) FROM users WHERE id IN (SELECT user_id FROM orders UNION ALL SELECT user_id FROM refunds)"),
    ("r3-in-group-by", "SELECT COUNT(*) FROM users WHERE id IN (SELECT user_id FROM orders GROUP BY user_id)",
     "SELECT COUNT(*) FROM users WHERE id IN (SELECT user_id FROM orders)"),
    ("r3-in-group-by-two-keys", "SELECT COUNT(*) FROM users WHERE id IN (SELECT user_id FROM orders "
     "GROUP BY user_id, status)",
     "SELECT COUNT(*) FROM users WHERE id IN (SELECT user_id FROM orders)"),
    ("r3-not-in-group-by", "SELECT COUNT(*) FROM users WHERE id NOT IN (SELECT user_id FROM orders "
     "WHERE user_id IS NOT NULL GROUP BY user_id)",
     "SELECT COUNT(*) FROM users WHERE id NOT IN (SELECT user_id FROM orders WHERE user_id IS NOT NULL)"),
    ("r3-in-group-by-having-count-noop", "SELECT COUNT(*) FROM users WHERE id IN (SELECT user_id FROM orders "
     "GROUP BY user_id HAVING COUNT(*) >= 1)",
     "SELECT COUNT(*) FROM users WHERE id IN (SELECT user_id FROM orders)"),
    # 结果只是可空一侧的聚合（跳过空值、参数在那一侧整行为空时一定是空值）：LEFT JOIN 和 INNER JOIN 一样
    ("r3-left-coalesce-sum", "SELECT COALESCE(SUM(r.amount), 0) FROM orders o LEFT JOIN refunds r ON r.order_id = o.id "
     "WHERE o.status = 'paid'",
     "SELECT COALESCE(SUM(r.amount), 0) FROM orders o JOIN refunds r ON r.order_id = o.id WHERE o.status = 'paid'"),
    ("r3-left-sum-div-count", "SELECT SUM(r.amount) / COUNT(r.id) FROM orders o LEFT JOIN refunds r ON r.order_id = o.id",
     "SELECT SUM(r.amount) / COUNT(r.id) FROM orders o JOIN refunds r ON r.order_id = o.id"),
    ("r3-left-round-sum", "SELECT ROUND(SUM(r.amount)) FROM orders o LEFT JOIN refunds r ON r.order_id = o.id",
     "SELECT ROUND(SUM(r.amount)) FROM orders o INNER JOIN refunds r ON r.order_id = o.id"),
    ("r3-left-constant-column", "SELECT 'all', SUM(r.amount), MAX(r.refunded_at) FROM orders o "
     "LEFT JOIN refunds r ON r.order_id = o.id",
     "SELECT 'all', SUM(r.amount), MAX(r.refunded_at) FROM orders o JOIN refunds r ON r.order_id = o.id"),
    # 没用到的 LEFT JOIN：左表的行只会变多、不会变少，结果再去重（或者只取不怕重复的聚合）就一样
    ("r3-left-extra-distinct", "SELECT DISTINCT o.user_id FROM orders o LEFT JOIN refunds r ON r.order_id = o.id",
     "SELECT DISTINCT o.user_id FROM orders o"),
    ("r3-left-extra-max", "SELECT MAX(o.amount) FROM orders o LEFT JOIN refunds r ON r.order_id = o.id",
     "SELECT MAX(o.amount) FROM orders o"),
    ("r3-left-extra-count-distinct", "SELECT COUNT(DISTINCT o.user_id) FROM orders o LEFT JOIN refunds r "
     "ON r.order_id = o.id WHERE o.is_deleted = 0",
     "SELECT COUNT(DISTINCT o.user_id) FROM orders o WHERE o.is_deleted = 0"),
    ("r3-left-extra-group-by-max", "SELECT o.user_id, MAX(o.amount) FROM orders o LEFT JOIN refunds r "
     "ON r.order_id = o.id GROUP BY o.user_id",
     "SELECT o.user_id, MAX(o.amount) FROM orders o GROUP BY o.user_id"),
    ("r3-left-extra-in-subquery", "SELECT COUNT(*) FROM users WHERE id IN (SELECT o.user_id FROM orders o "
     "LEFT JOIN refunds r ON r.order_id = o.id)",
     "SELECT COUNT(*) FROM users WHERE id IN (SELECT o.user_id FROM orders o)"),
    # ON 里另一个条件已经保证不为空：多写的 IS NOT NULL 不影响关联上哪些行
    ("r3-left-on-redundant-not-null", "SELECT o.id, r.amount FROM orders o LEFT JOIN refunds r ON r.order_id = o.id "
     "AND r.order_id IS NOT NULL",
     "SELECT o.id, r.amount FROM orders o LEFT JOIN refunds r ON r.order_id = o.id"),
    ("r3-left-on-redundant-not-null-2key", "SELECT o.id, r.amount FROM orders o LEFT JOIN refunds r "
     "ON r.order_id = o.id AND r.user_id = o.user_id AND r.user_id IS NOT NULL",
     "SELECT o.id, r.amount FROM orders o LEFT JOIN refunds r ON r.order_id = o.id AND r.user_id = o.user_id"),
    ("r3-left-on-redundant-preserved-not-null", "SELECT o.id, r.amount FROM orders o LEFT JOIN refunds r "
     "ON r.order_id = o.id AND o.id IS NOT NULL",
     "SELECT o.id, r.amount FROM orders o LEFT JOIN refunds r ON r.order_id = o.id"),
    ("r3-where-redundant-not-null-in", "SELECT COUNT(*) FROM orders WHERE user_id IN (SELECT id FROM users) "
     "AND user_id IS NOT NULL",
     "SELECT COUNT(*) FROM orders WHERE user_id IN (SELECT id FROM users)"),
    # 一模一样的分组键、排序键写了两遍；SQLite 的负数 LIMIT 就是不限行数；LIMIT 写成常量算式
    ("r3-group-by-duplicate-key", "SELECT user_id, COUNT(*) FROM orders GROUP BY user_id, user_id",
     "SELECT user_id, COUNT(*) FROM orders GROUP BY user_id"),
    ("r3-order-by-duplicate-key", "SELECT user_id FROM orders GROUP BY user_id ORDER BY user_id, user_id",
     "SELECT user_id FROM orders GROUP BY user_id ORDER BY user_id"),
    ("r3-order-by-repeated-key-other-direction", "SELECT id FROM orders ORDER BY id, id DESC LIMIT 5",
     "SELECT id FROM orders ORDER BY id LIMIT 5"),
    ("r3-limit-minus-one", "SELECT id FROM orders ORDER BY id LIMIT -1", "SELECT id FROM orders ORDER BY id"),
    ("r3-limit-negative", "SELECT id FROM orders ORDER BY id LIMIT -5", "SELECT id FROM orders ORDER BY id"),
    ("r3-limit-negative-offset", "SELECT id FROM orders ORDER BY id LIMIT -1 OFFSET 2",
     "SELECT id FROM orders ORDER BY id LIMIT -7 OFFSET 2"),
    ("r3-limit-arithmetic", "SELECT id FROM orders ORDER BY id LIMIT 1 + 2", "SELECT id FROM orders ORDER BY id LIMIT 3"),
    ("r3-having-true-aggregate", "SELECT COUNT(*) FROM orders HAVING 1 = 1", "SELECT COUNT(*) FROM orders"),
]

#: 意思一样、要给表结构才认得出的（双引号里的名字是不是外层表的列、关联条件定成相等的两列声明的类型一样不一样，都要
#: 看表结构）：没给表结构时只许给兜底说明；认出来的比例单算（见 test_most_known_equivalent_rewrites_are_recognized）
_SAME_TYPED_R3 = [
    # 外层的 refunds 有 state 列：双引号里的 "State"、"state" 都是这一列（SQLite 不分大小写），不是字符串
    ("r3-double-quoted-outer-column", 'SELECT COUNT(*) FROM refunds r WHERE EXISTS (SELECT 1 FROM orders o '
     'WHERE o.id = r.order_id AND o.status = "State")',
     'SELECT COUNT(*) FROM refunds r WHERE EXISTS (SELECT 1 FROM orders o WHERE o.id = r.order_id AND o.status = "state")'),
    # 关联条件定成相等的两列：按哪一列分组、排序、选出来都一样（要给表结构：两列声明的类型一样）
    ("r3-group-key-equal-by-join", "SELECT u.id, COUNT(*) AS n FROM users u JOIN orders o ON o.user_id = u.id GROUP BY u.id",
     "SELECT o.user_id, COUNT(*) AS n FROM orders o JOIN users u ON u.id = o.user_id GROUP BY o.user_id"),
    ("r3-group-key-equal-by-join-sum", "SELECT o.user_id, SUM(o.amount) FROM orders o JOIN users u ON u.id = o.user_id "
     "GROUP BY o.user_id",
     "SELECT u.id, SUM(o.amount) FROM orders o JOIN users u ON u.id = o.user_id GROUP BY u.id"),
    ("r3-group-key-equal-by-using", "SELECT COUNT(*) FROM orders o JOIN refunds r USING (user_id) GROUP BY o.user_id",
     "SELECT COUNT(*) FROM orders o JOIN refunds r USING (user_id) GROUP BY r.user_id"),
    ("r3-group-key-equal-by-where", "SELECT COUNT(*) FROM orders WHERE user_id = referrer_id GROUP BY user_id",
     "SELECT COUNT(*) FROM orders WHERE user_id = referrer_id GROUP BY referrer_id"),
    ("r3-order-key-equal-by-join", "SELECT o.id FROM orders o JOIN users u ON u.id = o.user_id ORDER BY o.user_id, o.id",
     "SELECT o.id FROM orders o JOIN users u ON u.id = o.user_id ORDER BY u.id, o.id"),
    # 带 LIMIT 的一段结果看行出来的先后，规范化不换列（换了可能换一种扫描的先后），给了表结构也认不出，只给兜底
    ("r3-order-key-equal-by-join-limit", "SELECT o.id FROM orders o JOIN users u ON u.id = o.user_id "
     "ORDER BY o.user_id, o.id LIMIT 5",
     "SELECT o.id FROM orders o JOIN users u ON u.id = o.user_id ORDER BY u.id, o.id LIMIT 5"),
]

#: 意思一样、要靠约束（主键不为空）才看得出：给了表结构（schema_cache 里有 primary_key）认得出；没给表结构时说得出
#: 「A 排除了 id 为空的行」也不算错（不知道它不会为空）
_SAME_SCHEMA_R3 = [
    ("r3-left-not-null-primary-key-vs-inner", "SELECT o.id, r.amount FROM orders o LEFT JOIN refunds r "
     "ON r.order_id = o.id WHERE r.id IS NOT NULL",
     "SELECT o.id, r.amount FROM orders o JOIN refunds r ON r.order_id = o.id"),
    ("r3-where-not-null-primary-key", "SELECT COUNT(*) FROM orders WHERE id IS NOT NULL AND status = 'paid'",
     "SELECT COUNT(*) FROM orders WHERE status = 'paid'"),
]

#: 意思一样、要按语义推理才看得出的：认不出时只许给兜底说明（复验专挑这类对查「说出不存在的差异」）
_SAME_HARD_R3 = [
    ("r3-disjoint-union-all-vs-union", "SELECT DISTINCT user_id FROM orders WHERE user_id > 5 UNION ALL "
     "SELECT DISTINCT user_id FROM orders WHERE user_id <= 5",
     "SELECT user_id FROM orders WHERE user_id > 5 UNION SELECT user_id FROM orders WHERE user_id <= 5"),
    ("r3-order-single-group", "SELECT user_id, COUNT(*) FROM orders WHERE user_id = 3 GROUP BY user_id ORDER BY user_id",
     "SELECT user_id, COUNT(*) FROM orders WHERE user_id = 3 GROUP BY user_id"),
    ("r3-distinct-join-vs-in", "SELECT DISTINCT o.id FROM orders o JOIN users u ON u.id = o.user_id",
     "SELECT DISTINCT o.id FROM orders o WHERE o.user_id IN (SELECT id FROM users)"),
    ("r3-count-distinct-join-vs-in", "SELECT COUNT(DISTINCT o.id) FROM orders o JOIN refunds r ON r.order_id = o.id",
     "SELECT COUNT(DISTINCT o.id) FROM orders o WHERE o.id IN (SELECT order_id FROM refunds)"),
    ("r3-join-vs-exists-distinct", "SELECT DISTINCT o.id FROM orders o JOIN refunds r ON r.order_id = o.id "
     "AND r.state = 'done'",
     "SELECT o.id FROM orders o WHERE EXISTS (SELECT 1 FROM refunds r WHERE r.order_id = o.id AND r.state = 'done')"),
    # 子查询的别名遮住了外层的别名，B 把它写明白了：两条意思一样，A 的 o.status 指外层的 o
    ("r3-shadowed-alias-written-out", "SELECT COUNT(*) FROM orders o JOIN orders c ON c.parent_id = o.id WHERE EXISTS "
     "(SELECT 1 FROM refunds AS o WHERE o.amount > 0 AND o.status = 'paid')",
     "SELECT COUNT(*) FROM orders o JOIN orders c ON c.parent_id = o.id WHERE o.status = 'paid' AND EXISTS "
     "(SELECT 1 FROM refunds AS r WHERE r.amount > 0)"),
    ("r3-group-key-where-vs-having", "SELECT status, COUNT(*) FROM orders WHERE status = 'paid' GROUP BY status",
     "SELECT status, COUNT(*) FROM orders GROUP BY status HAVING status = 'paid'"),
    ("r3-max-outer-vs-inner", "SELECT COUNT(*) FROM (SELECT user_id, MAX(amount) AS m FROM orders GROUP BY user_id) t "
     "WHERE m > 50",
     "SELECT COUNT(*) FROM (SELECT user_id, MAX(amount) AS m FROM orders WHERE amount > 50 GROUP BY user_id) t"),
]

#: 只在 PostgreSQL / MySQL 才有的写法（不执行，只查不判等价）：第 2 轮复验在真实的库上跑出结果不同的
REGRESSIONS_R2: list[tuple[str, str, str, str]] = [
    ("having_binary_is_not_where", "mysql",
     "SELECT city, COUNT(*) FROM users GROUP BY city HAVING BINARY city = 'SH'",
     "SELECT city, COUNT(*) FROM users WHERE BINARY city = 'SH' GROUP BY city"),
    ("having_like_binary_is_not_where", "mysql",
     "SELECT city, COUNT(*) FROM users GROUP BY city HAVING city LIKE BINARY 'S%'",
     "SELECT city, COUNT(*) FROM users WHERE city LIKE BINARY 'S%' GROUP BY city"),
    ("having_collate_is_not_where", "mysql",
     "SELECT city, COUNT(*) FROM users GROUP BY city HAVING city COLLATE utf8mb4_bin = 'SH'",
     "SELECT city, COUNT(*) FROM users WHERE city COLLATE utf8mb4_bin = 'SH' GROUP BY city"),
    ("distinct_city_join_order_ci", "mysql",
     "SELECT DISTINCT u.city FROM users u JOIN orders o ON o.user_id = u.id",
     "SELECT DISTINCT u.city FROM orders o JOIN users u ON o.user_id = u.id"),
    ("group_city_join_order_ci", "mysql",
     "SELECT u.city, COUNT(*) FROM users u JOIN orders o ON o.user_id = u.id GROUP BY u.city",
     "SELECT u.city, COUNT(*) FROM orders o JOIN users u ON o.user_id = u.id GROUP BY u.city"),
    ("order_by_aggregate_in_exists", "postgres",
     "SELECT COUNT(*) FROM orders o WHERE EXISTS (SELECT 1 FROM refunds r WHERE r.order_id = o.id ORDER BY COUNT(*))",
     "SELECT COUNT(*) FROM orders o WHERE EXISTS (SELECT 1 FROM refunds r WHERE r.order_id = o.id)"),
    ("order_by_aggregate_in_in", "postgres",
     "SELECT COUNT(*) FROM users WHERE vip IN (SELECT 1 FROM refunds WHERE amount > 1000 ORDER BY COUNT(*))",
     "SELECT COUNT(*) FROM users WHERE vip IN (SELECT 1 FROM refunds WHERE amount > 1000)"),
    ("cte_column_list_swap", "postgres",
     "WITH p(uid, total) AS (SELECT user_id, SUM(amount) FROM orders GROUP BY user_id) SELECT COUNT(*) FROM p WHERE total > 100",
     "WITH p(total, uid) AS (SELECT user_id, SUM(amount) FROM orders GROUP BY user_id) SELECT COUNT(*) FROM p WHERE total > 100"),
    ("cte_column_list_swap", "mysql",
     "WITH t(user_id, amount) AS (SELECT amount, user_id FROM orders) SELECT SUM(amount) FROM t",
     "WITH t(amount, user_id) AS (SELECT amount, user_id FROM orders) SELECT SUM(amount) FROM t"),
    ("derived_column_list_swap", "postgres",
     "SELECT SUM(t.x) FROM (SELECT amount, tax FROM orders) AS t(x, y)",
     "SELECT SUM(t.x) FROM (SELECT amount, tax FROM orders) AS t(y, x)"),
    ("derived_column_list_swap", "mysql",
     "SELECT t.b FROM (SELECT user_id, amount FROM orders) AS t(a, b) WHERE t.a > 3",
     "SELECT t.b FROM (SELECT user_id, amount FROM orders) AS t(b, a) WHERE t.a > 3"),
    ("derived_column_list_renames", "postgres",
     "SELECT amount, user_id FROM (SELECT amount, user_id FROM orders) AS t(user_id, amount)",
     "SELECT amount, user_id FROM orders"),
    ("table_alias_column_list", "postgres",
     "SELECT COUNT(*) FROM orders AS o(user_id, id) WHERE o.user_id > 5",
     "SELECT COUNT(*) FROM orders AS o WHERE o.user_id > 5"),
    ("union_all_in_cte_alias_order", "postgres",
     "WITH u AS (SELECT user_id AS a, amount AS b FROM orders UNION ALL SELECT amount AS b, user_id AS a FROM refunds) "
     "SELECT SUM(b), SUM(a) FROM u",
     "WITH u AS (SELECT amount AS b, user_id AS a FROM refunds UNION ALL SELECT user_id AS a, amount AS b FROM orders) "
     "SELECT SUM(b), SUM(a) FROM u"),
    ("union_all_in_cte_alias_order", "mysql",
     "WITH u AS (SELECT user_id AS a, amount AS b FROM orders UNION ALL SELECT amount AS b, user_id AS a FROM refunds) "
     "SELECT SUM(b), SUM(a) FROM u",
     "WITH u AS (SELECT amount AS b, user_id AS a FROM refunds UNION ALL SELECT user_id AS a, amount AS b FROM orders) "
     "SELECT SUM(b), SUM(a) FROM u"),
    ("between_symmetric", "postgres",
     "SELECT COUNT(*) FROM orders WHERE amount BETWEEN SYMMETRIC 100 AND 10",
     "SELECT COUNT(*) FROM orders WHERE amount BETWEEN 100 AND 10"),
    ("group_key_order_with_limit", "postgres",
     "SELECT user_id, store_id, COUNT(*) FROM orders GROUP BY user_id, store_id ORDER BY COUNT(*) DESC LIMIT 2",
     "SELECT user_id, store_id, COUNT(*) FROM orders GROUP BY store_id, user_id ORDER BY COUNT(*) DESC LIMIT 2"),
    ("exists_distinct_offset", "postgres",
     "SELECT COUNT(*) FROM orders o WHERE EXISTS (SELECT DISTINCT r.state FROM refunds r WHERE r.amount > 0 LIMIT 1 OFFSET 1)",
     "SELECT COUNT(*) FROM orders o WHERE EXISTS (SELECT r.state FROM refunds r WHERE r.amount > 0 LIMIT 1 OFFSET 1)"),
    ("distinct_on_keeps_a_different_row", "postgres",
     "SELECT DISTINCT ON (user_id) user_id, status FROM orders ORDER BY user_id, created_at DESC NULLS LAST, id",
     "SELECT DISTINCT ON (user_id) user_id, status FROM orders ORDER BY user_id, paid_at DESC NULLS LAST, id"),
    ("float_folding", "postgres",
     "SELECT COUNT(*) FROM orders WHERE amount = 0.1 + 0.2", "SELECT COUNT(*) FROM orders WHERE amount = 0.3"),
    ("double_negation", "mysql", "SELECT id, -(-name) FROM users", "SELECT id, name FROM users"),
    # 本轮自己在私起的 PostgreSQL 18、MySQL 8.0 上跑出来的：调换 UNION ALL 各支，浮点数加的先后变了
    # （1902.2 对 1902.2000000000003）；MySQL 把外层只涉及分组键的条件推进子查询的 WHERE，和 HAVING 结果不同
    ("float_sum_union_branch_order", "postgres",
     "SELECT SUM(x) FROM (SELECT amount AS x FROM orders UNION ALL SELECT amount AS x FROM refunds) t",
     "SELECT SUM(x) FROM (SELECT amount AS x FROM refunds UNION ALL SELECT amount AS x FROM orders) t"),
    ("float_sum_union_branch_order", "mysql",
     "SELECT SUM(x) FROM (SELECT amount AS x FROM orders UNION ALL SELECT amount AS x FROM refunds) t",
     "SELECT SUM(x) FROM (SELECT amount AS x FROM refunds UNION ALL SELECT amount AS x FROM orders) t"),
    ("outer_where_on_group_key_vs_having", "mysql",
     "SELECT COUNT(*) FROM (SELECT city, COUNT(*) AS n FROM users GROUP BY city) t WHERE BINARY t.city = 'SH'",
     "SELECT COUNT(*) FROM (SELECT city, COUNT(*) AS n FROM users GROUP BY city HAVING BINARY city = 'SH') t"),
]

#: 意思一样、规范化认得出的（PostgreSQL / MySQL 的写法）
DIALECT_SAME_R2: list[tuple[str, str, str, str]] = [
    ("fetch_first_is_limit", "postgres", "SELECT id FROM orders ORDER BY id FETCH FIRST 4 ROWS ONLY",
     "SELECT id FROM orders ORDER BY id LIMIT 4"),
    ("offset_fetch_is_limit_offset", "postgres", "SELECT id FROM orders ORDER BY id OFFSET 2 ROWS FETCH NEXT 4 ROWS ONLY",
     "SELECT id FROM orders ORDER BY id LIMIT 4 OFFSET 2"),
    ("mysql_column_names_ignore_case", "mysql", "SELECT SUM(amount) FROM orders WHERE is_deleted = 0",
     "SELECT SUM(AMOUNT) FROM orders WHERE IS_DELETED = 0"),
    ("is_not_null_forms", "postgres", "SELECT COUNT(*) FROM orders WHERE NOT (paid_at IS NOT NULL)",
     "SELECT COUNT(*) FROM orders WHERE paid_at IS NULL"),
    ("not_like_forms", "postgres", "SELECT COUNT(*) FROM orders WHERE status NOT LIKE 'p%'",
     "SELECT COUNT(*) FROM orders WHERE NOT status LIKE 'p%'"),
    ("if_vs_case", "mysql", "SELECT SUM(IF(status = 'paid', amount, 0)) FROM orders",
     "SELECT SUM(CASE WHEN status = 'paid' THEN amount ELSE 0 END) FROM orders"),
]

#: 意思一样、规范化故意不认的（认了就要假设「相等就是一样」、或者 PostgreSQL / MySQL 里浮点数求和的先后不变，见模块
#: 说明）：只给兜底说明，不给具体说法
KNOWN_UNRECOGNIZED: list[tuple[str, str, str, str]] = [
    # PostgreSQL / MySQL 里对浮点数求和的一段调换关联先后：加的先后变了，结果的最后几位可能不同
    ("the_same_derived_column_written_differently_is_no_difference", "mysql",
     "SELECT SUM(x.total) FROM (SELECT p.user_id, p.total FROM (SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id) p JOIN (SELECT user_id, SUM(amount) AS total FROM refunds GROUP BY user_id) r ON r.user_id = p.user_id) x",
     "SELECT SUM(y.total) FROM (SELECT a.user_id, a.total FROM (SELECT user_id, SUM(amount) AS total FROM refunds GROUP BY user_id) b JOIN (SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id) a ON a.user_id = b.user_id) y"),
    ("derived_tables_reading_the_same_table_written_differently_ar", "postgres",
     "SELECT SUM(p.amount) FROM (SELECT * FROM orders WHERE status = 'paid') p JOIN (SELECT * FROM orders WHERE status = 'open') o ON o.user_id = p.user_id",
     "SELECT SUM(x.amount) FROM (SELECT * FROM orders WHERE status = 'open') y JOIN (SELECT * FROM orders WHERE status = 'paid') x ON x.user_id = y.user_id"),
    ("having_on_group_key_vs_where", "sqlite",
     "WITH t AS (SELECT user_id, SUM(amount) AS total FROM orders WHERE user_id > 100 GROUP BY user_id) SELECT SUM(total) FROM t",
     "WITH t AS (SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id) SELECT SUM(total) FROM t WHERE user_id > 100"),
    ("union_dedup_branch_order", "sqlite",
     "SELECT id, amount FROM orders UNION SELECT id, amount FROM orders_archive",
     "SELECT id, amount FROM orders_archive UNION SELECT id, amount FROM orders"),
    ("window_partition_key_order", "sqlite",
     "SELECT id, SUM(amount) OVER (PARTITION BY user_id, store_id) AS s FROM orders",
     "SELECT id, SUM(amount) OVER (PARTITION BY store_id, user_id) AS s FROM orders"),
    ("distinct_vs_group_mysql_strings", "mysql",
     "SELECT DISTINCT city FROM users", "SELECT city FROM users GROUP BY city"),
]

#: 第 3 轮：只在某个方言才有、或者执行起来结果每次都不同的写法（不执行，只查不判等价）。PostgreSQL / MySQL 的几对
#: 复验者和本轮都在真实的 PostgreSQL 18、MySQL 8.0 上跑过（或者按官方文档）
REGRESSIONS_R3: list[tuple[str, str, str, str]] = [
    # 外连接改内连接：NOT IN (空集)、x op ALL (空集)、NOT (x = ANY (空集)) 在 x 是空值时也是真
    ("left_not_in_subquery", "postgres",
     "SELECT o.id FROM orders o LEFT JOIN users u ON u.id = o.user_id WHERE u.city NOT IN "
     "(SELECT s.city FROM stores s WHERE s.region = 'nowhere')",
     "SELECT o.id FROM orders o JOIN users u ON u.id = o.user_id WHERE u.city NOT IN "
     "(SELECT s.city FROM stores s WHERE s.region = 'nowhere')"),
    ("left_not_in_subquery", "mysql",
     "SELECT o.id FROM orders o LEFT JOIN users u ON u.id = o.user_id WHERE u.city NOT IN "
     "(SELECT s.city FROM stores s WHERE s.region = 'nowhere')",
     "SELECT o.id FROM orders o JOIN users u ON u.id = o.user_id WHERE u.city NOT IN "
     "(SELECT s.city FROM stores s WHERE s.region = 'nowhere')"),
    ("left_gt_all", "postgres",
     "SELECT o.id FROM orders o LEFT JOIN users u ON u.id = o.user_id WHERE u.vip > ALL "
     "(SELECT s.id FROM stores s WHERE s.region = 'nowhere')",
     "SELECT o.id FROM orders o JOIN users u ON u.id = o.user_id WHERE u.vip > ALL "
     "(SELECT s.id FROM stores s WHERE s.region = 'nowhere')"),
    ("left_gt_all", "mysql",
     "SELECT o.id FROM orders o LEFT JOIN users u ON u.id = o.user_id WHERE u.vip > ALL "
     "(SELECT s.id FROM stores s WHERE s.region = 'nowhere')",
     "SELECT o.id FROM orders o JOIN users u ON u.id = o.user_id WHERE u.vip > ALL "
     "(SELECT s.id FROM stores s WHERE s.region = 'nowhere')"),
    ("left_neq_all", "postgres",
     "SELECT o.id FROM orders o LEFT JOIN users u ON u.id = o.user_id WHERE u.vip <> ALL "
     "(SELECT s.id FROM stores s WHERE s.region = 'nowhere')",
     "SELECT o.id FROM orders o JOIN users u ON u.id = o.user_id WHERE u.vip <> ALL "
     "(SELECT s.id FROM stores s WHERE s.region = 'nowhere')"),
    ("left_not_eq_any", "postgres",
     "SELECT o.id FROM orders o LEFT JOIN users u ON u.id = o.user_id WHERE NOT (u.vip = ANY "
     "(SELECT s.id FROM stores s WHERE s.region = 'nowhere'))",
     "SELECT o.id FROM orders o JOIN users u ON u.id = o.user_id WHERE NOT (u.vip = ANY "
     "(SELECT s.id FROM stores s WHERE s.region = 'nowhere'))"),
    ("left_neq_all_empty_array", "postgres",
     "SELECT o.id FROM orders o LEFT JOIN users u ON u.id = o.user_id WHERE u.vip <> ALL (ARRAY[]::int[])",
     "SELECT o.id FROM orders o JOIN users u ON u.id = o.user_id WHERE u.vip <> ALL (ARRAY[]::int[])"),
    # PostgreSQL 里空的数组接上一个数组还是那个数组（不是空值）
    ("left_array_concat", "postgres",
     "SELECT o.id FROM orders o LEFT JOIN users u ON u.id = o.user_id WHERE u.tags || ARRAY['x'] = ARRAY['x']",
     "SELECT o.id FROM orders o JOIN users u ON u.id = o.user_id WHERE u.tags || ARRAY['x'] = ARRAY['x']"),
    ("left_concat_function", "postgres",
     "SELECT o.id FROM orders o LEFT JOIN users u ON u.id = o.user_id WHERE CONCAT(u.city, 'x') = 'x'",
     "SELECT o.id FROM orders o JOIN users u ON u.id = o.user_id WHERE CONCAT(u.city, 'x') = 'x'"),
    # ORDER BY 里单独一个名字先认列别名
    ("using_order_by_alias", "postgres",
     "SELECT -o.id AS id FROM orders o JOIN users USING (id) ORDER BY id LIMIT 2",
     "SELECT -o.id AS id FROM orders o JOIN users u ON o.id = u.id ORDER BY o.id LIMIT 2"),
    # PostgreSQL 里没有 GROUP BY 的 HAVING 让整条语句成了只出一行的聚合
    ("having_true_without_group", "postgres", "SELECT 1 FROM orders HAVING 1 = 1", "SELECT 1 FROM orders"),
    ("having_true_without_group_where", "postgres",
     "SELECT 1 AS k FROM orders WHERE status = 'paid' HAVING TRUE", "SELECT 1 AS k FROM orders WHERE status = 'paid'"),
    ("having_true_without_group", "mysql", "SELECT 1 FROM orders HAVING 1 = 1", "SELECT 1 FROM orders"),
    # MySQL：/*! … */ 里的代码会执行；! 比比较运算结合得紧；--1 是减负一（-- 后面要有空白才是注释）；
    # BETWEEN 的上下界类型不同时三个参数一起换算
    ("executable_comment", "mysql", "SELECT COUNT(*) FROM orders /*! WHERE status = 'paid' */",
     "SELECT COUNT(*) FROM orders"),
    ("executable_comment_versioned", "mysql", "SELECT COUNT(*) FROM orders /*!50001 WHERE status = 'paid' */",
     "SELECT COUNT(*) FROM orders"),
    ("bang_precedence", "mysql", "SELECT COUNT(*) FROM orders WHERE ! status = 'paid'",
     "SELECT COUNT(*) FROM orders WHERE NOT status = 'paid'"),
    ("double_dash_without_space", "mysql", "SELECT id, amount --1 FROM orders", "SELECT id, amount FROM orders"),
    ("between_mixed_bound_types", "mysql", "SELECT id FROM users WHERE name BETWEEN 2 AND '3'",
     "SELECT id FROM users WHERE name >= 2 AND name <= '3'"),
    ("between_column_bounds", "mysql", "SELECT id FROM orders WHERE amount BETWEEN discount AND tax",
     "SELECT id FROM orders WHERE amount >= discount AND amount <= tax"),
    # 不确定的函数：每次求值都可能不同，两次 RANDOM() 不是同一个值（执行起来每次结果都不同，只查结论）
    ("random_twice", "sqlite", "SELECT COUNT(*) FROM orders WHERE RANDOM() % 2 = 0 AND RANDOM() % 2 = 0",
     "SELECT COUNT(*) FROM orders WHERE RANDOM() % 2 = 0"),
    ("random_in_list", "sqlite", "SELECT COUNT(*) FROM orders WHERE 0 IN (abs(RANDOM()) % 3, abs(RANDOM()) % 3)",
     "SELECT COUNT(*) FROM orders WHERE 0 IN (abs(RANDOM()) % 3)"),
    ("random_or", "sqlite", "SELECT COUNT(*) FROM orders WHERE RANDOM() > 0 OR RANDOM() > 0",
     "SELECT COUNT(*) FROM orders WHERE RANDOM() > 0"),
    ("random_order_by_twice", "sqlite", "SELECT id FROM orders ORDER BY RANDOM(), RANDOM() LIMIT 3",
     "SELECT id FROM orders ORDER BY RANDOM() LIMIT 3"),
    ("rand_twice", "mysql", "SELECT COUNT(*) FROM orders WHERE RAND() < 0.5 AND RAND() < 0.5",
     "SELECT COUNT(*) FROM orders WHERE RAND() < 0.5"),
    # 解析时换成同一个节点、方言里却不是一回事的写法
    ("log10_vs_log_base", "postgres", "SELECT LOG10(amount) FROM orders", "SELECT LOG(10, amount) FROM orders"),
    ("char_length_is_not_sqlite", "sqlite", "SELECT CHAR_LENGTH(name) FROM users", "SELECT LENGTH(name) FROM users"),
    # PostgreSQL 的 LIMIT NULL 是不限行数，SQLite 里 LIMIT NULL 报错；MySQL 里 LIMIT 不能写算式
    ("limit_null_is_an_error", "sqlite", "SELECT id FROM orders ORDER BY id LIMIT NULL", "SELECT id FROM orders ORDER BY id"),
    ("limit_arithmetic_is_an_error", "mysql", "SELECT id FROM orders ORDER BY id LIMIT 1 + 2",
     "SELECT id FROM orders ORDER BY id LIMIT 3"),
    ("limit_negative_is_an_error", "postgres", "SELECT id FROM orders ORDER BY id LIMIT -1", "SELECT id FROM orders ORDER BY id"),
]

#: 第 3 轮：意思一样（PostgreSQL / MySQL 的写法），认得出最好；认不出时只许给兜底说明，不说具体的不同
DIALECT_SAME_R3: list[tuple[str, str, str, str]] = [
    ("group_key_equal_by_join", "postgres",
     "SELECT s.id, COUNT(*) AS n FROM stores s JOIN orders o ON o.store_id = s.id GROUP BY s.id",
     "SELECT o.store_id, COUNT(*) AS n FROM orders o JOIN stores s ON s.id = o.store_id GROUP BY o.store_id"),
    ("group_key_equal_by_join", "mysql",
     "SELECT u.id, COUNT(*) FROM orders o JOIN users u ON u.id = o.user_id GROUP BY u.id",
     "SELECT u.id, COUNT(*) FROM orders o JOIN users u ON u.id = o.user_id GROUP BY o.user_id"),
    ("order_key_equal_by_join_limit", "postgres",
     "SELECT o.id FROM orders o JOIN users u ON u.id = o.user_id ORDER BY o.user_id, o.id LIMIT 5",
     "SELECT o.id FROM orders o JOIN users u ON u.id = o.user_id ORDER BY u.id, o.id LIMIT 5"),
    ("left_round_sum", "postgres",
     "SELECT ROUND(SUM(r.amount)) FROM orders o LEFT JOIN refunds r ON r.order_id = o.id",
     "SELECT ROUND(SUM(r.amount)) FROM orders o INNER JOIN refunds r ON r.order_id = o.id"),
    ("left_constant_column", "mysql",
     "SELECT 'all', SUM(r.amount) FROM orders o LEFT JOIN refunds r ON r.order_id = o.id",
     "SELECT 'all', SUM(r.amount) FROM orders o JOIN refunds r ON r.order_id = o.id"),
    ("left_extra_max", "postgres", "SELECT MAX(o.amount) FROM orders o LEFT JOIN refunds r ON r.order_id = o.id",
     "SELECT MAX(o.amount) FROM orders o"),
    ("left_extra_count_distinct", "mysql",
     "SELECT COUNT(DISTINCT o.user_id) FROM orders o LEFT JOIN refunds r ON r.order_id = o.id WHERE o.is_deleted = 0",
     "SELECT COUNT(DISTINCT o.user_id) FROM orders o WHERE o.is_deleted = 0"),
    ("left_on_redundant_not_null", "postgres",
     "SELECT o.id, r.amount FROM orders o LEFT JOIN refunds r ON r.order_id = o.id AND r.user_id = o.user_id "
     "AND r.user_id IS NOT NULL",
     "SELECT o.id, r.amount FROM orders o LEFT JOIN refunds r ON r.order_id = o.id AND r.user_id = o.user_id"),
    ("left_on_redundant_preserved_not_null", "mysql",
     "SELECT o.id, r.amount FROM orders o LEFT JOIN refunds r ON r.order_id = o.id AND o.id IS NOT NULL",
     "SELECT o.id, r.amount FROM orders o LEFT JOIN refunds r ON r.order_id = o.id"),
    ("in_union_vs_union_all", "postgres",
     "SELECT COUNT(*) FROM users WHERE id IN (SELECT user_id FROM orders UNION SELECT user_id FROM refunds)",
     "SELECT COUNT(*) FROM users WHERE id IN (SELECT user_id FROM orders UNION ALL SELECT user_id FROM refunds)"),
    ("in_group_by", "postgres",
     "SELECT COUNT(*) FROM users WHERE id IN (SELECT user_id FROM refunds WHERE state = 'done' GROUP BY user_id)",
     "SELECT COUNT(*) FROM users WHERE id IN (SELECT user_id FROM refunds WHERE state = 'done')"),
    ("in_group_by", "mysql",
     "SELECT COUNT(*) FROM users WHERE id IN (SELECT user_id FROM refunds GROUP BY user_id)",
     "SELECT COUNT(*) FROM users WHERE id IN (SELECT user_id FROM refunds)"),
    ("exists_distinct", "postgres",
     "SELECT o.id FROM orders o WHERE EXISTS (SELECT DISTINCT r.amount FROM refunds r WHERE r.order_id = o.id)",
     "SELECT o.id FROM orders o WHERE EXISTS (SELECT r.amount FROM refunds r WHERE r.order_id = o.id)"),
    ("limit_null_is_no_limit", "postgres", "SELECT id FROM orders ORDER BY id LIMIT NULL", "SELECT id FROM orders ORDER BY id"),
    ("limit_all_is_no_limit", "postgres", "SELECT id FROM orders ORDER BY id LIMIT ALL", "SELECT id FROM orders ORDER BY id"),
    ("limit_arithmetic", "postgres", "SELECT id FROM orders ORDER BY id LIMIT 1 + 2", "SELECT id FROM orders ORDER BY id LIMIT 3"),
    ("mod_function_is_percent", "postgres", "SELECT COUNT(*) FROM orders WHERE MOD(store_id, 2) = 0",
     "SELECT COUNT(*) FROM orders WHERE store_id % 2 = 0"),
    ("mod_function_is_percent", "mysql", "SELECT COUNT(*) FROM orders WHERE MOD(store_id, 2) = 0",
     "SELECT COUNT(*) FROM orders WHERE store_id % 2 = 0"),
    ("ifnull_is_coalesce", "mysql", "SELECT SUM(IFNULL(discount, 0)) FROM orders",
     "SELECT SUM(COALESCE(discount, 0)) FROM orders"),
    ("self_join_roles_swapped_names", "mysql", "SELECT a.id, b.amount FROM orders a JOIN orders b ON a.parent_id = b.id",
     "SELECT b.id, a.amount FROM orders b JOIN orders a ON b.parent_id = a.id"),
]


PAIRS: list[Pair] = [Pair(f"same:{i}", a, b, "same") for i, a, b in _SAME] + \
    [Pair(f"diff:{i}", a, b, "diff") for i, a, b in _DIFF] + \
    [Pair(f"validator:{n}:{k}", a, b, "diff") for k, (n, a, b) in enumerate(VALIDATOR_DIFF)] + \
    [Pair(f"same:{i}", a, b, "same") for i, a, b in _SAME_R2] + \
    [Pair(f"hard:{i}", a, b, "same") for i, a, b in _SAME_HARD] + \
    [Pair(f"diff:{i}", a, b, "diff") for i, a, b in _DIFF_R2] + \
    [Pair(f"diff:{i}", a, b, "diff") for i, a, b in _DIFF_R3] + \
    [Pair(f"same:{i}", a, b, "same") for i, a, b in _SAME_R3] + \
    [Pair(f"same:{i}", a, b, "same", typed=True) for i, a, b in _SAME_TYPED_R3] + \
    [Pair(f"same:{i}", a, b, "same", needs_schema=True) for i, a, b in _SAME_SCHEMA_R3] + \
    [Pair(f"hard:{i}", a, b, "same") for i, a, b in _SAME_HARD_R3]

#: 按编号找语料里的一对（测试里引用第 2 轮的反例用）
PAIRS_BY_ID: dict[str, Pair] = {p.id: p for p in PAIRS}
