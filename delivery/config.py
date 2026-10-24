"""Paths and shared constants for the delivery control tower project."""

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

DATA = ROOT / "data"
RAW = DATA / "raw"
STAGING = DATA / "staging"
MARTS = DATA / "marts"
REPORTS = ROOT / "reports"
FIGURES = REPORTS / "figures"

DUCKDB_PATH = MARTS / "delivery.duckdb"

# Source CSVs come from a public test task, copied into data/raw/ by `etl.copy_raw`.
# Override with DELIVERY_RAW_SOURCE when the files live elsewhere.
RAW_SOURCE = Path(os.environ.get("DELIVERY_RAW_SOURCE") or "data/raw_source")
RAW_AGG = "оценка_категорий_4.csv"
RAW_ORDERS = "DA_sql_sample - sql_task.csv"

AGG_FILE = RAW / "city_day_channel_category.csv"
ORDERS_FILE = RAW / "orders_sample.csv"

DATE_MIN = "2022-01-01"
DATE_MAX = "2022-06-30"

EXPECTED_AGG_ROWS = 550_710
EXPECTED_ORDER_ROWS = 130_079

FACT_SALES = "fact_sales"
FACT_ORDERS = "fact_orders"
