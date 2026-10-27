"""Shared inline navigation for the class bot."""
from __future__ import annotations

from typing import Any

from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters.callback_data import CallbackData
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from bot.keyboards.inline import InlineKeyboardBuilder
from bot.utils.emojis import get_button_label
from bot.utils.permissions import role_allows


ADMIN_ROLES = {"owner", "starosta", "moderator"}


class MenuCallback(CallbackData, prefix="menu"):
    action: str


class CancelCallback(CallbackData, prefix="cancel"):
    action: str = "form"


class DocumentCallback(CallbackData, prefix="docs"):
    action: str
    value: str


DOCUMENT_CATEGORIES = {
    "labs": "Лабораторные",
    "manuals": "Методички",
    "templates": "Шаблоны",
    "cloud": "Облако",
}
DOCUMENT_CATEGORY_EMOJI = {
    "labs": "laboratory",
    "manuals": "manual",
    "templates": "template",
    "cloud": "cloud",
}


def add_cancel_button(keyboard: InlineKeyboardBuilder) -> None:
    """Append the same cancellation action to an in-progress form."""
    keyboard.button(
        text=get_button_label("Отменить", icon_key="cancel"),
        callback_data=CancelCallback().pack(),
        style="danger",
    )


def main_menu_keyboard(role: str) -> InlineKeyboardMarkup:
    """Build the compact navigation panel appropriate for the user's role."""
    keyboard = InlineKeyboardBuilder()
    keyboard.button(
        text=get_button_label("ДЗ", icon_key="homework"),
        callback_data=MenuCallback(action="homework").pack(),
        style="primary",
    )
    keyboard.button(
        text=get_button_label("Расписание", icon_key="schedule"),
        callback_data=MenuCallback(action="schedule").pack(),
        style="primary",
    )
    keyboard.button(
        text=get_button_label("Меня не будет", icon_key="absence"),
        callback_data=MenuCallback(action="absence").pack(),
        style="primary",
    )
    keyboard.button(
        text=get_button_label("Мои заявки", icon_key="cases"),
        callback_data=MenuCallback(action="my_absences").pack(),
        style="primary",
    )
    keyboard.button(
        text=get_button_label("Мой статус", icon_key="user"),
        callback_data=MenuCallback(action="profile").pack(),
        style="primary",
    )
    keyboard.button(
        text=get_button_label("Моя подгруппа", icon_key="users"),
        callback_data=MenuCallback(action="my_group").pack(),
        style="primary",
    )
    keyboard.button(
        text=get_button_label("Документы", icon_key="documents"),
        callback_data=MenuCallback(action="documents").pack(),
        style="primary",
    )
    keyboard.button(
        text=get_button_label("Как это работает", icon_key="help"),
        callback_data=MenuCallback(action="help").pack(),
        style="primary",
    )
    keyboard.adjust(2, 2, 2, 2)

    if role in ADMIN_ROLES:
        keyboard.button(
            text=get_button_label("Админка", icon_key="settings"),
            callback_data=MenuCallback(action="admin").pack(),
            style="primary",
        )
        keyboard.adjust(2, 2, 2, 2, 1)

    return keyboard.as_markup()


def admin_menu_keyboard(role: str = "user") -> InlineKeyboardMarkup:
    keyboard = InlineKeyboardBuilder()
    items = (
        ("homework_management", "Добавить ДЗ", "add", "add_homework"),
        ("replacements", "Замена уроков", "replacement", "replace"),
        ("broadcasts", "Создать рассылку", "notice", "broadcast"),
        ("schedule_time", "Сокращённые уроки", "timer", "schedule_time_overrides"),
        ("documents_manage", "Загрузить файл", "attachment", "add_document"),
        ("events_manage", "Новое событие", "add", "add_event"),
        ("student_manage", "Добавить студента", "add", "add_student"),
        ("bans", "Управление банами", "restriction", "bans"),
        ("absence_review", "Рапортичка отсутствий", "cases", "class_absences"),
        ("absence_review", "Снять ограничение отсутствий", "restriction", "restrictions"),
        ("activity", "Активность", "activity", "activity"),
        ("admin_history", "История действий админов", "history", "admin_history"),
        ("admin_management", "Управление администраторами", "users", "admin_management"),
    )
    for permission, label, icon_key, action in items:
        if role_allows(role, permission):
            keyboard.button(
                text=get_button_label(label, icon_key=icon_key),
                callback_data=MenuCallback(action=action).pack(),
                style="primary",
            )
    keyboard.button(
        text=get_button_label("Обычное меню", icon_key="back_arrow"),
        callback_data=MenuCallback(action="home").pack(),
    )
    keyboard.adjust(2, 1, 2, 1, 2, 1, 2, 1)
    return keyboard.as_markup()


def menu_back_keyboard(text: str | None = None) -> InlineKeyboardMarkup:
    keyboard = InlineKeyboardBuilder()
    keyboard.button(
        text=text or get_button_label("В меню", icon_key="back_arrow"),
        callback_data=MenuCallback(action="home").pack(),
    )
    add_cancel_button(keyboard)
    keyboard.adjust(2)
    return keyboard.as_markup()


def document_categories_keyboard(
    action: str,
    *,
    include_cancel: bool = False,
) -> InlineKeyboardMarkup:
    keyboard = InlineKeyboardBuilder()
    for category, label in DOCUMENT_CATEGORIES.items():
        keyboard.button(
            text=get_button_label(
                label,
                icon_key=DOCUMENT_CATEGORY_EMOJI[category],
            ),
            callback_data=DocumentCallback(action=action, value=category).pack(),
            style="primary",
        )
    keyboard.button(
        text=get_button_label("Назад в главное меню", icon_key="back_arrow"),
        callback_data=MenuCallback(action="home").pack(),
    )
    if include_cancel:
        add_cancel_button(keyboard)
    keyboard.adjust(1)
    return keyboard.as_markup()


async def remove_inline_screen(callback: CallbackQuery) -> None:
    """Remove the previous bot screen before opening a new section.

    Telegram may reject deletion for an old or already removed message; navigation
    must still work in that case, so those errors are intentionally ignored.
    """
    if callback.message is None:
        return
    try:
        await callback.message.delete()
    except TelegramAPIError:
        return


async def edit_text_if_changed(
    message: Message,
    text: str,
    **kwargs: Any,
) -> None:
    """Ignore Telegram's harmless error when an edit repeats identical content."""
    try:
        await message.edit_text(text, **kwargs)
    except TelegramBadRequest as exc:
        if "message is not modified" not in str(exc).casefold():
            raise
