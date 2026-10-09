"""Live lookup of OI across configured exchanges."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import aiohttp

from app.collectors import build_collectors
from app.config import AppConfig

logger = logging.getLogger(__name__)

TZ_UTC7 = timezone(timedelta(hours=7))


@dataclass
class ExchangeOiRow:
    exchange: str
    available: bool
    oi_usd: float | None
    price: float | None = None
    contract_symbol: str | None = None
    error: str | None = None


def format_oi(v: float | None) -> str:
    if v is None:
        return "—"
    abs_v = abs(v)
    if abs_v >= 1_000_000_000:
        return f"${v / 1_000_000_000:.2f}B"
    if abs_v >= 1_000_000:
        return f"${v / 1_000_000:.2f}M"
    if abs_v >= 1_000:
        return f"${v / 1_000:.1f}K"
    return f"${v:.4g}"


def format_price(v: float | None) -> str:
    if v is None:
        return "—"
    if v >= 1000:
        return f"{v:,.2f}"
    if v >= 1:
        return f"{v:.4f}"
    return f"{v:.6g}"


async def lookup_base_on_exchanges(base: str, cfg: AppConfig) -> list[ExchangeOiRow]:
    """Probe configured exchanges for base asset perpetual OI + price."""
    base = base.upper()
    rows: list[ExchangeOiRow] = []
    timeout = aiohttp.ClientTimeout(total=45)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        collectors = build_collectors(session, cfg)
        sem = asyncio.Semaphore(cfg.resources.max_concurrent_requests)

        async def one(name: str, coll) -> ExchangeOiRow:
            async with sem:
                try:
                    snaps = await coll.discover({base})
                    match = [s for s in snaps if s.base_symbol.upper() == base]
                    if not match:
                        return ExchangeOiRow(
                            exchange=name, available=False, oi_usd=None, price=None
                        )
                    best = max(match, key=lambda s: s.oi_usd or 0.0)
                    price = best.mark_price if best.mark_price is not None else best.last_price
                    return ExchangeOiRow(
                        exchange=name,
                        available=True,
                        oi_usd=best.oi_usd,
                        price=price,
                        contract_symbol=best.contract_symbol,
                    )
                except Exception as e:  # noqa: BLE001
                    logger.warning("lookup %s on %s: %s", base, name, e)
                    return ExchangeOiRow(
                        exchange=name,
                        available=False,
                        oi_usd=None,
                        price=None,
                        error=str(e),
                    )

        results = await asyncio.gather(*[one(n, c) for n, c in collectors.items()])
        by_name = {r.exchange: r for r in results}
        for name in cfg.exchanges:
            rows.append(
                by_name.get(name.lower())
                or ExchangeOiRow(
                    exchange=name.lower(), available=False, oi_usd=None, price=None
                )
            )
    return rows


def format_check_report(base: str, rows: list[ExchangeOiRow]) -> str:
    """Только доступные биржи: Exch | Price | OI."""
    available = [r for r in rows if r.available]
    lines = [f"Токен: {base}", ""]
    if not available:
        lines.append("На наблюдаемых биржах контракт не найден.")
        return "\n".join(lines)

    lines.append(f"{'Exch':<14} {'Price':<14} {'OI'}")
    lines.append("-" * 42)
    for r in available:
        lines.append(
            f"{r.exchange.capitalize():<14} "
            f"{format_price(r.price):<14} "
            f"{format_oi(r.oi_usd)}"
        )
    return "\n".join(lines)


def format_added_at_utc7(dt: datetime | None) -> str:
    if dt is None:
        return "—"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    local = dt.astimezone(TZ_UTC7)
    return local.strftime("%d.%m.%Y %H:%M UTC+7")


def format_list_report(
    entries: list[tuple[str, datetime | None]],
    exch_map: dict[str, list[str]],
) -> str:
    """
    Отслеживаются
    <Тикер> - <Биржи>, <дата добавления UTC+7>
    """
    lines = ["Отслеживаются", ""]
    if not entries:
        lines.append("(пусто)")
        return "\n".join(lines)
    for sym, created_at in entries:
        exchanges = exch_map.get(sym) or []
        ex_part = ", ".join(exchanges) if exchanges else "биржи пока не определены"
        when = format_added_at_utc7(created_at)
        lines.append(f"{sym} - {ex_part}, {when}")
    return "\n".join(lines)
