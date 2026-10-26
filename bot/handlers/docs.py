from __future__ import annotations

import html
from datetime import datetime, timezone
from urllib.parse import urlsplit

from aiogram import F, Router
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.keyboards.reply import main_reply_keyboard
from bot.keyboards.inline import InlineKeyboardBuilder
from bot.ui import (
    ADMIN_ROLES,
    DOCUMENT_CATEGORIES,
    DocumentCallback,
    MenuCallback,
    add_cancel_button,
    document_categories_keyboard,
)
from bot.utils.admin_audit import log_admin_action
from bot.utils.emojis import get_btn_emoji, get_button_label, get_emoji
from database.models import ClassDocument


router = Router(name="documents")

CATEGORIES = DOCUMENT_CATEGORIES
CATEGORY_NAMES = {
    "labs": "Лабораторные",
    "manuals": "Методички",
    "templates": "Шаблоны",
    "cloud": "Облако",
}
CATEGORY_EMOJI = {
    "labs": "laboratory",
    "manuals": "manual",
    "templates": "template",
    "cloud": "cloud",
}


class DocumentForm(StatesGroup):
    category = State()
    title = State()
    content = State()


def _normalize_http_url(value: str) -> str | None:
    candidate = value.strip()
    if not candidate or any(character.isspace() for character in candidate):
        return None

    if not candidate.lower().startswith(("http://", "https://")):
        if "://" in candidate:
            return None
        candidate = f"https://{candidate}"

    try:
        parsed = urlsplit(candidate)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            return None
        parsed.port
    except ValueError:
        return None
    return candidate


def _categories_keyboard(action: str) -> InlineKeyboardMarkup:
    return document_categories_keyboard(action)


def _add_categories_keyboard() -> InlineKeyboardMarkup:
    return document_categories_keyboard("add", include_cancel=True)


async def show_document_categories(message: Message) -> None:
    await message.answer(
        f"{get_emoji('documents')} Документы и материалы\n\nВыбери категорию:",
        reply_markup=_categories_keyboard("browse"),
        parse_mode=ParseMode.HTML,
    )


@router.message(Command("docs"))
@router.message(F.text == f"{get_btn_emoji('documents')} Документы")
async def documents_handler(message: Message, state: FSMContext) -> None:
    await state.clear()
    await show_document_categories(message)


@router.callback_query(DocumentCallback.filter(F.action == "browse"))
async def browse_category_handler(
    callback: CallbackQuery,
    callback_data: DocumentCallback,
    session: AsyncSession,
) -> None:
    if callback_data.value not in CATEGORIES:
        await callback.answer("Неизвестная категория.", show_alert=True)
        return

    documents = await session.scalars(
        select(ClassDocument)
        .where(ClassDocument.category == callback_data.value)
        .order_by(ClassDocument.created_at, ClassDocument.id)
    )
    items = list(documents)
    keyboard = InlineKeyboardBuilder()
    titles: list[str] = []
    for document in items:
        titles.append(f"• {html.escape(document.title)}")
        if document.url:
            keyboard.button(
                text=get_button_label(document.title, icon_key="documents"),
                url=document.url,
            )
        elif document.file_id:
            keyboard.button(
                text=get_button_label(document.title, icon_key="attachment"),
                callback_data=DocumentCallback(
                    action="download", value=str(document.id)
                ).pack(),
            )
    keyboard.button(
        text=f"« {get_btn_emoji('back_categories')} Назад в категории",
        callback_data=DocumentCallback(action="categories", value="").pack(),
    )
    keyboard.button(
        text=get_button_label("Назад в главное меню", icon_key="back_arrow"),
        callback_data=MenuCallback(action="home").pack(),
    )
    keyboard.adjust(1)
    await callback.answer()
    if callback.message:
        listing = (
            f"{get_emoji(CATEGORY_EMOJI[callback_data.value])} "
            f"{CATEGORY_NAMES[callback_data.value]}\n\n" + "\n".join(titles)
            if items
            else (
                f"{get_emoji(CATEGORY_EMOJI[callback_data.value])} "
                f"{CATEGORY_NAMES[callback_data.value]}\n\n"
                "В этой категории пока нет материалов."
            )
        )
        await callback.message.edit_text(
            listing,
            reply_markup=keyboard.as_markup(),
            parse_mode=ParseMode.HTML,
        )


@router.callback_query(DocumentCallback.filter(F.action == "categories"))
async def document_categories_handler(callback: CallbackQuery) -> None:
    await callback.answer()
    if callback.message:
        await callback.message.edit_text(
            f"{get_emoji('documents')} Документы и материалы\n\nВыбери категорию:",
            reply_markup=_categories_keyboard("browse"),
            parse_mode=ParseMode.HTML,
        )


