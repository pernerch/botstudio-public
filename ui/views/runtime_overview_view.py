import json
from datetime import datetime, timezone

from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, QTimer, Signal
from PySide6.QtGui import QColor

from PySide6.QtWidgets import (
    QCheckBox,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QFileDialog,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from services.runtime_profile_activation_service import (
    RuntimeProfileActivationService,
)
from services.runtime_service import (
    RUNTIME_ERROR,
    RUNTIME_RECOVERING,
    RUNTIME_RUNNING,
    RUNTIME_STOPPED,
    RUNTIME_PAUSED,
    RuntimeService,
)
from services.logging_service import LOG_DIR
from ui.i18n.translator import t


class _RuntimeOperationSignals(QObject):
    finished = Signal(str, object)


class _RuntimeOperation(QRunnable):

    def __init__(self, runtime_service, operation):
        super().__init__()
        self.runtime_service = runtime_service
        self.operation = operation
        self.signals = _RuntimeOperationSignals()

    def run(self):
        try:
            if self.operation == "start":
                result = self.runtime_service.start()
            elif self.operation == "stop":
                result = self.runtime_service.stop()
            elif self.operation == "events":
                result = RuntimeOverviewView._read_events()
            else:
                result = self.runtime_service.heartbeat_once()
            self.signals.finished.emit(self.operation, result)
        except Exception as ex:
            self.signals.finished.emit(self.operation, ex)


class RuntimeOverviewView(QWidget):
    action_feedback = Signal(str)

    def __init__(
        self,
        runtime_service=None,
        parent=None,
    ):
        super().__init__(parent)

        self.runtime_service = (
            runtime_service
            or RuntimeService()
        )
        if self.runtime_service.scheduler_runner_factory is None:
            self.runtime_service.scheduler_runner_factory = (
                self.runtime_service.create_function_runner
            )

        self.activation_service = RuntimeProfileActivationService()
        self.profile_name_map = {}
        self.is_loading_profiles = False
        self._runtime_operation = None
        self._runtime_operation_running = False
        self._events_operation = None

        root = QVBoxLayout(
            self
        )

        status_group = QGroupBox(
            t("runtime.title")
        )

        status_layout = QVBoxLayout(
            status_group
        )

        control_layout = QHBoxLayout()

        self.btn_start = QPushButton(
            t("runtime.start")
        )

        self.btn_stop = QPushButton(
            t("runtime.stop")
        )

        self.lbl_status = QLabel(
            t("runtime.stopped")
        )
        self.lbl_operation_feedback = QLabel("")
        self.lbl_operation_feedback.setWordWrap(True)
        self.lbl_heartbeat = QLabel("● Inactive")
        self.lbl_heartbeat.setToolTip("Runtime activity heartbeat")
        self._heartbeat_pulse_on = False

        control_layout.addWidget(
            self.btn_start
        )

        control_layout.addWidget(
            self.btn_stop
        )

        control_layout.addWidget(
            self.lbl_status,
            stretch=1,
        )
        control_layout.addWidget(self.lbl_operation_feedback, stretch=2)

        control_layout.addWidget(
            self.lbl_heartbeat,
        )

        status_layout.addLayout(
            control_layout
        )

        summary_layout = QHBoxLayout()

        self.lbl_summary_runtime = QLabel("-")
        self.lbl_summary_healthy = QLabel("-")
        self.lbl_summary_recovering = QLabel("-")
        self.lbl_summary_failed = QLabel("-")
        self.lbl_summary_queued = QLabel("-")

        summary_layout.addWidget(
            self._summary_card(
                t("runtime.summary_state"),
                self.lbl_summary_runtime,
            )
        )

        summary_layout.addWidget(
            self._summary_card(
                t("runtime.summary_healthy"),
                self.lbl_summary_healthy,
            )
        )

        summary_layout.addWidget(
            self._summary_card(
                t("runtime.summary_recovering"),
                self.lbl_summary_recovering,
            )
        )

        summary_layout.addWidget(
            self._summary_card(
                t("runtime.summary_failed"),
                self.lbl_summary_failed,
            )
        )

        summary_layout.addWidget(
            self._summary_card(
                t("runtime.summary_queued"),
                self.lbl_summary_queued,
            )
        )

        status_layout.addLayout(
            summary_layout
        )

        self.status_table = QTableWidget(
            0,
            4,
        )

        self.status_table.setHorizontalHeaderLabels(
            [
                t("runtime.profile"),
                t("runtime.game"),
                t("runtime.state"),
                t("runtime.message"),
            ]
        )

        self.status_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.Stretch
        )

        status_layout.addWidget(
            self.status_table
        )

        events_group = QGroupBox(
            t("runtime.events")
        )

        events_layout = QVBoxLayout(
            events_group
        )

        self.events_table = QTableWidget(
            0,
            4,
        )

        self.events_table.setHorizontalHeaderLabels(
            [
                t("logs.timestamp"),
                t("logs.level"),
                t("logs.profile"),
                t("logs.message"),
            ]
        )

        self.events_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.Stretch
        )

        events_layout.addWidget(
            self.events_table
        )

        status_layout.addWidget(
            events_group
        )

        root.addWidget(
            status_group
        )

        profile_group = QGroupBox(
            t("runtime.profile_overview")
        )

        profile_layout = QVBoxLayout(
            profile_group
        )

        self.profile_table = QTableWidget(
            0,
            3,
        )

        self.profile_table.setHorizontalHeaderLabels(
            [
                t("runtime.profile"),
                t("runtime.game"),
                t("runtime.enabled"),
            ]
        )

        self.profile_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.Stretch
        )

        profile_layout.addWidget(
            self.profile_table
        )

        root.addWidget(
            profile_group
        )

        self.btn_start.clicked.connect(
            self.start_runtime
        )

        self.btn_stop.clicked.connect(
            self.stop_runtime
        )

        self.refresh_timer = QTimer(
            self
        )

        self.refresh_timer.setInterval(
            1000
        )

        self.refresh_timer.timeout.connect(
            self.refresh_runtime_status
        )

        self.refresh_profiles()
        self.refresh_runtime_status()
        self.refresh_timer.start()

    def start_runtime(
        self,
    ):

        if not self._ensure_emulator_manager_path():
            self._set_operation_feedback(t("runtime.start_blocked"))
            return

        if not self._ensure_game_packages_configured():
            self._set_operation_feedback(t("runtime.start_blocked"))
            return

        self._set_operation_feedback(t("runtime.start_queued"))
        self._start_runtime_operation("start")

    def stop_runtime(
        self,
    ):
        # Stop is deliberately synchronous and constant-time: it only sets
        # cancellation state and stops heartbeat scheduling.
        self._set_operation_feedback(t("runtime.stop_requested"))
        self.runtime_service.stop()
        self.refresh_runtime_status()
        self._set_operation_feedback(t("runtime.stop_complete"))

    def _set_operation_feedback(self, message):
        self.lbl_operation_feedback.setText(message)
        self.action_feedback.emit(message)

    def _start_runtime_operation(self, operation):

        if self._runtime_operation_running:
            self._set_operation_feedback(t("runtime.operation_busy"))
            return False

        self._runtime_operation_running = True
        self._set_operation_feedback(
            t("runtime.start_queued")
            if operation == "start"
            else t("runtime.heartbeat_queued")
        )
        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self._runtime_operation = _RuntimeOperation(
            self.runtime_service,
            operation,
        )
        self._runtime_operation.signals.finished.connect(
            self._finish_runtime_operation
        )
        QThreadPool.globalInstance().start(self._runtime_operation)
        return True

    def _finish_runtime_operation(self, operation, result):

        self._runtime_operation_running = False
        self._runtime_operation = None
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(True)

        if isinstance(result, Exception):
            self.runtime_service.logger.error(
                "Runtime GUI Operation Failed",
                operation=operation,
                error=repr(result),
            )
            self._set_operation_feedback(t("runtime.operation_failed"))
        elif operation == "start":
            self.refresh_timer.start()
            self._set_operation_feedback(
                t("runtime.start_complete")
                if self.runtime_service.running
                else t("runtime.operation_failed")
            )
        elif operation == "stop":
            self.refresh_timer.stop()
            self._set_operation_feedback(t("runtime.stop_complete"))
        elif operation == "heartbeat":
            self._set_operation_feedback(t("runtime.heartbeat_complete"))

        self.refresh_runtime_status()

    def _ensure_emulator_manager_path(
        self,
    ):

        emulator_manager = (
            self.runtime_service.emulator_manager
        )

        if emulator_manager.emulator_type != "mumu":
            return True

        if emulator_manager.mumu_manager_path:
            return True

        path, _ = QFileDialog.getOpenFileName(
            self,
            t("runtime.select_mumu_manager"),
            "D:/Program Files/Netease/MuMuPlayer/nx_main",
            "MuMuManager.exe (MuMuManager.exe);;Executables (*.exe)",
        )

        if not path:
            return False

        emulator_manager.set_mumu_manager_path(
            path
        )

        return True

    def _ensure_game_packages_configured(
        self,
    ):

        missing = []

        for profile in self.activation_service.get_profiles():

            if not profile.get("enabled", False):
                continue

            package_name = (
                self.runtime_service
                .game_runtime_service
                .get_package_name(
                    profile
                )
            )

            if package_name:
                continue

            missing.append(
                profile.get("name", "-")
            )

        if not missing:
            return True

        QMessageBox.warning(
            self,
            t("runtime.package_missing_title"),
            t("runtime.package_missing_text").format(
                profiles="\n".join(missing)
            ),
        )

        return False


    def refresh_runtime_status(
        self,
    ):

        status = self.runtime_service.get_status()

        self.lbl_status.setText(
            self._status_text(
                status["status"]
            )
        )

        self._apply_status_color(
            status["status"]
        )
        self._apply_heartbeat_status(status)

        profiles = status.get(
            "profiles",
            [],
        )

        counts = self._health_counts(
            profiles
        )

        self.lbl_summary_runtime.setText(
            self._status_text(
                status["status"]
            )
        )

        self.lbl_summary_healthy.setText(
            str(counts["healthy"])
        )

        self.lbl_summary_recovering.setText(
            str(counts["recovering"])
        )

        self.lbl_summary_failed.setText(
            str(counts["failed"])
        )

        self.lbl_summary_queued.setText(
            str(counts["queued"])
        )

        self.status_table.setRowCount(
            len(profiles)
        )

        for row, profile_status in enumerate(profiles):
            profile_id = str(profile_status.get("profile_id") or "")
            profile_name = self.profile_name_map.get(
                profile_id,
                profile_status.get("profile_name", "-"),
            )

            self.status_table.setItem(
                row,
                0,
                QTableWidgetItem(
                    profile_name
                ),
            )

            self.status_table.setItem(
                row,
                1,
                QTableWidgetItem(
                    self._game_text(
                        profile_status.get("game", "-")
                    )
                ),
            )

            self.status_table.setItem(
                row,
                2,
                self._state_item(
                    profile_status.get("state", "-")
                ),
            )

            self.status_table.setItem(
                row,
                3,
                QTableWidgetItem(
                    profile_status.get("message", "-")
                ),
            )

        self._refresh_events()

    def select_profile(
        self,
        profile_name,
    ):

        for row in range(self.status_table.rowCount()):

            item = self.status_table.item(
                row,
                0,
            )

            if not item:
                continue

            if item.text() == profile_name:
                self.status_table.selectRow(row)
                return

    def refresh_profiles(
        self,
    ):

        self.is_loading_profiles = True

        profiles = self.activation_service.get_profiles()
        self.profile_name_map = {}
        for profile in profiles:
            name = str(profile.get("name") or profile.get("id") or "-")
            for profile_id in (
                profile.get("id"),
                profile.get("botstudio_profile_id"),
            ):
                if profile_id:
                    self.profile_name_map[str(profile_id)] = name

        self.profile_table.setRowCount(
            len(profiles)
        )

        for row, profile in enumerate(profiles):

            self.profile_table.setItem(
                row,
                0,
                self._readonly_item(
                    profile.get("name", "-")
                ),
            )

            game_id = profile.get(
                "game",
                "-",
            )

            self.profile_table.setItem(
                row,
                1,
                self._readonly_item(
                    self.activation_service.get_game_display_name(
                        game_id
                    )
                ),
            )

            enabled = QCheckBox()

            enabled.setChecked(
                profile.get("enabled", False)
            )

            enabled.setProperty(
                "profile_id",
                profile.get("id"),
            )

            enabled.stateChanged.connect(
                self.on_profile_enabled_changed
            )

            enabled_cell = QWidget()
            enabled_layout = QHBoxLayout(
                enabled_cell
            )
            enabled_layout.setContentsMargins(0, 0, 0, 0)
            enabled_layout.setAlignment(Qt.AlignCenter)
            enabled_layout.addWidget(enabled)

            self.profile_table.setCellWidget(
                row,
                2,
                enabled_cell,
            )

        self.is_loading_profiles = False

    def on_profile_enabled_changed(
        self,
        state,
    ):

        if self.is_loading_profiles:
            return

        checkbox = self.sender()

        profile_id = checkbox.property(
            "profile_id"
        )

        enabled = (
            state == Qt.CheckState.Checked.value
        )

        result = self.activation_service.set_enabled(
            profile_id,
            enabled,
        )

        conflict = result.get(
            "conflict"
        )

        if conflict:
            self._handle_conflict(
                profile_id,
                conflict,
            )
            self._set_operation_feedback(t("runtime.profile_change_rejected"))
        else:
            self._set_operation_feedback(
                t("runtime.profile_enabled")
                if enabled
                else t("runtime.profile_disabled")
            )

        self.refresh_profiles()

        if self.runtime_service.running:
            self._start_runtime_operation("heartbeat")
            self.refresh_runtime_status()

    def _handle_conflict(
        self,
        profile_id,
        conflict,
    ):

        profile = conflict["profile"]
        conflicts = conflict["conflicts"]

        names = "\n".join(
            item.get("name", "-")
            for item in conflicts
        )

        game_names = "\n".join(
            self.activation_service.get_game_display_name(
                item.get("game")
            )
            for item in conflicts
        )

        text = t("runtime.conflict_text").format(
            instance=conflict["emulator_instance"],
            profiles=names,
            games=game_names,
            target=profile.get("name", "-"),
        )

        dialog = QMessageBox(
            self,
        )

        dialog.setWindowTitle(
            t("runtime.conflict_title")
        )

        dialog.setText(
            text
        )

        replace_button = dialog.addButton(
            t("runtime.conflict_replace"),
            QMessageBox.AcceptRole,
        )

        dialog.addButton(
            t("common.cancel"),
            QMessageBox.RejectRole,
        )

        dialog.exec()

        if dialog.clickedButton() != replace_button:
            return

        self.activation_service.resolve_conflict_by_replacing(
            profile_id,
            conflicts,
        )

    def _apply_status_color(
        self,
        status,
    ):

        if status == RUNTIME_RUNNING:
            color = "#36c275"
        elif status == RUNTIME_RECOVERING:
            color = "#ffb84d"
        elif status == RUNTIME_ERROR:
            color = "#ff6b6b"
        else:
            color = "#e8e8e8"

        self.lbl_status.setStyleSheet(
            f"color: {color}; font-weight: bold;"
        )

    def _apply_heartbeat_status(self, status):
        timestamp = status.get("last_health_at") or status.get("last_heartbeat_at")
        active = status.get("running") and status.get("status") != RUNTIME_PAUSED and timestamp
        if status.get("status") == RUNTIME_ERROR:
            color, label = "#ff6b6b", "● Error"
        elif active:
            try:
                age = datetime.now(timezone.utc) - datetime.fromisoformat(timestamp)
                active = age.total_seconds() <= self.runtime_service.heartbeat.interval_seconds * 2
            except (TypeError, ValueError):
                active = False
            if active:
                self._heartbeat_pulse_on = not self._heartbeat_pulse_on
                color = "#36c275" if self._heartbeat_pulse_on else "#86efac"
                label = "● Healthy"
            else:
                color, label = "#9ca3af", "● Inactive"
        else:
            color, label = "#9ca3af", "● Inactive"
        self.lbl_heartbeat.setText(label)
        self.lbl_heartbeat.setStyleSheet(f"color: {color}; font-weight: bold;")

    def _readonly_item(
        self,
        text,
    ):

        item = QTableWidgetItem(
            str(text)
        )

        item.setFlags(
            item.flags()
            & ~Qt.ItemFlag.ItemIsEditable
        )

        return item

    def _summary_card(
        self,
        title,
        value_label,
    ):

        card = QGroupBox(
            title
        )

        layout = QVBoxLayout(
            card
        )

        value_label.setAlignment(
            Qt.AlignCenter
        )

        layout.addWidget(
            value_label
        )

        return card

    def _health_counts(
        self,
        profiles,
    ):

        counts = {
            "healthy": 0,
            "recovering": 0,
            "failed": 0,
            "queued": 0,
        }

        for profile_status in profiles:

            state = profile_status.get(
                "state"
            )

            if state == "Healthy":
                counts["healthy"] += 1
            elif state == "Recovering":
                counts["recovering"] += 1
            elif state == "Failed":
                counts["failed"] += 1
            elif state == "Queued":
                counts["queued"] += 1

        return counts

    def _refresh_events(
        self,
    ):
        if self._events_operation is not None:
            return
        self._events_operation = _RuntimeOperation(self.runtime_service, "events")
        self._events_operation.signals.finished.connect(self._finish_events)
        QThreadPool.globalInstance().start(self._events_operation)

    @staticmethod
    def _read_events():
        records = []
        runtime_log = LOG_DIR / "runtime.log"
        if runtime_log.exists():
            with open(runtime_log, "rb") as handle:
                size = handle.seek(0, 2)
                offset = max(0, size - 64 * 1024)
                handle.seek(offset)
                if offset:
                    handle.readline()
                for line in handle.read().decode("utf-8", errors="replace").splitlines():
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(record, dict):
                        records.append(record)
        return list(reversed(records[-8:]))

    def _finish_events(self, operation, records):
        self._events_operation = None
        if isinstance(records, Exception):
            self.runtime_service.logger.error("Runtime Events Refresh Failed", error=repr(records))
            self._set_operation_feedback(t("logs.refresh_failed"))
            return
        self.events_table.setRowCount(
            len(records)
        )

        for row, record in enumerate(records):

            values = [
                record.get("timestamp", ""),
                record.get("severity", ""),
                record.get("profile", ""),
                record.get("message", ""),
            ]

            for column, value in enumerate(values):

                self.events_table.setItem(
                    row,
                    column,
                    self._event_item(
                        value or "",
                        column,
                    ),
                )

    def _state_item(
        self,
        state,
    ):

        item = self._readonly_item(
            self._state_text(state)
        )

        if state == "Healthy":
            item.setForeground(
                QColor("#36c275")
            )
        elif state == "Recovering":
            item.setForeground(
                QColor("#ffb84d")
            )
        elif state == "Failed":
            item.setForeground(
                QColor("#ff6b6b")
            )
        elif state == "Queued":
            item.setForeground(
                QColor("#8aa1c9")
            )

        return item

    def _event_item(
        self,
        value,
        column,
    ):

        item = self._readonly_item(
            value
        )

        if column == 1:

            if value == "WARNING":
                item.setForeground(
                    QColor("#ffb84d")
                )

            elif value in (
                "ERROR",
                "CRITICAL",
            ):
                item.setForeground(
                    QColor("#ff6b6b")
                )

        return item

    def _status_text(
        self,
        status,
    ):

        mapping = {
            RUNTIME_STOPPED: "runtime.stopped",
            RUNTIME_PAUSED: "runtime.paused",
            RUNTIME_RUNNING: "runtime.running",
            RUNTIME_RECOVERING: "runtime.recovering",
            RUNTIME_ERROR: "runtime.error",
        }

        return t(
            mapping.get(
                status,
                status,
            )
        )

    def _state_text(
        self,
        state,
    ):

        mapping = {
            "Healthy": "runtime.state_healthy",
            "Idle": "runtime.state_idle",
            "Unknown": "runtime.state_unknown",
            "Recovering": "runtime.state_recovering",
            "Failed": "runtime.state_failed",
            "Queued": "runtime.state_queued",
        }

        return t(
            mapping.get(
                state,
                state,
            )
        )

    def _game_text(
        self,
        game,
    ):

        if isinstance(game, dict):
            game_id = game.get(
                "id",
                "-",
            )
        else:
            game_id = game

        return self.activation_service.get_game_display_name(
            game_id
        )
