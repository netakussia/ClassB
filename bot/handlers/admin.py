from __future__ import annotations

import asyncio
import html
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from aiogram import Bot, F, Router
from aiogram.enums import ParseMode
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
)
from aiogram.filters import Command
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.keyboards.inline import InlineKeyboardBuilder
from bot.ui import MenuCallback, add_cancel_button, menu_back_keyboard
from bot.utils.homework_dates import local_today, next_lesson_date, subjects_for_date
from bot.utils.schedule_parser import WEEKDAYS, parse_schedule
from bot.utils.subjects import all_subjects, upcoming_subjects
from bot.utils.time_utils import (
    WEEK_PARITY_DATE_KEY,
    WEEK_PARITY_VALUE_KEY,
    calculate_lesson_times,
    get_configured_week_parity,
)
from config_loader import AppConfig, Lesson, StudentConfig, append_student
from database.models import (
    AdminActionLog,
    BotSetting,
    DynamicRole,
    Homework,
    HomeworkHistory,
    ScheduleTimeOverride,
    User,
)
from bot.utils.admin_audit import log_admin_action
from bot.utils.emojis import get_btn_emoji, get_button_label, get_emoji


router = Router(name="admin")
logger = logging.getLogger(__name__)
ADMIN_ROLES = {"owner", "starosta", "moderator"}


class HomeworkForm(StatesGroup):
    subject = State()
    new_subject_confirmation = State()
    target_date = State()
    task = State()
    attachments = State()
    deadline = State()
    overwrite_confirmation = State()


class StudentAddForm(StatesGroup):
    telegram_id = State()
    full_name = State()
    subgroup = State()


class ScheduleTimeOverrideForm(StatesGroup):
    target_date = State()
    manual_date = State()
    mode = State()
    lesson_number = State()
    all_duration = State()
    all_break = State()
    specific_duration = State()
    specific_break = State()
    confirmation = State()


class HomeworkOverwriteCallback(CallbackData, prefix="hw_overwrite"):
    action: str


class HomeworkSubjectCallback(CallbackData, prefix="hw_add_subject"):
    action: str
    subject_index: int


class HomeworkTargetCallback(CallbackData, prefix="hw_add_target"):
    choice: str


class HomeworkDeadlineCallback(CallbackData, prefix="hw_add_deadline"):
    action: str


class HomeworkAttachmentCallback(CallbackData, prefix="hw_add_attachment"):
    action: str


class StudentSubgroupCallback(CallbackData, prefix="student_add_subgroup"):
    subgroup: str


class ScheduleTimeCallback(CallbackData, prefix="schedule_time"):
    action: str


class ScheduleTimeLessonCallback(CallbackData, prefix="schedule_time_lesson"):
    lesson_number: int


class WeekParityCallback(CallbackData, prefix="week_parity"):
    parity: str


class WeekParityForm(StatesGroup):
    reference_date = State()
    parity = State()


class BroadcastForm(StatesGroup):
    content = State()
    confirmation = State()
    sending = State()


class BroadcastCallback(CallbackData, prefix="admin_broadcast"):
    action: str


class AdminHistoryCallback(CallbackData, prefix="admin_history"):
    page: int


class AdminManagementCallback(CallbackData, prefix="admin_management"):
    action: str
    telegram_id: int = 0


class DynamicRoleCallback(CallbackData, prefix="dynamic_role"):
    telegram_id: int
    role: str


class AdminManagementForm(StatesGroup):
    telegram_id = State()


@dataclass(frozen=True, slots=True)
class BroadcastPayload:
    content_type: str
    content: str
    file_id: str | None = None


def _broadcast_payload(message: Message) -> BroadcastPayload | None:
    source_content = message.caption if message.caption is not None else message.text
    content = message.html_text or source_content or ""
    if source_content and not message.entities and not message.caption_entities:
        if re.search(
            r"</?(?:b|strong|i|em|u|ins|s|strike|del|span|tg-spoiler|a|code|pre|blockquote)(?:\s|>)",
            source_content,
            flags=re.IGNORECASE,
        ):
            content = source_content

    if message.photo:
        return BroadcastPayload("photo", content, message.photo[-1].file_id)
    if message.document:
        return BroadcastPayload("document", content, message.document.file_id)
    if message.text is not None:
        return BroadcastPayload("text", content)
    return None


async def _send_broadcast_payload(
    bot: Bot,
    chat_id: int,
    payload: BroadcastPayload,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    if payload.content_type == "text":
        await bot.send_message(
            chat_id=chat_id,
            text=payload.content,
            parse_mode=ParseMode.HTML,
            reply_markup=reply_markup,
        )
    elif payload.content_type == "photo" and payload.file_id:
        await bot.send_photo(
            chat_id=chat_id,
            photo=payload.file_id,
            caption=payload.content or None,
            parse_mode=ParseMode.HTML,
            reply_markup=reply_markup,
        )
    elif payload.content_type == "document" and payload.file_id:
        await bot.send_document(
            chat_id=chat_id,
            document=payload.file_id,
            caption=payload.content or None,
            parse_mode=ParseMode.HTML,
            reply_markup=reply_markup,
        )
    else:
        raise ValueError("Unsupported or incomplete broadcast payload")


def _broadcast_confirmation_keyboard() -> InlineKeyboardMarkup:
    keyboard = InlineKeyboardBuilder()
    keyboard.button(
        text=get_button_label("Подтвердить и отправить", icon_key="send"),
        callback_data=BroadcastCallback(action="confirm").pack(),
        style="success",
    )
    keyboard.button(
        text=get_button_label("Отмена", icon_key="cancel"),
        callback_data=BroadcastCallback(action="cancel").pack(),
        style="danger",
    )
    keyboard.adjust(1)
    return keyboard.as_markup()


def _admin_history_keyboard(page: int, has_next: bool) -> InlineKeyboardMarkup:
    keyboard = InlineKeyboardBuilder()
    if page > 0:
        keyboard.button(
            text=get_button_label("Назад", icon_key="back_arrow"),
            callback_data=AdminHistoryCallback(page=page - 1).pack(),
        )
    if has_next:
        keyboard.button(
            text=get_button_label("Далее", icon_key="forward"),
            callback_data=AdminHistoryCallback(page=page + 1).pack(),
        )
    keyboard.button(
        text=get_button_label("В админку", icon_key="back_arrow"),
        callback_data=MenuCallback(action="admin").pack(),
    )
    keyboard.adjust(2)
    return keyboard.as_markup()


async def _admin_history_page(
    session: AsyncSession,
    page: int,
) -> tuple[str, InlineKeyboardMarkup]:
    page_size = 15
    rows = list(
        await session.scalars(
            select(AdminActionLog)
            .order_by(AdminActionLog.created_at.desc(), AdminActionLog.id.desc())
            .offset(page * page_size)
            .limit(page_size + 1)
        )
    )
    has_next = len(rows) > page_size
    rows = rows[:page_size]
    if not rows:
        text = f"{get_emoji('history')} История действий админов пока пуста."
    else:
        admin_ids = {row.admin_id for row in rows}
        users = list(
            await session.scalars(
                select(User).where(User.telegram_id.in_(admin_ids))
            )
        )
        usernames = {user.telegram_id: user.username for user in users}
        lines = [
            f"{get_emoji('history')} История действий админов · страница {page + 1}"
        ]
        for row in rows:
            created_at = row.created_at
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=timezone.utc)
            username = usernames.get(row.admin_id)
            admin_label = f"@{username}" if username else str(row.admin_id)
            description = row.action
            if row.details:
                description = f"{description}: {row.details}"
            lines.append(
                f"• [{created_at:%d.%m %H:%M}] "
                f"{html.escape(admin_label)}: {html.escape(description)}"
            )
        text = "\n".join(lines)
    return text, _admin_history_keyboard(page, has_next)


async def show_admin_action_history(
    message: Message,
    session: AsyncSession,
    page: int = 0,
) -> None:
    text, keyboard = await _admin_history_page(session, max(0, page))
    await message.answer(text, reply_markup=keyboard, parse_mode="HTML")


@router.callback_query(AdminHistoryCallback.filter())
async def admin_history_page_handler(
    callback: CallbackQuery,
    callback_data: AdminHistoryCallback,
    session: AsyncSession,
    role: str,
) -> None:
    if role not in ADMIN_ROLES:
        await callback.answer("История доступна только администраторам.", show_alert=True)
        return
    if callback.message is None:
        await callback.answer("Открой историю из админ-панели.", show_alert=True)
        return
    text, keyboard = await _admin_history_page(session, max(0, callback_data.page))
    await callback.answer()
    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")


def _dynamic_roles_keyboard(roles: list[DynamicRole]) -> InlineKeyboardMarkup:
    keyboard = InlineKeyboardBuilder()
    keyboard.button(
        text=get_button_label("Добавить админа", icon_key="add"),
        callback_data=AdminManagementCallback(action="add").pack(),
        style="primary",
    )
    for dynamic_role in roles:
        keyboard.button(
            text=get_button_label(
                f"Снять права: {dynamic_role.telegram_id} ({dynamic_role.role})",
                icon_key="cancel",
            ),
            callback_data=AdminManagementCallback(
                action="remove",
                telegram_id=dynamic_role.telegram_id,
            ).pack(),
            style="danger",
        )
    keyboard.button(
        text=get_button_label("В админку", icon_key="back_arrow"),
        callback_data=MenuCallback(action="admin").pack(),
    )
    keyboard.adjust(1)
    return keyboard.as_markup()


