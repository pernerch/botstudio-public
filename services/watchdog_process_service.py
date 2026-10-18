import logging
import math
import os
import subprocess
import threading
import time
from collections import deque
from datetime import datetime, timezone
from time import monotonic

from services.watchdog_ipc_service import WatchdogIpcClient


class WatchdogProcessService:
    CONTROL_OPERATION_TIMEOUT_SECONDS = 10
    CONTROL_OPERATION_POLL_SECONDS = 0.1
    CONTROL_COMMANDS = {
        "studio.stop",
        "runtime.start",
        "runtime.stop",
        "events.list",
        "tasks.list",
        "event.enable",
        "event.disable",
        "event.run_now",
        "control.operation.get",
        "emulators.list",
        "emulator.restart",
        "statistics.get",
        "info.get",
        "info.photo.get",
    }

    def __init__(
        self,
        studio_command,
        working_directory,
        ipc_client_factory=None,
        process_factory=None,
        logger=None,
        health_interval_seconds=5,
        health_timeout_ms=1_000,
        startup_timeout_seconds=60,
        unhealthy_threshold=3,
        restart_limit=3,
        restart_window_seconds=86_400,
        backoff_base_seconds=5,
        backoff_max_seconds=300,
        terminate_timeout_seconds=3,
        clock=None,
        initial_process=None,
        notification_handler=None,
        attached_process_factory=None,
    ):
        if not studio_command or not all(
            isinstance(part, str) and part for part in studio_command
        ):
            raise ValueError("studio_command must contain non-empty strings")
        if not isinstance(working_directory, str) or not working_directory:
            raise ValueError("working_directory must be a non-empty path")
        if type(unhealthy_threshold) is not int or unhealthy_threshold < 1:
            raise ValueError("unhealthy_threshold must be a positive integer")
        if type(restart_limit) is not int or restart_limit < 1:
            raise ValueError("restart_limit must be a positive integer")
        if (
            not math.isfinite(health_interval_seconds)
            or not math.isfinite(startup_timeout_seconds)
            or health_interval_seconds <= 0
            or startup_timeout_seconds <= 0
        ):
            raise ValueError("health and startup timeouts must be positive")
        if type(health_timeout_ms) is not int or not 1 <= health_timeout_ms <= 10_000:
            raise ValueError("health_timeout_ms must be between 1 and 10000")
        if (
            not math.isfinite(restart_window_seconds)
            or not math.isfinite(backoff_base_seconds)
            or not math.isfinite(backoff_max_seconds)
            or restart_window_seconds <= 0
            or backoff_base_seconds < 0
        ):
            raise ValueError("restart window must be positive and backoff non-negative")
        if (
            backoff_max_seconds < backoff_base_seconds
            or not math.isfinite(terminate_timeout_seconds)
            or terminate_timeout_seconds <= 0
        ):
            raise ValueError("invalid restart backoff or termination timeout")

        self.studio_command = list(studio_command)
        self.working_directory = working_directory
        self.ipc_client_factory = ipc_client_factory or WatchdogIpcClient
        self.process_factory = process_factory or subprocess.Popen
        self.logger = logger or logging.getLogger("botstudio.watchdog")
        self.health_interval_seconds = health_interval_seconds
        self.health_timeout_ms = health_timeout_ms
        self.startup_timeout_seconds = startup_timeout_seconds
        self.unhealthy_threshold = unhealthy_threshold
        self.restart_limit = restart_limit
        self.restart_window_seconds = restart_window_seconds
        self.backoff_base_seconds = backoff_base_seconds
        self.backoff_max_seconds = backoff_max_seconds
        self.terminate_timeout_seconds = terminate_timeout_seconds
        self.clock = clock or monotonic
        self.notification_handler = notification_handler
        self.attached_process_factory = attached_process_factory

        self.process = initial_process
        self.state = "starting" if initial_process is not None else "stopped"
        self.health = "unknown"
        self.last_error = None
        self.last_status = None
        self.last_check_at = None
        self.runtime_recovery = "not_required"
        self._runtime_was_running = False
        self._consecutive_failures = 0
        self._restart_times = deque()
        self._pending_restart = False
        self._restart_at = None
        self._restart_reason = None
        self._process_started_at = (
            self.clock() if initial_process is not None else None
        )
        self._runtime_restore_pending = False
        self._runtime_restore_started_at = None
        self._intentional_studio_exit = False
        self._error_stream_id = None
        self._last_error_sequence = 0
        self._last_supervisor_alert = None
        self._stop_event = threading.Event()
        self._thread = None
        self._lock = threading.RLock()

    def start(self, launch_studio=False):
        with self._lock:
            if self._thread and self._thread.is_alive():
                return self.get_status()
            self._stop_event.clear()
            if self.process is not None and self.process.poll() is not None:
                self._release_process(self.process)
                self.process = None
            if self.process is None and launch_studio:
                self._launch_studio()
            self._thread = threading.Thread(
                target=self._monitor_loop,
                name="BotStudioWatchdog",
                daemon=True,
            )
            self._thread.start()
        return self.get_status()

    def stop(self, stop_studio=False):
        self._stop_event.set()
        if stop_studio:
            with self._lock:
                process = self.process
                if process is not None and process.poll() is None:
                    self._terminate_process(process)
                    self.process = None
                    self._release_process(process)
                    self.state = "stopped"
                    self.health = "unknown"
                    self._pending_restart = False
        thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout=self.terminate_timeout_seconds + 1)
            if thread.is_alive():
                raise TimeoutError("Watchdog monitor thread did not stop")
        with self._lock:
            self._thread = None
        return self.get_status()

    def start_studio(self):
        with self._lock:
            if self.process is None:
                self._attach_available_studio()
            if self.process is not None and self.process.poll() is None:
                return {"accepted": True, "state": "already_running"}
            if self.process is not None:
                self._release_process(self.process)
                self.process = None
            if self._pending_restart:
                return {"accepted": False, "state": "restart_backoff"}
            self._runtime_restore_pending = self._runtime_was_running
            if self._runtime_restore_pending:
                self.runtime_recovery = "pending"
            self._launch_studio()
            return {"accepted": True, "state": "starting"}

    def close_studio(self):
        with self._lock:
            if self.process is None:
                self._attach_available_studio()
            result = self.stop_studio()
            if self.process is not None:
                self._wait_for_process_exit(self.process)
                self._release_process(self.process)
                self.process = None
            self._pending_restart = False
            self._restart_at = None
            self._intentional_studio_exit = False
            self.state = "studio_stopped"
            self.health = "unavailable"
            return result

    def _attach_available_studio(self):
        if self.attached_process_factory is None:
            return
        try:
            status = self.ipc_client_factory().request(
                "status", timeout_ms=self.health_timeout_ms,
            )
        except (OSError, TimeoutError):
            self.logger.debug("No Studio control endpoint available for attachment")
            return
        self._validate_status(status)
        pid = status.get("studio_pid")
        if type(pid) is not int or pid <= 0:
            raise ValueError("Studio status does not contain a valid process ID")
        process = self.attached_process_factory(pid)
        if process.poll() is not None:
            self._release_process(process)
            raise RuntimeError("Studio exited before the watchdog could attach")
        self.process = process
        self._intentional_studio_exit = False
        self.state = "starting"
        self.health = "unknown"
        self._process_started_at = self.clock()
        self.logger.info("Attached to running BotStudio", extra={"studio_pid": pid})

    def stop_studio(self):
        with self._lock:
            process = self.process
            if process is None or process.poll() is not None:
                self._release_process(process)
                self.process = None
                self.state = "studio_stopped"
                self.health = "unavailable"
                return {"accepted": True, "state": "already_stopped"}

            result = self.runtime_command("studio.stop")
            if not isinstance(result, dict) or result.get("accepted") is not True:
                raise RuntimeError("BotStudio did not accept the stop request")
            self._intentional_studio_exit = True
            self._pending_restart = False
            self._restart_at = None
            self._runtime_restore_pending = self._runtime_was_running
            self.runtime_recovery = (
                "pending" if self._runtime_restore_pending else "not_required"
            )
            self.state = "studio_stopping"
            self.last_error = None
            return {"accepted": True, "state": "stopping"}

    def restart_studio(self):
        with self._lock:
            if self.process is not None and self.process.poll() is None:
                self._intentional_studio_exit = True
                try:
                    result = self.runtime_command("studio.stop")
                except Exception:
                    self._intentional_studio_exit = False
                    raise
                if not isinstance(result, dict) or result.get("accepted") is not True:
                    self._intentional_studio_exit = False
                    raise RuntimeError("BotStudio did not accept the stop request")
                self._wait_for_process_exit(self.process)
                self._release_process(self.process)
            elif self.process is not None:
                self._release_process(self.process)
            self.process = None
            self._pending_restart = False
            self._restart_at = None
            self._runtime_restore_pending = self._runtime_was_running
            self.runtime_recovery = (
                "pending" if self._runtime_restore_pending else "not_required"
            )
            self._intentional_studio_exit = False
            self._launch_studio()
            return {"accepted": True, "state": "restarting"}

    def runtime_command(self, command, arguments=None):
        if command not in self.CONTROL_COMMANDS:
            raise ValueError(f"Unsupported control command: {command}")
        client = self.ipc_client_factory()
        result = self._request_control(client, command, arguments)
        if command == "emulator.restart":
            return result
        if (
            not isinstance(result, dict)
            or not isinstance(result.get("operation_id"), str)
        ):
            return result

        operation_id = result["operation_id"]
        deadline = monotonic() + self.CONTROL_OPERATION_TIMEOUT_SECONDS
        while monotonic() < deadline:
            operation = self._request_control(
                client,
                "control.operation.get",
                {"operation_id": operation_id},
            )
            if not isinstance(operation, dict):
                raise ValueError("Control operation response is invalid")
            state = operation.get("state")
            if state == "completed":
                return operation.get("result")
            if state == "failed":
                return {
                    "accepted": False,
                    "state": "failed",
                    "error": operation.get("error", "Control operation failed"),
                }
            if state != "running":
                raise ValueError("Control operation state is invalid")
            threading.Event().wait(self.CONTROL_OPERATION_POLL_SECONDS)
        raise TimeoutError("Timed out waiting for the Studio control operation")

    def _request_control(self, client, command, arguments):
        if arguments is None:
            return client.request(
                command,
                timeout_ms=self.health_timeout_ms,
            )
        return client.request(
            command,
            timeout_ms=self.health_timeout_ms,
            arguments=arguments,
        )

    def monitor_once(self):
        with self._lock:
            self._prune_restart_times()
            if self._pending_restart:
                if self._stop_event.is_set():
                    self._pending_restart = False
                    self.state = "stopped"
                    return self.get_status()
                if self.clock() < self._restart_at:
                    self.state = "restart_backoff"
                    return self.get_status()
                self._launch_studio()

            process = self.process
            if process is None:
                self._attach_available_studio()
                process = self.process
            if process is None:
                if self.state not in {"restart_limit", "runtime_recovery_blocked"}:
                    self.state = "studio_stopped"
                    self.health = "unavailable"
                return self.get_status()

            exit_code = process.poll()
            if exit_code is not None:
                self.process = None
                self._release_process(process)
                self.health = "unavailable"
                if self._intentional_studio_exit:
                    self._intentional_studio_exit = False
                    self.state = "studio_stopped"
                    self.last_error = None
                    return self.get_status()
                if exit_code == 0:
                    self.state = "studio_stopped"
                    self.last_error = None
                    self._runtime_restore_pending = self._runtime_was_running
                    self.runtime_recovery = (
                        "pending"
                        if self._runtime_restore_pending
                        else "not_required"
                    )
                    self.logger.info(
                        "BotStudio exited normally; automatic restart suppressed"
                    )
                    return self.get_status()
                self._schedule_restart(f"studio exited with code {exit_code}")
                return self.get_status()

            if self._intentional_studio_exit:
                self.state = "studio_stopping"
                return self.get_status()

        try:
            status = self.ipc_client_factory().request(
                "status",
                timeout_ms=self.health_timeout_ms,
            )
            self._validate_status(status)
        except Exception as ex:
            with self._lock:
                self.last_check_at = datetime.now(timezone.utc).isoformat()
                self._consecutive_failures += 1
                self.last_error = str(ex)
                self.health = "unhealthy"
                started_at = self._process_started_at
                elapsed = self.clock() - (
                    started_at if started_at is not None else self.clock()
                )
                if elapsed < self.startup_timeout_seconds:
                    self.state = "starting"
                elif self._consecutive_failures >= self.unhealthy_threshold:
                    self._schedule_restart(
                        f"studio IPC unhealthy for {self._consecutive_failures} checks"
                    )
                else:
                    self.state = "degraded"
                return self.get_status()

        self._notify_new_studio_errors(status)
        with self._lock:
            self.last_check_at = datetime.now(timezone.utc).isoformat()
            self.last_status = status
            self._consecutive_failures = 0
            self.last_error = None
            self.health = "healthy"
            if self.state not in {
                "runtime_recovery_blocked",
                "runtime_recovery_failed",
            }:
                self.state = "running"
            runtime = status["runtime"]
            if runtime.get("running") is True:
                self._runtime_was_running = True
                if self.runtime_recovery == "restoring":
                    self.runtime_recovery = "restored"
                    self._runtime_restore_started_at = None
            elif (
                runtime.get("running") is False
                and not self._runtime_restore_pending
                and self.runtime_recovery != "restoring"
            ):
                self._runtime_was_running = False
            if self.runtime_recovery == "restoring" and not runtime.get("running"):
                started_at = self._runtime_restore_started_at
                if (
                    started_at is not None
                    and self.clock() - started_at > self.startup_timeout_seconds
                ):
                    self.runtime_recovery = "failed"
                    self.state = "runtime_recovery_failed"
                    self.last_error = "Runtime did not become active after recovery"
                    self.logger.error(self.last_error)
            self._restore_runtime_if_safe(status)
            return self.get_status()

    def get_status(self):
        with self._lock:
            self._prune_restart_times()
            pid = (
                getattr(self.process, "pid", None)
                if self.process is not None and self.process.poll() is None
                else None
            )
            return {
                "state": self.state,
                "health": self.health,
                "studio_pid": pid,
                "last_check_at": self.last_check_at,
                "restarts_last_window": len(self._restart_times),
                "restart_limit": self.restart_limit,
                "last_error": self.last_error,
                "runtime_recovery": self.runtime_recovery,
                "studio_status": self.last_status,
            }

    def _monitor_loop(self):
        while not self._stop_event.is_set():
            try:
                status = self.monitor_once()
                self._notify_supervisor_fault(status)
            except Exception as ex:
                with self._lock:
                    self.last_error = str(ex)
                    self.health = "unknown"
                    self.state = "degraded"
                self.logger.exception("Watchdog monitor iteration failed")
                self._send_notification(
                    "BotStudio watchdog alert: monitor iteration failed."
                )
            self._stop_event.wait(self.health_interval_seconds)

    def _notify_new_studio_errors(self, status):
        journal = status.get("error_journal")
        if not isinstance(journal, dict):
            return
        stream_id = journal.get("stream_id")
        events = journal.get("events")
        if (
            not isinstance(stream_id, str)
            or not stream_id
            or not isinstance(events, list)
        ):
            return

        valid_events = [
            event
            for event in events
            if isinstance(event, dict)
            and type(event.get("sequence")) is int
            and event["sequence"] > 0
        ]
        if self._error_stream_id != stream_id:
            self._error_stream_id = stream_id
            self._last_error_sequence = max(
                (event["sequence"] for event in valid_events),
                default=0,
            )
            return

        new_events = sorted(
            (
                event
                for event in valid_events
                if event["sequence"] > self._last_error_sequence
            ),
            key=lambda event: event["sequence"],
        )
        if not new_events:
            return
        self._last_error_sequence = new_events[-1]["sequence"]
        if self.notification_handler is None:
            return

        task_events = [
            event for event in new_events
            if event.get("event") == "Scheduler Task Failed"
            and isinstance(event.get("context"), dict)
        ]
        selected = (
            [
                event for event in new_events
                if event.get("event") != "Function Block Failed"
            ]
            if task_events else new_events
        )
        lines = [
            (
                f"BotStudio reported {len(task_events)} failed task execution(s)."
                if task_events else f"BotStudio reported {len(new_events)} new error(s)."
            ),
        ]
        for event in selected[-3:]:
            context = event.get("context")
            context = context if isinstance(context, dict) else {}
            event_name = event.get("event")
            description = {
                "Scheduler Event Fehlerfall": "The event finished, but its next run could not be scheduled.",
                "Scheduler Task Failed": "A task could not be completed.",
                "Scheduler Task Statistics Failed": "Task statistics could not be saved.",
                "Runtime Profile Switch Missing": "The profile could not be activated: no profile-switch function is assigned.",
            }.get(event_name)
            lines.append(description or (
                f"{self._safe_alert_field(event.get('severity'))} "
                f"in {self._safe_alert_field(event.get('module'))}: "
                f"{self._safe_alert_field(event_name)}"
            ))
            for key, label in (
                ("profile", "Profile"), ("event_name", "Event"), ("task_name", "Task"),
                ("function_name", "Function"), ("failed_function_name", "Failed function"),
                ("failed_block_type", "Block"), ("failure_reason", "Reason"),
            ):
                if context.get(key):
                    value = context[key]
                    if key == "failure_reason":
                        value = {
                            "failed": "Function reported failure",
                            "failure": "Function reported failure",
                            "timeout": "Function timed out",
                            "function_file_missing": "Assigned function file is missing",
                            "function_load_failed": "Assigned function could not be loaded",
                            "function_not_saved": "Assigned function is not saved",
                            "invalid_event_stop_delay": "Invalid event stop delay",
                            "no_event_variable_and_no_default_scheduling": (
                                "No fresh duration was returned by the event, and no fallback interval or time is configured."
                            ),
                        }.get(value, value)
                    lines.append(f"{label}: {self._safe_alert_field(value)}")
            if event_name == "Scheduler Event Fehlerfall":
                lines.append(
                    "Next step: configure an interval or local time in the event, "
                    "or have its function return the duration until the next run."
                )
            chain = context.get("function_chain")
            if isinstance(chain, list) and len(chain) > 1:
                lines.append(
                    "Function path: " + " -> ".join(self._safe_alert_field(name) for name in chain[:4])
                )
            if context.get("task_name") and event.get("event") == "Scheduler Task Failed":
                current = context.get("task_failures_current")
                total = context.get("task_failures_all_time")
                successes = context.get("task_successes_all_time")
                if all(type(value) is int and value >= 0 for value in (current, total, successes)):
                    lines.append(
                        f"Task errors: {current} since reset | {total} all-time\n"
                        f"Task runs all-time: {total + successes} ({successes} successful)"
                    )
                else:
                    lines.append("Task error counters: unavailable")
            lines.append(f"At: {self._safe_alert_field(event.get('timestamp'))}")
        if len(selected) > 3:
            lines.append(f"{len(selected) - 3} additional error(s); see Studio logs.")
        message = "\n".join(lines)
        self._send_notification(message)

    def _notify_supervisor_fault(self, status):
        alert_states = {
            "restart_backoff",
            "restart_limit",
            "restart_failed",
            "runtime_recovery_blocked",
            "runtime_recovery_failed",
        }
        state = status.get("state")
        if state not in alert_states:
            self._last_supervisor_alert = None
            return
        if state == self._last_supervisor_alert:
            return
        self._last_supervisor_alert = state
        self._send_notification(
            f"BotStudio watchdog alert: {state}. "
            f"Restarts last 24h: {status.get('restarts_last_window', 0)}."
        )

    @staticmethod
    def _safe_alert_field(value):
        if not isinstance(value, str):
            return "unknown"
        return " ".join(value.split())[:120] or "unknown"

    def _send_notification(self, message):
        if self.notification_handler is None:
            return
        try:
            self.notification_handler(message)
        except Exception as ex:
            self.logger.warning(
                "Watchdog Telegram notification could not be queued",
                extra={"error_type": type(ex).__name__},
            )

    def _launch_studio(self):
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        process = self.process_factory(
            self.studio_command,
            cwd=self.working_directory,
            creationflags=creationflags,
        )
        self.process = process
        self._process_started_at = self.clock()
        self._consecutive_failures = 0
        self._pending_restart = False
        self._restart_at = None
        self.state = "starting"
        self.health = "unknown"
        self.last_error = None
        self.logger.info(
            "BotStudio process started",
            extra={"studio_pid": getattr(process, "pid", None)},
        )

    def _schedule_restart(self, reason):
        process = self.process
        if process is not None and process.poll() is None:
            try:
                self._terminate_process(process)
            except Exception as ex:
                self.last_error = f"{reason}; unable to stop Studio: {ex}"
                self.health = "unhealthy"
                self.state = "restart_failed"
                self.logger.exception("BotStudio process termination failed")
                return
        self.process = None
        self._release_process(process)

        self._prune_restart_times()
        if len(self._restart_times) >= self.restart_limit:
            self._pending_restart = False
            self._restart_at = None
            self.last_error = reason
            self.health = "unhealthy"
            self.state = "restart_limit"
            self._runtime_restore_pending = self._runtime_was_running
            self.runtime_recovery = (
                "blocked_restart_limit"
                if self._runtime_restore_pending
                else "not_required"
            )
            self.logger.error(
                "BotStudio restart limit reached",
                extra={"reason": reason, "restart_count": len(self._restart_times)},
            )
            return

        self._restart_times.append(self.clock())
        delay = min(
            self.backoff_base_seconds * (2 ** (len(self._restart_times) - 1)),
            self.backoff_max_seconds,
        )
        self._pending_restart = True
        self._restart_at = self.clock() + delay
        self._restart_reason = reason
        self.last_error = reason
        self.health = "unhealthy"
        self.state = "restart_backoff"
        self.runtime_recovery = (
            "pending" if self._runtime_was_running else "not_required"
        )
        self._runtime_restore_pending = self._runtime_was_running
        self.logger.warning(
            "BotStudio restart scheduled",
            extra={
                "reason": reason,
                "restart_count": len(self._restart_times),
                "backoff_seconds": delay,
            },
        )

    def _restore_runtime_if_safe(self, status):
        if not self._runtime_restore_pending or self.runtime_recovery != "pending":
            return
        recovery = status.get("runtime_recovery")
        if not isinstance(recovery, dict) or recovery.get("safe_to_resume") is not True:
            self.runtime_recovery = "blocked_interrupted_work"
            self.state = "runtime_recovery_blocked"
            self.last_error = "Runtime resume blocked: interrupted work requires review"
            self.logger.error(
                "Runtime recovery blocked because an interrupted event may be replayed"
            )
            return

        if status["runtime"].get("running") is True:
            self.runtime_recovery = "restored"
            self._runtime_restore_pending = False
            return

        try:
            result = self.ipc_client_factory().request(
                "runtime.start",
                timeout_ms=self.health_timeout_ms,
            )
        except Exception as ex:
            self.runtime_recovery = "failed"
            self.state = "runtime_recovery_failed"
            self.last_error = f"Runtime recovery failed: {ex}"
            self.logger.exception("Runtime recovery request failed")
            return

        if not isinstance(result, dict) or result.get("accepted") is not True:
            self.runtime_recovery = "failed"
            self.state = "runtime_recovery_failed"
            self.last_error = "Runtime recovery request was not accepted"
            self.logger.error(self.last_error)
            return
        self.runtime_recovery = "restoring"
        self._runtime_restore_pending = False
        self._runtime_restore_started_at = self.clock()

    def _validate_status(self, status):
        if not isinstance(status, dict) or status.get("studio") != "running":
            raise ValueError("Studio status response is invalid")
        if not isinstance(status.get("runtime"), dict):
            raise ValueError("Studio runtime status is missing")
        if type(status["runtime"].get("running")) is not bool:
            raise ValueError("Studio runtime running state is invalid")

    def _terminate_process(self, process):
        process.terminate()
        try:
            process.wait(timeout=self.terminate_timeout_seconds)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=self.terminate_timeout_seconds)

    @staticmethod
    def _release_process(process):
        close = getattr(process, "close", None)
        if callable(close):
            close()

    def _wait_for_process_exit(self, process):
        deadline = time.monotonic() + self.terminate_timeout_seconds
        while process.poll() is None and time.monotonic() < deadline:
            time.sleep(min(0.05, max(0, deadline - time.monotonic())))
        if process.poll() is None:
            raise TimeoutError(
                "BotStudio did not close after the graceful stop request"
            )

    def _prune_restart_times(self):
        cutoff = self.clock() - self.restart_window_seconds
        while self._restart_times and self._restart_times[0] < cutoff:
            self._restart_times.popleft()
