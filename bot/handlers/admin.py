from __future__ import annotations

from datetime import date, datetime, timezone

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.ui import MenuCallback, add_cancel_button, menu_back_keyboard
from bot.utils.homework_dates import local_today, next_lesson_date, subjects_for_date
from bot.utils.subjects import all_subjects, upcoming_subjects
from config_loader import AppConfig, StudentConfig, append_student
from database.models import Homework, HomeworkHistory


router = Router(name="admin")
ADMIN_ROLES = {"owner", "starosta", "moderator"}


class HomeworkForm(StatesGroup):
    subject = State()
    new_subject_confirmation = State()
    target_date = State()
    task = State()
    attachments = State()
    deadline = State()
    overwrite_confirmation = State()


class StudentAddForm(StatesGroup):
    telegram_id = State()
    full_name = State()
    subgroup = State()


class HomeworkOverwriteCallback(CallbackData, prefix="hw_overwrite"):
    action: str


class HomeworkSubjectCallback(CallbackData, prefix="hw_add_subject"):
    action: str
    subject_index: int


class HomeworkTargetCallback(CallbackData, prefix="hw_add_target"):
    choice: str


class HomeworkDeadlineCallback(CallbackData, prefix="hw_add_deadline"):
    action: str


class HomeworkAttachmentCallback(CallbackData, prefix="hw_add_attachment"):
    action: str


class StudentSubgroupCallback(CallbackData, prefix="student_add_subgroup"):
    subgroup: str


def _subject_keyboard(subjects: list[str]) -> InlineKeyboardMarkup:
    keyboard = InlineKeyboardBuilder()
    for index, subject in enumerate(subjects):
        keyboard.button(
            text=subject,
            callback_data=HomeworkSubjectCallback(
                action="select", subject_index=index
            ).pack(),
        )
    keyboard.button(
        text="📚 Показать все предметы",
        callback_data=HomeworkSubjectCallback(action="all", subject_index=0).pack(),
    )
    keyboard.button(
        text="➕ Добавить новый предмет в список",
        callback_data=HomeworkSubjectCallback(action="new", subject_index=0).pack(),
    )
    keyboard.button(
        text="🏠 В меню",
        callback_data=MenuCallback(action="home").pack(),
    )
    add_cancel_button(keyboard)
    keyboard.adjust(1)
    return keyboard.as_markup()


def _attachment_keyboard() -> InlineKeyboardMarkup:
    keyboard = InlineKeyboardBuilder()
    keyboard.button(text="➕ Добавить ещё файл", callback_data=HomeworkAttachmentCallback(action="more").pack())
    keyboard.button(text="✅ Готово", callback_data=HomeworkAttachmentCallback(action="done").pack())
    keyboard.button(text="🏠 В меню", callback_data=MenuCallback(action="home").pack())
    add_cancel_button(keyboard)
    keyboard.adjust(1)
    return keyboard.as_markup()


def _deadline_keyboard() -> InlineKeyboardMarkup:
    keyboard = InlineKeyboardBuilder()
    keyboard.button(
        text="📚 Обычное ДЗ",
        callback_data=HomeworkDeadlineCallback(action="regular").pack(),
    )
    keyboard.button(
        text="⏳ Указать дедлайн",
        callback_data=HomeworkDeadlineCallback(action="deadline").pack(),
    )
    keyboard.button(
        text="🏠 В меню",
        callback_data=MenuCallback(action="home").pack(),
    )
    add_cancel_button(keyboard)
    keyboard.adjust(2, 1)
    return keyboard.as_markup()


def _student_subgroup_keyboard() -> InlineKeyboardMarkup:
    keyboard = InlineKeyboardBuilder()
    keyboard.button(
        text="🇬🇧 Английская",
        callback_data=StudentSubgroupCallback(subgroup="english").pack(),
    )
    keyboard.button(
        text="🇫🇷 Французская",
        callback_data=StudentSubgroupCallback(subgroup="french").pack(),
    )
    add_cancel_button(keyboard)
    keyboard.adjust(2, 1)
    return keyboard.as_markup()


async def start_student_add(
    message: Message,
    state: FSMContext,
    role: str,
) -> None:
    if role not in ADMIN_ROLES:
        await message.answer("Эта штука только для админов.")
        return
    await state.clear()
    await state.set_state(StudentAddForm.telegram_id)
    await message.answer(
        "Введи Telegram ID студента (ученик может узнать его через @getmyid_bot):",
        reply_markup=menu_back_keyboard(),
    )


