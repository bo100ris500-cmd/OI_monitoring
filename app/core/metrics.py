"""Metrics calculation for OI windows."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Sequence

from app.config import AppConfig
from app.core.normalize import parse_window_to_seconds
from app.storage.models import RawTick


@dataclass
class WindowMetrics:
    window: str
    oi_now: float | None
    oi_before: float | None
    oi_growth_pct: float | None
    oi_log_change: float | None
    oi_z: float | None
    oi_pctl: float | None
    price_now: float | None
    price_before: float | None
    price_change_pct: float | None
    price_z: float | None
    gap: float | None
    ratio: float | None
    funding_8h: float | None
    oi_to_volume: float | None
    long_pct: float | None
    short_pct: float | None
    volume_24h: float | None
    history_days: float
    new_history: bool
    oi_volume_pctl: float | None = None


def _median(xs: list[float]) -> float:
    if not xs:
        return float("nan")
    s = sorted(xs)
    n = len(s)
    mid = n // 2
    if n % 2:
        return s[mid]
    return (s[mid - 1] + s[mid]) / 2


def _mad(xs: list[float], med: float) -> float:
    if not xs:
        return float("nan")
    return _median([abs(x - med) for x in xs])


def robust_z(value: float, history: list[float]) -> float | None:
    if len(history) < 3:
        return None
    med = _median(history)
    mad = _mad(history, med)
    if mad == 0 or math.isnan(mad):
        return 0.0 if value == med else None
    return 0.6745 * (value - med) / mad


def percentile_rank(value: float, history: list[float]) -> float | None:
    if not history:
        return None
    below = sum(1 for x in history if x <= value)
    return 100.0 * below / len(history)


def funding_to_8h(funding_rate: float | None, exchange: str) -> float | None:
    """Normalize funding to 8h period. Most perps quote per 8h already; HL may differ."""
    if funding_rate is None:
        return None
    # Stored as fraction (0.0001 = 0.01%). Convert to percent for display/thresholds in TZ
    # TZ thresholds like +0.05% mean 0.05 in percent units.
    # We keep funding_8h as percent value (e.g. 0.012 means 0.012%).
    rate_pct = funding_rate * 100.0
    ex = exchange.lower()
    if ex == "hyperliquid":
        # HL often hourly — approximate to 8h
        return rate_pct * 8.0
    return rate_pct


def _price(t: RawTick) -> float | None:
    return t.mark_price if t.mark_price is not None else t.last_price


def growth_series(ticks: Sequence[RawTick], window_sec: int) -> list[float]:
    """OI growth % samples over history for z/pctl."""
    if len(ticks) < 2:
        return []
    by_ts = list(ticks)
    out: list[float] = []
    # sample at most every interval-ish using later ticks
    step = max(1, len(by_ts) // 500)
    for i in range(0, len(by_ts), step):
        now = by_ts[i]
        if now.oi_usd is None:
            continue
        target = now.ts_utc.timestamp() - window_sec
        before = None
        for j in range(i, -1, -1):
            if by_ts[j].ts_utc.timestamp() <= target and by_ts[j].oi_usd:
                before = by_ts[j]
                break
        if before and before.oi_usd and before.oi_usd > 0:
            out.append((now.oi_usd / before.oi_usd - 1.0) * 100.0)
    return out


def price_growth_series(ticks: Sequence[RawTick], window_sec: int) -> list[float]:
    if len(ticks) < 2:
        return []
    by_ts = list(ticks)
    out: list[float] = []
    step = max(1, len(by_ts) // 500)
    for i in range(0, len(by_ts), step):
        now = by_ts[i]
        pn = _price(now)
        if pn is None:
            continue
        target = now.ts_utc.timestamp() - window_sec
        before = None
        for j in range(i, -1, -1):
            pj = _price(by_ts[j])
            if by_ts[j].ts_utc.timestamp() <= target and pj:
                before = by_ts[j]
                break
        if before:
            pb = _price(before)
            if pb and pb > 0:
                out.append((pn / pb - 1.0) * 100.0)
    return out


def oi_to_volume_series(ticks: Sequence[RawTick]) -> list[float]:
    out = []
    for t in ticks:
        if t.oi_usd and t.volume_24h and t.volume_24h > 0:
            out.append(t.oi_usd / t.volume_24h)
    return out


def compute_window_metrics(
    now: RawTick,
    before: RawTick | None,
    history_ticks: Sequence[RawTick],
    window: str,
    cfg: AppConfig,
    exchange: str,
) -> WindowMetrics:
    window_sec = parse_window_to_seconds(window)
    if history_ticks:
        first = history_ticks[0].ts_utc
        last = history_ticks[-1].ts_utc
        if first.tzinfo is None:
            first = first.replace(tzinfo=timezone.utc)
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        history_days = max(0.0, (last - first).total_seconds() / 86400.0)
    else:
        history_days = 0.0
    new_history = history_days < cfg.history.minimum_days

    oi_now = now.oi_usd
    oi_before = before.oi_usd if before else None
    price_now = _price(now)
    price_before = _price(before) if before else None

    oi_growth = None
    oi_log = None
    if oi_now is not None and oi_before and oi_before > 0:
        oi_growth = (oi_now / oi_before - 1.0) * 100.0
        oi_log = math.log(oi_now / oi_before)

    price_change = None
    if price_now is not None and price_before and price_before > 0:
        price_change = (price_now / price_before - 1.0) * 100.0

    oi_z = oi_pctl = price_z = None
    oi_vol_pctl = None
    if not new_history and oi_growth is not None:
        oi_hist = growth_series(history_ticks, window_sec)
        oi_z = robust_z(oi_growth, oi_hist)
        oi_pctl = percentile_rank(oi_growth, oi_hist)
    if not new_history and price_change is not None:
        p_hist = price_growth_series(history_ticks, window_sec)
        price_z = robust_z(price_change, p_hist)
    if not new_history and oi_now and now.volume_24h and now.volume_24h > 0:
        ov = oi_now / now.volume_24h
        ov_hist = oi_to_volume_series(history_ticks)
        oi_vol_pctl = percentile_rank(ov, ov_hist)

    gap = None
    ratio = None
    if oi_growth is not None and price_change is not None:
        gap = oi_growth - price_change
        if oi_growth != 0:
            ratio = price_change / oi_growth
        else:
            ratio = None

    oi_to_vol = None
    if oi_now and now.volume_24h and now.volume_24h > 0:
        oi_to_vol = oi_now / now.volume_24h

    return WindowMetrics(
        window=window,
        oi_now=oi_now,
        oi_before=oi_before,
        oi_growth_pct=oi_growth,
        oi_log_change=oi_log,
        oi_z=oi_z,
        oi_pctl=oi_pctl,
        price_now=price_now,
        price_before=price_before,
        price_change_pct=price_change,
        price_z=price_z,
        gap=gap,
        ratio=ratio,
        funding_8h=funding_to_8h(now.funding_rate, exchange),
        oi_to_volume=oi_to_vol,
        long_pct=now.long_pct,
        short_pct=now.short_pct if now.short_pct is not None else (
            100.0 - now.long_pct if now.long_pct is not None else None
        ),
        volume_24h=now.volume_24h,
        history_days=history_days,
        new_history=new_history,
        oi_volume_pctl=oi_vol_pctl,
    )


def history_cutoff(cfg: AppConfig) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=cfg.history.zscore_days)
