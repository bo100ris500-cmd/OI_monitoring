"""Collector base types."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol


@dataclass
class MarketSnapshot:
    exchange: str
    contract_symbol: str
    base_symbol: str
    quote: str
    multiplier: float
    ts_utc: datetime
    oi_usd: float | None
    last_price: float | None
    mark_price: float | None
    funding_rate: float | None  # raw fraction e.g. 0.0001
    volume_24h: float | None
    long_pct: float | None
    short_pct: float | None


class Collector(Protocol):
    name: str

    async def discover(self, bases: set[str]) -> list[MarketSnapshot]:
        """Return latest snapshots for contracts matching base symbols."""
        ...

    async def fetch_one(self, contract_symbol: str, base_symbol: str) -> MarketSnapshot | None:
        ...

    async def close(self) -> None:
        ...


def now_utc() -> datetime:
    return datetime.now(timezone.utc)
