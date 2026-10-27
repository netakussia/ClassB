from __future__ import annotations

from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from sqlalchemy.ext.asyncio import AsyncSession

from bot.handlers.admin import (
    BanManagementCallback,
    _ban_management_page,
    ban_management_handler,
)
from bot.middlewares.access import ROLE_DENIED_TEXT
from config_loader import RolesConfig
from database.models import AdminActionLog, User


class BanManagementTests(unittest.IsolatedAsyncioTestCase):
    async def test_user_listing_includes_statuses_and_paginates(self) -> None:
        users = [
            User(
                telegram_id=telegram_id,
                full_name=f"Student {telegram_id}",
                is_banned=telegram_id == 2,
            )
            for telegram_id in range(1, 12)
        ]

        class Session(AsyncSession):
            async def scalars(self, _statement):
                return users

        text, keyboard = await _ban_management_page(Session(), 0)
        self.assertIn("Student 1 (ID: 1) — 🟢 Активен", text)
        self.assertIn("Student 2 (ID: 2) — 🔴 Заблокирован", text)
        self.assertNotIn("Student 11", text)
        buttons = [
            button
            for row in keyboard.inline_keyboard
            for button in row
        ]
        self.assertEqual(len(buttons), 12)
        self.assertEqual(buttons[-2].callback_data, "ban_management:page:1:0")

    async def test_starosta_toggle_persists_ban_and_audit_entry(self) -> None:
        target = User(telegram_id=9, full_name="Test student", is_banned=False)
        audit_logs: list[AdminActionLog] = []

        class Session(AsyncSession):
            async def get(self, model, key):
                return target if model is User and key == 9 else None

            async def scalars(self, _statement):
                return [target]

            def add(self, item) -> None:
                if isinstance(item, AdminActionLog):
                    audit_logs.append(item)

            async def commit(self) -> None:
                return None

        message = SimpleNamespace(edit_text=AsyncMock())
        callback = SimpleNamespace(
            answer=AsyncMock(),
            message=message,
            from_user=SimpleNamespace(id=2),
        )
        await ban_management_handler(
            callback,
            BanManagementCallback(action="toggle", page=0, telegram_id=9),
            Session(),
            role="starosta",
            config=SimpleNamespace(
                roles=RolesConfig(owner=[], starosta=[], moderators=[])
            ),
        )

        self.assertTrue(target.is_banned)
        callback.answer.assert_awaited_once_with("Пользователь заблокирован.")
        message.edit_text.assert_awaited_once()
        self.assertEqual(len(audit_logs), 1)
        self.assertEqual(audit_logs[0].admin_id, 2)
        self.assertEqual(audit_logs[0].action, "Заблокировал пользователя")
        self.assertIn("telegram_id=9", audit_logs[0].details)
        self.assertNotIn("Test student", audit_logs[0].details)

    async def test_starosta_cannot_ban_configured_staff(self) -> None:
        target = User(telegram_id=9, full_name="Owner", is_banned=False)

        class Session(AsyncSession):
            async def get(self, model, key):
                return target if model is User and key == 9 else None

            async def commit(self) -> None:
                raise AssertionError("Protected staff changes must not commit")

        callback = SimpleNamespace(
            answer=AsyncMock(),
            message=SimpleNamespace(edit_text=AsyncMock()),
            from_user=SimpleNamespace(id=2),
        )
        await ban_management_handler(
            callback,
            BanManagementCallback(action="toggle", page=0, telegram_id=9),
            Session(),
            role="starosta",
            config=SimpleNamespace(
                roles=RolesConfig(owner=[9], starosta=[], moderators=[])
            ),
        )

        self.assertFalse(target.is_banned)
        callback.answer.assert_awaited_once_with(
            "Нельзя блокировать сотрудников.",
            show_alert=True,
        )
        callback.message.edit_text.assert_not_awaited()

    async def test_moderator_cannot_toggle_user_ban(self) -> None:
        callback = SimpleNamespace(
            answer=AsyncMock(),
            message=SimpleNamespace(edit_text=AsyncMock()),
            from_user=SimpleNamespace(id=3),
        )
        session = AsyncMock()
        await ban_management_handler(
            callback,
            BanManagementCallback(action="toggle", page=0, telegram_id=9),
            session,
            role="moderator",
            config=SimpleNamespace(
                roles=RolesConfig(owner=[], starosta=[], moderators=[])
            ),
        )

        callback.answer.assert_awaited_once_with(ROLE_DENIED_TEXT, show_alert=True)
        session.get.assert_not_awaited()
        session.commit.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
