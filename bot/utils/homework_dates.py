from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.utils.schedule_parser import WEEKDAYS
from bot.utils.schedule_parser import parse_schedule
from bot.utils.time_utils import calculate_lesson_times, get_configured_week_parity
from config_loader import AppConfig
from database.models import ScheduleOverride, ScheduleTimeOverride


def local_today(config: AppConfig) -> date:
    return datetime.now(ZoneInfo(config.bot.timezone)).date()


async def subjects_for_date(
    session: AsyncSession,
    target_date: date,
    config: AppConfig,
) -> list[str]:
    schedule = parse_schedule(config.schedule)
    base_lessons = (
        schedule.lessons_for(WEEKDAYS[target_date.weekday()], await get_configured_week_parity(session, target_date, config))
        if target_date.weekday() < len(WEEKDAYS)
        else []
    )
    overrides = await session.scalars(
        select(ScheduleOverride)
        .where(ScheduleOverride.target_date == target_date)
        .order_by(ScheduleOverride.lesson_number, ScheduleOverride.id)
    )
    replacements = {item.lesson_number: item for item in overrides}
    resolved: list[str] = []
    for number in range(1, max(len(base_lessons), max(replacements, default=0)) + 1):
        override = replacements.get(number)
        if override is not None and override.is_cancelled:
            continue
        base_subject = base_lessons[number - 1].subject if number <= len(base_lessons) else None
        subject = (
            override.new_subject
            if override is not None and override.override_type == "lesson" and override.new_subject
            else base_subject
        )
        if subject:
            resolved.append(subject)
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
        parity = await get_configured_week_parity(session, today, config)
        lessons = config.schedule.lessons_for(weekday, parity)
        overrides = await session.scalars(
            select(ScheduleOverride)
            .where(ScheduleOverride.target_date == today)
            .order_by(ScheduleOverride.lesson_number, ScheduleOverride.id)
        )
        latest_overrides = {item.lesson_number: item for item in overrides}
        active_lesson_numbers = [
            number
            for number in range(1, len(lessons) + 1)
            if not (
                latest_overrides.get(number)
                and latest_overrides[number].is_cancelled
            )
        ]
        time_overrides = await session.scalars(
            select(ScheduleTimeOverride)
            .where(ScheduleTimeOverride.target_date == today)
            .order_by(ScheduleTimeOverride.id)
        )
        clocks = calculate_lesson_times(
            (lesson.time for lesson in lessons),
            config.schedule.lesson_duration_minutes,
            time_overrides,
        )
        active_ends = [
            time.fromisoformat(clocks[number - 1].end)
            for number in active_lesson_numbers
        ]
        if not active_ends:
            return today
        last_end = max(active_ends)
        if local_now.time() < last_end:
            return today

    return await next_school_date(session, config, today + timedelta(days=1))