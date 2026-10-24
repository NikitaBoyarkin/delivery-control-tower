"""Tests for the static offline dashboard (delivery/dashboard.py)."""

from __future__ import annotations

import re

import duckdb
import pytest

from delivery import config as cfg
from delivery import dashboard

pytestmark = pytest.mark.skipif(
    not cfg.DUCKDB_PATH.exists(), reason=f"marts DB missing: {cfg.DUCKDB_PATH}"
)

KPI_SQL = {
    "orders": "SELECT SUM(tickets) FROM mart_daily_total",
    "units": "SELECT SUM(qty) FROM mart_daily_total",
    "items_per_order": "SELECT SUM(qty) / SUM(tickets) FROM mart_daily_total",
    "cost_per_order": "SELECT SUM(cost) / SUM(tickets) FROM mart_daily_total",
    "avg_time": "SELECT SUM(qty * weighted_avg_time) / SUM(qty) FROM mart_daily_total",
    "cities": "SELECT COUNT(DISTINCT city_name) FROM mart_city_daily",
    "peak_day_orders": "SELECT MAX(tickets) FROM mart_daily_total",
    "pareto_cities_80": """
        WITH city_orders AS (
            SELECT city_name, SUM(tickets) AS orders
            FROM mart_city_daily
            GROUP BY city_name
        ), ranked AS (
            SELECT orders,
                   ROW_NUMBER() OVER (ORDER BY orders DESC, city_name) AS rn,
                   SUM(orders) OVER () AS total,
                   SUM(orders) OVER (
                       ORDER BY orders DESC, city_name ROWS UNBOUNDED PRECEDING
                   ) AS cum
            FROM city_orders
        )
        SELECT MIN(rn) FROM ranked WHERE cum >= 0.8 * total
    """,
}


@pytest.fixture(scope="module")
def html_path(tmp_path_factory) -> object:
    out = tmp_path_factory.mktemp("dashboard") / "dashboard.html"
    return dashboard.build(out_path=out)


@pytest.fixture(scope="module")
def page(html_path) -> str:
    return html_path.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def con():
    connection = duckdb.connect(str(cfg.DUCKDB_PATH), read_only=True)
    yield connection
    connection.close()


def test_html_exists_and_is_small(html_path, page):
    assert html_path.exists()
    size = html_path.stat().st_size
    assert size < 2 * 1024 * 1024, f"dashboard is {size} bytes"
    assert size > 10_000
    assert page.startswith("<!doctype html>")
    assert page.rstrip().endswith("</html>")


def test_no_external_resources(page):
    lowered = page.lower()
    assert "<script" not in lowered
    assert "<link" not in lowered
    assert "<img" not in lowered
    assert "@import" not in lowered
    assert "url(" not in lowered
    assert "src=" not in lowered
    assert "http://" not in page
    assert "https://" not in page


def test_basic_document_structure(page):
    assert page.count("<head>") == 1
    head = page.split("</head>", 1)[0]
    titles = re.findall(r"<title>.*?</title>", head, flags=re.S)
    assert len(titles) == 1
    assert "Delivery Control Tower" in titles[0]
    assert page.count("<html") == 1
    assert page.count("</html>") == 1
    assert page.count("<svg") == page.count("</svg>")
    assert page.count("<body>") == 1


def test_three_role_sections_present(page):
    for heading in ("Операции", "Коммерция", "Руководство"):
        assert f"</span>{heading}</h2>" in page, heading
    assert 'id="operations"' in page
    assert 'id="commerce"' in page
    assert 'id="executive"' in page


def test_kpi_tiles_match_direct_sql(page, con):
    found = {
        key: float(value)
        for key, value in re.findall(
            r'data-kpi="([a-z0-9_]+)" data-value="([^"]+)"', page
        )
    }
    assert set(found) == set(KPI_SQL)
    for key, sql in KPI_SQL.items():
        expected = con.execute(sql).fetchone()[0]
        assert expected is not None, key
        assert abs(found[key] - float(expected)) <= 0.01, (key, found[key], expected)


def test_required_kpi_labels(page):
    for label in (
        "Заказы",
        "Позиции",
        "Позиций на заказ",
        "Стоимость заказа",
        "Среднее время",
        "Городов",
    ):
        assert label in page, label
    for key in ("orders", "units", "items_per_order", "cost_per_order", "avg_time", "cities"):
        assert f'data-kpi="{key}"' in page, key


def test_obsolete_ticket_metric_and_money_symbols_removed(page):
    lowered = page.lower()
    assert "tickets_per_100" not in page
    assert "тикет" not in lowered
    assert "выручк" not in lowered
    assert "₽" not in page


def test_orders_kpi_equals_ticket_sum(page, con):
    found = {
        key: float(value)
        for key, value in re.findall(r'data-kpi="([a-z0-9_]+)" data-value="([^"]+)"', page)
    }
    expected = con.execute("SELECT SUM(tickets) FROM mart_daily_total").fetchone()[0]
    assert abs(found["orders"] - float(expected)) <= 0.01


def test_heatmap_is_seven_by_twenty_six(page):
    cells = re.findall(r'class="hm(?:-empty)?" data-dow="(\d)" data-week="(\d+)"', page)
    assert len(cells) == 7 * 26 == 182
    dows = {int(dow) for dow, _ in cells}
    weeks = {int(week) for _, week in cells}
    assert dows == set(range(7))
    assert weeks == set(range(26))
    filled = page.count('class="hm" data-dow=')
    assert filled == 181  # one cell per day in the period


def test_written_reports_have_no_obsolete_ticket_metric():
    files = list(cfg.REPORTS.glob("*.csv")) + [cfg.REPORTS / "dashboard.html"]
    present = [path for path in files if path.exists()]
    assert present, "no report artifacts found"
    for path in present:
        assert "tickets_per_100" not in path.read_text(encoding="utf-8")


def test_charts_and_period_are_rendered(page):
    assert "Динамика по дням" in page
    assert "Топ-10 городов" in page
    assert "Парето по городам" in page
    assert "День недели × неделя" in page
    assert "01.01.2022 — 30.06.2022" in page
    assert page.count("<svg") >= 6
