from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from sqlalchemy import create_engine, inspect
from sqlalchemy.ext.asyncio import AsyncSession

from bot.handlers.admin import _admin_history_page, show_admin_management
from bot.middlewares.access import AccessMiddleware
from bot.middlewares.role import RoleMiddleware
from bot.utils.admin_audit import log_admin_action
from bot.ui import admin_menu_keyboard
from config_loader import (
    AppConfig,
    BotConfig,
    DaySchedule,
    RolesConfig,
    SanctionsConfig,
    ScheduleConfig,
    WeekParity,
)
from database.migrations import migrate_admin_action_logs, migrate_dynamic_roles
from database.models import AdminActionLog, DynamicRole, User


def _config() -> AppConfig:
    empty_day = DaySchedule(odd=[], even=[])
    return AppConfig(
        bot=BotConfig(token="test", timezone="UTC", group_chat_id=0),
        roles=RolesConfig(owner=[1], starosta=[2], moderators=[3]),
        students=[],
        students_path=Path("students.yaml"),
        sanctions=SanctionsConfig(ban_threshold=3, ban_days=1),
        schedule=ScheduleConfig(
            week_parity=WeekParity(date(2026, 10, 26), "odd"),
            monday=empty_day,
            tuesday=empty_day,
            wednesday=empty_day,
            thursday=empty_day,
            friday=empty_day,
        ),
    )


class AdminControlsTests(unittest.TestCase):
    def test_schema_migrations_are_idempotent(self) -> None:
        with NamedTemporaryFile(suffix=".sqlite") as database_file:
            engine = create_engine(f"sqlite:///{database_file.name}")
            with engine.begin() as connection:
                migrate_admin_action_logs(connection)
                migrate_admin_action_logs(connection)
                migrate_dynamic_roles(connection)
                migrate_dynamic_roles(connection)
                tables = set(inspect(connection).get_table_names())
                self.assertIn("admin_action_logs", tables)
                self.assertIn("dynamic_roles", tables)
            engine.dispose()

    def test_role_middleware_resolves_dynamic_and_yaml_roles(self) -> None:
        async def run_test() -> None:
            class Session(AsyncSession):
                async def get(self, model, telegram_id):
                    if model is DynamicRole and telegram_id == 4:
                        return DynamicRole(telegram_id=4, role="moderator")
                    return None

            session = Session()
            middleware = RoleMiddleware()
            config = _config()

            async def handler(_event, data):
                return data["role"]

            roles = []
            for telegram_id in (1, 2, 3, 4, 5):
                result = await middleware(
                    handler,
                    SimpleNamespace(from_user=SimpleNamespace(id=telegram_id)),
                    {"config": config, "session": session},
                )
                roles.append(result)
            self.assertEqual(
                roles,
                ["owner", "starosta", "moderator", "moderator", "user"],
            )

        asyncio.run(run_test())

    def test_access_middleware_allows_dynamic_admin_but_denies_unknown_user(self) -> None:
        async def run_test() -> None:
            class Session(AsyncSession):
                async def get(self, model, telegram_id):
                    if model is DynamicRole and telegram_id == 4:
                        return DynamicRole(telegram_id=4, role="starosta")
                    if model is User:
                        return User(telegram_id=telegram_id)
                    return None

                def add(self, _item) -> None:
                    return None

                async def commit(self) -> None:
                    return None

            middleware = AccessMiddleware()
            config = _config()
            session = Session()
            calls: list[int] = []

            async def handler(event, _data):
                calls.append(event.from_user.id)
                return "handled"

            allowed = await middleware(
                handler,
                SimpleNamespace(from_user=SimpleNamespace(id=4, username="admin")),
                {"config": config, "session": session},
            )
            denied = await middleware(
                handler,
                SimpleNamespace(from_user=SimpleNamespace(id=5, username="student")),
                {"config": config, "session": session},
            )
            self.assertEqual(allowed, "handled")
            self.assertIsNone(denied)
            self.assertEqual(calls, [4])

        asyncio.run(run_test())

    def test_admin_management_controls_are_visible_only_to_owner(self) -> None:
        owner_buttons = {
            button.text
            for row in admin_menu_keyboard("owner").inline_keyboard
            for button in row
        }
        moderator_buttons = {
            button.text
            for row in admin_menu_keyboard("moderator").inline_keyboard
            for button in row
        }
        self.assertIn("👥 Управление администраторами", owner_buttons)
        self.assertNotIn("👥 Управление администраторами", moderator_buttons)

    def test_management_handler_rejects_non_owner_without_querying(self) -> None:
        async def run_test() -> None:
            for non_owner_role in ("starosta", "moderator"):
                message = SimpleNamespace(answer=AsyncMock())
                session = AsyncMock()
                await show_admin_management(message, session, _config(), non_owner_role)
                message.answer.assert_awaited_once_with(
                    "Управлять ролями может только владелец."
                )
                session.scalars.assert_not_called()

        asyncio.run(run_test())

    def test_admin_history_escapes_user_controlled_fields(self) -> None:
        async def run_test() -> None:
            class Session(AsyncSession):
                def __init__(self) -> None:
                    self.logs = []
                    self.users = [User(telegram_id=17, username="<admin>")]

                def add(self, item) -> None:
                    item.id = 1
                    self.logs.append(item)

                async def scalars(self, statement):
                    entity = statement.column_descriptions[0]["entity"]
                    return self.logs if entity is AdminActionLog else self.users

            session = Session()
            await log_admin_action(
                session,
                17,
                "Изменил <расписание>",
                "дата=28.10 <script>",
            )
            session.logs[0].created_at = datetime.now(timezone.utc)
            text, _keyboard = await _admin_history_page(session, 0)
            self.assertIn("@&lt;admin&gt;", text)
            self.assertIn("&lt;расписание&gt;", text)
            self.assertIn("&lt;script&gt;", text)
            self.assertNotIn("<script>", text)

        asyncio.run(run_test())


if __name__ == "__main__":
    unittest.main()
