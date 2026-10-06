#!/usr/bin/env python3
"""OI Telegram monitoring bot — entrypoint."""

from __future__ import annotations

import asyncio
import logging
import shutil
import sys
from pathlib import Path

from aiogram import Bot, Dispatcher

# ensure project root on path
ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.bot.handlers import setup_bot_routes
from app.config import ConfigStore
from app.settings import get_settings
from app.storage.db import create_engine, init_db, make_session_factory
from app.storage import repo
from app.workers.collector_loop import Pipeline
from app.workers.config_watcher import ConfigWatcher
from app.workers.post_factum import PostFactumWorker


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )


async def amain() -> None:
    settings = get_settings()
    setup_logging(settings.log_level)

    config_path = Path(settings.config_path)
    if not config_path.exists():
        example = ROOT / "config.example.yaml"
        if example.exists():
            shutil.copy(example, config_path)
        else:
            raise SystemExit(f"Missing config: {config_path}")

    config_store = ConfigStore(config_path)
    config_store.load(force=True)

    engine = create_engine(settings.database_url)
    await init_db(engine)
    session_factory = make_session_factory(engine)

    async with session_factory() as session:
        await repo.save_config_meta(session, config_store.hash)

    bot = Bot(token=settings.bot_token)
    dp = Dispatcher()
    setup_bot_routes(dp, session_factory, config_store, settings)

    pipeline = Pipeline(bot, session_factory, config_store)
    post_factum = PostFactumWorker(session_factory, config_store)
    watcher = ConfigWatcher(config_store, session_factory)

    logging.getLogger(__name__).info(
        "Starting OI bot config_hash=%s mode=%s",
        config_store.hash,
        config_store.config.monitoring_mode,
    )

    await asyncio.gather(
        dp.start_polling(bot),
        pipeline.start(),
        post_factum.start(),
        watcher.start(),
    )


def main() -> None:
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
