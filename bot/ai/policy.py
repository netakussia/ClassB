from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any


ALLOWED_INTENTS = {
    "navigation_homework",
    "navigation_schedule",
    "navigation_help",
    "bot_question",
    "joke_or_banter",
    "ordinary_reply",
    "uncertain",
    "no_action",
}
SERIOUS_TERMS = re.compile(
    r"\b(срочно|больно|болит|болезн\w*|плох[а-я]*|умер\w*|смерт\w*|"
    r"травл\w*|издева\w*|депресс\w*|паник\w*|тревож\w*|суицид\w*|"
    r"самоуб\w*|самоповреж\w*|насили\w*|потерял\w*.{0,30}близк\w*|"
    r"горю\w*|похорон\w*|кризис\w*|страшно|боюсь|не справляюсь|"
    r"груст\w*|плачу|слез\w*|увол\w*|развод\w*|расстал\w*|"
    r"emergency|suicid\w*|self[- ]harm|abuse|serious|sad|scared|"
    r"afraid|grief|loss|fired|panic|anxious|depressed)\b",
    re.IGNORECASE,
)
HTML_OR_COMMAND = re.compile(r"</?[a-z][^>]*>|(^|\s)/[a-z_]+", re.IGNORECASE)
BOT_REFERENCE = re.compile(r"\b(?:бот\w*|the bot|this bot|telegram bot)\b", re.IGNORECASE)
BOT_CAPABILITY_QUESTION = re.compile(
    r"\b(?:(?:что\s+(?:ты\s+)?(?:умеешь|можешь))|"
    r"(?:ты\s+)?(?:умеешь|можешь)\s+(?:ли\s+)?ты|"
    r"(?:ты\s+)?(?:можешь|умеешь)\s+(?:ли\s+)?(?:менять|изменять|"
    r"выдавать|назначать|удалять|создавать|отправлять|показывать|"
    r"открывать|управлять|смотреть)|"
    r"(?:что|какие)\s+(?:умеет|может)\s+(?:(?:этот|наш)\s+)?бот\w*|"
    r"какие\s+(?:у\s+тебя\s+)?(?:функци\w*|возможност\w*|команд\w*)|"
    r"какие\s+(?:функци\w*|возможност\w*|команд\w*)\s+у\s+бот\w*|"
    r"как\s+пользоваться\s+бот\w*|"
    r"what\s+can\s+(?:you|the\s+bot|this\s+bot)\s+do|"
    r"can\s+you\s+(?:change|manage|assign|grant|delete|edit|"
    r"create|send|show|open|view)\b|"
    r"what\s+(?:features|capabilities|commands)\s+(?:does\s+)?(?:the\s+)?bot)\b",
    re.IGNORECASE,
)
BOT_CAPABILITY_CONTEXT = re.compile(
    r"\bбот\w*\b.{0,80}\b(?:умеешь|умеет|можешь|может|функци\w*|"
    r"возможност\w*|команд\w*|что делать|как)\b",
    re.IGNORECASE,
)
ENGLISH_BOT_CAPABILITY_CONTEXT = re.compile(
    r"\b(?:the\s+bot|this\s+bot|telegram\s+bot)\b.{0,80}\b"
    r"(?:can|features|capabilities|commands|do)\b",
    re.IGNORECASE,
)
EXPLICIT_BANTER_REQUEST = re.compile(
    r"\b(?:шут\w*|анекдот\w*|мем\w*|пошути|рассмеши|"
    r"jok\w*|meme\w*)\b",
    re.IGNORECASE,
)
ABUSIVE_RESPONSE = re.compile(
    r"\b(идиот|дурак|дебил|урод|туп(ой|ая|ые)|лох|ничтожество)\b",
    re.IGNORECASE,
)
PROMPT_OR_SECRET_DISCLOSURE = re.compile(
    r"(system prompt|system instruction|системн\w+\s+(инструкц|промпт)|"
    r"api[_ -]?key|AIza[0-9A-Za-z_-]{20,})",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class AIResult:
    intent: str
    response: str


def truncate_input(text: str, max_chars: int) -> str:
    """Bound untrusted text before it can enter an external request."""
    if max_chars <= 0:
        raise ValueError("max_chars must be positive")
    return text[:max_chars]


def parse_model_result(raw: str) -> AIResult | None:
    try:
        payload: Any = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    intent = payload.get("intent")
    response = payload.get("response")
    if not isinstance(intent, str) or intent not in ALLOWED_INTENTS:
        return None
    if not isinstance(response, str):
        return None
    response = response.strip()
    if (
        len(response) > 1000
        or HTML_OR_COMMAND.search(response)
        or BOT_REFERENCE.search(response)
        or ABUSIVE_RESPONSE.search(response)
        or PROMPT_OR_SECRET_DISCLOSURE.search(response)
    ):
        return None
    if intent in {"uncertain", "no_action"}:
        response = ""
    return AIResult(intent=intent, response=response)


def is_serious(text: str) -> bool:
    return bool(SERIOUS_TERMS.search(text))


def is_bot_capability_question(text: str) -> bool:
    return bool(
        BOT_CAPABILITY_QUESTION.search(text)
        or BOT_CAPABILITY_CONTEXT.search(text)
        or ENGLISH_BOT_CAPABILITY_CONTEXT.search(text)
    )


def is_explicit_banter_request(text: str) -> bool:
    return bool(EXPLICIT_BANTER_REQUEST.search(text))


def explicit_navigation(text: str) -> str | None:
    """Recognize clear section requests locally; never infer navigation from a joke."""
    normalized = re.sub(r"[^\w\sё]", " ", text.casefold())
    normalized = " ".join(normalized.split())
    if any(token in normalized for token in ("шут", "анекдот", "мем", "хаха")):
        return None
    if any(phrase in normalized for phrase in ("что задали", "домашнее задание", "покажи дз", "открой дз")):
        return "navigation_homework"
    if any(phrase in normalized for phrase in ("покажи расписание", "открой расписание", "расписание на сегодня")):
        return "navigation_schedule"
    if any(phrase in normalized for phrase in ("покажи помощь", "открой помощь", "как пользоваться ботом")):
        return "navigation_help"
    return None
