import html
import logging

from aiogram import F, Router
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.ai.gateway import AIGateway
from bot.keyboards.inline import InlineKeyboardBuilder
from bot.ai.personalization import greeting_fallback, personalized_greeting

from bot.handlers.admin import (
    admin_broadcast_start_handler,
    show_admin_action_history,
    show_admin_management,
    show_user_activity,
    start_schedule_time_override,
    start_homework_add,
    start_student_add,
    show_user_bans,
)
from bot.handlers.docs import show_document_categories, start_document_add
from bot.handlers.events import start_event_add
from bot.handlers.absent import (
    show_absence_restrictions,
    show_class_absences,
    show_my_absences,
    start_absence_form,
)
from bot.handlers.hw import show_tomorrow_homework
from bot.keyboards.reply import main_reply_keyboard
from bot.handlers.replacements import start_replacement_form
from bot.handlers.schedule import show_my_group, show_next_schedule
from bot.ui import (
    ADMIN_ROLES,
    CancelCallback,
    MenuCallback,
    admin_menu_keyboard,
    main_menu_keyboard,
    menu_back_keyboard,
    remove_inline_screen,
)
from bot.utils.emojis import get_btn_emoji, get_button_label, get_emoji
from bot.utils.permissions import role_allows
from config_loader import AppConfig
from database.models import User


router = Router(name="base")
logger = logging.getLogger(__name__)


ROLE_NAMES = {
    "owner": "главный по кнопочкам",
    "starosta": "староста",
    "moderator": "модератор",
    "user": "ученик",
}


def _main_menu_text(role: str) -> str:
    admin_hint = (
        f"\n\n{get_emoji('admin')} Доступна админ-панель."
        if role in ADMIN_ROLES
        else ""
    )
    return (
        f"{get_emoji('control')} КЛАССНЫЙ ПУЛЬТ\n"
        "ДЗ, расписание и заявки — всё рядом."
        f"{admin_hint}"
    )


def _help_text(role: str) -> str:
    admin_hint = (
        "\n\nДля админов в отдельной панели: добавление ДЗ, замены и заявки класса."
        if role in ADMIN_ROLES
        else ""
    )
    return (
        f"{get_emoji('help')} ЧТО ГДЕ ЛЕЖИТ\n\n"
        "• Домашнее задание — ДЗ на ближайший учебный день и поиск по предмету.\n"
        "• Расписание — ближайший учебный день с учётом замен.\n"
        "• Меня не будет — создай заявку на отсутствие.\n"
        "• Мои заявки — посмотреть или отменить свою отметку."
        f"{admin_hint}\n\n"
        f"{get_emoji('admin')} Написать модератору или "
        f"{get_emoji('owner')} владельцу можно кнопками ниже.\n\n"
        "Выбери нужный раздел в меню.\n\n"
        "Кстати, мой отдельный проект для знакомств (16+) — по кнопке ниже."
    )


def _help_keyboard(config: AppConfig) -> InlineKeyboardMarkup:
    keyboard = InlineKeyboardBuilder()
    moderator_ids = sorted(
        set(config.roles.starosta) | set(config.roles.moderators)
    )
    for telegram_id in moderator_ids:
        keyboard.button(
            text=get_button_label("Написать модератору", icon_key="admin"),
            url=f"tg://user?id={telegram_id}",
        )
    for telegram_id in sorted(set(config.roles.owner)):
        keyboard.button(
            text=get_button_label("Написать владельцу", icon_key="owner"),
            url=f"tg://user?id={telegram_id}",
        )
    keyboard.button(
        text=get_button_label("MeAnima · знакомства (16+)", icon_key="chat"),
        url="https://t.me/MeAnima_Bot",
    )
    keyboard.button(
        text=get_button_label("В меню", icon_key="back_arrow"),
        callback_data=MenuCallback(action="home").pack(),
    )
    keyboard.adjust(1)
    return keyboard.as_markup()


async def show_main_menu(
    message: Message,
    role: str,
    greeting: str,
) -> None:
    await message.answer(
        f"{get_emoji('greeting')} {html.escape(greeting)}",
        reply_markup=main_menu_keyboard(role),
        parse_mode=ParseMode.HTML,
    )
    await message.answer("Меню", reply_markup=main_reply_keyboard(role))


async def _greeting_name(session: AsyncSession, user_id: int, fallback: str) -> str:
    try:
        user = await session.get(User, user_id)
    except Exception as exc:
        logger.warning("Could not load stored greeting name (%s)", type(exc).__name__)
        return fallback
    return user.full_name if user and user.full_name else fallback


