"""Hyperliquid perpetual collector (crypto only)."""

from __future__ import annotations

import logging
from typing import Any

import aiohttp

from app.collectors.base import MarketSnapshot, now_utc
from app.config import NormalizationCfg
from app.core.normalize import is_excluded_base

logger = logging.getLogger(__name__)


class HyperliquidCollector:
    name = "hyperliquid"

    def __init__(self, session: aiohttp.ClientSession, norm: NormalizationCfg):
        self.session = session
        self.norm = norm
        self._meta: list[dict] | None = None
        self._ctx: list[dict] | None = None

    async def close(self) -> None:
        return

    async def _post(self, payload: dict) -> Any:
        url = "https://api.hyperliquid.xyz/info"
        async with self.session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=25)) as resp:
            resp.raise_for_status()
            return await resp.json()

    async def _refresh(self) -> None:
        data = await self._post({"type": "metaAndAssetCtxs"})
        # [meta, ctxs]
        meta = data[0] if isinstance(data, list) else data
        ctxs = data[1] if isinstance(data, list) and len(data) > 1 else []
        self._meta = meta.get("universe", []) if isinstance(meta, dict) else []
        self._ctx = ctxs

    async def discover(self, bases: set[str]) -> list[MarketSnapshot]:
        await self._refresh()
        wanted = {b.upper() for b in bases}
        out: list[MarketSnapshot] = []
        assert self._meta is not None and self._ctx is not None
        for i, asset in enumerate(self._meta):
            name = (asset.get("name") or "").upper()
            if name not in wanted:
                continue
            if is_excluded_base(name, self.norm, self.name):
                continue
            ctx = self._ctx[i] if i < len(self._ctx) else {}
            snap = self._from_ctx(name, ctx)
            if snap:
                out.append(snap)
        return out

    async def fetch_one(self, contract_symbol: str, base_symbol: str) -> MarketSnapshot | None:
        await self._refresh()
        assert self._meta is not None and self._ctx is not None
        for i, asset in enumerate(self._meta):
            name = (asset.get("name") or "").upper()
            if name == base_symbol.upper() or name == contract_symbol.upper():
                ctx = self._ctx[i] if i < len(self._ctx) else {}
                return self._from_ctx(base_symbol.upper(), ctx)
        return None

    def _from_ctx(self, base: str, ctx: dict) -> MarketSnapshot | None:
        try:
            mark = float(ctx.get("markPx") or 0) or None
            mid = float(ctx.get("midPx") or 0) or None
            funding = float(ctx.get("funding") or 0)
            oi = float(ctx.get("openInterest") or 0)
            # OI on HL often in coins
            oi_usd = oi * (mark or mid or 0) if (mark or mid) else None
            day_ntl = float(ctx.get("dayNtlVlm") or 0) or None
            return MarketSnapshot(
                exchange=self.name,
                contract_symbol=base,
                base_symbol=base,
                quote="USDC",
                multiplier=1.0,
                ts_utc=now_utc(),
                oi_usd=oi_usd,
                last_price=mid or mark,
                mark_price=mark,
                funding_rate=funding,
                volume_24h=day_ntl,
                long_pct=None,
                short_pct=None,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("hyperliquid parse %s: %s", base, e)
            return None