@router.message(StudentAddForm.telegram_id, F.text)
async def student_add_id_handler(message: Message, state: FSMContext, role: str) -> None:
    if role not in ADMIN_ROLES:
        await state.clear()
        await message.answer("Эта штука только для админов.")
        return
    try:
        telegram_id = int((message.text or "").strip())
    except ValueError:
        telegram_id = 0
    if telegram_id <= 0:
        await message.answer("Telegram ID должен быть положительным числом.")
        return
    await state.update_data(telegram_id=telegram_id)
    await state.set_state(StudentAddForm.full_name)
    await message.answer(
        "Введи Фамилию и Имя студента:",
        reply_markup=menu_back_keyboard(),
    )


@router.message(StudentAddForm.telegram_id)
async def student_add_id_fallback_handler(message: Message) -> None:
    await message.answer("Пожалуйста, отправь Telegram ID числом.")


@router.message(StudentAddForm.full_name, F.text)
async def student_add_name_handler(message: Message, state: FSMContext, role: str) -> None:
    if role not in ADMIN_ROLES:
        await state.clear()
        await message.answer("Эта штука только для админов.")
        return
    full_name = (message.text or "").strip()
    if not full_name:
        await message.answer("Фамилия и имя не должны быть пустыми.")
        return
    await state.update_data(full_name=full_name)
    await state.set_state(StudentAddForm.subgroup)
    await message.answer("Выбери подгруппу:", reply_markup=_student_subgroup_keyboard())


@router.message(StudentAddForm.full_name)
async def student_add_name_fallback_handler(message: Message) -> None:
    await message.answer("Пожалуйста, отправь Фамилию и Имя текстом.")


@router.callback_query(StudentAddForm.subgroup, StudentSubgroupCallback.filter())
async def student_add_subgroup_handler(
    callback: CallbackQuery,
    callback_data: StudentSubgroupCallback,
    state: FSMContext,
    role: str,
    config: AppConfig,
) -> None:
    if role not in ADMIN_ROLES:
        await state.clear()
        await callback.answer("Эта кнопка только для админов.", show_alert=True)
        return
    form_data = await state.get_data()
    telegram_id = form_data.get("telegram_id")
    full_name = form_data.get("full_name")
    if (
        not isinstance(telegram_id, int)
        or not isinstance(full_name, str)
        or callback_data.subgroup not in {"english", "french"}
    ):
        await state.clear()
        await callback.answer("Данные студента потерялись. Начни ещё раз.", show_alert=True)
        return
    if any(student.telegram_id == telegram_id for student in config.students):
        await state.clear()
        await callback.answer("Этот Telegram ID уже есть в списке.", show_alert=True)
        if callback.message is not None:
            await callback.message.edit_text(
                "Студент с таким Telegram ID уже добавлен.",
                reply_markup=menu_back_keyboard(),
            )
        return

    student = StudentConfig(
        telegram_id=telegram_id,
        full_name=full_name,
        subgroup=callback_data.subgroup,
    )
    try:
        append_student(config, student)
    except (OSError, ValueError) as exc:
        await state.clear()
        await callback.answer("Не удалось сохранить список студентов.", show_alert=True)
        if callback.message is not None:
            await callback.message.edit_text(
                "Не удалось сохранить students.yaml. Проверь права доступа к файлу.",
                reply_markup=menu_back_keyboard(),
            )
        return

    await state.clear()
    await callback.answer("Студент добавлен.")
    if callback.message is not None:
        await callback.message.edit_text(
            f"✅ Студент {student.full_name} успешно добавлен и получил доступ к боту!",
            reply_markup=menu_back_keyboard(),
        )


async def start_homework_add(
    message: Message,
    state: FSMContext,
    role: str,
    config: AppConfig,
) -> None:
    """Start the existing homework form from a command or the admin panel."""
    if role not in ADMIN_ROLES:
        await message.answer("Эта штука только для админов.")
        return

    target_date = local_today(config)
    subjects = upcoming_subjects(target_date, config)
    await state.clear()
    await state.update_data(
        target_date=target_date.isoformat(),
        available_subjects=subjects,
    )
    await state.set_state(HomeworkForm.subject)
    prompt = "➕ ДОБАВЛЯЕМ ДЗ\n\nВыбери предмет из ближайших учебных дней:"
    await message.answer(prompt, reply_markup=_subject_keyboard(subjects))


