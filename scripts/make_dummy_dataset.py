"""Generate a realistic, schema-identical dummy dataset for the delivery control tower.

The two source CSVs (a proprietary test-task aggregate and an order sample) are not
published, so a clone cannot run the pipeline. ``scripts/make_synthetic_data.py``
covers CI with a deliberately small fixture; this module produces a *full-size*
stand-in that the whole pipeline accepts and that reproduces the aggregates the
README and ``docs/data_dictionary.md`` publish:

===========================================  ==========================
published value                              target
===========================================  ==========================
aggregate rows                               550 710
order rows                                   130 079
``SUM(qty)``                                 57 576 550
``SUM(tickets)``                             33 167 293
``SUM(cost)``                                4 606 030 381.34
``SUM(clients_qty)``                         31 255 898
``AVG(avg_price)``                           221.8055
items per order                              1.7359
clients per ticket                           0.9424
cost per ticket                              138.8727
===========================================  ==========================

What is modelled, not copied: city sizes (log-normal plus Moscow/St Petersburg
shares), weekday and month seasonality, public-holiday spikes, the channel mix
drifting from web to marketplace, category-level delivery-time constants, a weak
negative volume/time association (Spearman rho ~ -0.09), and the sparse cell
pattern (small cities miss more channel x category cells).

This is **not** the original dataset and must never be cited as one. It exists so
the pipeline is runnable and testable without the proprietary files.

Usage::

    uv run python scripts/make_dummy_dataset.py                     # -> data/synthetic/
    uv run python scripts/make_dummy_dataset.py --out /tmp/dummy
    uv run python scripts/make_dummy_dataset.py --validate          # build + check contract
    uv run python scripts/make_dummy_dataset.py --validate --full   # + forecast/bottlenecks/H1-H2
"""

from __future__ import annotations

import argparse
import shutil
import tempfile
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

CITIES: tuple[str, ...] = (
    "Абакан",
    "Абинск",
    "Азов",
    "Аксай Россия",
    "Александров",
    "Альметьевск",
    "Анапа",
    "Апатиты",
    "Апрелевка",
    "Арзамас",
    "Архангельск",
    "Астрахань",
    "Балаково",
    "Балашиха",
    "Балтийск",
    "Барнаул",
    "Батайск",
    "Белгород",
    "Белогорск",
    "Бердск",
    "Березники",
    "Благовещенск",
    "Братск",
    "Брянск",
    "Бугульма",
    "Бузулук",
    "Великий Новгород",
    "Верхняя Пышма",
    "Видное",
    "Владивосток",
    "Владикавказ",
    "Владимир",
    "Внуково",
    "Волгоград",
    "Волжский",
    "Вологда",
    "Волхов",
    "Воркута",
    "Воронеж",
    "Воскресенск",
    "Воткинск",
    "Всеволожск",
    "Выборг",
    "Вышний Волочек",
    "Вязьма",
    "Гатчина",
    "Геленджик",
    "Глазов",
    "Голицыно",
    "Горно-Алтайск",
    "Горячий Ключ",
    "Грозный",
    "Губкин",
    "Губкинский",
    "Дедовск",
    "Дзержинск",
    "Димитровград",
    "Дмитров",
    "Долгопрудный",
    "Домодедово",
    "Дубна",
    "Егорьевск",
    "Екатеринбург",
    "Елабуга",
    "Ессентуки",
    "Железнодорожный",
    "Жуковский",
    "Заречный",
    "Звенигород",
    "Зеленоград",
    "Златоуст",
    "Иваново",
    "Ивантеевка",
    "Ижевск",
    "Иркутск",
    "Истра",
    "Йошкар-Ола",
    "Казань",
    "Калининград",
    "Калуга",
    "Каменск-Уральский",
    "Каменск-Шахтинский",
    "Камышин",
    "Канск",
    "Кемерово",
    "Кингисепп",
    "Кириши",
    "Киров",
    "Клин",
    "Ковров",
    "Когалым",
    "Коломна",
    "Колпино",
    "Коммунарка",
    "Комсомольск-на-Амуре",
    "Конаково",
    "Копейск",
    "Королев",
    "Корсаков",
    "Котлас",
    "Красногорск",
    "Краснодар",
    "Красное Село",
    "Краснокамск",
    "Красноярск",
    "Кронштадт",
    "Кропоткин",
    "Крымск",
    "Кстово",
    "Кубинка",
    "Кудрово",
    "Куйбышев",
    "Курган",
    "Курск",
    "Лабытнанги",
    "Лангепас",
    "Лениногорск",
    "Ленинск-Кузнецкий",
    "Ликино-Дулево",
    "Липецк",
    "Лобня",
    "Луга",
    "Лыткарино",
    "Люберцы",
    "Лянтор",
    "Магадан",
    "Магнитогорск",
    "Майкоп",
    "Махачкала",
    "Металлострой",
    "Миасс",
    "Минеральные Воды",
    "Можайск",
    "Москва",
    "Московский",
    "Муравленко",
    "Мурино",
    "Мурманск",
    "Муром",
    "Мытищи",
    "Набережные Челны",
    "Надым",
    "Наро-Фоминск",
    "Нахабино",
    "Находка",
    "Невинномысск",
    "Нефтекамск",
    "Нефтеюганск",
    "Нижневартовск",
    "Нижнекамск",
    "Нижний Новгород",
    "Нижний Тагил",
    "Никольское",
    "Новокузнецк",
    "Новокуйбышевск",
    "Новороссийск",
    "Новосибирск",
    "Новоуральск",
    "Новочебоксарск",
    "Новый Уренгой",
    "Ногинск",
    "Норильск",
    "Ноябрьск",
    "Нягань",
    "Обнинск",
    "Одинцово",
    "Октябрьский",
    "Омск",
    "Орел",
    "Оренбург",
    "Орехово-Зуево",
    "Орск",
    "Павловский Посад",
    "Парголово",
    "Пенза",
    "Переславль-Залесский",
    "Пермь",
    "Петергоф",
    "Петрозаводск",
    "Петропавловск-Камчатский",
    "Подольск",
    "Полевской",
    "Прокопьевск",
    "Псков",
    "Пушкин",
    "Пушкино",
    "Пятигорск",
    "Раменское",
    "Рассказовка",
    "Реутов",
    "Ржев",
    "Ростов-на-Дону",
    "Рыбинск",
    "Рязань",
    "Салават",
    "Салехард",
    "Самара",
    "Санкт-Петербург",
    "Саранск",
    "Сарапул",
    "Саратов",
    "Саров",
    "Северодвинск",
    "Северск",
    "Сергиев Посад",
    "Серпухов",
    "Сертолово",
    "Славянка",
    "Смоленск",
    "Солнечногорск",
    "Сосновый Бор",
    "Сочи",
    "Ставрополь",
    "Старая Купавна",
    "Старый Оскол",
    "Стерлитамак",
    "Стрежевой",
    "Ступино",
    "Сургут",
    "Сызрань",
    "Сыктывкар",
    "Таганрог",
    "Тамбов",
    "Тверь",
    "Тобольск",
    "Тольятти",
    "Томск",
    "Торжок",
    "Тосно",
    "Троицк",
    "Туапсе",
    "Туймазы",
    "Тула",
    "Тюмень",
    "Улан-Удэ",
    "Ульяновск",
    "Урай",
    "Усинск",
    "Усолье-Сибирское",
    "Уссурийск",
    "Уфа",
    "Ухта",
    "Фрязино",
    "Хабаровск",
    "Ханты-Мансийск",
    "Химки",
    "Хотьково",
    "Чайковский",
    "Чебоксары",
    "Челябинск",
    "Череповец",
    "Чехов",
    "Чита",
    "Шатура",
    "Шлиссельбург",
    "Шушары",
    "Щелково",
    "Щербинка",
    "Электросталь",
    "Электроугли",
    "Энгельс",
    "Югорск",
    "Южно-Сахалинск",
    "Якутск",
)

