"""Hypothesis tests for the delivery control tower.

H1: the ``mp`` channel share grows faster than ``web`` (January vs June daily mix).
H2: weekend delivery time is higher than on weekdays (``mart_city_daily``).

Both tests report a Mann-Whitney U p-value and a bootstrap 95% CI of the effect.
A hypothesis is ``supported`` only when p < alpha AND the CI excludes zero;
otherwise the text says "тенденция, не вывод".
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import duckdb
import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from scipy import stats  # noqa: E402

from delivery import config as cfg  # noqa: E402

ALPHA = 0.05
BOOTSTRAP_N = 10_000
SEED = 42
CI_LEVEL = 95.0
RESULT_COLUMNS = [
    "hypothesis",
    "n_test",
    "n_control",
    "statistic",
    "p_value",
    "effect",
    "ci_low",
    "ci_high",
    "alpha",
    "supported",
    "note",
]


def channel_daily_shares(db_path: Path = cfg.DUCKDB_PATH) -> pd.DataFrame:
    """Daily channel volume for January and June plus the mp share of the mp+web pair."""
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        daily = con.execute(
            """
            SELECT d.date, d.month, ch.channel_name AS channel, SUM(f.qty) AS qty
            FROM fact_sales f
            JOIN dim_date d USING (date_key)
            JOIN dim_channel ch USING (channel_key)
            WHERE d.month IN (1, 6)
            GROUP BY 1, 2, 3
            """
        ).df()
    finally:
        con.close()

    wide = (
        daily.pivot_table(index=["date", "month"], columns="channel", values="qty", aggfunc="sum")
        .fillna(0.0)
        .reset_index()
    )
    pair = wide["mp"] + wide["web"]
    wide["mp_share_pair"] = np.where(pair > 0, wide["mp"] / pair.replace(0, np.nan), np.nan)
    return wide


def load_city_daily(db_path: Path = cfg.DUCKDB_PATH) -> pd.DataFrame:
    """City-day mart used by H2."""
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        return con.execute("SELECT * FROM mart_city_daily").df()
    finally:
        con.close()


def bootstrap_diff(
    test: np.ndarray | pd.Series,
    control: np.ndarray | pd.Series,
    statistic: Any = np.mean,
    n_boot: int = BOOTSTRAP_N,
    seed: int = SEED,
    batch: int = 200,
) -> tuple[float, float]:
    """Percentile bootstrap CI for ``statistic(test) - statistic(control)``.

    Resampling is vectorised in batches so 10k resamples over 30k+ rows stay fast.
    The CI is returned sorted, so ``low <= high`` always holds.
    """
    test_values = np.asarray(test, dtype=float)
    control_values = np.asarray(control, dtype=float)
    if test_values.size == 0 or control_values.size == 0:
        return float("nan"), float("nan")

    rng = np.random.default_rng(seed)
    diffs = np.empty(n_boot, dtype=float)
    done = 0
    while done < n_boot:
        size = min(batch, n_boot - done)
        idx_test = rng.integers(0, test_values.size, size=(size, test_values.size))
        idx_control = rng.integers(0, control_values.size, size=(size, control_values.size))
        resampled = statistic(test_values[idx_test], axis=1) - statistic(
            control_values[idx_control], axis=1
        )
        diffs[done : done + size] = resampled
        done += size

    tail = (100.0 - CI_LEVEL) / 2.0
    low, high = np.percentile(diffs, [tail, 100.0 - tail])
    return float(min(low, high)), float(max(low, high))


def _supported(p_value: float, ci_low: float, ci_high: float, alpha: float = ALPHA) -> bool:
    """A claim survives only with p < alpha and a CI that does not cross zero."""
    if not np.isfinite(p_value) or not np.isfinite(ci_low) or not np.isfinite(ci_high):
        return False
    return bool(p_value < alpha and (ci_low > 0.0 or ci_high < 0.0))


def test_h1(shares: pd.DataFrame, alpha: float = ALPHA) -> dict[str, Any]:
    """H1: the mp share of the mp+web mix is higher in June than in January."""
    data = shares.dropna(subset=["mp_share_pair"])
    january = data.loc[data["month"] == 1, "mp_share_pair"].to_numpy(dtype=float)
    june = data.loc[data["month"] == 6, "mp_share_pair"].to_numpy(dtype=float)
    if january.size == 0 or june.size == 0:
        return {
            "hypothesis": "H1: доля канала mp растёт быстрее, чем web",
            "n_test": int(june.size),
            "n_control": int(january.size),
            "statistic": float("nan"),
            "p_value": float("nan"),
            "effect": float("nan"),
            "ci_low": float("nan"),
            "ci_high": float("nan"),
            "alpha": alpha,
            "supported": False,
            "note": "тенденция, не вывод: нет данных за январь или июнь",
        }

    statistic, p_value = stats.mannwhitneyu(june, january, alternative="two-sided")
    effect = (float(june.mean()) - float(january.mean())) * 100.0
    ci_low, ci_high = bootstrap_diff(june, january, statistic=np.mean)
    ci_low *= 100.0
    ci_high *= 100.0
    supported = _supported(float(p_value), ci_low, ci_high, alpha)
    note = (
        f"Доля mp внутри пары mp+web: январь {january.mean():.1%} → июнь {june.mean():.1%} "
        f"({effect:+.1f} п.п.); доля web падает на те же {abs(effect):.1f} п.п. по построению."
    )
    if not supported:
        note = "тенденция, не вывод: " + note
    return {
        "hypothesis": "H1: доля канала mp растёт быстрее, чем web",
        "n_test": int(june.size),
        "n_control": int(january.size),
        "statistic": float(statistic),
        "p_value": float(p_value),
        "effect": effect,
        "ci_low": ci_low,
        "ci_high": ci_high,
        "alpha": alpha,
        "supported": supported,
        "note": note,
    }


def test_h2(city_daily: pd.DataFrame, alpha: float = ALPHA) -> dict[str, Any]:
    """H2: weekend city-day delivery time is higher than on weekdays."""
    data = city_daily.dropna(subset=["avg_time"])
    weekend = data.loc[data["is_weekend"] == 1, "avg_time"].to_numpy(dtype=float)
    weekday = data.loc[data["is_weekend"] == 0, "avg_time"].to_numpy(dtype=float)
    if weekend.size == 0 or weekday.size == 0:
        return {
            "hypothesis": "H2: время доставки в выходные выше, чем в будни",
            "n_test": int(weekend.size),
            "n_control": int(weekday.size),
            "statistic": float("nan"),
            "p_value": float("nan"),
            "effect": float("nan"),
            "ci_low": float("nan"),
            "ci_high": float("nan"),
            "alpha": alpha,
            "supported": False,
            "note": "тенденция, не вывод: нет наблюдений выходных или будней",
        }

    statistic, p_value = stats.mannwhitneyu(weekend, weekday, alternative="two-sided")
    median_weekend = float(np.median(weekend))
    median_weekday = float(np.median(weekday))
    effect = (median_weekend - median_weekday) / median_weekday * 100.0
    low, high = bootstrap_diff(weekend, weekday, statistic=np.median)
    scale = 100.0 / median_weekday
    ci_low, ci_high = sorted((low * scale, high * scale))
    supported = _supported(float(p_value), ci_low, ci_high, alpha)
    note = (
        f"Медиана на city-day: выходные {median_weekend:.2f} мин, будни {median_weekday:.2f} мин "
        f"({effect:+.2f}%), n = {weekend.size} / {weekday.size}."
    )
    if not supported:
        note = "тенденция, не вывод: " + note
    return {
        "hypothesis": "H2: время доставки в выходные выше, чем в будни",
        "n_test": int(weekend.size),
        "n_control": int(weekday.size),
        "statistic": float(statistic),
        "p_value": float(p_value),
        "effect": effect,
        "ci_low": ci_low,
        "ci_high": ci_high,
        "alpha": alpha,
        "supported": supported,
        "note": note,
    }


def results_frame(records: list[dict[str, Any]]) -> pd.DataFrame:
    """Normalise hypothesis records into the reporting schema."""
    return pd.DataFrame(records, columns=RESULT_COLUMNS)


def analyze(db_path: Path = cfg.DUCKDB_PATH) -> dict[str, Any]:
    """Run H1 and H2 against the mart and return the results plus plotting inputs."""
    shares = channel_daily_shares(db_path)
    city_daily = load_city_daily(db_path)
    results = results_frame([test_h1(shares), test_h2(city_daily)])
    return {"results": results, "shares": shares, "city_daily": city_daily}


def write_results(results: pd.DataFrame, reports_dir: Path = cfg.REPORTS) -> Path:
    """Write ``reports/hypothesis_results.csv``."""
    reports_dir.mkdir(parents=True, exist_ok=True)
    path = reports_dir / "hypothesis_results.csv"
    results.to_csv(path, index=False)
    return path


def _plot_channel_shares(shares: pd.DataFrame, path: Path) -> None:
    data = shares.dropna(subset=["mp_share_pair"])
    january = data.loc[data["month"] == 1, "mp_share_pair"].to_numpy(dtype=float) * 100.0
    june = data.loc[data["month"] == 6, "mp_share_pair"].to_numpy(dtype=float) * 100.0
    fig, ax = plt.subplots(figsize=(7, 5))
    ax.boxplot([january, june], tick_labels=["Январь", "Июнь"], widths=0.5)
    rng = np.random.default_rng(SEED)
    for position, values in ((1, january), (2, june)):
        jitter = rng.normal(position, 0.04, size=values.size)
        ax.scatter(jitter, values, s=12, alpha=0.6, color="#4C72B0")
    ax.set_ylabel("Доля mp в паре mp+web, %")
    ax.set_title("H1: доля mp по дням, январь и июнь")
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def _plot_weekend_time(city_daily: pd.DataFrame, path: Path) -> None:
    data = city_daily.dropna(subset=["avg_time"])
    weekday = data.loc[data["is_weekend"] == 0, "avg_time"].to_numpy(dtype=float)
    weekend = data.loc[data["is_weekend"] == 1, "avg_time"].to_numpy(dtype=float)
    fig, ax = plt.subplots(figsize=(7, 5))
    ax.boxplot(
        [weekday, weekend],
        tick_labels=["Будни", "Выходные"],
        widths=0.5,
        showfliers=False,
    )
    means = [float(np.mean(weekday)), float(np.mean(weekend))]
    ax.scatter([1, 2], means, color="#C44E52", zorder=3, label="Среднее")
    ax.legend()
    ax.set_ylabel("Среднее время доставки города за день, мин")
    ax.set_title("H2: время доставки в будни и выходные")
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def plot_hypotheses(shares: pd.DataFrame, city_daily: pd.DataFrame, figures_dir: Path = cfg.FIGURES) -> dict[str, Path]:
    """Persist the two hypothesis figures."""
    figures_dir.mkdir(parents=True, exist_ok=True)
    paths = {
        "channel_shares_png": figures_dir / "hypothesis_channel_shares.png",
        "weekend_time_png": figures_dir / "hypothesis_weekend_time.png",
    }
    _plot_channel_shares(shares, paths["channel_shares_png"])
    _plot_weekend_time(city_daily, paths["weekend_time_png"])
    return paths


def main() -> None:
    analysis = analyze()
    results = analysis["results"]
    path = write_results(results)
    figures = plot_hypotheses(analysis["shares"], analysis["city_daily"])

    for row in results.itertuples():
        verdict = "supported" if row.supported else "НЕ supported — тенденция, не вывод"
        print(
            f"{row.hypothesis}\n"
            f"  n: {row.n_test} vs {row.n_control}; U = {row.statistic:.1f}; "
            f"p = {row.p_value:.3e}; эффект = {row.effect:+.2f}; "
            f"95% CI [{row.ci_low:+.2f}; {row.ci_high:+.2f}]; {verdict}"
        )
        print(f"  {row.note}")

    print("\nФайлы:")
    print(f"  results_csv: {path}")
    for name, figure in figures.items():
        print(f"  {name}: {figure}")


if __name__ == "__main__":
    main()
