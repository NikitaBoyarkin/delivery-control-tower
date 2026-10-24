"""Bottleneck analysis: time decomposition, load curve, city degradation ranking.

Three views of the same question - where does the delivery process lose time:

1. ``channel_category_table`` - weighted time / cost / items per order by channel x
   category, with deviation from the overall average.
2. ``load_curve`` - daily city volume split into deciles, mean delivery time per decile
   plus a pooled Spearman correlation between volume and time.
3. ``city_ranking`` - per-city OLS slope of ``avg_time ~ qty`` (sec per 1000 orders)
   and a composite degradation index.

``analyze`` returns the three frames plus a numeric summary; ``write_reports`` persists
CSVs (``reports/bottleneck_*.csv``) and figures (``reports/figures/bottleneck_*.png``).
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

MIN_CITY_DAYS = 60
DECILES = 10
METRICS = ("weighted_avg_time", "cost_per_item", "items_per_order")

CHANNEL_CATEGORY_COLUMNS = [
    "channel",
    "category",
    "qty",
    "weighted_avg_time",
    "cost_per_item",
    "items_per_order",
    "qty_share_pct",
    "weighted_avg_time_dev_pct",
    "cost_per_item_dev_pct",
    "items_per_order_dev_pct",
]
LOAD_CURVE_COLUMNS = [
    "decile",
    "qty_min",
    "qty_max",
    "mean_qty",
    "mean_avg_time",
    "median_avg_time",
    "n_obs",
]
CITY_COLUMNS = [
    "rank",
    "city_name",
    "days",
    "slope_sec_per_1000",
    "slope_r2",
    "weighted_avg_time",
    "cost_per_item",
    "items_per_order",
    "qty",
    "degradation_index",
    "segment",
]


def load_frames(db_path: Path = cfg.DUCKDB_PATH) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Read ``mart_delivery`` and ``mart_city_daily`` from the DuckDB mart (read-only)."""
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        mart = con.execute("SELECT * FROM mart_delivery").df()
        city_daily = con.execute("SELECT * FROM mart_city_daily").df()
    finally:
        con.close()
    return mart, city_daily


def _overall_metrics(mart: pd.DataFrame) -> dict[str, float]:
    qty = float(mart["qty"].sum())
    time_num = float((mart["weighted_avg_time"] * mart["qty"]).sum())
    return {
        "qty": qty,
        "weighted_avg_time": time_num / qty,
        "cost_per_item": float(mart["cost"].sum()) / qty,
        "items_per_order": qty / float(mart["tickets"].sum()),
    }


def channel_category_table(mart: pd.DataFrame) -> pd.DataFrame:
    """Channel x category time/cost/items-per-order table with deviation from the average.

    Deviation columns are percentages: ``(value / overall - 1) * 100``. The final row
    (``channel == "all"``) carries the overall averages and zero deviations.
    """
    if mart.empty:
        return pd.DataFrame(columns=CHANNEL_CATEGORY_COLUMNS)

    overall = _overall_metrics(mart)
    work = mart.assign(_time_num=mart["weighted_avg_time"] * mart["qty"])
    grouped = (
        work.groupby(["channel", "category"], as_index=False)
        .agg(
            qty=("qty", "sum"),
            cost=("cost", "sum"),
            tickets=("tickets", "sum"),
            time_num=("_time_num", "sum"),
        )
        .assign(
            weighted_avg_time=lambda d: d["time_num"] / d["qty"],
            cost_per_item=lambda d: d["cost"] / d["qty"],
            items_per_order=lambda d: d["qty"] / d["tickets"],
            qty_share_pct=lambda d: 100.0 * d["qty"] / overall["qty"],
        )
        .drop(columns=["cost", "tickets", "time_num"])
    )

    overall_row = pd.DataFrame(
        [
            {
                "channel": "all",
                "category": "all",
                "qty": overall["qty"],
                "weighted_avg_time": overall["weighted_avg_time"],
                "cost_per_item": overall["cost_per_item"],
                "items_per_order": overall["items_per_order"],
                "qty_share_pct": 100.0,
            }
        ]
    )
    table = pd.concat([overall_row, grouped], ignore_index=True, sort=False)
    for metric in METRICS:
        table[f"{metric}_dev_pct"] = (table[metric] / overall[metric] - 1.0) * 100.0
    table.loc[table["channel"] == "all", [f"{m}_dev_pct" for m in METRICS]] = 0.0
    table = table.sort_values("qty", ascending=False, kind="stable").reset_index(drop=True)
    return table[CHANNEL_CATEGORY_COLUMNS]


