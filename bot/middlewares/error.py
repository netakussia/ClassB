from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject


logger = logging.getLogger(__name__)
USER_ERROR_TEXT = "⚠️ Произошла внутренняя ошибка. Мы уже разбираемся!"


class ErrorMiddleware(BaseMiddleware):
    """Log an unexpected handler failure and leave the user with an answer."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        try:
            return await handler(event, data)
        except Exception:
            logger.exception("Unhandled bot update")
            try:
                if isinstance(event, CallbackQuery):
                    await event.answer(USER_ERROR_TEXT, show_alert=True)
                elif isinstance(event, Message):
                    await event.answer(USER_ERROR_TEXT)
            except Exception:
                logger.exception("Could not send an error response to the user")
            return None
