from __future__ import annotations

import html
from functools import lru_cache
from pathlib import Path
import re
from typing import Any

import yaml


EMOJI_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "emojis.yaml"
_CUSTOM_ID_PATTERN = re.compile(r"[0-9]+")
_EMOJI_CODEPOINT_RANGES = (
    (0x2190, 0x21FF),
    (0x2300, 0x23FF),
    (0x2460, 0x24FF),
    (0x2500, 0x25FF),
    (0x2600, 0x27FF),
    (0x2900, 0x2BFF),
    (0x1F000, 0x1FAFF),
)
_EMOJI_SEQUENCE_CHARACTERS = {
    0x200D,
    0x20E3,
    0xFE0E,
    0xFE0F,
}


def _is_emoji_character(character: str) -> bool:
    codepoint = ord(character)
    return (
        any(start <= codepoint <= end for start, end in _EMOJI_CODEPOINT_RANGES)
        or 0x1F3FB <= codepoint <= 0x1F3FF
        or codepoint in _EMOJI_SEQUENCE_CHARACTERS
    )


def _validate_emoji_config(raw: Any) -> dict[str, tuple[str, str | None]]:
    if not isinstance(raw, dict):
        raise ValueError(f"Expected an emoji mapping in {EMOJI_CONFIG_PATH}")

    parsed: dict[str, tuple[str, str | None]] = {}
    for key, entry in raw.items():
        if not isinstance(key, str) or not key:
            raise ValueError("Emoji config keys must be non-empty strings")
        if not isinstance(entry, dict):
            raise ValueError(f"Expected a mapping for emoji '{key}'")
        unicode_emoji = entry.get("unicode")
        custom_id = entry.get("custom_id")
        if (
            not isinstance(unicode_emoji, str)
            or not unicode_emoji
            or not any(
                any(start <= ord(character) <= end for start, end in _EMOJI_CODEPOINT_RANGES)
                for character in unicode_emoji
            )
            or any(not _is_emoji_character(character) for character in unicode_emoji)
        ):
            raise ValueError(f"Invalid Unicode emoji fallback for '{key}'")
        if custom_id in (None, ""):
            parsed[key] = unicode_emoji, None
            continue
        if type(custom_id) is int:
            custom_id = str(custom_id)
        if not isinstance(custom_id, str) or not _CUSTOM_ID_PATTERN.fullmatch(custom_id):
            raise ValueError(
                f"custom_id for emoji '{key}' must be a decimal string, integer, or null"
            )
        parsed[key] = unicode_emoji, custom_id
    return parsed


@lru_cache(maxsize=4)
def _load_emoji_config(path: Path) -> dict[str, tuple[str, str | None]]:
    try:
        with path.open(encoding="utf-8") as config_file:
            raw = yaml.safe_load(config_file)
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"Emoji configuration file not found: {path}") from exc
    parsed = _validate_emoji_config(raw)
    if not parsed:
        raise ValueError(f"Emoji configuration is empty: {path}")
    return parsed


def _emoji_value(key: str) -> tuple[str, str | None]:
    try:
        return _load_emoji_config(EMOJI_CONFIG_PATH)[key]
    except KeyError as exc:
        raise KeyError(f"Unknown emoji key: {key}") from exc


def get_emoji(key: str) -> str:
    """Return an HTML Telegram custom-emoji tag or its plain Unicode fallback."""
    unicode_emoji, custom_id = _emoji_value(key)
    escaped_unicode = html.escape(unicode_emoji, quote=True)
    if custom_id is None:
        return escaped_unicode
    return f'<tg-emoji emoji-id="{custom_id}">{escaped_unicode}</tg-emoji>'


def get_btn_emoji(key: str) -> str:
    """Return only the Unicode fallback, safe for inline and reply button labels."""
    return _emoji_value(key)[0]


def get_button_label(
    label: str,
    *,
    icon_key: str | None = None,
) -> str:
    """Build a plain-text button label with an optional Unicode icon."""
    icon = f" {get_btn_emoji(icon_key)}" if icon_key is not None else ""
    return f"{icon} {label}".lstrip()
