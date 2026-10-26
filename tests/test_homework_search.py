from __future__ import annotations

import asyncio
from datetime import date
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from aiogram.filters.command import CommandObject

from bot.handlers import hw
from config_loader import (
    AppConfig,
    BotConfig,
    DaySchedule,
    Lesson,
    RolesConfig,
    SanctionsConfig,
    ScheduleConfig,
    WeekParity,
)


class _ScalarResult:
    def __init__(self, value: object | None) -> None:
        self.value = value

    def first(self) -> object | None:
        return self.value


class _HomeworkSession:
    def __init__(self, homework: object, expected_date: date) -> None:
        self.homework = homework
        self.expected_date = expected_date
        self.queries: list[tuple[str, dict[str, object]]] = []

    async def scalars(self, query):
        compiled = query.compile()
        params = compiled.params
        self.queries.append((str(compiled), params))
        value = self.homework if self.expected_date in params.values() else None
        return _ScalarResult(value)


class HomeworkSearchTests(unittest.TestCase):
    def test_subject_inline_button_finds_homework_for_next_lesson(self) -> None:
        today = date(2026, 10, 26)
        target_date = date(2026, 10, 28)
        empty_day = DaySchedule(odd=[], even=[])
        config = AppConfig(
            bot=BotConfig(token="test", timezone="UTC", group_chat_id=0),
            roles=RolesConfig(owner=[], starosta=[], moderators=[]),
            students=[],
            students_path=Path("students.yaml"),
            sanctions=SanctionsConfig(ban_threshold=3, ban_days=1),
            schedule=ScheduleConfig(
                week_parity=WeekParity(today, "odd"),
                monday=empty_day,
                tuesday=empty_day,
                wednesday=DaySchedule(
                    odd=[Lesson("15:00", "L. franceză", "208", "Teacher")],
                    even=[],
                ),
                thursday=empty_day,
                friday=empty_day,
            ),
        )
        homework = SimpleNamespace(
            subject="L. franceză",
            target_date=target_date,
            text="Повторить тему",
            attachments=[],
        )
        session = _HomeworkSession(homework, target_date)
        message = SimpleNamespace(edit_text=AsyncMock())
        callback = SimpleNamespace(message=message, answer=AsyncMock())

        with (
            patch.object(hw, "local_today", return_value=today),
            patch.object(hw, "next_lesson_date", new=AsyncMock(return_value=target_date)) as next_date,
            patch.object(hw, "_homework_lines", new=AsyncMock(return_value=["ДЗ найдена"])),
            patch.object(hw, "_edit_homework_record", new=AsyncMock()) as edit_record,
        ):
            asyncio.run(
                hw.homework_subject_handler(
                    callback,
                    hw.HomeworkNavigationCallback(action="subject", subject_index=0),
                    config,
                    session,
                )
            )

        self.assertEqual(next_date.await_args.args[1], "L. franceză")
        self.assertEqual(next_date.await_args.args[3], today)
        self.assertEqual(len(session.queries), 2)
        self.assertIn(target_date, session.queries[1][1].values())
        edit_record.assert_awaited_once_with(message, ["ДЗ найдена"], homework)
        callback.answer.assert_awaited_once()

    def test_hw_command_resolves_french_alias_and_queries_matching_date(self) -> None:
        today = date(2026, 10, 26)
        target_date = date(2026, 10, 28)
        empty_day = DaySchedule(odd=[], even=[])
        config = AppConfig(
            bot=BotConfig(token="test", timezone="UTC", group_chat_id=0),
            roles=RolesConfig(owner=[], starosta=[], moderators=[]),
            students=[],
            students_path=Path("students.yaml"),
            sanctions=SanctionsConfig(ban_threshold=3, ban_days=1),
            schedule=ScheduleConfig(
                week_parity=WeekParity(today, "odd"),
                monday=empty_day,
                tuesday=empty_day,
                wednesday=DaySchedule(
                    odd=[Lesson("15:00", "L. franceză", "208", "Teacher")],
                    even=[],
                ),
                thursday=empty_day,
                friday=empty_day,
            ),
        )
        homework = SimpleNamespace(
            subject="L. franceză",
            target_date=target_date,
            is_deadline=False,
            deadline_date=None,
            text="Повторить тему",
            attachments=[],
            created_at=SimpleNamespace(tzinfo=None),
            updated_at=None,
        )
        session = _HomeworkSession(homework, target_date)
        message = SimpleNamespace(answer=AsyncMock())

        with (
            patch.object(hw, "local_today", return_value=today),
            patch.object(hw, "next_lesson_date", new=AsyncMock(return_value=target_date)) as next_date,
            patch.object(hw, "_homework_lines", new=AsyncMock(return_value=["ДЗ найдена"])),
            patch.object(hw, "_answer_long_text", new=AsyncMock()) as answer_text,
            patch.object(hw, "_send_homework_attachments", new=AsyncMock()),
        ):
            asyncio.run(
                hw.homework_handler(
                    message,
                    CommandObject(command="hw", args="французский"),
                    config,
                    session,
                )
            )

        self.assertEqual(next_date.await_args.args[1], "L. franceză")
        self.assertEqual(len(session.queries), 2)
        subject_params = [
            next(value for key, value in params.items() if key.startswith("subject"))
            for _, params in session.queries
        ]
        self.assertEqual(subject_params, ["L. franceză", "L. franceză"])
        self.assertIn(target_date, session.queries[1][1].values())
        answer_text.assert_awaited_once()

    def test_hw_command_handles_value_error_from_subject_or_schedule(self) -> None:
        message = SimpleNamespace(answer=AsyncMock())
        config = SimpleNamespace()

        with patch.object(hw, "resolve_subject", side_effect=ValueError("invalid input")):
            asyncio.run(
                hw.homework_handler(
                    message,
                    CommandObject(command="hw", args="💥"),
                    config,
                    SimpleNamespace(),
                )
            )

        message.answer.assert_awaited_once()
        self.assertIn("Не удалось обработать", message.answer.await_args.args[0])


if __name__ == "__main__":
    unittest.main()
