from __future__ import annotations

import html
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram import Router
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject
from aiogram.filters.callback_data import CallbackData
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message, User as TelegramUser
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.keyboards.inline import InlineKeyboardBuilder
from bot.ui import menu_back_keyboard
from bot.utils.emojis import get_btn_emoji, get_button_label, get_emoji
from bot.utils.schedule_parser import (
    WEEKDAYS,
    Lesson,
    lesson_numbers_for_day,
    parse_schedule,
)
from bot.utils.time_utils import (
    calculate_lesson_times,
    get_configured_week_parity,
)
from bot.utils.homework_dates import homework_target_date
from config_loader import AppConfig
from database.models import ScheduleOverride, ScheduleTimeOverride, User


router = Router(name="schedule")
WEEKDAY_NAMES = {
    "monday": "Понедельник",
    "tuesday": "Вторник",
    "wednesday": "Среда",
    "thursday": "Четверг",
    "friday": "Пятница",
    "saturday": "Суббота",
    "sunday": "Воскресенье",
}
SUBGROUP_NAMES = {"english": "Английская", "french": "Французская"}


class ScheduleActionCallback(CallbackData, prefix="schedule"):
    action: str


async def _get_or_create_user(
    session: AsyncSession,
    telegram_user: TelegramUser,
) -> User:
    user = await session.get(User, telegram_user.id)
    if user is None:
        user = User(
            telegram_id=telegram_user.id,
            full_name=telegram_user.full_name.strip() or None,
            username=telegram_user.username,
        )
        session.add(user)
        await session.flush()
    return user


async def show_my_group(
    message: Message,
    session: AsyncSession,
    telegram_user: TelegramUser | None = None,
) -> None:
    telegram_user = telegram_user or message.from_user
    if telegram_user is None:
        return
    user = await _get_or_create_user(session, telegram_user)
    await session.commit()
    group = SUBGROUP_NAMES.get(user.subgroup, "не указана")
    icon_key = "english" if user.subgroup == "english" else "french" if user.subgroup == "french" else "users"
    name = user.full_name or telegram_user.full_name
    await message.answer(
        f"Студент: {html.escape(name)} | Подгруппа: "
        f"{get_emoji(icon_key)} {html.escape(group)} (из базы группы)",
        reply_markup=menu_back_keyboard(),
        parse_mode=ParseMode.HTML,
    )


def _lesson_subgroup(lesson: Lesson) -> str | None:
    if lesson.subgroup is not None:
        return lesson.subgroup
    subject = lesson.subject.casefold()
    if any(word in subject for word in ("англий", "englez", "english", "eng.", "engleza")):
        return "english"
    if any(word in subject for word in ("француз", "francez", "french", "français", "franc.")):
        return "french"
    return None


def _personalized_lessons(
    lessons: list[Lesson],
    subgroup: str | None,
    lesson_numbers: list[int] | None = None,
) -> list[tuple[int, Lesson | None]]:
    """Return schedule rows, using a None lesson for parallel-group free time."""
    rows: list[tuple[int, Lesson | None]] = []
    free_times: set[str] = set()
    numbers = lesson_numbers or list(range(1, len(lessons) + 1))
    for index, lesson in zip(numbers, lessons):
        lesson_subgroup = _lesson_subgroup(lesson)
        if subgroup is None or lesson_subgroup is None or lesson_subgroup == subgroup:
            rows.append((index, lesson))
            continue

        has_own_lesson = any(
            other.time == lesson.time
            and (_lesson_subgroup(other) is None or _lesson_subgroup(other) == subgroup)
            for other in lessons
        )
        if not has_own_lesson and lesson.time not in free_times:
            rows.append((index, None))
            free_times.add(lesson.time)
    return rows


def _weekend_easter_egg(now: datetime) -> bool:
    return now.weekday() in {5, 6} or (now.weekday() == 4 and now.hour >= 16)


def _weekend_keyboard() -> InlineKeyboardMarkup:
    keyboard = InlineKeyboardBuilder()
    keyboard.button(
        text=get_button_label(
            "Всё равно показать Понедельник",
            icon_key="schedule",
        ),
        callback_data=ScheduleActionCallback(action="monday").pack(),
        style="primary",
    )
    return keyboard.as_markup()


