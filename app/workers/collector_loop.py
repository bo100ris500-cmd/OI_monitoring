"""Collector loop, metrics, signals pipeline."""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from datetime import datetime, timedelta

import aiohttp
import psutil
from aiogram import Bot
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.collectors import build_collectors
from app.config import AppConfig, ConfigStore
from app.core.filters import apply_f8_flags, apply_pre_filters
from app.core.metrics import compute_window_metrics, history_cutoff
from app.core.normalize import parse_window_to_seconds
from app.core.signal_manager import persist_and_notify
from app.core.signals import evaluate_signals
from app.core.timeutils import ensure_utc, utcnow
from app.storage import repo
from app.storage.db import with_db_retry

logger = logging.getLogger(__name__)


class Pipeline:
    def __init__(
        self,
        bot: Bot,
        session_factory: async_sessionmaker[AsyncSession],
        config_store: ConfigStore,
    ):
        self.bot = bot
        self.session_factory = session_factory
        self.config_store = config_store
        self._http: aiohttp.ClientSession | None = None
        self._running = False
        self._db_write_lock = asyncio.Lock()
        # recent S1 events for S7: base -> list[(ts, exchange)]
        self._recent_s1: dict[str, list[tuple[datetime, str]]] = defaultdict(list)

    async def start(self) -> None:
        self._running = True
        self._http = aiohttp.ClientSession()
        while self._running:
            cfg = self.config_store.snapshot()
            t0 = asyncio.get_event_loop().time()
            try:
                await self.run_once(cfg)
            except Exception as e:  # noqa: BLE001
                logger.exception("pipeline cycle error: %s", e)
            # memory guard
            rss_mb = psutil.Process().memory_info().rss / (1024 * 1024)
            if rss_mb > cfg.resources.max_memory_mb:
                logger.warning("RSS %.0f MB > limit %s", rss_mb, cfg.resources.max_memory_mb)
            elapsed = asyncio.get_event_loop().time() - t0
            sleep_for = max(1.0, cfg.collector.interval_sec - elapsed)
            await asyncio.sleep(sleep_for)

    async def stop(self) -> None:
        self._running = False
        if self._http:
            await self._http.close()

    async def run_once(self, cfg: AppConfig) -> None:
        assert self._http is not None
        collectors = build_collectors(self._http, cfg)
        sem = asyncio.Semaphore(cfg.resources.max_concurrent_requests)

        async with self.session_factory() as session:
            if cfg.monitoring_mode == "global":
                from sqlalchemy import select
                from app.storage.models import Instrument

                result = await session.execute(
                    select(Instrument.base_symbol).distinct().where(Instrument.is_active.is_(True))
                )
                bases = list(result.scalars().all())
                if not bases:
                    bases = await repo.union_watchlist_symbols(session)
            else:
                bases = await repo.union_watchlist_symbols(session)

            # Always include BTC for F8 background
            if "BTC" not in [b.upper() for b in bases]:
                bases = list(bases) + ["BTC"]

        if not bases:
            logger.debug("watchlist empty — skip collect")
            return

        base_set = set(bases)
        snapshots = []
        snap_lock = asyncio.Lock()

        async def collect_exchange(name: str, coll) -> None:
            async with sem:
                try:
                    snaps = await coll.discover(base_set)
                    # F5: повторное чтение вне DB-lock
                    prepared: list = []
                    for s in snaps:
                        need_confirm = False
                        async with self._db_write_lock:
                            async with self.session_factory() as session:
                                prev = await repo.latest_tick(
                                    session, s.exchange, s.contract_symbol
                                )
                                if (
                                    prev
                                    and prev.oi_usd
                                    and s.oi_usd
                                    and prev.oi_usd > 0
                                    and s.oi_usd / prev.oi_usd
                                    > cfg.filters.F5.max_oi_jump_ratio
                                ):
                                    need_confirm = True
                        if need_confirm:
                            confirmed = s
                            for _ in range(cfg.filters.F5.confirmation_reads - 1):
                                await asyncio.sleep(0.5)
                                again = await coll.fetch_one(s.contract_symbol, s.base_symbol)
                                if again and again.oi_usd:
                                    confirmed = again
                            prepared.append(confirmed)
                        else:
                            prepared.append(s)

                    async with self._db_write_lock:
                        async with self.session_factory() as session:
                            await repo.update_exchange_health(session, name, success=True)
                            for s in prepared:
                                await repo.upsert_instrument(
                                    session,
                                    commit=False,
                                    exchange=s.exchange,
                                    contract_symbol=s.contract_symbol,
                                    base_symbol=s.base_symbol,
                                    quote=s.quote,
                                    multiplier=s.multiplier,
                                    is_active=True,
                                )
                                await repo.insert_tick(
                                    session,
                                    {
                                        "exchange": s.exchange,
                                        "contract_symbol": s.contract_symbol,
                                        "base_symbol": s.base_symbol,
                                        "ts_utc": s.ts_utc,
                                        "oi_usd": s.oi_usd,
                                        "last_price": s.last_price,
                                        "mark_price": s.mark_price,
                                        "funding_rate": s.funding_rate,
                                        "volume_24h": s.volume_24h,
                                        "long_pct": s.long_pct,
                                        "short_pct": s.short_pct,
                                    },
                                    commit=False,
                                )
                                async with snap_lock:
                                    snapshots.append(s)
                            await session.commit()
                except Exception as e:  # noqa: BLE001
                    logger.error("collector %s failed: %s", name, e)
                    async with self._db_write_lock:
                        async with self.session_factory() as session:
                            health = await repo.update_exchange_health(
                                session, name, success=False, error=str(e)
                            )
                            if (
                                health.consecutive_failures
                                >= cfg.monitoring.missing_data_intervals
                                and not health.alerted
                            ):
                                await self._alert_admins(
                                    f"⚠ Exchange `{name}` no data for "
                                    f"{health.consecutive_failures} intervals: {e}"
                                )
                                health.alerted = True
                                await session.commit()

        await asyncio.gather(*[collect_exchange(n, c) for n, c in collectors.items()])

        # Evaluate signals per snapshot
        signal_bases: set[str] = set()
        btc_metrics_by_window: dict[str, object] = {}

        for snap in snapshots:
            await self._evaluate_snapshot(cfg, snap, signal_bases, btc_metrics_by_window)

        # cleanup collectors
        for c in collectors.values():
            await c.close()

    async def _alert_admins(self, text: str) -> None:
        from app.settings import get_settings

        for admin_id in get_settings().admin_id_set:
            try:
                await self.bot.send_message(admin_id, text)
            except Exception as e:  # noqa: BLE001
                logger.error("admin alert failed: %s", e)

    async def _evaluate_snapshot(
        self,
        cfg: AppConfig,
        snap,
        signal_bases: set[str],
        btc_metrics_by_window: dict,
    ) -> None:
        async with self.session_factory() as session:
            now_tick = await repo.latest_tick(session, snap.exchange, snap.contract_symbol)
            if not now_tick:
                return
            # SQLite returns naive datetimes — normalize before any arithmetic
            now_tick.ts_utc = ensure_utc(now_tick.ts_utc)
            hist_since = history_cutoff(cfg)
            history = await repo.ticks_since(
                session, snap.exchange, snap.contract_symbol, hist_since
            )
            for t in history:
                t.ts_utc = ensure_utc(t.ts_utc)
            metrics_by_window = {}
            for window in cfg.windows:
                sec = parse_window_to_seconds(window)
                before_ts = now_tick.ts_utc - timedelta(seconds=sec)
                before = await repo.tick_at_or_before(
                    session, snap.exchange, snap.contract_symbol, before_ts
                )
                if before:
                    before.ts_utc = ensure_utc(before.ts_utc)
                metrics_by_window[window] = compute_window_metrics(
                    now_tick, before, history, window, cfg, snap.exchange
                )

            # pick primary window for filters: first configured
            primary_w = cfg.windows[0]
            primary_m = metrics_by_window[primary_w]
            age = (utcnow() - now_tick.ts_utc).total_seconds()

            from sqlalchemy import select
            from app.storage.models import Instrument

            inst = (
                await session.execute(
                    select(Instrument).where(
                        Instrument.exchange == snap.exchange,
                        Instrument.contract_symbol == snap.contract_symbol,
                    )
                )
            ).scalar_one_or_none()

            pre = apply_pre_filters(
                cfg=cfg,
                base=snap.base_symbol,
                exchange=snap.exchange,
                metrics=primary_m,
                instrument=inst,
                tick_age_sec=age,
                interval_sec=cfg.collector.interval_sec,
            )
            if not pre.allow:
                return

            # S5 helpers
            since_s1 = utcnow() - timedelta(hours=cfg.signals.S5.lookback_hours)
            prior_s1 = await repo.recent_s1_signals(
                session, snap.base_symbol, snap.exchange, since_s1
            )
            oi_drop_1h = None
            m1h = metrics_by_window.get("1h")
            if m1h and m1h.oi_growth_pct is not None:
                oi_drop_1h = m1h.oi_growth_pct

            fired = evaluate_signals(
                metrics_by_window,
                cfg,
                had_s1_last_24h=bool(prior_s1),
                oi_drop_1h_pct=oi_drop_1h,
            )
            if not fired:
                return

            # S7 tracking
            now = utcnow()
            if any(f.code == "S1" for f in fired):
                self._recent_s1[snap.base_symbol].append((now, snap.exchange))
                # prune
                win = timedelta(minutes=cfg.signals.S7.confirmation_window_min)
                self._recent_s1[snap.base_symbol] = [
                    x for x in self._recent_s1[snap.base_symbol] if now - x[0] <= win
                ]

            exchanges_hit = {ex for _, ex in self._recent_s1.get(snap.base_symbol, [])}
            s7_status = (
                "confirmed"
                if len(exchanges_hit) >= cfg.signals.S7.exchanges_required
                else "local"
            )
            if s7_status == "confirmed" and not any(f.code == "S7" for f in fired):
                from app.core.signals import FiredSignal, SIGNAL_NAMES

                fired.append(
                    FiredSignal(
                        "S7",
                        SIGNAL_NAMES["S7"],
                        primary_w,
                        {"exchanges": sorted(exchanges_hit)},
                    )
                )

            # F8
            share = None
            f8 = apply_f8_flags(
                cfg=cfg,
                metrics_btc=btc_metrics_by_window.get(primary_w),  # type: ignore
                window=primary_w,
                signal_bases_share=share,
            )
            flags = {**pre.flags, **f8}

            if snap.base_symbol == "BTC":
                btc_metrics_by_window.update(metrics_by_window)

            signal_bases.add(snap.base_symbol)

            # Notify using best window among fired (prefer window with S1)
            by_window: dict[str, list] = defaultdict(list)
            for f in fired:
                by_window[f.window].append(f)
            for window, flist in by_window.items():
                m = metrics_by_window.get(window) or primary_m

                async def _notify(w=window, fl=flist, met=m):
                    async with self._db_write_lock:
                        await persist_and_notify(
                            bot=self.bot,
                            session_factory=self.session_factory,
                            cfg=cfg,
                            base=snap.base_symbol,
                            exchange=snap.exchange,
                            window=w,
                            fired=fl,
                            metrics=met,
                            s7_status=s7_status,
                            flags=flags,
                            ts=now_tick.ts_utc,
                        )

                try:
                    await with_db_retry(_notify)
                except Exception as e:  # noqa: BLE001
                    logger.error(
                        "persist/notify failed %s %s %s: %s",
                        snap.base_symbol,
                        snap.exchange,
                        window,
                        e,
                    )
