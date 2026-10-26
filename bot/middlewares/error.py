from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message, TelegramObject

from bot.utils.emojis import get_btn_emoji, get_emoji

logger = logging.getLogger(__name__)
USER_ERROR_TEXT = f"{get_btn_emoji('warning')} Произошла внутренняя ошибка. Мы уже разбираемся!"


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
            state = data.get("state")
            if isinstance(state, FSMContext):
                try:
                    await state.clear()
                except Exception:
                    logger.exception("Could not clear FSM state after handler failure")
            try:
                if isinstance(event, CallbackQuery):
                    await event.answer(USER_ERROR_TEXT, show_alert=True)
                elif isinstance(event, Message):
                    await event.answer(
                        f"{get_emoji('warning')} Произошла внутренняя ошибка. "
                        "Мы уже разбираемся!",
                        parse_mode="HTML",
                    )
            except Exception:
                logger.exception("Could not send an error response to the user")
            return None
