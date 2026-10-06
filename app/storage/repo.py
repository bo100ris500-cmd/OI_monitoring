"""Data access helpers."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.storage.models import (
    CooldownState,
    ConfigMeta,
    ExchangeHealth,
    Instrument,
    RawTick,
    Signal,
    User,
    UserSignalDelivery,
    WatchlistItem,
    utcnow,
)


async def get_or_create_user(session: AsyncSession, telegram_id: int, admin_ids: set[int]) -> User:
    result = await session.execute(select(User).where(User.telegram_id == telegram_id))
    user = result.scalar_one_or_none()
    if user:
        return user
    user = User(telegram_id=telegram_id, is_admin=telegram_id in admin_ids)
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


async def add_watchlist(session: AsyncSession, user_id: int, base_symbol: str) -> bool:
    result = await session.execute(
        select(WatchlistItem).where(
            WatchlistItem.user_id == user_id, WatchlistItem.base_symbol == base_symbol
        )
    )
    if result.scalar_one_or_none():
        return False
    session.add(WatchlistItem(user_id=user_id, base_symbol=base_symbol))
    await session.commit()
    return True


async def remove_watchlist(session: AsyncSession, user_id: int, base_symbol: str) -> bool:
    result = await session.execute(
        select(WatchlistItem).where(
            WatchlistItem.user_id == user_id, WatchlistItem.base_symbol == base_symbol
        )
    )
    item = result.scalar_one_or_none()
    if not item:
        return False
    await session.delete(item)
    await session.commit()
    return True


async def list_watchlist(session: AsyncSession, user_id: int) -> list[str]:
    result = await session.execute(
        select(WatchlistItem.base_symbol)
        .where(WatchlistItem.user_id == user_id)
        .order_by(WatchlistItem.base_symbol)
    )
    return list(result.scalars().all())


async def union_watchlist_symbols(session: AsyncSession) -> list[str]:
    result = await session.execute(select(WatchlistItem.base_symbol).distinct())
    return sorted(result.scalars().all())


async def users_watching(session: AsyncSession, base_symbol: str) -> list[User]:
    result = await session.execute(
        select(User)
        .join(WatchlistItem)
        .where(WatchlistItem.base_symbol == base_symbol)
    )
    return list(result.scalars().unique().all())


async def upsert_instrument(session: AsyncSession, **kwargs) -> Instrument:
    result = await session.execute(
        select(Instrument).where(
            Instrument.exchange == kwargs["exchange"],
            Instrument.contract_symbol == kwargs["contract_symbol"],
        )
    )
    inst = result.scalar_one_or_none()
    if inst:
        for k, v in kwargs.items():
            setattr(inst, k, v)
    else:
        inst = Instrument(**kwargs)
        session.add(inst)
    await session.commit()
    await session.refresh(inst)
    return inst


async def insert_tick(session: AsyncSession, tick: dict) -> None:
    existing = await session.execute(
        select(RawTick).where(
            RawTick.exchange == tick["exchange"],
            RawTick.contract_symbol == tick["contract_symbol"],
            RawTick.ts_utc == tick["ts_utc"],
        )
    )
    if existing.scalar_one_or_none():
        return
    session.add(RawTick(**tick))
    await session.commit()


async def latest_tick(
    session: AsyncSession, exchange: str, contract_symbol: str
) -> RawTick | None:
    result = await session.execute(
        select(RawTick)
        .where(RawTick.exchange == exchange, RawTick.contract_symbol == contract_symbol)
        .order_by(RawTick.ts_utc.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def tick_at_or_before(
    session: AsyncSession, exchange: str, contract_symbol: str, ts: datetime
) -> RawTick | None:
    result = await session.execute(
        select(RawTick)
        .where(
            RawTick.exchange == exchange,
            RawTick.contract_symbol == contract_symbol,
            RawTick.ts_utc <= ts,
        )
        .order_by(RawTick.ts_utc.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def ticks_since(
    session: AsyncSession, exchange: str, contract_symbol: str, since: datetime
) -> list[RawTick]:
    result = await session.execute(
        select(RawTick)
        .where(
            RawTick.exchange == exchange,
            RawTick.contract_symbol == contract_symbol,
            RawTick.ts_utc >= since,
        )
        .order_by(RawTick.ts_utc.asc())
    )
    return list(result.scalars().all())


async def instruments_for_bases(session: AsyncSession, bases: list[str]) -> list[Instrument]:
    if not bases:
        return []
    result = await session.execute(
        select(Instrument).where(Instrument.base_symbol.in_(bases), Instrument.is_active.is_(True))
    )
    return list(result.scalars().all())


async def all_active_instruments(session: AsyncSession) -> list[Instrument]:
    result = await session.execute(select(Instrument).where(Instrument.is_active.is_(True)))
    return list(result.scalars().all())


async def save_signal(session: AsyncSession, **kwargs) -> Signal | None:
    uid = kwargs["signal_uid"]
    existing = await session.execute(select(Signal).where(Signal.signal_uid == uid))
    if existing.scalar_one_or_none():
        return None
    sig = Signal(**kwargs)
    session.add(sig)
    await session.commit()
    await session.refresh(sig)
    return sig


async def mark_delivery(session: AsyncSession, user_id: int, signal_id: int) -> bool:
    existing = await session.execute(
        select(UserSignalDelivery).where(
            UserSignalDelivery.user_id == user_id,
            UserSignalDelivery.signal_id == signal_id,
        )
    )
    if existing.scalar_one_or_none():
        return False
    session.add(UserSignalDelivery(user_id=user_id, signal_id=signal_id))
    await session.commit()
    return True


async def get_cooldown(
    session: AsyncSession, user_id: int, exchange: str, base: str, signal_type: str
) -> CooldownState | None:
    result = await session.execute(
        select(CooldownState).where(
            CooldownState.user_id == user_id,
            CooldownState.exchange == exchange,
            CooldownState.base_symbol == base,
            CooldownState.signal_type == signal_type,
        )
    )
    return result.scalar_one_or_none()


async def upsert_cooldown(
    session: AsyncSession,
    user_id: int,
    exchange: str,
    base: str,
    signal_type: str,
    oi_growth: float,
) -> None:
    row = await get_cooldown(session, user_id, exchange, base, signal_type)
    if row:
        row.last_oi_growth = oi_growth
        row.last_sent_at = utcnow()
    else:
        session.add(
            CooldownState(
                user_id=user_id,
                exchange=exchange,
                base_symbol=base,
                signal_type=signal_type,
                last_oi_growth=oi_growth,
                last_sent_at=utcnow(),
            )
        )
    await session.commit()


async def save_config_meta(session: AsyncSession, config_hash: str) -> None:
    session.add(ConfigMeta(config_hash=config_hash, applied_at=utcnow()))
    await session.commit()


async def update_exchange_health(
    session: AsyncSession,
    exchange: str,
    *,
    success: bool,
    error: str | None = None,
) -> ExchangeHealth:
    result = await session.execute(select(ExchangeHealth).where(ExchangeHealth.exchange == exchange))
    row = result.scalar_one_or_none()
    if not row:
        row = ExchangeHealth(exchange=exchange)
        session.add(row)
    if success:
        row.last_success_at = utcnow()
        row.consecutive_failures = 0
        row.last_error = None
        row.alerted = False
    else:
        row.consecutive_failures = (row.consecutive_failures or 0) + 1
        row.last_error = error
    await session.commit()
    await session.refresh(row)
    return row


async def stats(session: AsyncSession) -> dict:
    users = await session.scalar(select(func.count()).select_from(User))
    wl = await session.scalar(select(func.count()).select_from(WatchlistItem))
    symbols = await session.scalar(select(func.count(func.distinct(WatchlistItem.base_symbol))))
    ticks = await session.scalar(select(func.count()).select_from(RawTick))
    signals = await session.scalar(select(func.count()).select_from(Signal))
    return {
        "users": users or 0,
        "watchlist_rows": wl or 0,
        "unique_symbols": symbols or 0,
        "raw_ticks": ticks or 0,
        "signals": signals or 0,
    }


async def pending_post_factum(session: AsyncSession, limit: int = 50) -> list[Signal]:
    result = await session.execute(
        select(Signal)
        .where(Signal.post_factum_done.is_(False))
        .order_by(Signal.ts_utc.asc())
        .limit(limit)
    )
    return list(result.scalars().all())


async def recent_s1_signals(
    session: AsyncSession, base: str, exchange: str, since: datetime
) -> list[Signal]:
    result = await session.execute(
        select(Signal).where(
            Signal.base_symbol == base,
            Signal.exchange == exchange,
            Signal.ts_utc >= since,
        )
    )
    out = []
    for s in result.scalars().all():
        types = json.loads(s.types_json)
        if "S1" in types:
            out.append(s)
    return out


async def s1_in_window(
    session: AsyncSession, base: str, since: datetime
) -> list[Signal]:
    result = await session.execute(
        select(Signal).where(Signal.base_symbol == base, Signal.ts_utc >= since)
    )
    out = []
    for s in result.scalars().all():
        if "S1" in json.loads(s.types_json):
            out.append(s)
    return out


async def purge_old_ticks(session: AsyncSession, keep_days: int = 30) -> int:
    cutoff = datetime.now(timezone.utc) - timedelta(days=keep_days)
    result = await session.execute(delete(RawTick).where(RawTick.ts_utc < cutoff))
    await session.commit()
    return result.rowcount or 0
