"""Binance USDT-M / USDC-M futures collector."""

from __future__ import annotations

import logging
from typing import Any

import aiohttp

from app.collectors.base import MarketSnapshot, now_utc
from app.config import NormalizationCfg
from app.core.normalize import base_from_contract, detect_multiplier, is_excluded_base

logger = logging.getLogger(__name__)


class BinanceCollector:
    name = "binance"

    def __init__(self, session: aiohttp.ClientSession, norm: NormalizationCfg):
        self.session = session
        self.norm = norm
        self._exchange_info: dict[str, Any] | None = None

    async def close(self) -> None:
        return

    async def _get(self, url: str, params: dict | None = None) -> Any:
        async with self.session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=20)) as resp:
            resp.raise_for_status()
            return await resp.json()

    async def _ensure_info(self) -> dict[str, Any]:
        if self._exchange_info is None:
            data = await self._get("https://fapi.binance.com/fapi/v1/exchangeInfo")
            self._exchange_info = {
                s["symbol"]: s
                for s in data.get("symbols", [])
                if s.get("contractType") == "PERPETUAL"
                and s.get("quoteAsset") in ("USDT", "USDC")
                and s.get("status") == "TRADING"
            }
        return self._exchange_info

    async def discover(self, bases: set[str]) -> list[MarketSnapshot]:
        info = await self._ensure_info()
        out: list[MarketSnapshot] = []
        wanted = {b.upper() for b in bases}
        for sym, meta in info.items():
            base = base_from_contract(sym, self.norm)
            if base not in wanted:
                continue
            if is_excluded_base(base, self.norm, self.name):
                continue
            snap = await self.fetch_one(sym, base)
            if snap:
                snap.quote = meta.get("quoteAsset", "USDT")
                out.append(snap)
        return out

    async def fetch_one(self, contract_symbol: str, base_symbol: str) -> MarketSnapshot | None:
        try:
            ticker, premium, oi, lsr = await self._bundle(contract_symbol)
        except Exception as e:  # noqa: BLE001
            logger.warning("binance fetch %s: %s", contract_symbol, e)
            return None
        last = float(ticker.get("lastPrice") or 0) or None
        mark = float(premium.get("markPrice") or 0) or None
        funding = float(premium.get("lastFundingRate") or 0)
        vol = float(ticker.get("quoteVolume") or 0) or None
        oi_contracts = float(oi.get("openInterest") or 0)
        px = mark or last or 0
        mult = detect_multiplier(contract_symbol, self.norm)
        oi_usd = oi_contracts * px * mult if px else None
        long_pct = short_pct = None
        if lsr:
            try:
                long_pct = float(lsr.get("longAccount") or lsr.get("longPosition") or 0) * 100
                short_pct = 100.0 - long_pct
            except (TypeError, ValueError):
                pass
        return MarketSnapshot(
            exchange=self.name,
            contract_symbol=contract_symbol,
            base_symbol=base_symbol,
            quote="USDT" if contract_symbol.endswith("USDT") else "USDC",
            multiplier=mult,
            ts_utc=now_utc(),
            oi_usd=oi_usd,
            last_price=last,
            mark_price=mark,
            funding_rate=funding,
            volume_24h=vol,
            long_pct=long_pct,
            short_pct=short_pct,
        )

    async def _bundle(self, symbol: str) -> tuple[dict, dict, dict, dict | None]:
        ticker = await self._get("https://fapi.binance.com/fapi/v1/ticker/24hr", {"symbol": symbol})
        premium = await self._get("https://fapi.binance.com/fapi/v1/premiumIndex", {"symbol": symbol})
        oi = await self._get("https://fapi.binance.com/fapi/v1/openInterest", {"symbol": symbol})
        lsr = None
        try:
            lsr = await self._get(
                "https://fapi.binance.com/futures/data/globalLongShortAccountRatio",
                {"symbol": symbol, "period": "5m", "limit": 1},
            )
            if isinstance(lsr, list) and lsr:
                lsr = lsr[0]
            else:
                lsr = None
        except Exception:  # noqa: BLE001
            lsr = None
        return ticker, premium, oi, lsr
