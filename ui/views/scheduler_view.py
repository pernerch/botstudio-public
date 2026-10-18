from concurrent.futures import ThreadPoolExecutor
import json

from PySide6.QtCore import QObject, QRunnable, QSettings, QThreadPool, QTime, QTimer, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtGui import QFont, QFontMetrics
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QScrollArea,
    QStyle,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QInputDialog,
    QMenu,
    QPushButton,
    QToolButton,
    QSizePolicy,
    QSlider,
    QSpinBox,
    QSplitter,
    QTimeEdit,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)
from datetime import datetime, timedelta, timezone

from services.function_service import FunctionService
from services.function_block_catalog import collect_field_declarations
from scheduler.models import Event, Task
from services.game_service import GameService
from services.logging_service import get_logger
from services.profile_service import ProfileService
from services.scheduler_service import SchedulerService
from services.scheduler_field_service import SchedulerFieldService, field_scope_key
from services.timeline_service import build_timeline_items
from services.variable_service import VariableService
from ui.i18n.translator import t
from ui.widgets.event_create_dialog import ProfileChecklist
from ui.widgets.searchable_combo import SearchableComboBox
from ui.widgets.toggle_switch import ToggleSwitch


MAX_QTIME_INTERVAL_SECONDS = 23 * 60 * 60 + 59 * 60 + 59


def interval_time_from_seconds(seconds) -> QTime:
    """Convert persisted seconds to a QTime without wrapping at 24 hours."""

    try:
        total = max(0, int(seconds))
    except (TypeError, ValueError):
        total = 0
    total = min(total, MAX_QTIME_INTERVAL_SECONDS)
    return QTime(
        total // 3600,
        (total % 3600) // 60,
        total % 60,
    )


def interval_seconds_from_time(value: QTime) -> int:
    return value.hour() * 3600 + value.minute() * 60 + value.second()


def create_interval_time_edit(seconds) -> QTimeEdit:
    """Create an HH:mm:ss editor while retaining untouched legacy overflow."""

    try:
        original_seconds = max(0, int(seconds))
    except (TypeError, ValueError):
        original_seconds = 0
    editor = QTimeEdit()
    editor.setDisplayFormat("HH:mm:ss")
    editor.setWrapping(False)
    editor.setTime(interval_time_from_seconds(original_seconds))
    editor.setProperty("original_interval_seconds", original_seconds)
    editor.setProperty("interval_changed", False)
    editor.timeChanged.connect(
        lambda _time: editor.setProperty("interval_changed", True)
    )
    if original_seconds > MAX_QTIME_INTERVAL_SECONDS:
        editor.setToolTip(
            f"Stored value: {original_seconds} seconds. "
            "It remains unchanged until this field is edited."
        )
    return editor


def interval_seconds_from_editor(editor: QTimeEdit) -> int:
    original = int(editor.property("original_interval_seconds") or 0)
    if original > MAX_QTIME_INTERVAL_SECONDS and not bool(
        editor.property("interval_changed")
    ):
        return original
    return interval_seconds_from_time(editor.time())


class _SchedulerTree(QTreeWidget):
    """Profile -> Event -> Task tree that persists order via drag and drop."""

    def __init__(self, order_changed, parent=None):
        super().__init__(parent)
        self._order_changed = order_changed

    def dropEvent(self, event):
        super().dropEvent(event)
        self._order_changed()

    def keyPressEvent(self, event):
        if event.modifiers() & Qt.ShiftModifier and event.key() in (
            Qt.Key_Up,
            Qt.Key_Down,
        ):
            if self._move_current_item(event.key() == Qt.Key_Down):
                event.accept()
                return
        super().keyPressEvent(event)

    def _move_current_item(self, move_down: bool) -> bool:
        item = self.currentItem()
        if item is None:
            return False
        kind = item.data(0, Qt.UserRole) or {}
        if kind.get("kind") not in {"event", "task"} or kind.get("protected"):
            return False

        parent = item.parent()
        if parent is None:
            container_count = self.topLevelItemCount()
            current_index = self.indexOfTopLevelItem(item)
        else:
            container_count = parent.childCount()
            current_index = parent.indexOfChild(item)

        target_index = current_index + (1 if move_down else -1)
        if target_index < 0 or target_index >= container_count:
            return False

        target = (
            self.topLevelItem(target_index)
            if parent is None
            else parent.child(target_index)
        )
        target_kind = target.data(0, Qt.UserRole) or {}
        if target_kind.get("protected"):
            return False

        if parent is None:
            moved = self.takeTopLevelItem(current_index)
            self.insertTopLevelItem(target_index, moved)
        else:
            moved = parent.takeChild(current_index)
            parent.insertChild(target_index, moved)
        self.setCurrentItem(moved)
        self._order_changed()
        return True


class _RefreshSignals(QObject):
    finished = Signal(object)


class _RefreshTask(QRunnable):
    def __init__(self, collect):
        super().__init__()
        self.setAutoDelete(False)
        self.collect = collect
        self.signals = _RefreshSignals()

    def run(self):
        try:
            self.signals.finished.emit((self.collect(), None))
        except Exception as error:
            self.signals.finished.emit((None, error))


class _FieldSaveSignals(QObject):
    finished = Signal(object, object)


class _FieldSaveTask(QRunnable):
    def __init__(self, variable_root, request):
        super().__init__()
        self.setAutoDelete(False)
        self.variable_root = variable_root
        self.request = dict(request)
        self.signals = _FieldSaveSignals()

    def run(self):
        try:
            service = VariableService(root=str(self.variable_root))
            if self.request.get("confirm"):
                service.confirm_value(
                    self.request["game_id"],
                    self.request["profile_id"],
                    self.request["field_target"],
                    self.request.get("scope_id", ""),
                    self.request["name"],
                )
                value = self.request.get("value")
            else:
                value = service.set_context_value(
                    self.request["game_id"],
                    self.request["profile_id"],
                    self.request["event_id"],
                    self.request.get("task_id", ""),
                    self.request["name"],
                    self.request["value"],
                    source="user",
                )
            self.signals.finished.emit(value, None)
        except Exception as error:
            self.signals.finished.emit(None, error)


class _ProfileSyncSignals(QObject):
    finished = Signal(object, object)


class _ProfileSyncTask(QRunnable):
    def __init__(
        self,
        scheduler_service,
        game_id,
        source_profile_id,
        target_profile_ids,
        excluded_event_ids=None,
    ):
        super().__init__()
        self.setAutoDelete(False)
        self.scheduler_service = scheduler_service
        self.game_id = game_id
        self.source_profile_id = source_profile_id
        self.target_profile_ids = target_profile_ids
        self.excluded_event_ids = list(excluded_event_ids or [])
        self.signals = _ProfileSyncSignals()

    def run(self):
        try:
            result = self.scheduler_service.sync_profile_events(
                self.game_id,
                self.source_profile_id,
                self.target_profile_ids,
                excluded_event_ids=self.excluded_event_ids,
            )
            self.signals.finished.emit(result, None)
        except Exception as error:
            self.signals.finished.emit(None, error)


