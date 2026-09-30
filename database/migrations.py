"""Synchronous schema migrations used during application startup."""

from __future__ import annotations

import json
from datetime import date, datetime

from sqlalchemy import MetaData, inspect, select, text
from sqlalchemy.engine import Connection

from database.models import Homework, HomeworkHistory


def _as_datetime(value: object) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        return datetime.fromisoformat(value)
    raise ValueError("Legacy homework has an invalid updated_at value")


def _as_date_or_none(value: object) -> date | None:
    if value is None or isinstance(value, date):
        return value
    if isinstance(value, str):
        return date.fromisoformat(value)
    raise ValueError("Legacy homework has an invalid deadline_date value")


def migrate_homework_history(connection: Connection) -> None:
    columns = {column["name"] for column in inspect(connection).get_columns("homework_history")}
    if "previous_is_deadline" not in columns:
        connection.execute(text("ALTER TABLE homework_history ADD COLUMN previous_is_deadline BOOLEAN NOT NULL DEFAULT FALSE"))
    if "previous_deadline_date" not in columns:
        connection.execute(text("ALTER TABLE homework_history ADD COLUMN previous_deadline_date DATE"))
    if "previous_target_date" not in columns:
        connection.execute(text("ALTER TABLE homework_history ADD COLUMN previous_target_date DATE"))


def migrate_homework_target_date(connection: Connection) -> None:
    columns = {column["name"] for column in inspect(connection).get_columns("homework")}
    if "target_date" not in columns:
        connection.execute(text("ALTER TABLE homework ADD COLUMN target_date DATE"))
    connection.execute(text(
        "UPDATE homework SET target_date = COALESCE(target_date, DATE(created_at), "
        "DATE(updated_at), DATE('now'))"
    ))


def migrate_homework_timestamps(connection: Connection) -> None:
    columns = {column["name"] for column in inspect(connection).get_columns("homework")}
    if "created_at" not in columns:
        connection.execute(text("ALTER TABLE homework ADD COLUMN created_at DATETIME"))
        connection.execute(text("UPDATE homework SET created_at = updated_at WHERE created_at IS NULL"))


def migrate_homework_subject_uniqueness(connection: Connection) -> None:
    """Archive duplicate subject/date rows and enforce the composite key."""
    rows = connection.execute(text(
        "SELECT id, subject, text, attachments, target_date, is_deadline, deadline_date, "
        "updated_by, updated_at FROM homework "
        "ORDER BY subject, target_date, updated_at DESC, id DESC"
    )).mappings().all()
    current_by_key: dict[tuple[str, date], int] = {}
    for row in rows:
        key = (row["subject"], _as_date_or_none(row["target_date"]))
        homework_id = row["id"]
        if key not in current_by_key:
            current_by_key[key] = homework_id
            continue
        attachments = row["attachments"]
        if isinstance(attachments, str):
            attachments = json.loads(attachments)
        current_id = current_by_key[key]
        connection.execute(HomeworkHistory.__table__.insert(), {
            "homework_id": current_id,
            "previous_text": row["text"],
            "previous_attachments": attachments,
            "previous_target_date": key[1],
            "previous_is_deadline": bool(row["is_deadline"]),
            "previous_deadline_date": _as_date_or_none(row["deadline_date"]),
            "changed_by": row["updated_by"],
            "changed_at": _as_datetime(row["updated_at"]),
        })
        connection.execute(text("UPDATE homework_history SET homework_id = :current_id WHERE homework_id = :old_id"), {"current_id": current_id, "old_id": homework_id})
        connection.execute(Homework.__table__.delete().where(Homework.id == homework_id))

    unique_keys = {
        frozenset(constraint.get("column_names") or [])
        for constraint in inspect(connection).get_unique_constraints("homework")
    }
    unique_keys.update(
        frozenset(index["column_names"])
        for index in inspect(connection).get_indexes("homework")
        if index.get("unique")
    )
    target_key = frozenset({"subject", "target_date"})
    if target_key in unique_keys:
        return

    if connection.dialect.name != "sqlite":
        raise RuntimeError("Migrating the homework key is currently supported only for SQLite")

    temporary = Homework.__table__.to_metadata(MetaData(), name="_homework_new")
    temporary.create(connection)
    columns = [column.name for column in Homework.__table__.columns]
    connection.execute(
        temporary.insert().from_select(
            columns,
            select(*(Homework.__table__.c[column] for column in columns)),
        )
    )
    connection.execute(text("PRAGMA defer_foreign_keys = ON"))
    connection.execute(text("DROP TABLE homework"))
    connection.execute(text("ALTER TABLE _homework_new RENAME TO homework"))


def migrate_users(connection: Connection) -> None:
    columns = {column["name"] for column in inspect(connection).get_columns("users")}
    if "full_name" not in columns:
        connection.execute(text("ALTER TABLE users ADD COLUMN full_name VARCHAR"))
    if "username" not in columns:
        connection.execute(text("ALTER TABLE users ADD COLUMN username VARCHAR"))
    if "subgroup" not in columns:
        connection.execute(text("ALTER TABLE users ADD COLUMN subgroup VARCHAR"))
