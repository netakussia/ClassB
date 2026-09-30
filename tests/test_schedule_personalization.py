from __future__ import annotations

from datetime import datetime
import tempfile
import unittest
from unittest.mock import patch

from sqlalchemy import create_engine, inspect, text

from bot.handlers.schedule import (
    _lesson_subgroup,
    _parse_target_date,
    _personalized_lessons,
    _weekend_easter_egg,
)
from config_loader import Lesson, load_config
from database.migrations import migrate_users


def _lesson(time: str, subject: str, subgroup: str | None = None) -> Lesson:
    return Lesson(time=time, subject=subject, room="101", teacher="Teacher", subgroup=subgroup)


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

    def test_explicit_subgroup_marker_is_supported(self) -> None:
        lesson = _lesson("11:40", "Language", "french")
        self.assertEqual(_lesson_subgroup(lesson), "french")

    def test_schedule_date_formats_and_weekend_boundary(self) -> None:
        self.assertEqual(_parse_target_date("2026-10-05").isoformat(), "2026-10-05")
        self.assertEqual(_parse_target_date("05.10.2026").isoformat(), "2026-10-05")
        self.assertTrue(_weekend_easter_egg(datetime(2026, 10, 2, 16, 0)))
        self.assertFalse(_weekend_easter_egg(datetime(2026, 10, 2, 15, 59)))
        self.assertTrue(_weekend_easter_egg(datetime(2026, 10, 3, 9, 0)))

    def test_bot_token_can_be_overridden_by_environment(self) -> None:
        with patch.dict("os.environ", {"BOT_TOKEN": "test-token"}):
            self.assertEqual(load_config().bot.token, "test-token")

    def test_students_roster_is_loaded_with_authoritative_profile_fields(self) -> None:
        students = load_config().students

        self.assertEqual(students[0].telegram_id, 123456789)
        self.assertEqual(students[0].full_name, "Иванов Иван")
        self.assertEqual(students[0].subgroup, "english")

    def test_migrate_users_adds_nullable_subgroup_to_existing_table(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".sqlite") as database_file:
            engine = create_engine(f"sqlite:///{database_file.name}")
            with engine.begin() as connection:
                connection.execute(text("CREATE TABLE users (telegram_id BIGINT PRIMARY KEY)"))

                migrate_users(connection)

                columns = {column["name"]: column for column in inspect(connection).get_columns("users")}
                self.assertIn("subgroup", columns)
                self.assertTrue(columns["subgroup"]["nullable"])


if __name__ == "__main__":
    unittest.main()
