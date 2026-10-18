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

from services.app_config_service import AppConfigService
from services.game_service import GameService
from services.logging_service import get_logger
from ui.i18n.translator import LANGUAGES, t
from ui.widgets.searchable_combo import SearchableComboBox


class SettingsView(QWidget):
    """Per-game Instance/Game settings; backs the Settings nav tab (DEC-087)."""

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

        self.game_service = GameService()
        self.app_config_service = AppConfigService()
        self.game_id = game_id
        self.logger = get_logger("settings")

        layout = QVBoxLayout(self)

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
        layout.addWidget(language_group)

        header = QHBoxLayout()
        header.addWidget(QLabel(t("settings.game")))
        self.game_combo = SearchableComboBox()
        header.addWidget(self.game_combo, stretch=1)
        self.btn_save = QPushButton(t("common.save"))
        header.addWidget(self.btn_save)
        layout.addLayout(header)

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
        active_row = QHBoxLayout()
        active_row.addWidget(self.limit_active_time_check)
        active_row.addStretch(1)
        active_row.addWidget(self.max_active_spin)
        active_row.addWidget(QLabel(t("settings.max_active_minutes")))
        instance_layout.addRow(active_row)
        self.limit_active_time_check.toggled.connect(
            self.max_active_spin.setEnabled
        )

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
        instance_layout.addRow("Emulator start delay", self.emulator_start_delay_spin)

        self.startup_view_combo = SearchableComboBox()
        for label, value in sorted(self.STARTUP_VIEWS, key=lambda item: item[0].casefold()):
            self.startup_view_combo.addItem(label, value)
        self.startup_view_combo.setToolTip(
            t("settings.application_startup_view_tooltip")
        )
        instance_layout.addRow(
            t("settings.application_startup_view"),
            self.startup_view_combo,
        )

        layout.addWidget(instance_group)

        logging_group = QGroupBox("Logging")
        logging_layout = QFormLayout(logging_group)
        self.log_level_combo = self._option_combo(("normal", "debug", "trace"))
        self.max_log_file_size_spin = QSpinBox()
        self.max_log_file_size_spin.setRange(1, 1024)
        self.max_archive_files_spin = QSpinBox()
        self.max_archive_files_spin.setRange(1, 1000)
        logging_layout.addRow("Log Level", self.log_level_combo)
        logging_layout.addRow("Max Log File Size (MB)", self.max_log_file_size_spin)
        logging_layout.addRow("Max Archive Files", self.max_archive_files_spin)
        layout.addWidget(logging_group)

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

        layout.addWidget(game_group)
        layout.addStretch(1)

        self.status_label = QLabel("")
        layout.addWidget(self.status_label)

        self.game_combo.currentIndexChanged.connect(self.load_settings)
        self.btn_save.clicked.connect(self.save_settings)

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
        self.game_combo.blockSignals(True)
        self.game_combo.clear()
        for game in sorted(
            self.game_service.load(),
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
        settings = self.app_config_service.get_studio_settings()
        language_index = self.language_combo.findData(settings.get("language", "en"))
        self.language_combo.setCurrentIndex(language_index if language_index >= 0 else 0)

        if not game_id:
            return

        config = self.game_service.get_game_config(game_id)
        self.logger.info(
            "Settings Loaded",
            game=game_id,
            idle_behavior=settings["idle_behavior"],
            stop_behavior=settings["stop_behavior"],
            max_concurrent_instances=settings["max_concurrent_instances"],
        )

        self._set_combo_value(self.idle_behavior_combo, settings["idle_behavior"])
        self.max_idle_spin.setValue(int(settings["max_idle_minutes"]))
        self.max_concurrent_spin.setValue(int(settings["max_concurrent_instances"]))
        self.limit_active_time_check.setChecked(bool(settings["limit_profile_active_time"]))
        self.max_active_spin.setValue(int(settings["max_active_minutes"]))
        self.max_active_spin.setEnabled(bool(settings["limit_profile_active_time"]))
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
        game = self.game_service.get_game(game_id) or {}
        self.game_version_label.setText(game.get("package_name", "-") or "-")

        self.status_label.setText("")

    def save_settings(self):
        language = self.language_combo.currentData() or "en"
        previous_language = self.app_config_service.get_studio_settings().get("language", "en")
        if not self.game_id:
            self.app_config_service.save_studio_settings({"language": language})
            self.status_label.setText(t("settings.language_restart_hint"))
            return

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
        self.app_config_service.save_studio_settings(settings)
        self.logger.info(
            "Settings Saved",
            game=self.game_id,
            idle_behavior=settings["idle_behavior"],
            stop_behavior=settings["stop_behavior"],
            stop_behavior_telegram=settings["stop_behavior_telegram"],
            max_concurrent_instances=settings["max_concurrent_instances"],
            startup_view=settings["startup_view"],
        )
        self.status_label.setText(
            t("settings.language_restart_hint")
            if language != previous_language
            else t("settings.saved")
        )
