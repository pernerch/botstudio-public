import json
import os
from datetime import datetime, timedelta

from PySide6.QtCore import QObject, QRunnable, QThreadPool, QTimer, Qt, Signal
from PySide6.QtGui import QColor

from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from services.issue_service import IssueService
from services.logging_service import LOG_ARCHIVE_DIR, LOG_DIR, get_logger
from services.profile_service import ProfileService
from services.support_package_service import SupportPackageService
from services.theme_service import ThemeService
from ui.i18n.translator import t
from ui.widgets.searchable_combo import SearchableComboBox


COLUMN_WIDTHS = (
    150,
    70,
    90,
    130,
    140,
    600,
)


SEVERITY_ORDER = {
    "TRACE": 0,
    "DEBUG": 1,
    "INFO": 2,
    "WARNING": 3,
    "ERROR": 4,
    "CRITICAL": 5,
}


class _LogRefreshSignals(QObject):
    finished = Signal(object, object)


class _LogRefreshOperation(QRunnable):
    def __init__(self, load_records, profile_service, issue_service):
        super().__init__()
        self.load_records = load_records
        self.profile_service = profile_service
        self.issue_service = issue_service
        self.signals = _LogRefreshSignals()

    def run(self):
        try:
            snapshot = (
                self.load_records(),
                self.profile_service.load_all(),
                self.issue_service.list_issues(),
                self.issue_service.status_counts(),
            )
        except Exception as error:
            self.signals.finished.emit(None, error)
        else:
            self.signals.finished.emit(snapshot, None)


