from __future__ import annotations

import html
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.filters.callback_data import CallbackData
from aiogram.filters.command import CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message, User as TelegramUser
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from bot.keyboards.inline import InlineKeyboardBuilder
from bot.ui import MenuCallback, menu_back_keyboard
from bot.utils.admin_audit import log_admin_action
from bot.utils.emojis import get_btn_emoji, get_button_label, get_emoji
from bot.utils.schedule_parser import (
    WEEKDAYS,
    Lesson,
    lesson_numbers_for_day,
    parse_schedule,
)
from bot.utils.time_utils import get_configured_week_parity, parse_day_month_date
from config_loader import AppConfig
from database.models import Absence, User


router = Router(name="absent")
ADMIN_ROLES = {"owner", "starosta", "moderator"}


class AbsentForm(StatesGroup):
    date_input = State()
    lesson_selection = State()


class DateChoiceCallback(CallbackData, prefix="abs"):
    action: str
    choice: str


class LessonChoiceCallback(CallbackData, prefix="abs"):
    action: str
    lesson_number: int


class SimpleActionCallback(CallbackData, prefix="abs"):
    action: str


class RecordActionCallback(CallbackData, prefix="abs"):
    action: str
    absence_id: int


class UserActionCallback(CallbackData, prefix="abs_user"):
    action: str
    telegram_id: int


def _local_today(config: AppConfig) -> date:
    return datetime.now(ZoneInfo(config.bot.timezone)).date()


def _admin_ids(config: AppConfig) -> set[int]:
    roles = config.roles
    return set(roles.owner) | set(roles.starosta) | set(roles.moderators)


def _profile_values(telegram_user: TelegramUser | None) -> tuple[str | None, str | None]:
    if telegram_user is None:
        return None, None
    full_name = telegram_user.full_name.strip() or None
    username = telegram_user.username.strip() if telegram_user.username else None
    return full_name, username


SUBGROUP_NAMES = {"english": "Английская", "french": "Французская"}


def _user_label(user: User) -> str:
    return user.full_name or "Пользователь"


def _student_label(user: User) -> str:
    subgroup = SUBGROUP_NAMES.get(user.subgroup)
    return f"{_user_label(user)} ({subgroup} подгруппа)" if subgroup else _user_label(user)


async def _get_or_create_user(
    session: AsyncSession,
    telegram_id: int,
    telegram_user: TelegramUser | None = None,
) -> User:
    user = await session.get(User, telegram_id)
    full_name, username = _profile_values(telegram_user)
    if user is None:
        user = User(
            telegram_id=telegram_id,
            full_name=full_name,
            username=username,
        )
        session.add(user)
        await session.commit()
    elif telegram_user is not None and user.username != username:
        user.username = username
        await session.commit()
    return user


def _date_keyboard() -> InlineKeyboardMarkup:
    keyboard = InlineKeyboardBuilder()
    keyboard.button(
        text=get_button_label("Сегодня", icon_key="today"),
        callback_data=DateChoiceCallback(action="date", choice="today").pack(),
        style="primary",
    )
    keyboard.button(
        text=get_button_label("Завтра", icon_key="tomorrow"),
        callback_data=DateChoiceCallback(action="date", choice="tomorrow").pack(),
        style="primary",
    )
    keyboard.button(
        text=get_button_label("Другая дата", icon_key="date"),
        callback_data=DateChoiceCallback(action="date", choice="other").pack(),
        style="primary",
    )
    keyboard.button(
        text=get_button_label("В меню", icon_key="back_arrow"),
        callback_data=MenuCallback(action="home").pack(),
    )
    keyboard.adjust(2, 1, 1)
    return keyboard.as_markup()


def _lesson_keyboard(
    lessons: list[Lesson],
    selected_lessons: set[int],
    lesson_numbers: list[int] | None = None,
) -> InlineKeyboardMarkup:
    keyboard = InlineKeyboardBuilder()
    keyboard.button(
        text=get_button_label("Весь день", icon_key="all_day"),
        callback_data=SimpleActionCallback(action="all").pack(),
        style="primary",
    )
    numbers = lesson_numbers or list(range(1, len(lessons) + 1))
    subjects_by_slot: dict[int, list[str]] = {}
    for number, lesson in zip(numbers, lessons):
        subjects = subjects_by_slot.setdefault(number, [])
        if lesson.subject not in subjects:
            subjects.append(lesson.subject)
    for number, subjects in subjects_by_slot.items():
        is_selected = number in selected_lessons
        mark = f"{get_btn_emoji('check')} " if is_selected else "◾ "
        button_options = (
            {"style": "success", "icon_key": "check"} if is_selected else {}
        )
        keyboard.button(
            text=get_button_label(
                f"{mark}{number}. {' / '.join(subjects)}"
            ),
            callback_data=LessonChoiceCallback(
                action="lesson", lesson_number=number
            ).pack(),
            **button_options,
        )
    keyboard.button(
        text=get_button_label("Готово", icon_key="success"),
        callback_data=SimpleActionCallback(action="done").pack(),
        style="success",
    )
    keyboard.button(
        text=get_button_label("В меню", icon_key="back_arrow"),
        callback_data=MenuCallback(action="home").pack(),
    )
    keyboard.adjust(1)
    return keyboard.as_markup()


