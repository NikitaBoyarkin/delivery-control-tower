"""Tests for delivery.bottlenecks."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from delivery import bottlenecks
from delivery import config as cfg


def _mart() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "city_name": ["A", "A", "B", "B"],
            "channel": ["mp", "web", "mp", "web"],
            "category": ["пицца", "пицца", "пицца", "пицца"],
            "qty": [100, 50, 30, 20],
            "cost": [1000.0, 400.0, 300.0, 200.0],
            "tickets": [10, 2, 3, 1],
            "weighted_avg_time": [30.0, 40.0, 35.0, 45.0],
        }
    )


def _city_daily(
    n_days: int = 120, seed: int = 0, city: str = "A", base: float = 30.0
) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    dates = pd.date_range("2022-01-01", periods=n_days)
    qty = rng.integers(50, 2000, n_days).astype(float)
    return pd.DataFrame(
        {
            "city_name": city,
            "date": dates,
            "day_of_week": dates.day_of_week,
            "is_weekend": (dates.day_of_week >= 5).astype(int),
            "qty": qty,
            "cost": qty * 40.0,
            "tickets": np.round(qty * 0.05),
            "clients_qty": np.round(qty * 0.7),
            "avg_time": base + 0.001 * qty + rng.normal(0.0, 0.2, n_days),
        }
    )


def test_channel_category_covers_all_volume():
    table = bottlenecks.channel_category_table(_mart())
    segments = table.loc[table["channel"] != "all"]
    assert segments["qty"].sum() == pytest.approx(_mart()["qty"].sum())
    assert set(bottlenecks.METRICS).issubset(table.columns)


def test_channel_category_metrics_bounded():
    table = bottlenecks.channel_category_table(_mart())
    segments = table.loc[table["channel"] != "all"]
    assert (segments["items_per_order"] > 1).all()
    assert "tickets_per_100" not in table.columns
    assert (segments["weighted_avg_time"] > 0).all()
    assert (segments["cost_per_item"] >= 0).all()
    assert segments["qty_share_pct"].between(0, 100).all()


def test_overall_row_has_zero_deviation():
    table = bottlenecks.channel_category_table(_mart())
    overall = table.loc[table["channel"] == "all"]
    assert len(overall) == 1
    for metric in bottlenecks.METRICS:
        assert overall[f"{metric}_dev_pct"].iloc[0] == pytest.approx(0.0)


def test_deviation_direction_matches_metric():
    table = bottlenecks.channel_category_table(_mart())
    mp_row = table.loc[table["channel"] == "mp"].iloc[0]
    web_row = table.loc[table["channel"] == "web"].iloc[0]
    assert mp_row["weighted_avg_time_dev_pct"] < 0 < web_row["weighted_avg_time_dev_pct"]


def test_deciles_cover_all_observations():
    city_daily = pd.concat(
        [_city_daily(seed=1), _city_daily(seed=2, city="B")], ignore_index=True
    )
    curve, correlation = bottlenecks.load_curve(city_daily)
    assert curve["n_obs"].sum() == len(city_daily)
    assert curve["decile"].is_monotonic_increasing
    assert curve["decile"].nunique() == bottlenecks.DECILES
    assert correlation["n"] == len(city_daily)


def test_spearman_correlation_reported():
    city_daily = _city_daily(seed=3)
    _, correlation = bottlenecks.load_curve(city_daily)
    assert -1.0 <= correlation["rho"] <= 1.0
    assert 0.0 <= correlation["p_value"] <= 1.0
    assert correlation["n"] == len(city_daily)


def test_city_ranking_requires_min_days():
    short = _city_daily(n_days=10, seed=4, city="short")
    long = _city_daily(n_days=100, seed=5, city="long")
    ranking = bottlenecks.city_ranking(pd.concat([short, long], ignore_index=True), min_days=60)
    assert ranking["city_name"].tolist() == ["long"]
    assert ranking["days"].iloc[0] == 100
    assert ranking["rank"].tolist() == [1]


def test_city_ranking_segments_top_and_bottom():
    frames = [_city_daily(seed=i, city=f"city{i}", base=29.0 + 0.1 * i) for i in range(30)]
    ranking = bottlenecks.city_ranking(pd.concat(frames, ignore_index=True))
    assert len(ranking) == 30
    assert ranking["degradation_index"].is_monotonic_decreasing
    assert (ranking["segment"] == "worst10").sum() == 10
    assert (ranking["segment"] == "best5").sum() == 5
    worst, best = bottlenecks.select_extremes(ranking)
    assert len(worst) == 10
    assert len(best) == 5


def test_empty_ranking_returns_schema():
    ranking = bottlenecks.city_ranking(_city_daily(n_days=5), min_days=60)
    assert ranking.empty
    assert list(ranking.columns) == bottlenecks.CITY_COLUMNS


def test_write_reports_writes_csv_and_figures(tmp_path):
    mart = _mart()
    city_daily = pd.concat(
        [_city_daily(seed=i, city=f"city{i}") for i in range(6)], ignore_index=True
    )
    channels = bottlenecks.channel_category_table(mart)
    curve, correlation = bottlenecks.load_curve(city_daily)
    cities = bottlenecks.city_ranking(city_daily)
    result = {
        "channels": channels,
        "load_curve": curve,
        "cities": cities,
        "spearman": correlation,
        "summary": bottlenecks.summarize(channels, curve, correlation, cities),
    }
    paths = bottlenecks.write_reports(result, reports_dir=tmp_path, figures_dir=tmp_path)
    assert paths["channels_csv"].exists()
    assert paths["cities_csv"].exists()
    assert paths["load_curve_csv"].exists()
    assert paths["load_curve_png"].exists()
    assert paths["city_slopes_png"].exists()
    assert paths["channel_category_png"].exists()
    reloaded = pd.read_csv(paths["load_curve_csv"])
    assert reloaded["n_obs"].sum() == len(city_daily)
    assert "Средневзвешенное время" in result["summary"]
    channels_reloaded = pd.read_csv(paths["channels_csv"])
    assert "items_per_order" in channels_reloaded.columns
    assert "tickets_per_100" not in channels_reloaded.columns
    cities_reloaded = pd.read_csv(paths["cities_csv"])
    assert "items_per_order" in cities_reloaded.columns
    assert "tickets_per_100" not in cities_reloaded.columns


@pytest.mark.skipif(not cfg.DUCKDB_PATH.exists(), reason="mart database is not built")
def test_analyze_on_real_mart():
    result = bottlenecks.analyze()
    assert len(result["channels"]) >= 12
    assert len(result["load_curve"]) == bottlenecks.DECILES
    assert len(result["cities"]) >= 50
    assert np.isfinite(result["spearman"]["rho"])
    assert "Spearman" in result["summary"]
    assert "tickets_per_100" not in result["channels"].columns
    assert "items_per_order" in result["cities"].columns
    assert "тикет" not in result["summary"].lower()


@pytest.mark.skipif(not cfg.REPORTS.exists(), reason="reports are not built")
def test_reports_have_no_obsolete_ticket_metric():
    files = list(cfg.REPORTS.glob("*.csv")) + [cfg.REPORTS / "dashboard.html"]
    present = [path for path in files if path.exists()]
    assert present, "no report artifacts found"
    for path in present:
        assert "tickets_per_100" not in path.read_text(encoding="utf-8")
