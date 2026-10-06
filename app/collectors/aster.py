"""Aster perpetual collector (Binance-compatible public API where available)."""

from __future__ import annotations

import logging
from typing import Any

import aiohttp

from app.collectors.base import MarketSnapshot, now_utc
from app.config import NormalizationCfg
from app.core.normalize import base_from_contract, detect_multiplier, is_excluded_base

logger = logging.getLogger(__name__)

# Aster docs evolve; try fapi-compatible endpoints first, fallback to bybit-like.
ASTER_BASES = [
    "https://fapi.aster.finance",
    "https://api.aster.finance",
]


class AsterCollector:
    name = "aster"

    def __init__(self, session: aiohttp.ClientSession, norm: NormalizationCfg):
        self.session = session
        self.norm = norm
        self._base: str | None = None
        self._symbols: set[str] = set()

    async def close(self) -> None:
        return

    async def _try_get(self, base: str, path: str, params: dict | None = None) -> Any | None:
        url = f"{base}{path}"
        try:
            async with self.session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status != 200:
                    return None
                return await resp.json()
        except Exception:  # noqa: BLE001
            return None

    async def _ensure_base(self) -> str | None:
        if self._base:
            return self._base
        for b in ASTER_BASES:
            data = await self._try_get(b, "/fapi/v1/exchangeInfo")
            if isinstance(data, dict) and data.get("symbols"):
                self._base = b
                self._symbols = {
                    s["symbol"]
                    for s in data["symbols"]
                    if s.get("contractType", "PERPETUAL") in ("PERPETUAL", None)
                    and s.get("quoteAsset", "USDT") in ("USDT", "USDC")
                }
                return b
            # alternate: ticker list
            data2 = await self._try_get(b, "/fapi/v1/ticker/24hr")
            if isinstance(data2, list) and data2:
                self._base = b
                self._symbols = {x.get("symbol") for x in data2 if x.get("symbol")}
                return b
        logger.warning("aster: no reachable API base")
        return None

    async def discover(self, bases: set[str]) -> list[MarketSnapshot]:
        base_url = await self._ensure_base()
        if not base_url:
            return []
        wanted = {b.upper() for b in bases}
        out: list[MarketSnapshot] = []
        for sym in self._symbols:
            if not sym:
                continue
            b = base_from_contract(sym, self.norm)
            if b not in wanted or is_excluded_base(b, self.norm, self.name):
                continue
            snap = await self.fetch_one(sym, b)
            if snap:
                out.append(snap)
        return out

    async def fetch_one(self, contract_symbol: str, base_symbol: str) -> MarketSnapshot | None:
        base_url = await self._ensure_base()
        if not base_url:
            return None
        try:
            ticker = await self._try_get(base_url, "/fapi/v1/ticker/24hr", {"symbol": contract_symbol})
            premium = await self._try_get(base_url, "/fapi/v1/premiumIndex", {"symbol": contract_symbol})
            oi = await self._try_get(base_url, "/fapi/v1/openInterest", {"symbol": contract_symbol})
            if not ticker:
                return None
            if isinstance(ticker, list):
                ticker = next((x for x in ticker if x.get("symbol") == contract_symbol), None)
            if not ticker:
                return None
            last = float(ticker.get("lastPrice") or 0) or None
            mark = float((premium or {}).get("markPrice") or 0) or None
            funding = float((premium or {}).get("lastFundingRate") or 0)
            vol = float(ticker.get("quoteVolume") or 0) or None
            oi_qty = float((oi or {}).get("openInterest") or 0)
            px = mark or last or 0
            mult = detect_multiplier(contract_symbol, self.norm)
            oi_usd = oi_qty * px * mult if px else None
            return MarketSnapshot(
                exchange=self.name,
                contract_symbol=contract_symbol,
                base_symbol=base_symbol,
                quote="USDT",
                multiplier=mult,
                ts_utc=now_utc(),
                oi_usd=oi_usd,
                last_price=last,
                mark_price=mark,
                funding_rate=funding,
                volume_24h=vol,
                long_pct=None,
                short_pct=None,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("aster fetch %s: %s", contract_symbol, e)
            return None
