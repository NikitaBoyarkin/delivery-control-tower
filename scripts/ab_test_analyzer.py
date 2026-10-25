"""A/B-grade readout for the two pre-registered comparisons in this project.

The project has no randomised experiment: no ``user_id`` + variant assignment +
primary metric exists. What it does have is two two-arm comparisons on
observational data (H1: January vs June channel mix, H2: weekend vs weekday
delivery time). This module analyses them the way an A/B readout would: SRM,
group balance, effect with a cluster-robust CI, power / MDE.

Usage:
    python scripts/ab_test_analyzer.py --readout all
    python scripts/ab_test_analyzer.py --readout h2 --data data/raw/city_day_channel_category.csv
    python scripts/ab_test_analyzer.py --check-srm --n-control 34030 --n-test 13720 --expected-share 0.2873

It is read-only with respect to the marts: it reads the raw CSV and recomputes
the aggregation itself, so it does not inherit ``mart_city_daily.avg_time``.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from scipy import stats

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATA = ROOT / "data" / "raw" / "city_day_channel_category.csv"
DEFAULT_OUT = ROOT / "reports" / "ab_test_metrics.csv"
ALPHA = 0.05
POWER = 0.80
BOOTSTRAP_N = 5_000
SEED = 42

METRIC_COLUMNS = [
    "readout",
    "metric",
    "control",
    "variant",
    "effect",
    "ci_low",
    "ci_high",
    "p_value",
    "test",
    "n_control",
    "n_test",
    "note",
]


def load_raw(path: Path = DEFAULT_DATA) -> pd.DataFrame:
    frame = pd.read_csv(path, parse_dates=["Date"])
    frame["is_weekend"] = (frame["Date"].dt.day_of_week >= 5).astype(int)
    return frame


def city_day(frame: pd.DataFrame) -> pd.DataFrame:
    """City-day rows in two aggregations: the mart's unweighted mean and a qty-weighted mean.

    ``avg_time`` is a per-cell mean, i.e. a ratio. The project's own dictionary says
    it must be re-weighted by ``qty`` when aggregated; ``AVG(avg_time)`` (what
    ``mart_city_daily`` does) lets a 3-order cell outvote a 300-order cell.
    """
    base = frame.groupby(["CityName", "Date", "is_weekend"], as_index=False).agg(
        time_unweighted=("avg_time", "mean"),
        cells=("avg_time", "size"),
        tickets=("tickets", "sum"),
    )
    weighted_qty = (
        frame.assign(_w=frame["avg_time"] * frame["qty"])
        .groupby(["CityName", "Date"], as_index=False)
        .agg(_sum=("_w", "sum"), _qty=("qty", "sum"))
    )
    weighted_tickets = (
        frame.assign(_w=frame["avg_time"] * frame["tickets"])
        .groupby(["CityName", "Date"], as_index=False)
        .agg(_sum=("_w", "sum"), _tickets=("tickets", "sum"))
    )
    base = base.merge(weighted_qty, on=["CityName", "Date"])
    base["time_weighted"] = base["_sum"] / base["_qty"]
    base = base.drop(columns=["_sum", "_qty"])
    base = base.merge(weighted_tickets, on=["CityName", "Date"])
    base["time_tickets_weighted"] = base["_sum"] / base["_tickets"]
    base = base.drop(columns=["_sum", "_tickets"])
    base["orders_per_cell"] = base["tickets"] / base["cells"]
    return base


def welch(a: np.ndarray, b: np.ndarray) -> tuple[float, float]:
    result = stats.ttest_ind(a, b, equal_var=False)
    return float(result.statistic), float(result.pvalue)


def mde_means(
    sd: float, n_control: int, n_test: int, alpha: float = ALPHA, power: float = POWER
) -> float:
    z = stats.norm.ppf(1 - alpha / 2) + stats.norm.ppf(power)
    return float(z * sd * np.sqrt(1.0 / n_control + 1.0 / n_test))


def mde_proportions(
    base: float, n_control: int, n_test: int, alpha: float = ALPHA, power: float = POWER
) -> float:
    z = stats.norm.ppf(1 - alpha / 2) + stats.norm.ppf(power)
    se = np.sqrt(base * (1 - base) * (1.0 / n_control + 1.0 / n_test))
    return float(z * se)


def srm_check(n_control: int, n_test: int, expected_share: float) -> dict[str, Any]:
    """Chi-square of observed allocation against the planned share (not an assumed 50/50)."""
    total = n_control + n_test
    expected = np.array([total * (1 - expected_share), total * expected_share])
    observed = np.array([n_control, n_test], dtype=float)
    chi2, p_value = stats.chisquare(observed, expected)
    return {
        "chi2": float(chi2),
        "p_value": float(p_value),
        "observed_share": n_test / total,
        "expected_share": expected_share,
        "ok": bool(p_value >= 0.001),
    }


def iid_bootstrap_diff(
    a: np.ndarray, b: np.ndarray, n_boot: int = BOOTSTRAP_N, seed: int = SEED
) -> tuple[float, float]:
    rng = np.random.default_rng(seed)
    diffs = np.empty(n_boot)
    for i in range(n_boot):
        diffs[i] = rng.choice(a, a.size).mean() - rng.choice(b, b.size).mean()
    low, high = np.percentile(diffs, [2.5, 97.5])
    return float(low), float(high)


def cluster_bootstrap_mean_diff(
    frame: pd.DataFrame,
    value_col: str,
    group_col: str,
    cluster_col: str = "CityName",
    n_boot: int = BOOTSTRAP_N,
    seed: int = SEED,
) -> tuple[float, float]:
    """Percentile CI of mean(variant) - mean(control), resampling whole clusters.

    City-day rows are not independent: the same 264 cities appear on all 181 days.
    Resampling cities keeps that dependence and stops the CI from being too narrow.
    """
    per_cluster = (
        frame.groupby([cluster_col, group_col])[value_col].agg(["sum", "count"]).unstack(group_col)
    )
    sums = per_cluster["sum"].fillna(0.0).to_numpy()
    counts = per_cluster["count"].fillna(0.0).to_numpy()
    groups = list(per_cluster["sum"].columns)
    control_idx, test_idx = groups.index(0), groups.index(1)

    rng = np.random.default_rng(seed)
    idx = rng.integers(0, sums.shape[0], size=(n_boot, sums.shape[0]))
    test_mean = sums[idx, test_idx].sum(axis=1) / counts[idx, test_idx].sum(axis=1)
    control_mean = sums[idx, control_idx].sum(axis=1) / counts[idx, control_idx].sum(axis=1)
    low, high = np.percentile(test_mean - control_mean, [2.5, 97.5])
    return float(low), float(high)


def standardised_mean_diff(frame: pd.DataFrame, value_col: str, group_col: str) -> float:
    test = frame.loc[frame[group_col] == 1, value_col].to_numpy(dtype=float)
    control = frame.loc[frame[group_col] == 0, value_col].to_numpy(dtype=float)
    pooled = np.sqrt((test.var(ddof=1) + control.var(ddof=1)) / 2.0)
    if pooled == 0:
        return 0.0
    return float((test.mean() - control.mean()) / pooled)


def records_h2(frame: pd.DataFrame) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    days = city_day(frame)
    records: list[dict[str, Any]] = []
    diagnostics: dict[str, Any] = {}

    for label, column in (
        ("delivery_time_unweighted", "time_unweighted"),
        ("delivery_time_weighted", "time_weighted"),
        ("delivery_time_weighted_by_tickets", "time_tickets_weighted"),
    ):
        weekend = days.loc[days["is_weekend"] == 1, column].to_numpy(dtype=float)
        weekday = days.loc[days["is_weekend"] == 0, column].to_numpy(dtype=float)
        diff = weekend.mean() - weekday.mean()
        _, p_value = welch(weekend, weekday)
        naive_low, naive_high = iid_bootstrap_diff(weekend, weekday)
        clust_low, clust_high = cluster_bootstrap_mean_diff(days, column, "is_weekend")
        sd = float(np.sqrt((weekend.var(ddof=1) + weekday.var(ddof=1)) / 2.0))
        records.append(
            {
                "readout": "H2 weekend vs weekday",
                "metric": label,
                "control": f"{weekday.mean():.4f}",
                "variant": f"{weekend.mean():.4f}",
                "effect": f"{diff / weekday.mean() * 100:+.4f}%",
                "ci_low": round(naive_low, 4),
                "ci_high": round(naive_high, 4),
                "p_value": p_value,
                "test": "Welch t-test on city-day rows",
                "n_control": weekday.size,
                "n_test": weekend.size,
                "note": (
                    f"cluster-bootstrap CI by city on the difference: "
                    f"[{clust_low:+.4f}; {clust_high:+.4f}] min"
                ),
            }
        )
        diagnostics[label] = {
            "weekday_mean": float(weekday.mean()),
            "weekend_mean": float(weekend.mean()),
            "cluster_ci_minutes": (round(clust_low, 4), round(clust_high, 4)),
            "mde_minutes_80pct": round(mde_means(sd, weekday.size, weekend.size), 4),
        }

    network = (
        frame.assign(_w=frame["avg_time"] * frame["qty"])
        .groupby(["Date", "is_weekend"], as_index=False)
        .agg(_sum=("_w", "sum"), _qty=("qty", "sum"))
    )
    network["time"] = network["_sum"] / network["_qty"]
    weekend = network.loc[network["is_weekend"] == 1, "time"].to_numpy(dtype=float)
    weekday = network.loc[network["is_weekend"] == 0, "time"].to_numpy(dtype=float)
    _, p_value = welch(weekend, weekday)
    diff = weekend.mean() - weekday.mean()
    records.append(
        {
            "readout": "H2 weekend vs weekday",
            "metric": "delivery_time_network_day",
            "control": f"{weekday.mean():.4f}",
            "variant": f"{weekend.mean():.4f}",
            "effect": f"{diff / weekday.mean() * 100:+.4f}%",
            "ci_low": float("nan"),
            "ci_high": float("nan"),
            "p_value": p_value,
            "test": "Welch t-test on 181 network days (qty-weighted)",
            "n_control": weekday.size,
            "n_test": weekend.size,
            "note": "one observation per calendar day; serial correlation not modelled",
        }
    )

    paired = (
        days.groupby(["CityName", "is_weekend"])["time_weighted"]
        .mean()
        .unstack("is_weekend")
        .dropna()
    )
    paired_stat, paired_p = stats.ttest_rel(paired[1], paired[0])
    paired_diff = float(paired[1].mean() - paired[0].mean())
    records.append(
        {
            "readout": "H2 weekend vs weekday",
            "metric": "delivery_time_within_city_paired",
            "control": f"{paired[0].mean():.4f}",
            "variant": f"{paired[1].mean():.4f}",
            "effect": f"{paired_diff / paired[0].mean() * 100:+.4f}%",
            "ci_low": float("nan"),
            "ci_high": float("nan"),
            "p_value": float(paired_p),
            "test": f"paired t-test across {paired.shape[0]} cities",
            "n_control": int(paired.shape[0]),
            "n_test": int(paired.shape[0]),
            "note": "each city contributes its own weekday and weekend mean; city mix held fixed",
        }
    )
    diagnostics["paired"] = {
        "cities": int(paired.shape[0]),
        "statistic": round(float(paired_stat), 4),
        "diff_minutes": round(paired_diff, 4),
    }

    balance = frame.groupby("is_weekend").agg(
        qty=("qty", "sum"),
        tickets=("tickets", "sum"),
        cells=("qty", "size"),
    )
    balance["items_per_order"] = balance["qty"] / balance["tickets"]
    mp_share = (
        frame[frame["OrderSource"].isin(["mp", "web"])]
        .assign(_mp=lambda d: np.where(d["OrderSource"] == "mp", d["qty"], 0.0))
        .groupby("is_weekend")
        .apply(lambda d: float(d["_mp"].sum() / d["qty"].sum()), include_groups=False)
    )
    diagnostics["balance"] = {
        "items_per_order": balance["items_per_order"].round(4).to_dict(),
        "mp_share": mp_share.round(4).to_dict(),
        "smd_orders_per_cell": round(
            standardised_mean_diff(days, "orders_per_cell", "is_weekend"), 4
        ),
        "smd_tickets": round(standardised_mean_diff(days, "tickets", "is_weekend"), 4),
    }
    diagnostics["srm"] = srm_check(
        n_control=int((days["is_weekend"] == 0).sum()),
        n_test=int((days["is_weekend"] == 1).sum()),
        expected_share=float(weekend.size / network.shape[0]),
    )
    return records, diagnostics


def records_h1(frame: pd.DataFrame) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    pair = frame[frame["OrderSource"].isin(["mp", "web"])].copy()
    pair["_mp"] = np.where(pair["OrderSource"] == "mp", pair["qty"], 0.0)
    daily = pair.groupby("Date", as_index=False).agg(mp=("_mp", "sum"), total=("qty", "sum"))
    daily["share"] = daily["mp"] / daily["total"]
    january = daily.loc[daily["Date"].dt.month == 1, "share"].to_numpy(dtype=float)
    june = daily.loc[daily["Date"].dt.month == 6, "share"].to_numpy(dtype=float)

    jan = pair[pair["Date"].dt.month == 1]
    jun = pair[pair["Date"].dt.month == 6]
    control_p = float(jan["_mp"].sum() / jan["qty"].sum())
    test_p = float(jun["_mp"].sum() / jun["qty"].sum())
    n_control, n_test = int(jan["qty"].sum()), int(jun["qty"].sum())

    se = np.sqrt(test_p * (1 - test_p) / n_test + control_p * (1 - control_p) / n_control)
    z_stat = (test_p - control_p) / se
    p_value = float(2 * stats.norm.sf(abs(z_stat)))

    rng = np.random.default_rng(SEED)
    jan_rows = jan[["_mp", "qty"]].to_numpy()
    jun_rows = jun[["_mp", "qty"]].to_numpy()
    diffs = np.empty(BOOTSTRAP_N)
    for i in range(BOOTSTRAP_N):
        a = jun_rows[rng.integers(0, jun_rows.shape[0], jun_rows.shape[0])].sum(axis=0)
        b = jan_rows[rng.integers(0, jan_rows.shape[0], jan_rows.shape[0])].sum(axis=0)
        diffs[i] = a[0] / a[1] - b[0] / b[1]
    clust_low, clust_high = np.percentile(diffs * 100, [2.5, 97.5])

    effect_pp = (test_p - control_p) * 100
    records = [
        {
            "readout": "H1 June vs January",
            "metric": "mp_share_of_mp_plus_web",
            "control": f"{control_p:.4f}",
            "variant": f"{test_p:.4f}",
            "effect": f"{effect_pp:+.4f} pp",
            "ci_low": round(float(clust_low), 4),
            "ci_high": round(float(clust_high), 4),
            "p_value": p_value,
            "test": "two-proportion z-test on pooled qty",
            "n_control": n_control,
            "n_test": n_test,
            "note": (
                f"day-cluster bootstrap CI on the pp difference: "
                f"[{clust_low:.2f}; {clust_high:.2f}] pp; pair share is closed "
                "(mp up forces web down)"
            ),
        }
    ]
    diagnostics = {
        "mde_pp_80pct": round(mde_proportions(control_p, n_control, n_test) * 100, 3),
        "observed_pp": round(effect_pp, 3),
        "daily_share_sd_jan": round(float(january.std(ddof=1)), 4),
        "daily_share_sd_jun": round(float(june.std(ddof=1)), 4),
    }
    return records, diagnostics


def _fmt(records: list[dict[str, Any]]) -> str:
    header = (
        "| metric | control | variant | effect | p-value | n control/test | test |\n"
        "|---|---|---|---|---|---|---|"
    )
    rows = [
        f"| {r['metric']} | {r['control']} | {r['variant']} | {r['effect']} | "
        f"{r['p_value']:.4g} | {r['n_control']}/{r['n_test']} | {r['test']} |"
        for r in records
    ]
    return "\n".join([header, *rows])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--readout", choices=["h1", "h2", "all"], default="all")
    parser.add_argument("--data", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--check-srm", action="store_true", help="only run an SRM check")
    parser.add_argument("--n-control", type=int)
    parser.add_argument("--n-test", type=int)
    parser.add_argument("--expected-share", type=float, default=0.5)
    args = parser.parse_args(argv)

    if args.check_srm:
        if args.n_control is None or args.n_test is None:
            parser.error("--check-srm needs --n-control and --n-test")
        result = srm_check(args.n_control, args.n_test, args.expected_share)
        verdict = "OK" if result["ok"] else "MISMATCH — stop and investigate assignment"
        print(
            f"SRM chi2={result['chi2']:.3f} p={result['p_value']:.4g} "
            f"observed={result['observed_share']:.4f} expected={result['expected_share']:.4f} {verdict}"
        )
        return 0

    frame = load_raw(args.data)
    records: list[dict[str, Any]] = []
    diagnostics: dict[str, Any] = {}
    if args.readout in {"h1", "all"}:
        recs, diag = records_h1(frame)
        records += recs
        diagnostics["H1"] = diag
    if args.readout in {"h2", "all"}:
        recs, diag = records_h2(frame)
        records += recs
        diagnostics["H2"] = diag

    table = pd.DataFrame(records, columns=METRIC_COLUMNS)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    table.to_csv(args.out, index=False)
    print(_fmt(records))
    print()
    for key, value in diagnostics.items():
        print(f"{key}: {value}")
    print(f"\nwritten: {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
