#!/usr/bin/env python3
"""
Обогащение result.csv ценами с биржи через 5м / 15м / 1ч / 4ч / 1д
после времени сообщения (msg_unixtime, UTC).

Binance Futures USDT-M: /fapi/v1/klines
Bybit linear:           /v5/market/kline
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

OFFSETS = [
    ("price_5m_usd", 5 * 60),
    ("price_15m_usd", 15 * 60),
    ("price_1h_usd", 60 * 60),
    ("price_4h_usd", 4 * 60 * 60),
    ("price_1d_usd", 24 * 60 * 60),
]

PRICE_COLUMNS = [name for name, _ in OFFSETS]
MINUTE_MS = 60_000
CHUNK = 1000
MAX_WORKERS = 3
REQUEST_PAUSE = 0.08

CACHE_LOCK = threading.Lock()


def to_contract(symbol: str) -> str:
    s = symbol.strip().upper()
    return s if s.endswith("USDT") else f"{s}USDT"


def floor_minute_ms(ts_ms: int) -> int:
    return ts_ms - (ts_ms % MINUTE_MS)


def http_get_json(url: str, retries: int = 4):
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "oi-analictick/1.0"})
            with urllib.request.urlopen(req, timeout=25) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code in (418, 429, 503):
                time.sleep(1.2 * (attempt + 1))
                continue
            if e.code == 400:
                return None
            time.sleep(0.6 * (attempt + 1))
        except Exception as e:  # noqa: BLE001
            last_err = e
            time.sleep(0.5 * (attempt + 1))
    print(f"GET failed: {url[:120]}... ({last_err})", flush=True)
    return None


def fetch_binance_klines(symbol: str, start_ms: int, limit: int = CHUNK) -> list[tuple[int, float]]:
    end_ms = start_ms + limit * MINUTE_MS
    params = urllib.parse.urlencode(
        {
            "symbol": symbol,
            "interval": "1m",
            "startTime": start_ms,
            "endTime": end_ms,
            "limit": limit,
        }
    )
    data = http_get_json(f"https://fapi.binance.com/fapi/v1/klines?{params}")
    if not data:
        return []
    return [(int(row[0]), float(row[4])) for row in data]


def fetch_bybit_klines(symbol: str, start_ms: int, limit: int = CHUNK) -> list[tuple[int, float]]:
    end_ms = start_ms + limit * MINUTE_MS
    params = urllib.parse.urlencode(
        {
            "category": "linear",
            "symbol": symbol,
            "interval": "1",
            "start": start_ms,
            "end": end_ms,
            "limit": limit,
        }
    )
    data = http_get_json(f"https://api.bybit.com/v5/market/kline?{params}")
    if not isinstance(data, dict) or data.get("retCode") != 0:
        return []
    rows = data.get("result", {}).get("list") or []
    out = [(int(row[0]), float(row[4])) for row in rows]
    out.sort(key=lambda x: x[0])
    # только свечи в запрошенном окне
    return [(ot, px) for ot, px in out if start_ms <= ot < end_ms]


def fetch_one_minute(exchange: str, symbol: str, minute_ms: int) -> float | None:
    """Цена close на минуте minute_ms; если свечи нет — ближайшая в следующие 5 мин."""
    time.sleep(REQUEST_PAUSE)
    look_ahead = 5 * MINUTE_MS
    if exchange.lower() == "binance":
        params = urllib.parse.urlencode(
            {
                "symbol": symbol,
                "interval": "1m",
                "startTime": minute_ms,
                "endTime": minute_ms + look_ahead,
                "limit": 5,
            }
        )
        data = http_get_json(f"https://fapi.binance.com/fapi/v1/klines?{params}")
        if not data:
            return None
        for row in data:
            if int(row[0]) >= minute_ms:
                return float(row[4])
        return None

    if exchange.lower() == "bybit":
        params = urllib.parse.urlencode(
            {
                "category": "linear",
                "symbol": symbol,
                "interval": "1",
                "start": minute_ms,
                "end": minute_ms + look_ahead,
                "limit": 5,
            }
        )
        data = http_get_json(f"https://api.bybit.com/v5/market/kline?{params}")
        if not isinstance(data, dict) or data.get("retCode") != 0:
            return None
        rows = data.get("result", {}).get("list") or []
        if not rows:
            return None
        rows = sorted(rows, key=lambda r: int(r[0]))
        for row in rows:
            if int(row[0]) >= minute_ms:
                return float(row[4])
        return None

    return None


def nearest_price(got: dict[int, float], minute_ms: int, max_ahead_ms: int = 5 * MINUTE_MS) -> float | None:
    if minute_ms in got:
        return got[minute_ms]
    for ot in range(minute_ms, minute_ms + max_ahead_ms + 1, MINUTE_MS):
        if ot in got:
            return got[ot]
    return None


def fetch_chunk(exchange: str, symbol: str, start_ms: int) -> list[tuple[int, float]]:
    time.sleep(REQUEST_PAUSE)
    if exchange.lower() == "binance":
        return fetch_binance_klines(symbol, start_ms)
    if exchange.lower() == "bybit":
        return fetch_bybit_klines(symbol, start_ms)
    return []


def cache_key(exchange: str, symbol: str, minute_ms: int) -> str:
    return f"{exchange}|{symbol}|{minute_ms}"


def load_cache(path: Path) -> dict:
    if path.exists():
        with path.open(encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_cache(path: Path, cache: dict) -> None:
    with CACHE_LOCK:
        snapshot = dict(cache)
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(snapshot, f)
    tmp.replace(path)


def ensure_minutes(
    exchange: str,
    contract: str,
    minutes_ms: list[int],
    cache: dict,
) -> None:
    with CACHE_LOCK:
        missing = sorted({m for m in minutes_ms if cache_key(exchange, contract, m) not in cache})
    if not missing:
        return

    # Если точки редкие (разрыв > 2ч) — по одной минуте; иначе чанками
    GAP_CHUNK = 120 * MINUTE_MS

    i = 0
    while i < len(missing):
        start = missing[i]
        # смотрим плотный кластер от start
        j = i + 1
        while j < len(missing) and missing[j] - missing[j - 1] <= GAP_CHUNK:
            j += 1
        cluster = missing[i:j]

        if len(cluster) == 1 or (cluster[-1] - cluster[0]) > (CHUNK * MINUTE_MS):
            # точечные запросы
            for m in cluster:
                ck = cache_key(exchange, contract, m)
                with CACHE_LOCK:
                    if ck in cache:
                        continue
                px = fetch_one_minute(exchange, contract, m)
                with CACHE_LOCK:
                    cache[ck] = px
            i = j
            continue

        # плотный кластер — чанками от start
        cursor = start
        end_cluster = cluster[-1]
        safety = 0
        while cursor <= end_cluster and safety < 50:
            safety += 1
            candles = fetch_chunk(exchange, contract, cursor)
            with CACHE_LOCK:
                if not candles:
                    window_end = cursor + CHUNK * MINUTE_MS
                    for m in cluster:
                        if cursor <= m < window_end:
                            ck = cache_key(exchange, contract, m)
                            if ck not in cache:
                                cache[ck] = None
                    cursor = window_end
                    continue
                got = {ot: px for ot, px in candles}
                last = candles[-1][0]
                if last < cursor:
                    for m in cluster:
                        if m == cursor:
                            cache[cache_key(exchange, contract, m)] = None
                    cursor += MINUTE_MS
                    continue
                for m in cluster:
                    if cursor <= m <= last + 5 * MINUTE_MS:
                        ck = cache_key(exchange, contract, m)
                        if ck not in cache:
                            cache[ck] = nearest_price(got, m)
                cursor = last + MINUTE_MS

            with CACHE_LOCK:
                if all(cache_key(exchange, contract, m) in cache for m in cluster):
                    break
        i = j


def prices_for_targets(
    exchange: str,
    raw_symbol: str,
    targets_sec: list[int],
    cache: dict,
) -> dict[int, float | None]:
    contract = to_contract(raw_symbol)
    minutes = [floor_minute_ms(t * 1000) for t in targets_sec]
    ensure_minutes(exchange, contract, minutes, cache)
    out: dict[int, float | None] = {}
    with CACHE_LOCK:
        for t, m in zip(targets_sec, minutes):
            out[t] = cache.get(cache_key(exchange, contract, m))
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description="Цены после сигнала → CSV")
    parser.add_argument("-i", "--input", default="result.csv")
    parser.add_argument("-o", "--output", default="result.csv")
    parser.add_argument("--cache", default="price_cache.json")
    parser.add_argument("--workers", type=int, default=MAX_WORKERS)
    parser.add_argument("--limit", type=int, default=0, help="Первые N строк (тест)")
    parser.add_argument("--save-every-groups", type=int, default=10)
    args = parser.parse_args()

    in_path = Path(args.input)
    out_path = Path(args.output)
    cache_path = Path(args.cache)

    with in_path.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        fieldnames = list(reader.fieldnames or [])
        rows = list(reader)

    if args.limit > 0:
        rows = rows[: args.limit]
        if out_path.resolve() == in_path.resolve():
            out_path = in_path.with_name(in_path.stem + f"_sample{args.limit}.csv")
            print(f"--limit: writing {out_path.name}", flush=True)

    if "msg_unixtime" not in fieldnames:
        print("No msg_unixtime column. Run: python parse_oi_to_csv.py", file=sys.stderr)
        sys.exit(1)

    for col in PRICE_COLUMNS:
        if col not in fieldnames:
            fieldnames.append(col)
    for row in rows:
        for col in PRICE_COLUMNS:
            row.setdefault(col, "")

    cache = load_cache(cache_path)
    # Перезапрашиваем ранее пустые значения (могли быть ложные промахи)
    none_keys = [k for k, v in cache.items() if v is None]
    for k in none_keys:
        del cache[k]
    if none_keys:
        print(f"cleared {len(none_keys)} empty cache entries", flush=True)
    print(f"rows={len(rows)} cache={len(cache)}", flush=True)

    groups: dict[tuple[str, str], list[int]] = defaultdict(list)
    for idx, row in enumerate(rows):
        exch = (row.get("exchange") or "").strip()
        sym = (row.get("symbol") or "").strip()
        if exch and sym and str(row.get("msg_unixtime") or "").strip():
            groups[(exch, sym)].append(idx)

    items = list(groups.items())
    total = len(items)
    done = 0
    t0 = time.time()

    def work(item):
        (exch, sym), indices = item
        all_targets: list[int] = []
        per_row: list[tuple[int, list[int]]] = []
        for idx in indices:
            base = int(float(rows[idx]["msg_unixtime"]))
            ts_list = [base + off for _, off in OFFSETS]
            per_row.append((idx, ts_list))
            all_targets.extend(ts_list)
        prices = prices_for_targets(exch, sym, all_targets, cache)
        return exch, sym, per_row, prices

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futs = {pool.submit(work, it): it[0] for it in items}
        for fut in as_completed(futs):
            done += 1
            try:
                exch, sym, per_row, prices = fut.result()
            except Exception as e:  # noqa: BLE001
                print(f"[{done}/{total}] ERROR {futs[fut]}: {e}", flush=True)
                continue
            for idx, ts_list in per_row:
                for (col, _), t in zip(OFFSETS, ts_list):
                    px = prices.get(t)
                    rows[idx][col] = "" if px is None else px
            if done % args.save_every_groups == 0 or done == total:
                save_cache(cache_path, cache)
                with out_path.open("w", encoding="utf-8-sig", newline="") as f:
                    w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
                    w.writeheader()
                    w.writerows(rows)
                elapsed = time.time() - t0
                print(
                    f"[{done}/{total}] saved last={exch}/{sym} "
                    f"cache={len(cache)} elapsed={elapsed:.0f}s",
                    flush=True,
                )

    filled = {col: sum(1 for r in rows if r.get(col) not in ("", None)) for col in PRICE_COLUMNS}
    print(f"DONE {out_path.resolve()}")
    print("filled:", filled)


if __name__ == "__main__":
    main()