def _parse_target_date(value: str | None) -> date | None:
    if not value:
        return None
    for date_format in ("%Y-%m-%d", "%d.%m.%Y"):
        try:
            return datetime.strptime(value.strip(), date_format).date()
        except ValueError:
            continue
    raise ValueError("Укажите дату в формате ГГГГ-ММ-ДД или ДД.ММ.ГГГГ.")


def _lesson_number_label(number: int) -> str:
    circled_numbers = "①②③④⑤⑥⑦⑧⑨⑩"
    return circled_numbers[number - 1] if number <= len(circled_numbers) else f"#{number}"


def _lesson_card(
    lesson_number: int,
    lesson: Lesson,
    override: ScheduleOverride | None,
    *,
    display_time: str | None = None,
    shortened: bool = False,
) -> str:
    subject = lesson.subject
    room = lesson.room
    teacher = lesson.teacher
    note = ""
    lesson_label = (
        f"{lesson_number}."
        if display_time is not None
        else _lesson_number_label(lesson_number)
    )
    separator = " | " if display_time is not None else "  ·  "

    if override is not None and override.is_cancelled:
        cancelled_subject = lesson.subject if lesson.subject != "—" else (override.new_subject or "Пара")
        return (
            f"{lesson_label} {html.escape(display_time or lesson.time)}{separator}"
            f"<s>{html.escape(cancelled_subject)}</s>\n"
            f"   {get_emoji('cancel_lesson')} Урок отменён"
        )

    if override is not None and override.override_type == "room":
        room = override.new_room or "не указан"
        note = (
            f"\n   {get_emoji('replacement')} Кабинет изменён · был "
            f"{html.escape(lesson.room)}"
        )
    elif override is not None and override.override_type == "lesson":
        subject = override.new_subject or "предмет не указан"
        room = override.new_room or "не указан"
        teacher = override.new_teacher or lesson.teacher
        note = (
            f"\n   {get_emoji('replacement')} Вместо: "
            f"{html.escape(lesson.subject)}"
        )

    return (
        f"{lesson_label} {html.escape(display_time or lesson.time)}"
        f"{separator}{html.escape(subject)}"
        f"{' (сокращённый)' if shortened else ''}\n"
        f"   {get_emoji('user')} {html.escape(teacher)}   •   "
        f"{get_emoji('room')} {html.escape(room)}{note}"
    )