CHANNELS: tuple[str, ...] = ("mp", "web", "other")
CATEGORIES: tuple[str, ...] = ("пицца", "закуски", "напитки", "десерты")

#: Category-level constants measured on the source aggregate. ``time_base`` is the
#: weighted mean delivery time, ``time_sigma`` the within-city daily spread.
CATEGORY_PROFILE: dict[str, dict[str, float]] = {
    "пицца": {
        "time_base": 26.550,
        "time_sigma": 4.2,
        "items_per_order": 1.6576,
        "cost_per_item": 122.11,
        "price_per_item": 451.82,
        "clients_per_ticket": 0.9470,
    },
    "закуски": {
        "time_base": 39.973,
        "time_sigma": 1.8,
        "items_per_order": 2.0432,
        "cost_per_item": 56.98,
        "price_per_item": 163.86,
        "clients_per_ticket": 0.9439,
    },
    "напитки": {
        "time_base": 30.009,
        "time_sigma": 7.8,
        "items_per_order": 1.5658,
        "cost_per_item": 34.94,
        "price_per_item": 80.40,
        "clients_per_ticket": 0.9248,
    },
    "десерты": {
        "time_base": 24.986,
        "time_sigma": 2.8,
        "items_per_order": 1.5170,
        "cost_per_item": 51.04,
        "price_per_item": 139.03,
        "clients_per_ticket": 0.9449,
    },
}

#: Category share of volume inside each channel (rows sum to 1).
CATEGORY_SHARE_BY_CHANNEL: dict[str, dict[str, float]] = {
    "mp": {"пицца": 0.4107, "закуски": 0.3552, "напитки": 0.1192, "десерты": 0.1148},
    "web": {"пицца": 0.5403, "закуски": 0.2988, "напитки": 0.0763, "десерты": 0.0846},
    "other": {"пицца": 0.3011, "закуски": 0.2829, "напитки": 0.3054, "десерты": 0.1106},
}

