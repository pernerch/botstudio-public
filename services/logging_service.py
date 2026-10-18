import copy
import json
import logging
import shutil
import time
import threading
from collections import deque
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from uuid import uuid4

from services.app_config_service import AppConfigService
from services.log_redaction_service import redact_log_text


class RecentErrorJournal:
    MAX_EVENTS = 50
    MAX_EVENT_LENGTH = 120
    MAX_SNAPSHOT_BYTES = 24 * 1024

    def __init__(self):
        self.stream_id = uuid4().hex
        self._events = deque(maxlen=self.MAX_EVENTS)
        self._sequence = 0
        self._lock = threading.Lock()

    def record(self, severity, module, event, context=None):
        safe_event = str(event).replace("\r", " ").replace("\n", " ")
        safe_event = redact_log_text(safe_event)
        with self._lock:
            self._sequence += 1
            entry = {
                "sequence": self._sequence,
                "id": f"{self.stream_id}:{self._sequence}",
                "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "severity": severity,
                "module": module,
                "event": safe_event[: self.MAX_EVENT_LENGTH],
            }
            safe_context = {}
            for key in (
                "profile", "event_name", "task_name", "function_name",
                "failed_function_name", "failed_block_type", "failure_reason",
            ):
                value = (context or {}).get(key)
                if isinstance(value, str) and value:
                    safe_context[key] = redact_log_text(
                        value.replace("\r", " ").replace("\n", " ")
                    )[:64]
            for key in (
                "task_failures_current", "task_failures_all_time", "task_successes_all_time",
            ):
                value = (context or {}).get(key)
                if type(value) is int and value >= 0:
                    safe_context[key] = value
            chain = (context or {}).get("function_chain")
            if isinstance(chain, list):
                safe_context["function_chain"] = [
                    redact_log_text(name.replace("\r", " ").replace("\n", " "))[:48]
                    for name in chain[:4] if isinstance(name, str)
                ]
            if safe_context:
                entry["context"] = safe_context
            self._events.append(entry)

    def snapshot(self):
        with self._lock:
            snapshot = {
                "stream_id": self.stream_id,
                "events": copy.deepcopy(list(self._events)),
            }
            while len(json.dumps(snapshot).encode("utf-8")) > self.MAX_SNAPSHOT_BYTES:
                snapshot["events"].pop(0)
            return snapshot


_RECENT_ERROR_JOURNAL = RecentErrorJournal()


def get_recent_errors():
    return _RECENT_ERROR_JOURNAL.snapshot()


TRACE_LEVEL = 5

logging.addLevelName(
    TRACE_LEVEL,
    "TRACE",
)

LOG_LEVELS = {
    "normal": logging.INFO,
    "debug": logging.DEBUG,
    "trace": TRACE_LEVEL,
}

LOG_DIR = Path("logs")
LOG_ARCHIVE_DIR = LOG_DIR / "archive"


class ArchiveRotatingFileHandler(RotatingFileHandler):
    """Rotate active logs into one shared FIFO archive directory."""

    def __init__(self, filename, maxBytes, backupCount, encoding=None):
        Path(filename).parent.mkdir(parents=True, exist_ok=True)
        super().__init__(filename, maxBytes=maxBytes, backupCount=0, encoding=encoding)
        self.archive_count = max(1, backupCount)
        self.archive_dir = Path(self.baseFilename).parent / "archive"
        self._rollover_retry_at = 0.0

    # after a failed rollover (file held by another process) every further
    # record would retry and sleep again; wait before the next attempt
    ROLLOVER_RETRY_SECONDS = 60.0

    def shouldRollover(self, record):
        if time.monotonic() < self._rollover_retry_at:
            return False
        return super().shouldRollover(record)

    def doRollover(self):
        if self.stream:
            self.stream.close()
            self.stream = None

        # A locked log (scanner, editor) must never break logging: retry briefly,
        # otherwise keep appending to the active file and try again later (DEC-162).
        source = Path(self.baseFilename)
        if source.exists():
            try:
                self.archive_dir.mkdir(parents=True, exist_ok=True)
                index = 1
                while (self.archive_dir / f"{source.stem}_{index:03d}.log").exists():
                    index += 1
                target = self.archive_dir / f"{source.stem}_{index:03d}.log"
                for attempt in range(5):
                    try:
                        shutil.move(str(source), target)
                        break
                    except OSError:
                        if attempt == 4:
                            raise
                        time.sleep(0.1 * (attempt + 1))
                self._rollover_retry_at = 0.0
            except OSError:
                self._rollover_retry_at = time.monotonic() + self.ROLLOVER_RETRY_SECONDS

        try:
            archived_logs = sorted(
                self.archive_dir.glob("*.log"),
                key=lambda path: path.stat().st_mtime,
            )
            for archived_log in archived_logs[:-self.archive_count]:
                try:
                    archived_log.unlink(missing_ok=True)
                except OSError:
                    pass
        except OSError:
            pass

        if not self.delay:
            self.stream = self._open()


