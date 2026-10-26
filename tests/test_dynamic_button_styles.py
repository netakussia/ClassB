from __future__ import annotations

from datetime import date, datetime, timezone
import unittest

from bot.handlers.absent import _lesson_keyboard
from bot.handlers.hw import _homework_subject_keyboard
from bot.handlers.replacements import _lesson_picker_keyboard
from config_loader import Lesson
from database.models import ScheduleOverride


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

        self.assertEqual([button.text for button in buttons[:2]], [
            "✅ Математика",
            "◾ Физика",
        ])
        self.assertEqual(
            [button.model_dump(exclude_none=True)["style"] for button in buttons[:2]],
            ["success", "primary"],
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
                button.model_dump(exclude_none=True)["style"]
                for button in lesson_buttons
            ],
            ["primary", "success"],
        )
        self.assertTrue(lesson_buttons[0].text.startswith("1."))
        self.assertTrue(lesson_buttons[1].text.startswith("✅ 2."))

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
            [button.model_dump(exclude_none=True)["style"] for button in buttons[:3]],
            ["primary", "success", "danger"],
        )
        self.assertEqual(buttons[1].text, "🔄 2 пара")
        self.assertEqual(buttons[2].text, "❌ 3 пара")


if __name__ == "__main__":
    unittest.main()
