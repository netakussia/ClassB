from __future__ import annotations

from typing import Any

from aiogram.utils.keyboard import InlineKeyboardBuilder as AiogramInlineKeyboardBuilder


class InlineKeyboardBuilder(AiogramInlineKeyboardBuilder):
    def button(self, **kwargs: Any) -> InlineKeyboardBuilder:
        super().button(**kwargs)
        return self
