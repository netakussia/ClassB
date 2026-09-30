"""Shared inline navigation for the class bot."""
from __future__ import annotations

from aiogram.exceptions import TelegramAPIError
from aiogram.filters.callback_data import CallbackData
from aiogram.types import CallbackQuery, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder


ADMIN_ROLES = {"owner", "starosta", "moderator"}


class MenuCallback(CallbackData, prefix="menu"):
    action: str


def main_menu_keyboard(role: str) -> InlineKeyboardMarkup:
    """Build the compact navigation panel appropriate for the user's role."""
    keyboard = InlineKeyboardBuilder()
    keyboard.button(
        text="📚 Домашнее задание",
        callback_data=MenuCallback(action="homework").pack(),
    )
    keyboard.button(
        text="🗓 Расписание",
        callback_data=MenuCallback(action="schedule").pack(),
    )
    keyboard.button(
        text="🙋 Меня не будет",
        callback_data=MenuCallback(action="absence").pack(),
    )
    keyboard.button(
        text="📋 Мои заявки",
        callback_data=MenuCallback(action="my_absences").pack(),
    )
    keyboard.button(
        text="👤 Мой статус",
        callback_data=MenuCallback(action="profile").pack(),
    )
    keyboard.button(
        text="👥 Моя подгруппа",
        callback_data=MenuCallback(action="my_group").pack(),
    )
    keyboard.button(
        text="ℹ️ Как это работает",
        callback_data=MenuCallback(action="help").pack(),
    )
    keyboard.adjust(2, 2, 2, 1)

    if role in ADMIN_ROLES:
        keyboard.button(
            text="🛠 Админ-панель",
            callback_data=MenuCallback(action="admin").pack(),
        )
        keyboard.adjust(2, 2, 2, 1, 1)

    return keyboard.as_markup()


def admin_menu_keyboard() -> InlineKeyboardMarkup:
    keyboard = InlineKeyboardBuilder()
    keyboard.button(
        text="➕ Добавить ДЗ",
        callback_data=MenuCallback(action="add_homework").pack(),
    )
    keyboard.button(
        text="🔁 Добавить замену",
        callback_data=MenuCallback(action="replace").pack(),
    )
    keyboard.button(
        text="📋 Заявки класса",
        callback_data=MenuCallback(action="class_absences").pack(),
    )
    keyboard.button(
        text="🔓 Снять ограничения",
        callback_data=MenuCallback(action="restrictions").pack(),
    )
    keyboard.button(
        text="🏠 Обычное меню",
        callback_data=MenuCallback(action="home").pack(),
    )
    keyboard.adjust(2, 2, 1)
    return keyboard.as_markup()


def menu_back_keyboard(text: str = "🏠 В меню") -> InlineKeyboardMarkup:
    keyboard = InlineKeyboardBuilder()
    keyboard.button(text=text, callback_data=MenuCallback(action="home").pack())
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
