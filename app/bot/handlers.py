"""Telegram bot handlers."""

from __future__ import annotations

import logging

import psutil
from aiogram import Dispatcher, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import ConfigStore
from app.core.normalize import normalize_user_input
from app.settings import Settings
from app.storage import repo
from app.storage.models import ExchangeHealth

logger = logging.getLogger(__name__)

router = Router()


def _is_admin(message: Message, settings: Settings) -> bool:
    return bool(message.from_user and message.from_user.id in settings.admin_id_set)


def setup_bot_routes(
    dp: Dispatcher,
    session_factory: async_sessionmaker[AsyncSession],
    config_store: ConfigStore,
    settings: Settings,
) -> None:
    @router.message(Command("start"))
    async def cmd_start(message: Message) -> None:
        async with session_factory() as session:
            user = await repo.get_or_create_user(
                session, message.from_user.id, settings.admin_id_set
            )
        await message.answer(
            "OI Monitor бот запущен.\n"
            "Команды:\n"
            "/add BTC — добавить тикер\n"
            "/remove BTC — убрать из новых сигналов\n"
            "/list — ваш watchlist\n"
            "/help — справка"
        )
        logger.info("user registered telegram_id=%s db_id=%s", message.from_user.id, user.id)

    @router.message(Command("help"))
    async def cmd_help(message: Message) -> None:
        await message.answer(
            "Мониторинг открытого интереса (OI) по perpetual.\n\n"
            "/add <ticker> — подписка (BTC, ETH, 1000PEPE…)\n"
            "/remove <ticker> — сразу прекращает новые сигналы для вас\n"
            "/list — список тикеров\n\n"
            "Сигналы S1–S7, фильтры ликвидности и cooldown. "
            "/remove не удаляет историю."
        )

    @router.message(Command("add"))
    async def cmd_add(message: Message, command: CommandObject) -> None:
        if not command.args:
            await message.answer("Использование: /add BTC")
            return
        cfg = config_store.config
        base = normalize_user_input(command.args.split()[0], cfg.normalization)
        if not base:
            await message.answer("Некорректный тикер")
            return
        async with session_factory() as session:
            user = await repo.get_or_create_user(
                session, message.from_user.id, settings.admin_id_set
            )
            added = await repo.add_watchlist(session, user.id, base)
        if added:
            await message.answer(f"Добавлено: {base}")
        else:
            await message.answer(f"{base} уже в списке")

    @router.message(Command("remove"))
    async def cmd_remove(message: Message, command: CommandObject) -> None:
        if not command.args:
            await message.answer("Использование: /remove BTC")
            return
        cfg = config_store.config
        base = normalize_user_input(command.args.split()[0], cfg.normalization)
        async with session_factory() as session:
            user = await repo.get_or_create_user(
                session, message.from_user.id, settings.admin_id_set
            )
            removed = await repo.remove_watchlist(session, user.id, base)
        if removed:
            await message.answer(
                f"Убрано: {base}. Новые сигналы по этому тикеру больше не придут. "
                "История сохранена."
            )
        else:
            await message.answer(f"{base} не найден в вашем списке")

    @router.message(Command("list"))
    async def cmd_list(message: Message) -> None:
        async with session_factory() as session:
            user = await repo.get_or_create_user(
                session, message.from_user.id, settings.admin_id_set
            )
            items = await repo.list_watchlist(session, user.id)
        if not items:
            await message.answer("Список пуст. Добавьте: /add BTC")
        else:
            await message.answer("Watchlist:\n" + "\n".join(f"• {x}" for x in items))

    @router.message(Command("admin_stats"))
    async def cmd_admin_stats(message: Message) -> None:
        if not _is_admin(message, settings):
            return
        async with session_factory() as session:
            st = await repo.stats(session)
        rss = psutil.Process().memory_info().rss / (1024 * 1024)
        await message.answer(
            "Stats:\n"
            f"users={st['users']}\n"
            f"watchlist_rows={st['watchlist_rows']}\n"
            f"unique_symbols={st['unique_symbols']}\n"
            f"raw_ticks={st['raw_ticks']}\n"
            f"signals={st['signals']}\n"
            f"rss_mb={rss:.1f}\n"
            f"config_hash={config_store.hash}"
        )

    @router.message(Command("admin_symbols"))
    async def cmd_admin_symbols(message: Message) -> None:
        if not _is_admin(message, settings):
            return
        async with session_factory() as session:
            symbols = await repo.union_watchlist_symbols(session)
        await message.answer(
            "Active bases:\n" + (", ".join(symbols) if symbols else "(empty)")
        )

    @router.message(Command("admin_reload"))
    async def cmd_admin_reload(message: Message) -> None:
        if not _is_admin(message, settings):
            return
        changed, msg = config_store.try_reload()
        if changed:
            async with session_factory() as session:
                await repo.save_config_meta(session, config_store.hash)
        await message.answer(msg)

    @router.message(Command("admin_status"))
    async def cmd_admin_status(message: Message) -> None:
        if not _is_admin(message, settings):
            return
        async with session_factory() as session:
            rows = (await session.execute(select(ExchangeHealth))).scalars().all()
        if not rows:
            await message.answer("No exchange health data yet")
            return
        lines = []
        for r in rows:
            lines.append(
                f"{r.exchange}: fails={r.consecutive_failures} "
                f"last_ok={r.last_success_at} err={r.last_error or '-'}"
            )
        await message.answer("Exchanges:\n" + "\n".join(lines))

    dp.include_router(router)
