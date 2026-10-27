from __future__ import annotations

import html
from collections.abc import Iterable
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.filters.callback_data import CallbackData
from aiogram.filters.command import CommandObject
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.keyboards.inline import InlineKeyboardBuilder
from bot.ui import MenuCallback, edit_text_if_changed, remove_inline_screen
from bot.utils.emojis import get_btn_emoji, get_button_label, get_emoji
from bot.utils.subjects import all_subjects, resolve_subject
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


def _homework_subject_keyboard(
    subjects: list[str],
    homework_subjects: set[str],
) -> InlineKeyboardMarkup:
    keyboard = InlineKeyboardBuilder()
    for index, subject in enumerate(subjects):
        has_hw = subject in homework_subjects
        if has_hw:
            mark = get_btn_emoji("check")
            button_options = {"style": "success", "icon_key": "check"}
        else:
            mark = "◾"
            button_options = {}
        keyboard.button(
            text=f"{mark} {subject}",
            callback_data=HomeworkNavigationCallback(
                action="day_subject",
                subject_index=index,
            ).pack(),
            **button_options,
        )
    keyboard.button(
        text=get_button_label("Найти ДЗ по предмету", icon_key="search"),
        callback_data=HomeworkNavigationCallback(action="subjects", subject_index=0).pack(),
        style="primary",
    )
    keyboard.button(
        text=get_button_label("В меню", icon_key="back_arrow"),
        callback_data=MenuCallback(action="home").pack(),
    )
    keyboard.adjust(1)
    return keyboard.as_markup()


async def _answer_long_text(
    message: Message,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
    parse_mode: ParseMode | None = None,
) -> None:
    """Send text in Telegram-safe chunks without losing newlines."""
    while len(text) > TELEGRAM_TEXT_LIMIT:
        split_at = text.rfind("\n", 0, TELEGRAM_TEXT_LIMIT)
        if split_at <= 0:
            split_at = TELEGRAM_TEXT_LIMIT
        await message.answer(text[:split_at], parse_mode=parse_mode)
        text = text[split_at:].lstrip("\n")
    if text:
        await message.answer(text, reply_markup=reply_markup, parse_mode=parse_mode)


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
    lines = [f"{html.escape(heading)} · <b>{html.escape(homework.subject)}</b>"]
    if homework.is_deadline and homework.deadline_date is not None:
        lines.append(f"Дедлайн: {homework.deadline_date:%d.%m.%Y}")
    if homework.text:
        lines.append(f"<blockquote>{html.escape(homework.text)}</blockquote>")
    elif not homework.attachments:
        lines.append("Текст и вложения не указаны.")
    await _answer_long_text(
        message,
        "\n".join(lines),
        reply_markup=reply_markup,
        parse_mode=ParseMode.HTML,
    )
    await _send_homework_attachments(message, homework)


def _updated_label(updated_at: datetime, timezone_name: str) -> str:
    """Format the freshness timestamp in the class timezone."""
    if updated_at.tzinfo is None:
        updated_at = updated_at.replace(tzinfo=ZoneInfo("UTC"))
    return updated_at.astimezone(ZoneInfo(timezone_name)).strftime("%d.%m.%Y в %H:%M")


def _homework_actions_keyboard() -> InlineKeyboardMarkup:
    keyboard = InlineKeyboardBuilder()
    keyboard.button(
        text=get_button_label("ДЗ по предмету", icon_key="search"),
        callback_data=HomeworkNavigationCallback(action="subjects", subject_index=0).pack(),
        style="primary",
    )
    keyboard.button(
        text=get_button_label("ДЗ на ближайший учебный день", icon_key="homework"),
        callback_data=HomeworkNavigationCallback(action="day", subject_index=0).pack(),
        style="primary",
    )
    keyboard.button(
        text=get_button_label("В меню", icon_key="back_arrow"),
        callback_data=MenuCallback(action="home").pack(),
    )
    keyboard.adjust(1)
    return keyboard.as_markup()


