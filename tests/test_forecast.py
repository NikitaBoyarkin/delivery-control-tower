import duckdb
import numpy as np
import pandas as pd
import pytest

from delivery import config as cfg
from delivery import forecast


def _series(days: int = 120, seed: int = 0) -> pd.Series:
    rng = np.random.default_rng(seed)
    index = pd.date_range("2022-01-03", periods=days, freq="D")
    weekly = np.resize(np.array([1.0, 1.1, 1.05, 1.0, 1.2, 1.35, 1.25]), days)
    trend = 1.0 + 0.01 * np.arange(days) / days
    values = 300_000 * weekly * trend + rng.normal(0.0, 2_000.0, days)
    return pd.Series(values, index=index, name="orders")


@pytest.mark.skipif(not cfg.DUCKDB_PATH.exists(), reason="mart database is not built")
def test_forecast_trains_on_tickets_not_positions():
    series = forecast.load_daily_total()
    assert series.name == "orders"
    con = duckdb.connect(str(cfg.DUCKDB_PATH), read_only=True)
    try:
        expected = con.execute("SELECT SUM(tickets) FROM mart_daily_total").fetchone()[0]
        positions = con.execute("SELECT SUM(qty) FROM mart_daily_total").fetchone()[0]
    finally:
        con.close()
    assert series.sum() == pytest.approx(float(expected))
    assert not series.sum() == pytest.approx(float(positions))


def test_wape_on_toy_series():
    assert forecast.wape([100, 100, 100, 100], [100, 100, 100, 100]) == pytest.approx(0.0)
    assert forecast.wape([100, 100, 100, 100], [110, 90, 100, 100]) == pytest.approx(0.05)
    assert np.isnan(forecast.wape([0, 0], [1, 1]))


def test_mase_is_positive_finite_and_neutral_for_seasonal_naive():
    series = _series()
    scale = forecast.mase_scale(series)
    assert np.isfinite(scale)
    assert scale > 0
    in_sample = forecast.mase(series.iloc[7:], series.iloc[:-7], scale)
    assert in_sample == pytest.approx(1.0)
    assert forecast.mase(series.iloc[7:], series.iloc[7:] * 1.1, scale) > 0


def test_feature_rows_ignore_future_values():
    series = _series()
    features, target = forecast.build_supervised(series)
    cutoff = features.index[10]
    position = series.index.get_loc(cutoff)

    mutated = series.copy()
    mutated.loc[mutated.index > cutoff] *= 5.0
    mutated_features, _ = forecast.build_supervised(mutated)

    pd.testing.assert_series_equal(features.loc[cutoff], mutated_features.loc[cutoff])
    assert features.loc[cutoff, "lag_1"] == pytest.approx(series.iloc[position - 1])
    assert features.loc[cutoff, "lag_7"] == pytest.approx(series.iloc[position - 7])
    assert features.loc[cutoff, "lag_28"] == pytest.approx(series.iloc[position - 28])
    assert features.loc[cutoff, "roll_mean_7"] == pytest.approx(
        series.iloc[position - 7 : position].mean()
    )
    assert target.loc[cutoff] == pytest.approx(series.loc[cutoff])


def test_test_window_does_not_leak_into_training():
    series = _series()
    folds = forecast.make_folds(series)
    assert folds
    assert folds[0].train.index.max() < folds[0].actual.index.min()

    mutated = series.copy()
    mutated.loc[folds[0].actual.index] *= 3.0
    mutated_folds = forecast.make_folds(mutated)

    clean = forecast.forecast_all_models(folds[0].train, len(folds[0].actual))
    dirty = forecast.forecast_all_models(mutated_folds[0].train, len(mutated_folds[0].actual))
    for model in forecast.MODELS:
        np.testing.assert_allclose(clean[model], dirty[model])


def test_all_models_and_horizons_present():
    predictions = forecast.run_backtest(_series(), "total")
    metrics = forecast.build_metrics(predictions)

    pairs = set(zip(metrics["model"], metrics["horizon"], strict=True))
    expected = {(model, horizon) for model in forecast.MODELS for horizon in forecast.HORIZONS}
    assert expected <= pairs
    assert len(metrics) == len(forecast.MODELS) * len(forecast.HORIZONS)
    assert np.isfinite(metrics["wape"]).all()
    assert (metrics["wape"] >= 0).all()
    assert np.isfinite(metrics["mase"]).all()
    assert (metrics["mase"] > 0).all()
    assert (metrics["n_folds"] >= 3).all()


def test_best_model_is_deterministic():
    metrics = forecast.build_metrics(forecast.run_backtest(_series(), "total"))
    first = forecast.select_best_model(metrics, horizon=7)
    assert first == forecast.select_best_model(metrics, horizon=7)

    tie = pd.DataFrame(
        [
            {"model": "zeta", "horizon": 7, "wape": 0.1, "mase": 0.5},
            {"model": "alpha", "horizon": 7, "wape": 0.1, "mase": 0.5},
        ]
    )
    assert forecast.select_best_model(tie, horizon=7) == "alpha"
    with pytest.raises(ValueError):
        forecast.select_best_model(tie, horizon=3)


def test_backtest_skips_short_series():
    short = _series(days=40)
    assert forecast.make_folds(short) == []
    predictions = forecast.run_backtest(short, "short")
    assert predictions.empty
    assert list(predictions.columns) == list(forecast.PREDICTION_COLUMNS)
    metrics = forecast.build_metrics(predictions)
    assert metrics.empty
    assert list(metrics.columns) == list(forecast.METRIC_COLUMNS)


def test_baselines_match_definitions():
    series = _series(days=30)
    np.testing.assert_allclose(forecast.forecast_naive(series, 5), np.full(5, series.iloc[-1]))
    weekly = forecast.forecast_seasonal_naive(series, 10)
    last_week = series.iloc[-7:].to_numpy()
    np.testing.assert_allclose(weekly[:7], last_week)
    np.testing.assert_allclose(weekly[7:], last_week[:3])


def test_outputs_and_plots_are_written(tmp_path):
    predictions = forecast.run_backtest(_series(), "total")
    metrics = forecast.build_metrics(predictions)
    metrics.insert(0, "series", "total")
    metrics.insert(0, "level", "total")

    paths = forecast.write_outputs(metrics, predictions, reports_dir=tmp_path)
    assert paths["metrics"].exists()
    assert paths["predictions"].exists()
    assert len(pd.read_csv(paths["metrics"])) == len(metrics)
    assert len(pd.read_csv(paths["predictions"])) == len(predictions)

    best = forecast.select_best_model(metrics, horizon=7)
    figure = forecast.plot_test_period(predictions, best, path=tmp_path / "forecast_test.png")
    wape_figure = forecast.plot_wape(metrics, horizon=7, path=tmp_path / "forecast_wape.png")
    assert figure.exists() and figure.stat().st_size > 0
    assert wape_figure.exists() and wape_figure.stat().st_size > 0