def load_curve(city_daily: pd.DataFrame, deciles: int = DECILES) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Split city-day volume into deciles; return per-decile time stats and Spearman.

    All finite ``(qty, avg_time)`` observations land in exactly one decile, so
    ``n_obs`` sums to the number of valid rows. Ties in ``qty`` cannot move an
    observation out of the curve: ``qcut`` uses ``duplicates="drop"`` and every row
    still receives a bin label.
    """
    df = city_daily.loc[:, ["qty", "avg_time"]].dropna()
    df = df[np.isfinite(df["qty"]) & np.isfinite(df["avg_time"])]
    if df.empty:
        return pd.DataFrame(columns=LOAD_CURVE_COLUMNS), {
            "rho": float("nan"),
            "p_value": float("nan"),
            "n": 0,
            "n_bins": 0,
        }

    bins = pd.qcut(df["qty"], q=deciles, labels=False, duplicates="drop")
    df = df.assign(decile=bins.astype(int) + 1)
    curve = (
        df.groupby("decile", as_index=False)
        .agg(
            qty_min=("qty", "min"),
            qty_max=("qty", "max"),
            mean_qty=("qty", "mean"),
            mean_avg_time=("avg_time", "mean"),
            median_avg_time=("avg_time", "median"),
            n_obs=("qty", "size"),
        )
        .sort_values("decile")
        .reset_index(drop=True)
    )
    rho, p_value = stats.spearmanr(df["qty"], df["avg_time"])
    correlation = {
        "rho": float(rho),
        "p_value": float(p_value),
        "n": int(len(df)),
        "n_bins": int(curve["decile"].nunique()),
    }
    return curve[LOAD_CURVE_COLUMNS], correlation


def _zscore(series: pd.Series) -> pd.Series:
    std = float(series.std(ddof=0))
    if not np.isfinite(std) or std == 0.0:
        return pd.Series(0.0, index=series.index)
    return (series - series.mean()) / std


def city_ranking(city_daily: pd.DataFrame, min_days: int = MIN_CITY_DAYS) -> pd.DataFrame:
    """Rank cities by a composite degradation index.

    A city qualifies only with ``min_days`` distinct active days and non-constant
    ``qty``; the OLS slope of ``avg_time ~ qty`` is converted to seconds per 1000
    positions. ``degradation_index`` is the mean of three z-scores (slope,
    qty-weighted mean time, cost per position) - higher means a more strained city. The frame is sorted
    worst-first, ``rank`` starts at 1, and ``segment`` marks the worst 10 / best 5.
    """
    records: list[dict[str, Any]] = []
    for city, group in city_daily.groupby("city_name", sort=False):
        days = int(group["date"].nunique())
        if days < min_days:
            continue
        x = group["qty"].to_numpy(dtype=float)
        y = group["avg_time"].to_numpy(dtype=float)
        mask = np.isfinite(x) & np.isfinite(y)
        x, y = x[mask], y[mask]
        if x.size < min_days or np.ptp(x) == 0.0:
            continue
        slope = float(np.polyfit(x, y, 1)[0]) * 1000.0 * 60.0
        corr = float(np.corrcoef(x, y)[0, 1]) if np.ptp(y) > 0.0 else 0.0
        valid = group.loc[mask]
        qty = float(valid["qty"].sum())
        tickets = float(valid["tickets"].sum())
        time_num = float((valid["qty"] * valid["avg_time"]).sum())
        records.append(
            {
                "city_name": city,
                "days": days,
                "slope_sec_per_1000": slope,
                "slope_r2": corr**2,
                "weighted_avg_time": time_num / qty if qty else float("nan"),
                "cost_per_item": float(valid["cost"].sum()) / qty if qty else float("nan"),
                "items_per_order": qty / tickets if tickets else float("nan"),
                "qty": qty,
            }
        )

    if not records:
        return pd.DataFrame(columns=CITY_COLUMNS)

    frame = pd.DataFrame(records)
    frame["degradation_index"] = (
        _zscore(frame["slope_sec_per_1000"])
        + _zscore(frame["weighted_avg_time"])
        + _zscore(frame["cost_per_item"])
    ) / 3.0
    frame = frame.sort_values("degradation_index", ascending=False, kind="stable").reset_index(drop=True)
    frame.insert(0, "rank", np.arange(1, len(frame) + 1))
    total = len(frame)
    frame["segment"] = np.where(
        frame["rank"] <= 10,
        "worst10",
        np.where(frame["rank"] > total - 5, "best5", "middle"),
    )
    return frame[CITY_COLUMNS]


def select_extremes(ranking: pd.DataFrame, top: int = 10, bottom: int = 5) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Return the worst ``top`` and the best ``bottom`` rows of a ranked city frame."""
    if ranking.empty:
        return ranking.copy(), ranking.copy()
    return ranking.head(top).copy(), ranking.tail(bottom).copy()


