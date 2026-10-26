from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from bot.handlers.admin import (
    HomeworkDeadlineCallback,
    HomeworkOverwriteCallback,
    homework_deadline_choice_handler,
    homework_deadline_handler,
    homework_overwrite_handler,
)


class HomeworkAdminRoleTests(unittest.IsolatedAsyncioTestCase):
    async def test_revoked_role_cannot_save_regular_homework(self) -> None:
        callback = SimpleNamespace(answer=AsyncMock())
        state = AsyncMock()
        session = AsyncMock()

        await homework_deadline_choice_handler(
            callback,
            HomeworkDeadlineCallback(action="regular"),
            state,
            session,
            role="user",
        )

        state.clear.assert_awaited_once()
        callback.answer.assert_awaited_once_with(
            "Эта функция только для админов.",
            show_alert=True,
        )
        session.add.assert_not_called()
        session.commit.assert_not_awaited()

    async def test_revoked_role_cannot_save_deadline_homework(self) -> None:
        message = SimpleNamespace(text="05.10.2026", answer=AsyncMock())
        state = AsyncMock()
        session = AsyncMock()

        await homework_deadline_handler(message, state, session, role="user")

        state.clear.assert_awaited_once()
        message.answer.assert_awaited_once_with(
            "Эта функция только для админов."
        )
        session.add.assert_not_called()
        session.commit.assert_not_awaited()

    async def test_revoked_role_cannot_overwrite_homework(self) -> None:
        callback = SimpleNamespace(answer=AsyncMock())
        state = AsyncMock()
        session = AsyncMock()

        await homework_overwrite_handler(
            callback,
            HomeworkOverwriteCallback(action="yes"),
            state,
            session,
            role="user",
        )

        state.clear.assert_awaited_once()
        callback.answer.assert_awaited_once_with(
            "Эта функция только для админов.",
            show_alert=True,
        )
        session.get.assert_not_awaited()
        session.add.assert_not_called()
        session.commit.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
