from aiogram.types import KeyboardButton, ReplyKeyboardMarkup


def main_reply_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📅 Расписание"), KeyboardButton(text="📚 ДЗ")],
            [KeyboardButton(text="📂 Документы"), KeyboardButton(text="🙋 Мои отсутствия")],
            [KeyboardButton(text="👥 Моя подгруппа")],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )