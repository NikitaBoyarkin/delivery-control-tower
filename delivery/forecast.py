"""Daily order-count forecast: baselines, Holt-Winters and gradient boosting.

The backtest is rolling-origin: the last 28 days form the test window, the training
window expands, and every model is refitted every 7 days (four folds). Horizons 1, 7
and 14 are scored on the errors accumulated over the first h days of each fold.
Feature rows for day t are built only from observations strictly before t.
"""

from __future__ import annotations

import warnings
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

import duckdb
import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from sklearn.ensemble import HistGradientBoostingRegressor  # noqa: E402
from statsmodels.tools.sm_exceptions import ConvergenceWarning  # noqa: E402
from statsmodels.tsa.holtwinters import ExponentialSmoothing  # noqa: E402

from delivery import config as cfg  # noqa: E402

MODELS: tuple[str, ...] = ("naive", "seasonal_naive", "holt_winters", "hgb")
HORIZONS: tuple[int, ...] = (1, 7, 14)
TEST_DAYS = 28
REFIT_EVERY = 7
SEASONAL_PERIOD = 7
LAGS: tuple[int, ...] = (1, 7, 14, 28)
ROLLING_WINDOWS: tuple[int, ...] = (7, 14)
MIN_TRAIN = max(LAGS) + 4 * SEASONAL_PERIOD
TOP_CITIES = 10
MULTIPLICATIVE_SWING = 0.25

FEATURE_COLUMNS: tuple[str, ...] = (
    *(f"lag_{lag}" for lag in LAGS),
    *(f"roll_mean_{window}" for window in ROLLING_WINDOWS),
    "day_of_week",
    "is_weekend",
    "month",
    "day_of_month",
)

PREDICTION_COLUMNS: tuple[str, ...] = (
    "series",
    "model",
    "fold",
    "origin",
    "date",
    "step",
    "y_true",
    "y_pred",
    "mase_scale",
)

METRIC_COLUMNS: tuple[str, ...] = (
    "model",
    "horizon",
    "wape",
    "mase",
    "mae",
    "n_folds",
    "n_obs",
)


@dataclass(frozen=True)
class Fold:
    """One rolling origin: everything up to `origin` trains, the rest is scored."""

    origin: pd.Timestamp
    train: pd.Series
    actual: pd.Series


def _connect(db_path: Path) -> duckdb.DuckDBPyConnection:
    return duckdb.connect(str(db_path), read_only=True)


def load_daily_total(db_path: Path = cfg.DUCKDB_PATH) -> pd.Series:
    """Total daily orders (``tickets``, not positions) across all cities, indexed by date."""
    con = _connect(db_path)
    try:
        frame = con.execute("SELECT date, tickets FROM mart_daily_total ORDER BY date").df()
    finally:
        con.close()
    index = pd.to_datetime(frame["date"])
    series = pd.Series(frame["tickets"].to_numpy(dtype=float), index=index, name="orders")
    return series.asfreq("D")


def top_cities(n: int = TOP_CITIES, db_path: Path = cfg.DUCKDB_PATH) -> list[str]:
    con = _connect(db_path)
    try:
        frame = con.execute(
            """
            SELECT city_name, SUM(tickets) AS orders
            FROM mart_city_daily
            GROUP BY city_name
            ORDER BY orders DESC, city_name
            LIMIT ?
            """,
            [n],
        ).df()
    finally:
        con.close()
    return frame["city_name"].tolist()


def load_city_panel(cities: Sequence[str], db_path: Path = cfg.DUCKDB_PATH) -> pd.DataFrame:
    """Wide city panel of daily orders (date x city); days without orders are zero-filled."""
    con = _connect(db_path)
    try:
        frame = con.execute("SELECT city_name, date, tickets FROM mart_city_daily").df()
    finally:
        con.close()
    frame = frame.loc[frame["city_name"].isin(list(cities))]
    frame["date"] = pd.to_datetime(frame["date"])
    panel = frame.pivot(index="date", columns="city_name", values="tickets").sort_index()
    full_range = pd.date_range(panel.index.min(), panel.index.max(), freq="D")
    return panel.reindex(full_range, fill_value=0.0).loc[:, list(cities)]


