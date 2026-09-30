from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from functools import wraps
from zoneinfo import ZoneInfo

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession

from bot.ui import MenuCallback, add_cancel_button, menu_back_keyboard
from bot.utils.schedule_parser import WEEKDAYS, parse_schedule
from bot.utils.subjects import all_subjects
from config_loader import AppConfig, Lesson
from database.models import ScheduleOverride


router = Router(name="replacements")
logger = logging.getLogger(__name__)
ADMIN_ROLES = {"owner", "starosta", "moderator"}


class ReplaceForm(StatesGroup):
    target_date = State()
    lesson_number = State()
    override_type = State()
    subject = State()
    new_subject_confirmation = State()
    new_data = State()


class ReplaceDateCallback(CallbackData, prefix="replace_date"):
    choice: str


class ReplaceSubjectCallback(CallbackData, prefix="replace_subject"):
    action: str
    subject_index: int


def admin_required(handler):
    """Prevent ordinary users from running a protected handler."""

    @wraps(handler)
    async def wrapped(event: Message | CallbackQuery, *args, **kwargs):
        if kwargs.get("role") in ADMIN_ROLES:
            return await handler(event, *args, **kwargs)
        if isinstance(event, CallbackQuery):
            await event.answer("Эта кнопка только для админов.", show_alert=True)
        else:
            await event.answer("Эта команда только для админов.")
        return None

    return wrapped


def _date_keyboard() -> InlineKeyboardMarkup:
    keyboard = InlineKeyboardBuilder()
    keyboard.button(
        text="📍 На сегодня",
        callback_data=ReplaceDateCallback(choice="today").pack(),
    )
    keyboard.button(
        text="🗓 На завтра",
        callback_data=ReplaceDateCallback(choice="tomorrow").pack(),
    )
    keyboard.button(
        text="✍️ Другая дата",
        callback_data=ReplaceDateCallback(choice="other").pack(),
    )
    keyboard.button(
        text="🏠 В меню",
        callback_data=MenuCallback(action="home").pack(),
    )
    add_cancel_button(keyboard)
    keyboard.adjust(2, 1, 1)
    return keyboard.as_markup()


def _subject_keyboard(config: AppConfig) -> InlineKeyboardMarkup:
    keyboard = InlineKeyboardBuilder()
    for index, subject in enumerate(all_subjects(config)):
        keyboard.button(text=subject, callback_data=ReplaceSubjectCallback(action="select", subject_index=index).pack())
    keyboard.button(text="➕ Добавить новый предмет в список", callback_data=ReplaceSubjectCallback(action="new", subject_index=0).pack())
    keyboard.button(text="🏠 В меню", callback_data=MenuCallback(action="home").pack())
    add_cancel_button(keyboard)
    keyboard.adjust(1)
    return keyboard.as_markup()


def _local_today(config: AppConfig) -> date:
    return datetime.now(ZoneInfo(config.bot.timezone)).date()


def _parse_short_date(value: str, today: date) -> date | None:
    try:
        day_text, month_text = value.split(".")
        if len(day_text) != 2 or len(month_text) != 2:
            return None
        target_date = date(today.year, int(month_text), int(day_text))
        if target_date < today:
            target_date = date(today.year + 1, int(month_text), int(day_text))
        return target_date
    except ValueError:
        return None


def _lessons_for_date(target_date: date, config: AppConfig) -> list[Lesson]:
    weekday = target_date.weekday()
    if weekday >= len(WEEKDAYS):
        return []
    schedule = parse_schedule(config.schedule)
    parity = schedule.get_parity(target_date)
    return schedule.lessons_for(WEEKDAYS[weekday], parity)