async def show_admin_management(
    message: Message,
    session: AsyncSession,
    config: AppConfig,
    role: str,
) -> None:
    if role != "owner":
        await message.answer("Управлять ролями может только владелец.")
        return

    dynamic_roles = list(
        await session.scalars(select(DynamicRole).order_by(DynamicRole.telegram_id))
    )
    users = list(
        await session.scalars(
            select(User).where(
                User.telegram_id.in_({item.telegram_id for item in dynamic_roles})
            )
        )
    ) if dynamic_roles else []
    usernames = {user.telegram_id: user.username for user in users}
    lines = [f"{get_emoji('users')} Управление администраторами"]
    configured = (
        [(telegram_id, "owner") for telegram_id in config.roles.owner]
        + [(telegram_id, "starosta") for telegram_id in config.roles.starosta]
        + [(telegram_id, "moderator") for telegram_id in config.roles.moderators]
    )
    for telegram_id, configured_role in configured:
        username = usernames.get(telegram_id)
        label = f"@{username}" if username else str(telegram_id)
        lines.append(
            f"• {html.escape(label)} — {html.escape(configured_role)} (roles.yaml)"
        )
    for dynamic_role in dynamic_roles:
        username = usernames.get(dynamic_role.telegram_id)
        label = f"@{username}" if username else str(dynamic_role.telegram_id)
        lines.append(
            f"• {html.escape(label)} — {html.escape(dynamic_role.role)} (динамическая роль)"
        )
    if not configured and not dynamic_roles:
        lines.append("Администраторов пока нет.")
    await message.answer(
        "\n".join(lines),
        reply_markup=_dynamic_roles_keyboard(dynamic_roles),
        parse_mode="HTML",
    )


@router.callback_query(AdminManagementCallback.filter(F.action == "add"))
async def admin_management_add_handler(
    callback: CallbackQuery,
    state: FSMContext,
    role: str,
) -> None:
    if role != "owner":
        await callback.answer("Управлять ролями может только владелец.", show_alert=True)
        return
    await state.clear()
    await state.set_state(AdminManagementForm.telegram_id)
    await callback.answer()
    if callback.message is not None:
        await callback.message.answer(
            "Введи Telegram ID или перешли сообщение пользователя, "
            "которому нужно назначить роль.",
            reply_markup=menu_back_keyboard(),
        )


@router.message(AdminManagementForm.telegram_id)
async def admin_management_id_handler(
    message: Message,
    state: FSMContext,
    role: str,
) -> None:
    if role != "owner":
        await state.clear()
        await message.answer("Управлять ролями может только владелец.")
        return
    forwarded_user = getattr(message, "forward_from", None)
    forward_origin = getattr(message, "forward_origin", None)
    if forwarded_user is None and getattr(forward_origin, "type", None) == "user":
        forwarded_user = getattr(forward_origin, "sender_user", None)
    telegram_id = getattr(forwarded_user, "id", None)
    if not isinstance(telegram_id, int):
        try:
            telegram_id = int((message.text or "").strip())
        except ValueError:
            telegram_id = 0
    if telegram_id <= 0:
        await message.answer("Не удалось получить корректный Telegram ID.")
        return
    await state.update_data(target_telegram_id=telegram_id)
    keyboard = InlineKeyboardBuilder()
    for role_name in ("starosta", "moderator"):
        keyboard.button(
            text=get_button_label(
                "Староста" if role_name == "starosta" else "Модератор"
            ),
            callback_data=DynamicRoleCallback(
                telegram_id=telegram_id,
                role=role_name,
            ).pack(),
            style="primary",
        )
    keyboard.button(
        text=get_button_label("Отмена", icon_key="cancel"),
        callback_data=AdminManagementCallback(action="cancel").pack(),
        style="danger",
    )
    keyboard.adjust(1)
    await state.set_state(AdminManagementForm.telegram_id)
    await message.answer(
        f"Выбери роль для пользователя {telegram_id}:",
        reply_markup=keyboard.as_markup(),
    )


@router.callback_query(
    AdminManagementForm.telegram_id,
    AdminManagementCallback.filter(F.action == "cancel"),
)
async def admin_management_cancel_handler(
    callback: CallbackQuery,
    state: FSMContext,
    role: str,
) -> None:
    if role != "owner":
        await state.clear()
        await callback.answer("Управлять ролями может только владелец.", show_alert=True)
        return
    await state.clear()
    await callback.answer("Назначение отменено.")
    if callback.message is not None:
        await callback.message.answer("Назначение роли отменено.")


@router.callback_query(DynamicRoleCallback.filter())
async def dynamic_role_assign_handler(
    callback: CallbackQuery,
    callback_data: DynamicRoleCallback,
    state: FSMContext,
    session: AsyncSession,
    config: AppConfig,
    role: str,
) -> None:
    if role != "owner":
        await callback.answer("Управлять ролями может только владелец.", show_alert=True)
        return
    data = await state.get_data()
    if (
        callback_data.role not in {"starosta", "moderator"}
        or data.get("target_telegram_id") != callback_data.telegram_id
        or callback_data.telegram_id == callback.from_user.id
        or callback_data.telegram_id in config.roles.owner
    ):
        await callback.answer("Назначение роли недопустимо или устарело.", show_alert=True)
        return
    if (
        callback_data.telegram_id in config.roles.starosta
        or callback_data.telegram_id in config.roles.moderators
    ):
        await callback.answer("Этот пользователь уже назначен через roles.yaml.", show_alert=True)
        return

    dynamic_role = await session.get(DynamicRole, callback_data.telegram_id)
    previous_role = dynamic_role.role if dynamic_role is not None else None
    if dynamic_role is None:
        session.add(
            DynamicRole(
                telegram_id=callback_data.telegram_id,
                role=callback_data.role,
            )
        )
    else:
        dynamic_role.role = callback_data.role
    await log_admin_action(
        session,
        callback.from_user.id,
        f"Выдал роль {callback_data.role}",
        f"telegram_id={callback_data.telegram_id}"
        + (f", прежняя роль={previous_role}" if previous_role else ""),
    )
    await session.commit()
    await state.clear()
    await callback.answer("Роль назначена.")
    if callback.message is not None:
        await callback.message.edit_text(
            f"{get_emoji('success')} Пользователю {callback_data.telegram_id} назначена роль "
            f"{callback_data.role}.",
            reply_markup=menu_back_keyboard(),
            parse_mode=ParseMode.HTML,
        )


@router.callback_query(AdminManagementCallback.filter(F.action == "remove"))
async def admin_management_remove_handler(
    callback: CallbackQuery,
    callback_data: AdminManagementCallback,
    session: AsyncSession,
    config: AppConfig,
    role: str,
) -> None:
    if role != "owner":
        await callback.answer("Управлять ролями может только владелец.", show_alert=True)
        return
    if (
        callback_data.telegram_id in config.roles.owner
        or callback_data.telegram_id in config.roles.starosta
        or callback_data.telegram_id in config.roles.moderators
    ):
        await callback.answer(
            "Роль из roles.yaml нельзя снять через интерфейс.",
            show_alert=True,
        )
        return
    dynamic_role = await session.get(DynamicRole, callback_data.telegram_id)
    if dynamic_role is None:
        await callback.answer("Динамическая роль уже снята.", show_alert=True)
        return
    removed_role = dynamic_role.role
    await session.delete(dynamic_role)
    await log_admin_action(
        session,
        callback.from_user.id,
        "Снял роль администратора",
        f"telegram_id={callback_data.telegram_id}, role={removed_role}",
    )
    await session.commit()
    await callback.answer("Права сняты.")
    if callback.message is not None:
        await show_admin_management(
            callback.message,
            session,
            config,
            role,
        )


async def _deliver_broadcast(
    bot: Bot,
    recipients: list[int],
    payload: BroadcastPayload,
) -> tuple[int, int]:
    sent = 0
    failed = 0
    unique_recipients = sorted(set(recipients))
    for index, telegram_id in enumerate(unique_recipients):
        try:
            await _send_broadcast_payload(bot, telegram_id, payload)
            sent += 1
        except TelegramAPIError:
            failed += 1
            logger.info("Не удалось доставить массовую рассылку одному из пользователей")
        if index + 1 < len(unique_recipients):
            await asyncio.sleep(0.05)
    return sent, failed


async def start_admin_broadcast(
    message: Message,
    state: FSMContext,
    role: str,
) -> None:
    if role not in ADMIN_ROLES:
        await message.answer("Эта команда только для админов.")
        return
    await state.clear()
    await state.set_state(BroadcastForm.content)
    await message.answer(
        "Отправь текст сообщения, фото или документ с подписью. "
        "HTML-разметка и вложения сохранятся при копировании.",
        reply_markup=menu_back_keyboard(),
    )


@router.message(Command("broadcast"))
async def admin_broadcast_start_handler(
    message: Message,
    state: FSMContext,
    role: str,
) -> None:
    await start_admin_broadcast(message, state, role)


@router.message(BroadcastForm.content, F.text | F.photo | F.document)
async def admin_broadcast_content_handler(
    message: Message,
    state: FSMContext,
    role: str,
    bot: Bot,
) -> None:
    if role not in ADMIN_ROLES:
        await state.clear()
        await message.answer("Эта функция только для админов.")
        return
    if message.from_user is None:
        await message.answer("Не удалось определить отправителя сообщения.")
        return
    payload = _broadcast_payload(message)
    if payload is None:
        await message.answer("Отправь текст, фото или документ с подписью.")
        return

    await state.update_data(
        admin_id=message.from_user.id,
        content_type=payload.content_type,
        content=payload.content,
        file_id=payload.file_id,
    )
    await _send_broadcast_payload(
        bot,
        message.chat.id,
        payload,
        reply_markup=_broadcast_confirmation_keyboard(),
    )
    await state.set_state(BroadcastForm.confirmation)


