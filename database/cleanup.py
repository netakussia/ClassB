from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import Absence, HomeworkHistory, ScheduleOverride


async def clean_old_data(session: AsyncSession, timezone_name: str) -> None:
    """Delete historical records that have exceeded their retention periods."""
    now = datetime.now(timezone.utc)
    history_cutoff = now - timedelta(days=90)
    today = datetime.now(ZoneInfo(timezone_name)).date()
    old_date_cutoff = today - timedelta(days=90)
    absence_cutoff = today - timedelta(days=30)

    await session.execute(
        delete(HomeworkHistory).where(HomeworkHistory.changed_at < history_cutoff)
    )
    await session.execute(
        delete(ScheduleOverride).where(ScheduleOverride.target_date < old_date_cutoff)
    )
    await session.execute(
        delete(Absence).where(
            Absence.target_date < absence_cutoff,
            Absence.status.in_(("completed", "false")),
        )
    )
    await session.commit()