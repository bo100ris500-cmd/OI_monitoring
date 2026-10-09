#!/usr/bin/env python3
"""Выгрузка сигналов из PostgreSQL (или SQLite) в CSV.

Примеры:
  python export_signals.py
  python export_signals.py --db-url postgresql+asyncpg://oi_bot:oi_bot@127.0.0.1:5432/oi_bot
  python export_signals.py --since 2026-10-01 -o signals.csv
"""

from __future__ import annotations

import argparse
import csv
import os
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import create_engine, text


def _sync_url(url: str) -> str:
    """asyncpg/aiosqlite → sync драйвер для простого скрипта."""
    return (
        url.replace("postgresql+asyncpg://", "postgresql+psycopg://")
        .replace("postgres+asyncpg://", "postgresql+psycopg://")
        .replace("sqlite+aiosqlite://", "sqlite://")
    )


def main() -> None:
    load_dotenv()
    p = argparse.ArgumentParser(description="Export OI signals to CSV")
    p.add_argument("-o", "--output", default="signals_export.csv")
    p.add_argument(
        "--db-url",
        default=os.getenv("DATABASE_URL", "postgresql+asyncpg://oi_bot:oi_bot@127.0.0.1:5432/oi_bot"),
        help="SQLAlchemy URL (как в .env)",
    )
    p.add_argument("--since", default="", help="YYYY-MM-DD")
    args = p.parse_args()

    url = _sync_url(args.db_url)
    engine = create_engine(url)

    sql = """
    SELECT
      s.id,
      s.signal_uid,
      s.base_symbol,
      s.exchange,
      s.window,
      s.types_json,
      s.s7_status,
      s.flags_json,
      s.ts_utc,
      s.price_at_signal,
      s.price_after_5m,
      s.price_after_15m,
      s.price_after_1h,
      s.price_after_4h,
      s.price_after_1d,
      s.return_5m_pct,
      s.return_15m_pct,
      s.return_1h_pct,
      s.return_4h_pct,
      s.return_1d_pct,
      s.post_factum_done,
      COUNT(d.id) AS deliveries_count,
      STRING_AGG(CAST(u.telegram_id AS TEXT), ',') AS delivered_to_telegram_ids,
      STRING_AGG(CAST(d.sent_at AS TEXT), ',') AS delivered_at
    FROM signals s
    LEFT JOIN user_signal_delivery d ON d.signal_id = s.id
    LEFT JOIN users u ON u.id = d.user_id
    """
    # SQLite не имеет STRING_AGG в старых версиях — для PG основной путь
    if url.startswith("sqlite"):
        sql = """
        SELECT
          s.id, s.signal_uid, s.base_symbol, s.exchange, s.window, s.types_json,
          s.s7_status, s.flags_json, s.ts_utc, s.price_at_signal,
          s.price_after_5m, s.price_after_15m, s.price_after_1h, s.price_after_4h, s.price_after_1d,
          s.return_5m_pct, s.return_15m_pct, s.return_1h_pct, s.return_4h_pct, s.return_1d_pct,
          s.post_factum_done,
          COUNT(d.id) AS deliveries_count,
          GROUP_CONCAT(u.telegram_id) AS delivered_to_telegram_ids,
          GROUP_CONCAT(d.sent_at) AS delivered_at
        FROM signals s
        LEFT JOIN user_signal_delivery d ON d.signal_id = s.id
        LEFT JOIN users u ON u.id = d.user_id
        """

    params: dict = {}
    if args.since:
        sql += " WHERE s.ts_utc >= :since "
        params["since"] = args.since
    sql += " GROUP BY s.id ORDER BY s.ts_utc ASC "

    with engine.connect() as conn:
        rows = conn.execute(text(sql), params).mappings().all()

    out = Path(args.output)
    if not rows:
        print(f"0 signals → {out.resolve()}")
        with out.open("w", encoding="utf-8-sig", newline="") as f:
            csv.writer(f).writerow(["id", "signal_uid", "base_symbol", "ts_utc"])
        return

    fieldnames = list(rows[0].keys())
    with out.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in rows:
            w.writerow(dict(r))

    delivered = sum(1 for r in rows if (r["deliveries_count"] or 0) > 0)
    print(f"signals={len(rows)} delivered_at_least_once={delivered} → {out.resolve()}")


if __name__ == "__main__":
    main()