@router.message(Command("hw_add"))
async def homework_add_start_handler(
    message: Message,
    state: FSMContext,
    role: str,
    config: AppConfig,
) -> None:
    await start_homework_add(message, state, role, config)


async def _ask_for_homework_task(
    message: Message,
    state: FSMContext,
    subject: str,
    replace_message: bool = False,
) -> None:
    data = await state.get_data()
    target_date = data.get("target_date")
    date_label = ""
    if isinstance(target_date, str):
        try:
            date_label = f"\nДата ДЗ: {date.fromisoformat(target_date):%d.%m.%Y}\n"
        except ValueError:
            pass
    await state.update_data(subject=subject)
    await state.set_state(HomeworkForm.task)
    prompt = (
        f"📝 {subject}\n\n"
        f"{date_label}\n"
        "Отправь текст, фото или документ.\n"
        "После этого можно будет добавить ещё вложения."
    )
    if replace_message:
        await message.edit_text(prompt, reply_markup=menu_back_keyboard())
    else:
        await message.answer(prompt, reply_markup=menu_back_keyboard())


@router.callback_query(
    HomeworkForm.subject,
    HomeworkSubjectCallback.filter(),
)
@router.callback_query(
    HomeworkForm.new_subject_confirmation,
    HomeworkSubjectCallback.filter(),
)
async def homework_subject_choice_handler(
    callback: CallbackQuery,
    callback_data: HomeworkSubjectCallback,
    state: FSMContext,
    config: AppConfig,
    session: AsyncSession,
) -> None:
    if callback.message is None:
        await callback.answer("Открой добавление ДЗ ещё раз.", show_alert=True)
        return
    if callback_data.action == "all":
        subjects = all_subjects(config)
        await state.update_data(available_subjects=subjects)
        await callback.answer()
        await callback.message.edit_text("Выбери предмет из полного списка расписания:", reply_markup=_subject_keyboard(subjects))
        return
    if callback_data.action == "new":
        await callback.answer()
        await state.set_state(HomeworkForm.new_subject_confirmation)
        await callback.message.edit_text(
            "Введи название нового предмета. Он будет добавлен вне расписания только после подтверждения.",
            reply_markup=menu_back_keyboard(),
        )
        return
    if callback_data.action == "cancel_new":
        await state.clear()
        await callback.answer("Добавление отменено.")
        await callback.message.edit_text("Новый предмет не добавлен.", reply_markup=menu_back_keyboard())
        return
    if callback_data.action == "confirm_new":
        subject = (await state.get_data()).get("pending_new_subject")
        if not isinstance(subject, str):
            await state.clear()
            await callback.answer("Название предмета потерялось.", show_alert=True)
            return
        await callback.answer()
        await _ask_for_homework_task(callback.message, state, subject, replace_message=True)
        return
    if callback_data.action != "select":
        await callback.answer("Не понял выбор предмета.", show_alert=True)
        return

    subjects = (await state.get_data()).get("available_subjects", [])
    if (
        not isinstance(subjects, list)
        or callback_data.subject_index not in range(len(subjects))
        or not isinstance(subjects[callback_data.subject_index], str)
    ):
        await state.clear()
        await callback.answer("Список предметов устарел. Начни добавление заново.", show_alert=True)
        return
    subject = subjects[callback_data.subject_index]
    today = local_today(config)
    today_subjects = await subjects_for_date(session, today, config)
    if subject in today_subjects:
        next_date = await next_lesson_date(
            session,
            subject,
            config,
            today,
            inclusive=False,
        )
        if next_date is None:
            await callback.answer("Следующий урок по этому предмету не найден.", show_alert=True)
            return
        await state.update_data(
            subject=subject,
            today_date=today.isoformat(),
            next_lesson_date=next_date.isoformat(),
        )
        await state.set_state(HomeworkForm.target_date)
        keyboard = InlineKeyboardBuilder()
        keyboard.button(
            text=f"📝 Задали СЕГОДНЯ (на {next_date:%d.%m.%Y})",
            callback_data=HomeworkTargetCallback(choice="next").pack(),
        )
        keyboard.button(
            text=f"✏️ Изменить ДЗ НА СЕГОДНЯ ({today:%d.%m.%Y})",
            callback_data=HomeworkTargetCallback(choice="today").pack(),
        )
        keyboard.button(text="🏠 В меню", callback_data=MenuCallback(action="home").pack())
        add_cancel_button(keyboard)
        keyboard.adjust(1)
        await callback.answer()
        await callback.message.edit_text(
            f"Для «{subject}» уже есть урок сегодня. К какому занятию относится это ДЗ?",
            reply_markup=keyboard.as_markup(),
        )
        return

    target_date = await next_lesson_date(session, subject, config, today)
    if target_date is None:
        await callback.answer("Ближайший урок по этому предмету не найден.", show_alert=True)
        return
    await state.update_data(target_date=target_date.isoformat())
    await callback.answer()
    await _ask_for_homework_task(
        callback.message,
        state,
        subject,
        replace_message=True,
    )


