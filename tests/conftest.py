"""Pytest bootstrap: make the suite self-sufficient on a clean clone.

``data/raw``, ``data/staging``, ``data/marts`` and ``reports/sql_results`` are
gitignored, so a fresh checkout has no data and most tests skip. Several
skipif gates are evaluated at import time (``pytest.mark.skipif(not
cfg.DUCKDB_PATH.exists())`` and ``test_etl.RAW_MISSING``), so the fixture must
exist *before collection* -- hence :func:`pytest_configure`, not a fixture.

Strategy (two profiles):

* **real data** -- if ``data/raw`` (or ``DELIVERY_RAW_SOURCE``) holds both CSVs,
  do nothing and let the suite use the real dataset.
* **synthetic** -- otherwise build a small deterministic dataset in a temp dir
  (see ``scripts/make_synthetic_data.py``), retarget every ``delivery.config``
  path at it, and run ``etl -> quality -> marts`` so the DB exists at import.

Retargeting caveat: several module functions captured ``cfg.DUCKDB_PATH`` as a
default argument at import time. Setting ``cfg.DUCKDB_PATH`` alone does not move
those defaults, so :func:`_retarget_defaults` rewrites the ``__defaults__`` of
every callable whose default points at an old path.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

import pytest

from delivery import bottlenecks, dashboard, etl, forecast, hypothesis, marts, quality
from delivery import config as cfg
from scripts.make_synthetic_data import apply_paths, bind_expected_rows, write_raw

_MODULES = (marts, forecast, hypothesis, bottlenecks, dashboard)
_PATCHED = False


def _real_data_available() -> bool:
    """True when both source CSVs are already present locally."""
    if os.environ.get("DELIVERY_FORCE_SYNTHETIC", "0") not in ("", "0"):
        return False
    local = cfg.AGG_FILE.exists() and cfg.ORDERS_FILE.exists()
    sourced = (cfg.RAW_SOURCE / cfg.RAW_AGG).exists() and (
        cfg.RAW_SOURCE / cfg.RAW_ORDERS
    ).exists()
    return local or sourced


def _retarget_defaults(mapping: dict[Path, Path]) -> None:
    """Rewrite default arguments that captured a superseded config path."""
    for module in _MODULES:
        for value in vars(module).values():
            defaults = getattr(value, "__defaults__", None)
            if not defaults:
                continue
            new_defaults = tuple(
                mapping.get(default, default) if isinstance(default, Path) else default
                for default in defaults
            )
            if new_defaults != defaults:
                value.__defaults__ = new_defaults


def _prepare_real_profile() -> None:
    """Build the derived layers when the CSVs are present but nothing was derived yet."""
    if cfg.DUCKDB_PATH.exists():
        return
    if not (cfg.STAGING / "sales_daily.parquet").exists():
        etl.write_staging()
    sales, orders = quality.load_staging()
    quality.write_report(quality.run_checks(sales, orders))
    marts.build(db_path=cfg.DUCKDB_PATH)
    dashboard.build()


def pytest_configure(config: pytest.Config) -> None:
    global _PATCHED
    if _PATCHED:
        return
    if _real_data_available():
        _prepare_real_profile()
        _PATCHED = True
        return

    old_duckdb, old_reports, old_figures = cfg.DUCKDB_PATH, cfg.REPORTS, cfg.FIGURES
    base = Path(tempfile.mkdtemp(prefix="dct-synthetic-"))
    paths = apply_paths(base)
    counts = write_raw(paths["raw"])

    # The synthetic dataset is authoritative for the synthetic profile: the row
    # counts that quality/etl assert must describe *this* data.
    bind_expected_rows(counts)

    _retarget_defaults(
        {
            old_duckdb: cfg.DUCKDB_PATH,
            old_reports: cfg.REPORTS,
            old_figures: cfg.FIGURES,
        }
    )

    etl.write_staging()
    sales, orders = quality.load_staging()
    quality.write_report(quality.run_checks(sales, orders))
    marts.build(db_path=cfg.DUCKDB_PATH)
    dashboard.build()

    config.add_cleanup(lambda: shutil.rmtree(base, ignore_errors=True))
    _PATCHED = True


_SKIPPED: list[str] = []


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    if report.skipped:
        _SKIPPED.append(report.nodeid)


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """A green run must have exercised every test; a skipped one is a silent hole.

    The suite builds its own data (see ``pytest_configure``), so nothing here is
    legitimately skippable: a skip means the fixture stopped being built and the
    data-dependent tests went quiet instead of failing.
    """
    if not _SKIPPED or exitstatus != 0:
        return
    session.exitstatus = 1
    lines = [f"{len(_SKIPPED)} tests skipped, the suite must run all of them:"]
    lines += [f"  {nodeid}" for nodeid in _SKIPPED[:20]]
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    message = "\n".join(lines)
    if reporter is not None:
        reporter.write_line(message)
    else:
        print(message)
