"""Generate a small, deterministic synthetic dataset for CI and clean clones.

The real source CSVs are gitignored, so a fresh checkout has no data and most of
the suite skips. This module writes a schema-identical fixture so the pipeline
(``etl -> quality -> marts``) actually executes.

Sizing notes:

* ``181`` days (2022-01-01..2022-06-30) matches the hardcoded calendar in
  ``delivery.marts`` and the dashboard heatmap (7 x 26 weeks, 181 filled cells).
* ``60`` cities keeps ``bottlenecks.city_ranking`` populated (``MIN_CITY_DAYS``
  is 60 and the analysis asserts at least 50 cities).
* ``mp`` / ``web`` channels and January/June coverage feed ``hypothesis``.
* A handful of volume spikes keep ``anomaly_days_3x_ma7`` non-empty.
* Orders touch every one of the 181 dates so ``active_clients_30d`` yields a
  full daily series.

Run as a smoke test in CI::

    uv run python -m scripts.make_synthetic_data --base /tmp/delivery-fixture
"""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd

from delivery import config as cfg
from delivery import etl, marts, quality

DATES = pd.date_range("2022-01-01", "2022-06-30", freq="D")
CITIES = [f"City{i:02d}" for i in range(60)]
CHANNELS = ["mp", "web", "app"]
CATEGORIES = ["пицца", "суши", "бургеры", "десерты"]
SPIKE_DATES = [DATES[40], DATES[100], DATES[140]]
CLIENTS = np.arange(1, 81)


def _aggregate_frame() -> pd.DataFrame:
    index = pd.MultiIndex.from_product(
        [DATES, CITIES, CHANNELS, CATEGORIES],
        names=["Date", "CityName", "OrderSource", "Category"],
    )
    frame = pd.DataFrame(index=index).reset_index()
    n = len(frame)
    rng = np.random.default_rng(20220101)

    weekend = (frame["Date"].dt.day_of_week >= 5).to_numpy()
    city_factor = pd.Series(rng.uniform(0.6, 1.6, len(CITIES)), index=CITIES)
    city_time = pd.Series(rng.uniform(0.0, 8.0, len(CITIES)), index=CITIES)

    qty = (
        rng.integers(8, 260, n).astype(float)
        * frame["CityName"].map(city_factor).to_numpy()
        * np.where(weekend, 1.35, 1.0)
    )
    spike = np.isin(frame["Date"].to_numpy(), SPIKE_DATES) & (
        frame["CityName"] == "City00"
    ).to_numpy()
    qty[spike] *= 12.0
    qty = np.maximum(np.round(qty), 2).astype("int64")

    items_per_order = rng.uniform(1.2, 2.4, n)
    tickets = np.maximum(1, np.round(qty / items_per_order).astype("int64"))
    tickets = np.minimum(tickets, qty - 1)

    result = pd.DataFrame(
        {
            "Date": frame["Date"].dt.strftime("%Y-%m-%d"),
            "CityName": frame["CityName"],
            "OrderSource": frame["OrderSource"],
            "Category": frame["Category"],
            "rto": rng.uniform(0.003, 0.04, n),
            "rto_do": rng.uniform(0.002, 0.03, n),
            "cost": qty * rng.uniform(70.0, 220.0, n),
            "qty": qty,
            "tickets": tickets,
            "clients_qty": np.maximum(
                1, (qty * rng.uniform(0.6, 0.95, n)).astype("int64")
            ),
            "avg_time": np.clip(
                22.0
                + frame["CityName"].map(city_time).to_numpy()
                + np.where(weekend, 3.0, 0.0)
                + rng.normal(0.0, 0.8, n),
                5.0,
                None,
            ),
            "avg_price": rng.uniform(120.0, 900.0, n),
        }
    )
    return result


def _orders_frame() -> pd.DataFrame:
    rng = np.random.default_rng(7)
    rows = []
    order_id = 1
    for date in DATES:
        for slot in range(2):
            rows.append(
                {
                    "cityname": "City00" if slot == 0 else "City01",
                    "date": date.strftime("%Y-%m-%d"),
                    "clientid": int(rng.choice(CLIENTS)),
                    "orderid": order_id,
                    "sales": 0.0 if rng.random() < 0.05 else float(rng.uniform(150, 2500)),
                }
            )
            order_id += 1
    return pd.DataFrame(rows)