class _TimelineCanvas(QWidget):
    """Paints profile lanes without putting scheduling logic in the GUI."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.items = []
        self.profile_labels = {}
        self.window_start = None
        self.window_end = None
        self._hit_boxes = []
        self.setMouseTracking(True)
        self.setMinimumHeight(160)

    def update_timeline(self, items, profile_labels, window_start, window_end):
        self.items = items
        self.profile_labels = profile_labels
        self.window_start = window_start
        self.window_end = window_end
        profile_count = len({item.profile_id for item in items})
        max_lane = max((item.lane for item in items), default=0) + 1
        self.setMinimumHeight(max(160, 48 + profile_count * (max_lane * 34 + 34)))
        self.update()

    def paintEvent(self, event):
        if not self.window_start or not self.window_end:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setFont(QFont(painter.font().family(), 8))
        text_metrics = QFontMetrics(painter.font())
        self._hit_boxes = []
        width = max(self.width(), 900)
        left = 170
        right = 18
        usable_width = max(1, width - left - right)
        total_seconds = (self.window_end - self.window_start).total_seconds()
        groups = []
        for item in self.items:
            if item.profile_id not in groups:
                groups.append(item.profile_id)
        painter.setPen(QPen(QColor("#7d8791")))
        for tick in range(0, 5):
            x = left + usable_width * tick / 4
            painter.drawLine(int(x), 24, int(x), self.height() - 12)
            stamp = self.window_start + (self.window_end - self.window_start) * tick / 4
            painter.drawText(int(x) - 35, 18, stamp.astimezone().strftime("%H:%M"))

        # current-time marker: a yellow line where "now" sits in the window
        now = datetime.now(timezone.utc)
        if self.window_start <= now <= self.window_end:
            now_x = left + usable_width * (
                (now - self.window_start).total_seconds() / total_seconds
            )
            painter.setPen(QPen(QColor("#FDC601"), 2))
            painter.drawLine(int(now_x), 24, int(now_x), self.height() - 12)
            painter.drawText(
                int(now_x) + 4,
                18,
                now.astimezone().strftime("%H:%M"),
            )

        y = 42
        for profile_id in groups:
            profile_items = [item for item in self.items if item.profile_id == profile_id]
            lane_count = max((item.lane for item in profile_items), default=0) + 1
            painter.setPen(QPen(QColor("#E8ECF1")))
            painter.drawText(8, y + 18, self.profile_labels.get(profile_id, profile_id))
            for item in profile_items:
                start = max(item.start, self.window_start)
                end = min(item.end, self.window_end)
                x = left + usable_width * (start - self.window_start).total_seconds() / total_seconds
                block_width = max(5, usable_width * (end - start).total_seconds() / total_seconds)
                block_y = y + item.lane * 34
                color = QColor("#FDC601") if item.status == "running" else QColor("#4FD42A")
                painter.fillRect(int(x), block_y, max(5, int(block_width)), 24, color)
                painter.setPen(QPen(QColor("#263238")))
                painter.drawRect(int(x), block_y, max(5, int(block_width)), 24)
                box_width = max(5, int(block_width))
                self._hit_boxes.append((int(x), block_y, box_width, 24, item))
                label = text_metrics.elidedText(
                    item.label,
                    Qt.ElideRight,
                    max(1, box_width - 10),
                )
                painter.drawText(int(x) + 5, block_y + 17, label)
            y += lane_count * 34 + 28

    def mouseMoveEvent(self, event):
        position = event.position().toPoint()
        for x, y, width, height, item in self._hit_boxes:
            if x <= position.x() <= x + width and y <= position.y() <= y + height:
                start = item.start.astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
                end = item.end.astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
                self.setToolTip(f"{item.label}\n{start} - {end}\nStatus: {item.status}")
                return
        self.setToolTip("")
        super().mouseMoveEvent(event)


class SchedulerView(QWidget):
    """Scheduler screen: Profile -> Events -> Tasks -> Functions."""

    action_feedback = Signal(str, str)
    schedule_changed = Signal(str)
    FIELD_DATA_ROLE = int(Qt.ItemDataRole.UserRole) + 1

    def __init__(self, game_id: str = "", runtime_service=None, parent=None):
        super().__init__(parent)
        self.game_id = game_id
        self.variable_service = VariableService()
        self.scheduler_service = SchedulerService(
            variable_service=self.variable_service,
            statistics_service=runtime_service.statistics_service if runtime_service else None,
        )
        self.function_service = FunctionService()
        self.game_service = GameService()
        self.profile_service = ProfileService()
        self._ui_settings = QSettings("BotStudio", "BotStudio")
        # Queue persistence stays off the GUI thread.
        self._runtime_service = runtime_service
        self.logger = get_logger("scheduler")
        self._manual_run_executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="scheduler-event-queue",
        )
        self._active_manual_run = None

        layout = QVBoxLayout(self)

        header = QGridLayout()
        title = QLabel(t("scheduler.title"))
        title.setStyleSheet("font-size: 18px; font-weight: bold;")
        header.addWidget(title, 0, 0)
        header.addWidget(QLabel(t("scheduler.game")), 0, 1)
        self.game_combo = SearchableComboBox()
        if isinstance(self.game_combo, QWidget):
            header.addWidget(self.game_combo, 0, 2)

        self.refresh_button = QPushButton(t("scheduler.refresh"))
        self.add_button = QPushButton(t("scheduler.add_event"))
        self.add_task_button = QPushButton(t("scheduler.add_task"))
        self.edit_button = QPushButton(t("scheduler.edit_selected"))
        self.toggle_button = QPushButton(t("scheduler.toggle_enabled"))
        self.delete_button = QPushButton(t("scheduler.delete_selected"))
        self.run_button = QPushButton(t("scheduler.run_selected"))
        self.cancel_button = QPushButton(t("scheduler.cancel_selected"))
        self.cancel_button.setEnabled(False)
        self.sync_profiles_button = QPushButton(t("scheduler.sync_profiles"))
        self.sync_profiles_button.setToolTip(t("scheduler.sync_profiles_tooltip"))
        self.all_events_off_button = QPushButton(t("scheduler.all_events_off"))
        self.all_events_off_button.setToolTip(t("scheduler.all_events_off_tooltip"))
        self.all_events_on_button = QPushButton(t("scheduler.all_events_on"))
        self.all_events_on_button.setToolTip(t("scheduler.all_events_on_tooltip"))
        # the label names the target view, not the current one
        self.view_mode_button = QPushButton(t("scheduler.timeline_view"))
        self.view_mode_button.setCheckable(True)
        self.view_mode_button.setToolTip(t("scheduler.view_mode_tooltip"))
        self.timeline_range_combo = QComboBox()
        for label, hours in (
            ("30 min", 0.5),
            ("1 h", 1),
            ("2 h", 2),
            ("6 h", 6),
            ("12 h", 12),
            ("24 h", 24),
            ("Week", 24 * 7),
        ):
            self.timeline_range_combo.addItem(label, hours)
        self.timeline_range_combo.setCurrentIndex(2)
        self.timeline_range_combo.setVisible(False)
        self.event_filter_combo = QComboBox()
        self.event_filter_combo.setToolTip(t("scheduler.event_filter_tooltip"))
        self.event_filter_combo.addItem(t("scheduler.event_filter_all"), "")
        self._event_filter_names = []
        self.run_button.setEnabled(False)
        self.add_task_button.setEnabled(False)
        self.edit_button.setEnabled(False)
        self.toggle_button.setEnabled(False)
        self.delete_button.setEnabled(False)

        controls = (
            self.refresh_button,
            self.add_button,
            self.sync_profiles_button,
            self.all_events_off_button,
            self.all_events_on_button,
        )
        for index, button in enumerate(controls):
            button.setSizePolicy(
                QSizePolicy.Ignored,
                QSizePolicy.Preferred,
            )
            header.addWidget(button, 1 + index // 5, index % 5)
        for button in (
            self.add_task_button,
            self.edit_button,
            self.toggle_button,
            self.delete_button,
            self.run_button,
            self.cancel_button,
        ):
            button.setVisible(False)
        self.game_combo.setSizePolicy(
            QSizePolicy.Ignored,
            QSizePolicy.Preferred,
        )
        header.addWidget(self.view_mode_button, 0, 3)
        if isinstance(self.timeline_range_combo, QWidget):
            header.addWidget(self.timeline_range_combo, 0, 4)
        if isinstance(self.event_filter_combo, QWidget):
            header.addWidget(self.event_filter_combo, 0, 4)
        header.setColumnStretch(0, 1)
        header.setColumnStretch(1, 1)
        header.setColumnStretch(2, 1)
        header.setColumnStretch(3, 1)
        header.setColumnStretch(4, 1)

        self.tree = _SchedulerTree(self._persist_tree_order, self)
        self.tree.setColumnCount(7)
        self.tree.setHeaderLabels([
            "Profile / Event / Task",
            "Priority",
            "Next due / Functions",
            "Status",
            "Enabled",
            t("scheduler.fields"),
            t("scheduler.actions"),
        ])
        self.tree.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.tree.setSelectionMode(QAbstractItemView.SingleSelection)
        self.tree.setAlternatingRowColors(True)
        self.tree.setStyleSheet(
            """
            QTreeWidget {
                background-color: #303846;
            }
            QTreeWidget::branch {
                background-color: #303846;
            }
            """
        )
        self.tree.setDragDropMode(QAbstractItemView.InternalMove)
        self.tree.setDefaultDropAction(Qt.MoveAction)
        self.tree.setDragEnabled(True)
        self.tree.setAcceptDrops(True)
        self.tree.viewport().setAcceptDrops(True)
        self.tree.setDropIndicatorShown(True)
        self._restore_tree_column_widths()
        self.tree.header().sectionResized.connect(self._save_tree_column_widths)
        self.tree.itemSelectionChanged.connect(self._update_action_states)
        self.tree.itemSelectionChanged.connect(self._update_selection_summary)
        self.tree.itemSelectionChanged.connect(self._update_field_inspector_selection)
        self.tree.itemClicked.connect(self._tree_item_clicked)

        self.timeline_canvas = _TimelineCanvas(self)
        self.timeline_scroll = QScrollArea(self)
        self.timeline_scroll.setWidgetResizable(True)
        self.timeline_scroll.setWidget(self.timeline_canvas)
        self.timeline_scroll.hide()

        self.refresh_timer = QTimer(self)
        self.refresh_timer.setInterval(1000)
        self.refresh_timer.timeout.connect(self._refresh_if_visible)
        self.refresh_timer.start()
        self._refresh_pool = QThreadPool(self)
        self._refresh_running = False
        self._refresh_pending = False
        self._refresh_state = None
        self._refresh_task = None
        self._profile_sync_pool = QThreadPool(self)
        self._profile_sync_pool.setMaxThreadCount(1)
        self._profile_sync_task = None
        self._field_save_pool = QThreadPool(self)
        self._field_save_pool.setMaxThreadCount(1)
        self._field_save_tasks = set()
        self._field_snapshot = {}
        self._field_change_generation = 0
        self._inspector_scope = None

        self.refresh_button.clicked.connect(self.refresh)
        self.add_button.clicked.connect(self.add_event_dialog)
        self.add_task_button.clicked.connect(self.add_task_dialog)
        self.edit_button.clicked.connect(self.edit_selected)
        self.toggle_button.clicked.connect(self.toggle_selected_enabled)
        self.delete_button.clicked.connect(self.delete_selected)
        self.run_button.clicked.connect(self.run_selected)
        self.cancel_button.clicked.connect(self.cancel_selected)
        self.sync_profiles_button.clicked.connect(self.sync_profiles_dialog)
        self.all_events_off_button.clicked.connect(
            lambda: self.set_events_enabled(False)
        )
        self.all_events_on_button.clicked.connect(
            lambda: self.set_events_enabled(True)
        )
        self.view_mode_button.toggled.connect(self._on_view_mode_toggled)
        self.timeline_range_combo.currentTextChanged.connect(self.refresh)
        self.event_filter_combo.currentIndexChanged.connect(self._on_event_filter_changed)
        self.game_combo.currentIndexChanged.connect(self._on_game_combo_changed)

        self.selection_label = QLabel(t("scheduler.no_event_selected"))
        self.selection_label.setWordWrap(True)
        self.selection_label.setSizePolicy(
            QSizePolicy.Expanding,
            QSizePolicy.Maximum,
        )
        self.action_feedback_label = QLabel("")
        self.action_feedback_label.setWordWrap(True)
        self.action_feedback_label.setSizePolicy(
            QSizePolicy.Expanding,
            QSizePolicy.Maximum,
        )
        self.action_feedback.connect(self._show_event_feedback)

        content_widget = QWidget(self)
        content_layout = QVBoxLayout(content_widget)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.addWidget(self.tree)
        content_layout.addWidget(self.timeline_scroll)

        self.field_inspector = QWidget(self)
        inspector_layout = QVBoxLayout(self.field_inspector)
        inspector_header = QHBoxLayout()
        self.field_inspector_title = QLabel(t("scheduler.field_inspector"))
        self.field_inspector_title.setStyleSheet("font-weight: bold;")
        self.field_inspector_close = QPushButton("×")
        self.field_inspector_close.setFixedSize(28, 28)
        self.field_inspector_close.setToolTip(t("common.close"))
        self.field_inspector_close.clicked.connect(self._close_field_inspector)
        inspector_header.addWidget(self.field_inspector_title, stretch=1)
        inspector_header.addWidget(self.field_inspector_close)
        inspector_layout.addLayout(inspector_header)

        self.field_form_widget = QWidget(self.field_inspector)
        self.field_form = QFormLayout(self.field_form_widget)
        self.field_scroll = QScrollArea(self.field_inspector)
        self.field_scroll.setWidgetResizable(True)
        self.field_scroll.setWidget(self.field_form_widget)
        inspector_layout.addWidget(self.field_scroll)
        self.field_inspector.hide()

        self.content_splitter = QSplitter(Qt.Horizontal, self)
        self.content_splitter.addWidget(content_widget)
        self.content_splitter.addWidget(self.field_inspector)
        self.content_splitter.setStretchFactor(0, 1)
        self.content_splitter.setStretchFactor(1, 0)
        self.content_splitter.splitterMoved.connect(self._save_inspector_width)

        self.editor_hint_label = QLabel(t("scheduler.editor_hint"))
        self.editor_hint_label.setWordWrap(True)
        self.editor_hint_label.setSizePolicy(
            QSizePolicy.Expanding,
            QSizePolicy.Maximum,
        )

        layout.addLayout(header)
        layout.addWidget(self.content_splitter, stretch=1)
        layout.addWidget(self.selection_label)
        layout.addWidget(self.action_feedback_label)
        layout.addWidget(self.editor_hint_label)

        self.refresh_games()
        self.schedule_changed.connect(self._on_schedule_changed, Qt.QueuedConnection)
        bus = self.scheduler_service.reinitialize_service
        listener = self._schedule_persisted
        bus.subscribe(listener)
        self.destroyed.connect(lambda: bus.unsubscribe(listener))

    def _schedule_persisted(self, source, payload):
        if source in {"scheduler", "variable"}:
            self.schedule_changed.emit(str(payload.get("game_id") or ""))

    def _on_schedule_changed(self, game_id):
        if game_id == self.game_id:
            self._field_change_generation += 1
            self._refresh_if_visible()

    def _refresh_if_visible(self):
        if self.isVisible():
            if QApplication.mouseButtons() != Qt.MouseButton.NoButton:
                return
            # focus gate: while the user edits in the field inspector, a tree
            # refresh would rebuild the rows under their cursor; defer it and
            # let the next tick/close resume the refresh cycle
            focus_widget = QApplication.focusWidget()
            if (
                self.field_inspector.isVisible()
                and focus_widget is not None
                and self.field_inspector.isAncestorOf(focus_widget)
            ):
                return
            self.refresh()

    def _restore_tree_column_widths(self):
        header = self.tree.header()
        stored = self._ui_settings.value("scheduler/tree_column_widths", "")
        try:
            widths = [int(value) for value in str(stored).split(",") if value]
        except (TypeError, ValueError):
            widths = []
        defaults = (280, 90, 300, 110, 90, 80, 150)
        header.setStretchLastSection(False)
        for index, default in enumerate(defaults):
            header.resizeSection(index, widths[index] if index < len(widths) else default)

    def _save_tree_column_widths(self, *_args):
        widths = [str(self.tree.header().sectionSize(index)) for index in range(self.tree.columnCount())]
        self._ui_settings.setValue("scheduler/tree_column_widths", ",".join(widths))

    def _save_inspector_width(self, *_args):
        if self.field_inspector.isVisible():
            sizes = self.content_splitter.sizes()
            if len(sizes) > 1 and sizes[1] > 0:
                self._ui_settings.setValue(
                    "scheduler/field_inspector_width",
                    sizes[1],
                )

    def _tree_item_clicked(self, item, column):
        fields = item.data(0, self.FIELD_DATA_ROLE) or []
        if column == 5 and fields:
            self._show_field_inspector(item, fields)

    def _show_field_inspector(self, item, fields):
        kind = item.data(0, Qt.UserRole) or {}
        self._inspector_scope = dict(kind)
        self.field_inspector_title.setText(
            t("scheduler.field_inspector_for").format(name=item.text(0))
        )
        self._render_field_controls(fields)
        self.field_inspector.show()
        width = int(
            self._ui_settings.value(
                "scheduler/field_inspector_width",
                360,
            )
        )
        total = max(self.content_splitter.width(), width + 400)
        self.content_splitter.setSizes([max(400, total - width), width])

    def _close_field_inspector(self):
        self._save_inspector_width()
        self.field_inspector.hide()
        self._inspector_scope = None
        # refreshes deferred by the focus gate resume once editing ends
        QTimer.singleShot(0, self._refresh_if_visible)

    def _update_field_inspector_selection(self):
        if not self.field_inspector.isVisible():
            return
        item = self.tree.currentItem()
        if item is None:
            self._close_field_inspector()
            return
        kind = item.data(0, Qt.UserRole) or {}
        if kind == self._inspector_scope:
            return
        fields = item.data(0, self.FIELD_DATA_ROLE) or []
        if fields:
            self._show_field_inspector(item, fields)
        else:
            self._close_field_inspector()

    def _render_field_controls(self, fields):
        while self.field_form.rowCount():
            self.field_form.removeRow(0)
        for field in fields:
            name = str(field.get("name") or "")
            label_text = f"{name} *" if field.get("required") else name
            if field.get("unknown"):
                label_text += " (" + t("runtime.state_unknown") + ")"
            if field.get("from_helper"):
                label_text += t("scheduler.field_from_helper")
            description = str(field.get("description") or "").strip()
            if description:
                label_text = f"{label_text}\n{description}"
            label = QLabel(label_text)
            label.setWordWrap(True)
            if description:
                label.setStyleSheet("color: #C7D0DE;")
            tooltip = self._field_tooltip(field)
            label.setToolTip(tooltip)
            control = self._create_field_control(field)
            control.setToolTip(tooltip)
            if field.get("error") or field.get("unconfirmed") or (
                field.get("required") and field.get("value") in (None, "")
            ):
                control.setStyleSheet("border: 1px solid #D64545;")
            if field.get("error"):
                control.setEnabled(False)
            if field.get("unconfirmed") and not field.get("error"):
                row_widget = QWidget(self.field_inspector)
                row_layout = QHBoxLayout(row_widget)
                row_layout.setContentsMargins(0, 0, 0, 0)
                row_layout.addWidget(control, stretch=1)
                confirm = QPushButton(t("scheduler.confirm_field"))
                confirm.clicked.connect(
                    lambda _checked=False, current_field=field, button=confirm:
                    self._queue_field_confirm(current_field, button)
                )
                row_layout.addWidget(confirm)
                self.field_form.addRow(label, row_widget)
            else:
                self.field_form.addRow(label, control)

    @staticmethod
    def _field_tooltip(field):
        parts = [str(field.get("description") or "").strip()]
        if field.get("from_helper"):
            parts.append(t("scheduler.field_from_helper_hint"))
        origins = field.get("origin_functions") or []
        if origins:
            parts.append(
                t("scheduler.field_defined_by").format(
                    functions=", ".join(origins),
                )
            )
        parts.append(
            t("scheduler.field_contract").format(
                kind=field.get("variable_kind") or "-",
                value_type=field.get("value_type") or "-",
            )
        )
        return "\n".join(part for part in parts if part)

    def _create_field_control(self, field):
        value = field.get("value")
        options = list(field.get("options") or [])
        if options:
            combo = QComboBox()
            combo.addItem(t("scheduler.select_field_value"), None)
            for option in options:
                combo.addItem(str(option), option)
            index = combo.findData(value)
            if index < 0 and value not in (None, ""):
                combo.addItem(str(value), value)
                index = combo.count() - 1
                combo.setStyleSheet("border: 1px solid #D64545;")
            combo.setCurrentIndex(index if index >= 0 else 0)
            combo.currentIndexChanged.connect(
                lambda _index: self._queue_field_save(
                    field,
                    combo.currentData(),
                    combo,
                )
            )
            return combo

        value_type = field.get("value_type")
        if value_type == "boolean":
            check = QCheckBox()
            check.setChecked(bool(value))
            check.stateChanged.connect(
                lambda state: self._queue_field_save(
                    field,
                    bool(state),
                    check,
                )
            )
            return check

        if value_type in {"number", "duration"}:
            spin = QDoubleSpinBox()
            spin.setRange(0 if value_type == "duration" else -1_000_000_000, 1_000_000_000)
            spin.setDecimals(4)
            spin.setValue(float(value or 0))
            if value_type == "duration":
                spin.setSuffix(" s")
            spin.editingFinished.connect(
                lambda: self._queue_field_save(
                    field,
                    spin.value(),
                    spin,
                )
            )
            return spin

        if value_type == "collection":
            button = QPushButton(
                t("scheduler.edit_collection").format(
                    count=len(value) if isinstance(value, list) else 0,
                )
            )
            button.clicked.connect(
                lambda: self._edit_collection_field(field, button)
            )
            return button

        line = QLineEdit("" if value is None else str(value))
        line.editingFinished.connect(
            lambda: self._queue_field_save(
                field,
                line.text(),
                line,
            )
        )
        return line

    def _edit_collection_field(self, field, control):
        current = field.get("value")
        text, accepted = QInputDialog.getMultiLineText(
            self,
            t("scheduler.edit_collection_title"),
            t("scheduler.collection_json"),
            json.dumps(current if isinstance(current, list) else [], ensure_ascii=False, indent=2),
        )
        if accepted:
            self._queue_field_save(field, text, control)

    def _queue_field_save(self, field, value, control):
        request = {
            "game_id": self.game_id,
            "profile_id": field.get("profile_id", ""),
            "event_id": field.get("event_id", ""),
            "task_id": field.get("task_id", ""),
            "name": field.get("name", ""),
            "value": value,
            "field_target": field.get("field_target", ""),
        }
        self._field_change_generation += 1
        task = _FieldSaveTask(self.variable_service.root, request)
        self._field_save_tasks.add(task)
        control.setEnabled(False)
        task.signals.finished.connect(
            lambda saved_value, error, active_task=task: self._field_save_finished(
                active_task,
                field,
                control,
                saved_value,
                error,
            ),
            Qt.ConnectionType.QueuedConnection,
        )
        self._field_save_pool.start(task)

    def _queue_field_confirm(self, field, control):
        request = {
            "game_id": self.game_id,
            "profile_id": field.get("profile_id", ""),
            "event_id": field.get("event_id", ""),
            "task_id": field.get("task_id", ""),
            "name": field.get("name", ""),
            "value": field.get("value"),
            "field_target": field.get("field_target", ""),
            "scope_id": field.get("event_id", "")
            if field.get("field_target") == "event"
            else field.get("task_id", ""),
            "confirm": True,
        }
        self._field_change_generation += 1
        task = _FieldSaveTask(self.variable_service.root, request)
        self._field_save_tasks.add(task)
        control.setEnabled(False)
        task.signals.finished.connect(
            lambda saved_value, error, active_task=task: self._field_save_finished(
                active_task,
                field,
                control,
                saved_value,
                error,
            ),
            Qt.ConnectionType.QueuedConnection,
        )
        self._field_save_pool.start(task)

    def _field_save_finished(self, task, field, control, value, error):
        self._field_save_tasks.discard(task)
        control.setEnabled(True)
        if error:
            control.setStyleSheet("border: 1px solid #D64545;")
            control.setToolTip(
                self._field_tooltip(field) + "\n" + str(error)
            )
            self.action_feedback_label.setText(
                t("scheduler.field_save_failed").format(error=str(error))
            )
            return
        field["value"] = value
        field["unconfirmed"] = False
        control.setStyleSheet("")
        self.action_feedback_label.setText(t("scheduler.field_saved"))
        if not self._field_save_tasks:
            self.refresh()

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
        # a mocked QComboBox in tests must never overwrite the real game_id;
        # an explicitly requested (even unknown) game_id is never silently
        # replaced by an unrelated game either, only an empty one is
        if isinstance(self.game_combo, QWidget):
            index = self.game_combo.findData(self.game_id)
            if index < 0 and not self.game_id and self.game_combo.count():
                index = 0
            self.game_combo.setCurrentIndex(index)
            current = self.game_combo.currentData()
            if current:
                self.game_id = current
        self.game_combo.blockSignals(False)
        self.refresh()

    def _on_game_combo_changed(self):
        game_id = self.game_combo.currentData() or ""
        if game_id == self.game_id:
            return
        self.logger.info(
            "Scheduler Game Changed",
            previous_game=self.game_id,
            game=game_id,
        )
        self.game_id = game_id
        self._reset_event_filter()
        self.refresh()

    def set_game_id(self, game_id: str):
        if game_id != self.game_id:
            self._reset_event_filter()
        self.game_id = game_id
        index = self.game_combo.findData(game_id)
        self.game_combo.blockSignals(True)
        self.game_combo.setCurrentIndex(index if index >= 0 else -1)
        self.game_combo.blockSignals(False)
        self.refresh()

    def refresh(self):
        if self._refresh_running:
            self._refresh_pending = True
            return
        self._refresh_running = True
        self._refresh_state = (
            self._selected_kind(),
            self.tree.hasFocus(),
            self.tree.verticalScrollBar().value(),
            self.tree.horizontalScrollBar().value(),
        )
        game_id = self.game_id
        view_mode = self._current_view_mode()
        task = _RefreshTask(
            lambda: self._collect_refresh_snapshot(
                game_id,
                view_mode,
                self._field_change_generation,
            )
        )
        self._refresh_task = task
        task.signals.finished.connect(
            self._apply_refresh_snapshot,
            Qt.ConnectionType.QueuedConnection,
        )
        self._refresh_pool.start(task)

    def _current_view_mode(self):
        return "Timeline" if self.view_mode_button.isChecked() else "Tree"

    def _on_view_mode_toggled(self, timeline_enabled):
        if timeline_enabled:
            self._close_field_inspector()
        self._reset_event_filter()
        self._apply_view_mode_controls(timeline_enabled)
        self.refresh()

    def _apply_view_mode_controls(self, timeline_enabled):
        self.view_mode_button.setText(
            t("scheduler.table_view" if timeline_enabled else "scheduler.timeline_view")
        )
        self.timeline_range_combo.setVisible(timeline_enabled)
        self.event_filter_combo.setVisible(not timeline_enabled)

    def set_view_mode(self, mode):
        timeline_enabled = str(mode).casefold() in {
            "timeline",
            "scheduler_timeline",
        }
        if self.view_mode_button.isChecked() != timeline_enabled:
            # toggled signal handles controls, filter reset and refresh
            self.view_mode_button.setChecked(timeline_enabled)
            return
        self._apply_view_mode_controls(timeline_enabled)
        self.refresh()

    def _current_event_filter(self):
        combo = getattr(self, "event_filter_combo", None)
        return (combo.currentData() if combo is not None else "") or ""

    def _reset_event_filter(self):
        combo = getattr(self, "event_filter_combo", None)
        if combo is None or combo.currentIndex() <= 0:
            return
        combo.blockSignals(True)
        combo.setCurrentIndex(0)
        combo.blockSignals(False)

    def _on_event_filter_changed(self, _index=None):
        self.refresh()

    def _sync_event_filter_options(self, events_by_profile):
        names = sorted(
            {
                event.name
                for events in events_by_profile.values()
                for event in events
                if event.name
            },
            key=str.casefold,
        )
        if names == self._event_filter_names:
            return
        current = self._current_event_filter()
        self._event_filter_names = names
        self.event_filter_combo.blockSignals(True)
        self.event_filter_combo.clear()
        self.event_filter_combo.addItem(t("scheduler.event_filter_all"), "")
        for name in names:
            self.event_filter_combo.addItem(name, name)
        index = self.event_filter_combo.findData(current) if current else 0
        self.event_filter_combo.setCurrentIndex(max(0, index))
        self.event_filter_combo.blockSignals(False)

    def _filter_snapshot_events(self, snapshot):
        if getattr(self, "event_filter_combo", None) is None:
            return
        events_by_profile = snapshot.get("events_by_profile") or {}
        self._sync_event_filter_options(events_by_profile)
        selected = self._current_event_filter()
        if not selected:
            return
        snapshot["events_by_profile"] = {
            profile_id: [event for event in events if event.name == selected]
            for profile_id, events in events_by_profile.items()
        }

    def hideEvent(self, event):
        # leaving the scheduler page always clears the table filter
        if self._current_event_filter():
            self._reset_event_filter()
            self.refresh()
        super().hideEvent(event)

    def sync_profiles_dialog(self):
        if not self.game_id:
            self.action_feedback_label.setText(t("scheduler.select_game_before_sync"))
            return False

        profiles = [
            profile
            for profile in self.profile_service.load_all()
            if profile.get("game") == self.game_id
        ]
        if len(profiles) < 2:
            self.action_feedback_label.setText(t("scheduler.sync_requires_two_profiles"))
            return False

        dialog = QDialog(self)
        dialog.setWindowTitle(t("scheduler.sync_profiles"))
        dialog.resize(440, 460)
        layout = QVBoxLayout(dialog)

        source_combo = QComboBox(dialog)
        for profile in profiles:
            profile_id = str(profile.get("botstudio_profile_id") or profile.get("id") or "")
            state_key = "scheduler.profile_enabled" if profile.get("enabled", False) else "scheduler.profile_disabled"
            label = f"{profile.get('name') or profile_id} ({t(state_key)})"
            source_combo.addItem(label, profile_id)

        selected = self._selected_kind() or {}
        selected_source_id = str(selected.get("profile_id") or "")
        if selected.get("kind") == "event":
            selected_event = self.scheduler_service.load_event(
                self.game_id,
                selected.get("event_id", ""),
            )
            selected_source_id = selected_event.profile_id if selected_event else selected_source_id
        source_index = source_combo.findData(selected_source_id)
        source_combo.setCurrentIndex(source_index if source_index >= 0 else 0)
        layout.addWidget(QLabel(t("scheduler.sync_source_profile")))
        layout.addWidget(source_combo)

        targets = QListWidget(dialog)
        targets.setSelectionMode(QAbstractItemView.NoSelection)
        layout.addWidget(QLabel(t("scheduler.sync_target_profiles")))
        layout.addWidget(targets, stretch=1)

        def populate_targets(_index=None):
            previous = {
                targets.item(row).data(Qt.UserRole)
                for row in range(targets.count())
                if targets.item(row).checkState() == Qt.Checked
            }
            source_profile_id = source_combo.currentData()
            targets.clear()
            for profile in profiles:
                profile_id = str(profile.get("botstudio_profile_id") or profile.get("id") or "")
                if profile_id == source_profile_id:
                    continue
                state_key = "scheduler.profile_enabled" if profile.get("enabled", False) else "scheduler.profile_disabled"
                label = f"{profile.get('name') or profile_id} ({t(state_key)})"
                item = QListWidgetItem(label)
                item.setData(Qt.UserRole, profile_id)
                item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
                item.setCheckState(Qt.Checked if profile_id in previous else Qt.Unchecked)
                targets.addItem(item)

        source_combo.currentIndexChanged.connect(populate_targets)
        populate_targets()

        sync_events = QListWidget(dialog)
        sync_events.setSelectionMode(QAbstractItemView.NoSelection)
        layout.addWidget(QLabel(t("scheduler.sync_events")))
        sync_events.setToolTip(t("scheduler.sync_events_tooltip"))
        layout.addWidget(sync_events, stretch=1)

        def populate_sync_events(_index=None):
            source_profile_id = str(source_combo.currentData() or "")
            sync_events.clear()
            for event in self.scheduler_service.list_events(
                self.game_id,
                profile_id=source_profile_id,
            ):
                if event.is_init or event.is_interrupt:
                    continue
                item = QListWidgetItem(event.name or t("scheduler.unnamed_event"))
                item.setData(Qt.UserRole, event.event_id)
                item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
                item.setCheckState(Qt.Checked)
                sync_events.addItem(item)

        source_combo.currentIndexChanged.connect(populate_sync_events)
        populate_sync_events()

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel, parent=dialog)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if dialog.exec() != QDialog.Accepted:
            return False

        source_profile_id = str(source_combo.currentData() or "")
        target_profile_ids = [
            str(targets.item(row).data(Qt.UserRole))
            for row in range(targets.count())
            if targets.item(row).checkState() == Qt.Checked
        ]
        if not target_profile_ids:
            self.action_feedback_label.setText(t("scheduler.sync_select_targets"))
            return False
        excluded_event_ids = [
            str(sync_events.item(row).data(Qt.UserRole))
            for row in range(sync_events.count())
            if sync_events.item(row).checkState() != Qt.Checked
        ]
        excluded_names = [
            sync_events.item(row).text()
            for row in range(sync_events.count())
            if sync_events.item(row).checkState() != Qt.Checked
        ]

        target_names = [
            profile.get("name") or str(profile.get("id", ""))
            for profile in profiles
            if str(profile.get("botstudio_profile_id") or profile.get("id") or "") in target_profile_ids
        ]
        confirmation = QMessageBox.warning(
            self,
            t("scheduler.sync_replace_title"),
            t("scheduler.sync_replace_text").format(
                targets=", ".join(target_names),
            ) + (
                "\n\n" + t("scheduler.sync_excluded_text").format(
                    events=", ".join(excluded_names),
                )
                if excluded_names else ""
            ),
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if confirmation != QMessageBox.Yes:
            return False

        self.sync_profiles_button.setEnabled(False)
        self.action_feedback_label.setText(t("scheduler.sync_in_progress"))
        task = _ProfileSyncTask(
            self.scheduler_service,
            self.game_id,
            source_profile_id,
            target_profile_ids,
            excluded_event_ids,
        )
        task.signals.finished.connect(
            self._profile_sync_finished,
            Qt.ConnectionType.QueuedConnection,
        )
        self._profile_sync_task = task
        self._profile_sync_pool.start(task)
        return True

    def _profile_sync_finished(self, result, error):
        self.sync_profiles_button.setEnabled(True)
        self._profile_sync_task = None
        if error:
            self.logger.error(
                "Scheduler Profile Sync Failed",
                game=self.game_id,
                error=repr(error),
            )
            self.action_feedback_label.setText(
                t("scheduler.sync_failed").format(error=str(error))
            )
            return

        self.action_feedback_label.setText(
            t("scheduler.sync_complete").format(
                copied=result["copied_events"],
                replaced=result["replaced_events"],
                init_tasks=result.get("copied_init_tasks", 0),
                targets=len(result["target_profile_ids"]),
            )
            + (
                " " + t("scheduler.sync_skipped").format(
                    skipped=result.get("skipped_events", 0),
                )
                if result.get("skipped_events") else ""
            )
        )
        self.refresh()

    def _collect_refresh_snapshot(self, game_id, view_mode, field_generation=0):
        snapshot_variable_service = VariableService()
        snapshot_function_service = FunctionService(
            variable_service=snapshot_variable_service,
        )
        snapshot_profile_service = ProfileService()
        snapshot_scheduler_service = SchedulerService()
        snapshot_field_service = SchedulerFieldService(
            function_service=snapshot_function_service,
            variable_service=snapshot_variable_service,
        )
        snapshot = {
            "game_id": game_id,
            "view_mode": view_mode,
            "field_generation": field_generation,
        }
        if not game_id:
            snapshot["no_game"] = True
            return snapshot

        if snapshot["view_mode"] == "Timeline":
            snapshot["events"] = snapshot_scheduler_service.list_events(game_id)
            return snapshot

        functions = snapshot_function_service.list_functions(game_id)
        snapshot["functions_by_id"] = {
            function.function_id: function.name
            for function in functions
        }
        snapshot["profiles"] = snapshot_profile_service.get_profiles_for_game(game_id)
        snapshot["events_by_profile"] = {}
        snapshot["fields_by_scope"] = {}
        for profile in snapshot["profiles"]:
            profile_id = profile.get("botstudio_profile_id") or profile.get("id") or ""
            events = snapshot_scheduler_service.list_events(
                game_id,
                profile_id=profile_id,
            )
            snapshot["events_by_profile"][profile_id] = events
            snapshot["fields_by_scope"].update(
                snapshot_field_service.collect_profile_fields(
                    game_id,
                    profile_id,
                    events,
                    functions=functions,
                )
            )
        return snapshot

    def _apply_refresh_snapshot(self, result):
        snapshot, error = result
        self._refresh_running = False
        self._refresh_task = None
        state = self._refresh_state
        self._refresh_state = None
        latest_state = (
            self._selected_kind(),
            self.tree.hasFocus(),
            self.tree.verticalScrollBar().value(),
            self.tree.horizontalScrollBar().value(),
        )
        if state:
            state = (
                latest_state[0] or state[0],
                latest_state[1] or state[1],
                latest_state[2],
                latest_state[3],
            )
        else:
            state = latest_state
        try:
            if error:
                raise error
            if snapshot is None:
                return
            if snapshot.get("game_id") != self.game_id:
                self.refresh()
                return
            if snapshot.get("view_mode") != self._current_view_mode():
                self.refresh()
                return
            if snapshot.get("field_generation", 0) != self._field_change_generation:
                if not self._field_save_tasks:
                    self.refresh()
                return
            self._field_snapshot = snapshot.get("fields_by_scope", {})
            if snapshot.get("view_mode") == "Tree":
                self._filter_snapshot_events(snapshot)
            if (
                snapshot.get("view_mode") == "Tree"
                and self._update_tree_items_in_place(snapshot)
            ):
                self._update_action_states()
            else:
                self._refresh_impl(snapshot)
        except Exception as refresh_error:
            self.logger.error(
                "Scheduler View Refresh Failed",
                game=self.game_id,
                error=repr(refresh_error),
                next_action="keep view alive and retry on next refresh",
            )
            self.tree.clear()
            self.tree.addTopLevelItem(
                QTreeWidgetItem(["Scheduler refresh failed", "", "", "", ""])
            )
            self.tree.show()
            self.timeline_scroll.hide()
        finally:
            if state:
                selected_kind, tree_had_focus, vertical_scroll, horizontal_scroll = state
                if selected_kind:
                    self._restore_tree_selection(selected_kind, tree_had_focus)
                self.tree.verticalScrollBar().setValue(vertical_scroll)
                self.tree.horizontalScrollBar().setValue(horizontal_scroll)
            self._apply_pending_reveal(final=True)
            QTimer.singleShot(0, self._refresh_open_field_inspector)
            if self._refresh_pending:
                self._refresh_pending = False
                QTimer.singleShot(0, self.refresh)

    def _refresh_open_field_inspector(self):
        if not self.field_inspector.isVisible() or self._field_save_tasks:
            return
        focused = QApplication.focusWidget()
        if focused is not None and self.field_inspector.isAncestorOf(focused):
            return
        item = self.tree.currentItem()
        if item is None or (item.data(0, Qt.UserRole) or {}) != self._inspector_scope:
            return
        fields = item.data(0, self.FIELD_DATA_ROLE) or []
        if fields:
            self._render_field_controls(fields)

    def _update_tree_items_in_place(self, snapshot):
        profiles = snapshot.get("profiles", [])
        events_by_profile = snapshot.get("events_by_profile", {})
        functions_by_id = snapshot.get("functions_by_id", {})
        fields_by_scope = snapshot.get("fields_by_scope", {})
        if self.tree.topLevelItemCount() != len(profiles):
            return False

        event_map = {}
        for profile in profiles:
            profile_id = profile.get("botstudio_profile_id") or profile.get("id") or ""
            events = events_by_profile.get(profile_id, [])
            event_map[profile_id] = {event.event_id: event for event in events}

        for profile_index, profile in enumerate(profiles):
            profile_id = profile.get("botstudio_profile_id") or profile.get("id") or ""
            profile_item = self.tree.topLevelItem(profile_index)
            profile_kind = profile_item.data(0, Qt.UserRole) or {}
            if profile_kind.get("profile_id") != profile_id:
                return False
            profile_item.setText(0, profile.get("name") or profile_id)
            self._set_item_fields(
                profile_item,
                fields_by_scope.get(field_scope_key("profile", profile_id, ""), []),
            )
            events = list(event_map[profile_id].values())
            if profile_item.childCount() != len(events):
                return False
            for event_index, event in enumerate(events):
                event_item = profile_item.child(event_index)
                event_kind = event_item.data(0, Qt.UserRole) or {}
                if event_kind.get("event_id") != event.event_id:
                    return False
                event_item.setText(0, event.name or event.event_id)
                event_item.setText(1, str(event.priority))
                event_item.setText(2, self._format_next_due(event))
                event_item.setText(3, event.status or "idle")
                event_item.setText(4, "yes" if event.enabled else "no")
                event_kind["enabled"] = event.enabled
                event_kind["protected"] = event.is_init or event.is_interrupt
                event_kind["system_event"] = event.is_init or event.is_interrupt
                event_kind["interrupt"] = event.is_interrupt
                event_kind["running"] = event.status == "running"
                event_item.setData(0, Qt.UserRole, event_kind)
                self._set_tree_item_colors(event_item, "event")
                self._set_item_fields(
                    event_item,
                    fields_by_scope.get(
                        field_scope_key(
                            "event",
                            profile_id,
                            event.event_id,
                        ),
                        [],
                    ),
                )
                self._refresh_row_actions(event_item)
                tasks = event.ordered_tasks()
                if event_item.childCount() != len(tasks):
                    return False
                missing_in_event = False
                for task_index, task in enumerate(tasks):
                    task_item = event_item.child(task_index)
                    task_kind = task_item.data(0, Qt.UserRole) or {}
                    if task_kind.get("task_id") != task.task_id:
                        return False
                    label = task.name or task.task_id
                    if task.protected:
                        label += " (protected)"
                    task_item.setText(0, label)
                    task_item.setText(1, str(task.priority))
                    task_item.setText(
                        2,
                        ", ".join(functions_by_id.get(fid, fid) for fid in task.function_ids)
                        or "no functions",
                    )
                    task_item.setText(3, "active" if task.active else "inactive")
                    task_item.setText(4, "yes" if task.enabled else "no")
                    task_kind["enabled"] = task.enabled
                    task_kind["protected"] = task.protected
                    task_kind["running"] = task.status == "running"
                    task_item.setData(0, Qt.UserRole, task_kind)
                    self._set_tree_item_colors(
                        task_item,
                        "task",
                        task.status == "running",
                    )
                    self._set_item_fields(
                        task_item,
                        fields_by_scope.get(
                            field_scope_key(
                                "task",
                                profile_id,
                                event.event_id,
                                task.task_id,
                            ),
                            [],
                        ),
                    )
                    if self._apply_missing_functions(task_item, task, functions_by_id):
                        missing_in_event = True
                    self._refresh_row_actions(task_item)
                if missing_in_event:
                    self._mark_event_missing_functions(event_item)
                elif not event.is_interrupt:
                    event_item.setToolTip(0, "")
        return True

    def _apply_missing_functions(self, task_item, task, functions_by_id):
        """Mark a task red when it references deleted functions.

        Runs after the color/field setters in both the full build and the
        in-place refresh so the marking can never be lost on a refresh.
        """

        missing_function_ids = [
            function_id
            for function_id in task.function_ids
            if function_id not in functions_by_id
        ]
        if not missing_function_ids:
            task_item.setToolTip(0, "")
            task_item.setToolTip(2, "")
            return False
        present = [
            functions_by_id[function_id]
            for function_id in task.function_ids
            if function_id in functions_by_id
        ]
        text = t("scheduler.missing_function").format(count=len(missing_function_ids))
        if present:
            text = ", ".join(present) + " | " + text
        task_item.setText(2, text)
        task_item.setText(3, "inactive")
        red = QColor("#d00000")
        for column in range(self.tree.columnCount()):
            task_item.setForeground(column, red)
        tooltip = t("scheduler.missing_function_tooltip")
        task_item.setToolTip(0, tooltip)
        task_item.setToolTip(2, tooltip)
        from services.reference_log import log_missing_function_reference

        kind = task_item.data(0, Qt.UserRole) or {}
        user = {
            "kind": "task",
            "id": task.task_id,
            "name": task.name or "",
            "event_id": kind.get("event_id", "") if isinstance(kind, dict) else "",
        }
        for function_id in missing_function_ids:
            log_missing_function_reference(
                getattr(self, "game_id", "") or "", function_id, user
            )
        return True

    def _restore_tree_selection(self, selected_kind, restore_focus=False):
        def matches(item):
            return item.data(0, Qt.UserRole) == selected_kind

        def visit(parent):
            for index in range(parent.childCount()):
                item = parent.child(index)
                if matches(item):
                    self.tree.setCurrentItem(item)
                    if restore_focus:
                        self.tree.setFocus()
                    return True
                if visit(item):
                    return True
            return False

        for index in range(self.tree.topLevelItemCount()):
            item = self.tree.topLevelItem(index)
            if matches(item):
                self.tree.setCurrentItem(item)
                if restore_focus:
                    self.tree.setFocus()
                return
            if visit(item):
                return

    def reveal_task(self, game_id, event_id, task_id):
        """Select a task in the tree view (used by dependency/log links).

        Refreshes are asynchronous, so the target is kept pending and applied
        after the next snapshot as well as immediately when already visible.
        """

        self._pending_reveal = (event_id or "", task_id or "")
        if self._current_view_mode() != "Tree":
            self.set_view_mode("Tree")
        if game_id and game_id != self.game_id:
            self.set_game_id(game_id)
        elif self._apply_pending_reveal():
            return
        else:
            self.refresh()

    def _apply_pending_reveal(self, final=False):
        pending = getattr(self, "_pending_reveal", None)
        if not pending:
            return False
        event_id, task_id = pending

        def visit(parent):
            for index in range(parent.childCount()):
                item = parent.child(index)
                kind = item.data(0, Qt.UserRole) or {}
                if (
                    isinstance(kind, dict)
                    and kind.get("kind") == "task"
                    and kind.get("task_id") == task_id
                    and (not event_id or kind.get("event_id") == event_id)
                ):
                    return item
                found = visit(item)
                if found is not None:
                    return found
            return None

        item = visit(self.tree.invisibleRootItem())
        if item is None:
            if final and not getattr(self, "_refresh_running", False):
                self._pending_reveal = None
                label = getattr(self, "action_feedback_label", None)
                if label is not None:
                    label.setText(t("scheduler.reveal_not_found"))
            return False
        # a refresh started before the reveal would restore the old selection,
        # so keep the target until that snapshot has been applied
        if not getattr(self, "_refresh_running", False):
            self._pending_reveal = None
        parent = item.parent()
        while parent is not None:
            parent.setExpanded(True)
            parent = parent.parent()
        self.tree.setCurrentItem(item)
        self.tree.scrollToItem(item)
        self.tree.setFocus()
        return True

    def _refresh_impl(self, snapshot):
        self.tree.clear()
        if snapshot.get("no_game"):
            self.tree.show()
            self.timeline_scroll.hide()
            no_game_item = QTreeWidgetItem([t("scheduler.no_game_selected"), "", "", "", ""])
            self.tree.addTopLevelItem(no_game_item)
            self._update_action_states()
            return

        if snapshot.get("view_mode") == "Timeline":
            self.tree.hide()
            self.timeline_scroll.show()
            self._refresh_timeline(snapshot.get("events", []))
            self._update_action_states()
            return

        self.timeline_scroll.hide()
        self.tree.show()
        # Namensauflösung über alle Functions: geschützte Init Tasks dürfen
        # auch Helper referenzieren, sonst stünde dort die nackte UUID.
        functions_by_id = snapshot.get("functions_by_id", {})
        fields_by_scope = snapshot.get("fields_by_scope", {})
        profiles = snapshot.get("profiles", [])
        for profile in profiles:
            profile_id = profile.get("botstudio_profile_id") or profile.get("id") or ""
            profile_item = QTreeWidgetItem([profile.get("name") or profile_id, "", "", "", ""])
            profile_item.setData(0, Qt.UserRole, {"kind": "profile", "profile_id": profile_id})
            self._set_tree_item_colors(profile_item, "profile")
            self._set_item_fields(
                profile_item,
                fields_by_scope.get(field_scope_key("profile", profile_id, ""), []),
            )
            profile_item.setFlags(profile_item.flags() & ~Qt.ItemIsDragEnabled)
            self.tree.addTopLevelItem(profile_item)
            self._install_profile_actions(profile_item)
            for event in snapshot.get("events_by_profile", {}).get(profile_id, []):
                event_item = self._build_event_item(
                    event,
                    functions_by_id,
                    fields_by_scope,
                    profile_id,
                )
                profile_item.addChild(event_item)
                self._install_row_actions(event_item)
                for task_index in range(event_item.childCount()):
                    self._install_row_actions(event_item.child(task_index))
            profile_item.setExpanded(True)
        self.tree.expandAll()
        self._update_action_states()

    def _build_event_item(
        self,
        event,
        functions_by_id,
        fields_by_scope=None,
        profile_id="",
    ):
        fields_by_scope = fields_by_scope or {}
        profile_id = profile_id or event.profile_id
        event_item = QTreeWidgetItem([
            event.name or event.event_id,
            str(event.priority),
            self._format_next_due(event),
            event.status or "idle",
            "yes" if event.enabled else "no",
            "",
        ])
        event_item.setData(
            0,
            Qt.UserRole,
            {
                "kind": "event",
                "event_id": event.event_id,
                "enabled": event.enabled,
                "protected": event.is_init or event.is_interrupt,
                "system_event": event.is_init or event.is_interrupt,
                "interrupt": event.is_interrupt,
                "running": event.status == "running",
            },
        )
        self._set_tree_item_colors(event_item, "event")
        self._set_item_fields(
            event_item,
            fields_by_scope.get(
                field_scope_key(
                    "event",
                    profile_id,
                    event.event_id,
                ),
                [],
            ),
        )
        if event.is_init:
            event_item.setFlags(event_item.flags() & ~Qt.ItemIsDragEnabled)
        if event.is_interrupt:
            event_item.setFlags(event_item.flags() & ~Qt.ItemIsDragEnabled)
            event_item.setToolTip(0, "System Interrupt: only tasks can be added or deleted")
        missing_in_event = False
        for task in event.ordered_tasks():
            label = task.name or task.task_id
            if task.protected:
                label += " (protected)"
            function_names = ", ".join(functions_by_id.get(fid, fid) for fid in task.function_ids)
            task_item = QTreeWidgetItem([
                label,
                str(task.priority),
                function_names or "no functions",
                "active" if task.active else "inactive",
                "yes" if task.enabled else "no",
                "",
            ])
            task_item.setData(
                0,
                Qt.UserRole,
                {
                    "kind": "task",
                    "event_id": event.event_id,
                    "task_id": task.task_id,
                    "enabled": task.enabled,
                    "protected": task.protected,
                    "running": task.status == "running",
                },
            )
            self._set_tree_item_colors(task_item, "task", task.status == "running")
            self._set_item_fields(
                task_item,
                fields_by_scope.get(
                    field_scope_key(
                        "task",
                        profile_id,
                        event.event_id,
                        task.task_id,
                    ),
                    [],
                ),
            )
            if self._apply_missing_functions(task_item, task, functions_by_id):
                missing_in_event = True
            if task.protected:
                task_item.setFlags(task_item.flags() & ~Qt.ItemIsDragEnabled)
            event_item.addChild(task_item)
        if missing_in_event:
            self._mark_event_missing_functions(event_item)
        return event_item

    def _mark_event_missing_functions(self, event_item):
        event_item.setForeground(0, QColor("#d00000"))
        event_item.setToolTip(0, t("scheduler.event_missing_functions"))

    def _set_item_fields(self, item, fields):
        item.setData(0, self.FIELD_DATA_ROLE, list(fields))
        kind = (item.data(0, Qt.UserRole) or {}).get("kind")
        symbol = "◈" if kind == "profile" else "⚙"
        item.setText(5, f"{symbol} {len(fields)}" if fields else "")
        dependency_errors = [
            str(field.get("error"))
            for field in fields
            if field.get("error")
        ]
        if dependency_errors:
            for column in range(self.tree.columnCount()):
                item.setForeground(column, QColor("#D00000"))
            item.setToolTip(
                5,
                "\n".join(dependency_errors),
            )
        if fields:
            if not dependency_errors:
                item.setToolTip(
                    5,
                    "\n".join(str(field.get("name") or "") for field in fields),
                )
        else:
            item.setToolTip(5, "")

    def _item_kind(self, item):
        return item.data(0, Qt.UserRole) or {}

    def _select_and_run(self, item):
        self.tree.setCurrentItem(item)
        kind = self._item_kind(item)
        if kind.get("kind") == "task":
            parent = item.parent()
            if parent is not None:
                self.tree.setCurrentItem(parent)
        self.run_selected()

    def _select_and_cancel(self, item):
        self.tree.setCurrentItem(item)
        kind = self._item_kind(item)
        if kind.get("kind") == "task":
            parent = item.parent()
            if parent is not None:
                self.tree.setCurrentItem(parent)
        self.cancel_selected()

    def _select_and_toggle(self, item):
        self.tree.setCurrentItem(item)
        self.toggle_selected_enabled()

    def _open_row_menu(self, item, button):
        kind = self._item_kind(item)
        menu = QMenu(button)
        menu.setObjectName("schedulerOverflowMenu")
        menu.setStyleSheet(
            """
            QMenu#schedulerOverflowMenu {
                background: #20252D;
                color: #E7EBF0;
                border: 1px solid #566171;
                padding: 4px;
            }
            QMenu#schedulerOverflowMenu::item {
                background: transparent;
                border-radius: 3px;
                padding: 7px 28px 7px 10px;
            }
            QMenu#schedulerOverflowMenu::item:selected {
                background: #3E628C;
                color: #FFFFFF;
            }
            QMenu#schedulerOverflowMenu::item:disabled {
                color: #737D89;
            }
            QMenu#schedulerOverflowMenu::separator {
                height: 1px;
                background: #39424E;
                margin: 4px 6px;
            }
            """
        )
        edit_action = menu.addAction(t("scheduler.edit_selected"))
        edit_action.setEnabled(
            kind.get("kind") == "task"
            or not kind.get("system_event", False)
        )
        edit_action.triggered.connect(
            lambda: (self.tree.setCurrentItem(item), self.edit_selected())
        )
        menu.addSeparator()
        delete_action = menu.addAction(t("scheduler.delete_selected"))
        delete_action.setEnabled(
            not kind.get("protected", False)
            and not kind.get("system_event", False)
        )
        delete_action.triggered.connect(
            lambda: (self.tree.setCurrentItem(item), self.delete_selected())
        )
        if kind.get("kind") == "event":
            menu.addSeparator()
            add_task_action = menu.addAction(t("scheduler.add_task"))
            add_task_action.triggered.connect(
                lambda: (self.tree.setCurrentItem(item), self.add_task_dialog())
            )
        menu.exec(button.mapToGlobal(button.rect().bottomLeft()))

    def _install_profile_actions(self, item):
        if item is None:
            return
        profile_id = str(self._item_kind(item).get("profile_id") or "")
        action_widget = QWidget(self.tree)
        action_widget.setObjectName("schedulerProfileActions")
        action_widget.setStyleSheet(
            """
            QWidget#schedulerProfileActions { background: transparent; }
            QToolButton {
                background: transparent;
                border: 0;
                color: #C8D0D9;
                min-width: 24px;
                max-width: 24px;
                min-height: 24px;
                max-height: 24px;
            }
            QToolButton:hover {
                background: rgba(255, 255, 255, 0.12);
                border-radius: 3px;
            }
            """
        )
        action_layout = QHBoxLayout(action_widget)
        action_layout.setContentsMargins(4, 0, 4, 0)
        action_layout.setSpacing(4)
        action_layout.addStretch(1)
        menu_button = QToolButton(action_widget)
        menu_button.setObjectName("profileMoreButton")
        menu_button.setAutoRaise(True)
        menu_button.setText("?")
        menu_button.setToolTip(t("scheduler.profile_actions"))
        menu_button.clicked.connect(
            lambda: self._open_profile_menu(profile_id, menu_button)
        )
        action_layout.addWidget(menu_button)
        self.tree.setItemWidget(item, 6, action_widget)

    def _open_profile_menu(self, profile_id, button):
        menu = QMenu(button)
        off_action = menu.addAction(t("scheduler.profile_events_off"))
        off_action.triggered.connect(
            lambda: self.set_events_enabled(False, profile_id=profile_id)
        )
        on_action = menu.addAction(t("scheduler.profile_events_on"))
        on_action.triggered.connect(
            lambda: self.set_events_enabled(True, profile_id=profile_id)
        )
        menu.exec(button.mapToGlobal(button.rect().bottomLeft()))

    def set_events_enabled(self, enabled, profile_id=None, confirm=True):
        """Bulk on/off for one profile or (profile_id=None) all profiles."""

        if not self.game_id:
            return 0
        if profile_id is None and confirm:
            answer = QMessageBox.question(
                self,
                t("scheduler.all_events_on" if enabled else "scheduler.all_events_off"),
                t(
                    "scheduler.all_events_on_confirm"
                    if enabled else "scheduler.all_events_off_confirm"
                ),
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                return 0
        changed = self.scheduler_service.set_events_enabled(
            self.game_id,
            enabled,
            profile_id=profile_id,
        )
        self.action_feedback_label.setText(
            t(
                "scheduler.events_switched_on"
                if enabled else "scheduler.events_switched_off"
            ).format(count=changed)
        )
        self.refresh()
        return changed

    def _install_row_actions(self, item):
        if item is None:
            return
        action_widget = QWidget(self.tree)
        action_widget.setObjectName("schedulerRowActions")
        action_widget.setStyleSheet(
            """
            QWidget#schedulerRowActions { background: transparent; }
            QToolButton {
                background: transparent;
                border: 0;
                color: #C8D0D9;
                padding: 0;
                margin: 0;
                min-width: 24px;
                max-width: 24px;
                min-height: 24px;
                max-height: 24px;
            }
            QToolButton:hover {
                background: rgba(255, 255, 255, 0.12);
                border-radius: 3px;
            }
            QToolButton:disabled { color: #68717D; }
            """
        )
        action_layout = QHBoxLayout(action_widget)
        action_layout.setContentsMargins(4, 0, 4, 0)
        action_layout.setSpacing(4)
        kind = self._item_kind(item)

        if kind.get("kind") == "event":
            add_button = QToolButton(action_widget)
            add_button.setObjectName("addTaskButton")
            add_button.setAutoRaise(True)
            add_button.setText("+")
            add_button.setToolTip(t("scheduler.add_task"))
            add_button.clicked.connect(lambda: self._add_task_for_item(item))
            action_layout.addWidget(add_button)

        run_button = QToolButton(action_widget)
        run_button.setObjectName("runButton")
        run_button.setAutoRaise(True)
        run_button.setText("▶")
        run_button.setToolTip(t("scheduler.run_selected"))
        run_button.clicked.connect(lambda: self._select_and_run(item))
        action_layout.addWidget(run_button)

        cancel_button = QToolButton(action_widget)
        cancel_button.setObjectName("cancelButton")
        cancel_button.setAutoRaise(True)
        cancel_button.setText("■")
        cancel_button.setToolTip(t("scheduler.cancel_selected"))
        cancel_button.clicked.connect(lambda: self._select_and_cancel(item))
        action_layout.addWidget(cancel_button)

        switch = ToggleSwitch(action_widget)
        switch.setObjectName("enabledSwitch")
        switch.set_active_color(
            self._switch_active_color(self._item_kind(item))
        )
        switch.setChecked(self._row_enabled(item))
        switch.setToolTip(t("scheduler.toggle_enabled"))
        switch.toggled.connect(lambda _checked: self._select_and_toggle(item))
        action_layout.addWidget(switch)

        menu_button = QToolButton(action_widget)
        menu_button.setObjectName("moreButton")
        menu_button.setAutoRaise(True)
        menu_button.setText("⋮")
        menu_button.setToolTip(t("scheduler.more_actions"))
        menu_button.clicked.connect(lambda: self._open_row_menu(item, menu_button))
        action_layout.addWidget(menu_button)

        self.tree.setItemWidget(item, 6, action_widget)
        self._refresh_row_actions(item)

    def _row_enabled(self, item):
        kind = self._item_kind(item)
        if kind.get("kind") == "event":
            return bool(kind.get("enabled", True))
        return bool(kind.get("enabled", True))

    def _refresh_row_actions(self, item):
        widget = self.tree.itemWidget(item, 6)
        if widget is None:
            return
        kind = self._item_kind(item)
        protected = bool(kind.get("protected"))
        system_event = bool(kind.get("system_event"))
        controls = widget.findChildren(QToolButton)
        for control in controls:
            control.setEnabled(True)
        add_button = widget.findChild(QToolButton, "addTaskButton")
        if add_button is not None:
            add_button.setEnabled(kind.get("kind") == "event")
        run_button = widget.findChild(QToolButton, "runButton")
        if run_button is not None:
            run_button.setEnabled(not protected and not system_event)
        cancel_button = widget.findChild(QToolButton, "cancelButton")
        if cancel_button is not None:
            cancel_button.setEnabled(not protected and not system_event)
        menu_button = widget.findChild(QToolButton, "moreButton")
        if menu_button is not None:
            menu_button.setEnabled(True)
        switch = widget.findChild(ToggleSwitch)
        if switch is not None:
            switch.blockSignals(True)
            switch.setChecked(bool(kind.get("enabled", True)))
            switch.set_active_color(self._switch_active_color(kind))
            switch.setEnabled(not protected and not system_event)
            switch.blockSignals(False)

    @staticmethod
    def _switch_active_color(kind):
        """Context color for the enable switch: running yellow, event blue, task green."""

        if kind.get("running"):
            return "#FDC601"
        if kind.get("kind") == "event":
            return "#4F8DFF"
        return "#4FD42A"

    def _add_task_for_item(self, item):
        self.tree.setCurrentItem(item)
        kind = self._item_kind(item)
        self.add_task_dialog(kind)

    def _set_tree_item_colors(self, item, kind, running=False):
        colors = {
            "profile": ("#111111", "#FFFFFF"),
            "event": ("#173A6B", "#FFFFFF"),
            "task": ("#303846", "#F3F4F6"),
        }
        background_name, foreground_name = colors[kind]
        if running:
            background_name = "#FDC601"
            foreground_name = "#111827"
        for column in range(self.tree.columnCount()):
            item.setBackground(column, QColor(background_name))
            item.setForeground(column, QColor(foreground_name))
        if kind in {"profile", "event"}:
            font = item.font(0)
            font.setBold(True)
            item.setFont(0, font)

    def _ensure_init_events(self, game_id=None):
        game_id = game_id or self.game_id
        profiles = self.profile_service.get_profiles_for_game(game_id)
        seed = self.game_service.legacy_init_function_ids(game_id)
        if not profiles:
            self.scheduler_service.ensure_init_event(game_id, seed_functions=seed)
            return
        for profile in profiles:
            profile_id = profile.get("botstudio_profile_id") or profile.get("id") or ""
            self.scheduler_service.ensure_init_event(
                game_id,
                profile_id=profile_id,
                seed_functions=seed,
            )
            self.scheduler_service.ensure_interrupt_event(
                game_id,
                profile_id,
            )

    def _refresh_timeline(self, events):
        horizon_hours = self.timeline_range_combo.currentData() or 24
        items, window_start, window_end = build_timeline_items(events, horizon_hours)
        profile_labels = {}
        for item in items:
            profile_labels[item.profile_id] = self._profile_label(item.profile_id)
        self.timeline_canvas.update_timeline(items, profile_labels, window_start, window_end)

    def _build_demo_event(self, functions):
        function_ids = [function.function_id for function in functions if getattr(function, "saved", False)]
        return self.build_event_from_function_ids(function_ids, name="Demo event", profile_id="demo-profile", priority=10)

    def _profile_label(self, profile_id):
        if not profile_id:
            return "All enabled profiles"
        profile = self.profile_service.get_profile(profile_id)
        return profile.get("name", profile_id) if profile else profile_id

    @staticmethod
    def _format_next_due(event):
        if event.is_interrupt:
            return "Interrupt loop"
        if event.status == "running":
            return "running"
        if not event.next_due_at:
            if event.enabled and event.function_ids and not event.is_init:
                if event.is_due():
                    return t("scheduler.due_now")
                return t("scheduler.on_profile_activation")
            return "due now" if event.function_ids else "not scheduled"
        due = Event._parse_datetime(event.next_due_at)
        if due is None:
            return "invalid schedule"
        return due.astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")

    def _resolve_profile_id(self, value):
        value = str(value or "").strip()
        if not value:
            return ""
        for profile in self.profile_service.get_profiles_for_game(self.game_id):
            profile_id = profile.get("botstudio_profile_id") or profile.get("id")
            if value == profile_id or value == profile.get("name"):
                return profile_id
        return value

    def _profile_selector(self, selected_id=""):
        selector = SearchableComboBox()
        for profile in self.profile_service.get_profiles_for_game(self.game_id):
            profile_id = profile.get("botstudio_profile_id") or profile.get("id")
            if profile_id:
                selector.addItem(profile.get("name") or profile_id, profile_id)
        if selected_id and selector.findData(selected_id) < 0:
            selector.addItem(self._profile_label(selected_id), selected_id)
        selector.setCurrentIndex(selector.findData(selected_id))
        return selector

    def _selected_kind(self):
        if self._current_view_mode() == "Timeline":
            return None
        item = self.tree.currentItem()
        return item.data(0, Qt.UserRole) if item else None

    def _show_event_feedback(self, event_id, message):
        self.action_feedback_label.setText(message)
        kind = self._selected_kind()
        if kind and kind.get("kind") == "event" and kind.get("event_id") == event_id:
            self.selection_label.setText(message)

    def _persist_tree_order(self):
        if not self.game_id or self._current_view_mode() != "Tree":
            return
        for profile_index in range(self.tree.topLevelItemCount()):
            profile_item = self.tree.topLevelItem(profile_index)
            position = 0
            for event_index in range(profile_item.childCount()):
                event_item = profile_item.child(event_index)
                kind = event_item.data(0, Qt.UserRole) or {}
                if kind.get("kind") != "event":
                    continue
                event = self.scheduler_service.load_event(self.game_id, kind["event_id"])
                if event is None or event.is_init:
                    continue
                event.sort_order = position
                position += 1
                self._persist_task_order(event, event_item)
                self.scheduler_service.save_event(event)
        self.refresh()

    @staticmethod
    def _persist_task_order(event, event_item):
        tasks_by_id = {task.task_id: task for task in event.tasks}
        position = 0
        for task_index in range(event_item.childCount()):
            task_item = event_item.child(task_index)
            kind = task_item.data(0, Qt.UserRole) or {}
            task = tasks_by_id.get(kind.get("task_id"))
            if task is None or task.protected:
                continue
            task.sort_order = position
            position += 1

    def build_event_from_function_ids(self, function_ids, name="Manual run", profile_id="global", priority=50):
        function_ids = [function_id for function_id in function_ids if function_id]
        event = Event(
            game_id=self.game_id,
            profile_id=profile_id,
            name=name,
            tasks=[Task(name=name, function_ids=function_ids, priority=priority)] if function_ids else [],
            priority=priority,
            enabled=True,
            status="idle",
            next_due_at=None,
        )
        self.scheduler_service.save_event(event)
        return event

    def add_demo_event(self):
        functions = self.function_service.list_schedulable(self.game_id)
        if not functions:
            return None
        event = self._build_demo_event(functions)
        self.refresh()
        return event

    @staticmethod
    def _function_selector(functions, selected_ids=()):
        selected_ids = list(selected_ids)
        selected_set = set(selected_ids)
        function_by_id = {function.function_id: function for function in functions}

        available = QListWidget()
        selected = QListWidget()
        for widget in (available, selected):
            widget.setSelectionMode(QAbstractItemView.ExtendedSelection)
            widget.setDragDropMode(QAbstractItemView.DragDrop)
            widget.setDefaultDropAction(Qt.MoveAction)
        for function in sorted(functions, key=lambda f: str(f.name).casefold()):
            if function.function_id not in selected_set:
                item = QListWidgetItem(function.name)
                item.setData(Qt.UserRole, function.function_id)
                available.addItem(item)
        for function_id in selected_ids:
            function = function_by_id.get(function_id)
            if function is None:
                continue
            item = QListWidgetItem(function.name)
            item.setData(Qt.UserRole, function.function_id)
            selected.addItem(item)

        search_input = QLineEdit()
        search_input.setPlaceholderText(t("common.search"))
        search_input.setClearButtonEnabled(True)

        def apply_filter(text):
            needle = text.strip().casefold()
            for index in range(available.count()):
                row_item = available.item(index)
                row_item.setHidden(
                    bool(needle) and needle not in row_item.text().casefold()
                )

        search_input.textChanged.connect(apply_filter)

        add_button = QPushButton(">>")
        remove_button = QPushButton("<<")
        transfer_layout = QVBoxLayout()
        transfer_layout.addWidget(add_button)
        transfer_layout.addWidget(remove_button)

        def resort_available():
            available.sortItems(Qt.AscendingOrder)
            apply_filter(search_input.text())

        def transfer_items(source, target):
            for item in source.selectedItems():
                target.addItem(source.takeItem(source.row(item)))
            resort_available()

        add_button.clicked.connect(lambda: transfer_items(available, selected))
        remove_button.clicked.connect(lambda: transfer_items(selected, available))

        layout = QHBoxLayout()
        available_column = QVBoxLayout()
        available_column.addWidget(QLabel(t("scheduler.available_functions")))
        available_column.addWidget(search_input)
        available_column.addWidget(available)
        selected_column = QVBoxLayout()
        selected_column.addWidget(QLabel(t("scheduler.event_functions")))
        selected_column.addWidget(selected)
        layout.addLayout(available_column)
        layout.addLayout(transfer_layout)
        layout.addLayout(selected_column)
        return layout, available, selected

    @staticmethod
    def _selected_function_ids(widget):
        return [
            widget.item(index).data(Qt.UserRole)
            for index in range(widget.count())
        ]

    def add_event_dialog(self):
        if not self.game_id:
            self.selection_label.setText(
                t("scheduler.select_game_before_event")
            )
            return

        dialog = QDialog(self)
        dialog.setWindowTitle(t("scheduler.add_event"))
        dialog.resize(380, 280)

        layout = QVBoxLayout()
        dialog.setLayout(layout)
        name_input = QLineEdit("Manual event")
        profile_input = self._profile_selector()
        multi_profile_checkbox = QCheckBox(t("scheduler.create_for_multiple_profiles"))
        multi_profile_checkbox.setToolTip(t("scheduler.create_for_multiple_profiles_tooltip"))
        profile_checklist = ProfileChecklist(
            [
                (profile_input.itemText(index), profile_input.itemData(index))
                for index in range(profile_input.count())
            ],
            checked_ids=[profile_input.currentData()],
        )
        profile_checklist.setVisible(False)

        def toggle_multi_profile(checked):
            if checked:
                profile_checklist.set_checked(
                    set(profile_checklist.checked_ids()) | {profile_input.currentData()}
                )
            profile_checklist.setVisible(checked)
            profile_input.setVisible(not checked)

        multi_profile_checkbox.toggled.connect(toggle_multi_profile)
        function_service = getattr(self, "function_service", None)
        functions = (
            function_service.list_schedulable(self.game_id)
            if function_service is not None else []
        )
        task_name_input = QLineEdit()
        task_name_input.setPlaceholderText(t("scheduler.task_name_optional"))
        selected_functions = None
        if functions:
            function_layout, _available, selected_functions = self._function_selector(functions)
        priority_widget, priority_spin = self._priority_selector(50)
        mode_combo = QComboBox()
        mode_combo.addItems(sorted(["manual", "fixed"], key=str.casefold))
        interval_input = create_interval_time_edit(300)
        run_at_input = QLineEdit("")

        layout.addWidget(QLabel(t("scheduler.event_name")))
        layout.addWidget(name_input)
        layout.addWidget(QLabel(t("scheduler.profiles")))
        layout.addWidget(profile_input)
        layout.addWidget(multi_profile_checkbox)
        layout.addWidget(profile_checklist)
        layout.addWidget(QLabel(t("scheduler.priority")))
        layout.addWidget(priority_widget)
        layout.addWidget(QLabel(t("scheduler.schedule_mode")))
        layout.addWidget(mode_combo)
        layout.addWidget(QLabel(t("scheduler.interval_time")))
        layout.addWidget(interval_input)
        layout.addWidget(QLabel(t("scheduler.local_times")))
        layout.addWidget(run_at_input)
        if selected_functions is not None:
            dialog.resize(640, 640)
            layout.addWidget(QLabel(t("scheduler.initial_task")))
            layout.addWidget(task_name_input)
            layout.addLayout(function_layout)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)

        if dialog.exec() != QDialog.Accepted:
            return None

        interval_value = interval_seconds_from_editor(interval_input)

        run_at_local = self._parse_local_times(run_at_input.text())
        schedule_mode = mode_combo.currentText() or "manual"

        event = Event(
            game_id=self.game_id,
            profile_id=profile_input.currentData() or "",
            name=name_input.text().strip() or "Manual event",
            priority=priority_spin.value(),
            enabled=True,
            status="idle",
            next_due_at=None,
            schedule_mode=schedule_mode,
            interval_seconds=interval_value if schedule_mode == "fixed" else 0,
            run_at_local=run_at_local if schedule_mode == "fixed" else [],
        )
        function_ids = (
            self._selected_function_ids(selected_functions)
            if selected_functions is not None else []
        )
        task_name = task_name_input.text().strip()
        profile_ids = (
            profile_checklist.checked_ids()
            if multi_profile_checkbox.isChecked() else [event.profile_id]
        )
        if len(profile_ids) > 1:
            events = self.scheduler_service.create_event_for_profiles(
                event,
                profile_ids,
                task_name=task_name,
                function_ids=function_ids,
                variable_service=getattr(self, "variable_service", None),
            )
            self.logger.info(
                "Event Created For Profiles",
                game=self.game_id,
                name=event.name,
                profiles=len(events),
            )
            self.action_feedback_label.setText(
                t("scheduler.event_created_for_profiles").format(
                    name=event.name, count=len(events)
                )
            )
            self.refresh()
            return events[0] if events else None
        if profile_ids and profile_ids[0]:
            event.profile_id = profile_ids[0]
        if function_ids:
            event.tasks.append(
                Task(
                    name=task_name or event.name,
                    function_ids=function_ids,
                    priority=event.priority,
                )
            )
        self.scheduler_service.save_event(event)
        self.logger.info(
            "Event Created",
            game=self.game_id,
            event_id=event.event_id,
            name=event.name,
            profile=event.profile_id,
            priority=event.priority,
        )
        self.refresh()
        return event

    def add_task_dialog(self, selected_kind=None):
        kind = selected_kind or self._selected_kind()
        if not kind or kind.get("kind") not in ("event", "task") or not self.game_id:
            return None

        event = self.scheduler_service.load_event(self.game_id, kind["event_id"])
        if event is None:
            return None

        functions = self.function_service.list_schedulable(self.game_id)

        dialog = QDialog(self)
        dialog.setWindowTitle(t("scheduler.add_task"))
        dialog.resize(420, 360)

        layout = QVBoxLayout()
        dialog.setLayout(layout)
        name_input = QLineEdit("New task")
        priority_widget, priority_spin = self._priority_selector(50)
        selector_layout, _, selected_functions = self._function_selector(functions)

        layout.addWidget(QLabel(t("scheduler.task_name")))
        layout.addWidget(name_input)
        layout.addWidget(QLabel(t("scheduler.priority")))
        layout.addWidget(priority_widget)
        layout.addWidget(QLabel(t("scheduler.functions_in_order")))
        layout.addLayout(selector_layout)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)

        if dialog.exec() != QDialog.Accepted:
            return None

        selected = [function_id for function_id in self._selected_function_ids(selected_functions) if function_id]

        task = Task(
            name=name_input.text().strip() or "New task",
            function_ids=selected,
            priority=priority_spin.value(),
            sort_order=len(event.tasks),
        )
        event.tasks.append(task)
        self.scheduler_service.save_event(event)
        self.logger.info(
            "Task Created",
            game=self.game_id,
            event_id=event.event_id,
            task_id=task.task_id,
            name=task.name,
            function_ids=task.function_ids,
        )
        self.refresh()
        return task

    @staticmethod
    def _parse_local_times(value: str) -> list[str]:
        parsed = []
        for raw_value in value.split(","):
            raw_value = raw_value.strip()
            try:
                hour, minute = (int(part) for part in raw_value.split(":", 1))
            except (TypeError, ValueError):
                continue
            if 0 <= hour <= 23 and 0 <= minute <= 59:
                parsed.append(f"{hour:02d}:{minute:02d}")
        return sorted(set(parsed))

    @staticmethod
    def _priority_selector(value: int = 50):
        """Slider + spinbox for the user range 1-100, like the profile priority."""

        slider = QSlider(Qt.Horizontal)
        slider.setRange(1, 100)
        spin = QSpinBox()
        spin.setRange(1, 100)
        initial = max(1, min(100, int(value)))
        slider.setValue(initial)
        spin.setValue(initial)
        slider.valueChanged.connect(spin.setValue)
        spin.valueChanged.connect(slider.setValue)

        widget = QWidget()
        row = QHBoxLayout(widget)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(slider, stretch=1)
        row.addWidget(spin)
        return widget, spin

    def edit_selected(self):
        kind = self._selected_kind()
        if not kind:
            return None
        if kind["kind"] == "event":
            event = self.scheduler_service.load_event(self.game_id, kind["event_id"])
            if event is None or event.is_init or event.is_interrupt:
                return None
            return self._edit_event_dialog(kind["event_id"])
        if kind["kind"] == "task":
            return self._edit_task_dialog(kind["event_id"], kind["task_id"])
        return None

    def _edit_event_dialog(self, event_id):
        event = self.scheduler_service.load_event(self.game_id, event_id)
        if event is None or event.is_init or event.is_interrupt:
            return None

        dialog = QDialog(self)
        dialog.setWindowTitle(t("scheduler.edit_selected"))
        dialog.resize(380, 280)

        layout = QVBoxLayout()
        dialog.setLayout(layout)
        name_input = QLineEdit(event.name or event.event_id)
        profile_input = self._profile_selector(event.profile_id)
        priority_widget, priority_spin = self._priority_selector(event.priority)
        mode_combo = QComboBox()
        mode_combo.addItems(sorted(["manual", "fixed"], key=str.casefold))
        mode_combo.setCurrentText(event.schedule_mode or "manual")
        interval_input = create_interval_time_edit(event.interval_seconds)
        run_at_input = QLineEdit(", ".join(event.run_at_local))

        layout.addWidget(QLabel(t("scheduler.event_name")))
        layout.addWidget(name_input)
        layout.addWidget(QLabel("Profile"))
        layout.addWidget(profile_input)
        layout.addWidget(QLabel(t("scheduler.priority")))
        layout.addWidget(priority_widget)
        layout.addWidget(QLabel(t("scheduler.schedule_mode")))
        layout.addWidget(mode_combo)
        layout.addWidget(QLabel(t("scheduler.interval_time")))
        layout.addWidget(interval_input)
        layout.addWidget(QLabel(t("scheduler.local_times")))
        layout.addWidget(run_at_input)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)

        if dialog.exec() != QDialog.Accepted:
            return None

        interval_value = interval_seconds_from_editor(interval_input)

        event.name = name_input.text().strip() or event.event_id
        if not event.is_init:
            event.profile_id = profile_input.currentData() or ""
        event.priority = priority_spin.value()
        event.schedule_mode = mode_combo.currentText() or "manual"
        event.interval_seconds = interval_value if event.schedule_mode == "fixed" else 0
        event.run_at_local = self._parse_local_times(run_at_input.text()) if event.schedule_mode == "fixed" else []
        self.scheduler_service.save_event(event)
        self.logger.info(
            "Event Updated",
            game=self.game_id,
            event_id=event.event_id,
            name=event.name,
            profile=event.profile_id,
            priority=event.priority,
        )
        self.refresh()
        return event

    def _edit_task_dialog(self, event_id, task_id):
        event = self.scheduler_service.load_event(self.game_id, event_id)
        if event is None:
            return None
        task = next((task for task in event.tasks if task.task_id == task_id), None)
        if task is None:
            return None

        # Profile Check/Switch sind typischerweise Helper-Functions.
        functions = (
            self.function_service.list_functions(self.game_id)
            if task.protected
            else self.function_service.list_schedulable(self.game_id)
        )

        dialog = QDialog(self)
        dialog.setWindowTitle(t("scheduler.edit_selected"))
        dialog.resize(420, 360)

        layout = QVBoxLayout()
        dialog.setLayout(layout)
        name_input = QLineEdit(task.name or task.task_id)
        name_input.setEnabled(not task.protected)
        priority_widget, priority_spin = self._priority_selector(task.priority)
        selector_layout, _, selected_functions = self._function_selector(
            functions,
            task.function_ids,
        )

        layout.addWidget(QLabel(t("scheduler.task_name")))
        layout.addWidget(name_input)
        layout.addWidget(QLabel(t("scheduler.priority")))
        layout.addWidget(priority_widget)
        layout.addWidget(QLabel(t("scheduler.functions_in_order")))
        layout.addLayout(selector_layout)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)

        if dialog.exec() != QDialog.Accepted:
            return None

        selected = [function_id for function_id in self._selected_function_ids(selected_functions) if function_id]

        if not task.protected:
            task.name = name_input.text().strip() or task.task_id
        task.priority = priority_spin.value()
        task.function_ids = selected
        self.scheduler_service.save_event(event)
        self.logger.info(
            "Task Updated",
            game=self.game_id,
            event_id=event.event_id,
            task_id=task.task_id,
            name=task.name,
            function_ids=task.function_ids,
        )
        self.refresh()
        return task

    def _update_action_states(self):
        kind = self._selected_kind()
        kind_name = kind.get("kind") if kind else None
        is_event = kind_name == "event"
        is_task = kind_name == "task"
        event = (
            self.scheduler_service.load_event(self.game_id, kind["event_id"])
            if (is_event or is_task) and self.game_id
            else None
        )
        is_interrupt = bool(event and event.is_interrupt)
        is_active = bool(
            event
            and event.status in ("queued", "running")
        ) or self._active_manual_run == (kind.get("event_id") if kind else None)
        self.run_button.setEnabled(is_event and not is_interrupt and not is_active)
        self.cancel_button.setEnabled(is_event and not is_interrupt)
        self.add_task_button.setEnabled(is_event or is_task)
        self.edit_button.setEnabled(
            is_task
            or (
                is_event
                and event is not None
                and not event.is_init
                and not is_interrupt
            )
        )
        self.toggle_button.setEnabled(is_task or (is_event and not is_interrupt))
        self.delete_button.setEnabled(is_event or is_task)
        self._update_selection_summary()

    def _update_selection_summary(self):
        kind = self._selected_kind()
        if not kind or kind.get("kind") not in {"event", "task"} or not self.game_id:
            self.selection_label.setText(t("scheduler.no_event_selected"))
            return

        event = self.scheduler_service.load_event(self.game_id, kind["event_id"])
        if event is None:
            self.selection_label.setText(t("scheduler.no_event_selected"))
            return

        if kind["kind"] == "event":
            self.selection_label.setText(
                f"{event.name or event.event_id} · {event.status or 'idle'} · {self._format_next_due(event)}"
            )
            return

        if kind["kind"] == "task":
            task = next((task for task in event.tasks if task.task_id == kind["task_id"]), None)
            if task is None:
                self.selection_label.setText(t("scheduler.no_event_selected"))
                return
            state = "active" if task.active else "inactive"
            self.selection_label.setText(
                f"{task.name or task.task_id} · {state} · {len(task.function_ids)} functions"
            )
            return

        self.selection_label.setText(t("scheduler.no_event_selected"))

    def toggle_selected_enabled(self):
        kind = self._selected_kind()
        if not kind or kind.get("kind") not in {"event", "task"} or not self.game_id:
            return None
        event = self.scheduler_service.load_event(self.game_id, kind["event_id"])
        if event is None:
            return None

        if kind["kind"] == "event":
            if event.is_init or event.is_interrupt:
                return None
            event.enabled = not event.enabled
            self.scheduler_service.save_event(event)
            self.logger.info(
                "Event Toggled",
                game=self.game_id,
                event_id=event.event_id,
                enabled=event.enabled,
            )
            self.refresh()
            return event

        if kind["kind"] == "task":
            task = next((task for task in event.tasks if task.task_id == kind["task_id"]), None)
            if task is None or task.protected:
                return None
            task.enabled = not task.enabled
            self.scheduler_service.save_event(event)
            self.logger.info(
                "Task Toggled",
                game=self.game_id,
                event_id=event.event_id,
                task_id=task.task_id,
                enabled=task.enabled,
            )
            self.refresh()
            return task

        return None

    def delete_selected(self):
        kind = self._selected_kind()
        if not kind or not self.game_id:
            return None

        if kind["kind"] == "event":
            event = self.scheduler_service.load_event(self.game_id, kind["event_id"])
            if event is None or event.is_init or event.is_interrupt:
                return None
            dependencies = [
                f"{task.name or task.task_id}: {function_id}"
                for task in event.tasks
                for function_id in task.function_ids
            ]
            warning = event.name or event.event_id
            if dependencies:
                warning += "\n\nFunctions in this event:\n" + "\n".join(dependencies)
            buttons = QDialogButtonBox(QDialogButtonBox.Yes | QDialogButtonBox.No)
            confirmation = QDialog(self)
            confirmation.setWindowTitle(t("scheduler.delete_selected"))
            confirmation_layout = QVBoxLayout(confirmation)
            confirmation_layout.addWidget(QLabel(warning))
            confirmation_layout.addWidget(buttons)
            buttons.accepted.connect(confirmation.accept)
            buttons.rejected.connect(confirmation.reject)
            if confirmation.exec() != QDialog.Accepted:
                return None
            deleted = self.scheduler_service.delete_event(self.game_id, event.event_id)
            if deleted:
                self.logger.info(
                    "Event Deleted",
                    game=self.game_id,
                    event_id=event.event_id,
                    name=event.name,
                )
                self.refresh()
                return event
            return None

        if kind["kind"] == "task":
            event = self.scheduler_service.load_event(self.game_id, kind["event_id"])
            if event is None:
                return None
            task = next((task for task in event.tasks if task.task_id == kind["task_id"]), None)
            if task is None or task.protected:
                return None
            dependencies = "\n".join(task.function_ids) or "no functions"
            confirmation = QMessageBox.question(
                self,
                t("scheduler.delete_selected"),
                f"{task.name or task.task_id}\n\nFunctions:\n{dependencies}\n\nDelete?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if confirmation != QMessageBox.Yes:
                return None
            event.tasks.remove(task)
            self.scheduler_service.save_event(event)
            self.logger.info(
                "Task Deleted",
                game=self.game_id,
                event_id=event.event_id,
                task_id=task.task_id,
            )
            self.refresh()
            return task

        return None

    def cancel_selected(self):
        kind = self._selected_kind()
        if not kind or kind.get("kind") != "event" or not self.game_id:
            self.action_feedback_label.setText(t("scheduler.select_event_to_cancel"))
            return None

        event = self.scheduler_service.load_event(self.game_id, kind["event_id"])
        if event is None or event.is_init or event.is_interrupt:
            self.action_feedback_label.setText(t("scheduler.cancel_unavailable"))
            return None
        cancelled = self.scheduler_service.cancel_event(event.event_id)
        if cancelled:
            event.status = "cancelling"
            self.scheduler_service.save_event(event)
            message = t("scheduler.cancel_requested")
            self.action_feedback.emit(event.event_id, message)
            self.logger.info(
                "Event Cancel Requested",
                game=self.game_id,
                event_id=event.event_id,
            )
            self.refresh()
            return event
        self.action_feedback_label.setText(t("scheduler.cancel_not_running"))
        return None

    @property
    def runtime_service(self):
        """Lazy, weil der produktive Runner OCR- und Vision-Dienste mitbringt."""

        if self._runtime_service is None:
            from services.runtime_service import RuntimeService

            self._runtime_service = RuntimeService(variable_service=self.variable_service)
            self.scheduler_service.statistics_service = self._runtime_service.statistics_service
        return self._runtime_service

    def closeEvent(self, event):
        self._manual_run_executor.shutdown(wait=False, cancel_futures=False)
        super().closeEvent(event)

    def run_selected(self):
        kind = self._selected_kind()
        if not kind or kind.get("kind") != "event" or not self.game_id:
            self.action_feedback_label.setText(t("scheduler.select_event_to_run"))
            return False

        event = self.scheduler_service.load_event(self.game_id, kind["event_id"])
        if event is None:
            self.action_feedback_label.setText(t("scheduler.event_not_found"))
            return False

        if event.status in ("queued", "running") or self._active_manual_run == event.event_id:
            self.action_feedback_label.setText(t("scheduler.event_already_running"))
            return False

        if not event.function_ids:
            self.logger.info(
                "Manual Event Run Rejected",
                game=self.game_id,
                event_id=event.event_id,
                reason="no_active_tasks",
            )
            self.action_feedback_label.setText(
                t("scheduler.event_no_active_tasks")
            )
            return False

        profile = self.profile_service.get_profile(event.profile_id) if event.profile_id else None
        if not profile:
            self.logger.error(
                "Event Run Rejected",
                game=self.game_id,
                event_id=event.event_id,
                reason="profile_missing",
                next_action="assign the event to an existing profile",
            )
            self.action_feedback_label.setText(t("scheduler.event_missing_profile"))
            return False

        try:
            self._active_manual_run = event.event_id
            self._manual_run_executor.submit(
                self._queue_selected_event_background,
                event.event_id,
            )
        except Exception as error:
            self._active_manual_run = None
            self.logger.error(
                "Manual Event Queue Failed",
                game=self.game_id,
                event_id=event.event_id,
                error=repr(error),
            )
            self.action_feedback.emit(event.event_id, t("scheduler.event_failed"))
            return False

        return True

    def _queue_selected_event_background(self, event_id):
        try:
            event = self.scheduler_service.load_event(self.game_id, event_id)
            if event is None:
                raise ValueError("event is no longer available")
            self.scheduler_service.queue_event_now(event)
            self.logger.info(
                "Event Run Queued",
                game=self.game_id,
                event_id=event_id,
                profile=event.profile_id,
                next_due_at=event.next_due_at,
            )
            self.action_feedback.emit(event_id, t("scheduler.event_queued"))
        except Exception as error:
            self.logger.error(
                "Manual Event Queue Failed",
                game=self.game_id,
                event_id=event_id,
                error=repr(error),
            )
            self.action_feedback.emit(event_id, t("scheduler.event_failed"))
        finally:
            self._active_manual_run = None
            self.schedule_changed.emit(self.game_id)


if __name__ == "__main__":
    import sys
    from PySide6.QtWidgets import QApplication

    app = QApplication(sys.argv)
    view = SchedulerView("game-1")
    view.show()
    sys.exit(app.exec())
