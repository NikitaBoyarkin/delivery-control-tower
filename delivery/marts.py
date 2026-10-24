"""Build the star schema and analysis marts in DuckDB from staging parquet."""

from __future__ import annotations

import duckdb

from delivery import config as cfg

DDL = """
CREATE OR REPLACE TABLE dim_date AS
SELECT
    CAST(strftime(d, '%Y%m%d') AS INTEGER) AS date_key,
    CAST(d AS DATE)                        AS date,
    YEAR(d)                                AS year,
    MONTH(d)                               AS month,
    DAY(d)                                 AS day,
    DAYOFWEEK(d)                           AS day_of_week,
    WEEKOFYEAR(d)                          AS iso_week,
    CASE WHEN DAYOFWEEK(d) IN (0, 6) THEN 1 ELSE 0 END AS is_weekend
FROM (
    SELECT UNNEST(GENERATE_SERIES(DATE '2022-01-01', DATE '2022-06-30', INTERVAL 1 DAY)) AS d
);

CREATE OR REPLACE TABLE dim_city AS
SELECT
    ROW_NUMBER() OVER (ORDER BY city_name) AS city_key,
    city_name,
    SUM(qty)                               AS total_qty,
    SUM(cost)                              AS total_cost,
    COUNT(DISTINCT date)                   AS active_days,
    AVG(avg_time)                          AS avg_delivery_time
FROM stg_sales
GROUP BY city_name;

CREATE OR REPLACE TABLE dim_channel AS
SELECT ROW_NUMBER() OVER (ORDER BY channel) AS channel_key, channel AS channel_name
FROM (SELECT DISTINCT channel FROM stg_sales);

CREATE OR REPLACE TABLE dim_category AS
SELECT ROW_NUMBER() OVER (ORDER BY category) AS category_key, category AS category_name
FROM (SELECT DISTINCT category FROM stg_sales);

CREATE OR REPLACE TABLE fact_sales AS
SELECT
    CAST(strftime(s.date, '%Y%m%d') AS INTEGER) AS date_key,
    c.city_key,
    ch.channel_key,
    cat.category_key,
    s.qty,
    s.cost,
    s.tickets,
    s.clients_qty,
    s.avg_time,
    s.avg_price,
    s.rto,
    s.rto_do
FROM stg_sales s
JOIN dim_city c      ON c.city_name = s.city_name
JOIN dim_channel ch  ON ch.channel_name = s.channel
JOIN dim_category cat ON cat.category_name = s.category;

CREATE OR REPLACE TABLE dim_order_city AS
SELECT ROW_NUMBER() OVER (ORDER BY city_name) AS city_key, city_name
FROM (SELECT DISTINCT city_name FROM stg_orders);

CREATE OR REPLACE TABLE dim_client AS
SELECT
    ROW_NUMBER() OVER (ORDER BY client_id) AS client_key,
    client_id,
    MIN(date)                              AS first_order_date,
    MAX(date)                              AS last_order_date,
    COUNT(DISTINCT order_id)               AS orders_cnt,
    SUM(sales)                             AS revenue
FROM stg_orders
GROUP BY client_id;

CREATE OR REPLACE TABLE fact_orders AS
SELECT
    o.order_id,
    CAST(strftime(o.date, '%Y%m%d') AS INTEGER) AS date_key,
    c.city_key,
    cl.client_key,
    o.sales,
    o.is_zero_sale
FROM stg_orders o
JOIN dim_order_city c ON c.city_name = o.city_name
JOIN dim_client cl    ON cl.client_id = o.client_id;

CREATE OR REPLACE TABLE mart_city_daily AS
SELECT
    s.city_name,
    s.date,
    s.day_of_week,
    s.is_weekend,
    SUM(s.qty)        AS qty,
    SUM(s.cost)       AS cost,
    SUM(s.tickets)    AS tickets,
    SUM(s.clients_qty) AS clients_qty,
    SUM(s.qty) / NULLIF(SUM(s.tickets), 0) AS items_per_order,
    AVG(s.avg_time)   AS avg_time
FROM stg_sales s
GROUP BY s.city_name, s.date, s.day_of_week, s.is_weekend;

CREATE OR REPLACE TABLE mart_daily_total AS
SELECT
    date,
    day_of_week,
    is_weekend,
    SUM(qty)          AS qty,
    SUM(cost)         AS cost,
    SUM(tickets)      AS tickets,
    SUM(clients_qty)  AS clients_qty,
    SUM(qty) / NULLIF(SUM(tickets), 0) AS items_per_order,
    SUM(qty * avg_time) / NULLIF(SUM(qty), 0) AS weighted_avg_time
FROM stg_sales
GROUP BY date, day_of_week, is_weekend
ORDER BY date;

CREATE OR REPLACE TABLE mart_delivery AS
SELECT
    city_name,
    channel,
    category,
    SUM(qty)                                     AS qty,
    SUM(cost)                                    AS cost,
    SUM(tickets)                                 AS tickets,
    SUM(qty * avg_time) / NULLIF(SUM(qty), 0)    AS weighted_avg_time,
    SUM(rto * qty) / NULLIF(SUM(qty), 0)         AS weighted_rto,
    SUM(rto_do * qty) / NULLIF(SUM(qty), 0)      AS weighted_rto_do,
    SUM(cost) / NULLIF(SUM(qty), 0)              AS cost_per_item,
    SUM(qty) / NULLIF(SUM(tickets), 0)           AS items_per_order
FROM stg_sales
GROUP BY city_name, channel, category;
"""


def build(db_path=cfg.DUCKDB_PATH) -> dict[str, int]:
    cfg.MARTS.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()
    con = duckdb.connect(str(db_path))
    con.execute(
        f"CREATE OR REPLACE VIEW stg_sales AS SELECT * FROM read_parquet('{cfg.STAGING / 'sales_daily.parquet'}')"
    )
    con.execute(
        f"CREATE OR REPLACE VIEW stg_orders AS SELECT * FROM read_parquet('{cfg.STAGING / 'orders.parquet'}')"
    )
    con.execute(DDL)
    tables = [
        "dim_date",
        "dim_city",
        "dim_channel",
        "dim_category",
        "dim_client",
        "dim_order_city",
        "fact_sales",
        "fact_orders",
        "mart_city_daily",
        "mart_daily_total",
        "mart_delivery",
    ]
    counts = {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}
    con.close()
    return counts


if __name__ == "__main__":
    print(build())
