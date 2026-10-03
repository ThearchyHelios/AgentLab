"""虚构的景区业务库：入园记录、渠道、票种、订单、园内门店、会员、营销、停车、旅行团……共 50 张表和 1 个视图。

为数据目录设计的几处特点：

- **真外键和只能靠命名推断的关系混在一起。** 核心交易表（订单、订单明细、入园记录的票种）声明了外键；
  很多「看名字就知道连哪」的列（visits.gate_id、store_sales.productId、inventory_snapshots.storeID）没有约束，
  只能按列名推断。三种命名都有：xxx_id、xxxId、xxxID；表名有复数、驼峰（memberTags）、y→ies（categories、agencies）。
- **关联表。** channel_visits 把渠道和入园记录连起来（复合主键，两端都是外键）：入园记录本身不带渠道，
  从 visits 到 channels 要经过它，正好是两跳的连接路径。group_visits 同理（旅行团与入园记录）。
- **命名推断必须放过的列**（NOT_INFERRED）：自引用（employees.employee_id 是工号，不是指向自己）、
  多个候选（guide 和 guides 两张表都在，tour_assignments.guide_id 不知道指哪张）、没有对应的表
  （operation_logs.operator_id、categories.parentId）、类型对不上（feedback_forms.store_id 是文字，stores.id 是整数）。
  parking_records.lot_id 没有直接对上的 lot / lots，但和引用它的表共用 parking 前缀的 parking_lots 只有一张，
  推得出来（在 NAME_RELATIONS 里）。
- **唯一约束 / 唯一索引。** orders.order_no、channels.channel_code 是列上的 UNIQUE；members.member_no、
  visits.ticket_no 是单独建的唯一索引。

数据是固定种子生成的假数，量很小（入园记录 1500 行），只为让查询有结果；名称一律虚构。
"""
from __future__ import annotations

import random
import sqlite3
from pathlib import Path

