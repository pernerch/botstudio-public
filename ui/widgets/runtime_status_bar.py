from datetime import datetime

from PySide6.QtCore import Signal, QTimer

from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QMenu,
    QPushButton,
    QWidget,
)

from services.runtime_service import (
    RUNTIME_ERROR,
    RUNTIME_RECOVERING,
    RUNTIME_RUNNING,
    RUNTIME_PAUSED,
)
from services.theme_service import ThemeService
from ui.i18n.translator import t


class RuntimeStatusBar(QWidget):

    problem_clicked = Signal(dict)
    start_clicked = Signal()
    stop_clicked = Signal()

    def __init__(
        self,
        runtime_service,
        parent=None,
    ):
        super().__init__(parent)

        self.runtime_service = runtime_service
        self.profile_name_map = {}
        self.started_at = None
        self._paused = False
        self.problem_status = None

        font_px = (
            ThemeService()
            .get_compact_font_size_px()
        )

        self._font_css = f"font-size: {font_px}px;"
        self._active_control_css = (
            "background-color: #FDC601; color: #111827; font-weight: bold;"
        )

        # buttons stay at the theme size, only the readouts are compact
        self.setStyleSheet(
            f"QLabel {{ {self._font_css} }}"
        )

        layout = QHBoxLayout(
            self
        )

        layout.setContentsMargins(
            4,
            2,
            4,
            2,
        )

        self.lbl_runtime = QLabel("-")
        self.lbl_healthy = QLabel("-")
        self.lbl_recovering = QLabel("-")
        self.lbl_failed = QLabel("-")
        self.lbl_queued = QLabel("-")
        self.lbl_detail = QLabel("-")
        self.lbl_action = QLabel("")
        self.lbl_uptime = QLabel("-")

        self.btn_start = QPushButton(
            t("runtime.start_short")
        )

        self.btn_pause = QPushButton(
            t("runtime.pause_short")
        )
        self.pause_menu = QMenu(self.btn_pause)
        self.pause_menu.aboutToShow.connect(self._refresh_pause_menu)
        self.btn_pause.setMenu(self.pause_menu)

        self.btn_stop = QPushButton(
            t("runtime.stop_short")
        )

        layout.addWidget(self.lbl_runtime)
        layout.addWidget(self.lbl_healthy)
        layout.addWidget(self.lbl_recovering)
        layout.addWidget(self.lbl_failed)
        layout.addWidget(self.lbl_queued)
        layout.addWidget(self.lbl_detail, stretch=1)
        layout.addWidget(self.lbl_action)
        layout.addWidget(self.lbl_uptime)
        layout.addWidget(self.btn_start)
        layout.addWidget(self.btn_pause)
        layout.addWidget(self.btn_stop)

        self.lbl_detail.mousePressEvent = self._on_detail_clicked

        self.btn_start.clicked.connect(
            self.start_clicked.emit
        )

        self.btn_stop.clicked.connect(
            self.stop_clicked.emit
        )

        self.timer = QTimer(
            self
        )

        self.timer.setInterval(
            1000
        )

        self.timer.timeout.connect(
            self.refresh
        )

        self.timer.start()
        self.refresh()

    def _update_control_styles(self, status):
        for button in (self.btn_start, self.btn_pause, self.btn_stop):
            button.setStyleSheet("")

        if self._paused and self.runtime_service.running:
            self.btn_pause.setStyleSheet(self._active_control_css)
        elif status == RUNTIME_RUNNING or status == RUNTIME_RECOVERING:
            self.btn_start.setStyleSheet(self._active_control_css)

    def pause_runtime(
        self,
        instance=None,
        paused=None,
    ):

        if paused is None:
            paused = not self.runtime_service.paused
        try:
            if paused:
                self.runtime_service.pause(instance)
            else:
                self.runtime_service.resume(instance)
        except (ValueError, RuntimeError) as error:
            self.set_action_feedback(str(error))
            return
        self._paused = self.runtime_service.paused
        self.set_action_feedback(
            t("runtime.pause_complete") if paused else t("runtime.resume_complete"),
        )
        self.refresh()

    def _refresh_pause_menu(self):
        status = self.runtime_service.get_status()
        pauses = status.get("paused_emulators", {})
        self.pause_menu.clear()
        for label, paused in (
            ("runtime.pause_all", True), ("runtime.resume_all", False),
        ):
            action = self.pause_menu.addAction(t(label))
            action.triggered.connect(
                lambda _checked=False, value=paused: self.pause_runtime(paused=value)
            )
        self.pause_menu.addSeparator()
        instances = {
            str(profile["emulator_instance"])
            for profile in status.get("profiles", [])
            if profile.get("emulator_instance") is not None
        } | set(pauses)
        for instance in sorted(
            instances,
            key=lambda value: (0, int(value)) if value.isascii() and value.isdigit() else (1, value),
        ):
            pause = pauses.get(instance)
            blocked = pause is not None or status.get("pause_all", False)
            label = t("runtime.resume_emulator" if blocked else "runtime.pause_emulator").format(
                instance=instance,
            )
            action = self.pause_menu.addAction(label)
            action.setEnabled(not pause or pause["state"] != "resuming")
            action.triggered.connect(
                lambda _checked=False, target=instance, value=not blocked:
                self.pause_runtime(instance=target, paused=value)
            )

    def set_action_feedback(self, message):
        self.lbl_action.setText(message)
        self.lbl_action.setToolTip(message)

    def refresh(
        self,
    ):

        status = self.runtime_service.get_status()
        self._paused = self.runtime_service.paused or bool(status.get("paused_emulators"))
        self.btn_pause.setEnabled(self.runtime_service.running)
        pause_count = len(status.get("paused_emulators", {}))
        self.btn_pause.setText(
            f"{t('runtime.pause_short')} ({pause_count})" if pause_count else t("runtime.pause_short")
        )
        self.btn_pause.setToolTip(f"{t('runtime.status_paused_prefix')}: {pause_count}")
        if not self.runtime_service.running:
            self._paused = False
        profiles = []
        for profile_status in status.get("profiles", []):
            profile_status = dict(profile_status)
            profile_id = str(profile_status.get("profile_id") or "")
            profile_status["profile_name"] = self.profile_name_map.get(
                profile_id,
                profile_status.get("profile_name", "-"),
            )
            profiles.append(profile_status)
        counts = self._counts(profiles)

        if status["status"] == RUNTIME_RUNNING and not self.started_at:
            self.started_at = datetime.now()

        if status["status"] != RUNTIME_RUNNING and status["status"] != RUNTIME_RECOVERING:
            self.started_at = None

        self.lbl_runtime.setText(
            self._runtime_text(status["status"])
        )

        self.lbl_healthy.setText(
            f"{t('runtime.status_healthy_prefix')}: {counts['healthy']}"
        )

        self.lbl_healthy.setStyleSheet(
            f"color: #36c275; font-weight: bold; {self._font_css}"
        )

        self.lbl_recovering.setText(
            f"{t('runtime.status_recovering_prefix')}: {counts['recovering']}"
        )

        self.lbl_recovering.setStyleSheet(
            f"color: #ffb84d; font-weight: bold; {self._font_css}"
        )

        self.lbl_failed.setText(
            f"{t('runtime.status_failed_prefix')}: {counts['failed']}"
        )

        self.lbl_failed.setStyleSheet(
            f"color: #ff6b6b; font-weight: bold; {self._font_css}"
        )

        self.lbl_queued.setText(
            f"{t('runtime.status_queued_prefix')}: {counts['queued']}"
        )

        self.lbl_queued.setStyleSheet(
            f"color: #8aa1c9; font-weight: bold; {self._font_css}"
        )

        self.problem_status = self._first_problem(profiles)

        if self.problem_status:
            self.lbl_detail.setText(
                f"{self.problem_status.get('profile_name', '-')} "
                f"{self.problem_status.get('message', '-')}"
            )
        else:
            self.lbl_detail.setText(
                t("runtime.no_problems")
            )

        self.lbl_uptime.setText(
            self._uptime_text()
        )
        self._update_control_styles(status["status"])

    def _counts(
        self,
        profiles,
    ):

        counts = {
            "healthy": 0,
            "recovering": 0,
            "failed": 0,
            "queued": 0,
        }

        for profile in profiles:

            state = profile.get("state")

            if state == "Healthy":
                counts["healthy"] += 1
            elif state == "Recovering":
                counts["recovering"] += 1
            elif state == "Failed":
                counts["failed"] += 1
            elif state == "Queued":
                counts["queued"] += 1

        return counts

    def _first_problem(
        self,
        profiles,
    ):

        for profile in profiles:

            if profile.get("state") in (
                "Failed",
                "Recovering",
                "Unknown",
            ):
                return profile

        return None

    def _runtime_text(
        self,
        status,
    ):

        if status == RUNTIME_RUNNING:
            return t("runtime.running")
        if status == RUNTIME_PAUSED:
            return t("runtime.paused")

        if status == RUNTIME_RECOVERING:
            return t("runtime.recovering")

        if status == RUNTIME_ERROR:
            return t("runtime.error")

        return t("runtime.stopped")

    def _uptime_text(
        self,
    ):

        if not self.started_at or not self.runtime_service.running:
            return t("runtime.uptime_zero")

        delta = datetime.now() - self.started_at
        seconds = int(delta.total_seconds())
        hours = seconds // 3600
        minutes = (seconds % 3600) // 60
        seconds = seconds % 60

        return f"{t('runtime.uptime')}: {hours:02}:{minutes:02}:{seconds:02}"

    def _on_detail_clicked(
        self,
        event,
    ):

        if self.problem_status:
            self.problem_clicked.emit(
                self.problem_status
            )
