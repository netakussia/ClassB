from __future__ import annotations

import asyncio
from datetime import datetime
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from aiogram import Bot
from aiogram.enums import ParseMode
from aiogram.types import (
    Chat,
    InlineKeyboardMarkup,
    Message,
    ReplyKeyboardMarkup,
    User as TelegramUser,
)

from bot.ai.gateway import AIGateway
from bot.ai.personalization import (
    GREETING_CACHE_TTL_SECONDS,
    clear_user_cache,
    personalized_greeting,
)
from bot.ai.policy import AIResult
from bot.handlers.base import start_handler
from config_loader import AIConfig
from database.models import AIUserSettings, User


class AIPersonalizationTests(unittest.TestCase):
    def _config(
        self,
        *,
        personalization_enabled: bool = True,
        max_input_chars: int = 500,
    ) -> AIConfig:
        return AIConfig(
            enabled=True,
            project_limits_verified=True,
            personalization_enabled=personalization_enabled,
            humor_enabled=True,
            max_input_chars=max_input_chars,
            retries=0,
        )

    def _settings(
        self,
        user_id: int,
        *,
        gemini_enabled: bool = True,
        personalization_enabled: bool = True,
        humor_enabled: bool = False,
        humor_style: str = "gentle",
    ) -> AIUserSettings:
        return AIUserSettings(
            telegram_id=user_id,
            gemini_enabled=gemini_enabled,
            personalization_enabled=personalization_enabled,
            humor_enabled=humor_enabled,
            humor_style=humor_style,
            reactions_enabled=False,
        )

    def _session(
        self,
        settings: AIUserSettings | None,
        *,
        activity_count: int | None = 0,
    ) -> AsyncMock:
        session = AsyncMock()

        async def get(model: type[object], user_id: int) -> object | None:
            self.assertGreater(user_id, 0)
            if model is AIUserSettings:
                return settings
            if model is User:
                return None
            raise AssertionError(f"Unexpected model lookup: {model.__name__}")

        session.get.side_effect = get
        session.scalar.return_value = activity_count
        return session

    def _gateway(self, response: str = "Привет, Николай!") -> AsyncMock:
        gateway = AsyncMock(spec=AIGateway)
        gateway.generate.return_value = AIResult(
            intent="ordinary_reply",
            response=response,
        )
        return gateway

    def _start_message(self, display_name: str = "Student") -> tuple[Bot, Message]:
        bot = Bot("123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi")
        message = Message.model_construct(
            message_id=1,
            date=datetime.now().astimezone(),
            chat=Chat.model_construct(id=7, type="private"),
            from_user=TelegramUser.model_construct(
                id=7,
                is_bot=False,
                first_name=display_name,
            ),
            text="/start",
        ).as_(bot)
        return bot, message

    def _greet(
        self,
        user_id: int,
        session: AsyncMock,
        gateway: AsyncMock,
        *,
        name: str = "Nicolai",
        config: AIConfig | None = None,
    ) -> str:
        return asyncio.run(
            personalized_greeting(
                session,
                user_id,
                config or self._config(),
                "Europe/Chisinau",
                display_name=name,
                ai_gateway=gateway,
                now=datetime(2026, 10, 29, 10, 0),
            )
        )

    def test_new_user_without_personalization_settings_gets_simple_fallback(self) -> None:
        user_id = 701
        clear_user_cache(user_id)
        session = self._session(None)
        gateway = self._gateway()

        result = self._greet(user_id, session, gateway, name="Nicolai")

        self.assertEqual(result, "Привет, Nicolai!")
        session.scalar.assert_not_awaited()
        gateway.generate.assert_not_awaited()
        clear_user_cache(user_id)

    def test_enabled_personalization_sends_only_safe_context_to_gateway(self) -> None:
        async def test() -> None:
            user_id = 702
            clear_user_cache(user_id)
            session = self._session(
                self._settings(user_id, humor_enabled=True, humor_style="memey"),
                activity_count=8,
            )
            gateway = self._gateway("Привет, Николай!")

            result = await personalized_greeting(
                session,
                user_id,
                self._config(),
                "Europe/Chisinau",
                display_name="Nicolai",
                ai_gateway=gateway,
                now=datetime(2026, 10, 29, 10, 0),
            )

            self.assertEqual(result, "Привет, Николай!")
            gateway.generate.assert_awaited_once()
            args, kwargs = gateway.generate.await_args
            self.assertEqual(args[0], user_id)
            self.assertIn('"name":"Nicolai"', args[1])
            self.assertIn('"preferred_tone":"memey"', args[1])
            self.assertIn('"frequently_opened_homework_today":true', args[1])
            self.assertNotIn('"count": 8', args[1])
            self.assertNotIn("переписк", args[1].casefold())
            self.assertLessEqual(len(args[1]), self._config().max_input_chars)
            self.assertFalse(kwargs["allow_humor"])
            self.assertEqual(kwargs["humor_style"], "memey")
            clear_user_cache(user_id)

        asyncio.run(test())

    def test_user_opt_out_uses_fallback_without_gateway_call(self) -> None:
        for user_id, settings in (
            (
                703,
                self._settings(703, personalization_enabled=False),
            ),
            (
                704,
                self._settings(704, gemini_enabled=False),
            ),
        ):
            clear_user_cache(user_id)
            session = self._session(settings)
            gateway = self._gateway()

            result = self._greet(user_id, session, gateway)

            self.assertEqual(result, "Привет, Nicolai!")
            session.scalar.assert_not_awaited()
            gateway.generate.assert_not_awaited()
            clear_user_cache(user_id)

    def test_global_opt_out_does_not_read_user_context(self) -> None:
        user_id = 705
        clear_user_cache(user_id)
        session = self._session(self._settings(user_id))
        gateway = self._gateway()

        result = self._greet(
            user_id,
            session,
            gateway,
            config=self._config(personalization_enabled=False),
        )

        self.assertEqual(result, "Привет, Nicolai!")
        session.get.assert_not_awaited()
        gateway.generate.assert_not_awaited()

    def test_short_gateway_input_limit_skips_truncated_request(self) -> None:
        user_id = 720
        clear_user_cache(user_id)
        session = self._session(self._settings(user_id))
        gateway = self._gateway()

        result = self._greet(
            user_id,
            session,
            gateway,
            config=self._config(max_input_chars=100),
        )

        self.assertEqual(result, "Привет, Nicolai!")
        gateway.generate.assert_not_awaited()
        clear_user_cache(user_id)

    def test_personalization_disabled_falls_back_to_name_only(self) -> None:
        user_id = 706
        clear_user_cache(user_id)
        session = self._session(self._settings(user_id), activity_count=None)
        gateway = self._gateway("Здравствуйте!")

        result = self._greet(user_id, session, gateway, name="Nicolai")

        self.assertEqual(result, "Здравствуйте!")
        self.assertIn(
            '"frequently_opened_homework_today":false',
            gateway.generate.await_args.args[1],
        )
        clear_user_cache(user_id)

    def test_repeated_start_within_ttl_uses_cached_greeting(self) -> None:
        user_id = 707
        clear_user_cache(user_id)
        session = self._session(self._settings(user_id))
        gateway = self._gateway("Привет, Николай!")

        first = self._greet(user_id, session, gateway)
        gateway.generate.return_value = AIResult(
            intent="ordinary_reply",
            response="Здравствуй, Николай!",
        )
        second = self._greet(user_id, session, gateway)

        self.assertEqual(first, "Привет, Николай!")
        self.assertEqual(second, first)
        gateway.generate.assert_awaited_once()
        clear_user_cache(user_id)

    def test_cache_expires_after_fifteen_minutes(self) -> None:
        async def test() -> None:
            user_id = 708
            clear_user_cache(user_id)
            session = self._session(self._settings(user_id))
            gateway = self._gateway()
            with patch(
                "bot.ai.personalization.time.monotonic",
                side_effect=[100.0, 101.0, 100.0 + GREETING_CACHE_TTL_SECONDS + 1, 1000.0],
            ):
                first = await personalized_greeting(
                    session,
                    user_id,
                    self._config(),
                    "Europe/Chisinau",
                    display_name="Nicolai",
                    ai_gateway=gateway,
                    now=datetime(2026, 10, 29, 10, 0),
                )
                second = await personalized_greeting(
                    session,
                    user_id,
                    self._config(),
                    "Europe/Chisinau",
                    display_name="Nicolai",
                    ai_gateway=gateway,
                    now=datetime(2026, 10, 29, 10, 0),
                )
                third = await personalized_greeting(
                    session,
                    user_id,
                    self._config(),
                    "Europe/Chisinau",
                    display_name="Nicolai",
                    ai_gateway=gateway,
                    now=datetime(2026, 10, 29, 10, 0),
                )

            self.assertEqual(first, second)
            self.assertEqual(gateway.generate.await_count, 2)
            self.assertEqual(third, "Привет, Николай!")
            clear_user_cache(user_id)

        asyncio.run(test())

    def test_changed_personalization_settings_invalidate_cache_signature(self) -> None:
        async def test() -> None:
            user_id = 709
            clear_user_cache(user_id)
            settings = self._settings(
                user_id,
                humor_enabled=True,
                humor_style="memey",
            )
            session = self._session(settings)
            gateway = self._gateway("Привет!")
            await personalized_greeting(
                session,
                user_id,
                self._config(),
                "Europe/Chisinau",
                display_name="Nicolai",
                ai_gateway=gateway,
                now=datetime(2026, 10, 29, 10, 0),
            )
            settings.humor_style = "gentle"
            gateway.generate.return_value = AIResult(
                intent="ordinary_reply",
                response="Здравствуй!",
            )
            updated = await personalized_greeting(
                session,
                user_id,
                self._config(),
                "Europe/Chisinau",
                display_name="Nicolai",
                ai_gateway=gateway,
                now=datetime(2026, 10, 29, 10, 0),
            )

            self.assertEqual(updated, "Здравствуй!")
            self.assertEqual(gateway.generate.await_count, 2)
            clear_user_cache(user_id)

        asyncio.run(test())

    def test_disabling_and_reenabling_personalization_does_not_reuse_old_greeting(self) -> None:
        async def test() -> None:
            user_id = 722
            clear_user_cache(user_id)
            settings = self._settings(user_id)
            session = self._session(settings)
            gateway = self._gateway("Привет!")
            first = await personalized_greeting(
                session,
                user_id,
                self._config(),
                "Europe/Chisinau",
                display_name="Nicolai",
                ai_gateway=gateway,
            )

            settings.personalization_enabled = False
            clear_user_cache(user_id)
            opted_out = await personalized_greeting(
                session,
                user_id,
                self._config(),
                "Europe/Chisinau",
                display_name="Nicolai",
                ai_gateway=gateway,
            )
            settings.personalization_enabled = True
            clear_user_cache(user_id)
            gateway.generate.return_value = AIResult(
                intent="ordinary_reply",
                response="Здравствуй!",
            )
            opted_in = await personalized_greeting(
                session,
                user_id,
                self._config(),
                "Europe/Chisinau",
                display_name="Nicolai",
                ai_gateway=gateway,
            )

            self.assertEqual(first, "Привет!")
            self.assertEqual(opted_out, "Привет, Nicolai!")
            self.assertEqual(opted_in, "Здравствуй!")
            self.assertEqual(gateway.generate.await_count, 2)
            clear_user_cache(user_id)

        asyncio.run(test())

    def test_settings_change_during_generation_cannot_cache_stale_greeting(self) -> None:
        async def test() -> None:
            user_id = 723
            clear_user_cache(user_id)
            settings = self._settings(user_id)
            session = self._session(settings)
            gateway = self._gateway()
            started = asyncio.Event()
            release = asyncio.Event()

            async def generate(
                requested_user_id: int,
                user_text: str,
                *,
                allow_humor: bool,
                humor_style: str,
            ) -> AIResult:
                self.assertEqual(requested_user_id, user_id)
                self.assertTrue(user_text)
                self.assertFalse(allow_humor)
                self.assertIn(humor_style, {"memey", "gentle"})
                started.set()
                await release.wait()
                return AIResult(intent="ordinary_reply", response="Привет!")

            gateway.generate.side_effect = generate
            pending = asyncio.create_task(
                personalized_greeting(
                    session,
                    user_id,
                    self._config(),
                    "Europe/Chisinau",
                    display_name="Nicolai",
                    ai_gateway=gateway,
                )
            )
            await started.wait()
            clear_user_cache(user_id)
            release.set()
            stale_result = await pending

            self.assertEqual(stale_result, "Привет, Nicolai!")
            gateway.generate.side_effect = None
            gateway.generate.return_value = AIResult(
                intent="ordinary_reply",
                response="Здравствуй!",
            )
            fresh_result = await personalized_greeting(
                session,
                user_id,
                self._config(),
                "Europe/Chisinau",
                display_name="Nicolai",
                ai_gateway=gateway,
            )

            self.assertEqual(fresh_result, "Здравствуй!")
            self.assertEqual(gateway.generate.await_count, 2)
            clear_user_cache(user_id)

        asyncio.run(test())

    def test_gateway_failure_uses_and_caches_fallback(self) -> None:
        async def test() -> None:
            user_id = 710
            clear_user_cache(user_id)
            session = self._session(self._settings(user_id))
            gateway = self._gateway()
            gateway.generate.side_effect = TimeoutError()

            with self.assertLogs("bot.ai.personalization", level="WARNING"):
                first = await personalized_greeting(
                    session,
                    user_id,
                    self._config(),
                    "Europe/Chisinau",
                    display_name="Nicolai",
                    ai_gateway=gateway,
                )
            second = await personalized_greeting(
                session,
                user_id,
                self._config(),
                "Europe/Chisinau",
                display_name="Nicolai",
                ai_gateway=gateway,
            )

            self.assertEqual(first, "Привет, Nicolai!")
            self.assertEqual(second, first)
            gateway.generate.assert_awaited_once()
            clear_user_cache(user_id)

        asyncio.run(test())

    def test_quota_rejection_uses_fallback_without_user_facing_error(self) -> None:
        user_id = 721
        clear_user_cache(user_id)
        session = self._session(self._settings(user_id))
        gateway = self._gateway()
        gateway.generate.return_value = None

        result = self._greet(user_id, session, gateway)

        self.assertEqual(result, "Привет, Nicolai!")
        gateway.generate.assert_awaited_once()
        clear_user_cache(user_id)

    def test_empty_long_or_non_greeting_responses_use_fallback(self) -> None:
        async def test() -> None:
            for user_id, response in (
                (711, ""),
                (712, "Привет! " + "x" * 170),
                (713, "Открой меню и выбери раздел."),
                (714, "Вот интересный факт."),
                (715, "Привет!\nВыбери действие."),
            ):
                clear_user_cache(user_id)
                session = self._session(self._settings(user_id))
                gateway = self._gateway(response)

                result = await personalized_greeting(
                    session,
                    user_id,
                    self._config(),
                    "Europe/Chisinau",
                    display_name="Nicolai",
                    ai_gateway=gateway,
                )

                self.assertEqual(result, "Привет, Nicolai!")
                clear_user_cache(user_id)

        asyncio.run(test())

    def test_users_do_not_share_cached_greetings(self) -> None:
        async def test() -> None:
            first_id, second_id = 716, 717
            clear_user_cache(first_id)
            clear_user_cache(second_id)
            gateway = self._gateway()

            async def generate(
                user_id: int,
                user_text: str,
                *,
                allow_humor: bool,
                humor_style: str,
            ) -> AIResult:
                self.assertTrue(user_text)
                self.assertFalse(allow_humor)
                self.assertIn(humor_style, {"memey", "gentle"})
                return AIResult(
                    intent="ordinary_reply",
                    response=f"Привет, пользователь {user_id}!",
                )

            gateway.generate.side_effect = generate
            first = await personalized_greeting(
                self._session(self._settings(first_id)),
                first_id,
                self._config(),
                "Europe/Chisinau",
                display_name="First",
                ai_gateway=gateway,
            )
            second = await personalized_greeting(
                self._session(self._settings(second_id)),
                second_id,
                self._config(),
                "Europe/Chisinau",
                display_name="Second",
                ai_gateway=gateway,
            )

            self.assertEqual(first, f"Привет, пользователь {first_id}!")
            self.assertEqual(second, f"Привет, пользователь {second_id}!")
            self.assertEqual(gateway.generate.await_count, 2)
            clear_user_cache(first_id)
            clear_user_cache(second_id)

        asyncio.run(test())

    def test_parallel_repeated_starts_make_one_gateway_call(self) -> None:
        async def test() -> None:
            user_id = 718
            clear_user_cache(user_id)
            session = self._session(self._settings(user_id))
            gateway = self._gateway()
            started = asyncio.Event()
            release = asyncio.Event()

            async def generate(
                user_id: int,
                user_text: str,
                *,
                allow_humor: bool,
                humor_style: str,
            ) -> AIResult:
                self.assertEqual(user_id, 718)
                self.assertTrue(user_text)
                self.assertFalse(allow_humor)
                self.assertIn(humor_style, {"memey", "gentle"})
                started.set()
                await release.wait()
                return AIResult(intent="ordinary_reply", response="Привет!")

            gateway.generate.side_effect = generate
            first = asyncio.create_task(
                personalized_greeting(
                    session,
                    user_id,
                    self._config(),
                    "Europe/Chisinau",
                    display_name="Nicolai",
                    ai_gateway=gateway,
                )
            )
            await started.wait()
            second = asyncio.create_task(
                personalized_greeting(
                    session,
                    user_id,
                    self._config(),
                    "Europe/Chisinau",
                    display_name="Nicolai",
                    ai_gateway=gateway,
                )
            )
            await asyncio.sleep(0)
            release.set()
            results = await asyncio.gather(first, second)

            self.assertEqual(results, ["Привет!", "Привет!"])
            gateway.generate.assert_awaited_once()
            clear_user_cache(user_id)

        asyncio.run(test())

    def test_name_and_generated_text_are_escaped_for_telegram_html(self) -> None:
        async def test() -> None:
            user_id = 719
            clear_user_cache(user_id)
            bot, message = self._start_message("<b>Injected</b>")
            state = AsyncMock()
            session = self._session(self._settings(user_id))
            gateway = self._gateway("Привет, <b>Injected</b>!")
            config = SimpleNamespace(
                ai=self._config(),
                bot=SimpleNamespace(timezone="Europe/Chisinau"),
            )
            with patch.object(Message, "answer", new_callable=AsyncMock) as answer:
                await start_handler(
                    message,
                    state,
                    "user",
                    session,
                    config,
                    gateway,
                )

            welcome = answer.await_args_list[0]
            self.assertEqual(welcome.kwargs["parse_mode"], ParseMode.HTML)
            self.assertIn("&lt;b&gt;Injected&lt;/b&gt;", welcome.args[0])
            self.assertNotIn("<b>Injected</b>", welcome.args[0])
            await bot.session.close()
            clear_user_cache(user_id)

        asyncio.run(test())

    def test_start_keeps_fsm_clear_and_existing_keyboards_without_duplicate_prompt(self) -> None:
        async def test() -> None:
            bot, message = self._start_message()
            state = AsyncMock()
            session = self._session(None)
            gateway = self._gateway()
            config = SimpleNamespace(
                ai=self._config(),
                bot=SimpleNamespace(timezone="Europe/Chisinau"),
            )
            with (
                patch(
                    "bot.handlers.base.personalized_greeting",
                    new_callable=AsyncMock,
                    return_value="Привет, Student!",
                ) as personalize,
                patch.object(Message, "answer", new_callable=AsyncMock) as answer,
            ):
                await start_handler(
                    message,
                    state,
                    "user",
                    session,
                    config,
                    gateway,
                )

            state.clear.assert_awaited_once()
            personalize.assert_awaited_once()
            self.assertEqual(answer.await_count, 2)
            welcome = answer.await_args_list[0]
            self.assertTrue(welcome.args[0].endswith("Привет, Student!"))
            self.assertIsInstance(welcome.kwargs["reply_markup"], InlineKeyboardMarkup)
            self.assertEqual(answer.await_args_list[1].args[0], "Меню")
            self.assertIsInstance(answer.await_args_list[1].kwargs["reply_markup"], ReplyKeyboardMarkup)
            self.assertNotIn("Выбери нужное действие", welcome.args[0])
            self.assertNotIn("подгрупп", welcome.args[0].casefold())
            await bot.session.close()

        asyncio.run(test())

    def test_start_prefers_existing_roster_name_from_user_record(self) -> None:
        async def test() -> None:
            user_id = 724
            bot, message = self._start_message("Telegram Name")
            state = AsyncMock()
            session = AsyncMock()
            session.get.return_value = User(
                telegram_id=user_id,
                full_name="Roster Name",
            )
            config = SimpleNamespace(
                ai=self._config(),
                bot=SimpleNamespace(timezone="Europe/Chisinau"),
            )
            with (
                patch(
                    "bot.handlers.base.personalized_greeting",
                    new_callable=AsyncMock,
                    return_value="Привет, Roster Name!",
                ) as personalize,
                patch.object(Message, "answer", new_callable=AsyncMock) as answer,
            ):
                await start_handler(
                    message,
                    state,
                    "user",
                    session,
                    config,
                    self._gateway(),
                )

            personalize.assert_awaited_once()
            await_call = personalize.await_args
            assert await_call is not None
            self.assertEqual(
                await_call.kwargs["display_name"],
                "Roster Name",
            )
            self.assertIn("Привет, Roster Name!", answer.await_args_list[0].args[0])
            await bot.session.close()

        asyncio.run(test())

    def test_start_survives_personalization_context_failure(self) -> None:
        async def test() -> None:
            bot, message = self._start_message("Nicolai")
            state = AsyncMock()
            session = AsyncMock()
            telegram_user = message.from_user
            assert telegram_user is not None

            async def get_settings(model: type[object], user_id: int) -> object | None:
                self.assertEqual(user_id, telegram_user.id)
                if model is User:
                    return None
                if model is AIUserSettings:
                    raise RuntimeError("unavailable")
                raise AssertionError(f"Unexpected model lookup: {model.__name__}")

            session.get.side_effect = get_settings
            gateway = self._gateway()
            config = SimpleNamespace(
                ai=self._config(),
                bot=SimpleNamespace(timezone="Europe/Chisinau"),
            )
            with (
                patch.object(Message, "answer", new_callable=AsyncMock) as answer,
                self.assertLogs("bot.ai.personalization", level="WARNING"),
            ):
                await start_handler(
                    message,
                    state,
                    "user",
                    session,
                    config,
                    gateway,
                )

            self.assertIn("Привет, Nicolai!", answer.await_args_list[0].args[0])
            gateway.generate.assert_not_awaited()
            await bot.session.close()

        asyncio.run(test())


if __name__ == "__main__":
    unittest.main()
