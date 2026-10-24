"""Raw -> staging. Copies source CSVs, normalises dtypes, writes parquet."""

from __future__ import annotations

import shutil

import pandas as pd

from delivery import config as cfg

CALENDAR_COLUMNS = ["year", "month", "day", "day_of_week", "iso_week", "is_weekend"]


def copy_raw() -> None:
    """Make sure the two source CSVs exist in ``data/raw/``.

    A non-empty destination is authoritative: the copy is skipped even when the
    original source directory is absent, so a clean clone that ships the CSVs in
    ``data/raw/`` (or a user who put them there) runs end-to-end. Only when the
    destination is missing does the source path come into play.
    """
    cfg.RAW.mkdir(parents=True, exist_ok=True)
    pairs = {
        cfg.RAW_SOURCE / cfg.RAW_AGG: cfg.AGG_FILE,
        cfg.RAW_SOURCE / cfg.RAW_ORDERS: cfg.ORDERS_FILE,
    }
    for src, dst in pairs.items():
        if dst.exists() and dst.stat().st_size > 0:
            continue
        if src.exists():
            shutil.copy2(src, dst)
            continue
        raise FileNotFoundError(
            f"raw file missing: {dst.name} was not found in {cfg.RAW} "
            f"and the source {src} does not exist.\n"
            f"Fix: put the file at {dst} (or set DELIVERY_RAW_SOURCE to the "
            f"directory that contains {src.name})."
        )


def _add_calendar(df: pd.DataFrame, date_col: str = "date") -> pd.DataFrame:
    dates = pd.to_datetime(df[date_col])
    df = df.assign(
        year=dates.dt.year,
        month=dates.dt.month,
        day=dates.dt.day,
        day_of_week=dates.dt.day_of_week,
        iso_week=dates.dt.isocalendar().week.astype(int),
        is_weekend=(dates.dt.day_of_week >= 5).astype(int),
    )
    return df


def load_agg() -> pd.DataFrame:
    """City x day x channel x category aggregate."""
    df = pd.read_csv(cfg.AGG_FILE)
    df = df.rename(
        columns={
            "Date": "date",
            "CityName": "city_name",
            "OrderSource": "channel",
            "Category": "category",
        }
    )
    df["date"] = pd.to_datetime(df["date"]).dt.date
    metrics = ["rto", "rto_do", "cost", "qty", "tickets", "clients_qty", "avg_time", "avg_price"]
    df[metrics] = df[metrics].astype("float64")
    df["qty"] = df["qty"].astype("int64")
    df["tickets"] = df["tickets"].astype("int64")
    df["clients_qty"] = df["clients_qty"].astype("int64")
    df = _add_calendar(df)
    return df[
        [
            "date",
            "city_name",
            "channel",
            "category",
            "qty",
            "cost",
            "tickets",
            "clients_qty",
            "avg_time",
            "avg_price",
            "rto",
            "rto_do",
            *CALENDAR_COLUMNS,
        ]
    ]


def load_orders() -> pd.DataFrame:
    """Order-level sample: two anonymised cities."""
    df = pd.read_csv(cfg.ORDERS_FILE)
    df = df.rename(
        columns={
            "cityname": "city_name",
            "date": "date",
            "clientid": "client_id",
            "orderid": "order_id",
            "sales": "sales",
        }
    )
    df["date"] = pd.to_datetime(df["date"]).dt.date
    df["sales"] = df["sales"].astype("float64")
    df["client_id"] = df["client_id"].astype("int64")
    df["order_id"] = df["order_id"].astype("int64")
    df["is_zero_sale"] = (df["sales"] == 0).astype(int)
    df = _add_calendar(df)
    return df[
        [
            "date",
            "city_name",
            "client_id",
            "order_id",
            "sales",
            "is_zero_sale",
            *CALENDAR_COLUMNS,
        ]
    ]


def write_staging() -> dict[str, int]:
    copy_raw()
    cfg.STAGING.mkdir(parents=True, exist_ok=True)
    agg = load_agg()
    orders = load_orders()
    agg.to_parquet(cfg.STAGING / "sales_daily.parquet", index=False)
    orders.to_parquet(cfg.STAGING / "orders.parquet", index=False)
    return {"sales_daily": len(agg), "orders": len(orders)}


if __name__ == "__main__":
    try:
        print(write_staging())
    except FileNotFoundError as exc:
        print(exc)
        raise SystemExit(1) from None