DDL = """
CREATE TABLE parks (id INTEGER PRIMARY KEY, park_code TEXT NOT NULL UNIQUE, name TEXT NOT NULL, city TEXT,
                    opened_on TEXT);
CREATE TABLE areas (id INTEGER PRIMARY KEY, park_id INTEGER NOT NULL, name TEXT NOT NULL);
CREATE TABLE gates (id INTEGER PRIMARY KEY, park_id INTEGER NOT NULL REFERENCES parks(id), gate_code TEXT NOT NULL,
                    name TEXT);
CREATE TABLE channels (id INTEGER PRIMARY KEY, channel_code TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
                       channel_type TEXT NOT NULL);
CREATE TABLE ticket_types (id INTEGER PRIMARY KEY, park_id INTEGER NOT NULL REFERENCES parks(id),
                           type_code TEXT NOT NULL, name TEXT NOT NULL, base_price REAL NOT NULL);
CREATE TABLE member_levels (id INTEGER PRIMARY KEY, level_name TEXT NOT NULL, discount_rate REAL);
CREATE TABLE members (id INTEGER PRIMARY KEY, member_no TEXT NOT NULL, name TEXT, member_level_id INTEGER,
                      joined_on TEXT, status INTEGER NOT NULL DEFAULT 1);
CREATE UNIQUE INDEX ux_members_member_no ON members(member_no);
CREATE TABLE "memberTags" (id INTEGER PRIMARY KEY, tag_name TEXT NOT NULL);
CREATE TABLE member_tag_links (id INTEGER PRIMARY KEY, member_id INTEGER NOT NULL REFERENCES members(id),
                               "memberTagId" INTEGER NOT NULL);
CREATE TABLE orders (id INTEGER PRIMARY KEY, order_no TEXT NOT NULL UNIQUE, member_id INTEGER REFERENCES members(id),
                     channel_id INTEGER NOT NULL REFERENCES channels(id), ordered_at TEXT NOT NULL,
                     total_amount REAL NOT NULL, status INTEGER NOT NULL);
CREATE TABLE order_items (id INTEGER PRIMARY KEY, order_id INTEGER NOT NULL REFERENCES orders(id),
                          ticket_type_id INTEGER NOT NULL REFERENCES ticket_types(id), qty INTEGER NOT NULL,
                          unit_price REAL NOT NULL, amount REAL NOT NULL);
CREATE TABLE visits (id INTEGER PRIMARY KEY, ticket_no TEXT NOT NULL, park_id INTEGER NOT NULL REFERENCES parks(id),
                     gate_id INTEGER, ticket_type_id INTEGER NOT NULL REFERENCES ticket_types(id), member_id INTEGER,
                     visit_time TEXT NOT NULL, visitor_count INTEGER NOT NULL DEFAULT 1, status INTEGER NOT NULL);
CREATE UNIQUE INDEX ux_visits_ticket_no ON visits(ticket_no);
CREATE TABLE channel_visits (channel_id INTEGER NOT NULL REFERENCES channels(id),
                             visit_id INTEGER NOT NULL REFERENCES visits(id),
                             attributed_share REAL NOT NULL DEFAULT 1, PRIMARY KEY (channel_id, visit_id));
CREATE TABLE stores (id INTEGER PRIMARY KEY, store_code TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
                     park_id INTEGER NOT NULL REFERENCES parks(id), area_id INTEGER, opened_on TEXT);
CREATE TABLE categories (id INTEGER PRIMARY KEY, name TEXT NOT NULL, "parentId" INTEGER);
CREATE TABLE products (id INTEGER PRIMARY KEY, sku TEXT NOT NULL UNIQUE, name TEXT NOT NULL, category_id INTEGER,
                       list_price REAL);
CREATE TABLE store_sales (id INTEGER PRIMARY KEY, store_id INTEGER NOT NULL REFERENCES stores(id),
                          "productId" INTEGER NOT NULL, member_id INTEGER, sold_at TEXT NOT NULL, qty INTEGER NOT NULL,
                          amount REAL NOT NULL);
CREATE TABLE inventory_snapshots (id INTEGER PRIMARY KEY, "storeID" INTEGER NOT NULL, "productId" INTEGER NOT NULL,
                                  snapshot_date TEXT NOT NULL, on_hand INTEGER NOT NULL);
CREATE TABLE campaigns (id INTEGER PRIMARY KEY, channel_id INTEGER REFERENCES channels(id), name TEXT NOT NULL,
                        starts_on TEXT, ends_on TEXT);
CREATE TABLE coupons (id INTEGER PRIMARY KEY, code TEXT NOT NULL UNIQUE, campaign_id INTEGER,
                      discount_amount REAL NOT NULL);
CREATE TABLE coupon_redemptions (id INTEGER PRIMARY KEY, coupon_id INTEGER NOT NULL REFERENCES coupons(id),
                                 order_id INTEGER NOT NULL REFERENCES orders(id), redeemed_at TEXT NOT NULL);
CREATE TABLE payments (id INTEGER PRIMARY KEY, order_id INTEGER NOT NULL REFERENCES orders(id),
                       pay_method TEXT NOT NULL, paid_amount REAL NOT NULL, paid_at TEXT NOT NULL);
CREATE TABLE refunds (id INTEGER PRIMARY KEY, order_id INTEGER NOT NULL REFERENCES orders(id),
                      refund_amount REAL NOT NULL, reason_code TEXT, refunded_at TEXT NOT NULL);
CREATE TABLE reviews (id INTEGER PRIMARY KEY, visit_id INTEGER NOT NULL, score INTEGER NOT NULL, review_text TEXT,
                      created_at TEXT NOT NULL);
CREATE TABLE complaints (id INTEGER PRIMARY KEY, visit_id INTEGER, category_code TEXT NOT NULL,
                         status INTEGER NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE events (id INTEGER PRIMARY KEY, park_id INTEGER NOT NULL REFERENCES parks(id), name TEXT NOT NULL,
                     event_date TEXT NOT NULL);
CREATE TABLE event_bookings (id INTEGER PRIMARY KEY, event_id INTEGER NOT NULL, member_id INTEGER NOT NULL,
                             seats INTEGER NOT NULL, booked_at TEXT NOT NULL);
CREATE TABLE parking_lots (id INTEGER PRIMARY KEY, park_id INTEGER NOT NULL REFERENCES parks(id),
                           name TEXT NOT NULL, capacity INTEGER NOT NULL);
CREATE TABLE parking_records (id INTEGER PRIMARY KEY, lot_id INTEGER NOT NULL, plate_masked TEXT,
                              entered_at TEXT NOT NULL, exited_at TEXT, fee REAL);
CREATE TABLE weather_daily (park_id INTEGER NOT NULL, biz_date TEXT NOT NULL, weather TEXT, temp_high REAL,
                            temp_low REAL, PRIMARY KEY (park_id, biz_date));
CREATE TABLE holidays (biz_date TEXT PRIMARY KEY, holiday_name TEXT NOT NULL, is_holiday INTEGER NOT NULL);
CREATE TABLE dim_dates (date_key INTEGER PRIMARY KEY, biz_date TEXT NOT NULL UNIQUE, weekday INTEGER NOT NULL,
                        is_weekend INTEGER NOT NULL);
CREATE TABLE price_rules (id INTEGER PRIMARY KEY, ticket_type_id INTEGER NOT NULL REFERENCES ticket_types(id),
                          valid_from TEXT NOT NULL, valid_to TEXT, price REAL NOT NULL);
CREATE TABLE ticket_inventory (id INTEGER PRIMARY KEY, ticket_type_id INTEGER NOT NULL, biz_date TEXT NOT NULL,
                               quota INTEGER NOT NULL, sold INTEGER NOT NULL);
CREATE TABLE member_points_log (id INTEGER PRIMARY KEY, member_id INTEGER NOT NULL REFERENCES members(id),
                                change_points INTEGER NOT NULL, reason TEXT, created_at TEXT NOT NULL);
CREATE TABLE sms_logs (id INTEGER PRIMARY KEY, member_id INTEGER, template_code TEXT NOT NULL, sent_at TEXT NOT NULL);
CREATE TABLE suppliers (id INTEGER PRIMARY KEY, name TEXT NOT NULL);
CREATE TABLE purchase_orders (id INTEGER PRIMARY KEY, supplier_id INTEGER NOT NULL REFERENCES suppliers(id),
                              store_id INTEGER NOT NULL REFERENCES stores(id), ordered_at TEXT NOT NULL,
                              amount REAL NOT NULL);
CREATE TABLE purchase_order_lines (id INTEGER PRIMARY KEY, purchase_order_id INTEGER NOT NULL,
                                   product_id INTEGER NOT NULL REFERENCES products(id), qty INTEGER NOT NULL,
                                   cost REAL NOT NULL);
CREATE TABLE employees (id INTEGER PRIMARY KEY, employee_id TEXT NOT NULL, store_id INTEGER, name TEXT NOT NULL,
                        role TEXT);
CREATE TABLE agencies (id INTEGER PRIMARY KEY, channel_id INTEGER NOT NULL REFERENCES channels(id),
                       name TEXT NOT NULL);
CREATE TABLE tour_groups (id INTEGER PRIMARY KEY, agency_id INTEGER NOT NULL, group_size INTEGER NOT NULL,
                          visit_date TEXT NOT NULL);
CREATE TABLE group_visits (tour_group_id INTEGER NOT NULL REFERENCES tour_groups(id),
                           visit_id INTEGER NOT NULL REFERENCES visits(id), PRIMARY KEY (tour_group_id, visit_id));
CREATE TABLE guide (id INTEGER PRIMARY KEY, name TEXT);
CREATE TABLE guides (id INTEGER PRIMARY KEY, name TEXT NOT NULL, licensed INTEGER);
CREATE TABLE tour_assignments (id INTEGER PRIMARY KEY, tour_group_id INTEGER NOT NULL, guide_id INTEGER NOT NULL,
                               assigned_on TEXT NOT NULL);
CREATE TABLE feedback_forms (id INTEGER PRIMARY KEY, store_id TEXT, submitted_at TEXT NOT NULL, rating INTEGER);
CREATE TABLE lost_and_found (id INTEGER PRIMARY KEY, park_id INTEGER NOT NULL, item_desc TEXT NOT NULL,
                             found_at TEXT NOT NULL);
CREATE TABLE operation_logs (id INTEGER PRIMARY KEY, operator_id INTEGER, action TEXT NOT NULL,
                             created_at TEXT NOT NULL);
CREATE TABLE sys_config (config_key TEXT PRIMARY KEY, config_value TEXT);
CREATE VIEW v_daily_visits AS
    SELECT park_id, substr(visit_time, 1, 10) AS biz_date, SUM(visitor_count) AS visitors
    FROM visits WHERE status = 1 GROUP BY park_id, substr(visit_time, 1, 10);
"""

