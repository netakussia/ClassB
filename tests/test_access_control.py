from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from aiogram.types import CallbackQuery, Chat, Message, User as TelegramUser
from sqlalchemy.ext.asyncio import AsyncSession

from bot.middlewares.access import AccessMiddleware, BAN_DENIED_TEXT, ROLE_DENIED_TEXT
from config_loader import (
    AppConfig,
    BotConfig,
    DaySchedule,
    RolesConfig,
    SanctionsConfig,
    ScheduleConfig,
    StudentConfig,
    WeekParity,
)
from database.models import DynamicRole, User


def _config() -> AppConfig:
    empty_day = DaySchedule(odd=[], even=[])
    return AppConfig(
        bot=BotConfig(token="test", timezone="UTC", group_chat_id=0),
        roles=RolesConfig(owner=[1], starosta=[2], moderators=[3]),
        students=[StudentConfig(telegram_id=4, full_name="Student", subgroup=None)],
        students_path=Path("students.yaml"),
        sanctions=SanctionsConfig(ban_threshold=3, ban_days=1),
        schedule=ScheduleConfig(
            week_parity=WeekParity(date(2026, 10, 26), "odd"),
            monday=empty_day,
            tuesday=empty_day,
            wednesday=empty_day,
            thursday=empty_day,
            friday=empty_day,
        ),
    )


def _message(telegram_id: int, text: str) -> Message:
    return Message.model_construct(
        message_id=1,
        date=datetime.now(timezone.utc),
        chat=Chat.model_construct(id=telegram_id, type="private"),
        from_user=TelegramUser.model_construct(
            id=telegram_id,
            is_bot=False,
            first_name="Student",
        ),
        text=text,
    )


def _callback(telegram_id: int, data: str) -> CallbackQuery:
    return CallbackQuery.model_construct(
        id="callback",
        from_user=TelegramUser.model_construct(
            id=telegram_id,
            is_bot=False,
            first_name="Student",
        ),
        chat_instance="instance",
        data=data,
    )


class AccessControlTests(unittest.TestCase):
    def _session(self, telegram_id: int, is_banned: bool) -> AsyncSession:
        class Session(AsyncSession):
            async def get(self, model, key):
                if model is DynamicRole:
                    return None
                if model is User:
                    return User(telegram_id=key, is_banned=is_banned)
                return None

            def add(self, _item) -> None:
                return None

            async def commit(self) -> None:
                return None

        return Session()

    def _invoke(self, event, *, telegram_id: int, is_banned: bool, raw_state=None):
        async def run():
            middleware = AccessMiddleware()
            session = self._session(telegram_id, is_banned)
            calls = []

            async def handler(_event, _data):
                calls.append(True)
                return "handled"

            data = {"config": _config(), "session": session}
            if raw_state is not None:
                data["raw_state"] = raw_state
            result = await middleware(handler, event, data)
            return result, calls

        return asyncio.run(run())

    def test_banned_user_can_only_open_homework_and_use_homework_buttons(self) -> None:
        with patch.object(Message, "answer", new_callable=AsyncMock) as answer:
            result, calls = self._invoke(
                _message(4, "/start"),
                telegram_id=4,
                is_banned=True,
            )
        self.assertIsNone(result)
        self.assertEqual(calls, [])
        answer.assert_awaited_once_with(BAN_DENIED_TEXT)

        for event in (
            _message(4, "/hw"),
            _callback(4, "hwui:subjects:0"),
            _callback(4, "hwui:day_subject:1"),
        ):
            result, calls = self._invoke(
                event,
                telegram_id=4,
                is_banned=True,
            )
            self.assertEqual(result, "handled")
            self.assertEqual(calls, [True])

    def test_banned_user_cannot_use_non_homework_inline_or_reply_menu(self) -> None:
        with patch.object(CallbackQuery, "answer", new_callable=AsyncMock) as answer:
            result, calls = self._invoke(
                _callback(4, "menu:homework"),
                telegram_id=4,
                is_banned=True,
            )
        self.assertIsNone(result)
        self.assertEqual(calls, [])
        answer.assert_awaited_once_with(BAN_DENIED_TEXT, show_alert=True)

        with patch.object(Message, "answer", new_callable=AsyncMock) as answer:
            self._invoke(
                _message(4, "📚 ДЗ"),
                telegram_id=4,
                is_banned=True,
            )
        answer.assert_awaited_once_with(BAN_DENIED_TEXT)

    def test_moderator_can_manage_homework_but_not_starosta_or_owner_actions(self) -> None:
        allowed_command, calls = self._invoke(
            _message(3, "/hw_add"),
            telegram_id=3,
            is_banned=False,
        )
        self.assertEqual(allowed_command, "handled")
        self.assertEqual(calls, [True])

        with patch.object(Message, "answer", new_callable=AsyncMock) as answer:
            result, calls = self._invoke(
                _message(3, "/set_parity"),
                telegram_id=3,
                is_banned=False,
            )
        self.assertIsNone(result)
        self.assertEqual(calls, [])
        answer.assert_awaited_once_with(ROLE_DENIED_TEXT)

        with patch.object(CallbackQuery, "answer", new_callable=AsyncMock) as answer:
            result, calls = self._invoke(
                _callback(3, "menu:schedule_time_overrides"),
                telegram_id=3,
                is_banned=False,
            )
        self.assertIsNone(result)
        self.assertEqual(calls, [])
        answer.assert_awaited_once_with(ROLE_DENIED_TEXT, show_alert=True)

    def test_student_can_view_events_without_event_management_permission(self) -> None:
        result, calls = self._invoke(
            _message(4, "/events"),
            telegram_id=4,
            is_banned=False,
        )

        self.assertEqual(result, "handled")
        self.assertEqual(calls, [True])

    def test_update_timings_log_durations_without_event_or_user_data(self) -> None:
        with self.assertLogs("bot.middlewares.access", level="INFO") as captured:
            result, calls = self._invoke(
                _callback(4, "menu:help"),
                telegram_id=4,
                is_banned=False,
            )

        self.assertEqual(result, "handled")
        self.assertEqual(calls, [True])
        output = "\n".join(captured.output)
        self.assertIn("event_type=callback", output)
        self.assertIn("db_lookup_ms=", output)
        self.assertIn("activity_ms=", output)
        self.assertIn("commit_ms=", output)
        self.assertIn("handler_ms=", output)
        self.assertNotIn("menu:help", output)
        self.assertNotIn("telegram_id", output)

    def test_moderator_cannot_continue_starosta_fsm_state(self) -> None:
        with patch.object(Message, "answer", new_callable=AsyncMock) as answer:
            result, calls = self._invoke(
                _message(3, "45"),
                telegram_id=3,
                is_banned=False,
                raw_state="ScheduleTimeOverrideForm:all_duration",
            )
        self.assertIsNone(result)
        self.assertEqual(calls, [])
        answer.assert_awaited_once_with(ROLE_DENIED_TEXT)


if __name__ == "__main__":
    unittest.main()
