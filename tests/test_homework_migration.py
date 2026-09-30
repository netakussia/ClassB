from __future__ import annotations

from datetime import datetime, timezone
import tempfile
import unittest

from sqlalchemy import create_engine, text

from database.migrations import (
    migrate_homework_history,
    migrate_homework_subject_uniqueness,
    migrate_homework_target_date,
    migrate_homework_timestamps,
)


class HomeworkMigrationTests(unittest.TestCase):
    def test_missing_target_date_is_backfilled_from_creation_date(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".sqlite") as database_file:
            engine = create_engine(f"sqlite:///{database_file.name}")
            with engine.begin() as connection:
                connection.execute(text("""
                    CREATE TABLE homework (
                        id INTEGER PRIMARY KEY, subject VARCHAR NOT NULL, text TEXT,
                        attachments JSON NOT NULL, is_deadline BOOLEAN NOT NULL,
                        deadline_date DATE, updated_by BIGINT NOT NULL,
                        created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL
                    )
                """))
                connection.execute(text("""
                    INSERT INTO homework VALUES
                    (1, 'Math', 'task', '[]', 0, NULL, 10,
                     '2026-09-30 10:00:00', '2026-09-30 10:00:00')
                """))

                migrate_homework_target_date(connection)

                target_date = connection.scalar(text("SELECT target_date FROM homework WHERE id = 1"))
                self.assertEqual(target_date, "2026-09-30")

    def test_duplicate_subject_dates_become_history_and_other_dates_are_allowed(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".sqlite") as database_file:
            engine = create_engine(f"sqlite:///{database_file.name}")
            with engine.begin() as connection:
                connection.execute(text("""
                    CREATE TABLE homework (
                        id INTEGER PRIMARY KEY, subject VARCHAR NOT NULL, text TEXT,
                        attachments JSON NOT NULL, is_deadline BOOLEAN NOT NULL,
                        deadline_date DATE, target_date DATE NOT NULL,
                        updated_by BIGINT NOT NULL, updated_at DATETIME NOT NULL
                    )
                """))
                connection.execute(text("""
                    CREATE TABLE homework_history (
                        id INTEGER PRIMARY KEY, homework_id INTEGER NOT NULL,
                        previous_text TEXT, previous_attachments JSON,
                        previous_is_deadline BOOLEAN NOT NULL,
                        previous_deadline_date DATE, previous_target_date DATE,
                        changed_by BIGINT NOT NULL,
                        changed_at DATETIME NOT NULL
                    )
                """))
                connection.execute(text("""
                    INSERT INTO homework VALUES
                    (1, 'Math', 'old', '[]', 0, NULL, '2026-01-02', 10, '2026-01-01 10:00:00'),
                    (2, 'Math', 'new', '[]', 0, NULL, '2026-01-02', 11, '2026-01-02 10:00:00')
                """))

                migrate_homework_history(connection)
                migrate_homework_timestamps(connection)
                migrate_homework_target_date(connection)
                migrate_homework_subject_uniqueness(connection)

                current = connection.execute(text("SELECT id, text FROM homework")).fetchall()
                history = connection.execute(text("SELECT homework_id, previous_text, previous_target_date FROM homework_history")).fetchall()
                self.assertEqual(current, [(2, "new")])
                self.assertEqual(history, [(2, "old", "2026-01-02")])
                connection.execute(text("""
                    INSERT INTO homework (id, subject, text, attachments, target_date, is_deadline, updated_by, created_at, updated_at)
                    VALUES (3, 'Math', 'next lesson', '[]', '2026-01-09', 0, 1, '2026-01-03', '2026-01-03')
                """))
                with self.assertRaises(Exception):
                    connection.execute(text("""
                        INSERT INTO homework (id, subject, text, attachments, target_date, is_deadline, updated_by, created_at, updated_at)
                        VALUES (4, 'Math', 'duplicate', '[]', '2026-01-02', 0, 1, '2026-01-03', '2026-01-03')
                    """))


if __name__ == "__main__":
    unittest.main()
