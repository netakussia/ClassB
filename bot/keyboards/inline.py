from __future__ import annotations

from typing import Any

from aiogram.utils.keyboard import InlineKeyboardBuilder as AiogramInlineKeyboardBuilder

from bot.utils.emojis import (
    _ButtonLabel,
    get_btn_emoji,
    get_custom_emoji_id,
)


class InlineKeyboardBuilder(AiogramInlineKeyboardBuilder):
    def button(
        self,
        *,
        icon_key: str | None = None,
        **kwargs: Any,
    ) -> InlineKeyboardBuilder:
        text = kwargs.get("text")
        if icon_key is None and isinstance(text, _ButtonLabel):
            icon_key = text.icon_key

        if icon_key is not None:
            custom_id = get_custom_emoji_id(icon_key)
            if custom_id is not None:
                if isinstance(text, _ButtonLabel):
                    kwargs["text"] = text.label
                elif isinstance(text, str):
                    unicode_emoji = get_btn_emoji(icon_key)
                    if text.startswith(unicode_emoji):
                        kwargs["text"] = text[len(unicode_emoji):].lstrip()
                kwargs["icon_custom_emoji_id"] = custom_id
            elif isinstance(text, _ButtonLabel):
                kwargs["text"] = str(text)
        elif isinstance(text, _ButtonLabel):
            kwargs["text"] = str(text)

        super().button(**kwargs)
        return self
