"""Tests for the public Power BI-style dashboard (delivery/dashboard_pages.py)."""

from __future__ import annotations

import json
import re

import pytest

from delivery import config as cfg
from delivery import dashboard_pages

pytestmark = pytest.mark.skipif(
    not cfg.DUCKDB_PATH.exists(), reason=f"marts DB missing: {cfg.DUCKDB_PATH}"
)


@pytest.fixture(scope="module")
def page(tmp_path_factory):
    out = tmp_path_factory.mktemp("pages") / "index.html"
    dashboard_pages.build(db_path=cfg.DUCKDB_PATH, out_path=out)
    return out.read_text(encoding="utf-8")


def test_payload_matches_the_marts(page):
    raw = re.search(r"window\.DATA = (\{.*?\});\s*</script>", page, re.S).group(1)
    payload = json.loads(raw.replace("<\\/", "</"))

    daily = payload["daily"]["rows"]
    city = payload["city"]["rows"]
    assert payload["meta"]["source"]["factRows"] == cfg.EXPECTED_AGG_ROWS
    assert len(payload["meta"]["orders"]["category"]) == 4
    assert len(payload["meta"]["orders"]["channel"]) == 3
    assert len(payload["meta"]["days"]) == 181
    assert sum(row[3] for row in daily) == 33_167_293  # orders from the source
    assert len({row[0] for row in city}) == 264


def test_page_is_self_contained(page):
    assert "window.DATA" in page
    assert not re.search(r"<script[^>]+src=", page), "no external scripts"
    assert not re.search(r"<link[^>]+stylesheet", page), "no external stylesheets"
    assert "fetch(" not in page, "file:// blocks fetch; data must be inlined"
