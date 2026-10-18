import math
from concurrent.futures import ThreadPoolExecutor

from PySide6.QtCore import QObject, QTimer, Signal, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

from ui.app_icon import load_app_icon
from ui.i18n.translator import t


def indicator_state(status):
    state = status["state"]
    if state in {
        "restart_limit", "runtime_recovery_blocked", "runtime_recovery_failed",
        "degraded",
    }:
        return "failed"
    if state in {"starting", "restart_backoff", "studio_stopping"}:
        return "recovering"
    if status["studio_pid"] is None:
        return "recovering" if state == "running" else "ready"
    studio = status.get("studio_status") or {}
    runtime = studio.get("runtime") or {}
    runtime_state = str(runtime.get("status", "")).lower()
    profile_states = {
        str(profile.get("state", "")).lower()
        for profile in runtime.get("profiles", [])
        if isinstance(profile, dict)
    } if runtime.get("running") else set()
    if (
        runtime_state in {"failed", "error"}
        or "failed" in profile_states
        or status["health"] == "unhealthy"
    ):
        return "failed"
    if profile_states & {"recovering", "unknown"} or runtime_state in {
        "recovering", "starting",
    } or status["runtime_recovery"] in {
        "pending", "restoring",
    }:
        return "recovering"
    return "ready" if status["health"] == "healthy" else "recovering"


class WatchdogTray(QObject):
    status_ready = Signal(object)
    command_finished = Signal(str, object)
    command_failed = Signal(str, str)
    quit_requested = Signal()

    def __init__(self, service, logger, parent=None):
        super().__init__(parent)
        self.service = service
        self.logger = logger
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="WatchdogTray")
        self._refresh_pending = False
        self._busy = False
        self._closed = False
        self._phase = 0
        self._indicator = "recovering"
        self._base_icon = load_app_icon()
        if self._base_icon.isNull():
            raise FileNotFoundError("BotStudio application icon is unavailable")
        self.menu = QMenu()
        self.start_action = self.menu.addAction(t("watchdog.tray.start"))
        self.stop_action = self.menu.addAction(t("watchdog.tray.stop"))
        self.menu.addSeparator()
        self.exit_action = self.menu.addAction(t("watchdog.tray.exit"))
        self.start_action.setEnabled(False)
        self.stop_action.setEnabled(False)
        self.tray = QSystemTrayIcon(self._base_icon, self)
        self.tray.setContextMenu(self.menu)
        self.start_action.triggered.connect(lambda: self._command("start"))
        self.stop_action.triggered.connect(lambda: self._command("stop"))
        self.exit_action.triggered.connect(lambda: self._command("exit"))
        self.menu.aboutToShow.connect(self.refresh)
        self.status_ready.connect(self._show_status)
        self.command_finished.connect(self._finished)
        self.command_failed.connect(self._failed)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(100)
        self.tray.show()
        self.refresh()

    def _tick(self):
        self._phase = (self._phase + 1) % 20
        self.tray.setIcon(self._render_icon())
        if self._phase % 10 == 0:
            self.refresh()

    def _render_icon(self):
        pixmap = QPixmap(32, 32)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        self._base_icon.paint(painter, 0, 0, 32, 32)
        color = QColor({
            "ready": "#25c36a", "recovering": "#f2bd25", "failed": "#e54848",
        }[self._indicator])
        if self._indicator == "ready":
            color.setAlphaF(0.55 + 0.45 * (1 + math.sin(self._phase * math.pi / 10)) / 2)
        painter.setPen(QPen(QColor("#202020"), 1.5))
        painter.setBrush(color)
        painter.drawEllipse(19, 19, 12, 12)
        painter.end()
        return QIcon(pixmap)

    def refresh(self):
        if self._closed or self._refresh_pending or self._busy:
            return
        self._refresh_pending = True
        self.start_action.setEnabled(False)
        self.stop_action.setEnabled(False)
        self._executor.submit(self._read_status)

    def _read_status(self):
        try:
            self.status_ready.emit(self.service.get_status())
        except Exception as ex:
            self.logger.exception("Watchdog tray status query failed")
            self.command_failed.emit("status", str(ex))

    def _show_status(self, status):
        self._refresh_pending = False
        if self._closed:
            return
        self._indicator = indicator_state(status)
        running = status["studio_pid"] is not None
        self.start_action.setEnabled(not self._busy and not running)
        self.stop_action.setEnabled(
            not self._busy and running and status["state"] != "studio_stopping"
        )
        self.tray.setToolTip(
            "BotStudio Watchdog - " + t(f"watchdog.tray.{self._indicator}")
            + "\n" + t("watchdog.tray.running" if running else "watchdog.tray.waiting")
        )

    def _command(self, name):
        if self._closed or self._busy:
            return
        self._busy = True
        for action in (self.start_action, self.stop_action, self.exit_action):
            action.setEnabled(False)
        self._executor.submit(self._execute, name)

    def _execute(self, name):
        try:
            operation = {
                "start": self.service.start_studio,
                "stop": self.service.stop_studio,
                "exit": self.service.close_studio,
            }[name]
            result = operation()
            if result.get("accepted") is not True:
                raise RuntimeError(result.get("error") or result.get("state", "Command rejected"))
            self.command_finished.emit(name, result)
        except Exception as ex:
            self.logger.exception("Watchdog tray command failed", extra={"command": name})
            self.command_failed.emit(name, str(ex))

    def _finished(self, name, _result):
        self._busy = False
        self.exit_action.setEnabled(True)
        if name == "exit":
            self.quit_requested.emit()
        else:
            self.refresh()

    def _failed(self, name, message):
        if name == "status":
            self._refresh_pending = False
        else:
            self._busy = False
        self._indicator = "failed"
        self.exit_action.setEnabled(True)
        self.tray.setToolTip(t("watchdog.tray.failed") + ": " + message)
        self.tray.showMessage(
            "BotStudio Watchdog", message, QSystemTrayIcon.MessageIcon.Critical,
        )

    def close(self):
        self._closed = True
        self.timer.stop()
        self.tray.hide()
        self.menu.close()
        self._executor.shutdown(wait=False, cancel_futures=True)
        self.menu.deleteLater()
