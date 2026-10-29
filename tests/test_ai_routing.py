from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from aiogram import Bot, Dispatcher, Router, F
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import StateFilter
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import SendMessage
from aiogram.types import Chat, Message, Update, User as TelegramUser

from bot.ai.gateway import AIGateway
from bot.ai.policy import AIResult
from bot.handlers.ai import (
    AI_THINKING_CUSTOM_EMOJI_ID,
    AI_THINKING_TEXT,
    _send_thinking_indicator,
    private_text_handler,
)
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
    def __init__(self, *, gemini_enabled: bool = True) -> None:
        self.settings = AIUserSettings(
            telegram_id=45,
            gemini_enabled=gemini_enabled,
            humor_enabled=True,
            personalization_enabled=True,
            reactions_enabled=False,
            humor_style="memey",
        )

    async def get(self, model, key):
        return self.settings if model is AIUserSettings else None


class AIRoutingTests(unittest.TestCase):
    @staticmethod
    def _thinking_message():
        return SimpleNamespace(delete=AsyncMock())

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

    def test_thinking_indicator_uses_plain_text_when_no_custom_emoji_id(self) -> None:
        async def test() -> None:
            indicator = self._thinking_message()
            message = SimpleNamespace(answer=AsyncMock(return_value=indicator))

            result = await _send_thinking_indicator(message, custom_emoji_id=None)

            self.assertIs(result, indicator)
            message.answer.assert_awaited_once_with(AI_THINKING_TEXT)

        asyncio.run(test())

    def test_thinking_indicator_uses_provided_custom_emoji_entity(self) -> None:
        async def test() -> None:
            indicator = self._thinking_message()
            message = SimpleNamespace(answer=AsyncMock(return_value=indicator))

            result = await _send_thinking_indicator(message)

            self.assertIs(result, indicator)
            message.answer.assert_awaited_once()
            args, kwargs = message.answer.await_args
            self.assertEqual(args, (AI_THINKING_TEXT,))
            entities = kwargs["entities"]
            self.assertEqual(len(entities), 1)
            entity = entities[0]
            self.assertEqual(entity.type, "custom_emoji")
            self.assertEqual(entity.offset, 6)
            self.assertEqual(entity.length, 2)
            self.assertEqual(
                entity.custom_emoji_id,
                AI_THINKING_CUSTOM_EMOJI_ID,
            )
            self.assertEqual(AI_THINKING_CUSTOM_EMOJI_ID, "5427181942934088912")

        asyncio.run(test())

    def test_custom_emoji_send_failure_retries_indicator_as_plain_text(self) -> None:
        async def test() -> None:
            indicator = self._thinking_message()
            message = SimpleNamespace(
                answer=AsyncMock(
                    side_effect=[
                        TelegramBadRequest(
                            method=SendMessage(chat_id=45, text=AI_THINKING_TEXT),
                            message="custom emoji rejected",
                        ),
                        indicator,
                    ]
                )
            )

            with self.assertLogs("bot.handlers.ai", level="WARNING"):
                result = await _send_thinking_indicator(message)

            self.assertIs(result, indicator)
            self.assertEqual(message.answer.await_count, 2)
            self.assertIn("entities", message.answer.await_args_list[0].kwargs)
            self.assertEqual(
                message.answer.await_args_list[1].args,
                (AI_THINKING_TEXT,),
            )
            self.assertNotIn("entities", message.answer.await_args_list[1].kwargs)

        asyncio.run(test())

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
            message = self._message(bot, "private", "Что задали на завтра?")
            gateway = AsyncMock(spec=AIGateway)
            with patch(
                "bot.handlers.ai.show_tomorrow_homework",
                new_callable=AsyncMock,
            ) as show_homework:
                await private_text_handler(
                    message,
                    RoutingSession(),
                    _config(),
                    gateway,
                )
            show_homework.assert_awaited_once()
            gateway.generate.assert_not_awaited()
            await bot.session.close()

    def test_joking_reference_does_not_trigger_random_navigation(self) -> None:
        from bot.ai.policy import explicit_navigation

        self.assertIsNone(explicit_navigation("Расскажи шутку про домашнее задание"))
        self.assertEqual(explicit_navigation("Что задали?"), "navigation_homework")

    def test_model_navigation_intent_does_not_open_a_section(self) -> None:
        async def test() -> None:
            bot = Bot("123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi")
            message = self._message(
                bot,
                "private",
                "Какой самый странный научный факт о космосе?",
            )
            gateway = AsyncMock(spec=AIGateway)
            gateway.generate.return_value = AIResult(
                intent="navigation_homework",
                response="Чтобы открыть раздел, назови его явно. Список разделов — /help",
            )
            thinking = self._thinking_message()
            with (
                patch(
                    "bot.handlers.ai.show_tomorrow_homework",
                    new_callable=AsyncMock,
                ) as show_homework,
                patch.object(
                    Message,
                    "answer",
                    new_callable=AsyncMock,
                    side_effect=[thinking, None],
                ) as answer,
            ):
                with self.assertLogs("bot.handlers.ai", level="INFO") as captured:
                    await private_text_handler(
                        message,
                        RoutingSession(),
                        _config(),
                        gateway,
                    )
            show_homework.assert_not_awaited()
            self.assertEqual(
                [call.args[0] for call in answer.await_args_list],
                [
                    AI_THINKING_TEXT,
                    "Не до конца понял вопрос. Попробуй сформулировать иначе.",
                ],
            )
            self.assertNotIn("назови его явно", answer.await_args_list[1].args[0])
            thinking.delete.assert_awaited_once()
            self.assertIn("intent=navigation_homework", captured.output[0])
            self.assertNotIn(message.text, captured.output[0])
            await bot.session.close()

        asyncio.run(test())

    def test_gateway_deadline_uses_existing_safe_fallback(self) -> None:
        async def test() -> None:
            bot = Bot("123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi")
            message = self._message(bot, "private", "обычный вопрос")
            gateway = AsyncMock(spec=AIGateway)
            gateway.generate.return_value = None
            thinking = self._thinking_message()

            with patch.object(
                Message,
                "answer",
                new_callable=AsyncMock,
                side_effect=[thinking, None],
            ) as answer:
                await private_text_handler(
                    message,
                    RoutingSession(),
                    _config(),
                    gateway,
                )

            gateway.generate.assert_awaited_once()
            self.assertEqual(
                [call.args[0] for call in answer.await_args_list],
                [
                    AI_THINKING_TEXT,
                    "Сейчас не удалось получить ответ от AI. "
                    "Обычные разделы бота работают.",
                ],
            )
            thinking.delete.assert_awaited_once()
            await bot.session.close()

        asyncio.run(test())

    def test_ordinary_question_reaches_ai_and_returns_one_answer(self) -> None:
        async def test() -> None:
            bot = Bot("123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi")
            text = "Какой самый странный научный факт о космосе?"
            message = self._message(bot, "private", text)
            gateway = AsyncMock(spec=AIGateway)
            gateway.generate.return_value = AIResult(
                intent="ordinary_reply",
                response="На Венере сутки длиннее года.",
            )
            thinking = self._thinking_message()
            answer_order: list[str] = []

            async def delete_indicator():
                answer_order.append("delete")

            thinking.delete.side_effect = delete_indicator

            async def generate(*args, **kwargs):
                answer_order.append("gateway")
                return gateway.generate.return_value

            async def answer_side_effect(text, **kwargs):
                answer_order.append(text)
                return thinking if text == AI_THINKING_TEXT else None

            gateway.generate.side_effect = generate
            with patch.object(
                Message,
                "answer",
                new_callable=AsyncMock,
                side_effect=answer_side_effect,
            ) as answer:
                await private_text_handler(
                    message,
                    RoutingSession(),
                    _config(),
                    gateway,
                )
            gateway.generate.assert_awaited_once_with(
                45,
                text,
                allow_humor=False,
                humor_style="memey",
            )
            self.assertEqual(answer_order[0], AI_THINKING_TEXT)
            self.assertEqual(answer_order[1], "gateway")
            self.assertEqual(answer_order[2], "На Венере сутки длиннее года.")
            self.assertEqual(answer_order[3], "delete")
            self.assertEqual(answer.await_count, 2)
            thinking.delete.assert_awaited_once()
            await bot.session.close()

        asyncio.run(test())

    def test_explicit_joke_request_reaches_ai_once_with_opted_in_humor(self) -> None:
        async def test() -> None:
            bot = Bot("123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi")
            text = "Расскажи короткую шутку"
            message = self._message(bot, "private", text)
            gateway = AsyncMock(spec=AIGateway)
            gateway.generate.return_value = AIResult(
                intent="joke_or_banter",
                response="Шутка про кота.",
            )
            config = _config()
            config = replace(
                config,
                ai=replace(config.ai, humor_enabled=True),
            )
            thinking = self._thinking_message()
            with patch.object(
                Message,
                "answer",
                new_callable=AsyncMock,
                side_effect=[thinking, None],
            ) as answer:
                await private_text_handler(
                    message,
                    RoutingSession(),
                    config,
                    gateway,
                )
            gateway.generate.assert_awaited_once_with(
                45,
                text,
                allow_humor=True,
                humor_style="memey",
            )
            self.assertEqual(
                [call.args[0] for call in answer.await_args_list],
                [AI_THINKING_TEXT, "Шутка про кота."],
            )
            thinking.delete.assert_awaited_once()
            await bot.session.close()

        asyncio.run(test())

    def test_unavailable_ai_fallback_does_not_claim_a_limit_was_reached(self) -> None:
        async def test() -> None:
            bot = Bot("123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi")
            message = self._message(bot, "private", "Поясни этот научный факт")
            gateway = AsyncMock(spec=AIGateway)
            gateway.generate.return_value = None
            thinking = self._thinking_message()
            with patch.object(
                Message,
                "answer",
                new_callable=AsyncMock,
                side_effect=[thinking, None],
            ) as answer:
                await private_text_handler(
                    message,
                    RoutingSession(),
                    _config(),
                    gateway,
                )
            gateway.generate.assert_awaited_once()
            fallback = "Сейчас не удалось получить ответ от AI. Обычные разделы бота работают."
            self.assertEqual(
                [call.args[0] for call in answer.await_args_list],
                [AI_THINKING_TEXT, fallback],
            )
            thinking.delete.assert_awaited_once()
            self.assertNotIn("лимит", fallback.casefold())
            await bot.session.close()

        asyncio.run(test())

    def test_gateway_exception_shows_fallback_and_removes_indicator(self) -> None:
        async def test() -> None:
            bot = Bot("123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi")
            message = self._message(bot, "private", "обычный вопрос")
            gateway = AsyncMock(spec=AIGateway)
            gateway.generate.side_effect = TimeoutError()
            thinking = self._thinking_message()
            with (
                patch.object(
                    Message,
                    "answer",
                    new_callable=AsyncMock,
                    side_effect=[thinking, None],
                ) as answer,
                self.assertLogs("bot.handlers.ai", level="WARNING") as captured,
            ):
                await private_text_handler(
                    message,
                    RoutingSession(),
                    _config(),
                    gateway,
                )

            self.assertEqual(answer.await_count, 2)
            self.assertEqual(
                answer.await_args_list[1].args[0],
                "Сейчас не удалось получить ответ от AI. "
                "Обычные разделы бота работают.",
            )
            thinking.delete.assert_awaited_once()
            self.assertIn("TimeoutError", captured.output[0])
            await bot.session.close()

        asyncio.run(test())

    def test_unhandled_ai_intent_shows_fallback_and_removes_indicator(self) -> None:
        async def test() -> None:
            bot = Bot("123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi")
            message = self._message(bot, "private", "обычный вопрос")
            gateway = AsyncMock(spec=AIGateway)
            gateway.generate.return_value = AIResult(
                intent="uncertain",
                response="",
            )
            thinking = self._thinking_message()
            with patch.object(
                Message,
                "answer",
                new_callable=AsyncMock,
                side_effect=[thinking, None],
            ) as answer:
                await private_text_handler(
                    message,
                    RoutingSession(),
                    _config(),
                    gateway,
                )

            self.assertEqual(
                answer.await_args_list[1].args[0],
                "Не до конца понял вопрос. Попробуй сформулировать иначе.",
            )
            thinking.delete.assert_awaited_once()
            await bot.session.close()

        asyncio.run(test())

    def test_failed_thinking_send_does_not_prevent_final_answer(self) -> None:
        async def test() -> None:
            bot = Bot("123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi")
            message = self._message(bot, "private", "обычный вопрос")
            gateway = AsyncMock(spec=AIGateway)
            gateway.generate.return_value = AIResult(
                intent="ordinary_reply",
                response="Ответ готов.",
            )
            thinking = self._thinking_message()
            with (
                patch.object(
                    Message,
                    "answer",
                    new_callable=AsyncMock,
                    side_effect=[
                        TelegramBadRequest(
                            method=SendMessage(chat_id=45, text=AI_THINKING_TEXT),
                            message="indicator unavailable",
                        ),
                        thinking,
                        None,
                    ],
                ) as answer,
                self.assertLogs("bot.handlers.ai", level="WARNING") as captured,
            ):
                await private_text_handler(
                    message,
                    RoutingSession(),
                    _config(),
                    gateway,
                )

            self.assertEqual(answer.await_count, 3)
            self.assertEqual(answer.await_args_list[2].args[0], "Ответ готов.")
            gateway.generate.assert_awaited_once()
            self.assertIn("TelegramBadRequest", captured.output[0])
            await bot.session.close()

        asyncio.run(test())

    def test_failed_indicator_delete_does_not_remove_delivered_answer(self) -> None:
        async def test() -> None:
            bot = Bot("123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi")
            message = self._message(bot, "private", "обычный вопрос")
            gateway = AsyncMock(spec=AIGateway)
            gateway.generate.return_value = AIResult(
                intent="ordinary_reply",
                response="Ответ доставлен.",
            )
            thinking = self._thinking_message()
            thinking.delete.side_effect = TelegramBadRequest(
                method=SendMessage(chat_id=45, text=AI_THINKING_TEXT),
                message="cannot delete",
            )
            with (
                patch.object(
                    Message,
                    "answer",
                    new_callable=AsyncMock,
                    side_effect=[thinking, None],
                ) as answer,
                self.assertLogs("bot.handlers.ai", level="WARNING") as captured,
            ):
                await private_text_handler(
                    message,
                    RoutingSession(),
                    _config(),
                    gateway,
                )

            self.assertEqual(answer.await_args_list[1].args[0], "Ответ доставлен.")
            thinking.delete.assert_awaited_once()
            self.assertIn("TelegramBadRequest", captured.output[0])
            await bot.session.close()

        asyncio.run(test())

    def test_failed_final_response_keeps_indicator_visible(self) -> None:
        async def test() -> None:
            bot = Bot("123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi")
            message = self._message(bot, "private", "обычный вопрос")
            gateway = AsyncMock(spec=AIGateway)
            gateway.generate.return_value = AIResult(
                intent="ordinary_reply",
                response="Ответ готов.",
            )
            thinking = self._thinking_message()
            with (
                patch.object(
                    Message,
                    "answer",
                    new_callable=AsyncMock,
                    side_effect=[
                        thinking,
                        TelegramBadRequest(
                            method=SendMessage(chat_id=45, text="Ответ готов."),
                            message="answer rejected",
                        ),
                    ],
                ) as answer,
                self.assertLogs("bot.handlers.ai", level="WARNING") as captured,
            ):
                await private_text_handler(
                    message,
                    RoutingSession(),
                    _config(),
                    gateway,
                )

            self.assertEqual(answer.await_count, 2)
            thinking.delete.assert_not_awaited()
            self.assertIn("Could not send AI final response", captured.output[0])
            self.assertIn("TelegramBadRequest", captured.output[0])
            await bot.session.close()

        asyncio.run(test())

    def test_cancelled_ai_request_removes_indicator_without_background_tasks(self) -> None:
        async def test() -> None:
            bot = Bot("123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi")
            message = self._message(bot, "private", "обычный вопрос")
            gateway = AsyncMock(spec=AIGateway)
            gateway.generate.side_effect = asyncio.CancelledError
            thinking = self._thinking_message()
            with patch.object(
                Message,
                "answer",
                new_callable=AsyncMock,
                side_effect=[thinking],
            ) as answer:
                tasks_before = set(asyncio.all_tasks())
                with self.assertRaises(asyncio.CancelledError):
                    await private_text_handler(
                        message,
                        RoutingSession(),
                        _config(),
                        gateway,
                    )

            self.assertEqual(answer.await_count, 1)
            thinking.delete.assert_awaited_once()
            self.assertEqual(set(asyncio.all_tasks()), tasks_before)
            await bot.session.close()

        asyncio.run(test())

    def test_user_opt_out_does_not_call_ai_or_send_ai_fallback(self) -> None:
        async def test() -> None:
            bot = Bot("123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi")
            message = self._message(bot, "private", "Привет")
            gateway = AsyncMock(spec=AIGateway)
            with patch.object(Message, "answer", new_callable=AsyncMock) as answer:
                await private_text_handler(
                    message,
                    RoutingSession(gemini_enabled=False),
                    _config(),
                    gateway,
                )
            gateway.generate.assert_not_awaited()
            answer.assert_not_awaited()
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
