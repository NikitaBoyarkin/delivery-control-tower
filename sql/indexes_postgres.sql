-- ============================================================================
-- POSTGRES PRODUCTION RECIPE - NOT EXECUTED BY THE TEST SUITE.
--
-- DuckDB (data/marts/delivery.duckdb) has no user-managed indexes, so the SQL
-- layer measures EXPLAIN ANALYZE instead. This file ports fact_orders to
-- PostgreSQL and documents the indexes the access patterns need in production.
-- Nothing here is run by pytest, by delivery.sql_layer, or by the DuckDB ETL.
-- Run it manually against a scratch Postgres database after loading the rows.
--
-- Note: Postgres does not support COUNT(DISTINCT ...) as a window aggregate,
-- so the DuckDB rolling-active-base query cannot be ported verbatim. The
-- equivalent per-day correlated form is shown under query 1.
-- ============================================================================

DROP TABLE IF EXISTS fact_orders;

CREATE TABLE fact_orders (
    order_id     BIGINT           NOT NULL,
    date_key     INTEGER          NOT NULL,
    city_key     BIGINT           NOT NULL,
    client_key   BIGINT           NOT NULL,
    sales        DOUBLE PRECISION NOT NULL DEFAULT 0,
    is_zero_sale SMALLINT         NOT NULL DEFAULT 0,
    CONSTRAINT fact_orders_pkey PRIMARY KEY (order_id),
    CONSTRAINT fact_orders_zero_sale_chk CHECK (is_zero_sale IN (0, 1))
);

-- 1. Date-range scans: rolling windows, month filters, daily rollups.
CREATE INDEX idx_fact_orders_date_key ON fact_orders (date_key);

-- 2. Per-client history: last order, days between orders, lifetime aggregates.
--    (client_key, date_key) also serves ORDER BY client_key, date_key without a sort.
CREATE INDEX idx_fact_orders_client_date ON fact_orders (client_key, date_key);

-- 3. Partial index for zero-sale diagnostics. Only 619 of 130,079 rows match,
--    so the index stays small and makes zero-sale filters cheap.
CREATE INDEX idx_fact_orders_zero_sale ON fact_orders (date_key) WHERE is_zero_sale = 1;

ANALYZE fact_orders;

-- ----------------------------------------------------------------------------
-- Query 1: rolling 30-day active client base, one row per observed date.
-- Uses idx_fact_orders_date_key for the BETWEEN range scan. The date spine is
-- small (181 rows), so the correlated subquery runs 181 times, not per order.
-- ----------------------------------------------------------------------------
EXPLAIN (ANALYZE, BUFFERS)
WITH days AS (
    SELECT DISTINCT TO_DATE(date_key::text, 'YYYYMMDD') AS day
    FROM fact_orders
)
SELECT
    day,
    (
        SELECT COUNT(DISTINCT o.client_key)
        FROM fact_orders o
        WHERE o.date_key BETWEEN TO_CHAR(day - 29, 'YYYYMMDD')::integer
                             AND TO_CHAR(day, 'YYYYMMDD')::integer
    ) AS active_clients_30d
FROM days
ORDER BY day;

-- ----------------------------------------------------------------------------
-- Query 2: most recent order per client (DISTINCT ON).
-- Uses idx_fact_orders_client_date: rows arrive pre-sorted by client_key,
-- date_key, so Postgres avoids an explicit sort per client.
-- ----------------------------------------------------------------------------
EXPLAIN (ANALYZE, BUFFERS)
SELECT DISTINCT ON (client_key)
    client_key,
    date_key,
    sales
FROM fact_orders
ORDER BY client_key, date_key DESC, order_id DESC;

-- Partial-index check (not part of the two EXPLAINs above): the planner should
-- choose idx_fact_orders_zero_sale for a zero-sale slice of a single month.
-- EXPLAIN (ANALYZE, BUFFERS)
-- SELECT date_key, COUNT(*) AS zero_sale_orders
-- FROM fact_orders
-- WHERE is_zero_sale = 1
--   AND date_key BETWEEN 20220401 AND 20220430
-- GROUP BY date_key
-- ORDER BY date_key;
