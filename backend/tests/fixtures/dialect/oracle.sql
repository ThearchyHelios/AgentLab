-- 剖析与取值观察的 SQL 金样：oracle（tests/test_profile_dialect_golden.py 生成，改动要人审）
-- stats
SELECT NUM_ROWS FROM ALL_TABLES WHERE OWNER = 'SALES' AND TABLE_NAME = 'ORDERS';
-- stats_quoted
SELECT NUM_ROWS FROM ALL_TABLES WHERE OWNER = SYS_CONTEXT('USERENV', 'CURRENT_SCHEMA') AND TABLE_NAME = 'visits';
-- bounded
SELECT COUNT(*) AS n FROM (SELECT * FROM (SELECT 1 AS one FROM sales.orders) WHERE ROWNUM <= 100001) s;
-- distinct
SELECT * FROM (SELECT DISTINCT store_code FROM sales.orders WHERE store_code IS NOT NULL) WHERE ROWNUM <= 2000;
-- distinct_capped
SELECT * FROM (SELECT DISTINCT store_code FROM (SELECT * FROM (SELECT store_code FROM sales.orders WHERE store_code IS NOT NULL) WHERE ROWNUM <= 100000) s) WHERE ROWNUM <= 2000;
-- match
SELECT COUNT(DISTINCT code) AS n FROM stores WHERE code IN ('12', '7');
-- unique
SELECT COUNT("level") AS n, COUNT(DISTINCT "level") AS d FROM "visits";
-- value_counts
SELECT * FROM (SELECT status, COUNT(*) AS cnt FROM sales.orders WHERE status IS NOT NULL GROUP BY status ORDER BY cnt DESC, status) WHERE ROWNUM <= 21;
-- value_counts_capped
SELECT * FROM (SELECT status, COUNT(*) AS cnt FROM (SELECT * FROM (SELECT status FROM sales.orders WHERE status IS NOT NULL) WHERE ROWNUM <= 100000) s WHERE status IS NOT NULL GROUP BY status ORDER BY cnt DESC, status) WHERE ROWNUM <= 21;
-- min_max
SELECT MIN(CASE WHEN paid_at >= DATE '0001-01-02' AND paid_at < DATE '9999-12-31' THEN paid_at END), MAX(CASE WHEN paid_at >= DATE '0001-01-02' AND paid_at < DATE '9999-12-31' THEN paid_at END), SUM(CASE WHEN paid_at < DATE '0001-01-02' OR paid_at >= DATE '9999-12-31' THEN 1 ELSE 0 END), MIN(CASE WHEN biz_day >= '0001-01-02' AND biz_day < '9999-12-31' THEN biz_day END), MAX(CASE WHEN biz_day >= '0001-01-02' AND biz_day < '9999-12-31' THEN biz_day END), SUM(CASE WHEN biz_day < '0001-01-02' OR biz_day >= '9999-12-31' THEN 1 ELSE 0 END) FROM sales.orders;
-- date_window
SELECT * FROM (SELECT status, COUNT(*) AS cnt FROM sales.orders WHERE paid_at >= DATE '2026-08-27' AND paid_at < DATE '9999-12-31' AND status IS NOT NULL GROUP BY status ORDER BY cnt DESC, status) WHERE ROWNUM <= 21;
-- day_volume
SELECT TRUNC(paid_at) AS d, COUNT(*) AS n FROM sales.orders WHERE paid_at >= DATE '2026-08-27' AND paid_at < DATE '2026-10-02' GROUP BY TRUNC(paid_at) ORDER BY d;
-- sample
SELECT * FROM (SELECT status, COUNT(*) AS cnt FROM sales.orders SAMPLE BLOCK (1) WHERE status IS NOT NULL GROUP BY status ORDER BY cnt DESC, status) WHERE ROWNUM <= 21;
-- max_date
SELECT MAX(paid_at) FROM sales.orders WHERE paid_at >= DATE '0001-01-02' AND paid_at < DATE '9999-12-31';
-- max_date_text
SELECT MAX(biz_day) FROM sales.orders WHERE biz_day >= '0001-01-02' AND biz_day < '9999-12-31';
