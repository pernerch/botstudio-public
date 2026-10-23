from services.telegram_service import TelegramDocuments, TelegramMenu, TelegramPhoto


class TelegramControlMenu:
    PAGE_SIZE = 8
    MAIN_BUTTONS = (
        ("Status", "status"), ("Studio controls", "studio"),
        ("Runtime controls", "runtime"), ("Events", "events"),
        ("Open tasks", "tasks"), ("Emulators", "emulators"),
        ("Log files", "logs"), ("Help", "help"),
        ("Live info & screenshot", "info"), ("Statistics", "statistics"),
    )

    def __init__(self, service, command_handler):
        self.service = service
        self.command_handler = command_handler

    def handle(self, command, arguments):
        if not isinstance(arguments, list):
            return self._with_navigation("Invalid command arguments.")
        if command in {"start", "menu"}:
            if arguments:
                return self._with_navigation(f"Usage: /{command}")
            return TelegramMenu("🤖 BotStudio • Main Menu\nChoose a function.", self.MAIN_BUTTONS)
        if command in {"studio", "runtime"}:
            if arguments:
                return self._with_navigation(f"Usage: /{command}")
            buttons = (
                (("Start Studio", "studio_start"),
                 ("Stop Studio", "studio_stop"),
                 ("Restart Studio", "studio_restart"))
                if command == "studio"
                else (("Start Runtime", "runtime_start"), ("Stop Runtime", "runtime_stop"),
                      ("Pause all emulators", "runtime_pause all"),
                      ("Resume all (reinitialize)", "runtime_resume all"),
                      ("Choose emulator", "emulators"))
            )
            return TelegramMenu(
                f"{command.title()} controls. Actions require confirmation.",
                buttons + (("Status", "status"), ("Main Menu", "menu")),
            )
        if command in {"events", "tasks", "emulators"}:
            return self._list(command, arguments)
        if command == "menu_event":
            return self._event(arguments)
        if command == "emulator_restart":
            if len(arguments) != 1:
                return self._with_navigation("Select an instance from Emulators.")
            result = self.service.runtime_command(
                "emulator.restart", {"instance": arguments[0]},
            )
            error = self._error(result)
            if error:
                return self._with_navigation(error)
            operation_id = result.get("operation_id")
            if not isinstance(operation_id, str) or not operation_id:
                return self._with_navigation("Studio returned an invalid operation ID.")
            return TelegramMenu(
                "Emulator maintenance accepted. Runtime will be stopped safely.",
                (("Check progress", f"operation {operation_id}"),
                 ("Emulators", "emulators"), ("Main Menu", "menu")),
            )
        response = self.command_handler(command, arguments)
        if isinstance(response, (TelegramDocuments, TelegramPhoto)):
            return response
        if isinstance(response, TelegramMenu):
            return TelegramMenu(response.text, response.buttons + (("Main Menu", "menu"),))
        buttons = []
        if command == "operation" and len(arguments) == 1:
            buttons.append(("Refresh progress", f"operation {arguments[0]}"))
        if command in {"event_on", "event_off", "event_run"}:
            buttons.append(("Events", "events"))
        return self._with_navigation(response, tuple(buttons))

    @staticmethod
    def _error(result):
        if not isinstance(result, dict):
            return "Studio returned an invalid control response."
        if result.get("state") in {"failed", "busy"} or result.get("accepted") is False:
            return result.get("error") or "Studio is busy. Try again shortly."
        return None

    @staticmethod
    def _with_navigation(text, buttons=()):
        if not isinstance(text, str) or not text.strip():
            raise ValueError("Telegram command returned an invalid result")
        return TelegramMenu(text, buttons + (("Status", "status"), ("Main Menu", "menu")))

    def _list(self, command, arguments):
        if len(arguments) > 1 or (
            arguments and (not arguments[0].isascii() or not arguments[0].isdigit())
        ):
            return self._with_navigation(f"Usage: /{command} [page]")
        page = int(arguments[0]) if arguments else 1
        result = self.service.runtime_command({
            "events": "events.list", "tasks": "tasks.list", "emulators": "emulators.list",
        }[command])
        error = self._error(result)
        if error:
            return self._with_navigation(error)
        entries = result.get(command, [])
        pages = max(1, (len(entries) + self.PAGE_SIZE - 1) // self.PAGE_SIZE)
        if not 1 <= page <= pages:
            return self._with_navigation(f"Page must be between 1 and {pages}.")
        lines = [f"{command.title()} | page {page}/{pages}"]
        buttons = []
        for entry in entries[(page - 1) * self.PAGE_SIZE:page * self.PAGE_SIZE]:
            if command == "events":
                label = (
                    f"{entry['profile_name'][:24]} / {entry['event_name'][:24]} "
                    f"| {'ON' if entry['enabled'] else 'OFF'}"
                )
                lines.append(label)
                buttons.append((label, f"menu_event {entry['event_id']} {entry['profile_id']}"))
            elif command == "emulators":
                label = f"Instance {entry['instance']}: {'running' if entry['running'] else 'stopped'}"
                automation = entry.get("automation", "unknown")
                lines.append(f"{label} | automation: {automation}")
                if entry.get("pause_error"):
                    lines.append(f"Resume failed: {entry['pause_error']}")
                buttons.append((f"Restart {label}", f"emulator_restart {entry['instance']}"))
                buttons.append((f"Pause instance {entry['instance']}", f"runtime_pause {entry['instance']}"))
                buttons.append((f"Resume instance {entry['instance']} (init)", f"runtime_resume {entry['instance']}"))
            else:
                lines.append(
                    f"{entry['profile_name'][:32]} / {entry['event_name'][:32]} / "
                    f"{entry['task_name'][:32]} | {entry['task_status']}"
                )
        if not entries:
            lines.append("No entries available.")
        if result.get("truncated"):
            lines.append("Studio list limit reached. See BotStudio for additional entries.")
        if page > 1:
            buttons.append(("Previous page", f"{command} {page - 1}"))
        if page < pages:
            buttons.append(("Next page", f"{command} {page + 1}"))
        buttons.extend((("Refresh", f"{command} {page}"), ("Main Menu", "menu")))
        return TelegramMenu("\n".join(lines), tuple(buttons))

    def _event(self, arguments):
        if len(arguments) != 2:
            return self._with_navigation("Select an event from Events.")
        result = self.service.runtime_command("events.list")
        error = self._error(result)
        if error:
            return self._with_navigation(error)
        matches = [
            entry for entry in result.get("events", [])
            if entry["event_id"] == arguments[0] and entry["profile_id"] == arguments[1]
        ]
        if len(matches) != 1:
            return self._with_navigation(
                "Event no longer available. Refresh Events.",
                (("Events", "events"),),
            )
        entry = matches[0]
        reference = f"{entry['event_id']} {entry['profile_id']}"
        return TelegramMenu(
            f"{entry['profile_name']} / {entry['event_name']}\n"
            f"Enabled: {'yes' if entry['enabled'] else 'no'} | State: {entry['status']}",
            (("Enable", f"event_on {reference}"),
             ("Disable", f"event_off {reference}"),
             ("Run now", f"event_run {reference}"),
             ("Events", "events"), ("Main Menu", "menu")),
        )
