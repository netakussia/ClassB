from __future__ import annotations

import html
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.keyboards.inline import InlineKeyboardBuilder
from bot.ui import ADMIN_ROLES, add_cancel_button, menu_back_keyboard
from bot.utils.admin_audit import log_admin_action
from bot.utils.emojis import get_button_label, get_emoji
from config_loader import AppConfig
from database.models import ClassEvent


router = Router(name="events")

EVENT_CATEGORIES = {
    "exam": "Зачёты/Модули",
    "coursework": "Курсовые/Проекты",
    "holiday": "Праздники/ДР",
    "other": "Другое",
}
EVENT_PERIODS = (
    (f"{get_emoji('this_week')} На этой неделе", 7),
    (f"{get_emoji('month')} В этом месяце", 30),
)


class EventForm(StatesGroup):
    category = State()
    title = State()
    event_date = State()


class EventCategoryCallback(CallbackData, prefix="event_category"):
    category: str


def _category_keyboard() -> InlineKeyboardMarkup:
    keyboard = InlineKeyboardBuilder()
    for category, title in EVENT_CATEGORIES.items():
        keyboard.button(
            text=get_button_label(title),
            callback_data=EventCategoryCallback(category=category).pack(),
        )
    add_cancel_button(keyboard)
    keyboard.adjust(1)
    return keyboard.as_markup()


def _event_sections(
    events: list[ClassEvent], today: date
) -> list[tuple[str, list[ClassEvent]]]:
    sections: list[tuple[str, list[ClassEvent]]] = [
        (EVENT_PERIODS[0][0], []),
        (EVENT_PERIODS[1][0], []),
        (f"{get_emoji('later')} Позже", []),
    ]
    week_end = today + timedelta(days=EVENT_PERIODS[0][1])
    month_end = today + timedelta(days=EVENT_PERIODS[1][1])

    for event in events:
        if event.event_date <= week_end:
            sections[0][1].append(event)
        elif event.event_date <= month_end:
            sections[1][1].append(event)
        else:
            sections[2][1].append(event)
    return [(title, items) for title, items in sections if items]


def _format_event_list(events: list[ClassEvent], today: date) -> str:
    lines: list[str] = []
    for section_title, section_events in _event_sections(events, today):
        if lines:
            lines.append("")
        lines.append(section_title)
        lines.extend(
            f"• {event.event_date:%d.%m} {html.escape(event.title)} "
            f"({html.escape(EVENT_CATEGORIES.get(event.category, event.category))})"
            for event in section_events
        )
    return "\n".join(lines)


def _local_today(config: AppConfig) -> date:
    return datetime.now(ZoneInfo(config.bot.timezone)).date()


@router.message(Command("events"))
async def events_handler(
    message: Message,
    config: AppConfig,
    session: AsyncSession,
) -> None:
    today = _local_today(config)
    events = await session.scalars(
        select(ClassEvent)
        .where(ClassEvent.event_date >= today)
        .order_by(ClassEvent.event_date, ClassEvent.id)
    )
    upcoming = list(events)
    if not upcoming:
        await message.answer(
            "Предстоящих важных событий пока не запланировано.",
            reply_markup=menu_back_keyboard(),
        )
        return
    await message.answer(
        _format_event_list(upcoming, today),
        reply_markup=menu_back_keyboard(),
        parse_mode=ParseMode.HTML,
    )


async def start_event_add(
    message: Message,
    state: FSMContext,
    role: str,
) -> None:
    if role not in ADMIN_ROLES:
        await message.answer("Добавлять события могут только администраторы.")
        return
    await state.clear()
    await state.set_state(EventForm.category)
    await message.answer("Выбери категорию события:", reply_markup=_category_keyboard())


@router.message(Command("event_add"))
async def event_add_start_handler(message: Message, state: FSMContext, role: str) -> None:
    await start_event_add(message, state, role)


@router.callback_query(EventForm.category, EventCategoryCallback.filter())
async def event_category_handler(
    callback: CallbackQuery,
    callback_data: EventCategoryCallback,
    state: FSMContext,
    role: str,
) -> None:
    if role not in ADMIN_ROLES:
        await callback.answer("Добавлять события могут только администраторы.", show_alert=True)
        await state.clear()
        return
    if callback_data.category not in EVENT_CATEGORIES:
        await callback.answer("Неизвестная категория.", show_alert=True)
        return
    await state.update_data(category=callback_data.category)
    await state.set_state(EventForm.title)
    await callback.answer()
    if callback.message:
        await callback.message.answer("Введи название события:", reply_markup=_cancel_keyboard())


@router.message(EventForm.title, F.text)
async def event_title_handler(
    message: Message,
    state: FSMContext,
    role: str,
) -> None:
    if role not in ADMIN_ROLES:
        await state.clear()
        await message.answer("Добавлять события могут только администраторы.")
        return
    title = (message.text or "").strip()
    if not title:
        await message.answer("Название не должно быть пустым. Введи название события:")
        return
    await state.update_data(title=title)
    await state.set_state(EventForm.event_date)
    await message.answer(
        "Введи дату события в формате ДД.ММ.ГГГГ "
        "(например, 15.10.2026):",
        reply_markup=_cancel_keyboard(),
    )


@router.message(EventForm.category)
async def event_category_fallback_handler(message: Message) -> None:
    await message.answer("Пожалуйста, выбери категорию события кнопкой.")


@router.message(EventForm.title)
async def event_title_fallback_handler(message: Message) -> None:
    await message.answer("Пожалуйста, отправь название события текстом.")


def _cancel_keyboard() -> InlineKeyboardMarkup:
    keyboard = InlineKeyboardBuilder()
    add_cancel_button(keyboard)
    return keyboard.as_markup()


@router.message(EventForm.event_date, F.text)
async def event_date_handler(
    message: Message,
    state: FSMContext,
    role: str,
    session: AsyncSession,
) -> None:
    if role not in ADMIN_ROLES:
        await state.clear()
        await message.answer("Добавлять события могут только администраторы.")
        return
    date_text = (message.text or "").strip()
    try:
        event_date = datetime.strptime(date_text, "%d.%m.%Y").date()
    except ValueError:
        event_date = None
    if event_date is None or event_date.strftime("%d.%m.%Y") != date_text:
        await message.answer(
            "Неверный формат даты. Введи дату в формате ДД.ММ.ГГГГ "
            "(например, 15.10.2026):"
        )
        return

    data = await state.get_data()
    user = message.from_user
    if user is None:
        await message.answer("Не удалось определить администратора. Попробуй ещё раз.")
        return
    event = ClassEvent(
        title=data["title"],
        event_date=event_date,
        category=data["category"],
        created_by=user.id,
        created_at=datetime.now(timezone.utc),
    )
    session.add(event)
    await log_admin_action(
        session,
        user.id,
        "Добавил событие",
        f"название={event.title}, дата={event.event_date:%d.%m.%Y}",
    )
    await session.commit()
    await state.clear()
    await message.answer(
        f"Событие «{event.title}» добавлено на {event.event_date:%d.%m.%Y}.",
        reply_markup=menu_back_keyboard(),
    )


@router.message(EventForm.event_date)
async def event_date_fallback_handler(message: Message) -> None:
    await message.answer("Пожалуйста, отправь дату текстом в формате ДД.ММ.ГГГГ.")