@router.message(BroadcastForm.content)
async def admin_broadcast_content_fallback_handler(message: Message) -> None:
    await message.answer("Отправь текст, фото или документ с подписью.")


@router.callback_query(
    BroadcastForm.confirmation,
    BroadcastCallback.filter(F.action == "cancel"),
)
async def admin_broadcast_cancel_handler(
    callback: CallbackQuery,
    state: FSMContext,
    role: str,
) -> None:
    if role not in ADMIN_ROLES:
        await state.clear()
        await callback.answer("Эта функция только для админов.", show_alert=True)
        return
    await state.clear()
    await callback.answer("Рассылка отменена.")
    if callback.message is not None:
        await callback.message.answer("Рассылка отменена.")


@router.callback_query(
    BroadcastForm.confirmation,
    BroadcastCallback.filter(F.action == "confirm"),
)
async def admin_broadcast_confirm_handler(
    callback: CallbackQuery,
    callback_data: BroadcastCallback,
    state: FSMContext,
    session: AsyncSession,
    bot: Bot,
    role: str,
) -> None:
    if role not in ADMIN_ROLES:
        await state.clear()
        await callback.answer("Эта функция только для админов.", show_alert=True)
        return

    data = await state.get_data()
    admin_id = data.get("admin_id")
    content_type = data.get("content_type")
    content = data.get("content")
    file_id = data.get("file_id")
    if (
        callback_data.action != "confirm"
        or content_type not in {"text", "photo", "document"}
        or not isinstance(content, str)
        or (file_id is not None and not isinstance(file_id, str))
        or admin_id != callback.from_user.id
    ):
        await state.clear()
        await callback.answer("Данные рассылки устарели. Начни заново.", show_alert=True)
        return

    await state.set_state(BroadcastForm.sending)
    await callback.answer("Начинаю рассылку.")
    recipients = list(await session.scalars(select(User.telegram_id)))
    payload = BroadcastPayload(content_type, content, file_id)
    await log_admin_action(
        session,
        callback.from_user.id,
        "Создал рассылку",
        f"тип={content_type}, адресатов={len(set(recipients))}",
    )
    await session.commit()
    try:
        sent, failed = await _deliver_broadcast(
            bot,
            recipients,
            payload,
        )
    finally:
        await state.clear()

    if callback.message is not None:
        await callback.message.answer(
            f"Рассылка завершена.\nОтправлено: {sent}, "
            f"Ошибок/заблокировали: {failed}"
        )


async def show_user_activity(
    message: Message,
    session: AsyncSession,
    config: AppConfig,
) -> None:
    result = await session.scalars(
        select(User)
        .where(User.last_active_at.is_not(None))
        .order_by(User.last_active_at.desc())
        .limit(15)
    )
    users = list(result)
    if not users:
        await message.answer("Активности пока нет.", reply_markup=menu_back_keyboard())
        return

    lines = [f"{get_emoji('activity')} <b>Последняя активность</b>"]
    local_timezone = ZoneInfo(config.bot.timezone)
    for user in users:
        active_at = user.last_active_at
        if active_at is None:
            continue
        if active_at.tzinfo is None:
            active_at = active_at.replace(tzinfo=timezone.utc)
        local_time = active_at.astimezone(local_timezone)
        name = html.escape(user.full_name or "Без имени")
        username = html.escape(f"@{user.username}" if user.username else "@не указан")
        action = html.escape(user.last_action or "Действие не указано")
        lines.append(f"• {local_time:%H:%M} — {name} ({username}) | {action}")

    await message.answer(
        "\n".join(lines),
        reply_markup=menu_back_keyboard(),
        parse_mode="HTML",
    )


@router.message(Command("activity"))
async def activity_command_handler(
    message: Message,
    role: str,
    session: AsyncSession,
    config: AppConfig,
) -> None:
    if role not in ADMIN_ROLES:
        await message.answer("Эта команда только для админов.")
        return
    await show_user_activity(message, session, config)


def _week_parity_keyboard() -> InlineKeyboardMarkup:
    keyboard = InlineKeyboardBuilder()
    keyboard.button(
        text=get_button_label("Нечётная"),
        callback_data=WeekParityCallback(parity="odd").pack(),
        style="primary",
    )
    keyboard.button(
        text=get_button_label("Чётная"),
        callback_data=WeekParityCallback(parity="even").pack(),
        style="primary",
    )
    add_cancel_button(keyboard)
    keyboard.adjust(2, 1)
    return keyboard.as_markup()


@router.message(Command("set_parity"))
async def set_week_parity_start_handler(
    message: Message,
    state: FSMContext,
    role: str,
) -> None:
    if role not in ADMIN_ROLES:
        await message.answer("Эта команда только для админов.")
        return
    await state.clear()
    await state.set_state(WeekParityForm.reference_date)
    await message.answer(
        "Введи дату прошедшего или текущего понедельника в формате ДД.ММ.ГГГГ.",
        reply_markup=menu_back_keyboard(),
    )


@router.message(WeekParityForm.reference_date, F.text)
async def set_week_parity_date_handler(
    message: Message,
    state: FSMContext,
    config: AppConfig,
    role: str,
) -> None:
    if role not in ADMIN_ROLES:
        await state.clear()
        await message.answer("Эта команда только для админов.")
        return
    value = (message.text or "").strip()
    try:
        reference_date = datetime.strptime(value, "%d.%m.%Y").date()
    except ValueError:
        reference_date = None
    if (
        reference_date is None
        or reference_date.strftime("%d.%m.%Y") != value
        or reference_date.weekday() != 0
        or reference_date > datetime.now(ZoneInfo(config.bot.timezone)).date()
    ):
        await message.answer("Нужен прошедший или текущий понедельник в формате ДД.ММ.ГГГГ.")
        return
    await state.update_data(reference_date=reference_date.isoformat())
    await state.set_state(WeekParityForm.parity)
    await message.answer("Выбери чётность этой недели:", reply_markup=_week_parity_keyboard())


@router.callback_query(WeekParityForm.parity, WeekParityCallback.filter())
async def set_week_parity_handler(
    callback: CallbackQuery,
    callback_data: WeekParityCallback,
    state: FSMContext,
    session: AsyncSession,
    role: str,
) -> None:
    if role not in ADMIN_ROLES:
        await state.clear()
        await callback.answer("Эта команда только для админов.", show_alert=True)
        return
    data = await state.get_data()
    reference_date = data.get("reference_date")
    if callback_data.parity not in {"odd", "even"} or not isinstance(reference_date, str):
        await state.clear()
        await callback.answer("Данные настройки потерялись. Начни заново.", show_alert=True)
        return
    for key, value in (
        (WEEK_PARITY_DATE_KEY, reference_date),
        (WEEK_PARITY_VALUE_KEY, callback_data.parity),
    ):
        setting = await session.get(BotSetting, key)
        if setting is None:
            session.add(BotSetting(key=key, value=value))
        else:
            setting.value = value
    await log_admin_action(
        session,
        callback.from_user.id,
        "Изменил чётность учебной недели",
        f"опорная дата={reference_date}, чётность={callback_data.parity}",
    )
    await session.commit()
    await state.clear()
    await callback.answer("Чётность сохранена.")
    if callback.message is not None:
        label = "нечётная" if callback_data.parity == "odd" else "чётная"
        await callback.message.edit_text(
            f"Опорная неделя сохранена: понедельник {datetime.fromisoformat(reference_date):%d.%m.%Y}, {label}.",
            reply_markup=menu_back_keyboard(),
        )


def _subject_keyboard(subjects: list[str]) -> InlineKeyboardMarkup:
    keyboard = InlineKeyboardBuilder()
    for index, subject in enumerate(subjects):
        keyboard.button(
            text=get_button_label(subject),
            callback_data=HomeworkSubjectCallback(
                action="select", subject_index=index
            ).pack(),
        )
    keyboard.button(
        text=get_button_label("Показать все предметы", icon_key="view"),
        callback_data=HomeworkSubjectCallback(action="all", subject_index=0).pack(),
        style="primary",
    )
    keyboard.button(
        text=get_button_label("Добавить новый предмет в список", icon_key="add"),
        callback_data=HomeworkSubjectCallback(action="new", subject_index=0).pack(),
        style="primary",
    )
    keyboard.button(
        text=get_button_label("В меню", icon_key="back_arrow"),
        callback_data=MenuCallback(action="home").pack(),
    )
    add_cancel_button(keyboard)
    keyboard.adjust(1)
    return keyboard.as_markup()


def _attachment_keyboard() -> InlineKeyboardMarkup:
    keyboard = InlineKeyboardBuilder()
    keyboard.button(
        text=get_button_label("Добавить ещё файл", icon_key="add"),
        callback_data=HomeworkAttachmentCallback(action="more").pack(),
        style="primary",
    )
    keyboard.button(
        text=get_button_label("Готово", icon_key="success"),
        callback_data=HomeworkAttachmentCallback(action="done").pack(),
        style="success",
    )
    keyboard.button(
        text=get_button_label("В меню", icon_key="back_arrow"),
        callback_data=MenuCallback(action="home").pack(),
    )
    add_cancel_button(keyboard)
    keyboard.adjust(1)
    return keyboard.as_markup()


