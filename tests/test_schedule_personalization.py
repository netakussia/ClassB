from __future__ import annotations

import asyncio
from datetime import date, datetime
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from sqlalchemy import create_engine, inspect, text
import yaml

from bot.handlers.schedule import (
    _lesson_subgroup,
    _parse_target_date,
    _personalized_lessons,
    show_next_schedule,
    _weekend_easter_egg,
)
from bot.handlers.replacements import _parse_short_date
from bot.utils.time_utils import parse_day_month_date
from config_loader import (
    AppConfig,
    BotConfig,
    DaySchedule,
    Lesson,
    RolesConfig,
    SanctionsConfig,
    ScheduleConfig,
    WeekParity,
    load_config,
)
from database.migrations import migrate_users
from database.models import User


def _lesson(time: str, subject: str, subgroup: str | None = None) -> Lesson:
    return Lesson(time=time, subject=subject, room="101", teacher="Teacher", subgroup=subgroup)


def _write_test_config(directory: Path) -> None:
    config_files = {
        "bot.yaml": "token: test-token\ntimezone: Europe/Chisinau\ngroup_chat_id: 0\n",
        "roles.yaml": "owner: []\nstarosta: []\nmoderators: []\n",
        "students.yaml": (
            "students:\n"
            "  - telegram_id: 123456789\n"
            "    full_name: Иванов Иван\n"
            "    subgroup: english\n"
        ),
        "sanctions.yaml": "ban_threshold: 3\nban_days: 0\n",
    }
    for filename, content in config_files.items():
        (directory / filename).write_text(content, encoding="utf-8")

    schedule = {
        "week_parity": {
            "reference_date": "2026-09-28",
            "reference_parity": "odd",
        },
        "lesson_duration_minutes": 90,
        **{
            weekday: {"odd": [], "even": []}
            for weekday in ("monday", "tuesday", "wednesday", "thursday", "friday")
        },
    }
    (directory / "schedule.yaml").write_text(
        yaml.safe_dump(schedule, sort_keys=False),
        encoding="utf-8",
    )


