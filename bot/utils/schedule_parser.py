"""Compatibility helpers for the typed schedule loaded by ``config_loader``."""

from config_loader import Lesson, ScheduleConfig


WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday")


def parse_schedule(schedule_config: ScheduleConfig) -> ScheduleConfig:
    """Return the schedule already validated by :func:`config_loader.load_config`."""
    if not isinstance(schedule_config, ScheduleConfig):
        raise ValueError("Expected a ScheduleConfig instance")
    return schedule_config
