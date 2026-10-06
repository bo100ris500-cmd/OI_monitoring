"""Lightweight smoke tests (no network / no bot token)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.config import AppConfig, ConfigStore, config_hash
from app.core.filters import apply_pre_filters, cooldown_allows
from app.core.metrics import WindowMetrics, compute_window_metrics, robust_z
from app.core.normalize import normalize_user_input, base_from_contract, parse_window_to_seconds
from app.core.signals import evaluate_signals
from app.storage.models import CooldownState, RawTick


def test_normalize():
    cfg = AppConfig().normalization
    assert normalize_user_input("btcusdt", cfg) == "BTC"
    assert normalize_user_input("BTC-USDT", cfg) == "BTC"
    assert normalize_user_input("$BTC", cfg) == "BTC"
    assert normalize_user_input("1000PEPEUSDT", cfg) == "PEPE"
    assert base_from_contract("1000PEPEUSDT", cfg) == "PEPE"
    assert parse_window_to_seconds("4h") == 14400


def test_robust_z():
    hist = [1, 2, 2, 2, 3, 10]
    z = robust_z(10, hist)
    assert z is not None and z > 0


def test_metrics_and_s1():
    cfg = AppConfig()
    now = datetime.now(timezone.utc)
    ticks = []
    oi = 1_000_000.0
    for i in range(200):
        ticks.append(
            RawTick(
                exchange="binance",
                contract_symbol="BTCUSDT",
                base_symbol="BTC",
                ts_utc=now - timedelta(minutes=200 - i),
                oi_usd=oi * (1 + i * 0.001),
                last_price=60000,
                mark_price=60000,
                funding_rate=0.0001,
                volume_24h=50_000_000,
                long_pct=55,
                short_pct=45,
            )
        )
    before = ticks[0]
    current = ticks[-1]
    # force big growth
    current.oi_usd = before.oi_usd * 1.2
    m = compute_window_metrics(current, before, ticks, "4h", cfg, "binance")
    assert m.oi_growth_pct is not None and m.oi_growth_pct > 15
    fired = evaluate_signals({"4h": m, "1h": m, "24h": m}, cfg)
    codes = {f.code for f in fired}
    assert "S1" in codes or "S2" in codes


def test_filters_f1():
    cfg = AppConfig()
    m = WindowMetrics(
        window="1h",
        oi_now=1000,
        oi_before=900,
        oi_growth_pct=10,
        oi_log_change=None,
        oi_z=None,
        oi_pctl=None,
        price_now=1.0,
        price_before=1.0,
        price_change_pct=0,
        price_z=None,
        gap=None,
        ratio=None,
        funding_8h=0.01,
        oi_to_volume=0.1,
        long_pct=50,
        short_pct=50,
        volume_24h=1000,
        history_days=10,
        new_history=False,
    )
    r = apply_pre_filters(
        cfg=cfg,
        base="BTC",
        exchange="binance",
        metrics=m,
        instrument=None,
        tick_age_sec=10,
        interval_sec=60,
    )
    assert r.allow is False and r.drop_reason and r.drop_reason.startswith("F1")


def test_cooldown():
    cfg = AppConfig()
    now = datetime.now(timezone.utc)
    cd = CooldownState(
        user_id=1,
        exchange="binance",
        base_symbol="BTC",
        signal_type="S1",
        last_oi_growth=20,
        last_sent_at=now,
    )
    assert cooldown_allows(cfg=cfg, cooldown=cd, signal_type="S1", oi_growth=21, now=now) is False
    assert cooldown_allows(cfg=cfg, cooldown=cd, signal_type="S1", oi_growth=35, now=now) is True


def test_config_hash(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text("collector:\n  interval_sec: 60\n", encoding="utf-8")
    store = ConfigStore(p)
    cfg = store.load()
    h = config_hash(cfg)
    assert len(h) == 16
    p.write_text("collector:\n  interval_sec: 120\n", encoding="utf-8")
    changed, msg = store.try_reload()
    assert changed is True


if __name__ == "__main__":
    test_normalize()
    test_robust_z()
    test_metrics_and_s1()
    test_filters_f1()
    test_cooldown()
    print("smoke ok")