#: 表的个数（不含视图）
TABLE_COUNT = 50
VIEWS = ("v_daily_visits",)

#: 声明了外键约束的关系：(本表, (本表列,), 被指向的表, (被指向的列,))
FK_RELATIONS = frozenset({
    ("gates", ("park_id",), "parks", ("id",)),
    ("ticket_types", ("park_id",), "parks", ("id",)),
    ("member_tag_links", ("member_id",), "members", ("id",)),
    ("orders", ("member_id",), "members", ("id",)),
    ("orders", ("channel_id",), "channels", ("id",)),
    ("order_items", ("order_id",), "orders", ("id",)),
    ("order_items", ("ticket_type_id",), "ticket_types", ("id",)),
    ("visits", ("park_id",), "parks", ("id",)),
    ("visits", ("ticket_type_id",), "ticket_types", ("id",)),
    ("channel_visits", ("channel_id",), "channels", ("id",)),
    ("channel_visits", ("visit_id",), "visits", ("id",)),
    ("stores", ("park_id",), "parks", ("id",)),
    ("store_sales", ("store_id",), "stores", ("id",)),
    ("campaigns", ("channel_id",), "channels", ("id",)),
    ("coupon_redemptions", ("coupon_id",), "coupons", ("id",)),
    ("coupon_redemptions", ("order_id",), "orders", ("id",)),
    ("payments", ("order_id",), "orders", ("id",)),
    ("refunds", ("order_id",), "orders", ("id",)),
    ("events", ("park_id",), "parks", ("id",)),
    ("parking_lots", ("park_id",), "parks", ("id",)),
    ("price_rules", ("ticket_type_id",), "ticket_types", ("id",)),
    ("member_points_log", ("member_id",), "members", ("id",)),
    ("purchase_orders", ("supplier_id",), "suppliers", ("id",)),
    ("purchase_orders", ("store_id",), "stores", ("id",)),
    ("purchase_order_lines", ("product_id",), "products", ("id",)),
    ("agencies", ("channel_id",), "channels", ("id",)),
    ("group_visits", ("tour_group_id",), "tour_groups", ("id",)),
    ("group_visits", ("visit_id",), "visits", ("id",)),
})

