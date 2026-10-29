from __future__ import annotations

from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from bot.handlers.ai import _settings_text
from config_loader import AIConfig, _ai_config


class AIConfigTests(unittest.TestCase):
    def test_missing_file_uses_disabled_safe_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict("os.environ", {}, clear=True):
                config = _ai_config(Path(directory))
        self.assertFalse(config.enabled)
        self.assertFalse(config.humor_enabled)
        self.assertFalse(config.personalization_enabled)
        self.assertFalse(config.reactions_enabled)
        self.assertEqual(config.model, "gemini-3.5-flash-lite")
        self.assertEqual(config.user_daily_limit, 25)
        self.assertEqual(config.user_per_minute_limit, 6)
        self.assertEqual(config.global_daily_limit, 500)
        self.assertEqual(config.global_per_minute_limit, 15)
        self.assertEqual(config.global_tokens_per_minute, 250000)
        self.assertEqual(config.max_input_chars, 500)
        self.assertEqual(config.retries, 1)
        self.assertIsNone(config.api_key)

    def test_api_key_is_read_only_from_environment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict("os.environ", {"GEMINI_API_KEY": "test-key"}, clear=True):
                config = _ai_config(Path(directory))
        self.assertEqual(config.api_key, "test-key")

    def test_model_can_be_overridden_in_ai_yaml(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_file = Path(directory) / "ai.yaml"
            config_file.write_text(
                'model: "gemini-2.5-flash"\n',
                encoding="utf-8",
            )
            config = _ai_config(Path(directory))
        self.assertEqual(config.model, "gemini-2.5-flash")

    def test_user_quotas_cannot_exceed_approved_limits(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_file = Path(directory) / "ai.yaml"
            config_file.write_text("user_per_minute_limit: 7\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "6 requests/minute"):
                _ai_config(Path(directory))

    def test_retries_are_limited_to_one(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_file = Path(directory) / "ai.yaml"
            config_file.write_text("retries: 2\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "0 or 1"):
                _ai_config(Path(directory))

    def test_settings_disclose_local_data_retention(self) -> None:
        text = _settings_text(
            SimpleNamespace(
                ai=AIConfig(
                    activity_retention_days=12,
                    quota_retention_days=18,
                )
            )
        )

        self.assertIn("до 12 дней", text)
        self.assertIn("до 18 дней", text)
        self.assertIn("хеш шутки", text)


if __name__ == "__main__":
    unittest.main()