def _lessons_label(lessons: object) -> str:
    if lessons == "all" or lessons == ["all"]:
        return "весь день"
    if not isinstance(lessons, list):
        return "уроки не указаны"
    numbers = sorted(
        int(item) for item in lessons if isinstance(item, int) or str(item).isdigit()
    )
    return ", ".join(str(number) for number in numbers) + " пара" if numbers else "уроки не указаны"


def _lessons_overlap(existing: object, requested: list[int] | list[str]) -> bool:
    if existing == "all" or existing == ["all"] or requested == ["all"]:
        return True
    if not isinstance(existing, list):
        return False
    existing_numbers = {
        int(item) for item in existing if isinstance(item, int) or str(item).isdigit()
    }
    return bool(existing_numbers.intersection(int(item) for item in requested))


async def _schedule_for(
    target_date: date,
    config: AppConfig,
    session: AsyncSession,
) -> list[Lesson]:
    weekday = target_date.weekday()
    if weekday >= len(WEEKDAYS):
        return []
    schedule = parse_schedule(config.schedule)
    parity = await get_configured_week_parity(session, target_date, config)
    return schedule.lessons_for(WEEKDAYS[weekday], parity)


async def _show_lesson_selection(
    message: Message,
    state: FSMContext,
    target_date: date,
    config: AppConfig,
    session: AsyncSession,
    replace_message: bool = False,
) -> None:
    try:
        lessons = await _schedule_for(target_date, config, session)
    except ValueError:
        await state.clear()
        await message.answer("Расписание ещё не настроено. Обратитесь к администратору.")
        return

    if not lessons:
        await state.clear()
        await message.answer(f"На {target_date:%d.%m.%Y} уроков нет, заявку создать нельзя.")
        return

    lesson_numbers = lesson_numbers_for_day(lessons, config.schedule)
    await state.update_data(
        target_date=target_date.isoformat(),
        lesson_numbers=lesson_numbers,
        selected_lessons=[],
    )
    await state.set_state(AbsentForm.lesson_selection)
    prompt = f"Выберите уроки на {target_date:%d.%m.%Y} (можно несколько):"
    if replace_message:
        await message.edit_text(
            prompt,
            reply_markup=_lesson_keyboard(lessons, set(), lesson_numbers),
        )
    else:
        await message.answer(
            prompt,
            reply_markup=_lesson_keyboard(lessons, set(), lesson_numbers),
        )


