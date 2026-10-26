from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo
import unittest

from bot.handlers.hw import (
    TELEGRAM_TEXT_LIMIT,
    _next_lesson_date,
    _subjects_for_homework_date,
    _updated_label,
)
from bot.utils.homework_dates import homework_target_date
from bot.utils.homework_dates import subjects_for_date
from bot.utils.time_utils import get_configured_week_parity, get_week_parity
from config_loader import (
    AppConfig,
    BotConfig,
    DaySchedule,
    Lesson,
    RolesConfig,
    SanctionsConfig,
    ScheduleConfig,
    WeekParity,
)
from database.models import BotSetting, ScheduleOverride, ScheduleTimeOverride


def _test_config() -> AppConfig:
    monday_lessons = [
        Lesson("08:00", "Первый предмет", "101", "Преподаватель"),
        Lesson("09:40", "Второй предмет", "102", "Преподаватель"),
        Lesson("11:40", "Третий предмет", "103", "Преподаватель"),
        Lesson("13:20", "Четвёртый предмет", "104", "Преподаватель"),
    ]
    empty_day = DaySchedule(odd=[], even=[])
    return AppConfig(
        bot=BotConfig("test-token", "Europe/Chisinau", 0),
        roles=RolesConfig(owner=[], starosta=[], moderators=[]),
        students=[],
        students_path=Path("unused-students.yaml"),
        sanctions=SanctionsConfig(ban_threshold=3, ban_days=0),
        schedule=ScheduleConfig(
            week_parity=WeekParity(date(2026, 9, 28), "odd"),
            monday=DaySchedule(odd=monday_lessons, even=monday_lessons),
            tuesday=DaySchedule(odd=monday_lessons, even=monday_lessons),
            wednesday=empty_day,
            thursday=empty_day,
            friday=empty_day,
        ),
    )


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
        self.config = _test_config()

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

    def test_homework_target_uses_shortened_last_lesson_end(self) -> None:
        class Session:
            async def scalars(self, statement):
                model = statement.column_descriptions[0]["entity"]
                if model is ScheduleTimeOverride:
                    return [
                        ScheduleTimeOverride(
                            target_date=date(2026, 9, 28),
                            lesson_number=4,
                            duration_minutes=30,
                            big_break_minutes=None,
                            created_at=datetime(2026, 9, 27, tzinfo=timezone.utc),
                        )
                    ]
                return []

        session = Session()
        before_end = datetime(2026, 9, 28, 13, 49, tzinfo=ZoneInfo("Europe/Chisinau"))
        at_end = datetime(2026, 9, 28, 13, 50, tzinfo=ZoneInfo("Europe/Chisinau"))

        today_target = asyncio.run(homework_target_date(session, self.config, before_end))
        next_target = asyncio.run(homework_target_date(session, self.config, at_end))

        self.assertEqual(today_target, date(2026, 9, 28))
        self.assertEqual(next_target, date(2026, 9, 29))

    def test_telegram_limit_is_realistic_for_long_homework(self) -> None:
        self.assertGreater(TELEGRAM_TEXT_LIMIT, 3500)
        self.assertLess(TELEGRAM_TEXT_LIMIT, 4097)


class ScheduleOverrideBehaviorTests(unittest.TestCase):
    def test_saved_week_parity_is_used_instead_of_yaml_reference(self) -> None:
        class StoredSettings:
            async def get(self, model, key):
                values = {
                    "week_parity.reference_date": BotSetting(
                        key="week_parity.reference_date", value="2026-10-19"
                    ),
                    "week_parity.reference_parity": BotSetting(
                        key="week_parity.reference_parity", value="odd"
                    ),
                }
                return values.get(key)

        schedule = ScheduleConfig(
            week_parity=WeekParity(date(2026, 9, 28), "even"),
            monday=DaySchedule(odd=[], even=[]),
            tuesday=DaySchedule(odd=[], even=[]),
            wednesday=DaySchedule(odd=[], even=[]),
            thursday=DaySchedule(odd=[], even=[]),
            friday=DaySchedule(odd=[], even=[]),
        )
        parity = asyncio.run(
            get_configured_week_parity(
                StoredSettings(), date(2026, 10, 26), SimpleNamespace(schedule=schedule)
            )
        )

        self.assertEqual(parity, "even")

    def test_cancelled_lesson_is_ignored_and_free_slot_replacement_is_included(self) -> None:
        class Session(_Session):
            pass

        schedule = ScheduleConfig(
            week_parity=WeekParity(date(2026, 10, 19), "odd"),
            monday=DaySchedule(
                odd=[Lesson("08:00", "Cancelled subject", "101", "Teacher")],
                even=[],
            ),
            tuesday=DaySchedule(odd=[], even=[]),
            wednesday=DaySchedule(odd=[], even=[]),
            thursday=DaySchedule(odd=[], even=[]),
            friday=DaySchedule(odd=[], even=[]),
        )
        target = date(2026, 10, 19)
        cancelled = ScheduleOverride(
            target_date=target,
            lesson_number=1,
            override_type="lesson",
            new_subject=None,
            new_room=None,
            new_teacher=None,
            is_cancelled=True,
            created_by=1,
            created_at=datetime.now(timezone.utc),
        )
        free_slot = ScheduleOverride(
            target_date=target,
            lesson_number=3,
            override_type="lesson",
            new_subject="Added subject",
            new_room="101",
            new_teacher=None,
            created_by=1,
            created_at=datetime.now(timezone.utc),
        )

        subjects = asyncio.run(
            subjects_for_date(
                Session([cancelled, free_slot]), target, SimpleNamespace(schedule=schedule)
            )
        )

        self.assertEqual(subjects, ["Added subject"])


if __name__ == "__main__":
    unittest.main()
