"""Factory for exchange collectors."""

from __future__ import annotations

import aiohttp

from app.collectors.aster import AsterCollector
from app.collectors.binance import BinanceCollector
from app.collectors.bitget import BitgetCollector
from app.collectors.bybit import BybitCollector
from app.collectors.hyperliquid import HyperliquidCollector
from app.config import AppConfig


def build_collectors(session: aiohttp.ClientSession, cfg: AppConfig) -> dict:
    mapping = {
        "binance": BinanceCollector,
        "bybit": BybitCollector,
        "bitget": BitgetCollector,
        "hyperliquid": HyperliquidCollector,
        "aster": AsterCollector,
    }
    out = {}
    for name in cfg.exchanges:
        cls = mapping.get(name.lower())
        if cls:
            out[name.lower()] = cls(session, cfg.normalization)
    return out