#: Channel share of volume by month; the web-to-marketplace drift drives H1.
CHANNEL_SHARE_BY_MONTH: dict[int, dict[str, float]] = {
    1: {"mp": 0.7260, "web": 0.1615, "other": 0.1125},
    2: {"mp": 0.7331, "web": 0.1564, "other": 0.1104},
    3: {"mp": 0.7174, "web": 0.1371, "other": 0.1455},
    4: {"mp": 0.7172, "web": 0.1253, "other": 0.1575},
    5: {"mp": 0.7237, "web": 0.1093, "other": 0.1670},
    6: {"mp": 0.7161, "web": 0.1014, "other": 0.1826},
}

WEEKDAY_FACTOR = {0: 0.837, 1: 0.873, 2: 0.889, 3: 0.882, 4: 1.160, 5: 1.265, 6: 1.099}
MONTH_FACTOR = {1: 0.882, 2: 0.933, 3: 0.908, 4: 1.012, 5: 1.147, 6: 1.115}

#: Public-holiday multipliers relative to the same weekday (measured on the source).
HOLIDAYS: dict[str, float] = {
    "01-01": 0.58,
    "01-03": 1.29,
    "01-08": 0.75,
    "02-14": 1.29,
    "02-23": 1.85,
    "03-03": 0.77,
    "03-04": 0.64,
    "03-07": 1.29,
    "03-08": 1.70,
    "03-09": 0.73,
    "05-02": 1.50,
    "05-09": 1.75,
    "05-10": 1.32,
    "05-25": 1.35,
    "05-26": 1.30,
    "06-01": 1.60,
    "06-13": 1.48,
}

#: Published targets, used when ``--scale 1.0`` (the full profile).
TARGETS: dict[str, float] = {
    "agg_rows": 550_710,
    "order_rows": 130_079,
    "qty": 57_576_550,
    "tickets": 33_167_293,
    "cost": 4_606_030_381.344505,
    "clients": 31_255_898,
    "avg_price_mean": 221.80552474488167,
    "order_clients": 31_426,
    "city1_orders": 105_540,
}

DATE_MIN = "2022-01-01"
DATE_MAX = "2022-06-30"
DATE_FORMAT = "%Y-%m-%dT00:00:00.000Z"


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _project_box(
    weights: np.ndarray, total: float, lower: float | np.ndarray, upper: float | np.ndarray
) -> np.ndarray:
    """Scale ``weights`` so they sum to ``total`` while staying inside the box.

    Iterative clipping (water filling): the continuous allocation is clipped to
    the bounds and the leftover is spread over the coordinates that still have
    headroom. The result is the closest point in the box to the proportional
    allocation.
    """
    lo = np.broadcast_to(np.asarray(lower, dtype=float), weights.shape).astype(float)
    hi = np.broadcast_to(np.asarray(upper, dtype=float), weights.shape).astype(float)
    if total < lo.sum() - 1e-9 or total > hi.sum() + 1e-9:
        raise ValueError(f"infeasible allocation: total={total}, box=[{lo.sum()}, {hi.sum()}]")

    x = np.asarray(weights, dtype=float)
    x = x / x.sum() * total
    for _ in range(200):
        x = np.clip(x, lo, hi)
        gap = total - x.sum()
        if abs(gap) <= 1e-6:
            return x
        free = x < hi if gap > 0 else x > lo
        if not free.any():
            break
        room = (hi - x)[free] if gap > 0 else (x - lo)[free]
        if room.sum() <= 0:
            break
        x[free] += gap * room / room.sum()
    raise RuntimeError("box projection did not converge")


def _integerize(
    x: np.ndarray, total: int, lower: int | np.ndarray, upper: int | np.ndarray
) -> np.ndarray:
    """Round a continuous allocation to integers with an exact sum and bounds."""
    lo = np.broadcast_to(np.asarray(lower, dtype=np.int64), x.shape).astype(np.int64)
    hi = np.broadcast_to(np.asarray(upper, dtype=np.int64), x.shape).astype(np.int64)
    values = np.clip(np.floor(x).astype(np.int64), lo, hi)
    gap = int(total - values.sum())
    frac = x - np.floor(x)

    if gap > 0:
        candidates = np.flatnonzero(values < hi)
        if not len(candidates):
            raise RuntimeError("integer allocation has no cell with headroom")
        order = candidates[np.argsort(-frac[candidates], kind="stable")]
        cursor = 0
        while gap > 0:
            index = order[cursor % len(order)]
            if values[index] < hi[index]:
                values[index] += 1
                gap -= 1
            cursor += 1
    elif gap < 0:
        candidates = np.flatnonzero(values > lo)
        if not len(candidates):
            raise RuntimeError("integer allocation has no cell to reduce")
        order = candidates[np.argsort(frac[candidates], kind="stable")]
        cursor = 0
        while gap < 0:
            index = order[cursor % len(order)]
            if values[index] > lo[index]:
                values[index] -= 1
                gap += 1
            cursor += 1
    return values


def _allocate(
    raw: np.ndarray,
    total: int,
    lower: int | np.ndarray,
    upper: int | np.ndarray,
) -> np.ndarray:
    """Integer allocation of ``total`` across cells with per-cell bounds."""
    projected = _project_box(np.asarray(raw, dtype=float), float(total), lower, upper)
    return _integerize(projected, int(total), lower, upper)


