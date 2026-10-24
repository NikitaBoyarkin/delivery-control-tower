"""Run the named analytical SQL queries and the optimization benchmark on DuckDB.

Two entry points:

* :func:`run_windows` executes every ``-- name:`` block from ``sql/windows.sql``,
  stores one CSV per query under ``reports/sql_results/`` and a summary in
  ``reports/sql_timings.csv``.
* :func:`benchmark_optimizations` executes the slow/fast pairs from
  ``sql/optimization.sql`` under ``EXPLAIN ANALYZE``, keeps the median total
  execution time per variant and writes ``reports/sql_optimization.csv``.

Both are read-only against ``delivery.config.DUCKDB_PATH``. Run from the project
root with ``uv run python -m delivery.sql_layer``.
"""

from __future__ import annotations

import argparse
import re
import statistics
import time
from dataclasses import dataclass
from pathlib import Path

import duckdb
import pandas as pd

from delivery import config as cfg

SQL_DIR = cfg.ROOT / "sql"
WINDOWS_SQL = SQL_DIR / "windows.sql"
OPTIMIZATION_SQL = SQL_DIR / "optimization.sql"

RESULTS_DIRNAME = "sql_results"
TIMINGS_FILE = "sql_timings.csv"
OPTIMIZATION_FILE = "sql_optimization.csv"

TIMING_COLUMNS = ["name", "rows", "time_ms"]
OPTIMIZATION_COLUMNS = ["pair", "variant", "rows", "time_ms", "speedup"]

_NAME_RE = re.compile(r"^--\s*name:\s*(\w+)\s*$", re.MULTILINE)
_PAIR_RE = re.compile(r"^--\s*pair:\s*(\w+)\s*$", re.MULTILINE)
_VARIANT_RE = re.compile(r"^--\s*variant:\s*(slow|fast)\s*$", re.MULTILINE | re.IGNORECASE)
_NOTE_RE = re.compile(r"^--\s*note:\s*(.+)$", re.MULTILINE)
_TOTAL_TIME_RE = re.compile(r"Total Time:\s*([0-9.]+)s")


@dataclass(frozen=True)
class NamedQuery:
    """A single ``-- name:`` SQL block."""

    name: str
    sql: str


@dataclass(frozen=True)
class OptimizationQuery:
    """One variant of an optimization pair."""

    pair: str
    variant: str
    sql: str
    note: str = ""


@dataclass(frozen=True)
class OptimizationPair:
    """A slow query and its rewrite, sharing the pair name."""

    name: str
    slow: OptimizationQuery
    fast: OptimizationQuery


def _strip_leading_comments(text: str) -> str:
    """Drop comment-only lines that precede the statement body."""
    lines = text.splitlines()
    while lines and lines[0].strip().startswith("--"):
        lines.pop(0)
    return "\n".join(lines).strip()


def parse_named_queries(path: Path = WINDOWS_SQL) -> list[NamedQuery]:
    """Parse ``-- name:`` blocks from a SQL file.

    Every block must contain a statement terminated by a semicolon. Anything
    after the first semicolon in a block is ignored, which keeps description
    comments out of the executed SQL.
    """
    text = Path(path).read_text(encoding="utf-8")
    markers = list(_NAME_RE.finditer(text))
    if not markers:
        raise ValueError(f"no '-- name:' blocks found in {path}")

    queries: list[NamedQuery] = []
    seen: set[str] = set()
    for index, marker in enumerate(markers):
        block_end = markers[index + 1].start() if index + 1 < len(markers) else len(text)
        block = text[marker.end() : block_end]
        sql, separator, _ = block.partition(";")
        sql = _strip_leading_comments(sql)
        name = marker.group(1)
        if name in seen:
            raise ValueError(f"duplicate query name {name!r} in {path}")
        if not separator:
            raise ValueError(f"query {name!r} in {path} is not terminated with ';'")
        if not sql:
            raise ValueError(f"query {name!r} in {path} has an empty body")
        seen.add(name)
        queries.append(NamedQuery(name=name, sql=sql))
    return queries


def parse_optimization_pairs(path: Path = OPTIMIZATION_SQL) -> list[OptimizationPair]:
    """Parse ``-- pair:`` / ``-- variant:`` blocks into slow/fast pairs."""
    text = Path(path).read_text(encoding="utf-8")
    markers = list(_PAIR_RE.finditer(text))
    if not markers:
        raise ValueError(f"no '-- pair:' blocks found in {path}")

    grouped: dict[str, dict[str, OptimizationQuery]] = {}
    order: list[str] = []
    for index, marker in enumerate(markers):
        block_end = markers[index + 1].start() if index + 1 < len(markers) else len(text)
        block = text[marker.end() : block_end]
        pair_name = marker.group(1)

        variant_match = _VARIANT_RE.search(block)
        if variant_match is None:
            raise ValueError(f"pair {pair_name!r} in {path} has a block with no variant")
        variant = variant_match.group(1).lower()
        note_match = _NOTE_RE.search(block)
        note = note_match.group(1).strip() if note_match else ""

        body = block[variant_match.end() :]
        sql, separator, _ = body.partition(";")
        sql = _strip_leading_comments(sql)
        if not separator:
            raise ValueError(f"pair {pair_name!r} variant {variant!r} has no terminating ';'")
        if not sql:
            raise ValueError(f"pair {pair_name!r} variant {variant!r} has an empty body")

        variants = grouped.setdefault(pair_name, {})
        if variant in variants:
            raise ValueError(f"pair {pair_name!r} defines variant {variant!r} twice")
        variants[variant] = OptimizationQuery(
            pair=pair_name, variant=variant, sql=sql, note=note
        )
        if pair_name not in order:
            order.append(pair_name)

    pairs: list[OptimizationPair] = []
    for pair_name in order:
        variants = grouped[pair_name]
        missing = {"slow", "fast"} - set(variants)
        if missing:
            raise ValueError(f"pair {pair_name!r} is missing variants: {sorted(missing)}")
        pairs.append(
            OptimizationPair(
                name=pair_name, slow=variants["slow"], fast=variants["fast"]
            )
        )
    return pairs


