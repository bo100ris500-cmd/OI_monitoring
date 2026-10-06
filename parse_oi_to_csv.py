#!/usr/bin/env python3
"""Парсинг сообщений OI из result.json в CSV."""

from __future__ import annotations

import argparse
import csv
import json
import re
from pathlib import Path

COLUMNS = [
    "symbol",
    "date",
    "timestamp",
    "msg_unixtime",
    "exchange",
    "oi_acceleration_4h_pct",
    "oi_before_usd",
    "oi_after_usd",
    "oi_profit_usd",
    "price_usd",
    "price_change_pct",
    "long_pct",
    "short_pct",
    "funding_rate_pct",
    "volume_24h_usd",
]

# Известные биржи в сообщениях
EXCHANGE_RE = re.compile(
    r"^(Bybit|Binance|OKX|Bitget|Gate|KuCoin|Huobi|MEXC|Hyperliquid)\s*$",
    re.IGNORECASE | re.MULTILINE,
)

MONEY_RE = re.compile(
    r"\$?\s*([+-]?\d+(?:\.\d+)?)\s*([KMB])?\b",
    re.IGNORECASE,
)


def flatten_text(text) -> str:
    if isinstance(text, str):
        return text
    if isinstance(text, list):
        parts = []
        for item in text:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                parts.append(item.get("text", ""))
        return "".join(parts)
    return ""


def fix_mojibake(s: str) -> str:
    """Чинит двойную кодировку UTF-8↔CP1251 в части экспорта Telegram."""
    if "Р" not in s:
        return s
    try:
        fixed = s.encode("cp1251").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return s
    # Применяем только если стало читаемее
    markers = ("OI", "За", "вырос", "Цена", "Прирост", "Объём", "ускорение")
    if sum(m in fixed for m in markers) >= sum(m in s for m in markers):
        return fixed
    return s


def parse_money(raw: str | None) -> float | None:
    if raw is None:
        return None
    m = MONEY_RE.search(raw.replace(",", "").replace(" ", ""))
    if not m:
        return None
    value = float(m.group(1))
    suffix = (m.group(2) or "").upper()
    mult = {"": 1, "K": 1_000, "M": 1_000_000, "B": 1_000_000_000}[suffix]
    return round(value * mult, 2)


def parse_pct(raw: str | None) -> float | None:
    if raw is None:
        return None
    m = re.search(r"([+-]?\d+(?:\.\d+)?)\s*%", raw.replace(",", ""))
    return float(m.group(1)) if m else None


def extract_symbol(text: str) -> str | None:
    m = re.search(r"\$([A-Za-z0-9]+)", text)
    return m.group(1) if m else None


def extract_date(msg_date: str | None) -> str:
    """Дата сообщения из поля date экспорта (YYYY-MM-DD)."""
    if not msg_date:
        return ""
    # "2026-04-02T05:44:32" → "2026-04-02"
    return msg_date[:10]


def extract_timestamp(text: str, fallback_date: str | None = None) -> str:
    m = re.search(r"(\d{1,2}:\d{2})\s*UTC", text)
    if m:
        return f"{m.group(1)} UTC"
    if fallback_date:
        # date вида 2026-04-02T05:44:32 — берём время как UTC-маркер из сообщения отсутствует
        try:
            hhmm = fallback_date[11:16]
            return f"{hhmm} UTC"
        except Exception:
            pass
    return ""


def is_oi_alert(text: str) -> bool:
    if not text or not text.strip():
        return False
    lower = text.lower()
    if "гайд" in lower or "teletype.in" in lower:
        return False
    markers = (
        "oi ускорение",
        "oi вырос",
        "oi упал",
        "oi $:",
        "прирост:",
        "за 4ч oi",
        "за 1ч oi",
    )
    return any(m in lower for m in markers)


def split_exchange_blocks(text: str) -> list[tuple[str, str]]:
    """
    Возвращает [(exchange, block_text), ...].
    Для старого формата `$SYM | Bybit\\n...` — один блок.
    Для нового `$SYM | Bybit + Binance\\n\\nBybit\\n...\\nBinance\\n...` — по блоку на биржу.
    """
    lines = text.splitlines()
    # Явные секции по названию биржи в начале строки
    indices = []
    for i, line in enumerate(lines):
        if EXCHANGE_RE.match(line.strip()):
            indices.append((i, line.strip()))

    # Несколько секций (Bybit / Binance / ...)
    if len(indices) >= 1 and (
        len(indices) >= 2
        or ("OI ускорение" in text and indices[0][0] > 0)
    ):
        blocks = []
        for n, (start, name) in enumerate(indices):
            end = indices[n + 1][0] if n + 1 < len(indices) else len(lines)
            block = "\n".join(lines[start + 1 : end]).strip()
            blocks.append((name, block))
        # Отфильтровываем пустые / не-OI блоки
        oi_blocks = [(ex, b) for ex, b in blocks if "OI" in b or "Цена" in b or "Funding" in b]
        if oi_blocks:
            return oi_blocks

    # Старый формат: $SYM | Exchange
    m = re.search(r"\$[A-Za-z0-9]+\s*\|\s*([A-Za-z0-9]+)", text)
    if m:
        return [(m.group(1), text)]

    return []


