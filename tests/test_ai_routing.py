from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone
from dataclasses import replace
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, patch

from aiogram import Bot, Dispatcher, Router, F
from aiogram.filters import StateFilter
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Chat, Message, Update, User as TelegramUser

from bot.ai.gateway import AIGateway
from bot.ai.policy import AIResult
from bot.handlers.ai import private_text_handler
from bot.handlers.base import start_handler
from config_loader import (
    AIConfig,
    AppConfig,
    BotConfig,
    DaySchedule,
    RolesConfig,
    SanctionsConfig,
    ScheduleConfig,
    WeekParity,
)
from database.models import AIUserSettings


def _config() -> AppConfig:
    day = DaySchedule(odd=[], even=[])
    return AppConfig(
        bot=BotConfig(token="test-token", timezone="Europe/Chisinau", group_chat_id=-1),
        roles=RolesConfig(owner=[], starosta=[], moderators=[]),
        students=[],
        students_path=Path("students.yaml"),
        sanctions=SanctionsConfig(ban_threshold=3, ban_days=1),
        schedule=ScheduleConfig(
            week_parity=WeekParity(date(2026, 10, 26), "odd"),
            monday=day,
            tuesday=day,
            wednesday=day,
            thursday=day,
            friday=day,
        ),
        ai=AIConfig(
            enabled=True,
            project_limits_verified=True,
            api_key="test-key",
        ),
    )


class RoutingSession:
    def __init__(self) -> None:
        self.settings = AIUserSettings(
            telegram_id=45,
            gemini_enabled=True,
            humor_enabled=True,
            personalization_enabled=True,
            reactions_enabled=False,
        )

    async def get(self, model, key):
        return self.settings if model is AIUserSettings else None


