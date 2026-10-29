from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import Command, StateFilter
from aiogram.filters.callback_data import CallbackData
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.ai.gateway import AIGateway
from bot.ai.personalization import clear_user_cache, reset_personalization
from bot.ai.policy import explicit_navigation, is_bot_capability_question
from bot.ai.reactions import react_to_message
from bot.handlers.base import _help_keyboard, _help_text
from bot.handlers.hw import show_tomorrow_homework
from bot.handlers.schedule import show_next_schedule
from bot.keyboards.inline import InlineKeyboardBuilder
from config_loader import AppConfig
from database.models import AIUserSettings

logger = logging.getLogger(__name__)
router = Router(name="ai")


class AISettingsCallback(CallbackData, prefix="aiset"):
    action: str


def _settings_keyboard(settings: AIUserSettings) -> object:
    keyboard = InlineKeyboardBuilder()
    toggles = (
        ("gemini", "Gemini", settings.gemini_enabled),
        ("humor", "Юмор", settings.humor_enabled),
        ("personalization", "Персонализация", settings.personalization_enabled),
        ("reactions", "Реакции", settings.reactions_enabled),
    )
    for action, label, enabled in toggles:
        keyboard.button(
            text=f"{'✅' if enabled else '▫️'} {label}",
            callback_data=AISettingsCallback(action=action).pack(),
        )
    keyboard.button(
        text=f"Стиль: {'мемный' if settings.humor_style == 'memey' else 'мягкий'}",
        callback_data=AISettingsCallback(action="style").pack(),
    )
    keyboard.button(
        text="Сбросить AI-данные",
        callback_data=AISettingsCallback(action="reset").pack(),
        style="danger",
    )
    keyboard.adjust(2, 2, 1, 1)
    return keyboard.as_markup()


async def _get_settings(session: AsyncSession, user_id: int) -> AIUserSettings:
    settings = await session.get(AIUserSettings, user_id)
    if settings is None:
        settings = AIUserSettings(telegram_id=user_id)
        session.add(settings)
        await session.flush()
    return settings


def _settings_text(config: AppConfig) -> str:
    global_state = (
        "включён после проверки квот"
        if config.ai.enabled and config.ai.project_limits_verified
        else "выключен администратором или ожидает проверки квот"
    )
    credential_state = "ключ Gemini настроен" if config.ai.api_key else "ключ Gemini не настроен"
    return (
        "Настройки AI\n"
        f"Gemini на сервере: {global_state}; {credential_state}.\n"
        "Каждая функция выключена для пользователя по умолчанию. "
        "Нажми кнопки, чтобы включить нужные функции.\n"
        "Текст сообщения не сохраняется; при включённом Gemini только текущее сообщение "
        "(не более 500 символов) отправляется Google. Локальные агрегаты активности "
        f"хранятся до {config.ai.activity_retention_days} дней, счётчики квот — "
        f"до {config.ai.quota_retention_days} дней, а хеш шутки для защиты от повторов — "
        "до 1 дня.\n"
        "Персональные настройки не меняют лимиты запросов."
    )


@router.message(StateFilter(None), Command("ai"))
async def ai_settings_handler(
    message: Message,
    session: AsyncSession,
    config: AppConfig,
) -> None:
    if message.chat.type != "private" or message.from_user is None:
        return
    settings = await _get_settings(session, message.from_user.id)
    await session.commit()
    await message.answer(
        _settings_text(config),
        reply_markup=_settings_keyboard(settings),
    )


@router.callback_query(StateFilter(None), AISettingsCallback.filter())
async def ai_settings_callback_handler(
    callback: CallbackQuery,
    callback_data: AISettingsCallback,
    session: AsyncSession,
    config: AppConfig,
) -> None:
    if callback.message is None or callback.message.chat.type != "private":
        await callback.answer()
        return
    settings = await _get_settings(session, callback.from_user.id)
    if callback_data.action == "reset":
        await reset_personalization(session, callback.from_user.id)
        await session.commit()
        settings = await session.get(AIUserSettings, callback.from_user.id)
        if settings is None:
            settings = await _get_settings(session, callback.from_user.id)
        await callback.answer("Персональные AI-настройки сброшены. Лимиты запросов сохранены.")
    elif callback_data.action == "style":
        settings.humor_style = "gentle" if settings.humor_style == "memey" else "memey"
        clear_user_cache(callback.from_user.id)
        await session.commit()
        await callback.answer("Стиль общения обновлён.")
    else:
        field_by_action = {
            "gemini": "gemini_enabled",
            "humor": "humor_enabled",
            "personalization": "personalization_enabled",
            "reactions": "reactions_enabled",
        }
        field = field_by_action.get(callback_data.action)
        if field is None:
            await callback.answer("Неизвестная настройка.", show_alert=True)
            return
        setattr(settings, field, not getattr(settings, field))
        if field in {"gemini_enabled", "humor_enabled", "personalization_enabled"}:
            clear_user_cache(callback.from_user.id)
        await session.commit()
        await callback.answer("Настройка сохранена.")

    await callback.message.edit_reply_markup(reply_markup=_settings_keyboard(settings))


@router.message(
    StateFilter(None),
    F.chat.type == "private",
    F.text,
    ~F.text.startswith("/"),
)
async def private_text_handler(
    message: Message,
    session: AsyncSession,
    config: AppConfig,
    ai_gateway: AIGateway,
) -> None:
    if message.from_user is None or message.text is None:
        return
    settings = await session.get(AIUserSettings, message.from_user.id)

    if config.ai.reactions_enabled and settings is not None and settings.reactions_enabled:
        await react_to_message(message, message.from_user.id)

    navigation = explicit_navigation(message.text)
    if navigation == "navigation_homework":
        await show_tomorrow_homework(message, config, session)
        return
    if navigation == "navigation_schedule":
        await show_next_schedule(
            message,
            config,
            session,
            telegram_id=message.from_user.id,
            display_name=message.from_user.first_name,
        )
        return
    if navigation == "navigation_help":
        await message.answer(_help_text("user"))
        await message.answer("Справка и разделы:", reply_markup=_help_keyboard(config))
        return
    if is_bot_capability_question(message.text):
        await message.answer(
            "Я показываю домашние задания, расписание, документы, заявки и подгруппу. "
            "Открой /help, чтобы посмотреть разделы."
        )
        return

    if (
        settings is None
        or not settings.gemini_enabled
        or not config.ai.enabled
        or not config.ai.project_limits_verified
        or not config.ai.api_key
    ):
        return
    result = await ai_gateway.generate(
        message.from_user.id,
        message.text,
        allow_humor=config.ai.humor_enabled and settings.humor_enabled,
        humor_style=settings.humor_style,
    )
    if result is None:
        await message.answer("Сейчас не удалось получить ответ от AI. Обычные разделы бота работают.")
        return
    if result.intent == "bot_question":
        await message.answer(
            "Я показываю домашние задания, расписание, документы, заявки и подгруппу. "
            "Открой /help, чтобы посмотреть разделы."
        )
    elif result.intent.startswith("navigation_"):
        logger.info(
            "Ignored model navigation intent without an explicit local navigation request "
            "intent=%s",
            result.intent,
        )
        await message.answer("Не до конца понял вопрос. Попробуй сформулировать иначе.")
    elif result.intent in {"ordinary_reply", "joke_or_banter"} and result.response:
        await message.answer(result.response)
