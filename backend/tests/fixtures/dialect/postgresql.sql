-- 剖析与取值观察的 SQL 金样：postgresql（tests/test_profile_dialect_golden.py 生成，改动要人审）
-- stats
SELECT c.reltuples FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'sales' AND c.relname = 'orders';
-- stats_quoted
SELECT c.reltuples FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = current_schema() AND c.relname = 'visits';
-- bounded
SELECT COUNT(*) AS n FROM (SELECT 1 AS one FROM sales.orders LIMIT 100001) s;
-- distinct
SELECT DISTINCT store_code FROM sales.orders WHERE store_code IS NOT NULL LIMIT 2000;
-- distinct_capped
SELECT DISTINCT store_code FROM (SELECT store_code FROM sales.orders WHERE store_code IS NOT NULL LIMIT 100000) s LIMIT 2000;
-- match
SELECT COUNT(DISTINCT code) AS n FROM stores WHERE code IN ('12', '7');
-- unique
SELECT COUNT("level") AS n, COUNT(DISTINCT "level") AS d FROM "visits";
-- value_counts
SELECT status, COUNT(*) AS cnt FROM sales.orders WHERE status IS NOT NULL GROUP BY status ORDER BY cnt DESC, status LIMIT 21;
-- value_counts_capped
SELECT status, COUNT(*) AS cnt FROM (SELECT status FROM sales.orders WHERE status IS NOT NULL LIMIT 100000) s WHERE status IS NOT NULL GROUP BY status ORDER BY cnt DESC, status LIMIT 21;
-- min_max
SELECT MIN(CASE WHEN paid_at >= DATE '0001-01-02' AND paid_at < DATE '9999-12-31' THEN paid_at END), MAX(CASE WHEN paid_at >= DATE '0001-01-02' AND paid_at < DATE '9999-12-31' THEN paid_at END), SUM(CASE WHEN paid_at < DATE '0001-01-02' OR paid_at >= DATE '9999-12-31' THEN 1 ELSE 0 END), MIN(CASE WHEN biz_day >= '0001-01-02' AND biz_day < '9999-12-31' THEN biz_day END), MAX(CASE WHEN biz_day >= '0001-01-02' AND biz_day < '9999-12-31' THEN biz_day END), SUM(CASE WHEN biz_day < '0001-01-02' OR biz_day >= '9999-12-31' THEN 1 ELSE 0 END) FROM sales.orders;
-- date_window
SELECT status, COUNT(*) AS cnt FROM sales.orders WHERE paid_at >= DATE '2026-08-27' AND paid_at < DATE '9999-12-31' AND status IS NOT NULL GROUP BY status ORDER BY cnt DESC, status LIMIT 21;
-- day_volume
SELECT CAST(paid_at AS DATE) AS d, COUNT(*) AS n FROM sales.orders WHERE paid_at >= DATE '2026-08-27' AND paid_at < DATE '9999-12-31' GROUP BY CAST(paid_at AS DATE) ORDER BY d;
-- sample
SELECT status, COUNT(*) AS cnt FROM sales.orders TABLESAMPLE SYSTEM (1) WHERE status IS NOT NULL GROUP BY status ORDER BY cnt DESC, status LIMIT 21;
