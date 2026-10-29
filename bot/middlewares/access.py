from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
import logging
from time import monotonic
from typing import Any
from zoneinfo import ZoneInfo

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.ext.asyncio import AsyncSession

from config_loader import AppConfig, StudentConfig
from bot.utils.emojis import get_btn_emoji, get_emoji
from database.models import AIActivityDaily, AIUserSettings, DynamicRole, User
from bot.utils.permissions import role_allows

logger = logging.getLogger(__name__)

BAN_DENIED_TEXT = (
    "⛔ Ваш аккаунт заблокирован администратором. Вам доступен только просмотр ДЗ."
)
ROLE_DENIED_TEXT = "⛔ Недостаточно прав для выполнения этой операции."
ACCESS_DENIED_TEXT = (
    f"{get_btn_emoji('forbidden')} Доступ ограничен. "
    "Бот предназначен только для студентов группы RC-215."
)

COMMAND_PERMISSIONS = {
    "/admin": "admin_panel",
    "/hw_add": "homework_management",
    "/replace": "replacements",
    "/broadcast": "broadcasts",
    "/set_parity": "week_parity",
    "/activity": "activity",
    "/admin_history": "admin_history",
    "/docs_add": "documents_manage",
    "/event_add": "events_manage",
    "/absences": "absence_review",
    "/unban_absent": "absence_review",
    "/schedule_time": "schedule_time",
}

MENU_PERMISSIONS = {
    "admin": "admin_panel",
    "add_homework": "homework_management",
    "replace": "replacements",
    "broadcast": "broadcasts",
    "set_parity": "week_parity",
    "activity": "activity",
    "admin_history": "admin_history",
    "admin_management": "admin_management",
    "add_document": "documents_manage",
    "add_event": "events_manage",
    "add_student": "student_manage",
    "bans": "bans",
    "class_absences": "absence_review",
    "restrictions": "absence_review",
    "schedule_time_overrides": "schedule_time",
}

CALLBACK_PERMISSIONS = {
    "admin_management": "admin_management",
    "dynamic_role": "admin_management",
    "admin_broadcast": "broadcasts",
    "week_parity": "week_parity",
    "admin_history": "admin_history",
    "ban_management": "bans",
    "schedule_time": "schedule_time",
    "schedule_time_lesson": "schedule_time",
    "student_add_subgroup": "student_manage",
    "event_category": "events_manage",
    "abs_user": "absence_review",
}

STATE_PERMISSIONS = {
    "AdminManagementForm": "admin_management",
    "BroadcastForm": "broadcasts",
    "WeekParityForm": "week_parity",
    "ScheduleTimeOverrideForm": "schedule_time",
    "StudentAddForm": "student_manage",
    "EventForm": "events_manage",
    "DocumentForm": "documents_manage",
    "HomeworkForm": "homework_management",
    "ReplaceForm": "replacements",
}

HOMEWORK_CALLBACK_ACTIONS = {"subjects", "subject", "day", "day_subject"}

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


def _private_activity_category(event: Message | CallbackQuery) -> str | None:
    if isinstance(event, CallbackQuery):
        parts = (event.data or "").split(":")
        if len(parts) == 2 and parts[0] == "menu":
            return {
                "homework": "homework",
                "schedule": "schedule",
                "help": "help",
                "documents": "documents",
            }.get(parts[1])
        return None

    text = (event.text or "").strip().casefold()
    if not text:
        return None
    command = text.split(maxsplit=1)[0].split("@", maxsplit=1)[0]
    if command in {"/hw"}:
        return "homework"
    if command in {"/schedule", "/расписание"}:
        return "schedule"
    if command == "/help":
        return "help"
    if command == "/docs":
        return "documents"
    if text.endswith(" дз"):
        return "homework"
    if text.endswith(" расписание"):
        return "schedule"
    if text.endswith(" документы"):
        return "documents"
    return None


