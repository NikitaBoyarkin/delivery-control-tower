import duckdb
import pandas as pd
import pytest

from delivery import config as cfg
from delivery import etl, marts


@pytest.fixture(scope="module")
def db():
    etl.write_staging()
    marts.build()
    con = duckdb.connect(str(cfg.DUCKDB_PATH), read_only=True)
    yield con
    con.close()


def _scalar(con, sql):
    return con.execute(sql).fetchone()[0]


def test_fact_sales_row_count_equals_staging(db):
    staged = len(pd.read_parquet(cfg.STAGING / "sales_daily.parquet"))
    assert _scalar(db, "SELECT COUNT(*) FROM fact_sales") == staged


def test_fact_orders_row_count(db):
    assert _scalar(db, "SELECT COUNT(*) FROM fact_orders") == cfg.EXPECTED_ORDER_ROWS


def test_qty_is_preserved(db):
    staged = pd.read_parquet(cfg.STAGING / "sales_daily.parquet")["qty"].sum()
    assert _scalar(db, "SELECT SUM(qty) FROM fact_sales") == staged


def test_referential_integrity(db):
    assert (
        _scalar(
            db,
            "SELECT COUNT(*) FROM fact_sales f LEFT JOIN dim_city c USING (city_key) WHERE c.city_key IS NULL",
        )
        == 0
    )
    assert (
        _scalar(
            db,
            "SELECT COUNT(*) FROM fact_sales f LEFT JOIN dim_date d USING (date_key) WHERE d.date_key IS NULL",
        )
        == 0
    )
    assert (
        _scalar(
            db,
            "SELECT COUNT(*) FROM fact_orders f LEFT JOIN dim_client c USING (client_key) WHERE c.client_key IS NULL",
        )
        == 0
    )


def test_dim_date_is_contiguous(db):
    days = _scalar(db, "SELECT COUNT(*) FROM dim_date")
    assert days == 181


def test_dims_are_unique(db):
    for dim, key in [
        ("dim_city", "city_key"),
        ("dim_channel", "channel_key"),
        ("dim_category", "category_key"),
        ("dim_client", "client_key"),
    ]:
        assert _scalar(db, f"SELECT COUNT(*) - COUNT(DISTINCT {key}) FROM {dim}") == 0


def test_mart_daily_total_matches_fact(db):
    fact = _scalar(db, "SELECT SUM(qty) FROM fact_sales")
    mart = _scalar(db, "SELECT SUM(qty) FROM mart_daily_total")
    assert fact == mart


def test_mart_city_daily_matches_fact(db):
    fact = _scalar(db, "SELECT SUM(qty) FROM fact_sales")
    mart = _scalar(db, "SELECT SUM(qty) FROM mart_city_daily")
    assert fact == mart


def test_city_time_is_qty_weighted_not_averaged(db):
    """``avg_time`` is a ratio (time / orders), so a period aggregate must re-weight by qty.

    ``AVG(avg_time)`` gives a 3-order cell the same say as a 300-order cell and makes the
    weekday/weekend comparison disappear; ``docs/data_dictionary.md`` forbids it. This test
    recomputes the weighted city-day value from ``fact_sales`` and fails on any drifting row —
    reverting to ``AVG`` fails on essentially every row.
    """
    mismatches = _scalar(
        db,
        """
        WITH recomputed AS (
            SELECT c.city_name, d.date,
                   SUM(f.qty * f.avg_time) / NULLIF(SUM(f.qty), 0) AS expected
            FROM fact_sales f
            JOIN dim_city c USING (city_key)
            JOIN dim_date d USING (date_key)
            GROUP BY 1, 2
        )
        SELECT COUNT(*)
        FROM recomputed r
        JOIN mart_city_daily m USING (city_name, date)
        WHERE abs(r.expected - m.avg_time) > 1e-9
        """,
    )
    assert mismatches == 0


def test_city_dimension_time_is_qty_weighted(db):
    weighted = _scalar(db, "SELECT SUM(qty * avg_time) / NULLIF(SUM(qty), 0) FROM stg_sales")
    dimension = _scalar(
        db, "SELECT SUM(total_qty * avg_delivery_time) / SUM(total_qty) FROM dim_city"
    )
    assert dimension == pytest.approx(weighted, rel=1e-9)


def test_items_per_order_is_above_one(db):
    bad = _scalar(db, "SELECT COUNT(*) FROM mart_delivery WHERE items_per_order <= 1")
    assert bad == 0


def test_items_per_order_matches_qty_over_tickets(db):
    qty = _scalar(db, "SELECT SUM(qty) FROM mart_delivery")
    tickets = _scalar(db, "SELECT SUM(tickets) FROM mart_delivery")
    weighted = _scalar(
        db, "SELECT SUM(items_per_order * tickets) / SUM(tickets) FROM mart_delivery"
    )
    assert weighted == pytest.approx(qty / tickets, rel=1e-9)


def test_time_marts_expose_items_per_order(db):
    for table in ("mart_daily_total", "mart_city_daily"):
        assert _scalar(db, f"SELECT COUNT(*) FROM {table} WHERE items_per_order IS NULL") == 0
        assert _scalar(db, f"SELECT MIN(items_per_order) FROM {table}") > 0
        assert _scalar(db, f"SELECT SUM(qty) / SUM(tickets) FROM {table}") > 1


def test_mart_delivery_uses_cost_per_item(db):
    columns = {row[0] for row in db.execute("DESCRIBE mart_delivery").fetchall()}
    assert "cost_per_item" in columns
    assert "cost_per_order" not in columns
    qty = _scalar(db, "SELECT SUM(qty) FROM mart_delivery")
    cost = _scalar(db, "SELECT SUM(cost) FROM mart_delivery")
    weighted = _scalar(db, "SELECT SUM(cost_per_item * qty) / SUM(qty) FROM mart_delivery")
    assert weighted == pytest.approx(cost / qty, rel=1e-9)
