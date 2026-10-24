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
    assert _scalar(db, "SELECT COUNT(*) FROM fact_sales f LEFT JOIN dim_city c USING (city_key) WHERE c.city_key IS NULL") == 0
    assert _scalar(db, "SELECT COUNT(*) FROM fact_sales f LEFT JOIN dim_date d USING (date_key) WHERE d.date_key IS NULL") == 0
    assert _scalar(db, "SELECT COUNT(*) FROM fact_orders f LEFT JOIN dim_client c USING (client_key) WHERE c.client_key IS NULL") == 0


def test_dim_date_is_contiguous(db):
    days = _scalar(db, "SELECT COUNT(*) FROM dim_date")
    assert days == 181


def test_dims_are_unique(db):
    for dim, key in [("dim_city", "city_key"), ("dim_channel", "channel_key"),
                     ("dim_category", "category_key"), ("dim_client", "client_key")]:
        assert _scalar(db, f"SELECT COUNT(*) - COUNT(DISTINCT {key}) FROM {dim}") == 0


def test_mart_daily_total_matches_fact(db):
    fact = _scalar(db, "SELECT SUM(qty) FROM fact_sales")
    mart = _scalar(db, "SELECT SUM(qty) FROM mart_daily_total")
    assert fact == mart


def test_mart_city_daily_matches_fact(db):
    fact = _scalar(db, "SELECT SUM(qty) FROM fact_sales")
    mart = _scalar(db, "SELECT SUM(qty) FROM mart_city_daily")
    assert fact == mart


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
