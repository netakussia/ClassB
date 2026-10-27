from __future__ import annotations

import asyncio
import logging
import re
import time

from aiogram.types import Message, ReactionTypeEmoji

logger = logging.getLogger(__name__)
COOLDOWN_SECONDS = 5 * 60
MAX_MESSAGE_CHARS = 80
THANKS = {
    "спасибо", "спасибо большое", "благодарю", "мерси", "thanks", "thank you",
}
CONGRATULATIONS = {
    "поздравляю", "с днём рождения", "с днем рождения", "с праздником", "congratulations",
}
_LAST_REACTION: dict[int, float] = {}
_LOCK = asyncio.Lock()


def reaction_for_text(text: str) -> str | None:
    normalized = re.sub(r"[!?.…,\s]+$", "", text.casefold().strip())
    if len(normalized) > MAX_MESSAGE_CHARS or "\n" in normalized:
        return None
    if normalized in THANKS:
        return "👍"
    if normalized in CONGRATULATIONS:
        return "🎉"
    return None


async def react_to_message(message: Message, user_id: int) -> bool:
    emoji = reaction_for_text(message.text or "")
    if emoji is None:
        return False
    now = time.monotonic()
    async with _LOCK:
        previous = _LAST_REACTION.get(user_id)
        if previous is not None and now - previous < COOLDOWN_SECONDS:
            return False
        _LAST_REACTION[user_id] = now
        for stale_id, timestamp in list(_LAST_REACTION.items()):
            if now - timestamp >= COOLDOWN_SECONDS:
                _LAST_REACTION.pop(stale_id, None)
    try:
        result = await message.bot.set_message_reaction(
            chat_id=message.chat.id,
            message_id=message.message_id,
            reaction=[ReactionTypeEmoji(type="emoji", emoji=emoji)],
        )
    except Exception as exc:
        logger.warning("Telegram rejected a local AI reaction (%s)", type(exc).__name__)
        return False
    return result
