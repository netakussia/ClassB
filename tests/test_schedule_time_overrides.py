from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone
import re
from pathlib import Path
from tempfile import NamedTemporaryFile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine, inspect, text

from bot.handlers.schedule import show_next_schedule
from bot.utils.time_utils import calculate_lesson_times
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
from database.migrations import migrate_schedule_time_overrides
from database.models import ScheduleOverride, ScheduleTimeOverride, User


def _override(
    lesson_number: int | None,
    duration_minutes: int,
    big_break_minutes: int | None = None,
) -> ScheduleTimeOverride:
    return ScheduleTimeOverride(
        target_date=date(2026, 10, 26),
        lesson_number=lesson_number,
        duration_minutes=duration_minutes,
        big_break_minutes=big_break_minutes,
        created_at=datetime.now(timezone.utc),
    )


class ScheduleTimeOverrideTests(unittest.TestCase):
    def test_no_overrides_use_configured_lesson_times_and_durations(self) -> None:
        clocks = calculate_lesson_times(
            ["08:00", "09:40", "11:40", "13:20"],
            90,
        )

        self.assertEqual(
            clocks,
            [
                ("08:00", "09:30", False),
                ("09:40", "11:10", False),
                ("11:40", "13:10", False),
                ("13:20", "14:50", False),
            ],
        )

    def test_parallel_lessons_share_one_slot_clock(self) -> None:
        clocks = calculate_lesson_times(
            ["08:00", "08:00", "09:40"],
            90,
            lesson_numbers=[1, 1, 2],
        )

        self.assertEqual(clocks[0], clocks[1])
        self.assertEqual(clocks[0], ("08:00", "09:30", False))
        self.assertEqual(clocks[2], ("09:40", "11:10", False))

    def test_all_lessons_and_big_break_are_recalculated(self) -> None:
        clocks = calculate_lesson_times(
            ["08:00", "09:40", "11:40", "13:20"],
            90,
            [_override(None, 45, 15)],
        )

        self.assertEqual(
            clocks,
            [
                ("08:00", "08:45", True),
                ("08:55", "09:40", True),
                ("09:55", "10:40", True),
                ("10:50", "11:35", True),
            ],
        )

    def test_sixty_minute_lessons_place_big_break_after_second(self) -> None:
        clocks = calculate_lesson_times(
            ["08:00", "09:40", "11:40", "13:20", "15:00"],
            60,
            [_override(None, 60, 20)],
        )

        self.assertEqual(
            clocks,
            [
                ("08:00", "09:00", False),
                ("09:10", "10:10", False),
                ("10:30", "11:30", False),
                ("11:40", "12:40", False),
                ("12:50", "13:50", False),
            ],
        )

    def test_shortened_schedule_calculation(self) -> None:
        clocks = calculate_lesson_times(
            ["11:40", "13:20", "15:00"],
            60,
            [_override(None, 60, 20)],
            lesson_numbers=[3, 4, 5],
            configured_start_times=["08:00", "09:40", "11:40", "13:20", "15:00"],
        )

        self.assertEqual(
            clocks,
            [
                ("10:30", "11:30", False),
                ("11:40", "12:40", False),
                ("12:50", "13:50", False),
            ],
        )

    def test_specific_lesson_override_keeps_other_lesson_durations(self) -> None:
        clocks = calculate_lesson_times(
            ["08:00", "09:40", "11:40", "13:20"],
            90,
            [_override(2, 45, 15)],
        )
        self.assertEqual(
            clocks,
            [
                ("08:00", "09:30", False),
                ("09:40", "10:25", True),
                ("10:40", "12:10", False),
                ("12:20", "13:50", False),
            ],
        )

    def test_schedule_displays_override_times_and_warning(self) -> None:
        async def run_test() -> None:
            target_date = date(2026, 10, 26)
            lessons = [
                Lesson("08:00", "Matematica", "301", "Teacher"),
                Lesson("09:40", "Fizica", "314", "Teacher"),
                Lesson("11:40", "Engleză", "212", "Teacher"),
            ]
            day = DaySchedule(odd=lessons, even=lessons)
            config = AppConfig(
                bot=BotConfig(token="test", timezone="UTC", group_chat_id=0),
                roles=RolesConfig(owner=[], starosta=[], moderators=[]),
                students=[],
                students_path=Path("students.yaml"),
                sanctions=SanctionsConfig(ban_threshold=3, ban_days=1),
                schedule=ScheduleConfig(
                    week_parity=WeekParity(target_date, "odd"),
                    monday=day,
                    tuesday=day,
                    wednesday=day,
                    thursday=day,
                    friday=day,
                ),
            )
            overrides = [_override(None, 45, 15)]

            class Session:
                async def get(self, model, telegram_id):
                    return User(telegram_id=telegram_id) if model is User else None

                async def scalars(self, statement):
                    model = statement.column_descriptions[0]["entity"]
                    if model is ScheduleOverride:
                        return []
                    if model is ScheduleTimeOverride:
                        return overrides
                    raise AssertionError(f"Unexpected query for {model}")

            message = SimpleNamespace(
                from_user=SimpleNamespace(id=12, first_name="Test"),
                answer=AsyncMock(),
            )
            await show_next_schedule(
                message,
                config,
                Session(),
                target_date=target_date,
            )

            rendered = re.sub(
                r'<tg-emoji emoji-id="[0-9]+">(.*?)</tg-emoji>',
                r"\1",
                message.answer.await_args.args[0],
            )
            self.assertIn("⚠️ Внимание! В этот день уроки сокращены!", rendered)
            self.assertIn("1. 08:00–08:45 | Matematica (сокращённый)", rendered)
            self.assertIn("2. 08:55–09:40 | Fizica (сокращённый)", rendered)

        asyncio.run(run_test())

    def test_schedule_moves_to_next_day_after_shortened_last_lesson(self) -> None:
        async def run_test() -> None:
            today = date(2026, 10, 28)
            wednesday = [
                Lesson("11:40", "Literatura", "214", "Teacher"),
                Lesson("13:20", "Programming", "229", "Teacher"),
                Lesson("15:00", "French", "208", "Teacher"),
            ]
            early_lessons = [
                Lesson("08:00", "First", "101", "Teacher"),
                Lesson("09:40", "Second", "102", "Teacher"),
            ]
            thursday = [Lesson("08:00", "Architecture", "202", "Teacher")]
            config = AppConfig(
                bot=BotConfig(token="test", timezone="Europe/Chisinau", group_chat_id=0),
                roles=RolesConfig(owner=[], starosta=[], moderators=[]),
                students=[],
                students_path=Path("students.yaml"),
                sanctions=SanctionsConfig(ban_threshold=3, ban_days=1),
                schedule=ScheduleConfig(
                    week_parity=WeekParity(today, "odd"),
                    monday=DaySchedule(odd=early_lessons, even=early_lessons),
                    tuesday=DaySchedule(odd=early_lessons, even=early_lessons),
                    wednesday=DaySchedule(odd=wednesday, even=wednesday),
                    thursday=DaySchedule(odd=thursday, even=thursday),
                    friday=DaySchedule(odd=[], even=[]),
                ),
            )
            time_overrides = [
                ScheduleTimeOverride(
                    target_date=today,
                    lesson_number=5,
                    duration_minutes=30,
                    big_break_minutes=None,
                    created_at=datetime(2026, 10, 27, tzinfo=timezone.utc),
                )
            ]

            class Session:
                async def get(self, model, key):
                    return User(telegram_id=key) if model is User else None

                async def scalars(self, statement):
                    model = statement.column_descriptions[0]["entity"]
                    if model is ScheduleOverride:
                        return []
                    if model is ScheduleTimeOverride:
                        if today in statement.compile().params.values():
                            return time_overrides
                        return []
                    raise AssertionError(f"Unexpected query for {model}")

            message = SimpleNamespace(
                from_user=SimpleNamespace(id=12, first_name="Test"),
                answer=AsyncMock(),
            )
            fixed_now = datetime(2026, 10, 28, 15, 45, tzinfo=ZoneInfo("Europe/Chisinau"))
            with patch("bot.handlers.schedule.datetime") as mocked_datetime:
                mocked_datetime.now.return_value = fixed_now
                await show_next_schedule(message, config, Session())

            rendered = message.answer.await_args.args[0]
            self.assertIn("Четверг, 29.10", rendered)
            self.assertIn("Architecture", rendered)

        asyncio.run(run_test())

    def test_schedule_time_override_migration_is_idempotent(self) -> None:
        with NamedTemporaryFile(suffix=".sqlite") as database_file:
            engine = create_engine(f"sqlite:///{database_file.name}")
            with engine.begin() as connection:
                migrate_schedule_time_overrides(connection)
                migrate_schedule_time_overrides(connection)
                columns = {
                    column["name"]
                    for column in inspect(connection).get_columns(
                        "schedule_time_overrides"
                    )
                }
                self.assertEqual(
                    columns,
                    {
                        "id",
                        "target_date",
                        "lesson_number",
                        "duration_minutes",
                        "big_break_minutes",
                        "created_at",
                    },
                )
                connection.execute(
                    text(
                        "INSERT INTO schedule_time_overrides "
                        "(target_date, lesson_number, duration_minutes, "
                        "big_break_minutes, created_at) "
                        "VALUES ('2026-10-26', NULL, 45, 15, '2026-10-25 12:00:00')"
                    )
                )
                self.assertEqual(
                    connection.scalar(
                        text(
                            "SELECT duration_minutes FROM schedule_time_overrides "
                            "WHERE target_date = '2026-10-26'"
                        )
                    ),
                    45,
                )
            engine.dispose()


if __name__ == "__main__":
    unittest.main()