def _deadline_keyboard() -> InlineKeyboardMarkup:
    keyboard = InlineKeyboardBuilder()
    keyboard.button(
        text=get_button_label("Обычное ДЗ", icon_key="homework"),
        callback_data=HomeworkDeadlineCallback(action="regular").pack(),
        style="primary",
    )
    keyboard.button(
        text=get_button_label("Указать дедлайн", icon_key="deadline"),
        callback_data=HomeworkDeadlineCallback(action="deadline").pack(),
        style="primary",
    )
    keyboard.button(
        text=get_button_label("В меню", icon_key="back_arrow"),
        callback_data=MenuCallback(action="home").pack(),
    )
    add_cancel_button(keyboard)
    keyboard.adjust(2, 1)
    return keyboard.as_markup()


def _student_subgroup_keyboard() -> InlineKeyboardMarkup:
    keyboard = InlineKeyboardBuilder()
    keyboard.button(
        text=get_button_label("Английская", icon_key="english"),
        callback_data=StudentSubgroupCallback(subgroup="english").pack(),
        style="primary",
    )
    keyboard.button(
        text=get_button_label("Французская", icon_key="french"),
        callback_data=StudentSubgroupCallback(subgroup="french").pack(),
        style="primary",
    )
    add_cancel_button(keyboard)
    keyboard.adjust(2, 1)
    return keyboard.as_markup()


async def start_student_add(
    message: Message,
    state: FSMContext,
    role: str,
) -> None:
    if role not in ADMIN_ROLES:
        await message.answer("Эта штука только для админов.")
        return
    await state.clear()
    await state.set_state(StudentAddForm.telegram_id)
    await message.answer(
        "Введи Telegram ID студента (ученик может узнать его через @getmyid_bot):",
        reply_markup=menu_back_keyboard(),
    )


@router.message(StudentAddForm.telegram_id, F.text)
async def student_add_id_handler(message: Message, state: FSMContext, role: str) -> None:
    if role not in ADMIN_ROLES:
        await state.clear()
        await message.answer("Эта штука только для админов.")
        return
    try:
        telegram_id = int((message.text or "").strip())
    except ValueError:
        telegram_id = 0
    if telegram_id <= 0:
        await message.answer("Telegram ID должен быть положительным числом.")
        return
    await state.update_data(telegram_id=telegram_id)
    await state.set_state(StudentAddForm.full_name)
    await message.answer(
        "Введи Фамилию и Имя студента:",
        reply_markup=menu_back_keyboard(),
    )


@router.message(StudentAddForm.telegram_id)
async def student_add_id_fallback_handler(message: Message) -> None:
    await message.answer("Пожалуйста, отправь Telegram ID числом.")


@router.message(StudentAddForm.full_name, F.text)
async def student_add_name_handler(message: Message, state: FSMContext, role: str) -> None:
    if role not in ADMIN_ROLES:
        await state.clear()
        await message.answer("Эта штука только для админов.")
        return
    full_name = (message.text or "").strip()
    if not full_name:
        await message.answer("Фамилия и имя не должны быть пустыми.")
        return
    await state.update_data(full_name=full_name)
    await state.set_state(StudentAddForm.subgroup)
    await message.answer("Выбери подгруппу:", reply_markup=_student_subgroup_keyboard())


@router.message(StudentAddForm.full_name)
async def student_add_name_fallback_handler(message: Message) -> None:
    await message.answer("Пожалуйста, отправь Фамилию и Имя текстом.")


@router.callback_query(StudentAddForm.subgroup, StudentSubgroupCallback.filter())
async def student_add_subgroup_handler(
    callback: CallbackQuery,
    callback_data: StudentSubgroupCallback,
    state: FSMContext,
    role: str,
    config: AppConfig,
    session: AsyncSession,
) -> None:
    if role not in ADMIN_ROLES:
        await state.clear()
        await callback.answer("Эта кнопка только для админов.", show_alert=True)
        return
    form_data = await state.get_data()
    telegram_id = form_data.get("telegram_id")
    full_name = form_data.get("full_name")
    if (
        not isinstance(telegram_id, int)
        or not isinstance(full_name, str)
        or callback_data.subgroup not in {"english", "french"}
    ):
        await state.clear()
        await callback.answer("Данные студента потерялись. Начни ещё раз.", show_alert=True)
        return
    if any(student.telegram_id == telegram_id for student in config.students):
        await state.clear()
        await callback.answer("Этот Telegram ID уже есть в списке.", show_alert=True)
        if callback.message is not None:
            await callback.message.edit_text(
                "Студент с таким Telegram ID уже добавлен.",
                reply_markup=menu_back_keyboard(),
            )
        return

    student = StudentConfig(
        telegram_id=telegram_id,
        full_name=full_name,
        subgroup=callback_data.subgroup,
    )
    try:
        append_student(config, student)
    except (OSError, ValueError) as exc:
        await state.clear()
        await callback.answer("Не удалось сохранить список студентов.", show_alert=True)
        if callback.message is not None:
            await callback.message.edit_text(
                "Не удалось сохранить students.yaml. Проверь права доступа к файлу.",
                reply_markup=menu_back_keyboard(),
            )
        return

    await log_admin_action(
            session,
            callback.from_user.id,
            "Добавил студента",
            f"telegram_id={telegram_id}, subgroup={callback_data.subgroup}",
    )
    await session.commit()
    await state.clear()
    await callback.answer("Студент добавлен.")
    if callback.message is not None:
        await callback.message.edit_text(
            f"{get_emoji('success')} Студент {html.escape(student.full_name)} "
            "успешно добавлен и получил доступ к боту!",
            reply_markup=menu_back_keyboard(),
            parse_mode=ParseMode.HTML,
        )


async def start_homework_add(
    message: Message,
    state: FSMContext,
    role: str,
    config: AppConfig,
    session: AsyncSession,
) -> None:
    """Start the existing homework form from a command or the admin panel."""
    if role not in ADMIN_ROLES:
        await message.answer("Эта штука только для админов.")
        return

    target_date = local_today(config)
    subjects = await upcoming_subjects(target_date, config, session)
    await state.clear()
    await state.update_data(
        target_date=target_date.isoformat(),
        available_subjects=subjects,
    )
    await state.set_state(HomeworkForm.subject)
    prompt = (
        f"{get_emoji('add')} ДОБАВЛЯЕМ ДЗ\n\n"
        "Выбери предмет из ближайших учебных дней:"
    )
    await message.answer(
        prompt,
        reply_markup=_subject_keyboard(subjects),
        parse_mode=ParseMode.HTML,
    )


@router.message(Command("hw_add"))
async def homework_add_start_handler(
    message: Message,
    state: FSMContext,
    role: str,
    config: AppConfig,
    session: AsyncSession,
) -> None:
    await start_homework_add(message, state, role, config, session)


async def _ask_for_homework_task(
    message: Message,
    state: FSMContext,
    subject: str,
    replace_message: bool = False,
) -> None:
    data = await state.get_data()
    target_date = data.get("target_date")
    date_label = ""
    if isinstance(target_date, str):
        try:
            date_label = f"\nДата ДЗ: {date.fromisoformat(target_date):%d.%m.%Y}\n"
        except ValueError:
            pass
    await state.update_data(subject=subject)
    await state.set_state(HomeworkForm.task)
    prompt = (
        f"{get_emoji('template')} {html.escape(subject)}\n\n"
        f"{date_label}\n"
        "Отправь текст, фото или документ.\n"
        "После этого можно будет добавить ещё вложения."
    )
    if replace_message:
        await message.edit_text(
            prompt,
            reply_markup=menu_back_keyboard(),
            parse_mode=ParseMode.HTML,
        )
    else:
        await message.answer(
            prompt,
            reply_markup=menu_back_keyboard(),
            parse_mode=ParseMode.HTML,
        )


@router.callback_query(
    HomeworkForm.subject,
    HomeworkSubjectCallback.filter(),
)
@router.callback_query(
    HomeworkForm.new_subject_confirmation,
    HomeworkSubjectCallback.filter(),
)
async def homework_subject_choice_handler(
    callback: CallbackQuery,
    callback_data: HomeworkSubjectCallback,
    state: FSMContext,
    config: AppConfig,
    session: AsyncSession,
) -> None:
    if callback.message is None:
        await callback.answer("Открой добавление ДЗ ещё раз.", show_alert=True)
        return
    if callback_data.action == "all":
        subjects = all_subjects(config)
        await state.update_data(available_subjects=subjects)
        await callback.answer()
        await callback.message.edit_text("Выбери предмет из полного списка расписания:", reply_markup=_subject_keyboard(subjects))
        return
    if callback_data.action == "new":
        await callback.answer()
        await state.set_state(HomeworkForm.new_subject_confirmation)
        await callback.message.edit_text(
            "Введи название нового предмета. Он будет добавлен вне расписания только после подтверждения.",
            reply_markup=menu_back_keyboard(),
        )
        return
    if callback_data.action == "cancel_new":
        await state.clear()
        await callback.answer("Добавление отменено.")
        await callback.message.edit_text("Новый предмет не добавлен.", reply_markup=menu_back_keyboard())
        return
    if callback_data.action == "confirm_new":
        subject = (await state.get_data()).get("pending_new_subject")
        if not isinstance(subject, str):
            await state.clear()
            await callback.answer("Название предмета потерялось.", show_alert=True)
            return
        await callback.answer()
        await _ask_for_homework_task(callback.message, state, subject, replace_message=True)
        return
    if callback_data.action != "select":
        await callback.answer("Не понял выбор предмета.", show_alert=True)
        return

    subjects = (await state.get_data()).get("available_subjects", [])
    if (
        not isinstance(subjects, list)
        or callback_data.subject_index not in range(len(subjects))
        or not isinstance(subjects[callback_data.subject_index], str)
    ):
        await state.clear()
        await callback.answer("Список предметов устарел. Начни добавление заново.", show_alert=True)
        return
    subject = subjects[callback_data.subject_index]
    today = local_today(config)
    today_subjects = await subjects_for_date(session, today, config)
    if subject in today_subjects:
        next_date = await next_lesson_date(
            session,
            subject,
            config,
            today,
            inclusive=False,
        )
        if next_date is None:
            await callback.answer("Следующий урок по этому предмету не найден.", show_alert=True)
            return
        await state.update_data(
            subject=subject,
            today_date=today.isoformat(),
            next_lesson_date=next_date.isoformat(),
        )
        await state.set_state(HomeworkForm.target_date)
        keyboard = InlineKeyboardBuilder()
        keyboard.button(
            text=get_button_label(
                f"Задали СЕГОДНЯ (на {next_date:%d.%m.%Y})",
                icon_key="template",
            ),
            callback_data=HomeworkTargetCallback(choice="next").pack(),
        )
        keyboard.button(
            text=get_button_label(
                f"Изменить ДЗ НА СЕГОДНЯ ({today:%d.%m.%Y})",
                icon_key="edit",
            ),
            callback_data=HomeworkTargetCallback(choice="today").pack(),
        )
        keyboard.button(
            text=get_button_label("В меню", icon_key="back_arrow"),
            callback_data=MenuCallback(action="home").pack(),
        )
        add_cancel_button(keyboard)
        keyboard.adjust(1)
        await callback.answer()
        await callback.message.edit_text(
            f"Для «{html.escape(subject)}» уже есть урок сегодня. "
            "К какому занятию относится это ДЗ?",
            reply_markup=keyboard.as_markup(),
        )
        return

    target_date = await next_lesson_date(session, subject, config, today)
    if target_date is None:
        await callback.answer("Ближайший урок по этому предмету не найден.", show_alert=True)
        return
    await state.update_data(target_date=target_date.isoformat())
    await callback.answer()
    await _ask_for_homework_task(
        callback.message,
        state,
        subject,
        replace_message=True,
    )


