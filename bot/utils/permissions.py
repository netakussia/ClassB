from __future__ import annotations


ROLE_PERMISSIONS = {
    "moderator": frozenset({"admin_panel", "homework_management", "replacements"}),
    "starosta": frozenset(
        {
            "admin_panel",
            "homework_management",
            "replacements",
            "bans",
            "schedule_time",
            "documents_manage",
            "events_manage",
            "student_manage",
            "absence_review",
        }
    ),
    "owner": frozenset(
        {
            "admin_panel",
            "homework_management",
            "replacements",
            "bans",
            "schedule_time",
            "documents_manage",
            "events_manage",
            "student_manage",
            "absence_review",
            "broadcasts",
            "week_parity",
            "admin_management",
            "admin_history",
            "activity",
        }
    ),
}


def role_allows(role: str, permission: str) -> bool:
    return permission in ROLE_PERMISSIONS.get(role, frozenset())
