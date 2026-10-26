"""Canonical subject names derived exclusively from the configured schedule."""

from __future__ import annotations

import re
import unicodedata
from datetime import date, timedelta

from bot.utils.schedule_parser import WEEKDAYS, parse_schedule
from bot.utils.time_utils import get_configured_week_parity
from config_loader import AppConfig
from sqlalchemy.ext.asyncio import AsyncSession


def _normalized_subject_name(subject: str) -> str:
    decomposed = unicodedata.normalize("NFKD", subject.casefold())
    without_diacritics = "".join(
        character for character in decomposed if not unicodedata.combining(character)
    )
    return re.sub(r"[^a-z0-9а-яё]+", "", without_diacritics)


def _language_subgroup(subject: str) -> str | None:
    normalized = _normalized_subject_name(subject)
    if any(
        marker in normalized
        for marker in ("француз", "francez", "french", "francais", "franc")
    ):
        return "french"
    if any(
        marker in normalized
        for marker in ("англий", "englez", "english", "engleza")
    ):
        return "english"
    return None


def resolve_subject(query: str, config: AppConfig) -> str | None:
    """Resolve a typed subject name or language-subgroup alias to its schedule name."""
    subjects = all_subjects(config)
    normalized_query = _normalized_subject_name(query)
    if not normalized_query:
        return None

    exact_matches = [
        subject
        for subject in subjects
        if _normalized_subject_name(subject) == normalized_query
    ]
    if len(exact_matches) == 1:
        return exact_matches[0]

    subgroup = _language_subgroup(query)
    if subgroup is not None:
        subgroup_matches = [
            subject for subject in subjects if _language_subgroup(subject) == subgroup
        ]
        if len(subgroup_matches) == 1:
            return subgroup_matches[0]
        return None

    partial_matches = [
        subject
        for subject in subjects
        if normalized_query in _normalized_subject_name(subject)
        or _normalized_subject_name(subject) in normalized_query
    ]
    return partial_matches[0] if len(partial_matches) == 1 else None


def all_subjects(config: AppConfig) -> list[str]:
    """Return schedule subjects in their first-occurrence order."""
    result: list[str] = []
    schedule = parse_schedule(config.schedule)
    for weekday in WEEKDAYS:
        for parity in ("odd", "even"):
            for lesson in schedule.lessons_for(weekday, parity):
                if lesson.subject not in result:
                    result.append(lesson.subject)
    return result


def subjects_for_date(target_date: date, config: AppConfig) -> list[str]:
    return list(dict.fromkeys(subject_slots_for_date(target_date, config)))


def subject_slots_for_date(target_date: date, config: AppConfig) -> list[str]:
    """Return subjects per lesson slot without removing repetitions."""
    schedule = parse_schedule(config.schedule)
    if target_date.weekday() >= len(WEEKDAYS):
        return []
    parity = schedule.get_parity(target_date)
    return [lesson.subject for lesson in schedule.lessons_for(WEEKDAYS[target_date.weekday()], parity)]


async def upcoming_subjects(
    start: date,
    config: AppConfig,
    session: AsyncSession,
    school_days: int = 5,
) -> list[str]:
    """Subjects occurring during the next ``school_days`` days with lessons."""
    result: list[str] = []
    schedule = parse_schedule(config.schedule)
    cursor = start
    found_days = 0
    while found_days < school_days:
        daily: list[str] = []
        if cursor.weekday() < len(WEEKDAYS):
            parity = await get_configured_week_parity(session, cursor, config)
            daily = [
                lesson.subject
                for lesson in schedule.lessons_for(WEEKDAYS[cursor.weekday()], parity)
            ]
        daily = list(dict.fromkeys(daily))
        if daily:
            found_days += 1
            for subject in daily:
                if subject not in result:
                    result.append(subject)
        cursor += timedelta(days=1)
    return result
