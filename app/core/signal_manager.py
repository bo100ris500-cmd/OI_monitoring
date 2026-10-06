"""Signal merge, persistence, Telegram formatting, delivery."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from aiogram import Bot
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import AppConfig
from app.core.filters import cooldown_allows
from app.core.metrics import WindowMetrics
from app.core.signals import FiredSignal, SIGNAL_NAMES
from app.storage import repo

logger = logging.getLogger(__name__)


def _fmt_usd(v: float | None) -> str:
    if v is None:
        return "—"
    abs_v = abs(v)
    if abs_v >= 1_000_000_000:
        return f"${v/1_000_000_000:.2f}B"
    if abs_v >= 1_000_000:
        return f"${v/1_000_000:.2f}M"
    if abs_v >= 1_000:
        return f"${v/1_000:.1f}K"
    return f"${v:.4g}"


def _fmt_pct(v: float | None, signed: bool = True) -> str:
    if v is None:
        return "—"
    if signed:
        return f"{v:+.2f}%"
    return f"{v:.2f}%"


def format_signal_message(
    *,
    base: str,
    exchange: str,
    window: str,
    fired: list[FiredSignal],
    metrics: WindowMetrics,
    s7_status: str,
    flags: dict[str, Any],
    ts: datetime,
) -> str:
    codes = sorted({f.code for f in fired})
    titles = " + ".join(codes)
    regime = next((f.details.get("regime") for f in fired if f.code == "S2"), None)
    lines = [
        f"🚨 {titles}",
        f"{base} · {window.upper()}",
        f"OI       {_fmt_pct(metrics.oi_growth_pct)}   {_fmt_usd(metrics.oi_before)} → {_fmt_usd(metrics.oi_now)}",
        f"Price     {_fmt_pct(metrics.price_change_pct)}",
    ]
    if metrics.oi_z is not None:
        lines.append(f"OI Z       {metrics.oi_z:.2f}")
    if metrics.oi_pctl is not None:
        lines.append(f"Pctl      {metrics.oi_pctl:.1f}%")
    lines.append(f"Funding   {_fmt_pct(metrics.funding_8h)}")
    if metrics.long_pct is not None:
        short = metrics.short_pct if metrics.short_pct is not None else 100 - metrics.long_pct
        lines.append(f"L/S       {metrics.long_pct:.1f} / {short:.1f}")
    lines.append(f"Vol 24h   {_fmt_usd(metrics.volume_24h)}")
    if metrics.oi_to_volume is not None:
        lines.append(f"OI/Vol    {metrics.oi_to_volume:.3f}")
    lines.append(f"📍 {exchange.capitalize()}")
    if s7_status == "confirmed":
        lines.append("🌐 Confirmed")
    else:
        lines.append("🌐 Local")
    if regime:
        lines.append(regime)
    if flags.get("new_history"):
        lines.append("⚠ new_history")
    if flags.get("new_listing"):
        lines.append("⚠ new_listing")
    if flags.get("market_wide"):
        lines.append("⚠ market_wide")
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    lines.append(ts.strftime("%d.%m.%Y %H:%M UTC"))
    names = ", ".join(f"{c} {SIGNAL_NAMES.get(c, '')}" for c in codes)
    lines.insert(1, names)
    return "\n".join(lines)


def make_signal_uid(base: str, exchange: str, window: str, codes: list[str], minute_ts: int) -> str:
    return f"{base}|{exchange}|{window}|{'-'.join(sorted(codes))}|{minute_ts}"


async def persist_and_notify(
    *,
    bot: Bot,
    session_factory: async_sessionmaker[AsyncSession],
    cfg: AppConfig,
    base: str,
    exchange: str,
    window: str,
    fired: list[FiredSignal],
    metrics: WindowMetrics,
    s7_status: str,
    flags: dict[str, Any],
    ts: datetime,
) -> None:
    if not fired:
        return
    codes = sorted({f.code for f in fired})
    minute_ts = int(ts.timestamp()) // 60 * 60
    uid = make_signal_uid(base, exchange, window, codes, minute_ts)
    payload = {
        "metrics": metrics.__dict__,
        "fired": [{"code": f.code, "window": f.window, "details": f.details} for f in fired],
        "flags": flags,
    }
    # convert non-serializable
    payload_json = json.dumps(payload, default=str)

    async with session_factory() as session:
        sig = await repo.save_signal(
            session,
            signal_uid=uid,
            base_symbol=base,
            exchange=exchange,
            window=window,
            types_json=json.dumps(codes),
            payload_json=payload_json,
            ts_utc=ts,
            s7_status=s7_status,
            flags_json=json.dumps(flags),
            price_at_signal=metrics.price_now,
        )
        if sig is None:
            logger.debug("duplicate signal uid=%s", uid)
            return

        users = await repo.users_watching(session, base)
        text = format_signal_message(
            base=base,
            exchange=exchange,
            window=window,
            fired=fired,
            metrics=metrics,
            s7_status=s7_status,
            flags=flags,
            ts=ts,
        )
        oi_growth = metrics.oi_growth_pct
        for user in users:
            # per-type cooldown
            allowed_codes = []
            for code in codes:
                cd = await repo.get_cooldown(session, user.id, exchange, base, code)
                if cooldown_allows(cfg=cfg, cooldown=cd, signal_type=code, oi_growth=oi_growth):
                    allowed_codes.append(code)
            if not allowed_codes:
                continue
            # still require at least one overlapping with fired after cooldown
            if not await repo.mark_delivery(session, user.id, sig.id):
                continue
            try:
                await bot.send_message(user.telegram_id, text)
                for code in allowed_codes:
                    await repo.upsert_cooldown(
                        session, user.id, exchange, base, code, oi_growth or 0.0
                    )
            except Exception as e:  # noqa: BLE001
                logger.error("notify user %s: %s", user.telegram_id, e)
