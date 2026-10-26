from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject
from sqlalchemy.ext.asyncio import AsyncSession

from config_loader import AppConfig, StudentConfig
from bot.utils.emojis import get_btn_emoji, get_emoji
from database.models import DynamicRole, User


ACCESS_DENIED_TEXT = (
    f"{get_btn_emoji('forbidden')} Доступ ограничен. "
    "Бот предназначен только для студентов группы RC-215."
)

COMMAND_ACTIONS = {
    "/start": "Открыл главное меню",
    "/schedule": "Просмотр расписания",
    "/расписание": "Просмотр расписания",
    "/my_group": "Просмотр своей подгруппы",
    "/hw": "Просмотр или поиск ДЗ",
    "/absent": "Начал подачу заявки",
    "/my_absences": "Просмотр своих заявок",
    "/absences": "Просмотр заявок группы",
    "/replace": "Работа с заменами",
    "/hw_add": "Добавление ДЗ",
    "/docs": "Просмотр документов",
    "/docs_add": "Добавление документа",
    "/events": "Просмотр событий",
    "/event_add": "Добавление события",
    "/admin": "Открыл админ-панель",
    "/broadcast": "Создание рассылки",
    "/activity": "Просмотр активности пользователей",
    "/set_parity": "Настройка чётности недели",
}

MENU_ACTIONS = {
    "home": "Вернулся в главное меню",
    "homework": "Просмотр ДЗ",
    "schedule": "Просмотр расписания",
    "absence": "Начал подачу заявки",
    "my_absences": "Просмотр своих заявок",
    "my_group": "Просмотр своей подгруппы",
    "admin": "Открыл админ-панель",
    "broadcast": "Создание рассылки",
    "add_homework": "Добавление ДЗ",
    "replace": "Работа с заменами",
    "add_document": "Добавление документа",
    "add_event": "Добавление события",
    "add_student": "Добавление студента",
    "class_absences": "Просмотр заявок группы",
    "restrictions": "Просмотр ограничений",
    "activity": "Просмотр активности пользователей",
}


def describe_user_action(event: Message | CallbackQuery) -> str:
    if isinstance(event, Message):
        text = (event.text or "").strip()
        if text.startswith("/"):
            command = text.split(maxsplit=1)[0].split("@", maxsplit=1)[0].lower()
            return COMMAND_ACTIONS.get(command, f"Команда {command}")
        if text:
            return "Отправил текстовое сообщение"
        if event.photo:
            return "Отправил фото"
        if event.document:
            return "Отправил документ"
        if event.voice:
            return "Отправил голосовое сообщение"
        if event.video:
            return "Отправил видео"
        return "Отправил сообщение"

    callback_data = event.data or ""
    parts = callback_data.split(":")
    if len(parts) >= 2 and parts[0] == "menu":
        return MENU_ACTIONS.get(parts[1], "Открыл раздел")
    if parts[0] in {"abs", "abs_user"}:
        if "my" in parts or "user" in parts:
            return "Просмотр своих заявок"
        return "Работа с заявкой на отсутствие"
    if parts[0].startswith("hw") or parts[0].startswith("homework"):
        if "tomorrow" in parts:
            return "Просмотр ДЗ на завтра"
        if "search" in parts or "subject" in parts:
            return "Поиск ДЗ"
        return "Работа с домашним заданием"
    if parts[0].startswith("replace"):
        return "Работа с заменами"
    if parts[0].startswith("event"):
        return "Работа с событиями"
    if parts[0].startswith("doc"):
        return "Работа с документами"
    if parts[0] == "schedule":
        return "Просмотр расписания"
    if parts[0] == "week_parity":
        return "Настройка чётности недели"
    return "Нажал кнопку"


def record_user_activity(user: User, event: Message | CallbackQuery) -> None:
    user.last_action = describe_user_action(event)
    user.last_active_at = datetime.now(timezone.utc)


class AccessMiddleware(BaseMiddleware):
    """Allow only configured students and staff, and sync roster data to the DB."""

    @staticmethod
    def _student(config: AppConfig, telegram_id: int) -> StudentConfig | None:
        return next((item for item in config.students if item.telegram_id == telegram_id), None)

    @staticmethod
    def _is_configured_staff(config: AppConfig, telegram_id: int) -> bool:
        roles = config.roles
        return telegram_id in set(roles.owner) | set(roles.starosta) | set(roles.moderators)

    async def _deny(self, event: TelegramObject) -> None:
        if isinstance(event, CallbackQuery):
            await event.answer(ACCESS_DENIED_TEXT, show_alert=True)
        elif isinstance(event, Message):
            await event.answer(
                f"{get_emoji('warning')} Доступ ограничен. "
                "Бот предназначен только для студентов группы RC-215.",
                parse_mode="HTML",
            )

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
        dynamic_role = await session.get(DynamicRole, telegram_id)
        has_dynamic_role = dynamic_role is not None and dynamic_role.role in {
            "starosta",
            "moderator",
        }
        if (
            student is None
            and not self._is_configured_staff(config, telegram_id)
            and not has_dynamic_role
        ):
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
        if isinstance(event, (Message, CallbackQuery)):
            record_user_activity(user, event)
        await session.commit()
        data["student"] = student
        return await handler(event, data)
