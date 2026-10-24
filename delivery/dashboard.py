"""Static, offline HTML dashboard for the delivery control tower.

The module reads the pre-built DuckDB marts and renders a single self-contained
HTML file: no CDN, no external JS/CSS, no client-side requests. Every chart is an
inline SVG produced by the functions below.

KPI definitions (kept in one place so tests can mirror them in SQL):

* ``orders``          -- SUM(tickets), i.e. number of customer orders (чеки).
* ``units``           -- SUM(qty), i.e. number of items (позиции).
* ``items_per_order`` -- SUM(qty) / SUM(tickets).
* ``cost_per_order``  -- SUM(cost) / SUM(tickets); cost is a source money metric
  with unconfirmed currency, so it is never labelled as revenue or roubles.
* ``avg_time``        -- SUM(qty * weighted_avg_time) / SUM(qty) (qty-weighted).
* ``cities``          -- distinct cities present in mart_city_daily.
* ``peak_day_orders`` -- MAX daily orders (SUM(tickets) per day).
* ``pareto_cities_80``-- number of cities (sorted by orders) giving 80% of orders.

Run with ``uv run python -m delivery.dashboard``; writes ``reports/dashboard.html``.
"""

from __future__ import annotations

import html
from datetime import date, datetime, timedelta
from pathlib import Path

import duckdb

from delivery import config as cfg

WEEKDAYS = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")
PERIOD_START = date(2022, 1, 1)