def _day_factor(dates: pd.DatetimeIndex) -> np.ndarray:
    """Weekday x month x holiday multiplier, normalised to mean 1 over the window."""
    weekday = np.array([WEEKDAY_FACTOR[d] for d in dates.day_of_week], dtype=float)
    month = np.array([MONTH_FACTOR[m] for m in dates.month], dtype=float)
    holiday = np.array([HOLIDAYS.get(d.strftime("%m-%d"), 1.0) for d in dates], dtype=float)
    factor = weekday * month * holiday
    return factor / factor.mean()


def _channel_shares(dates: pd.DatetimeIndex) -> np.ndarray:
    """Daily channel share matrix ``(days, channels)`` interpolated between months."""
    anchors = sorted(CHANNEL_SHARE_BY_MONTH)
    anchor_positions = [15 + 30.4 * (m - 1) for m in anchors]
    positions = np.array([(d - pd.Timestamp(DATE_MIN)).days for d in dates], dtype=float)
    shares = np.empty((len(dates), len(CHANNELS)))
    for j, channel in enumerate(CHANNELS):
        values = [CHANNEL_SHARE_BY_MONTH[m][channel] for m in anchors]
        shares[:, j] = np.interp(positions, anchor_positions, values)
    return shares / shares.sum(axis=1, keepdims=True)


def _city_weights(rng: np.random.Generator, cities: tuple[str, ...]) -> np.ndarray:
    """Log-normal city sizes with Moscow and St Petersburg pinned to real shares."""
    raw = rng.lognormal(mean=0.0, sigma=0.80, size=len(cities))
    weights = pd.Series(raw, index=cities, dtype=float)
    rest = weights.drop(["Москва", "Санкт-Петербург"], errors="ignore")
    others = rest.sum()
    if "Москва" in weights.index:
        weights["Москва"] = 0.15305 * others
    if "Санкт-Петербург" in weights.index:
        weights["Санкт-Петербург"] = 0.08766 * others
    return weights.to_numpy()