async def _save_absence(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    config: AppConfig,
    lessons: list[int] | list[str],
) -> None:
    if callback.from_user is None:
        await callback.answer("Не удалось определить пользователя.", show_alert=True)
        return

    form_data = await state.get_data()
    target_date_value = form_data.get("target_date")
    if not isinstance(target_date_value, str):
        await state.clear()
        await callback.answer("Данные заявки потеряны. Начните заново.", show_alert=True)
        return
    try:
        target_date = date.fromisoformat(target_date_value)
    except ValueError:
        await state.clear()
        await callback.answer("Некорректная дата заявки. Начните заново.", show_alert=True)
        return

    today = _local_today(config)
    if not today <= target_date <= today + timedelta(days=14):
        await state.clear()
        await callback.answer(
            "Дата заявки должна быть не далее чем через 14 дней.",
            show_alert=True,
        )
        return

    user = await _get_or_create_user(
        session,
        callback.from_user.id,
        callback.from_user,
    )
    if user.false_absences_count >= config.sanctions.ban_threshold:
        await state.clear()
        await callback.answer("Доступ к отметкам отсутствия ограничен.", show_alert=True)
        return

    active_absences = await session.scalars(
        select(Absence).where(
            Absence.telegram_id == callback.from_user.id,
            Absence.target_date == target_date,
            Absence.status == "active",
        )
    )
    if any(_lessons_overlap(absence.lessons, lessons) for absence in active_absences):
        await callback.answer(
            "Заявка на эти уроки в этот день уже существует.", show_alert=True
        )
        return

    session.add(
        Absence(
            telegram_id=callback.from_user.id,
            target_date=target_date,
            lessons=lessons,
            status="active",
            flagged_by=None,
            created_at=datetime.now(timezone.utc),
        )
    )
    await session.commit()
    await state.clear()

    if target_date == today:
        notification = (
            f"{get_emoji('warning')} Заявка на отсутствие (Сегодня)\n"
            f"Студент: {html.escape(_student_label(user))}\n"
            f"Уроки: {_lessons_label(lessons)}"
        )
        for admin_id in _admin_ids(config):
            try:
                await callback.bot.send_message(
                    admin_id,
                    notification,
                    parse_mode=ParseMode.HTML,
                )
            except TelegramAPIError:
                continue
    await callback.answer()
    if callback.message is not None:
        await callback.message.edit_text(
            f"{get_emoji('success')} Заявка на отсутствие {target_date:%d.%m.%Y} сохранена: "
            f"{_lessons_label(lessons)}. Всё записано, можешь выдохнуть.",
            reply_markup=menu_back_keyboard(),
            parse_mode=ParseMode.HTML,
        )


async def start_absence_form(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    config: AppConfig,
    telegram_id: int | None = None,
    telegram_user: TelegramUser | None = None,
) -> None:
    profile = telegram_user or message.from_user
    actor_id = telegram_id if telegram_id is not None else getattr(profile, "id", None)
    if actor_id is None:
        await message.answer("Не удалось определить пользователя Telegram.")
        return

    await state.clear()
    user = await _get_or_create_user(session, actor_id, profile)
    if user.false_absences_count >= config.sanctions.ban_threshold:
        await message.answer(
            "У тебя ограничен доступ к отметкам отсутствия. "
            "Обратись к администратору для снятия ограничения.",
            reply_markup=menu_back_keyboard(),
        )
        return

    await state.set_state(AbsentForm.date_input)
    await message.answer(
        f"{get_emoji('absence')} На какую дату тебя не будет? "
        "Выбирай, я всё запомню.",
        reply_markup=_date_keyboard(),
        parse_mode=ParseMode.HTML,
    )


@router.message(Command("absent"))
async def absent_start_handler(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    config: AppConfig,
) -> None:
    await start_absence_form(message, state, session, config)


@router.callback_query(AbsentForm.date_input, DateChoiceCallback.filter(F.action == "date"))
async def absent_date_choice_handler(
    callback: CallbackQuery,
    callback_data: DateChoiceCallback,
    state: FSMContext,
    config: AppConfig,
    session: AsyncSession,
) -> None:
    today = _local_today(config)
    if callback_data.choice == "other":
        await callback.answer()
        if callback.message is not None:
            await callback.message.edit_text(
                "Введите дату в формате ДД.ММ (не более чем на 14 дней вперёд).",
                reply_markup=menu_back_keyboard(),
            )
        return

    choices = {"today": today, "tomorrow": today + timedelta(days=1)}
    target_date = choices.get(callback_data.choice)
    if target_date is None:
        await callback.answer("Неизвестный вариант даты.", show_alert=True)
        return
    await callback.answer()
    if callback.message is not None:
        await _show_lesson_selection(
            callback.message,
            state,
            target_date,
            config,
            session,
            replace_message=True,
        )


@router.message(AbsentForm.date_input, F.text)
async def absent_custom_date_handler(
    message: Message,
    state: FSMContext,
    config: AppConfig,
    session: AsyncSession,
) -> None:
    value = (message.text or "").strip()
    today = _local_today(config)
    target_date = parse_day_month_date(value, today)
    if target_date is None:
        await message.answer("Введите дату строго в формате ДД.ММ, например 05.10.")
        return

    if not today <= target_date <= today + timedelta(days=14):
        await message.answer("Можно выбрать дату от сегодня и не более чем на 14 дней вперёд.")
        return
    await _show_lesson_selection(message, state, target_date, config, session)


@router.message(AbsentForm.date_input)
async def absent_date_fallback_handler(message: Message) -> None:
    await message.answer("Пожалуйста, отправь дату текстом в формате ДД.ММ.")