def _subject_picker_keyboard(config: AppConfig) -> InlineKeyboardMarkup:
    keyboard = InlineKeyboardBuilder()
    for index, subject in enumerate(all_subjects(config)):
        keyboard.button(
            text=get_button_label(subject),
            callback_data=HomeworkNavigationCallback(
                action="subject", subject_index=index
            ).pack(),
        )
    keyboard.button(
        text=get_button_label(
            "К ДЗ на ближайший учебный день",
            icon_key="back_arrow",
        ),
        callback_data=HomeworkNavigationCallback(action="day", subject_index=0).pack(),
    )
    keyboard.button(
        text=get_button_label("В меню", icon_key="back_arrow"),
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
    lines = [
        f"{heading}: <b>{html.escape(homework.subject)}</b>"
    ]
    lines.append(f"{get_emoji('date')} На дату: {homework.target_date:%d.%m.%Y}")
    created_label = _updated_label(homework.created_at, config.bot.timezone)
    lines.append(f"{get_emoji('time')} Добавлено: {created_label}")
    if homework.updated_at != homework.created_at:
        lines.append(
            f"{get_emoji('edit')} Обновлено: "
            f"{_updated_label(homework.updated_at, config.bot.timezone)}"
        )
    if homework.is_deadline and homework.deadline_date is not None:
        lines.append(f"{get_emoji('deadline')} Дедлайн: {homework.deadline_date:%d.%m.%Y}")
    if homework.text:
        lines.extend(["", f"<blockquote>{html.escape(homework.text)}</blockquote>"])
    elif homework.attachments:
        lines.extend(["", f"{get_emoji('attachment')} Вложения отправлены ниже."])
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
        await edit_text_if_changed(
            message,
            text,
            reply_markup=_homework_actions_keyboard(),
            parse_mode=ParseMode.HTML,
        )
    else:
        await edit_text_if_changed(
            message,
            f"{get_emoji('homework')} ДЗ: {html.escape(homework.subject)}\n\n"
            "Сообщение длинное — отправляю его ниже.",
            parse_mode=ParseMode.HTML,
        )
        await _answer_long_text(
            message,
            text,
            reply_markup=_homework_actions_keyboard(),
            parse_mode=ParseMode.HTML,
        )
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

    await message.answer(
        f"{get_emoji('homework')} ДЗ на {target_date:%d.%m.%Y}\n\n"
        f"{get_emoji('check')} — задание есть\n"
        f"{get_emoji('bullet_empty')} — пока ничего не задано\n\n"
        "Выбери предмет:",
        reply_markup=_homework_subject_keyboard(subjects, set(regular_by_subject)),
        parse_mode=ParseMode.HTML,
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

    try:
        subject = resolve_subject(subject_query, config) or subject_query
        today = local_today(config)
        homework = await _latest_homework_for_subject(session, subject, today)
        future_date = await next_lesson_date(
            session,
            subject,
            config,
            today,
            inclusive=False,
        )
        future_homework = (
            await _latest_homework_for_subject(session, subject, future_date)
            if future_date is not None
            else None
        )
    except ValueError:
        await message.answer(
            "Не удалось обработать предмет или расписание. "
            "Проверь название предмета или обратись к администратору.",
            reply_markup=_homework_actions_keyboard(),
        )
        return
    if homework is None and future_homework is None:
        await message.answer(
            f"По предмету «{html.escape(subject_query)}» пока нет ДЗ.",
            reply_markup=_homework_actions_keyboard(),
            parse_mode=ParseMode.HTML,
        )
        return
    primary = homework or future_homework
    assert primary is not None
    lines = await _homework_lines(
        primary,
        config,
        session,
        f"{get_emoji('homework')} ДЗ на сегодня"
        if homework is not None
        else f"{get_emoji('homework')} ДЗ на следующий урок",
    )
    if homework is not None and future_homework is not None:
        future_text = future_homework.text or "Текст не указан."
        lines.extend([
            "",
            f"{get_emoji('today')} На следующий урок "
            f"({future_homework.target_date:%d.%m}): {html.escape(future_text)}",
        ])
    await _answer_long_text(
        message,
        "\n".join(lines),
        reply_markup=_homework_actions_keyboard(),
        parse_mode=ParseMode.HTML,
    )
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
    await edit_text_if_changed(
        callback.message,
        f"{get_emoji('search')} ДЗ ПО ПРЕДМЕТУ\n\nВыбери предмет:",
        reply_markup=_subject_picker_keyboard(config),
        parse_mode=ParseMode.HTML,
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
    today = local_today(config)
    homework = await _latest_homework_for_subject(
        session,
        subject,
        today,
    )
    if homework is None:
        future_date = await next_lesson_date(
            session,
            subject,
            config,
            today,
            inclusive=False,
        )
        if future_date is not None:
            homework = await _latest_homework_for_subject(
                session,
                subject,
                future_date,
            )
    await callback.answer()
    if homework is None:
        await edit_text_if_changed(
            callback.message,
            f"По «{subject}» пока ничего не задано.",
            reply_markup=_homework_actions_keyboard(),
        )
        return

    lines = await _homework_lines(
        homework,
        config,
        session,
        f"{get_emoji('homework')} Последнее ДЗ",
    )
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
        await edit_text_if_changed(
            callback.message,
            f"По «{subject}» пока ничего не задано.",
            reply_markup=_homework_actions_keyboard(),
        )
        return
    lines = await _homework_lines(
        homework,
        config,
        session,
        f"{get_emoji('homework')} ДЗ",
    )
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
