from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.filters.callback_data import CallbackData
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message, User as TelegramUser
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.ui import menu_back_keyboard
from bot.utils.schedule_parser import WEEKDAYS, Lesson, parse_schedule
from bot.utils.time_utils import get_next_school_day
from config_loader import AppConfig
from database.models import ScheduleOverride, User


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
    icon = "🇬🇧" if user.subgroup == "english" else "🇫🇷" if user.subgroup == "french" else "👥"
    name = user.full_name or telegram_user.full_name
    await message.answer(
        f"Студент: {name} | Подгруппа: {icon} {group} (из базы группы)",
        reply_markup=menu_back_keyboard(),
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
) -> list[tuple[int, Lesson | None]]:
    """Return schedule rows, using a None lesson for parallel-group free time."""
    rows: list[tuple[int, Lesson | None]] = []
    free_times: set[str] = set()
    for index, lesson in enumerate(lessons, start=1):
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
        text="📅 Всё равно показать Понедельник",
        callback_data=ScheduleActionCallback(action="monday").pack(),
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
) -> str:
    subject = lesson.subject
    room = lesson.room
    teacher = lesson.teacher
    note = ""

    if override is not None and override.override_type == "room":
        room = override.new_room or "не указан"
        note = f"\n   🔁 Кабинет изменён · был {lesson.room}"
    elif override is not None and override.override_type == "lesson":
        subject = override.new_subject or "предмет не указан"
        room = override.new_room or "не указан"
        teacher = override.new_teacher or lesson.teacher
        note = f"\n   🔁 Вместо: {lesson.subject}"

    return (
        f"{_lesson_number_label(lesson_number)} {lesson.time}  ·  {subject}\n"
        f"   👤 {teacher}   •   🚪 {room}{note}"
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
            "Какое расписание, выдохни, выходные! 🥳 Пора отдыхать.",
            reply_markup=_weekend_keyboard(),
        )
        return
    target_date = target_date or get_next_school_day(config.bot.timezone)

    if telegram_id is None and message.from_user is not None:
        telegram_id = message.from_user.id
    if display_name is None and message.from_user is not None:
        display_name = message.from_user.first_name
    user = await session.get(User, telegram_id) if telegram_id is not None else None
    subgroup = user.subgroup if user is not None else None
    name = display_name or (user.full_name.split()[0] if user and user.full_name else "друг")
    if 5 <= now.hour < 12:
        greeting = f"Доброе утро, {name}! ☀️ Вот расписание на сегодня:"
    elif 12 <= now.hour < 18:
        greeting = f"Добрый день, {name}! 📚 Вот расписание:"
    else:
        greeting = f"Добрый вечер, {name}! 🌙 Вот расписание на завтра:"

    try:
        schedule = parse_schedule(config.schedule)
    except ValueError:
        await message.answer(
            "Расписание ещё не настроено. Обратитесь к администратору.",
            reply_markup=menu_back_keyboard(),
        )
        return

    parity = schedule.get_parity(target_date)
    weekday = WEEKDAYS[target_date.weekday()] if target_date.weekday() < 5 else (
        "saturday" if target_date.weekday() == 5 else "sunday"
    )
    lessons = schedule.lessons_for(weekday, parity) if weekday in WEEKDAYS else []

    result = await session.scalars(
        select(ScheduleOverride)
        .where(ScheduleOverride.target_date == target_date)
        .order_by(ScheduleOverride.lesson_number, ScheduleOverride.id)
    )
    overrides = {override.lesson_number: override for override in result}

    lines = [
        greeting,
        "🗓 РАСПИСАНИЕ",
        f"{WEEKDAY_NAMES[weekday]}, {target_date:%d.%m} · {parity} неделя",
        "━━━━━━━━━━━━━━━━",
    ]
    last_lesson_number = max(len(lessons), max(overrides, default=0))
    schedule_rows = _personalized_lessons(lessons, subgroup)
    rendered_count = 0
    for lesson_number, lesson in schedule_rows:
        rendered_count += 1
        if lesson is None:
            lines.append(f"• [{lessons[lesson_number - 1].time}] Свободное окно ☕ (у параллельной подгруппы пара)")
        else:
            lines.append(_lesson_card(rendered_count, lesson, overrides.get(lesson_number)))
    for lesson_number in range(len(lessons) + 1, last_lesson_number + 1):
        rendered_count += 1
        placeholder = Lesson(time="—", subject="—", room="—", teacher="—")
        lines.append(_lesson_card(rendered_count, placeholder, overrides.get(lesson_number)))

    if rendered_count == 0:
        lines.append("На этот день уроков нет.")

    await message.answer("\n\n".join(lines), reply_markup=menu_back_keyboard())


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