def write_raw(raw_dir: str | Path, overwrite: bool = False) -> dict[str, int]:
    """Write the two source CSVs into ``raw_dir`` and return their row counts.

    Refuses to replace existing files by default: the real source CSVs are
    gitignored and a stray run without ``--base`` used to destroy them.
    """
    raw = Path(raw_dir)
    targets = (raw / "city_day_channel_category.csv", raw / "orders_sample.csv")
    existing = [path for path in targets if path.exists()]
    if existing and not overwrite:
        listed = ", ".join(str(path) for path in existing)
        raise FileExistsError(
            f"refusing to overwrite existing source CSVs: {listed} (pass --force to replace them)"
        )
    raw.mkdir(parents=True, exist_ok=True)
    aggregate = _aggregate_frame()
    orders = _orders_frame()
    targets[0].write_text(aggregate.to_csv(index=False), encoding="utf-8")
    targets[1].write_text(orders.to_csv(index=False), encoding="utf-8")
    return {"agg": len(aggregate), "orders": len(orders)}


def apply_paths(base: str | Path) -> dict[str, Path]:
    """Point every ``delivery.config`` path at ``base`` and create the dirs."""
    base = Path(base)
    raw = base / "raw"
    staging = base / "staging"
    marts_dir = base / "marts"
    reports = base / "reports"
    figures = reports / "figures"

    cfg.RAW = raw
    cfg.STAGING = staging
    cfg.MARTS = marts_dir
    cfg.REPORTS = reports
    cfg.FIGURES = figures
    cfg.DUCKDB_PATH = marts_dir / "delivery.duckdb"
    cfg.AGG_FILE = raw / "city_day_channel_category.csv"
    cfg.ORDERS_FILE = raw / "orders_sample.csv"
    cfg.RAW_SOURCE = base / "raw_source"

    for directory in (raw, staging, marts_dir, reports, figures):
        directory.mkdir(parents=True, exist_ok=True)
    return {
        "raw": raw,
        "staging": staging,
        "marts": marts_dir,
        "reports": reports,
        "duckdb": cfg.DUCKDB_PATH,
    }


def bind_expected_rows(counts: dict[str, int]) -> None:
    """Point quality's row-count contract at the fixture that was just written.

    The synthetic dataset is schema-identical but deliberately smaller than the
    real one, so ``config.EXPECTED_*_ROWS`` (the real dataset's counts) can never
    match it. Binding the expectation to the rows just written keeps the check
    meaningful: it still fails when ETL drops or duplicates raw rows.
    """
    cfg.EXPECTED_AGG_ROWS = counts["agg"]
    cfg.EXPECTED_ORDER_ROWS = counts["orders"]


def build(base: str | Path, overwrite: bool = False) -> dict[str, int]:
    """Materialise the fixture and run ``etl -> quality -> marts`` into ``base``."""
    paths = apply_paths(base)
    counts = write_raw(paths["raw"], overwrite=overwrite)
    bind_expected_rows(counts)
    etl.write_staging()
    sales, orders = quality.load_staging()
    report = quality.write_report(quality.run_checks(sales, orders))
    failed = report.loc[~report["passed"]]
    if len(failed):
        raise RuntimeError(
            f"{len(failed)} quality checks failed:\n{failed.to_string(index=False)}"
        )
    marts.build(db_path=cfg.DUCKDB_PATH)
    return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base",
        type=Path,
        default=None,
        help="where to write the fixture; required, never defaults to data/",
    )
    parser.add_argument(
        "--force", action="store_true", help="replace source CSVs that already exist"
    )
    args = parser.parse_args(argv)
    if args.base is None:
        parser.error(
            "--base is required: the default data/ directory holds the real source "
            "CSVs, which are gitignored and cannot be restored from the repository"
        )
    counts = build(args.base, overwrite=args.force)
    print(f"synthetic fixture built in {args.base}: {counts}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
