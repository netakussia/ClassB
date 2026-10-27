from __future__ import annotations

import time
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from config_loader import AIConfig
from database.models import AIActivityDaily, AIRecentResponse, AIUserSettings

_GREETING_CACHE: dict[tuple[int, str], tuple[float, str]] = {}
logger = logging.getLogger(__name__)


def clear_user_cache(user_id: int) -> None:
    for key in [key for key in _GREETING_CACHE if key[0] == user_id]:
        _GREETING_CACHE.pop(key, None)


async def personalized_greeting(
    session: AsyncSession,
    user_id: int,
    config: AIConfig,
    timezone_name: str,
    *,
    now: datetime | None = None,
) -> str:
    if not config.personalization_enabled:
        return ""
    try:
        settings = await session.get(AIUserSettings, user_id)
    except Exception as exc:
        logger.warning("Could not load AI personalization settings (%s)", type(exc).__name__)
        return ""
    if settings is None or not settings.personalization_enabled:
        return ""

    instant = now or datetime.now(ZoneInfo(timezone_name))
    local_now = instant.astimezone(ZoneInfo(timezone_name))
    day_part = (
        "night" if local_now.hour < 6
        else "morning" if local_now.hour < 12
        else "day" if local_now.hour < 18
        else "evening"
    )
    cache_key = (
        user_id,
        f"{local_now.date()}:{day_part}:{settings.humor_enabled}",
    )
    cached = _GREETING_CACHE.get(cache_key)
    if cached is not None and cached[0] > time.monotonic():
        return cached[1]

    try:
        homework_count = await session.scalar(
            select(func.coalesce(func.sum(AIActivityDaily.count), 0)).where(
                AIActivityDaily.telegram_id == user_id,
                AIActivityDaily.activity_date == local_now.date(),
                AIActivityDaily.category == "homework",
            )
        )
    except Exception as exc:
        logger.warning("Could not load local AI activity aggregate (%s)", type(exc).__name__)
        homework_count = 0
    if (
        homework_count
        and homework_count >= 3
        and config.humor_enabled
        and settings.humor_enabled
    ):
        suffix = "Домашку сегодня уже проверяли — у нас тут режим продуктивности."
    else:
        suffix = {
            "night": "Поздний визит в цифровой хаб принят.",
            "morning": "Хорошего утра — нужные разделы уже под рукой.",
            "day": "Хорошего дня — ДЗ и расписание рядом.",
            "evening": "Вечерний заход в хаб — без лишней суеты.",
        }[day_part]
    _GREETING_CACHE[cache_key] = (
        time.monotonic() + config.personalization_cache_hours * 3600,
        suffix,
    )
    return suffix


async def reset_personalization(session: AsyncSession, user_id: int) -> None:
    from sqlalchemy import delete

    settings = await session.get(AIUserSettings, user_id)
    if settings is not None:
        settings.gemini_enabled = False
        settings.humor_enabled = False
        settings.personalization_enabled = False
        settings.reactions_enabled = False
        settings.humor_style = "memey"
    await session.execute(
        delete(AIActivityDaily).where(AIActivityDaily.telegram_id == user_id)
    )
    await session.execute(
        delete(AIRecentResponse).where(AIRecentResponse.telegram_id == user_id)
    )
    clear_user_cache(user_id)
