"""Database engine and session helpers (PostgreSQL primary, SQLite optional for tests)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import TypeVar

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.storage.models import Base

logger = logging.getLogger(__name__)

T = TypeVar("T")


def _is_sqlite(url: str) -> bool:
    return "sqlite" in url


def _ensure_sqlite_dir(url: str) -> None:
    if not _is_sqlite(url):
        return
    path_part = url.split("///")[-1]
    if path_part and path_part != ":memory:":
        Path(path_part).parent.mkdir(parents=True, exist_ok=True)


def _attach_sqlite_pragmas(engine) -> None:
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
    kwargs: dict = {
        "echo": False,
        "pool_pre_ping": True,
    }
    if _is_sqlite(database_url):
        kwargs["connect_args"] = {"timeout": 60, "check_same_thread": False}
        kwargs["poolclass"] = NullPool
    else:
        # PostgreSQL / asyncpg
        kwargs["pool_size"] = 5
        kwargs["max_overflow"] = 10
        kwargs["pool_timeout"] = 30

    engine = create_async_engine(database_url, **kwargs)
    if _is_sqlite(database_url):
        _attach_sqlite_pragmas(engine)
    else:
        logger.info("DB engine: PostgreSQL (%s)", database_url.split("@")[-1] if "@" in database_url else database_url)
    return engine


async def init_db(engine) -> None:
    async with engine.begin() as conn:
        if _is_sqlite(str(engine.url)):
            await conn.exec_driver_sql("PRAGMA journal_mode=WAL")
            await conn.exec_driver_sql("PRAGMA synchronous=NORMAL")
            await conn.exec_driver_sql("PRAGMA busy_timeout=60000")
        await conn.run_sync(Base.metadata.create_all)
    logger.info("DB schema ready (%s)", engine.url.render_as_string(hide_password=True))


def make_session_factory(engine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


async def session_scope(factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[AsyncSession]:
    async with factory() as session:
        yield session


def _is_retryable_db_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    needles = (
        "database is locked",
        "locked",
        "deadlock detected",
        "could not serialize",
        "connection was closed",
        "server closed the connection",
        "too many clients",
    )
    return any(n in msg for n in needles)


async def with_db_retry(
    fn: Callable[[], Awaitable[T]],
    *,
    retries: int = 8,
    delay: float = 0.15,
) -> T:
    """Повтор при временных ошибках БД (SQLite lock / PG deadlock)."""
    last: Exception | None = None
    for attempt in range(retries):
        try:
            return await fn()
        except Exception as e:  # noqa: BLE001
            if not _is_retryable_db_error(e):
                raise
            last = e
            await asyncio.sleep(delay * (attempt + 1))
    assert last is not None
    raise last
