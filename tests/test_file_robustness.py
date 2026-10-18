"""DEC-162: transient file locks and leaked screenshots must never fail a task."""

import json
import os
import time
from pathlib import Path

import pytest

import core.emulator as emulator_module
import services.file_io as file_io
import services.screen_service as screen_module
from core.emulator import Emulator, cleanup_screenshots
from services.file_io import atomic_write_json, read_json


@pytest.fixture(autouse=True)
def fast_retry(monkeypatch):
    monkeypatch.setattr(file_io.time, "sleep", lambda _seconds: None)


def test_atomic_write_retries_transient_permission_error(tmp_path, monkeypatch):
    target = tmp_path / "store.json"
    real_replace = os.replace
    calls = {"count": 0}

    def flaky_replace(source, destination):
        calls["count"] += 1
        if calls["count"] < 3:
            raise PermissionError(13, "Zugriff verweigert")
        return real_replace(source, destination)

    monkeypatch.setattr(file_io.os, "replace", flaky_replace)
    atomic_write_json(target, {"value": "ä"})

    assert calls["count"] == 3
    assert json.loads(target.read_text(encoding="utf-8")) == {"value": "ä"}
    assert list(tmp_path.glob("*.tmp")) == []


def test_atomic_write_gives_up_after_persistent_lock(tmp_path, monkeypatch):
    def locked(_source, _destination):
        raise PermissionError(13, "Zugriff verweigert")

    monkeypatch.setattr(file_io.os, "replace", locked)
    with pytest.raises(PermissionError):
        atomic_write_json(tmp_path / "store.json", {})
    assert list(tmp_path.glob("*.tmp")) == []


def test_read_json_retries_transient_lock(tmp_path, monkeypatch):
    target = tmp_path / "store.json"
    target.write_text('{"a": 1}', encoding="utf-8")
    real_read = Path.read_text
    calls = {"count": 0}

    def flaky_read(self, *args, **kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            raise PermissionError(13, "Zugriff verweigert")
        return real_read(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", flaky_read)
    assert read_json(target) == {"a": 1}
    assert calls["count"] == 2


def test_read_json_does_not_retry_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        read_json(tmp_path / "missing.json")


def test_statistics_save_survives_transient_lock(tmp_path, monkeypatch):
    from services import statistics_service

    real_replace = os.replace
    calls = {"count": 0}

    def flaky_replace(source, destination):
        calls["count"] += 1
        if calls["count"] == 1:
            raise PermissionError(13, "Zugriff verweigert")
        return real_replace(source, destination)

    monkeypatch.setattr(file_io.os, "replace", flaky_replace)
    service_class = next(
        value for name, value in vars(statistics_service).items()
        if isinstance(value, type) and hasattr(value, "_save") and hasattr(value, "_load")
    )
    service = service_class.__new__(service_class)
    service.file_name = tmp_path / "statistics.json"
    service._save({"runs": 1})

    assert json.loads(service.file_name.read_text(encoding="utf-8")) == {"runs": 1}
    assert list(tmp_path.glob("*.tmp")) == []


def test_emulator_screenshot_retries_locked_file(monkeypatch, tmp_path):
    monkeypatch.setattr(emulator_module, "SCREENSHOT_RETRY_DELAY", 0)
    attempts = {"count": 0}
    produced = tmp_path / "botstudio_screen_ok.png"

    def capture(self):
        attempts["count"] += 1
        if attempts["count"] < 3:
            raise PermissionError(13, "Zugriff verweigert")
        return produced

    monkeypatch.setattr(Emulator, "_capture_screenshot", capture)
    emulator = Emulator.__new__(Emulator)
    assert emulator.screenshot() == produced
    assert attempts["count"] == 3


def test_emulator_screenshot_raises_runtime_error_after_retries(monkeypatch):
    monkeypatch.setattr(emulator_module, "SCREENSHOT_RETRY_DELAY", 0)

    def capture(self):
        raise PermissionError(13, "Zugriff verweigert")

    monkeypatch.setattr(Emulator, "_capture_screenshot", capture)
    emulator = Emulator.__new__(Emulator)
    with pytest.raises(RuntimeError, match="locked"):
        emulator.screenshot()


def _redirect_temp(monkeypatch, tmp_path):
    monkeypatch.setattr(emulator_module.tempfile, "gettempdir", lambda: str(tmp_path))


def test_cleanup_screenshots_respects_age_keep_and_legacy(monkeypatch, tmp_path):
    _redirect_temp(monkeypatch, tmp_path)
    folder = emulator_module.screenshot_directory()
    old_legacy = tmp_path / "botstudio_screen_legacy.png"
    old_new = folder / "botstudio_screen_old.png"
    kept = folder / "botstudio_screen_kept.png"
    fresh = folder / "botstudio_screen_fresh.png"
    other = tmp_path / "unrelated.png"
    for path in (old_legacy, old_new, kept, fresh, other):
        path.write_bytes(b"x")
    past = time.time() - 3600
    for path in (old_legacy, old_new, kept, other):
        os.utime(path, (past, past))

    removed = cleanup_screenshots(600, keep=[kept])

    assert removed == 2
    assert not old_legacy.exists()
    assert not old_new.exists()
    assert kept.exists()
    assert fresh.exists()
    assert other.exists()


def test_screen_service_keeps_only_newest_captures(monkeypatch, tmp_path):
    monkeypatch.setattr(screen_module.threading, "Thread", _NoThread)
    service = screen_module.ScreenService()
    paths = []
    for index in range(6):
        path = tmp_path / f"botstudio_screen_{index}.png"
        path.write_bytes(b"x")
        paths.append(path)
        service._remember("profile", path)

    assert [path.exists() for path in paths] == [False, False, False, True, True, True]
    assert service.get_latest("profile") == paths[-1]


class _NoThread:
    def __init__(self, *args, **kwargs):
        pass

    def start(self):
        pass


def test_runner_capture_failure_is_reported_not_raised(monkeypatch):
    from services import function_runner

    class FailingScreen:
        def capture(self, profile):
            raise RuntimeError("screenshot file locked: [Errno 13]")

    runner = function_runner.FunctionRunner.__new__(function_runner.FunctionRunner)
    runner.screen_service = FailingScreen()
    runner.logger = _NullLogger()

    path, error = runner._capture_screen({"id": "p", "name": "Marty"})

    assert path is None
    assert "locked" in error


class _NullLogger:
    def __getattr__(self, _name):
        return lambda *args, **kwargs: None

def test_trim_adb_log_keeps_tail(tmp_path):
    from core.emulator import trim_adb_log
    log = tmp_path / "adb.log"
    log.write_bytes(b"".join(b"line %06d\n" % i for i in range(20000)))
    assert trim_adb_log(max_bytes=1000, keep_bytes=500, path=log) is True
    data = log.read_bytes()
    assert len(data) <= 500
    assert data.endswith(b"line 019999\n")
    assert data.startswith(b"line ")


def test_trim_adb_log_small_or_missing(tmp_path):
    from core.emulator import trim_adb_log
    log = tmp_path / "adb.log"
    assert trim_adb_log(path=log) is False
    log.write_bytes(b"x\n")
    assert trim_adb_log(max_bytes=1000, path=log) is False
    assert log.read_bytes() == b"x\n"