class SchedulePersonalizationTests(unittest.TestCase):
    def test_language_subjects_are_classified_and_common_lessons_are_preserved(self) -> None:
        lessons = [
            _lesson("08:00", "Matematica"),
            _lesson("09:40", "L. engleză"),
            _lesson("09:40", "L. franceză"),
        ]

        english_rows = _personalized_lessons(lessons, "english")
        french_rows = _personalized_lessons(lessons, "french")

        self.assertEqual([index for index, _ in english_rows], [1, 2])
        self.assertIs(english_rows[1][1], lessons[1])
        self.assertEqual([index for index, _ in french_rows], [1, 3])
        self.assertIs(french_rows[1][1], lessons[2])
        self.assertEqual(_lesson_subgroup(lessons[1]), "english")
        self.assertEqual(_lesson_subgroup(lessons[2]), "french")

    def test_foreign_only_language_lesson_becomes_a_free_window(self) -> None:
        french_lesson = _lesson("15:00", "L. franceză")

        rows = _personalized_lessons([french_lesson], "english")

        self.assertEqual(rows, [(1, None)])

    def test_schedule_free_window_uses_non_contiguous_timetable_slot(self) -> None:
        early_lessons = [
            _lesson("08:00", "First"),
            _lesson("09:40", "Second"),
        ]
        wednesday_lessons = [
            _lesson("11:40", "Common"),
            _lesson("13:20", "L. franceză"),
            _lesson("15:00", "L. engleză"),
        ]
        config = AppConfig(
            bot=BotConfig(
                token="test-token",
                timezone="Europe/Chisinau",
                group_chat_id=0,
            ),
            roles=RolesConfig(owner=[], starosta=[], moderators=[]),
            students=[],
            students_path=Path("students.yaml"),
            sanctions=SanctionsConfig(ban_threshold=3, ban_days=0),
            schedule=ScheduleConfig(
                week_parity=WeekParity(date(2026, 9, 28), "odd"),
                monday=DaySchedule(odd=early_lessons, even=early_lessons),
                tuesday=DaySchedule(odd=[], even=[]),
                wednesday=DaySchedule(
                    odd=wednesday_lessons,
                    even=wednesday_lessons,
                ),
                thursday=DaySchedule(odd=[], even=[]),
                friday=DaySchedule(odd=[], even=[]),
            ),
        )

        class Session:
            async def get(self, model, key):
                return User(telegram_id=key, subgroup="english") if model is User else None

            async def scalars(self, statement):
                return []

        message = AsyncMock()
        with patch(
            "bot.handlers.schedule.get_configured_week_parity",
            new=AsyncMock(return_value="odd"),
        ):
            asyncio.run(
                show_next_schedule(
                    message,
                    config,
                    Session(),
                    target_date=date(2026, 10, 28),
                    telegram_id=123,
                    display_name="Student",
                )
            )

        text = message.answer.await_args.args[0]
        self.assertIn("Свободное окно", text)
        self.assertIn("13:20", text)

    def test_explicit_subgroup_marker_is_supported(self) -> None:
        lesson = _lesson("11:40", "Language", "french")
        self.assertEqual(_lesson_subgroup(lesson), "french")

    def test_schedule_date_formats_and_weekend_boundary(self) -> None:
        self.assertEqual(_parse_target_date("2026-10-05").isoformat(), "2026-10-05")
        self.assertEqual(_parse_target_date("05.10.2026").isoformat(), "2026-10-05")
        with self.assertRaisesRegex(ValueError, "Укажите дату"):
            _parse_target_date("31.02.2026")
        self.assertTrue(_weekend_easter_egg(datetime(2026, 10, 2, 16, 0)))
        self.assertFalse(_weekend_easter_egg(datetime(2026, 10, 2, 15, 59)))
        self.assertTrue(_weekend_easter_egg(datetime(2026, 10, 3, 9, 0)))

    def test_short_date_parser_finds_next_leap_year(self) -> None:
        self.assertEqual(
            parse_day_month_date("29.02", datetime(2025, 3, 1).date()),
            datetime(2028, 2, 29).date(),
        )
        self.assertEqual(
            _parse_short_date("29.02", datetime(2024, 3, 1).date()),
            datetime(2028, 2, 29).date(),
        )
        self.assertIsNone(
            parse_day_month_date("31.02", datetime(2025, 1, 1).date())
        )

    def test_bot_token_can_be_overridden_by_environment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_dir = Path(directory)
            _write_test_config(config_dir)
            with patch.dict("os.environ", {"BOT_TOKEN": "environment-test-token"}):
                self.assertEqual(
                    load_config(config_dir).bot.token,
                    "environment-test-token",
                )

    def test_students_roster_is_loaded_with_authoritative_profile_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_dir = Path(directory)
            _write_test_config(config_dir)
            with patch.dict("os.environ", {"BOT_TOKEN": "roster-test-token"}):
                students = load_config(config_dir).students

        self.assertEqual(students[0].telegram_id, 123456789)
        self.assertEqual(students[0].full_name, "Иванов Иван")
        self.assertEqual(students[0].subgroup, "english")

    def test_migrate_users_adds_nullable_subgroup_to_existing_table(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".sqlite") as database_file:
            engine = create_engine(f"sqlite:///{database_file.name}")
            with engine.begin() as connection:
                connection.execute(text("CREATE TABLE users (telegram_id BIGINT PRIMARY KEY)"))
                connection.execute(text("INSERT INTO users (telegram_id) VALUES (123)"))

                migrate_users(connection)
                migrate_users(connection)

                columns = {column["name"]: column for column in inspect(connection).get_columns("users")}
                self.assertIn("subgroup", columns)
                self.assertIn("last_action", columns)
                self.assertIn("last_active_at", columns)
                self.assertIn("is_banned", columns)
                self.assertTrue(columns["subgroup"]["nullable"])
                self.assertTrue(columns["last_action"]["nullable"])
                self.assertTrue(columns["last_active_at"]["nullable"])
                self.assertFalse(columns["is_banned"]["nullable"])
                self.assertEqual(
                    connection.scalar(text("SELECT telegram_id FROM users")),
                    123,
                )
                self.assertEqual(
                    connection.scalar(text("SELECT is_banned FROM users")),
                    0,
                )


if __name__ == "__main__":
    unittest.main()