async def _ask_for_lesson(
    message: Message,
    state: FSMContext,
    target_date: date,
    config: AppConfig,
    replace_message: bool = False,
) -> None:
    try:
        lessons = _lessons_for_date(target_date, config)
    except ValueError:
        await message.answer("Расписание недоступно. Выбери другую дату.")
        return
    if not lessons:
        await message.answer(f"На {target_date:%d.%m.%Y} уроков нет. Выбери другую дату.")
        return

    await state.update_data(target_date=target_date.isoformat(), lesson_count=len(lessons))
    await state.set_state(ReplaceForm.lesson_number)
    keyboard = InlineKeyboardBuilder()
    for lesson_number, lesson in enumerate(lessons, start=1):
        keyboard.button(
            text=f"{lesson_number}. {lesson.subject}",
            callback_data=f"replace:lesson:{lesson_number}",
        )
    keyboard.button(
        text="🏠 В меню",
        callback_data=MenuCallback(action="home").pack(),
    )
    add_cancel_button(keyboard)
    keyboard.adjust(1)
    prompt = f"Выбери урок для замены на {target_date:%d.%m.%Y}:"
    if replace_message:
        await message.edit_text(prompt, reply_markup=keyboard.as_markup())
    else:
        await message.answer(prompt, reply_markup=keyboard.as_markup())


async def start_replacement_form(
    message: Message,
    state: FSMContext,
    role: str,
) -> None:
    if role not in ADMIN_ROLES:
        await message.answer("Эта штука только для админов.")
        return
    await state.clear()
    await state.set_state(ReplaceForm.target_date)
    await message.answer(
        "🔁 ДОБАВЛЯЕМ ЗАМЕНУ\n\nНа какую дату?",
        reply_markup=_date_keyboard(),
    )


@router.message(Command("replace"))
async def replace_start_handler(message: Message, state: FSMContext, role: str) -> None:
    await start_replacement_form(message, state, role)


async def _select_replacement_date(
    message: Message,
    state: FSMContext,
    target_date: date,
    config: AppConfig,
) -> None:
    await _ask_for_lesson(message, state, target_date, config)


@router.callback_query(ReplaceForm.target_date, ReplaceDateCallback.filter())
@admin_required
async def replace_date_choice_handler(
    callback: CallbackQuery,
    callback_data: ReplaceDateCallback,
    state: FSMContext,
    config: AppConfig,
    role: str,
) -> None:
    if callback.message is None:
        await callback.answer("Начни добавление замены ещё раз.", show_alert=True)
        return
    if callback_data.choice == "other":
        await callback.answer()
        await callback.message.edit_text(
            "Введи дату в формате ДД.ММ, например 05.10.",
            reply_markup=menu_back_keyboard(),
        )
        return
    today = _local_today(config)
    choices = {"today": today, "tomorrow": today + timedelta(days=1)}
    target_date = choices.get(callback_data.choice)
    if target_date is None:
        await callback.answer("Неизвестная дата.", show_alert=True)
        return
    await callback.answer()
    await _ask_for_lesson(
        callback.message,
        state,
        target_date,
        config,
        replace_message=True,
    )


@router.message(ReplaceForm.target_date, F.text)
@admin_required
async def replace_date_handler(
    message: Message,
    state: FSMContext,
    config: AppConfig,
    role: str,
) -> None:
    value = (message.text or "").strip()
    today = _local_today(config)
    if value == "На сегодня":
        target_date = today
    elif value == "На завтра":
        target_date = today + timedelta(days=1)
    else:
        target_date = _parse_short_date(value, today)
    if target_date is None:
        await message.answer("Нужна дата строго в формате ДД.ММ, например 05.10.")
        return
    await _select_replacement_date(message, state, target_date, config)


@router.message(ReplaceForm.target_date)
@admin_required
async def replace_date_fallback_handler(message: Message, role: str) -> None:
    await message.answer("Пожалуйста, отправь дату текстом в формате ДД.ММ.")