def make_feature_row(history: Sequence[float], date: pd.Timestamp) -> dict[str, float]:
    """Feature row for `date` using only observations strictly before it."""
    values = np.asarray(history, dtype=float)
    if len(values) < max(LAGS):
        raise ValueError(f"history needs at least {max(LAGS)} observations, got {len(values)}")
    row: dict[str, float] = {f"lag_{lag}": float(values[-lag]) for lag in LAGS}
    for window in ROLLING_WINDOWS:
        row[f"roll_mean_{window}"] = float(values[-window:].mean())
    weekday = date.day_of_week
    row["day_of_week"] = float(weekday)
    row["is_weekend"] = float(weekday >= 5)
    row["month"] = float(date.month)
    row["day_of_month"] = float(date.day)
    return row


def build_supervised(series: pd.Series) -> tuple[pd.DataFrame, pd.Series]:
    """Supervised frame where the row for day t carries only values from before t."""
    values = series.to_numpy(dtype=float)
    start = max(LAGS)
    rows = [make_feature_row(values[:i], series.index[i]) for i in range(start, len(values))]
    index = series.index[start:]
    features = pd.DataFrame(rows, index=index, columns=list(FEATURE_COLUMNS))
    target = pd.Series(values[start:], index=index, name="target")
    return features, target


def forecast_naive(series: pd.Series, horizon: int) -> np.ndarray:
    return np.full(horizon, float(series.iloc[-1]), dtype=float)


def forecast_seasonal_naive(series: pd.Series, horizon: int) -> np.ndarray:
    values = series.to_numpy(dtype=float)
    if len(values) < SEASONAL_PERIOD:
        raise ValueError("seasonal-naive needs at least one full season of history")
    working = list(values)
    predictions: list[float] = []
    for _ in range(horizon):
        value = working[-SEASONAL_PERIOD]
        predictions.append(value)
        working.append(value)
    return np.asarray(predictions, dtype=float)


def seasonal_mode(series: pd.Series) -> str:
    """Additive seasonality unless the weekly swing scales with the level."""
    weekly = series.groupby(series.index.day_of_week).mean()
    swing = float((weekly.max() - weekly.min()) / weekly.mean())
    strictly_positive = bool((series > 0).all())
    return "mul" if strictly_positive and swing > MULTIPLICATIVE_SWING else "add"


def forecast_holt_winters(series: pd.Series, horizon: int) -> np.ndarray:
    model = ExponentialSmoothing(
        series.to_numpy(dtype=float),
        trend="add",
        seasonal=seasonal_mode(series),
        seasonal_periods=SEASONAL_PERIOD,
        initialization_method="estimated",
    )
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", ConvergenceWarning)
        fitted = model.fit(optimized=True)
    return np.maximum(np.asarray(fitted.forecast(horizon), dtype=float), 0.0)


def fit_hgb(series: pd.Series) -> tuple[HistGradientBoostingRegressor, list[float]]:
    features, target = build_supervised(series)
    model = HistGradientBoostingRegressor(
        max_iter=300,
        learning_rate=0.05,
        min_samples_leaf=5,
        l2_regularization=1.0,
        early_stopping=False,
        random_state=0,
    )
    model.fit(features, target)
    return model, list(series.to_numpy(dtype=float))


def forecast_hgb(series: pd.Series, horizon: int) -> np.ndarray:
    """Recursive one-step model: each prediction is fed back as the next lag."""
    model, history = fit_hgb(series)
    origin = series.index[-1]
    predictions: list[float] = []
    for step in range(1, horizon + 1):
        date = origin + pd.Timedelta(days=step)
        row = make_feature_row(history, date)
        frame = pd.DataFrame([row], columns=list(FEATURE_COLUMNS))
        predictions.append(max(float(model.predict(frame)[0]), 0.0))
        history.append(predictions[-1])
    return np.asarray(predictions, dtype=float)


FORECASTERS: dict[str, Callable[[pd.Series, int], np.ndarray]] = {
    "naive": forecast_naive,
    "seasonal_naive": forecast_seasonal_naive,
    "holt_winters": forecast_holt_winters,
    "hgb": forecast_hgb,
}


def forecast_all_models(series: pd.Series, horizon: int) -> dict[str, np.ndarray]:
    return {model: FORECASTERS[model](series, horizon) for model in MODELS}


