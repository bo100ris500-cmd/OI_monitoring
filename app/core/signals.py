"""Signal engine S1–S7."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.config import AppConfig
from app.core.metrics import WindowMetrics


SIGNAL_NAMES = {
    "S1": "Всплеск OI",
    "S2": "Режим OI + цена",
    "S3": "Расхождение OI и цены",
    "S4": "Перекос толпы",
    "S5": "Капитуляция",
    "S6": "Перегрев плечом",
    "S7": "Кросс-биржевое подтверждение",
}


@dataclass
class FiredSignal:
    code: str
    name: str
    window: str
    details: dict[str, Any] = field(default_factory=dict)


def s2_regime(m: WindowMetrics, cfg: AppConfig) -> str | None:
    if m.oi_growth_pct is None or m.price_change_pct is None:
        return None
    oi_dz = cfg.signals.S2.oi_deadzone_pct
    p_dz = cfg.signals.S2.price_deadzone_pct
    oi = m.oi_growth_pct
    pr = m.price_change_pct
    if abs(oi) < oi_dz or abs(pr) < p_dz:
        return None
    if oi > 0 and pr > 0:
        return "OI ↑ + Price ↑ → Новые лонги"
    if oi > 0 and pr < 0:
        return "OI ↑ + Price ↓ → Новые шорты"
    if oi < 0 and pr > 0:
        return "OI ↓ + Price ↑ → Закрытие шортов"
    if oi < 0 and pr < 0:
        return "OI ↓ + Price ↓ → Закрытие лонгов"
    return None


def evaluate_signals(
    metrics_by_window: dict[str, WindowMetrics],
    cfg: AppConfig,
    *,
    had_s1_last_24h: bool = False,
    oi_drop_1h_pct: float | None = None,
) -> list[FiredSignal]:
    fired: list[FiredSignal] = []
    s1_cfg = cfg.signals.S1
    s1_hit_windows: list[str] = []

    for window, m in metrics_by_window.items():
        # S1
        if m.oi_growth_pct is not None and m.oi_growth_pct >= s1_cfg.oi_growth_pct:
            if m.new_history:
                fire_s1 = True  # F2: без z/pctl — только рост
            else:
                pctl_ok = m.oi_pctl is not None and m.oi_pctl >= s1_cfg.oi_percentile
                z_ok = m.oi_z is not None and m.oi_z >= s1_cfg.oi_z
                fire_s1 = pctl_ok or z_ok
            if fire_s1:
                s1_hit_windows.append(window)
                fired.append(
                    FiredSignal(
                        "S1",
                        SIGNAL_NAMES["S1"],
                        window,
                        {
                            "oi_growth_pct": m.oi_growth_pct,
                            "oi_z": m.oi_z,
                            "oi_pctl": m.oi_pctl,
                            "new_history": m.new_history,
                        },
                    )
                )

        # S2
        regime = s2_regime(m, cfg)
        if regime:
            fired.append(FiredSignal("S2", SIGNAL_NAMES["S2"], window, {"regime": regime}))

        # S3 requires S1 on same window
        s1_here = window in s1_hit_windows
        if s1_here:
            s3 = cfg.signals.S3
            ratio_hit = m.ratio is not None and m.ratio < s3.ratio_max
            z_gap = None
            if m.oi_z is not None and m.price_z is not None:
                z_gap = m.oi_z - m.price_z
            z_hit = z_gap is not None and z_gap >= s3.z_gap_min
            if ratio_hit or z_hit:
                fired.append(
                    FiredSignal(
                        "S3",
                        SIGNAL_NAMES["S3"],
                        window,
                        {"ratio": m.ratio, "z_gap": z_gap, "label": "цена отстаёт от OI"},
                    )
                )

        # S4
        s4 = cfg.signals.S4
        if m.long_pct is not None and m.funding_8h is not None:
            if m.long_pct >= s4.long_pct_min and m.funding_8h >= s4.funding_long_min_8h:
                fired.append(
                    FiredSignal(
                        "S4",
                        SIGNAL_NAMES["S4"],
                        window,
                        {"side": "long", "long_pct": m.long_pct, "funding_8h": m.funding_8h},
                    )
                )
            if m.long_pct <= s4.short_pct_max and m.funding_8h <= s4.funding_short_max_8h:
                fired.append(
                    FiredSignal(
                        "S4",
                        SIGNAL_NAMES["S4"],
                        window,
                        {"side": "short", "long_pct": m.long_pct, "funding_8h": m.funding_8h},
                    )
                )

        # S6
        s6 = cfg.signals.S6
        if m.oi_volume_pctl is not None and m.oi_volume_pctl >= s6.oi_volume_percentile:
            fired.append(
                FiredSignal(
                    "S6",
                    SIGNAL_NAMES["S6"],
                    window,
                    {"oi_to_volume": m.oi_to_volume, "oi_volume_pctl": m.oi_volume_pctl},
                )
            )

    # S5 — once if conditions met (use 1h metrics)
    s5 = cfg.signals.S5
    m1h = metrics_by_window.get("1h")
    if had_s1_last_24h and oi_drop_1h_pct is not None and m1h is not None:
        if oi_drop_1h_pct <= s5.oi_drop_1h_pct and (
            m1h.price_change_pct is not None and m1h.price_change_pct < 0
        ):
            fired.append(
                FiredSignal(
                    "S5",
                    SIGNAL_NAMES["S5"],
                    "1h",
                    {"oi_drop_1h_pct": oi_drop_1h_pct, "price_change_pct": m1h.price_change_pct},
                )
            )

    # Deduplicate same code+window keeping first
    seen = set()
    unique: list[FiredSignal] = []
    for f in fired:
        key = (f.code, f.window, str(f.details.get("side", "")))
        if key in seen:
            continue
        seen.add(key)
        unique.append(f)
    return unique