@router.callback_query(ReplaceForm.lesson_number, F.data.startswith("replace:lesson:"))
@admin_required
async def replace_lesson_handler(
    callback: CallbackQuery,
    state: FSMContext,
    role: str,
) -> None:
    raw_number = (callback.data or "").removeprefix("replace:lesson:")
    data = await state.get_data()
    lesson_count = data.get("lesson_count")
    try:
        lesson_number = int(raw_number)
    except ValueError:
        await callback.answer("Некорректный номер урока.", show_alert=True)
        return
    if not isinstance(lesson_count, int) or lesson_number not in range(1, lesson_count + 1):
        await state.clear()
        await callback.answer("Данные замены потерялись. Начни ещё раз.", show_alert=True)
        return

    await state.update_data(lesson_number=lesson_number)
    await state.set_state(ReplaceForm.override_type)
    keyboard = InlineKeyboardBuilder()
    keyboard.button(text="🚪 Кабинет", callback_data="replace:type:room")
    keyboard.button(text="🔄 Урок целиком", callback_data="replace:type:lesson")
    keyboard.button(
        text="🏠 В меню",
        callback_data=MenuCallback(action="home").pack(),
    )
    add_cancel_button(keyboard)
    keyboard.adjust(2, 1)
    await callback.answer()
    if callback.message is not None:
        await callback.message.edit_text("Что нужно заменить?", reply_markup=keyboard.as_markup())


@router.callback_query(ReplaceForm.override_type, F.data.startswith("replace:type:"))
@admin_required
async def replace_type_handler(
    callback: CallbackQuery,
    state: FSMContext,
    role: str,
    config: AppConfig,
) -> None:
    override_type = (callback.data or "").removeprefix("replace:type:")
    if override_type not in {"room", "lesson"}:
        await callback.answer("Неизвестный тип замены.", show_alert=True)
        return
    await state.update_data(override_type=override_type)
    await state.set_state(ReplaceForm.new_data if override_type == "room" else ReplaceForm.subject)
    prompt = "Введи новый номер кабинета, например 312."
    if override_type == "lesson":
        prompt = "Выбери предмет для замены урока:"
    await callback.answer()
    if callback.message is not None:
        await callback.message.edit_text(prompt, reply_markup=_subject_keyboard(config) if override_type == "lesson" else menu_back_keyboard())


@router.callback_query(ReplaceForm.subject, ReplaceSubjectCallback.filter())
@admin_required
async def replace_subject_handler(callback: CallbackQuery, callback_data: ReplaceSubjectCallback, state: FSMContext, config: AppConfig, role: str) -> None:
    if callback.message is None:
        return
    if callback_data.action == "new":
        await state.set_state(ReplaceForm.new_subject_confirmation)
        await callback.answer()
        await callback.message.edit_text(
            "Введи название нового предмета. Он будет добавлен только после подтверждения.",
            reply_markup=menu_back_keyboard(),
        )
        return
    subjects = all_subjects(config)
    if callback_data.action != "select" or callback_data.subject_index not in range(len(subjects)):
        await callback.answer("Список предметов изменился. Начни замену ещё раз.", show_alert=True)
        return
    await state.update_data(new_subject=subjects[callback_data.subject_index])
    await state.set_state(ReplaceForm.new_data)
    await callback.answer()
    await callback.message.edit_text("Введи кабинет и, при необходимости, преподавателя через запятую: Кабинет, Преподаватель.", reply_markup=menu_back_keyboard())


@router.message(ReplaceForm.new_subject_confirmation, F.text)
@admin_required
async def replace_new_subject_name_handler(message: Message, state: FSMContext, role: str) -> None:
    subject = (message.text or "").strip()
    if not subject:
        await message.answer("Название предмета не должно быть пустым.")
        return
    await state.update_data(pending_new_subject=subject)
    keyboard = InlineKeyboardBuilder()
    keyboard.button(text="✅ Да, добавить", callback_data=ReplaceSubjectCallback(action="confirm_new", subject_index=0).pack())
    keyboard.button(text="✋ Отмена", callback_data=ReplaceSubjectCallback(action="cancel_new", subject_index=0).pack())
    add_cancel_button(keyboard)
    keyboard.adjust(2)
    await message.answer(f"Такого предмета нет в расписании, точно добавить «{subject}» как новый?", reply_markup=keyboard.as_markup())


