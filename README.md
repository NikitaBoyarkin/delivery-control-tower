# Delivery Control Tower

Delivery operations analytics on a six-month order aggregate: a reproducible
ETL pipeline, a star schema in DuckDB, a rolling-origin order forecast, a
bottleneck analysis of delivery time, two pre-registered hypotheses, a
window-function SQL layer with measured optimizations, and an offline
role-based dashboard.

The project asks one operational question — *where does the delivery process
lose time, and can volume be forecast well enough to staff for it?* — and
answers it with numbers that can be re-run from the CSVs in `reports/`.

## What it demonstrates

| Layer | What is shown |
|---|---|
| Data engineering | Raw → staging (parquet) → DuckDB star schema; every quality check is a named assertion with a recorded value |
| Forecasting | Rolling-origin backtest (test window 28 days, refit every 7 days, four folds) of four models at horizons 1, 7 and 14, scored with WAPE and MASE |
| Operations analysis | Delivery-time decomposition by channel × category, a volume-decile load curve, a per-city degradation slope and a composite degradation index |
| Statistics | Mann-Whitney U with bootstrap confidence intervals; a hypothesis is called supported only when the CI excludes zero |
| SQL | 14 window-function queries written as named blocks, plus three slow/fast pairs benchmarked under `EXPLAIN ANALYZE` |
| Presentation | A single self-contained HTML dashboard (no CDN, no client-side requests) with three blocks, one per role |

## Data & caveats

The source is the aggregate table shipped with a public data-analyst test task
(Dodo Brands): **day × city × channel × category** for **2022-01-01 .. 2022-06-30**,
covering **550,710 rows**, **264 cities**, **3 channels** (`mp`, `web`, `other`) and
**4 categories**. A second file is an order-level sample of **130,079 orders**
across **2 anonymized cities**.

Read these caveats before quoting any number:

- `tickets` means **orders** (чеки), not support tickets. `SUM(tickets)` over the
  period is 33,167,293.
- `qty` means **items** (позиции): 57,576,550 in total, 1.736 items per order.
- `cost` is a **money metric in unconfirmed source units**. The currency is not
  documented, and nothing in the data proves it is revenue. It is labelled
  `cost, source units` everywhere and never called revenue or roubles.
- `avg_price` does not reconcile with `SUM(cost) / SUM(tickets)` (221.8 vs 138.9),
  so its semantics are unconfirmed and it is **not used in any calculation**.
- `clients_qty` is unique clients *inside a cell*; summing it across cells does
  not give unique clients for the period.
- `avg_time` is a per-cell mean in minutes; any aggregation over a period is
  weighted by `qty`, never a plain `AVG`.

Full definitions: `docs/data_dictionary.md`.

## Architecture

```
data/raw/*.csv                     source aggregates, copied verbatim
        │  delivery.etl           dtype normalisation, calendar columns → parquet
        ▼
data/staging/*.parquet             sales_daily, orders
        │  delivery.quality       named assertions: row count, grain uniqueness, ranges
        │  delivery.marts         star schema + analysis marts
        ▼
data/marts/delivery.duckdb
        dim_date · dim_city · dim_channel · dim_category · dim_client · dim_order_city
        fact_sales (day × city × channel × category) · fact_orders (sample)
        mart_daily_total · mart_city_daily · mart_delivery
        │
        ├─ delivery.forecast      4 models × 3 horizons, rolling-origin backtest
        ├─ delivery.bottlenecks   channel×category, load deciles, city ranking
        ├─ delivery.hypothesis    H1 channel mix, H2 weekend time (bootstrap CI)
        ├─ delivery.sql_layer     14 window queries + 3 optimization pairs
        └─ delivery.dashboard     self-contained HTML → reports/dashboard.html
```

`delivery/` is a plain module package; there is no orchestration framework.
Every stage is idempotent and writes its artifacts into `reports/`.

## Quickstart

Python 3.12 (declared `>=3.11`), dependencies managed with `uv`.

```bash
uv sync --all-groups      # runtime + dev dependencies
uv run python -m delivery.etl
uv run python -m delivery.marts
uv run python -m delivery.forecast
uv run python -m delivery.bottlenecks
uv run python -m delivery.hypothesis
uv run python -m delivery.sql_layer   # window queries + optimization benchmark
uv run python -m delivery.dashboard   # → reports/dashboard.html
uv run pytest                         # 79 tests
uv run ruff check delivery/ tests/
```

`delivery.etl` expects the two source CSVs in `data/raw/`. With the files already
there the later stages run on their own.

**Data.** The source CSVs are not in the repository — together they are about
**~85 MB**. Put them in `data/raw/` as `city_day_channel_category.csv` and
`orders_sample.csv`, or set the `DELIVERY_RAW_SOURCE` environment variable to
the directory that holds the original files (named
`оценка_категорий_4.csv` and `DA_sql_sample - sql_task.csv`); `delivery.etl`
then copies them into `data/raw/`. A non-empty `data/raw/` always wins: the copy
is skipped, so a fresh clone with the CSVs in place needs no source directory.

