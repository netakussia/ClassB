from __future__ import annotations

from collections.abc import Iterable
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.filters.callback_data import CallbackData
from aiogram.filters.command import CommandObject
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.ui import MenuCallback, remove_inline_screen
from bot.utils.subjects import all_subjects
from bot.utils.homework_dates import (
    homework_target_date,
    local_today,
    next_lesson_date,
    subjects_for_date,
)
from config_loader import AppConfig
from database.models import Homework


router = Router(name="homework")
TELEGRAM_TEXT_LIMIT = 4000


class HomeworkNavigationCallback(CallbackData, prefix="hwui"):
    action: str
    subject_index: int


async def _answer_long_text(
    message: Message,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    """Send text in Telegram-safe chunks without losing newlines."""
    while len(text) > TELEGRAM_TEXT_LIMIT:
        split_at = text.rfind("\n", 0, TELEGRAM_TEXT_LIMIT)
        if split_at <= 0:
            split_at = TELEGRAM_TEXT_LIMIT
        await message.answer(text[:split_at])
        text = text[split_at:].lstrip("\n")
    if text:
        await message.answer(text, reply_markup=reply_markup)


def _attachments_for(homework: Homework) -> Iterable[dict[str, Any]]:
    return homework.attachments or []


async def _send_homework_attachments(message: Message, homework: Homework) -> None:
    attachment_label = f"Вложение к ДЗ по {homework.subject}"
    for attachment in _attachments_for(homework):
        file_id = attachment.get("file_id")
        attachment_type = attachment.get("type")
        if not isinstance(file_id, str) or not file_id:
            continue
        if attachment_type == "photo":
            await message.answer_photo(photo=file_id, caption=attachment_label)
        elif attachment_type == "document":
            await message.answer_document(document=file_id, caption=attachment_label)


async def _send_homework_record(
    message: Message,
    homework: Homework,
    heading: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    lines = [f"{heading} · {homework.subject}"]
    if homework.is_deadline and homework.deadline_date is not None:
        lines.append(f"Дедлайн: {homework.deadline_date:%d.%m.%Y}")
    if homework.text:
        lines.append(homework.text)
    elif not homework.attachments:
        lines.append("Текст и вложения не указаны.")
    await _answer_long_text(message, "\n".join(lines), reply_markup=reply_markup)
    await _send_homework_attachments(message, homework)


def _updated_label(updated_at: datetime, timezone_name: str) -> str:
    """Format the freshness timestamp in the class timezone."""
    if updated_at.tzinfo is None:
        updated_at = updated_at.replace(tzinfo=ZoneInfo("UTC"))
    return updated_at.astimezone(ZoneInfo(timezone_name)).strftime("%d.%m.%Y в %H:%M")


def _homework_actions_keyboard() -> InlineKeyboardMarkup:
    keyboard = InlineKeyboardBuilder()
    keyboard.button(
        text="🔎 ДЗ по предмету",
        callback_data=HomeworkNavigationCallback(action="subjects", subject_index=0).pack(),
    )
    keyboard.button(
        text="📚 ДЗ на ближайший учебный день",
        callback_data=HomeworkNavigationCallback(action="day", subject_index=0).pack(),
    )
    keyboard.button(
        text="🏠 В меню",
        callback_data=MenuCallback(action="home").pack(),
    )
    keyboard.adjust(1)
    return keyboard.as_markup()


def _subject_picker_keyboard(config: AppConfig) -> InlineKeyboardMarkup:
    keyboard = InlineKeyboardBuilder()
    for index, subject in enumerate(all_subjects(config)):
        keyboard.button(
            text=subject,
            callback_data=HomeworkNavigationCallback(
                action="subject", subject_index=index
            ).pack(),
        )
    keyboard.button(
        text="⬅️ К ДЗ на ближайший учебный день",
        callback_data=HomeworkNavigationCallback(action="day", subject_index=0).pack(),
    )
    keyboard.button(
        text="🏠 В меню",
        callback_data=MenuCallback(action="home").pack(),
    )
    keyboard.adjust(1)
    return keyboard.as_markup()


async def _latest_homework_for_subject(
    session: AsyncSession,
    subject: str,
    target_date: date,
) -> Homework | None:
    result = await session.scalars(
        select(Homework)
        .where(Homework.subject == subject, Homework.target_date == target_date)
        .order_by(
            Homework.updated_at.desc(),
            Homework.id.desc(),
        )
        .limit(1)
    )
    return result.first()


async def _subjects_for_homework_date(
    session: AsyncSession,
    target_date: date,
    config: AppConfig,
) -> list[str]:
    """Resolve the actual subjects for a day, including approved lesson replacements."""
    return await subjects_for_date(session, target_date, config)


async def _next_lesson_date(
    session: AsyncSession,
    subject: str,
    config: AppConfig,
    start: date | None = None,
) -> date | None:
    """Find the next actual lesson, including replacements, within five weeks."""
    return await next_lesson_date(
        session,
        subject,
        config,
        start or local_today(config),
    )


async def _homework_lines(
    homework: Homework,
    config: AppConfig,
    session: AsyncSession,
    heading: str,
) -> list[str]:
    lines = [f"{heading}: {homework.subject}"]
    lines.append(f"🗓 На дату: {homework.target_date:%d.%m.%Y}")
    created_label = _updated_label(homework.created_at, config.bot.timezone)
    lines.append(f"🕒 Добавлено: {created_label}")
    if homework.updated_at != homework.created_at:
        lines.append(f"✏️ Обновлено: {_updated_label(homework.updated_at, config.bot.timezone)}")
    if homework.is_deadline and homework.deadline_date is not None:
        lines.append(f"⏳ Дедлайн: {homework.deadline_date:%d.%m.%Y}")
    if homework.text:
        lines.extend(["", homework.text])
    elif homework.attachments:
        lines.extend(["", "📎 Вложения отправлены ниже."])
    else:
        lines.extend(["", "Текст и вложения не указаны."])
    return lines


async def _edit_homework_record(
    message: Message,
    lines: list[str],
    homework: Homework,
) -> None:
    """Keep callback screens valid even when a homework text exceeds Telegram's limit."""
    text = "\n".join(lines)
    if len(text) <= TELEGRAM_TEXT_LIMIT:
        await message.edit_text(text, reply_markup=_homework_actions_keyboard())
    else:
        await message.edit_text(
            f"📚 ДЗ: {homework.subject}\n\nСообщение длинное — отправляю его ниже.",
        )
        await _answer_long_text(message, text, reply_markup=_homework_actions_keyboard())
    await _send_homework_attachments(message, homework)


async def show_tomorrow_homework(
    message: Message,
    config: AppConfig,
    session: AsyncSession,
) -> None:
    """Show homework for today until classes finish, then the next school day."""
    try:
        target_date = await homework_target_date(session, config)
    except ValueError:
        await message.answer(
            "Расписание ещё не настроено. Обратитесь к администратору.",
            reply_markup=_homework_actions_keyboard(),
        )
        return
    if target_date is None:
        await message.answer(
            "Ближайший учебный день не найден в расписании.",
            reply_markup=_homework_actions_keyboard(),
        )
        return
    try:
        subjects = await _subjects_for_homework_date(session, target_date, config)
    except ValueError:
        await message.answer(
            "Расписание ещё не настроено. Обратитесь к администратору.",
            reply_markup=_homework_actions_keyboard(),
        )
        return

    if not subjects:
        await message.answer(
            f"На {target_date:%d.%m.%Y} уроков нет.",
            reply_markup=_homework_actions_keyboard(),
        )
        return

    homework_result = await session.scalars(
        select(Homework)
        .where(
            Homework.subject.in_(subjects),
            Homework.target_date == target_date,
        )
        .order_by(Homework.updated_at.desc(), Homework.id.desc())
    )
    regular_by_subject = {homework.subject: homework for homework in homework_result}

    keyboard = InlineKeyboardBuilder()
    for index, subject in enumerate(subjects):
        mark = "✅" if subject in regular_by_subject else "▫️"
        keyboard.button(text=f"{mark} {subject}", callback_data=HomeworkNavigationCallback(action="day_subject", subject_index=index).pack())
    keyboard.button(
        text="🔎 Найти ДЗ по предмету",
        callback_data=HomeworkNavigationCallback(action="subjects", subject_index=0).pack(),
    )
    keyboard.button(text="🏠 В меню", callback_data=MenuCallback(action="home").pack())
    keyboard.adjust(1)
    await message.answer(
        f"📚 ДЗ на {target_date:%d.%m.%Y}\n\n"
        "✅ — задание есть\n▫️ — пока ничего не задано\n\n"
        "Выбери предмет:",
        reply_markup=keyboard.as_markup(),
    )



@router.message(Command("hw"))
async def homework_handler(
    message: Message,
    command: CommandObject,
    config: AppConfig,
    session: AsyncSession,
) -> None:
    subject_query = (command.args or "").strip()
    if not subject_query:
        await show_tomorrow_homework(message, config, session)
        return

    today = local_today(config)
    homework = await _latest_homework_for_subject(session, subject_query, today)
    future_date = await next_lesson_date(
        session,
        subject_query,
        config,
        today,
        inclusive=False,
    )
    future_homework = (
        await _latest_homework_for_subject(session, subject_query, future_date)
        if future_date is not None
        else None
    )
    if homework is None and future_homework is None:
        await message.answer(
            f"По предмету «{subject_query}» пока нет ДЗ.",
            reply_markup=_homework_actions_keyboard(),
        )
        return
    primary = homework or future_homework
    assert primary is not None
    lines = await _homework_lines(
        primary,
        config,
        session,
        "📚 ДЗ на сегодня" if homework is not None else "📚 ДЗ на следующий урок",
    )
    if homework is not None and future_homework is not None:
        future_text = future_homework.text or "Текст не указан."
        lines.extend([
            "",
            f"📌 На следующий урок ({future_homework.target_date:%d.%m}): {future_text}",
        ])
    await _answer_long_text(message, "\n".join(lines), reply_markup=_homework_actions_keyboard())
    await _send_homework_attachments(message, primary)


@router.callback_query(
    HomeworkNavigationCallback.filter(F.action == "subjects")
)
async def homework_subject_picker_handler(
    callback: CallbackQuery,
    config: AppConfig,
) -> None:
    if callback.message is None:
        await callback.answer("Открой меню ещё раз.", show_alert=True)
        return
    await callback.answer()
    await callback.message.edit_text(
        "🔎 ДЗ ПО ПРЕДМЕТУ\n\nВыбери предмет:",
        reply_markup=_subject_picker_keyboard(config),
    )


@router.callback_query(
    HomeworkNavigationCallback.filter(F.action == "subject")
)
async def homework_subject_handler(
    callback: CallbackQuery,
    callback_data: HomeworkNavigationCallback,
    config: AppConfig,
    session: AsyncSession,
) -> None:
    if callback.message is None:
        await callback.answer("Открой меню ещё раз.", show_alert=True)
        return
    subjects = all_subjects(config)
    if callback_data.subject_index not in range(len(subjects)):
        await callback.answer("Список предметов успел измениться. Открой его ещё раз.", show_alert=True)
        return

    subject = subjects[callback_data.subject_index]
    homework = await _latest_homework_for_subject(
        session,
        subject,
        local_today(config),
    )
    await callback.answer()
    if homework is None:
        await callback.message.edit_text(
            f"По «{subject}» пока ничего не задано.",
            reply_markup=_homework_actions_keyboard(),
        )
        return

    lines = await _homework_lines(homework, config, session, "📚 Последнее ДЗ")
    await _edit_homework_record(callback.message, lines, homework)


@router.callback_query(HomeworkNavigationCallback.filter(F.action == "day_subject"))
async def homework_day_subject_handler(callback: CallbackQuery, callback_data: HomeworkNavigationCallback, config: AppConfig, session: AsyncSession) -> None:
    if callback.message is None:
        await callback.answer("Открой /hw ещё раз.", show_alert=True)
        return
    target_date = await homework_target_date(session, config)
    if target_date is None:
        await callback.answer("Ближайший учебный день не найден.", show_alert=True)
        return
    subjects = await _subjects_for_homework_date(session, target_date, config)
    if callback_data.subject_index not in range(len(subjects)):
        await callback.answer("Расписание изменилось. Открой /hw ещё раз.", show_alert=True)
        return
    subject = subjects[callback_data.subject_index]
    homework = await _latest_homework_for_subject(session, subject, target_date)
    await callback.answer()
    if homework is None:
        await callback.message.edit_text(f"По «{subject}» пока ничего не задано.", reply_markup=_homework_actions_keyboard())
        return
    lines = await _homework_lines(homework, config, session, "📚 ДЗ")
    await _edit_homework_record(callback.message, lines, homework)


@router.callback_query(HomeworkNavigationCallback.filter(F.action == "day"))
async def homework_day_picker_handler(
    callback: CallbackQuery,
    config: AppConfig,
    session: AsyncSession,
) -> None:
    if callback.message is None:
        await callback.answer("Открой /hw ещё раз.", show_alert=True)
        return
    await callback.answer()
    await remove_inline_screen(callback)
    await show_tomorrow_homework(callback.message, config, session)