# --------------------------------------------------------------------------- #
# aggregate dataset
# --------------------------------------------------------------------------- #
def build_aggregate(
    rng: np.random.Generator,
    cities: tuple[str, ...] = CITIES,
    days: int = 181,
    calibrated: bool = True,
) -> pd.DataFrame:
    """Build the day x city x channel x category aggregate."""
    dates = pd.date_range(DATE_MIN, periods=days, freq="D")
    grid = pd.MultiIndex.from_product(
        [dates, cities, CHANNELS, CATEGORIES],
        names=["date", "city_name", "channel", "category"],
    ).to_frame(index=False)

    day_factor = pd.Series(_day_factor(dates), index=dates)
    channel_shares = pd.DataFrame(_channel_shares(dates), index=dates, columns=list(CHANNELS))
    city_weights = pd.Series(_city_weights(rng, cities), index=cities)
    city_weights = city_weights / city_weights.sum()

    grid["city_day_qty"] = (
        grid["city_name"].map(city_weights).to_numpy()
        * grid["date"].map(day_factor).to_numpy()
        * rng.lognormal(0.0, 0.10, len(grid))
    )
    grid["channel_share"] = [
        channel_shares.at[date, channel]
        for date, channel in zip(grid["date"], grid["channel"], strict=True)
    ]
    grid["category_share"] = [
        CATEGORY_SHARE_BY_CHANNEL[channel][category]
        for channel, category in zip(grid["channel"], grid["category"], strict=True)
    ]
    grid["weight"] = (
        grid["city_day_qty"]
        * grid["channel_share"]
        * grid["category_share"]
        * rng.lognormal(0.0, 0.14, len(grid))
    )

    # Sparse cells: small cities and non-mp channels miss more, as in the source.
    log_city = np.log(grid["city_name"].map(city_weights).to_numpy())
    z_city = (log_city - log_city.mean()) / log_city.std()
    channel_penalty = grid["channel"].map({"mp": -2.2, "web": 0.15, "other": 0.0}).to_numpy()
    category_penalty = (
        grid["category"]
        .map({"пицца": -1.6, "закуски": -0.6, "напитки": 0.35, "десерты": 0.55})
        .to_numpy()
    )
    score = -1.35 * z_city + channel_penalty + category_penalty + rng.gumbel(0.0, 1.0, len(grid))
    n_missing = len(grid) - (TARGETS["agg_rows"] if calibrated else len(grid))
    if n_missing > 0:
        drop = np.argsort(-score)[:n_missing]
        grid = grid.drop(index=drop).reset_index(drop=True)

    # Never let a channel x category disappear: the bottleneck table asserts all 12.
    present = grid.groupby(["channel", "category"]).size()
    if len(present) < len(CHANNELS) * len(CATEGORIES):
        raise RuntimeError("sparsity removed a whole channel x category segment")

    qty_total = int(round(TARGETS["qty"] * len(grid) / TARGETS["agg_rows"]))
    qty = _allocate(grid["weight"].to_numpy(), qty_total, lower=1, upper=10**9)

    grid["qty"] = qty
    items_per_order = (
        grid["category"]
        .map({name: profile["items_per_order"] for name, profile in CATEGORY_PROFILE.items()})
        .to_numpy()
    )
    clients_per_ticket = (
        grid["category"]
        .map({name: profile["clients_per_ticket"] for name, profile in CATEGORY_PROFILE.items()})
        .to_numpy()
    )
    cost_per_item = (
        grid["category"]
        .map({name: profile["cost_per_item"] for name, profile in CATEGORY_PROFILE.items()})
        .to_numpy()
    )
    price_per_item = (
        grid["category"]
        .map({name: profile["price_per_item"] for name, profile in CATEGORY_PROFILE.items()})
        .to_numpy()
    )

    grid["tickets"] = _allocate(
        qty / items_per_order * rng.lognormal(0.0, 0.06, len(grid)),
        int(TARGETS["tickets"] * len(grid) / TARGETS["agg_rows"])
        if calibrated
        else int(qty.sum() / 1.74),
        lower=1,
        upper=qty,
    )
    grid["clients_qty"] = _allocate(
        grid["tickets"] * clients_per_ticket * rng.lognormal(0.0, 0.03, len(grid)),
        int(TARGETS["clients"] * len(grid) / TARGETS["agg_rows"])
        if calibrated
        else int(grid["tickets"].sum() * 0.94),
        lower=1,
        upper=grid["tickets"].to_numpy(),
    )

    cost_raw = qty * cost_per_item * rng.lognormal(0.0, 0.08, len(grid))
    cost_total = TARGETS["cost"] * len(grid) / TARGETS["agg_rows"] if calibrated else cost_raw.sum()
    grid["cost"] = np.round(cost_raw * cost_total / cost_raw.sum(), 8)

    price = price_per_item * rng.lognormal(0.0, 0.08, len(grid))
    if calibrated:
        price = price * TARGETS["avg_price_mean"] / price.mean()
    # Mirror the source layout: rto is rounded, avg_price is derived from it.
    grid["rto"] = np.round(qty * price, 4)
    grid["avg_price"] = grid["rto"] / qty
    grid["rto_do"] = np.round(grid["rto"] * 1.083 * rng.lognormal(0.0, 0.08, len(grid)), 2)

    # Delivery time: category constant + city offset + daily noise, weekends slightly faster.
    city_qty = grid.groupby("city_name")["qty"].transform("sum")
    log_size = np.log(city_qty.to_numpy())
    city_offset = -0.229 * (log_size - log_size.mean())
    city_offset += rng.normal(0.0, 0.45, len(cities))[
        pd.Categorical(grid["city_name"], categories=list(cities)).codes
    ]
    weekend = (grid["date"].dt.day_of_week >= 5).to_numpy()
    noise = np.array(
        [rng.normal(0.0, CATEGORY_PROFILE[category]["time_sigma"]) for category in grid["category"]]
    )
    grid["avg_time"] = (
        grid["category"].map(lambda c: CATEGORY_PROFILE[c]["time_base"]).to_numpy()
        + city_offset
        + noise
        - 0.25 * weekend
    )
    grid["avg_time"] = np.maximum(grid["avg_time"], 0.5)

    grid["Date"] = grid["date"].dt.strftime(DATE_FORMAT)
    grid = grid.rename(
        columns={"city_name": "CityName", "channel": "OrderSource", "category": "Category"}
    )
    columns = [
        "Date",
        "CityName",
        "OrderSource",
        "Category",
        "rto",
        "rto_do",
        "cost",
        "qty",
        "tickets",
        "clients_qty",
        "avg_time",
        "avg_price",
    ]
    return grid.sort_values(["Date", "CityName", "OrderSource", "Category"])[columns].reset_index(
        drop=True
    )


# --------------------------------------------------------------------------- #
# order-level sample
# --------------------------------------------------------------------------- #
SALES_QUANTILES = np.array(
    [
        0.0,
        0.002,
        0.01,
        0.05,
        0.10,
        0.20,
        0.30,
        0.40,
        0.50,
        0.60,
        0.70,
        0.80,
        0.90,
        0.95,
        0.99,
        0.999,
        1.0,
    ]
)
SALES_VALUES = np.array(
    [
        1.0,
        1.0,
        1.0,
        115.0,
        219.0,
        468.0,
        664.0,
        758.0,
        859.0,
        999.0,
        1137.0,
        1351.0,
        1712.0,
        2153.0,
        3799.0,
        8000.0,
        16098.0,
    ]
)