@router.callback_query(HomeworkForm.target_date, HomeworkTargetCallback.filter())
async def homework_target_choice_handler(
    callback: CallbackQuery,
    callback_data: HomeworkTargetCallback,
    state: FSMContext,
) -> None:
    if callback.message is None:
        await callback.answer("Открой добавление ДЗ ещё раз.", show_alert=True)
        return
    data = await state.get_data()
    choice_key = "next_lesson_date" if callback_data.choice == "next" else "today_date"
    target_date = data.get(choice_key)
    subject = data.get("subject")
    if callback_data.choice not in {"next", "today"} or not isinstance(target_date, str) or not isinstance(subject, str):
        await callback.answer("Выбор даты устарел. Начни добавление заново.", show_alert=True)
        await state.clear()
        return
    try:
        date.fromisoformat(target_date)
    except ValueError:
        await callback.answer("Дата некорректна. Начни добавление заново.", show_alert=True)
        await state.clear()
        return
    await state.update_data(target_date=target_date)
    await callback.answer()
    await _ask_for_homework_task(callback.message, state, subject, replace_message=True)


@router.message(HomeworkForm.subject, F.text)
async def homework_subject_handler(message: Message) -> None:
    await message.answer("Предмет выбирается кнопками. Свободный ввод здесь отключён.")


@router.message(HomeworkForm.new_subject_confirmation, F.text)
async def homework_new_subject_handler(message: Message, state: FSMContext) -> None:
    subject = (message.text or "").strip()
    if not subject:
        await message.answer("Название предмета не должно быть пустым.")
        return
    await state.update_data(pending_new_subject=subject)
    keyboard = InlineKeyboardBuilder()
    keyboard.button(
        text=get_button_label("Да, добавить", icon_key="success"),
        callback_data=HomeworkSubjectCallback(
            action="confirm_new",
            subject_index=0,
        ).pack(),
        style="success",
    )
    keyboard.button(
        text=get_button_label("Отмена", icon_key="cancel"),
        callback_data=HomeworkSubjectCallback(
            action="cancel_new",
            subject_index=0,
        ).pack(),
        style="danger",
    )
    add_cancel_button(keyboard)
    keyboard.adjust(2)
    await message.answer(f"Такого предмета нет в расписании, точно добавить «{subject}» как новый?", reply_markup=keyboard.as_markup())