class AIRoutingTests(unittest.TestCase):
    def _message(self, bot: Bot, chat_type: str, text: str) -> Message:
        return Message.model_construct(
            message_id=1,
            date=datetime.now(timezone.utc),
            chat=Chat.model_construct(
                id=45 if chat_type == "private" else -10045,
                type=chat_type,
            ),
            from_user=TelegramUser.model_construct(
                id=45,
                is_bot=False,
                first_name="Student",
            ),
            text=text,
        ).as_(bot)

    def test_group_messages_never_reach_ai_handler(self) -> None:
        async def test() -> None:
            bot = Bot("123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi")
            storage = MemoryStorage()
            dispatcher = Dispatcher(storage=storage)
            router = Router(name="ai_group_regression")
            router.message.register(
                private_text_handler,
                StateFilter(None),
                F.chat.type == "private",
                F.text,
                ~F.text.startswith("/"),
            )
            dispatcher.include_router(router)
            gateway = AsyncMock(spec=AIGateway)
            session = RoutingSession()
            dispatcher["config"] = _config()
            dispatcher["ai_gateway"] = gateway
            dispatcher["session"] = session
            await dispatcher.feed_update(
                bot,
                Update(
                    update_id=1,
                    message=self._message(bot, "supergroup", "hello there"),
                ),
            )
            gateway.generate.assert_not_awaited()
            await bot.session.close()
            await storage.close()

        asyncio.run(test())

    def test_active_fsm_text_does_not_reach_ai_handler(self) -> None:
        async def test() -> None:
            bot = Bot("123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi")
            storage = MemoryStorage()
            dispatcher = Dispatcher(storage=storage)
            router = Router(name="ai_fsm_regression")
            router.message.register(
                private_text_handler,
                StateFilter(None),
                F.chat.type == "private",
                F.text,
                ~F.text.startswith("/"),
            )
            dispatcher.include_router(router)
            key = StorageKey(bot_id=bot.id, chat_id=45, user_id=45)
            await storage.set_state(key, "HomeworkForm:task")
            gateway = AsyncMock(spec=AIGateway)
            dispatcher["config"] = _config()
            dispatcher["ai_gateway"] = gateway
            dispatcher["session"] = RoutingSession()
            await dispatcher.feed_update(
                bot,
                Update(
                    update_id=2,
                    message=self._message(bot, "private", "hello there"),
                ),
            )
            gateway.generate.assert_not_awaited()
            await bot.session.close()
            await storage.close()

        asyncio.run(test())

    def test_explicit_homework_request_uses_existing_handler_without_gemini(self) -> None:
        async def test() -> None:
            bot = Bot("123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi")
            message = self._message(bot, "private", "Что задали?")
            with patch(
                "bot.handlers.ai.show_tomorrow_homework",
                new_callable=AsyncMock,
            ) as show_homework:
                await private_text_handler(
                    message,
                    RoutingSession(),
                    _config(),
                    AsyncMock(spec=AIGateway),
                )
            show_homework.assert_awaited_once()
            await bot.session.close()

    def test_joking_reference_does_not_trigger_random_navigation(self) -> None:
        from bot.ai.policy import explicit_navigation

        self.assertIsNone(explicit_navigation("Расскажи шутку про домашнее задание"))
        self.assertEqual(explicit_navigation("Что задали?"), "navigation_homework")

    def test_model_navigation_intent_does_not_open_a_section(self) -> None:
        async def test() -> None:
            bot = Bot("123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi")
            message = self._message(bot, "private", "Расскажи шутку про домашнее задание")
            gateway = AsyncMock(spec=AIGateway)
            gateway.generate.return_value = AIResult(
                intent="navigation_homework",
                response="",
            )
            with (
                patch(
                    "bot.handlers.ai.show_tomorrow_homework",
                    new_callable=AsyncMock,
                ) as show_homework,
                patch.object(Message, "answer", new_callable=AsyncMock) as answer,
            ):
                await private_text_handler(
                    message,
                    RoutingSession(),
                    _config(),
                    gateway,
                )
            show_homework.assert_not_awaited()
            answer.assert_awaited_once()
            self.assertIn("назови его явно", answer.await_args.args[0])
            await bot.session.close()

        asyncio.run(test())

    def test_bot_capability_question_uses_verified_local_description(self) -> None:
        async def test() -> None:
            bot = Bot("123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi")
            message = self._message(bot, "private", "Что умеет этот бот?")
            gateway = AsyncMock(spec=AIGateway)
            with patch.object(Message, "answer", new_callable=AsyncMock) as answer:
                await private_text_handler(
                    message,
                    RoutingSession(),
                    _config(),
                    gateway,
                )
            gateway.generate.assert_not_awaited()
            self.assertIn("домашние задания, расписание, документы", answer.await_args.args[0])
            await bot.session.close()

        asyncio.run(test())

    def test_english_bot_capability_question_does_not_reach_gemini(self) -> None:
        async def test() -> None:
            bot = Bot("123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi")
            message = self._message(bot, "private", "What can this bot do?")
            gateway = AsyncMock(spec=AIGateway)
            with patch.object(Message, "answer", new_callable=AsyncMock):
                await private_text_handler(
                    message,
                    RoutingSession(),
                    _config(),
                    gateway,
                )
            gateway.generate.assert_not_awaited()
            await bot.session.close()

        asyncio.run(test())

    def test_role_change_capability_question_uses_local_bot_catalog(self) -> None:
        async def test() -> None:
            bot = Bot("123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi")
            message = self._message(bot, "private", "Можешь менять роли?")
            gateway = AsyncMock(spec=AIGateway)
            with patch.object(Message, "answer", new_callable=AsyncMock) as answer:
                await private_text_handler(
                    message,
                    RoutingSession(),
                    _config(),
                    gateway,
                )
            gateway.generate.assert_not_awaited()
            self.assertIn("домашние задания, расписание, документы", answer.await_args.args[0])
            await bot.session.close()

        asyncio.run(test())

    def test_group_start_does_not_apply_private_personalization(self) -> None:
        async def test() -> None:
            bot = Bot("123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi")
            message = self._message(bot, "group", "/start")
            state = AsyncMock()
            session = AsyncMock()
            session.get.return_value = None
            config = replace(
                _config(),
                ai=AIConfig(
                    enabled=True,
                    project_limits_verified=True,
                    personalization_enabled=True,
                ),
            )
            with (
                patch(
                    "bot.handlers.base.personalized_greeting",
                    new_callable=AsyncMock,
                ) as personalize,
                patch.object(Message, "answer", new_callable=AsyncMock) as answer,
            ):
                await start_handler(message, state, "user", session, config)
            personalize.assert_not_awaited()
            self.assertEqual(answer.await_count, 2)
            self.assertNotIn("стабильность", answer.await_args_list[0].args[0])
            await bot.session.close()

        asyncio.run(test())


if __name__ == "__main__":
    unittest.main()
