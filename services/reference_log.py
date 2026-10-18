"""Technical IDs of broken references go to the log, never into the GUI.

Each entry carries an ``open_target`` so the log view can jump straight to the
affected task or function. Entries are deduplicated per session because the
views re-render on every refresh.
"""

from __future__ import annotations

from services.logging_service import get_logger

_LOGGED: set[tuple] = set()


def open_target_for_user(game_id: str, user: dict) -> dict:
    if user.get("kind") == "task":
        return {
            "kind": "task",
            "game_id": game_id or "",
            "event_id": user.get("event_id", ""),
            "task_id": user.get("id", ""),
        }
    return {
        "kind": "function",
        "game_id": game_id or "",
        "function_id": user.get("id", ""),
    }


def log_missing_function_reference(game_id: str, function_id: str, user: dict) -> bool:
    """Log one broken function reference once per session; True when logged."""

    key = (game_id or "", function_id or "", user.get("kind"), user.get("id"))
    if key in _LOGGED:
        return False
    _LOGGED.add(key)
    get_logger("function").warning(
        "Missing Function Reference",
        game=game_id or None,
        missing_function_id=function_id,
        user_kind=user.get("kind", ""),
        user_name=user.get("name", ""),
        user_id=user.get("id", ""),
        event_name=user.get("event_name", ""),
        event_id=user.get("event_id", ""),
        profile_name=user.get("profile_name", ""),
        open_target=open_target_for_user(game_id, user),
        next_action="open the user and select the current function again",
    )
    return True