@router.callback_query(HomeworkForm.target_date, HomeworkTargetCallback.filter())
async def homework_target_choice_handler(
    callback: CallbackQuery,
    callback_data: HomeworkTargetCallback,
    state: FSMContext,
) -> None:
    if callback.message is None:
        await callback.answer("Открой добавление ДЗ ещё раз.", show_alert=True)
        return
    data = await state.get_data()
    choice_key = "next_lesson_date" if callback_data.choice == "next" else "today_date"
    target_date = data.get(choice_key)
    subject = data.get("subject")
    if callback_data.choice not in {"next", "today"} or not isinstance(target_date, str) or not isinstance(subject, str):
        await callback.answer("Выбор даты устарел. Начни добавление заново.", show_alert=True)
        await state.clear()
        return
    try:
        date.fromisoformat(target_date)
    except ValueError:
        await callback.answer("Дата некорректна. Начни добавление заново.", show_alert=True)
        await state.clear()
        return
    await state.update_data(target_date=target_date)
    await callback.answer()
    await _ask_for_homework_task(callback.message, state, subject, replace_message=True)


@router.message(HomeworkForm.subject, F.text)
async def homework_subject_handler(message: Message) -> None:
    await message.answer("Предмет выбирается кнопками. Свободный ввод здесь отключён.")


@router.message(HomeworkForm.new_subject_confirmation, F.text)
async def homework_new_subject_handler(message: Message, state: FSMContext) -> None:
    subject = (message.text or "").strip()
    if not subject:
        await message.answer("Название предмета не должно быть пустым.")
        return
    await state.update_data(pending_new_subject=subject)
    keyboard = InlineKeyboardBuilder()
    keyboard.button(text="✅ Да, добавить", callback_data=HomeworkSubjectCallback(action="confirm_new", subject_index=0).pack())
    keyboard.button(text="✋ Отмена", callback_data=HomeworkSubjectCallback(action="cancel_new", subject_index=0).pack())
    add_cancel_button(keyboard)
    keyboard.adjust(2)
    await message.answer(f"Такого предмета нет в расписании, точно добавить «{subject}» как новый?", reply_markup=keyboard.as_markup())


