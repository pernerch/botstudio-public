from pathlib import Path

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)
from PySide6.QtGui import QIcon
from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal, Slot
from PySide6.QtWidgets import QTabWidget

from services.app_config_service import AppConfigService
from services.game_service import GameService
from services.logging_service import get_logger
from ui.i18n.translator import LANGUAGES, t
from ui.widgets.searchable_combo import SearchableComboBox


STUDIO_DEFAULTS = dict(AppConfigService.STUDIO_SETTINGS_DEFAULTS)
SCHEDULING_DEFAULTS = dict(AppConfigService.SCHEDULING_DEFAULTS)


class _SettingsSignals(QObject):
    completed = Signal(object, str)


class _SettingsOperation(QRunnable):
    def __init__(self, action):
        super().__init__()
        self.action = action
        self.signals = _SettingsSignals()

    def run(self):
        try:
            result = self.action()
        except Exception as exc:
            self.signals.completed.emit(None, str(exc))
        else:
            self.signals.completed.emit(result, "")


class SettingsView(QWidget):
    """Global Studio settings and explicit per-game scheduling overrides."""

    IDLE_BEHAVIORS = ("do_nothing", "close_game", "close_emulator")
    STOP_BEHAVIORS = ("do_nothing", "close_emulator")
    TELEGRAM_MODES = ("continuous", "once")
    STARTUP_VIEWS = (
        ("Game", "game"),
        ("Scheduler Tree", "scheduler_tree"),
        ("Scheduler Timeline", "scheduler_timeline"),
        ("Settings", "settings"),
    )

    def __init__(self, game_id: str = "", parent=None):
        super().__init__(parent)

        self.game_service = None
        self.app_config_service = None
        self.game_id = game_id
        self.logger = get_logger("settings")
        self._operation = None
        self._operation_callback = None
        self._games = {}
        self._game_overrides = {}
        self._game_drafts = {}
        self._displayed_game_id = ""
        self._rendering_game = False
        self._globals_loaded = False

        layout = QVBoxLayout(self)
        self.tabs = QTabWidget()
        general_tab = QWidget()
        scheduling_tab = QWidget()
        games_tab = QWidget()
        general_layout = QVBoxLayout(general_tab)
        scheduling_layout = QVBoxLayout(scheduling_tab)
        games_layout = QVBoxLayout(games_tab)
        self.tabs.addTab(general_tab, t("settings.tab_general"))
        self.tabs.addTab(scheduling_tab, t("settings.tab_scheduling"))
        self.tabs.addTab(games_tab, t("settings.tab_games"))
        layout.addWidget(self.tabs)

        language_group = QGroupBox(t("settings.language"))
        language_layout = QFormLayout(language_group)
        self.language_combo = QComboBox()
        flag_root = Path(__file__).resolve().parents[2] / "assets" / "flags"
        for language_code, language_name, flag_code in LANGUAGES:
            flag_path = flag_root / f"{language_code}.png"
            icon = QIcon(str(flag_path)) if flag_path.is_file() else QIcon()
            self.language_combo.addItem(icon, language_name, language_code)
        self.language_combo.setToolTip(t("settings.language_restart_hint"))
        language_layout.addRow(t("settings.language"), self.language_combo)
        general_layout.addWidget(language_group)

        header = QHBoxLayout()
        header.addWidget(QLabel(t("settings.game")))
        self.game_combo = SearchableComboBox()
        header.addWidget(self.game_combo, stretch=1)
        self.btn_save = QPushButton(t("common.save"))
        games_layout.addLayout(header)

        instance_group = QGroupBox(t("settings.instance_settings"))
        instance_layout = QFormLayout(instance_group)

        self.idle_behavior_combo = self._option_combo(self.IDLE_BEHAVIORS)
        self.max_idle_spin = self._minutes_spin()
        self.max_concurrent_spin = QSpinBox()
        self.max_concurrent_spin.setRange(1, 99)

        row = QHBoxLayout()
        row.addWidget(self.idle_behavior_combo, stretch=1)
        row.addWidget(QLabel(t("settings.max_concurrent_instances")))
        row.addWidget(self.max_concurrent_spin)
        instance_layout.addRow(t("settings.idle_behavior"), row)
        instance_layout.addRow(t("settings.max_idle_minutes"), self.max_idle_spin)

        self.limit_active_time_check = QCheckBox(t("settings.limit_profile_active_time"))
        self.max_active_spin = self._minutes_spin()
        self.max_active_spin.setMinimum(1)
        active_row = QHBoxLayout()
        active_row.addWidget(self.limit_active_time_check)
        active_row.addStretch(1)
        active_row.addWidget(self.max_active_spin)
        active_row.addWidget(QLabel(t("settings.max_active_minutes")))
        scheduling_form = QFormLayout()
        scheduling_layout.addLayout(scheduling_form)
        scheduling_form.addRow(active_row)
        self.limit_active_time_check.toggled.connect(
            self.max_active_spin.setEnabled
        )
        self.lookahead_spin = QSpinBox()
        self.lookahead_spin.setRange(0, 3600)
        self.max_wait_spin = self._minutes_spin()
        self.max_wait_spin.setMinimum(1)
        scheduling_form.addRow(t("settings.scheduling_lookahead_seconds"), self.lookahead_spin)
        scheduling_form.addRow(t("settings.scheduling_max_wait_minutes"), self.max_wait_spin)
        scheduling_hint = QLabel(t("settings.scheduling_help"))
        scheduling_hint.setWordWrap(True)
        scheduling_form.addRow(scheduling_hint)
        scheduling_layout.addStretch(1)

        self.stop_behavior_combo = self._option_combo(self.STOP_BEHAVIORS)
        self.auto_start_check = QCheckBox(t("settings.auto_start_all_profiles"))
        stop_row = QHBoxLayout()
        stop_row.addWidget(self.stop_behavior_combo, stretch=1)
        stop_row.addWidget(self.auto_start_check)
        instance_layout.addRow(t("settings.stop_behavior"), stop_row)

        self.stop_telegram_combo = self._option_combo(self.STOP_BEHAVIORS)
        self.telegram_delay_spin = self._minutes_spin()
        self.telegram_mode_combo = self._option_combo(self.TELEGRAM_MODES)
        telegram_row = QHBoxLayout()
        telegram_row.addWidget(self.stop_telegram_combo, stretch=1)
        telegram_row.addWidget(QLabel(t("settings.delay")))
        telegram_row.addWidget(self.telegram_delay_spin)
        telegram_row.addWidget(QLabel(t("settings.mode")))
        telegram_row.addWidget(self.telegram_mode_combo)
        instance_layout.addRow(t("settings.stop_behavior_telegram"), telegram_row)

        self.emulator_start_delay_spin = QSpinBox()
        self.emulator_start_delay_spin.setRange(0, 3600)
        self.emulator_start_delay_spin.setSuffix(" s")
        instance_layout.addRow(t("settings.emulator_start_delay"), self.emulator_start_delay_spin)

        self.startup_view_combo = SearchableComboBox()
        for label, value in sorted(self.STARTUP_VIEWS, key=lambda item: item[0].casefold()):
            self.startup_view_combo.addItem(t(f"settings.startup_{value}"), value)
        self.startup_view_combo.setToolTip(
            t("settings.application_startup_view_tooltip")
        )
        instance_layout.addRow(
            t("settings.application_startup_view"),
            self.startup_view_combo,
        )

        general_layout.addWidget(instance_group)

        logging_group = QGroupBox(t("settings.logging"))
        logging_layout = QFormLayout(logging_group)
        self.log_level_combo = self._option_combo(("normal", "debug", "trace"))
        self.max_log_file_size_spin = QSpinBox()
        self.max_log_file_size_spin.setRange(1, 1024)
        self.max_archive_files_spin = QSpinBox()
        self.max_archive_files_spin.setRange(1, 1000)
        logging_layout.addRow(t("settings.log_level"), self.log_level_combo)
        logging_layout.addRow(t("settings.max_log_file_size_mb"), self.max_log_file_size_spin)
        logging_layout.addRow(t("settings.max_archive_files"), self.max_archive_files_spin)
        general_layout.addWidget(logging_group)
        general_layout.addStretch(1)

        game_group = QGroupBox(t("settings.game_settings"))
        game_layout = QFormLayout(game_group)

        self.game_version_label = QLabel("-")
        game_layout.addRow(t("settings.game_version"), self.game_version_label)

        functions_hint = QLabel(
            t("settings.profile_functions_hint").format(
                variable=GameService.MULTI_ACCOUNT_VARIABLE
            )
        )
        functions_hint.setWordWrap(True)
        game_layout.addRow(functions_hint)

        games_layout.addWidget(game_group)
        self.game_scheduling_group = QGroupBox(t("settings.tab_scheduling"))
        game_scheduling_layout = QFormLayout(self.game_scheduling_group)
        self.use_global_scheduling_check = QCheckBox(t("settings.use_global_scheduling"))
        game_scheduling_layout.addRow(self.use_global_scheduling_check)
        self.game_limit_active_time_check = QCheckBox(t("settings.limit_profile_active_time"))
        self.game_max_active_spin = self._minutes_spin()
        self.game_max_active_spin.setMinimum(1)
        self.game_lookahead_spin = QSpinBox()
        self.game_lookahead_spin.setRange(0, 3600)
        self.game_max_wait_spin = self._minutes_spin()
        self.game_max_wait_spin.setMinimum(1)
        game_scheduling_layout.addRow(self.game_limit_active_time_check)
        game_scheduling_layout.addRow(t("settings.max_active_minutes"), self.game_max_active_spin)
        game_scheduling_layout.addRow(t("settings.scheduling_lookahead_seconds"), self.game_lookahead_spin)
        game_scheduling_layout.addRow(t("settings.scheduling_max_wait_minutes"), self.game_max_wait_spin)
        game_hint = QLabel(t("settings.game_scheduling_help"))
        game_hint.setWordWrap(True)
        game_scheduling_layout.addRow(game_hint)
        games_layout.addWidget(self.game_scheduling_group)
        games_layout.addStretch(1)

        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)
        layout.addWidget(self.btn_save)

        self.game_combo.currentIndexChanged.connect(self.load_settings)
        self.btn_save.clicked.connect(self.save_settings)
        for control in (self.max_active_spin, self.lookahead_spin, self.max_wait_spin):
            control.valueChanged.connect(self._global_scheduling_changed)
        self.limit_active_time_check.toggled.connect(self._global_scheduling_changed)
        self.use_global_scheduling_check.toggled.connect(self._game_scheduling_changed)
        self.game_limit_active_time_check.toggled.connect(self._game_scheduling_changed)
        for control in (self.game_max_active_spin, self.game_lookahead_spin, self.game_max_wait_spin):
            control.valueChanged.connect(self._game_scheduling_changed)

        self._load_global_settings(STUDIO_DEFAULTS)
        self.refresh_games()

    @staticmethod
    def _option_combo(keys):
        combo = SearchableComboBox()
        for key in sorted(keys, key=lambda item: t(f"settings.option_{item}").casefold()):
            combo.addItem(t(f"settings.option_{key}"), key)
        return combo

    @staticmethod
    def _minutes_spin():
        spin = QSpinBox()
        spin.setRange(0, 100000)
        return spin

    @staticmethod
    def _set_combo_value(combo, value):
        index = combo.findData(value)
        combo.setCurrentIndex(index if index >= 0 else 0)

    def refresh_games(self):
        if self._operation is not None:
            return

        def load():
            games = self.game_service or GameService()
            config = self.app_config_service or AppConfigService()
            return games, config, games.load(), config.get_studio_settings()

        self._start_operation(load, self._loaded)

    def _start_operation(self, action, callback):
        self.btn_save.setEnabled(False)
        self.tabs.setEnabled(False)
        self._operation_callback = callback
        self._operation = _SettingsOperation(action)
        self._operation.signals.completed.connect(self._operation_finished)
        QThreadPool.globalInstance().start(self._operation)

    @Slot(object, str)
    def _operation_finished(self, result, error):
        callback = self._operation_callback
        self._operation = None
        self._operation_callback = None
        self.btn_save.setEnabled(True)
        self.tabs.setEnabled(True)
        if error:
            self.status_label.setText(t("settings.operation_failed").format(error=error))
            return
        try:
            callback(result)
        except Exception as exc:
            self.status_label.setText(t("settings.operation_failed").format(error=str(exc)))

    def _loaded(self, result):
        self.game_service, self.app_config_service, games, settings = result
        self._games = {game["id"]: game for game in games}
        self._game_overrides = {
            game["id"]: dict(game.get("scheduling_overrides", {})) for game in games
        }
        if not self._globals_loaded:
            self._load_global_settings({**STUDIO_DEFAULTS, **settings})
            self._globals_loaded = True
        self.game_combo.blockSignals(True)
        self.game_combo.clear()
        for game in sorted(
            games,
            key=lambda item: str(item.get("display_name", item.get("id", ""))).casefold(),
        ):
            self.game_combo.addItem(
                game.get("display_name", game.get("id", "")),
                game.get("id", ""),
            )
        index = self.game_combo.findData(self.game_id)
        self.game_combo.setCurrentIndex(index if index >= 0 else 0)
        self.game_combo.blockSignals(False)
        self.load_settings()

    def load_settings(self):
        game_id = self.game_combo.currentData() or ""
        self.game_id = game_id
        self._displayed_game_id = game_id
        game = self._games.get(game_id, {})
        self.game_version_label.setText(game.get("package_name", "-") or "-")
        self.game_scheduling_group.setEnabled(bool(game_id))
        self._render_game_scheduling()

    def _load_global_settings(self, settings):
        language_index = self.language_combo.findData(settings.get("language", "en"))
        self.language_combo.setCurrentIndex(language_index if language_index >= 0 else 0)

        self._set_combo_value(self.idle_behavior_combo, settings["idle_behavior"])
        self.max_idle_spin.setValue(int(settings["max_idle_minutes"]))
        self.max_concurrent_spin.setValue(int(settings["max_concurrent_instances"]))
        self.limit_active_time_check.setChecked(bool(settings["limit_profile_active_time"]))
        self.max_active_spin.setValue(int(settings["max_active_minutes"]))
        self.max_active_spin.setEnabled(bool(settings["limit_profile_active_time"]))
        self.lookahead_spin.setValue(settings["scheduling_lookahead_seconds"])
        self.max_wait_spin.setValue(settings["scheduling_max_wait_minutes"])
        self._set_combo_value(self.stop_behavior_combo, settings["stop_behavior"])
        self.auto_start_check.setChecked(bool(settings["auto_start_all_profiles"]))
        self._set_combo_value(self.stop_telegram_combo, settings["stop_behavior_telegram"])
        self.telegram_delay_spin.setValue(int(settings["stop_behavior_telegram_delay_minutes"]))
        self._set_combo_value(self.telegram_mode_combo, settings["stop_behavior_telegram_mode"])
        self.emulator_start_delay_spin.setValue(int(settings["emulator_start_delay_seconds"]))
        self._set_combo_value(self.startup_view_combo, settings["startup_view"])
        self._set_combo_value(self.log_level_combo, settings["log_level"])
        self.max_log_file_size_spin.setValue(int(settings["max_log_file_size_mb"]))
        self.max_archive_files_spin.setValue(int(settings["max_archive_files"]))
        self.status_label.setText("")

    def _global_scheduling_values(self):
        return {
            "limit_profile_active_time": self.limit_active_time_check.isChecked(),
            "max_active_minutes": self.max_active_spin.value(),
            "scheduling_lookahead_seconds": self.lookahead_spin.value(),
            "scheduling_max_wait_minutes": self.max_wait_spin.value(),
        }

    def _global_scheduling_changed(self):
        self._render_game_scheduling()

    def _render_game_scheduling(self):
        overrides = self._game_drafts.get(
            self._displayed_game_id,
            self._game_overrides.get(self._displayed_game_id, {}),
        )
        effective = {**self._global_scheduling_values(), **overrides}
        self._rendering_game = True
        try:
            self.use_global_scheduling_check.setChecked(not overrides)
            self.game_limit_active_time_check.setChecked(effective["limit_profile_active_time"])
            self.game_max_active_spin.setValue(effective["max_active_minutes"])
            self.game_lookahead_spin.setValue(effective["scheduling_lookahead_seconds"])
            self.game_max_wait_spin.setValue(effective["scheduling_max_wait_minutes"])
            for control in (
                self.game_limit_active_time_check, self.game_lookahead_spin,
                self.game_max_wait_spin,
            ):
                control.setEnabled(bool(overrides))
            self.game_max_active_spin.setEnabled(
                bool(overrides) and effective["limit_profile_active_time"]
            )
        finally:
            self._rendering_game = False

    def _game_scheduling_changed(self):
        if self._rendering_game or not self._displayed_game_id:
            return
        self._game_drafts[self._displayed_game_id] = (
            {} if self.use_global_scheduling_check.isChecked() else {
                "limit_profile_active_time": self.game_limit_active_time_check.isChecked(),
                "max_active_minutes": self.game_max_active_spin.value(),
                "scheduling_lookahead_seconds": self.game_lookahead_spin.value(),
                "scheduling_max_wait_minutes": self.game_max_wait_spin.value(),
            }
        )
        self._render_game_scheduling()

    def save_settings(self):
        if self._operation is not None or self.app_config_service is None:
            return
        language = self.language_combo.currentData() or "en"
        settings = {
            "language": language,
            "idle_behavior": self.idle_behavior_combo.currentData(),
            "max_idle_minutes": self.max_idle_spin.value(),
            "max_concurrent_instances": self.max_concurrent_spin.value(),
            "emulator_start_delay_seconds": self.emulator_start_delay_spin.value(),
            "startup_view": self.startup_view_combo.currentData(),
            "limit_profile_active_time": self.limit_active_time_check.isChecked(),
            "max_active_minutes": self.max_active_spin.value(),
            "stop_behavior": self.stop_behavior_combo.currentData(),
            "auto_start_all_profiles": self.auto_start_check.isChecked(),
            "stop_behavior_telegram": self.stop_telegram_combo.currentData(),
            "stop_behavior_telegram_delay_minutes": self.telegram_delay_spin.value(),
            "stop_behavior_telegram_mode": self.telegram_mode_combo.currentData(),
            "log_level": self.log_level_combo.currentData(),
            "max_log_file_size_mb": self.max_log_file_size_spin.value(),
            "max_archive_files": self.max_archive_files_spin.value(),
        }
        settings.update(self._global_scheduling_values())
        drafts = {game_id: dict(values) for game_id, values in self._game_drafts.items()}

        def save():
            self.app_config_service.validate_scheduling_settings(
                {key: settings[key] for key in SCHEDULING_DEFAULTS}
            )
            for values in drafts.values():
                self.app_config_service.validate_scheduling_settings(values)
            previous_language = self.app_config_service.get_studio_settings().get("language", "en")
            self.app_config_service.save_studio_settings(settings)
            for game_id, values in drafts.items():
                self.game_service.save_scheduling_overrides(game_id, values)
            return previous_language

        def saved(previous_language):
            self._game_overrides.update(drafts)
            self._game_drafts.clear()
            self.logger.info("Settings Saved", game=self.game_id)
            self.status_label.setText(
                t("settings.language_restart_hint")
                if language != previous_language else t("settings.saved")
            )

        self._start_operation(save, saved)
