from PySide6.QtCore import QTimer
from time import monotonic
from PySide6.QtWidgets import (
    QGroupBox,
    QHeaderView,
    QLabel,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ui.views.game_setup_view import (
    GameSetupView,
)

from ui.i18n.translator import t

from ui.views.runtime_overview_view import (
    RuntimeOverviewView,
)


class GameOverviewView(QWidget):

    def __init__(
        self,
        game_id: str,
        profile=None,
        device_id=None,
        connected=False,
        runtime_service=None,
        emulator_scan_service=None,
    ):
        super().__init__()

        self.runtime_overview = None
        self.profile = profile
        self.emulator_scan_service = emulator_scan_service
        self.profile_name_map = {}

        layout = QVBoxLayout(
            self
        )

        self.tabs = QTabWidget()

        overview = QWidget()

        overview_layout = QVBoxLayout(
            overview
        )

        overview_layout.addWidget(
            QLabel(
                f"Game: {game_id}"
            )
        )

        profile_name = (
            profile.get(
                "name",
                "-"
            )
            if profile
            else "-"
        )

        self.profile_name_label = QLabel(f"Profile: {profile_name}")
        overview_layout.addWidget(self.profile_name_label)

        self.device_label = QLabel(t("game_overview.device_value").format(value="-"))
        self.connection_label = QLabel(
            t("game_overview.connection_value").format(value=t("runtime.state_unknown")),
        )
        overview_layout.addWidget(self.device_label)
        overview_layout.addWidget(self.connection_label)

        self.runtime_overview = RuntimeOverviewView(
            runtime_service=runtime_service
        )

        overview_layout.addWidget(
            self.runtime_overview,
            stretch=1,
        )

        sessions_group = QGroupBox(
            t("game_overview.detected_sessions")
        )
        sessions_layout = QVBoxLayout(sessions_group)

        self.sessions_table = QTableWidget(0, 6)
        self.sessions_table.setHorizontalHeaderLabels([
            t("game_overview.instance"),
            t("game_overview.device"),
            t("game_overview.running_app"),
            t("game_overview.game"),
            t("runtime.profile"),
            t("runtime.state"),
        ])
        self.sessions_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.Stretch
        )
        sessions_layout.addWidget(self.sessions_table)

        overview_layout.addWidget(sessions_group)

        overview_layout.addStretch()

        self.tabs.addTab(
            overview,
            t("game_overview.overview"),
        )

        self.tabs.addTab(
            GameSetupView(),
            t("game_setup.tab"),
        )

        layout.addWidget(
            self.tabs
        )

        self.refresh_sessions()
        self._sessions_timer = QTimer(self)
        self._sessions_timer.setInterval(5000)
        self._sessions_timer.timeout.connect(self.refresh_sessions)
        self._sessions_timer.start()

    def show_setup_tab(self):
        self.tabs.setCurrentIndex(1)

    def set_profile(self, profile):
        self.profile = profile
        profile_name = profile.get("name", "-") if profile else "-"
        self.profile_name_label.setText(f"Profile: {profile_name}")
        self.refresh_sessions()

    def refresh_games(self):
        setup_view = self.tabs.widget(1)
        if hasattr(setup_view, "refresh_games"):
            setup_view.refresh_games()
        self.refresh_sessions()

    def refresh_sessions(self):
        if not self.emulator_scan_service:
            return

        sessions = self.emulator_scan_service.last_scan or []
        selected = next((
            entry for entry in sessions
            if self.profile and entry.get("instance") == self.profile.get("emulator_instance")
            and monotonic() - entry.get("scanned_at", 0) <= 15
        ), None)
        self.device_label.setText(t("game_overview.device_value").format(
            value=selected.get("device_id") or "-" if selected else "-",
        ))
        self.connection_label.setText(t("game_overview.connection_value").format(
            value=self.runtime_overview._state_text(selected.get("state")) if selected
            else t("runtime.state_unknown"),
        ))
        self.sessions_table.setRowCount(len(sessions))

        for row, entry in enumerate(sessions):
            entry = dict(entry)
            if monotonic() - entry.get("scanned_at", 0) > 15:
                entry.update(
                    state="Unknown", foreground_package=None,
                    game_id=None, profile_id=None, profile_name=None,
                )
            profile_id = str(entry.get("profile_id") or "")
            profile_name = self.profile_name_map.get(
                profile_id,
                entry.get("profile_name") or "-",
            )
            self.sessions_table.setItem(
                row, 0, QTableWidgetItem(str(entry.get("instance", "-")))
            )
            self.sessions_table.setItem(
                row, 1, QTableWidgetItem(entry.get("device_id") or "-")
            )
            self.sessions_table.setItem(
                row, 2, QTableWidgetItem(entry.get("foreground_package") or "-")
            )
            self.sessions_table.setItem(
                row, 3, QTableWidgetItem(entry.get("game_id") or "-")
            )
            # unmatched MultiAccount profiles stay blank: which account is
            # active on a shared instance cannot be inferred from the outside
            self.sessions_table.setItem(
                row, 4, QTableWidgetItem(profile_name)
            )
            self.sessions_table.setItem(
                row, 5, QTableWidgetItem(self.runtime_overview._state_text(entry.get("state", "Unknown")))
            )