@router.callback_query(
    AbsentForm.lesson_selection,
    LessonChoiceCallback.filter(F.action == "lesson"),
)
async def absent_lesson_choice_handler(
    callback: CallbackQuery,
    callback_data: LessonChoiceCallback,
    state: FSMContext,
    config: AppConfig,
    session: AsyncSession,
) -> None:
    form_data = await state.get_data()
    lesson_numbers = form_data.get("lesson_numbers")
    selected_raw = form_data.get("selected_lessons", [])
    if (
        not isinstance(lesson_numbers, list)
        or any(not isinstance(item, int) for item in lesson_numbers)
        or not isinstance(selected_raw, list)
    ):
        await state.clear()
        await callback.answer("Данные заявки потеряны. Начните заново.", show_alert=True)
        return
    lesson_number = callback_data.lesson_number
    if lesson_number not in lesson_numbers:
        await callback.answer("Такого урока нет.", show_alert=True)
        return

    selected = {item for item in selected_raw if isinstance(item, int)}
    if lesson_number in selected:
        selected.remove(lesson_number)
    else:
        selected.add(lesson_number)
    await state.update_data(selected_lessons=sorted(selected))

    if callback.message is not None:
        target_date_value = form_data.get("target_date")
        if not isinstance(target_date_value, str):
            await state.clear()
            await callback.answer("Данные заявки потеряны. Начните заново.", show_alert=True)
            return
        try:
            target_date = date.fromisoformat(target_date_value)
            lessons = await _schedule_for(target_date, config, session)
        except ValueError:
            await state.clear()
            await callback.answer("Расписание недоступно. Начните заново.", show_alert=True)
            return
        await callback.message.edit_reply_markup(
            reply_markup=_lesson_keyboard(
                lessons,
                selected,
                lesson_numbers_for_day(lessons, config.schedule),
            )
        )
    await callback.answer()


@router.callback_query(
    AbsentForm.lesson_selection,
    SimpleActionCallback.filter(F.action == "all"),
)
async def absent_all_handler(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    config: AppConfig,
) -> None:
    await _save_absence(callback, state, session, config, ["all"])


@router.callback_query(
    AbsentForm.lesson_selection,
    SimpleActionCallback.filter(F.action == "done"),
)
async def absent_done_handler(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    config: AppConfig,
) -> None:
    selected_lessons = (await state.get_data()).get("selected_lessons", [])
    if not isinstance(selected_lessons, list) or not selected_lessons:
        await callback.answer("Выберите хотя бы один урок или «Весь день».", show_alert=True)
        return
    if any(not isinstance(item, int) for item in selected_lessons):
        await state.clear()
        await callback.answer("Данные заявки потеряны. Начните заново.", show_alert=True)
        return
    await _save_absence(callback, state, session, config, sorted(set(selected_lessons)))


async def show_my_absences(
    message: Message,
    session: AsyncSession,
    config: AppConfig,
    telegram_id: int | None = None,
    telegram_user: TelegramUser | None = None,
) -> None:
    profile = telegram_user or message.from_user
    actor_id = telegram_id if telegram_id is not None else getattr(profile, "id", None)
    if actor_id is None:
        await message.answer("Не удалось определить пользователя Telegram.")
        return
    absences = list(
        await session.scalars(
            select(Absence)
            .where(
                Absence.telegram_id == actor_id,
                Absence.status == "active",
                Absence.target_date >= _local_today(config),
            )
            .order_by(Absence.target_date, Absence.id)
        )
    )
    if not absences:
        await message.answer(
            "Активных заявок нет. Посещаемость сегодня в ударе.",
            reply_markup=menu_back_keyboard(),
        )
        return

    today = _local_today(config)
    for absence in absences:
        keyboard = None
        suffix = ""
        if absence.target_date >= today:
            builder = InlineKeyboardBuilder()
            builder.button(
                text=get_button_label("Отменить", icon_key="cancel"),
                callback_data=RecordActionCallback(
                    action="cancel", absence_id=absence.id
                ).pack(),
                style="danger",
            )
            builder.button(
                text=get_button_label("В меню", icon_key="back_arrow"),
                callback_data=MenuCallback(action="home").pack(),
            )
            builder.adjust(2)
            keyboard = builder.as_markup()
        else:
            suffix = "\nОтмена недоступна: день уже прошёл."
        await message.answer(
            f"{absence.target_date:%d.%m.%Y}: {_lessons_label(absence.lessons)}.{suffix}",
            reply_markup=keyboard,
        )


@router.message(Command("my_absences"))
async def my_absences_handler(
    message: Message,
    session: AsyncSession,
    config: AppConfig,
) -> None:
    await show_my_absences(message, session, config)