@router.message(CommandStart())
async def start_handler(
    message: Message,
    state: FSMContext,
    role: str,
    session: AsyncSession,
    config: AppConfig,
    ai_gateway: AIGateway | None = None,
) -> None:
    await state.clear()
    display_name = (
        await _greeting_name(
            session,
            message.from_user.id,
            message.from_user.full_name,
        )
        if message.from_user
        else None
    )
    greeting = (
        await personalized_greeting(
            session,
            message.from_user.id,
            config.ai,
            config.bot.timezone,
            display_name=display_name,
            ai_gateway=ai_gateway,
        )
        if message.chat.type == "private" and message.from_user is not None
        else greeting_fallback(display_name)
    )
    await show_main_menu(message, role, greeting)


@router.message(Command("admin"))
async def admin_hub_handler(message: Message, role: str) -> None:
    if role not in ADMIN_ROLES:
        await message.answer("Эта панель только для администраторов.")
        return
    await message.answer(
        f"{get_emoji('admin')} <b>АДМИН-ХАБ</b>\nВыбери действие.",
        reply_markup=admin_menu_keyboard(role),
        parse_mode=ParseMode.HTML,
    )


@router.callback_query(CancelCallback.filter())
async def cancel_form_handler(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.answer("Действие отменено.")
    if callback.message is None:
        return
    try:
        await callback.message.delete()
    except Exception:
        pass
    await callback.message.answer("Действие отменено.", reply_markup=menu_back_keyboard())


@router.message(Command("whoami"))
async def whoami_handler(message: Message, role: str) -> None:
    await message.answer(
        f"{get_emoji('user')} Ты здесь как: "
        f"{html.escape(ROLE_NAMES.get(role, role))}.",
        reply_markup=menu_back_keyboard(),
        parse_mode=ParseMode.HTML,
    )


@router.message(Command("chatid"))
async def chat_id_handler(message: Message) -> None:
    if message.chat.type == "private":
        await message.answer(
            "Эта штука работает только внутри нужной группы. Зайди туда и вызови её ещё раз.",
            reply_markup=menu_back_keyboard(),
        )
        return
    await message.answer(
        f"Технический ID этого чата: {message.chat.id}",
        reply_markup=menu_back_keyboard(),
    )


@router.message(Command("help"))
async def help_handler(message: Message, role: str, config: AppConfig) -> None:
    await message.answer(
        _help_text(role),
        reply_markup=main_reply_keyboard(role),
        parse_mode=ParseMode.HTML,
    )
    await message.answer("Связь и полезные ссылки:", reply_markup=_help_keyboard(config))


@router.message(F.text == f"{get_btn_emoji('settings')} Админка")
async def reply_admin_hub_handler(message: Message, role: str) -> None:
    await admin_hub_handler(message, role)


@router.message(F.text == f"{get_btn_emoji('schedule')} Расписание")
async def reply_schedule_handler(
    message: Message,
    state: FSMContext,
    config: AppConfig,
    session: AsyncSession,
) -> None:
    await state.clear()
    await show_next_schedule(message, config, session)


@router.message(F.text == f"{get_btn_emoji('users')} Моя подгруппа")
async def reply_my_group_handler(message: Message, session: AsyncSession) -> None:
    await show_my_group(message, session)


@router.message(F.text == f"{get_btn_emoji('homework')} ДЗ")
async def reply_homework_handler(
    message: Message,
    state: FSMContext,
    config: AppConfig,
    session: AsyncSession,
) -> None:
    await state.clear()
    await show_tomorrow_homework(message, config, session)


@router.message(F.text == f"{get_btn_emoji('absence')} Мои отсутствия")
async def reply_my_absences_handler(
    message: Message,
    state: FSMContext,
    config: AppConfig,
    session: AsyncSession,
) -> None:
    await state.clear()
    if message.from_user is None:
        return
    await show_my_absences(
        message,
        session,
        config,
        telegram_id=message.from_user.id,
        telegram_user=message.from_user,
    )


@router.callback_query(MenuCallback.filter())
async def menu_callback_handler(
    callback: CallbackQuery,
    callback_data: MenuCallback,
    state: FSMContext,
    role: str,
    config: AppConfig,
    session: AsyncSession,
    ai_gateway: AIGateway | None = None,
) -> None:
    if callback.message is None:
        await callback.answer("Не вижу сообщение с меню. Открой /start.", show_alert=True)
        return

    action = callback_data.action
    if action == "home":
        await state.clear()
        await callback.answer()
        display_name = await _greeting_name(
            session,
            callback.from_user.id,
            callback.from_user.full_name,
        )
        greeting = (
            await personalized_greeting(
                session,
                callback.from_user.id,
                config.ai,
                config.bot.timezone,
                display_name=display_name,
                ai_gateway=ai_gateway,
            )
            if callback.message.chat.type == "private"
            else greeting_fallback(display_name)
        )
        await callback.message.edit_text(
            f"{get_emoji('greeting')} {html.escape(greeting)}",
            reply_markup=main_menu_keyboard(role),
            parse_mode=ParseMode.HTML,
        )
        return

    # Any section switch is also a clear, predictable way to abandon a half-filled form.
    await state.clear()

    if action == "help":
        await callback.answer()
        await callback.message.edit_text(
            _help_text(role),
            reply_markup=_help_keyboard(config),
            parse_mode=ParseMode.HTML,
        )
        return

    if action == "profile":
        await callback.answer()
        await callback.message.edit_text(
            f"{get_emoji('user')} Ты здесь как: "
            f"{html.escape(ROLE_NAMES.get(role, role))}.",
            reply_markup=menu_back_keyboard(),
            parse_mode=ParseMode.HTML,
        )
        return

    if action == "my_group":
        await callback.answer()
        await show_my_group(callback.message, session, callback.from_user)
        return

    if action == "admin":
        if role not in ADMIN_ROLES:
            await callback.answer("Эта панель только для админов.", show_alert=True)
            return
        await callback.answer()
        await callback.message.edit_text(
            f"{get_emoji('admin')} АДМИН-ПАНЕЛЬ\nВыбери действие.",
            parse_mode=ParseMode.HTML,
            reply_markup=admin_menu_keyboard(role),
        )
        return

    menu_permissions = {
        "add_homework": "homework_management",
        "replace": "replacements",
        "broadcast": "broadcasts",
        "admin_history": "admin_history",
        "add_document": "documents_manage",
        "add_event": "events_manage",
        "add_student": "student_manage",
        "bans": "bans",
        "class_absences": "absence_review",
        "restrictions": "absence_review",
        "activity": "activity",
        "schedule_time_overrides": "schedule_time",
        "admin_management": "admin_management",
    }
    permission = menu_permissions.get(action)
    if permission is not None and not role_allows(role, permission):
        await callback.answer("⛔ Недостаточно прав для выполнения этой операции.", show_alert=True)
        return

    await callback.answer()
    await remove_inline_screen(callback)
    if action == "homework":
        await show_tomorrow_homework(callback.message, config, session)
    elif action == "documents":
        await show_document_categories(callback.message)
    elif action == "schedule":
        await show_next_schedule(
            callback.message,
            config,
            session,
            telegram_id=callback.from_user.id,
            display_name=callback.from_user.first_name,
        )
    elif action == "absence":
        await start_absence_form(
            callback.message,
            state,
            session,
            config,
            telegram_id=callback.from_user.id,
            telegram_user=callback.from_user,
        )
    elif action == "my_absences":
        await show_my_absences(
            callback.message,
            session,
            config,
            telegram_id=callback.from_user.id,
            telegram_user=callback.from_user,
        )
    elif action == "add_homework":
        await start_homework_add(callback.message, state, role, config, session)
    elif action == "replace":
        await start_replacement_form(callback.message, state, role)
    elif action == "broadcast":
        await admin_broadcast_start_handler(callback.message, state, role)
    elif action == "schedule_time_overrides":
        await start_schedule_time_override(callback.message, state, role)
    elif action == "admin_history":
        await show_admin_action_history(callback.message, session, page=0)
    elif action == "admin_management":
        await show_admin_management(callback.message, session, config, role)
    elif action == "add_document":
        await start_document_add(callback.message, state, role)
    elif action == "add_event":
        await start_event_add(callback.message, state, role)
    elif action == "add_student":
        await start_student_add(callback.message, state, role)
    elif action == "bans":
        await show_user_bans(callback.message, session)
    elif action == "class_absences":
        await show_class_absences(callback.message, role, session, config)
    elif action == "restrictions":
        await show_absence_restrictions(callback.message, role, session, config)
    elif action == "activity":
        await show_user_activity(callback.message, session, config)
    else:
        await callback.message.answer("Такой раздел пока не найден.", reply_markup=menu_back_keyboard())
