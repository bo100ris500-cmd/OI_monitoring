"""Live lookup of OI across configured exchanges."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

import aiohttp

from app.collectors import build_collectors
from app.config import AppConfig

logger = logging.getLogger(__name__)


@dataclass
class ExchangeOiRow:
    exchange: str
    available: bool
    oi_usd: float | None
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


async def lookup_base_on_exchanges(base: str, cfg: AppConfig) -> list[ExchangeOiRow]:
    """Probe all configured exchanges for base asset perpetual OI."""
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
                    # exact base match preferred
                    match = [s for s in snaps if s.base_symbol.upper() == base]
                    if not match:
                        return ExchangeOiRow(exchange=name, available=False, oi_usd=None)
                    # if several contracts (USDT/USDC) — take max OI
                    best = max(match, key=lambda s: s.oi_usd or 0.0)
                    return ExchangeOiRow(
                        exchange=name,
                        available=True,
                        oi_usd=best.oi_usd,
                        contract_symbol=best.contract_symbol,
                    )
                except Exception as e:  # noqa: BLE001
                    logger.warning("lookup %s on %s: %s", base, name, e)
                    return ExchangeOiRow(
                        exchange=name, available=False, oi_usd=None, error=str(e)
                    )

        results = await asyncio.gather(*[one(n, c) for n, c in collectors.items()])
        # stable order by cfg.exchanges
        by_name = {r.exchange: r for r in results}
        for name in cfg.exchanges:
            rows.append(
                by_name.get(name.lower())
                or ExchangeOiRow(exchange=name.lower(), available=False, oi_usd=None)
            )
    return rows


def format_check_report(base: str, rows: list[ExchangeOiRow]) -> str:
    lines = [
        f"Токен: {base}",
        "",
        f"{'Биржа':<14} {'Доступен':<10} {'OI USD'}",
        "-" * 40,
    ]
    for r in rows:
        avail = "да" if r.available else "нет"
        oi = format_oi(r.oi_usd) if r.available else "—"
        name = r.exchange.capitalize()
        lines.append(f"{name:<14} {avail:<10} {oi}")
    available = [r.exchange.capitalize() for r in rows if r.available]
    lines.append("")
    if available:
        lines.append("Доступен на: " + ", ".join(available))
    else:
        lines.append("На наблюдаемых биржах контракт не найден.")
    return "\n".join(lines)


def format_list_line(base: str, exchanges: list[str]) -> str:
    if exchanges:
        return f"• {base} — {', '.join(exchanges)}"
    return f"• {base} — (биржи пока не определены)"
