from __future__ import annotations

import asyncio
import html
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher
from aiogram.enums import ParseMode
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.handlers.admin import router as admin_router
from bot.handlers.absent import mark_past_absences_completed, router as absent_router
from bot.handlers.base import router as base_router
from bot.handlers.docs import router as docs_router
from bot.handlers.events import router as events_router
from bot.handlers.hw import router as hw_router
from bot.handlers.replacements import router as replacements_router
from bot.handlers.schedule import router as schedule_router
from bot.middlewares.db import DbSessionMiddleware
from bot.middlewares.role import RoleMiddleware
from config_loader import AppConfig, load_config
from database.cleanup import clean_old_data
from database.engine import async_engine, init_db
from database.models import ClassEvent


logger = logging.getLogger(__name__)


async def _daily_cleanup(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    while True:
        await asyncio.sleep(24 * 60 * 60)
        try:
            async with session_factory() as session:
                await clean_old_data(session)
        except Exception:
            logger.exception("Ежедневная очистка устаревших данных завершилась ошибкой")


async def _daily_event_reminders(
    bot: Bot,
    config: AppConfig,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    zone = ZoneInfo(config.bot.timezone)
    while True:
        now = datetime.now(zone)
        next_run = now.replace(hour=8, minute=0, second=0, microsecond=0)
        if next_run <= now:
            next_run += timedelta(days=1)
        await asyncio.sleep(max(0, next_run.timestamp() - now.timestamp()))

        tomorrow = datetime.now(zone).date() + timedelta(days=1)
        try:
            async with session_factory() as session:
                events = await session.scalars(
                    select(ClassEvent)
                    .where(ClassEvent.event_date == tomorrow)
                    .order_by(ClassEvent.event_date, ClassEvent.id)
                )
                upcoming = list(events)
            if upcoming:
                event_lines = "\n".join(
                    f"• {html.escape(event.title)}" for event in upcoming
                )
                await bot.send_message(
                    chat_id=config.bot.group_chat_id,
                    text=(
                        f"🔔 <b>Напоминание на завтра ({tomorrow:%d.%m}):</b>\n"
                        f"{event_lines}"
                    ),
                    parse_mode=ParseMode.HTML,
                )
        except Exception:
            logger.exception("Не удалось отправить напоминания о событиях")


async def main() -> None:
    bot: Bot | None = None
    cleanup_task: asyncio.Task[None] | None = None
    event_reminder_task: asyncio.Task[None] | None = None

    try:
        config = load_config()
        await init_db()
        session_factory = async_sessionmaker(
            bind=async_engine,
            expire_on_commit=False,
        )
        async with session_factory() as session:
            await mark_past_absences_completed(session, config)
        try:
            async with session_factory() as session:
                await clean_old_data(session)
        except Exception:
            logger.exception("Стартовая очистка устаревших данных завершилась ошибкой")
        cleanup_task = asyncio.create_task(_daily_cleanup(session_factory))

        bot = Bot(token=config.bot.token)
        event_reminder_task = asyncio.create_task(
            _daily_event_reminders(bot, config, session_factory)
        )
        dispatcher = Dispatcher()
        dispatcher["config"] = config

        db_middleware = DbSessionMiddleware(session_factory)
        role_middleware = RoleMiddleware()
        dispatcher.message.middleware(db_middleware)
        dispatcher.message.middleware(role_middleware)
        dispatcher.callback_query.middleware(db_middleware)
        dispatcher.callback_query.middleware(role_middleware)

        dispatcher.include_router(base_router)
        dispatcher.include_router(docs_router)
        dispatcher.include_router(events_router)
        dispatcher.include_router(hw_router)
        dispatcher.include_router(schedule_router)
        dispatcher.include_router(replacements_router)
        dispatcher.include_router(admin_router)
        dispatcher.include_router(absent_router)
        await dispatcher.start_polling(bot, close_bot_session=False)
    finally:
        background_tasks = [
            task
            for task in (cleanup_task, event_reminder_task)
            if task is not None
        ]
        for task in background_tasks:
            task.cancel()
        if background_tasks:
            await asyncio.gather(*background_tasks, return_exceptions=True)
        try:
            if bot is not None:
                await bot.session.close()
        finally:
            await async_engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
