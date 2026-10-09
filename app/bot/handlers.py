"""Telegram bot handlers."""

from __future__ import annotations

import logging
import re

import psutil
from aiogram import Dispatcher, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bot.market_info import (
    format_check_report,
    format_list_report,
    lookup_base_on_exchanges,
)
from app.config import ConfigStore
from app.core.normalize import normalize_user_input
from app.settings import Settings
from app.storage import repo
from app.storage.models import ExchangeHealth

logger = logging.getLogger(__name__)

router = Router()


class Form(StatesGroup):
    waiting_check = State()
    waiting_add = State()


def _is_admin(message: Message, settings: Settings) -> bool:
    return bool(message.from_user and message.from_user.id in settings.admin_id_set)


def _remove_keyboard(symbols: list[str]) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text=f"✖ {sym}", callback_data=f"rm:{sym}")]
        for sym in symbols
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def setup_bot_routes(
    dp: Dispatcher,
    session_factory: async_sessionmaker[AsyncSession],
    config_store: ConfigStore,
    settings: Settings,
) -> None:
    @router.message(Command("start"))
    async def cmd_start(message: Message, state: FSMContext) -> None:
        await state.clear()
        async with session_factory() as session:
            user = await repo.get_or_create_user(
                session, message.from_user.id, settings.admin_id_set
            )
        await message.answer(
            "OI Monitor бот запущен.\n\n"
            "Команды:\n"
            "/check — проверить тикер (биржи и OI)\n"
            "/add — добавить тикер в мониторинг\n"
            "/remove — убрать тикер (кнопки)\n"
            "/list — список мониторинга"
        )
        logger.info("user registered telegram_id=%s db_id=%s", message.from_user.id, user.id)

    # —— /check ——
    @router.message(Command("check"))
    async def cmd_check(message: Message, state: FSMContext) -> None:
        await state.set_state(Form.waiting_check)
        await message.answer("Введите тикер")

    # Cyrillic «с» + heck (частая опечатка)
    @router.message(F.text.regexp(re.compile(r"^/сheck(?:@\w+)?$", re.IGNORECASE)))
    async def cmd_check_cyr(message: Message, state: FSMContext) -> None:
        await cmd_check(message, state)

    @router.message(Form.waiting_check, F.text)
    async def on_check_ticker(message: Message, state: FSMContext) -> None:
        text = (message.text or "").strip()
        if text.startswith("/"):
            await state.clear()
            await message.answer("Ожидание тикера сброшено. Выберите команду заново.")
            return
        cfg = config_store.config
        base = normalize_user_input(text, cfg.normalization)
        if not base:
            await message.answer("Некорректный тикер. Введите ещё раз или /start")
            return
        await message.answer(f"Ищу {base} на биржах…")
        try:
            rows = await lookup_base_on_exchanges(base, cfg)
            await message.answer(f"<pre>{format_check_report(base, rows)}</pre>", parse_mode="HTML")
        except Exception as e:  # noqa: BLE001
            logger.exception("check failed")
            await message.answer(f"Ошибка запроса: {e}")
        await state.clear()

    # —— /add ——
    @router.message(Command("add"))
    async def cmd_add(message: Message, state: FSMContext, command: CommandObject) -> None:
        cfg = config_store.config
        # совместимость: /add BTC сразу
        if command.args:
            base = normalize_user_input(command.args.split()[0], cfg.normalization)
            await _do_add(message, base)
            await state.clear()
            return
        await state.set_state(Form.waiting_add)
        await message.answer("Введите тикер")

    @router.message(Form.waiting_add, F.text)
    async def on_add_ticker(message: Message, state: FSMContext) -> None:
        text = (message.text or "").strip()
        if text.startswith("/"):
            await state.clear()
            await message.answer("Ожидание тикера сброшено. Выберите команду заново.")
            return
        cfg = config_store.config
        base = normalize_user_input(text, cfg.normalization)
        if not base:
            await message.answer("Некорректный тикер. Введите ещё раз или /start")
            return
        await _do_add(message, base)
        await state.clear()

    async def _do_add(message: Message, base: str) -> None:
        async with session_factory() as session:
            user = await repo.get_or_create_user(
                session, message.from_user.id, settings.admin_id_set
            )
            added = await repo.add_watchlist(session, user.id, base)
        if added:
            await message.answer(f"Добавлено в мониторинг: {base}")
        else:
            await message.answer(f"{base} уже в списке")

    # —— /remove (кнопки) ——
    @router.message(Command("remove"))
    async def cmd_remove(message: Message, state: FSMContext) -> None:
        await state.clear()
        async with session_factory() as session:
            user = await repo.get_or_create_user(
                session, message.from_user.id, settings.admin_id_set
            )
            items = await repo.list_watchlist(session, user.id)
        if not items:
            await message.answer("Список пуст — удалять нечего.")
            return
        await message.answer(
            "Выберите тикер для удаления из мониторинга:",
            reply_markup=_remove_keyboard(items),
        )

    @router.callback_query(F.data.startswith("rm:"))
    async def on_remove_callback(query: CallbackQuery) -> None:
        base = (query.data or "").split(":", 1)[-1].upper()
        async with session_factory() as session:
            user = await repo.get_or_create_user(
                session, query.from_user.id, settings.admin_id_set
            )
            removed = await repo.remove_watchlist_with_user_data(session, user.id, base)
            items = await repo.list_watchlist(session, user.id)
        if removed:
            text = f"Удалено: {base} (watchlist + ваши cooldown/delivery по тикеру)."
        else:
            text = f"{base} не найден в списке."
        if items:
            await query.message.edit_text(
                text + "\n\nВыберите следующий тикер или закройте сообщение:",
                reply_markup=_remove_keyboard(items),
            )
        else:
            await query.message.edit_text(text + "\nСписок мониторинга пуст.")
        await query.answer()

    # —— /list ——
    @router.message(Command("list"))
    async def cmd_list(message: Message, state: FSMContext) -> None:
        await state.clear()
        async with session_factory() as session:
            user = await repo.get_or_create_user(
                session, message.from_user.id, settings.admin_id_set
            )
            entries = await repo.list_watchlist_entries(session, user.id)
            bases = [e[0] for e in entries]
            exch_map = await repo.exchanges_map_for_bases(session, bases)
        if not entries:
            await message.answer("Отслеживаются\n\n(пусто)\n\nДобавьте тикер: /add")
            return
        await message.answer(format_list_report(entries, exch_map))

    # —— admin ——
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