#: 没有外键约束、命名推断应当找出来的关系（被指向一端都是单列主键 id）
NAME_RELATIONS = frozenset({
    ("areas", ("park_id",), "parks", ("id",)),
    ("members", ("member_level_id",), "member_levels", ("id",)),
    ("member_tag_links", ("memberTagId",), "memberTags", ("id",)),
    ("visits", ("gate_id",), "gates", ("id",)),
    ("visits", ("member_id",), "members", ("id",)),
    ("stores", ("area_id",), "areas", ("id",)),
    ("products", ("category_id",), "categories", ("id",)),
    ("store_sales", ("productId",), "products", ("id",)),
    ("store_sales", ("member_id",), "members", ("id",)),
    ("inventory_snapshots", ("storeID",), "stores", ("id",)),
    ("inventory_snapshots", ("productId",), "products", ("id",)),
    ("coupons", ("campaign_id",), "campaigns", ("id",)),
    ("reviews", ("visit_id",), "visits", ("id",)),
    ("complaints", ("visit_id",), "visits", ("id",)),
    ("event_bookings", ("event_id",), "events", ("id",)),
    ("event_bookings", ("member_id",), "members", ("id",)),
    ("weather_daily", ("park_id",), "parks", ("id",)),
    ("ticket_inventory", ("ticket_type_id",), "ticket_types", ("id",)),
    ("sms_logs", ("member_id",), "members", ("id",)),
    ("purchase_order_lines", ("purchase_order_id",), "purchase_orders", ("id",)),
    ("employees", ("store_id",), "stores", ("id",)),
    ("tour_groups", ("agency_id",), "agencies", ("id",)),
    ("tour_assignments", ("tour_group_id",), "tour_groups", ("id",)),
    ("lost_and_found", ("park_id",), "parks", ("id",)),
    # 没有 lot / lots，按引用表共用的 parking 前缀对上 parking_lots
    ("parking_records", ("lot_id",), "parking_lots", ("id",)),
    # 视图没有约束，按列名同样推得出来：按日汇总的入园人数照样能按 park_id 连到景区
    ("v_daily_visits", ("park_id",), "parks", ("id",)),
})

