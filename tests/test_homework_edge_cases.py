from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo
import unittest

from bot.handlers.hw import (
    TELEGRAM_TEXT_LIMIT,
    _next_lesson_date,
    _subjects_for_homework_date,
    _updated_label,
)
from bot.utils.homework_dates import homework_target_date
from bot.utils.time_utils import get_week_parity
from config_loader import load_config
from database.models import ScheduleOverride


class _ScalarResult:
    def __init__(self, values: list[object]) -> None:
        self._values = values

    def __iter__(self):
        return iter(self._values)


class _Session:
    def __init__(self, values: list[object]) -> None:
        self.values = values
        self.calls = 0

    async def scalars(self, _query):
        self.calls += 1
        return _ScalarResult(self.values)


class HomeworkEdgeCaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()

    def test_updated_timestamp_is_shown_in_configured_timezone(self) -> None:
        label = _updated_label(datetime(2026, 9, 27, 12, 30, tzinfo=timezone.utc), "Europe/Chisinau")
        self.assertEqual(label, "27.09.2026 в 15:30")

    def test_replacement_subject_is_used_for_homework_day(self) -> None:
        target = date(2026, 9, 28)  # Monday
        old_override = ScheduleOverride(
            target_date=target,
            lesson_number=1,
            override_type="lesson",
            new_subject="Старый предмет",
            new_room="101",
            new_teacher=None,
            created_by=1,
            created_at=datetime.now(timezone.utc),
        )
        new_override = ScheduleOverride(
            target_date=target,
            lesson_number=1,
            override_type="lesson",
            new_subject="Тестовый предмет",
            new_room="101",
            new_teacher=None,
            created_by=1,
            created_at=datetime.now(timezone.utc),
        )
        subjects = asyncio.run(_subjects_for_homework_date(_Session([old_override, new_override]), target, self.config))
        self.assertEqual(subjects[0], "Тестовый предмет")

    def test_unknown_subject_stops_search_after_bounded_window(self) -> None:
        session = _Session([])
        result = asyncio.run(_next_lesson_date(session, "Несуществующий предмет", self.config, date(2026, 9, 26)))
        self.assertIsNone(result)
        self.assertEqual(session.calls, 35)

    def test_week_parity_for_reference_monday_and_thursday(self) -> None:
        self.assertEqual(
            get_week_parity(date(2026, 10, 1), date(2026, 9, 28), "even"),
            "even",
        )

    def test_homework_target_switches_at_last_lesson_end(self) -> None:
        session = _Session([])
        before_end = datetime(2026, 9, 28, 14, 49, tzinfo=ZoneInfo("Europe/Chisinau"))
        at_end = datetime(2026, 9, 28, 14, 50, tzinfo=ZoneInfo("Europe/Chisinau"))

        today_target = asyncio.run(homework_target_date(session, self.config, before_end))
        next_target = asyncio.run(homework_target_date(session, self.config, at_end))

        self.assertEqual(today_target, date(2026, 9, 28))
        self.assertEqual(next_target, date(2026, 9, 29))

    def test_telegram_limit_is_realistic_for_long_homework(self) -> None:
        self.assertGreater(TELEGRAM_TEXT_LIMIT, 3500)
        self.assertLess(TELEGRAM_TEXT_LIMIT, 4097)


if __name__ == "__main__":
    unittest.main()
