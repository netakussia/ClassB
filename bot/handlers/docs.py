from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import urlsplit

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.keyboards.reply import main_reply_keyboard
from bot.ui import ADMIN_ROLES, menu_back_keyboard
from database.models import ClassDocument


router = Router(name="documents")

CATEGORIES = {
    "cloud": "📁 Облачный диск",
    "labs": "🧪 Лабораторные",
    "manuals": "📚 Методички",
    "templates": "📝 Шаблоны отчётов",
}


class DocumentForm(StatesGroup):
    category = State()
    title = State()
    content = State()


class DocumentCallback(CallbackData, prefix="docs"):
    action: str
    value: str


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
    keyboard = InlineKeyboardBuilder()
    for category, label in CATEGORIES.items():
        keyboard.button(
            text=label,
            callback_data=DocumentCallback(action=action, value=category).pack(),
        )
    keyboard.adjust(1)
    return keyboard.as_markup()


async def show_document_categories(message: Message) -> None:
    await message.answer(
        "📂 Документы и материалы\n\nВыбери категорию:",
        reply_markup=_categories_keyboard("browse"),
    )


@router.message(Command("docs"))
@router.message(F.text == "📂 Документы")
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
    if not items:
        await callback.answer()
        if callback.message:
            await callback.message.answer(
                "В этой категории пока ничего нет.",
                reply_markup=menu_back_keyboard(),
            )
        return

    keyboard = InlineKeyboardBuilder()
    titles = []
    for document in items:
        titles.append(f"• {document.title}")
        if document.url:
            keyboard.button(text=document.title, url=document.url)
        elif document.file_id:
            keyboard.button(
                text=document.title,
                callback_data=DocumentCallback(
                    action="download", value=str(document.id)
                ).pack(),
            )
    keyboard.adjust(1)
    await callback.answer()
    if callback.message:
        await callback.message.answer(
            f"{CATEGORIES[callback_data.value]}\n\n" + "\n".join(titles),
            reply_markup=keyboard.as_markup(),
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


@router.message(Command("docs_add"))
async def document_add_start_handler(
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
        reply_markup=_categories_keyboard("add"),
    )


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
        await callback.message.answer("Введи название документа:")


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
        "Отправь файл документом или пришли текстовую ссылку (http...):"
    )


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
    await session.commit()
    await state.clear()
    await message.answer(
        f"Документ «{document.title}» сохранён в категории «{CATEGORIES[document.category]}».",
        reply_markup=main_reply_keyboard(),
    )