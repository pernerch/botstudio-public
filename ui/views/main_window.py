from contextlib import contextmanager
from services.application_info import APP_NAME, APP_VERSION

from PySide6.QtCore import Qt, Signal
from PySide6.QtCore import QTimer

from PySide6.QtWidgets import (
    QMainWindow,
    QSplitter,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from services.app_config_service import (
    AppConfigService,
)

from services.game_service import (
    GameService,
)

from services.profile_runtime_service import (
    ProfileRuntimeService,
)

from ui.views.active_game_view import (
    ActiveGameView,
)

from ui.views.game_overview_view import (
    GameOverviewView,
)

from ui.views.log_view import (
    LogView,
)

from ui.views.navigation_view import (
    NavigationView,
)

from ui.views.profile_view import (
    ProfileView,
)

from ui.views.learning_view import (
    LearningView,
)

from ui.views.settings_view import (
    SettingsView,
)

from ui.views.telegram_remote_control_view import (
    TelegramRemoteControlView,
)

from ui.views.scheduler_view import (
    SchedulerView,
)

from ui.views.statistics_view import (
    StatisticsView,
)

from ui.views.function_editor_view import (
    FunctionEditorView,
)

from ui.views.dependencies_view import (
    DependenciesView,
)

from ui.views.debug_view import (
    DebugView,
)

from services.emulator_scan_service import (
    EmulatorScanService,
)

from services.runtime_service import (
    RuntimeService,
)
from services.watchdog_control_service import (
    WatchdogControlService,
)
from services.watchdog_ipc_service import (
    WatchdogIpcServer,
)
from services.reinitialize_service import get_default_reinitialize_service
from services.package_activation_service import PackageActivationService
from services.package_service import PackageError
from ui.widgets.managed_packages_dialog import ManagedPackagesDialog

from ui.widgets.runtime_status_bar import (
    RuntimeStatusBar,
)

from ui.widgets.app_header_bar import (
    AppHeaderBar,
)
from ui.i18n.translator import set_language, t

class MainWindow(QMainWindow):
    data_reinitialized = Signal(str, object)

    def __init__(
        self,
    ):
        super().__init__()
        self.setWindowTitle(f"{APP_NAME} --V{APP_VERSION}--")
        self._reinitialize_service = get_default_reinitialize_service()
        self._reinitialize_listener = self._queue_reinitialize
        self._reinitialize_service.subscribe(self._reinitialize_listener)
        self.data_reinitialized.connect(
            self._handle_reinitialize,
            Qt.ConnectionType.QueuedConnection,
        )

        self.config_service = (
            AppConfigService()
        )
        set_language(
            self.config_service.get_studio_settings().get("language", "en")
        )

        self.game_service = (
            GameService()
        )

        self.profile_runtime_service = (
            ProfileRuntimeService()
        )

        self.runtime_service = RuntimeService()
        self.runtime_service.scheduler_runner_factory = (
            self.runtime_service.create_function_runner
        )
        self.watchdog_control_service = WatchdogControlService(
            self.runtime_service,
            self,
        )
        self.watchdog_control_service.studio_stop_requested.connect(
            lambda: QTimer.singleShot(100, self.close)
        )
        self.watchdog_ipc_server = WatchdogIpcServer(
            self.watchdog_control_service.handle_request,
            self,
            logger=self.runtime_service.logger,
        )
        try:
            self.watchdog_ipc_server.start()
        except OSError as ex:
            self.runtime_service.logger.error(
                "Watchdog IPC Server Start Failed",
                error=str(ex),
            )

        self.emulator_scan_service = EmulatorScanService()
        self.emulator_scan_service.start(interval_seconds=5)

        active_game = (
            self.config_service.get_active_game()
        )

        if not active_game:

            default_game = (
                self.game_service.get_default_game()
            )

            if default_game:

                active_game = (
                    default_game["id"]
                )

                self.config_service.set_active_game(
                    active_game
                )

        self.active_game = (
            active_game
        )

        self.runtime_context = (
            self.profile_runtime_service
            .get_runtime_context(
                self.active_game
            )
        )

        self.active_profile = (
            self.runtime_context[
                "profile"
            ]
        )

        self.device_id = (
            self.runtime_context[
                "device_id"
            ]
        )

        self.adb_connected = (
            self.runtime_context[
                "connected"
            ]
        )

        splitter = QSplitter(
            Qt.Horizontal
        )

        self.navigation = (
            NavigationView()
        )

        self.navigation.menu.currentRowChanged.connect(
            self.on_navigation_changed
        )

       # self.navigation.menu.setCurrentRow(
       #     0
       # )

        self.game_view = (
            ActiveGameView(
                self.active_game
            )
        )

        self.game_overview = GameOverviewView(
            game_id=self.active_game,
            profile=self.active_profile,
            device_id=self.device_id,
            connected=self.adb_connected,
            runtime_service=self.runtime_service,
            emulator_scan_service=self.emulator_scan_service,
        )

        if not self.active_game:
            self.game_overview.show_setup_tab()

        self.game_view.set_tab(
            "Overview",
            self.game_overview,
        )
        self._add_packages_tab()

        self.profile_view = (
            ProfileView()
        )

        self.learning_view = (
            LearningView()
        )   

        self.scheduler_view = SchedulerView(
            game_id=self.active_game,
            runtime_service=self.runtime_service,
        )

        self.statistics_view = StatisticsView()

        self.settings_view = (
            SettingsView(
                game_id=self.active_game,
            )
        )

        self.telegram_remote_control_view = TelegramRemoteControlView()
        QTimer.singleShot(0, self.telegram_remote_control_view.start_watchdog_if_enabled)

        self.function_editor_view = FunctionEditorView(
            runtime_service=self.runtime_service,
        )

        self.dependencies_view = DependenciesView(
            game_id=self.active_game,
        )
        self.dependencies_view.open_function_requested.connect(
            self.open_function_in_editor
        )
        self.dependencies_view.open_task_requested.connect(
            self.open_task_in_scheduler
        )

        self.debug_view = DebugView(
            runtime_service=self.runtime_service,
        )
        # live block events come from the editor's runtime supervisor
        self.debug_view.supervisor = self.function_editor_view.supervisor

        self.log_view = LogView()
        self.log_view.open_target_requested.connect(self.open_log_target)

        self.content_stack = (
            QStackedWidget()
        )

        self.content_stack.addWidget(
            self.game_view
        )

        self.content_stack.addWidget(
            self.profile_view
        )

        self.content_stack.addWidget(
            self.learning_view
        )

        self.content_stack.addWidget(
            self.scheduler_view
        )

        self.content_stack.addWidget(
            self.statistics_view
        )

        self.content_stack.addWidget(
            self.settings_view
        )

        self.content_stack.addWidget(
            self.telegram_remote_control_view
        )

        self.content_stack.addWidget(
            self.function_editor_view
        )

        self.content_stack.addWidget(
            self.dependencies_view
        )

        self.content_stack.addWidget(
            self.debug_view
        )

        self.content_stack.addWidget(
            self.log_view
        )

        self.view_indexes = {
            "game": self.content_stack.indexOf(self.game_view),
            "profiles": self.content_stack.indexOf(self.profile_view),
            "learning": self.content_stack.indexOf(self.learning_view),
            "scheduler": self.content_stack.indexOf(self.scheduler_view),
            "statistics": self.content_stack.indexOf(self.statistics_view),
            "settings": self.content_stack.indexOf(self.settings_view),
            "telegram": self.content_stack.indexOf(
                self.telegram_remote_control_view
            ),
            "functions": self.content_stack.indexOf(self.function_editor_view),
            "dependencies": self.content_stack.indexOf(self.dependencies_view),
            "debug": self.content_stack.indexOf(self.debug_view),
            "logs": self.content_stack.indexOf(self.log_view),
        }

        splitter.addWidget(
            self.navigation
        )

        splitter.addWidget(
            self.content_stack
        )

        splitter.setStretchFactor(
            0,
            1,
        )

        splitter.setStretchFactor(
            1,
            8,
        )

        self.header_bar = AppHeaderBar()
        self.telegram_remote_control_view.github_account.account_changed.connect(
            self._github_account_changed
        )

        central = QWidget()

        central_layout = QVBoxLayout(
            central
        )

        central_layout.setContentsMargins(
            0,
            0,
            0,
            0,
        )

        central_layout.setSpacing(
            0
        )

        central_layout.addWidget(
            self.header_bar
        )

        central_layout.addWidget(
            splitter,
            stretch=1,
        )

        self.setCentralWidget(
            central
        )

        self.runtime_status_bar = RuntimeStatusBar(
            self.runtime_service
        )
        if self.game_overview.runtime_overview:
            self.game_overview.runtime_overview.action_feedback.connect(
                self.runtime_status_bar.set_action_feedback
            )

        self.statusBar().addPermanentWidget(
            self.runtime_status_bar,
            1,
        )

        self.runtime_status_bar.problem_clicked.connect(
            self.on_runtime_problem_clicked
        )

        self.runtime_status_bar.start_clicked.connect(
            self.start_runtime_from_status_bar
        )

        self.runtime_status_bar.stop_clicked.connect(
            self.stop_runtime_from_status_bar
        )

        self._apply_application_start_view()

    def _apply_application_start_view(self):
        startup_view = self.config_service.get_studio_settings().get(
            "startup_view",
            "game",
        )
        self.navigation.select_view(
            "scheduler" if startup_view.startswith("scheduler_") else startup_view
        )
        if startup_view == "scheduler_timeline":
            self.scheduler_view.set_view_mode("Timeline")
        elif startup_view == "scheduler_tree":
            self.scheduler_view.set_view_mode("Tree")

    def on_navigation_changed(
        self,
        row,
    ):
        item = self.navigation.menu.item(row)
        key = item.data(Qt.UserRole) if item else "game"
        if key == "game":
            self._game_tab_changed(self.game_view.tabs.currentIndex())
        elif key == "profiles":
            self.profile_view.refresh_profiles()
        elif key == "learning":
            self.learning_view.refresh_profiles()
            self.learning_view.refresh_library_items()
        elif key == "functions":
            self.function_editor_view.refresh_profiles()
            self.function_editor_view.refresh_function_list()
            self.function_editor_view.refresh_library_choices()
            self.function_editor_view.refresh_variable_choices()
        elif key == "dependencies":
            self.dependencies_view.refresh()
        elif key == "scheduler":
            self.scheduler_view.refresh()
        elif key == "statistics":
            self.statistics_view.refresh()
        elif key == "telegram":
            self.telegram_remote_control_view.load_settings()
        elif key == "logs":
            self.log_view.refresh()
        self.content_stack.setCurrentIndex(self.view_indexes.get(key, 0))

    def open_function_in_editor(self, game_id, function_id):
        menu = self.navigation.menu
        for row in range(menu.count()):
            if menu.item(row).data(Qt.UserRole) == "functions":
                menu.setCurrentRow(row)
                break
        self.function_editor_view.open_function(game_id, function_id)

    def open_task_in_scheduler(self, game_id, event_id, task_id):
        menu = self.navigation.menu
        for row in range(menu.count()):
            if menu.item(row).data(Qt.UserRole) == "scheduler":
                menu.setCurrentRow(row)
                break
        self.scheduler_view.reveal_task(game_id, event_id, task_id)

    def open_log_target(self, target):
        if not isinstance(target, dict):
            return
        if target.get("kind") == "task":
            self.open_task_in_scheduler(
                target.get("game_id", ""),
                target.get("event_id", ""),
                target.get("task_id", ""),
            )
        elif target.get("kind") == "function":
            self.open_function_in_editor(
                target.get("game_id", ""), target.get("function_id", "")
            )

    def _queue_reinitialize(self, source, payload):
        self.data_reinitialized.emit(source, payload)

    def _package_debug_idle(self):
        editor = self.function_editor_view
        return editor._save_task is None and not any(
            worker.process is not None and worker.process.is_alive()
            for worker in editor.supervisor.workers.values()
        )

    def _github_account_changed(self, account):
        self.header_bar.set_github_account(account)

    def _add_packages_tab(self):
        self.packages_view = ManagedPackagesDialog(self.game_view, embedded=True)
        self.packages_view.activation_service = PackageActivationService(
            managed=self.packages_view.service,
            execution_guard=self._package_execution_guard,
            logger=self.packages_view.logger,
        )
        self.game_view.set_tab(t("packages.tab"), self.packages_view)
        self.game_view.tabs.currentChanged.connect(self._game_tab_changed)

    def _game_tab_changed(self, index):
        if (
            self.game_view.tabs.widget(index) is self.packages_view
            and self.packages_view.worker is None
            and self.packages_view.confirmation is None
        ):
            self.packages_view.reload()

    @contextmanager
    def _package_execution_guard(self):
        with self.runtime_service.content_import_guard():
            if not self._package_debug_idle():
                raise PackageError("Stop debug runs and wait for Function saves before activating packages")
            yield

    def _handle_reinitialize(self, source, payload):
        if source == "profile":
            self.profile_view.refresh_profiles()
            self.refresh_profile_consumers()
        elif source == "game":
            self.profile_view.load_games()
            self.profile_view.refresh_profiles()
            self.function_editor_view.refresh_games()
            self.scheduler_view.refresh_games()
            self.settings_view.refresh_games()
            self.game_overview.refresh_games()
        elif source == "function":
            self.function_editor_view.refresh_function_list()
            if self.content_stack.currentWidget() is self.scheduler_view:
                self.scheduler_view.refresh()
        elif source == "library":
            self.learning_view.refresh_library_items()
            self.function_editor_view.refresh_library_choices()
        elif source == "variable":
            # Profile value updates do not change the editor's variable definitions.
            if "profile_id" not in payload:
                self.function_editor_view.refresh_variable_choices()
        elif source == "scheduler":
            if self.content_stack.currentWidget() is self.scheduler_view:
                self.scheduler_view.refresh()
        elif source == "app_config":
            self.settings_view.load_settings()
            self.telegram_remote_control_view.load_settings()

    def refresh_profile_consumers(self):
        profiles = self.profile_view.profile_service.load_all()
        profiles_by_id = {}
        profile_name_map = {}
        for profile in profiles:
            profile_id = str(profile.get("id") or "")
            profile_name = str(profile.get("name") or profile_id)
            for identity in (
                profile.get("id"),
                profile.get("botstudio_profile_id"),
            ):
                if identity:
                    identity = str(identity)
                    profiles_by_id[identity] = profile
                    profile_name_map[identity] = profile_name

        self.learning_view.refresh_profiles()
        self.function_editor_view.refresh_profiles()
        self.scheduler_view.refresh()
        self.statistics_view.refresh()
        self.log_view.refresh()

        if self.game_overview.runtime_overview:
            self.game_overview.runtime_overview.profile_name_map = profile_name_map
            self.game_overview.runtime_overview.refresh_profiles()
            self.game_overview.runtime_overview.refresh_runtime_status()
        self.runtime_status_bar.profile_name_map = profile_name_map
        self.game_overview.profile_name_map = profile_name_map

        if self.active_profile:
            profile_id = str(
                self.active_profile.get("botstudio_profile_id")
                or self.active_profile.get("id")
                or ""
            )
            self.active_profile = profiles_by_id.get(profile_id) or self.active_profile
        self.game_overview.set_profile(self.active_profile)
        self.game_overview.refresh_sessions()
        self.runtime_status_bar.refresh()

    def on_runtime_problem_clicked(
        self,
        status,
    ):

        self.navigation.select_view("game")

        overview = self.game_view.tabs.widget(
            0
        )

        if hasattr(overview, "runtime_overview"):
            overview.runtime_overview.select_profile(
                status.get("profile_name")
            )

        self.navigation.select_view("logs")

        self.log_view.apply_context_filter(
            profile=status.get("profile_name"),
            minimum_severity="WARNING",
        )

    def start_runtime_from_status_bar(
        self,
    ):

        self.runtime_status_bar.set_action_feedback(
            t("runtime.start_queued")
        )

        overview = self.game_view.tabs.widget(
            0
        )

        if hasattr(overview, "runtime_overview"):
            overview.runtime_overview.start_runtime()
            self.runtime_status_bar.refresh()

    def stop_runtime_from_status_bar(
        self,
    ):
        # Stop is a control command and must never wait behind the worker pool.
        self.runtime_status_bar.set_action_feedback(
            t("runtime.stop_requested")
        )
        self.runtime_service.stop()
        self.runtime_status_bar.refresh()
        self.runtime_status_bar.set_action_feedback(
            t("runtime.stop_complete")
        )

    def closeEvent(
        self,
        event,
    ):

        if self.packages_view.worker is not None or self.packages_view.confirmation is not None:
            event.ignore()
            return
        self.watchdog_ipc_server.stop()
        self._reinitialize_service.unsubscribe(self._reinitialize_listener)
        self.emulator_scan_service.stop()

        try:

            if self.active_profile:

                self.profile_runtime_service\
                    .disconnect_profile(
                        self.active_profile
                    )

        except Exception as ex:

            print(
                "Disconnect failed:",
                ex,
            )

        super().closeEvent(
            event
        )