async def show_next_schedule(
    message: Message,
    config: AppConfig,
    session: AsyncSession,
    *,
    target_date: date | None = None,
    telegram_id: int | None = None,
    display_name: str | None = None,
) -> None:
    """Show a personalized schedule from a command or menu."""
    now = datetime.now(ZoneInfo(config.bot.timezone))
    if target_date is None and _weekend_easter_egg(now):
        await message.answer(
            f"Какое расписание, выдохни, выходные! "
            f"{get_emoji('celebration')} Пора отдыхать.",
            reply_markup=_weekend_keyboard(),
            parse_mode=ParseMode.HTML,
        )
        return
    if target_date is None:
        target_date = await homework_target_date(session, config, now)
        if target_date is None:
            await message.answer(
                "Не удалось найти ближайший учебный день в расписании.",
                reply_markup=menu_back_keyboard(),
            )
            return

    if telegram_id is None and message.from_user is not None:
        telegram_id = message.from_user.id
    if display_name is None and message.from_user is not None:
        display_name = message.from_user.first_name
    user = await session.get(User, telegram_id) if telegram_id is not None else None
    subgroup = user.subgroup if user is not None else None
    name = display_name or (user.full_name.split()[0] if user and user.full_name else "друг")
    if 5 <= now.hour < 12:
        greeting = (
            f"Доброе утро, {html.escape(name)}! {get_emoji('morning')} "
            "Вот расписание на сегодня:"
        )
    elif 12 <= now.hour < 18:
        greeting = (
            f"Добрый день, {html.escape(name)}! {get_emoji('homework')} "
            "Вот расписание:"
        )
    else:
        greeting = (
            f"Добрый вечер, {html.escape(name)}! {get_emoji('evening')} "
            "Вот расписание на завтра:"
        )

    try:
        schedule = parse_schedule(config.schedule)
    except ValueError:
        await message.answer(
            "Расписание ещё не настроено. Обратитесь к администратору.",
            reply_markup=menu_back_keyboard(),
        )
        return

    parity = await get_configured_week_parity(session, target_date, config)
    weekday = WEEKDAYS[target_date.weekday()] if target_date.weekday() < 5 else (
        "saturday" if target_date.weekday() == 5 else "sunday"
    )
    lessons = schedule.lessons_for(weekday, parity) if weekday in WEEKDAYS else []
    lesson_numbers = lesson_numbers_for_day(lessons, schedule)

    result = await session.scalars(
        select(ScheduleOverride)
        .where(ScheduleOverride.target_date == target_date)
        .order_by(ScheduleOverride.lesson_number, ScheduleOverride.id)
    )
    overrides = {override.lesson_number: override for override in result}
    time_overrides = list(
        await session.scalars(
            select(ScheduleTimeOverride)
            .where(ScheduleTimeOverride.target_date == target_date)
            .order_by(ScheduleTimeOverride.id)
        )
    )
    lesson_clocks = (
        calculate_lesson_times(
            (lesson.time for lesson in lessons),
            config.schedule.lesson_duration_minutes,
            time_overrides,
            lesson_numbers=lesson_numbers,
        )
        if time_overrides
        else []
    )
    clocks_by_slot = dict(zip(lesson_numbers, lesson_clocks))

    lines = [
        greeting,
        f"{get_emoji('date')} РАСПИСАНИЕ",
        f"{WEEKDAY_NAMES[weekday]}, {target_date:%d.%m} · {parity} неделя",
    ]
    if time_overrides:
        lines.append(f"{get_emoji('warning')} Внимание! В этот день уроки сокращены!")
    lines.append("━━━━━━━━━━━━━━━━")
    base_lessons_by_slot: dict[int, Lesson] = {}
    for lesson_number, lesson in zip(lesson_numbers, lessons):
        base_lessons_by_slot.setdefault(lesson_number, lesson)
    schedule_rows = dict(
        _personalized_lessons(lessons, subgroup, lesson_numbers)
    )
    placeholder = Lesson(time="—", subject="—", room="—", teacher="—")
    for lesson_number, override in overrides.items():
        if override.is_cancelled:
            schedule_rows[lesson_number] = base_lessons_by_slot.get(
                lesson_number,
                placeholder,
            )
        elif lesson_number not in base_lessons_by_slot:
            schedule_rows[lesson_number] = placeholder
    for lesson_number, lesson in sorted(schedule_rows.items()):
        clock = (
            clocks_by_slot.get(lesson_number)
        )
        display_time = (
            f"{clock.start}–{clock.end}"
            if clock is not None
            else lesson.time if lesson is not None else None
        )
        if lesson is None:
            base_lesson = base_lessons_by_slot.get(lesson_number)
            free_time = (
                display_time
                or (base_lesson.time if base_lesson is not None else "—")
            )
            lines.append(
                f"• [{free_time}] Свободное окно {get_emoji('free_slot')} "
                "(у параллельной подгруппы пара)"
            )
        else:
            lines.append(
                _lesson_card(
                    lesson_number,
                    lesson,
                    overrides.get(lesson_number),
                    display_time=display_time,
                    shortened=clock.shortened if clock is not None else False,
                )
            )

    if not schedule_rows:
        lines.append("На этот день уроков нет.")

    await message.answer(
        "\n\n".join(lines),
        reply_markup=menu_back_keyboard(),
        parse_mode="HTML",
    )


@router.message(Command("schedule", "расписание"))
async def schedule_handler(
    message: Message,
    config: AppConfig,
    session: AsyncSession,
    command: CommandObject,
) -> None:
    try:
        target_date = _parse_target_date(command.args)
    except ValueError as exc:
        await message.answer(str(exc), reply_markup=menu_back_keyboard())
        return
    await show_next_schedule(message, config, session, target_date=target_date)


@router.message(Command("my_group"))
async def my_group_handler(message: Message, session: AsyncSession) -> None:
    await show_my_group(message, session)


@router.callback_query(ScheduleActionCallback.filter())
async def weekend_schedule_callback(
    callback: CallbackQuery,
    config: AppConfig,
    session: AsyncSession,
) -> None:
    if callback.message is None:
        await callback.answer()
        return
    await callback.answer()
    now = datetime.now(ZoneInfo(config.bot.timezone))
    monday = now.date() + timedelta(days=7 - now.weekday())
    await show_next_schedule(
        callback.message,
        config,
        session,
        target_date=monday,
        telegram_id=callback.from_user.id,
        display_name=callback.from_user.first_name,
    )