class JsonLogFormatter(logging.Formatter):

    def format(
        self,
        record,
    ):

        event = record.getMessage()

        details = getattr(
            record,
            "details",
            None,
        ) or {}

        source = (
            f"{record.filename}"
            f":{record.funcName}"
            f":{record.lineno}"
        )

        payload = {
            "timestamp": self.formatTime(
                record,
                "%Y-%m-%dT%H:%M:%S%z",
            ),
            "severity": record.levelname,
            "module": getattr(
                record,
                "botstudio_module",
                record.name,
            ),
            "source": source,
            "profile": getattr(
                record,
                "profile",
                None,
            ),
            "game": getattr(
                record,
                "game",
                None,
            ),
            "emulator_instance": getattr(
                record,
                "emulator_instance",
                None,
            ),
            "function_id": getattr(
                record,
                "function_id",
                None,
            ),
            "execution_id": getattr(record, "execution_id", None),
            "trace_id": getattr(record, "trace_id", None),
            "parent_id": getattr(record, "parent_id", None),
            "event_id": getattr(record, "event_id", None),
            "event": event,
            "message": self._compose(
                event,
                details,
                source,
            ),
        }

        if details:
            payload["details"] = details

        if record.exc_info:
            payload["exception"] = self.formatException(
                record.exc_info
            )

        return json.dumps(
            payload,
            ensure_ascii=False,
        )

    def _compose(
        self,
        event: str,
        details: dict,
        source: str,
    ) -> str:
        # the message alone must tell a developer what broke and what happens next
        if not details:
            return f"{event} | at {source}"

        rendered = " ".join(
            f"{key}={self._render(value)}"
            for key, value in details.items()
        )

        return f"{event} | {rendered} | at {source}"

    def _render(
        self,
        value,
    ) -> str:

        if value is None or isinstance(
            value,
            (str, int, float, bool),
        ):
            return str(value)

        return json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
        )


class BotStudioLogger:

    def __init__(
        self,
        module: str,
        level_name: str | None = None,
    ):

        LOG_DIR.mkdir(
            parents=True,
            exist_ok=True,
        )

        LOG_ARCHIVE_DIR.mkdir(
            parents=True,
            exist_ok=True,
        )

        self.module = module

        self.logger = logging.getLogger(
            f"botstudio.{module}"
        )

        self.logger.setLevel(
            TRACE_LEVEL
        )

        self.logger.propagate = False

        settings = AppConfigService().get_studio_settings()

        if not self.logger.handlers:
            max_bytes = max(1, int(settings.get("max_log_file_size_mb", 1) or 1)) * 1024 * 1024
            archive_count = max(1, int(settings.get("max_archive_files", 20) or 1))

            handler = ArchiveRotatingFileHandler(
                LOG_DIR / f"{module}.log",
                maxBytes=max_bytes,
                backupCount=archive_count,
                encoding="utf-8",
            )

            handler.setFormatter(
                JsonLogFormatter()
            )

            self.logger.addHandler(
                handler
            )

        self.set_level(level_name or settings.get("log_level", "normal"))

    def set_level(
        self,
        level_name: str,
    ):

        level = LOG_LEVELS.get(
            level_name,
            logging.INFO,
        )

        for handler in self.logger.handlers:
            handler.setLevel(
                level
            )

    def trace(
        self,
        message: str,
        **metadata,
    ):

        self._log(
            TRACE_LEVEL,
            message,
            metadata,
        )

    def debug(
        self,
        message: str,
        **metadata,
    ):

        self._log(
            logging.DEBUG,
            message,
            metadata,
        )

    def info(
        self,
        message: str,
        **metadata,
    ):

        self._log(
            logging.INFO,
            message,
            metadata,
        )

    def warning(
        self,
        message: str,
        **metadata,
    ):

        self._log(
            logging.WARNING,
            message,
            metadata,
        )

    def error(
        self,
        message: str,
        **metadata,
    ):

        self._log(
            logging.ERROR,
            message,
            metadata,
        )

    def critical(
        self,
        message: str,
        **metadata,
    ):

        self._log(
            logging.CRITICAL,
            message,
            metadata,
        )

    def _log(
        self,
        level,
        message: str,
        metadata,
    ):

        metadata = dict(metadata)
        error_context = dict(metadata)

        extra = {
            "botstudio_module": self.module,
            "profile": metadata.pop("profile", None),
            "game": metadata.pop("game", None),
            "emulator_instance": metadata.pop(
                "emulator_instance",
                None,
            ),
            "function_id": metadata.pop("function_id", None),
            "execution_id": metadata.pop("execution_id", None),
            "trace_id": metadata.pop("trace_id", None),
            "parent_id": metadata.pop("parent_id", None),
            "event_id": metadata.pop("event_id", None),
            "details": metadata or None,
        }

        self.logger.log(
            level,
            message,
            extra=extra,
            stacklevel=3,
        )
        if level >= logging.ERROR:
            _RECENT_ERROR_JOURNAL.record(
                logging.getLevelName(level),
                self.module,
                message,
                error_context,
            )


class RuntimeLogger(BotStudioLogger):

    def __init__(
        self,
        level_name: str | None = None,
    ):
        super().__init__(
            "runtime",
            level_name,
        )


class ADBLogger(BotStudioLogger):

    def __init__(
        self,
        level_name: str | None = None,
    ):
        super().__init__(
            "adb",
            level_name,
        )


class EmulatorLogger(BotStudioLogger):

    def __init__(
        self,
        level_name: str | None = None,
    ):
        super().__init__(
            "emulator",
            level_name,
        )


class LearningLogger(BotStudioLogger):

    def __init__(
        self,
        level_name: str | None = None,
    ):
        super().__init__(
            "learning",
            level_name,
        )


class SchedulerLogger(BotStudioLogger):

    def __init__(
        self,
        level_name: str | None = None,
    ):
        super().__init__(
            "scheduler",
            level_name,
        )


class FunctionLogger(BotStudioLogger):

    def __init__(
        self,
        level_name: str | None = None,
    ):
        super().__init__(
            "function",
            level_name,
        )


def get_logger(
    module: str,
    level_name: str | None = None,
):

    return BotStudioLogger(
        module,
        level_name,
    )