#: 看起来像关联、命名推断必须放过的列：(表, 列) → 原因
NOT_INFERRED = {
    ("employees", "employee_id"): "自引用：工号，不是指向本表",
    ("tour_assignments", "guide_id"): "多个候选：guide 与 guides",
    ("operation_logs", "operator_id"): "没有名为 operator / operators 的表",
    ("categories", "parentId"): "没有名为 parent / parents 的表",
    ("feedback_forms", "store_id"): "类型对不上：文字列指向整数主键",
}

#: 唯一约束 / 唯一索引：表 → 列组
UNIQUE = {
    "parks": [["park_code"]], "channels": [["channel_code"]], "orders": [["order_no"]],
    "members": [["member_no"]], "visits": [["ticket_no"]], "stores": [["store_code"]],
    "products": [["sku"]], "coupons": [["code"]], "dim_dates": [["biz_date"]],
}

_PARKS = [(1, "P01", "青松谷景区", "甲市", "2015-05-01"), (2, "P02", "白鹭湾景区", "乙市", "2018-04-20"),
          (3, "P03", "星河乐园", "甲市", "2021-10-01")]
_CHANNELS = [(1, "WEB", "官网", "线上直销"), (2, "MINI", "小程序", "线上直销"), (3, "BOX", "窗口售票", "线下"),
             (4, "AGT", "旅行社分销", "分销"), (5, "OTA1", "第三方平台甲", "分销"), (6, "OTA2", "第三方平台乙", "分销")]
_LEVELS = [(1, "普通会员", 1.0), (2, "银卡会员", 0.95), (3, "金卡会员", 0.9), (4, "年卡会员", 0.0)]


def _day(rng: random.Random) -> str:
    month = rng.choice((7, 8, 9))
    return f"2026-{month:02d}-{rng.randint(1, 30 if month == 9 else 31):02d}"


