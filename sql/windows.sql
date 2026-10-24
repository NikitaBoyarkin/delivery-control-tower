-- Named analytical queries over the delivery marts.
--
-- Every block starts with `-- name: <snake_name>` and ends with a semicolon.
-- `delivery.sql_layer.parse_named_queries` splits on the name marker and runs
-- the statements read-only against data/marts/delivery.duckdb.
-- No final query uses SELECT *: projections are explicit so the intent of each
-- result set is visible and DuckDB prunes nothing silently.

-- name: daily_volume_ma7
-- Trailing 7-day moving average of network-wide daily order volume.
SELECT
    date,
    qty,
    ROUND(AVG(qty) OVER (ORDER BY date ROWS BETWEEN 6 PRECEDING AND CURRENT ROW), 1) AS ma7,
    ROUND(AVG(qty) OVER (ORDER BY date ROWS BETWEEN 7 PRECEDING AND 1 PRECEDING), 1) AS ma7_prior
FROM mart_daily_total
ORDER BY date;

-- name: wow_growth_pct
-- Week-over-week volume growth in percent, computed with LAG on full calendar weeks only.
WITH weekly AS (
    SELECT
        DATE_TRUNC('week', date) AS week_start,
        COUNT(*)                 AS days_in_week,
        SUM(qty)                 AS qty
    FROM mart_daily_total
    GROUP BY week_start
),
full_weeks AS (
    SELECT week_start, qty
    FROM weekly
    WHERE days_in_week = 7
)
SELECT
    week_start,
    qty,
    LAG(qty) OVER (ORDER BY week_start) AS prev_qty,
    ROUND(
        100.0 * (qty - LAG(qty) OVER (ORDER BY week_start))
        / NULLIF(LAG(qty) OVER (ORDER BY week_start), 0),
        2
    ) AS wow_growth_pct
FROM full_weeks
ORDER BY week_start;

-- name: cumulative_volume_running_total
-- Running (unbounded preceding) totals and share of the whole period completed.
SELECT
    date,
    qty,
    SUM(qty) OVER (ORDER BY date ROWS UNBOUNDED PRECEDING) AS cumulative_qty,
    ROUND(
        100.0 * SUM(qty) OVER (ORDER BY date ROWS UNBOUNDED PRECEDING) / SUM(qty) OVER (),
        2
    ) AS pct_of_period
FROM mart_daily_total
ORDER BY date;

-- name: city_monthly_rank
-- RANK and DENSE_RANK of cities by monthly volume, plus each city's share of its month.
SELECT
    YEAR(date)  AS year,
    MONTH(date) AS month,
    city_name,
    SUM(qty)    AS qty,
    RANK()       OVER (PARTITION BY YEAR(date), MONTH(date) ORDER BY SUM(qty) DESC) AS rnk,
    DENSE_RANK() OVER (PARTITION BY YEAR(date), MONTH(date) ORDER BY SUM(qty) DESC) AS dense_rnk,
    ROUND(
        100.0 * SUM(qty) / SUM(SUM(qty)) OVER (PARTITION BY YEAR(date), MONTH(date)),
        3
    ) AS month_share_pct
FROM mart_city_daily
GROUP BY year, month, city_name
ORDER BY year, month, rnk, city_name;

-- name: daily_volume_quartile
-- NTILE(4) over daily network volume: which quarter of the 181 days each day falls into.
SELECT
    date,
    day_of_week,
    is_weekend,
    qty,
    NTILE(4) OVER (ORDER BY qty) AS volume_quartile
FROM mart_daily_total
ORDER BY date;

-- name: city_share_of_total
-- Each city's share of network volume and its cumulative share in descending rank.
WITH city_tot AS (
    SELECT city_name, SUM(qty) AS qty, SUM(cost) AS cost
    FROM mart_city_daily
    GROUP BY city_name
)
SELECT
    city_name,
    qty,
    cost,
    ROUND(100.0 * qty / SUM(qty) OVER (), 4) AS volume_share_pct,
    ROUND(
        100.0 * SUM(qty) OVER (ORDER BY qty DESC ROWS UNBOUNDED PRECEDING) / SUM(qty) OVER (),
        4
    ) AS cumulative_share_pct
FROM city_tot
ORDER BY qty DESC, city_name;

-- name: city_day_vs_median
-- Every city-day compared with that city's median day (window MEDIAN keeps the grain).
SELECT
    city_name,
    date,
    qty,
    MEDIAN(qty) OVER (PARTITION BY city_name) AS city_median_qty,
    qty - MEDIAN(qty) OVER (PARTITION BY city_name) AS diff_from_median,
    ROUND(qty / NULLIF(MEDIAN(qty) OVER (PARTITION BY city_name), 0), 2) AS ratio_to_median
FROM mart_city_daily
ORDER BY city_name, date;

-- name: active_clients_30d
-- Distinct active clients in a rolling 30-day window on fact_orders.
-- The window is ordered by date_key with a 29-day RANGE frame. All rows of one
-- date_key share the same frame, so MAX collapses the order-level grain to a
-- daily series without changing the window result.
WITH windowed AS (
    SELECT
        date_key,
        COUNT(DISTINCT client_key) OVER (
            ORDER BY date_key
            RANGE BETWEEN 29 PRECEDING AND CURRENT ROW
        ) AS active_clients_30d
    FROM fact_orders
)
SELECT
    date_key,
    MAX(active_clients_30d) AS active_clients_30d