def _explain_total_ms(con: duckdb.DuckDBPyConnection, sql: str) -> float:
    """Execute ``EXPLAIN ANALYZE`` and return the report's ``Total Time`` in ms."""
    rows = con.execute("EXPLAIN ANALYZE " + sql).fetchall()
    chunks: list[str] = []
    for row in rows:
        value = row[1]
        if isinstance(value, (list, tuple)):
            chunks.extend(str(part) for part in value)
        else:
            chunks.append(str(value))
    match = _TOTAL_TIME_RE.search("\n".join(chunks))
    if match is None:
        raise RuntimeError("EXPLAIN ANALYZE output contains no 'Total Time' line")
    return float(match.group(1)) * 1000.0


def run_windows(
    con: duckdb.DuckDBPyConnection,
    queries_path: Path = WINDOWS_SQL,
    out_dir: Path = cfg.REPORTS,
) -> pd.DataFrame:
    """Run every named query, save one CSV per result plus the timing summary."""
    queries = parse_named_queries(queries_path)
    out_dir = Path(out_dir)
    results_dir = out_dir / RESULTS_DIRNAME
    results_dir.mkdir(parents=True, exist_ok=True)

    records: list[dict[str, object]] = []
    for query in queries:
        started = time.perf_counter()
        frame = con.execute(query.sql).df()
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        frame.to_csv(results_dir / f"{query.name}.csv", index=False)
        records.append(
            {"name": query.name, "rows": len(frame), "time_ms": round(elapsed_ms, 2)}
        )
        print(f"{query.name:<34} rows={len(frame):>7,}  {elapsed_ms:8.2f} ms")

    timings = pd.DataFrame(records, columns=TIMING_COLUMNS)
    timings.to_csv(out_dir / TIMINGS_FILE, index=False)
    return timings


def benchmark_optimizations(
    con: duckdb.DuckDBPyConnection,
    path: Path = OPTIMIZATION_SQL,
    out_dir: Path = cfg.REPORTS,
    repeats: int = 3,
) -> pd.DataFrame:
    """Time slow/fast pairs with ``EXPLAIN ANALYZE`` and write the comparison CSV.

    ``time_ms`` is the median of ``repeats`` total execution times. The slow
    variant is the baseline and reports ``speedup = 1.0``; the fast variant
    reports ``slow_time / fast_time``.
    """
    pairs = parse_optimization_pairs(path)
    if repeats < 1:
        raise ValueError("repeats must be >= 1")

    records: list[dict[str, object]] = []
    for pair in pairs:
        times: dict[str, float] = {}
        row_counts: dict[str, int] = {}
        for variant in (pair.slow, pair.fast):
            row_counts[variant.variant] = len(con.execute(variant.sql).df())
            _explain_total_ms(con, variant.sql)  # warm-up, discarded
            samples = [_explain_total_ms(con, variant.sql) for _ in range(repeats)]
            times[variant.variant] = statistics.median(samples)

        fast_ms = times["fast"]
        speedup = times["slow"] / fast_ms if fast_ms > 0 else float("inf")
        for variant in (pair.slow, pair.fast):
            records.append(
                {
                    "pair": pair.name,
                    "variant": variant.variant,
                    "rows": row_counts[variant.variant],
                    "time_ms": round(times[variant.variant], 2),
                    "speedup": 1.0 if variant.variant == "slow" else round(speedup, 2),
                }
            )
        print(
            f"{pair.name:<26} slow={times['slow']:8.2f} ms  "
            f"fast={times['fast']:8.2f} ms  speedup={speedup:5.2f}x  "
            f"rows={row_counts['fast']:,}"
        )

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    benchmark = pd.DataFrame(records, columns=OPTIMIZATION_COLUMNS)
    benchmark.to_csv(out_dir / OPTIMIZATION_FILE, index=False)
    return benchmark


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: ``uv run python -m delivery.sql_layer``."""
    parser = argparse.ArgumentParser(
        prog="python -m delivery.sql_layer",
        description="Run named analytical SQL and optimization benchmarks on DuckDB.",
    )
    parser.add_argument("--db", type=Path, default=cfg.DUCKDB_PATH, help="DuckDB file")
    parser.add_argument("--windows", type=Path, default=WINDOWS_SQL, help="named queries")
    parser.add_argument(
        "--optimizations", type=Path, default=OPTIMIZATION_SQL, help="slow/fast pairs"
    )
    parser.add_argument("--out-dir", type=Path, default=cfg.REPORTS, help="report directory")
    parser.add_argument("--repeats", type=int, default=3, help="EXPLAIN ANALYZE repeats")
    args = parser.parse_args(argv)

    if not args.db.exists():
        print(f"DuckDB file not found: {args.db}")
        print("Build it first with: uv run python -m delivery.marts")
        return 1

    con = duckdb.connect(str(args.db), read_only=True)
    try:
        print("Named analytical queries")
        run_windows(con, args.windows, args.out_dir)
        print("\nOptimization pairs")
        benchmark_optimizations(con, args.optimizations, args.out_dir, args.repeats)
    finally:
        con.close()

    print(f"\nArtifacts written to {args.out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