@router.callback_query(RecordActionCallback.filter(F.action == "cancel"))
async def cancel_absence_handler(
    callback: CallbackQuery,
    callback_data: RecordActionCallback,
    session: AsyncSession,
    config: AppConfig,
) -> None:
    absence = await session.get(Absence, callback_data.absence_id)
    if (
        absence is None
        or callback.from_user is None
        or absence.telegram_id != callback.from_user.id
        or absence.status != "active"
    ):
        await callback.answer("Заявка не найдена или недоступна.", show_alert=True)
        return
    if absence.target_date < _local_today(config):
        await callback.answer("Нельзя отменить заявку за прошедший день.", show_alert=True)
        return
    await session.delete(absence)
    await session.commit()
    await callback.answer("Заявка отменена.")
    if callback.message is not None:
        await callback.message.edit_text(
            "Заявка на отсутствие отменена. Будто её и не было.",
            reply_markup=menu_back_keyboard(),
        )


async def show_class_absences(
    message: Message,
    role: str,
    session: AsyncSession,
    config: AppConfig,
) -> None:
    if role not in ADMIN_ROLES:
        await message.answer("Эта команда доступна только администраторам.")
        return
    absences = list(
        await session.scalars(
            select(Absence)
            .where(
                Absence.status == "active",
                Absence.target_date >= _local_today(config),
            )
            .order_by(Absence.target_date, Absence.telegram_id, Absence.id)
        )
    )
    if not absences:
        await message.answer(
            "Активных заявок на отсутствие нет. Подозрительно образцовый класс.",
            reply_markup=menu_back_keyboard(),
        )
        return

    users = {
        user.telegram_id: user
        for user in await session.scalars(
            select(User).where(
                User.telegram_id.in_({absence.telegram_id for absence in absences})
            )
        )
    }
    lines: list[str] = []
    current_date: date | None = None
    keyboard = InlineKeyboardBuilder()
    for absence in absences:
        if absence.target_date != current_date:
            current_date = absence.target_date
            if lines:
                lines.append("")
            lines.append(
                f"{get_emoji('cases')} Отсутствия на {current_date:%d.%m.%Y}:"
            )
        user = users.get(absence.telegram_id)
        label = _user_label(user) if user is not None else "Студент не найден"
        lines.append(
            f"• {html.escape(label)} — {_lessons_label(absence.lessons)}"
        )
        keyboard.button(
            text=get_button_label(
                f"Отметить как ложную #{absence.id}",
                icon_key="warning",
            ),
            callback_data=RecordActionCallback(action="flag", absence_id=absence.id).pack(),
            style="danger",
        )
    keyboard.button(
        text=get_button_label("В меню", icon_key="back_arrow"),
        callback_data=MenuCallback(action="home").pack(),
    )
    keyboard.adjust(1)
    await message.answer(
        "\n".join(lines),
        reply_markup=keyboard.as_markup(),
        parse_mode=ParseMode.HTML,
    )


@router.message(Command("absences"))
async def class_absences_handler(
    message: Message,
    role: str,
    session: AsyncSession,
    config: AppConfig,
) -> None:
    await show_class_absences(message, role, session, config)


@router.callback_query(RecordActionCallback.filter(F.action == "flag"))
async def flag_absence_handler(
    callback: CallbackQuery,
    callback_data: RecordActionCallback,
    role: str,
    session: AsyncSession,
    config: AppConfig,
) -> None:
    if role not in ADMIN_ROLES:
        await callback.answer("Недостаточно прав.", show_alert=True)
        return
    absence = await session.get(Absence, callback_data.absence_id)
    if absence is None or absence.status != "active":
        await callback.answer("Заявка уже обработана или не найдена.", show_alert=True)
        return

    user = await _get_or_create_user(session, absence.telegram_id)
    absence.status = "false"
    absence.flagged_by = callback.from_user.id if callback.from_user is not None else None
    user.false_absences_count += 1

    ban_text = ""
    if user.false_absences_count >= config.sanctions.ban_threshold:
        user.banned_until = datetime.now(timezone.utc) + timedelta(
            days=config.sanctions.ban_days
        )
        ban_text = " Пользователь заблокирован до ручного снятия ограничения."
    await log_admin_action(
        session,
        callback.from_user.id,
        "Выдал бан" if ban_text else "Отметил ложную заявку",
        f"ложная заявка #{absence.id}, пользователь={user.telegram_id}, "
        f"ложных заявок={user.false_absences_count}",
    )
    await session.commit()
    await callback.answer("Заявка отмечена как ложная.")
    if callback.message is not None:
        await callback.message.edit_text(
            f"Заявка #{absence.id} пользователя {_user_label(user)} отмечена как ложная. "
            f"Ложных заявок: {user.false_absences_count}.{ban_text}",
            reply_markup=menu_back_keyboard(),
        )


