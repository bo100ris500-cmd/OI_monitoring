"""Post-factum price enrichment worker."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

import aiohttp
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.collectors import build_collectors
from app.config import ConfigStore
from app.core.normalize import parse_window_to_seconds
from app.storage import repo
from app.storage.db import with_db_retry
from app.storage.models import Instrument, RawTick
from app.storage.write_lock import db_write_lock

logger = logging.getLogger(__name__)

HORIZON_FIELDS = {
    "5m": ("price_after_5m", "return_5m_pct"),
    "15m": ("price_after_15m", "return_15m_pct"),
    "1h": ("price_after_1h", "return_1h_pct"),
    "4h": ("price_after_4h", "return_4h_pct"),
    "1d": ("price_after_1d", "return_1d_pct"),
}


class PostFactumWorker:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession], config_store: ConfigStore):
        self.session_factory = session_factory
        self.config_store = config_store
        self._running = False

    async def start(self) -> None:
        self._running = True
        async with aiohttp.ClientSession() as http:
            while self._running:
                try:
                    await self.run_once(http)
                except Exception as e:  # noqa: BLE001
                    logger.exception("post_factum error: %s", e)
                await asyncio.sleep(60)

    async def stop(self) -> None:
        self._running = False

    async def run_once(self, http: aiohttp.ClientSession) -> None:
        cfg = self.config_store.snapshot()
        collectors = build_collectors(http, cfg)
        now = datetime.now(timezone.utc)

        async with self.session_factory() as session:
            pending = list(await repo.pending_post_factum(session, limit=40))
            pending_ids = [s.id for s in pending]

        for sig_id in pending_ids:
            await self._process_one(sig_id, collectors, cfg, now)

    async def _process_one(self, sig_id: int, collectors, cfg, now: datetime) -> None:
        from app.storage.models import Signal

        async with self.session_factory() as session:
            sig = await session.get(Signal, sig_id)
            if not sig or sig.post_factum_done:
                return
            ts = sig.ts_utc
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            base_price = sig.price_at_signal
            exchange = sig.exchange
            base_symbol = sig.base_symbol
            updates: dict = {}

            for horizon in cfg.post_factum.horizons:
                price_field, ret_field = HORIZON_FIELDS[horizon]
                if getattr(sig, price_field) is not None:
                    continue
                sec = parse_window_to_seconds(horizon)
                target = ts.timestamp() + sec
                if now.timestamp() < target:
                    continue

                tick = (
                    await session.execute(
                        select(RawTick)
                        .where(
                            RawTick.exchange == exchange,
                            RawTick.base_symbol == base_symbol,
                            RawTick.ts_utc
                            >= datetime.fromtimestamp(target - 90, tz=timezone.utc),
                            RawTick.ts_utc
                            <= datetime.fromtimestamp(target + 90, tz=timezone.utc),
                        )
                        .order_by(RawTick.ts_utc.asc())
                        .limit(1)
                    )
                ).scalar_one_or_none()
                px = tick.mark_price or tick.last_price if tick else None

                if px is None:
                    coll = collectors.get(exchange.lower())
                    if coll:
                        inst = (
                            await session.execute(
                                select(Instrument).where(
                                    Instrument.exchange == exchange,
                                    Instrument.base_symbol == base_symbol,
                                    Instrument.is_active.is_(True),
                                )
                            )
                        ).scalar_one_or_none()
                        if inst:
                            # сеть вне write-lock / без долгой транзакции
                            snap = await coll.fetch_one(inst.contract_symbol, base_symbol)
                            if snap:
                                px = snap.mark_price or snap.last_price

                if px is None:
                    continue
                updates[price_field] = px
                if base_price and base_price > 0:
                    updates[ret_field] = (px / base_price - 1.0) * 100.0

            # merged view for done-check
            for k, v in updates.items():
                setattr(sig, k, v)
            missing = [
                h
                for h in cfg.post_factum.horizons
                if getattr(sig, HORIZON_FIELDS[h][0]) is None
            ]
            oldest_needed = max(parse_window_to_seconds(h) for h in cfg.post_factum.horizons)
            done = False
            if not missing:
                done = True
            elif now.timestamp() > ts.timestamp() + oldest_needed + 3600:
                done = True

        if not updates and not done:
            return

        async def _commit() -> None:
            async with db_write_lock():
                async with self.session_factory() as session:
                    sig2 = await session.get(Signal, sig_id)
                    if not sig2:
                        return
                    for k, v in updates.items():
                        setattr(sig2, k, v)
                    if done:
                        sig2.post_factum_done = True
                    await session.commit()

        try:
            await with_db_retry(_commit, retries=10, delay=0.2)
        except Exception as e:  # noqa: BLE001
            logger.error("post_factum commit signal %s: %s", sig_id, e)
