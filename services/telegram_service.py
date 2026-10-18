import json
import logging
import os
import queue
import threading
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from time import monotonic, time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from uuid import uuid4


@dataclass(frozen=True)
class TelegramDocument:
    filename: str
    content: bytes


@dataclass(frozen=True)
class TelegramDocuments:
    documents: tuple[TelegramDocument, ...]


@dataclass(frozen=True)
class TelegramMenu:
    text: str
    buttons: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class TelegramPhoto:
    content: bytes
    caption: str
    menu: TelegramMenu


TelegramFileList = TelegramMenu


class TelegramApiError(RuntimeError):
    def __init__(self, description, retry_after=None):
        super().__init__(description)
        self.retry_after = retry_after


class TelegramService:
    API_TIMEOUT_SECONDS = 6
    LONG_POLL_SECONDS = 2
    MAX_RETRY_DELAY_SECONDS = 60
    MAX_SEND_RETRIES = 2
    MAX_MESSAGE_LENGTH = 3500
    MAX_NOTIFICATION_QUEUE_SIZE = 32
    MAX_MESSAGE_AGE_SECONDS = 120
    MAX_COMMANDS_PER_MINUTE = 10
    MAX_COMMAND_ARGUMENTS = 2
    MAX_COMMAND_ARGUMENT_LENGTH = 128
    MAX_DOCUMENT_BYTES = 4 * 1024 * 1024
    MAX_DOCUMENTS_PER_COMMAND = 2
    MAX_MENU_BUTTONS = 32
    MAX_CALLBACKS = 256
    ACK_TIMEOUT_SECONDS = 2
    WORKER_STOP_TIMEOUT_SECONDS = 30
    MENU_LABEL_COMMANDS = {
        "Menu": "menu", "Status": "status", "Studio": "studio",
        "Runtime": "runtime", "Events": "events", "Tasks": "tasks",
        "Emulators": "emulators", "Logs": "logs", "Help": "help",
        "Main Menu": "menu", "Info": "info", "Statistics": "statistics",
    }
    COMMAND_ICONS = {
        "menu": "🏠", "start": "🏠", "status": "ℹ️",
        "studio": "🖥️", "runtime": "🤖", "events": "📅",
        "tasks": "📋", "emulators": "📱", "logs": "📄", "log": "📥",
        "help": "❓", "studio_start": "▶️", "runtime_start": "▶️",
        "studio_stop": "⏹️", "runtime_stop": "⏹️", "studio_restart": "🔄",
        "emulator_restart": "🔄", "operation": "🔎", "menu_event": "📅",
        "event_on": "✅", "event_off": "⛔", "event_run": "⚡",
        "info": "📸", "statistics": "📊",
    }
    MAIN_KEYBOARD_ROWS = (
        (("ℹ️ Status", "status"), ("🖥️ Studio", "studio")),
        (("🤖 Runtime", "runtime"), ("📅 Events", "events")),
        (("📋 Tasks", "tasks"), ("📱 Emulators", "emulators")),
        (("📸 Info", "info"), ("📊 Statistics", "statistics")),
        (("📄 Logs", "logs"), ("❓ Help", "help")),
    )
    CONFIRM_COMMANDS = {
        "studio_start", "studio_stop", "studio_restart",
        "runtime_start", "runtime_stop", "emulator_restart",
        "event_on", "event_off", "event_run",
    }

    def __init__(
        self,
        token,
        allowed_user_ids,
        allowed_chat_ids,
        command_handler,
        logger=None,
        api_call=None,
        clock=None,
        unix_time=None,
        state_file=None,
    ):
        if not isinstance(token, str) or not token.strip():
            raise ValueError("Telegram token is required")
        self.allowed_user_ids = self._validate_ids(
            allowed_user_ids,
            "allowed_user_ids",
        )
        self.allowed_chat_ids = self._validate_ids(
            allowed_chat_ids,
            "allowed_chat_ids",
        )
        if not callable(command_handler):
            raise ValueError("command_handler must be callable")

        self._token = token.strip()
        self.command_handler = command_handler
        self.logger = logger or logging.getLogger("botstudio.telegram")
        self._api_call_override = api_call
        self.clock = clock or monotonic
        self.unix_time = unix_time or time
        self.state_file = Path(state_file) if state_file else self._default_state_file()
        self._stop_event = threading.Event()
        self._thread = None
        self._command_thread = None
        self._notification_thread = None
        self._command_queue = queue.Queue(maxsize=self.MAX_NOTIFICATION_QUEUE_SIZE)
        self._send_lock = threading.Lock()
        self._offset = None
        self._last_send_at = None
        self._lock = threading.RLock()
        self._last_error = None
        self._state = "stopped"
        self._command_times = deque()
        self._rate_notice_times = deque()
        self._callbacks = {}
        self._notification_queue = queue.Queue(
            maxsize=self.MAX_NOTIFICATION_QUEUE_SIZE
        )
        self._offset = self._load_offset()

    @classmethod
    def from_environment(
        cls,
        command_handler,
        logger=None,
        environ=None,
        **kwargs,
    ):
        environ = os.environ if environ is None else environ
        token = environ.get("BOTSTUDIO_TELEGRAM_TOKEN", "").strip()
        raw_users = environ.get("BOTSTUDIO_TELEGRAM_ALLOWED_USER_IDS", "").strip()
        raw_chats = environ.get("BOTSTUDIO_TELEGRAM_ALLOWED_CHAT_IDS", "").strip()
        configured = any((token, raw_users, raw_chats))
        if not configured:
            return None
        if not token or not raw_users or not raw_chats:
            raise ValueError(
                "Telegram requires a token and both allowed-ID lists"
            )

        return cls(
            token=token,
            allowed_user_ids=raw_users.split(","),
            allowed_chat_ids=raw_chats.split(","),
            command_handler=command_handler,
            logger=logger,
            **kwargs,
        )

    @staticmethod
    def _validate_ids(values, name):
        if isinstance(values, (str, bytes)):
            values = [values]
        normalized = set()
        for value in values or []:
            text = str(value).strip()
            if not text or not text.lstrip("-").isdigit():
                raise ValueError(f"{name} must contain integer IDs")
            normalized.add(int(text))
        if not normalized:
            raise ValueError(f"{name} must not be empty")
        return frozenset(normalized)

    @staticmethod
    def _default_state_file():
        roaming = os.environ.get("APPDATA")
        base = Path(roaming) if roaming else Path.home() / ".config"
        return base / "BotStudio" / "watchdog" / "telegram-offset.json"

    def _load_offset(self):
        if not self.state_file.exists():
            return None
        try:
            with self.state_file.open("r", encoding="utf-8") as handle:
                state = json.load(handle)
        except (OSError, json.JSONDecodeError) as ex:
            raise ValueError("Telegram update state cannot be read") from ex
        offset = state.get("offset") if isinstance(state, dict) else None
        if type(offset) is not int or offset < 0:
            raise ValueError("Telegram update state is invalid")
        return offset

    def _save_offset(self, offset):
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_file.with_name(
            f"{self.state_file.name}.{threading.get_ident()}.tmp"
        )
        try:
            with temporary.open("w", encoding="utf-8") as handle:
                json.dump({"offset": offset}, handle)
            os.replace(temporary, self.state_file)
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                self.logger.error("Unable to remove Telegram state temporary file")

    @property
    def state(self):
        with self._lock:
            return {
                "state": self._state,
                "last_error": self._last_error,
                "running": bool(self._thread and self._thread.is_alive()),
            }

    def start(self):
        with self._lock:
            if self._thread and self._thread.is_alive():
                return self.state
            if any(thread and thread.is_alive() for thread in (self._command_thread, self._notification_thread)):
                raise RuntimeError("Previous Telegram workers are still stopping")
            self._stop_event.clear()
            self._state = "starting"
            self._last_error = None
            self._command_thread = threading.Thread(
                target=self._command_loop, name="BotStudioTelegramCommands", daemon=True,
            )
            self._notification_thread = threading.Thread(
                target=self._notification_loop, name="BotStudioTelegramNotifications", daemon=True,
            )
            self._command_thread.start()
            self._notification_thread.start()
            self._thread = threading.Thread(
                target=self._poll_loop,
                name="BotStudioTelegram",
                daemon=True,
            )
            self._thread.start()
        return self.state

    def stop(self):
        self._stop_event.set()
        deadline = monotonic() + self.WORKER_STOP_TIMEOUT_SECONDS
        for thread in (self._thread, self._command_thread, self._notification_thread):
            if thread and thread is not threading.current_thread():
                thread.join(timeout=max(0, deadline - monotonic()))
                if thread.is_alive():
                    raise TimeoutError(f"Telegram thread did not stop: {thread.name}")
        with self._lock:
            self._thread = None
            self._command_thread = None
            self._notification_thread = None
            self._state = "stopped"
            self._callbacks.clear()
        while True:
            try:
                self._notification_queue.get_nowait()
            except queue.Empty:
                break
        while True:
            try:
                self._command_queue.get_nowait()
            except queue.Empty:
                break
        return self.state

    def send_notification(self, text):
        if not isinstance(text, str) or not text.strip():
            raise ValueError("Notification text must not be empty")
        try:
            self._notification_queue.put_nowait(
                (
                    text.strip()[: self.MAX_MESSAGE_LENGTH],
                    tuple(sorted(self.allowed_chat_ids)),
                )
            )
        except queue.Full:
            self.logger.warning("Telegram notification queue is full")
            return False
        return True

    def poll_once(self):
        result = self._api_call(
            "getUpdates",
            {
                "offset": self._offset,
                "timeout": self.LONG_POLL_SECONDS,
                "allowed_updates": ["message", "callback_query"],
            },
        )
        if not isinstance(result, list):
            raise TelegramApiError("Telegram returned invalid updates")

        for update in result:
            if self._stop_event.is_set():
                return
            if not isinstance(update, dict):
                continue
            update_id = update.get("update_id")
            if type(update_id) is not int:
                continue
            if self._offset is not None and update_id < self._offset:
                continue

            next_offset = update_id + 1
            self._save_offset(next_offset)
            self._offset = next_offset
            self._handle_update(update)

    def test_connection(self):
        return self.probe_connection(self._token, self._api_call_override)

    @classmethod
    def probe_connection(cls, token, api_call=None):
        if not isinstance(token, str) or not token.strip():
            raise ValueError("Telegram token is required")
        probe = object.__new__(cls)
        probe._token = token.strip()
        probe._api_call_override = api_call
        result = cls._api_call(probe, "getMe", {})
        if not isinstance(result, dict) or not isinstance(
            result.get("username"),
            str,
        ):
            raise TelegramApiError("Telegram returned an invalid bot identity")
        return result["username"]

    def _handle_update(self, update):
        callback = update.get("callback_query")
        if isinstance(callback, dict):
            self._handle_menu_callback(callback)
            return
        if self._stop_event.is_set():
            return
        message = update.get("message")
        if not isinstance(message, dict):
            return
        chat = message.get("chat")
        sender = message.get("from")
        if not isinstance(chat, dict) or not isinstance(sender, dict):
            return
        try:
            chat_id = int(chat.get("id"))
            user_id = int(sender.get("id"))
        except (TypeError, ValueError):
            return
        if (
            chat_id not in self.allowed_chat_ids
            or user_id not in self.allowed_user_ids
        ):
            self.logger.warning("Rejected unauthorized Telegram control message")
            return

        text = message.get("text")
        if isinstance(text, str):
            labels = {
                label: command for row in self.MAIN_KEYBOARD_ROWS for label, command in row
            }
            labels["🏠 Main Menu"] = "menu"
            labels.update(self.MENU_LABEL_COMMANDS)
            if text.strip() in labels:
                text = "/" + labels[text.strip()]
        parsed_command = self._parse_command(text)
        if parsed_command is None:
            return
        command, arguments = parsed_command
        message_date = message.get("date")
        if (
            type(message_date) is not int
            or self.unix_time() - message_date > self.MAX_MESSAGE_AGE_SECONDS
        ):
            self.logger.info("Ignored stale Telegram control message")
            return

        self._execute_command(command, arguments, chat_id, user_id)

    def _execute_command(self, command, arguments, chat_id, user_id):
        if self._stop_event.is_set():
            return
        if not self._allow_command():
            if self._allow_rate_notice():
                self._send_message(
                    chat_id,
                    "Too many commands. Please try again shortly.",
                    retry=False, immediate=True,
                )
            return

        self._send_message(
            chat_id, "Request received. Preparing the result...", retry=False, immediate=True,
        )
        self._dispatch_command(command, arguments, chat_id, user_id)

    def _dispatch_command(self, command, arguments, chat_id, user_id, confirmation=False):
        item = (command, arguments, chat_id, user_id, confirmation)
        if self._command_thread and self._command_thread.is_alive():
            try:
                self._command_queue.put_nowait(item)
            except queue.Full:
                self.logger.warning("Telegram command queue is full")
                self._send_message(chat_id, "Command queue full. Try again shortly.", retry=False, immediate=True)
        else:
            self._process_command(*item)

    def _command_loop(self):
        while not self._stop_event.is_set():
            try:
                item = self._command_queue.get(timeout=0.1)
            except queue.Empty:
                continue
            if self._stop_event.is_set():
                return
            try:
                self._process_command(*item)
            except Exception as error:
                self.logger.error("Telegram command delivery failed", extra={"error_type": type(error).__name__})
                self._send_message(
                    item[2], "Result delivery failed. Please try the command again.", retry=False,
                )

    def _notification_loop(self):
        while not self._stop_event.is_set():
            try:
                self._deliver_queued_notification(max_chats=1)
            except Exception as error:
                self.logger.error("Telegram notification delivery failed", extra={"error_type": type(error).__name__})
            self._stop_event.wait(0.1)

    def _process_command(self, command, arguments, chat_id, user_id, confirmation=False):
        if self._stop_event.is_set():
            return
        if confirmation:
            self._send_menu(chat_id, user_id, arguments, confirmation=True)
            return
        try:
            response = self.command_handler(command, arguments)
        except Exception as ex:
            self.logger.error(
                "Telegram command failed",
                extra={"command": command, "error_type": type(ex).__name__},
            )
            response = "Command failed. Check the watchdog log."
        if isinstance(response, TelegramPhoto):
            if not self._send_photo(chat_id, response):
                self._send_message(chat_id, "Screenshot delivery failed. Request /info again.")
            self._send_menu(chat_id, user_id, response.menu)
            return
        if isinstance(response, TelegramDocuments):
            if not 1 <= len(response.documents) <= self.MAX_DOCUMENTS_PER_COMMAND:
                self.logger.error("Rejected invalid Telegram document batch")
                self._send_message(chat_id, "Invalid log selection. Select files with /logs.")
                return
            self.logger.info(
                "Telegram log download requested",
                extra={"command": command, "document_count": len(response.documents)},
            )
            for document in response.documents:
                if not self._send_document(chat_id, document):
                    self._send_message(
                        chat_id,
                        "Log delivery failed or was cancelled. Select the file again with /logs.",
                    )
                    return
            self.logger.info(
                "Telegram log download delivered",
                extra={"document_count": len(response.documents)},
            )
            return
        if isinstance(response, TelegramMenu):
            self._send_menu(chat_id, user_id, response)
            return
        if not isinstance(response, str) or not response.strip():
            response = "Command completed."
        self._send_message(chat_id, response[: self.MAX_MESSAGE_LENGTH])

    def _register_button(self, chat_id, user_id, command, needs_confirmation):
        with self._lock:
            return self._register_button_locked(chat_id, user_id, command, needs_confirmation)

    def _register_button_locked(self, chat_id, user_id, command, needs_confirmation):
        parsed = self._parse_command("/" + command)
        if parsed is None or any(not argument for argument in parsed[1]):
            raise ValueError("Invalid Telegram menu command")
        now = self.clock()
        self._callbacks = {
            key: value for key, value in self._callbacks.items()
            if value["expires_at"] > now
        }
        while len(self._callbacks) >= self.MAX_CALLBACKS:
            del self._callbacks[next(iter(self._callbacks))]
        callback_data = "menu_" + uuid4().hex
        self._callbacks[callback_data] = {
            "chat_id": chat_id,
            "user_id": user_id,
            "command": parsed[0],
            "arguments": parsed[1],
            "expires_at": now + self.MAX_MESSAGE_AGE_SECONDS,
            "needs_confirmation": needs_confirmation,
            "group": uuid4().hex,
            "description": command,
        }
        return callback_data

    def _send_menu(self, chat_id, user_id, menu, confirmation=False):
        if self._stop_event.is_set():
            return False
        if len(menu.buttons) > self.MAX_MENU_BUTTONS:
            raise ValueError("Telegram menu contains too many buttons")
        keyboard = []
        group = uuid4().hex
        for label, command in menu.buttons:
            command_name = command.split()[0]
            data = self._register_button(
                chat_id, user_id, command,
                not confirmation and command_name in self.CONFIRM_COMMANDS,
            )
            with self._lock:
                if confirmation:
                    self._callbacks[data]["group"] = group
                self._callbacks[data]["description"] = f"{label}\n{menu.text[:1500]}"
            keyboard.append([{"text": self._button_text(label, command, confirmation)[:64], "callback_data": data}])
        return self._send_response(
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": menu.text[:self.MAX_MESSAGE_LENGTH],
                "reply_markup": {"inline_keyboard": keyboard},
            },
        )

    @classmethod
    def _button_text(cls, label, command, confirmation=False):
        command_name = command.split()[0]
        if command_name == "menu":
            return "🏠 Main Menu"
        if confirmation:
            return f"✅ {label}"
        if label == "Previous page":
            icon = "⬅️"
        elif label == "Next page":
            icon = "➡️"
        elif label.startswith("Refresh"):
            icon = "🔄"
        else:
            icon = cls.COMMAND_ICONS.get(command_name)
        return f"{icon} {label}" if icon else label

    def _handle_menu_callback(self, callback):
        message = callback.get("message")
        sender = callback.get("from")
        if not isinstance(message, dict) or not isinstance(sender, dict):
            return
        chat = message.get("chat", {})
        if (
            not isinstance(chat, dict)
            or type(chat.get("id")) is not int
            or type(sender.get("id")) is not int
            or chat["id"] not in self.allowed_chat_ids
            or sender["id"] not in self.allowed_user_ids
        ):
            self.logger.warning("Rejected unauthorized Telegram menu selection")
            return
        data = callback.get("data")
        callback_id = callback.get("id")
        if not isinstance(callback_id, str) or not callback_id:
            self.logger.warning("Rejected Telegram menu selection without callback ID")
            return
        if self._stop_event.is_set():
            return
        with self._lock:
            selection = self._callbacks.get(data) if isinstance(data, str) else None
        fresh = (
            selection is not None
            and selection["chat_id"] == chat["id"]
            and selection["user_id"] == sender["id"]
            and selection["expires_at"] > self.clock()
        )
        if not fresh:
            self.logger.warning("Rejected expired or unavailable Telegram menu selection")
        self._api_call(
            "answerCallbackQuery",
            {
                "callback_query_id": callback_id,
                "text": "Preparing selection..." if fresh else "Selection expired or unavailable. Open Main Menu with /menu.",
            },
            timeout_seconds=self.ACK_TIMEOUT_SECONDS,
        )
        if fresh:
            if self._stop_event.is_set():
                return
            if selection["expires_at"] <= self.clock():
                self._send_message(chat["id"], "Selection expired. Open Main Menu with /menu.")
                return
            with self._lock:
                self._callbacks = {
                    key: value for key, value in self._callbacks.items()
                    if value["group"] != selection["group"]
                }
            if selection["needs_confirmation"]:
                if not self._allow_command():
                    self._send_message(chat["id"], "Too many commands. Please try again shortly.")
                    return
                command = " ".join([selection["command"], *selection["arguments"]])
                self._dispatch_command(
                    selection["command"],
                    TelegramMenu(
                        f"Confirm action: {selection['description']}\n"
                        "This may change or interrupt Studio/Runtime work.",
                        (("Confirm", command), ("Main Menu", "menu")),
                    ),
                    chat["id"], sender["id"], confirmation=True,
                )
            else:
                self._execute_command(
                    selection["command"], selection["arguments"],
                    chat["id"], sender["id"],
                )

    def _allow_command(self):
        now = self.clock()
        cutoff = now - 60
        while self._command_times and self._command_times[0] <= cutoff:
            self._command_times.popleft()
        if len(self._command_times) >= self.MAX_COMMANDS_PER_MINUTE:
            return False
        self._command_times.append(now)
        return True

    def _allow_rate_notice(self):
        now = self.clock()
        cutoff = now - 60
        while self._rate_notice_times and self._rate_notice_times[0] <= cutoff:
            self._rate_notice_times.popleft()
        if self._rate_notice_times:
            return False
        self._rate_notice_times.append(now)
        return True

    @staticmethod
    def _parse_command(text):
        if not isinstance(text, str):
            return None
        parts = text.strip().split()
        if not parts or not parts[0].startswith("/"):
            return None
        command = parts[0][1:].split("@", 1)[0].lower()
        if not command or not command.replace("_", "").isalnum():
            return None
        arguments = parts[1:]
        if len(arguments) > TelegramService.MAX_COMMAND_ARGUMENTS:
            return command, [""] * (TelegramService.MAX_COMMAND_ARGUMENTS + 1)
        if any(
            not argument
            or len(argument) > TelegramService.MAX_COMMAND_ARGUMENT_LENGTH
            or not all(character.isalnum() or character in "-_" for character in argument)
            for argument in arguments
        ):
            return command, [""] * (TelegramService.MAX_COMMAND_ARGUMENTS + 1)
        return command, arguments

    def _send_message(self, chat_id, text, *, retry=True, immediate=False):
        return self._send_response(
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": text,
                "disable_web_page_preview": True,
                "reply_markup": {
                    "keyboard": [
                        [{"text": label} for label, _command in row]
                        for row in self.MAIN_KEYBOARD_ROWS
                    ],
                    "resize_keyboard": True,
                    "is_persistent": True,
                },
            },
            retry=retry,
            immediate=immediate,
        )

    def _send_photo(self, chat_id, photo):
        if not isinstance(photo.content, bytes) or not 0 < len(photo.content) <= self.MAX_DOCUMENT_BYTES:
            self.logger.error("Rejected invalid Telegram screenshot")
            return False
        return self._send_response(
            "sendPhoto", {"chat_id": chat_id, "photo": photo, "caption": photo.caption[:1024]},
        )

    def _send_document(self, chat_id, document):
        if (
            not isinstance(document, TelegramDocument)
            or not isinstance(document.content, bytes)
            or not 0 < len(document.content) <= self.MAX_DOCUMENT_BYTES
            or not isinstance(document.filename, str)
            or not document.filename.endswith(".log")
            or not all(
                character.isascii() and (character.isalnum() or character in "_.-")
                for character in document.filename
            )
        ):
            self.logger.error("Rejected invalid Telegram log document")
            return False
        return self._send_response(
            "sendDocument",
            {"chat_id": chat_id, "document": document},
        )

    def _send_response(self, method, parameters, *, retry=True, immediate=False):
        if immediate:
            if self._stop_event.is_set():
                return False
            try:
                self._api_call(method, parameters, timeout_seconds=self.ACK_TIMEOUT_SECONDS)
                return True
            except Exception as error:
                self.logger.warning("Telegram acknowledgement failed", extra={"error_type": type(error).__name__})
                return False
        while not self._stop_event.is_set():
            if self._send_lock.acquire(timeout=0.05):
                try:
                    return self._send_response_serialized(method, parameters, retry=retry)
                finally:
                    self._send_lock.release()
        return False

    def _send_response_serialized(self, method, parameters, *, retry=True):
        if self._stop_event.is_set():
            return False
        retries = self.MAX_SEND_RETRIES if retry else 0
        for attempt in range(retries + 1):
            elapsed = (
                self.clock() - self._last_send_at
                if self._last_send_at is not None
                else 1
            )
            delay = max(0, 1 - elapsed)
            if delay and self._stop_event.wait(delay):
                return False
            try:
                self._api_call(method, parameters)
                self._last_send_at = self.clock()
                return True
            except TelegramApiError as ex:
                if attempt >= retries:
                    self.logger.warning(
                        "Telegram response delivery failed",
                        extra={"error_type": type(ex).__name__},
                    )
                    return False
                delay = self._retry_delay(
                    ex.retry_after,
                    min(2 ** attempt, self.MAX_RETRY_DELAY_SECONDS),
                )
                if self._stop_event.wait(min(delay, self.MAX_RETRY_DELAY_SECONDS)):
                    return False
            except Exception as ex:
                self.logger.warning(
                    "Telegram response delivery failed",
                    extra={"error_type": type(ex).__name__},
                )
                return False

    def _poll_loop(self):
        delay = 1
        self._set_state("running", None)
        while not self._stop_event.is_set():
            try:
                self.poll_once()
                delay = 1
                self._set_state("running", None)
            except Exception as ex:
                retry_after = (
                    ex.retry_after
                    if isinstance(ex, TelegramApiError)
                    else None
                )
                self._set_state("degraded", type(ex).__name__)
                self.logger.warning(
                    "Telegram polling failed; retrying",
                    extra={"error_type": type(ex).__name__},
                )
                self._stop_event.wait(
                    min(self._retry_delay(retry_after, delay), self.MAX_RETRY_DELAY_SECONDS)
                )
                delay = min(delay * 2, self.MAX_RETRY_DELAY_SECONDS)
        self._set_state("stopped", self._last_error)

    def _deliver_queued_notification(self, *, max_chats=None):
        try:
            text, chat_ids = self._notification_queue.get_nowait()
        except queue.Empty:
            return
        for index, chat_id in enumerate(chat_ids):
            if self._stop_event.is_set():
                return
            delivered = self._send_message(chat_id, text, retry=False)
            if not delivered or (max_chats is not None and index + 1 >= max_chats):
                pending = chat_ids[index + 1:] if delivered else chat_ids[index:]
                if not pending:
                    return
                try:
                    self._notification_queue.put_nowait(
                        (text, pending)
                    )
                except queue.Full:
                    self.logger.warning(
                        "Telegram notification could not be requeued"
                    )
                return

    @staticmethod
    def _retry_delay(value, default):
        try:
            delay = float(value)
        except (TypeError, ValueError):
            return default
        if delay <= 0:
            return default
        return delay

    def _set_state(self, state, error):
        with self._lock:
            self._state = state
            self._last_error = error

    def _api_call(self, method, parameters, *, timeout_seconds=None):
        if self._api_call_override:
            return self._api_call_override(method, parameters)

        url = f"https://api.telegram.org/bot{self._token}/{method}"
        request_parameters = {
            key: value
            for key, value in parameters.items()
            if value is not None
        }
        for key in ("allowed_updates", "reply_markup"):
            if key in request_parameters:
                request_parameters[key] = json.dumps(request_parameters[key])
        photo = request_parameters.pop("photo", None)
        document = request_parameters.pop("document", None)
        attachment = photo or document
        if attachment is not None:
            boundary = f"BotStudio{uuid4().hex}"
            chunks = []
            for key, value in request_parameters.items():
                chunks.append(
                    (
                        f"--{boundary}\r\n"
                        f'Content-Disposition: form-data; name="{key}"\r\n\r\n'
                        f"{value}\r\n"
                    ).encode("utf-8")
                )
            chunks.extend([
                (
                    f"--{boundary}\r\n"
                    f'Content-Disposition: form-data; name="{"photo" if photo else "document"}"; '
                    f'filename="{"emulator.jpg" if photo else document.filename}"\r\n'
                    f'Content-Type: {"image/jpeg" if photo else "text/plain; charset=utf-8"}\r\n\r\n'
                ).encode("utf-8"),
                attachment.content,
                f"\r\n--{boundary}--\r\n".encode("ascii"),
            ])
            encoded = b"".join(chunks)
            content_type = f"multipart/form-data; boundary={boundary}"
        else:
            encoded = urlencode(request_parameters).encode("utf-8")
            content_type = "application/x-www-form-urlencoded"
        request = Request(
            url,
            data=encoded,
            headers={"Content-Type": content_type},
            method="POST",
        )
        try:
            with urlopen(request, timeout=timeout_seconds or self.API_TIMEOUT_SECONDS) as response:
                body = json.loads(response.read().decode("utf-8"))
        except HTTPError as ex:
            retry_after = None
            try:
                error_body = json.loads(ex.read().decode("utf-8"))
                retry_after = (
                    error_body.get("parameters", {}).get("retry_after")
                    if isinstance(error_body, dict)
                    else None
                )
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                pass
            raise TelegramApiError("Telegram HTTP request failed", retry_after) from None
        except (URLError, TimeoutError, OSError, UnicodeDecodeError, json.JSONDecodeError):
            raise TelegramApiError("Telegram network request failed") from None

        if not isinstance(body, dict) or body.get("ok") is not True:
            parameters_block = body.get("parameters", {}) if isinstance(body, dict) else {}
            retry_after = (
                parameters_block.get("retry_after")
                if isinstance(parameters_block, dict)
                else None
            )
            raise TelegramApiError("Telegram API returned an error", retry_after)
        return body.get("result")