def summarize(
    channels: pd.DataFrame,
    curve: pd.DataFrame,
    correlation: dict[str, Any],
    cities: pd.DataFrame,
) -> str:
    """Build a short Russian summary with the numbers that matter."""
    if channels.empty:
        return "Нет данных в витрине mart_delivery."

    overall = channels.loc[channels["channel"] == "all"].iloc[0]
    pairs = channels.loc[channels["channel"] != "all"]
    slowest = pairs.sort_values("weighted_avg_time", ascending=False).iloc[0]
    largest_basket = pairs.sort_values("items_per_order", ascending=False).iloc[0]
    priciest = pairs.sort_values("cost_per_item", ascending=False).iloc[0]

    lines = [
        "УЗКИЕ МЕСТА",
        (
            f"Средневзвешенное время доставки: {overall['weighted_avg_time']:.1f} мин; "
            f"{overall['items_per_order']:.2f} позиции на заказ; "
            f"cost на позицию: {overall['cost_per_item']:.1f} ед. источника."
        ),
        (
            f"Самый медленный сегмент: {slowest['channel']} / {slowest['category']} - "
            f"{slowest['weighted_avg_time']:.1f} мин "
            f"({slowest['weighted_avg_time_dev_pct']:+.1f}% к среднему)."
        ),
        (
            f"Больше всего позиций на заказ: {largest_basket['channel']} / "
            f"{largest_basket['category']} - {largest_basket['items_per_order']:.2f}."
        ),
        (
            f"Самая высокая cost на позицию: {priciest['channel']} / {priciest['category']} - "
            f"{priciest['cost_per_item']:.1f} ед. источника."
        ),
    ]

    if not curve.empty:
        first = curve.iloc[0]
        last = curve.iloc[-1]
        delta = last["mean_avg_time"] - first["mean_avg_time"]
        lines.append(
            f"Кривая нагрузки: дециль 1 (qty ~ {first['mean_qty']:.0f}) - "
            f"{first['mean_avg_time']:.2f} мин, дециль 10 (qty ~ {last['mean_qty']:.0f}) - "
            f"{last['mean_avg_time']:.2f} мин, разница {delta:+.2f} мин."
        )
    if np.isfinite(correlation.get("rho", float("nan"))):
        lines.append(
            f"Spearman qty x avg_time: rho = {correlation['rho']:.3f}, "
            f"p = {correlation['p_value']:.2e}, n = {correlation['n']}."
        )

    if not cities.empty:
        top = cities.head(3)
        names = "; ".join(
            f"{row.city_name} ({row.degradation_index:+.2f})" for row in top.itertuples()
        )
        steepest = cities.sort_values("slope_sec_per_1000", ascending=False).iloc[0]
        flattest = cities.sort_values("slope_sec_per_1000").iloc[0]
        lines.append(f"Верх деградации: {names}.")
        lines.append(
            f"Самый крутой рост времени: {steepest.city_name} "
            f"{steepest.slope_sec_per_1000:+.1f} сек на 1000 позиций; "
            f"самый обратный: {flattest.city_name} {flattest.slope_sec_per_1000:+.1f}."
        )

    return "\n".join(lines)


def analyze(db_path: Path = cfg.DUCKDB_PATH) -> dict[str, Any]:
    """Run the full bottleneck analysis and return frames, Spearman stats and summary."""
    mart, city_daily = load_frames(db_path)
    channels = channel_category_table(mart)
    curve, correlation = load_curve(city_daily)
    cities = city_ranking(city_daily)
    return {
        "channels": channels,
        "load_curve": curve,
        "cities": cities,
        "spearman": correlation,
        "summary": summarize(channels, curve, correlation, cities),
    }


