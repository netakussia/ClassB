from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


SCHOOL_DAY_START_CUTOFF = time(15, 0)


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
