import pandas as pd
import pytest

from delivery import config as cfg
from delivery import etl, quality


@pytest.fixture(scope="module")
def staging():
    etl.copy_raw()
    return etl.load_agg(), etl.load_orders()


def test_agg_row_count(staging):
    sales, _ = staging
    assert len(sales) == cfg.EXPECTED_AGG_ROWS


def test_orders_row_count(staging):
    _, orders = staging
    assert len(orders) == cfg.EXPECTED_ORDER_ROWS


def test_date_range_is_six_months(staging):
    sales, orders = staging
    assert str(sales["date"].min()) == cfg.DATE_MIN
    assert str(sales["date"].max()) == cfg.DATE_MAX
    assert str(orders["date"].min()) == cfg.DATE_MIN
    assert str(orders["date"].max()) == cfg.DATE_MAX


def test_grain_is_unique(staging):
    sales, _ = staging
    dup = sales.duplicated(subset=["date", "city_name", "channel", "category"]).sum()
    assert dup == 0


def test_no_nulls(staging):
    sales, orders = staging
    assert not sales.isna().any().any()
    assert not orders.isna().any().any()


def test_metric_bounds(staging):
    sales, orders = staging
    assert (sales["qty"] > 0).all()
    assert (sales["tickets"] <= sales["qty"]).all()
    assert (sales["cost"] >= 0).all()
    assert (orders["sales"] >= 0).all()


def test_calendar_columns(staging):
    sales, _ = staging
    assert set(sales["day_of_week"].unique()) == set(range(7))
    assert sales["is_weekend"].isin([0, 1]).all()


def test_quality_report_passes(staging):
    sales, orders = staging
    report = quality.run_checks(sales, orders)
    failed = report.loc[~report["passed"], "check"].tolist()
    assert failed == []


def test_staging_roundtrip(tmp_path):
    etl.write_staging()
    sales = pd.read_parquet(cfg.STAGING / "sales_daily.parquet")
    assert len(sales) == cfg.EXPECTED_AGG_ROWS


def test_copy_raw_skips_when_dst_present(tmp_path, monkeypatch):
    """A populated data/raw/ is enough; the personal source directory may be absent."""
    raw = tmp_path / "raw"
    raw.mkdir()
    agg = raw / "city_day_channel_category.csv"
    orders = raw / "orders_sample.csv"
    agg.write_text("city\n")
    orders.write_text("order\n")
    monkeypatch.setattr(cfg, "RAW", raw)
    monkeypatch.setattr(cfg, "AGG_FILE", agg)
    monkeypatch.setattr(cfg, "ORDERS_FILE", orders)
    monkeypatch.setattr(cfg, "RAW_SOURCE", tmp_path / "missing_source")

    etl.copy_raw()  # must not raise


def test_copy_raw_raises_actionable_error(tmp_path, monkeypatch):
    raw = tmp_path / "raw"
    monkeypatch.setattr(cfg, "RAW", raw)
    monkeypatch.setattr(cfg, "AGG_FILE", raw / "city_day_channel_category.csv")
    monkeypatch.setattr(cfg, "ORDERS_FILE", raw / "orders_sample.csv")
    monkeypatch.setattr(cfg, "RAW_SOURCE", tmp_path / "missing_source")

    with pytest.raises(FileNotFoundError, match="DELIVERY_RAW_SOURCE"):
        etl.copy_raw()


def test_quality_cli_passes_on_current_data():
    etl.write_staging()
    assert quality.main() == 0
    assert (cfg.STAGING / "quality_report.csv").exists()


def test_quality_cli_fails_on_violation(tmp_path, monkeypatch):
    etl.write_staging()
    sales = pd.read_parquet(cfg.STAGING / "sales_daily.parquet").head(1)
    orders = pd.read_parquet(cfg.STAGING / "orders.parquet").head(1)
    sales.to_parquet(tmp_path / "sales_daily.parquet", index=False)
    orders.to_parquet(tmp_path / "orders.parquet", index=False)
    monkeypatch.setattr(cfg, "STAGING", tmp_path)

    assert quality.main() == 1
    assert (tmp_path / "quality_report.csv").exists()
