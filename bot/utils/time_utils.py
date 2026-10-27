from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Iterable, NamedTuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy.ext.asyncio import AsyncSession

from config_loader import AppConfig
from database.models import BotSetting, ScheduleTimeOverride


SCHOOL_DAY_START_CUTOFF = time(15, 0)
WEEK_PARITY_DATE_KEY = "week_parity.reference_date"
WEEK_PARITY_VALUE_KEY = "week_parity.reference_parity"


class LessonClock(NamedTuple):
    start: str
    end: str
    shortened: bool


def calculate_lesson_times(
    lesson_start_times: Iterable[str],
    default_duration_minutes: int,
    overrides: Iterable[ScheduleTimeOverride] = (),
    *,
    lesson_numbers: Iterable[int] | None = None,
) -> list[LessonClock]:
    """Calculate lesson start/end times while preserving the configured breaks."""
    if default_duration_minutes <= 0:
        raise ValueError("default_duration_minutes must be positive")

    starts: list[datetime] = []
    for value in lesson_start_times:
        start_text = value.strip().split("–", maxsplit=1)[0].split("-", maxsplit=1)[0].strip()
        try:
            starts.append(datetime.combine(date.min, time.fromisoformat(start_text)))
        except ValueError as exc:
            raise ValueError(f"Invalid lesson start time: {value}") from exc

    slot_numbers = list(lesson_numbers) if lesson_numbers is not None else list(
        range(1, len(starts) + 1)
    )
    if len(slot_numbers) != len(starts) or any(number <= 0 for number in slot_numbers):
        raise ValueError("lesson_numbers must contain one positive slot number per lesson")

    start_by_slot: dict[int, datetime] = {}
    unique_slots: list[int] = []
    unique_starts: list[datetime] = []
    for slot, start in zip(slot_numbers, starts):
        if slot in start_by_slot:
            if start_by_slot[slot] != start:
                raise ValueError("A timetable slot cannot have multiple start times")
            continue
        start_by_slot[slot] = start
        unique_slots.append(slot)
        unique_starts.append(start)

    override_rows = list(overrides)
    global_override = next(
        (row for row in reversed(override_rows) if row.lesson_number is None),
        None,
    )
    specific_overrides: dict[int, ScheduleTimeOverride] = {}
    for row in override_rows:
        if row.lesson_number is not None:
            specific_overrides[row.lesson_number] = row

    default_duration = timedelta(minutes=default_duration_minutes)
    durations: list[timedelta] = []
    shortened: list[bool] = []
    for lesson_number in unique_slots:
        specific_override = specific_overrides.get(lesson_number)
        duration_minutes = (
            specific_override.duration_minutes
            if specific_override is not None
            else global_override.duration_minutes
            if global_override is not None
            else default_duration_minutes
        )
        if duration_minutes <= 0:
            raise ValueError("Lesson duration overrides must be positive")
        durations.append(timedelta(minutes=duration_minutes))
        shortened.append(duration_minutes < default_duration_minutes)

    big_break_minutes = next(
        (
            row.big_break_minutes
            for row in reversed(override_rows)
            if row.big_break_minutes is not None
        ),
        None,
    )
    gaps = [
        max(
            timedelta(0),
            unique_starts[index + 1] - unique_starts[index] - default_duration,
        )
        for index in range(max(0, len(unique_starts) - 1))
    ]
    if big_break_minutes is not None:
        if big_break_minutes < 0:
            raise ValueError("Big break duration cannot be negative")
        if len(gaps) >= 2:
            gaps[1] = timedelta(minutes=big_break_minutes)

    result: list[LessonClock] = []
    current_start = unique_starts[0] if unique_starts else None
    for index, duration in enumerate(durations):
        assert current_start is not None
        current_end = current_start + duration
        result.append(
            LessonClock(
                current_start.strftime("%H:%M"),
                current_end.strftime("%H:%M"),
                shortened[index],
            )
        )
        if index + 1 < len(durations):
            current_start = current_end + gaps[index]
    clock_by_slot = dict(zip(unique_slots, result))
    return [clock_by_slot[slot] for slot in slot_numbers]


def get_next_school_day(timezone: str) -> date:
    """Return the school date selected by the local 15:00 cutoff.

    Monday through Thursday, dates before 15:00 are the current date and
    dates at or after 15:00 are the next date. Friday before 15:00 is Friday;
    Friday at or after the cutoff and weekends resolve to the next Monday.
    """
    try:
        now = datetime.now(ZoneInfo(timezone))
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"Unknown IANA timezone: {timezone}") from exc

    today = now.date()
    weekday = today.weekday()  # Monday=0 ... Sunday=6

    if weekday >= 5:
        return today + timedelta(days=(7 - weekday))

    if weekday == 4:
        return today if now.time() < SCHOOL_DAY_START_CUTOFF else today + timedelta(days=3)

    if now.time() < SCHOOL_DAY_START_CUTOFF:
        return today
    return today + timedelta(days=1)


def parse_day_month_date(value: str, today: date) -> date | None:
    """Parse DD.MM as its next occurrence, including the next leap year."""
    try:
        day_text, month_text = value.split(".")
        if (
            len(day_text) != 2
            or len(month_text) != 2
            or not day_text.isascii()
            or not month_text.isascii()
            or not day_text.isdigit()
            or not month_text.isdigit()
        ):
            return None
        day, month = int(day_text), int(month_text)
    except ValueError:
        return None

    for year in range(today.year, today.year + 9):
        try:
            candidate = date(year, month, day)
        except ValueError:
            continue
        if candidate >= today:
            return candidate
    return None


def get_week_parity(target_date: date, ref_date: date, ref_parity: str) -> str:
    """Calculate parity from the Mondays of the target and reference weeks."""
    if ref_parity not in {"even", "odd"}:
        raise ValueError("ref_parity must be either 'even' or 'odd'")

    target_monday = target_date - timedelta(days=target_date.weekday())
    reference_monday = ref_date - timedelta(days=ref_date.weekday())
    weeks_from_reference = (target_monday - reference_monday).days // 7
    if weeks_from_reference % 2 == 0:
        return ref_parity
    return "odd" if ref_parity == "even" else "even"


async def get_configured_week_parity(
    session: AsyncSession,
    target_date: date,
    config: AppConfig,
) -> str:
    """Use the administrator's saved reference, falling back to legacy YAML."""
    get_setting = getattr(session, "get", None)
    if get_setting is None:
        return config.schedule.get_parity(target_date)

    saved_date = await get_setting(BotSetting, WEEK_PARITY_DATE_KEY)
    saved_parity = await get_setting(BotSetting, WEEK_PARITY_VALUE_KEY)
    if saved_date is None or saved_parity is None:
        return config.schedule.get_parity(target_date)

    try:
        reference_date = date.fromisoformat(saved_date.value)
    except (TypeError, ValueError):
        return config.schedule.get_parity(target_date)
    if saved_parity.value not in {"odd", "even"}:
        return config.schedule.get_parity(target_date)
    return get_week_parity(target_date, reference_date, saved_parity.value)
