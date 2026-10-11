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
    WatchlistBinding,
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


async def get_watchlist_item(
    session: AsyncSession, user_id: int, base_symbol: str
) -> WatchlistItem | None:
    result = await session.execute(
        select(WatchlistItem).where(
            WatchlistItem.user_id == user_id, WatchlistItem.base_symbol == base_symbol
        )
    )
    return result.scalar_one_or_none()


async def add_watchlist_with_bindings(
    session: AsyncSession,
    user_id: int,
    display_symbol: str,
    pairs: list[tuple[str, str]],
) -> tuple[str, bool]:
    """Добавить группу (биржа, тикер). OI считается как одна монета (display_symbol).

    pairs: [(exchange, base_symbol), ...]
    Returns: (display_symbol, created_new_item)
    """
    display_symbol = display_symbol.upper()
    item = await get_watchlist_item(session, user_id, display_symbol)
    created = False
    if not item:
        # если display уже занят другим тикером — всё равно создаём под первым тикером
        item = WatchlistItem(user_id=user_id, base_symbol=display_symbol)
        session.add(item)
        await session.flush()
        created = True
    existing = (
        await session.execute(
            select(WatchlistBinding).where(WatchlistBinding.watchlist_id == item.id)
        )
    ).scalars().all()
    by_ex = {b.exchange.lower(): b for b in existing}
    for exchange, ticker in pairs:
        ex = exchange.lower()
        tick = ticker.upper()
        if ex in by_ex:
            by_ex[ex].base_symbol = tick
        else:
            session.add(
                WatchlistBinding(watchlist_id=item.id, exchange=ex, base_symbol=tick)
            )
    await session.commit()
    return display_symbol, created


async def add_watchlist_exchange(
    session: AsyncSession, user_id: int, base_symbol: str, exchange: str
) -> str:
    """Добавить тикер на конкретной бирже. Возвращает статус-текст."""
    base_symbol = base_symbol.upper()
    exchange = exchange.lower()
    item = await get_watchlist_item(session, user_id, base_symbol)
    if item is None:
        item = WatchlistItem(user_id=user_id, base_symbol=base_symbol)
        session.add(item)
        await session.flush()
        session.add(
            WatchlistBinding(
                watchlist_id=item.id, exchange=exchange, base_symbol=base_symbol
            )
        )
        await session.commit()
        return "added_exchange"
    bindings = (
        await session.execute(
            select(WatchlistBinding).where(WatchlistBinding.watchlist_id == item.id)
        )
    ).scalars().all()
    if not bindings:
        # уже мониторится на всех биржах
        return "already_all"
    for b in bindings:
        if b.exchange.lower() == exchange:
            return "already_exchange"
    session.add(
        WatchlistBinding(
            watchlist_id=item.id, exchange=exchange, base_symbol=base_symbol
        )
    )
    await session.commit()
    return "added_exchange"


async def add_watchlist_all_exchanges(
    session: AsyncSession, user_id: int, base_symbol: str, exchanges: list[str]
) -> str:
    """Добавить тикер на все биржи (без bindings = любое совпадение имени).

    Если запись уже была с узкими bindings — снимаем ограничения (удаляем bindings).
    """
    del exchanges  # намеренно: «все» = без привязок к списку
    base_symbol = base_symbol.upper()
    item = await get_watchlist_item(session, user_id, base_symbol)
    if item is None:
        session.add(WatchlistItem(user_id=user_id, base_symbol=base_symbol))
        await session.commit()
        return "added"
    bindings = (
        await session.execute(
            select(WatchlistBinding).where(WatchlistBinding.watchlist_id == item.id)
        )
    ).scalars().all()
    if not bindings:
        return "already"
    for b in bindings:
        await session.delete(b)
    await session.commit()
    return "added"


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