def make_folds(
    series: pd.Series,
    test_days: int = TEST_DAYS,
    refit_every: int = REFIT_EVERY,
) -> list[Fold]:
    """Expanding-window folds; returns an empty list when history is too short."""
    if test_days < 1 or len(series) < MIN_TRAIN + 1:
        return []
    origin = len(series) - test_days - 1
    if origin + 1 < MIN_TRAIN:
        return []
    folds: list[Fold] = []
    while origin < len(series) - 1:
        available = len(series) - 1 - origin
        if available < min(HORIZONS):
            break
        horizon = min(max(HORIZONS), available)
        folds.append(
            Fold(
                origin=series.index[origin],
                train=series.iloc[: origin + 1],
                actual=series.iloc[origin + 1 : origin + 1 + horizon],
            )
        )
        origin += refit_every
    return folds


def mase_scale(series: pd.Series, period: int = SEASONAL_PERIOD) -> float:
    """In-sample seasonal-naive MAE, the denominator of MASE."""
    values = series.to_numpy(dtype=float)
    if len(values) <= period:
        return float("nan")
    return float(np.abs(values[period:] - values[:-period]).mean())


def wape(y_true: Sequence[float], y_pred: Sequence[float]) -> float:
    actual = np.asarray(y_true, dtype=float)
    predicted = np.asarray(y_pred, dtype=float)
    denominator = float(np.abs(actual).sum())
    if denominator == 0.0 or not np.isfinite(denominator):
        return float("nan")
    return float(np.abs(actual - predicted).sum() / denominator)


def mase(y_true: Sequence[float], y_pred: Sequence[float], scale: float) -> float:
    if not np.isfinite(scale) or scale <= 0.0:
        return float("nan")
    actual = np.asarray(y_true, dtype=float)
    predicted = np.asarray(y_pred, dtype=float)
    return float(np.abs(actual - predicted).mean() / scale)


def run_backtest(series: pd.Series, series_name: str = "total") -> pd.DataFrame:
    """Score every model on every fold; returns tidy per-day predictions."""
    rows: list[dict[str, object]] = []
    for fold_id, fold in enumerate(make_folds(series)):
        horizon = len(fold.actual)
        scale = mase_scale(fold.train)
        forecasts = forecast_all_models(fold.train, horizon)
        actual_values = fold.actual.to_numpy(dtype=float)
        for model in MODELS:
            for step, date in enumerate(fold.actual.index, start=1):
                rows.append(
                    {
                        "series": series_name,
                        "model": model,
                        "fold": fold_id,
                        "origin": fold.origin,
                        "date": date,
                        "step": step,
                        "y_true": float(actual_values[step - 1]),
                        "y_pred": float(forecasts[model][step - 1]),
                        "mase_scale": scale,
                    }
                )
    return pd.DataFrame(rows, columns=list(PREDICTION_COLUMNS))


def build_metrics(predictions: pd.DataFrame) -> pd.DataFrame:
    """WAPE and MASE per (model, horizon) over the first h days of every fold."""
    if predictions.empty:
        return pd.DataFrame(columns=list(METRIC_COLUMNS))
    rows: list[dict[str, object]] = []
    for model in MODELS:
        model_predictions = predictions.loc[predictions["model"] == model]
        if model_predictions.empty:
            continue
        for horizon in HORIZONS:
            window = model_predictions.loc[model_predictions["step"] <= horizon]
            if window.empty:
                continue
            fold_mae: list[float] = []
            fold_scale: list[float] = []
            for _, fold_window in window.groupby("fold", sort=True):
                errors = (fold_window["y_true"] - fold_window["y_pred"]).abs().to_numpy()
                fold_mae.append(float(errors.mean()))
                fold_scale.append(float(fold_window["mase_scale"].iloc[0]))
            scale = float(np.mean(fold_scale))
            rows.append(
                {
                    "model": model,
                    "horizon": horizon,
                    "wape": wape(window["y_true"], window["y_pred"]),
                    "mase": float(np.mean(fold_mae) / scale) if scale > 0 else float("nan"),
                    "mae": float(np.mean(fold_mae)),
                    "n_folds": int(window["fold"].nunique()),
                    "n_obs": int(len(window)),
                }
            )
    return pd.DataFrame(rows, columns=list(METRIC_COLUMNS))


