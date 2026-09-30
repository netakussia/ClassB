"""Canonical subject names derived exclusively from the configured schedule."""

from __future__ import annotations

from datetime import date, timedelta

from bot.utils.schedule_parser import WEEKDAYS, parse_schedule
from config_loader import AppConfig


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


def upcoming_subjects(start: date, config: AppConfig, school_days: int = 5) -> list[str]:
    """Subjects occurring during the next ``school_days`` days with lessons."""
    result: list[str] = []
    cursor = start
    found_days = 0
    while found_days < school_days:
        daily = subjects_for_date(cursor, config)
        if daily:
            found_days += 1
            for subject in daily:
                if subject not in result:
                    result.append(subject)
        cursor += timedelta(days=1)
    return result
