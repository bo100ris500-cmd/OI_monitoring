"""Filters F1–F8."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from app.config import AppConfig
from app.core.metrics import WindowMetrics
from app.core.normalize import is_excluded_base
from app.core.signals import FiredSignal
from app.storage.models import CooldownState, Instrument


@dataclass
class FilterResult:
    allow: bool
    flags: dict[str, Any] = field(default_factory=dict)
    drop_reason: str | None = None


def apply_pre_filters(
    *,
    cfg: AppConfig,
    base: str,
    exchange: str,
    metrics: WindowMetrics,
    instrument: Instrument | None,
    tick_age_sec: float | None,
    interval_sec: int,
) -> FilterResult:
    flags: dict[str, Any] = {}

    # F4
    if is_excluded_base(base, cfg.normalization, exchange):
        return FilterResult(False, flags, "F4_excluded")

    # F1 liquidity
    f1 = cfg.filters.F1
    if metrics.oi_now is None or metrics.oi_now < f1.min_oi_usd:
        return FilterResult(False, flags, "F1_oi")
    if metrics.volume_24h is None or metrics.volume_24h < f1.min_volume_24h_usd:
        return FilterResult(False, flags, "F1_volume")

    # F5 stale / price required
    f5 = cfg.filters.F5
    if tick_age_sec is not None and tick_age_sec > f5.stale_intervals * interval_sec:
        return FilterResult(False, flags, "F5_stale")
    if metrics.price_now is None:
        return FilterResult(False, flags, "F5_no_price")

    # F2
    if metrics.new_history:
        flags["new_history"] = True

    # F3 new listing
    f3 = cfg.filters.F3
    if instrument and instrument.listed_at:
        listed = instrument.listed_at
        if listed.tzinfo is None:
            listed = listed.replace(tzinfo=timezone.utc)
        age_days = (datetime.now(timezone.utc) - listed).total_seconds() / 86400
        if age_days < f3.new_listing_days:
            flags["new_listing"] = True
            if f3.action == "exclude":
                return FilterResult(False, flags, "F3_new_listing")

    return FilterResult(True, flags)


def cooldown_allows(
    *,
    cfg: AppConfig,
    cooldown: CooldownState | None,
    signal_type: str,
    oi_growth: float | None,
    now: datetime | None = None,
) -> bool:
    """F6. /remove handled outside by watchlist check."""
    if cooldown is None:
        return True
    now = now or datetime.now(timezone.utc)
    last = cooldown.last_sent_at
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    hours = cfg.filters.F6.cooldown_hours
    if now - last >= timedelta(hours=hours):
        return True
    # allow if oi growth exceeds previous by +extra pp
    if oi_growth is not None:
        if oi_growth >= cooldown.last_oi_growth + cfg.filters.F6.extra_growth_pct_points:
            return True
    return False


def filter_fired_by_cooldown(
    fired: list[FiredSignal],
    *,
    cfg: AppConfig,
    cooldown_map: dict[str, CooldownState | None],
    oi_growth: float | None,
) -> list[FiredSignal]:
    out = []
    for f in fired:
        cd = cooldown_map.get(f.code)
        if cooldown_allows(cfg=cfg, cooldown=cd, signal_type=f.code, oi_growth=oi_growth):
            out.append(f)
    return out


def apply_f8_flags(
    *,
    cfg: AppConfig,
    metrics_btc: WindowMetrics | None,
    window: str,
    signal_bases_share: float | None,
) -> dict[str, Any]:
    flags: dict[str, Any] = {}
    f8 = cfg.filters.F8
    if metrics_btc and metrics_btc.price_change_pct is not None:
        if abs(metrics_btc.price_change_pct) > f8.btc_move_pct:
            flags["market_wide"] = True
            flags["btc_move_pct"] = metrics_btc.price_change_pct
    if signal_bases_share is not None and signal_bases_share * 100 >= f8.market_signal_pct:
        flags["market_wide"] = True
        flags["market_signal_pct"] = signal_bases_share * 100
    return flags
