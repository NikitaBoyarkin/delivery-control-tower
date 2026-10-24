# Superset: интерактивный BI-слой

Superset поверх витрин DuckDB (`mart_daily_total`, `mart_city_daily`, `mart_delivery`,
`dim_city`) для кейса Delivery Control Tower.

## Статус (честно)

- **Docker на машине не запускался.** `docker-compose.yml`, `.env.example` и
  `init_superset.sh` написаны, но **не проверены в работе**: нет ни одного запуска,
  ни скриншотов, ни подтверждённого «Test Connection».
- Проверенный артефакт — статический дашборд: `uv run python -m delivery.dashboard`
  → `reports/dashboard.html`. Его собирают и проверяют тесты
  (`tests/test_dashboard.py`), он не требует сервера.
- Версия Superset закреплена (`apache/superset:4.1.2`). При смене тега проверь
  Python-блок в `init_superset.sh`: внутренние импорты Superset между мажорными
  версиями меняются.

## Предпосылки

- Docker Engine + Compose v2 (`docker compose version`).
- Собранные витрины: `data/marts/delivery.duckdb` (создаётся `python -m delivery.marts`).
- Свободный порт 8088 (или задай `SUPERSET_PORT` в `.env`).

## Быстрый старт

```bash
cd superset
cp .env.example .env
# отредактируй .env: SUPERSET_SECRET_KEY = openssl rand -base64 42, пароли
docker compose up -d
./init_superset.sh
```

`docker compose up` поднимает Postgres (метаданные Superset) и сам Superset. Конфиг
`superset_config.py` генерируется при старте контейнера в томе `superset_home`, поэтому
отдельный файл в репозитории не нужен.

`init_superset.sh` идемпотентен: `superset db upgrade` → `superset init` → админ
(создаётся, только если пользователя нет) → база `Delivery (DuckDB)` и три датасета.

Открой `http://localhost:8088`, логин — `SUPERSET_ADMIN_USERNAME` / `SUPERSET_ADMIN_PASSWORD`.

## Драйвер DuckDB: требует сборки образа

Официальный образ `apache/superset` **не содержит** `duckdb-engine`. Скрипт
`init_superset.sh` ставит его в работающий контейнер:

```bash
docker compose exec -T superset pip install "duckdb-engine>=0.13,<0.18"
```

Это работает до первого пересоздания контейнера (`docker compose up --force-recreate`,
смена образа, `docker compose down`). Для постоянного решения нужен производный образ.
Файл `Dockerfile` в этот набор намеренно не добавлен; сниппет:

```dockerfile
FROM apache/superset:4.1.2
USER root
RUN pip install --no-cache-dir "duckdb-engine>=0.13,<0.18"
USER superset
```

Затем в `docker-compose.yml` замени `image: apache/superset:4.1.2` на
`build: .` — и образ придётся собирать (`docker compose build`). Это шаг, который
**не выполнялся**: Docker недоступен.

## Подключение к DuckDB

- Витрина монтируется в контейнер только для чтения: `../data/marts:/data:ro`.
- URI: `duckdb:////data/delivery.duckdb` (четыре слэша = абсолютный путь).
- **DuckDB — встроенная БД, один писатель.** Superset читает файл, но параллельные
  воркеры и запись из ETL в тот же файл конфликтуют (блокировка файла). Практика:
  держать один gunicorn-воркер (`superset run` вместо нескольких), не пересобирать
  витрину во время работы Superset; для тяжёлых сценариев — копия файла.
- Если путь или драйвер неверны, `Test Connection` в UI покажет ошибку импорта
  `duckdb_engine` — вернись к разделу выше.

## Датасеты и гранулярность

| Датасет | Строк | Гранулярность | Ключевые поля |
|---|---|---|---|
| `mart_daily_total` | 181 | день | `date`, `day_of_week`, `is_weekend`, `qty`, `cost`, `tickets`, `clients_qty`, `items_per_order`, `weighted_avg_time` |
| `mart_city_daily` | 47 750 | город × день (264 города) | `city_name`, `date`, `qty`, `cost`, `tickets`, `items_per_order`, `avg_time` |
| `mart_delivery` | 3 168 | город × канал × категория | `channel`, `category`, `qty`, `cost`, `tickets`, `weighted_rto`, `cost_per_item`, `items_per_order` |

`day_of_week`: 0 = понедельник … 6 = воскресенье (из исходного CSV, не `DAYOFWEEK` DuckDB).

### Метрики и осторожность с весами

- `weighted_avg_time` в `mart_daily_total` уже взвешено по `qty` **внутри дня**. При
  агрегации за период нельзя брать `AVG(weighted_avg_time)` — только
  `SUM(qty * weighted_avg_time) / SUM(qty)`.
- `cost_per_item`, `items_per_order`, `weighted_rto` в `mart_delivery` — отношения на
  гранулярности «город × канал × категория». Для среза по каналу используй
  `SUM(cost)/SUM(qty)`, `SUM(qty)/SUM(tickets)`, а не `AVG(...)` по строкам.
- **`cost_per_item` — это cost на позицию** (`SUM(cost)/SUM(qty)`), а метрика
  дашборда `cost_per_order` — **cost на заказ** (`SUM(cost)/SUM(tickets)`). Это
  две разные величины (различаются в ~1.74 раза): колонка витрины — на позицию,
  в дашборде используется метрика на заказ.
- `tickets` — число заказов (чеков), `qty` — число позиций; `tickets ≤ qty`.
- `cost` — денежная метрика источника; **валюта и то, что это выручка, не подтверждены**,
  поэтому в подписях пишем «cost, ед. источника».