class LogView(QWidget):

    open_target_requested = Signal(dict)

    def __init__(
        self,
        parent=None,
    ):
        super().__init__(parent)

        root = QVBoxLayout(
            self
        )

        filter_layout = QGridLayout()

        self.search = QLineEdit()
        self.search.setPlaceholderText(
            t("logs.search")
        )

        self.profile_filter = SearchableComboBox()
        self.severity_filter = SearchableComboBox()
        self.module_filter = SearchableComboBox()
        self.time_filter = SearchableComboBox()

        self.profile_filter.addItem(
            t("logs.all_profiles"),
            "",
        )

        self.severity_filter.addItem(
            t("logs.all_severities"),
            "",
        )

        for severity in sorted(SEVERITY_ORDER, key=str.casefold):
            self.severity_filter.addItem(
                severity,
                severity,
            )

        self.module_filter.addItem(
            t("logs.all_modules"),
            "",
        )

        time_options = sorted(
            (
                (t("logs.all_time"), "all"),
                (t("logs.last_hour"), "1h"),
                (t("logs.last_day"), "24h"),
            ),
            key=lambda option: option[0].casefold(),
        )
        for label, value in time_options:
            self.time_filter.addItem(label, value)

        self.btn_refresh = QPushButton(
            t("logs.refresh")
        )
        self.btn_clear = QPushButton("Clear Logs")
        self.btn_open_logs = QPushButton("Open Log Folder")
        self.btn_open_archive = QPushButton("Open Archive Folder")
        self.btn_create_issue = QPushButton("Create Issue")
        self.issue_status = QLabel("")
        self.issue_list = SearchableComboBox()

        filter_widgets = (
            self.search,
            self.profile_filter,
            self.severity_filter,
            self.module_filter,
            self.time_filter,
            self.btn_refresh,
            self.btn_clear,
            self.btn_open_logs,
            self.btn_open_archive,
            self.btn_create_issue,
        )

        for widget in filter_widgets:
            widget.setSizePolicy(
                QSizePolicy.Ignored,
                QSizePolicy.Preferred,
            )

        filter_layout.addWidget(QLabel(t("logs.search")), 0, 0)
        filter_layout.addWidget(self.search, 0, 1, 1, 3)
        filter_layout.addWidget(QLabel(t("logs.profile")), 1, 0)
        filter_layout.addWidget(self.profile_filter, 1, 1)
        filter_layout.addWidget(QLabel(t("logs.severity")), 1, 2)
        filter_layout.addWidget(self.severity_filter, 1, 3)
        filter_layout.addWidget(QLabel(t("logs.module")), 2, 0)
        filter_layout.addWidget(self.module_filter, 2, 1)
        filter_layout.addWidget(QLabel(t("logs.time")), 2, 2)
        filter_layout.addWidget(self.time_filter, 2, 3)
        filter_layout.addWidget(self.btn_refresh, 0, 4)
        filter_layout.addWidget(self.btn_clear, 0, 5)
        filter_layout.addWidget(self.btn_open_logs, 1, 4)
        filter_layout.addWidget(self.btn_open_archive, 1, 5)
        filter_layout.addWidget(self.btn_create_issue, 2, 4, 1, 2)
        filter_layout.setColumnStretch(1, 1)
        filter_layout.setColumnStretch(3, 1)

        root.addLayout(
            filter_layout
        )

        root.addWidget(
            self.issue_status
        )

        root.addWidget(
            self.issue_list
        )

        self.table = QTreeWidget()
        self.table.setColumnCount(6)

        self.table.setHeaderLabels(
            [
                t("logs.timestamp"),
                t("logs.level"),
                t("logs.module"),
                t("logs.profile"),
                t("logs.game"),
                t("logs.message"),
            ]
        )

        header = self.table.header()

        header.setSectionResizeMode(
            QHeaderView.Interactive
        )

        header.setStretchLastSection(
            True
        )

        for column, width in enumerate(
            COLUMN_WIDTHS
        ):
            self.table.setColumnWidth(
                column,
                width,
            )

        font_px = (
            ThemeService()
            .get_compact_font_size_px()
        )

        self.table.setStyleSheet(
            f"QTreeWidget {{ font-size: {font_px}px; }}"
            f"QHeaderView::section {{ font-size: {font_px}px; }}"
        )

        root.addWidget(
            self.table,
            stretch=1,
        )

        self.btn_refresh.clicked.connect(
            self.refresh
        )

        self.btn_clear.clicked.connect(
            self.clear_active_logs
        )

        self.btn_open_logs.clicked.connect(
            lambda: self.open_log_folder(LOG_DIR)
        )

        self.btn_open_archive.clicked.connect(
            lambda: self.open_log_folder(LOG_ARCHIVE_DIR)
        )

        self.btn_create_issue.clicked.connect(
            self.create_issue_bundle
        )

        self.table.itemDoubleClicked.connect(
            self.show_record_details
        )

        self.search.textChanged.connect(
            lambda: self._apply_filters()
        )

        self.profile_filter.currentIndexChanged.connect(
            lambda: self._apply_filters()
        )

        self.severity_filter.currentIndexChanged.connect(
            lambda: self._apply_filters()
        )

        self.module_filter.currentIndexChanged.connect(
            lambda: self._apply_filters()
        )

        self.time_filter.currentIndexChanged.connect(
            lambda: self._apply_filters()
        )

        self._records = []
        self._profiles = []
        self._refresh_operation = None
        self._refresh_generation = 0
        self._operation_generation = None
        self.issue_service = IssueService()
        self.profile_service = ProfileService()
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setInterval(2000)
        self._refresh_timer.timeout.connect(self._auto_refresh)

    def showEvent(self, event):
        super().showEvent(event)
        self._refresh_timer.start()
        self.refresh()

    def hideEvent(self, event):
        self._refresh_timer.stop()
        self._refresh_generation += 1
        super().hideEvent(event)

    def _auto_refresh(self):
        if self.isVisible():
            self.refresh()

    def clear_active_logs(self):
        for log_file in LOG_DIR.glob("*.log"):
            log_file.write_text("", encoding="utf-8")
        self.refresh()

    @staticmethod
    def open_log_folder(path):
        path.mkdir(parents=True, exist_ok=True)
        os.startfile(path)

    def show_record_details(self, item, _column):
        record = item.data(0, Qt.UserRole) if item else None
        if not record:
            return

        dialog = QDialog(self)
        dialog.setWindowTitle("Log Details")
        dialog.resize(720, 500)
        layout = QVBoxLayout(dialog)
        details = QPlainTextEdit(json.dumps(record, indent=2, ensure_ascii=False))
        details.setReadOnly(True)
        layout.addWidget(details)
        open_target = self.record_open_target(record)
        if open_target:
            open_button = QPushButton(t("logs.open_target"))
            open_button.setToolTip(t("logs.open_target_tooltip"))

            def open_and_close():
                dialog.accept()
                self.open_target_requested.emit(dict(open_target))

            open_button.clicked.connect(open_and_close)
            layout.addWidget(open_button)
        dialog.exec()

    @staticmethod
    def record_open_target(record):
        if not isinstance(record, dict):
            return None
        details = record.get("details")
        target = details.get("open_target") if isinstance(details, dict) else None
        if not isinstance(target, dict) or target.get("kind") not in {"task", "function"}:
            return None
        return target

    def create_issue_bundle(self):
        item = self.table.currentItem()
        selected_record = item.data(0, Qt.UserRole) if item else None
        package_path = SupportPackageService().create_package(
            issue_context={
                "source": "log_view",
                "selected_record": selected_record or {},
            },
        )
        issue = self.issue_service.create_issue(
            package_path,
            context={
                "source": "log_view",
                "selected_record": selected_record or {},
            },
        )
        self.issue_status.setText(
            f"Issue {issue['issue_id'][:8]} created: {package_path}"
        )
        self._refresh_issues()

    def refresh(
        self,
    ):
        if not self.isVisible() or self._refresh_operation is not None:
            return
        self._operation_generation = self._refresh_generation
        self._refresh_operation = _LogRefreshOperation(
            self._load_records, self.profile_service, self.issue_service,
        )
        self._refresh_operation.signals.finished.connect(self._finish_refresh)
        QThreadPool.globalInstance().start(self._refresh_operation)

    def _finish_refresh(self, snapshot, error):
        generation = self._operation_generation
        self._refresh_operation = None
        self._operation_generation = None
        if error is not None:
            get_logger("studio").error("Log Refresh Failed", error=repr(error))
        if not self.isVisible():
            return
        if generation != self._refresh_generation:
            self.refresh()
            return
        if error is not None:
            self.issue_status.setText(t("logs.refresh_failed"))
            return
        records, self._profiles, issues, counts = snapshot
        changed = records != self._records
        self._records = records
        self._sync_filter_items()
        if changed:
            self._apply_filters()
        self._refresh_issues(issues, counts)

    def _refresh_issues(self, issues=None, counts=None):
        if issues is None or counts is None:
            self.refresh()
            return
        self.issue_list.clear()
        self.issue_list.addItem(
            " | ".join([
                f"Open: {counts[IssueService.STATUS_OPEN]}",
                f"In Progress: {counts[IssueService.STATUS_IN_PROGRESS]}",
                f"Waiting for User: {counts[IssueService.STATUS_WAITING_FOR_USER]}",
                f"Closed: {counts[IssueService.STATUS_CLOSED]}",
            ]),
            "",
        )
        for issue in sorted(
            issues,
            key=lambda item: (
                str(item.get("status", "Open")).casefold(),
                str(item.get("issue_id", "")).casefold(),
            ),
        ):
            issue_id = issue.get("issue_id", "")
            status = issue.get("status", "Open")
            self.issue_list.addItem(f"{status} - {issue_id[:8]}", issue_id)

    def apply_context_filter(
        self,
        *,
        profile=None,
        module=None,
        minimum_severity="WARNING",
    ):

        self.refresh()

        self._set_combo_data(
            self.profile_filter,
            profile or "",
        )

        self._set_combo_data(
            self.module_filter,
            module or "",
        )

        self._set_combo_data(
            self.severity_filter,
            minimum_severity,
        )

        self._apply_filters(
            minimum_severity=minimum_severity,
        )

    def _load_records(
        self,
    ):

        records = []

        if not LOG_DIR.exists():
            return records

        for log_file in LOG_DIR.glob("*.log*"):

            with open(log_file, "rb") as handle:
                size = handle.seek(0, 2)
                offset = max(0, size - 2 * 1024 * 1024)
                handle.seek(offset)
                if offset:
                    handle.readline()
                for line in handle.read().decode("utf-8", errors="replace").splitlines():

                    line = line.strip()

                    if not line:
                        continue

                    try:
                        record = json.loads(line)

                    except json.JSONDecodeError:
                        continue

                    if isinstance(record, dict):
                        records.append(record)

        records.sort(
            key=lambda record: record.get("timestamp", ""),
            reverse=True,
        )

        return records[:5000]

    def _sync_filter_items(
        self,
    ):

        self._sync_profile_filter()

        self._sync_combo(
            self.module_filter,
            t("logs.all_modules"),
            sorted(
                {
                    self._display_value(record.get("module"))
                    for record in self._records
                    if record.get("module")
                },
                key=str.casefold,
            ),
        )

    def _sync_profile_filter(self):
        current = self.profile_filter.currentData() or ""
        profile_values = {
            self._display_value(record.get("profile"))
            for record in self._records
            if record.get("profile")
        }
        self.profile_filter.blockSignals(True)
        self.profile_filter.clear()
        self.profile_filter.addItem(t("logs.all_profiles"), "")

        matched_values = set()
        for profile in self._profiles:
            candidates = [
                str(profile.get("name") or ""),
                str(profile.get("botstudio_profile_id") or ""),
                str(profile.get("id") or ""),
            ]
            value = next(
                (candidate for candidate in candidates if candidate in profile_values),
                "",
            )
            if not value or value in matched_values:
                continue
            label = (
                f"[{profile.get('game', '')}] "
                f"{profile.get('name') or profile.get('id') or value}"
            )
            self.profile_filter.addItem(label, value)
            matched_values.add(value)

        for value in sorted(profile_values - matched_values, key=str.casefold):
            self.profile_filter.addItem(value, value)

        self._set_combo_data(self.profile_filter, current)
        self.profile_filter.blockSignals(False)

    def _sync_combo(
        self,
        combo,
        all_label,
        values,
    ):

        current = combo.currentData()

        combo.blockSignals(True)
        combo.clear()
        combo.addItem(all_label, "")

        for value in values:
            combo.addItem(
                value,
                value,
            )

        self._set_combo_data(
            combo,
            current or "",
        )

        combo.blockSignals(False)

    def _set_combo_data(
        self,
        combo,
        value,
    ):

        for index in range(combo.count()):

            if combo.itemData(index) == value:
                combo.setCurrentIndex(index)
                return

    def _apply_filters(
        self,
        minimum_severity=None,
    ):

        search_text = self.search.text().strip().lower()
        profile = self.profile_filter.currentData() or ""
        module = self.module_filter.currentData() or ""
        severity = minimum_severity or self.severity_filter.currentData() or ""
        time_filter = self.time_filter.currentData() or "all"

        filtered = []

        for record in self._records:

            if profile and self._display_value(record.get("profile")) != profile:
                continue

            if module and self._display_value(record.get("module")) != module:
                continue

            if severity:

                if (
                    SEVERITY_ORDER.get(record.get("severity"), 0)
                    < SEVERITY_ORDER.get(severity, 0)
                ):
                    continue

            if not self._matches_time_filter(
                record,
                time_filter,
            ):
                continue

            if search_text:

                text = " ".join(
                    str(record.get(key, ""))
                    for key in (
                        "timestamp",
                        "severity",
                        "module",
                        "profile",
                        "game",
                        "message",
                    )
                ).lower()

                if search_text not in text:
                    continue

            filtered.append(record)

        self._fill_table(
            filtered
        )

    def _fill_table(
        self,
        records,
    ):
        expanded = {
            self.table.topLevelItem(index).text(5)
            for index in range(self.table.topLevelItemCount())
            if self.table.topLevelItem(index).isExpanded()
        }
        current = self.table.currentItem()
        selected = current.data(0, Qt.UserRole) if current else None
        scroll = self.table.verticalScrollBar().value()
        self.table.clear()
        groups = {}

        for record in records:
            correlation_id = (
                record.get("trace_id")
                or record.get("execution_id")
                or record.get("event_id")
            )
            if not correlation_id:
                self.table.addTopLevelItem(self._record_item(record))
                continue

            group_key = str(correlation_id)
            parent = groups.get(group_key)
            if parent is None:
                parent = QTreeWidgetItem([
                    self._display_value(record.get("timestamp")),
                    "",
                    self._display_value(record.get("module")),
                    self._display_value(record.get("profile")),
                    self._display_value(record.get("game")),
                    f"Trace {group_key[:12]}",
                ])
                parent.setData(0, Qt.UserRole, {
                    "correlation_id": group_key,
                    "records": [],
                })
                groups[group_key] = parent
                self.table.addTopLevelItem(parent)

            parent.data(0, Qt.UserRole)["records"].append(record)
            parent.addChild(self._record_item(record))

        for index in range(self.table.topLevelItemCount()):
            item = self.table.topLevelItem(index)
            item.setExpanded(item.text(5) in expanded)
            for candidate in [item, *[item.child(i) for i in range(item.childCount())]]:
                if selected is not None and candidate.data(0, Qt.UserRole) == selected:
                    self.table.setCurrentItem(candidate)
        self.table.verticalScrollBar().setValue(scroll)

    def _record_item(self, record):
        item = QTreeWidgetItem([
            self._display_value(record.get("timestamp")),
            self._display_value(record.get("severity")),
            self._display_value(record.get("module")),
            self._display_value(record.get("profile")),
            self._display_value(record.get("game")),
            self._display_value(record.get("message")),
        ])
        item.setData(0, Qt.UserRole, record)
        self._apply_severity_color(item, str(record.get("severity", "") or ""))
        return item

    @staticmethod
    def _display_value(value):
        if value is None:
            return ""
        if isinstance(value, (dict, list)):
            return json.dumps(value, ensure_ascii=False, sort_keys=True)
        return str(value)

    def _apply_severity_color(
        self,
        item,
        severity,
    ):

        if severity == "WARNING":
            item.setForeground(
                1,
                QColor("#ffb84d")
            )

        elif severity in (
            "ERROR",
            "CRITICAL",
        ):
            item.setForeground(
                1,
                QColor("#ff6b6b")
            )

        elif severity == "INFO":
            item.setForeground(
                1,
                QColor("#36c275")
            )

    def _matches_time_filter(
        self,
        record,
        time_filter,
    ):

        if time_filter == "all":
            return True

        timestamp = record.get(
            "timestamp",
            "",
        )

        try:
            value = datetime.strptime(
                timestamp,
                "%Y-%m-%dT%H:%M:%S%z",
            )

        except ValueError:
            return True

        now = datetime.now(
            value.tzinfo
        )

        if time_filter == "1h":
            return now - value <= timedelta(hours=1)

        if time_filter == "24h":
            return now - value <= timedelta(days=1)

        return True
