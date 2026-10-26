from __future__ import annotations

from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

import yaml

from config_loader import StudentConfig, append_student


class StudentRosterWriteTests(unittest.TestCase):
    def test_append_student_persists_yaml_and_updates_live_roster(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "students.yaml"
            path.write_text("students: []\n", encoding="utf-8")
            config = SimpleNamespace(students=[], students_path=path)
            student = StudentConfig(123456789, "Иванов Иван", "english")

            append_student(config, student)

            self.assertEqual(config.students, [student])
            self.assertEqual(
                yaml.safe_load(path.read_text(encoding="utf-8")),
                {
                    "students": [
                        {
                            "telegram_id": 123456789,
                            "full_name": "Иванов Иван",
                            "subgroup": "english",
                        }
                    ]
                },
            )


if __name__ == "__main__":
    unittest.main()
