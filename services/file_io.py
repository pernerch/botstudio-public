"""Robust file IO for persistent JSON stores (DEC-162).

On Windows a virus scanner, the search indexer or a concurrent replace can
lock a file for a few milliseconds; ``open``/``os.replace`` then raise
``PermissionError``. Such locks are transient, so every store retries with a
short backoff instead of failing the running task.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from uuid import uuid4


RETRY_ATTEMPTS = 8
RETRY_BASE_DELAY = 0.05
RETRY_MAX_DELAY = 0.5


def _retry(operation, attempts=RETRY_ATTEMPTS):
    delay = RETRY_BASE_DELAY
    for attempt in range(attempts):
        try:
            return operation()
        except FileNotFoundError:
            raise
        except PermissionError:
            if attempt == attempts - 1:
                raise
        except OSError as error:
            # ERROR_SHARING_VIOLATION / ERROR_LOCK_VIOLATION on Windows
            if getattr(error, "winerror", None) not in (32, 33) or attempt == attempts - 1:
                raise
        time.sleep(delay)
        delay = min(delay * 2, RETRY_MAX_DELAY)


def read_text(path, encoding: str = "utf-8") -> str:
    target = Path(path)
    return _retry(lambda: target.read_text(encoding=encoding))


def read_json(path):
    return json.loads(read_text(path))


def atomic_write_text(path, text: str, encoding: str = "utf-8") -> None:
    """Write via a unique temp file and replace; retries transient locks."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
    try:
        _retry(lambda: temporary.write_text(text, encoding=encoding))
        _retry(lambda: os.replace(temporary, target))
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def atomic_write_json(path, data, indent: int = 4, ensure_ascii: bool = False) -> None:
    atomic_write_text(
        path,
        json.dumps(data, indent=indent, ensure_ascii=ensure_ascii),
    )
