from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bot.utils import emojis


class EmojiTests(unittest.TestCase):
    def setUp(self) -> None:
        emojis._load_emoji_config.cache_clear()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.config_path = Path(self.temp_dir.name) / "emojis.yaml"
        self.config_path.write_text(
            "schedule:\n"
            '  unicode: "📅"\n'
            '  custom_id: "5368324170671202286"\n'
            "homework:\n"
            '  unicode: "📚"\n'
            "  custom_id: null\n"
            "empty:\n"
            '  unicode: "✅"\n'
            '  custom_id: ""\n'
            "success:\n"
            '  unicode: "✅"\n'
            "save:\n"
            '  unicode: "💾"\n'
            "send:\n"
            '  unicode: "🚀"\n'
            "back_arrow:\n"
            '  unicode: "⬅️"\n'
            "trash:\n"
            '  unicode: "🗑️"\n',
            encoding="utf-8",
        )
        self.path_patch = patch.object(emojis, "EMOJI_CONFIG_PATH", self.config_path)
        self.path_patch.start()

    def tearDown(self) -> None:
        self.path_patch.stop()
        emojis._load_emoji_config.cache_clear()
        self.temp_dir.cleanup()

    def test_message_emoji_uses_custom_tag_and_fallback(self) -> None:
        self.assertEqual(
            emojis.get_emoji("schedule"),
            '<tg-emoji emoji-id="5368324170671202286">📅</tg-emoji>',
        )
        self.assertEqual(emojis.get_emoji("homework"), "📚")
        self.assertEqual(emojis.get_emoji("empty"), "✅")

    def test_numeric_yaml_custom_id_is_supported(self) -> None:
        self.config_path.write_text(
            "schedule:\n  unicode: '📅'\n  custom_id: 5235616308359359800\n",
            encoding="utf-8",
        )
        emojis._load_emoji_config.cache_clear()

        self.assertEqual(
            emojis.get_emoji("schedule"),
            '<tg-emoji emoji-id="5235616308359359800">📅</tg-emoji>',
        )

    def test_button_emoji_is_always_unicode(self) -> None:
        self.assertEqual(emojis.get_btn_emoji("schedule"), "📅")
        self.assertNotIn("<", emojis.get_btn_emoji("schedule"))

    def test_button_labels_use_only_configured_unicode_icons(self) -> None:
        self.assertEqual(
            emojis.get_button_label("Сохранить", icon_key="save"),
            "💾 Сохранить",
        )
        self.assertEqual(
            emojis.get_button_label("Назад", icon_key="back_arrow"),
            "⬅️ Назад",
        )
        self.assertEqual(
            emojis.get_button_label("Сбросить", icon_key="trash"),
            "🗑️ Сбросить",
        )
        self.assertNotIn(
            "<tg-emoji",
            emojis.get_button_label("Отправить", icon_key="send"),
        )

    def test_invalid_custom_id_is_rejected(self) -> None:
        self.config_path.write_text(
            "schedule:\n  unicode: '📅'\n  custom_id: '<script>'\n",
            encoding="utf-8",
        )
        emojis._load_emoji_config.cache_clear()

        with self.assertRaisesRegex(ValueError, "decimal string, integer, or null"):
            emojis.get_emoji("schedule")

    def test_unknown_key_is_reported(self) -> None:
        with self.assertRaisesRegex(KeyError, "Unknown emoji key"):
            emojis.get_emoji("unknown")


if __name__ == "__main__":
    unittest.main()
