"""Bybit linear perpetual collector."""

from __future__ import annotations

import logging
from typing import Any

import aiohttp

from app.collectors.base import MarketSnapshot, now_utc
from app.config import NormalizationCfg
from app.core.normalize import base_from_contract, detect_multiplier, is_excluded_base

logger = logging.getLogger(__name__)


class BybitCollector:
    name = "bybit"

    def __init__(self, session: aiohttp.ClientSession, norm: NormalizationCfg):
        self.session = session
        self.norm = norm
        self._instruments: dict[str, dict] | None = None

    async def close(self) -> None:
        return

    async def _get(self, path: str, params: dict | None = None) -> Any:
        url = f"https://api.bybit.com{path}"
        async with self.session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=20)) as resp:
            resp.raise_for_status()
            data = await resp.json()
            if data.get("retCode") != 0:
                raise RuntimeError(data.get("retMsg", "bybit error"))
            return data.get("result", {})

    async def _ensure(self) -> dict[str, dict]:
        if self._instruments is None:
            result = await self._get(
                "/v5/market/instruments-info",
                {"category": "linear", "limit": 1000},
            )
            items = {}
            for row in result.get("list", []):
                if row.get("status") != "Trading":
                    continue
                if row.get("quoteCoin") not in ("USDT", "USDC"):
                    continue
                # Linear perpetuals (skip dated futures)
                ctype = str(row.get("contractType") or "")
                if ctype and ctype not in ("LinearPerpetual", "linearperpetual", ""):
                    if "Perpetual" not in ctype and "perpetual" not in ctype.lower():
                        continue
                items[row["symbol"]] = row
            self._instruments = items
        return self._instruments

    async def discover(self, bases: set[str]) -> list[MarketSnapshot]:
        info = await self._ensure()
        wanted = {b.upper() for b in bases}
        out: list[MarketSnapshot] = []
        for sym, meta in info.items():
            base = base_from_contract(sym, self.norm)
            if base not in wanted or is_excluded_base(base, self.norm, self.name):
                continue
            snap = await self.fetch_one(sym, base)
            if snap:
                snap.quote = meta.get("quoteCoin", "USDT")
                out.append(snap)
        return out

    async def fetch_one(self, contract_symbol: str, base_symbol: str) -> MarketSnapshot | None:
        try:
            tickers = await self._get(
                "/v5/market/tickers", {"category": "linear", "symbol": contract_symbol}
            )
            rows = tickers.get("list") or []
            if not rows:
                return None
            t = rows[0]
            last = float(t.get("lastPrice") or 0) or None
            mark = float(t.get("markPrice") or 0) or None
            funding = float(t.get("fundingRate") or 0)
            vol = float(t.get("turnover24h") or 0) or None
            oi_raw = float(t.get("openInterestValue") or 0) or None
            if oi_raw is None:
                oi_qty = float(t.get("openInterest") or 0)
                px = mark or last or 0
                mult = detect_multiplier(contract_symbol, self.norm)
                oi_raw = oi_qty * px * mult if px else None
            long_pct = short_pct = None
            try:
                ratio = await self._get(
                    "/v5/market/account-ratio",
                    {"category": "linear", "symbol": contract_symbol, "period": "5min", "limit": 1},
                )
                rlist = ratio.get("list") or []
                if rlist:
                    buy = float(rlist[0].get("buyRatio") or 0)
                    long_pct = buy * 100 if buy <= 1 else buy
                    short_pct = 100.0 - long_pct
            except Exception:  # noqa: BLE001
                pass
            return MarketSnapshot(
                exchange=self.name,
                contract_symbol=contract_symbol,
                base_symbol=base_symbol,
                quote="USDT" if contract_symbol.endswith("USDT") else "USDC",
                multiplier=detect_multiplier(contract_symbol, self.norm),
                ts_utc=now_utc(),
                oi_usd=oi_raw,
                last_price=last,
                mark_price=mark,
                funding_rate=funding,
                volume_24h=vol,
                long_pct=long_pct,
                short_pct=short_pct,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("bybit fetch %s: %s", contract_symbol, e)
            return None
