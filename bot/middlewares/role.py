from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject

from config_loader import AppConfig


class RoleMiddleware(BaseMiddleware):
    """Resolve and attach the sender's configured role to each update."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        config = data.get("config")
        if not isinstance(config, AppConfig):
            raise RuntimeError("AppConfig was not provided in dispatcher workflow data")

        user = getattr(event, "from_user", None)
        telegram_id = getattr(user, "id", None)
        roles = config.roles

        if telegram_id in roles.owner:
            role = "owner"
        elif telegram_id in roles.starosta:
            role = "starosta"
        elif telegram_id in roles.moderators:
            role = "moderator"
        else:
            role = "user"

        data["role"] = role
        return await handler(event, data)
