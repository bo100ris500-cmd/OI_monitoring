"""Live lookup of OI across configured exchanges + report helpers."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import aiohttp
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from app.collectors import build_collectors
from app.config import AppConfig
from app.core.normalize import parse_window_to_seconds
from app.core.timeutils import ensure_utc, utcnow
from app.storage import repo

logger = logging.getLogger(__name__)

TZ_UTC7 = timezone(timedelta(hours=7))

REPORT_WINDOWS = ["5m", "15m", "1h", "4h", "12h", "1d", "1w"]


@dataclass
class ExchangeOiRow:
    exchange: str
    available: bool
    oi_usd: float | None
    price: float | None = None
    long_pct: float | None = None
    short_pct: float | None = None
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


def format_ls(long_pct: float | None, short_pct: float | None) -> str:
    if long_pct is None:
        return "—"
    short = short_pct if short_pct is not None else 100.0 - long_pct
    return f"{long_pct:.1f}/{short:.1f}"


def format_pct(v: float | None) -> str:
    if v is None:
        return "—"
    return f"{v:+.2f}%"


async def lookup_base_on_exchanges(base: str, cfg: AppConfig) -> list[ExchangeOiRow]:
    """Probe configured exchanges for base asset perpetual OI + price + L/S."""
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
                        long_pct=best.long_pct,
                        short_pct=best.short_pct,
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
    """Доступные биржи: Exch | Price | OI | Long/Short."""
    available = [r for r in rows if r.available]
    lines = [f"Токен: {base}", ""]
    if not available:
        lines.append("На наблюдаемых биржах контракт не найден.")
        return "\n".join(lines)

    lines.append(f"{'Exch':<12} {'Price':<12} {'OI':<12} {'Long/Short'}")
    lines.append("-" * 52)
    for r in available:
        lines.append(
            f"{r.exchange.capitalize():<12} "
            f"{format_price(r.price):<12} "
            f"{format_oi(r.oi_usd):<12} "
            f"{format_ls(r.long_pct, r.short_pct)}"
        )
    return "\n".join(lines)


def check_add_keyboard(base: str, rows: list[ExchangeOiRow]) -> InlineKeyboardMarkup | None:
    available = [r for r in rows if r.available]
    if not available:
        return None
    buttons: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                text=f"добавить все ({base})",
                callback_data=f"chkaddall:{base}",
            )
        ]
    ]
    for r in available:
        buttons.append(
            [
                InlineKeyboardButton(
                    text=f"добавить {base} {r.exchange.capitalize()}",
                    callback_data=f"chkadd:{base}:{r.exchange}",
                )
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=buttons)


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


def _tick_price(tick) -> float | None:
    if tick is None:
        return None
    return tick.mark_price if tick.mark_price is not None else tick.last_price


async def build_otchet_report(
    session: AsyncSession,
    user_id: int | None,
    ticker: str,
) -> str:
    """Широкоформатная сводка по таймфреймам, выравнивание влево."""
    display, legs = await repo.resolve_report_legs(session, user_id, ticker)
    if not legs:
        return f"{display}\n\nНет данных в истории. Добавьте тикер в мониторинг и подождите сбора."

    now = utcnow()
    # актуальные ноги: только те, где есть свежий тик
    live_legs: list[tuple[str, str, object]] = []
    for exchange, base in legs:
        tick = await repo.latest_tick_for_base(session, exchange, base)
        if tick:
            tick.ts_utc = ensure_utc(tick.ts_utc)
            live_legs.append((exchange, base, tick))
    if not live_legs:
        return f"{display}\n\nНет свежих тиков по тикеру."

    lines = [display, ""]
    any_window = False
    for window in REPORT_WINDOWS:
        sec = parse_window_to_seconds(window)
        before_ts = now - timedelta(seconds=sec)
        per_ex: list[tuple[str, float, float | None, float | None]] = []
        # (name, oi_now, oi_chg, price_chg)
        oi_now_sum = 0.0
        oi_before_sum = 0.0
        price_chgs: list[float] = []
        usable = 0
        for exchange, base, tick_now in live_legs:
            before = await repo.tick_at_or_before_base(
                session, exchange, base, before_ts
            )
            if before is None or before.oi_usd is None or tick_now.oi_usd is None:
                continue
            before.ts_utc = ensure_utc(before.ts_utc)
            # слишком свежий «before» (меньше половины окна) — данных мало
            age = (ensure_utc(tick_now.ts_utc) - before.ts_utc).total_seconds()
            if age < sec * 0.5:
                continue
            oi_now = float(tick_now.oi_usd)
            oi_before = float(before.oi_usd)
            if oi_before <= 0:
                continue
            oi_chg = (oi_now / oi_before - 1.0) * 100.0
            pn = _tick_price(tick_now)
            pb = _tick_price(before)
            price_chg = None
            if pn is not None and pb and pb > 0:
                price_chg = (pn / pb - 1.0) * 100.0
                price_chgs.append(price_chg)
            per_ex.append((exchange.capitalize(), oi_now, oi_chg, price_chg))
            oi_now_sum += oi_now
            oi_before_sum += oi_before
            usable += 1
        if usable == 0:
            continue
        any_window = True
        agg_chg = (oi_now_sum / oi_before_sum - 1.0) * 100.0 if oi_before_sum > 0 else None
        # агрегированное изменение цены — среднее по биржам с данными
        agg_price = sum(price_chgs) / len(price_chgs) if price_chgs else None
        lines.append(window)
        lines.append(
            f"OI {format_oi(oi_now_sum)}  {format_pct(agg_chg)}  "
            f"Price {format_pct(agg_price)}"
        )
        for name, oi_now, oi_chg, price_chg in per_ex:
            lines.append(
                f"{name}  {format_oi(oi_now)}  {format_pct(oi_chg)}  "
                f"Price {format_pct(price_chg)}"
            )
        lines.append("")
    if not any_window:
        return (
            f"{display}\n\n"
            "Недостаточно истории для таймфреймов (нужны тики старше окна)."
        )
    return "\n".join(lines).rstrip() + "\n"
