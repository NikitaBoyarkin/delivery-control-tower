"""Tests for delivery.hypothesis."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from delivery import config as cfg
from delivery import hypothesis


def _shares(
    jan_mean: float = 0.80, jun_mean: float = 0.86, seed: int = 0, n: int = 30
) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    january = pd.DataFrame(
        {
            "date": pd.date_range("2022-01-01", periods=n),
            "month": 1,
            "mp_share_pair": rng.normal(jan_mean, 0.01, n),
        }
    )
    june = pd.DataFrame(
        {
            "date": pd.date_range("2022-06-01", periods=n),
            "month": 6,
            "mp_share_pair": rng.normal(jun_mean, 0.01, n),
        }
    )
    return pd.concat([january, june], ignore_index=True)


def _city_daily(weekend_shift: float = 0.0, seed: int = 0, n: int = 300) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    weekend = np.array([0] * n + [1] * n)
    avg_time = rng.normal(30.0, 0.5, 2 * n)
    avg_time[n:] += weekend_shift
    return pd.DataFrame({"is_weekend": weekend, "avg_time": avg_time})


def test_h1_required_fields_and_supported():
    record = hypothesis.test_h1(_shares(jun_mean=0.86))
    assert set(hypothesis.RESULT_COLUMNS).issubset(record)
    assert record["n_test"] == 30
    assert record["n_control"] == 30
    assert 0.0 <= record["p_value"] <= 1.0
    assert record["effect"] > 0
    assert record["ci_low"] <= record["ci_high"]
    assert record["supported"] is True
    assert record["alpha"] == hypothesis.ALPHA


def test_h1_not_supported_when_groups_identical():
    shares = _shares()
    values = shares.loc[shares["month"] == 1, "mp_share_pair"].to_numpy()
    shares.loc[shares["month"] == 6, "mp_share_pair"] = values
    record = hypothesis.test_h1(shares)
    assert record["p_value"] >= hypothesis.ALPHA
    assert record["ci_low"] <= 0.0 <= record["ci_high"]
    assert record["supported"] is False
    assert record["note"].startswith("тенденция, не вывод")


def test_bootstrap_ci_is_monotone():
    rng = np.random.default_rng(1)
    test = rng.normal(31.0, 1.0, 200)
    control = rng.normal(30.0, 1.0, 200)
    for statistic in (np.mean, np.median):
        for seed in (0, 7):
            low, high = hypothesis.bootstrap_diff(test, control, statistic=statistic, seed=seed)
            assert low <= high
            assert np.isfinite(low) and np.isfinite(high)


def test_h2_detects_higher_weekend_time():
    record = hypothesis.test_h2(_city_daily(weekend_shift=1.0))
    assert set(hypothesis.RESULT_COLUMNS).issubset(record)
    assert record["p_value"] < hypothesis.ALPHA
    assert record["effect"] > 0
    assert record["ci_low"] > 0
    assert record["supported"] is True
    assert record["n_test"] == 300
    assert record["n_control"] == 300


def test_h2_not_supported_when_identical():
    values = np.random.default_rng(2).normal(30.0, 0.5, 300)
    city_daily = pd.DataFrame(
        {
            "is_weekend": np.array([0] * 300 + [1] * 300),
            "avg_time": np.concatenate([values, values]),
        }
    )
    record = hypothesis.test_h2(city_daily)
    assert record["p_value"] >= hypothesis.ALPHA
    assert record["ci_low"] <= 0.0 <= record["ci_high"]
    assert record["supported"] is False
    assert "тенденция, не вывод" in record["note"]


def test_h2_rejects_significant_effect_in_wrong_direction():
    """A significant drop contradicts "weekends are slower" — it must not read as support."""
    record = hypothesis.test_h2(_city_daily(weekend_shift=-1.0))
    assert record["p_value"] < hypothesis.ALPHA
    assert record["effect"] < 0
    assert record["ci_high"] < 0
    assert record["supported"] is False
    assert record["note"].startswith("эффект значим в обратную сторону")


def test_h1_rejects_significant_decline():
    record = hypothesis.test_h1(_shares(jan_mean=0.86, jun_mean=0.80))
    assert record["p_value"] < hypothesis.ALPHA
    assert record["effect"] < 0
    assert record["supported"] is False


def test_results_frame_and_csv_roundtrip(tmp_path):
    records = [
        hypothesis.test_h1(_shares()),
        hypothesis.test_h2(_city_daily(weekend_shift=0.5)),
    ]
    results = hypothesis.results_frame(records)
    assert list(results.columns) == hypothesis.RESULT_COLUMNS
    assert len(results) == 2
    path = hypothesis.write_results(results, reports_dir=tmp_path)
    assert path.exists()
    reloaded = pd.read_csv(path)
    assert len(reloaded) == 2
    assert set(hypothesis.RESULT_COLUMNS) == set(reloaded.columns)
    assert reloaded["supported"].dtype == bool


def test_plot_hypotheses_writes_figures(tmp_path):
    paths = hypothesis.plot_hypotheses(_shares(), _city_daily(), figures_dir=tmp_path)
    assert paths["channel_shares_png"].exists()
    assert paths["weekend_time_png"].exists()


@pytest.mark.skipif(not cfg.DUCKDB_PATH.exists(), reason="mart database is not built")
def test_analyze_on_real_mart():
    analysis = hypothesis.analyze()
    results = analysis["results"]
    assert len(results) == 2
    assert set(hypothesis.RESULT_COLUMNS) == set(results.columns)
    assert (results["alpha"] == hypothesis.ALPHA).all()
    assert results["p_value"].between(0, 1).all()
    assert (results["ci_low"] <= results["ci_high"]).all()
    assert "mp_share_pair" in analysis["shares"].columns
