"""Shared inline navigation for the class bot."""
from __future__ import annotations

from typing import Any

from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters.callback_data import CallbackData
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from bot.keyboards.inline import InlineKeyboardBuilder
from bot.utils.emojis import get_btn_emoji, get_button_label


ADMIN_ROLES = {"owner", "starosta", "moderator"}


class MenuCallback(CallbackData, prefix="menu"):
    action: str


class CancelCallback(CallbackData, prefix="cancel"):
    action: str = "form"


class DocumentCallback(CallbackData, prefix="docs"):
    action: str
    value: str


DOCUMENT_CATEGORIES = {
    "labs": f"{get_btn_emoji('laboratory')} Лабораторные",
    "manuals": f"{get_btn_emoji('manual')} Методички",
    "templates": f"{get_btn_emoji('template')} Шаблоны",
    "cloud": f"{get_btn_emoji('cloud')} Облако",
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
        text=get_button_label("Документы", icon_key="folder"),
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
    keyboard.button(
        text=get_button_label("Добавить ДЗ", icon_key="add"),
        callback_data=MenuCallback(action="add_homework").pack(),
        style="primary",
    )
    keyboard.button(
        text=get_button_label("Замена уроков", icon_key="replacement"),
        callback_data=MenuCallback(action="replace").pack(),
        style="primary",
    )
    keyboard.button(
        text=get_button_label("Создать рассылку", icon_key="notice"),
        callback_data=MenuCallback(action="broadcast").pack(),
        style="primary",
    )
    keyboard.button(
        text=get_button_label("Сокращения", icon_key="timer"),
        callback_data=MenuCallback(action="schedule_time_overrides").pack(),
        style="primary",
    )
    keyboard.button(
        text=get_button_label("Загрузить файл", icon_key="attachment"),
        callback_data=MenuCallback(action="add_document").pack(),
        style="primary",
    )
    keyboard.button(
        text=get_button_label("Новое событие", icon_key="add"),
        callback_data=MenuCallback(action="add_event").pack(),
        style="primary",
    )
    keyboard.button(
        text=get_button_label("Добавить студента", icon_key="add"),
        callback_data=MenuCallback(action="add_student").pack(),
        style="primary",
    )
    keyboard.button(
        text=get_button_label("Рапортичка отсутствий", icon_key="cases"),
        callback_data=MenuCallback(action="class_absences").pack(),
        style="primary",
    )
    keyboard.button(
        text=get_button_label("Снять бан", icon_key="restriction"),
        callback_data=MenuCallback(action="restrictions").pack(),
        style="primary",
    )
    keyboard.button(
        text=get_button_label("Активность", icon_key="activity"),
        callback_data=MenuCallback(action="activity").pack(),
        style="primary",
    )
    keyboard.button(
        text=get_button_label("История действий админов", icon_key="history"),
        callback_data=MenuCallback(action="admin_history").pack(),
        style="primary",
    )
    if role == "owner":
        keyboard.button(
            text=get_button_label("Управление администраторами", icon_key="users"),
            callback_data=MenuCallback(action="admin_management").pack(),
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
            text=get_button_label(label),
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