@router.message(HomeworkForm.task)
async def homework_task_handler(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    previous_prompt_id = data.get("attachment_prompt_id")
    if isinstance(previous_prompt_id, int):
        try:
            await message.bot.edit_message_reply_markup(
                chat_id=message.chat.id,
                message_id=previous_prompt_id,
                reply_markup=None,
            )
        except TelegramAPIError:
            # The prompt may have been deleted or already replaced; accepting the
            # attachment is more important than cosmetic cleanup.
            pass
    attachments: list[dict[str, str]] = data.get("attachments", [])
    if message.photo:
        attachments.append({"file_id": message.photo[-1].file_id, "type": "photo"})
        task_text = message.caption
    elif message.document is not None:
        attachments.append({"file_id": message.document.file_id, "type": "document"})
        task_text = message.caption
    elif message.text is not None and not data.get("homework_text"):
        task_text = message.text.strip() or None
    else:
        await message.answer("Пришли фото или документ, либо нажми «Готово».", reply_markup=_attachment_keyboard())
        return

    await state.update_data(
        homework_text=task_text.strip() if task_text else data.get("homework_text"),
        attachments=attachments,
    )
    await state.set_state(HomeworkForm.attachments)
    prompt_message = await message.answer(
        "Можно прикрепить ещё фото или документы.\nКогда закончишь — нажми «Готово».",
        reply_markup=_attachment_keyboard(),
    )
    await state.update_data(attachment_prompt_id=prompt_message.message_id)


@router.message(HomeworkForm.attachments)
async def homework_more_attachments_handler(message: Message, state: FSMContext) -> None:
    await homework_task_handler(message, state)


@router.callback_query(HomeworkForm.attachments, HomeworkAttachmentCallback.filter())
async def homework_attachments_done_handler(callback: CallbackQuery, callback_data: HomeworkAttachmentCallback, state: FSMContext) -> None:
    if callback.message is None:
        return
    await callback.answer()
    if callback_data.action == "more":
        await callback.message.edit_text("Пришли следующее фото или документ.", reply_markup=_attachment_keyboard())
        return
    if callback_data.action == "done":
        await state.set_state(HomeworkForm.deadline)
        await state.update_data(attachment_prompt_id=None)
        await callback.message.edit_text("Финишная прямая: это обычное ДЗ или проект с дедлайном?", reply_markup=_deadline_keyboard())


async def _save_homework_from_form(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    deadline_date: date | None,
    updated_by: int | None,
    replace_message: bool = False,
) -> None:
    async def respond(text: str, reply_markup: InlineKeyboardMarkup | None = None) -> None:
        if replace_message:
            await message.edit_text(
                text,
                reply_markup=reply_markup,
                parse_mode=ParseMode.HTML,
            )
        else:
            await message.answer(
                text,
                reply_markup=reply_markup,
                parse_mode=ParseMode.HTML,
            )

    form_data = await state.get_data()
    subject = form_data.get("subject")
    target_date_value = form_data.get("target_date")
    homework_text = form_data.get("homework_text")
    attachments = form_data.get("attachments")
    if (
        not isinstance(subject, str)
        or not isinstance(target_date_value, str)
        or (homework_text is not None and not isinstance(homework_text, str))
        or not isinstance(attachments, list)
        or updated_by is None
    ):
        await state.clear()
        await respond(
            "Данные ДЗ потерялись где-то между партами. Начни ещё раз из меню.",
            menu_back_keyboard(),
        )
        return

    try:
        target_date = date.fromisoformat(target_date_value)
    except ValueError:
        await state.clear()
        await respond("Дата ДЗ некорректна. Начни добавление заново.", menu_back_keyboard())
        return

    updated_at = datetime.now(timezone.utc)
    existing_result = await session.scalars(
        select(Homework)
        .where(
            Homework.subject == subject,
            Homework.target_date == target_date,
        )
        .order_by(Homework.updated_at.desc(), Homework.id.desc())
        .limit(1)
    )
    existing = existing_result.first()

    if existing is not None:
        await state.update_data(
            existing_homework_id=existing.id,
            target_date=target_date.isoformat(),
            deadline_date=deadline_date.isoformat() if deadline_date else None,
        )
        await state.set_state(HomeworkForm.overwrite_confirmation)
        keyboard = InlineKeyboardBuilder()
        keyboard.button(
            text=get_button_label("Да, заменить", icon_key="success"),
            callback_data=HomeworkOverwriteCallback(action="yes").pack(),
            style="success",
        )
        keyboard.button(
            text=get_button_label("Нет, отменить", icon_key="cancel"),
            callback_data=HomeworkOverwriteCallback(action="no").pack(),
            style="danger",
        )
        add_cancel_button(keyboard)
        keyboard.adjust(2)
        await respond(
            f"ДЗ по «{html.escape(subject)}» уже есть. Заменить его? "
            "Прошлая версия останется в истории.",
            keyboard.as_markup(),
        )
        return

    session.add(
        Homework(
            subject=subject,
            text=homework_text,
            attachments=attachments,
            target_date=target_date,
            is_deadline=deadline_date is not None,
            deadline_date=deadline_date,
            updated_by=updated_by,
            created_at=updated_at,
            updated_at=updated_at,
        )
    )
    await log_admin_action(
        session,
        updated_by,
        "Добавил ДЗ",
        f"предмет={subject}, дата={target_date:%d.%m.%Y}",
    )
    await session.commit()
    await state.clear()
    await respond(
        f"{get_emoji('success')} ДЗ по «{html.escape(subject)}» добавлено. Красота.",
        menu_back_keyboard(),
    )


@router.callback_query(
    HomeworkForm.deadline,
    HomeworkDeadlineCallback.filter(),
)
async def homework_deadline_choice_handler(
    callback: CallbackQuery,
    callback_data: HomeworkDeadlineCallback,
    state: FSMContext,
    session: AsyncSession,
    role: str,
) -> None:
    if role not in ADMIN_ROLES:
        await state.clear()
        await callback.answer("Эта функция только для админов.", show_alert=True)
        return
    if callback.message is None or callback.from_user is None:
        await callback.answer("Не удалось определить пользователя.", show_alert=True)
        return
    if callback_data.action == "regular":
        await callback.answer()
        await _save_homework_from_form(
            callback.message,
            state,
            session,
            deadline_date=None,
            updated_by=callback.from_user.id,
            replace_message=True,
        )
        return
    if callback_data.action == "deadline":
        await callback.answer()
        await callback.message.edit_text(
            "Введи дату дедлайна в формате ДД.ММ.ГГГГ.",
            reply_markup=menu_back_keyboard(),
        )
        return
    await callback.answer("Не понял тип ДЗ.", show_alert=True)


@router.message(HomeworkForm.deadline, F.text)
async def homework_deadline_handler(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    role: str,
) -> None:
    if role not in ADMIN_ROLES:
        await state.clear()
        await message.answer("Эта функция только для админов.")
        return
    deadline_text = (message.text or "").strip()
    try:
        deadline_date = datetime.strptime(deadline_text, "%d.%m.%Y").date()
        if deadline_date.strftime("%d.%m.%Y") != deadline_text:
            raise ValueError("Deadline date must use DD.MM.YYYY format")
    except ValueError:
        await message.answer("Нужна дата строго в формате ДД.ММ.ГГГГ, например 05.10.2026.")
        return

    await _save_homework_from_form(
        message,
        state,
        session,
        deadline_date=deadline_date,
        updated_by=message.from_user.id if message.from_user is not None else None,
    )


@router.message(HomeworkForm.deadline)
async def homework_deadline_fallback_handler(message: Message) -> None:
    await message.answer("Пожалуйста, отправь дату текстом в формате ДД.ММ.ГГГГ.")


@router.callback_query(
    HomeworkForm.overwrite_confirmation,
    HomeworkOverwriteCallback.filter(),
)
async def homework_overwrite_handler(
    callback: CallbackQuery,
    callback_data: HomeworkOverwriteCallback,
    state: FSMContext,
    session: AsyncSession,
    role: str,
) -> None:
    if role not in ADMIN_ROLES:
        await state.clear()
        await callback.answer("Эта функция только для админов.", show_alert=True)
        return
    if callback_data.action == "no":
        await state.clear()
        await callback.answer("Добавление отменено.")
        if callback.message is not None:
            await callback.message.edit_text(
                "Добавление ДЗ отменено. Ничего не сломалось.",
                reply_markup=menu_back_keyboard(),
            )
        return
    if callback_data.action != "yes" or callback.from_user is None:
        await callback.answer("Некорректное действие.", show_alert=True)
        return

    data = await state.get_data()
    homework_id = data.get("existing_homework_id")
    subject = data.get("subject")
    homework_text = data.get("homework_text")
    target_date_value = data.get("target_date")
    attachments = data.get("attachments")
    deadline_value = data.get("deadline_date")
    if (
        not isinstance(homework_id, int)
        or not isinstance(subject, str)
        or not isinstance(target_date_value, str)
        or (homework_text is not None and not isinstance(homework_text, str))
        or not isinstance(attachments, list)
        or (deadline_value is not None and not isinstance(deadline_value, str))
    ):
        await state.clear()
        await callback.answer("Данные ДЗ потеряны. Начни ещё раз.", show_alert=True)
        return

    homework = await session.get(Homework, homework_id)
    if homework is None:
        await state.clear()
        await callback.answer("Исходное ДЗ уже удалено.", show_alert=True)
        return

    try:
        deadline_date = date.fromisoformat(deadline_value) if deadline_value else None
        target_date = date.fromisoformat(target_date_value)
    except ValueError:
        await state.clear()
        await callback.answer("Некорректная дата дедлайна.", show_alert=True)
        return

    changed_at = datetime.now(timezone.utc)
    session.add(
        HomeworkHistory(
            homework_id=homework.id,
            previous_text=homework.text,
            previous_attachments=list(homework.attachments or []),
            previous_target_date=homework.target_date,
            previous_is_deadline=homework.is_deadline,
            previous_deadline_date=homework.deadline_date,
            changed_by=callback.from_user.id,
            changed_at=changed_at,
        )
    )
    homework.text = homework_text
    homework.attachments = attachments
    homework.target_date = target_date
    homework.is_deadline = deadline_date is not None
    homework.deadline_date = deadline_date
    homework.updated_by = callback.from_user.id
    homework.updated_at = changed_at
    await log_admin_action(
        session,
        callback.from_user.id,
        "Изменил ДЗ",
        f"предмет={subject}, дата={target_date:%d.%m.%Y}",
    )
    await session.commit()
    await state.clear()

    await callback.answer("ДЗ обновлено.")
    if callback.message is not None:
        await callback.message.edit_text(
            f"{get_emoji('success')} ДЗ по «{html.escape(subject)}» обновлено; "
            "старая версия сохранена в истории.",
            reply_markup=menu_back_keyboard(),
            parse_mode=ParseMode.HTML,
        )


def _schedule_time_date_keyboard() -> InlineKeyboardMarkup:
    keyboard = InlineKeyboardBuilder()
    keyboard.button(
        text=get_button_label("Сегодня", icon_key="today"),
        callback_data=ScheduleTimeCallback(action="today").pack(),
        style="primary",
    )
    keyboard.button(
        text=get_button_label("Завтра", icon_key="tomorrow"),
        callback_data=ScheduleTimeCallback(action="tomorrow").pack(),
        style="primary",
    )
    keyboard.button(
        text=get_button_label("Ввести дату", icon_key="date"),
        callback_data=ScheduleTimeCallback(action="manual").pack(),
        style="primary",
    )
    add_cancel_button(keyboard)
    keyboard.adjust(2, 1, 1)
    return keyboard.as_markup()


def _schedule_time_mode_keyboard() -> InlineKeyboardMarkup:
    keyboard = InlineKeyboardBuilder()
    keyboard.button(
        text=get_button_label("Сократить ВСЕ уроки", icon_key="shortened"),
        callback_data=ScheduleTimeCallback(action="all").pack(),
        style="primary",
    )
    keyboard.button(
        text=get_button_label("Сократить конкретную пару", icon_key="goal"),
        callback_data=ScheduleTimeCallback(action="specific").pack(),
        style="primary",
    )
    add_cancel_button(keyboard)
    keyboard.adjust(1)
    return keyboard.as_markup()


def _schedule_time_lesson_keyboard(lesson_count: int) -> InlineKeyboardMarkup:
    keyboard = InlineKeyboardBuilder()
    for lesson_number in range(1, lesson_count + 1):
        keyboard.button(
            text=get_button_label(f"{lesson_number} пара"),
            callback_data=ScheduleTimeLessonCallback(
                lesson_number=lesson_number
            ).pack(),
            style="primary",
        )
    add_cancel_button(keyboard)
    keyboard.adjust(3)
    return keyboard.as_markup()


def _schedule_time_confirmation_keyboard(has_existing: bool) -> InlineKeyboardMarkup:
    keyboard = InlineKeyboardBuilder()
    keyboard.button(
        text=get_button_label("Применить и разослать", icon_key="send"),
        callback_data=ScheduleTimeCallback(action="confirm").pack(),
        style="success",
    )
    keyboard.button(
        text=get_button_label("Отмена", icon_key="cancel"),
        callback_data=ScheduleTimeCallback(action="cancel").pack(),
        style="danger",
    )
    if has_existing:
        keyboard.button(
            text=get_button_label(
                "Сбросить сокращения на этот день",
                icon_key="trash",
            ),
            callback_data=ScheduleTimeCallback(action="reset").pack(),
            style="danger",
        )
    keyboard.adjust(1)
    return keyboard.as_markup()


async def start_schedule_time_override(
    message: Message,
    state: FSMContext,
    role: str,
) -> None:
    if role not in ADMIN_ROLES:
        await message.answer("Настраивать звонки могут только администраторы.")
        return
    await state.clear()
    await state.set_state(ScheduleTimeOverrideForm.target_date)
    await message.answer(
        f"{get_emoji('shortened')} СОКРАЩЁННЫЕ УРОКИ\n\n"
        "Выбери дату расписания звонков:",
        reply_markup=_schedule_time_date_keyboard(),
        parse_mode=ParseMode.HTML,
    )


async def _schedule_lessons_for_date(
    target_date: date,
    config: AppConfig,
    session: AsyncSession,
) -> list[Lesson]:
    weekday_index = target_date.weekday()
    if weekday_index >= len(WEEKDAYS):
        return []
    schedule = parse_schedule(config.schedule)
    parity = await get_configured_week_parity(session, target_date, config)
    return schedule.lessons_for(WEEKDAYS[weekday_index], parity)


async def _schedule_time_open_mode(
    message: Message,
    state: FSMContext,
    target_date: date,
    config: AppConfig,
    session: AsyncSession,
) -> None:
    if target_date < datetime.now(ZoneInfo(config.bot.timezone)).date():
        await message.answer("Нельзя изменить расписание звонков для прошедшей даты.")
        return
    lessons = await _schedule_lessons_for_date(target_date, config, session)
    if not lessons:
        await message.answer("На выбранную дату в расписании нет уроков.")
        return
    await state.update_data(target_date=target_date.isoformat())
    await state.set_state(ScheduleTimeOverrideForm.mode)
    await message.answer(
        f"Дата: {target_date:%d.%m.%Y}. Как сократить расписание?",
        reply_markup=_schedule_time_mode_keyboard(),
    )


@router.callback_query(
    ScheduleTimeOverrideForm.target_date,
    ScheduleTimeCallback.filter(),
)
async def schedule_time_date_handler(
    callback: CallbackQuery,
    callback_data: ScheduleTimeCallback,
    state: FSMContext,
    config: AppConfig,
    session: AsyncSession,
    role: str,
) -> None:
    if role not in ADMIN_ROLES:
        await callback.answer("Эта функция доступна только администраторам.", show_alert=True)
        return
    if callback.message is None:
        await callback.answer("Открой настройку звонков заново.", show_alert=True)
        return
    choice = callback_data.action
    if choice == "manual":
        await state.set_state(ScheduleTimeOverrideForm.manual_date)
        await callback.answer()
        await callback.message.edit_text(
            "Введи дату в формате ДД.ММ.ГГГГ.",
            reply_markup=menu_back_keyboard(),
        )
        return

    today = datetime.now(ZoneInfo(config.bot.timezone)).date()
    target_date = {"today": today, "tomorrow": today + timedelta(days=1)}.get(choice)
    if target_date is None:
        await callback.answer("Неизвестный вариант даты.", show_alert=True)
        return
    await callback.answer()
    await _schedule_time_open_mode(
        callback.message,
        state,
        target_date,
        config,
        session,
    )


@router.message(ScheduleTimeOverrideForm.manual_date, F.text)
async def schedule_time_manual_date_handler(
    message: Message,
    state: FSMContext,
    config: AppConfig,
    session: AsyncSession,
    role: str,
) -> None:
    if role not in ADMIN_ROLES:
        await state.clear()
        await message.answer("Настраивать звонки могут только администраторы.")
        return
    value = (message.text or "").strip()
    try:
        target_date = datetime.strptime(value, "%d.%m.%Y").date()
        if target_date.strftime("%d.%m.%Y") != value:
            raise ValueError
    except ValueError:
        await message.answer("Нужна дата в формате ДД.ММ.ГГГГ, например 28.10.2026.")
        return
    await _schedule_time_open_mode(message, state, target_date, config, session)


@router.message(ScheduleTimeOverrideForm.manual_date)
async def schedule_time_manual_date_fallback(message: Message) -> None:
    await message.answer("Отправь дату текстом в формате ДД.ММ.ГГГГ.")


@router.callback_query(
    ScheduleTimeOverrideForm.mode,
    ScheduleTimeCallback.filter(),
)
async def schedule_time_mode_handler(
    callback: CallbackQuery,
    callback_data: ScheduleTimeCallback,
    state: FSMContext,
    config: AppConfig,
    session: AsyncSession,
    role: str,
) -> None:
    if role not in ADMIN_ROLES:
        await callback.answer("Эта функция доступна только администраторам.", show_alert=True)
        return
    if callback.message is None:
        await callback.answer("Открой настройку звонков заново.", show_alert=True)
        return
    data = await state.get_data()
    try:
        target_date = date.fromisoformat(data["target_date"])
    except (KeyError, TypeError, ValueError):
        await state.clear()
        await callback.answer("Дата потерялась. Начни настройку заново.", show_alert=True)
        return

    if callback_data.action == "all":
        await state.update_data(mode="all")
        await state.set_state(ScheduleTimeOverrideForm.all_duration)
        await callback.answer()
        await callback.message.edit_text(
            "Введи длительность каждой пары в минутах, например 45.",
            reply_markup=menu_back_keyboard(),
        )
        return
    if callback_data.action != "specific":
        await callback.answer("Неизвестный режим.", show_alert=True)
        return
    lessons = await _schedule_lessons_for_date(target_date, config, session)
    if not lessons:
        await callback.answer("На эту дату нет уроков.", show_alert=True)
        return
    await state.set_state(ScheduleTimeOverrideForm.lesson_number)
    await callback.answer()
    await callback.message.edit_text(
        "Выбери пару, которую нужно сократить:",
        reply_markup=_schedule_time_lesson_keyboard(min(len(lessons), 6)),
    )


@router.callback_query(
    ScheduleTimeOverrideForm.lesson_number,
    ScheduleTimeLessonCallback.filter(),
)
async def schedule_time_lesson_handler(
    callback: CallbackQuery,
    callback_data: ScheduleTimeLessonCallback,
    state: FSMContext,
    config: AppConfig,
    session: AsyncSession,
    role: str,
) -> None:
    if role not in ADMIN_ROLES:
        await callback.answer("Эта функция доступна только администраторам.", show_alert=True)
        return
    if callback.message is None:
        await callback.answer("Открой настройку звонков заново.", show_alert=True)
        return
    data = await state.get_data()
    try:
        target_date = date.fromisoformat(data["target_date"])
    except (KeyError, TypeError, ValueError):
        await state.clear()
        await callback.answer("Дата потерялась. Начни настройку заново.", show_alert=True)
        return
    lessons = await _schedule_lessons_for_date(target_date, config, session)
    if not 1 <= callback_data.lesson_number <= min(len(lessons), 6):
        await callback.answer("Такой пары нет в расписании на выбранную дату.", show_alert=True)
        return
    await state.update_data(
        mode="specific",
        lesson_number=callback_data.lesson_number,
    )
    await state.set_state(ScheduleTimeOverrideForm.specific_duration)
    await callback.answer()
    await callback.message.edit_text(
        f"Введи длительность {callback_data.lesson_number}-й пары в минутах, например 45.",
        reply_markup=menu_back_keyboard(),
    )


def _parse_minutes(value: str, *, allow_zero: bool = False) -> int | None:
    if not re.fullmatch(r"\d{1,3}", value.strip()):
        return None
    minutes = int(value.strip())
    minimum = 0 if allow_zero else 1
    return minutes if minimum <= minutes <= 180 else None


async def _prepare_schedule_time_preview(
    message: Message,
    state: FSMContext,
    config: AppConfig,
    session: AsyncSession,
) -> None:
    data = await state.get_data()
    try:
        target_date = date.fromisoformat(data["target_date"])
        mode = data["mode"]
        duration = int(data["duration_minutes"])
        lesson_number = data.get("lesson_number")
        big_break = data.get("big_break_minutes")
    except (KeyError, TypeError, ValueError):
        await state.clear()
        await message.answer("Настройки потерялись. Начни оформление заново.")
        return
    lessons = await _schedule_lessons_for_date(target_date, config, session)
    if not lessons or (mode == "specific" and lesson_number not in range(1, len(lessons) + 1)):
        await state.clear()
        await message.answer("Расписание на эту дату изменилось. Начни настройку заново.")
        return
    existing = list(
        await session.scalars(
            select(ScheduleTimeOverride)
            .where(ScheduleTimeOverride.target_date == target_date)
            .order_by(ScheduleTimeOverride.id)
        )
    )
    old_big_break = next(
        (row.big_break_minutes for row in reversed(existing) if row.big_break_minutes is not None),
        None,
    )
    draft = ScheduleTimeOverride(
        target_date=target_date,
        lesson_number=None if mode == "all" else lesson_number,
        duration_minutes=duration,
        big_break_minutes=big_break if big_break is not None else old_big_break,
        created_at=datetime.now(timezone.utc),
    )
    effective = (
        [draft]
        if mode == "all"
        else [row for row in existing if row.lesson_number != lesson_number] + [draft]
    )
    clocks = calculate_lesson_times(
        (lesson.time for lesson in lessons),
        config.schedule.lesson_duration_minutes,
        effective,
    )
    lines = [
        f"{get_emoji('shortened')} Предпросмотр звонков на {target_date:%d.%m.%Y}:",
    ]
    for index, clock in enumerate(clocks, start=1):
        suffix = " (сокращённая)" if clock.shortened else ""
        lines.append(f"• {index} пара: {clock.start} — {clock.end}{suffix}")
    await state.set_state(ScheduleTimeOverrideForm.confirmation)
    await state.update_data(
        mode=mode,
        lesson_number=lesson_number,
        big_break_minutes=draft.big_break_minutes,
    )
    await message.answer(
        "\n".join(lines),
        reply_markup=_schedule_time_confirmation_keyboard(bool(existing)),
        parse_mode=ParseMode.HTML,
    )


@router.message(ScheduleTimeOverrideForm.all_duration, F.text)
async def schedule_time_all_duration_handler(
    message: Message,
    state: FSMContext,
    role: str,
) -> None:
    if role not in ADMIN_ROLES:
        await state.clear()
        await message.answer("Настраивать звонки могут только администраторы.")
        return
    minutes = _parse_minutes((message.text or "").strip())
    if minutes is None:
        await message.answer("Введи целое число минут от 1 до 180.")
        return
    await state.update_data(duration_minutes=minutes)
    await state.set_state(ScheduleTimeOverrideForm.all_break)
    await message.answer("Введи длительность большой перемены в минутах (0–180).")


@router.message(ScheduleTimeOverrideForm.all_break, F.text)
async def schedule_time_all_break_handler(
    message: Message,
    state: FSMContext,
    config: AppConfig,
    session: AsyncSession,
    role: str,
) -> None:
    if role not in ADMIN_ROLES:
        await state.clear()
        await message.answer("Настраивать звонки могут только администраторы.")
        return
    minutes = _parse_minutes((message.text or "").strip(), allow_zero=True)
    if minutes is None:
        await message.answer("Введи целое число минут от 0 до 180.")
        return
    await state.update_data(big_break_minutes=minutes)
    await _prepare_schedule_time_preview(message, state, config, session)


@router.message(ScheduleTimeOverrideForm.specific_duration, F.text)
async def schedule_time_specific_duration_handler(
    message: Message,
    state: FSMContext,
    role: str,
) -> None:
    if role not in ADMIN_ROLES:
        await state.clear()
        await message.answer("Настраивать звонки могут только администраторы.")
        return
    minutes = _parse_minutes((message.text or "").strip())
    if minutes is None:
        await message.answer("Введи целое число минут от 1 до 180.")
        return
    await state.update_data(duration_minutes=minutes)
    await state.set_state(ScheduleTimeOverrideForm.specific_break)
    keyboard = InlineKeyboardBuilder()
    keyboard.button(
        text=get_button_label("Изменить большую перемену", icon_key="edit"),
        callback_data=ScheduleTimeCallback(action="set_break").pack(),
        style="primary",
    )
    keyboard.button(
        text=get_button_label("Оставить как есть", icon_key="check"),
        callback_data=ScheduleTimeCallback(action="keep_break").pack(),
        style="success",
    )
    keyboard.adjust(1)
    await message.answer(
        "Задать новую длительность большой перемены?",
        reply_markup=keyboard.as_markup(),
    )


@router.callback_query(
    ScheduleTimeOverrideForm.specific_break,
    ScheduleTimeCallback.filter(),
)
async def schedule_time_specific_break_choice_handler(
    callback: CallbackQuery,
    callback_data: ScheduleTimeCallback,
    state: FSMContext,
    config: AppConfig,
    session: AsyncSession,
    role: str,
) -> None:
    if role not in ADMIN_ROLES:
        await callback.answer("Эта функция доступна только администраторам.", show_alert=True)
        return
    if callback.message is None:
        await callback.answer("Открой настройку звонков заново.", show_alert=True)
        return
    if callback_data.action == "keep_break":
        await state.update_data(big_break_minutes=None)
        await callback.answer()
        await _prepare_schedule_time_preview(callback.message, state, config, session)
    elif callback_data.action == "set_break":
        await callback.answer()
        await callback.message.edit_text(
            "Введи длительность большой перемены в минутах (0–180).",
            reply_markup=menu_back_keyboard(),
        )
        await state.set_state(ScheduleTimeOverrideForm.specific_break)
        await state.update_data(waiting_for_break_minutes=True)
    else:
        await callback.answer("Неизвестный вариант.", show_alert=True)


@router.message(ScheduleTimeOverrideForm.specific_break, F.text)
async def schedule_time_specific_break_handler(
    message: Message,
    state: FSMContext,
    config: AppConfig,
    session: AsyncSession,
    role: str,
) -> None:
    if role not in ADMIN_ROLES:
        await state.clear()
        await message.answer("Настраивать звонки могут только администраторы.")
        return
    data = await state.get_data()
    if not data.get("waiting_for_break_minutes"):
        await message.answer("Выбери, задать новую перемену или оставить текущую.")
        return
    minutes = _parse_minutes((message.text or "").strip(), allow_zero=True)
    if minutes is None:
        await message.answer("Введи целое число минут от 0 до 180.")
        return
    await state.update_data(
        big_break_minutes=minutes,
        waiting_for_break_minutes=False,
    )
    await _prepare_schedule_time_preview(message, state, config, session)


async def _send_schedule_time_broadcast(
    bot: Bot,
    config: AppConfig,
    session: AsyncSession,
    target_date: date,
    lessons: list[Lesson],
    overrides: list[ScheduleTimeOverride],
) -> tuple[int, int]:
    clocks = calculate_lesson_times(
        (lesson.time for lesson in lessons),
        config.schedule.lesson_duration_minutes,
        overrides,
    )
    text = (
        f"{get_emoji('notice')} Изменение расписания звонков "
        f"на {target_date:%d.%m.%Y}!\n"
        + "\n".join(
            f"• {index} пара: {clock.start} — {clock.end}"
            for index, clock in enumerate(clocks, start=1)
        )
    )
    recipients = {student.telegram_id for student in config.students}
    recipients.update(await session.scalars(select(User.telegram_id)))
    sent = 0
    failed = 0
    for index, telegram_id in enumerate(sorted(recipients)):
        try:
            await bot.send_message(
                chat_id=telegram_id,
                text=text,
                parse_mode=ParseMode.HTML,
            )
            sent += 1
        except (TelegramForbiddenError, TelegramBadRequest):
            failed += 1
            logger.info("Не удалось доставить уведомление об изменении звонков")
        except TelegramAPIError:
            failed += 1
            logger.exception("Ошибка Telegram при рассылке изменения звонков")
        if index + 1 < len(recipients):
            await asyncio.sleep(0.05)
    return sent, failed


@router.callback_query(
    ScheduleTimeOverrideForm.confirmation,
    ScheduleTimeCallback.filter(),
)
async def schedule_time_confirmation_handler(
    callback: CallbackQuery,
    callback_data: ScheduleTimeCallback,
    state: FSMContext,
    session: AsyncSession,
    config: AppConfig,
    bot: Bot,
    role: str,
) -> None:
    if role not in ADMIN_ROLES:
        await callback.answer("Эта функция доступна только администраторам.", show_alert=True)
        return
    if callback.message is None:
        await callback.answer("Откройте настройку звонков заново.", show_alert=True)
        return
    data = await state.get_data()
    try:
        target_date = date.fromisoformat(data["target_date"])
    except (KeyError, TypeError, ValueError):
        await state.clear()
        await callback.answer("Данные настройки потерялись.", show_alert=True)
        return

    if callback_data.action == "cancel":
        await state.clear()
        await callback.answer("Изменение отменено.")
        await callback.message.edit_text(
            "Настройка сокращённых уроков отменена.",
            reply_markup=menu_back_keyboard(),
        )
        return

    existing = list(
        await session.scalars(
            select(ScheduleTimeOverride)
            .where(ScheduleTimeOverride.target_date == target_date)
            .order_by(ScheduleTimeOverride.id)
        )
    )
    if callback_data.action == "reset":
        if not existing:
            await callback.answer("На этот день нет сокращений.", show_alert=True)
            return
        for row in existing:
            await session.delete(row)
        await log_admin_action(
            session,
            callback.from_user.id,
            "Сбросил сокращённые уроки",
            f"дата={target_date:%d.%m.%Y}",
        )
        await session.commit()
        lessons = await _schedule_lessons_for_date(target_date, config, session)
        sent, failed = await _send_schedule_time_broadcast(
            bot,
            config,
            session,
            target_date,
            lessons,
            [],
        )
        await state.clear()
        await callback.answer("Сокращения сброшены.")
        await callback.message.edit_text(
            f"{get_emoji('success')} Сокращения на этот день сброшены; "
            "стандартное расписание восстановлено.\n"
            f"ЛС: отправлено {sent}, ошибок/заблокировали {failed}.",
            reply_markup=menu_back_keyboard(),
            parse_mode=ParseMode.HTML,
        )
        return

    if callback_data.action != "confirm":
        await callback.answer("Неизвестное действие.", show_alert=True)
        return
    try:
        duration = int(data["duration_minutes"])
        mode = data["mode"]
        lesson_number = data.get("lesson_number")
        big_break = data.get("big_break_minutes")
    except (KeyError, TypeError, ValueError):
        await state.clear()
        await callback.answer("Данные настройки потерялись.", show_alert=True)
        return
    if mode not in {"all", "specific"} or duration <= 0:
        await state.clear()
        await callback.answer("Некорректные параметры. Начни заново.", show_alert=True)
        return
    if mode == "all":
        for row in existing:
            await session.delete(row)
        lesson_number = None
    else:
        if not isinstance(lesson_number, int):
            await callback.answer("Не удалось определить номер пары.", show_alert=True)
            return
        old_big_break = next(
            (row.big_break_minutes for row in reversed(existing) if row.big_break_minutes is not None),
            None,
        )
        for row in existing:
            if row.lesson_number == lesson_number:
                await session.delete(row)
        if big_break is None:
            big_break = old_big_break

    session.add(
        ScheduleTimeOverride(
            target_date=target_date,
            lesson_number=lesson_number,
            duration_minutes=duration,
            big_break_minutes=big_break,
            created_at=datetime.now(timezone.utc),
        )
    )
    await log_admin_action(
        session,
        callback.from_user.id,
        "Изменил расписание звонков",
        f"дата={target_date:%d.%m.%Y}, "
        f"{'все пары' if mode == 'all' else f'пара={lesson_number}'}, "
        f"длительность={duration} мин",
    )
    await session.commit()
    lessons = await _schedule_lessons_for_date(target_date, config, session)
    saved_overrides = list(
        await session.scalars(
            select(ScheduleTimeOverride)
            .where(ScheduleTimeOverride.target_date == target_date)
            .order_by(ScheduleTimeOverride.id)
        )
    )
    sent, failed = await _send_schedule_time_broadcast(
        bot,
        config,
        session,
        target_date,
        lessons,
        saved_overrides,
    )
    await state.clear()
    await callback.answer("Расписание звонков обновлено.")
    await callback.message.edit_text(
        f"{get_emoji('success')} Изменения применены.\n"
        f"ЛС: отправлено {sent}, ошибок/заблокировали {failed}.",
        reply_markup=menu_back_keyboard(),
        parse_mode=ParseMode.HTML,
    )
