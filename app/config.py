"""YAML config schema with hot-reload support."""

from __future__ import annotations

import hashlib
import logging
from copy import deepcopy
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, field_validator

logger = logging.getLogger(__name__)


class CollectorCfg(BaseModel):
    interval_sec: int = 60


class HistoryCfg(BaseModel):
    zscore_days: int = 14
    minimum_days: int = 7


class ResourcesCfg(BaseModel):
    max_memory_mb: int = 700
    max_concurrent_requests: int = 10


class NormalizationCfg(BaseModel):
    strip_prefixes: list[str] = Field(default_factory=lambda: ["1000", "10000", "1000000", "1M", "k"])
    strip_quote_suffixes: list[str] = Field(default_factory=lambda: ["USDT", "USDC"])
    multiplier_prefixes: dict[str, float] = Field(
        default_factory=lambda: {"1000": 1000, "10000": 10000, "1000000": 1000000, "1M": 1000000}
    )
    blacklist_bases: list[str] = Field(
        default_factory=lambda: ["USDT", "USDC", "BUSD", "DAI", "TUSD", "FDUSD", "USD"]
    )
    wrapped_prefixes: list[str] = Field(default_factory=lambda: ["W"])
    index_bases: list[str] = Field(default_factory=lambda: ["DEFI", "ALL"])
    hyperliquid_tradfi_bases: list[str] = Field(
        default_factory=lambda: ["EUR", "GBP", "JPY", "GOLD", "SILVER", "OIL", "NDX", "SPX"]
    )


class S1Cfg(BaseModel):
    oi_growth_pct: float = 15
    oi_percentile: float = 97
    oi_z: float = 3


class S2Cfg(BaseModel):
    oi_deadzone_pct: float = 2
    price_deadzone_pct: float = 0.5


class S3Cfg(BaseModel):
    ratio_max: float = 0.5
    z_gap_min: float = 2


class S4Cfg(BaseModel):
    long_pct_min: float = 65
    short_pct_max: float = 35
    funding_long_min_8h: float = 0.05
    funding_short_max_8h: float = -0.05


class S5Cfg(BaseModel):
    lookback_hours: int = 24
    oi_drop_1h_pct: float = -10


class S6Cfg(BaseModel):
    oi_volume_percentile: float = 95


class S7Cfg(BaseModel):
    exchanges_required: int = 2
    confirmation_window_min: int = 5


class SignalsCfg(BaseModel):
    S1: S1Cfg = Field(default_factory=S1Cfg)
    S2: S2Cfg = Field(default_factory=S2Cfg)
    S3: S3Cfg = Field(default_factory=S3Cfg)
    S4: S4Cfg = Field(default_factory=S4Cfg)
    S5: S5Cfg = Field(default_factory=S5Cfg)
    S6: S6Cfg = Field(default_factory=S6Cfg)
    S7: S7Cfg = Field(default_factory=S7Cfg)


class F1Cfg(BaseModel):
    min_oi_usd: float = 500_000
    min_volume_24h_usd: float = 5_000_000


class F2Cfg(BaseModel):
    minimum_history_days: int = 7


class F3Cfg(BaseModel):
    new_listing_days: int = 7
    action: Literal["mark", "exclude"] = "mark"


class F5Cfg(BaseModel):
    stale_intervals: int = 2
    max_oi_jump_ratio: float = 10
    confirmation_reads: int = 2


class F6Cfg(BaseModel):
    cooldown_hours: int = 4
    extra_growth_pct_points: float = 10


class F8Cfg(BaseModel):
    btc_move_pct: float = 3
    market_signal_pct: float = 20


class FiltersCfg(BaseModel):
    F1: F1Cfg = Field(default_factory=F1Cfg)
    F2: F2Cfg = Field(default_factory=F2Cfg)
    F3: F3Cfg = Field(default_factory=F3Cfg)
    F5: F5Cfg = Field(default_factory=F5Cfg)
    F6: F6Cfg = Field(default_factory=F6Cfg)
    F8: F8Cfg = Field(default_factory=F8Cfg)


class PostFactumCfg(BaseModel):
    horizons: list[str] = Field(default_factory=lambda: ["5m", "15m", "1h", "4h", "1d"])


class MonitoringCfg(BaseModel):
    missing_data_intervals: int = 3


class AppConfig(BaseModel):
    collector: CollectorCfg = Field(default_factory=CollectorCfg)
    windows: list[str] = Field(default_factory=lambda: ["1h", "4h", "24h"])
    history: HistoryCfg = Field(default_factory=HistoryCfg)
    resources: ResourcesCfg = Field(default_factory=ResourcesCfg)
    monitoring_mode: Literal["watchlist", "global"] = "watchlist"
    exchanges: list[str] = Field(
        default_factory=lambda: ["binance", "bybit", "bitget", "hyperliquid", "aster"]
    )
    normalization: NormalizationCfg = Field(default_factory=NormalizationCfg)
    signals: SignalsCfg = Field(default_factory=SignalsCfg)
    filters: FiltersCfg = Field(default_factory=FiltersCfg)
    post_factum: PostFactumCfg = Field(default_factory=PostFactumCfg)
    monitoring: MonitoringCfg = Field(default_factory=MonitoringCfg)

    @field_validator("windows")
    @classmethod
    def windows_nonempty(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("windows must not be empty")
        return v


def config_hash(raw: dict[str, Any] | AppConfig) -> str:
    if isinstance(raw, AppConfig):
        payload = raw.model_dump_json(indent=None)
    else:
        payload = yaml.safe_dump(raw, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


class ConfigStore:
    """Thread-safe-ish config holder with hot reload."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._config: AppConfig | None = None
        self._hash: str = ""
        self._mtime: float | None = None

    @property
    def config(self) -> AppConfig:
        if self._config is None:
            self.load(force=True)
        assert self._config is not None
        return self._config

    @property
    def hash(self) -> str:
        return self._hash

    def load(self, force: bool = False) -> AppConfig:
        if not self.path.exists():
            example = self.path.with_name("config.example.yaml")
            if example.exists() and not self.path.exists():
                self.path.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
            elif not self.path.exists():
                raise FileNotFoundError(f"Config not found: {self.path}")

        mtime = self.path.stat().st_mtime
        if not force and self._config is not None and self._mtime == mtime:
            return self._config

        with self.path.open(encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        cfg = AppConfig.model_validate(raw)
        self._config = cfg
        self._hash = config_hash(cfg)
        self._mtime = mtime
        logger.info("Config loaded hash=%s mode=%s", self._hash, cfg.monitoring_mode)
        return cfg

    def try_reload(self) -> tuple[bool, str]:
        """Returns (changed_or_ok, message). Keeps old config on validation error."""
        if not self.path.exists():
            return False, "config file missing"
        mtime = self.path.stat().st_mtime
        if self._mtime is not None and mtime == self._mtime:
            return False, "unchanged"
        try:
            with self.path.open(encoding="utf-8") as f:
                raw = yaml.safe_load(f) or {}
            cfg = AppConfig.model_validate(raw)
            old = self._config
            self._config = cfg
            self._hash = config_hash(cfg)
            self._mtime = mtime
            return True, f"reloaded hash={self._hash} (was {config_hash(old) if old else 'none'})"
        except Exception as e:  # noqa: BLE001
            logger.error("Config reload failed, keeping previous: %s", e)
            return False, f"invalid config, kept previous: {e}"

    def snapshot(self) -> AppConfig:
        return deepcopy(self.config)
