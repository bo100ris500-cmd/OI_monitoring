"""Bitget USDT/USDC perpetual collector."""

from __future__ import annotations

import logging
from typing import Any

import aiohttp

from app.collectors.base import MarketSnapshot, now_utc
from app.config import NormalizationCfg
from app.core.normalize import base_from_contract, detect_multiplier, is_excluded_base

logger = logging.getLogger(__name__)


class BitgetCollector:
    name = "bitget"

    def __init__(self, session: aiohttp.ClientSession, norm: NormalizationCfg):
        self.session = session
        self.norm = norm
        self._contracts: dict[str, dict] | None = None

    async def close(self) -> None:
        return

    async def _get(self, path: str, params: dict | None = None) -> Any:
        url = f"https://api.bitget.com{path}"
        async with self.session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=20)) as resp:
            resp.raise_for_status()
            data = await resp.json()
            if str(data.get("code")) not in ("00000", "0"):
                raise RuntimeError(data.get("msg", "bitget error"))
            return data.get("data")

    async def _ensure(self) -> dict[str, dict]:
        if self._contracts is None:
            data = await self._get("/api/v2/mix/market/contracts", {"productType": "USDT-FUTURES"})
            items = {}
            for row in data or []:
                # symbol like BTCUSDT
                sym = row.get("symbol") or ""
                if row.get("symbolStatus") not in (None, "normal", "listed"):
                    continue
                items[sym] = row
            # also USDC
            try:
                data2 = await self._get("/api/v2/mix/market/contracts", {"productType": "USDC-FUTURES"})
                for row in data2 or []:
                    sym = row.get("symbol") or ""
                    items[sym] = row
            except Exception:  # noqa: BLE001
                pass
            self._contracts = items
        return self._contracts

    def _product_type(self, symbol: str) -> str:
        return "USDC-FUTURES" if "USDC" in symbol.upper() else "USDT-FUTURES"

    async def discover(self, bases: set[str]) -> list[MarketSnapshot]:
        info = await self._ensure()
        wanted = {b.upper() for b in bases}
        out: list[MarketSnapshot] = []
        for sym in info:
            base = base_from_contract(sym, self.norm)
            if base not in wanted or is_excluded_base(base, self.norm, self.name):
                continue
            snap = await self.fetch_one(sym, base)
            if snap:
                out.append(snap)
        return out

    async def fetch_one(self, contract_symbol: str, base_symbol: str) -> MarketSnapshot | None:
        try:
            pt = self._product_type(contract_symbol)
            tickers = await self._get(
                "/api/v2/mix/market/ticker",
                {"productType": pt, "symbol": contract_symbol},
            )
            t = tickers[0] if isinstance(tickers, list) and tickers else tickers
            if not t:
                return None
            last = float(t.get("lastPr") or t.get("last") or 0) or None
            mark = float(t.get("markPrice") or 0) or None
            funding = float(t.get("fundingRate") or 0)
            vol = float(t.get("quoteVolume") or t.get("usdtVolume") or 0) or None
            oi_usd = float(t.get("holdingAmount") or 0) or None
            # holdingAmount may be contracts — prefer openInterestUsd if present
            if t.get("openInterestUsd"):
                oi_usd = float(t["openInterestUsd"])
            elif oi_usd and (mark or last):
                oi_usd = oi_usd * (mark or last) * detect_multiplier(contract_symbol, self.norm)
            return MarketSnapshot(
                exchange=self.name,
                contract_symbol=contract_symbol,
                base_symbol=base_symbol,
                quote="USDC" if "USDC" in contract_symbol.upper() else "USDT",
                multiplier=detect_multiplier(contract_symbol, self.norm),
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
            logger.warning("bitget fetch %s: %s", contract_symbol, e)
            return None
