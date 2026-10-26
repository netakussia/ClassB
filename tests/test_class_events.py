from __future__ import annotations

from datetime import date, datetime, timezone
import re
import unittest

from bot.handlers.events import _format_event_list
from database.models import Base, ClassEvent


def _event(event_date: date, title: str, category: str = "exam") -> ClassEvent:
    return ClassEvent(
        title=title,
        event_date=event_date,
        category=category,
        created_by=1,
        created_at=datetime.now(timezone.utc),
    )


class ClassEventTests(unittest.TestCase):
    def test_event_list_groups_events_by_seven_and_thirty_day_windows(self) -> None:
        today = date(2026, 10, 1)
        events = [
            _event(today, "Сегодня"),
            _event(date(2026, 10, 8), "Через неделю", "holiday"),
            _event(date(2026, 10, 9), "Позже недели", "coursework"),
            _event(date(2026, 10, 31), "Граница месяца", "other"),
            _event(date(2026, 11, 1), "Позже месяца"),
        ]

        result = re.sub(
            r'<tg-emoji emoji-id="[0-9]+">(.*?)</tg-emoji>',
            r"\1",
            _format_event_list(events, today),
        )

        self.assertIn("📌 На этой неделе\n• 01.10 Сегодня (Зачёты/Модули)", result)
        self.assertIn("• 08.10 Через неделю (Праздники/ДР)", result)
        self.assertIn("📅 В этом месяце", result)
        self.assertIn("• 31.10 Граница месяца (Другое)", result)
        self.assertIn("🔮 Позже\n• 01.11 Позже месяца", result)

    def test_class_event_table_is_registered_in_metadata(self) -> None:
        self.assertIn("class_events", Base.metadata.tables)


if __name__ == "__main__":
    unittest.main()