#: Share of clients by number of orders, k = 1..20, measured on the source sample.
CLIENT_COUNT_SHARES = np.array(
    [
        0.45539,
        0.15474,
        0.08757,
        0.05915,
        0.04219,
        0.03290,
        0.02581,
        0.02040,
        0.01607,
        0.01416,
        0.01276,
        0.00936,
        0.00799,
        0.00786,
        0.00557,
        0.00535,
        0.00493,
        0.00382,
        0.00328,
        0.00274,
    ]
)
CLIENT_COUNT_TAIL = np.arange(21, 251)
_CLIENT_TAIL_WEIGHTS = CLIENT_COUNT_TAIL**-2.48
_CLIENT_COUNT_VALUES = np.concatenate([np.arange(1, 21), CLIENT_COUNT_TAIL])
_CLIENT_COUNT_PMF = np.concatenate(
    [
        CLIENT_COUNT_SHARES,
        _CLIENT_TAIL_WEIGHTS * (1.0 - CLIENT_COUNT_SHARES.sum()) / _CLIENT_TAIL_WEIGHTS.sum(),
    ]
)


def _sample_sales(rng: np.random.Generator, size: int, factor: float = 1.0) -> np.ndarray:
    """Draw integer order totals from the source's empirical quantiles."""
    uniform = rng.random(size)
    values = np.interp(uniform, SALES_QUANTILES, SALES_VALUES) * factor
    return np.maximum(0, np.round(values)).astype(np.int64)


def _client_counts(rng: np.random.Generator, clients: int, orders: int) -> np.ndarray:
    """Heavy-tailed per-client order counts that sum exactly to ``orders``."""
    raw = rng.choice(_CLIENT_COUNT_VALUES, size=clients, p=_CLIENT_COUNT_PMF)
    return _allocate(raw.astype(float), orders, lower=1, upper=250)


def _unique_ids(rng: np.random.Generator, low: int, high: int, size: int) -> np.ndarray:
    """``size`` distinct integers from ``[low, high)`` without materialising the range."""
    drawn: set[int] = set()
    while len(drawn) < size:
        batch = rng.integers(low, high, size=max(size - len(drawn), 1) * 2)
        drawn.update(int(value) for value in batch)
    return np.array(sorted(drawn)[:size], dtype=np.int64)


def build_orders(
    rng: np.random.Generator,
    days: int = 181,
    calibrated: bool = True,
    scale: float = 1.0,
) -> pd.DataFrame:
    """Build the two-city order-level sample."""
    dates = pd.date_range(DATE_MIN, periods=days, freq="D")
    total_orders = int(TARGETS["order_rows"] * scale) if calibrated else int(20_000 * scale)
    total_orders = max(total_orders, 2_000)

    day_weight = _day_factor(dates) * rng.lognormal(0.0, 0.10, len(dates))
    city1_total = int(TARGETS["city1_orders"] * scale) if calibrated else int(total_orders * 0.8114)
    city_totals = {"city1": city1_total, "city2": total_orders - city1_total}
    # Client pools measured on the source: city1 orders more often per client.
    city_clients = (
        {"city1": 23_454, "city2": TARGETS["order_clients"] - 23_454}
        if calibrated
        else {city: max(1, int(round(total / 4.0))) for city, total in city_totals.items()}
    )

    frames = []
    all_client_ids = _unique_ids(rng, 47_035, 51_161_662, sum(city_clients.values()))
    offset = 0
    for city, city_total in city_totals.items():
        per_day = _allocate(day_weight, city_total, lower=0, upper=10**6)
        # Keep every day populated so the 30-day active-client window stays continuous.
        empty = np.flatnonzero(per_day == 0)
        for index in empty:
            donor = int(np.argmax(per_day))
            if per_day[donor] > 1:
                per_day[donor] -= 1
                per_day[index] = 1

        clients = city_clients[city]
        counts = _client_counts(rng, clients, city_total)
        client_ids = all_client_ids[offset : offset + clients]
        offset += clients

        order_dates = np.repeat(dates.to_numpy(), per_day)
        rng.shuffle(order_dates)
        order_clients = np.repeat(client_ids, counts)
        rng.shuffle(order_clients)

        sales = _sample_sales(rng, city_total, factor=1.0 if city == "city1" else 0.827)
        frames.append(
            pd.DataFrame(
                {
                    "cityname": city,
                    "date": pd.to_datetime(order_dates),
                    "clientid": order_clients.astype(np.int64),
                    "sales": sales.astype(float),
                }
            )
        )

    orders = pd.concat(frames, ignore_index=True)
    orders = orders.iloc[rng.permutation(len(orders))].reset_index(drop=True)

    zeros = 619 if calibrated else max(1, int(round(len(orders) * 0.0048)))
    orders.loc[rng.choice(len(orders), size=zeros, replace=False), "sales"] = 0.0

    order_ids = 180_871_096 + np.cumsum(rng.integers(1, 900, len(orders)))
    orders["orderid"] = order_ids.astype(np.int64)

    orders["date"] = orders["date"].dt.strftime("%Y-%m-%d")
    orders = orders.sort_values(["date", "cityname", "orderid"]).reset_index(drop=True)
    return orders[["cityname", "date", "clientid", "orderid", "sales"]]


