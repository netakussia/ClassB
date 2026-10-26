from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import BigInteger, Boolean, Date, DateTime, ForeignKey, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    telegram_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    full_name: Mapped[str | None] = mapped_column(String, nullable=True)
    username: Mapped[str | None] = mapped_column(String, nullable=True)
    subgroup: Mapped[str | None] = mapped_column(String, nullable=True)
    last_action: Mapped[str | None] = mapped_column(String, nullable=True)
    last_active_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    false_absences_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    banned_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Homework(Base):
    __tablename__ = "homework"
    __table_args__ = (UniqueConstraint("subject", "target_date", name="uq_homework_subject_target_date"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    subject: Mapped[str] = mapped_column(String, nullable=False)
    text: Mapped[str | None] = mapped_column(Text, nullable=True)
    attachments: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)
    target_date: Mapped[date] = mapped_column(Date, nullable=False)
    is_deadline: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="0", nullable=False
    )
    deadline_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    updated_by: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class HomeworkHistory(Base):
    __tablename__ = "homework_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    homework_id: Mapped[int] = mapped_column(ForeignKey("homework.id"), nullable=False)
    previous_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    previous_attachments: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, nullable=True)
    previous_target_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    previous_is_deadline: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    previous_deadline_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    changed_by: Mapped[int] = mapped_column(BigInteger, nullable=False)
    changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ScheduleOverride(Base):
    __tablename__ = "schedule_overrides"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    target_date: Mapped[date] = mapped_column(Date, nullable=False)
    lesson_number: Mapped[int] = mapped_column(Integer, nullable=False)
    override_type: Mapped[str] = mapped_column(String, nullable=False)
    new_subject: Mapped[str | None] = mapped_column(String, nullable=True)
    new_room: Mapped[str | None] = mapped_column(String, nullable=True)
    new_teacher: Mapped[str | None] = mapped_column(String, nullable=True)
    is_cancelled: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0", nullable=False)
    created_by: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ScheduleTimeOverride(Base):
    __tablename__ = "schedule_time_overrides"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    target_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    lesson_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duration_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    big_break_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class BotSetting(Base):
    __tablename__ = "bot_settings"

    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[str] = mapped_column(String, nullable=False)


class DynamicRole(Base):
    __tablename__ = "dynamic_roles"

    telegram_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    role: Mapped[str] = mapped_column(String, nullable=False)


class AdminActionLog(Base):
    __tablename__ = "admin_action_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    admin_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    action: Mapped[str] = mapped_column(String, nullable=False)
    details: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class Absence(Base):
    __tablename__ = "absences"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    telegram_id: Mapped[int] = mapped_column(ForeignKey("users.telegram_id"), nullable=False)
    target_date: Mapped[date] = mapped_column(Date, nullable=False)
    lessons: Mapped[list[int] | str] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    flagged_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ClassDocument(Base):
    __tablename__ = "class_documents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    category: Mapped[str] = mapped_column(String, nullable=False)
    title: Mapped[str] = mapped_column(String, nullable=False)
    file_id: Mapped[str | None] = mapped_column(String, nullable=True)
    url: Mapped[str | None] = mapped_column(String, nullable=True)
    uploaded_by: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ClassEvent(Base):
    __tablename__ = "class_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String, nullable=False)
    event_date: Mapped[date] = mapped_column(Date, nullable=False)
    category: Mapped[str] = mapped_column(String, nullable=False)
    created_by: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
