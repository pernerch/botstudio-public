import argparse
import base64
import logging
import signal
import sys
from concurrent.futures import ThreadPoolExecutor
from time import monotonic
from logging.handlers import RotatingFileHandler
from pathlib import Path

from PySide6.QtCore import QLockFile, QTimer
from PySide6.QtWidgets import QApplication, QSystemTrayIcon

from services.watchdog_process_service import WatchdogProcessService
from services.watchdog_ipc_service import WatchdogIpcClient
from services.telegram_service import TelegramService, TelegramMenu, TelegramPhoto
from services.telegram_log_service import TelegramLogService
from services.telegram_control_menu import TelegramControlMenu
from services.app_config_service import AppConfigService
from services.windows_credential_service import WindowsCredentialService
from services.windows_process_handle import WindowsProcessHandle
from services.watchdog_autostart_service import WatchdogAutoStartService
from ui.watchdog_tray import WatchdogTray
from ui.i18n.translator import set_language


def _configure_logging(project_root):
    log_dir = project_root / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        log_dir / "watchdog.log",
        maxBytes=1_000_000,
        backupCount=5,
        encoding="utf-8",
    )
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s %(message)s",
        )
    )
    logger = logging.getLogger("botstudio.watchdog")
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    return logger


def _handle_telegram_command(command_name, arguments, service):
    if not isinstance(arguments, list):
        return "Invalid command arguments."

    no_argument_commands = {
        "help",
        "status",
        "studio_start",
        "studio_stop",
        "studio_restart",
        "runtime_start",
        "runtime_stop",
    }
    if command_name in no_argument_commands and arguments:
        return f"Usage: /{command_name}"
    if command_name in {"statistics", "stats", "info"}:
        return _monitoring_response(command_name, arguments, service)
    if command_name == "help":
        return (
            "🤖 BotStudio • Help\n\n"
            "🖥️ Studio control — Watchdog must be running\n"
            "/studio — Open Studio controls\n"
            "/studio_start — Launch BotStudio\n"
            "/studio_stop — Close Studio intentionally; no automatic restart\n"
            "/studio_restart — Restart Studio using the safe recovery path\n\n"
            "🤖 Bot control — Studio must be running\n"
            "/runtime — Open bot controls\n"
            "/runtime_start — Start the bot and check all active profiles\n"
            "/runtime_stop — Stop bot automation, keep Studio open\n\n"
            "/runtime_pause [instance|all] — Pause automation, leave emulators open\n"
            "/runtime_resume [instance|all] — Resume through fresh profile checks and Init\n\n"
            "📅 Events & tasks — Studio must be running\n"
            "/events [page] — Select an event and enable, disable or run it now\n"
            "/tasks [page] — Show queued and running tasks\n"
            "/event_on <event-id> [profile-id] — Enable an event\n"
            "/event_off <event-id> [profile-id] — Disable an event\n"
            "/event_run <event-id> [profile-id] — Make an event due for the next bot cycle\n\n"
            "📱 Emulator maintenance — Studio must be running\n"
            "/emulators [page] — List instances assigned to active profiles\n"
            "/emulator_restart <instance> — Restart one emulator safely; "
            "resume a previously running bot when safe\n"
            "/operation <operation-id> — Check maintenance progress\n\n"
            "📊 Monitoring & files\n"
            "/status — Studio, bot and Watchdog state, recovery and restarts in the last 24h\n"
            "/info [page] — Live emulator state, running Function and fresh screenshot\n"
            "/statistics [current|alltime] [page] — Task results, uptime and profile switches\n"
            "/stats — Alias for /statistics\n"
            "/logs [page] — Select individual log files, no ZIP\n"
            "/log <id> [id] — Download one or two selected files, up to 4 MiB each\n"
            "New errors are reported automatically with available task/function details and counters.\n\n"
            "🏠 Navigation & help\n"
            "/start or /menu — Open Main Menu\n"
            "/help — Show this help, even when Studio is closed\n\n"
            "💡 Use the buttons for quick access; no IDs need to be typed. "
            "Actions that change state require confirmation when selected by button.\n"
            "If Studio is closed, use Studio → Start Studio, then Runtime → Start Runtime.\n"
            "After a network outage, Telegram reconnects automatically. "
            "Interrupted tasks are not blindly replayed; check the recovery status."
        )
    if command_name in {"logs", "log"}:
        log_service = TelegramLogService(service.working_directory)
        return (
            log_service.list_files(arguments)
            if command_name == "logs"
            else log_service.download(arguments)
        )
    if command_name == "emulators":
        if arguments:
            return "Usage: /emulators"
        result = service.runtime_command("emulators.list")
        if result.get("state") in {"failed", "busy"} or result.get("accepted") is False:
            return result.get("error", "Studio is busy. Try again shortly.")
        entries = result.get("emulators", [])
        lines = [
            f"Instance {entry['instance']}: "
            f"{'running' if entry['running'] else 'stopped'}"
            for entry in entries
        ]
        if result.get("truncated"):
            lines.append("List truncated.")
        return "\n".join(lines) or "No instances assigned to enabled profiles."
    if command_name in {"emulator_restart", "operation"}:
        if len(arguments) != 1:
            return (
                "Usage: /emulator_restart <instance>. Use /emulators."
                if command_name == "emulator_restart"
                else "Usage: /operation <operation-id>"
            )
        result = service.runtime_command(
            "emulator.restart"
            if command_name == "emulator_restart"
            else "control.operation.get",
            {"instance": arguments[0]}
            if command_name == "emulator_restart"
            else {"operation_id": arguments[0]},
        )
        if result.get("state") in {"busy", "failed"} or result.get("accepted") is False:
            return result.get("error", "Studio is busy. Try again shortly.")
        if command_name == "emulator_restart":
            return (
                "Emulator maintenance accepted. Runtime will be stopped safely.\n"
                f"Check progress: /operation {result['operation_id']}"
            )
        if result.get("state") == "running":
            return "Emulator maintenance is still running."
        if result.get("state") != "completed":
            return "Studio returned an invalid operation state."
        completed = result.get("result", {})
        if not isinstance(completed, dict) or "instance" not in completed:
            return "Control operation completed."
        return (
            f"Instance {completed.get('instance', 'unknown')}: restart completed.\n"
            f"Runtime resumed: {'yes' if completed.get('runtime_resumed') else 'no'}"
        )
    if command_name == "status":
        status = service.get_status()
        try:
            studio = WatchdogIpcClient().request(
                "status",
                timeout_ms=1_000,
            )
        except Exception:
            studio = None
        runtime = (
            studio.get("runtime", {})
            if isinstance(studio, dict)
            else {}
        )
        runtime_state = runtime.get("status", "unknown")
        return (
            f"Watchdog: {status['state']} ({status['health']})\n"
            f"BotStudio: {'running' if status['studio_pid'] else 'stopped'}"
            f"{' | IPC unavailable' if studio is None else ''}\n"
            f"Runtime: {runtime_state}\n"
            f"Paused/resuming emulators: {', '.join(runtime.get('paused_emulators', {})) or 'none'}\n"
            f"Recovery: {status.get('runtime_recovery', 'unknown')}\n"
            f"Restarts last 24h: {status['restarts_last_window']}"
        )
    if command_name == "studio_start":
        result = service.start_studio()
        return f"BotStudio: {result['state']}"
    if command_name == "studio_stop":
        result = service.stop_studio()
        return f"BotStudio: {result['state']}"
    if command_name == "studio_restart":
        result = service.restart_studio()
        return f"BotStudio: {result['state']}"
    if command_name in {"runtime_start", "runtime_stop"}:
        ipc_command = (
            "runtime.start"
            if command_name == "runtime_start"
            else "runtime.stop"
        )
        result = service.runtime_command(ipc_command)
        return f"Runtime: {result.get('state', 'accepted')}"
    if command_name in {"runtime_pause", "runtime_resume"}:
        if len(arguments) > 1:
            return f"Usage: /{command_name} [instance|all]"
        target = arguments[0] if arguments else "all"
        if target != "all" and (not target.isascii() or not target.isdigit()):
            return f"Usage: /{command_name} [instance|all]"
        result = service.runtime_command(
            "runtime.pause" if command_name == "runtime_pause" else "runtime.resume",
            {} if target == "all" else {"instance": target},
        )
        if not isinstance(result, dict):
            return "Studio returned an invalid control response."
        if result.get("state") in {"busy", "failed"} or result.get("accepted") is False:
            return result.get("error") or "Studio is busy. Try again shortly."
        return (
            f"Emulator {target}: pause requested. Old automation is cancelled; "
            "wait for 'paused' in /emulators before manual use."
            if command_name == "runtime_pause" else
            f"Emulator {target}: resume requested. After old work ends, profile checks "
            "and Init run again; old Function execution is not continued."
        )
    if command_name in {"events", "tasks"}:
        if arguments:
            return f"Usage: /{command_name}"
        result = service.runtime_command(
            "events.list" if command_name == "events" else "tasks.list"
        )
        if not isinstance(result, dict):
            return "Studio returned an invalid control response."
        if result.get("state") == "busy":
            return "Studio is busy. Try again shortly."
        if result.get("state") == "failed":
            return result.get("error", "Studio control operation failed.")
        if result.get("accepted") is False:
            return result.get("error", "Studio control request was rejected.")
        key = "events" if command_name == "events" else "tasks"
        entries = result.get(key, [])
        if not entries:
            return (
                "No custom events found."
                if command_name == "events"
                else "No queued or running tasks."
            )
        lines = []
        for entry in entries[:10]:
            if command_name == "events":
                state = "ON" if entry.get("enabled") else "OFF"
                lines.append(
                    f"{entry.get('profile_name', 'Profile')[:48]} "
                    f"[{entry.get('profile_id', '')[:12]}] / "
                    f"{entry.get('event_name', 'Event')[:48]} | {state} | "
                    f"id={entry.get('event_id', '')}"
                )
            else:
                lines.append(
                    f"{entry.get('profile_name', 'Profile')[:48]} / "
                    f"{entry.get('event_name', 'Event')[:48]} / "
                    f"{entry.get('task_name', 'Task')[:48]} | "
                    f"{entry.get('task_status', 'unknown')}"
                )
        if result.get("truncated") or len(entries) > len(lines):
            lines.append("List truncated; refine the view in BotStudio.")
        return "\n".join(lines)
    if command_name in {"event_on", "event_off", "event_run"}:
        if len(arguments) not in {1, 2}:
            return (
                f"Usage: /{command_name} <event-id> [profile-id]. "
                "Use /events to find event IDs."
            )
        ipc_command = {
            "event_on": "event.enable",
            "event_off": "event.disable",
            "event_run": "event.run_now",
        }[command_name]
        result = service.runtime_command(
            ipc_command,
            {
                "event_id": arguments[0],
                **(
                    {"profile_id": arguments[1]}
                    if len(arguments) == 2
                    else {}
                ),
            },
        )
        if not isinstance(result, dict):
            return "Studio returned an invalid control response."
        if result.get("accepted") is False:
            return result.get("error") or "Studio is busy. Try again shortly."
        if result.get("state") == "failed":
            return result.get("error", "Studio control operation failed.")
        if command_name == "event_run":
            return (
                f"Event '{result.get('event_name', arguments[0])}' "
                "queued for the next Runtime scheduler cycle."
            )
        return (
            f"Event '{result.get('event_name', arguments[0])}' "
            f"{'enabled' if result.get('enabled') else 'disabled'}."
        )
    return "Unknown command. Send /help for supported commands."


