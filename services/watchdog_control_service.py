import os
from threading import Event
from time import monotonic
from uuid import uuid4

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal

from services.logging_service import get_recent_errors
from services.remote_monitoring_service import RemoteMonitoringService


class _RuntimeStartSignals(QObject):
    finished = Signal(object)


class _RuntimeStartOperation(QRunnable):
    def __init__(self, runtime_service, cancel_event):
        super().__init__()
        self.runtime_service = runtime_service
        self.cancel_event = cancel_event
        self.signals = _RuntimeStartSignals()

    def run(self):
        try:
            if self.cancel_event.is_set():
                result = self.runtime_service.get_status()
            else:
                result = self.runtime_service.start()
                if self.cancel_event.is_set():
                    result = self.runtime_service.stop()
        except Exception as ex:
            result = ex
        self.signals.finished.emit(result)


class _SchedulerCommandSignals(QObject):
    finished = Signal(str, object, object)


class _SchedulerCommandOperation(QRunnable):
    def __init__(self, operation_id, runtime_service, command, arguments, monitoring_service=None):
        super().__init__()
        self.operation_id = operation_id
        self.runtime_service = runtime_service
        self.command = command
        self.arguments = arguments
        self.monitoring_service = monitoring_service
        self.signals = _SchedulerCommandSignals()

    def run(self):
        try:
            handler = (
                self.monitoring_service.handle
                if self.command in {"statistics.get", "info.get", "info.photo.get"}
                else
                self.runtime_service.watchdog_emulator_command
                if self.command in {"emulators.list", "emulator.restart"}
                else self.runtime_service.watchdog_scheduler_command
            )
            result = handler(
                self.command,
                self.arguments,
            )
        except Exception as error:
            self.signals.finished.emit(self.operation_id, None, error)
        else:
            self.signals.finished.emit(self.operation_id, result, None)


