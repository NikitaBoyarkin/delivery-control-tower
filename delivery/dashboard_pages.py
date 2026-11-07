"""Public, Power-BI-style dashboard as a single self-contained HTML file.

Differences from :mod:`delivery.dashboard`:

* one page with two global filters (channel, category) that recompute every KPI
  and every chart, instead of three fixed role blocks;
* no client-side requests and no libraries -- the aggregated rows are inlined
  into ``window.DATA`` and every chart is inline SVG, so the file can be served
  straight from GitHub Pages;
* granularity is pre-aggregated in DuckDB (day x channel x category and
  city x channel x category), which keeps the page under ~1 MB while still
  reacting to the filters in the browser.

Run with ``uv run python -m delivery.dashboard_pages``; writes ``docs/index.html``
(the folder GitHub Pages publishes). Time metrics are qty-weighted, matching the
rest of the project; the eight source rows with a negative ``avg_time`` are
excluded from the weighted sums and never from the order/position counts.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import duckdb

from delivery import config as cfg

OUT_PATH = cfg.ROOT / "docs" / "index.html"

DAILY_SQL = """
SELECT
    CAST(d.date AS VARCHAR)                     AS day,
    ch.channel_name                             AS channel,
    cat.category_name                           AS category,
    SUM(f.tickets)                              AS tickets,
    SUM(f.qty)                                  AS qty,
    SUM(f.cost)                                 AS cost,
    SUM(CASE WHEN f.avg_time >= 0 THEN f.qty * f.avg_time ELSE 0 END) AS wtime,
    SUM(CASE WHEN f.avg_time >= 0 THEN f.qty ELSE 0 END)             AS time_qty
FROM fact_sales f
JOIN dim_date d      ON d.date_key = f.date_key
JOIN dim_channel ch  ON ch.channel_key = f.channel_key
JOIN dim_category cat ON cat.category_key = f.category_key
GROUP BY 1, 2, 3
ORDER BY 1, 2, 3
"""

CITY_SQL = """
SELECT
    c.city_name                                 AS city,
    ch.channel_name                             AS channel,
    cat.category_name                           AS category,
    SUM(f.tickets)                              AS tickets,
    SUM(f.qty)                                  AS qty,
    SUM(f.cost)                                 AS cost,
    SUM(CASE WHEN f.avg_time >= 0 THEN f.qty * f.avg_time ELSE 0 END) AS wtime,
    SUM(CASE WHEN f.avg_time >= 0 THEN f.qty ELSE 0 END)             AS time_qty
