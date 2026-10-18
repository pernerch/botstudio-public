import hashlib
import os
import re
from pathlib import Path

from services.log_redaction_service import redact_log_line
from services.telegram_service import (
    TelegramDocument, TelegramDocuments, TelegramFileList, TelegramService,
)


class TelegramLogService:
    PAGE_SIZE = 10
    MAX_FILE_BYTES = TelegramService.MAX_DOCUMENT_BYTES
    _LOG_NAME = re.compile(r"[A-Za-z0-9_.-]+\.log(?:\.[1-9][0-9]*)?\Z")

    def __init__(self, working_directory):
        self.root = Path(working_directory).resolve() / "logs"

    def _entries(self):
        entries = []
        if not self.root.exists():
            return entries
        if self.root.is_symlink() or self.root.is_junction():
            raise ValueError("Log directory links are not eligible for download.")
        for directory in (self.root, self.root / "archive"):
            if not directory.exists():
                continue
            if directory.is_symlink() or directory.is_junction():
                raise ValueError("Log archive links are not eligible for download.")
            for path in directory.iterdir():
                if (
                    not self._LOG_NAME.fullmatch(path.name)
                    or path.is_symlink()
                    or not path.is_file()
                ):
                    continue
                relative = path.relative_to(self.root).as_posix()
                reference = hashlib.sha256(relative.encode("utf-8")).hexdigest()[:16]
                stat = path.stat()
                entries.append((reference, path, stat.st_size, stat.st_mtime_ns))
        return sorted(entries, key=lambda entry: (-entry[3], entry[1].name))

    def list_files(self, arguments):
        if len(arguments) > 1 or (
            arguments and (not arguments[0].isascii() or not arguments[0].isdigit())
        ):
            return "Usage: /logs [page]"
        page = int(arguments[0]) if arguments else 1
        entries = self._entries()
        pages = max(1, (len(entries) + self.PAGE_SIZE - 1) // self.PAGE_SIZE)
        if not 1 <= page <= pages:
            return f"Log page must be between 1 and {pages}."
        if not entries:
            return "No log files available."
        lines = [f"Log files | page {page}/{pages}"]
        buttons = []
        for reference, path, size, _ in entries[
            (page - 1) * self.PAGE_SIZE:page * self.PAGE_SIZE
        ]:
            kind = "archive" if path.parent != self.root or path.suffix != ".log" else "current"
            limit = " | too large to download" if size > self.MAX_FILE_BYTES else ""
            lines.append(
                f"{reference} | {path.name} | {kind} | {size / 1024:.1f} KiB{limit}"
            )
            if size <= self.MAX_FILE_BYTES:
                buttons.append((f"Download {kind}: {path.name}", f"log {reference}"))
        lines.append("Select: /log <id> [id]. Files are delivered separately, redacted.")
        if page < pages:
            lines.append(f"Next page: /logs {page + 1}")
            buttons.append(("Next page", f"logs {page + 1}"))
        if page > 1:
            buttons.append(("Previous page", f"logs {page - 1}"))
        return TelegramFileList("\n".join(lines), tuple(buttons))

    def download(self, arguments):
        if (
            not 1 <= len(arguments) <= TelegramService.MAX_DOCUMENTS_PER_COMMAND
            or any(not re.fullmatch(r"[0-9a-f]{16}", item) for item in arguments)
        ):
            return "Usage: /log <id> [id]. Use /logs to select files."
        entries = {entry[0]: entry[1] for entry in self._entries()}
        documents = []
        for reference in dict.fromkeys(arguments):
            path = entries.get(reference)
            if path is None:
                return "Selected log is no longer available. Refresh with /logs."
            # Read a bounded snapshot: active files may grow or rotate while read.
            if path.is_symlink() or path.resolve().parent not in {
                self.root.resolve(), (self.root / "archive").resolve()
            }:
                raise ValueError("Selected log path is not eligible for download.")
            with path.open("rb") as handle:
                opened = os.fstat(handle.fileno())
                selected = path.stat(follow_symlinks=False)
                if (
                    (opened.st_dev, opened.st_ino) != (selected.st_dev, selected.st_ino)
                    or self.root.is_symlink()
                    or self.root.is_junction()
                    or path.parent.is_symlink()
                    or path.parent.is_junction()
                ):
                    raise ValueError("Selected log changed during download. Refresh with /logs.")
                content = handle.read(self.MAX_FILE_BYTES + 1)
            if len(content) > self.MAX_FILE_BYTES:
                return "Selected log exceeds the 4 MiB limit. Select a smaller file with /logs."
            sanitized = "\n".join(
                redact_log_line(line)
                for line in content.decode("utf-8", errors="replace").splitlines()
            ).encode("utf-8") + b"\n"
            if len(sanitized) > self.MAX_FILE_BYTES:
                return "Redacted log exceeds the 4 MiB limit. Select a smaller file with /logs."
            name = path.name if path.suffix == ".log" else f"{path.name}.log"
            documents.append(TelegramDocument(name, sanitized))
        return TelegramDocuments(tuple(documents))