class WatchdogControlService(QObject):
    studio_stop_requested = Signal()
    SCHEDULER_COMMANDS = {
        "events.list",
        "tasks.list",
        "event.enable",
        "event.disable",
        "event.run_now",
        "emulators.list",
        "emulator.restart",
        "statistics.get",
        "info.get",
        "info.photo.get",
    }
    OPERATION_RETENTION_SECONDS = 300
    MAX_STORED_OPERATIONS = 32
    MONITORING_COMMANDS = {"statistics.get", "info.get", "info.photo.get"}

    def __init__(self, runtime_service, parent=None):
        super().__init__(parent)
        self.runtime_service = runtime_service
        self.remote_monitoring = RemoteMonitoringService(runtime_service)
        self._operation = None
        self.last_control_error = None
        self._start_operation = None
        self._start_cancel_event = None
        self._scheduler_operations = {}
        self._active_scheduler_operation = None
        self._scheduler_worker_operation = None
        self._control_pool = QThreadPool(self)
        self._control_pool.setMaxThreadCount(2)
        self._monitoring_workers = {}
        self._monitoring_pool = QThreadPool(self)
        self._monitoring_pool.setMaxThreadCount(2)

    def handle_request(self, command, arguments=None):
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            raise ValueError("Command arguments must be an object")
        if command == "studio.stop":
            self._require_empty_arguments(arguments)
            self.studio_stop_requested.emit()
            return {"accepted": True, "state": "stopping"}
        if command == "status":
            self._require_empty_arguments(arguments)
            return self._status()
        if command == "runtime.start":
            self._require_empty_arguments(arguments)
            if self._active_scheduler_operation is not None:
                return {"accepted": False, "state": "busy"}
            return self._start_runtime()
        if command == "runtime.stop":
            self._require_empty_arguments(arguments)
            return self._stop_runtime()
        if command in self.SCHEDULER_COMMANDS:
            return self._start_scheduler_command(command, arguments)
        if command == "control.operation.get":
            return self._get_scheduler_operation(arguments)
        raise ValueError(f"Unsupported command: {command}")

    @staticmethod
    def _require_empty_arguments(arguments):
        if arguments:
            raise ValueError("This command does not accept arguments")

    def _start_scheduler_command(self, command, arguments):
        self._prune_scheduler_operations()
        monitoring = command in self.MONITORING_COMMANDS
        if monitoring and len(self._monitoring_workers) >= 2:
            return {"accepted": False, "state": "busy"}
        if not monitoring and (self._active_scheduler_operation is not None or self._operation is not None):
            return {"accepted": False, "state": "busy"}
        completed_operations = sorted(
            (
                (operation_id, operation)
                for operation_id, operation in self._scheduler_operations.items()
                if operation.get("state") != "running"
            ),
            key=lambda item: item[1].get("created_at", 0),
        )
        while (
            len(self._scheduler_operations) >= self.MAX_STORED_OPERATIONS
            and completed_operations
        ):
            operation_id, _operation = completed_operations.pop(0)
            del self._scheduler_operations[operation_id]
        if len(self._scheduler_operations) >= self.MAX_STORED_OPERATIONS:
            return {"accepted": False, "state": "busy"}

        operation_id = uuid4().hex
        self._scheduler_operations[operation_id] = {
            "state": "running",
            "created_at": monotonic(),
        }
        operation = _SchedulerCommandOperation(
            operation_id,
            self.runtime_service,
            command,
            dict(arguments),
            monitoring_service=self.remote_monitoring,
        )
        operation.signals.finished.connect(self._finish_scheduler_command)
        if monitoring:
            self._monitoring_workers[operation_id] = operation
            self._monitoring_pool.start(operation)
        else:
            self._active_scheduler_operation = operation_id
            self._scheduler_worker_operation = operation
            self._control_pool.start(operation)
        return {
            "accepted": True,
            "state": "running",
            "operation_id": operation_id,
        }

    def _get_scheduler_operation(self, arguments):
        if set(arguments) != {"operation_id"}:
            raise ValueError("operation_id is required")
        operation_id = arguments["operation_id"]
        if (
            not isinstance(operation_id, str)
            or not operation_id
            or len(operation_id) > 128
        ):
            raise ValueError("operation_id must be a non-empty string")
        self._prune_scheduler_operations()
        operation = self._scheduler_operations.get(operation_id)
        if operation is None:
            raise ValueError("Control operation was not found")
        return {
            key: value
            for key, value in operation.items()
            if key != "created_at"
        }

    def _finish_scheduler_command(self, operation_id, result, error):
        self._monitoring_workers.pop(operation_id, None)
        operation = self._scheduler_operations.get(operation_id)
        if operation is None:
            return
        operation["created_at"] = monotonic()
        if error is None:
            operation.update({"state": "completed", "result": result})
        else:
            operation.update({"state": "failed", "error": str(error)})
            self.runtime_service.logger.error(
                "Watchdog Control Operation Failed",
                command_error_type=type(error).__name__,
                operation_id=operation_id,
            )
        if self._active_scheduler_operation == operation_id:
            self._active_scheduler_operation = None
            self._scheduler_worker_operation = None

    def _prune_scheduler_operations(self):
        cutoff = monotonic() - self.OPERATION_RETENTION_SECONDS
        expired = [
            operation_id
            for operation_id, operation in self._scheduler_operations.items()
            if operation.get("state") != "running"
            and operation.get("created_at", 0) < cutoff
        ]
        for operation_id in expired:
            del self._scheduler_operations[operation_id]

    def _status(self):
        status = self.runtime_service.get_status()
        return {
            "studio": "running",
            "studio_pid": os.getpid(),
            "runtime": status,
            "runtime_recovery": self.runtime_service.get_recovery_state(),
            "control_operation": self._operation or "idle",
            "last_control_error": self.last_control_error,
            "error_journal": get_recent_errors(),
        }

    def _start_runtime(self):
        if self._operation is not None:
            return {"accepted": False, "state": "busy"}
        if self.runtime_service.running:
            return {"accepted": True, "state": "already_running"}

        self._operation = "starting"
        self.last_control_error = None
        self._start_cancel_event = Event()
        operation = _RuntimeStartOperation(
            self.runtime_service,
            self._start_cancel_event,
        )
        operation.signals.finished.connect(self._finish_start)
        self._start_operation = operation
        QThreadPool.globalInstance().start(operation)
        return {"accepted": True, "state": "starting"}

    def _stop_runtime(self):
        start_in_progress = self._operation == "starting"
        if self._start_cancel_event is not None:
            self._start_cancel_event.set()
        result = self.runtime_service.stop()
        if start_in_progress:
            self._operation = "stopping"
        else:
            self._operation = None
        return {
            "accepted": True,
            "state": "stopping" if start_in_progress else "stopped",
            "runtime": result,
        }

    def _finish_start(self, result):
        self._start_operation = None
        self._start_cancel_event = None
        self._operation = None
        if isinstance(result, Exception):
            self.last_control_error = str(result)
            self.runtime_service.logger.error(
                "Watchdog Runtime Start Failed",
                error=self.last_control_error,
            )
