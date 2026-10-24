#!/usr/bin/env bash
# Bootstrap Superset for the Delivery Control Tower marts.
#
# Idempotent: re-running upgrades metadata, keeps the admin user, and only
# creates missing database/datasets.
#
# STATUS: written without Docker on the machine; never executed end to end.
# Expect to adjust the Python block if the Superset image tag changes.

set -euo pipefail

cd "$(dirname "$0")"

if [[ ! -f .env ]]; then
  cp .env.example .env
  echo "Создан superset/.env из .env.example. Заполни пароли и запусти скрипт снова."
  exit 1
fi

set -a
# shellcheck disable=SC1091
. ./.env
set +a

ADMIN_USERNAME="${SUPERSET_ADMIN_USERNAME:-admin}"
ADMIN_EMAIL="${SUPERSET_ADMIN_EMAIL:-admin@example.com}"

echo "== 1/5 Контейнер Superset запущен?"
if ! docker compose ps --status running --services | grep -qx superset; then
  echo "Superset не запущен. Сначала: docker compose up -d" >&2
  exit 1
fi

echo "== 2/5 Драйвер DuckDB (duckdb-engine)"
if docker compose exec -T superset python -c "import duckdb_engine" >/dev/null 2>&1; then
  echo "duckdb-engine уже есть в контейнере"
else
  echo "Ставлю duckdb-engine в работающий контейнер."
  echo "ВАЖНО: пакет живёт до пересоздания контейнера. Постоянно — только сборкой"
  echo "образа (Dockerfile-сниппет в README.md, шаг «Драйвер DuckDB»)."
  docker compose exec -T superset pip install --no-cache-dir "duckdb-engine>=0.13,<0.18"
fi

echo "== 3/5 Миграции метаданных и роли"
docker compose exec -T superset superset db upgrade
docker compose exec -T superset superset init

echo "== 4/5 Админ (идемпотентно)"
if docker compose exec -T superset superset fab list-users 2>/dev/null \
     | grep -qw "${ADMIN_USERNAME}"; then
  echo "Пользователь ${ADMIN_USERNAME} уже существует"
else
  if [[ -z "${SUPERSET_ADMIN_PASSWORD:-}" ]]; then
    echo "SUPERSET_ADMIN_PASSWORD не задан в superset/.env" >&2
    exit 1
  fi
  docker compose exec -T superset superset fab create-admin \
    --username "${ADMIN_USERNAME}" \
    --firstname Superset \
    --lastname Admin \
    --email "${ADMIN_EMAIL}" \
    --password "${SUPERSET_ADMIN_PASSWORD}"
fi

echo "== 5/5 База DuckDB и три датасета"
docker compose exec -T superset python - <<'PY'
import os

from superset import db
from superset.app import create_app
from superset.connectors.sqla.models import SqlaTable
from superset.models.core import Database

DATABASE_NAME = "Delivery (DuckDB)"
DATASETS = ("mart_daily_total", "mart_city_daily", "mart_delivery")
URI = os.environ.get("DELIVERY_DUCKDB_URI", "duckdb:////data/delivery.duckdb")

app = create_app()
with app.app_context():
    database = (
        db.session.query(Database).filter_by(database_name=DATABASE_NAME).one_or_none()
    )
    if database is None:
        database = Database(
            database_name=DATABASE_NAME,
            sqlalchemy_uri=URI,
            expose_in_sqllab=True,
            allow_ctas=False,
            allow_cvas=False,
            allow_dml=False,
        )
        db.session.add(database)
        db.session.commit()
        print(f"created database: {DATABASE_NAME} -> {URI}")
    else:
        print(f"database exists: {DATABASE_NAME}")

    for table_name in DATASETS:
        existing = (
            db.session.query(SqlaTable)
            .filter_by(table_name=table_name, database_id=database.id)
            .one_or_none()
        )
        if existing is not None:
            print(f"dataset exists: {table_name}")
            continue
        dataset = SqlaTable(table_name=table_name, database=database)
        dataset.fetch_metadata()
        db.session.add(dataset)
        db.session.commit()
        print(f"created dataset: {table_name}")
PY

echo
echo "Готово. Открой http://localhost:${SUPERSET_PORT:-8088} и проверь:"
echo "  Databases -> ${DATABASE_NAME} -> Test Connection"
echo "  Datasets    -> mart_daily_total / mart_city_daily / mart_delivery"