def _monitoring_response(command, arguments, service):
    if command == "info":
        if len(arguments) > 1 or (arguments and not arguments[0].isascii()) or (
            arguments and not arguments[0].isdigit()
        ):
            return "Usage: /info [page]"
        page = int(arguments[0]) if arguments else 1
        result = service.runtime_command("info.get", {"page": page})
    else:
        scope = arguments[0] if arguments else "current"
        if scope not in {"current", "alltime"} or len(arguments) > 2 or (
            len(arguments) == 2 and (not arguments[1].isascii() or not arguments[1].isdigit())
        ):
            return "Usage: /statistics [current|alltime] [page]"
        page = int(arguments[1]) if len(arguments) == 2 else 1
        result = service.runtime_command("statistics.get", {"all_time": scope == "alltime", "page": page})
    if not isinstance(result, dict):
        raise ValueError("Studio returned invalid monitoring data")
    if result.get("state") in {"busy", "failed"} or result.get("accepted") is False:
        return result.get("error") or "Studio is busy. Try again shortly."
    page, pages = result["page"], result["pages"]
    buttons = []
    if command != "info":
        lines = [f"Statistics | {'All time' if result['all_time'] else 'Since reset'} | {page}/{pages}"]
        for account in result["accounts"]:
            lines.extend([
                f"\n{str(account['profile_name'])[:80]} ({str(account['game_id'])[:80]})",
                f"Tasks: {account['success']} successful / {account['failure']} failed",
                f"Uptime: {account['uptime_seconds']} s | Switches: {account['profile_switches']}",
                f"Emulator starts: {account['emulator_starts']}",
            ])
        if not result["accounts"]:
            lines.append("No statistics recorded.")
        prefix = f"statistics {scope}"
        buttons.append(("Since reset" if scope == "alltime" else "All time",
                        "statistics current" if scope == "alltime" else "statistics alltime"))
    else:
        lines = [
            f"Live Info | emulator {result['instance']} | {page}/{pages}",
            f"Runtime: {result['runtime']}",
            f"State: {result['state']}",
            f"Profile target: {str(result.get('profile_name', 'unknown'))[:80]}",
            f"Observed: {result.get('captured_at') or result['observed_at']}",
        ]
        activity = result["activity"]
        if activity:
            parents = {entry["parent_id"] for entry in activity}
            for entry in activity:
                if entry["execution_id"] not in parents:
                    lines.append(f"Running Function: {str(entry['function_name'])[:100]}")
            lines.append("Function chain: " + " > ".join(str(entry["function_name"])[:80] for entry in activity))
        else:
            lines.append("Running Function: none")
        lines.append(result["message"])
        prefix = "info"
    if page > 1:
        buttons.append(("Previous page", f"{prefix} {page - 1}"))
    if page < pages:
        buttons.append(("Next page", f"{prefix} {page + 1}"))
    buttons.extend((("Refresh", f"{prefix} {page}"), ("Main Menu", "menu")))
    text = "\n".join(lines)
    menu = TelegramMenu(text, tuple(buttons))
    if command != "info" or result["photo"] is None:
        return menu
    photo = result["photo"]
    length, chunks = photo["bytes"], photo["chunks"]
    if type(length) is not int or not 0 < length <= TelegramService.MAX_DOCUMENT_BYTES:
        raise ValueError("Invalid screenshot transfer size")
    if type(chunks) is not int or not 1 <= chunks <= 128:
        raise ValueError("Invalid screenshot chunk count")
    content = bytearray()
    deadline = monotonic() + 15
    for index in range(chunks):
        if monotonic() >= deadline:
            raise TimeoutError("Screenshot transfer timed out. Request /info again.")
        chunk = service.runtime_command("info.photo.get", {"token": photo["token"], "index": index})
        if not isinstance(chunk, dict) or not isinstance(chunk.get("content"), str):
            raise ValueError("Screenshot transfer failed")
        content.extend(base64.b64decode(chunk["content"], validate=True))
        if len(content) > length:
            raise ValueError("Screenshot transfer exceeds announced size")
    if len(content) != length:
        raise ValueError("Screenshot transfer is incomplete")
    return TelegramPhoto(bytes(content), text[:1024], menu)


