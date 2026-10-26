from __future__ import annotations

import unittest

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import TelegramObject

from bot.middlewares.error import ErrorMiddleware


class ErrorMiddlewareTests(unittest.IsolatedAsyncioTestCase):
    async def test_handler_error_clears_active_fsm_state(self) -> None:
        storage = MemoryStorage()
        state = FSMContext(
            storage=storage,
            key=StorageKey(bot_id=1, chat_id=2, user_id=3),
        )
        await state.set_state("test:form")

        async def failing_handler(_event, _data):
            raise RuntimeError("test failure")

        await ErrorMiddleware()(
            failing_handler,
            TelegramObject(),
            {"state": state},
        )

        self.assertIsNone(await state.get_state())
        await storage.close()


if __name__ == "__main__":
    unittest.main()
