from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
import yaml


WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday")


@dataclass(frozen=True, slots=True)
class BotConfig:
    token: str
    timezone: str
    group_chat_id: int


@dataclass(frozen=True, slots=True)
class RolesConfig:
    owner: list[int]
    starosta: list[int]
    moderators: list[int]


@dataclass(frozen=True, slots=True)
class StudentConfig:
    telegram_id: int
    full_name: str
    subgroup: str


@dataclass(frozen=True, slots=True)
class SanctionsConfig:
    ban_threshold: int
    ban_days: int


@dataclass(frozen=True, slots=True)
class AIConfig:
    enabled: bool = False
    project_limits_verified: bool = False
    humor_enabled: bool = False
    personalization_enabled: bool = False
    reactions_enabled: bool = False
    model: str = "gemini-3.5-flash-lite"
    api_key: str | None = None
    user_daily_limit: int = 25
    user_per_minute_limit: int = 6
    global_daily_limit: int = 500
    global_per_minute_limit: int = 15
    global_tokens_per_minute: int = 250000
    prompt_token_reserve: int = 512
    max_input_chars: int = 500
    joke_max_output_tokens: int = 120
    timeout_seconds: int = 15
    retries: int = 1
    personalization_cache_hours: int = 5
    joke_repeat_window_minutes: int = 30
    activity_retention_days: int = 30
    quota_retention_days: int = 31


@dataclass(frozen=True, slots=True)
class Lesson:
    time: str
    subject: str
    room: str
    teacher: str
    subgroup: str | None = None


@dataclass(frozen=True, slots=True)
class DaySchedule:
    odd: list[Lesson]
    even: list[Lesson]


@dataclass(frozen=True, slots=True)
class WeekParity:
    reference_date: date
    reference_parity: str


@dataclass(frozen=True, slots=True)
class ScheduleConfig:
    week_parity: WeekParity
    monday: DaySchedule
    tuesday: DaySchedule
    wednesday: DaySchedule
    thursday: DaySchedule
    friday: DaySchedule
    lesson_duration_minutes: int = 90

    def get_parity(self, current_date: date) -> str:
        """Return the parity for the week containing ``current_date``."""
        current_monday = current_date - timedelta(days=current_date.weekday())
        reference_monday = self.week_parity.reference_date - timedelta(
            days=self.week_parity.reference_date.weekday()
        )
        weeks_between = (current_monday - reference_monday).days // 7
        if weeks_between % 2 == 0:
            return self.week_parity.reference_parity
        return "even" if self.week_parity.reference_parity == "odd" else "odd"

    def lessons_for(self, weekday: str, parity: str) -> list[Lesson]:
        """Return lessons for an English weekday key and week parity."""
        if weekday not in WEEKDAYS:
            raise ValueError(f"Unknown weekday: {weekday}")
        if parity not in {"odd", "even"}:
            raise ValueError("parity must be either 'odd' or 'even'")
        day = getattr(self, weekday)
        return day.odd if parity == "odd" else day.even


@dataclass(frozen=True, slots=True)
class AppConfig:
    bot: BotConfig
    roles: RolesConfig
    students: list[StudentConfig]
    students_path: Path
    sanctions: SanctionsConfig
    schedule: ScheduleConfig
    ai: AIConfig = field(default_factory=AIConfig)


def _load_yaml(path: Path) -> dict[str, Any]:
    try:
        with path.open(encoding="utf-8") as config_file:
            data = yaml.safe_load(config_file)
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"Configuration file not found: {path}") from exc

    if not isinstance(data, dict):
        raise ValueError(f"Expected a YAML mapping in {path}")
    return data


def _required(data: dict[str, Any], key: str, expected_type: type, path: Path) -> Any:
    if key not in data:
        raise ValueError(f"Missing required key '{key}' in {path}")

    value = data[key]
    # bool is a subclass of int, so reject it when an integer is required.
    valid = type(value) is expected_type if expected_type is int else isinstance(value, expected_type)
    if not valid:
        raise ValueError(
            f"Expected '{key}' in {path} to have type {expected_type.__name__}"
        )
    return value


def _int_list(data: dict[str, Any], key: str, path: Path) -> list[int]:
    value = _required(data, key, list, path)
    if any(type(item) is not int for item in value):
        raise ValueError(f"Expected every item in '{key}' in {path} to be an integer")
    return value