FROM windowed
GROUP BY date_key
ORDER BY date_key;

-- name: top3_days_per_month
-- Three highest-volume days inside every month (ROW_NUMBER with a date tiebreak).
WITH ranked AS (
    SELECT
        YEAR(date)  AS year,
        MONTH(date) AS month,
        date,
        qty,
        ROW_NUMBER() OVER (
            PARTITION BY YEAR(date), MONTH(date)
            ORDER BY qty DESC, date
        ) AS rn
    FROM mart_daily_total
)
SELECT
    year,
    month,
    rn AS place,
    date,
    qty
FROM ranked
WHERE rn <= 3
ORDER BY year, month, place;

-- name: anomaly_days_3x_ma7
-- City-days whose volume exceeds 3x the city's prior 7-day moving average.
-- The frame excludes the current day, so a spike cannot inflate its own baseline.
WITH rolling AS (
    SELECT
        city_name,
        date,
        qty,
        AVG(qty) OVER (
            PARTITION BY city_name
            ORDER BY date
            ROWS BETWEEN 7 PRECEDING AND 1 PRECEDING
        ) AS ma7_prior
    FROM mart_city_daily
)
SELECT
    city_name,
    date,
    qty,
    ROUND(ma7_prior, 1) AS ma7_prior,
    ROUND(qty / NULLIF(ma7_prior, 0), 2) AS ratio_to_ma7
FROM rolling
WHERE ma7_prior IS NOT NULL
  AND qty > 3 * ma7_prior
ORDER BY ratio_to_ma7 DESC, city_name;

-- name: pareto_cities_cumulative_share
-- ABC/Pareto curve: rank cities by volume and track the cumulative share,
-- marking the first 20 percent of cities.
WITH city_tot AS (
    SELECT city_name, SUM(qty) AS qty
    FROM mart_city_daily
    GROUP BY city_name
),
ranked AS (
    SELECT
        city_name,
        qty,
        ROW_NUMBER() OVER (ORDER BY qty DESC, city_name) AS rn,
        SUM(qty) OVER (ORDER BY qty DESC, city_name ROWS UNBOUNDED PRECEDING) AS running_qty,
        SUM(qty) OVER () AS total_qty,
        COUNT(*) OVER () AS city_count
    FROM city_tot
)
SELECT
    city_name,
    qty,
    rn,
    ROUND(100.0 * rn / city_count, 2) AS pct_of_cities,
    ROUND(100.0 * running_qty / total_qty, 2) AS cumulative_volume_pct,
    CASE WHEN 100.0 * rn / city_count <= 20 THEN 'top_20pct' ELSE 'rest' END AS pareto_bucket
FROM ranked
ORDER BY rn;

-- name: city_channel_rank
-- Channel ranking inside each city with two window shares: channel share of the
-- city and the city's share of the channel.
WITH city_channel AS (
    SELECT city_name, channel, SUM(qty) AS qty, SUM(cost) AS cost
    FROM mart_delivery
    GROUP BY city_name, channel
)
SELECT
    city_name,
    channel,
    qty,
    cost,
    RANK() OVER (PARTITION BY city_name ORDER BY qty DESC) AS channel_rank_in_city,
    ROUND(100.0 * qty / SUM(qty) OVER (PARTITION BY city_name), 2) AS channel_share_in_city_pct,
    ROUND(100.0 * qty / SUM(qty) OVER (PARTITION BY channel), 2) AS city_share_in_channel_pct
FROM city_channel
ORDER BY city_name, channel_rank_in_city;

-- name: city_delivery_time_rank
-- Volume-weighted delivery time per city, ranked slowest first, against the
-- network average computed in the same window.
WITH city_avg AS (
    SELECT
        city_name,
        SUM(qty) AS qty,
        SUM(qty * weighted_avg_time) / NULLIF(SUM(qty), 0) AS avg_time
    FROM mart_delivery
    GROUP BY city_name
)
SELECT
    city_name,
    ROUND(avg_time, 1) AS avg_time,
    qty,
    RANK() OVER (ORDER BY avg_time DESC) AS slowest_rank,
    ROUND(avg_time - AVG(avg_time) OVER (), 1) AS diff_from_network_avg
FROM city_avg
ORDER BY slowest_rank, city_name;

-- name: client_days_between_orders
-- LAG over each client's order history: days elapsed since the previous order.
WITH ordered AS (
    SELECT
        f.client_key,
        f.order_id,
        d.date,
        LAG(d.date) OVER (
            PARTITION BY f.client_key
            ORDER BY d.date, f.order_id
        ) AS prev_date
    FROM fact_orders f
    JOIN dim_date d ON d.date_key = f.date_key
)
SELECT
    client_key,
    order_id,
    date,
    prev_date,
    DATE_DIFF('day', prev_date, date) AS days_since_prev
FROM ordered
WHERE prev_date IS NOT NULL
ORDER BY client_key, date, order_id;
