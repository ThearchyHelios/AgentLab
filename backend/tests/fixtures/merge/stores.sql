-- 门店库（虚构）：门店和销售订单。合并查询的端到端测试按「日期 + 门店」聚合订单数和销售额。
-- 门店编号是文本（'S01'），金额是实数。数据只为测试编造，和任何真实门店无关。
CREATE TABLE stores (
    store_id TEXT PRIMARY KEY,
    name     TEXT NOT NULL
);
CREATE TABLE orders (
    order_id   INTEGER PRIMARY KEY,
    store_id   TEXT NOT NULL REFERENCES stores (store_id),
    order_date TEXT NOT NULL,
    amount     REAL NOT NULL
);
INSERT INTO stores VALUES ('S01', '湖滨店'), ('S02', '东站店');
INSERT INTO orders VALUES
    (1, 'S01', '2026-05-01', 120.0),
    (2, 'S01', '2026-05-01', 80.0),
    (3, 'S01', '2026-05-01', 100.0),
    (4, 'S02', '2026-05-01', 60.0),
    (5, 'S02', '2026-05-01', 90.0),
    (6, 'S01', '2026-05-02', 200.0),
    (7, 'S01', '2026-05-02', 50.0),
    (8, 'S02', '2026-05-02', 75.0);
