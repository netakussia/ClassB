from __future__ import annotations

from datetime import date, datetime, timezone
import unittest

from aiogram.types import Chat, Message, User as TelegramUser
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from bot.middlewares.access import record_user_activity
from database.models import User


class ActivityMiddlewareTests(unittest.TestCase):
    def test_activity_is_persisted_without_resetting_existing_user_data(self) -> None:
        engine = create_engine("sqlite:///:memory:")
        User.__table__.create(engine)
        try:
            with Session(engine) as session:
                session.add(
                    User(
                        telegram_id=123,
                        full_name="Student Name",
                        username="student",
                        subgroup="english",
                        false_absences_count=2,
                        last_action="Previous action",
                    )
                )
                session.commit()

                message = Message(
                    message_id=1,
                    date=datetime.now(timezone.utc),
                    chat=Chat(id=123, type="private"),
                    from_user=TelegramUser(
                        id=123,
                        is_bot=False,
                        first_name="Student",
                        username="student",
                    ),
                    text="/schedule 2026-10-23",
                )
                started_at = datetime.now(timezone.utc)
                user = session.get(User, 123)
                record_user_activity(user, message)
                session.commit()
                finished_at = datetime.now(timezone.utc)
                session.expire_all()
                stored_user = session.get(User, 123)

                self.assertEqual(stored_user.last_action, "Просмотр расписания")
                self.assertIsNotNone(stored_user.last_active_at)
                active_at = stored_user.last_active_at
                if active_at.tzinfo is None:
                    active_at = active_at.replace(tzinfo=timezone.utc)
                self.assertLessEqual(started_at, active_at)
                self.assertLessEqual(active_at, finished_at)
                self.assertEqual(stored_user.full_name, "Student Name")
                self.assertEqual(stored_user.subgroup, "english")
                self.assertEqual(stored_user.false_absences_count, 2)
        finally:
            engine.dispose()


if __name__ == "__main__":
    unittest.main()