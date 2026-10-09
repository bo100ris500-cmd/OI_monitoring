"""Symbol normalization (F7 / §8)."""

from __future__ import annotations

import re

from app.config import NormalizationCfg


def normalize_user_input(raw: str, cfg: NormalizationCfg) -> str:
    s = raw.strip().upper().replace(" ", "")
    s = s.replace("-", "").replace("_", "").replace("/", "")
    if s.startswith("$"):
        s = s[1:]
    # strip quote
    for q in sorted(cfg.strip_quote_suffixes, key=len, reverse=True):
        if s.endswith(q) and len(s) > len(q):
            s = s[: -len(q)]
            break
    # strip contract multipliers like 1000PEPE → PEPE
    # только числовые / 1M префиксы — НЕ одиночные буквы (иначе KGEN→GEN)
    for pref in sorted(cfg.strip_prefixes, key=len, reverse=True):
        p = pref.upper()
        if not (p.isdigit() or p == "1M"):
            continue
        if s.startswith(p) and len(s) > len(p) and s[len(p) :].isalpha():
            s = s[len(p) :]
            break
    # numeric+alpha like 1000000CHEEMS
    m = re.match(r"^(\d+)([A-Z].*)$", s)
    if m:
        s = m.group(2)
    return s


def detect_multiplier(contract_symbol: str, cfg: NormalizationCfg) -> float:
    s = contract_symbol.upper().replace("-", "").replace("_", "")
    for pref, mult in sorted(cfg.multiplier_prefixes.items(), key=lambda x: -len(x[0])):
        p = pref.upper()
        if s.startswith(p):
            rest = s[len(p) :]
            for q in cfg.strip_quote_suffixes:
                if rest.endswith(q):
                    return float(mult)
    m = re.match(r"^(\d+)[A-Z]", s)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            pass
    return 1.0


def base_from_contract(contract_symbol: str, cfg: NormalizationCfg) -> str:
    s = contract_symbol.upper().replace("-", "").replace("_", "").replace("/", "")
    for q in sorted(cfg.strip_quote_suffixes, key=len, reverse=True):
        if s.endswith(q):
            s = s[: -len(q)]
            break
    return normalize_user_input(s, cfg)


def is_excluded_base(base: str, cfg: NormalizationCfg, exchange: str | None = None) -> bool:
    b = base.upper()
    if b in {x.upper() for x in cfg.blacklist_bases}:
        return True
    if b in {x.upper() for x in cfg.index_bases}:
        return True
    if exchange and exchange.lower() == "hyperliquid":
        if b in {x.upper() for x in cfg.hyperliquid_tradfi_bases}:
            return True
    return False


def parse_window_to_seconds(window: str) -> int:
    w = window.strip().lower()
    if w.endswith("m"):
        return int(w[:-1]) * 60
    if w.endswith("h"):
        return int(w[:-1]) * 3600
    if w.endswith("d"):
        return int(w[:-1]) * 86400
    raise ValueError(f"Unknown window: {window}")
