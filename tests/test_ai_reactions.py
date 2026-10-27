from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from aiogram.types import Chat, Message, User as TelegramUser

from bot.ai.reactions import (
    _LAST_REACTION,
    reaction_for_text,
    react_to_message,
)


def _message(text: str, bot) -> Message:
    message = Message.model_construct(
        message_id=15,
        date=datetime.now(timezone.utc),
        chat=Chat.model_construct(id=123, type="private"),
        from_user=TelegramUser.model_construct(id=123, is_bot=False, first_name="Student"),
        text=text,
    )
    return message.as_(bot)


class AIReactionTests(unittest.TestCase):
    def test_reaction_rules_are_exact_and_bounded(self) -> None:
        self.assertEqual(reaction_for_text("Спасибо!"), "👍")
        self.assertEqual(reaction_for_text("С днём рождения!"), "🎉")
        self.assertIsNone(reaction_for_text("Спасибо, но мне очень плохо"))
        self.assertIsNone(reaction_for_text("x" * 81))
        self.assertIsNone(reaction_for_text("/help"))
        self.assertIsNone(reaction_for_text("спасибо\nпомоги"))

    def test_cooldown_limits_user_to_one_reaction_per_five_minutes(self) -> None:
        async def test() -> None:
            _LAST_REACTION.clear()
            bot = SimpleNamespace(set_message_reaction=AsyncMock(return_value=True))
            first = await react_to_message(_message("Спасибо", bot), 123)
            second = await react_to_message(_message("Благодарю", bot), 123)
            self.assertTrue(first)
            self.assertFalse(second)
            bot.set_message_reaction.assert_awaited_once()
            _LAST_REACTION.clear()

        asyncio.run(test())

    def test_telegram_reaction_failure_is_handled_without_propagating(self) -> None:
        async def test() -> None:
            _LAST_REACTION.clear()
            bot = SimpleNamespace(
                set_message_reaction=AsyncMock(side_effect=RuntimeError("telegram failure"))
            )
            result = await react_to_message(_message("Спасибо", bot), 456)
            self.assertFalse(result)
            bot.set_message_reaction.assert_awaited_once()
            _LAST_REACTION.clear()

        asyncio.run(test())


if __name__ == "__main__":
    unittest.main()
