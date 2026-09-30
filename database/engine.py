from __future__ import annotations

import os
from pathlib import Path

from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from database.migrations import (
    migrate_homework_history,
    migrate_homework_subject_uniqueness,
    migrate_homework_timestamps,
    migrate_homework_target_date,
    migrate_users,
)
from database.models import Base


DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

DEFAULT_DATABASE_URL = URL.create(
    drivername="sqlite+aiosqlite",
    database=str(DATA_DIR / "bot.db"),
)
DATABASE_URL = os.getenv("DATABASE_URL") or DEFAULT_DATABASE_URL
async_engine: AsyncEngine = create_async_engine(DATABASE_URL, echo=False)


async def init_db() -> None:
    """Create all tables declared in the SQLAlchemy metadata."""
    async with async_engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        await connection.run_sync(migrate_homework_history)
        await connection.run_sync(migrate_homework_timestamps)
        await connection.run_sync(migrate_homework_target_date)
        await connection.run_sync(migrate_homework_subject_uniqueness)
        await connection.run_sync(migrate_users)
