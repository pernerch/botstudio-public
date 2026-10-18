import logging
import shutil

from services import logging_service
from services.logging_service import ArchiveRotatingFileHandler


def test_failed_rollover_is_not_retried_for_every_record(tmp_path, monkeypatch):
    handler = ArchiveRotatingFileHandler(
        tmp_path / "function.log", maxBytes=10, backupCount=2, encoding="utf-8"
    )
    moves = []
    sleeps = []

    def locked_move(*_args):
        moves.append(1)
        raise PermissionError("locked by another process")

    monkeypatch.setattr(shutil, "move", locked_move)
    monkeypatch.setattr(logging_service.time, "sleep", lambda seconds: sleeps.append(seconds))
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "x" * 50, None, None)
    try:
        for _ in range(10):
            handler.emit(record)
        assert len(moves) == 5  # one rollover attempt with its retries, then cooldown
        assert len(sleeps) == 4

        handler._rollover_retry_at = 0.0
        assert handler.shouldRollover(record)
    finally:
        handler.close()
