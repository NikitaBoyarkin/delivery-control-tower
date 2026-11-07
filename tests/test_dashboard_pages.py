"""Tests for the public Power BI-style dashboard (delivery/dashboard_pages.py)."""

from __future__ import annotations

import json
import re
from pathlib import Path

import duckdb
import pytest

from delivery import config as cfg
from delivery import dashboard_pages

pytestmark = pytest.mark.skipif(
    not cfg.DUCKDB_PATH.exists(), reason=f"marts DB missing: {cfg.DUCKDB_PATH}"
)

# tests/conftest.py retargets cfg paths at a temp dir when data/raw is absent
# (CI, clean clone), so the committed page -- built from the real marts -- can
# only be compared byte-for-byte in the real-data profile.
REAL_DATA = cfg.DUCKDB_PATH.is_relative_to(cfg.ROOT)


@pytest.fixture(scope="module")
def fresh_page_path(tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("pages") / "index.html"
    return dashboard_pages.build(db_path=cfg.DUCKDB_PATH, out_path=out)


@pytest.fixture(scope="module")
def page(fresh_page_path) -> str:
    return fresh_page_path.read_text(encoding="utf-8")


def test_committed_page_matches_a_fresh_build(fresh_page_path):
    """docs/index.html is generated and served by GitHub Pages, so it must be
    exactly the bytes the generator emits today -- a hand edit or a generator
    change without a rebuild means the published page lies about the marts."""
    if not REAL_DATA:
        again = dashboard_pages.build(
            db_path=cfg.DUCKDB_PATH, out_path=fresh_page_path.with_name("again.html")
        )
        assert again.read_bytes() == fresh_page_path.read_bytes(), (
            "generator is not deterministic: two builds from the same marts differ"
        )
        return

    committed = dashboard_pages.OUT_PATH
    assert committed.exists(), f"published page missing: {committed}"
    assert fresh_page_path.read_bytes() == committed.read_bytes(), (
        f"{committed} is stale: regenerate with `uv run python -m delivery.dashboard_pages`"
    )


def test_payload_matches_the_marts(page):
    raw = re.search(r"window\.DATA = (\{.*?\});\s*</script>", page, re.S).group(1)
    payload = json.loads(raw.replace("<\\/", "</"))

    con = duckdb.connect(str(cfg.DUCKDB_PATH), read_only=True)
    try:
        rows, tickets, days, channels, cities, categories = con.execute(
            """
            SELECT COUNT(*), SUM(tickets),
                   COUNT(DISTINCT date_key), COUNT(DISTINCT channel_key),
                   COUNT(DISTINCT city_key), COUNT(DISTINCT category_key)
            FROM fact_sales
            """
        ).fetchone()
    finally:
        con.close()

    daily = payload["daily"]["rows"]
    city = payload["city"]["rows"]
    assert payload["meta"]["source"]["factRows"] == rows == cfg.EXPECTED_AGG_ROWS
    assert sum(row[3] for row in daily) == tickets
    assert len(payload["meta"]["days"]) == days
    assert len(payload["meta"]["orders"]["channel"]) == channels
    assert len(payload["meta"]["orders"]["category"]) == categories
    assert len({row[0] for row in city}) == cities


def test_page_is_self_contained(page):
    assert "window.DATA" in page
    assert not re.search(r"<script[^>]+src=", page), "no external scripts"
    assert not re.search(r"<link[^>]+stylesheet", page), "no external stylesheets"
    assert "fetch(" not in page, "file:// blocks fetch; data must be inlined"