# --------------------------------------------------------------------------- #
# writing and validation
# --------------------------------------------------------------------------- #
def write_dataset(out_dir: str | Path, scale: float = 1.0, seed: int = 20220101) -> dict[str, int]:
    """Generate both CSVs into ``out_dir`` and return their row counts."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(seed)
    calibrated = scale == 1.0

    cities = CITIES if calibrated else CITIES[: max(8, int(round(len(CITIES) * scale)))]
    aggregate = build_aggregate(rng, cities=cities, calibrated=calibrated)
    orders = build_orders(rng, calibrated=calibrated, scale=scale)

    aggregate.to_csv(out / "city_day_channel_category.csv", index=False)
    orders.to_csv(out / "orders_sample.csv", index=False)
    return {"agg": len(aggregate), "orders": len(orders)}


def _signature(con: Any) -> dict[str, float]:
    """Compute the published aggregates plus the analysis signatures from DuckDB."""

    def scalar(sql: str) -> float:
        return float(con.execute(sql).fetchone()[0])

    signatures = {
        "agg_rows": scalar("SELECT COUNT(*) FROM fact_sales"),
        "order_rows": scalar("SELECT COUNT(*) FROM fact_orders"),
        "cities": scalar("SELECT COUNT(DISTINCT city_name) FROM dim_city"),
        "channels": scalar("SELECT COUNT(*) FROM dim_channel"),
        "categories": scalar("SELECT COUNT(*) FROM dim_category"),
        "qty": scalar("SELECT SUM(qty) FROM fact_sales"),
        "tickets": scalar("SELECT SUM(tickets) FROM fact_sales"),
        "cost": scalar("SELECT SUM(cost) FROM fact_sales"),
        "clients": scalar("SELECT SUM(clients_qty) FROM fact_sales"),
        "items_per_order": scalar("SELECT SUM(qty) / SUM(tickets) FROM fact_sales"),
        "cost_per_ticket": scalar("SELECT SUM(cost) / SUM(tickets) FROM fact_sales"),
        "avg_price": scalar("SELECT AVG(avg_price) FROM fact_sales"),
        "unique_clients": scalar("SELECT COUNT(*) FROM dim_client"),
        "weekend_time": scalar("SELECT AVG(avg_time) FROM mart_city_daily WHERE is_weekend = 1"),
        "weekday_time": scalar("SELECT AVG(avg_time) FROM mart_city_daily WHERE is_weekend = 0"),
        "rho": scalar(
            """
            SELECT corr(ln(qty), avg_time) FROM (
                SELECT qty, avg_time FROM mart_city_daily
            )
            """
        ),
        "snacks_time": scalar(
            "SELECT SUM(qty * weighted_avg_time) / SUM(qty) FROM mart_delivery WHERE category = 'закуски'"
        ),
        "pizza_time": scalar(
            "SELECT SUM(qty * weighted_avg_time) / SUM(qty) FROM mart_delivery WHERE category = 'пицца'"
        ),
        "mp_share_jan": scalar(
            """
            SELECT AVG(mp / (mp + web)) FROM (
                SELECT d.date, SUM(CASE WHEN ch.channel_name = 'mp' THEN f.qty END) AS mp,
                       SUM(CASE WHEN ch.channel_name = 'web' THEN f.qty END) AS web
                FROM fact_sales f JOIN dim_date d USING (date_key)
                JOIN dim_channel ch USING (channel_key)
                WHERE d.month = 1 GROUP BY d.date
            )
            """
        ),
        "mp_share_jun": scalar(
            """
            SELECT AVG(mp / (mp + web)) FROM (
                SELECT d.date, SUM(CASE WHEN ch.channel_name = 'mp' THEN f.qty END) AS mp,
                       SUM(CASE WHEN ch.channel_name = 'web' THEN f.qty END) AS web
                FROM fact_sales f JOIN dim_date d USING (date_key)
                JOIN dim_channel ch USING (channel_key)
                WHERE d.month = 6 GROUP BY d.date
            )
            """
        ),
    }
    # Spearman is what the project reports; DuckDB's corr on ranks is close enough to gate on.
    signatures["spearman"] = scalar(
        """
        SELECT corr(rank_qty, rank_time) FROM (
            SELECT rank() OVER (ORDER BY qty) AS rank_qty,
                   rank() OVER (ORDER BY avg_time) AS rank_time
            FROM mart_city_daily
        )
        """
    )
    return signatures


def validate(out_dir: str | Path, full: bool = False, calibrated: bool = True) -> dict[str, float]:
    """Build the derived layers from the dummy CSVs and assert the contract."""
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from delivery import config as cfg
    from delivery import etl, marts, quality

    base = Path(tempfile.mkdtemp(prefix="dct-dummy-"))
    raw = base / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    for name in ("city_day_channel_category.csv", "orders_sample.csv"):
        shutil.copy2(Path(out_dir) / name, raw / name)

    cfg.RAW = raw
    cfg.STAGING = base / "staging"
    cfg.MARTS = base / "marts"
    cfg.REPORTS = base / "reports"
    cfg.FIGURES = base / "reports" / "figures"
    cfg.DUCKDB_PATH = cfg.MARTS / "delivery.duckdb"
    cfg.AGG_FILE = raw / "city_day_channel_category.csv"
    cfg.ORDERS_FILE = raw / "orders_sample.csv"
    cfg.RAW_SOURCE = base / "raw_source"
    for directory in (cfg.STAGING, cfg.MARTS, cfg.REPORTS, cfg.FIGURES):
        directory.mkdir(parents=True, exist_ok=True)

    counts = {
        "agg": sum(1 for _ in open(cfg.AGG_FILE, encoding="utf-8")) - 1,
        "orders": sum(1 for _ in open(cfg.ORDERS_FILE, encoding="utf-8")) - 1,
    }
    cfg.EXPECTED_AGG_ROWS = counts["agg"]
    cfg.EXPECTED_ORDER_ROWS = counts["orders"]

    etl.write_staging()
    sales, orders = quality.load_staging()
    report = quality.run_checks(sales, orders)
    failed = report.loc[~report["passed"]]
    if len(failed):
        raise RuntimeError(f"quality checks failed:\n{failed.to_string(index=False)}")
    marts.build(db_path=cfg.DUCKDB_PATH)

    import duckdb

    con = duckdb.connect(str(cfg.DUCKDB_PATH), read_only=True)
    try:
        signatures = _signature(con)
    finally:
        con.close()

    checks = {
        "rows match target": (not calibrated) or signatures["agg_rows"] == TARGETS["agg_rows"],
        "orders match target": (not calibrated)
        or signatures["order_rows"] == TARGETS["order_rows"],
        "3 channels x 4 categories": signatures["channels"] == 3 and signatures["categories"] == 4,
        "264 cities": (not calibrated) or signatures["cities"] == 264,
        "qty total": (not calibrated) or abs(signatures["qty"] - TARGETS["qty"]) < 1,
        "tickets total": (not calibrated) or abs(signatures["tickets"] - TARGETS["tickets"]) < 1,
        "clients total": (not calibrated) or abs(signatures["clients"] - TARGETS["clients"]) < 1,
        "avg_price mean": (not calibrated)
        or abs(signatures["avg_price"] - TARGETS["avg_price_mean"]) < 0.01,
        "unique order clients": (not calibrated)
        or signatures["unique_clients"] == TARGETS["order_clients"],
        "snacks minus pizza >= 10 min": signatures["snacks_time"] - signatures["pizza_time"] >= 10,
        "weekends not slower": signatures["weekend_time"] < signatures["weekday_time"],
        "mp share grows >= 4 pp": signatures["mp_share_jun"] - signatures["mp_share_jan"] >= 0.04,
        "volume/time rank corr negative": signatures["spearman"] < 0,
    }
    failures = [name for name, passed in checks.items() if not passed]

    print(f"\ndummy dataset contract ({out_dir})")
    print("-" * 62)
    for key, value in signatures.items():
        print(
            f"  {key:<22} {value:,.4f}" if isinstance(value, float) else f"  {key:<22} {value:,.0f}"
        )
    print("-" * 62)
    for name, passed in checks.items():
        print(f"  [{'ok' if passed else 'FAIL'}] {name}")

    if full:
        from delivery import bottlenecks, dashboard, forecast, hypothesis

        metrics, predictions = forecast.run_total_forecast(db_path=cfg.DUCKDB_PATH)
        forecast.write_outputs(metrics, predictions, reports_dir=cfg.REPORTS)
        result = bottlenecks.analyze(db_path=cfg.DUCKDB_PATH)
        bottlenecks.write_reports(result, reports_dir=cfg.REPORTS, figures_dir=cfg.FIGURES)
        hypotheses = hypothesis.analyze(db_path=cfg.DUCKDB_PATH)
        hypothesis.write_results(hypotheses["results"], reports_dir=cfg.REPORTS)
        dashboard.build(db_path=cfg.DUCKDB_PATH, out_path=cfg.REPORTS / "dashboard.html")
        best = metrics.loc[metrics["horizon"] == 7].sort_values("wape").iloc[0]
        print(f"  forecast h=7 best: {best['model']} WAPE={best['wape']:.4f}")
        print(
            f"  H1 supported={bool(hypotheses['results'].iloc[0]['supported'])} "
            f"effect={hypotheses['results'].iloc[0]['effect']:+.4f}"
        )
        print(
            f"  H2 supported={bool(hypotheses['results'].iloc[1]['supported'])} "
            f"effect={hypotheses['results'].iloc[1]['effect']:+.4f}"
        )

    shutil.rmtree(base, ignore_errors=True)
    if failures:
        raise SystemExit("contract violations: " + ", ".join(failures))
    return signatures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=Path("data/synthetic"), help="output directory")
    parser.add_argument("--scale", type=float, default=1.0, help="1.0 = full published size")
    parser.add_argument("--seed", type=int, default=20220101)
    parser.add_argument("--validate", action="store_true", help="build etl/marts and assert")
    parser.add_argument("--full", action="store_true", help="also run forecast/bottlenecks/H1-H2")
    args = parser.parse_args(argv)

    counts = write_dataset(args.out, scale=args.scale, seed=args.seed)
    print(f"wrote {counts['agg']:,} aggregate rows and {counts['orders']:,} orders to {args.out}")
    if args.validate:
        validate(args.out, full=args.full, calibrated=args.scale == 1.0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