def _students_config(data: dict[str, Any], path: Path) -> list[StudentConfig]:
    raw_students = _required(data, "students", list, path)
    students: list[StudentConfig] = []
    seen_ids: set[int] = set()
    for index, raw_student in enumerate(raw_students, start=1):
        context = f"students[{index}]"
        if not isinstance(raw_student, dict):
            raise ValueError(f"Expected a mapping for '{context}' in {path}")
        telegram_id = raw_student.get("telegram_id")
        full_name = raw_student.get("full_name")
        subgroup = raw_student.get("subgroup")
        if type(telegram_id) is not int or telegram_id <= 0:
            raise ValueError(f"Expected a positive integer telegram_id for '{context}' in {path}")
        if not isinstance(full_name, str) or not full_name.strip():
            raise ValueError(f"Expected a non-empty full_name for '{context}' in {path}")
        if subgroup not in {"english", "french"}:
            raise ValueError(f"Expected subgroup 'english' or 'french' for '{context}' in {path}")
        if telegram_id in seen_ids:
            raise ValueError(f"Duplicate telegram_id {telegram_id} in {path}")
        seen_ids.add(telegram_id)
        students.append(
            StudentConfig(
                telegram_id=telegram_id,
                full_name=full_name.strip(),
                subgroup=subgroup,
            )
        )
    return students


def append_student(config: AppConfig, student: StudentConfig) -> None:
    """Persist a roster entry and update the live whitelist after a safe write."""
    path = config.students_path
    data = _load_yaml(path)
    students = _students_config(data, path)
    if any(item.telegram_id == student.telegram_id for item in students):
        raise ValueError(f"Student with Telegram ID {student.telegram_id} already exists")

    raw_students = data.setdefault("students", [])
    if not isinstance(raw_students, list):  # Defensive; _students_config already validates this.
        raise ValueError(f"Expected a list for 'students' in {path}")
    raw_students.append(
        {
            "telegram_id": student.telegram_id,
            "full_name": student.full_name,
            "subgroup": student.subgroup,
        }
    )
    with path.open("w", encoding="utf-8") as config_file:
        yaml.safe_dump(
            data,
            config_file,
            allow_unicode=True,
            default_flow_style=False,
            sort_keys=False,
        )
    config.students.append(student)


def _schedule_lessons(value: Any, context: str, path: Path) -> list[Lesson]:
    if not isinstance(value, list):
        raise ValueError(f"Expected a list for '{context}' in {path}")

    lessons: list[Lesson] = []
    for index, raw_lesson in enumerate(value, start=1):
        if not isinstance(raw_lesson, dict):
            raise ValueError(f"Expected a mapping for '{context}', lesson {index} in {path}")
        fields: dict[str, str] = {}
        for field in ("time", "subject", "room", "teacher"):
            field_value = raw_lesson.get(field)
            if not isinstance(field_value, str) or not field_value.strip():
                raise ValueError(
                    f"Expected a non-empty string for '{context}', lesson {index}, field "
                    f"'{field}' in {path}"
                )
            fields[field] = field_value
        subgroup = raw_lesson.get("subgroup")
        if subgroup is not None and (not isinstance(subgroup, str) or subgroup not in {"english", "french"}):
            raise ValueError(
                f"Expected subgroup 'english' or 'french' for '{context}', lesson {index} in {path}"
            )
        lessons.append(Lesson(**fields, subgroup=subgroup))
    return lessons


def _schedule_config(data: dict[str, Any], path: Path) -> ScheduleConfig:
    lesson_duration_minutes = data.get("lesson_duration_minutes", 90)
    if type(lesson_duration_minutes) is not int or lesson_duration_minutes <= 0:
        raise ValueError("lesson_duration_minutes must be a positive integer")

    raw_parity = _required(data, "week_parity", dict, path)
    reference_date_value = _required(raw_parity, "reference_date", str, path)
    try:
        reference_date = date.fromisoformat(reference_date_value)
    except ValueError as exc:
        raise ValueError("week_parity.reference_date must use YYYY-MM-DD format") from exc
    if reference_date.weekday() != 0:
        raise ValueError("week_parity.reference_date must be a Monday")

    reference_parity = _required(raw_parity, "reference_parity", str, path)
    if reference_parity not in {"odd", "even"}:
        raise ValueError("week_parity.reference_parity must be 'odd' or 'even'")

    days: dict[str, DaySchedule] = {}
    for weekday in WEEKDAYS:
        raw_day = _required(data, weekday, dict, path)
        days[weekday] = DaySchedule(
            odd=_schedule_lessons(raw_day.get("odd"), f"{weekday}.odd", path),
            even=_schedule_lessons(raw_day.get("even"), f"{weekday}.even", path),
        )

    return ScheduleConfig(
        week_parity=WeekParity(reference_date, reference_parity),
        monday=days["monday"],
        tuesday=days["tuesday"],
        wednesday=days["wednesday"],
        thursday=days["thursday"],
        friday=days["friday"],
        lesson_duration_minutes=lesson_duration_minutes,
    )


