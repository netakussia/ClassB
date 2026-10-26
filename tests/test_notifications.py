from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone
from pathlib import Path
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
)
from aiogram.enums import ParseMode
from aiogram.methods import SendMessage
from aiogram.types import Chat, Message, MessageEntity, PhotoSize, User

from bot.handlers.admin import (
    BroadcastPayload,
    _broadcast_payload,
    _deliver_broadcast,
    _send_broadcast_payload,
)
from bot.handlers.replacements import _send_replacement_notifications
from bot.keyboards.reply import main_reply_keyboard
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


def _config(students: list[StudentConfig], group_chat_id: int = 900) -> AppConfig:
    empty_day = DaySchedule(odd=[], even=[])
    return AppConfig(
        bot=BotConfig(token="test", timezone="UTC", group_chat_id=group_chat_id),
        roles=RolesConfig(owner=[], starosta=[], moderators=[]),
        students=students,
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


class NotificationTests(IsolatedAsyncioTestCase):
    async def test_broadcast_input_converts_telegram_entities_and_raw_html(self) -> None:
        common = {
            "message_id": 55,
            "date": datetime.now(timezone.utc),
            "chat": Chat(id=123, type="private"),
            "from_user": User(id=123, is_bot=False, first_name="Admin"),
        }
        entity_message = Message(
            **common,
            text="Notice",
            entities=[MessageEntity(type="bold", offset=0, length=6)],
        )
        raw_html_message = Message(**common, text="<b>Notice</b>")
        photo_message = Message(
            **common,
            caption="Notice",
            caption_entities=[MessageEntity(type="italic", offset=0, length=6)],
            photo=[
                PhotoSize(
                    file_id="photo-file-id",
                    file_unique_id="photo-unique-id",
                    width=800,
                    height=600,
                )
            ],
        )

        self.assertEqual(_broadcast_payload(entity_message), BroadcastPayload("text", "<b>Notice</b>"))
        self.assertEqual(_broadcast_payload(raw_html_message), BroadcastPayload("text", "<b>Notice</b>"))
        self.assertEqual(
            _broadcast_payload(photo_message),
            BroadcastPayload("photo", "<i>Notice</i>", "photo-file-id"),
        )

    async def test_replacement_sends_to_roster_and_database_users_safely(self) -> None:
        class Session:
            async def scalars(self, _query):
                return [102, 103, 104]

        class Bot:
            def __init__(self) -> None:
                self.sent: list[int] = []

            async def send_message(
                self,
                chat_id: int,
                text: str,
                parse_mode: ParseMode,
            ) -> None:
                self.sent.append(chat_id)
                if chat_id == 102:
                    raise TelegramForbiddenError(
                        method=SendMessage(chat_id=chat_id, text=text),
                        message="bot was blocked by the user",
                    )
                if chat_id == 103:
                    raise TelegramNetworkError(
                        method=SendMessage(chat_id=chat_id, text=text),
                        message="network timeout",
                    )

        bot = Bot()
        config = _config(
            [StudentConfig(101, "Student One", "french"), StudentConfig(102, "Student Two", "english")]
        )
        with patch("bot.handlers.replacements.asyncio.sleep", new=AsyncMock()) as sleep:
            sent, failed, group_delivered = await _send_replacement_notifications(
                bot, config, Session(), "Замена: кабинет 101"
            )

        self.assertEqual((sent, failed, group_delivered), (2, 2, True))
        self.assertEqual(bot.sent, [900, 101, 102, 103, 104])
        self.assertEqual(sleep.await_count, 3)
        self.assertTrue(all(call.args == (0.05,) for call in sleep.await_args_list))

    async def test_broadcast_continues_after_bad_request_and_deduplicates_users(self) -> None:
        class Bot:
            def __init__(self) -> None:
                self.sent: list[int] = []

            async def send_message(
                self,
                chat_id: int,
                text: str,
                parse_mode: ParseMode,
                reply_markup=None,
            ) -> None:
                self.sent.append(chat_id)
                if chat_id == 2:
                    raise TelegramBadRequest(
                        method=SendMessage(chat_id=chat_id, text=text),
                        message="chat not found",
                    )
                if chat_id == 3:
                    raise TelegramNetworkError(
                        method=SendMessage(chat_id=chat_id, text=text),
                        message="network timeout",
                    )

        bot = Bot()
        with patch("bot.handlers.admin.asyncio.sleep", new=AsyncMock()) as sleep:
            sent, failed = await _deliver_broadcast(
                bot, [1, 2, 2, 3, 4], BroadcastPayload("text", "<b>Update</b>")
            )

        self.assertEqual((sent, failed), (2, 2))
        self.assertEqual(bot.sent, [1, 2, 3, 4])
        self.assertEqual(sleep.await_count, 3)

    async def test_broadcast_payload_preserves_html_and_photo_caption(self) -> None:
        bot = AsyncMock()
        payload = BroadcastPayload("photo", "<b>Update</b>", "photo-file-id")

        await _send_broadcast_payload(bot, 123, payload)

        bot.send_photo.assert_awaited_once_with(
            chat_id=123,
            photo="photo-file-id",
            caption="<b>Update</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=None,
        )

    def test_persistent_keyboard_shows_admin_button_only_for_staff(self) -> None:
        student_labels = {
            button.text
            for row in main_reply_keyboard("user").keyboard
            for button in row
        }
        admin_labels = {
            button.text
            for row in main_reply_keyboard("moderator").keyboard
            for button in row
        }

        self.assertNotIn("⚙️ Админка", student_labels)
        self.assertIn("⚙️ Админка", admin_labels)


if __name__ == "__main__":
    unittest.main()