CSS = """
:root {
  color-scheme: light dark;
  --bg: #f4f6f9;
  --panel: #ffffff;
  --ink: #131820;
  --muted: #5b6675;
  --line: #e2e7ee;
  --grid: #dfe5ee;
  --accent: #2f6fed;
  --accent-soft: #cfe0ff;
  --warn: #d9691f;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #0e1116;
    --panel: #161b22;
    --ink: #e7edf5;
    --muted: #9aa6b5;
    --line: #242c37;
    --grid: #222a35;
    --accent: #5b9bff;
    --accent-soft: #23324a;
    --warn: #ff9a5c;
  }
}
* { box-sizing: border-box; }
html { -webkit-text-size-adjust: 100%; }
body {
  margin: 0;
  background: var(--bg);
  color: var(--ink);
  font: 15px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
        "Helvetica Neue", Arial, sans-serif;
}
.wrap { max-width: 1080px; margin: 0 auto; padding: 26px 20px 48px; }
.hero { padding-bottom: 18px; border-bottom: 1px solid var(--line); }
.eyebrow {
  margin: 0; font-size: 11px; font-weight: 700; letter-spacing: .14em;
  text-transform: uppercase; color: var(--accent);
}
h1 { margin: 6px 0 8px; font-size: clamp(23px, 4vw, 33px); line-height: 1.15; letter-spacing: -.01em; }
.meta { margin: 0; color: var(--muted); font-size: 13px; }
section { margin-top: 38px; }
.sec-head { display: flex; flex-wrap: wrap; align-items: baseline; gap: 4px 12px; margin-bottom: 4px; }
h2 {
  margin: 0; font-size: 15px; font-weight: 700; letter-spacing: .12em;
  text-transform: uppercase;
}
h2 .role-num { color: var(--accent); margin-right: 8px; }
.role-note { margin: 0 0 14px; color: var(--muted); font-size: 13px; max-width: 70ch; }
.kpis { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(150px, 100%), 1fr)); gap: 12px; margin: 0 0 18px; }
.kpi {
  background: var(--panel); border: 1px solid var(--line); border-radius: 12px;
  padding: 13px 15px; display: flex; flex-direction: column; gap: 1px; min-width: 0;
}
.kpi-label { font-size: 12px; color: var(--muted); }
.kpi-value {
  font-size: 23px; font-weight: 650; letter-spacing: -.015em;
  font-variant-numeric: tabular-nums;
}
.kpi-hint { font-size: 11px; color: var(--muted); opacity: .85; }
.grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(320px, 100%), 1fr)); gap: 16px; }
.grid.two { grid-template-columns: repeat(2, minmax(0, 1fr)); }
.card {
  background: var(--panel); border: 1px solid var(--line); border-radius: 14px;
  padding: 15px 16px 12px; min-width: 0;
}
.card.wide { grid-column: 1 / -1; }
.card h3 { margin: 0 0 2px; font-size: 15px; font-weight: 650; }
.sub { margin: 0 0 8px; font-size: 12px; color: var(--muted); }
.scroll { overflow-x: auto; }
.scroll svg { min-width: 680px; }
.only-narrow { display: none; }
svg { display: block; width: 100%; max-width: 100%; height: auto; }
svg text { fill: var(--muted); font-family: inherit; font-size: 11px; }
svg .label { fill: var(--ink); font-size: 12px; }
svg .grid-line { stroke: var(--grid); stroke-width: 1; }
svg .axis { stroke: var(--line); stroke-width: 1; }
svg .line { fill: none; stroke: var(--accent); stroke-width: 2.2; stroke-linejoin: round; }
svg .area { fill: var(--accent); opacity: .12; }
svg .dot { fill: var(--accent); opacity: 0; }
svg .dot:hover { opacity: 1; }
svg .bar { fill: var(--accent); }
svg .bar-alt { fill: var(--warn); }
svg .hm { fill: var(--accent); }
svg .hm-empty { fill: var(--grid); }
svg .ref { stroke: var(--warn); stroke-width: 1.4; stroke-dasharray: 5 5; }
svg .cum { fill: none; stroke: var(--warn); stroke-width: 2.2; stroke-linejoin: round; }
svg .marker { fill: var(--warn); }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th, td { text-align: left; padding: 7px 8px; border-bottom: 1px solid var(--line); white-space: nowrap; }
th { color: var(--muted); font-weight: 600; font-size: 12px; }
td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }.legend { display: flex; flex-wrap: wrap; gap: 14px; margin: 6px 0 0; font-size: 12px; color: var(--muted); }
.legend span { display: inline-flex; align-items: center; gap: 6px; }
.swatch { width: 11px; height: 11px; border-radius: 3px; display: inline-block; }
.swatch.accent { background: var(--accent); }
.swatch.warn { background: var(--warn); }
footer { margin-top: 42px; padding-top: 16px; border-top: 1px solid var(--line); color: var(--muted); font-size: 12px; }
footer p { margin: 0 0 6px; }
@media (max-width: 520px) {
  .wrap { padding: 18px 14px 40px; }
  .kpi-value { font-size: 20px; }
  .grid { gap: 12px; }
  .card { padding: 13px 12px 10px; }
  th, td { white-space: normal; padding: 6px; }
  .grid.two { grid-template-columns: minmax(0, 1fr); }
  .only-wide { display: none; }
  .only-narrow { display: block; }
}
"""


# --------------------------------------------------------------------------- #
# Formatting helpers
# --------------------------------------------------------------------------- #
def _fmt(value: float | None, digits: int = 0) -> str:
    """Russian-style number: narrow no-break space as thousands separator, comma decimal."""
    if value is None:
        return "—"
    text = f"{value:,.{digits}f}".replace(",", " ").replace(".", ",")
    return text


def _compact(value: float) -> str:
    if abs(value) >= 1e9:
        return f"{_fmt(value / 1e9, 2)}\u00a0млрд"
    if abs(value) >= 1e6:
        return f"{_fmt(value / 1e6, 2)}\u00a0млн"
    if abs(value) >= 1e3:
        return f"{_fmt(value / 1e3, 1)}\u00a0тыс."
    return _fmt(value)


def _source_units(value: float) -> str:
    return f"{_compact(value)}\u00a0ед. источника"


def _esc(value: object) -> str:
    return html.escape(str(value), quote=True)


