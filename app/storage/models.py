"""SQLAlchemy models."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)

    watchlist: Mapped[list[WatchlistItem]] = relationship(back_populates="user")


class WatchlistItem(Base):
    __tablename__ = "watchlist"
    __table_args__ = (UniqueConstraint("user_id", "base_symbol", name="uq_user_symbol"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    base_symbol: Mapped[str] = mapped_column(String(64), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    user: Mapped[User] = relationship(back_populates="watchlist")


class Instrument(Base):
    __tablename__ = "instruments"
    __table_args__ = (UniqueConstraint("exchange", "contract_symbol", name="uq_ex_contract"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    exchange: Mapped[str] = mapped_column(String(32), index=True)
    contract_symbol: Mapped[str] = mapped_column(String(64))
    base_symbol: Mapped[str] = mapped_column(String(64), index=True)
    quote: Mapped[str] = mapped_column(String(16), default="USDT")
    multiplier: Mapped[float] = mapped_column(Float, default=1.0)
    listed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class RawTick(Base):
    __tablename__ = "raw_ticks"
    __table_args__ = (
        UniqueConstraint("exchange", "contract_symbol", "ts_utc", name="uq_tick"),
        Index("ix_ticks_lookup", "exchange", "contract_symbol", "ts_utc"),
        Index("ix_ticks_base_ts", "base_symbol", "ts_utc"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    exchange: Mapped[str] = mapped_column(String(32))
    contract_symbol: Mapped[str] = mapped_column(String(64))
    base_symbol: Mapped[str] = mapped_column(String(64))
    ts_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    oi_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    last_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    mark_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    funding_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    volume_24h: Mapped[float | None] = mapped_column(Float, nullable=True)
    long_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    short_pct: Mapped[float | None] = mapped_column(Float, nullable=True)


class Signal(Base):
    __tablename__ = "signals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    signal_uid: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    base_symbol: Mapped[str] = mapped_column(String(64), index=True)
    exchange: Mapped[str] = mapped_column(String(32))
    window: Mapped[str] = mapped_column(String(16))
    types_json: Mapped[str] = mapped_column(Text)  # JSON list of signal codes
    payload_json: Mapped[str] = mapped_column(Text)
    ts_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    s7_status: Mapped[str] = mapped_column(String(32), default="local")  # local|confirmed
    flags_json: Mapped[str] = mapped_column(Text, default="{}")
    # post-factum
    price_at_signal: Mapped[float | None] = mapped_column(Float, nullable=True)
    price_after_5m: Mapped[float | None] = mapped_column(Float, nullable=True)
    price_after_15m: Mapped[float | None] = mapped_column(Float, nullable=True)
    price_after_1h: Mapped[float | None] = mapped_column(Float, nullable=True)
    price_after_4h: Mapped[float | None] = mapped_column(Float, nullable=True)
    price_after_1d: Mapped[float | None] = mapped_column(Float, nullable=True)
    return_5m_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    return_15m_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    return_1h_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    return_4h_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    return_1d_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    post_factum_done: Mapped[bool] = mapped_column(Boolean, default=False)


class UserSignalDelivery(Base):
    __tablename__ = "user_signal_delivery"
    __table_args__ = (UniqueConstraint("user_id", "signal_id", name="uq_delivery"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    signal_id: Mapped[int] = mapped_column(ForeignKey("signals.id", ondelete="CASCADE"))
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CooldownState(Base):
    __tablename__ = "cooldown_state"
    __table_args__ = (
        UniqueConstraint("user_id", "exchange", "base_symbol", "signal_type", name="uq_cd"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    exchange: Mapped[str] = mapped_column(String(32))
    base_symbol: Mapped[str] = mapped_column(String(64))
    signal_type: Mapped[str] = mapped_column(String(8))
    last_oi_growth: Mapped[float] = mapped_column(Float, default=0)
    last_sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ConfigMeta(Base):
    __tablename__ = "config_meta"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    config_hash: Mapped[str] = mapped_column(String(64))
    applied_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ExchangeHealth(Base):
    __tablename__ = "exchange_health"

    exchange: Mapped[str] = mapped_column(String(32), primary_key=True)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    alerted: Mapped[bool] = mapped_column(Boolean, default=False)
