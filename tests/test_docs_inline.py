from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot.handlers.docs import (
    browse_category_handler,
    document_categories_handler,
    start_document_add,
)
from bot.handlers.base import menu_callback_handler
from bot.handlers.base import _help_keyboard
from bot.keyboards.reply import main_reply_keyboard
from bot.keyboards.inline import InlineKeyboardBuilder
from bot.ui import (
    DOCUMENT_CATEGORIES,
    DocumentCallback,
    MenuCallback,
    admin_menu_keyboard,
    document_categories_keyboard,
    main_menu_keyboard,
)
from database.models import ClassDocument


def _buttons(markup):
    return [button for row in markup.inline_keyboard for button in row]


class DocsInlineTests(unittest.IsolatedAsyncioTestCase):
    async def test_help_has_clickable_role_contacts_from_config(self) -> None:
        keyboard = _help_keyboard(
            SimpleNamespace(
                roles=SimpleNamespace(
                    owner=[100],
                    starosta=[200],
                    moderators=[201, 200],
                )
            )
        )
        buttons = _buttons(keyboard)
        contacts = [
            button
            for button in buttons
            if button.url is not None and button.url.startswith("tg://")
        ]

        self.assertEqual(
            [(button.text, button.url) for button in contacts],
            [
                ("🛠️ Написать модератору", "tg://user?id=200"),
                ("🛠️ Написать модератору", "tg://user?id=201"),
                ("👑 Написать владельцу", "tg://user?id=100"),
            ],
        )
        self.assertTrue(all("<tg-emoji" not in button.text for button in buttons))

    async def test_category_keyboard_uses_requested_names_and_values(self) -> None:
        buttons = _buttons(document_categories_keyboard("browse"))

        self.assertEqual(
            [button.text for button in buttons[:4]],
            [
                "📁 Лабораторные",
                "📚 Методички",
                "📝 Шаблоны",
                "☁️ Облако",
            ],
        )
        self.assertEqual(
            [DocumentCallback.unpack(button.callback_data).value for button in buttons[:4]],
            list(DOCUMENT_CATEGORIES),
        )
        self.assertEqual(buttons[-1].text, "⬅️ Назад в главное меню")
        self.assertEqual(
            MenuCallback.unpack(buttons[-1].callback_data).action,
            "home",
        )

    async def test_main_start_menu_contains_documents_entry(self) -> None:
        buttons = _buttons(main_menu_keyboard("user"))

        documents_button = next(button for button in buttons if button.text == "📁 Документы")
        self.assertEqual(
            MenuCallback.unpack(documents_button.callback_data).action,
            "documents",
        )

    async def test_main_menu_documents_entry_opens_inline_categories(self) -> None:
        message = SimpleNamespace(delete=AsyncMock())
        callback = SimpleNamespace(
            answer=AsyncMock(),
            message=message,
            from_user=SimpleNamespace(id=123, first_name="Test"),
        )
        state = SimpleNamespace(clear=AsyncMock())
        with patch("bot.handlers.base.show_document_categories", new=AsyncMock()) as show_docs:
            await menu_callback_handler(
                callback,
                MenuCallback(action="documents"),
                state,
                role="user",
                config=SimpleNamespace(),
                session=SimpleNamespace(),
            )

        state.clear.assert_awaited_once()
        callback.answer.assert_awaited_once()
        message.delete.assert_awaited_once()
        show_docs.assert_awaited_once_with(message)

    async def test_category_selection_edits_message_and_adds_inline_back_button(self) -> None:
        documents = [
            ClassDocument(
                id=1,
                category="labs",
                title="Лабораторная",
                file_id="file-id",
                url=None,
                uploaded_by=10,
            ),
            ClassDocument(
                id=2,
                category="labs",
                title="Ссылка на методичку",
                file_id=None,
                url="https://example.test/guide",
                uploaded_by=10,
            ),
        ]
        session = SimpleNamespace(scalars=AsyncMock(return_value=documents))
        message = SimpleNamespace(edit_text=AsyncMock())
        callback = SimpleNamespace(answer=AsyncMock(), message=message)

        await browse_category_handler(
            callback,
            DocumentCallback(action="browse", value="labs"),
            session,
        )

        message.edit_text.assert_awaited_once()
        rendered, = message.edit_text.await_args.args
        self.assertIn("Лабораторная", rendered)
        buttons = _buttons(message.edit_text.await_args.kwargs["reply_markup"])
        self.assertEqual(buttons[0].callback_data, DocumentCallback(
            action="download", value="1"
        ).pack())
        self.assertEqual(buttons[1].url, "https://example.test/guide")
        self.assertEqual(buttons[-2].text, "« 🔙 Назад в категории")
        self.assertEqual(
            MenuCallback.unpack(buttons[-1].callback_data).action,
            "home",
        )
        self.assertEqual(buttons[-1].text, "⬅️ Назад в главное меню")

    async def test_back_button_edits_to_category_menu(self) -> None:
        message = SimpleNamespace(edit_text=AsyncMock())
        callback = SimpleNamespace(answer=AsyncMock(), message=message)

        await document_categories_handler(callback)

        message.edit_text.assert_awaited_once()
        self.assertEqual(
            message.edit_text.await_args.kwargs["reply_markup"],
            document_categories_keyboard("browse"),
        )

    async def test_admin_add_uses_inline_category_menu(self) -> None:
        message = SimpleNamespace(answer=AsyncMock())
        state = AsyncMock()

        await start_document_add(message, state, "moderator")

        markup = message.answer.await_args.kwargs["reply_markup"]
        buttons = _buttons(markup)
        self.assertEqual(
            [button.text for button in buttons[:4]],
            list(DOCUMENT_CATEGORIES.values()),
        )
        self.assertEqual(buttons[-2].text, "⬅️ Назад в главное меню")
        self.assertEqual(buttons[-1].text, "❌ Отменить")

    async def test_inline_and_reply_button_labels_are_unicode_plain_text(self) -> None:
        inline_keyboards = [
            main_menu_keyboard("owner"),
            admin_menu_keyboard("owner"),
            document_categories_keyboard("browse", include_cancel=True),
        ]
        inline_labels = [
            button.text
            for keyboard in inline_keyboards
            for button in _buttons(keyboard)
        ]
        reply_keyboard = main_reply_keyboard("owner")
        reply_labels = [
            button.text
            for row in reply_keyboard.keyboard
            for button in row
        ]

        for label in [*inline_labels, *reply_labels]:
            self.assertNotIn("<tg-emoji", label)
            self.assertFalse(
                any(marker in label for marker in ("🟢", "🔴", "🔵", "🟡"))
            )

        self.assertIn("📅 Расписание", reply_labels)
        self.assertIn("📚 ДЗ", reply_labels)
        self.assertIn("📁 Документы", reply_labels)
        self.assertIn("⚙️ Админка", reply_labels)

    async def test_inline_builder_serializes_native_button_styles(self) -> None:
        keyboard = InlineKeyboardBuilder()
        keyboard.button(text="Назад", callback_data="back")
        keyboard.button(
            text="Сохранить",
            callback_data="save",
            style="success",
        )
        keyboard.button(
            text="Отмена",
            callback_data="cancel",
            style="danger",
        )
        markup = keyboard.as_markup()
        serialized = [
            button.model_dump(exclude_none=True)
            for row in markup.inline_keyboard
            for button in row
        ]

        self.assertEqual([button["style"] for button in serialized], [
            "primary",
            "success",
            "danger",
        ])
        self.assertEqual(
            [button["text"] for button in serialized],
            ["Назад", "Сохранить", "Отмена"],
        )


if __name__ == "__main__":
    unittest.main()
