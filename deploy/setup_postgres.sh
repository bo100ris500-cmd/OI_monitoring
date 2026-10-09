#!/usr/bin/env bash
# Создание БД PostgreSQL для OI-бота (запускать на сервере от root/sudo).
set -euo pipefail

DB_NAME="${DB_NAME:-oi_bot}"
DB_USER="${DB_USER:-oi_bot}"
DB_PASS="${DB_PASS:-oi_bot}"

apt-get update -y
apt-get install -y postgresql postgresql-contrib

sudo -u postgres psql -v ON_ERROR_STOP=1 <<SQL
DO \$\$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '${DB_USER}') THEN
    CREATE ROLE ${DB_USER} LOGIN PASSWORD '${DB_PASS}';
  END IF;
END
\$\$;
SELECT 'CREATE DATABASE ${DB_NAME} OWNER ${DB_USER}'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = '${DB_NAME}')\gexec
GRANT ALL PRIVILEGES ON DATABASE ${DB_NAME} TO ${DB_USER};
SQL

echo "OK. Add to .env:"
echo "DATABASE_URL=postgresql+asyncpg://${DB_USER}:${DB_PASS}@127.0.0.1:5432/${DB_NAME}"
