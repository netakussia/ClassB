from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.utils.schedule_parser import WEEKDAYS
from bot.utils.subjects import subject_slots_for_date
from config_loader import AppConfig
from database.models import ScheduleOverride


def local_today(config: AppConfig) -> date:
    return datetime.now(ZoneInfo(config.bot.timezone)).date()


async def subjects_for_date(
    session: AsyncSession,
    target_date: date,
    config: AppConfig,
) -> list[str]:
    subjects = subject_slots_for_date(target_date, config)
    overrides = await session.scalars(
        select(ScheduleOverride)
        .where(ScheduleOverride.target_date == target_date)
        .order_by(ScheduleOverride.lesson_number, ScheduleOverride.id)
    )
    replacements = {item.lesson_number: item for item in overrides}
    resolved: list[str] = []
    for number, subject in enumerate(subjects, start=1):
        override = replacements.get(number)
        resolved.append(
            override.new_subject
            if override is not None
            and override.override_type == "lesson"
            and override.new_subject
            else subject
        )
    return list(dict.fromkeys(resolved))


async def next_lesson_date(
    session: AsyncSession,
    subject: str,
    config: AppConfig,
    start: date,
    *,
    inclusive: bool = True,
) -> date | None:
    cursor = start if inclusive else start + timedelta(days=1)
    for _ in range(35):
        if subject in await subjects_for_date(session, cursor, config):
            return cursor
        cursor += timedelta(days=1)
    return None


async def next_school_date(
    session: AsyncSession,
    config: AppConfig,
    start: date,
) -> date | None:
    for offset in range(35):
        target_date = start + timedelta(days=offset)
        if await subjects_for_date(session, target_date, config):
            return target_date
    return None


async def homework_target_date(
    session: AsyncSession,
    config: AppConfig,
    now: datetime | None = None,
) -> date | None:
    local_now = now or datetime.now(ZoneInfo(config.bot.timezone))
    if local_now.tzinfo is not None:
        local_now = local_now.astimezone(ZoneInfo(config.bot.timezone))
    today = local_now.date()
    subjects = await subjects_for_date(session, today, config)

    if subjects:
        weekday = WEEKDAYS[today.weekday()]
        parity = config.schedule.get_parity(today)
        lessons = config.schedule.lessons_for(weekday, parity)
        last_start = max(time.fromisoformat(lesson.time) for lesson in lessons)
        last_end = (
            datetime.combine(today, last_start)
            + timedelta(minutes=config.schedule.lesson_duration_minutes)
        ).time()
        if local_now.time() < last_end:
            return today

    return await next_school_date(session, config, today + timedelta(days=1))