def build(path: str | Path, *, seed: int = 20261003) -> Path:
    """在 path 写一个新的景区业务库并返回路径。path 已存在时先删掉：同样的种子造出同样的内容。"""
    path = Path(path)
    if path.exists():
        path.unlink()
    rng = random.Random(seed)
    db = sqlite3.connect(path)
    try:
        db.executescript(DDL)
        db.executemany("INSERT INTO parks VALUES (?,?,?,?,?)", _PARKS)
        db.executemany("INSERT INTO areas VALUES (?,?,?)",
                       [(i, 1 + (i - 1) // 2, f"{'东西南北中'[i % 5]}片区") for i in range(1, 7)])
        db.executemany("INSERT INTO gates VALUES (?,?,?,?)",
                       [(i, 1 + (i - 1) % 3, f"G{i:02d}", f"{i} 号闸口") for i in range(1, 9)])
        db.executemany("INSERT INTO channels VALUES (?,?,?,?)", _CHANNELS)
        types = [(i, 1 + (i - 1) // 3, f"T{i:02d}", name, price)
                 for i, (name, price) in enumerate([("成人票", 120.0), ("儿童票", 60.0), ("夜场票", 80.0)] * 3, 1)]
        db.executemany("INSERT INTO ticket_types VALUES (?,?,?,?,?)", types)
        db.executemany("INSERT INTO member_levels VALUES (?,?,?)", _LEVELS)
        db.executemany("INSERT INTO members VALUES (?,?,?,?,?,?)",
                       [(i, f"M{i:05d}", f"会员{i}", rng.randint(1, 4), _day(rng), 1 if rng.random() > 0.05 else 0)
                        for i in range(1, 201)])
        db.executemany('INSERT INTO "memberTags" VALUES (?,?)',
                       [(i, t) for i, t in enumerate(["亲子", "夜游", "摄影", "年卡", "团队"], 1)])
        db.executemany("INSERT INTO member_tag_links VALUES (?,?,?)",
                       [(i, rng.randint(1, 200), rng.randint(1, 5)) for i in range(1, 301)])
        orders, items = [], []
        for i in range(1, 601):
            qty = rng.randint(1, 4)
            tid = rng.randint(1, 9)
            amount = qty * types[tid - 1][4]
            orders.append((i, f"OD{i:06d}", rng.choice([None, rng.randint(1, 200)]), rng.randint(1, 6),
                           f"{_day(rng)} {rng.randint(8, 20):02d}:{rng.randint(0, 59):02d}:00", amount,
                           1 if rng.random() > 0.08 else 2))
            items.append((i, i, tid, qty, types[tid - 1][4], amount))
        db.executemany("INSERT INTO orders VALUES (?,?,?,?,?,?,?)", orders)
        db.executemany("INSERT INTO order_items VALUES (?,?,?,?,?,?)", items)
        visits = [(i, f"TK{i:07d}", 1 + i % 3, rng.randint(1, 8), rng.randint(1, 9), rng.choice([None, rng.randint(1, 200)]),
                   f"{_day(rng)} {rng.randint(8, 21):02d}:{rng.randint(0, 59):02d}:00", rng.randint(1, 5),
                   1 if rng.random() > 0.04 else 0) for i in range(1, 1501)]
        db.executemany("INSERT INTO visits VALUES (?,?,?,?,?,?,?,?,?)", visits)
        db.executemany("INSERT INTO channel_visits VALUES (?,?,?)", [(rng.randint(1, 6), i, 1.0) for i in range(1, 1501)])
        db.executemany("INSERT INTO stores VALUES (?,?,?,?,?,?)",
                       [(i, f"S{i:03d}", f"{i} 号门店", 1 + i % 3, 1 + i % 6, "2020-01-01") for i in range(1, 11)])
        db.executemany("INSERT INTO categories VALUES (?,?,?)",
                       [(1, "餐饮", None), (2, "文创", None), (3, "饮品", 1), (4, "小吃", 1), (5, "纪念品", 2)])
        db.executemany("INSERT INTO products VALUES (?,?,?,?,?)",
                       [(i, f"SKU{i:04d}", f"商品{i}", rng.randint(1, 5), float(rng.randint(5, 200))) for i in range(1, 41)])
        db.executemany("INSERT INTO store_sales VALUES (?,?,?,?,?,?,?)",
                       [(i, rng.randint(1, 10), rng.randint(1, 40), rng.choice([None, rng.randint(1, 200)]),
                         f"{_day(rng)} 12:00:00", rng.randint(1, 3), float(rng.randint(5, 300))) for i in range(1, 801)])
        db.executemany("INSERT INTO dim_dates VALUES (?,?,?,?)",
                       [(20260700 + d, f"2026-07-{d:02d}", d % 7, int(d % 7 in (5, 6))) for d in range(1, 32)])
        db.executemany("INSERT INTO holidays VALUES (?,?,?)", [("2026-10-01", "国庆节", 1), ("2026-10-02", "国庆节", 1)])
        db.executemany("INSERT INTO sys_config VALUES (?,?)", [("currency", "CNY"), ("timezone", "+08:00")])
        db.executemany("INSERT INTO guide VALUES (?,?)", [(1, "旧导游档案")])
        db.executemany("INSERT INTO guides VALUES (?,?,?)", [(1, "导游甲", 1), (2, "导游乙", 1)])
        db.commit()
    finally:
        db.close()
    return path
