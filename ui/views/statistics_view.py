from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
    QGridLayout,
)

from services.statistics_service import StatisticsService
from services.profile_service import ProfileService
from services.logging_service import get_logger


class StatisticsView(QWidget):
    """Displays current and immutable all-time runtime statistics."""

    HEADERS = (
        "Game Account",
        "Task",
        "Successful",
        "Failed",
        "Uptime (s)",
        "Emulator starts",
    )

    def __init__(self, statistics_service=None, parent=None, scheduler_service=None):
        super().__init__(parent)
        self.statistics_service = statistics_service or StatisticsService()
        self.profile_service = ProfileService()
        self.scheduler_service = scheduler_service
        self.logger = get_logger("statistics")

        root = QVBoxLayout(self)
        header = QHBoxLayout()
        header.addWidget(QLabel("Statistics"))
        header.addStretch(1)
        self.refresh_button = QPushButton("Refresh")
        self.reset_button = QPushButton("Delete current statistics")
        header.addWidget(self.refresh_button)
        header.addWidget(self.reset_button)
        root.addLayout(header)
        self.error_label = QLabel()
        self.error_label.setWordWrap(True)
        self.error_label.hide()
        root.addWidget(self.error_label)

        self.tabs = QTabWidget()
        self.cards_layout = QGridLayout()
        self.cards_layout.setContentsMargins(0, 0, 0, 0)
        root.addLayout(self.cards_layout)
        self.current_table = self._create_table()
        self.all_time_table = self._create_table()
        self.tabs.addTab(self.current_table, "Since reset")
        self.tabs.addTab(self.all_time_table, "AllTime")
        root.addWidget(self.tabs)

        self.refresh_button.clicked.connect(self.refresh)
        self.tabs.currentChanged.connect(self.refresh)
        self.reset_button.clicked.connect(self.reset_current)
        self.refresh()

    @staticmethod
    def _create_table():
        table = QTableWidget(0, len(StatisticsView.HEADERS))
        table.setHorizontalHeaderLabels(StatisticsView.HEADERS)
        table.setEditTriggers(QTableWidget.NoEditTriggers)
        return table

    def _existing_task_ids(self, accounts):
        """Task IDs, die aktuell noch in einem Event des Profils existieren.

        None bedeutet: unbekannt (Scheduler nicht lesbar) -> nichts markieren.
        """

        if self.scheduler_service is None:
            try:
                from scheduler.models import SchedulerService

                self.scheduler_service = SchedulerService(reinitialize_service=object())
            except (OSError, RuntimeError, ValueError, TypeError, ImportError):
                return None
        existing = {}
        try:
            for account in accounts.values():
                game_id = str(account.get("game_id") or "")
                profile_id = str(account.get("profile_id") or "")
                key = (game_id, profile_id)
                if not game_id or not profile_id or key in existing:
                    continue
                existing[key] = {
                    task.task_id
                    for event in self.scheduler_service.list_events(
                        game_id, profile_id=profile_id,
                    )
                    for task in event.ordered_tasks()
                }
        except (OSError, RuntimeError, ValueError, TypeError, AttributeError):
            return None
        return existing

    def _fill(self, table, accounts):
        existing = self._existing_task_ids(accounts)
        rows = []
        for account in accounts.values():
            tasks = account.get("task_executions") or {}
            if not tasks:
                rows.append((account, None, False))
                continue
            known = None
            if existing is not None:
                known = existing.get((
                    str(account.get("game_id") or ""),
                    str(account.get("profile_id") or ""),
                ))
            account_rows = [
                (account, task, known is not None and task_id not in known)
                for task_id, task in tasks.items()
            ]
            # gelöschte Tasks behalten ihre Historie, stehen aber am Ende
            account_rows.sort(key=lambda item: item[2])
            rows.extend(account_rows)
        table.setRowCount(len(rows))
        current_names = {}
        for profile in self.profile_service.load_all():
            profile_name = str(profile.get("name") or profile.get("id") or "")
            for profile_id in (
                profile.get("id"),
                profile.get("botstudio_profile_id"),
            ):
                if profile_id:
                    current_names[str(profile_id)] = profile_name
        for row, (account, task, deleted) in enumerate(rows):
            profile_id = str(account.get("profile_id") or "")
            task_name = (task or {}).get("task_name", "")
            values = (
                current_names.get(profile_id)
                or account.get("profile_name")
                or profile_id,
                f"{task_name} (deleted)" if deleted else task_name,
                (task or {}).get("success", 0),
                (task or {}).get("failure", 0),
                round(float(account.get("uptime_seconds", 0.0)), 1),
                account.get("emulator_starts", 0),
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                if deleted:
                    item.setForeground(QColor("#7A8494"))
                    item.setToolTip("Task no longer exists; history kept")
                table.setItem(row, column, item)
        table.resizeColumnsToContents()

    def _card(self, title, value, detail):
        card = QFrame()
        card.setStyleSheet(
            "QFrame { background: #202631; border: 1px solid #3B4658; "
            "border-radius: 6px; }"
            "QLabel { border: none; }"
        )
        layout = QVBoxLayout(card)
        layout.setContentsMargins(12, 8, 12, 8)
        title_label = QLabel(title)
        value_label = QLabel(str(value))
        value_label.setStyleSheet("font-size: 20px; font-weight: bold; color: #62B7E8;")
        detail_label = QLabel(detail)
        detail_label.setStyleSheet("color: #AAB4C4; font-size: 10px;")
        layout.addWidget(title_label)
        layout.addWidget(value_label)
        layout.addWidget(detail_label)
        return card

    def _clear_cards(self):
        while self.cards_layout.count():
            item = self.cards_layout.takeAt(0)
            widget = item.widget()
            if widget:
                widget.deleteLater()

    def _refresh_cards(self, accounts):
        self._clear_cards()
        total_success = 0
        total_failure = 0
        total_duration = 0.0
        total_uptime = 0.0
        switches = 0
        emulator_starts = 0
        task_count = 0
        for account in accounts.values():
            total_uptime += float(account.get("uptime_seconds", 0.0) or 0.0)
            switches += int(account.get("profile_switches", 0) or 0)
            emulator_starts += int(account.get("emulator_starts", 0) or 0)
            for task in (account.get("task_executions") or {}).values():
                task_count += 1
                total_success += int(task.get("success", 0) or 0)
                total_failure += int(task.get("failure", 0) or 0)
                total_duration += float(task.get("duration_seconds", 0.0) or 0.0)

        total_runs = total_success + total_failure
        success_rate = (total_success / total_runs * 100) if total_runs else 0.0
        average_duration = total_duration / total_runs if total_runs else 0.0
        cards = [
            ("Total runs", total_runs, f"{total_success} successful / {total_failure} failed"),
            ("Success rate", f"{success_rate:.1f}%", f"{task_count} task definitions"),
            ("Total time", f"{total_duration / 3600:.1f} h", "recorded task duration"),
            ("Average task time", f"{average_duration:.1f} s", "per execution"),
            ("Profile switches", switches, "account changes"),
            ("Emulator starts", emulator_starts, "recorded starts"),
            ("Uptime", f"{total_uptime / 3600:.1f} h", "runtime uptime"),
        ]
        for index, (title, value, detail) in enumerate(cards):
            self.cards_layout.addWidget(self._card(title, value, detail), 0, index)

    def _current_accounts(self):
        return self.statistics_service.snapshot(all_time=self.tabs.currentIndex() == 1)

    def refresh(self):
        try:
            current = self.statistics_service.snapshot()
            all_time = self.statistics_service.snapshot(all_time=True)
            self._fill(self.current_table, current)
            self._fill(self.all_time_table, all_time)
            self._refresh_cards(all_time if self.tabs.currentIndex() == 1 else current)
        except (OSError, RuntimeError, ValueError, TypeError) as error:
            self._show_storage_error(error)
            return
        self.error_label.hide()
        self.reset_button.setEnabled(True)

    def reset_current(self):
        try:
            self.statistics_service.reset_current()
        except (OSError, RuntimeError, ValueError, TypeError) as error:
            self._show_storage_error(error)
            return
        self.refresh()

    def _show_storage_error(self, error):
        self.logger.error(
            "Statistics Storage Failed",
            error_type=type(error).__name__,
            next_action="inspect statistics storage; counters unavailable",
        )
        self.current_table.setRowCount(0)
        self.all_time_table.setRowCount(0)
        self._clear_cards()
        self.error_label.setText("Statistics unavailable. Check statistics storage and refresh.")
        self.error_label.show()
        self.reset_button.setEnabled(False)