async def remove_watchlist_with_user_data(
    session: AsyncSession, user_id: int, base_symbol: str
) -> bool:
    """Удаляет тикер из watchlist пользователя + cooldown и delivery по этому тикеру.
    Общие raw_ticks / signals не трогает (нужны другим пользователям).
    """
    result = await session.execute(
        select(WatchlistItem).where(
            WatchlistItem.user_id == user_id, WatchlistItem.base_symbol == base_symbol
        )
    )
    item = result.scalar_one_or_none()
    if not item:
        return False

    # cooldown по display и по локальным тикерам bindings
    alias_bases = {base_symbol}
    bindings = (
        await session.execute(
            select(WatchlistBinding).where(WatchlistBinding.watchlist_id == item.id)
        )
    ).scalars().all()
    for b in bindings:
        alias_bases.add(b.base_symbol.upper())

    await session.delete(item)

    await session.execute(
        delete(CooldownState).where(
            CooldownState.user_id == user_id,
            CooldownState.base_symbol.in_(sorted(alias_bases)),
        )
    )

    signal_ids = (
        await session.execute(
            select(Signal.id).where(Signal.base_symbol.in_(sorted(alias_bases)))
        )
    ).scalars().all()
    if signal_ids:
        await session.execute(
            delete(UserSignalDelivery).where(
                UserSignalDelivery.user_id == user_id,
                UserSignalDelivery.signal_id.in_(signal_ids),
            )
        )

    await session.commit()
    return True


async def exchanges_for_base(session: AsyncSession, base_symbol: str) -> list[str]:
    """Биржи, где инструмент уже известен (из instruments / последних тиков)."""
    result = await session.execute(
        select(Instrument.exchange)
        .where(Instrument.base_symbol == base_symbol, Instrument.is_active.is_(True))
        .distinct()
    )
    exchanges = sorted({x.lower() for x in result.scalars().all()})
    if exchanges:
        return [e.capitalize() for e in exchanges]
    # fallback: raw ticks
    result2 = await session.execute(
        select(RawTick.exchange)
        .where(RawTick.base_symbol == base_symbol)
        .distinct()
    )
    return [e.capitalize() for e in sorted({x.lower() for x in result2.scalars().all()})]


async def exchanges_map_for_bases(
    session: AsyncSession, bases: list[str]
) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {b: [] for b in bases}
    if not bases:
        return out
    result = await session.execute(
        select(Instrument.base_symbol, Instrument.exchange).where(
            Instrument.base_symbol.in_(bases),
            Instrument.is_active.is_(True),
        )
    )
    for base, exchange in result.all():
        out.setdefault(base, [])
        name = exchange.capitalize()
        if name not in out[base]:
            out[base].append(name)
    for b in out:
        out[b].sort()
    return out


async def list_watchlist(session: AsyncSession, user_id: int) -> list[str]:
    result = await session.execute(
        select(WatchlistItem.base_symbol)
        .where(WatchlistItem.user_id == user_id)
        .order_by(WatchlistItem.base_symbol)
    )
    return list(result.scalars().all())


async def list_watchlist_entries(
    session: AsyncSession, user_id: int
) -> list[tuple[str, datetime | None]]:
    """[(base_symbol, created_at), ...] по алфавиту тикера."""
    result = await session.execute(
        select(WatchlistItem.base_symbol, WatchlistItem.created_at)
        .where(WatchlistItem.user_id == user_id)
        .order_by(WatchlistItem.base_symbol)
    )
    return [(row[0], row[1]) for row in result.all()]


async def union_watchlist_symbols(session: AsyncSession) -> list[str]:
    """Все base_symbol для коллектора: display + локальные тикеры bindings."""
    result = await session.execute(select(WatchlistItem.base_symbol).distinct())
    bases = {x.upper() for x in result.scalars().all()}
    result2 = await session.execute(select(WatchlistBinding.base_symbol).distinct())
    bases.update(x.upper() for x in result2.scalars().all())
    return sorted(bases)


async def watchlist_bindings_map(
    session: AsyncSession,
) -> dict[tuple[str, str], str]:
    """(exchange, local_base) -> display_symbol для групп с bindings."""
    result = await session.execute(
        select(
            WatchlistBinding.exchange,
            WatchlistBinding.base_symbol,
            WatchlistItem.base_symbol,
        ).join(WatchlistItem, WatchlistItem.id == WatchlistBinding.watchlist_id)
    )
    out: dict[tuple[str, str], str] = {}
    for exchange, local, display in result.all():
        out[(exchange.lower(), local.upper())] = display.upper()
    return out


async def users_watching(session: AsyncSession, base_symbol: str) -> list[User]:
    result = await session.execute(
        select(User)
        .join(WatchlistItem)
        .where(WatchlistItem.base_symbol == base_symbol)
    )
    return list(result.scalars().unique().all())


