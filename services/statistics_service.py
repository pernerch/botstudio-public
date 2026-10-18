from __future__ import annotations

import copy
import json
import threading
from datetime import datetime, timezone
from pathlib import Path

from services.file_io import atomic_write_json, read_json


class StatisticsService:
    """Persists current and immutable all-time runtime statistics."""

    _locks_guard = threading.Lock()
    _file_locks = {}

    def __init__(self, file_name: str = "data/statistics/statistics.json"):
        self.file_name = Path(file_name)
        self._last_heartbeat: dict[str, datetime] = {}
        key = str(self.file_name.resolve()).casefold()
        with self._locks_guard:
            self._file_lock = self._file_locks.setdefault(key, threading.RLock())

    @staticmethod
    def _empty_account(game_id: str, profile_id: str, profile_name: str) -> dict:
        return {
            "game_id": game_id,
            "profile_id": profile_id,
            "profile_name": profile_name,
            "task_executions": {},
            "uptime_seconds": 0.0,
            "profile_switches": 0,
            "emulator_starts": 0,
        }

    def _load(self) -> dict:
        if not self.file_name.exists():
            return {"current_since": self._now(), "current": {}, "all_time": {}}
        try:
            data = read_json(self.file_name)
        except (OSError, json.JSONDecodeError) as error:
            raise RuntimeError("Statistics cannot be read; counters are unavailable") from error
        if not isinstance(data, dict) or any(
            not isinstance(data.get(scope, {}), dict) for scope in ("current", "all_time")
        ):
            raise RuntimeError("Statistics storage is invalid; counters are unavailable")
        for scope in ("current", "all_time"):
            for account in data.get(scope, {}).values():
                if not isinstance(account, dict) or not isinstance(account.get("task_executions"), dict):
                    raise RuntimeError("Statistics account is invalid; counters are unavailable")
                for task in account["task_executions"].values():
                    if not isinstance(task, dict) or any(
                        type(task.get(key)) is not int or task[key] < 0
                        for key in ("success", "failure")
                    ):
                        raise RuntimeError("Statistics task counters are invalid; counters are unavailable")
        return {
            "current_since": data.get("current_since") or self._now(),
            "current": data.get("current") or {},
            "all_time": data.get("all_time") or {},
        }

    def _save(self, data: dict) -> None:
        # unique temp name + retry: a scanner lock must not lose a count (DEC-162)
        atomic_write_json(self.file_name, data, indent=2, ensure_ascii=True)

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _account_key(game_id: str, profile_id: str) -> str:
        return f"{game_id}:{profile_id}"

    def _account(self, data: dict, scope: str, game_id: str, profile_id: str, profile_name: str) -> dict:
        accounts = data[scope]
        key = self._account_key(game_id, profile_id)
        account = accounts.setdefault(
            key,
            self._empty_account(game_id, profile_id, profile_name),
        )
        account["profile_name"] = profile_name or account.get("profile_name", profile_id)
        return account

    def _update(self, game_id: str, profile_id: str, profile_name: str, updater) -> dict | None:
        if not game_id or not profile_id:
            return
        with self._file_lock:
            data = self._load()
            for scope in ("current", "all_time"):
                updater(self._account(data, scope, game_id, profile_id, profile_name))
            self._save(data)
            return data

    def record_task(
        self,
        game_id: str,
        profile_id: str,
        profile_name: str,
        task_id: str,
        task_name: str,
        outcome: str,
        duration_seconds: float = 0.0,
    ) -> dict | None:
        outcome_key = "success" if outcome == "success" else "failure"

        def update(account: dict) -> None:
            task = account["task_executions"].setdefault(
                task_id,
                {"task_name": task_name, "success": 0, "failure": 0, "duration_seconds": 0.0},
            )
            task["task_name"] = task_name or task.get("task_name", task_id)
            task[outcome_key] += 1
            task["duration_seconds"] += max(0.0, float(duration_seconds or 0))

        data = self._update(game_id, profile_id, profile_name, update)
        if data is None:
            return None
        key = self._account_key(game_id, profile_id)
        current = data["current"][key]["task_executions"][task_id]
        all_time = data["all_time"][key]["task_executions"][task_id]
        return {
            "task_failures_current": current["failure"],
            "task_failures_all_time": all_time["failure"],
            "task_successes_all_time": all_time["success"],
        }

    def record_uptime(self, profile: dict, now: datetime | None = None) -> None:
        game_id = str(profile.get("game") or "")
        profile_id = str(profile.get("botstudio_profile_id") or profile.get("id") or "")
        if not game_id or not profile_id:
            return
        current = now or datetime.now(timezone.utc)
        key = self._account_key(game_id, profile_id)
        previous = self._last_heartbeat.get(key)
        self._last_heartbeat[key] = current
        if previous is None:
            return
        elapsed = min(max((current - previous).total_seconds(), 0.0), 3600.0)
        self._update(
            game_id,
            profile_id,
            str(profile.get("name") or profile_id),
            lambda account: account.__setitem__(
                "uptime_seconds", account.get("uptime_seconds", 0.0) + elapsed
            ),
        )

    def record_profile_switch(self, profile: dict) -> None:
        self._update(
            str(profile.get("game") or ""),
            str(profile.get("botstudio_profile_id") or profile.get("id") or ""),
            str(profile.get("name") or ""),
            lambda account: account.__setitem__(
                "profile_switches", account.get("profile_switches", 0) + 1
            ),
        )

    def record_emulator_start(self, profile: dict) -> None:
        self._update(
            str(profile.get("game") or ""),
            str(profile.get("botstudio_profile_id") or profile.get("id") or ""),
            str(profile.get("name") or ""),
            lambda account: account.__setitem__(
                "emulator_starts", account.get("emulator_starts", 0) + 1
            ),
        )

    def snapshot(self, all_time: bool = False) -> dict:
        with self._file_lock:
            data = self._load()
            return copy.deepcopy(data["all_time" if all_time else "current"])

    def reset_current(self) -> None:
        with self._file_lock:
            data = self._load()
            data["current"] = {}
            data["current_since"] = self._now()
            self._last_heartbeat.clear()
            self._save(data)
