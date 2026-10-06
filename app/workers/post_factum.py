"""Post-factum price enrichment worker."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

import aiohttp
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.collectors import build_collectors
from app.config import ConfigStore
from app.core.normalize import parse_window_to_seconds
from app.storage import repo

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
            pending = await repo.pending_post_factum(session, limit=40)
            for sig in pending:
                ts = sig.ts_utc
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                all_ready = True
                base_price = sig.price_at_signal
                for horizon in cfg.post_factum.horizons:
                    price_field, ret_field = HORIZON_FIELDS[horizon]
                    current = getattr(sig, price_field)
                    if current is not None:
                        continue
                    sec = parse_window_to_seconds(horizon)
                    target = ts.timestamp() + sec
                    if now.timestamp() < target:
                        all_ready = False
                        continue
                    # Prefer tick from DB near target; else live fetch as approximation if just due
                    from sqlalchemy import select
                    from app.storage.models import RawTick

                    tick = (
                        await session.execute(
                            select(RawTick)
                            .where(
                                RawTick.exchange == sig.exchange,
                                RawTick.base_symbol == sig.base_symbol,
                                RawTick.ts_utc >= datetime.fromtimestamp(target - 90, tz=timezone.utc),
                                RawTick.ts_utc <= datetime.fromtimestamp(target + 90, tz=timezone.utc),
                            )
                            .order_by(RawTick.ts_utc.asc())
                            .limit(1)
                        )
                    ).scalar_one_or_none()
                    px = None
                    if tick:
                        px = tick.mark_price or tick.last_price
                    if px is None:
                        coll = collectors.get(sig.exchange.lower())
                        if coll:
                            # find contract
                            from app.storage.models import Instrument

                            inst = (
                                await session.execute(
                                    select(Instrument).where(
                                        Instrument.exchange == sig.exchange,
                                        Instrument.base_symbol == sig.base_symbol,
                                        Instrument.is_active.is_(True),
                                    )
                                )
                            ).scalar_one_or_none()
                            if inst:
                                snap = await coll.fetch_one(inst.contract_symbol, sig.base_symbol)
                                if snap:
                                    px = snap.mark_price or snap.last_price
                    if px is None:
                        all_ready = False
                        continue
                    setattr(sig, price_field, px)
                    if base_price and base_price > 0:
                        setattr(sig, ret_field, (px / base_price - 1.0) * 100.0)
                # done when all horizons present or older than 1d+buffer and remaining missing
                missing = [h for h in cfg.post_factum.horizons if getattr(sig, HORIZON_FIELDS[h][0]) is None]
                oldest_needed = max(parse_window_to_seconds(h) for h in cfg.post_factum.horizons)
                if not missing:
                    sig.post_factum_done = True
                elif now.timestamp() > ts.timestamp() + oldest_needed + 3600:
                    sig.post_factum_done = True  # give up on missing
                await session.commit()