async def users_watching_on_exchange(
    session: AsyncSession, base_symbol: str, exchange: str
) -> list[tuple[User, str]]:
    """Пользователи, следящие за тикером на бирже.

    Returns: [(user, display_symbol), ...]
    """
    base_symbol = base_symbol.upper()
    exchange = exchange.lower()
    seen: dict[int, tuple[User, str]] = {}

    # 1) прямой watchlist по display/base без bindings или с совпадающим binding
    items = (
        await session.execute(
            select(WatchlistItem).where(WatchlistItem.base_symbol == base_symbol)
        )
    ).scalars().all()
    for item in items:
        bindings = (
            await session.execute(
                select(WatchlistBinding).where(WatchlistBinding.watchlist_id == item.id)
            )
        ).scalars().all()
        if not bindings or any(
            b.exchange.lower() == exchange and b.base_symbol.upper() == base_symbol
            for b in bindings
        ):
            user = await session.get(User, item.user_id)
            if user:
                seen[user.id] = (user, item.base_symbol.upper())

    # 2) alias: binding (exchange, local ticker) -> display
    binds = (
        await session.execute(
            select(WatchlistBinding, WatchlistItem)
            .join(WatchlistItem, WatchlistItem.id == WatchlistBinding.watchlist_id)
            .where(
                WatchlistBinding.exchange == exchange,
                WatchlistBinding.base_symbol == base_symbol,
            )
        )
    ).all()
    for binding, item in binds:
        user = await session.get(User, item.user_id)
        if user:
            seen[user.id] = (user, item.base_symbol.upper())

    return list(seen.values())


async def resolve_report_legs(
    session: AsyncSession, user_id: int | None, ticker: str
) -> tuple[str, list[tuple[str, str]]]:
    """Для /otchet: (display, [(exchange, local_base), ...])."""
    ticker = ticker.upper()
    if user_id is not None:
        # группа пользователя, где ticker = display или один из bindings
        item = await get_watchlist_item(session, user_id, ticker)
        if item is None:
            bind_hit = (
                await session.execute(
                    select(WatchlistBinding, WatchlistItem)
                    .join(WatchlistItem, WatchlistItem.id == WatchlistBinding.watchlist_id)
                    .where(
                        WatchlistItem.user_id == user_id,
                        WatchlistBinding.base_symbol == ticker,
                    )
                )
            ).first()
            if bind_hit:
                _b, item = bind_hit
        if item is not None:
            bindings = (
                await session.execute(
                    select(WatchlistBinding).where(WatchlistBinding.watchlist_id == item.id)
                )
            ).scalars().all()
            if bindings:
                legs = [(b.exchange.lower(), b.base_symbol.upper()) for b in bindings]
                return item.base_symbol.upper(), legs

    # все активные инструменты с этим base
    result = await session.execute(
        select(Instrument.exchange, Instrument.base_symbol).where(
            Instrument.base_symbol == ticker, Instrument.is_active.is_(True)
        )
    )
    legs = sorted({(ex.lower(), base.upper()) for ex, base in result.all()})
    if not legs:
        # fallback по последним тикам
        result2 = await session.execute(
            select(RawTick.exchange, RawTick.base_symbol)
            .where(RawTick.base_symbol == ticker)
            .distinct()
        )
        legs = sorted({(ex.lower(), base.upper()) for ex, base in result2.all()})
    return ticker, legs


async def latest_tick_for_base(
    session: AsyncSession, exchange: str, base_symbol: str
) -> RawTick | None:
    result = await session.execute(
        select(RawTick)
        .where(RawTick.exchange == exchange, RawTick.base_symbol == base_symbol)
        .order_by(RawTick.ts_utc.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def tick_at_or_before_base(
    session: AsyncSession, exchange: str, base_symbol: str, ts: datetime
) -> RawTick | None:
    result = await session.execute(
        select(RawTick)
        .where(
            RawTick.exchange == exchange,
            RawTick.base_symbol == base_symbol,
            RawTick.ts_utc <= ts,
        )
        .order_by(RawTick.ts_utc.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def upsert_instrument(
    session: AsyncSession, *, commit: bool = True, **kwargs
) -> Instrument:
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
    if commit:
        await session.commit()
        await session.refresh(inst)
    else:
        await session.flush()
    return inst


async def insert_tick(session: AsyncSession, tick: dict, *, commit: bool = True) -> None:
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
    if commit:
        await session.commit()
    else:
        await session.flush()


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