# --------------------------------------------------------------------------- #
# SVG charts (inline, no external assets)
# --------------------------------------------------------------------------- #
def _line_chart(
    points: list[tuple[str, float]], *, aria: str, width: int = 960, height: int = 300
) -> str:
    if not points:
        return "<p>Нет данных</p>"
    pad_l, pad_r, pad_t, pad_b = 88, 16, 14, 30
    iw = width - pad_l - pad_r
    ih = height - pad_t - pad_b
    values = [v for _, v in points]
    vmax = max(values) or 1.0
    step = iw / max(len(points) - 1, 1)
    coords = [
        (pad_l + i * step, pad_t + ih - ih * (v / vmax)) for i, v in enumerate(values)
    ]

    parts = [
        f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="{_esc(aria)}">'
    ]
    for frac in (0.0, 0.25, 0.5, 0.75, 1.0):
        gy = pad_t + ih - ih * frac
        parts.append(
            f'<line class="grid-line" x1="{pad_l}" y1="{gy:.1f}" '
            f'x2="{width - pad_r}" y2="{gy:.1f}"/>'
        )
        parts.append(
            f'<text x="{pad_l - 8}" y="{gy + 4:.1f}" text-anchor="end">'
            f"{_compact(vmax * frac)}</text>"
        )

    area = (
        f'M{coords[0][0]:.1f} {pad_t + ih:.1f} '
        + " ".join(f"L{x:.1f} {y:.1f}" for x, y in coords)
        + f" L{coords[-1][0]:.1f} {pad_t + ih:.1f} Z"
    )
    parts.append(f'<path class="area" d="{area}"/>')
    polyline = " ".join(f"{x:.1f},{y:.1f}" for x, y in coords)
    parts.append(f'<polyline class="line" points="{polyline}"/>')

    for (label, value), (cx, cy) in zip(points, coords, strict=True):
        parts.append(
            f'<circle class="dot" cx="{cx:.1f}" cy="{cy:.1f}" r="7">'
            f"<title>{_esc(label)}: {_esc(_compact(value))}</title></circle>"
        )

    ticks = sorted({0, len(points) // 4, len(points) // 2, 3 * len(points) // 4, len(points) - 1})
    for idx in ticks:
        cx = coords[idx][0]
        anchor = "start" if idx == 0 else "end" if idx == len(points) - 1 else "middle"
        parts.append(
            f'<text x="{cx:.1f}" y="{pad_t + ih + 18}" text-anchor="{anchor}">'
            f"{_esc(points[idx][0])}</text>"
        )

    parts.append("</svg>")
    return "".join(parts)


def _hbars(
    items: list[tuple[str, float]],
    *,
    aria: str,
    width: int = 960,
    label_w: int = 168,
    value_w: int = 108,
    bar_h: int = 24,
    gap: int = 10,
) -> str:
    if not items:
        return "<p>Нет данных</p>"
    iw = width - label_w - value_w
    vmax = max(v for _, v in items) or 1.0
    height = len(items) * (bar_h + gap) + 6

    parts = [f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="{_esc(aria)}">']
    for i, (label, value) in enumerate(items):
        y = i * (bar_h + gap) + 3
        bar_w = max(iw * value / vmax, 2.0)
        parts.append(
            f'<text class="label" x="0" y="{y + bar_h * 0.7:.1f}">{_esc(label)}</text>'
        )
        parts.append(
            f'<rect class="bar" x="{label_w}" y="{y}" width="{bar_w:.1f}" '
            f'height="{bar_h}" rx="4"><title>{_esc(label)}: {_esc(_compact(value))}</title></rect>'
        )
        parts.append(
            f'<text x="{label_w + bar_w + 8:.1f}" y="{y + bar_h * 0.7:.1f}">'
            f"{_esc(_compact(value))}</text>"
        )
    parts.append("</svg>")
    return "".join(parts)


def _heatmap(
    cells: dict[tuple[int, int], float],
    *,
    weeks: int,
    aria: str,
    width: int = 960,
    cell_h: int = 24,
    gap: int = 3,
) -> str:
    row_label_w, pad_t, pad_b = 36, 6, 24
    cell_w = (width - row_label_w - gap * (weeks - 1)) / weeks
    height = pad_t + 7 * (cell_h + gap) + pad_b
    vmax = max(cells.values()) if cells else 1.0

    parts = [f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="{_esc(aria)}">']
    for dow in range(7):
        y = pad_t + dow * (cell_h + gap)
        parts.append(
            f'<text x="{row_label_w - 8}" y="{y + cell_h * 0.68:.1f}" '
            f'text-anchor="end">{WEEKDAYS[dow]}</text>'
        )
        for week in range(weeks):
            x = row_label_w + week * (cell_w + gap)
            value = cells.get((dow, week))
            if value is None:
                parts.append(
                    f'<rect class="hm-empty" data-dow="{dow}" data-week="{week}" '
                    f'x="{x:.1f}" y="{y}" width="{cell_w:.1f}" height="{cell_h}" rx="3">'
                    f"<title>{WEEKDAYS[dow]}, неделя {week + 1}: нет данных</title></rect>"
                )
                continue
            opacity = 0.14 + 0.86 * (value / vmax)
            parts.append(
                f'<rect class="hm" data-dow="{dow}" data-week="{week}" x="{x:.1f}" y="{y}" '
                f'width="{cell_w:.1f}" height="{cell_h}" rx="3" fill-opacity="{opacity:.2f}">'
                f"<title>{WEEKDAYS[dow]}, неделя {week + 1}: {_esc(_compact(value))}</title>"
                f"</rect>"
            )
        parts.append("")

    for week in range(0, weeks, 4):
        x = row_label_w + week * (cell_w + gap)
        label = (PERIOD_START + timedelta(days=7 * week)).strftime("%d.%m")
        parts.append(
            f'<text x="{x:.1f}" y="{height - 8}" text-anchor="start">{label}</text>'
        )
    parts.append("</svg>")
    return "".join(parts)


def _pareto_curve(
    shares: list[float],
    *,
    crossing: int,
    aria: str,
    width: int = 960,
    height: int = 300,
) -> str:
    """Cumulative share of volume over cities sorted by volume, with an 80% guide."""
    if not shares:
        return "<p>Нет данных</p>"
    pad_l, pad_r, pad_t, pad_b = 46, 20, 16, 34
    iw = width - pad_l - pad_r
    ih = height - pad_t - pad_b
    n = len(shares)
    step = iw / max(n - 1, 1)

    def y_of(share: float) -> float:
        return pad_t + ih * (1.0 - share)

    coords = [(pad_l + i * step, y_of(s)) for i, s in enumerate(shares)]
    parts = [f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="{_esc(aria)}">']
    for frac in (0.0, 0.25, 0.5, 0.75, 1.0):
        gy = pad_t + ih - ih * frac
        parts.append(
            f'<line class="grid-line" x1="{pad_l}" y1="{gy:.1f}" '
            f'x2="{width - pad_r}" y2="{gy:.1f}"/>'
        )
        parts.append(
            f'<text x="{pad_l - 8}" y="{gy + 4:.1f}" text-anchor="end">'
            f"{int(round(frac * 100))}%</text>"
        )

    area = (
        f"M{coords[0][0]:.1f} {pad_t + ih:.1f} "
        + " ".join(f"L{x:.1f} {y:.1f}" for x, y in coords)
        + f" L{coords[-1][0]:.1f} {pad_t + ih:.1f} Z"
    )
    parts.append(f'<path class="area" d="{area}"/>')
    polyline = " ".join(f"{x:.1f},{y:.1f}" for x, y in coords)
    parts.append(f'<polyline class="cum" points="{polyline}"/>')

    target_y = y_of(0.8)
    parts.append(
        f'<line class="ref" x1="{pad_l}" y1="{target_y:.1f}" '
        f'x2="{width - pad_r}" y2="{target_y:.1f}"/>'
    )
    parts.append(
        f'<text x="{width - pad_r}" y="{target_y - 6:.1f}" text-anchor="end">80% объёма</text>'
    )

    if 0 < crossing < n:
        cx, cy = coords[crossing]
        parts.append(f'<circle class="marker" cx="{cx:.1f}" cy="{cy:.1f}" r="5"/>')
        anchor = "end" if crossing > n * 0.6 else "start"
        dx = -8 if anchor == "end" else 8
        parts.append(
            f'<text class="label" x="{cx + dx:.1f}" y="{cy - 10:.1f}" text-anchor="{anchor}">'
            f"{crossing + 1} городов → 80%</text>"
        )

    for idx in sorted({0, n // 4, n // 2, 3 * n // 4, n - 1}):
        parts.append(
            f'<text x="{coords[idx][0]:.1f}" y="{height - 10}" text-anchor="middle">'
            f"{idx + 1}</text>"
        )
    parts.append("</svg>")
    return "".join(parts)


# --------------------------------------------------------------------------- #
# Data
# --------------------------------------------------------------------------- #
def load_data(con: duckdb.DuckDBPyConnection) -> dict:
    """Read every series the dashboard needs with one connection."""
    totals = con.execute(
        """
        SELECT SUM(qty), SUM(cost), SUM(tickets),
               SUM(qty * weighted_avg_time) / NULLIF(SUM(qty), 0)
        FROM mart_daily_total
        """
    ).fetchone()
    daily = con.execute("SELECT date, tickets FROM mart_daily_total ORDER BY date").fetchall()
    channels = con.execute(
        """
        SELECT channel, SUM(qty), SUM(cost), SUM(tickets)
        FROM mart_delivery GROUP BY 1 ORDER BY 2 DESC
        """
    ).fetchall()
    categories = con.execute(
        """
        SELECT category, SUM(qty), SUM(cost), SUM(tickets)
        FROM mart_delivery GROUP BY 1 ORDER BY 2 DESC
        """
    ).fetchall()
    cities = con.execute(
        """
        SELECT city_name, SUM(tickets) AS orders
        FROM mart_city_daily GROUP BY 1 ORDER BY 2 DESC, 1
        """
    ).fetchall()
    weeks = con.execute(
        """
        SELECT CAST(FLOOR(DATEDIFF('day', DATE '2022-01-01', date) / 7.0) AS INTEGER) AS week,
               day_of_week, SUM(tickets)
        FROM mart_daily_total
        GROUP BY 1, 2
        ORDER BY 1, 2
        """
    ).fetchall()
    n_cities = con.execute(
        "SELECT COUNT(DISTINCT city_name) FROM mart_city_daily"
    ).fetchone()[0]
    peak = con.execute("SELECT MAX(tickets) FROM mart_daily_total").fetchone()[0]
    return {
        "totals": totals,
        "daily": daily,
        "channels": channels,
        "categories": categories,
        "cities": cities,
        "weeks": weeks,
        "n_cities": n_cities,
        "peak": peak,
    }


def _pareto(rows: list[tuple[str, float]]) -> tuple[list[float], int]:
    """Cumulative order shares over order-sorted cities; index where 80% is crossed."""
    total = sum(orders for _, orders in rows)
    if not total:
        return [], -1
    shares: list[float] = []
    running = 0.0
    crossing = -1
    for idx, (_, orders) in enumerate(rows):
        running += orders
        share = running / total
        shares.append(share)
        if crossing < 0 and share >= 0.8:
            crossing = idx
    return shares, crossing


# --------------------------------------------------------------------------- #
# HTML assembly
# --------------------------------------------------------------------------- #
def _kpi(key: str, label: str, display: str, raw: float, hint: str) -> str:
    return (
        f'<div class="kpi" data-kpi="{_esc(key)}" data-value="{raw!r}">'
        f'<span class="kpi-label">{_esc(label)}</span>'
        f'<span class="kpi-value">{display}</span>'
        f'<span class="kpi-hint">{_esc(hint)}</span></div>'
    )


def _card(title: str, subtitle: str, body: str, *, wide: bool = False) -> str:
    cls = "card wide" if wide else "card"
    return (
        f'<div class="{cls}"><h3>{_esc(title)}</h3>'
        f'<p class="sub">{_esc(subtitle)}</p>{body}</div>'
    )


def render(data: dict) -> str:
    qty, cost, tickets, avg_time = data["totals"]
    qty = float(qty or 0)
    cost = float(cost or 0)
    tickets = float(tickets or 0)
    avg_time = float(avg_time or 0)
    items_per_order = qty / tickets if tickets else 0.0
    cost_per_order = cost / tickets if tickets else 0.0
    n_cities = int(data["n_cities"] or 0)
    peak = float(data["peak"] or 0)

    cities = data["cities"]
    shares, crossing = _pareto(cities)
    pareto_cities = crossing + 1 if crossing >= 0 else n_cities
    top_cities = [(name, float(value)) for name, value in cities[:10]]

    daily_points = [(d.strftime("%d.%m"), float(v)) for d, v in data["daily"]]
    week_points = []
    for week in range(26):
        total = sum(float(v) for w, _, v in data["weeks"] if w == week)
        if total:
            week_points.append(
                ((PERIOD_START + timedelta(days=7 * week)).strftime("%d.%m"), total)
            )
    heat_cells = {(int(dow), int(week)): float(v) for week, dow, v in data["weeks"]}

    channel_rows = [(str(name), float(q), float(c), float(t)) for name, q, c, t in data["channels"]]
    category_rows = [
        (str(name), float(q), float(c), float(t)) for name, q, c, t in data["categories"]
    ]

    ops_kpis = "".join(
        [
            _kpi("orders", "Заказы", _compact(tickets), tickets,
                 "сумма заказов (tickets) за период"),
            _kpi("units", "Позиции", _compact(qty), qty, "сумма позиций (qty) за период"),
            _kpi("items_per_order", "Позиций на заказ", _fmt(items_per_order, 2),
                 items_per_order, "позиции / заказы"),
            _kpi("avg_time", "Среднее время", f"{_fmt(avg_time, 1)} мин", avg_time,
                 "взвешено по позициям"),
        ]
    )
    commerce_kpis = "".join(
        [
            _kpi("cost_per_order", "Стоимость заказа", _source_units(cost_per_order),
                 cost_per_order, "cost / заказы (валюта не подтверждена)"),
        ]
    )
    exec_kpis = "".join(
        [
            _kpi("cities", "Городов", _fmt(n_cities), float(n_cities), "в периметре доставки"),
            _kpi("peak_day_orders", "Пиковый день", _compact(peak), peak,
                 "максимум заказов в день"),
            _kpi("pareto_cities_80", "80% заказов", f"{pareto_cities} городов",
                 float(pareto_cities), "правило Парето по заказам"),
        ]
    )

    channel_table = "".join(
        f"<tr><td>{_esc(name)}</td><td class='num'>{_compact(q)}</td>"
        f"<td class='num'>{_compact(c)}</td>"
        f"<td class='num'>{_fmt(100.0 * q / qty if qty else 0.0, 1)}%</td>"
        f"<td class='num'>{_fmt(q / t if t else 0.0, 2)}</td></tr>"
        for name, q, c, t in channel_rows
    )

    ops = f"""
<section id="operations">
  <div class="sec-head"><h2><span class="role-num">01</span>Операции</h2></div>
  <p class="role-note">Роль: операционный менеджер. Заказы, позиции, скорость доставки
  и размер заказа — то, что двигают сменой и маршрутом.</p>
  <div class="kpis">{ops_kpis}</div>
  <div class="grid">
    {_card("Динамика по дням", "Заказы (tickets) в день, 01.01—30.06.2022",
           _line_chart(daily_points, aria="Динамика заказов по дням"), wide=True)}
    {_card("День недели × неделя", "Сумма заказов; цвет — интенсивность заказов",
           f'<div class="scroll">{_heatmap(heat_cells, weeks=26, aria="Теплокарта день недели × неделя")}</div>',
           wide=True)}
  </div>
</section>
"""

    commerce = f"""
<section id="commerce">
  <div class="sec-head"><h2><span class="role-num">02</span>Коммерция</h2></div>
  <p class="role-note">Роль: коммерческий аналитик. Каналы и категории: где заказы,
  где cost и где размер заказа расходится с долей.</p>
  <div class="kpis">{commerce_kpis}</div>
  <div class="grid two">
    {_card("Каналы", "Заказы по каналам продаж",
           _hbars([(name, t) for name, _, _, t in channel_rows],
                  aria="Заказы по каналам", width=420, label_w=112, value_w=92,
                  bar_h=26, gap=12))}
    {_card("Категории", "Заказы по категориям",
           _hbars([(name, t) for name, _, _, t in category_rows],
                  aria="Заказы по категориям", width=420, label_w=112, value_w=92,
                  bar_h=26, gap=12))}
    {_card("Каналы: cost и позиции на заказ", "Доля позиций и позиций на заказ",
           f"<div class='scroll'><table><thead><tr><th>Канал</th>"
           f"<th class='num'>Позиции</th><th class='num'>cost, ед. источника</th>"
           f"<th class='num'>Доля позиций</th><th class='num'>Позиций на заказ</th></tr></thead>"
           f"<tbody>{channel_table}</tbody></table></div>", wide=True)}
  </div>
</section>
"""

    exec_block = f"""
<section id="executive">
  <div class="sec-head"><h2><span class="role-num">03</span>Руководство</h2></div>
  <p class="role-note">Роль: руководитель направления. Тренд по неделям, концентрация
  заказов в городах и хвост, который забирает ресурс без отдачи.</p>
  <div class="kpis">{exec_kpis}</div>
  <div class="grid">
    {_card("Тренд по неделям", "Сумма заказов за неделю",
           _line_chart(week_points, aria="Тренд заказов по неделям"), wide=True)}
    {_card("Топ-10 городов", "Заказы за период",
           f'<div class="only-wide">{_hbars(top_cities, aria="Топ-10 городов по заказам")}</div>'
           f'<div class="only-narrow">{_hbars(top_cities[:6], aria="Топ городов по заказам", width=400, label_w=132, value_w=76, bar_h=28, gap=12)}</div>',
           wide=True)}
    {_card("Парето по городам", f"{n_cities} городов; {pareto_cities} дают 80% заказов",
           _pareto_curve(shares, crossing=crossing, aria="Кумулятивная доля объёма по городам"),
           wide=True)}
  </div>
</section>
"""

    generated = datetime.now().strftime("%d.%m.%Y %H:%M")
    return f"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Delivery Control Tower — дашборд</title>
<style>{CSS}</style>
</head>
<body>
<div class="wrap">
  <header class="hero">
    <p class="eyebrow">Delivery Control Tower</p>
    <h1>Операционная аналитика доставки</h1>
    <p class="meta">Период 01.01.2022 — 30.06.2022 · {_fmt(n_cities)} городов ·
    источник: витрины DuckDB (<code>mart_daily_total</code>, <code>mart_city_daily</code>,
    <code>mart_delivery</code>, <code>dim_city</code>) · собрано {generated}</p>
  </header>
  <main>
    {ops}
    {commerce}
    {exec_block}
  </main>
  <footer>
    <p>Статический офлайн-отчёт: один HTML-файл, инлайновый SVG, без внешних
    ресурсов и запросов на клиенте. Пересобрать:
    <code>uv run python -m delivery.dashboard</code>.</p>
    <p>Интерактивный слой Superset описан в <code>superset/README.md</code>.
    Docker на машине не запускался — конфигурация Superset не проверена в работе;
    этот HTML-дашборд проверен тестами.</p>
  </footer>
</div>
</body>
</html>
"""


def build(db_path: str | Path | None = None, out_path: str | Path | None = None) -> Path:
    """Render the dashboard from DuckDB and return the written path."""
    db_path = Path(db_path) if db_path is not None else cfg.DUCKDB_PATH
    out_path = Path(out_path) if out_path is not None else cfg.REPORTS / "dashboard.html"
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        data = load_data(con)
    finally:
        con.close()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(render(data), encoding="utf-8")
    return out_path


def main() -> None:
    path = build()
    size_kb = path.stat().st_size / 1024
    print(f"dashboard: {path} ({size_kb:.1f} KB)")


if __name__ == "__main__":
    main()
