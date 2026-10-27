from __future__ import annotations

from datetime import date, datetime, timezone
import unittest

from bot.handlers.absent import LessonChoiceCallback, _lesson_keyboard
from bot.handlers.hw import _homework_subject_keyboard
from bot.handlers.replacements import _lesson_picker_keyboard
from config_loader import Lesson
from database.models import ScheduleOverride
from bot.utils.emojis import get_custom_emoji_id


def _buttons(markup):
    return [button for row in markup.inline_keyboard for button in row]


class DynamicButtonStyleTests(unittest.TestCase):
    def test_homework_subjects_show_success_only_when_homework_exists(self) -> None:
        buttons = _buttons(
            _homework_subject_keyboard(
                ["Математика", "Физика"],
                {"Математика"},
            )
        )

        self.assertEqual(
            [button.text for button in buttons[:2]],
            ["Математика", "◾ Физика"],
        )
        self.assertEqual(
            buttons[0].icon_custom_emoji_id,
            get_custom_emoji_id("check"),
        )
        self.assertEqual(
            [
                button.model_dump(exclude_none=True).get("style")
                for button in buttons[:2]
            ],
            ["success", None],
        )
        self.assertEqual(
            buttons[2].model_dump(exclude_none=True)["style"],
            "primary",
        )
        self.assertIsNone(
            buttons[3].model_dump(exclude_none=True).get("style")
        )

    def test_selected_absence_lessons_are_success_and_others_neutral(self) -> None:
        markup = _lesson_keyboard(
            [
                Lesson("08:00", "Математика", "301", "Учитель"),
                Lesson("09:40", "Физика", "302", "Учитель"),
            ],
            {2},
        )
        buttons = _buttons(markup)
        lesson_buttons = buttons[1:3]

        self.assertEqual(
            [
                button.model_dump(exclude_none=True).get("style")
                for button in lesson_buttons
            ],
            [None, "success"],
        )
        self.assertTrue(lesson_buttons[0].text.startswith("◾ 1."))
        self.assertTrue(lesson_buttons[1].text.startswith("2."))
        self.assertEqual(
            lesson_buttons[1].icon_custom_emoji_id,
            get_custom_emoji_id("check"),
        )

    def test_parallel_absence_lessons_share_one_numbered_button(self) -> None:
        buttons = _buttons(
            _lesson_keyboard(
                [
                    Lesson("08:00", "Английский", "301", "Учитель", "english"),
                    Lesson("08:00", "Французский", "302", "Учитель", "french"),
                    Lesson("09:40", "Математика", "303", "Учитель"),
                ],
                set(),
                [1, 1, 2],
            )
        )

        self.assertEqual(len(buttons), 5)
        self.assertIn("1. Английский / Французский", buttons[1].text)
        self.assertIn("2. Математика", buttons[2].text)
        self.assertEqual(
            LessonChoiceCallback.unpack(buttons[1].callback_data).lesson_number,
            1,
        )
        self.assertEqual(
            LessonChoiceCallback.unpack(buttons[2].callback_data).lesson_number,
            2,
        )

    def test_existing_replacements_and_cancellations_reflect_database_state(self) -> None:
        created_at = datetime.now(timezone.utc)
        replacement = ScheduleOverride(
            target_date=date(2026, 10, 26),
            lesson_number=2,
            override_type="lesson",
            new_subject="Новый предмет",
            new_room="201",
            new_teacher=None,
            is_cancelled=False,
            created_by=1,
            created_at=created_at,
        )
        cancellation = ScheduleOverride(
            target_date=date(2026, 10, 26),
            lesson_number=3,
            override_type="lesson",
            new_subject=None,
            new_room=None,
            new_teacher=None,
            is_cancelled=True,
            created_by=1,
            created_at=created_at,
        )
        buttons = _buttons(
            _lesson_picker_keyboard({2: replacement, 3: cancellation})
        )

        self.assertEqual(
            [
                button.model_dump(exclude_none=True).get("style")
                for button in buttons[:3]
            ],
            [None, "success", "danger"],
        )
        self.assertEqual(buttons[1].text, "2 пара")
        self.assertEqual(buttons[2].text, "3 пара")


if __name__ == "__main__":
    unittest.main()