@router.callback_query(DocumentCallback.filter(F.action == "download"))
async def download_document_handler(
    callback: CallbackQuery,
    callback_data: DocumentCallback,
    session: AsyncSession,
) -> None:
    try:
        document_id = int(callback_data.value)
    except ValueError:
        await callback.answer("Документ не найден.", show_alert=True)
        return

    document = await session.get(ClassDocument, document_id)
    if document is None or not document.file_id:
        await callback.answer("Файл больше недоступен.", show_alert=True)
        return
    await callback.answer()
    if callback.message:
        await callback.bot.send_document(
            chat_id=callback.message.chat.id,
            document=document.file_id,
            caption=document.title,
        )


async def start_document_add(
    message: Message,
    state: FSMContext,
    role: str,
) -> None:
    if role not in ADMIN_ROLES:
        await message.answer("Добавлять документы могут только администраторы.")
        return
    await state.clear()
    await state.set_state(DocumentForm.category)
    await message.answer(
        "Выбери категорию документа:",
        reply_markup=_add_categories_keyboard(),
    )


@router.message(Command("docs_add"))
async def document_add_start_handler(message: Message, state: FSMContext, role: str) -> None:
    await start_document_add(message, state, role)


@router.callback_query(
    DocumentForm.category,
    DocumentCallback.filter(F.action == "add"),
)
async def document_category_handler(
    callback: CallbackQuery,
    callback_data: DocumentCallback,
    state: FSMContext,
    role: str,
) -> None:
    if role not in ADMIN_ROLES:
        await callback.answer("Добавлять документы могут только администраторы.", show_alert=True)
        await state.clear()
        return
    if callback_data.value not in CATEGORIES:
        await callback.answer("Неизвестная категория.", show_alert=True)
        return
    await state.update_data(category=callback_data.value)
    await state.set_state(DocumentForm.title)
    await callback.answer()
    if callback.message:
        keyboard = InlineKeyboardBuilder()
        add_cancel_button(keyboard)
        await callback.message.edit_text(
            "Введи название документа:",
            reply_markup=keyboard.as_markup(),
        )


@router.message(DocumentForm.title, F.text)
async def document_title_handler(
    message: Message,
    state: FSMContext,
    role: str,
) -> None:
    if role not in ADMIN_ROLES:
        await state.clear()
        await message.answer("Добавлять документы могут только администраторы.")
        return
    title = (message.text or "").strip()
    if not title:
        await message.answer("Название не должно быть пустым. Введи название документа:")
        return
    await state.update_data(title=title)
    await state.set_state(DocumentForm.content)
    await message.answer(
        "Отправь файл документом или пришли текстовую ссылку (http...):",
        reply_markup=_cancel_keyboard(),
    )


@router.message(DocumentForm.category)
async def document_category_fallback_handler(message: Message) -> None:
    await message.answer("Пожалуйста, выбери категорию кнопкой.")


@router.message(DocumentForm.title)
async def document_title_fallback_handler(message: Message) -> None:
    await message.answer("Пожалуйста, отправь название документа текстом.")


def _cancel_keyboard() -> InlineKeyboardMarkup:
    keyboard = InlineKeyboardBuilder()
    add_cancel_button(keyboard)
    return keyboard.as_markup()


@router.message(DocumentForm.content)
async def document_content_handler(
    message: Message,
    state: FSMContext,
    role: str,
    session: AsyncSession,
) -> None:
    if role not in ADMIN_ROLES:
        await state.clear()
        await message.answer("Добавлять документы могут только администраторы.")
        return

    file_id: str | None = None
    url: str | None = None
    if message.document:
        file_id = message.document.file_id
    elif message.text:
        url = _normalize_http_url(message.text)
    if file_id is None and url is None:
        await message.answer(
            "Не удалось распознать содержимое. Отправь документ или ссылку http(s):"
        )
        return

    data = await state.get_data()
    user = message.from_user
    if user is None:
        await message.answer("Не удалось определить администратора. Попробуй ещё раз.")
        return

    document = ClassDocument(
        category=data["category"],
        title=data["title"],
        file_id=file_id,
        url=url,
        uploaded_by=user.id,
        created_at=datetime.now(timezone.utc),
    )
    session.add(document)
    await log_admin_action(
        session,
        user.id,
        "Добавил документ",
        f"название={document.title}, категория={document.category}",
    )
    await session.commit()
    await state.clear()
    await message.answer(
        f"Документ «{html.escape(document.title)}» сохранён в категории "
        f"«{CATEGORY_NAMES[document.category]}».",
        reply_markup=main_reply_keyboard(role),
        parse_mode=ParseMode.HTML,
    )
