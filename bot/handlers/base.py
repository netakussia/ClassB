from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession

from bot.handlers.admin import start_homework_add
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
    MenuCallback,
    admin_menu_keyboard,
    main_menu_keyboard,
    menu_back_keyboard,
    remove_inline_screen,
)
from config_loader import AppConfig


router = Router(name="base")


ROLE_NAMES = {
    "owner": "главный по кнопочкам",
    "starosta": "староста",
    "moderator": "модератор",
    "user": "ученик",
}


def _main_menu_text(role: str) -> str:
    admin_hint = "\n\n🛠 Доступна админ-панель." if role in ADMIN_ROLES else ""
    return (
        "🎛 КЛАССНЫЙ ПУЛЬТ\n"
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
        "🧭 ЧТО ГДЕ ЛЕЖИТ\n\n"
        "• Домашнее задание — ДЗ на ближайший учебный день и поиск по предмету.\n"
        "• Расписание — ближайший учебный день с учётом замен.\n"
        "• Меня не будет — создай заявку на отсутствие.\n"
        "• Мои заявки — посмотреть или отменить свою отметку."
        f"{admin_hint}\n\n"
        "Выбери нужный раздел в меню.\n\n"
        "Кстати, мой отдельный проект для знакомств (16+) — по кнопке ниже."
    )


def _help_keyboard() -> InlineKeyboardMarkup:
    keyboard = InlineKeyboardBuilder()
    keyboard.button(
        text="💬 MeAnima · знакомства (16+)",
        url="https://t.me/MeAnima_Bot",
    )
    keyboard.button(text="🏠 В меню", callback_data=MenuCallback(action="home").pack())
    keyboard.adjust(1)
    return keyboard.as_markup()


async def show_main_menu(message: Message, role: str) -> None:
    await message.answer(
        _main_menu_text(role),
        reply_markup=main_menu_keyboard(role),
    )
    await message.answer("Меню разделов доступно на клавиатуре ниже.", reply_markup=main_reply_keyboard())


@router.message(CommandStart())
async def start_handler(message: Message, state: FSMContext, role: str) -> None:
    await state.clear()
    await show_main_menu(message, role)


@router.message(Command("whoami"))
async def whoami_handler(message: Message, role: str) -> None:
    await message.answer(
        f"👤 Ты здесь как: {ROLE_NAMES.get(role, role)}.",
        reply_markup=menu_back_keyboard(),
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
async def help_handler(message: Message, role: str) -> None:
    await message.answer(_help_text(role), reply_markup=main_reply_keyboard())
    await message.answer("Полезная ссылка:", reply_markup=_help_keyboard())


@router.message(F.text == "📅 Расписание")
async def reply_schedule_handler(
    message: Message,
    state: FSMContext,
    config: AppConfig,
    session: AsyncSession,
) -> None:
    await state.clear()
    await show_next_schedule(message, config, session)


@router.message(F.text == "👥 Моя подгруппа")
async def reply_my_group_handler(message: Message, session: AsyncSession) -> None:
    await show_my_group(message, session)


@router.message(F.text == "📚 ДЗ")
async def reply_homework_handler(
    message: Message,
    state: FSMContext,
    config: AppConfig,
    session: AsyncSession,
) -> None:
    await state.clear()
    await show_tomorrow_homework(message, config, session)


@router.message(F.text == "🙋 Мои отсутствия")
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
) -> None:
    if callback.message is None:
        await callback.answer("Не вижу сообщение с меню. Открой /start.", show_alert=True)
        return

    action = callback_data.action
    if action == "home":
        await state.clear()
        await callback.answer()
        await callback.message.edit_text(
            _main_menu_text(role),
            reply_markup=main_menu_keyboard(role),
        )
        return

    # Any section switch is also a clear, predictable way to abandon a half-filled form.
    await state.clear()

    if action == "help":
        await callback.answer()
        await callback.message.edit_text(_help_text(role), reply_markup=_help_keyboard())
        return

    if action == "profile":
        await callback.answer()
        await callback.message.edit_text(
            f"👤 Ты здесь как: {ROLE_NAMES.get(role, role)}.",
            reply_markup=menu_back_keyboard(),
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
            "🛠 АДМИН-ПАНЕЛЬ\nВыбери действие.",
            reply_markup=admin_menu_keyboard(),
        )
        return

    if action in {"add_homework", "replace", "class_absences", "restrictions"} and role not in ADMIN_ROLES:
        await callback.answer("Эта кнопка только для админов.", show_alert=True)
        return

    await callback.answer()
    await remove_inline_screen(callback)
    if action == "homework":
        await show_tomorrow_homework(callback.message, config, session)
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
        await start_homework_add(callback.message, state, role, config)
    elif action == "replace":
        await start_replacement_form(callback.message, state, role)
    elif action == "class_absences":
        await show_class_absences(callback.message, role, session, config)
    elif action == "restrictions":
        await show_absence_restrictions(callback.message, role, session, config)
    else:
        await callback.message.answer("Такой раздел пока не найден.", reply_markup=menu_back_keyboard())