def _ai_config(directory: Path) -> AIConfig:
    path = directory / "ai.yaml"
    if path.exists():
        data = _load_yaml(path)
    else:
        data = {}

    defaults: dict[str, Any] = {
        "enabled": False,
        "project_limits_verified": False,
        "humor_enabled": False,
        "personalization_enabled": False,
        "reactions_enabled": False,
        "model": "gemini-3.5-flash-lite",
        "user_daily_limit": 25,
        "user_per_minute_limit": 6,
        "global_daily_limit": 500,
        "global_per_minute_limit": 15,
        "global_tokens_per_minute": 250000,
        "prompt_token_reserve": 512,
        "max_input_chars": 500,
        "joke_max_output_tokens": 120,
        "timeout_seconds": 15,
        "retries": 1,
        "personalization_cache_hours": 5,
        "joke_repeat_window_minutes": 30,
        "activity_retention_days": 30,
        "quota_retention_days": 31,
    }
    values = {key: data.get(key, default) for key, default in defaults.items()}
    for key in (
        "enabled", "project_limits_verified", "humor_enabled",
        "personalization_enabled", "reactions_enabled",
    ):
        if type(values[key]) is not bool:
            raise ValueError(f"{key} in {path} must be a boolean")
    if not isinstance(values["model"], str) or not values["model"].strip():
        raise ValueError(f"model in {path} must be a non-empty string")
    for key in defaults.keys() - {
        "enabled", "project_limits_verified", "humor_enabled",
        "personalization_enabled", "reactions_enabled", "model"
    }:
        if key == "retries":
            continue
        if type(values[key]) is not int or values[key] <= 0:
            raise ValueError(f"{key} in {path} must be a positive integer")
    if type(values["retries"]) is not int or values["retries"] not in {0, 1}:
        raise ValueError(f"retries in {path} must be 0 or 1")
    if values["user_daily_limit"] > 25 or values["user_per_minute_limit"] > 6:
        raise ValueError(
            "AI user quotas cannot exceed 25 requests/day and 6 requests/minute"
        )
    if values["max_input_chars"] > 500 or values["joke_max_output_tokens"] > 120:
        raise ValueError("AI input/output limits cannot exceed 500 characters/120 tokens")
    if values["retries"] > 1:
        raise ValueError("AI retries cannot exceed one retry")
    if values["joke_repeat_window_minutes"] > 1440:
        raise ValueError("joke_repeat_window_minutes cannot exceed one day")

    return AIConfig(
        **values,
        api_key=os.getenv("GEMINI_API_KEY"),
    )


def load_config(config_dir: str | Path | None = None) -> AppConfig:
    """Load and validate core YAML files plus the optional AI configuration.

    By default, the ``config/`` directory next to this module is used.
    """
    directory = Path(config_dir) if config_dir is not None else Path(__file__).parent / "config"
    load_dotenv(Path(__file__).parent / ".env")

    bot_path = directory / "bot.yaml"
    roles_path = directory / "roles.yaml"
    students_path = directory / "students.yaml"
    sanctions_path = directory / "sanctions.yaml"
    schedule_path = directory / "schedule.yaml"

    bot_data = _load_yaml(bot_path)
    roles_data = _load_yaml(roles_path)
    students_data = _load_yaml(students_path)
    sanctions_data = _load_yaml(sanctions_path)
    schedule_data = _load_yaml(schedule_path)

    return AppConfig(
        bot=BotConfig(
            token=os.getenv("BOT_TOKEN") or _required(bot_data, "token", str, bot_path),
            timezone=_required(bot_data, "timezone", str, bot_path),
            group_chat_id=_required(bot_data, "group_chat_id", int, bot_path),
        ),
        roles=RolesConfig(
            owner=_int_list(roles_data, "owner", roles_path),
            starosta=_int_list(roles_data, "starosta", roles_path),
            moderators=_int_list(roles_data, "moderators", roles_path),
        ),
        students=_students_config(students_data, students_path),
        students_path=students_path,
        sanctions=SanctionsConfig(
            ban_threshold=_required(sanctions_data, "ban_threshold", int, sanctions_path),
            ban_days=_required(sanctions_data, "ban_days", int, sanctions_path),
        ),
        schedule=_schedule_config(schedule_data, schedule_path),
        ai=_ai_config(directory),
    )