FROM fact_sales f
JOIN dim_city c      ON c.city_key = f.city_key
JOIN dim_channel ch  ON ch.channel_key = f.channel_key
JOIN dim_category cat ON cat.category_key = f.category_key
GROUP BY 1, 2, 3
"""

WEEKDAY_RU = ["Вс", "Пн", "Вт", "Ср", "Чт", "Пт", "Сб"]
MONTH_RU = [
    "",
    "янв",
    "фев",
    "мар",
    "апр",
    "май",
    "июн",
    "июл",
    "авг",
    "сен",
    "окт",
    "ноя",
    "дек",
]


def collect(db_path: Path) -> dict:
    """Read the marts and return the ``window.DATA`` payload."""
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        daily = con.execute(DAILY_SQL).fetchall()
        city = con.execute(CITY_SQL).fetchall()
        totals = con.execute("SELECT COUNT(*), COUNT(DISTINCT city_name) FROM stg_sales").fetchone()
    finally:
        con.close()

    daily_rows = [
        [
            day,
            channel,
            category,
            int(tickets),
            int(qty),
            round(float(cost), 2),
            round(float(wtime), 2),
            int(time_qty),
        ]
        for day, channel, category, tickets, qty, cost, wtime, time_qty in daily
    ]
    city_rows = [
        [
            name,
            channel,
            category,
            int(tickets),
            int(qty),
            round(float(cost), 2),
            round(float(wtime), 2),
            int(time_qty),
        ]
        for name, channel, category, tickets, qty, cost, wtime, time_qty in city
    ]

    days = sorted({row[0] for row in daily_rows})
    months = sorted({row[0][:7] for row in daily_rows})
    channels = sorted({row[1] for row in daily_rows})
    categories = sorted({row[2] for row in daily_rows})
    weekday_order = [1, 2, 3, 4, 5, 6, 0]

    return {
        "meta": {
            "title": "Delivery Control Tower",
            "eyebrow": "Операционный дашборд · 2022-01-01 — 2022-06-30",
            "blurb": (
                "Где процесс доставки теряет время. Все показатели пересчитываются "
                "выбранными фильтрами: канал и категория. Время доставки взвешено "
                "по позициям, cost — деньги в неподтверждённых единицах источника."
            ),
            "orders": {
                "channel": channels,
                "category": categories,
                "month": months,
                "weekday": [WEEKDAY_RU[d] for d in weekday_order],
            },
            "weekdayIndex": weekday_order,
            "monthsRu": MONTH_RU,
            "days": days,
            "source": {
                "factRows": int(totals[0]),
                "cities": int(totals[1]),
                "channels": len(channels),
                "categories": len(categories),
                "period": f"{days[0]} — {days[-1]}",
            },
        },
        "daily": {
            "cols": ["day", "channel", "category", "tickets", "qty", "cost", "wtime", "timeQty"],
            "rows": daily_rows,
        },
        "city": {
            "cols": ["city", "channel", "category", "tickets", "qty", "cost", "wtime", "timeQty"],
            "rows": city_rows,
        },
    }


def build(db_path: Path = cfg.DUCKDB_PATH, out_path: Path = OUT_PATH) -> Path:
    """Aggregate the marts and write the standalone HTML page."""
    payload = collect(db_path)
    data_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    html = TEMPLATE.replace("__DATA__", data_json)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")
    return out_path


TEMPLATE = r"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>Delivery Control Tower — операционный дашборд</title>
<meta name="description" content="Заказы, позиции, время доставки и география по агрегату 2022-01-01 — 2022-06-30: 550 710 строк источника, 264 города, 3 канала, 4 категории.">
<meta name="color-scheme" content="light dark">
<style>
  :root{
    color-scheme: light;
    --bg:#eef1f5; --surface:#ffffff; --surface-2:#f4f6f9;
    --ink:#111827; --ink-secondary:#4b5563; --ink-muted:#8b949e;
    --border:#e3e8ef; --grid:#e8ecf2; --baseline:#cfd6e0;
    --accent:#1f6feb; --accent-soft:#dbe8ff; --good:#0a7d3f; --warn:#c2410c;
    --chip-bg:#e6ebf2; --chip-active-bg:#111827; --chip-active-ink:#ffffff;
    --shadow:0 1px 2px rgba(16,24,40,.05), 0 10px 26px -16px rgba(16,24,40,.22);
    --wave-1:#1f6feb; --wave-2:#e2590c; --wave-3:#0f9d63;
    --wave-4:#b58500; --wave-5:#7c4ddb; --wave-6:#0e7490;
  }
  @media (prefers-color-scheme: dark){
    :root:not([data-theme="light"]){
      color-scheme: dark;
      --bg:#0d1117; --surface:#161b22; --surface-2:#1c232c;
      --ink:#e6edf3; --ink-secondary:#adb8c4; --ink-muted:#7d8792;
      --border:#242d38; --grid:#222a34; --baseline:#39424e;
      --accent:#4d90f5; --accent-soft:#1e2f4a; --good:#2ea043; --warn:#ff8a3d;
      --chip-bg:#212a34; --chip-active-bg:#e6edf3; --chip-active-ink:#161b22;
      --shadow:0 1px 2px rgba(0,0,0,.35), 0 10px 26px -16px rgba(0,0,0,.7);
      --wave-1:#4d90f5; --wave-2:#f2762e; --wave-3:#2bb37b;
      --wave-4:#d8a21b; --wave-5:#a480f0; --wave-6:#1fa3bd;
    }
  }
  :root[data-theme="dark"]{
    color-scheme: dark;
    --bg:#0d1117; --surface:#161b22; --surface-2:#1c232c;
    --ink:#e6edf3; --ink-secondary:#adb8c4; --ink-muted:#7d8792;
    --border:#242d38; --grid:#222a34; --baseline:#39424e;
    --accent:#4d90f5; --accent-soft:#1e2f4a; --good:#2ea043; --warn:#ff8a3d;
    --chip-bg:#212a34; --chip-active-bg:#e6edf3; --chip-active-ink:#161b22;
    --shadow:0 1px 2px rgba(0,0,0,.35), 0 10px 26px -16px rgba(0,0,0,.7);
    --wave-1:#4d90f5; --wave-2:#f2762e; --wave-3:#2bb37b;
    --wave-4:#d8a21b; --wave-5:#a480f0; --wave-6:#1fa3bd;
  }

  *{box-sizing:border-box;}
  body{
    margin:0; background:var(--bg); color:var(--ink); padding-inline:16px;
    font-family:system-ui,-apple-system,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;
    -webkit-font-smoothing:antialiased;
  }
  .page{max-width:1240px; margin:0 auto; padding-block:26px 52px;}
  h1,h2,h3{margin:0; text-wrap:balance;}

  header.top{display:flex; align-items:flex-start; justify-content:space-between; gap:16px;
             padding-bottom:18px; margin-bottom:18px; border-bottom:1px solid var(--border);}
  header.top .eyebrow{font-size:12px; letter-spacing:.08em; text-transform:uppercase;
                      color:var(--ink-muted); font-weight:600;}
  header.top h1{font-size:clamp(23px,3.6vw,31px); font-weight:700; letter-spacing:-.01em; margin-top:4px;}
  header.top p{margin:6px 0 0; color:var(--ink-secondary); font-size:14px; max-width:720px; line-height:1.55;}
  .themebtn{flex-shrink:0; width:38px; height:38px; border-radius:10px; border:1px solid var(--border);
            background:var(--surface); color:var(--ink-secondary); font-size:16px; cursor:pointer;
            box-shadow:var(--shadow);}
  .themebtn:hover{color:var(--ink);}
  .themebtn:focus-visible{outline:2px solid var(--accent); outline-offset:2px;}

  .filterbar{display:flex; align-items:center; gap:16px; flex-wrap:wrap; margin-bottom:18px;}
  .fgroup{display:flex; align-items:center; gap:8px; flex-wrap:wrap; min-width:0; max-width:100%;}
  .flabel{font-size:12.5px; color:var(--ink-muted); font-weight:600;}
  .segmented{display:inline-flex; gap:2px; background:var(--chip-bg); padding:3px; border-radius:10px;
             max-width:100%; overflow-x:auto;}
  .segmented button{font:inherit; font-size:13px; font-weight:500; color:var(--ink-secondary);
                    background:transparent; border:none; cursor:pointer; padding:6px 12px; border-radius:8px;
                    transition:background .12s,color .12s; white-space:nowrap;}
  .segmented button:hover{background:rgba(120,130,145,.18);}
  .segmented button.active{background:var(--chip-active-bg); color:var(--chip-active-ink);}
  .segmented button:focus-visible{outline:2px solid var(--accent); outline-offset:2px;}
  .reset{font:inherit; font-size:12.5px; color:var(--ink-secondary); background:transparent;
         border:1px solid var(--border); border-radius:8px; padding:6px 12px; cursor:pointer;}
  .reset:hover{color:var(--ink); border-color:var(--baseline);}
  .reset:focus-visible{outline:2px solid var(--accent); outline-offset:2px;}
  .count-note{font-size:12.5px; color:var(--ink-muted); margin-left:auto;}

  .kpis{display:grid; grid-template-columns:repeat(auto-fit,minmax(178px,1fr)); gap:12px; margin-bottom:30px;}
  .kpi{background:var(--surface); border:1px solid var(--border); border-radius:14px; padding:15px 17px;
       box-shadow:var(--shadow); display:flex; flex-direction:column; gap:3px;}
  .kpi .klabel{font-size:12.5px; color:var(--ink-muted); font-weight:500;}
  .kpi .kvalue{font-size:25px; font-weight:700; letter-spacing:-.015em; font-variant-numeric:tabular-nums;}
  .kpi .ksub{font-size:12px; color:var(--ink-muted);}
  .kdelta{font-size:12.5px; font-weight:600; font-variant-numeric:tabular-nums;}
  .kdelta.pos{color:var(--good);}
  .kdelta.neg{color:var(--warn);}

  section.block{margin-bottom:30px;}
  section.block .sechead{margin-bottom:12px;}
  section.block h2{font-size:18px; font-weight:700;}
  section.block .sechead p{margin:4px 0 0; color:var(--ink-secondary); font-size:13.5px;
                           max-width:760px; line-height:1.5;}

  .panels-grid{display:grid; gap:14px;}
  .panels-grid.two-col{grid-template-columns:1fr 1fr;}
  @media (max-width:900px){ .panels-grid.two-col{grid-template-columns:1fr;} }

  .panel{background:var(--surface); border:1px solid var(--border); border-radius:14px;
         padding:16px 16px 12px; box-shadow:var(--shadow); min-width:0; display:flex;
         flex-direction:column;}
  .phead{display:flex; align-items:flex-start; justify-content:space-between; gap:12px; margin-bottom:4px;}
  .panel h3{font-size:14px; font-weight:650;}
  .panel-sub{margin:0 0 8px; color:var(--ink-secondary); font-size:12.5px; line-height:1.45; max-width:700px;}
  .legend{display:flex; gap:11px; flex-wrap:wrap; flex-shrink:0;}
  .legend .lkey{display:flex; align-items:center; gap:5px; font-size:12px; color:var(--ink-secondary);
                font-weight:500; white-space:nowrap;}
  .legend .ldot{width:9px; height:9px; border-radius:2px; display:inline-block;}

  .chartwrap{position:relative; width:100%;}
  .chartwrap svg{display:block; width:100%; height:auto; overflow:visible;}
  .chartwrap svg text{font-family:inherit; fill:var(--ink-muted); font-variant-numeric:tabular-nums;}

  .tooltip{position:absolute; pointer-events:none; z-index:5; background:var(--ink); color:var(--surface);
           border-radius:8px; padding:8px 10px; font-size:12px; line-height:1.45;
           box-shadow:0 4px 16px rgba(0,0,0,.28); opacity:0; transform:translate(-50%,-100%);
           transition:opacity .08s; max-width:260px;}
  .tooltip.show{opacity:1;}
  .tooltip .thead{font-weight:600; opacity:.7; margin-bottom:3px;}
  .tooltip .trow{display:flex; align-items:baseline; gap:6px;}
  .tooltip .tkey{width:7px; height:7px; border-radius:2px; display:inline-block; flex-shrink:0;}
  .tooltip .tval{font-weight:700; font-variant-numeric:tabular-nums; margin-left:auto;}

  details.tblfold{margin-top:8px; border-top:1px solid var(--border); padding-top:7px;}
  details.tblfold summary{cursor:pointer; font-size:12px; color:var(--ink-muted); font-weight:500;
                          list-style:none; display:flex; align-items:center; gap:5px;}
  details.tblfold summary::-webkit-details-marker{display:none;}
  details.tblfold summary::before{content:"\25B8"; font-size:10px;}
  details.tblfold[open] summary::before{content:"\25BE";}
  .tblwrap{overflow-x:auto; margin-top:6px; max-width:100%;}
  table.datatable{border-collapse:collapse; width:100%; font-size:12px;}
  table.datatable th,table.datatable td{padding:5px 9px; text-align:right;
                                        border-bottom:1px solid var(--border); white-space:nowrap;}
  table.datatable th:first-child,table.datatable td:first-child{text-align:left;}
  table.datatable th{color:var(--ink-muted); font-weight:600; position:sticky; top:0; background:var(--surface);}
  table.datatable td{font-variant-numeric:tabular-nums; color:var(--ink-secondary);}
  table.datatable td.strong{color:var(--ink); font-weight:600;}

  .cards{display:grid; grid-template-columns:repeat(auto-fit,minmax(250px,1fr)); gap:14px;}
  .card{background:var(--surface); border:1px solid var(--border); border-radius:14px; padding:16px;
        box-shadow:var(--shadow); display:flex; flex-direction:column; gap:10px;}
  .card-head{display:flex; align-items:baseline; justify-content:space-between; gap:8px;}
  .card-head h3{font-size:15px; font-weight:700;}
  .card-share{font-size:12px; color:var(--ink-muted); font-weight:600;}
  .statrow{display:flex; align-items:baseline; justify-content:space-between; gap:10px;
           padding-bottom:7px; border-bottom:1px solid var(--border);}
  .statrow:last-child{border-bottom:none; padding-bottom:0;}
  .statrow .slabel{font-size:12.5px; color:var(--ink-muted);}
  .statrow .sval{font-size:14.5px; font-weight:700; font-variant-numeric:tabular-nums;}

  footer.notes{margin-top:20px; padding-top:16px; border-top:1px solid var(--border);
               color:var(--ink-muted); font-size:12.5px; line-height:1.6;}
  footer.notes p{margin:0 0 8px;}
  footer.notes strong{color:var(--ink-secondary);}
  footer.notes a{color:var(--accent);}

  @media (max-width:420px){
    .kpis{grid-template-columns:1fr 1fr;}
    .kpi .kvalue{font-size:21px;}
    .count-note{margin-left:0;}
  }
  @media (max-width:560px){
    table.datatable th,table.datatable td{white-space:normal;}
    .panel{padding:14px 12px 10px;}
  }
</style>
</head>
<body>
<div class="page">
  <header class="top">
    <div>
      <span class="eyebrow" id="eyebrow"></span>
      <h1 id="pageTitle"></h1>
      <p id="pageBlurb"></p>
    </div>
    <button class="themebtn" id="themeBtn" type="button" aria-label="Переключить тему">◐</button>
  </header>

  <div class="filterbar">
    <div class="fgroup"><span class="flabel">Канал</span><div class="segmented" id="chSeg" role="group" aria-label="Фильтр по каналу"></div></div>
    <div class="fgroup"><span class="flabel">Категория</span><div class="segmented" id="catSeg" role="group" aria-label="Фильтр по категории"></div></div>
    <button class="reset" id="resetBtn" type="button">Сбросить</button>
    <span class="count-note" id="countNote"></span>
  </div>

  <section class="kpis" id="kpis" aria-label="Ключевые показатели"></section>
  <div id="sections"></div>
  <footer class="notes" id="footer"></footer>
</div>

<script>window.DATA = __DATA__;</script>
<script>
(function(){
"use strict";

var D = window.DATA, M = D.meta;
var NS = "http://www.w3.org/2000/svg";
var CHANS = M.orders.channel, CATS = M.orders.category;
var MONTHS = M.orders.month, WDAYS = M.orders.weekday, WIDX = M.weekdayIndex;
var DAYS = M.days;
var SRC = M.source;

var DAILY = D.daily.rows.map(function(r){
  return {d:r[0], ch:r[1], cat:r[2], tk:r[3], qty:r[4], cost:r[5], wt:r[6], wq:r[7]};
});
var CITY = D.city.rows.map(function(r){
  return {c:r[0], ch:r[1], cat:r[2], tk:r[3], qty:r[4], cost:r[5], wt:r[6], wq:r[7]};
});

var state = {channel:"all", category:"all", metric:"orders"};

/* ---------- форматирование ---------- */
var nf = new Intl.NumberFormat("ru-RU");
function fmtInt(v){ return nf.format(Math.round(v)); }
function fmt1(v){
  return v.toLocaleString("ru-RU", {minimumFractionDigits:1, maximumFractionDigits:1});
}
function fmt2(v){
  return v.toLocaleString("ru-RU", {minimumFractionDigits:2, maximumFractionDigits:2});
}
function fmtCompact(v){
  var a = Math.abs(v);
  if(a >= 1e9) return fmt1(v/1e9) + " млрд";
  if(a >= 1e6) return fmt1(v/1e6) + " млн";
  if(a >= 1e4) return fmt1(v/1e3) + " тыс.";
  return fmtInt(v);
}
function fmtPct(v){ return (Math.round(v*10)/10).toLocaleString("ru-RU") + "%"; }
function fmtSigned(v){
  var s = (Math.round(Math.abs(v)*10)/10).toLocaleString("ru-RU");
  return (v > 0 ? "+" : v < 0 ? "−" : "±") + s;
}

/* ---------- тема и цвет ---------- */
function token(name){
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}
function slot(i){ return token("--wave-" + (((i % 6) + 6) % 6 + 1)); }
function isDark(){
  var stamp = document.documentElement.getAttribute("data-theme");
  if(stamp === "dark") return true;
  if(stamp === "light") return false;
  return !!(window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches);
}

/* ---------- фильтры ---------- */
function pass(r){
  if(state.channel !== "all" && r.ch !== state.channel) return false;
  if(state.category !== "all" && r.cat !== state.category) return false;
  return true;
}
function metricVal(a){
  if(state.metric === "items") return a.qty;
  if(state.metric === "time") return a.wq > 0 ? a.wt / a.wq : null;
  return a.tk;
}
function metricName(){
  return state.metric === "items" ? "позиции"
       : state.metric === "time" ? "время доставки" : "заказы";
}
function metricFull(v){
  if(v == null) return "—";
  return state.metric === "time" ? fmt1(v) + " мин" : fmtCompact(v);
}
function metricAxis(v){
  if(state.metric === "time") return Math.round(v) + " мин";
  return fmtCompact(v);
}

/* ---------- агрегация ---------- */
function blank(){ return {tk:0, qty:0, cost:0, wt:0, wq:0}; }
function add(a, r){
  a.tk += r.tk; a.qty += r.qty; a.cost += r.cost; a.wt += r.wt; a.wq += r.wq;
  return a;
}
function totals(rows){
  var a = blank();
  rows.forEach(function(r){ add(a, r); });
  return a;
}
function byMonth(rows, channel){
  var map = {};
  rows.forEach(function(r){
    if(channel && r.ch !== channel) return;
    var k = r.d.slice(0,7);
    (map[k] = map[k] || blank());
    add(map[k], r);
  });
  return map;
}
function byCity(rows){
  var map = {};
  rows.forEach(function(r){
    var a = map[r.c] = map[r.c] || blank();
    add(a, r);
  });
  return map;
}

/* ---------- DOM-хелперы ---------- */
function el(tag, cls, text){
  var n = document.createElement(tag);
  if(cls) n.className = cls;
  if(text != null) n.textContent = text;
  return n;
}
function sv(tag, attrs){
  var n = document.createElementNS(NS, tag);
  for(var k in attrs) if(attrs[k] != null) n.setAttribute(k, attrs[k]);
  return n;
}
function text(svg, x, y, str, attrs){
  var node = sv("text", Object.assign({x:x, y:y}, attrs || {}));
  node.textContent = str;
  svg.appendChild(node);
  return node;
}
function niceTicks(maxVal, count){
  if(!(maxVal > 0)) maxVal = 1;
  var raw = maxVal / count;
  var mag = Math.pow(10, Math.floor(Math.log10(raw)));
  var norm = raw / mag;
  var step = norm < 1.5 ? 1*mag : norm < 3 ? 2*mag : norm < 7 ? 5*mag : 10*mag;
  var ticks = [], v = 0;
  while(v < maxVal + step*0.999){ ticks.push(Math.round(v*100)/100); v += step; }
  return ticks;
}
function chart(body, h){
  body.innerHTML = "";
  var wrap = el("div", "chartwrap");
  body.appendChild(wrap);
  var w = Math.max(280, wrap.clientWidth || 700);
  var svg = sv("svg", {viewBox:"0 0 " + w + " " + h, role:"img"});
  wrap.appendChild(svg);
  var tip = el("div", "tooltip");
  wrap.appendChild(tip);
  return {wrap:wrap, svg:svg, tip:tip, w:w, h:h};
}
function showTip(tip, wrap, x, y, head, rows){
  tip.textContent = "";
  tip.appendChild(el("div", "thead", head));
  rows.forEach(function(r){
    var row = el("div", "trow");
    var key = el("span", "tkey");
    key.style.background = r.color;
    row.appendChild(key);
    row.appendChild(el("span", null, r.label));
    row.appendChild(el("span", "tval", r.value));
    tip.appendChild(row);
  });
  var w = wrap.getBoundingClientRect().width;
  tip.style.left = Math.max(70, Math.min(w - 70, x)) + "px";
  tip.style.top = Math.max(30, y - 8) + "px";
  tip.classList.add("show");
}
function hideTip(tip){ tip.classList.remove("show"); }

/* ---------- таблица-фоллбек ---------- */
function addTable(body, headers, rows){
  var details = el("details", "tblfold");
  details.appendChild(el("summary", null, "Показать как таблицу"));
  var wrap = el("div", "tblwrap");
  var table = el("table", "datatable");
  var thead = el("thead"), tr = el("tr");
  headers.forEach(function(h){ tr.appendChild(el("th", null, h)); });
  thead.appendChild(tr);
  table.appendChild(thead);
  var tbody = el("tbody");
  rows.forEach(function(r){
    var row = el("tr");
    r.forEach(function(cell, i){ row.appendChild(el("td", i === 0 ? "strong" : null, cell)); });
    tbody.appendChild(row);
  });
  table.appendChild(tbody);
  wrap.appendChild(table);
  details.appendChild(wrap);
  body.appendChild(details);
}

/* ---------- каркас ---------- */
function section(title, sub){
  var sec = el("section", "block");
  var head = el("div", "sechead");
  head.appendChild(el("h2", null, title));
  if(sub) head.appendChild(el("p", null, sub));
  sec.appendChild(head);
  var grid = el("div", "panels-grid");
  sec.appendChild(grid);
  return {sec:sec, grid:grid};
}
function panel(grid, title, sub, right){
  var p = el("div", "panel");
  var head = el("div", "phead");
  var left = el("div");
  left.appendChild(el("h3", null, title));
  head.appendChild(left);
  if(right) head.appendChild(right);
  p.appendChild(head);
  if(sub) p.appendChild(el("p", "panel-sub", sub));
  var body = el("div");
  p.appendChild(body);
  grid.appendChild(p);
  return body;
}
function legend(items){
  var lg = el("div", "legend");
  items.forEach(function(it){
    var key = el("span", "lkey");
    var dot = el("span", "ldot");
    dot.style.background = it.color;
    key.appendChild(dot);
    key.appendChild(document.createTextNode(it.label));
    lg.appendChild(key);
  });
  return lg;
}
function segmented(options, active, onPick){
  var box = el("div", "segmented");
  options.forEach(function(opt){
    var b = el("button", null, opt.label);
    b.type = "button";
    if(opt.value === active) b.classList.add("active");
    b.setAttribute("aria-pressed", opt.value === active ? "true" : "false");
    b.addEventListener("click", function(){ onPick(opt.value); });
    box.appendChild(b);
  });
  return box;
}

/* ---------- KPI ---------- */
function renderKpis(){
  var rows = DAILY.filter(pass);
  var t = totals(rows);
  var months = byMonth(rows);
  var keys = Object.keys(months).sort();
  var last = keys.length ? months[keys[keys.length-1]] : null;
  var prev = keys.length > 1 ? months[keys[keys.length-2]] : null;
  var cityMap = byCity(CITY.filter(pass));
  var cities = Object.keys(cityMap).filter(function(c){ return cityMap[c].tk > 0; }).length;

  var time = t.wq > 0 ? t.wt / t.wq : null;
  var prevTime = prev && prev.wq > 0 ? prev.wt / prev.wq : null;
  var lastTime = last && last.wq > 0 ? last.wt / last.wq : null;
  var box = document.getElementById("kpis");
  box.textContent = "";

  function tile(label, value, sub, delta){
    var k = el("div", "kpi");
    k.appendChild(el("span", "klabel", label));
    k.appendChild(el("span", "kvalue", value));
    if(delta){
      var d = el("span", "kdelta " + (delta.good ? "pos" : "neg"), delta.text);
      k.appendChild(d);
      if(delta.sub) k.appendChild(el("span", "ksub", delta.sub));
    } else if(sub){
      k.appendChild(el("span", "ksub", sub));
    }
    box.appendChild(k);
  }

  var dOrders = last && prev && prev.tk ? (last.tk - prev.tk) / prev.tk * 100 : null;
  tile("Заказы", fmtCompact(t.tk), "чеки за период", dOrders == null ? null : {
    text: fmtSigned(dOrders) + "% м/м",
    good: dOrders >= 0,
    sub: keys.length + " мес. в выборке"
  });
  tile("Позиции", fmtCompact(t.qty), "позиций в заказах", null);
  tile("Позиций на заказ", t.tk ? fmt2(t.qty / t.tk) : "—", "средняя корзина", null);
  tile("Время доставки", time == null ? "—" : fmt1(time) + " мин", "взвешено по позициям",
    lastTime != null && prevTime != null ? {
      text: fmtSigned(lastTime - prevTime) + " мин м/м",
      good: lastTime <= prevTime,
      sub: last && prev ? "последний месяц к предыдущему" : null
    } : null);
  tile("Cost на заказ", t.tk ? fmt1(t.cost / t.tk) : "—", "единицы источника", null);
  tile("Городов", fmtInt(cities), "с хотя бы одним заказом", null);

  document.getElementById("countNote").textContent =
    "в срезе: " + fmtCompact(t.tk) + " заказов, " + fmtCompact(t.qty) + " позиций, " + fmtInt(cities) + " городов";
}

/* ---------- линейные графики ---------- */
function renderLines(body, cfg){
  var ch = chart(body, cfg.height || 290);
  var svg = ch.svg, w = ch.w, h = ch.h;
  var pad = {l:60, r:20, t:26, b:34};
  var pw = w - pad.l - pad.r, ph = h - pad.t - pad.b;
  var max = 0;
  cfg.series.forEach(function(s){
    s.vals.forEach(function(v){ if(v != null && v > max) max = v; });
  });
  var ticks = niceTicks(max || 1, 4);
  var top = ticks[ticks.length - 1];
  function yv(v){ return pad.t + ph - (v / top) * ph; }
  var n = cfg.labels.length;
  function xv(i){ return n <= 1 ? pad.l + pw/2 : pad.l + (i / (n - 1)) * pw; }

  ticks.forEach(function(t){
    var y = yv(t);
    svg.appendChild(sv("line", {x1:pad.l, x2:pad.l + pw, y1:y, y2:y, stroke:"var(--grid)", "stroke-width":1}));
    text(svg, pad.l - 9, y + 4, cfg.fmtAxis(t), {"text-anchor":"end", "font-size":11});
  });
  svg.appendChild(sv("line", {x1:pad.l, x2:pad.l + pw, y1:pad.t + ph, y2:pad.t + ph,
    stroke:"var(--baseline)", "stroke-width":1}));

  (cfg.xTicks || []).forEach(function(item){
    text(svg, xv(item.i), h - pad.b + 18, item.label, {"text-anchor":"middle", "font-size":11});
  });

  cfg.series.forEach(function(s){
    var d = "", pen = false;
    s.vals.forEach(function(v, i){
      if(v == null){ pen = false; return; }
      d += (pen ? "L" : "M") + xv(i).toFixed(1) + " " + yv(v).toFixed(1) + " ";
      pen = true;
    });
    svg.appendChild(sv("path", {d:d, fill:"none", stroke:s.color, "stroke-width":2,
      "stroke-linejoin":"round", "stroke-linecap":"round"}));
  });

  var guide = sv("line", {x1:0, x2:0, y1:pad.t, y2:pad.t + ph, stroke:"var(--baseline)",
    "stroke-width":1, "stroke-dasharray":"3 3", opacity:0});
  svg.appendChild(guide);
  var dots = cfg.series.map(function(s){
    var c = sv("circle", {r:3.5, fill:s.color, stroke:"var(--surface)", "stroke-width":2, opacity:0});
    svg.appendChild(c);
    return c;
  });
  var hit = sv("rect", {x:pad.l, y:pad.t, width:Math.max(1, pw), height:ph, fill:"transparent"});
  svg.appendChild(hit);

  var step = n > 1 ? pw / (n - 1) : pw;
  hit.addEventListener("pointermove", function(evt){
    var r = ch.wrap.getBoundingClientRect();
    var px = evt.clientX - r.left;
    var idx = Math.max(0, Math.min(n - 1, Math.round((px - pad.l) / step)));
    var gx = xv(idx);
    guide.setAttribute("x1", gx); guide.setAttribute("x2", gx); guide.setAttribute("opacity", 1);
    var rows = [];
    cfg.series.forEach(function(s, si){
      var v = s.vals[idx];
      var dot = dots[si];
      if(v == null){ dot.setAttribute("opacity", 0); return; }
      dot.setAttribute("cx", gx); dot.setAttribute("cy", yv(v)); dot.setAttribute("opacity", 1);
      rows.push({color:s.color, label:s.name, value:cfg.fmtVal(v)});
    });
    showTip(ch.tip, ch.wrap, gx, yv(cfg.tipY == null ? top : cfg.tipY(idx)), cfg.labelFull(idx), rows);
  });
  hit.addEventListener("pointerleave", function(){
    hideTip(ch.tip);
    guide.setAttribute("opacity", 0);
    dots.forEach(function(d){ d.setAttribute("opacity", 0); });
  });
}

/* ---------- секция 1: динамика ---------- */
function renderTrend(){
  var rows = DAILY.filter(pass);
  var cats = state.category === "all" ? CATS : [state.category];
  var maps = {};
  cats.forEach(function(c){ maps[c] = {}; });
  rows.forEach(function(r){
    if(!maps[r.cat]) return;
    var a = maps[r.cat][r.d] = maps[r.cat][r.d] || blank();
    add(a, r);
  });
  var series = cats.map(function(c){
    return {
      name:c,
      color:slot(CATS.indexOf(c)),
      vals:DAYS.map(function(d){
        var a = maps[c][d];
        return a ? metricVal(a) : null;
      })
    };
  });

  var xTicks = [];
  DAYS.forEach(function(d, i){
    if(d.slice(8) === "01") xTicks.push({i:i, label:M.monthsRu[parseInt(d.slice(5,7), 10)]});
  });
  if(!xTicks.length || xTicks[xTicks.length-1].i !== DAYS.length - 1){
    var lastD = DAYS[DAYS.length - 1];
    xTicks.push({i:DAYS.length - 1, label:lastD.slice(8) + "." + lastD.slice(5,7)});
  }

  function dayLabel(i){
    var d = DAYS[i];
    return d.slice(8) + "." + d.slice(5,7) + "." + d.slice(0,4) + " · " + WDAYS[(new Date(d + "T00:00:00Z")).getUTCDay()];
  }

  var sec = section(
    "Динамика по дням",
    "Каждая линия — категория; цвет закреплён за категорией и не меняется при фильтрации. Переключатель справа меняет метрику всех графиков ниже."
  );

  var toggle = segmented([
    {value:"orders", label:"Заказы"},
    {value:"items", label:"Позиции"},
    {value:"time", label:"Время"}
  ], state.metric, function(v){ state.metric = v; renderAll(); });

  var body = panel(sec.grid, "Дневной ряд, " + metricName(), null, toggle);
  body.parentNode.querySelector(".phead").style.flexWrap = "wrap";
  var lg = legend(series.map(function(s){ return {label:s.name, color:s.color}; }));
  body.parentNode.insertBefore(lg, body);

  renderLines(body, {
    series:series,
    labels:DAYS,
    xTicks:xTicks,
    fmtAxis:metricAxis,
    fmtVal:metricFull,
    labelFull:dayLabel,
    height:300
  });

  addTable(body, ["Дата"].concat(cats).concat(["Итого"]), DAYS.map(function(d, i){
    var vals = series.map(function(s){ return s.vals[i] == null ? "—" : metricFull(s.vals[i]); });
    var sum = 0, has = false;
    series.forEach(function(s){ if(s.vals[i] != null){ sum += s.vals[i]; has = true; } });
    return [d].concat(vals).concat([has ? metricFull(sum) : "—"]);
  }));

  document.getElementById("sections").appendChild(sec.sec);
}

/* ---------- секция 2: профиль недели ---------- */
function renderWeek(){
  var rows = DAILY.filter(pass);
  var cells = {};
  var max = 0;
  rows.forEach(function(r){
    var month = r.d.slice(0,7);
    var wd = (new Date(r.d + "T00:00:00Z")).getUTCDay();
    var key = month + "|" + wd;
    var a = cells[key] = cells[key] || blank();
    add(a, r);
  });
  MONTHS.forEach(function(m){
    WIDX.forEach(function(wd){
      var a = cells[m + "|" + wd];
      if(!a) return;
      var v = metricVal(a);
      if(v != null && v > max) max = v;
    });
  });

  var sec = section(
    "Профиль недели",
    "Сумма по каждой ячейке «месяц × день недели». Насыщенность цвета — доля от максимума в текущем срезе; цифра в ячейке — значение метрики."
  );
  var body = panel(sec.grid, "Месяц × день недели", null);
  var ch = chart(body, 26 + MONTHS.length * 40 + 26);
  var svg = ch.svg, w = ch.w;
  var padL = 44, padT = 22, cellGap = 3;
  var colW = (w - padL - 8) / WIDX.length;
  var rowH = 40;

  WIDX.forEach(function(wd, ci){
    text(svg, padL + ci * colW + colW/2, padT - 6, WDAYS[ci],
      {"text-anchor":"middle", "font-size":11});
  });
  MONTHS.forEach(function(m, ri){
    var y = padT + ri * rowH;
    text(svg, padL - 8, y + rowH/2 + 4, m.slice(2), {"text-anchor":"end", "font-size":11});
    WIDX.forEach(function(wd, ci){
      var a = cells[m + "|" + wd];
      var v = a ? metricVal(a) : null;
      var x = padL + ci * colW;
      var alpha = v == null || !max ? 0.04 : 0.07 + 0.78 * (v / max);
      var rect = sv("rect", {x:x + cellGap/2, y:y + cellGap/2,
        width:colW - cellGap, height:rowH - cellGap, rx:5,
        fill:token("--accent"), "fill-opacity":alpha});
      svg.appendChild(rect);
      if(v != null){
        text(svg, x + colW/2, y + rowH/2 + 4, metricFull(v),
          {"text-anchor":"middle", "font-size":11,
           fill:alpha > 0.5 ? (isDark() ? "#0d1117" : "#ffffff") : "var(--ink-secondary)"});
      }
      rect.addEventListener("pointerenter", function(evt){
        var r = ch.wrap.getBoundingClientRect();
        showTip(ch.tip, ch.wrap, evt.clientX - r.left, evt.clientY - r.top,
          m + " · " + WDAYS[ci], [{color:token("--accent"), label:"Всего", value:metricFull(v)}]);
      });
      rect.addEventListener("pointerleave", function(){ hideTip(ch.tip); });
    });
  });

  var tableRows = MONTHS.map(function(m){
    return [m].concat(WIDX.map(function(wd){
      var a = cells[m + "|" + wd];
      return a ? metricFull(metricVal(a)) : "—";
    }));
  });
  addTable(body, ["Месяц"].concat(WDAYS), tableRows);
  document.getElementById("sections").appendChild(sec.sec);
}

/* ---------- секция 3: география ---------- */
function renderCityBars(body, items, fmt, colorFor, tipNote){
  var rowH = 22, padL = 132, padR = 112, padT = 6;
  var h = padT + items.length * rowH + 10;
  var ch = chart(body, h);
  var svg = ch.svg, w = ch.w;
  var max = 0, min = Infinity;
  items.forEach(function(it){
    if(it.value > max) max = it.value;
    if(it.value < min) min = it.value;
  });
  // Шкала начинается не от нуля, когда значения лежат в узком диапазоне:
  // иначе «худшие города» дают одинаковые полосы на всю ширину и разница не читается.
  var floor = max - min < max * 0.1 ? min : 0;
  var barMax = w - padL - padR;

  items.forEach(function(it, i){
    var y = padT + i * rowH;
    var bw = max > floor ? ((it.value - floor) / (max - floor)) * barMax : barMax * 0.4;
    text(svg, padL - 8, y + rowH/2 + 4, it.label.length > 18 ? it.label.slice(0, 17) + "…" : it.label,
      {"text-anchor":"end", "font-size":11.5, fill:"var(--ink-secondary)"});
    var rect = sv("rect", {x:padL, y:y + 4, width:Math.max(1, bw), height:rowH - 10, rx:3,
      fill:colorFor(it)});
    svg.appendChild(rect);
    text(svg, padL + bw + 7, y + rowH/2 + 4, fmt(it.value), {"font-size":11, fill:"var(--ink-secondary)"});
    rect.addEventListener("pointerenter", function(evt){
      var r = ch.wrap.getBoundingClientRect();
      showTip(ch.tip, ch.wrap, evt.clientX - r.left, evt.clientY - r.top, it.label,
        [{color:colorFor(it), label:tipNote, value:fmt(it.value)}].concat(it.extra || []));
    });
    rect.addEventListener("pointerleave", function(){ hideTip(ch.tip); });
  });
}

function renderGeo(){
  var rows = CITY.filter(pass);
  var map = byCity(rows);
  var all = Object.keys(map).map(function(name){ return {name:name, a:map[name]}; });

  var top = all.slice().sort(function(a, b){ return b.a.tk - a.a.tk; }).slice(0, 15);
  var totalTk = 0;
  all.forEach(function(it){ totalTk += it.a.tk; });

  var heavy = all.filter(function(it){ return it.a.tk >= 30000 && it.a.wq > 0; })
    .map(function(it){ return {name:it.name, time:it.a.wt / it.a.wq, tk:it.a.tk}; })
    .sort(function(a, b){ return b.time - a.time; })
    .slice(0, 12);

  var sec = section(
    "География",
    "Концентрация выручки по заказам и города, где доставка идёт дольше всего. Оба списка считаются из тех же отфильтрованных данных."
  );

  var bodyA = panel(sec.grid, "Топ-15 городов по заказам", "Доля в текущем срезе — подпись справа от столбца.");
  sec.grid.lastChild.classList.add("half");
  var bodyB = panel(sec.grid, "Самые долгие доставки", "Города от 30 тыс. заказов в срезе; значение — среднее время, взвешенное по позициям. Полосы масштабированы от минимума списка: подпись справа — абсолютное значение.");

  renderCityBars(bodyA,
    top.map(function(it){
      return {label:it.name, value:it.a.tk, extra:[{
        color:slot(1), label:"время доставки",
        value:it.a.wq > 0 ? fmt1(it.a.wt / it.a.wq) + " мин" : "—"
      }]};
    }),
    function(v){ return fmtCompact(v) + " · " + fmtPct(v / totalTk * 100); },
    function(){ return token("--accent"); },
    "заказы");

  renderCityBars(bodyB,
    heavy.map(function(it){
      return {label:it.name, value:it.time, extra:[
        {color:slot(0), label:"заказы", value:fmtCompact(it.tk)}
      ]};
    }),
    function(v){ return fmt1(v) + " мин"; },
    function(){ return token("--wave-2"); },
    "время доставки");

  addTable(bodyA, ["Город", "Заказы", "Доля", "Время, мин"], top.map(function(it){
    return [it.name, fmtInt(it.a.tk), fmtPct(it.a.tk / totalTk * 100),
      it.a.wq > 0 ? fmt1(it.a.wt / it.a.wq) : "—"];
  }));
  addTable(bodyB, ["Город", "Время, мин", "Заказы"], heavy.map(function(it){
    return [it.name, fmt1(it.time), fmtInt(it.tk)];
  }));

  document.getElementById("sections").appendChild(sec.sec);
}

/* ---------- секция 4: время по категории и каналу ---------- */
function renderTimeMatrix(){
  var rows = DAILY.filter(pass);
  var chans = state.channel === "all" ? CHANS : [state.channel];
  var cats = state.category === "all" ? CATS : [state.category];
  var cells = {};
  rows.forEach(function(r){
    var key = r.cat + "|" + r.ch;
    var a = cells[key] = cells[key] || blank();
    add(a, r);
  });
  var groups = cats.map(function(c){
    return {label:c, values:chans.map(function(ch){
      var a = cells[c + "|" + ch];
      return {name:ch, color:slot(CHANS.indexOf(ch)), value:a && a.wq > 0 ? a.wt / a.wq : null};
    })};
  });

  var sec = section(
    "Время доставки: категория × канал",
    "Главный разрыв в данных — не нагрузка, а категория: закуски идут около 40 минут, пицца — около 26. Разрыв держится во всех каналах."
  );
  var body = panel(sec.grid, "Среднее время, взвешенное по позициям", null,
    legend(chans.map(function(ch){ return {label:ch, color:slot(CHANS.indexOf(ch))}; })));

  var h = 300, ch = chart(body, h);
  var svg = ch.svg, w = ch.w;
  var pad = {l:56, r:16, t:22, b:40};
  var pw = w - pad.l - pad.r, ph = h - pad.t - pad.b;
  var max = 0;
  groups.forEach(function(g){ g.values.forEach(function(v){ if(v.value != null && v.value > max) max = v.value; }); });
  var ticks = niceTicks((max || 1) * 1.12, 4);
  var top = ticks[ticks.length - 1];
  function yv(v){ return pad.t + ph - (v / top) * ph; }

  ticks.forEach(function(t){
    var y = yv(t);
    svg.appendChild(sv("line", {x1:pad.l, x2:pad.l + pw, y1:y, y2:y, stroke:"var(--grid)"}));
    text(svg, pad.l - 9, y + 4, Math.round(t) + " мин", {"text-anchor":"end", "font-size":11});
  });
  svg.appendChild(sv("line", {x1:pad.l, x2:pad.l + pw, y1:pad.t + ph, y2:pad.t + ph,
    stroke:"var(--baseline)"}));

  var band = pw / groups.length;
  var inner = band * 0.62;
  var bw = inner / groups.length;

  groups.forEach(function(g, gi){
    var x0 = pad.l + gi * band + (band - inner) / 2;
    text(svg, pad.l + gi * band + band/2, h - pad.b + 18, g.label,
      {"text-anchor":"middle", "font-size":11.5, fill:"var(--ink-secondary)"});
    g.values.forEach(function(v, vi){
      if(v.value == null) return;
      var x = x0 + vi * bw;
      var y = yv(v.value);
      var rect = sv("rect", {x:x, y:y, width:Math.max(2, bw - 3), height:pad.t + ph - y,
        rx:3, fill:v.color});
      svg.appendChild(rect);
      if(bw - 3 >= 30){
        text(svg, x + (bw - 3)/2, y - 6, fmt1(v.value), {"text-anchor":"middle", "font-size":10.5,
          fill:"var(--ink-secondary)"});
      }
      rect.addEventListener("pointerenter", function(evt){
        var r = ch.wrap.getBoundingClientRect();
        showTip(ch.tip, ch.wrap, evt.clientX - r.left, evt.clientY - r.top,
          g.label + " · " + v.name,
          [{color:v.color, label:"время доставки", value:fmt1(v.value) + " мин"}]);
      });
      rect.addEventListener("pointerleave", function(){ hideTip(ch.tip); });
    });
  });

  addTable(body, ["Категория"].concat(chans), groups.map(function(g){
    return [g.label].concat(g.values.map(function(v){ return v.value == null ? "—" : fmt1(v.value); }));
  }));
  document.getElementById("sections").appendChild(sec.sec);
}

/* ---------- секция 5: карточки каналов ---------- */
function renderCards(){
  var rows = DAILY.filter(pass);
  var map = {};
  rows.forEach(function(r){
    var a = map[r.ch] = map[r.ch] || blank();
    add(a, r);
  });
  var totalTk = 0;
  Object.keys(map).forEach(function(k){ totalTk += map[k].tk; });
  var sec = section(
    "Каналы",
    "Микс сдвигается в маркетплейс: доля mp в паре mp+web растёт с 81.9% в январе до 87.6% в июне (предзарегистрированная гипотеза H1 подтверждена)."
  );
  var box = el("div", "cards");
  sec.grid.appendChild(box);

  Object.keys(map).sort().forEach(function(ch){
    var a = map[ch];
    var card = el("div", "card");
    var head = el("div", "card-head");
    var title = el("h3", null, ch);
    title.style.color = slot(CHANS.indexOf(ch));
    head.appendChild(title);
    head.appendChild(el("span", "card-share", totalTk ? fmtPct(a.tk / totalTk * 100) + " заказов" : "—"));
    card.appendChild(head);

    function row(label, value){
      var r = el("div", "statrow");
      r.appendChild(el("span", "slabel", label));
      r.appendChild(el("span", "sval", value));
      card.appendChild(r);
    }
    row("Заказы", fmtCompact(a.tk));
    row("Позиции", fmtCompact(a.qty));
    row("Позиций на заказ", a.tk ? fmt2(a.qty / a.tk) : "—");
    row("Время доставки", a.wq > 0 ? fmt1(a.wt / a.wq) + " мин" : "—");
    row("Cost на заказ", a.tk ? fmt1(a.cost / a.tk) : "—");
    box.appendChild(card);
  });

  document.getElementById("sections").appendChild(sec.sec);
}

/* ---------- секция 6: месячная сводка ---------- */
function renderMonthTable(){
  var rows = DAILY.filter(pass);
  var map = byMonth(rows);
  var keys = Object.keys(map).sort();
  var sec = section(
    "Сводка по месяцам",
    "Значения пересчитаны фильтрами; Δ заказов — к предыдущему месяцу в выборке."
  );
  var body = panel(sec.grid, "Месяц к месяцу", null);
  var headers = ["Месяц", "Заказы", "Δ заказов", "Позиции", "Позиций на заказ", "Время, мин", "Cost на заказ"];
  var tableRows = keys.map(function(k, i){
    var a = map[k];
    var prev = i > 0 ? map[keys[i-1]] : null;
    var d = prev && prev.tk ? (a.tk - prev.tk) / prev.tk * 100 : null;
    return [k, fmtInt(a.tk), d == null ? "—" : fmtSigned(d) + "%", fmtInt(a.qty),
      a.tk ? fmt2(a.qty / a.tk) : "—", a.wq > 0 ? fmt1(a.wt / a.wq) : "—",
      a.tk ? fmt1(a.cost / a.tk) : "—"];
  });
  var wrap = el("div", "tblwrap");
  var table = el("table", "datatable");
  var thead = el("thead"), tr = el("tr");
  headers.forEach(function(h){ tr.appendChild(el("th", null, h)); });
  thead.appendChild(tr);
  table.appendChild(thead);
  var tbody = el("tbody");
  tableRows.forEach(function(r){
    var row = el("tr");
    r.forEach(function(cell, i){ row.appendChild(el("td", i === 0 ? "strong" : null, cell)); });
    tbody.appendChild(row);
  });
  table.appendChild(tbody);
  wrap.appendChild(table);
  body.appendChild(wrap);

  document.getElementById("sections").appendChild(sec.sec);
}

/* ---------- футер ---------- */
function renderFooter(){
  var rows = DAILY.filter(pass);
  var t = totals(rows);
  var cities = Object.keys(byCity(CITY.filter(pass))).filter(function(c){ return true; }).length;
  var box = document.getElementById("footer");
  box.textContent = "";

  function p(strong, text){
    var node = el("p");
    if(strong){
      var b = el("strong", null, strong + " ");
      node.appendChild(b);
    }
    node.appendChild(document.createTextNode(text));
    box.appendChild(node);
  }

  p("Источник.",
    "Агрегат из тестового задания для аналитика (Dodo Brands): день × город × канал × категория, " +
    SRC.period + ". В источнике " + fmtInt(SRC.factRows) + " строк, " + fmtInt(SRC.cities) +
    " городов, " + SRC.channels + " канала и " + SRC.categories + " категории. Панель пересчитывается " +
    "из пересобранного DuckDB-марта, а не из ручных выгрузок.");
  p("Выборка в текущем срезе.",
    fmtInt(t.tk) + " заказов, " + fmtInt(t.qty) + " позиций, " + fmtInt(cities) +
    " городов с заказами. Заказ (tickets) — это чек; позиция (qty) — единица товара.");
  p("Метрики.",
    "Время доставки — среднее по ячейке, взвешенное по позициям: SUM(qty × avg_time) / SUM(qty). " +
    "Восемь исходных строк с отрицательным avg_time (167 позиций) исключены из средних, но остаются " +
    "в заказах и позициях. Cost — деньги в неподтверждённых единицах источника: валюту в данных никто " +
    "не описал, поэтому cost нигде не назван выручкой. avg_price не участвует в расчётах: он не " +
    "сходится с SUM(cost) / SUM(tickets).");
  p("Ограничение.",
    "Данные уже агрегированы до «день × город × канал × категория», поэтому панель описывает " +
    "ассоциации на наблюдаемых агрегатах, а не эффекты процесса. Причину разрыва между категориями " +
    "на этих данных установить нельзя.");
  p(null,
    "Файл самодостаточен: ни одной внешней библиотеки или запроса из браузера. Генератор — " +
    "delivery/dashboard_pages.py, источник цифр — data/marts/delivery.duckdb.");
}

/* ---------- рендер ---------- */
function renderAll(){
  document.getElementById("sections").textContent = "";
  renderKpis();
  renderTrend();
  renderWeek();
  renderGeo();
  renderTimeMatrix();
  renderCards();
  renderMonthTable();
  renderFooter();
}

function buildFilterControls(){
  var chBox = document.getElementById("chSeg");
  chBox.textContent = "";
  var chOpts = [{value:"all", label:"Все"}].concat(CHANS.map(function(c){ return {value:c, label:c}; }));
  chOpts.forEach(function(opt){
    var b = el("button", null, opt.label);
    b.type = "button";
    if(state.channel === opt.value) b.classList.add("active");
    b.setAttribute("aria-pressed", state.channel === opt.value ? "true" : "false");
    b.addEventListener("click", function(){
      state.channel = opt.value;
      buildFilterControls();
      renderAll();
    });
    chBox.appendChild(b);
  });

  var catBox = document.getElementById("catSeg");
  catBox.textContent = "";
  var catOpts = [{value:"all", label:"Все"}].concat(CATS.map(function(c){ return {value:c, label:c}; }));
  catOpts.forEach(function(opt){
    var b = el("button", null, opt.label);
    b.type = "button";
    if(state.category === opt.value) b.classList.add("active");
    b.setAttribute("aria-pressed", state.category === opt.value ? "true" : "false");
    b.addEventListener("click", function(){
      state.category = opt.value;
      buildFilterControls();
      renderAll();
    });
    catBox.appendChild(b);
  });
}

function applyTheme(stamp){
  if(stamp) document.documentElement.setAttribute("data-theme", stamp);
  else document.documentElement.removeAttribute("data-theme");
  try{
    if(stamp) localStorage.setItem("dct-theme", stamp);
    else localStorage.removeItem("dct-theme");
  }catch(e){ /* приватный режим: тема живёт до перезагрузки */ }
  renderAll();
}

function initTheme(){
  var stored = null;
  try{ stored = localStorage.getItem("dct-theme"); }catch(e){ stored = null; }
  if(stored === "dark" || stored === "light"){
    document.documentElement.setAttribute("data-theme", stored);
  }
  var btn = document.getElementById("themeBtn");
  btn.addEventListener("click", function(){
    var current = document.documentElement.getAttribute("data-theme");
    if(!current) current = isDark() ? "dark" : "light";
    applyTheme(current === "dark" ? "light" : "dark");
  });
}

document.getElementById("eyebrow").textContent = M.eyebrow;
document.getElementById("pageTitle").textContent = M.title;
document.getElementById("pageBlurb").textContent = M.blurb;

document.getElementById("resetBtn").addEventListener("click", function(){
  state.channel = "all";
  state.category = "all";
  buildFilterControls();
  renderAll();
});

initTheme();
buildFilterControls();
renderAll();

var raf = null;
window.addEventListener("resize", function(){
  clearTimeout(raf);
  raf = setTimeout(renderAll, 160);
});
var mq = window.matchMedia("(prefers-color-scheme: dark)");
if(mq.addEventListener){
  mq.addEventListener("change", function(){
    if(!document.documentElement.getAttribute("data-theme")) renderAll();
  });
}
})();
</script>
</body>
</html>
"""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=cfg.DUCKDB_PATH)
    parser.add_argument("--out", type=Path, default=OUT_PATH)
    args = parser.parse_args()
    path = build(db_path=args.db, out_path=args.out)
    size_kb = path.stat().st_size / 1024
    print(f"wrote {path} ({size_kb:.0f} KB)")


if __name__ == "__main__":
    main()
