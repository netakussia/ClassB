from __future__ import annotations

import asyncio
import html
import logging
import sys
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path
from time import monotonic
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher
from aiogram.enums import ParseMode
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.ai.gateway import AIGateway
from bot.ai.gemini_client import GeminiClient
from bot.handlers.ai import router as ai_router
from bot.handlers.admin import router as admin_router
from bot.handlers.absent import mark_past_absences_completed, router as absent_router
from bot.handlers.base import router as base_router
from bot.handlers.docs import router as docs_router
from bot.handlers.events import router as events_router
from bot.handlers.hw import router as hw_router
from bot.handlers.replacements import router as replacements_router
from bot.handlers.schedule import router as schedule_router
from bot.middlewares.db import DbSessionMiddleware
from bot.middlewares.access import AccessMiddleware
from bot.middlewares.error import ErrorMiddleware
from bot.middlewares.role import RoleMiddleware
from bot.utils.emojis import get_emoji
from config_loader import AppConfig, load_config
from database.cleanup import clean_old_data
from database.engine import async_engine, init_db
from database.models import ClassEvent


logger = logging.getLogger(__name__)


def configure_logging() -> None:
    """Configure durable production logs before any background tasks start."""
    log_dir = Path(__file__).resolve().parent / "logs"
    log_dir.mkdir(exist_ok=True)
    formatter = logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )
    stdout_handler = logging.StreamHandler(sys.stdout)
    file_handler = RotatingFileHandler(
        log_dir / "bot.log", maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    for handler in (stdout_handler, file_handler):
        handler.setFormatter(formatter)
    logging.basicConfig(level=logging.INFO, handlers=[stdout_handler, file_handler], force=True)
    logging.getLogger("sqlalchemy").setLevel(logging.WARNING)


async def _daily_cleanup(
    session_factory: async_sessionmaker[AsyncSession],
    config: AppConfig,
) -> None:
    while True:
        await asyncio.sleep(24 * 60 * 60)
        try:
            async with session_factory() as session:
                await clean_old_data(
                    session,
                    config.bot.timezone,
                    activity_retention_days=config.ai.activity_retention_days,
                    quota_retention_days=config.ai.quota_retention_days,
                )
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
                        f"{get_emoji('reminder')} <b>Напоминание на завтра "
                        f"({tomorrow:%d.%m}):</b>\n"
                        f"{event_lines}"
                    ),
                    parse_mode=ParseMode.HTML,
                )
        except Exception:
            logger.exception("Не удалось отправить напоминания о событиях")


async def main() -> None:
    startup_started = monotonic()
    previous_stage_started = startup_started
    bot: Bot | None = None
    gemini_client: GeminiClient | None = None
    cleanup_task: asyncio.Task[None] | None = None
    event_reminder_task: asyncio.Task[None] | None = None

    def log_startup_stage(stage: str) -> None:
        nonlocal previous_stage_started
        now = monotonic()
        logger.info(
            "Startup timing stage=%s elapsed_ms=%d stage_ms=%d",
            stage,
            int((now - startup_started) * 1000),
            int((now - previous_stage_started) * 1000),
        )
        previous_stage_started = now

    try:
        configure_logging()
        log_startup_stage("application_entered")
        config = load_config()
        log_startup_stage("configuration_loaded")
        await init_db()
        log_startup_stage("database_initialized")
        session_factory = async_sessionmaker(
            bind=async_engine,
            expire_on_commit=False,
        )
        if (
            config.ai.enabled
            and config.ai.project_limits_verified
            and config.ai.api_key
        ):
            gemini_client = GeminiClient(
                config.ai.api_key,
                config.ai.model,
                config.ai.timeout_seconds,
            )
        elif config.ai.enabled and not config.ai.project_limits_verified:
            logger.warning(
                "Gemini is configured as enabled but project/model quotas are not verified; "
                "AI requests are disabled"
            )
        elif config.ai.enabled:
            logger.warning(
                "Gemini is globally enabled but GEMINI_API_KEY is missing; AI requests are disabled"
            )
        ai_gateway = AIGateway(
            gemini_client,
            session_factory,
            config.ai,
            config.bot.timezone,
        )
        log_startup_stage("core_services_initialized")
        async with session_factory() as session:
            await mark_past_absences_completed(session, config)
        try:
            async with session_factory() as session:
                await clean_old_data(
                    session,
                    config.bot.timezone,
                    activity_retention_days=config.ai.activity_retention_days,
                    quota_retention_days=config.ai.quota_retention_days,
                )
        except Exception:
            logger.exception("Стартовая очистка устаревших данных завершилась ошибкой")
        log_startup_stage("startup_maintenance_completed")
        cleanup_task = asyncio.create_task(
            _daily_cleanup(session_factory, config)
        )

        bot = Bot(token=config.bot.token)
        event_reminder_task = asyncio.create_task(
            _daily_event_reminders(bot, config, session_factory)
        )
        dispatcher = Dispatcher()
        dispatcher["config"] = config
        dispatcher["ai_gateway"] = ai_gateway

        db_middleware = DbSessionMiddleware(session_factory)
        access_middleware = AccessMiddleware()
        error_middleware = ErrorMiddleware()
        role_middleware = RoleMiddleware()
        dispatcher.message.outer_middleware(error_middleware)
        dispatcher.message.middleware(db_middleware)
        dispatcher.message.middleware(access_middleware)
        dispatcher.message.middleware(role_middleware)
        dispatcher.callback_query.middleware(db_middleware)
        dispatcher.callback_query.outer_middleware(error_middleware)
        dispatcher.callback_query.middleware(access_middleware)
        dispatcher.callback_query.middleware(role_middleware)

        dispatcher.include_router(base_router)
        dispatcher.include_router(docs_router)
        dispatcher.include_router(events_router)
        dispatcher.include_router(hw_router)
        dispatcher.include_router(schedule_router)
        dispatcher.include_router(replacements_router)
        dispatcher.include_router(admin_router)
        dispatcher.include_router(absent_router)
        dispatcher.include_router(ai_router)
        log_startup_stage("dispatcher_configured")
        log_startup_stage("polling_starting")
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
            if gemini_client is not None:
                try:
                    await gemini_client.close()
                except Exception as exc:
                    logger.error("Gemini client close failed (%s)", type(exc).__name__)
            if bot is not None:
                await bot.session.close()
        finally:
            await async_engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
