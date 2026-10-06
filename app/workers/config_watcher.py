"""Hot-reload YAML config watcher."""

from __future__ import annotations

import asyncio
import logging

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import ConfigStore
from app.storage import repo

logger = logging.getLogger(__name__)


class ConfigWatcher:
    def __init__(
        self,
        config_store: ConfigStore,
        session_factory: async_sessionmaker[AsyncSession],
        interval_sec: float = 5.0,
    ):
        self.config_store = config_store
        self.session_factory = session_factory
        self.interval_sec = interval_sec
        self._running = False

    async def start(self) -> None:
        self._running = True
        while self._running:
            changed, msg = self.config_store.try_reload()
            if changed:
                logger.info("Config hot-reload: %s", msg)
                async with self.session_factory() as session:
                    await repo.save_config_meta(session, self.config_store.hash)
            elif msg.startswith("invalid"):
                logger.error(msg)
            await asyncio.sleep(self.interval_sec)

    async def stop(self) -> None:
        self._running = False