The mart tests read `data/marts/delivery.duckdb`; on a clean machine run
`uv run python -m delivery.marts` first, otherwise those tests skip.

## Results

### Order forecast — WAPE by horizon (%, four folds)

| Model | h=1 | h=7 | h=14 |
|---|---:|---:|---:|
| naive | 22.68 | 12.53 | 12.54 |
| seasonal_naive | 5.40 | **6.01** | **6.25** |
| holt_winters | **3.91** | 7.16 | 7.22 |
| hgb (gradient boosting) | 7.14 | 7.90 | 8.09 |

Holt-Winters wins the one-day horizon (3.91%), but the seven- and fourteen-day
windows belong to the weekly seasonal naive (6.01% and 6.25%). The gradient
boosting model does not beat the seasonal baseline at any horizon.

### Three findings

1. **Delivery time is driven by category, not by load.** The pooled Spearman
   correlation between daily city volume and delivery time is **ρ = −0.044**;
   mean time moves only from 30.58 min in the lowest volume decile to 30.35 min
   in the highest. Within the same channel, snacks take **≈39.95–39.99 min**
   against pizza at **≈26.49–26.56 min** — roughly **+13.4 min (+50%)**.
2. **Channel mix is shifting towards the marketplace.** The `mp` share inside
   the `mp + web` pair grows from **81.9% in January to 87.6% in June**
   (**+5.7 pp**, Mann-Whitney p = 2.1e-11, bootstrap CI [5.35; 6.03]).
3. **SQL pays for itself when rewritten.** Of three slow/fast pairs, the best
   speedup is **5.66×** on a running order count over 130,079 rows
   (62.3 ms → 11.0 ms); the other two reach 3.23× and 1.27×. All 14 window
   queries finish under 200 ms on this dataset, the slowest being a 30-day
   active-client count at **195.95 ms**. Timings are wall-clock on this machine,
   median of 3 runs after one warm-up; run-to-run spread is up to ±30%, so
   speedups are an order of magnitude, not an exact figure.

### One honest negative result

**Weekend deliveries are not slower.** The pre-registered hypothesis H2 is not
supported: median delivery time on a city-day is **30.44 min on weekends vs
30.45 min on weekdays (−0.02%)**, p = **0.617**, bootstrap CI [−0.151; +0.095],
n = 13,720 / 34,030. That is a flat trend, not an effect.

The forecast carries a second honest negative: the machine-learning model does
not beat a seasonal naive baseline, and the project reports the baseline as the
recommendation for h=7 and h=14 rather than the more complex model.

## Project structure

```
delivery/
  config.py        paths and shared constants
  etl.py           raw → staging parquet, calendar features
  quality.py       named data-quality assertions
  marts.py         star schema and analysis marts in DuckDB
  forecast.py      rolling-origin backtest, four models, WAPE/MASE
  bottlenecks.py   channel × category, load curve, city degradation ranking
  hypothesis.py    H1 / H2 with Mann-Whitney and bootstrap CI
  sql_layer.py     named window queries + EXPLAIN ANALYZE benchmark
  dashboard.py     offline HTML dashboard, inline SVG only
sql/
  windows.sql            14 named window-function queries
  optimization.sql       3 slow/fast pairs
  indexes_postgres.sql   index DDL for a Postgres deployment
superset/          docker-compose recipe for a live Superset (unverified, see below)
docs/data_dictionary.md
reports/           CSV artifacts, figures, dashboard.html
tests/             79 pytest tests
```

## Limitations

- **Aggregate granularity.** The data is pre-aggregated to
  day × city × channel × category. There is no order-level timing, no courier,
  no restaurant dimension, so a delivery-time decomposition stops at the
  category level.
- **The order sample covers two anonymized cities.** It is used for the SQL
  layer only and does not support city-level conclusions.
- **`cost` has no confirmed currency** and is not treated as revenue.
  `avg_price` is unused for the same reason.
- **The city degradation slopes are weak.** The top-ranked city (Азов,
  degradation index 2.07) has a slope R² of 0.0098, so the ranking is a
  prioritization aid, not a significant per-city trend.
- **Four backtest folds.** WAPE is estimated on a 28-day test window with four
  refits; the confidence around the model comparison is therefore narrow in
  sample count, and differences of a few tenths of a point should not be read
  as decisive.
- **The Superset recipe was never started.** Docker is unavailable on the build
  machine, so `superset/docker-compose.yml` and `init_superset.sh` are written
  but unverified: no run, no screenshots, no confirmed connection. The artifact
  that is tested is the static dashboard.
- **No causal claims.** The hypotheses are associational tests on observational
  aggregates; nothing here identifies a treatment effect.
