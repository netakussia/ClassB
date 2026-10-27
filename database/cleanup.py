from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import (
    AIActivityDaily,
    AIQuotaCounter,
    AIRecentResponse,
    Absence,
    HomeworkHistory,
    ScheduleOverride,
)


async def clean_old_data(
    session: AsyncSession,
    timezone_name: str,
    *,
    activity_retention_days: int = 30,
    quota_retention_days: int = 31,
) -> None:
    """Delete historical records that have exceeded their retention periods."""
    now = datetime.now(timezone.utc)
    history_cutoff = now - timedelta(days=90)
    today = datetime.now(ZoneInfo(timezone_name)).date()
    old_date_cutoff = today - timedelta(days=90)
    absence_cutoff = today - timedelta(days=30)
    activity_cutoff = today - timedelta(days=activity_retention_days - 1)
    quota_cutoff = now.replace(tzinfo=None) - timedelta(days=quota_retention_days)

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
    await session.execute(
        delete(AIActivityDaily).where(AIActivityDaily.activity_date < activity_cutoff)
    )
    await session.execute(
        delete(AIQuotaCounter).where(AIQuotaCounter.window_start < quota_cutoff)
    )
    await session.execute(
        delete(AIRecentResponse).where(
            AIRecentResponse.created_at < now.replace(tzinfo=None) - timedelta(days=1)
        )
    )
    await session.commit()