from __future__ import annotations

import unittest

from bot.handlers.absent import _lessons_label, _student_label
from database.models import User


class AbsenceLabelsTests(unittest.TestCase):
    def test_report_lesson_label_uses_starosta_format(self) -> None:
        self.assertEqual(_lessons_label([3, 4]), "3, 4 пара")
        self.assertEqual(_lessons_label(["all"]), "весь день")

    def test_student_label_uses_roster_name_and_subgroup(self) -> None:
        user = User(
            telegram_id=123456789,
            full_name="Иванов Иван",
            subgroup="english",
        )
        self.assertEqual(_student_label(user), "Иванов Иван (Английская подгруппа)")


if __name__ == "__main__":
    unittest.main()
