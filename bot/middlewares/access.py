from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject
from sqlalchemy.ext.asyncio import AsyncSession

from config_loader import AppConfig, StudentConfig
from database.models import User


ACCESS_DENIED_TEXT = (
    "⛔ Доступ ограничен. Бот предназначен только для студентов группы RC-215."
)


class AccessMiddleware(BaseMiddleware):
    """Allow only configured students and staff, and sync roster data to the DB."""

    @staticmethod
    def _student(config: AppConfig, telegram_id: int) -> StudentConfig | None:
        return next((item for item in config.students if item.telegram_id == telegram_id), None)

    @staticmethod
    def _is_staff(config: AppConfig, telegram_id: int) -> bool:
        roles = config.roles
        return telegram_id in set(roles.owner) | set(roles.starosta) | set(roles.moderators)

    async def _deny(self, event: TelegramObject) -> None:
        if isinstance(event, CallbackQuery):
            await event.answer(ACCESS_DENIED_TEXT, show_alert=True)
        elif isinstance(event, Message):
            await event.answer(ACCESS_DENIED_TEXT)

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        config = data.get("config")
        session = data.get("session")
        if not isinstance(config, AppConfig):
            raise RuntimeError("AppConfig was not provided in dispatcher workflow data")
        if not isinstance(session, AsyncSession):
            raise RuntimeError("AsyncSession was not provided in dispatcher workflow data")

        telegram_user = getattr(event, "from_user", None)
        telegram_id = getattr(telegram_user, "id", None)
        if not isinstance(telegram_id, int):
            return await handler(event, data)

        student = self._student(config, telegram_id)
        if student is None and not self._is_staff(config, telegram_id):
            await self._deny(event)
            return None

        user = await session.get(User, telegram_id)
        if user is None:
            user = User(telegram_id=telegram_id)
            session.add(user)

        # Roster fields are authoritative.  Staff outside the roster retain any
        # existing fields but can still use the administrative bot functions.
        if student is not None:
            user.full_name = student.full_name
            user.subgroup = student.subgroup
        if telegram_user is not None:
            user.username = telegram_user.username
        await session.commit()
        data["student"] = student
        return await handler(event, data)
