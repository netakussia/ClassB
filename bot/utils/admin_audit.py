from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from database.models import AdminActionLog


async def log_admin_action(
    session: AsyncSession,
    admin_id: int,
    action: str,
    details: str = "",
) -> None:
    """Add an audit record to the current transaction."""
    session.add(
        AdminActionLog(
            admin_id=admin_id,
            action=action,
            details=details,
            created_at=datetime.now(timezone.utc),
        )
    )
