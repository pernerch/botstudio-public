from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QWidget,
)

from core.emulator_manager import EmulatorManager
from services.adb_runtime_service import AdbRuntimeService
from services.emulator_health_service import (
    STATE_HEALTHY,
    EmulatorHealthService,
)
from services.game_runtime_service import GameRuntimeService
from services.game_service import GameService
from services.logging_service import get_logger
from services.runtime_recovery_service import RuntimeRecoveryService
from ui.i18n.translator import t


class ProfileEmulatorBar(QWidget):
    """Starts/stops one selected profile's emulator, independent of the global Runtime (DEC-089).

    Learning and Function screens need live screenshots from any profile of a
    game, including disabled ones, without requiring the whole Runtime to be
    started. This widget owns its own emulator/adb/recovery pipeline so it
    never depends on RuntimeService's start/stop state.
    """

    def __init__(
        self,
        emulator_manager=None,
        game_service=None,
        logger=None,
        parent=None,
    ):
        super().__init__(parent)

        self.emulator_manager = emulator_manager or EmulatorManager()
        self.game_service = game_service or GameService()
        self.logger = logger or get_logger("runtime")

        self.adb_runtime_service = AdbRuntimeService(
            self.emulator_manager.adb_path
        )
        self.health_service = EmulatorHealthService(
            self.emulator_manager,
            self.adb_runtime_service,
        )
        self.game_runtime_service = GameRuntimeService(
            self.emulator_manager.adb_path,
            self.game_service,
        )
        self.recovery_service = RuntimeRecoveryService(
            self.adb_runtime_service,
            self.game_runtime_service,
            self.emulator_manager,
        )

        self.profile = None
        self.running = False

        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 2, 4, 2)
        layout.setSpacing(4)

        self.btn_start = QPushButton(t("runtime.start_short"))
        self.btn_stop = QPushButton(t("runtime.stop_short"))
        self.btn_stop.setEnabled(False)
        self.lbl_status = QLabel(t("runtime.emulator_stopped"))
        self.lbl_status.setMinimumWidth(92)

        layout.addWidget(self.btn_start)
        layout.addWidget(self.btn_stop)
        layout.addWidget(self.lbl_status)

        self.btn_start.clicked.connect(self.start_profile)
        self.btn_stop.clicked.connect(self.stop_profile)

        self.timer = QTimer(self)
        self.timer.setInterval(2000)
        self.timer.timeout.connect(self._tick)

    def set_profile(self, profile: dict | None):
        """Selecting a different profile always stops the previous poll loop."""

        self.timer.stop()
        self.running = False
        self.profile = profile
        self.btn_stop.setEnabled(False)
        self.lbl_status.setText(t("runtime.emulator_stopped"))

    def start_profile(self):
        if not self.profile:
            self.lbl_status.setText(t("function.profile_required"))
            return

        self.logger.info(
            "Profile Emulator Start Requested",
            profile=self.profile.get("botstudio_profile_id", self.profile.get("id", "")),
            game=self.profile.get("game", ""),
            emulator_instance=self.profile.get("emulator_instance"),
        )

        self.running = True
        self.btn_stop.setEnabled(True)
        self.timer.start()
        self._tick()

    def stop_profile(self):
        if not self.profile:
            return

        instance = self.profile.get("emulator_instance")
        self.timer.stop()
        self.running = False
        self.btn_stop.setEnabled(False)

        try:
            self.emulator_manager.stop_instance(instance)
        except NotImplementedError:
            pass

        self.logger.info(
            "Profile Emulator Stop Requested",
            profile=self.profile.get("botstudio_profile_id", self.profile.get("id", "")),
            emulator_instance=instance,
        )
        self.lbl_status.setText(t("runtime.emulator_stopped"))

    def _tick(self):
        if not self.running or not self.profile:
            return

        status = self.health_service.validate(self.profile)

        if status["state"] != STATE_HEALTHY:
            recovery = self.recovery_service.recover_profile(self.profile, status)
            self.lbl_status.setText(
                f"{status['state']}: {recovery.get('message', '')}"
            )
            return

        self.lbl_status.setText(STATE_HEALTHY)