def parse_block(block: str) -> dict:
    row: dict = {c: "" for c in COLUMNS}

    # 4ч ускорение / рост
    m = re.search(
        r"(?:OI\s*ускорение\s*\(4ч\)|OI\s*вырос\s*на|За\s*4ч\s*OI\s*вырос\s*на|"
        r"OI\s*упал\s*на|За\s*4ч\s*OI\s*упал\s*на)\s*([+-]?\d+(?:\.\d+)?)\s*%",
        block,
        re.IGNORECASE,
    )
    if m:
        row["oi_acceleration_4h_pct"] = float(m.group(1))
    else:
        # альтернатива: "... +5.26% за 4ч"
        m = re.search(
            r"OI\s*вырос\s*на\s*([+-]?\d+(?:\.\d+)?)\s*%\s*за\s*4ч",
            block,
            re.IGNORECASE,
        )
        if m:
            row["oi_acceleration_4h_pct"] = float(m.group(1))

    # OI before -> after
    m = re.search(
        r"OI\s*\$?\s*:\s*(\$?[\d.,]+\s*[KMB]?)\s*->\s*(\$?[\d.,]+\s*[KMB]?)",
        block,
        re.IGNORECASE,
    )
    if m:
        row["oi_before_usd"] = parse_money(m.group(1))
        row["oi_after_usd"] = parse_money(m.group(2))

    # Прирост: $X  или  ($X) сразу после OI
    m = re.search(r"Прирост\s*:\s*(\$?[\d.,]+\s*[KMB]?)", block, re.IGNORECASE)
    if not m:
        m = re.search(r"\(\s*(\$?[\d.,]+\s*[KMB]?)\s*\)", block)
    if m:
        row["oi_profit_usd"] = parse_money(m.group(1))
    elif row["oi_before_usd"] != "" and row["oi_after_usd"] != "":
        row["oi_profit_usd"] = round(row["oi_after_usd"] - row["oi_before_usd"], 2)

    # Цена
    m = re.search(
        r"(?:Цена|Price)\s*:\s*\$?\s*([\d.]+)\s*(?:\(([+-]?\d+(?:\.\d+)?)%\))?",
        block,
        re.IGNORECASE,
    )
    if m:
        row["price_usd"] = float(m.group(1))
        if m.group(2) is not None:
            row["price_change_pct"] = float(m.group(2))

    # L/S
    m = re.search(
        r"L/S\s*:\s*.*?(\d+(?:\.\d+)?)\s*%\s*/\s*.*?(\d+(?:\.\d+)?)\s*%",
        block,
        re.IGNORECASE,
    )
    if m:
        row["long_pct"] = float(m.group(1))
        row["short_pct"] = float(m.group(2))

    # Funding
    m = re.search(r"Funding\s*:\s*([+-]?\d+(?:\.\d+)?)\s*%", block, re.IGNORECASE)
    if m:
        row["funding_rate_pct"] = float(m.group(1))

    # Volume 24h
    m = re.search(
        r"(?:Vol\s*24h|Объём\s*24h|Volume\s*24h)\s*:\s*(\$?[\d.,]+\s*[KMB]?)",
        block,
        re.IGNORECASE,
    )
    if m:
        row["volume_24h_usd"] = parse_money(m.group(1))

    return row


def parse_message(msg: dict) -> list[dict]:
    text = fix_mojibake(flatten_text(msg.get("text", "")))
    if not is_oi_alert(text):
        return []

    # Пропускаем чисто 1ч алерты (в схеме только 4ч)
    if re.search(r"За\s*1ч\s*OI|OI\s*вырос\s*на\s*[+-]?[\d.]+\s*%\s*за\s*1ч", text, re.I):
        if not re.search(r"4ч", text):
            return []

    symbol = extract_symbol(text)
    if not symbol:
        return []

    msg_date = msg.get("date")
    date = extract_date(msg_date)
    timestamp = extract_timestamp(text, msg_date)
    msg_unixtime = msg.get("date_unixtime", "")
    rows = []
    for exchange, block in split_exchange_blocks(text):
        parsed = parse_block(block)
        # Берём только блоки с 4ч ускорением (или старым "за 4ч")
        if parsed["oi_acceleration_4h_pct"] == "" and "4ч" not in block and "4ч" not in text:
            continue
        # Если в блоке нет цифр OI — пропускаем
        if parsed["oi_before_usd"] == "" and parsed["oi_acceleration_4h_pct"] == "":
            continue
        parsed["symbol"] = symbol
        parsed["date"] = date
        parsed["timestamp"] = timestamp
        parsed["msg_unixtime"] = msg_unixtime
        parsed["exchange"] = exchange
        rows.append(parsed)
    return rows


def empty_to_blank(value):
    return "" if value is None else value


def main() -> None:
    parser = argparse.ArgumentParser(description="Парсинг result.json → CSV")
    parser.add_argument(
        "-i",
        "--input",
        default="result.json",
        help="Путь к JSON экспорта Telegram (по умолчанию result.json)",
    )
    parser.add_argument(
        "-o",
        "--output",
        default="result.csv",
        help="Путь к выходному CSV (по умолчанию result.csv)",
    )
    args = parser.parse_args()

    input_path = Path(args.input)
    output_path = Path(args.output)

    with input_path.open(encoding="utf-8") as f:
        data = json.load(f)

    messages = data.get("messages", [])
    rows: list[dict] = []
    skipped = 0
    for msg in messages:
        if msg.get("type") != "message":
            continue
        parsed_rows = parse_message(msg)
        if not parsed_rows:
            text = fix_mojibake(flatten_text(msg.get("text", "")))
            if is_oi_alert(text):
                skipped += 1
            continue
        rows.extend(parsed_rows)

    with output_path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: empty_to_blank(row.get(k, "")) for k in COLUMNS})

    print(f"Сообщений в JSON: {len(messages)}")
    print(f"Строк в CSV: {len(rows)}")
    print(f"OI-алертов без 4ч / не разобрано: {skipped}")
    print(f"Записано: {output_path.resolve()}")


if __name__ == "__main__":
    main()