@router.message(Command("unban_absent"))
async def unban_absent_handler(
    message: Message,
    command: CommandObject,
    role: str,
    session: AsyncSession,
) -> None:
    if role not in ADMIN_ROLES:
        await message.answer("Эта команда доступна только администраторам.")
        return
    raw_telegram_id = (command.args or "").strip()
    try:
        telegram_id = int(raw_telegram_id)
    except ValueError:
        await message.answer("Использование: /unban_absent <telegram_id>.")
        return
    if telegram_id <= 0:
        await message.answer("Telegram ID должен быть положительным числом.")
        return
    user = await session.get(User, telegram_id)
    if user is None:
        await message.answer("Пользователь с таким Telegram ID не найден.")
        return
    user.false_absences_count = 0
    user.banned_until = None
    if message.from_user is not None:
        await log_admin_action(
            session,
            message.from_user.id,
            "Снял санкцию",
            f"telegram_id={telegram_id}",
        )
    await session.commit()
    await message.answer(
        f"Ограничение для пользователя {_user_label(user)} снято.",
        reply_markup=menu_back_keyboard(),
    )


async def show_absence_restrictions(
    message: Message,
    role: str,
    session: AsyncSession,
    config: AppConfig,
) -> None:
    if role not in ADMIN_ROLES:
        await message.answer("Эта штука только для админов.")
        return
    users = list(
        await session.scalars(
            select(User)
            .where(User.false_absences_count >= config.sanctions.ban_threshold)
            .order_by(User.telegram_id)
        )
    )
    if not users:
        await message.answer(
            f"{get_emoji('restriction')} Ограничений сейчас нет. "
            "Все держатся молодцом.",
            reply_markup=menu_back_keyboard(),
            parse_mode=ParseMode.HTML,
        )
        return

    lines = [
        f"{get_emoji('restriction')} Кому можно вернуть возможность отмечать отсутствие:"
    ]
    keyboard = InlineKeyboardBuilder()
    for user in users:
        lines.append(
            f"• {html.escape(_user_label(user))}\n"
            f"  Ложных заявок — {user.false_absences_count}"
        )
        keyboard.button(
            text=get_button_label(
                f"Снять ограничение: {user.telegram_id}",
                icon_key="success",
            ),
            callback_data=UserActionCallback(
                action="unban", telegram_id=user.telegram_id
            ).pack(),
            style="primary",
        )
    keyboard.button(
        text=get_button_label("В меню", icon_key="back_arrow"),
        callback_data=MenuCallback(action="home").pack(),
    )
    keyboard.adjust(1)
    await message.answer(
        "\n".join(lines),
        reply_markup=keyboard.as_markup(),
        parse_mode=ParseMode.HTML,
    )


@router.callback_query(UserActionCallback.filter(F.action == "unban"))
async def unban_absent_choice_handler(
    callback: CallbackQuery,
    callback_data: UserActionCallback,
    role: str,
    session: AsyncSession,
) -> None:
    if role not in ADMIN_ROLES:
        await callback.answer("Недостаточно прав.", show_alert=True)
        return
    user = await session.get(User, callback_data.telegram_id)
    if user is None or user.false_absences_count == 0:
        await callback.answer("Ограничение уже снято или пользователь не найден.", show_alert=True)
        return
    user.false_absences_count = 0
    user.banned_until = None
    await log_admin_action(
        session,
        callback.from_user.id,
        "Снял санкцию",
        f"telegram_id={callback_data.telegram_id}",
    )
    await session.commit()
    await callback.answer("Ограничение снято.")
    if callback.message is not None:
        await callback.message.edit_text(
            f"{get_emoji('success')} Ограничение для пользователя "
            f"{html.escape(_user_label(user))} снято.",
            reply_markup=menu_back_keyboard(),
            parse_mode=ParseMode.HTML,
        )


async def mark_past_absences_completed(session: AsyncSession, config: AppConfig) -> None:
    """Mark active applications from dates before today as completed."""
    await session.execute(
        update(Absence)
        .where(
            Absence.status == "active",
            Absence.target_date < _local_today(config),
        )
        .values(status="completed")
    )
    await session.commit()