async def _record_ai_activity(
    session: AsyncSession,
    event: Message | CallbackQuery,
    telegram_id: int,
    config: AppConfig,
    raw_state: object,
) -> None:
    if not config.ai.personalization_enabled or raw_state is not None:
        return
    settings = await session.get(AIUserSettings, telegram_id)
    if settings is None or not settings.personalization_enabled:
        return
    category = _private_activity_category(event)
    if category is None:
        return
    local_date = datetime.now(ZoneInfo(config.bot.timezone)).date()
    statement = (
        insert(AIActivityDaily)
        .values(
            telegram_id=telegram_id,
            activity_date=local_date,
            category=category,
            count=1,
        )
        .on_conflict_do_update(
            index_elements=["telegram_id", "activity_date", "category"],
            set_={"count": AIActivityDaily.count + 1},
        )
    )
    try:
        async with session.begin_nested():
            await session.execute(statement)
    except Exception as exc:
        logger.warning("Could not record local AI activity aggregate (%s)", type(exc).__name__)


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

    @staticmethod
    async def _deny_banned(event: TelegramObject) -> None:
        if isinstance(event, CallbackQuery):
            await event.answer(BAN_DENIED_TEXT, show_alert=True)
        elif isinstance(event, Message):
            await event.answer(BAN_DENIED_TEXT)

    @staticmethod
    async def _deny_role(event: TelegramObject) -> None:
        if isinstance(event, CallbackQuery):
            await event.answer(ROLE_DENIED_TEXT, show_alert=True)
        elif isinstance(event, Message):
            await event.answer(ROLE_DENIED_TEXT)

    @staticmethod
    def _command(event: Message) -> str | None:
        text = (event.text or "").strip()
        if not text.startswith("/"):
            return None
        return text.split(maxsplit=1)[0].split("@", maxsplit=1)[0].lower()

    @staticmethod
    def _callback_permission(callback_data: str) -> str | None:
        parts = callback_data.split(":")
        prefix = parts[0]
        if prefix == "menu":
            return MENU_PERMISSIONS.get(parts[1]) if len(parts) > 1 else None
        if prefix == "hwui":
            return None
        if prefix == "abs":
            return "absence_review" if len(parts) > 1 and parts[1] == "flag" else None
        if prefix == "docs":
            return "documents_manage" if len(parts) > 1 and parts[1] == "add" else None
        if prefix.startswith("hw_add") or prefix == "hw_overwrite":
            return "homework_management"
        if prefix.startswith("replace"):
            return "replacements"
        return CALLBACK_PERMISSIONS.get(prefix)

    @staticmethod
    def _state_permission(raw_state: object) -> str | None:
        if not isinstance(raw_state, str):
            return None
        state_group = raw_state.split(":", maxsplit=1)[0]
        return STATE_PERMISSIONS.get(state_group)

    @classmethod
    def _permission(
        cls,
        event: TelegramObject,
        raw_state: object,
    ) -> str | None:
        if isinstance(event, Message):
            command = cls._command(event)
            if command is not None:
                return COMMAND_PERMISSIONS.get(command)
            return cls._state_permission(raw_state)
        if isinstance(event, CallbackQuery):
            return cls._callback_permission(event.data or "")
        return None

    @staticmethod
    def _is_homework_view(event: TelegramObject) -> bool:
        if isinstance(event, Message):
            return AccessMiddleware._command(event) == "/hw"
        if isinstance(event, CallbackQuery):
            parts = (event.data or "").split(":")
            return (
                len(parts) >= 2
                and parts[0] == "hwui"
                and parts[1] in HOMEWORK_CALLBACK_ACTIONS
            )
        return False

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

        middleware_started = monotonic()
        telegram_user = getattr(event, "from_user", None)
        telegram_id = getattr(telegram_user, "id", None)
        if not isinstance(telegram_id, int):
            return await handler(event, data)

        student = self._student(config, telegram_id)
        db_lookup_started = monotonic()
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
        db_lookup_ms = int((monotonic() - db_lookup_started) * 1000)
        if user is None:
            user = User(telegram_id=telegram_id)
            session.add(user)

        if user.is_banned and not self._is_homework_view(event):
            await self._deny_banned(event)
            return None

        role = data.get("role")
        if not isinstance(role, str):
            roles = config.roles
            if telegram_id in roles.owner:
                role = "owner"
            elif telegram_id in roles.starosta:
                role = "starosta"
            elif telegram_id in roles.moderators:
                role = "moderator"
            elif has_dynamic_role and dynamic_role is not None:
                role = dynamic_role.role
            else:
                role = "user"
            data["role"] = role
        permission = self._permission(event, data.get("raw_state"))
        if permission is not None and not role_allows(role, permission):
            await self._deny_role(event)
            return None

        # Roster fields are authoritative.  Staff outside the roster retain any
        # existing fields but can still use the administrative bot functions.
        if student is not None:
            user.full_name = student.full_name
            user.subgroup = student.subgroup
        if telegram_user is not None:
            user.username = telegram_user.username
        if isinstance(event, (Message, CallbackQuery)):
            record_user_activity(user, event)
        event_chat = (
            event.chat if isinstance(event, Message)
            else event.message.chat if isinstance(event, CallbackQuery) and event.message is not None
            else None
        )
        if event_chat is not None and event_chat.type == "private":
            activity_started = monotonic()
            await _record_ai_activity(
                session,
                event,
                telegram_id,
                config,
                data.get("raw_state"),
            )
            activity_ms = int((monotonic() - activity_started) * 1000)
        else:
            activity_ms = 0
        commit_started = monotonic()
        await session.commit()
        commit_ms = int((monotonic() - commit_started) * 1000)
        data["student"] = student
        handler_started = monotonic()
        try:
            return await handler(event, data)
        finally:
            handler_ms = int((monotonic() - handler_started) * 1000)
            logger.info(
                "Access update timings event_type=%s db_lookup_ms=%d "
                "activity_ms=%d commit_ms=%d handler_ms=%d total_ms=%d",
                "callback" if isinstance(event, CallbackQuery) else "message",
                db_lookup_ms,
                activity_ms,
                commit_ms,
                handler_ms,
                int((monotonic() - middleware_started) * 1000),
            )
