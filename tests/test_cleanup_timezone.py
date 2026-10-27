from __future__ import annotations

from datetime import date, datetime, timezone
import unittest
from unittest.mock import AsyncMock, patch

from database.cleanup import clean_old_data
from database.models import Absence, ScheduleOverride


class _FixedDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        fixed_now = datetime(2026, 1, 1, 0, 30, tzinfo=timezone.utc)
        return fixed_now.astimezone(tz) if tz is not None else fixed_now


class CleanupTimezoneTests(unittest.IsolatedAsyncioTestCase):
    async def test_date_cutoffs_use_configured_timezone(self) -> None:
        session = AsyncMock()

        with patch("database.cleanup.datetime", _FixedDateTime):
            await clean_old_data(session, "America/Los_Angeles")

        statements = [call.args[0] for call in session.execute.await_args_list]
        cutoffs = {
            statement.table: [
                value
                for value in statement.compile().params.values()
                if isinstance(value, date) and not isinstance(value, datetime)
            ]
            for statement in statements
            if statement.table in {Absence.__table__, ScheduleOverride.__table__}
        }
        self.assertEqual(cutoffs[ScheduleOverride.__table__], [date(2025, 10, 2)])
        self.assertEqual(cutoffs[Absence.__table__], [date(2025, 12, 1)])

    async def test_ai_retention_overrides_change_cleanup_cutoffs(self) -> None:
        session = AsyncMock()

        with patch("database.cleanup.datetime", _FixedDateTime):
            await clean_old_data(
                session,
                "America/Los_Angeles",
                activity_retention_days=5,
                quota_retention_days=7,
            )

        statements = [call.args[0] for call in session.execute.await_args_list]
        activity_cutoff = next(
            value
            for value in statements
            if "DELETE FROM ai_activity_daily" in str(value)
        ).compile().params
        quota_cutoff = next(
            value
            for value in statements
            if "DELETE FROM ai_quota_counters" in str(value)
        ).compile().params
        self.assertIn(date(2025, 12, 27), activity_cutoff.values())
        self.assertIn(datetime(2025, 12, 25, 0, 30), quota_cutoff.values())


if __name__ == "__main__":
    unittest.main()