@router.callback_query(ReplaceForm.new_subject_confirmation, ReplaceSubjectCallback.filter())
@admin_required
async def replace_new_subject_confirmation_handler(callback: CallbackQuery, callback_data: ReplaceSubjectCallback, state: FSMContext, role: str) -> None:
    if callback.message is None:
        return
    if callback_data.action == "cancel_new":
        await state.clear()
        await callback.answer("Добавление отменено.")
        await callback.message.edit_text("Замена отменена.", reply_markup=menu_back_keyboard())
        return
    subject = (await state.get_data()).get("pending_new_subject")
    if callback_data.action != "confirm_new" or not isinstance(subject, str):
        await callback.answer("Не удалось подтвердить предмет.", show_alert=True)
        return
    await state.update_data(new_subject=subject)
    await state.set_state(ReplaceForm.new_data)
    await callback.answer()
    await callback.message.edit_text("Введи кабинет и, при необходимости, преподавателя через запятую: Кабинет, Преподаватель.", reply_markup=menu_back_keyboard())


def _parse_new_lesson(value: str) -> tuple[str, str | None] | None:
    parts = [part.strip() for part in value.split(",")]
    if len(parts) not in {1, 2} or not parts[0]:
        return None
    teacher = parts[1] if len(parts) == 2 and parts[1] else None
    return parts[0], teacher


@router.message(ReplaceForm.new_data, F.text)
@admin_required
async def replace_data_handler(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    config: AppConfig,
    bot: Bot,
    role: str,
) -> None:
    data = await state.get_data()
    override_type = data.get("override_type")
    lesson_number = data.get("lesson_number")
    target_date_value = data.get("target_date")
    if (
        override_type not in {"room", "lesson"}
        or not isinstance(lesson_number, int)
        or not isinstance(target_date_value, str)
    ):
        await state.clear()
        await message.answer("Данные замены потерялись. Начни ещё раз из меню.", reply_markup=menu_back_keyboard())
        return
    if message.from_user is None:
        await message.answer("Не удалось определить пользователя Telegram.")
        return

    text = (message.text or "").strip()
    if not text:
        await message.answer("Данные не должны быть пустыми. Попробуй ещё раз.")
        return
    try:
        target_date = date.fromisoformat(target_date_value)
        base_lesson = _lessons_for_date(target_date, config)[lesson_number - 1]
    except (IndexError, ValueError):
        await state.clear()
        await message.answer("Базовое расписание изменилось. Начни ещё раз из меню.", reply_markup=menu_back_keyboard())
        return

    new_subject: str | None = None
    new_room: str
    new_teacher: str | None = None
    if override_type == "room":
        new_room = text
    else:
        new_lesson = _parse_new_lesson(text)
        if new_lesson is None:
            await message.answer("Введите: Кабинет, Преподаватель. Преподаватель необязателен.")
            return
        new_room, new_teacher = new_lesson
        new_subject = data.get("new_subject")
        if not isinstance(new_subject, str):
            await state.clear()
            await message.answer("Предмет замены потерялся. Начни ещё раз.")
            return

    override = ScheduleOverride(
        target_date=target_date,
        lesson_number=lesson_number,
        override_type=override_type,
        new_subject=new_subject,
        new_room=new_room,
        new_teacher=new_teacher,
        created_by=message.from_user.id,
        created_at=datetime.now(timezone.utc),
    )
    session.add(override)
    await session.commit()
    await state.clear()

    if override_type == "room":
        notification = (
            f"📍 Замена {target_date:%d.%m.%Y}: {base_lesson.subject} "
            f"({lesson_number} пара) — кабинет {new_room} вместо {base_lesson.room}"
        )
    else:
        notification = (
            f"🔄 Замена {target_date:%d.%m.%Y}: {lesson_number} пара — вместо "
            f"{base_lesson.subject} будет {new_subject} (каб. {new_room})"
        )

    if config.bot.group_chat_id == 0:
        await message.answer(
            "Замена сохранена, но не разослана: укажите group_chat_id в config/bot.yaml.",
            reply_markup=menu_back_keyboard(),
        )
        return
    try:
        await bot.send_message(chat_id=config.bot.group_chat_id, text=notification)
    except TelegramAPIError:
        logger.exception("Не удалось отправить уведомление о замене в группу")
        await message.answer("Замена сохранена, но рассылку в группу выполнить не удалось.", reply_markup=menu_back_keyboard())
        return
    await message.answer("✅ Замена сохранена и разослана в группу.", reply_markup=menu_back_keyboard())