- `avg_price` в расчётах не используется: его семантика не подтверждена.

Готовые метрики Superset (Datasets → Edit → Metrics):

| Имя | SQL |
|---|---|
| `orders` | `SUM(tickets)` |
| `cost_total` | `SUM(cost)` |
| `units` | `SUM(qty)` |
| `items_per_order` | `SUM(qty) / NULLIF(SUM(tickets), 0)` |
| `avg_delivery_time` | `SUM(qty * weighted_avg_time) / NULLIF(SUM(qty), 0)` |
| `cost_per_order` | `SUM(cost) / NULLIF(SUM(tickets), 0)` |
| `cost_per_unit` | `SUM(cost) / NULLIF(SUM(qty), 0)` |

## Три дашборда по ролям

### 1. «Операции» — операционный менеджер

| Чарт | Тип | Датасет | Настройка |
|---|---|---|---|
| Заказы | Big Number | `mart_daily_total` | метрика `orders` |
| Позиции | Big Number | `mart_daily_total` | метрика `units` |
| Среднее время доставки | Big Number | `mart_daily_total` | `avg_delivery_time`, суффикс «мин» |
| Позиций на заказ | Big Number | `mart_daily_total` | `items_per_order` |
| Заказы по дням | Line | `mart_daily_total` | X = `date` (time grain: day), Y = `orders` |
| Время доставки по дням | Line | `mart_daily_total` | X = `date`, Y = `avg_delivery_time` |
| День недели × неделя | Heatmap | `mart_daily_total` | X = `date` (time grain: week), Y = `day_of_week`, metric `orders` |
| Позиций на заказ по каналам | Bar | `mart_delivery` | X = `channel`, Y = `items_per_order` |

Фильтры дашборда: `date` (time range), `is_weekend`.

### 2. «Коммерция» — коммерческий аналитик

| Чарт | Тип | Датасет | Настройка |
|---|---|---|---|
| Заказы по каналам | Bar | `mart_delivery` | X = `channel`, Y = `orders`, сортировка desc |
| Заказы по категориям | Bar | `mart_delivery` | X = `category`, Y = `orders` |
| cost по категориям | Bar | `mart_delivery` | X = `category`, Y = `cost_total` (ед. источника) |
| Экономика каналов | Table | `mart_delivery` | колонки `channel`, `orders`, `cost_total`, `cost_per_unit`, `items_per_order` |
| Города: cost на позицию vs позиций на заказ | Scatter | `mart_delivery` (group by `city_name`) | X = `cost_per_unit`, Y = `items_per_order`, размер = `units` |

Лимит строк в таблице — 3 канала; для категорий — 4.

### 3. «Руководство» — руководитель направления

| Чарт | Тип | Датасет | Настройка |
|---|---|---|---|
| Тренд по неделям | Line | `mart_daily_total` | X = `date` (week), Y = `orders` |
| Топ-10 городов | Bar | `mart_city_daily` | X = `city_name`, Y = `orders`, row limit 10, sort desc |
| Пиковый день | Big Number | `mart_daily_total` | `MAX(tickets)` как ad-hoc метрика |
| Городов в периметре | Big Number | `mart_city_daily` | `COUNT(DISTINCT city_name)` |
| Парето по городам | Bar + Line (см. ниже) | виртуальный датасет | кумулятивная доля заказов |

**Парето.** В Superset 4.x нет готового Pareto-чарта. Рабочий путь: SQL Lab →
виртуальный датасет с оконной функцией, затем Bar (заказы по городам) + Line
(кумулятивная доля) на одном дашборде:

```sql
SELECT city_name,
       orders,
       SUM(orders) OVER (ORDER BY orders DESC, city_name
                         ROWS UNBOUNDED PRECEDING) AS cum_orders,
       1.0 * SUM(orders) OVER (ORDER BY orders DESC, city_name
                               ROWS UNBOUNDED PRECEDING)
           / SUM(orders) OVER () AS cum_share
FROM (
    SELECT city_name, SUM(tickets) AS orders
    FROM mart_city_daily
    GROUP BY city_name
) city_orders
ORDER BY orders DESC, city_name;
```

Результат по этим данным: 119 городов из 264 дают 80% заказов. В статическом
дашборде этот график уже построен (`reports/dashboard.html`, блок «Руководство»).

## Проверка после запуска

1. Databases → `Delivery (DuckDB)` → **Test Connection** — зелёный.
2. Datasets → три датасета видны, у каждого заполнены колонки.
3. SQL Lab → `SELECT COUNT(*) FROM mart_daily_total` → 181.
4. Дашборды «Операции», «Коммерция», «Руководство» открываются без ошибок чартов.

## Типичные ошибки

| Симптом | Причина |
|---|---|
| `No module named 'duckdb_engine'` | драйвер не установлен / контейнер пересоздан (см. раздел про сборку образа) |
| `IO Error: Could not set lock on file` | витрина пересобирается, пока Superset держит файл; или несколько воркеров |
| Пустые чарты | не выбран time range или метрика агрегирует отношение как `AVG` |
| `password authentication failed` | пароль в `.env` и в томе Postgres разошлись — пересоздай том `postgres_data` (потеря метаданных) |

## Что осталось несделанным (PENDING)

- Запуск `docker compose up`, `init_superset.sh` и проверка `Test Connection`.
- Сборка образа с `duckdb-engine` (`docker compose build`).
- Создание самих трёх дашбордов и скриншоты — вручную в UI по таблицам выше.
- Роли и права (Gamma для «Операций»/«Коммерции», Alpha для «Руководства»).
