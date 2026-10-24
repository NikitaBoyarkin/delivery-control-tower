-- Slow / rewritten query pairs measured with EXPLAIN ANALYZE on DuckDB.
--
-- DuckDB has no user-managed indexes and its optimizer already pushes filters
-- and projections down, so rewrites aimed at "SELECT *" or extra joins buy
-- almost nothing here. The pairs below attack work the optimizer cannot remove:
-- correlated subqueries executed per outer row, scalar functions evaluated on
-- every fact row before aggregation, and a full re-scan per client.
--
-- Each pair returns exactly the same rows in both variants (the test suite
-- asserts frame equality). Format parsed by delivery.sql_layer.parse_optimization_pairs:
--   -- pair: <name>
--   -- variant: slow|fast
--   -- note: <one line describing the rewrite>
-- followed by one statement ending with a semicolon.

-- pair: running_order_count
-- variant: slow
-- note: correlated COUNT re-scans fact_orders for each of the 130,079 orders
SELECT
    o.order_id,
    (
        SELECT COUNT(*)
        FROM fact_orders o2
        WHERE o2.client_key = o.client_key
          AND o2.date_key <= o.date_key
    ) AS orders_to_date
FROM fact_orders o
ORDER BY o.order_id;

-- pair: running_order_count
-- variant: fast
-- note: one window pass with a RANGE frame keeps peer orders and drops the correlated scan
SELECT
    order_id,
    COUNT(*) OVER (
        PARTITION BY client_key
        ORDER BY date_key
        RANGE BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
    ) AS orders_to_date
FROM fact_orders
ORDER BY order_id;

-- pair: monthly_revenue_label
-- variant: slow
-- note: STRFTIME runs on all 550,710 fact rows before the aggregation
SELECT
    STRFTIME(d.date, '%Y-%m') AS month_label,
    SUM(f.cost) AS cost
FROM fact_sales f
JOIN dim_date d ON d.date_key = f.date_key
GROUP BY month_label
ORDER BY month_label;

-- pair: monthly_revenue_label
-- variant: fast
-- note: projection plus aggregate-before-join, so STRFTIME runs on 181 date rows
SELECT
    d.month_label,
    SUM(f.cost) AS cost
FROM (
    SELECT date_key, SUM(cost) AS cost
    FROM fact_sales
    GROUP BY date_key
) f
JOIN (
    SELECT date_key, STRFTIME(date, '%Y-%m') AS month_label
    FROM dim_date
) d ON d.date_key = f.date_key
GROUP BY d.month_label
ORDER BY d.month_label;

-- pair: client_lifetime_revenue
-- variant: slow
-- note: correlated SUM is executed once per client on top of a DISTINCT scan
SELECT
    c.client_key,
    (
        SELECT SUM(o2.sales)
        FROM fact_orders o2
        WHERE o2.client_key = c.client_key
    ) AS revenue
FROM (SELECT DISTINCT client_key FROM fact_orders) c
ORDER BY c.client_key;

-- pair: client_lifetime_revenue
-- variant: fast
-- note: a single grouped scan over fact_orders replaces 31,426 correlated aggregates
SELECT
    client_key,
    SUM(sales) AS revenue
FROM fact_orders
GROUP BY client_key
ORDER BY client_key;
