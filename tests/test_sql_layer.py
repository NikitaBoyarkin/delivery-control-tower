"""Tests for the SQL layer: named window queries and optimization pairs."""

import re

import duckdb
import numpy as np
import pandas as pd
import pytest

from delivery import config as cfg
from delivery import sql_layer

TOTAL_CLIENTS = 31_426
ACTIVE_CLIENTS_20220630 = 12_318

EXPECTED_ROW_COUNTS = {
    "city_day_vs_median": 47_750,
    "top3_days_per_month": 18,
    "city_share_of_total": 264,
}


@pytest.fixture(scope="module")
def con():
    if not cfg.DUCKDB_PATH.exists():
        pytest.skip(f"DuckDB mart not built: {cfg.DUCKDB_PATH}")
    connection = duckdb.connect(str(cfg.DUCKDB_PATH), read_only=True)
    yield connection
    connection.close()


@pytest.fixture(scope="module")
def windows_queries():
    return sql_layer.parse_named_queries(sql_layer.WINDOWS_SQL)


def _frame(con, queries, name):
    sql = {query.name: query.sql for query in queries}[name]
    return con.execute(sql).df()


def test_parse_named_queries_has_twelve_unique_queries(windows_queries):
    names = [query.name for query in windows_queries]
    assert len(names) >= 12
    assert len(names) == len(set(names))
    assert all(query.sql.strip() for query in windows_queries)


def test_windows_sql_blocks_end_with_semicolon():
    text = sql_layer.WINDOWS_SQL.read_text(encoding="utf-8")
    blocks = re.split(r"^--\s*name:", text, flags=re.MULTILINE)[1:]
    assert len(blocks) >= 12
    for block in blocks:
        assert block.strip().endswith(";"), block.strip().splitlines()[0]


def test_window_queries_avoid_select_star(windows_queries):
    offenders = [
        query.name
        for query in windows_queries
        if re.search(r"\bSELECT\s+\*", query.sql, flags=re.IGNORECASE)
    ]
    assert offenders == []


def test_every_window_query_returns_rows(con, windows_queries):
    for query in windows_queries:
        frame = con.execute(query.sql).df()
        assert len(frame) > 0, f"{query.name} returned no rows"


def test_row_counts_match_expected_grain(con, windows_queries):
    for name, expected in EXPECTED_ROW_COUNTS.items():
        frame = _frame(con, windows_queries, name)
        assert len(frame) == expected, f"{name}: {len(frame)} rows, expected {expected}"


def test_active_clients_30d_is_bounded(con, windows_queries):
    frame = _frame(con, windows_queries, "active_clients_30d")
    assert len(frame) == 181
    last_day = frame.loc[frame["date_key"] == 20220630, "active_clients_30d"]
    assert len(last_day) == 1
    value = int(last_day.iloc[0])
    assert 0 < value <= TOTAL_CLIENTS
    assert value == ACTIVE_CLIENTS_20220630


def test_monthly_city_shares_sum_to_100(con, windows_queries):
    frame = _frame(con, windows_queries, "city_monthly_rank")
    shares = frame.groupby(["year", "month"])["month_share_pct"].sum()
    assert len(shares) == 6
    for total in shares:
        assert total == pytest.approx(100.0, abs=0.5)
    best_rank = frame.groupby(["year", "month"])["rnk"].min()
    assert (best_rank == 1).all()


def test_pareto_cumulative_share_is_monotonic(con, windows_queries):
    frame = _frame(con, windows_queries, "pareto_cities_cumulative_share")
    cumulative = frame["cumulative_volume_pct"].to_numpy()
    assert np.all(np.diff(cumulative) >= 0)
    assert cumulative[-1] == pytest.approx(100.0, abs=0.1)
    assert set(frame["pareto_bucket"]) == {"top_20pct", "rest"}
    top = frame[frame["pareto_bucket"] == "top_20pct"]
    assert top["pct_of_cities"].max() <= 20.0
    assert len(top) < len(frame)


def test_wow_growth_matches_formula(con, windows_queries):
    frame = _frame(con, windows_queries, "wow_growth_pct")
    assert len(frame) == 25  # only complete calendar weeks
    valid = frame.dropna(subset=["wow_growth_pct"])
    expected = 100.0 * (valid["qty"] - valid["prev_qty"]) / valid["prev_qty"]
    assert np.allclose(valid["wow_growth_pct"], expected, atol=0.02)


def test_optimization_pairs_return_identical_rows(con):
    pairs = sql_layer.parse_optimization_pairs()
    assert len(pairs) == 3
    for pair in pairs:
        slow = con.execute(pair.slow.sql).df().reset_index(drop=True)
        fast = con.execute(pair.fast.sql).df().reset_index(drop=True)
        pd.testing.assert_frame_equal(slow, fast, obj=pair.name)


def test_benchmark_writes_speedups(con, tmp_path):
    benchmark = sql_layer.benchmark_optimizations(con, out_dir=tmp_path, repeats=1)
    assert list(benchmark.columns) == sql_layer.OPTIMIZATION_COLUMNS
    assert len(benchmark) == 6

    report = tmp_path / sql_layer.OPTIMIZATION_FILE
    assert report.exists()
    persisted = pd.read_csv(report)
    assert list(persisted.columns) == sql_layer.OPTIMIZATION_COLUMNS
    assert set(persisted["variant"]) == {"slow", "fast"}

    fast = persisted[persisted["variant"] == "fast"]
    assert fast["speedup"].notna().all()
    assert (fast["speedup"] > 0).all()
    assert (persisted.groupby("pair")["rows"].nunique() == 1).all()


def test_run_windows_writes_csv_artifacts(con, tmp_path):
    timings = sql_layer.run_windows(con, out_dir=tmp_path)
    assert list(timings.columns) == sql_layer.TIMING_COLUMNS
    assert len(timings) >= 12
    assert (timings["rows"] > 0).all()
    assert (tmp_path / sql_layer.TIMINGS_FILE).exists()
    result_files = list((tmp_path / sql_layer.RESULTS_DIRNAME).glob("*.csv"))
    assert len(result_files) == len(timings)


def test_cli_main_writes_artifacts(con, tmp_path):
    exit_code = sql_layer.main(["--out-dir", str(tmp_path), "--repeats", "1"])
    assert exit_code == 0
    assert (tmp_path / sql_layer.TIMINGS_FILE).exists()
    assert (tmp_path / sql_layer.OPTIMIZATION_FILE).exists()
    assert len(list((tmp_path / sql_layer.RESULTS_DIRNAME).glob("*.csv"))) >= 12
