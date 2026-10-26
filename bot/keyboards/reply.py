from aiogram.types import KeyboardButton, ReplyKeyboardMarkup

from bot.utils.emojis import get_btn_emoji


def main_reply_keyboard(role: str = "user") -> ReplyKeyboardMarkup:
    keyboard = [
        [
            KeyboardButton(text=f"{get_btn_emoji('schedule')} Расписание"),
            KeyboardButton(text=f"{get_btn_emoji('homework')} ДЗ"),
        ],
        [
            KeyboardButton(text=f"{get_btn_emoji('folder')} Документы"),
            KeyboardButton(text=f"{get_btn_emoji('absence')} Мои отсутствия"),
        ],
        [KeyboardButton(text=f"{get_btn_emoji('users')} Моя подгруппа")],
    ]
    if role in {"owner", "starosta", "moderator"}:
        keyboard.append([KeyboardButton(text=f"{get_btn_emoji('settings')} Админка")])
    return ReplyKeyboardMarkup(
        keyboard=keyboard,
        resize_keyboard=True,
        is_persistent=True,
    )