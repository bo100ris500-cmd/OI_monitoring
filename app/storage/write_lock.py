"""Глобальный asyncio-lock для записи в SQLite (один writer за раз)."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator

_write_lock = asyncio.Lock()


@asynccontextmanager
async def db_write_lock() -> AsyncIterator[None]:
    async with _write_lock:
        yield