def main(argv=None):
    from services.application_paths import ApplicationPaths
    from services.application_bootstrap import initialize_application

    paths = ApplicationPaths.discover()
    parser = argparse.ArgumentParser(
        description="Run BotStudio under a headless process watchdog."
    )
    parser.add_argument(
        "--working-directory",
        default=str(paths.data_root),
        help="BotStudio working directory",
    )
    parser.add_argument(
        "--studio-command",
        nargs=argparse.REMAINDER,
        help="Studio command and arguments (place after --studio-command)",
    )
    parser.add_argument(
        "--studio-pid",
        type=int,
        help="Attach to an already-running BotStudio process",
    )
    args = parser.parse_args(argv)
    command = args.studio_command
    if command is None and getattr(sys, "frozen", False):
        studio_executable = paths.program_root / "BotStudio.exe"
        if not studio_executable.is_file():
            parser.error(f"BotStudio executable must be available: {studio_executable}")
        command = [str(studio_executable)]
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        parser.error("--studio-command requires an executable")

    working_directory = Path(args.working_directory).resolve()
    try:
        initialize_application(
            ApplicationPaths(working_directory, paths.resource_root, paths.program_root),
            activate=False,
        )
    except OSError as error:
        parser.exit(1, f"BotStudio data initialization failed: {error}\n")

    app = QApplication(sys.argv[:1])
    app.setQuitOnLastWindowClosed(False)
    logger = _configure_logging(working_directory)
    watchdog_lock_path = WatchdogAutoStartService().lock_path
    watchdog_lock_path.parent.mkdir(parents=True, exist_ok=True)
    watchdog_lock = QLockFile(str(watchdog_lock_path))
    if not watchdog_lock.tryLock(0):
        logger.info("Another BotStudio watchdog instance is already running")
        return 0

    config_service = AppConfigService(
        file_name=str(working_directory / "config" / "app_config.json"),
        reinitialize_service=None,
    )
    try:
        initial_process = (
            WindowsProcessHandle(args.studio_pid)
            if args.studio_pid is not None
            else None
        )
    except OSError:
        logger.exception("Unable to attach the existing BotStudio process")
        watchdog_lock.unlock()
        return 1
    telegram = None

    def queue_telegram_notification(message):
        if telegram is not None:
            telegram.send_notification(message)

    service = WatchdogProcessService(
        studio_command=command,
        working_directory=str(working_directory),
        logger=logger,
        initial_process=initial_process,
        attached_process_factory=WindowsProcessHandle if sys.platform == "win32" else None,
        notification_handler=queue_telegram_notification,
    )

    control_menu = TelegramControlMenu(
        service,
        lambda command_name, arguments: _handle_telegram_command(
            command_name, arguments, service,
        ),
    )

    def handle_telegram_command(command_name, arguments):
        return control_menu.handle(command_name, arguments)

    telegram_configuration = None

    def reload_telegram_configuration():
        nonlocal telegram, telegram_configuration
        try:
            settings = config_service.get_studio_settings()
            set_language(settings.get("language", "en"))
            if settings["telegram_settings_configured"]:
                token = (
                    WindowsCredentialService().load()
                    if settings["telegram_enabled"]
                    else None
                )
                if token:
                    configuration = (
                        True,
                        token,
                        settings["telegram_allowed_user_ids"],
                        settings["telegram_allowed_chat_ids"],
                    )
                else:
                    configuration = (False, None, None, None)
                    if (
                        settings["telegram_enabled"]
                        and not token
                    ):
                        logger.error(
                            "Telegram is enabled but its bot token is unavailable"
                        )
            else:
                configuration = (False, None, None, None)
        except Exception as ex:
            logger.error(
                "Telegram configuration reload failed",
                extra={"error_type": type(ex).__name__},
            )
            configuration = (False, None, None, None)

        if configuration == telegram_configuration:
            return
        if telegram is not None:
            try:
                telegram.stop()
            except Exception:
                logger.exception("Unable to stop the previous Telegram service")
                return
            telegram = None
        telegram_configuration = configuration
        enabled, token, users, chats = configuration
        if not enabled:
            logger.info("Telegram is disabled")
            return
        try:
            telegram = TelegramService(
                token=token,
                allowed_user_ids=users.split(",") if isinstance(users, str) else users,
                allowed_chat_ids=chats.split(",") if isinstance(chats, str) else chats,
                command_handler=handle_telegram_command,
                logger=logger,
            )
            telegram.start()
            logger.info("Telegram control is enabled")
        except Exception as ex:
            telegram = None
            telegram_configuration = (False, None, None, None)
            logger.error(
                "Telegram could not be enabled",
                extra={"error_type": type(ex).__name__},
            )

    shutdown_requested = False

    def shutdown(_signal_number=None, _frame=None):
        nonlocal shutdown_requested
        del _signal_number, _frame
        if shutdown_requested:
            return
        shutdown_requested = True
        QTimer.singleShot(0, app.quit)

    signal.signal(signal.SIGINT, shutdown)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, shutdown)

    tray = None
    configuration_executor = ThreadPoolExecutor(
        max_workers=1, thread_name_prefix="WatchdogConfiguration",
    )
    configuration_future = None

    def refresh_configuration():
        nonlocal configuration_future
        if configuration_future is None or configuration_future.done():
            configuration_future = configuration_executor.submit(reload_telegram_configuration)

    try:
        service.start()
        reload_telegram_configuration()
        if not QSystemTrayIcon.isSystemTrayAvailable():
            logger.warning("System tray is not available yet; waiting for the desktop tray")
        tray = WatchdogTray(service, logger)
        tray.quit_requested.connect(app.quit)
        configuration_timer = QTimer()
        configuration_timer.timeout.connect(refresh_configuration)
        configuration_timer.start(1_000)
        return app.exec()
    except Exception:
        logger.exception("Headless watchdog failed to start")
        return 1
    finally:
        if tray is not None:
            tray.close()
        configuration_executor.shutdown(wait=True, cancel_futures=True)
        if telegram is not None:
            try:
                telegram.stop()
            except Exception:
                logger.exception("Telegram shutdown failed")
        try:
            service.stop()
        except Exception:
            logger.exception("Headless watchdog shutdown failed")
        watchdog_lock.unlock()


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        from services.release_self_test import run_self_test
        raise SystemExit(run_self_test("watchdog"))
    raise SystemExit(main())