@router.message(HomeworkForm.task)
async def homework_task_handler(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    previous_prompt_id = data.get("attachment_prompt_id")
    if isinstance(previous_prompt_id, int):
        try:
            await message.bot.edit_message_reply_markup(
                chat_id=message.chat.id,
                message_id=previous_prompt_id,
                reply_markup=None,
            )
        except TelegramAPIError:
            # The prompt may have been deleted or already replaced; accepting the
            # attachment is more important than cosmetic cleanup.
            pass
    attachments: list[dict[str, str]] = data.get("attachments", [])
    if message.photo:
        attachments.append({"file_id": message.photo[-1].file_id, "type": "photo"})
        task_text = message.caption
    elif message.document is not None:
        attachments.append({"file_id": message.document.file_id, "type": "document"})
        task_text = message.caption
    elif message.text is not None and not data.get("homework_text"):
        task_text = message.text.strip() or None
    else:
        await message.answer("Пришли фото или документ, либо нажми «Готово».", reply_markup=_attachment_keyboard())
        return

    await state.update_data(
        homework_text=task_text.strip() if task_text else data.get("homework_text"),
        attachments=attachments,
    )
    await state.set_state(HomeworkForm.attachments)
    prompt_message = await message.answer(
        "Можно прикрепить ещё фото или документы.\nКогда закончишь — нажми «Готово».",
        reply_markup=_attachment_keyboard(),
    )
    await state.update_data(attachment_prompt_id=prompt_message.message_id)


@router.message(HomeworkForm.attachments)
async def homework_more_attachments_handler(message: Message, state: FSMContext) -> None:
    await homework_task_handler(message, state)


@router.callback_query(HomeworkForm.attachments, HomeworkAttachmentCallback.filter())
async def homework_attachments_done_handler(callback: CallbackQuery, callback_data: HomeworkAttachmentCallback, state: FSMContext) -> None:
    if callback.message is None:
        return
    await callback.answer()
    if callback_data.action == "more":
        await callback.message.edit_text("Пришли следующее фото или документ.", reply_markup=_attachment_keyboard())
        return
    if callback_data.action == "done":
        await state.set_state(HomeworkForm.deadline)
        await state.update_data(attachment_prompt_id=None)
        await callback.message.edit_text("Финишная прямая: это обычное ДЗ или проект с дедлайном?", reply_markup=_deadline_keyboard())


async def _save_homework_from_form(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    deadline_date: date | None,
    updated_by: int | None,
    replace_message: bool = False,
) -> None:
    async def respond(text: str, reply_markup: InlineKeyboardMarkup | None = None) -> None:
        if replace_message:
            await message.edit_text(text, reply_markup=reply_markup)
        else:
            await message.answer(text, reply_markup=reply_markup)

    form_data = await state.get_data()
    subject = form_data.get("subject")
    target_date_value = form_data.get("target_date")
    homework_text = form_data.get("homework_text")
    attachments = form_data.get("attachments")
    if (
        not isinstance(subject, str)
        or not isinstance(target_date_value, str)
        or (homework_text is not None and not isinstance(homework_text, str))
        or not isinstance(attachments, list)
        or updated_by is None
    ):
        await state.clear()
        await respond(
            "Данные ДЗ потерялись где-то между партами. Начни ещё раз из меню.",
            menu_back_keyboard(),
        )
        return

    try:
        target_date = date.fromisoformat(target_date_value)
    except ValueError:
        await state.clear()
        await respond("Дата ДЗ некорректна. Начни добавление заново.", menu_back_keyboard())
        return

    updated_at = datetime.now(timezone.utc)
    existing_result = await session.scalars(
        select(Homework)
        .where(
            Homework.subject == subject,
            Homework.target_date == target_date,
        )
        .order_by(Homework.updated_at.desc(), Homework.id.desc())
        .limit(1)
    )
    existing = existing_result.first()

    if existing is not None:
        await state.update_data(
            existing_homework_id=existing.id,
            target_date=target_date.isoformat(),
            deadline_date=deadline_date.isoformat() if deadline_date else None,
        )
        await state.set_state(HomeworkForm.overwrite_confirmation)
        keyboard = InlineKeyboardBuilder()
        keyboard.button(
            text="✅ Да, заменить",
            callback_data=HomeworkOverwriteCallback(action="yes").pack(),
        )
        keyboard.button(
            text="✋ Нет, отменить",
            callback_data=HomeworkOverwriteCallback(action="no").pack(),
        )
        add_cancel_button(keyboard)
        keyboard.adjust(2)
        await respond(
            f"ДЗ по «{subject}» уже есть. Заменить его? "
            "Прошлая версия останется в истории.",
            keyboard.as_markup(),
        )
        return

    session.add(
        Homework(
            subject=subject,
            text=homework_text,
            attachments=attachments,
            target_date=target_date,
            is_deadline=deadline_date is not None,
            deadline_date=deadline_date,
            updated_by=updated_by,
            created_at=updated_at,
            updated_at=updated_at,
        )
    )
    await session.commit()
    await state.clear()
    await respond(
        f"✅ ДЗ по «{subject}» добавлено. Красота.",
        menu_back_keyboard(),
    )


@router.callback_query(
    HomeworkForm.deadline,
    HomeworkDeadlineCallback.filter(),
)
async def homework_deadline_choice_handler(
    callback: CallbackQuery,
    callback_data: HomeworkDeadlineCallback,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    if callback.message is None or callback.from_user is None:
        await callback.answer("Не удалось определить пользователя.", show_alert=True)
        return
    if callback_data.action == "regular":
        await callback.answer()
        await _save_homework_from_form(
            callback.message,
            state,
            session,
            deadline_date=None,
            updated_by=callback.from_user.id,
            replace_message=True,
        )
        return
    if callback_data.action == "deadline":
        await callback.answer()
        await callback.message.edit_text(
            "Введи дату дедлайна в формате ДД.ММ.ГГГГ.",
            reply_markup=menu_back_keyboard(),
        )
        return
    await callback.answer("Не понял тип ДЗ.", show_alert=True)


@router.message(HomeworkForm.deadline, F.text)
async def homework_deadline_handler(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    deadline_text = (message.text or "").strip()
    try:
        deadline_date = datetime.strptime(deadline_text, "%d.%m.%Y").date()
        if deadline_date.strftime("%d.%m.%Y") != deadline_text:
            raise ValueError("Deadline date must use DD.MM.YYYY format")
    except ValueError:
        await message.answer("Нужна дата строго в формате ДД.ММ.ГГГГ, например 05.10.2026.")
        return

    await _save_homework_from_form(
        message,
        state,
        session,
        deadline_date=deadline_date,
        updated_by=message.from_user.id if message.from_user is not None else None,
    )


@router.message(HomeworkForm.deadline)
async def homework_deadline_fallback_handler(message: Message) -> None:
    await message.answer("Пожалуйста, отправь дату текстом в формате ДД.ММ.ГГГГ.")


@router.callback_query(
    HomeworkForm.overwrite_confirmation,
    HomeworkOverwriteCallback.filter(),
)
async def homework_overwrite_handler(
    callback: CallbackQuery,
    callback_data: HomeworkOverwriteCallback,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    if callback_data.action == "no":
        await state.clear()
        await callback.answer("Добавление отменено.")
        if callback.message is not None:
            await callback.message.edit_text(
                "Добавление ДЗ отменено. Ничего не сломалось.",
                reply_markup=menu_back_keyboard(),
            )
        return
    if callback_data.action != "yes" or callback.from_user is None:
        await callback.answer("Некорректное действие.", show_alert=True)
        return

    data = await state.get_data()
    homework_id = data.get("existing_homework_id")
    subject = data.get("subject")
    homework_text = data.get("homework_text")
    target_date_value = data.get("target_date")
    attachments = data.get("attachments")
    deadline_value = data.get("deadline_date")
    if (
        not isinstance(homework_id, int)
        or not isinstance(subject, str)
        or not isinstance(target_date_value, str)
        or (homework_text is not None and not isinstance(homework_text, str))
        or not isinstance(attachments, list)
        or (deadline_value is not None and not isinstance(deadline_value, str))
    ):
        await state.clear()
        await callback.answer("Данные ДЗ потеряны. Начни ещё раз.", show_alert=True)
        return

    homework = await session.get(Homework, homework_id)
    if homework is None:
        await state.clear()
        await callback.answer("Исходное ДЗ уже удалено.", show_alert=True)
        return

    try:
        deadline_date = date.fromisoformat(deadline_value) if deadline_value else None
        target_date = date.fromisoformat(target_date_value)
    except ValueError:
        await state.clear()
        await callback.answer("Некорректная дата дедлайна.", show_alert=True)
        return

    changed_at = datetime.now(timezone.utc)
    session.add(
        HomeworkHistory(
            homework_id=homework.id,
            previous_text=homework.text,
            previous_attachments=list(homework.attachments or []),
            previous_target_date=homework.target_date,
            previous_is_deadline=homework.is_deadline,
            previous_deadline_date=homework.deadline_date,
            changed_by=callback.from_user.id,
            changed_at=changed_at,
        )
    )
    homework.text = homework_text
    homework.attachments = attachments
    homework.target_date = target_date
    homework.is_deadline = deadline_date is not None
    homework.deadline_date = deadline_date
    homework.updated_by = callback.from_user.id
    homework.updated_at = changed_at
    await session.commit()
    await state.clear()

    await callback.answer("ДЗ обновлено.")
    if callback.message is not None:
        await callback.message.edit_text(
            f"✅ ДЗ по «{subject}» обновлено; старая версия сохранена в истории.",
            reply_markup=menu_back_keyboard(),
        )
