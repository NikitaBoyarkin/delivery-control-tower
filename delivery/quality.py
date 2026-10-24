"""Data quality checks. Each check is a named assertion with a recorded value."""

from __future__ import annotations

import pandas as pd

from delivery import config as cfg


def _check(name: str, table: str, passed: bool, value, expected) -> dict:
    return {
        "check": name,
        "table": table,
        "passed": bool(passed),
        "value": value,
        "expected": expected,
    }


def run_checks(sales: pd.DataFrame, orders: pd.DataFrame) -> pd.DataFrame:
    checks = [
        _check("row_count", "sales_daily", len(sales) == cfg.EXPECTED_AGG_ROWS, len(sales), cfg.EXPECTED_AGG_ROWS),
        _check("grain_unique", "sales_daily",
               not sales.duplicated(subset=["date", "city_name", "channel", "category"]).any(),
               int(sales.duplicated(subset=["date", "city_name", "channel", "category"]).sum()), 0),
        _check("no_nulls", "sales_daily", not sales.isna().any().any(),
               int(sales.isna().sum().sum()), 0),
        _check("qty_positive", "sales_daily", bool((sales["qty"] > 0).all()),
               int((sales["qty"] <= 0).sum()), 0),
        _check("tickets_le_qty", "sales_daily", bool((sales["tickets"] <= sales["qty"]).all()),
               int((sales["tickets"] > sales["qty"]).sum()), 0),
        _check("cost_non_negative", "sales_daily", bool((sales["cost"] >= 0).all()),
               int((sales["cost"] < 0).sum()), 0),
        _check("date_min", "sales_daily", str(sales["date"].min()) == cfg.DATE_MIN,
               str(sales["date"].min()), cfg.DATE_MIN),
        _check("date_max", "sales_daily", str(sales["date"].max()) == cfg.DATE_MAX,
               str(sales["date"].max()), cfg.DATE_MAX),
        _check("row_count", "orders", len(orders) == cfg.EXPECTED_ORDER_ROWS, len(orders), cfg.EXPECTED_ORDER_ROWS),
        _check("order_id_unique", "orders", bool(orders["order_id"].is_unique),
               int(orders["order_id"].duplicated().sum()), 0),
        _check("sales_non_negative", "orders", bool((orders["sales"] >= 0).all()),
               int((orders["sales"] < 0).sum()), 0),
        _check("orders_no_nulls", "orders", not orders.isna().any().any(),
               int(orders.isna().sum().sum()), 0),
    ]
    return pd.DataFrame(checks)


def write_report(report: pd.DataFrame) -> pd.DataFrame:
    cfg.STAGING.mkdir(parents=True, exist_ok=True)
    path = cfg.STAGING / "quality_report.csv"
    report.to_csv(path, index=False)
    failed = report.loc[~report["passed"]]
    print(f"quality checks: {len(report)} total, {len(failed)} failed")
    if len(failed):
        print(failed.to_string(index=False))
    return report


def load_staging() -> tuple[pd.DataFrame, pd.DataFrame]:
    """Read the two staging parquet files, or explain how to create them."""
    sales_path = cfg.STAGING / "sales_daily.parquet"
    orders_path = cfg.STAGING / "orders.parquet"
    if not sales_path.exists() or not orders_path.exists():
        raise FileNotFoundError(
            f"staging parquet missing in {cfg.STAGING}: run "
            "'uv run python -m delivery.etl' first."
        )
    return pd.read_parquet(sales_path), pd.read_parquet(orders_path)


def main() -> int:
    """Run every quality check, write the CSV report, return the exit code."""
    try:
        sales, orders = load_staging()
    except FileNotFoundError as exc:
        print(exc)
        return 1
    report = run_checks(sales, orders)
    write_report(report)
    return 0 if bool(report["passed"].all()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