def select_best_model(metrics: pd.DataFrame, horizon: int = 7) -> str:
    """Lowest WAPE, then MASE, then model name — fully deterministic."""
    window = metrics.loc[metrics["horizon"] == horizon]
    if window.empty:
        raise ValueError(f"no metrics for horizon {horizon}")
    ordered = window.sort_values(["wape", "mase", "model"], kind="mergesort")
    return str(ordered.iloc[0]["model"])


def _tag(metrics: pd.DataFrame, level: str, series: str) -> pd.DataFrame:
    tagged = metrics.copy()
    tagged.insert(0, "series", series)
    tagged.insert(0, "level", level)
    return tagged


def run_total_forecast(db_path: Path = cfg.DUCKDB_PATH) -> tuple[pd.DataFrame, pd.DataFrame]:
    series = load_daily_total(db_path)
    predictions = run_backtest(series, "total")
    return _tag(build_metrics(predictions), "total", "total"), predictions


def run_city_forecasts(
    cities: Sequence[str] | None = None,
    db_path: Path = cfg.DUCKDB_PATH,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    names = list(cities) if cities is not None else top_cities(db_path=db_path)
    panel = load_city_panel(names, db_path)
    metric_parts: list[pd.DataFrame] = []
    prediction_parts: list[pd.DataFrame] = []
    for city in names:
        predictions = run_backtest(panel[city].rename(city), city)
        if predictions.empty:
            continue
        prediction_parts.append(predictions)
        metric_parts.append(_tag(build_metrics(predictions), "city", city))
    metrics = (
        pd.concat(metric_parts, ignore_index=True)
        if metric_parts
        else pd.DataFrame(columns=["level", "series", *METRIC_COLUMNS])
    )
    predictions = (
        pd.concat(prediction_parts, ignore_index=True)
        if prediction_parts
        else pd.DataFrame(columns=list(PREDICTION_COLUMNS))
    )
    return metrics, predictions


def write_outputs(
    metrics: pd.DataFrame,
    predictions: pd.DataFrame,
    reports_dir: Path = cfg.REPORTS,
) -> dict[str, Path]:
    reports_dir.mkdir(parents=True, exist_ok=True)
    paths = {
        "metrics": reports_dir / "forecast_metrics.csv",
        "predictions": reports_dir / "forecast_predictions.csv",
    }
    metrics.to_csv(paths["metrics"], index=False)
    predictions.to_csv(paths["predictions"], index=False)
    return paths


def plot_test_period(
    predictions: pd.DataFrame,
    model: str,
    path: Path | None = None,
) -> Path:
    """Actual orders against the chosen model's fold forecasts on the test window."""
    target = path or cfg.FIGURES / f"forecast_test_period_{model}.png"
    target.parent.mkdir(parents=True, exist_ok=True)
    subset = predictions.loc[predictions["model"] == model].copy()
    subset["date"] = pd.to_datetime(subset["date"])
    subset["origin"] = pd.to_datetime(subset["origin"])
    fig, ax = plt.subplots(figsize=(11, 5))
    actual = subset.drop_duplicates("date").sort_values("date")
    ax.plot(actual["date"], actual["y_true"], color="black", linewidth=2.0, label="факт")
    for fold_id, fold in subset.groupby("fold", sort=True):
        fold = fold.sort_values("date")
        ax.plot(
            fold["date"],
            fold["y_pred"],
            marker="o",
            markersize=3,
            linewidth=1.4,
            label=f"прогноз, фолд {int(fold_id) + 1}",
        )
        ax.axvline(fold["origin"].iloc[0], color="grey", linestyle=":", linewidth=0.8)
    ax.set_title(f"Заказы: факт vs прогноз заказов, модель {model} (тестовое окно)")
    ax.set_ylabel("Заказы, шт.")
    ax.grid(alpha=0.3)
    ax.legend(fontsize=8, ncol=2)
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(target, dpi=150)
    plt.close(fig)
    return target


def plot_wape(metrics: pd.DataFrame, horizon: int = 7, path: Path | None = None) -> Path:
    target = path or cfg.FIGURES / f"forecast_wape_h{horizon}.png"
    target.parent.mkdir(parents=True, exist_ok=True)
    window = metrics.loc[
        (metrics["level"] == "total") & (metrics["horizon"] == horizon)
    ].sort_values("wape", ascending=True)
    fig, ax = plt.subplots(figsize=(8, 4.5))
    positions = np.arange(len(window))
    ax.barh(positions, window["wape"], color="#4c72b0")
    ax.set_yticks(positions, labels=window["model"].tolist())
    for position, value in zip(positions, window["wape"], strict=True):
        ax.text(value, position, f" {value:.2%}", va="center", fontsize=9)
    ax.set_xlabel("WAPE (lower is better)")
    ax.set_title(f"Forecast accuracy by model, horizon {horizon} days")
    ax.grid(axis="x", alpha=0.3)
    fig.tight_layout()
    fig.savefig(target, dpi=150)
    plt.close(fig)
    return target


def _print_report(
    total_metrics: pd.DataFrame,
    city_metrics: pd.DataFrame,
    paths: dict[str, Path],
    figures: list[Path],
) -> None:
    print("Orders forecast: rolling-origin backtest")
    print(
        f"test window: last {TEST_DAYS} days, refit every {REFIT_EVERY} days, horizons {HORIZONS}"
    )
    folds = int(total_metrics["n_folds"].max()) if not total_metrics.empty else 0
    print(f"folds per horizon (max): {folds}")

    wape_pivot = total_metrics.pivot(index="model", columns="horizon", values="wape")
    mase_pivot = total_metrics.pivot(index="model", columns="horizon", values="mase")
    print("\nWAPE by model and horizon (total orders):")
    print(wape_pivot.to_string(float_format=lambda value: f"{value:.2%}"))
    print("\nMASE by model and horizon (total orders, seasonal-naive scale on train):")
    print(mase_pivot.to_string(float_format=lambda value: f"{value:.3f}"))

    baseline = total_metrics.loc[total_metrics["model"] == "seasonal_naive"].set_index("horizon")
    print("\nSeasonal-naive comparison (total orders):")
    for model in MODELS:
        if model == "seasonal_naive":
            continue
        model_rows = total_metrics.loc[total_metrics["model"] == model].set_index("horizon")
        wins = [
            horizon
            for horizon in HORIZONS
            if horizon in model_rows.index
            and horizon in baseline.index
            and model_rows.loc[horizon, "wape"] < baseline.loc[horizon, "wape"]
        ]
        if len(wins) == len(HORIZONS):
            verdict = "beats seasonal-naive on every horizon"
        elif not wins:
            verdict = "does NOT beat seasonal-naive at any horizon"
        else:
            verdict = f"beats seasonal-naive only at horizons {wins}"
        print(f"  {model}: {verdict}")

    best = select_best_model(total_metrics, horizon=7)
    print(f"\nbest model at horizon 7: {best}")

    if not city_metrics.empty:
        city_window = city_metrics.loc[city_metrics["horizon"] == 7]
        city_pivot = city_window.pivot(index="series", columns="model", values="wape")
        print("\nWAPE by city at horizon 7 (top cities by orders):")
        print(city_pivot.to_string(float_format=lambda value: f"{value:.2%}"))
        baseline_by_city = city_window.loc[city_window["model"] == "seasonal_naive"].set_index(
            "series"
        )["wape"]
        others = city_window.loc[city_window["model"] != "seasonal_naive"]
        improved = others.loc[
            others.apply(
                lambda row: row["wape"] < baseline_by_city.get(row["series"], np.inf),
                axis=1,
            )
        ]
        print(
            "cities where some model beats seasonal-naive at horizon 7: "
            f"{improved['series'].nunique()} of {city_window['series'].nunique()}"
        )

    print("\nartifacts:")
    for name, path in paths.items():
        print(f"  {name}: {path}")
    for figure in figures:
        print(f"  figure: {figure}")


def main() -> None:
    total_metrics, total_predictions = run_total_forecast()
    if total_metrics.empty:
        raise RuntimeError("not enough history for a rolling-origin backtest")
    city_metrics, city_predictions = run_city_forecasts()

    metrics = pd.concat([total_metrics, city_metrics], ignore_index=True)
    predictions = pd.concat([total_predictions, city_predictions], ignore_index=True)
    paths = write_outputs(metrics, predictions)

    best = select_best_model(total_metrics, horizon=7)
    figures = [
        plot_test_period(total_predictions, best),
        plot_wape(total_metrics, horizon=7),
    ]
    _print_report(total_metrics, city_metrics, paths, figures)


if __name__ == "__main__":
    main()
