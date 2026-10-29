"""Compatibility helpers for the typed schedule loaded by ``config_loader``."""

from datetime import time
from typing import Iterable

from config_loader import Lesson, ScheduleConfig


WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday")


def parse_schedule(schedule_config: ScheduleConfig) -> ScheduleConfig:
    """Return the schedule already validated by :func:`config_loader.load_config`."""
    if not isinstance(schedule_config, ScheduleConfig):
        raise ValueError("Expected a ScheduleConfig instance")
    return schedule_config


def _lesson_start_time(value: str) -> time:
    start_text = value.strip().replace("–", "-").split("-", maxsplit=1)[0].strip()
    try:
        return time.fromisoformat(start_text)
    except ValueError as exc:
        raise ValueError(f"Invalid lesson start time: {value}") from exc


def configured_lesson_start_times(schedule_config: ScheduleConfig) -> list[str]:
    """Return all configured timetable slots in chronological order."""
    starts = sorted(
        {
            _lesson_start_time(lesson.time)
            for weekday in WEEKDAYS
            for parity in ("odd", "even")
            for lesson in schedule_config.lessons_for(weekday, parity)
        }
    )
    return [start.isoformat() for start in starts]


def lesson_numbers_for_day(
    lessons: Iterable[Lesson],
    schedule_config: ScheduleConfig,
) -> list[int]:
    """Map listed lessons to their numbered timetable slots by configured start time."""
    lesson_rows = list(lessons)
    all_start_times = [
        _lesson_start_time(value)
        for value in configured_lesson_start_times(schedule_config)
    ]
    slot_by_time = {
        start_time: index
        for index, start_time in enumerate(all_start_times, start=1)
    }
    return [slot_by_time[_lesson_start_time(lesson.time)] for lesson in lesson_rows]
