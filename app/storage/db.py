"""Database engine and session helpers."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable, Awaitable
from pathlib import Path
from typing import TypeVar

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.storage.models import Base

logger = logging.getLogger(__name__)

T = TypeVar("T")


def _ensure_sqlite_dir(url: str) -> None:
    if "sqlite" in url:
        path_part = url.split("///")[-1]
        if path_part and path_part != ":memory:":
            Path(path_part).parent.mkdir(parents=True, exist_ok=True)


def _attach_sqlite_pragmas(engine) -> None:
    """WAL + busy_timeout на каждое новое соединение."""

    @event.listens_for(engine.sync_engine, "connect")
    def _on_connect(dbapi_conn, _connection_record) -> None:  # noqa: ANN001
        try:
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("PRAGMA busy_timeout=60000")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()
        except Exception as e:  # noqa: BLE001
            logger.warning("sqlite pragma setup failed: %s", e)


def create_engine(database_url: str):
    _ensure_sqlite_dir(database_url)
    is_sqlite = "sqlite" in database_url
    kwargs: dict = {"echo": False}
    if is_sqlite:
        # timeout — секунды ожидания при locked (aiosqlite/sqlite3)
        kwargs["connect_args"] = {"timeout": 60, "check_same_thread": False}
        # без пула соединений — меньше конкурирующих writers
        kwargs["poolclass"] = NullPool
    engine = create_async_engine(database_url, **kwargs)
    if is_sqlite:
        _attach_sqlite_pragmas(engine)
    return engine


async def init_db(engine) -> None:
    async with engine.begin() as conn:
        if "sqlite" in str(engine.url):
            await conn.exec_driver_sql("PRAGMA journal_mode=WAL")
            await conn.exec_driver_sql("PRAGMA synchronous=NORMAL")
            await conn.exec_driver_sql("PRAGMA busy_timeout=60000")
        await conn.run_sync(Base.metadata.create_all)


def make_session_factory(engine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


async def session_scope(factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[AsyncSession]:
    async with factory() as session:
        yield session


async def with_db_retry(
    fn: Callable[[], Awaitable[T]],
    *,
    retries: int = 8,
    delay: float = 0.15,
) -> T:
    """Повтор при sqlite 'database is locked'."""
    last: Exception | None = None
    for attempt in range(retries):
        try:
            return await fn()
        except Exception as e:  # noqa: BLE001
            msg = str(e).lower()
            if "database is locked" not in msg and "locked" not in msg:
                raise
            last = e
            await asyncio.sleep(delay * (attempt + 1))
    assert last is not None
    raise last