def _plot_load_curve(curve: pd.DataFrame, path: Path) -> None:
    fig, ax = plt.subplots(figsize=(9, 5))
    ax.bar(curve["decile"], curve["mean_avg_time"], color="#4C72B0", alpha=0.85)
    ax.set_xlabel("Дециль нагрузки (дневной qty города)")
    ax.set_ylabel("Среднее время доставки, мин")
    ax.set_title("Кривая нагрузки: время доставки по децилям объёма позиций")
    ax.set_xticks(curve["decile"])
    for row in curve.itertuples():
        ax.annotate(
            f"{row.mean_avg_time:.2f}",
            (row.decile, row.mean_avg_time),
            textcoords="offset points",
            xytext=(0, 3),
            ha="center",
            fontsize=8,
        )
    twin = ax.twinx()
    twin.plot(curve["decile"], curve["mean_qty"], color="#C44E52", marker="o")
    twin.set_yscale("log")
    twin.set_ylabel("Средний qty в дециле (лог. шкала)")
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def _plot_city_slopes(cities: pd.DataFrame, path: Path) -> None:
    worst, best = select_extremes(cities)
    subset = pd.concat([worst, best]).drop_duplicates(subset=["city_name"])
    fig, ax = plt.subplots(figsize=(9, 6))
    colors = ["#C44E52" if segment == "worst10" else "#55A868" for segment in subset["segment"]]
    ax.barh(subset["city_name"], subset["slope_sec_per_1000"], color=colors, alpha=0.9)
    ax.axvline(0, color="black", linewidth=0.8)
    ax.set_xlabel("Наклон avg_time ~ qty, сек на 1000 позиций")
    ax.set_title("Ранжирование городов: верх-10 (красный) и низ-5 (зелёный) по деградации")
    ax.invert_yaxis()
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def _plot_channel_category(channels: pd.DataFrame, path: Path) -> None:
    pairs = channels.loc[channels["channel"] != "all"]
    pivot = pairs.pivot(index="channel", columns="category", values="weighted_avg_time")
    fig, ax = plt.subplots(figsize=(8, 4.5))
    image = ax.imshow(pivot.to_numpy(), aspect="auto", cmap="YlOrRd")
    ax.set_xticks(range(len(pivot.columns)), pivot.columns)
    ax.set_yticks(range(len(pivot.index)), pivot.index)
    ax.set_title("Время доставки, мин: канал x категория")
    for i in range(len(pivot.index)):
        for j in range(len(pivot.columns)):
            value = pivot.iloc[i, j]
            if np.isfinite(value):
                ax.text(j, i, f"{value:.1f}", ha="center", va="center", fontsize=9)
    fig.colorbar(image, ax=ax, label="мин")
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def write_reports(
    result: dict[str, Any],
    reports_dir: Path = cfg.REPORTS,
    figures_dir: Path = cfg.FIGURES,
) -> dict[str, Path]:
    """Persist bottleneck CSVs and figures; return the written paths."""
    reports_dir.mkdir(parents=True, exist_ok=True)
    figures_dir.mkdir(parents=True, exist_ok=True)

    paths = {
        "channels_csv": reports_dir / "bottleneck_channels.csv",
        "cities_csv": reports_dir / "bottleneck_cities.csv",
        "load_curve_csv": reports_dir / "load_curve.csv",
        "load_curve_png": figures_dir / "bottleneck_load_curve.png",
        "city_slopes_png": figures_dir / "bottleneck_city_slopes.png",
        "channel_category_png": figures_dir / "bottleneck_channel_category_time.png",
    }
    result["channels"].to_csv(paths["channels_csv"], index=False)
    result["cities"].to_csv(paths["cities_csv"], index=False)
    result["load_curve"].to_csv(paths["load_curve_csv"], index=False)
    _plot_load_curve(result["load_curve"], paths["load_curve_png"])
    if not result["cities"].empty:
        _plot_city_slopes(result["cities"], paths["city_slopes_png"])
    _plot_channel_category(result["channels"], paths["channel_category_png"])
    return paths


def main() -> None:
    result = analyze()
    print(result["summary"])
    paths = write_reports(result)
    print("\nФайлы:")
    for name, path in paths.items():
        print(f"  {name}: {path}")


if __name__ == "__main__":
    main()
