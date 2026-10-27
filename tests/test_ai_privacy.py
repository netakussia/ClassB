from __future__ import annotations

import asyncio
from pathlib import Path
import tempfile
import unittest

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from bot.ai.personalization import reset_personalization
from bot.middlewares.access import _private_activity_category
from database.models import (
    AIActivityDaily,
    AIQuotaCounter,
    AIRecentResponse,
    AIUserSettings,
    Base,
    User,
)
from aiogram.types import Chat, Message, User as TelegramUser
from datetime import datetime, timezone


class AIPrivacyTests(unittest.TestCase):
    def _message(self, **kwargs) -> Message:
        return Message.model_construct(
            message_id=1,
            date=datetime.now(timezone.utc),
            chat=Chat.model_construct(id=4, type="private"),
            from_user=TelegramUser.model_construct(id=4, is_bot=False, first_name="A"),
            **kwargs,
        )

    def test_activity_classifier_only_returns_allowlisted_sections(self) -> None:
        message = self._message(text="secret: please remember all my messages")
        self.assertIsNone(_private_activity_category(message))

    def test_activity_classifier_ignores_messages_without_text(self) -> None:
        self.assertIsNone(_private_activity_category(self._message()))

    def test_activity_classifier_ignores_empty_and_whitespace_text(self) -> None:
        for text in ("", "   \t\n"):
            with self.subTest(text=repr(text)):
                self.assertIsNone(_private_activity_category(self._message(text=text)))

    def test_activity_classifier_keeps_allowlisted_text_categories(self) -> None:
        self.assertEqual(
            _private_activity_category(self._message(text="покажи расписание")),
            "schedule",
        )

    def test_reset_clears_preferences_but_not_quota_counters(self) -> None:
        async def test() -> None:
            with tempfile.TemporaryDirectory() as directory:
                engine = create_async_engine(
                    f"sqlite+aiosqlite:///{Path(directory) / 'privacy.db'}"
                )
                factory = async_sessionmaker(engine, expire_on_commit=False)
                async with engine.begin() as connection:
                    await connection.run_sync(Base.metadata.create_all)
                async with factory() as session:
                    session.add(
                        AIUserSettings(
                            telegram_id=4,
                            gemini_enabled=True,
                            humor_enabled=True,
                            personalization_enabled=True,
                            reactions_enabled=True,
                        )
                    )
                    session.add(
                        AIUserSettings(
                            telegram_id=5,
                            gemini_enabled=True,
                            personalization_enabled=True,
                        )
                    )
                    session.add(User(telegram_id=4, full_name="Private name"))
                    session.add(
                        AIActivityDaily(
                            telegram_id=4,
                            activity_date=datetime.now().date(),
                            category="homework",
                            count=3,
                        )
                    )
                    session.add(
                        AIQuotaCounter(
                            scope="user",
                            scope_id="4",
                            window="minute",
                            window_start=datetime(2026, 10, 27),
                            count=1,
                        )
                    )
                    session.add(
                        AIRecentResponse(
                            telegram_id=4,
                            response_hash="hash",
                            created_at=datetime.now(),
                        )
                    )
                    await session.commit()
                    await reset_personalization(session, 4)
                    await session.commit()
                    settings = await session.get(AIUserSettings, 4)
                    self.assertFalse(settings.gemini_enabled)
                    self.assertFalse(settings.humor_enabled)
                    self.assertFalse(settings.personalization_enabled)
                    self.assertFalse(settings.reactions_enabled)
                    other_settings = await session.get(AIUserSettings, 5)
                    self.assertTrue(other_settings.gemini_enabled)
                    user = await session.get(User, 4)
                    self.assertEqual(user.full_name, "Private name")
                    activity_count = await session.scalar(
                        select(func.count()).select_from(AIActivityDaily)
                    )
                    quota_count = await session.scalar(
                        select(func.count()).select_from(AIQuotaCounter)
                    )
                    repeat_count = await session.scalar(
                        select(func.count()).select_from(AIRecentResponse)
                    )
                    self.assertEqual(activity_count, 0)
                    self.assertEqual(quota_count, 1)
                    self.assertEqual(repeat_count, 0)
                await engine.dispose()

        asyncio.run(test())


if __name__ == "__main__":
    unittest.main()
