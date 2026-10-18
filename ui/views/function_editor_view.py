from copy import deepcopy
import json
from uuid import uuid4

from PySide6.QtCore import QObject, QRunnable, QThreadPool, QMimeData, QPoint, QSize, QSettings, Qt, Signal, QTimer
from PySide6.QtGui import QColor, QDrag, QIcon, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QCompleter,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QGraphicsScene,
    QGraphicsView,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QFrame,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from models.bot_function import (
    BotFunction,
    FUNCTION_KIND_HELPER,
    FUNCTION_KIND_REGULAR,
)
from models.function_block import FunctionBlock
from models.function_connection import FunctionConnection
from services.app_config_service import AppConfigService
from ui.image_utils import load_pixmap
from services.function_block_catalog import (
    AVAILABLE_DETECTORS,
    BLOCK_CATALOG,
    BLOCK_CONFIGURE_FIELD,
    BLOCK_COORDINATE_ANCHOR,
    BLOCK_FIND_AND_TAP_ELEMENT,
    BLOCK_FAILURE,
    BLOCK_FIND_COLOR_TRIGGER,
    BLOCK_FIND_ELEMENT,
    BLOCK_FIND_SHAPE,
    BLOCK_GATE,
    BLOCK_SUCCESS,
    BLOCK_SWIPE,
    BLOCK_MATH,
    BLOCK_REPEAT,
    BLOCK_STOP_EVENT,
    BLOCK_SET_EVENT_DUE,
    BLOCK_RESCHEDULE_EVENT,
    BLOCK_SEARCH_OCR,
    BLOCK_SEARCH_OCR_TAP,
    BLOCK_SWIPE_SEARCH,
    BLOCK_SWIPE_SEARCH_TAP,
    BLOCK_TAP_ELEMENT,
    BLOCK_TAP_POINT,
    BLOCK_CLEAR_FIELD,
    BLOCK_TYPE_TEXT,
    BLOCK_VARIABLE,
    BLOCK_WAIT_FOR_ELEMENT,
    BLOCK_WAIT_FOR_ELEMENT_AND_TAP,
    COMPARE_OPERATORS,
    GATE_OPERATORS,
    MATH_OPERATORS,
    OCR_EXPECT_TYPES,
    OUTCOME_BODY,
    SWIPE_DIRECTIONS,
)
from services.function_end_audit import OPEN_END_OUTCOMES, audit_game, block_outcomes
from services.function_service import FunctionService
from services.game_service import GameService
from services.function_runner import FunctionRunner
from services.profile_service import ProfileService
from services.runtime_supervisor import RuntimeSupervisor
from services.github_auth_service import GitHubAuthError, GitHubAuthService
from services.reinitialize_service import defer_notifications
from services.library_service import LibraryService
from services.screen_service import ScreenService
from services.scheduler_service import Event, SchedulerService
from services.scheduler_field_service import SchedulerFieldService
from services.logging_service import get_logger
from services.coordinate_search_service import build_offset_search_region, resolve_ocr_region
from services.variable_service import (
    VALUE_TYPE_ANY,
    VALUE_TYPE_BOOLEAN,
    VALUE_TYPE_COLLECTION,
    VALUE_TYPE_DATETIME,
    VALUE_TYPE_DURATION,
    VALUE_TYPE_NUMBER,
    VALUE_TYPE_TEXT,
    VARIABLE_KIND_CONFIG,
    VARIABLE_KIND_LIVE,
    VARIABLE_KIND_PROFILE,
    VariableService,
)
from ui.i18n.translator import t
from ui.views.learning_view import LearningView
from ui.widgets.profile_emulator_bar import ProfileEmulatorBar
from ui.widgets.searchable_combo import SearchableComboBox
from ui.widgets.event_create_dialog import EventCreateDialog
from ui.widgets.variable_dialog import VariableCreateDialog
from ui.widgets.variable_value_input import VariableValueInput


class _EventChoiceSignals(QObject):
    finished = Signal(object, object, object)


class _EventChoiceTask(QRunnable):
    def __init__(self, service, game_id, profile_id, profile_service):
        super().__init__()
        self.setAutoDelete(False)
        self.service = service
        self.game_id = game_id
        self.profile_id = profile_id
        self.profile_service = profile_service
        self.signals = _EventChoiceSignals()

    def run(self):
        try:
            profile = self.profile_service.get_profile(self.profile_id)
            if not profile:
                raise ValueError("selected account no longer exists")
            if profile.get("game") != self.game_id:
                raise ValueError("selected account belongs to another game")
            scheduler_profile_id = str(
                profile.get("botstudio_profile_id") or profile.get("id") or ""
            )
            if not scheduler_profile_id:
                raise ValueError("selected account has no profile identity")
            events = [
                event for event in self.service.list_events(self.game_id, scheduler_profile_id)
                if event.profile_id == scheduler_profile_id
                and event.enabled and not event.is_init and not event.is_interrupt
            ]
            self.signals.finished.emit(self, events, None)
        except Exception as error:
            self.signals.finished.emit(self, None, error)


class _FunctionSaveSignals(QObject):
    finished = Signal(object, object)


class _FunctionSaveTask(QRunnable):
    def __init__(self, service, auth, function):
        super().__init__()
        self.service = service
        self.auth = auth
        self.function = function
        self.notifications = []
        self.signals = _FunctionSaveSignals()

    def run(self):
        try:
            account = self.auth.restore()
        except GitHubAuthError as error:
            self.signals.finished.emit(None, t("function.identity_check_failed").format(error=str(error)))
            return
        except (OSError, ValueError):
            self.signals.finished.emit(
                None, t("function.identity_check_failed").format(error=t("github.operation_failed")),
            )
            return
        try:
            with defer_notifications() as self.notifications:
                self.function.bump_revision("", "saved from function editor", account=account)
                self.service.save(self.function)
        except Exception as error:
            self.signals.finished.emit(None, str(error))
        else:
            self.signals.finished.emit(self.function, "")


class DebugValuesDialog(QDialog):
    """Edits transient Configure Field values for Function debugging."""

    def __init__(self, fields, parent=None):
        super().__init__(parent)
        self.setWindowTitle(t("function.debug_values"))
        self.resize(460, min(640, 180 + len(fields) * 52))
        self._fields = fields
        self._controls = {}

        layout = QVBoxLayout(self)
        hint = QLabel(t("function.debug_values_hint"))
        hint.setWordWrap(True)
        layout.addWidget(hint)

        form_widget = QWidget()
        form = QFormLayout(form_widget)
        for field in fields:
            control = self._create_control(field)
            control.setToolTip(str(field.get("description") or ""))
            self._controls[field["name"]] = control
            form.addRow(field["name"], control)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(form_widget)
        layout.addWidget(scroll, stretch=1)

        self.error_label = QLabel("")
        self.error_label.setWordWrap(True)
        self.error_label.setStyleSheet("color: #D64545;")
        layout.addWidget(self.error_label)

        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok
            | QDialogButtonBox.Cancel
            | QDialogButtonBox.Reset
        )
        buttons.accepted.connect(self._validate_and_accept)
        buttons.rejected.connect(self.reject)
        buttons.button(QDialogButtonBox.Reset).clicked.connect(self._reset)
        layout.addWidget(buttons)

    def _create_control(self, field):
        value = field.get("value")
        options = list(field.get("options") or [])
        if options:
            combo = QComboBox()
            for option in options:
                combo.addItem(str(option), option)
            index = combo.findData(value)
            if index < 0 and value is not None:
                combo.addItem(str(value), value)
                index = combo.count() - 1
            combo.setCurrentIndex(max(0, index))
            return combo

        value_type = field.get("value_type")
        if value_type == VALUE_TYPE_BOOLEAN:
            check = QCheckBox()
            check.setChecked(bool(value))
            return check
        if value_type in {VALUE_TYPE_NUMBER, VALUE_TYPE_DURATION}:
            spin = QDoubleSpinBox()
            spin.setRange(
                0 if value_type == VALUE_TYPE_DURATION else -1_000_000_000,
                1_000_000_000,
            )
            spin.setDecimals(4)
            spin.setValue(float(value or 0))
            if value_type == VALUE_TYPE_DURATION:
                spin.setSuffix(" s")
            return spin
        if value_type == VALUE_TYPE_COLLECTION:
            edit = QTextEdit()
            edit.setMaximumHeight(120)
            edit.setPlainText(
                json.dumps(
                    value if isinstance(value, list) else [],
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return edit

        return QLineEdit("" if value is None else str(value))

    @staticmethod
    def _control_value(field, control):
        if isinstance(control, QComboBox):
            return control.currentData()
        if isinstance(control, QCheckBox):
            return control.isChecked()
        if isinstance(control, QDoubleSpinBox):
            value = control.value()
            return int(value) if value.is_integer() else value
        if isinstance(control, QTextEdit):
            value = json.loads(control.toPlainText() or "[]")
            if not isinstance(value, list):
                raise ValueError(
                    t("function.debug_collection_required").format(
                        name=field["name"],
                    )
                )
            return value
        return control.text()

    def values(self):
        return {
            field["name"]: self._control_value(
                field,
                self._controls[field["name"]],
            )
            for field in self._fields
        }

    def _validate_and_accept(self):
        try:
            self.values()
        except (TypeError, ValueError) as error:
            self.error_label.setText(str(error))
            return
        self.accept()

    def _reset(self):
        for field in self._fields:
            value = field.get("default")
            control = self._controls[field["name"]]
            if isinstance(control, QComboBox):
                index = control.findData(value)
                control.setCurrentIndex(max(0, index))
            elif isinstance(control, QCheckBox):
                control.setChecked(bool(value))
            elif isinstance(control, QDoubleSpinBox):
                control.setValue(float(value or 0))
            elif isinstance(control, QTextEdit):
                control.setPlainText(
                    json.dumps(
                        value if isinstance(value, list) else [],
                        ensure_ascii=False,
                        indent=2,
                    )
                )
            else:
                control.setText("" if value is None else str(value))
        self.error_label.clear()


class BlockPalette(QListWidget):
    order_changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(True)
        self.setDragDropMode(QAbstractItemView.DragDrop)
        self.setDefaultDropAction(Qt.MoveAction)
        self.setSelectionMode(QListWidget.SingleSelection)

    def startDrag(self, supported_actions):
        item = self.currentItem()
        if not item:
            return

        mime = self.model().mimeData(self.selectedIndexes()) or QMimeData()
        mime.setText(item.data(Qt.UserRole))

        drag = QDrag(self)
        drag.setMimeData(mime)
        drag.exec(Qt.CopyAction | Qt.MoveAction, Qt.CopyAction)

    def dropEvent(self, event):
        internal_move = event.source() is self
        event.setDropAction(Qt.MoveAction if internal_move else Qt.CopyAction)
        super().dropEvent(event)
        if internal_move:
            self.order_changed.emit()


def ordered_block_types(available_types, stored_order):
    available = list(available_types)
    if isinstance(stored_order, str):
        stored_order = [value for value in stored_order.split(",") if value]
    stored = []
    for block_type in stored_order or []:
        if block_type in available and block_type not in stored:
            stored.append(block_type)
    return stored + [block_type for block_type in available if block_type not in stored]


class ClickableImage(QLabel):

    point_clicked = Signal(int, int)
    region_selected = Signal(object)

    def __init__(self, text="", parent=None):
        super().__init__(text, parent)
        self.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        self.source_size = None
        self.overlay_region = None
        self.region_selection_active = False
        self.region_selection_start = None
        self.region_selection_preview = None

    def set_screenshot(self, pixmap, width):

        self.cancel_region_selection()
        self.source_size = pixmap.size()

        scaled = pixmap.scaledToWidth(
            width,
            Qt.SmoothTransformation,
        )

        self.setPixmap(scaled)
        # widgetResizable=False on the parent scroll area needs an explicit
        # size, otherwise the label keeps its previous (thin) size
        self.resize(scaled.size())

    def clear_screenshot(self, text):

        self.cancel_region_selection()
        self.source_size = None
        self.overlay_region = None
        self.setPixmap(QPixmap())
        self.setText(text)

    def set_overlay_region(self, region):

        self.overlay_region = region
        self.update()

    def begin_region_selection(self):
        def begin_region_selection(self):
            self.region_selection_active = True
            self.region_selection_start = None
            self.region_selection_preview = None
            self.setCursor(Qt.CursorShape.CrossCursor)

        def cancel_region_selection(self):
            self.region_selection_active = False
            self.region_selection_start = None
            self.region_selection_preview = None
            self.unsetCursor()
            self.update()

        def _source_point(self, point):
            pixmap = self.pixmap()
            if not pixmap or pixmap.isNull() or not self.source_size:
                return None
            scale_x = self.source_size.width() / pixmap.width()
            scale_y = self.source_size.height() / pixmap.height()
            return QPoint(
                max(0, min(self.source_size.width() - 1, round(point.x() * scale_x))),
                max(0, min(self.source_size.height() - 1, round(point.y() * scale_y))),
            )

        @staticmethod
        def _selection_region(first, second):
            return {
                "x": min(first.x(), second.x()),
                "y": min(first.y(), second.y()),
                "width": abs(second.x() - first.x()),
                "height": abs(second.y() - first.y()),
            }
        self.region_selection_active = True
        self.region_selection_start = None
        self.region_selection_preview = None
        self.setCursor(Qt.CursorShape.CrossCursor)

    def cancel_region_selection(self):
        self.region_selection_active = False
        self.region_selection_start = None
        self.region_selection_preview = None
        self.unsetCursor()
        self.update()

    def _source_point(self, point):
        pixmap = self.pixmap()
        if not pixmap or pixmap.isNull() or not self.source_size:
            return None
        scale_x = self.source_size.width() / pixmap.width()
        scale_y = self.source_size.height() / pixmap.height()
        return QPoint(
            max(0, min(self.source_size.width() - 1, round(point.x() * scale_x))),
            max(0, min(self.source_size.height() - 1, round(point.y() * scale_y))),
        )

    @staticmethod
    def _selection_region(first, second):
        return {
            "x": min(first.x(), second.x()),
            "y": min(first.y(), second.y()),
            "width": abs(second.x() - first.x()),
            "height": abs(second.y() - first.y()),
        }

    def paintEvent(self, event):

        super().paintEvent(event)

        pixmap = self.pixmap()
        if not pixmap or pixmap.isNull() or not self.source_size:
            return

        scale_x = pixmap.width() / self.source_size.width()
        scale_y = pixmap.height() / self.source_size.height()
        painter = QPainter(self)
        region = self.overlay_region
        if isinstance(region, dict):
            reference_width = int(region.get("screen_width") or self.source_size.width())
            reference_height = int(region.get("screen_height") or self.source_size.height())
            region_scale_x = self.source_size.width() / max(1, reference_width)
            region_scale_y = self.source_size.height() / max(1, reference_height)
            painter.setPen(QPen(Qt.GlobalColor.red, 3))
            if not region.get("point_only"):
                painter.drawRect(
                    round(int(region.get("x", 0)) * region_scale_x * scale_x),
                    round(int(region.get("y", 0)) * region_scale_y * scale_y),
                    max(1, round(int(region.get("width", 0)) * region_scale_x * scale_x)),
                    max(1, round(int(region.get("height", 0)) * region_scale_y * scale_y)),
                )
            if all(key in region for key in ("swipe_start_x", "swipe_start_y", "swipe_end_x", "swipe_end_y")):
                start_x = round(int(region["swipe_start_x"]) * scale_x)
                start_y = round(int(region["swipe_start_y"]) * scale_y)
                end_x = round(int(region["swipe_end_x"]) * scale_x)
                end_y = round(int(region["swipe_end_y"]) * scale_y)
                painter.setPen(QPen(Qt.GlobalColor.cyan, 3))
                painter.drawLine(start_x, start_y, end_x, end_y)
                painter.drawEllipse(start_x - 5, start_y - 5, 10, 10)
                painter.drawEllipse(end_x - 3, end_y - 3, 6, 6)
            if "tap_x" in region and "tap_y" in region:
                tap_x = round(int(region["tap_x"]) * scale_x)
                tap_y = round(int(region["tap_y"]) * scale_y)
                painter.setPen(QPen(Qt.GlobalColor.green, 3))
                painter.drawEllipse(tap_x - 6, tap_y - 6, 12, 12)
                painter.drawLine(tap_x - 10, tap_y, tap_x + 10, tap_y)
                painter.drawLine(tap_x, tap_y - 10, tap_x, tap_y + 10)
        preview = self.region_selection_preview
        if isinstance(preview, dict):
            painter.setPen(QPen(Qt.GlobalColor.yellow, 2, Qt.PenStyle.DashLine))
            painter.drawRect(
                round(int(preview["x"]) * scale_x),
                round(int(preview["y"]) * scale_y),
                max(1, round(int(preview["width"]) * scale_x)),
                max(1, round(int(preview["height"]) * scale_y)),
            )
        painter.end()

    def mousePressEvent(self, event):

        if self.region_selection_active and event.button() == Qt.MouseButton.LeftButton:
            self.region_selection_start = self._source_point(event.position().toPoint())
            self.region_selection_preview = None
            self.update()
            event.accept()
            return

        pixmap = self.pixmap()

        if pixmap and not pixmap.isNull() and self.source_size:
            point = event.position().toPoint()

            # the label shows a downscaled copy, the device expects source pixels
            scale_x = self.source_size.width() / pixmap.width()
            scale_y = self.source_size.height() / pixmap.height()

            self.point_clicked.emit(
                int(point.x() * scale_x),
                int(point.y() * scale_y),
            )

        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self.region_selection_active and self.region_selection_start is not None:
            point = self._source_point(event.position().toPoint())
            if point is not None:
                self.region_selection_preview = self._selection_region(
                    self.region_selection_start,
                    point,
                )
                self.update()
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if (
            self.region_selection_active
            and event.button() == Qt.MouseButton.LeftButton
            and self.region_selection_start is not None
        ):
            point = self._source_point(event.position().toPoint())
            region = (
                self._selection_region(self.region_selection_start, point)
                if point is not None
                else None
            )
            self.cancel_region_selection()
            self.region_selected.emit(
                region
                if region and region["width"] > 0 and region["height"] > 0
                else None
            )
            event.accept()
            return
        super().mouseReleaseEvent(event)


class DetailsEdit(QTextEdit):

    edit_requested = Signal()

    def mouseDoubleClickEvent(self, event):
        self.edit_requested.emit()
        super().mouseDoubleClickEvent(event)


class OpenEndsDialog(QDialog):
    """Functions with unwired failed / not_found outputs, worst first."""

    def __init__(self, rows, parent=None):
        super().__init__(parent)
        self.setWindowTitle(t("function.open_ends_title"))
        self.resize(720, 520)
        self.selected_function_id = ""
        layout = QVBoxLayout(self)
        total = sum(row["open_ends"] for row in rows)
        affected = sum(1 for row in rows if row["open_ends"])
        hint = QLabel(
            t("function.open_ends_hint").format(total=total, functions=affected)
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.table = QTableWidget(len(rows), 3, self)
        self.table.setHorizontalHeaderLabels([
            t("function.open_ends_col_function"),
            t("function.open_ends_col_kind"),
            t("function.open_ends_col_count"),
        ])
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.verticalHeader().setVisible(False)
        for row_index, row in enumerate(rows):
            name_item = QTableWidgetItem(str(row["name"]))
            name_item.setData(Qt.UserRole, row["function_id"])
            name_item.setToolTip(
                "\n".join(
                    f"{detail['block_name']} → {detail['outcome']}"
                    for detail in row["details"]
                )
            )
            kind_item = QTableWidgetItem(
                t("function.kind_helper")
                if row["kind"] == FUNCTION_KIND_HELPER
                else t("function.kind_regular")
            )
            count_item = QTableWidgetItem()
            count_item.setData(Qt.DisplayRole, int(row["open_ends"]))
            if row["open_ends"]:
                count_item.setForeground(QColor(255, 140, 0))
            for column, item in enumerate((name_item, kind_item, count_item)):
                self.table.setItem(row_index, column, item)
        header = self.table.horizontalHeader()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, header.ResizeMode.Stretch)
        self.table.itemDoubleClicked.connect(self._open_row)
        layout.addWidget(self.table)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        open_button = buttons.addButton(
            t("function.open_ends_open"),
            QDialogButtonBox.AcceptRole,
        )
        open_button.clicked.connect(self._open_selected)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _open_row(self, item):
        name_item = self.table.item(item.row(), 0)
        self.selected_function_id = str(name_item.data(Qt.UserRole) or "")
        if self.selected_function_id:
            self.accept()

    def _open_selected(self):
        rows = self.table.selectionModel().selectedRows()
        if rows:
            self._open_row(self.table.item(rows[0].row(), 0))


class FunctionCanvas(QGraphicsView):

    POSITIVE_OUTCOMES = ("ok", "found", "true", "success", "done", "elapsed", "out_1", "out_2", "out_3", "out_4")
    NEGATIVE_OUTCOMES = ("failed", "not_found", "false", "pending")
    WIRE_WIDTH = 4

    block_dropped = Signal(str, QPoint)
    block_selected = Signal(str)
    block_deleted = Signal(str)
    block_moved = Signal(str, QPoint)
    connection_requested = Signal(str, str, str, str)
    connection_removed = Signal(str, str)
    blocks_clone_requested = Signal()
    # (source_block_id, outcome, terminal_block_type, force_new_block)
    end_terminal_requested = Signal(str, str, str, bool)
    open_ends_changed = Signal(int)
    OPEN_END_PEN_COLOR = QColor(255, 140, 0)

    def _show_output_port_menu(self, block_id, outcome, global_pos):
        menu = QMenu(self)
        actions = {
            menu.addAction(t("function.end_wire_ok")): (BLOCK_SUCCESS, False),
            menu.addAction(t("function.end_wire_fail")): (BLOCK_FAILURE, False),
        }
        menu.addSeparator()
        actions[menu.addAction(t("function.end_wire_ok_new"))] = (BLOCK_SUCCESS, True)
        actions[menu.addAction(t("function.end_wire_fail_new"))] = (BLOCK_FAILURE, True)
        chosen = menu.exec(global_pos)
        if chosen in actions:
            terminal_type, force_new = actions[chosen]
            self.end_terminal_requested.emit(block_id, outcome, terminal_type, force_new)

    @classmethod
    def outcome_color(cls, outcome):

        if outcome == OUTCOME_BODY:
            return Qt.GlobalColor.yellow

        if outcome == "coordinates":
            return Qt.GlobalColor.darkYellow

        if outcome in cls.NEGATIVE_OUTCOMES:
            return Qt.GlobalColor.red

        return Qt.GlobalColor.green

    @staticmethod
    def block_color(block):

        if block.block_type != BLOCK_VARIABLE:
            return Qt.GlobalColor.darkGray

        if block.params.get("mode") == "write":
            return Qt.GlobalColor.darkMagenta

        return Qt.GlobalColor.darkCyan

    @staticmethod
    def input_ports(block):
        """Logic gates expose one port per operand, repeat exposes input and break ports, every other block exactly one."""

        if block.block_type == BLOCK_GATE:
            count = max(
                1,
                len(block.params.get("operands", [])),
            )

            return [
                f"operand_{index}"
                for index in range(count)
            ]

        if block.block_type == "repeat":
            return ["input", "break"]

        if block.block_type in (
            "ocr_read",
            BLOCK_SEARCH_OCR,
            BLOCK_SWIPE_SEARCH,
            BLOCK_FIND_ELEMENT,
            "find_color_trigger",
            "find_shape",
            BLOCK_FIND_AND_TAP_ELEMENT,
            BLOCK_TAP_ELEMENT,
            BLOCK_WAIT_FOR_ELEMENT_AND_TAP,
            BLOCK_TAP_POINT,
        ):
            return ["input", "coordinates"]

        if (
            block.block_type == BLOCK_VARIABLE
            and block.params.get("mode") == "write"
        ):
            return ["input", "value"]

        return ["input"]

    def __init__(self, parent=None):
        self.scene_model = QGraphicsScene()
        super().__init__(self.scene_model, parent)
        self.setAcceptDrops(True)
        # rubber band on empty canvas, block dragging stays item-driven
        self.setDragMode(QGraphicsView.RubberBandDrag)
        self.setSceneRect(0, 0, 2400, 1400)
        self._positions = {}
        self._selected = ""
        self._drag_block_id = ""
        self._drag_offset = QPoint()
        self._pending_output = None
        self._drag_reconnect = None
        self._drag_wire = None
        self._drag_source = None
        self._drag_start = QPoint()
        self._press_scene_pos = None
        self._press_block_id = ""
        self.layout_positions = {}
        self._block_rects = {}
        self._block_highlights = {}
        self._selected_block_id = ""
        self.scene_model.selectionChanged.connect(
            self._selection_changed
        )

    def set_layout_positions(
        self,
        positions: dict,
    ):

        self.layout_positions = positions or {}

    def _fit_scene_to_content(self):
        content_rect = self.scene_model.itemsBoundingRect()
        if content_rect.isNull():
            self.setSceneRect(0, 0, 2400, 1400)
            return
        margin = 120
        self.setSceneRect(content_rect.adjusted(-margin, -margin, margin, margin))

    def update_position(
        self,
        block_id: str,
        position: QPoint,
    ):

        self._positions[block_id] = position
        self.layout_positions[block_id] = {
            "x": position.x(),
            "y": position.y(),
        }

    def dragEnterEvent(self, event):
        if event.mimeData().hasText():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        event.acceptProposedAction()

    def dropEvent(self, event):
        position = self.mapToScene(event.position().toPoint())
        self.block_dropped.emit(
            event.mimeData().text(),
            position.toPoint(),
        )
        event.acceptProposedAction()

    def draw_blocks(self, blocks, connections=None):
        self._cancel_drag_wire()
        preserved_selection = set(self.selected_block_ids())
        if self._selected_block_id:
            preserved_selection.add(self._selected_block_id)
        block_ids = {block.block_id for block in blocks}
        preserved_selection &= block_ids
        if self._selected_block_id not in block_ids:
            self._selected_block_id = ""
        self._block_rects = {}
        self._block_highlights = {}
        self.scene_model.clear()

        if connections is None:
            connections = []

        current_function = getattr(self, "current_function", None)
        wired_outputs = {
            (connection.source_block_id, connection.source_outcome)
            for connection in connections
        }
        for block in blocks:
            for branch_outcome, targets in (block.branches or {}).items():
                if targets:
                    wired_outputs.add((block.block_id, branch_outcome))
        open_end_count = 0

        for index, block in enumerate(blocks):
            if block.block_id in self.layout_positions:
                stored_position = self.layout_positions[block.block_id]
                position = QPoint(
                    int(stored_position.get("x", 80 + index * 230)),
                    int(stored_position.get("y", 100)),
                )
            else:
                position = self._positions.get(
                    block.block_id,
                    QPoint(
                        int(80 + index * 230),
                        100,
                    ),
                )
            self._positions[block.block_id] = position

            rect = self.scene_model.addRect(
                0,
                0,
                190,
                82,
                QPen(Qt.GlobalColor.transparent),
            )
            rect.setBrush(self.block_color(block))
            rect.setPen(QPen(Qt.GlobalColor.transparent, 1))
            rect.setData(0, block.block_id)

            text = self.scene_model.addText(
                f"{block.name or block.block_type}"
            )
            text.setDefaultTextColor(Qt.GlobalColor.white)
            text.setPos(10, 10)
            text.setData(0, block.block_id)

            group = self.scene_model.createItemGroup(
                [rect, text]
            )
            group.setPos(position)
            group.setData(0, block.block_id)
            group.setFlag(
                group.GraphicsItemFlag.ItemIsMovable,
                True,
            )
            group.setFlag(
                group.GraphicsItemFlag.ItemIsSelectable,
                True,
            )
            # blocks/ports must stay above wires so they remain clickable
            # even where a wire's curve crosses over them
            group.setZValue(10)
            self._block_rects[block.block_id] = rect

            outcomes = list(
                BLOCK_CATALOG.get(
                    block.block_type,
                    {},
                ).get("outcomes", [])
            )

            if block.block_type == "splitter":
                output_count = max(2, min(4, int(block.params.get("outputs", 2))))
                outcomes = [f"out_{i+1}" for i in range(output_count)]
            elif block.block_type == "call_function":
                helper_id = block.params.get("function_id")
                if helper_id and current_function:
                    helper = self.function_service.load(current_function.game_id, helper_id)
                    if helper and helper.interface.outcomes:
                        outcomes = list(helper.interface.outcomes)

            for port_index, outcome in enumerate(outcomes):
                is_open_end = (
                    outcome in OPEN_END_OUTCOMES
                    and block.block_type != BLOCK_FAILURE
                    and (block.block_id, outcome) not in wired_outputs
                )
                port = self.scene_model.addEllipse(
                    178,
                    12 + port_index * 20,
                    12,
                    12,
                    QPen(self.OPEN_END_PEN_COLOR, 3)
                    if is_open_end
                    else QPen(Qt.GlobalColor.white),
                )
                port.setBrush(
                    self.outcome_color(outcome)
                )
                port.setToolTip(
                    f"{outcome} – {t('function.open_end_tooltip')}"
                    if is_open_end
                    else outcome
                )
                port.setData(3, "open_end" if is_open_end else "")
                if is_open_end:
                    open_end_count += 1
                port.setData(0, block.block_id)
                port.setData(1, "output")
                port.setData(2, outcome)
                port.setParentItem(group)

            inputs = self.input_ports(block)

            for slot_index, slot in enumerate(inputs):
                input_port = self.scene_model.addEllipse(
                    -6,
                    35 + (slot_index - (len(inputs) - 1) / 2) * 20,
                    12,
                    12,
                    QPen(Qt.GlobalColor.white),
                )
                if slot == "break":
                    input_port.setBrush(Qt.GlobalColor.magenta)
                    input_port.setToolTip(f"{slot} ({t('function.port_break')})")
                elif slot == "coordinates":
                    input_port.setBrush(Qt.GlobalColor.darkYellow)
                    input_port.setToolTip(slot)
                else:
                    input_port.setBrush(Qt.GlobalColor.darkCyan)
                    input_port.setToolTip(slot)
                input_port.setData(0, block.block_id)
                input_port.setData(1, "input")
                input_port.setData(2, slot)
                input_port.setParentItem(group)

        positions = {
            block.block_id: self._positions[block.block_id]
            for block in blocks
        }

        for connection in connections:

            source_position = self._positions.get(
                connection.source_block_id
            )
            target_position = self._positions.get(
                connection.target_block_id
            )

            if not source_position or not target_position:
                continue

            source_block = next(
                (
                    block
                    for block in blocks
                    if block.block_id == connection.source_block_id
                ),
                None,
            )
            outcomes = list(
                BLOCK_CATALOG.get(
                    source_block.block_type,
                    {},
                ).get("outcomes", [])
            ) if source_block else []

            if source_block and source_block.block_type == "splitter":
                output_count = max(2, min(4, int(source_block.params.get("outputs", 2))))
                outcomes = [f"out_{i+1}" for i in range(output_count)]
            elif source_block and source_block.block_type == "call_function":
                helper_id = source_block.params.get("function_id")
                if helper_id and current_function:
                    helper = self.function_service.load(current_function.game_id, helper_id)
                    if helper and helper.interface.outcomes:
                        outcomes = list(helper.interface.outcomes)

            port_index = max(
                0,
                outcomes.index(connection.source_outcome)
                if connection.source_outcome in outcomes
                else 0,
            )
            color = self.outcome_color(
                connection.source_outcome
            )

            source_port = QPoint(
                source_position.x() + 190,
                source_position.y() + 18 + port_index * 20,
            )

            target_block = next(
                (
                    b
                    for b in blocks
                    if b.block_id == connection.target_block_id
                ),
                None,
            )
            target_inputs = (
                self.input_ports(target_block)
                if target_block
                else ["input"]
            )
            target_slot_index = max(
                0,
                target_inputs.index(connection.target_input)
                if connection.target_input in target_inputs
                else 0,
            )
            target_port_y = (
                target_position.y()
                + 41
                + int((target_slot_index - (len(target_inputs) - 1) / 2) * 20)
            )
            terminal = target_block and target_block.block_type in (BLOCK_SUCCESS, BLOCK_FAILURE)
            wire_end = (
                QPoint(source_port.x() + 36, source_port.y())
                if terminal else QPoint(target_position.x(), target_port_y)
            )
            path = QPainterPath(source_port)
            if terminal:
                path.lineTo(wire_end)
            else:
                path = self._wire_path(source_port, wire_end)

            line = self.scene_model.addPath(
                path,
                QPen(color, self.WIRE_WIDTH),
            )
            line.setData(0, connection.source_block_id)
            line.setData(1, "wire")
            line.setData(2, connection.source_outcome)
            line.setData(3, source_port)
            terminal_label = (
                target_block.name or t(f"function.block.{target_block.block_type}")
                if terminal else ""
            )
            line.setToolTip(
                f"{terminal_label}: {target_block.name or target_block.block_id}\n{t('function.wire_hint')}"
                if terminal else t("function.wire_hint")
            )
            line.setZValue(1)
            label = self.scene_model.addText(
                terminal_label if terminal else connection.source_outcome
            )
            label.setDefaultTextColor(color)
            label.setAcceptedMouseButtons(Qt.NoButton)
            label.setToolTip(line.toolTip())
            label.setPos(
                wire_end.x() + 3 if terminal else (source_position.x() + target_position.x()) / 2,
                source_port.y() - 14 if terminal else (source_position.y() + target_position.y()) / 2 - 14,
            )

        self.open_end_count = open_end_count
        self.open_ends_changed.emit(open_end_count)

        if connections:
            self._fit_scene_to_content()
            self._create_block_highlights(blocks)
            return

        for block in blocks:

            source = positions.get(block.block_id)
            if not source:
                continue

            for outcome, targets in block.branches.items():

                for target in targets:
                    target_id = getattr(
                        target,
                        "block_id",
                        target if isinstance(target, str) else "",
                    )
                    target_position = positions.get(target_id)

                    if not target_position:
                        continue

                    color = self.outcome_color(
                        outcome
                    )

                    terminal_block = next((b for b in blocks if b.block_id == target_id), None)
                    terminal = terminal_block and terminal_block.block_type in (BLOCK_SUCCESS, BLOCK_FAILURE)
                    self.scene_model.addLine(
                        source.x() + 190,
                        source.y() + 41,
                        source.x() + 226 if terminal else target_position.x(),
                        source.y() + 41 if terminal else target_position.y() + 41,
                        QPen(color, 2),
                    )
                    if terminal:
                        label = self.scene_model.addText(
                            terminal_block.name or t(f"function.block.{terminal_block.block_type}")
                        )
                        label.setDefaultTextColor(color)
                        label.setAcceptedMouseButtons(Qt.NoButton)
                        label.setPos(source.x() + 229, source.y() + 27)
                        label.setToolTip(terminal_block.name or terminal_block.block_id)

        self._fit_scene_to_content()
        self._create_block_highlights(blocks)
        if preserved_selection:
            self.scene_model.clearSelection()
            for item in self.scene_model.items():
                if item.data(0) in preserved_selection and item.childItems():
                    item.setSelected(True)
            self._refresh_block_highlights(preserved_selection)
            if not self._selected_block_id:
                self._selected_block_id = next(iter(preserved_selection))
        else:
            self._refresh_block_highlights()

    def _create_block_highlights(self, blocks):
        for block in blocks:
            position = self._positions.get(block.block_id)
            if position is None:
                continue
            highlight = self.scene_model.addRect(
                position.x(),
                position.y(),
                190,
                82,
                QPen(QColor("#FDC601"), 4),
            )
            highlight.setBrush(Qt.NoBrush)
            highlight.setAcceptedMouseButtons(Qt.NoButton)
            highlight.setData(0, block.block_id)
            highlight.setZValue(20)
            highlight.setVisible(block.block_id == self._selected_block_id)
            self._block_highlights[block.block_id] = highlight

    def mousePressEvent(self, event):
        item = self._press_item_at(event.position().toPoint())

        if (
            event.button() == Qt.RightButton
            and item
            and item.data(1) == "output"
        ):
            self._show_output_port_menu(
                item.data(0),
                item.data(2),
                event.globalPosition().toPoint(),
            )
            event.accept()
            return

        # picking a wire detaches it on drop outside or rewires on drop on another port
        if item and item.data(1) == "wire":
            source_id = item.data(0)
            outcome = item.data(2)
            source_port = QPoint(item.data(3))

            self._pending_output = (source_id, outcome)
            self._drag_reconnect = (source_id, outcome)
            self._drag_source = None
            self._drag_start = source_port
            self._start_drag_wire()
            event.accept()
            return

        if item and item.data(1) == "output":
            source_id = item.data(0)
            outcome = item.data(2)

            self._pending_output = (
                source_id,
                outcome,
            )
            self._drag_reconnect = None
            self._drag_source = item
            self._drag_start = item.scenePos() + QPoint(6, 6)
            self._start_drag_wire()
            event.accept()
            return

        if item and item.data(1) == "input":
            if self._pending_output:
                pending = self._pending_output
                self._cancel_drag_wire()
                self.connection_requested.emit(
                    pending[0],
                    pending[1],
                    item.data(0),
                    item.data(2) or "input",
                )
                event.accept()
                return

        while item and not item.data(0):
            item = item.parentItem()
        # let Qt process the click first: Ctrl+click toggles the scene
        # selection, and selectionChanged notifies the inspector afterwards
        self._press_scene_pos = self.mapToScene(event.position().toPoint())
        self._press_block_id = item.data(0) if item else ""
        super().mousePressEvent(event)

    def keyPressEvent(self, event):

        if event.key() == Qt.Key_Delete:
            items = self.scene_model.selectedItems()
            item = items[0] if items else None
            while item and not item.data(0):
                item = item.parentItem()
            if item and item.data(0):
                self.block_deleted.emit(item.data(0))
                return

        if (
            event.key() == Qt.Key_D
            and event.modifiers() & Qt.ControlModifier
        ):
            self.blocks_clone_requested.emit()
            return

        super().keyPressEvent(event)

    def mouseReleaseEvent(self, event):

        super().mouseReleaseEvent(event)

        if self._drag_wire is not None:
            target, target_slot = self._input_at(
                self.mapToScene(event.position().toPoint())
            )

            pending_output = self._pending_output
            reconnect = self._drag_reconnect

            self._cancel_drag_wire()

            if target and pending_output:
                self.connection_requested.emit(
                    pending_output[0],
                    pending_output[1],
                    target,
                    target_slot or "input",
                )
            elif reconnect:
                self.connection_removed.emit(
                    reconnect[0],
                    reconnect[1],
                )

        # only emit block_moved when the block was actually dragged
        release_scene_pos = self.mapToScene(event.position().toPoint())
        dragged = (
            self._press_scene_pos is not None
            and (release_scene_pos - self._press_scene_pos).manhattanLength() > 4
        )
        self._press_scene_pos = None
        if not dragged:
            return

        for item in self.scene_model.selectedItems():
            while item and not item.data(0):
                item = item.parentItem()
            if not item:
                continue
            block_id = item.data(0)
            if block_id:
                self.block_moved.emit(
                    block_id,
                    item.scenePos().toPoint(),
                )
                break

    def mouseMoveEvent(self, event):

        if self._drag_wire is not None:
            try:
                self._drag_wire.setPath(
                    self._wire_path(
                        self._drag_start,
                        self.mapToScene(event.position().toPoint()),
                    )
                )
            except RuntimeError:
                self._drag_wire = None
                self._pending_output = None

        super().mouseMoveEvent(event)

    def _start_drag_wire(self):

        self._drag_wire = self.scene_model.addPath(
            self._wire_path(
                self._drag_start,
                self._drag_start,
            ),
            QPen(Qt.GlobalColor.cyan, 2, Qt.DashLine),
        )
        self._drag_wire.setZValue(100)

    def _cancel_drag_wire(self):

        if self._drag_wire is not None:
            try:
                self.scene_model.removeItem(
                    self._drag_wire
                )
            except RuntimeError:
                pass

        self._drag_wire = None
        self._drag_source = None
        self._pending_output = None

    def _input_at(self, scene_position):

        for item in self.scene_model.items(
            scene_position
        ):
            if item.data(1) == "input":
                return item.data(0), item.data(2) or "input"

        return None, None

    def _press_item_at(self, view_position):
        """Ports win over block chrome, which wins over wires, regardless of paint order."""

        items = self.items(view_position)

        for item in items:
            if item.data(1) in ("input", "output"):
                return item

        for item in items:
            if item.data(0) and item.data(1) is None:
                return item

        for item in items:
            if item.data(1) == "wire":
                return item

        return items[0] if items else None

    def _wire_path(self, start, end):

        delta_x = end.x() - start.x()
        delta_y = end.y() - start.y()
        path = QPainterPath(start)

        if delta_x < 30:
            direction = -1 if delta_y < -40 else 1
            horizontal = max(90, abs(delta_x) * 0.15)
            vertical = max(
                140,
                abs(delta_x) * 0.15 + abs(delta_y) * 0.1,
            )
            path.cubicTo(
                start.x() + horizontal,
                start.y() + vertical * direction,
                end.x() - horizontal,
                end.y() + vertical * direction,
                end.x(),
                end.y(),
            )
        else:
            offset = max(60, delta_x * 0.45)
            path.cubicTo(
                start.x() + offset,
                start.y(),
                end.x() - offset,
                end.y(),
                end.x(),
                end.y(),
            )

        return path

    def _selection_changed(self):
        try:
            items = self.scene_model.selectedItems()
        except RuntimeError:
            return
        selected_ids = set()
        for item in items:
            current = item
            while current is not None and not current.data(0):
                current = current.parentItem()
            if current is not None and current.data(0):
                selected_ids.add(current.data(0))
        self._refresh_block_highlights(selected_ids)
        if selected_ids:
            self.block_selected.emit(next(iter(selected_ids)))

    def _refresh_block_highlights(self, selected_ids=None):
        if selected_ids is None:
            selected_ids = {
                item.data(0)
                for item in self.scene_model.selectedItems()
                if item.data(0)
            }
        selected_ids = set(selected_ids)
        if selected_ids:
            self._selected_block_id = next(iter(selected_ids))
        elif self._selected_block_id:
            selected_ids = {self._selected_block_id}
        # a programmatic selection (debug screen) wins over an empty scene
        if self._selected_block_id and not selected_ids:
            selected_ids = {self._selected_block_id}
        for block_id, rect in list(self._block_rects.items()):
            try:
                if block_id in selected_ids:
                    rect.setPen(QPen(QColor("#FDC601"), 4))
                else:
                    rect.setPen(QPen(Qt.GlobalColor.transparent, 1))
            except RuntimeError:
                self._block_rects.pop(block_id, None)
        for block_id, highlight in list(self._block_highlights.items()):
            try:
                highlight.setVisible(block_id in selected_ids)
            except RuntimeError:
                self._block_highlights.pop(block_id, None)

    def selected_block_ids(self):
        """All currently selected block ids, multi-selection aware."""

        try:
            items = self.scene_model.selectedItems()
        except RuntimeError:
            return []

        selected_ids = []
        for item in items:
            current = item
            while current is not None and not current.data(0):
                current = current.parentItem()
            if current is None:
                continue
            block_id = current.data(0)
            if block_id and block_id not in selected_ids:
                selected_ids.append(block_id)
        return selected_ids

    def select_blocks(self, block_ids):
        """Selects the given blocks in the scene, replacing the current selection."""

        wanted = {block_id for block_id in block_ids if block_id}
        if not wanted:
            return
        self.scene_model.clearSelection()
        for item in self.scene_model.items():
            if item.data(0) in wanted and item.childItems():
                item.setSelected(True)
        self._refresh_block_highlights(wanted)


class FunctionEditorView(QWidget):

    LIVE_IMAGE_WIDTH = 420

    def __init__(self, runtime_service=None, parent=None):
        super().__init__(parent)

        self.runtime_service = runtime_service
        self.game_service = GameService()
        self.app_config_service = AppConfigService()
        self.library_service = LibraryService()
        self.variable_service = VariableService()
        self.function_service = FunctionService(
            library_service=self.library_service,
            variable_service=self.variable_service,
        )
        self.scheduler_field_service = SchedulerFieldService(
            function_service=self.function_service,
            variable_service=self.variable_service,
        )
        self.screen_service = ScreenService()
        self.profile_service = ProfileService()
        self.supervisor = RuntimeSupervisor(
            profile_service=self.profile_service,
            function_service=self.function_service,
        )
        self.github_auth = GitHubAuthService()
        self._save_task = None
        self._save_callback = None
        self.logger = get_logger("function")
        self.current_function = None
        self.selected_block = None
        self._event_choice_pool = QThreadPool(self)
        self._event_choice_tasks = {}
        self._event_target_combo = None
        self._last_runtime_coordinates = {}
        self._last_runtime_matches = {}
        self._debug_values_by_function = {}

        root = QVBoxLayout(self)
        splitter = QSplitter(Qt.Horizontal)

        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.addWidget(QLabel(t("function.palette")))

        self.palette = BlockPalette()
        self._palette_settings = QSettings("BotStudio", "BotStudio")
        self.palette.setToolTip(t("function.palette_order_tooltip"))
        self._populate_block_palette()
        self.palette.order_changed.connect(self._save_block_palette_order)
        left_layout.addWidget(self.palette)
        self.reset_palette_order_button = QPushButton(t("function.reset_palette_order"))
        self.reset_palette_order_button.clicked.connect(self._reset_block_palette_order)
        left_layout.addWidget(self.reset_palette_order_button)

        self.btn_play = QPushButton(t("function.run"))
        self.btn_stop = QPushButton(t("function.stop"))
        left_layout.addWidget(self.btn_play)
        left_layout.addWidget(self.btn_stop)

        center = QWidget()
        center_layout = QVBoxLayout(center)
        toolbar = QHBoxLayout()
        self.function_name = QLineEdit()
        self.function_name.setPlaceholderText(t("function.name"))
        self.game_combo = SearchableComboBox()
        games = sorted(
            self.game_service.load(),
            key=lambda game: str(game.get("display_name", game.get("id", ""))).casefold(),
        )
        game_names = {
            str(game.get("id", "")): str(game.get("display_name", game.get("id", "")))
            for game in games
        }
        for game in games:
            self.game_combo.addItem(
                game.get("display_name", game.get("id", "")),
                game.get("id", ""),
            )

        self.profile_combo = SearchableComboBox()
        self.profile_combo.addItem("", None)
        # live capture/debug needs every profile of the game, not only enabled ones
        for profile in self.profile_service.load_all():
            self.profile_combo.addItem(
                f"[{game_names.get(str(profile.get('game', '')), profile.get('game', ''))}] "
                f"{profile.get('name', profile.get('id', ''))}",
                profile.get("id", ""),
            )
        self.kind_combo = SearchableComboBox()
        for label, value in sorted(
            (
                (t("function.kind_regular"), FUNCTION_KIND_REGULAR),
                (t("function.kind_helper"), FUNCTION_KIND_HELPER),
            ),
            key=lambda item: item[0].casefold(),
        ):
            self.kind_combo.addItem(label, value)
        self.function_combo = SearchableComboBox()
        self.btn_new = QPushButton(t("function.new"))
        self.btn_save = QPushButton(t("common.save"))
        self.btn_save_copy = QPushButton(t("common.save_copy_as"))
        toolbar.addWidget(self.game_combo)
        toolbar.addWidget(self.profile_combo)
        toolbar.addWidget(self.kind_combo)
        toolbar.addWidget(self.function_combo, stretch=2)
        toolbar.addWidget(self.btn_new)
        self.btn_open_ends = QPushButton(t("function.open_ends_button").format(count=0))
        self.btn_open_ends.setToolTip(t("function.open_ends_button_tooltip"))
        toolbar.addWidget(self.btn_open_ends)
        self.btn_create_event = QPushButton(t("function.create_event_button"))
        self.btn_create_event.setToolTip(t("function.create_event_tooltip"))
        toolbar.addWidget(self.btn_create_event)
        center_layout.addLayout(toolbar)

        self.usage_label = QLabel("")
        self.usage_label.setStyleSheet("color: #AAB4C4; padding: 2px 4px;")
        self.usage_label.setWordWrap(True)
        center_layout.addWidget(self.usage_label)

        self.profile_emulator_bar = ProfileEmulatorBar()
        center_layout.addWidget(self.profile_emulator_bar)
        self.profile_combo.currentIndexChanged.connect(
            self._sync_profile_emulator_bar
        )
        self._sync_profile_emulator_bar()

        self.canvas = FunctionCanvas()
        self.canvas.function_service = self.function_service
        self.canvas.current_function = self.current_function
        center_layout.addWidget(self.canvas, stretch=3)

        self.block_label = QLabel("-")
        self.param_panel = QWidget()
        self.param_layout = QFormLayout(
            self.param_panel
        )
        self.block_error = QLabel("")
        self.block_error.setWordWrap(True)
        self.block_error.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self.block_status_scroll = QScrollArea()
        self.block_status_scroll.setWidgetResizable(True)
        self.block_status_scroll.setFixedHeight(96)
        self.block_status_scroll.setFrameShape(QFrame.NoFrame)
        self.block_status_scroll.setWidget(self.block_error)
        self.block_type = QLabel("-")
        self.btn_delete_block = QPushButton(t("function.delete_block"))
        self.btn_clone_block = QPushButton(t("function.clone_block"))
        self.btn_clone_block.setToolTip(t("function.clone_block_tooltip"))
        self.btn_debug_block = QPushButton(t("function.debug_block"))
        self.btn_debug_chain = QPushButton(t("function.debug_chain"))
        self.btn_debug_values = QPushButton(t("function.debug_values"))
        self.library_preview = QLabel(
            t("function.no_library_preview")
        )

        inspector = QWidget()
        inspector_layout = QFormLayout(inspector)
        inspector_layout.addRow(t("function.selected_block"), self.block_label)
        inspector_layout.addRow(t("function.block_type"), self.block_type)
        self.block_id_value = QLineEdit()
        self.block_id_value.setReadOnly(True)
        self.block_id_value.setStyleSheet("color: #AAB4C4;")
        inspector_layout.addRow(t("function.block_id"), self.block_id_value)
        inspector_layout.addRow(t("function.parameters"), self.param_panel)

        inspector_scroll = QScrollArea()
        inspector_scroll.setWidget(inspector)
        inspector_scroll.setWidgetResizable(True)

        details_panel = QWidget()
        details_layout = QVBoxLayout(details_panel)
        details_layout.setContentsMargins(0, 0, 0, 0)
        details_layout.addWidget(QLabel(t("function.details")))
        self.details = DetailsEdit()
        self.details.setPlaceholderText(t("function.description"))
        self.details.setReadOnly(False)
        self.details.setMaximumHeight(90)
        self.btn_edit_details = QPushButton(t("function.edit_details"))
        details_layout.addWidget(self.details)
        self.package_origin_label = QLabel()
        self.package_origin_label.setTextFormat(Qt.PlainText)
        self.package_origin_label.setWordWrap(True)
        self.package_origin_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        details_layout.addWidget(self.package_origin_label)
        details_layout.addStretch()

        bottom = QWidget()
        bottom_layout = QHBoxLayout(bottom)
        bottom_layout.setContentsMargins(0, 0, 0, 0)
        bottom_layout.addWidget(inspector_scroll)
        status_font_size = self.block_error.font().pointSizeF()
        if status_font_size > 0:
            bottom.setStyleSheet(f"font-size: {status_font_size}pt;")
        center_layout.addWidget(bottom, stretch=2)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.addWidget(self.function_name)
        right_layout.addWidget(QLabel(t("function.block_error")))
        right_layout.addWidget(self.block_status_scroll)
        right_layout.addWidget(self.btn_delete_block)
        right_layout.addWidget(self.btn_clone_block)
        debug_layout = QHBoxLayout()
        debug_layout.addWidget(self.btn_debug_values)
        debug_layout.addWidget(self.btn_debug_block)
        debug_layout.addWidget(self.btn_debug_chain)
        right_layout.addLayout(debug_layout)
        right_layout.addWidget(self.btn_edit_details)
        right_layout.addWidget(details_panel)
        right_layout.addWidget(self.btn_save)
        right_layout.addWidget(self.btn_save_copy)
        self.btn_delete_function = QPushButton("Delete function")
        right_layout.addWidget(self.btn_delete_function)
        separator = QFrame()
        separator.setFrameShape(QFrame.HLine)
        separator.setFrameShadow(QFrame.Sunken)
        right_layout.addWidget(separator)
        self.btn_capture = QPushButton(t("function.capture_screen"))
        self.live_image = ClickableImage(
            t("function.no_live_image")
        )
        self.live_scroll = QScrollArea()
        self.live_scroll.setWidget(self.live_image)
        self.live_scroll.setWidgetResizable(False)
        self.live_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.live_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        right_layout.addWidget(self.btn_capture)
        right_layout.addWidget(self.live_scroll, stretch=1)

        splitter.addWidget(left)
        splitter.addWidget(center)
        splitter.addWidget(right)
        splitter.setStretchFactor(1, 1)
        root.addWidget(splitter)

        self.canvas.block_dropped.connect(self.add_block)
        self.canvas.block_selected.connect(self.select_block)
        self.canvas.block_deleted.connect(self.delete_block)
        self.canvas.block_moved.connect(self.move_block)
        self.canvas.connection_requested.connect(
            self.request_connection
        )
        self.canvas.connection_removed.connect(
            self.remove_connection
        )
        self.canvas.end_terminal_requested.connect(
            self.wire_to_end_terminal
        )
        self.canvas.open_ends_changed.connect(self._update_open_ends_button)
        self.btn_open_ends.clicked.connect(self.show_open_ends_overview)
        self.btn_create_event.clicked.connect(self.create_event_for_function)
        self.btn_new.clicked.connect(self.new_function)
        self.btn_save.clicked.connect(lambda: self.save_function())
        self.btn_save_copy.clicked.connect(self.save_copy_as)
        self.btn_delete_function.clicked.connect(self.delete_current_function)
        self.btn_debug_block.clicked.connect(self.debug_block)
        self.btn_debug_chain.clicked.connect(self.debug_chain)
        self.btn_debug_values.clicked.connect(self.configure_debug_values)
        self.btn_play.clicked.connect(self.debug_chain)
        self.btn_stop.clicked.connect(self.stop_debug)
        self.btn_delete_block.clicked.connect(self.delete_selected_block)
        self.btn_clone_block.clicked.connect(self.clone_selected_blocks)
        self.canvas.blocks_clone_requested.connect(
            self.clone_selected_blocks
        )
        self.btn_capture.clicked.connect(self.capture_live_screen)
        self.btn_edit_details.clicked.connect(self.edit_details)
        self.details.edit_requested.connect(self.edit_details)
        self.function_name.textChanged.connect(self._update_required_field_styles)
        self.details.textChanged.connect(self._update_required_field_styles)
        self.live_image.point_clicked.connect(
            self.apply_live_coordinate
        )
        self._region_selection_block = None
        self.live_image.region_selected.connect(
            self._apply_search_region_selection
        )
        self.game_combo.currentIndexChanged.connect(
            self.refresh_function_list
        )
        self.function_combo.activated.connect(
            self.open_selected_function
        )

        self.event_timer = QTimer(self)
        self.event_timer.setInterval(250)
        self.event_timer.timeout.connect(self.poll_worker_events)
        self.event_timer.start()

        self.new_function()
        self.refresh_function_list()
        self._update_required_field_styles()

    def refresh_profiles(self):
        selected_profile_id = self.profile_combo.currentData()
        game_names = {
            str(game.get("id", "")): str(
                game.get("display_name", game.get("id", ""))
            )
            for game in self.game_service.load()
        }

        self.profile_combo.blockSignals(True)
        self.profile_combo.clear()
        self.profile_combo.addItem("", None)
        for profile in self.profile_service.load_all():
            game_id = str(profile.get("game", ""))
            self.profile_combo.addItem(
                f"[{game_names.get(game_id, game_id)}] "
                f"{profile.get('name', profile.get('id', ''))}",
                profile.get("id", ""),
            )
        index = self.profile_combo.findData(selected_profile_id)
        self.profile_combo.setCurrentIndex(index if index >= 0 else 0)
        self.profile_combo.blockSignals(False)
        self._sync_profile_emulator_bar()

    def refresh_games(self):
        selected_game_id = self.game_combo.currentData()
        games = sorted(
            self.game_service.load(),
            key=lambda game: str(game.get("display_name", game.get("id", ""))).casefold(),
        )
        self.game_combo.blockSignals(True)
        self.game_combo.clear()
        for game in games:
            self.game_combo.addItem(
                game.get("display_name", game.get("id", "")),
                game.get("id", ""),
            )
        index = self.game_combo.findData(selected_game_id)
        self.game_combo.setCurrentIndex(index if index >= 0 else (0 if games else -1))
        self.game_combo.blockSignals(False)
        self.refresh_profiles()
        self.refresh_function_list()

    def _populate_block_palette(self):
        block_types = list(BLOCK_CATALOG)
        stored_order = self._palette_settings.value("function/block_palette_order", [])
        ordered_types = ordered_block_types(block_types, stored_order)

        self.palette.clear()
        for block_type in ordered_types:
            item = QListWidgetItem(t(f"function.block.{block_type}"))
            item.setToolTip(t(f"function.block_description.{block_type}"))
            item.setData(Qt.UserRole, block_type)
            self.palette.addItem(item)

    def _save_block_palette_order(self):
        order = [
            self.palette.item(index).data(Qt.UserRole)
            for index in range(self.palette.count())
        ]
        self._palette_settings.setValue("function/block_palette_order", order)

    def _reset_block_palette_order(self):
        self._palette_settings.remove("function/block_palette_order")
        self._populate_block_palette()

    def refresh_library_choices(self):
        if self.selected_block and hasattr(self, "library_combo"):
            self._reset_library_combo(self.selected_block)
            self.refresh_library_preview()

    def refresh_variable_choices(self):
        if self.selected_block:
            self._render_parameter_controls(self.selected_block)

    def refresh_function_list(self):

        game_id = self.game_combo.currentData() or ""

        self.function_combo.blockSignals(True)
        self.function_combo.clear()
        self.function_combo.addItem(
            t("function.select_function"),
            "",
        )

        functions = self.function_service.list_functions(game_id)
        helpers = sorted(
            (function for function in functions if function.requires_expert_mode()),
            key=lambda function: (function.name or function.function_id).casefold(),
        )
        regulars = sorted(
            (function for function in functions if not function.requires_expert_mode()),
            key=lambda function: (function.name or function.function_id).casefold(),
        )

        def add_group(title, entries):
            if not entries:
                return
            self.function_combo.addItem(title, None)
            header = self.function_combo.model().item(self.function_combo.count() - 1)
            if header is not None:
                header.setEnabled(False)
            for function in entries:
                label = function.name or function.function_id[:8]
                self.function_combo.addItem(label, function.function_id)

        add_group("Hilfsfunktionen", helpers)
        add_group("Normale Funktionen", regulars)

        current_id = (
            self.current_function.function_id
            if self.current_function
            else ""
        )

        index = self.function_combo.findData(current_id)
        self.function_combo.setCurrentIndex(
            index if index >= 0 else 0
        )
        self.function_combo.blockSignals(False)

    def open_function(self, game_id, function_id):
        """Open a function from another screen (e.g. Dependencies)."""

        if game_id and self.game_combo.currentData() != game_id:
            game_index = self.game_combo.findData(game_id)
            if game_index < 0:
                return False
            self.game_combo.setCurrentIndex(game_index)
        self.refresh_function_list()
        index = self.function_combo.findData(function_id)
        if index < 0:
            return False
        self.function_combo.setCurrentIndex(index)
        self.open_selected_function()
        return bool(
            self.current_function
            and self.current_function.function_id == function_id
        )

    def open_selected_function(self):

        function_id = self.function_combo.currentData()

        if not function_id:
            self.new_function()
            return

        function = self.function_service.load(
            self.game_combo.currentData() or "",
            function_id,
        )

        if not function:
            self.logger.warning(
                "Function Open Failed",
                function_id=function_id,
                game=self.game_combo.currentData() or "",
            )
            self.block_error.setText(
                t("function.not_found")
            )
            return

        self._cancel_search_region_selection()

        self.logger.info(
            "Function Opened",
            function_id=function.function_id,
            name=function.name,
            game=function.game_id,
            kind=function.kind,
        )
        self.current_function = function
        self.canvas.current_function = function
        self._last_runtime_coordinates = {}
        self._last_runtime_matches = {}
        self.live_image.set_overlay_region(None)
        self.function_name.setText(function.name)
        self.details.setPlainText(function.description)
        self.selected_block = None

        kind_index = self.kind_combo.findData(function.kind)
        if kind_index >= 0:
            self.kind_combo.setCurrentIndex(kind_index)

        self.canvas.set_layout_positions(
            function.layout_positions
        )
        self.canvas.draw_blocks(
            function.blocks,
            function.connections,
        )
        self._apply_edit_permissions()
        self._refresh_details()
        self._refresh_usage_badge()

    def is_editable(self):

        if not self.current_function:
            return False

        return (
            not self.current_function.requires_expert_mode()
            or self.app_config_service.is_expert_mode()
            or self.app_config_service.is_helper_editing_allowed()
        )

    def _apply_edit_permissions(self):
        """Sealed helper functions may be opened and debugged, but never edited."""

        editable = self.is_editable()

        for widget in (
            self.palette,
            self.function_name,
            self.kind_combo,
            self.btn_save,
            self.btn_save_copy,
            self.btn_delete_block,
            self.btn_clone_block,
            self.btn_edit_details,
            self.param_panel,
        ):
            widget.setEnabled(editable)

        if not editable:
            self.block_error.setText(
                t("function.expert_mode_required")
            )

    def new_function(self):
        self._cancel_search_region_selection()
        # Neu ist immer eine normale Funktion, nicht die zuletzt geladene Art.
        regular_index = self.kind_combo.findData(FUNCTION_KIND_REGULAR)
        if regular_index >= 0:
            self.kind_combo.setCurrentIndex(regular_index)
        self.current_function = BotFunction.create(
            name="",
            game_id=self.game_combo.currentData() or "",
            author="",
            kind=FUNCTION_KIND_REGULAR,
        )
        self.canvas.current_function = self.current_function
        self._last_runtime_coordinates = {}
        self._last_runtime_matches = {}
        self.live_image.set_overlay_region(None)
        self.logger.info(
            "Function Draft Created",
            function_id=self.current_function.function_id,
            game=self.current_function.game_id,
            kind=self.current_function.kind,
        )
        self.function_name.clear()
        self.details.clear()
        self.selected_block = None
        self.canvas.set_layout_positions(
            self.current_function.layout_positions
        )
        self.canvas.draw_blocks([])
        self._apply_edit_permissions()
        self._refresh_details()
        self.usage_label.setText("")

    def create_event_for_function(self):
        """Create an event (plus optional task with the open function) for
        one or more profiles, so its event variable is usable right away."""

        game_id = self.game_combo.currentData() or ""
        if not game_id:
            QMessageBox.information(
                self, t("function.create_event"), t("scheduler.select_game_before_event")
            )
            return []
        profiles = []
        checked = []
        selected_profile = self.profile_combo.currentData() or ""
        for profile in self.profile_service.get_profiles_for_game(game_id):
            profile_id = profile.get("botstudio_profile_id") or profile.get("id")
            if not profile_id:
                continue
            profiles.append((profile.get("name") or profile_id, profile_id))
            if selected_profile and selected_profile in (
                profile.get("id"), profile.get("botstudio_profile_id")
            ):
                checked.append(profile_id)
        if not profiles:
            QMessageBox.information(
                self, t("function.create_event"), t("function.create_event_no_profiles")
            )
            return []
        function = self.current_function
        function_name = (
            function.name
            if function is not None and function.saved and function.is_schedulable()
            else ""
        )
        dialog = EventCreateDialog(profiles, checked, function_name, self)
        if dialog.exec() != QDialog.Accepted:
            return []
        values = dialog.values()
        template = Event(
            game_id=game_id,
            profile_id="",
            name=values["name"],
            priority=values["priority"],
            enabled=True,
            status="idle",
            next_due_at=None,
            schedule_mode=values["schedule_mode"],
            interval_seconds=values["interval_seconds"],
        )
        service = (
            self.runtime_service.scheduler_service
            if self.runtime_service is not None else SchedulerService()
        )
        function_ids = [function.function_id] if values["add_function"] else []
        try:
            events = service.create_event_for_profiles(
                template,
                values["profile_ids"],
                task_name=values["task_name"],
                function_ids=function_ids,
                variable_service=self.variable_service,
            )
        except Exception as error:
            self.logger.error("Event Create Failed", game=game_id, error=str(error))
            QMessageBox.critical(self, t("function.create_event"), str(error))
            return []
        self.logger.info(
            "Event Created From Editor",
            game=game_id,
            name=template.name,
            profiles=len(events),
            with_task=bool(function_ids),
        )
        self.refresh_variable_choices()
        if function_ids:
            self._refresh_usage_badge()
        QMessageBox.information(
            self,
            t("function.create_event"),
            t("scheduler.event_created_for_profiles").format(
                name=template.name, count=len(events)
            ),
        )
        return events

    def _refresh_usage_badge(self):
        """Show where the open function is deployed (tasks and callers)."""

        if not self.current_function or not self.current_function.saved:
            self.usage_label.setText("")
            return

        from services.dependency_service import DependencyService

        game_id = self.game_combo.currentData() or ""
        inventory = DependencyService(
            function_service=self.function_service,
            variable_service=self.variable_service,
        ).build_inventory(game_id)
        users = inventory["functions"].get(
            self.current_function.function_id,
            [],
        )
        if not users:
            self.usage_label.setText(t("function.usage_none"))
            return

        tasks = [user["name"] for user in users if user["kind"] == "task"]
        callers = [user["name"] for user in users if user["kind"] == "function"]
        parts = []
        if tasks:
            parts.append(
                t("function.usage_tasks").format(
                    count=len(tasks),
                    names=", ".join(tasks[:5]) + (" …" if len(tasks) > 5 else ""),
                )
            )
        if callers:
            parts.append(
                t("function.usage_callers").format(
                    count=len(callers),
                    names=", ".join(callers[:5]) + (" …" if len(callers) > 5 else ""),
                )
            )
        self.usage_label.setText(" · ".join(parts))

    def _update_open_ends_button(self, count):
        self.btn_open_ends.setText(
            t("function.open_ends_button").format(count=count)
        )
        self.btn_open_ends.setStyleSheet(
            "color: #FF8C00; font-weight: bold;" if count else ""
        )

    def wire_to_end_terminal(self, source_id, outcome, terminal_type, force_new=False):
        """Wire an output to an End block: reuse the nearest existing one of
        that type, or place a new one next to the output (editor only)."""

        if not self.is_editable() or not self.current_function:
            return
        if terminal_type not in (BLOCK_SUCCESS, BLOCK_FAILURE):
            return
        source_position = self.canvas._positions.get(source_id)
        if source_position is None:
            return
        source_block = next(
            (b for b in self.current_function.blocks if b.block_id == source_id),
            None,
        )
        if source_block is None:
            return
        if not force_new:
            existing = self._nearest_terminal(terminal_type, source_position)
            if existing is not None:
                self.request_connection(source_id, outcome, existing.block_id, "input")
                return
        outcomes = block_outcomes(
            source_block,
            self.current_function.game_id,
            self.function_service,
        )
        port_index = outcomes.index(outcome) if outcome in outcomes else 0
        position = QPoint(
            source_position.x() + 240,
            source_position.y() + port_index * 40,
        )
        block_count = len(self.current_function.blocks)
        self.add_block(terminal_type, position)
        if len(self.current_function.blocks) <= block_count:
            return
        terminal = self.current_function.blocks[-1]
        self.request_connection(source_id, outcome, terminal.block_id, "input")

    def _nearest_terminal(self, terminal_type, source_position):
        best, best_distance = None, None
        for block in self.current_function.blocks:
            if block.block_type != terminal_type:
                continue
            position = self.canvas._positions.get(block.block_id)
            if position is None:
                continue
            distance = (
                (position.x() - source_position.x()) ** 2
                + (position.y() - source_position.y()) ** 2
            )
            if best_distance is None or distance < best_distance:
                best, best_distance = block, distance
        return best

    def show_open_ends_overview(self):
        game_id = self.game_combo.currentData() or ""
        rows = audit_game(self.function_service, game_id) if game_id else []
        dialog = OpenEndsDialog(rows, self)
        if dialog.exec() == QDialog.Accepted and dialog.selected_function_id:
            self.open_function(game_id, dialog.selected_function_id)

    def add_block(self, block_type, position=None):
        definition = BLOCK_CATALOG.get(block_type)
        if not definition or not self.is_editable():
            return

        block = FunctionBlock.create(
            block_type=block_type,
            name=t(
                f"function.block.{block_type}"
            ),
            params=dict(definition.get("params", {})),
        )
        self.current_function.blocks.append(block)

        if position is not None:
            self.current_function.layout_positions[block.block_id] = {
                "x": position.x(),
                "y": position.y(),
            }
            self.canvas.update_position(
                block.block_id,
                position,
            )

        try:
            self.canvas.set_layout_positions(
                self.current_function.layout_positions
            )
            self.canvas.draw_blocks(
                self.current_function.blocks,
                self.current_function.connections,
            )
            self.select_block(block.block_id)
        except Exception as error:
            self.current_function.blocks.remove(block)
            self.current_function.layout_positions.pop(block.block_id, None)
            self.canvas._positions.pop(block.block_id, None)
            self.logger.error(
                "Function Block Insert Failed",
                function_id=self.current_function.function_id,
                block_type=block_type,
                error=repr(error),
                next_action="keep existing function unchanged",
            )
            try:
                self.canvas.draw_blocks(
                    self.current_function.blocks,
                    self.current_function.connections,
                )
            except Exception as restore_error:
                self.logger.error(
                    "Function Canvas Restore Failed",
                    function_id=self.current_function.function_id,
                    error=repr(restore_error),
                    next_action="keep function data in memory for next refresh",
                )
            self.block_error.setText(t("function.editor_error"))

    def select_block(self, block_id):
        self._cancel_search_region_selection()
        self.canvas._selected_block_id = block_id or ""
        self.selected_block = next(
            (block for block in self.current_function.walk_blocks()
             if block.block_id == block_id),
            None,
        )
        scene_selected = set(self.canvas.selected_block_ids())
        if block_id and block_id not in scene_selected:
            # programmatic selection (add_block, clone, debug) replaces the
            # scene selection; only a ctrl+click path keeps the existing set
            self.canvas.scene_model.clearSelection()
            for item in self.canvas.scene_model.items():
                if item.data(0) == block_id and item.childItems():
                    item.setSelected(True)
                    break
        self._refresh_details()
        self._refresh_live_ocr_overlay()

    def move_block(self, block_id, position):

        if self.current_function and self.is_editable():
            self.current_function.layout_positions[block_id] = {
                "x": position.x(),
                "y": position.y(),
            }
            self.canvas.update_position(
                block_id,
                position,
            )
            QTimer.singleShot(
                0,
                lambda: self.canvas.draw_blocks(
                    self.current_function.blocks,
                    self.current_function.connections,
                ),
            )

    def delete_block(self, block_id):

        if not self.is_editable() or not self.current_function:
            return

        deleted_block = next(
            (b for b in self.current_function.blocks if b.block_id == block_id),
            None,
        )
        self.logger.info(
            "Block Deleted",
            function_id=self.current_function.function_id,
            block_id=block_id,
            block_type=deleted_block.block_type if deleted_block else "",
        )

        for connection in list(self.current_function.connections):
            if (
                connection.source_block_id == block_id
                or connection.target_block_id == block_id
            ):
                self._unbind_gate_operand(connection)
                self._unbind_repeat_break(connection)

        self.current_function.blocks = [
            block
            for block in self.current_function.blocks
            if block.block_id != block_id
        ]
        self.current_function.remove_block_connections(block_id)
        self.current_function.layout_positions.pop(block_id, None)

        if self.selected_block and self.selected_block.block_id == block_id:
            self.selected_block = None

        self.canvas.draw_blocks(
            self.current_function.blocks,
            self.current_function.connections,
        )
        self._refresh_details()

    def delete_selected_block(self):

        if self.selected_block:
            self.delete_block(
                self.selected_block.block_id
            )

    def clone_selected_blocks(self):
        """Duplicates the marked blocks, including settings, variables and
        internal wiring, as new blocks inside the same function."""

        if not self.is_editable() or not self.current_function:
            return

        selected_ids = set(self.canvas.selected_block_ids())
        if not selected_ids and self.selected_block:
            selected_ids = {self.selected_block.block_id}
        if not selected_ids:
            return

        sources = [
            block
            for block in self.current_function.blocks
            if block.block_id in selected_ids
        ]
        if not sources:
            return

        id_map = {}
        clones = []
        for block in sources:
            # no blanket deepcopy: branches reference the same shared block
            # instances (rebuilt from connections by normalize_graph)
            clone = FunctionBlock(
                block_id=str(uuid4()),
                block_type=block.block_type,
                name=block.name,
                params=deepcopy(block.params),
                inputs=deepcopy(block.inputs),
                output_as=block.output_as,
            )
            id_map[block.block_id] = clone.block_id
            clones.append(clone)

        # only edges fully inside the selection are duplicated
        cloned_connections = [
            FunctionConnection(
                source_block_id=id_map[connection.source_block_id],
                source_outcome=connection.source_outcome,
                target_block_id=id_map[connection.target_block_id],
                target_input=connection.target_input,
                negated=connection.negated,
            )
            for connection in self.current_function.connections
            if connection.source_block_id in id_map
            and connection.target_block_id in id_map
        ]

        self.current_function.blocks.extend(clones)
        self.current_function.connections.extend(cloned_connections)
        self.current_function.normalize_graph()

        for source, clone in zip(sources, clones):
            position = self.current_function.layout_positions.get(
                source.block_id,
            )
            if position is not None:
                base_x = int(position.get("x", 80))
                base_y = int(position.get("y", 100))
            else:
                canvas_position = self.canvas._positions.get(source.block_id)
                base_x = canvas_position.x() if canvas_position else 80
                base_y = canvas_position.y() if canvas_position else 100
            self.current_function.layout_positions[clone.block_id] = {
                "x": base_x + 40,
                "y": base_y + 40,
            }

        self.logger.info(
            "Blocks Cloned",
            function_id=self.current_function.function_id,
            count=len(clones),
            source_block_ids=[block.block_id for block in sources],
            new_block_ids=[clone.block_id for clone in clones],
        )

        self.canvas.set_layout_positions(
            self.current_function.layout_positions
        )
        self.canvas.draw_blocks(
            self.current_function.blocks,
            self.current_function.connections,
        )
        self.canvas.select_blocks(
            [clone.block_id for clone in clones]
        )
        self.select_block(clones[0].block_id)

    def remove_connection(
        self,
        source_id: str,
        outcome: str,
        target_id: str | None = None,
        target_input: str | None = None,
    ):
        if not self.is_editable() or not self.current_function:
            return

        for connection in list(self.current_function.connections):
            if (
                connection.source_block_id == source_id
                and connection.source_outcome == outcome
                and (target_id is None or connection.target_block_id == target_id)
                and (target_input is None or connection.target_input == target_input)
            ):
                self._unbind_gate_operand(connection)
                self._unbind_repeat_break(connection)

        self.current_function.remove_connection(
            source_id,
            outcome,
            target_block_id=target_id,
            target_input=target_input,
        )

        self.logger.info(
            "Connection Removed",
            function_id=self.current_function.function_id,
            source_block_id=source_id,
            outcome=outcome,
            target_block_id=target_id,
            target_input=target_input,
        )

        self.canvas.draw_blocks(
            self.current_function.blocks,
            self.current_function.connections,
        )
        self._refresh_details()

    def _validate_connection(
        self,
        source_id: str,
        outcome: str,
        target_id: str,
        target_input: str,
    ) -> tuple[bool, str]:

        if source_id == target_id and target_input != "break":
            return False, t("function.error_self_connection")

        # Uncontrolled infinite cycle detection
        if target_input != "break":
            forward_edges = {}
            for conn in self.current_function.connections:
                if conn.target_input != "break":
                    if not (conn.source_block_id == source_id and conn.source_outcome == outcome):
                        forward_edges.setdefault(conn.source_block_id, set()).add(conn.target_block_id)

            forward_edges.setdefault(source_id, set()).add(target_id)

            visited = set()
            queue = [target_id]
            while queue:
                curr = queue.pop(0)
                if curr == source_id:
                    return False, t("function.error_cycle_detected")
                if curr not in visited:
                    visited.add(curr)
                    curr_block = next(
                        (b for b in self.current_function.blocks if b.block_id == curr),
                        None,
                    )
                    # A Repeat block controls its body iterations, so it breaks infinite recursion
                    if curr_block and curr_block.block_type == BLOCK_REPEAT:
                        continue
                    for nxt in forward_edges.get(curr, []):
                        if nxt not in visited:
                            queue.append(nxt)

        return True, ""

    def request_connection(
        self,
        source_id,
        outcome,
        target_id,
        target_input="input",
    ):

        if not self.is_editable() or not self.current_function:
            return

        source = next(
            (
                block
                for block in self.current_function.blocks
                if block.block_id == source_id
            ),
            None,
        )

        target = next(
            (
                block
                for block in self.current_function.blocks
                if block.block_id == target_id
            ),
            None,
        )

        if not source or not target:
            return

        # For Gate blocks: automatically distribute multiple incoming connections over operand slots
        if target.block_type == BLOCK_GATE:
            occupied_slots = {
                c.target_input
                for c in self.current_function.connections
                if c.target_block_id == target_id
                and not (c.source_block_id == source_id and c.source_outcome == outcome)
            }
            if target_input in occupied_slots or not target_input.startswith("operand_"):
                slot_index = 0
                while f"operand_{slot_index}" in occupied_slots:
                    slot_index += 1
                target_input = f"operand_{slot_index}"
                operands = list(target.params.get("operands", []))
                while len(operands) <= slot_index:
                    operands.append("")
                target.params["operands"] = operands

        valid, error_msg = self._validate_connection(
            source_id,
            outcome,
            target_id,
            target_input,
        )

        if not valid:
            self.logger.warning(
                "Connection Rejected",
                function_id=self.current_function.function_id,
                source_block_id=source_id,
                outcome=outcome,
                target_block_id=target_id,
                target_input=target_input,
                reason=error_msg,
            )
            self.block_error.setText(error_msg)
            self.canvas.draw_blocks(
                self.current_function.blocks,
                self.current_function.connections,
            )
            return

        for connection in list(self.current_function.connections):
            if (
                connection.source_block_id == source_id
                and connection.source_outcome == outcome
                and connection.target_block_id == target_id
                and connection.target_input == target_input
            ):
                self._unbind_gate_operand(connection)
                self._unbind_repeat_break(connection)

        self.current_function.connections = [
            connection
            for connection in self.current_function.connections
            if not (
                connection.source_block_id == source_id
                and connection.source_outcome == outcome
                and connection.target_block_id == target_id
                and connection.target_input == target_input
            )
        ]
        self.current_function.connections.append(
            FunctionConnection(
                source_block_id=source_id,
                source_outcome=outcome,
                target_block_id=target_id,
                target_input=target_input,
            )
        )

        self.current_function.normalize_graph()

        self._bind_gate_operand(
            source,
            target,
            target_input,
        )

        self._bind_repeat_break(
            source,
            target,
            target_input,
        )

        self.logger.info(
            "Connection Created",
            function_id=self.current_function.function_id,
            source_block_id=source_id,
            outcome=outcome,
            target_block_id=target_id,
            target_input=target_input,
        )
        self.block_error.setText(t("function.connection_created"))

        self._refresh_live_ocr_overlay()

        self.canvas.draw_blocks(
            self.current_function.blocks,
            self.current_function.connections,
        )
        self._refresh_details()

    def _bind_repeat_break(
        self,
        source,
        target,
        target_input,
    ):
        """A wire into repeat's break slot configures until_variable from the source output."""
        if target.block_type != BLOCK_REPEAT:
            return

        if target_input != "break":
            return

        if not source.output_as:
            source.output_as = (
                f"{source.block_type}_{source.block_id[:8]}"
            )

        target.params["until_variable"] = f"${source.output_as}"

    def _unbind_repeat_break(
        self,
        connection,
    ):
        if not self.current_function:
            return

        target = next(
            (
                block
                for block in self.current_function.blocks
                if block.block_id == connection.target_block_id
            ),
            None,
        )

        if not target or target.block_type != BLOCK_REPEAT:
            return

        if connection.target_input != "break":
            return

        source = next(
            (
                block
                for block in self.current_function.blocks
                if block.block_id == connection.source_block_id
            ),
            None,
        )

        if source and source.output_as:
            var_name = source.output_as.lstrip("$")
            target_var = str(target.params.get("until_variable", "")).lstrip("$")
            if target_var == var_name:
                target.params["until_variable"] = ""
        else:
            target.params["until_variable"] = ""

    def _bind_gate_operand(
        self,
        source,
        target,
        target_input,
    ):
        """A wire into a gate slot feeds the source result variable into that operand."""

        if target.block_type != BLOCK_GATE:
            return

        if not target_input.startswith("operand_"):
            return

        slot = int(
            target_input.split("_")[-1]
        )

        if not source.output_as:
            source.output_as = (
                f"{source.block_type}_{source.block_id[:8]}"
            )

        operands = list(
            target.params.get("operands", [])
        )

        while len(operands) <= slot:
            operands.append("")

        operands[slot] = f"${source.output_as}"
        target.params["operands"] = operands

    def _unbind_gate_operand(
        self,
        connection,
    ):
        if not self.current_function:
            return

        target = next(
            (
                block
                for block in self.current_function.blocks
                if block.block_id == connection.target_block_id
            ),
            None,
        )

        if not target or target.block_type != BLOCK_GATE:
            return

        if not connection.target_input.startswith("operand_"):
            return

        try:
            slot = int(connection.target_input.split("_")[-1])
            operands = list(target.params.get("operands", []))
            if 0 <= slot < len(operands):
                operands[slot] = ""
                target.params["operands"] = operands
        except (ValueError, IndexError):
            pass

    def apply_live_coordinate(
        self,
        x,
        y,
    ):

        coordinate_target_types = (
            BLOCK_FIND_AND_TAP_ELEMENT,
            BLOCK_TAP_ELEMENT,
            BLOCK_WAIT_FOR_ELEMENT_AND_TAP,
        )
        if self.selected_block and self.selected_block.block_type in coordinate_target_types:
            anchor = self._coordinate_preview_anchor(self.selected_block)
            if anchor:
                anchor_x = int(anchor.get("x", 0)) + int(anchor.get("width", 0)) // 2
                anchor_y = int(anchor.get("y", 0)) + int(anchor.get("height", 0)) // 2
                self.selected_block.params["offset_x"] = int(x) - anchor_x
                self.selected_block.params["offset_y"] = int(y) - anchor_y
                self._last_runtime_matches.pop(self.selected_block.block_id, None)
                self._refresh_details()

        if self.selected_block and self.selected_block.block_type == "tap_point":
            self.selected_block.params["x"] = x
            self.selected_block.params["y"] = y
            self._refresh_details()

        if self.selected_block and self.selected_block.block_type in (
            BLOCK_CLEAR_FIELD,
            BLOCK_TYPE_TEXT,
        ):
            self.selected_block.params["x"] = x
            self.selected_block.params["y"] = y
            self._refresh_details()

        if self.selected_block and self.selected_block.block_type in (
            "swipe",
            "swipe_search",
            BLOCK_SWIPE_SEARCH_TAP,
        ) and getattr(self, "_click_target_mode", "start") == "start":
            self.selected_block.params["start_x"] = x
            self.selected_block.params["start_y"] = y
            self._refresh_details()

        if self.selected_block and self.selected_block.block_type in (
            BLOCK_SWIPE_SEARCH_TAP,
            BLOCK_SEARCH_OCR_TAP,
        ) and getattr(self, "_click_target_mode", "") == "offset":
            anchor = self._tap_target_anchor(self.selected_block)
            if anchor:
                self.selected_block.params["offset_x"] = int(x) - anchor[0]
                self.selected_block.params["offset_y"] = int(y) - anchor[1]
                self._last_runtime_matches.pop(self.selected_block.block_id, None)
                self._refresh_details()

        self.block_error.setText(
            f"{t('function.coordinate_selected')}: {x}, {y}"
        )

    def _sync_profile_emulator_bar(self):
        profile_id = self.profile_combo.currentData()
        profile = self.profile_service.get_profile(profile_id) if profile_id else None
        self.profile_emulator_bar.set_profile(profile)
        if self.selected_block and self.selected_block.block_type in (
            BLOCK_SET_EVENT_DUE, BLOCK_RESCHEDULE_EVENT,
        ):
            self._refresh_details()

    def capture_live_screen(self):

        if self.live_image.region_selection_active:
            self._cancel_search_region_selection()

        profile_id = self.profile_combo.currentData()
        profile = self.profile_service.get_profile(profile_id)

        if not profile:
            self.block_error.setText(
                t("function.profile_required")
            )
            return

        path = self.screen_service.capture(profile)
        pixmap = QPixmap(str(path))
        if not pixmap.isNull():
            self.logger.info(
                "Live Screen Captured",
                profile=profile_id,
                path=str(path),
            )
            self.live_image.set_screenshot(
                pixmap,
                self.LIVE_IMAGE_WIDTH,
            )
            self._refresh_live_ocr_overlay()
        else:
            self.logger.error(
                "Live Screen Capture Failed",
                profile=profile_id,
                path=str(path),
            )
            self.live_image.clear_screenshot(str(path))

    def edit_details(self):

        dialog = QDialog(self)
        dialog.setWindowTitle(t("function.details"))
        dialog.resize(640, 420)

        layout = QVBoxLayout(dialog)
        editor = QTextEdit()
        editor.setPlaceholderText(t("function.description"))
        editor.setPlainText(self.details.toPlainText())
        layout.addWidget(editor)

        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel
        )
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)

        if dialog.exec() == QDialog.Accepted:
            self.details.setPlainText(
                editor.toPlainText()
            )

    def _update_required_field_styles(self):
        missing_style = "border: 1px solid #d9534f;"
        self.function_name.setStyleSheet(
            missing_style if not self.function_name.text().strip() else ""
        )
        self.details.setStyleSheet(
            missing_style if not self.details.toPlainText().strip() else ""
        )

    def _refresh_details(self):
        try:
            self._refresh_package_origin()
            block = self.selected_block
            if not block:
                self.block_label.setText("-")
                self.block_type.setText("-")
                self.block_id_value.setText("")
                self._clear_parameter_controls()
                return
            self.block_label.setText(block.name or block.block_type)
            self.block_type.setText(block.block_type)
            self.block_id_value.setText(block.block_id)
            self._render_parameter_controls(block)
            self._refresh_live_ocr_overlay()
        except Exception as ex:
            self.logger.error(
                "Function Editor Error",
                error=repr(ex),
                source="FunctionEditorView._refresh_details",
                next_action="reselect the block or restart the editor",
            )
            self.block_error.setText(
                t("function.editor_error")
            )

    def _refresh_package_origin(self):
        function = self.current_function
        origins = function.package_origins if function else []
        accounts = []
        if function is not None and function.creator_account is not None:
            accounts.append(t("function.creator_account").format(
                login=function.creator_account.login, account_id=function.creator_account.account_id,
            ))
        if function is not None and function.history and function.history[-1].account is not None:
            account = function.history[-1].account
            accounts.append(t("function.revision_account").format(
                login=account.login, account_id=account.account_id, revision=function.revision,
            ))
        self.package_origin_label.setVisible(bool(origins or accounts))
        if not origins and not accounts:
            self.package_origin_label.clear()
            return
        lines = accounts + ([t("function.account_attribution_notice")] if accounts else [])
        if origins:
            lines.append(t("function.package_origin_unverified"))
        lines.extend(
            t("function.package_origin_entry").format(
                publisher=origin.publisher_key,
                package=origin.package_id,
                version=origin.package_version,
                game=origin.game_id,
                function=origin.function_id,
                revision=origin.revision,
                sha256=origin.source_sha256,
            )
            for origin in origins
        )
        self.package_origin_label.setText("\n".join(lines))

    def _clear_parameter_controls(self):

        self._event_target_combo = None
        while self.param_layout.rowCount():
            try:
                self.param_layout.removeRow(0)
            except RuntimeError as ex:
                self.logger.error(
                    "Function Editor Error",
                    error=repr(ex),
                    source="FunctionEditorView._clear_parameter_controls",
                    next_action="rebuild the parameter panel",
                )

        # removeRow() destroys the underlying Qt widgets, so drop the stale
        # Python references too or later access raises a shiboken RuntimeError.
        for attr in (
            "library_combo",
            "library_preview",
            "learn_library_button",
            "search_region_label",
            "select_region_button",
            "clear_region_button",
            "ocr_query_edit",
            "ocr_variable_combo",
            "ocr_value_input",
            "parameter_value_inputs",
            "parameter_value_edits",
            "parameter_variable_combos",
        ):
            if hasattr(self, attr):
                delattr(self, attr)

    def _reset_library_combo(self, block):
        if not hasattr(self, "library_combo"):
            return

        entries = []
        for entry_file in self.library_service.list_entries():
            entry_id = entry_file.parent.name
            entry = self.library_service.load(entry_id) or {}
            label = entry.get("name") or entry_id
            samples = entry.get("accepted_samples") or []
            image_path = samples[0].get("crop") if samples else None
            image_path = image_path or entry.get("pattern", {}).get("reference_path")
            if not image_path:
                image_path = str(entry_file.parent / "pattern_reference.png")
            pixmap = load_pixmap(image_path, QSize(48, 48))
            entries.append((label, entry_id, pixmap))
        entries.sort(key=lambda item: item[0].casefold())

        # block signals: repopulating must not fire an unrelated selection change
        self.library_combo.blockSignals(True)
        self.library_combo.clear()
        self.library_combo.addItem(t("function.select_library"), "")
        for label, entry_id, pixmap in entries:
            icon = QIcon(pixmap) if not pixmap.isNull() else QIcon()
            self.library_combo.addItem(icon, label, entry_id)

        selected_id = block.params.get("library_entry_id", "")
        index = self.library_combo.findData(selected_id)
        if index >= 0:
            self.library_combo.setCurrentIndex(index)
        else:
            self.library_combo.setCurrentIndex(0)
        self.library_combo.blockSignals(False)

    def _open_learning_for_block(self, block, edit_existing=False):
        if not self.current_function:
            return

        self.logger.info(
            "Learning Opened From Function",
            function_id=self.current_function.function_id,
            block_id=block.block_id if block else "",
            block_type=block.block_type if block else "",
        )

        profile_id = self.profile_combo.currentData()
        profile = self.profile_service.get_profile(profile_id) if profile_id else None

        learning_view = LearningView()
        learning_view.setWindowTitle(t("function.learn_element_title"))
        learning_view.setWindowModality(Qt.ApplicationModal)

        if profile:
            profile_selected = learning_view.select_profile(profile_id)
        else:
            profile_selected = False

        if not profile_selected:
            learning_view.capture_screen()

        if block and block.name:
            learning_view.properties.label.setText(block.name)

        if edit_existing:
            learning_view.set_library_entry(
                block.params.get("library_entry_id", "")
            )

        def on_teach(entry):
            if not entry:
                learning_view.close()
                return

            self.logger.info(
                "Learning Result Applied To Block",
                function_id=self.current_function.function_id,
                block_id=block.block_id if block else "",
                library_entry_id=entry.entry_id,
            )
            if block:
                block.params["library_entry_id"] = entry.entry_id
            self._reset_library_combo(block)
            self.library_combo.setCurrentIndex(
                self.library_combo.findData(entry.entry_id)
            )
            learning_view.close()

        learning_view.teach_completed.connect(on_teach)
        learning_view.show()

    def _render_parameter_controls(self, block):

        self._clear_parameter_controls()

        if block.block_type in (
            BLOCK_SWIPE_SEARCH_TAP,
            BLOCK_SEARCH_OCR_TAP,
        ):
            self._add_click_target_mode_row(block)

        definition = BLOCK_CATALOG.get(
            block.block_type,
            {},
        )

        how_to = definition.get("how_to")
        if block.block_type in (
            BLOCK_CONFIGURE_FIELD,
            BLOCK_SWIPE_SEARCH_TAP,
            BLOCK_SEARCH_OCR_TAP,
            BLOCK_STOP_EVENT,
            BLOCK_SET_EVENT_DUE,
            BLOCK_RESCHEDULE_EVENT,
        ):
            how_to = t(f"block.{block.block_type}.how_to")
        if how_to:
            help_label = QLabel(how_to)
            help_label.setWordWrap(True)
            help_label.setStyleSheet("color: #AAB4C4; padding: 4px;")
            help_scroll = QScrollArea(self.param_panel)
            help_scroll.setObjectName("blockHowToScroll")
            help_scroll.setWidgetResizable(True)
            help_scroll.setFrameShape(QFrame.NoFrame)
            help_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            help_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
            help_scroll.setMaximumHeight(132)
            help_scroll.setWidget(help_label)
            self.param_layout.addRow(t("function.how_to"), help_scroll)

        for key, default in definition.get("params", {}).items():
            self._add_parameter_row(
                block,
                key,
                default,
            )

        if block.block_type != BLOCK_CONFIGURE_FIELD:
            self._add_output_variable_row(block)
        self._add_block_connection_rows(block)

    def _add_click_target_mode_row(self, block):
        """Choose what a live-image click writes: swipe start or tap offset."""

        combo = QComboBox()
        combo.addItem(t("function.click_target_start"), "start")
        combo.addItem(t("function.click_target_offset"), "offset")
        current = getattr(self, "_click_target_mode", "start")
        if block.block_type == BLOCK_SEARCH_OCR_TAP and current == "start":
            # search_ocr_tap has no swipe start; default to the tap target
            current = "offset"
        index = combo.findData(current)
        combo.setCurrentIndex(index if index >= 0 else 0)
        self._click_target_mode = combo.currentData()

        def on_change():
            self._click_target_mode = combo.currentData()

        combo.currentIndexChanged.connect(lambda _i: on_change())
        self.param_layout.addRow(
            t("function.click_target"),
            combo,
        )

    def _add_output_variable_row(self, block):

        combo = SearchableComboBox()
        combo.setEditable(True)
        combo.addItem("", "")

        for var in self._collect_available_variables():
            name = var.lstrip("$")
            combo.addItem(name, name)

        current_value = str(block.output_as or "")
        index = combo.findText(current_value)
        if index >= 0:
            combo.setCurrentIndex(index)
        else:
            combo.setCurrentText(current_value)

        def on_change(text):
            block.output_as = text.strip()
            self._warn_output_type_mismatch(block, combo)

        combo.currentTextChanged.connect(on_change)

        create_button = QPushButton(t("variables.create"))
        create_button.setEnabled(self.is_editable())

        def on_create():
            new_value = self._open_variable_dialog()
            if not new_value:
                return
            if combo.findText(new_value) < 0:
                combo.addItem(new_value, new_value)
            combo.setCurrentText(new_value)

        create_button.clicked.connect(on_create)

        row_widget = QWidget()
        row_layout = QHBoxLayout(row_widget)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.addWidget(combo, stretch=1)
        row_layout.addWidget(create_button)

        self.param_layout.addRow(
            t("function.output_variable"),
            row_widget,
        )
        self._warn_output_type_mismatch(block, combo)

    def _warn_output_type_mismatch(self, block, combo):
        """Highlight when a block result type cannot feed the chosen variable."""

        expected_by_block = {
            "compare": VALUE_TYPE_BOOLEAN,
            BLOCK_MATH: VALUE_TYPE_NUMBER,
            "condition": VALUE_TYPE_BOOLEAN,
            BLOCK_GATE: VALUE_TYPE_BOOLEAN,
        }
        expected = expected_by_block.get(block.block_type)
        if not expected or not block.output_as:
            combo.setStyleSheet("")
            combo.setToolTip("")
            return

        game_id = self.game_combo.currentData() or ""
        target_name = block.output_as.lstrip("$").strip()
        definition = next(
            (
                entry
                for entry in self.variable_service.list_globals(game_id)
                if entry.get("name") == target_name
            ),
            None,
        )
        actual = (definition or {}).get("value_type")
        if actual and actual != expected and actual != VALUE_TYPE_ANY:
            combo.setStyleSheet("border: 1px solid #FDC601;")
            combo.setToolTip(
                t("function.output_type_mismatch").format(
                    name=target_name,
                    expected=expected,
                    actual=actual,
                )
            )
        else:
            combo.setStyleSheet("")
            combo.setToolTip("")

    def _add_block_connection_rows(self, block):

        if not self.current_function:
            return

        outcomes = list(
            BLOCK_CATALOG.get(
                block.block_type,
                {},
            ).get("outcomes", [])
        )

        if block.block_type == "splitter":
            output_count = max(2, min(4, int(block.params.get("outputs", 2))))
            outcomes = [f"out_{i+1}" for i in range(output_count)]
        elif block.block_type == "call_function":
            helper_id = block.params.get("function_id")
            if helper_id and self.current_function:
                helper = self.function_service.load(self.current_function.game_id, helper_id)
                if helper and helper.interface.outcomes:
                    outcomes = list(helper.interface.outcomes)

        if outcomes:
            self.param_layout.addRow(
                QLabel(f"<b>{t('function.outgoing_connections')}</b>")
            )

            for outcome in outcomes:
                conns = [
                    c
                    for c in self.current_function.connections
                    if c.source_block_id == block.block_id
                    and c.source_outcome == outcome
                ]

                if conns:
                    for conn in conns:
                        row_widget = QWidget()
                        row_layout = QHBoxLayout(row_widget)
                        row_layout.setContentsMargins(0, 0, 0, 0)

                        target_block = next(
                            (
                                b
                                for b in self.current_function.blocks
                                if b.block_id == conn.target_block_id
                            ),
                            None,
                        )
                        target_name = (
                            (target_block.name or target_block.block_type)
                            if target_block
                            else conn.target_block_id[:8]
                        )
                        label_text = f"→ {target_name}"
                        if conn.target_input != "input":
                            label_text += f" ({conn.target_input})"

                        lbl = QLabel(label_text)
                        btn_disconnect = QPushButton(t("function.disconnect"))
                        btn_disconnect.setEnabled(self.is_editable())
                        btn_disconnect.clicked.connect(
                            lambda checked=False, out=outcome, tgt=conn.target_block_id, tin=conn.target_input: self.remove_connection(
                                block.block_id, out, target_id=tgt, target_input=tin
                            )
                        )
                        row_layout.addWidget(lbl, stretch=1)
                        row_layout.addWidget(btn_disconnect)

                        self.param_layout.addRow(
                            outcome,
                            row_widget,
                        )
                else:
                    row_widget = QWidget()
                    row_layout = QHBoxLayout(row_widget)
                    row_layout.setContentsMargins(0, 0, 0, 0)
                    lbl = QLabel(f"→ ({t('function.not_connected')})")
                    lbl.setStyleSheet("color: gray;")
                    row_layout.addWidget(lbl)

                    self.param_layout.addRow(
                        outcome,
                        row_widget,
                    )

        incoming = [
            c
            for c in self.current_function.connections
            if c.target_block_id == block.block_id
        ]

        if incoming:
            self.param_layout.addRow(
                QLabel(f"<b>{t('function.incoming_connections')}</b>")
            )

            for conn in incoming:
                source_block = next(
                    (
                        b
                        for b in self.current_function.blocks
                        if b.block_id == conn.source_block_id
                    ),
                    None,
                )
                source_name = (
                    (source_block.name or source_block.block_type)
                    if source_block
                    else conn.source_block_id[:8]
                )

                row_widget = QWidget()
                row_layout = QHBoxLayout(row_widget)
                row_layout.setContentsMargins(0, 0, 0, 0)

                label_text = f"{source_name} [{conn.source_outcome}] → {conn.target_input}"
                lbl = QLabel(label_text)
                btn_disconnect = QPushButton(t("function.disconnect"))
                btn_disconnect.setEnabled(self.is_editable())
                btn_disconnect.clicked.connect(
                    lambda checked=False, src=conn.source_block_id, out=conn.source_outcome, tgt=conn.target_block_id, tin=conn.target_input: self.remove_connection(
                        src, out, target_id=tgt, target_input=tin
                    )
                )
                row_layout.addWidget(lbl, stretch=1)
                row_layout.addWidget(btn_disconnect)

                self.param_layout.addRow(
                    row_widget,
                )

    def _collect_available_variables(self) -> list:

        variables = set()

        game_id = self.game_combo.currentData() or ""
        if game_id:
            # scheduler-populated conventions (DEC-087) live in the registry,
            # not on any single block, so seed them once per game
            self.variable_service.ensure_variable(
                game_id,
                GameService.MULTI_ACCOUNT_VARIABLE.split(":", 1)[1],
                t("variables.multi_account_description"),
            )
            self.variable_service.ensure_variable(
                game_id,
                GameService.MULTI_ACCOUNT_PROFILE_ID_VARIABLE.split(":", 1)[1],
                t("variables.multi_account_profile_id_description"),
            )
            for entry in self.variable_service.list_globals(game_id):
                variables.add(f"${entry['name']}")

        if not self.current_function:
            return sorted(variables, key=str.casefold)

        for block in self.current_function.walk_blocks():
            if block.output_as:
                var_name = block.output_as.lstrip("$").strip()
                if var_name:
                    variables.add(f"${var_name}")

            returns_info = BLOCK_CATALOG.get(
                block.block_type, {}
            ).get("returns", {})
            ret_name = returns_info.get("name")
            if ret_name:
                variables.add(f"${ret_name}")

        return sorted(variables, key=str.casefold)

    def _open_variable_dialog(self) -> str:

        dialog = VariableCreateDialog(
            self.game_combo.currentData() or "",
            self.variable_service,
            parent=self,
        )

        if dialog.exec() == QDialog.Accepted:
            return dialog.result_value

        return ""

    def _add_variable_choice_row(self, block, key, default):
        # The Variable block selects a variable name. Other rows accept either
        # literal values or references to existing variables.
        is_raw_scope_field = (
            block.block_type == BLOCK_VARIABLE and key == "variable"
        )

        if is_raw_scope_field:
            combo = SearchableComboBox()
            combo.setEnabled(self.is_editable())
            combo.setEditable(True)
            combo.addItem("", "")
            combo.lineEdit().setPlaceholderText(
                t("function.variable_name_hint")
            )
            for variable in self._collect_available_variables():
                name = variable.lstrip("$")
                combo.addItem(name, name)

            current_value = str(block.params.get(key, default) or "")
            index = combo.findText(current_value)
            if index >= 0:
                combo.setCurrentIndex(index)
            else:
                combo.setCurrentText(current_value)

            combo.currentTextChanged.connect(
                lambda text: block.params.__setitem__(key, text.strip())
            )

            create_button = QPushButton(t("variables.create"))
            create_button.setEnabled(self.is_editable())

            def on_create_raw_variable():
                new_value = self._open_variable_dialog()
                if not new_value:
                    return
                self.logger.info(
                    "Variable Created",
                    function_id=self.current_function.function_id if self.current_function else "",
                    block_type=block.block_type,
                    variable=new_value,
                )
                if combo.findText(new_value) < 0:
                    combo.addItem(new_value, new_value)
                combo.setCurrentText(new_value)

            create_button.clicked.connect(on_create_raw_variable)
            row_widget = QWidget()
            row_layout = QHBoxLayout(row_widget)
            row_layout.setContentsMargins(0, 0, 0, 0)
            row_layout.addWidget(combo, stretch=1)
            row_layout.addWidget(create_button)
            self.param_layout.addRow(self._parameter_label(key), row_widget)
            return

        value_input = self._make_value_input(
            block.params.get(key, default),
            lambda text: block.params.__setitem__(key, text),
        )
        self.param_layout.addRow(self._parameter_label(key), value_input)
        if not hasattr(self, "parameter_value_inputs"):
            self.parameter_value_inputs = {}
        self.parameter_value_inputs[(block.block_id, key)] = value_input
        if not hasattr(self, "parameter_value_edits"):
            self.parameter_value_edits = {}
        self.parameter_value_edits[(block.block_id, key)] = value_input.literal_edit
        if not hasattr(self, "parameter_variable_combos"):
            self.parameter_variable_combos = {}
        self.parameter_variable_combos[(block.block_id, key)] = value_input.variable_combo

    def _make_value_input(self, value, commit):
        value_input = VariableValueInput(value, self._collect_available_variables())
        value_input.setEnabled(self.is_editable())
        value_input.valueChanged.connect(commit)

        def on_create_variable():
            new_value = self._open_variable_dialog()
            if not new_value:
                return
            self.logger.info(
                "Variable Created",
                function_id=self.current_function.function_id if self.current_function else "",
                variable=new_value,
            )
            value_input.select_variable(new_value)

        value_input.createVariableRequested.connect(on_create_variable)
        return value_input

    def _add_search_ocr_query_row(self, block, default):
        value_input = self._make_value_input(
            block.params.get("query", default),
            lambda text: block.params.__setitem__("query", text),
        )
        value_input.literal_edit.setPlaceholderText(t("function.ocr_query_text"))
        self.ocr_value_input = value_input
        self.ocr_query_edit = value_input.literal_edit
        self.ocr_variable_combo = value_input.variable_combo
        self.param_layout.addRow(self._parameter_label("query"), value_input)

    def _add_search_region_row(self, block):
        region = block.params.get("region")
        has_region = (
            isinstance(region, dict)
            and int(region.get("width", 0)) > 0
            and int(region.get("height", 0)) > 0
        )
        if has_region:
            region_text = (
                f"x={int(region.get('x', 0))}, y={int(region.get('y', 0))}, "
                f"{int(region.get('width', 0))} x {int(region.get('height', 0))}"
            )
        else:
            region_text = t(
                "function.ocr_region_from_input"
                if block.block_type == "ocr_read"
                else "function.full_screen_region"
            )

        self.search_region_label = QLabel(region_text)
        self.search_region_label.setWordWrap(True)
        self.select_region_button = QPushButton(
            t("function.redraw_region") if has_region else t("function.select_region")
        )
        self.clear_region_button = QPushButton(t("function.clear_region"))
        editable = self.is_editable()
        self.select_region_button.setEnabled(editable)
        self.clear_region_button.setEnabled(editable and has_region)
        self.select_region_button.clicked.connect(
            lambda: self._begin_search_region_selection(block)
        )
        self.clear_region_button.clicked.connect(
            lambda: self._clear_search_region(block)
        )

        row = QWidget()
        layout = QVBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.search_region_label)
        buttons = QHBoxLayout()
        buttons.addWidget(self.select_region_button)
        buttons.addWidget(self.clear_region_button)
        layout.addLayout(buttons)
        self.param_layout.addRow(t("function.search_region"), row)

    def _begin_search_region_selection(self, block):
        if not self.live_image.source_size:
            self.block_error.setText(t("function.capture_before_region"))
            return
        if self.live_image.region_selection_active:
            self._cancel_search_region_selection()
            return
        self._region_selection_block = block
        self.select_region_button.setText(t("function.drag_region"))
        self.live_image.begin_region_selection()

    def _cancel_search_region_selection(self):
        if not hasattr(self, "live_image"):
            return
        self.live_image.cancel_region_selection()
        self._region_selection_block = None
        if hasattr(self, "select_region_button"):
            region = (
                self.selected_block.params.get("region")
                if self.selected_block
                else None
            )
            has_region = (
                isinstance(region, dict)
                and region.get("width")
                and region.get("height")
            )
            self.select_region_button.setText(
                t("function.redraw_region") if has_region else t("function.select_region")
            )

    def _apply_search_region_selection(self, region):
        block = self._region_selection_block or self.selected_block
        self._region_selection_block = None
        if not region or not block:
            if hasattr(self, "select_region_button"):
                current_region = (
                    block.params.get("region")
                    if block
                    else None
                )
                has_region = (
                    isinstance(current_region, dict)
                    and current_region.get("width")
                    and current_region.get("height")
                )
                self.select_region_button.setText(
                    t("function.redraw_region")
                    if has_region
                    else t("function.select_region")
                )
            return

        size = self.live_image.source_size
        stored_region = {
            **region,
            "screen_width": size.width(),
            "screen_height": size.height(),
        }
        block.params["region"] = stored_region
        if block.block_type == "ocr_read":
            block.params["offset_x"] = 0
            block.params["offset_y"] = 0
        self._last_runtime_matches.pop(block.block_id, None)
        self._refresh_details()
        self.live_image.set_overlay_region(stored_region)
        self.block_error.setText(t("function.region_selected"))

    def _clear_search_region(self, block):
        block.params["region"] = None
        self._last_runtime_matches.pop(block.block_id, None)
        self._cancel_search_region_selection()
        self._refresh_details()
        self.block_error.setText(t(
            "function.ocr_region_from_input"
            if block.block_type == "ocr_read"
            else "function.full_screen_region"
        ))

    def _add_event_choice_row(self, block):
        combo = SearchableComboBox()
        combo.setEditable(True)
        combo.setToolTip(t("function.event_target_hint"))
        combo.setCurrentText(str(block.params.get("event_id") or ""))
        combo.currentTextChanged.connect(
            lambda text: block.params.update(event_id=text.strip())
        )
        self._event_target_combo = combo
        self.param_layout.addRow(t("function.event_target"), combo)
        game_id = self.game_combo.currentData() or ""
        profile_id = self.profile_combo.currentData() or ""
        if not game_id or not profile_id:
            combo.setPlaceholderText(t("function.profile_required"))
            return
        combo.setPlaceholderText(t("function.event_targets_loading"))
        service = (
            self.runtime_service.scheduler_service
            if self.runtime_service is not None else SchedulerService()
        )
        task = _EventChoiceTask(service, game_id, profile_id, self.profile_service)
        self._event_choice_tasks[task] = (block, combo)
        task.signals.finished.connect(self._apply_event_choices, Qt.QueuedConnection)
        self._event_choice_pool.start(task)

    def _apply_event_choices(self, task, events, error):
        request = self._event_choice_tasks.pop(task, None)
        if request is None:
            return
        block, combo = request
        if (
            combo is not self._event_target_combo
            or block is not self.selected_block
            or task.game_id != self.game_combo.currentData()
            or task.profile_id != self.profile_combo.currentData()
        ):
            return
        if error is not None:
            self.logger.error("Function Event Choices Failed", error=repr(error))
            self.block_error.setText(t("function.event_targets_failed").format(error=str(error)))
            return
        selected = str(block.params.get("event_id") or "")
        combo.blockSignals(True)
        combo.clear()
        combo.addItem("")
        # Names resolve within the executing account when Functions are shared.
        for event in events:
            combo.addItem(event.name, event.name)
        combo.setCurrentText(selected)
        combo.blockSignals(False)
        combo.setPlaceholderText(t("function.event_target"))

    def _add_parameter_row(self, block, key, default):

        if key == "event_id" and block.block_type in (
            BLOCK_SET_EVENT_DUE, BLOCK_RESCHEDULE_EVENT,
        ):
            self._add_event_choice_row(block)
            return

        if block.block_type == BLOCK_CONFIGURE_FIELD:
            if key == "variable_name":
                self._add_configure_field_variable_row(block, default)
                return
            if key == "field_target":
                self._add_labeled_choice_row(
                    block,
                    key,
                    (("task", "variables.target_task"), ("event", "variables.target_event")),
                    default,
                )
                return
            if key == "variable_kind":
                self._add_labeled_choice_row(
                    block,
                    key,
                    (
                        (VARIABLE_KIND_CONFIG, "variables.kind_config"),
                        (VARIABLE_KIND_PROFILE, "variables.kind_profile"),
                        (VARIABLE_KIND_LIVE, "variables.kind_live"),
                    ),
                    default,
                )
                return
            if key == "value_type":
                self._add_labeled_choice_row(
                    block,
                    key,
                    (
                        (VALUE_TYPE_TEXT, "variables.type_text"),
                        (VALUE_TYPE_NUMBER, "variables.type_number"),
                        (VALUE_TYPE_BOOLEAN, "variables.type_boolean"),
                        (VALUE_TYPE_DATETIME, "variables.type_datetime"),
                        (VALUE_TYPE_DURATION, "variables.type_duration"),
                        (VALUE_TYPE_COLLECTION, "variables.type_collection"),
                    ),
                    default,
                )
                return
            if key == "options_source":
                self._add_labeled_choice_row(
                    block,
                    key,
                    (
                        ("none", "variables.options_none"),
                        ("fixed", "variables.options_fixed"),
                        ("variable", "variables.options_variable"),
                    ),
                    default,
                )
                return
            if key == "fixed_options":
                if block.params.get("options_source", "none") == "fixed":
                    self._add_fixed_options_row(block)
                return
            if key == "options_variable":
                if block.params.get("options_source", "none") == "variable":
                    self._add_collection_choice_row(block)
                return

        if block.block_type in (BLOCK_SEARCH_OCR, BLOCK_SEARCH_OCR_TAP) and key == "query":
            self._add_search_ocr_query_row(block, default)
            return

        if block.block_type == BLOCK_TYPE_TEXT and key == "text":
            self._add_variable_choice_row(block, key, default)
            return

        if block.block_type == BLOCK_REPEAT and key == "max_iterations":
            self._add_variable_choice_row(block, key, default)
            return

        if key in (
            "until_variable",
            "delay_variable",
            "variable",
            "until_var",
            "query",
            "expression",
            "left",
            "right",
            "value",
            "input",
            "default_value",
        ):
            self._add_variable_choice_row(block, key, default)
            return

        if key == "library_entry_id":
            self._add_library_rows(block)
            return

        if key == "region":
            if block.block_type in ("ocr_read", BLOCK_SEARCH_OCR, BLOCK_SEARCH_OCR_TAP):
                self._add_search_region_row(block)
            return

        if key == "detector":
            self._add_choice_row(
                block,
                key,
                list(AVAILABLE_DETECTORS),
                str(default),
            )
            return

        if key == "direction":
            self._add_choice_row(
                block,
                key,
                list(SWIPE_DIRECTIONS),
                str(default),
            )
            return

        if key == "mode" and block.block_type == BLOCK_VARIABLE:
            self._add_choice_row(
                block,
                key,
                ["read", "write"],
                str(default),
            )
            return

        if key == "expect":
            self._add_choice_row(
                block,
                key,
                list(OCR_EXPECT_TYPES),
                str(default),
            )
            return

        if key == "counter_part":
            self._add_choice_row(
                block,
                key,
                ["left", "right"],
                str(default),
            )
            return

        if key == "function_id":
            self._add_function_choice_row(block)
            return

        if key == "operator":
            if block.block_type == BLOCK_MATH:
                choices = list(MATH_OPERATORS)
            elif block.block_type == BLOCK_GATE:
                choices = list(GATE_OPERATORS)
            else:
                choices = list(COMPARE_OPERATORS)
            self._add_choice_row(
                block,
                key,
                choices,
                str(default),
            )
            return

        if key == "operands":
            self._add_operand_count_row(block)
            return

        if key == "outputs" and block.block_type == "splitter":
            self._add_splitter_output_count_row(block)
            return

        value = block.params.get(key, default)

        if isinstance(default, bool):
            check = QCheckBox()
            check.setChecked(bool(value))
            check.stateChanged.connect(
                lambda state: block.params.__setitem__(key, bool(state))
            )
            self.param_layout.addRow(
                self._parameter_label(key),
                check,
            )
            return

        if isinstance(default, int):
            spin = QSpinBox()
            minimum = 0 if key == "delay_before_read_ms" else -100000
            spin.setRange(minimum, 100000000)
            spin.setValue(int(value or 0))
            def on_value_changed(number):
                block.params[key] = number
                self._refresh_live_ocr_overlay()

            spin.valueChanged.connect(on_value_changed)
            self.param_layout.addRow(
                self._parameter_label(key),
                spin,
            )
            return

        line = QLineEdit(str(value or ""))
        line.textChanged.connect(
            lambda text: block.params.__setitem__(key, text)
        )
        self.param_layout.addRow(
            self._parameter_label(key),
            line,
        )

    def _add_labeled_choice_row(self, block, key, choices, default):
        combo = QComboBox()
        for value, label_key in choices:
            combo.addItem(t(label_key), value)
        current = block.params.get(key, default)
        index = combo.findData(current)
        combo.setCurrentIndex(index if index >= 0 else 0)
        combo.currentIndexChanged.connect(
            lambda _index: self._set_configure_field_choice(
                block,
                key,
                combo.currentData(),
            )
        )
        self.param_layout.addRow(self._parameter_label(key), combo)

    def _set_configure_field_choice(self, block, key, value):
        block.params[key] = value
        if key in {"variable_kind", "options_source"}:
            QTimer.singleShot(0, self._refresh_details)

    def _add_configure_field_variable_row(self, block, default):
        """Dropdown over known registry variables, free text stays allowed."""

        combo = SearchableComboBox()
        combo.setEditable(True)
        combo.addItem("", "")
        game_id = self.game_combo.currentData() or ""
        for entry in self.variable_service.list_globals(game_id):
            name = str(entry.get("name", "")).strip()
            if name:
                combo.addItem(name, name)
        current = str(block.params.get("variable_name", default) or "")
        index = combo.findText(current)
        if index >= 0:
            combo.setCurrentIndex(index)
        else:
            combo.setCurrentText(current)
        combo.currentTextChanged.connect(
            lambda text: block.params.__setitem__(
                "variable_name",
                text.strip(),
            )
        )

        create_button = QPushButton(t("variables.create"))
        create_button.setEnabled(self.is_editable())

        def on_create():
            self._create_variable_for_field(block, combo)

        create_button.clicked.connect(on_create)

        row_widget = QWidget()
        row_layout = QHBoxLayout(row_widget)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.addWidget(combo, stretch=1)
        row_layout.addWidget(create_button)

        self.param_layout.addRow(
            self._parameter_label("variable_name"),
            row_widget,
        )

    def _create_variable_for_field(self, block, combo):
        """Create a registry variable prefilled with this block's contract."""

        dialog = VariableCreateDialog(
            self.game_combo.currentData() or "",
            self.variable_service,
            parent=self,
            field_target=str(block.params.get("field_target") or "task"),
        )
        # prefill the dialog from the block's declared contract so the user
        # only confirms instead of re-typing kind/type/writability
        dialog.name_edit.setText(
            str(block.params.get("variable_name") or "").strip()
        )
        dialog.description_edit.setText(
            str(block.params.get("description") or "").strip()
        )
        kind_index = dialog.kind_combo.findData(
            str(block.params.get("variable_kind") or VARIABLE_KIND_CONFIG)
        )
        if kind_index >= 0:
            dialog.kind_combo.setCurrentIndex(kind_index)
        type_index = dialog.type_combo.findData(
            str(block.params.get("value_type") or VALUE_TYPE_TEXT)
        )
        if type_index >= 0:
            dialog.type_combo.setCurrentIndex(type_index)
        dialog.initial_value_edit.setText(
            str(block.params.get("initial_value") or "")
        )
        dialog.function_writable_check.setChecked(
            bool(block.params.get("writable_by_functions", False))
        )

        if dialog.exec() != QDialog.Accepted or not dialog.result_value:
            return

        name = dialog.result_value
        if combo.findText(name) < 0:
            combo.addItem(name, name)
        combo.setCurrentText(name)
        self.logger.info(
            "Variable Created From Configure Field",
            function_id=self.current_function.function_id if self.current_function else "",
            variable=name,
        )

    def _add_fixed_options_row(self, block):
        value = block.params.get("fixed_options", [])
        if isinstance(value, list):
            value = json.dumps(value, ensure_ascii=False)
        line = QLineEdit(str(value or "[]"))
        line.setPlaceholderText('["Option A", "Option B"]')
        line.textChanged.connect(
            lambda text: block.params.__setitem__("fixed_options", text)
        )
        self.param_layout.addRow(
            self._parameter_label("fixed_options"),
            line,
        )

    def _add_collection_choice_row(self, block):
        combo = SearchableComboBox()
        combo.setEditable(True)
        combo.addItem("", "")
        game_id = self.game_combo.currentData() or ""
        for entry in self.variable_service.list_globals(game_id):
            if entry.get("value_type") == VALUE_TYPE_COLLECTION:
                name = f"${entry['name']}"
                combo.addItem(name, name)
        current = str(block.params.get("options_variable") or "")
        index = combo.findText(current)
        if index >= 0:
            combo.setCurrentIndex(index)
        else:
            combo.setCurrentText(current)
        combo.currentTextChanged.connect(
            lambda text: block.params.__setitem__(
                "options_variable",
                text.strip(),
            )
        )
        self.param_layout.addRow(
            self._parameter_label("options_variable"),
            combo,
        )

    def _add_function_choice_row(self, block):

        combo = SearchableComboBox()
        combo.addItem(t("function.select_function"), "")
        game_id = self.game_combo.currentData() or ""
        helpers = sorted(
            self.function_service.list_functions(game_id, kind=FUNCTION_KIND_HELPER),
            key=lambda func: (func.name or func.function_id).casefold(),
        )
        for func in helpers:
            label = f"{func.name or func.function_id[:8]} [{t('function.kind_helper')}]"
            combo.addItem(label, func.function_id)

        current_id = block.params.get("function_id", "")
        index = combo.findData(current_id)
        if index >= 0:
            combo.setCurrentIndex(index)
        else:
            combo.setCurrentIndex(0)

        def on_helper_selected():
            selected_id = combo.currentData() or ""
            if block.params.get("function_id") == selected_id:
                return
            block.params["function_id"] = selected_id
            if selected_id:
                matched_func = next((f for f in helpers if f.function_id == selected_id), None)
                if matched_func:
                    block.name = matched_func.name or t("function.block.call_function")
            else:
                block.name = t("function.block.call_function")

            QTimer.singleShot(
                0,
                lambda: self._refresh_call_function_details(block),
            )

        combo.currentIndexChanged.connect(on_helper_selected)
        self.param_layout.addRow(
            self._parameter_label("function_id"),
            combo,
        )

        if current_id:
            helper_obj = self.function_service.load(game_id, current_id)
            if helper_obj:
                if helper_obj.description:
                    desc_lbl = QLabel(helper_obj.description)
                    desc_lbl.setStyleSheet("color: gray; font-style: italic;")
                    desc_lbl.setWordWrap(True)
                    self.param_layout.addRow(t("function.description"), desc_lbl)

                if helper_obj.interface.inputs:
                    inputs_dict = block.params.setdefault("inputs", {})
                    for inp in helper_obj.interface.inputs:
                        value_input = self._make_value_input(
                            inputs_dict.get(inp, ""),
                            lambda text, key=inp: inputs_dict.__setitem__(key, text.strip())
                        )
                        self.param_layout.addRow(f"{inp}:", value_input)

    def _refresh_call_function_details(self, block):
        if not self.current_function or block not in self.current_function.blocks:
            return
        self.canvas.draw_blocks(
            self.current_function.blocks,
            self.current_function.connections,
        )
        if self.selected_block is block:
            self._refresh_details()

    def _parameter_label(self, key):

        label = t(f"function.parameter_{key}")

        return key if label.startswith("function.") else label

    def _add_choice_row(self, block, key, choices, default):

        combo = SearchableComboBox()
        combo.addItems(sorted(choices, key=str.casefold))
        combo.setCurrentText(
            str(block.params.get(key, default))
        )

        def commit(text):
            # ignore signals from a combo the panel rebuild already replaced
            if not combo.isVisible() and combo.parentWidget() is None:
                return
            self._set_choice_value(block, key, text)

        combo.textActivated.connect(commit)
        combo.currentIndexChanged.connect(
            lambda _index: commit(combo.currentText())
        )
        self.param_layout.addRow(
            self._parameter_label(key),
            combo,
        )

    def _set_choice_value(
        self,
        block,
        key,
        value,
    ):

        block.params[key] = value

        if key == "mode" and block.block_type == BLOCK_VARIABLE:
            self.canvas.draw_blocks(
                self.current_function.blocks,
                self.current_function.connections,
            )
        # defer the re-render: the combo must survive its own change signal
        QTimer.singleShot(0, self._refresh_details)
        QTimer.singleShot(0, self._refresh_live_ocr_overlay)

    def _add_splitter_output_count_row(self, block):

        count_box = QSpinBox()
        count_box.setRange(2, 4)
        count_box.setValue(
            max(2, min(4, int(block.params.get("outputs", 2))))
        )
        count_box.valueChanged.connect(
            lambda value: self._set_splitter_output_count(block, value)
        )
        self.param_layout.addRow(
            t("function.splitter_outputs"),
            count_box,
        )

    def _set_splitter_output_count(self, block, count):

        block.params["outputs"] = count

        # Clean up any connections that were attached to pruned outputs
        valid_outcomes = {f"out_{i+1}" for i in range(count)}
        pruned_conns = [
            c for c in self.current_function.connections
            if c.source_block_id == block.block_id and c.source_outcome not in valid_outcomes
        ]
        for c in pruned_conns:
            self.remove_connection(c.source_block_id, c.source_outcome, target_id=c.target_block_id, target_input=c.target_input)

        self.canvas.draw_blocks(
            self.current_function.blocks,
            self.current_function.connections,
        )
        self._refresh_details()

    def _add_operand_count_row(self, block):

        input_count = QSpinBox()
        input_count.setRange(1, 32)
        input_count.setValue(
            max(1, len(block.params.get("operands", [])))
        )
        input_count.valueChanged.connect(
            lambda value: self._set_operand_count(block, value)
        )
        self.param_layout.addRow(
            t("function.gate_inputs"),
            input_count,
        )

    def _set_operand_count(self, block, count):

        operands = list(
            block.params.get("operands", [])
        )

        block.params["operands"] = (
            operands + [""] * count
        )[:count]

        self.canvas.draw_blocks(
            self.current_function.blocks,
            self.current_function.connections,
        )

    def _add_library_rows(self, block):

        self.library_combo = SearchableComboBox()
        self.library_combo.setEditable(True)
        self.library_combo.setInsertPolicy(QComboBox.NoInsert)
        self.library_preview = QLabel(
            t("function.no_library_preview")
        )
        self.library_preview.setMinimumHeight(100)
        self._reset_library_combo(block)

        # substring, case-insensitive typeahead filtering over the combo's own items
        completer = self.library_combo.completer()
        completer.setCaseSensitivity(Qt.CaseInsensitive)
        completer.setFilterMode(Qt.MatchContains)
        completer.setCompletionMode(QCompleter.PopupCompletion)

        self.library_combo.currentIndexChanged.connect(
            lambda: block.params.__setitem__(
                "library_entry_id",
                self.library_combo.currentData() or "",
            )
        )
        self.param_layout.addRow(
            t("function.library_entry"),
            self.library_combo,
        )

        self.learn_library_button = QPushButton(
            t("function.learn_missing")
        )
        self.learn_library_button.clicked.connect(
            lambda: self._open_learning_for_block(block)
        )
        self.param_layout.addRow(
            self.learn_library_button,
        )

        self.edit_library_button = QPushButton(
            t("function.edit_existing")
        )
        self.edit_library_button.setEnabled(
            bool(block.params.get("library_entry_id"))
        )
        self.edit_library_button.clicked.connect(
            lambda: self._open_learning_for_block(
                block,
                edit_existing=True,
            )
        )
        self.param_layout.addRow(
            self.edit_library_button,
        )

        self.param_layout.addRow(
            t("function.preview"),
            self.library_preview,
        )

        self.library_combo.currentIndexChanged.connect(
            self.refresh_library_preview
        )
        self.library_combo.currentIndexChanged.connect(
            self._refresh_live_ocr_overlay
        )
        self.refresh_library_preview()

    def _refresh_live_ocr_overlay(self):

        block = self.selected_block
        if block and self._refresh_live_geometry_overlay(block):
            return
        searchable_types = (
            "ocr_read",
            BLOCK_SEARCH_OCR,
            BLOCK_SEARCH_OCR_TAP,
            BLOCK_SWIPE_SEARCH,
            BLOCK_SWIPE_SEARCH_TAP,
            BLOCK_FIND_ELEMENT,
            BLOCK_FIND_AND_TAP_ELEMENT,
            BLOCK_TAP_ELEMENT,
            BLOCK_WAIT_FOR_ELEMENT,
            BLOCK_WAIT_FOR_ELEMENT_AND_TAP,
            BLOCK_FIND_SHAPE,
            BLOCK_FIND_COLOR_TRIGGER,
        )
        if not block or block.block_type not in searchable_types:
            self.live_image.set_overlay_region(None)
            return

        coordinate_target_types = (
            BLOCK_FIND_AND_TAP_ELEMENT,
            BLOCK_TAP_ELEMENT,
            BLOCK_WAIT_FOR_ELEMENT_AND_TAP,
        )
        offset_tap_types = coordinate_target_types + (
            BLOCK_SEARCH_OCR_TAP,
            BLOCK_SWIPE_SEARCH_TAP,
        )
        anchor = (
            self._coordinate_preview_anchor(block)
            if block.block_type in coordinate_target_types
            else None
        )

        runtime_match = self._last_runtime_matches.get(block.block_id)
        if (
            not anchor
            and isinstance(runtime_match, dict)
            and self._has_match_geometry(runtime_match)
        ):
            preview_match = dict(runtime_match)
            if block.block_type in offset_tap_types:
                # recomputed so a changed offset moves the tap marker live
                preview_match["tap_x"] = (
                    int(runtime_match["x"])
                    + int(runtime_match["width"]) // 2
                    + int(block.params.get("offset_x", 0))
                )
                preview_match["tap_y"] = (
                    int(runtime_match["y"])
                    + int(runtime_match["height"]) // 2
                    + int(block.params.get("offset_y", 0))
                )
            self.live_image.set_overlay_region(preview_match)
            return

        if block.block_type in (BLOCK_SEARCH_OCR, BLOCK_SEARCH_OCR_TAP):
            region = block.params.get("region")
            if (
                isinstance(region, dict)
                and int(region.get("width", 0)) > 0
                and int(region.get("height", 0)) > 0
            ):
                preview_region = dict(region)
                if block.block_type == BLOCK_SEARCH_OCR_TAP:
                    preview_region["tap_x"] = (
                        int(region.get("x", 0))
                        + int(region.get("width", 0)) // 2
                        + int(block.params.get("offset_x", 0))
                    )
                    preview_region["tap_y"] = (
                        int(region.get("y", 0))
                        + int(region.get("height", 0)) // 2
                        + int(block.params.get("offset_y", 0))
                    )
                self.live_image.set_overlay_region(preview_region)
            else:
                if block.block_type == BLOCK_SEARCH_OCR_TAP and self.live_image.source_size:
                    width = self.live_image.source_size.width()
                    height = self.live_image.source_size.height()
                    self.live_image.set_overlay_region({
                        "x": 0,
                        "y": 0,
                        "width": width,
                        "height": height,
                        "tap_x": width // 2 + int(block.params.get("offset_x", 0)),
                        "tap_y": height // 2 + int(block.params.get("offset_y", 0)),
                        "screen_width": width,
                        "screen_height": height,
                    })
                else:
                    self.live_image.set_overlay_region(None)
            return

        if block.block_type in coordinate_target_types:
            entry_id = block.params.get("library_entry_id", "")
            entry = self.library_service.load(entry_id) if entry_id else None
            region = build_offset_search_region(
                anchor,
                entry or {},
                block.params.get("offset_x", 0),
                block.params.get("offset_y", 0),
            ) if anchor else None
            if region:
                self.live_image.set_overlay_region(region)
                return

        if block.block_type != "ocr_read":
            self.live_image.set_overlay_region(
                self._library_search_preview(block, block.block_type in offset_tap_types)
            )
            return

        entry_id = block.params.get("library_entry_id", "")
        entry = self.library_service.load(entry_id) if entry_id else None
        self.live_image.set_overlay_region(resolve_ocr_region(
            block.params,
            self._coordinate_preview_anchor(block),
            entry,
        ))

    def _refresh_live_geometry_overlay(self, block):
        """Fixed-coordinate blocks: show point, swipe or anchor rectangle live."""

        params = block.params
        if block.block_type in (BLOCK_TAP_POINT, BLOCK_CLEAR_FIELD, BLOCK_TYPE_TEXT):
            x = int(params.get("x", 0) or 0)
            y = int(params.get("y", 0) or 0)
            self.live_image.set_overlay_region({
                "x": x, "y": y, "width": 0, "height": 0,
                "point_only": True, "tap_x": x, "tap_y": y,
            })
            return True

        if block.block_type == BLOCK_SWIPE:
            self.live_image.set_overlay_region(self._swipe_preview(params))
            return True

        if block.block_type == BLOCK_COORDINATE_ANCHOR:
            region = {
                "x": int(params.get("x", 0) or 0),
                "y": int(params.get("y", 0) or 0),
                "width": int(params.get("width", 0) or 0),
                "height": int(params.get("height", 0) or 0),
            }
            if int(params.get("screen_width", 0) or 0) > 0:
                region["screen_width"] = int(params["screen_width"])
            if int(params.get("screen_height", 0) or 0) > 0:
                region["screen_height"] = int(params["screen_height"])
            self.live_image.set_overlay_region(region)
            return True
        return False

    def _swipe_preview(self, params):
        size = self.live_image.source_size
        if not size:
            return None
        try:
            start_x, start_y, end_x, end_y = FunctionRunner._swipe_points(
                str(params.get("direction", "up")).lower(),
                int(params.get("start_x", 0) or 0),
                int(params.get("start_y", 0) or 0),
                size.width(),
                size.height(),
            )
        except ValueError:
            return None
        return {
            "x": start_x, "y": start_y, "width": 0, "height": 0,
            "point_only": True, "tap_x": start_x, "tap_y": start_y,
            "swipe_start_x": start_x, "swipe_start_y": start_y,
            "swipe_end_x": end_x, "swipe_end_y": end_y,
        }

    def _library_search_preview(self, block, with_tap):
        """Search area the runtime uses without anchor/match, plus tap target."""

        entry_id = block.params.get("library_entry_id", "")
        entry = self.library_service.load(entry_id) if entry_id else None
        entry = entry if isinstance(entry, dict) else {}
        boundary = entry.get("boundary")
        region = block.params.get("region")
        if isinstance(region, dict) and region.get("width") and region.get("height"):
            preview = dict(region)
        elif isinstance(boundary, dict) and boundary.get("width") and boundary.get("height"):
            preview = dict(boundary)
        else:
            preview = None

        if block.block_type in (BLOCK_SWIPE_SEARCH, BLOCK_SWIPE_SEARCH_TAP):
            swipe = self._swipe_preview(block.params)
            if swipe:
                if preview is None:
                    preview = swipe
                else:
                    for key in ("swipe_start_x", "swipe_start_y", "swipe_end_x", "swipe_end_y"):
                        preview[key] = swipe[key]

        if with_tap:
            anchor = self._tap_target_anchor(block)
            if anchor:
                if preview is None:
                    preview = {"x": anchor[0], "y": anchor[1], "width": 0, "height": 0, "point_only": True}
                preview["tap_x"] = anchor[0] + int(block.params.get("offset_x", 0) or 0)
                preview["tap_y"] = anchor[1] + int(block.params.get("offset_y", 0) or 0)
        return preview

    @staticmethod
    def _has_match_geometry(value):
        return all(
            key in value
            for key in ("x", "y", "width", "height")
        )

    @staticmethod
    def _normalize_runtime_match(value):
        if not isinstance(value, dict):
            return None
        if not all(key in value for key in ("x", "y")):
            return None
        width = value.get("width", value.get("w"))
        height = value.get("height", value.get("h"))
        if width is None or height is None:
            return None
        normalized = {
            "x": int(value.get("x", 0)),
            "y": int(value.get("y", 0)),
            "width": int(width),
            "height": int(height),
        }
        if "tap_x" in value and "tap_y" in value:
            normalized["tap_x"] = int(value["tap_x"])
            normalized["tap_y"] = int(value["tap_y"])
        return normalized

    def _tap_target_anchor(self, block):
        """Center of the search anchor in live-image coordinates for tap offsets."""

        entry_id = block.params.get("library_entry_id", "")
        entry = self.library_service.load(entry_id) if entry_id else None
        pattern = entry.get("pattern", {}) if entry else {}
        if pattern.get("width") and pattern.get("height"):
            return (
                int(pattern.get("x", 0)) + int(pattern["width"]) // 2,
                int(pattern.get("y", 0)) + int(pattern["height"]) // 2,
            )

        region = block.params.get("region")
        if isinstance(region, dict) and region.get("width") and region.get("height"):
            return (
                int(region.get("x", 0)) + int(region["width"]) // 2,
                int(region.get("y", 0)) + int(region["height"]) // 2,
            )

        if self.live_image.source_size:
            return (
                self.live_image.source_size.width() // 2,
                self.live_image.source_size.height() // 2,
            )
        return None

    def _ocr_preview_anchor(self, block):

        runtime_coordinates = self._last_runtime_coordinates.get(block.block_id)
        if isinstance(runtime_coordinates, dict):
            return (
                int(runtime_coordinates.get("x", 0)),
                int(runtime_coordinates.get("y", 0)),
            )

        connection = next(
            (
                item
                for item in self.current_function.connections
                if item.target_block_id == block.block_id
                and item.target_input == "coordinates"
            ),
            None,
        ) if self.current_function else None

        if not connection:
            return 0, 0

        source = next(
            (
                item
                for item in self.current_function.blocks
                if item.block_id == connection.source_block_id
            ),
            None,
        )
        source_entry_id = source.params.get("library_entry_id", "") if source else ""
        source_entry = self.library_service.load(source_entry_id) if source_entry_id else None
        pattern = source_entry.get("pattern", {}) if source_entry else {}
        return int(pattern.get("x", 0)), int(pattern.get("y", 0))

    def _coordinate_preview_anchor(self, block):
        runtime_coordinates = self._last_runtime_coordinates.get(block.block_id)
        if isinstance(runtime_coordinates, dict) and "x" in runtime_coordinates:
            return runtime_coordinates

        connection = next(
            (
                item
                for item in self.current_function.connections
                if item.target_block_id == block.block_id
                and item.target_input == "coordinates"
            ),
            None,
        ) if self.current_function else None
        if not connection:
            return None

        source = next(
            (
                item
                for item in self.current_function.blocks
                if item.block_id == connection.source_block_id
            ),
            None,
        )
        source_entry_id = source.params.get("library_entry_id", "") if source else ""
        source_entry = self.library_service.load(source_entry_id) if source_entry_id else None
        pattern = source_entry.get("pattern", {}) if source_entry else {}
        if not isinstance(pattern, dict) or "x" not in pattern or "y" not in pattern:
            return None
        return pattern

    def refresh_library_preview(self):

        if not hasattr(self, "library_combo") or not hasattr(self, "library_preview"):
            return

        entry_id = self.library_combo.currentData()

        if not entry_id:
            self.library_preview.clear()
            self.library_preview.setText(
                t("function.no_library_preview")
            )
            return

        entry = self.library_service.load(entry_id) or {}
        samples = entry.get("accepted_samples", [])
        image_path = None

        if samples:
            image_path = samples[0].get("crop")

        if not image_path:
            image_path = (
                entry.get("pattern", {}).get("reference_path")
            )

        if not image_path:
            image_path = str(
                self.library_service.root
                / entry_id
                / "pattern_reference.png"
            )

        pixmap = load_pixmap(image_path, QSize(440, 240))

        if pixmap.isNull():
            self.library_preview.setText(
                t("function.no_library_preview")
            )
            return

        self.library_preview.setPixmap(
            pixmap.scaled(
                220,
                120,
                Qt.KeepAspectRatio,
                Qt.SmoothTransformation,
            )
        )

    def save_function(self, on_finished=None):
        """Schedule a save; continuations run only after identity and disk writes finish."""
        if self._save_task is not None:
            return False
        if not self.current_function:
            if on_finished is not None:
                on_finished(False)
            return False

        if not self.is_editable():
            self.block_error.setText(
                t("function.expert_mode_required")
            )
            if on_finished is not None:
                on_finished(False)
            return False

        name = self.function_name.text().strip()
        description = self.details.toPlainText().strip()
        missing = []
        if not name:
            missing.append(t("function.name"))
        if not description:
            missing.append(t("function.details"))

        if missing:
            result = QMessageBox.question(
                self,
                t("common.save"),
                t("function.missing_fields_save").format(
                    fields=" / ".join(missing),
                ),
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if result != QMessageBox.Yes:
                self.logger.info(
                    "Function Save Cancelled",
                    function_id=self.current_function.function_id,
                    missing_fields=missing,
                )
                self._update_required_field_styles()
                if on_finished is not None:
                    on_finished(False)
                return False

        candidate = deepcopy(self.current_function)
        candidate.name = name
        candidate.game_id = self.game_combo.currentData() or ""
        candidate.kind = (
            self.kind_combo.currentData() or FUNCTION_KIND_REGULAR
        )
        candidate.description = description
        self._save_callback = on_finished
        self._save_task = _FunctionSaveTask(self.function_service, self.github_auth, candidate)
        self._save_task.signals.finished.connect(self._function_save_finished)
        self.block_error.setText(t("function.checking_identity"))
        self.setEnabled(False)
        QThreadPool.globalInstance().start(self._save_task)
        return True

    def _function_save_finished(self, function, error):
        callback = self._save_callback
        notifications = self._save_task.notifications
        self._save_callback = None
        self._save_task = None
        self.setEnabled(True)
        for service, source, payload in notifications:
            service.publish(source, **payload)
        if error:
            self.logger.error("Function Save Failed", error=error, next_action="check GitHub sign-in or remove local sign-in for an anonymous save")
            QMessageBox.critical(self, t("common.save"), error)
            self.block_error.setText(error)
        else:
            selected_id = self.selected_block.block_id if self.selected_block else None
            self.current_function = function
            self.canvas.current_function = function
            self.selected_block = next(
                (block for block in function.walk_blocks() if block.block_id == selected_id), None,
            )
            self._update_required_field_styles()
            self.refresh_function_list()
            self._refresh_details()
            self.block_error.setText(t("function.saved"))
        if callback is not None:
            callback(not bool(error))

    def delete_current_function(self):
        if not self.current_function or not self.current_function.saved:
            return

        function = self.current_function
        dependencies = []
        dependencies.extend(
            f"Function: {user.name or user.function_id}"
            for user in self.function_service.find_users_of_helper(
                function.game_id,
                function.function_id,
            )
        )
        dependencies.extend(
            f"Task {user['task_name']} in Event {user['event_name']}"
            for user in self.function_service.find_scheduler_users(
                function.game_id,
                function.function_id,
            )
        )
        message = f"Delete function '{function.name or function.function_id}'?"
        if dependencies:
            message += "\n\nDependencies:\n" + "\n".join(dependencies)
        answer = QMessageBox.warning(
            self,
            "Delete function",
            message,
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return

        if not self.function_service.delete(function.game_id, function.function_id):
            return
        self.new_function()
        self.refresh_function_list()
        if hasattr(self, "refresh_library_preview"):
            self.refresh_library_preview()

    def save_copy_as(self):
        if self._save_task is not None or not self.current_function or not self.is_editable():
            return False

        name, accepted = QInputDialog.getText(
            self,
            t("function.dialog.save_copy_title"),
            t("function.dialog.function_name"),
            text=f"{self.current_function.name} Copy".strip(),
        )
        name = name.strip()
        if not accepted or not name:
            return False

        original = self.current_function
        original_name = self.function_name.text()
        original_description = self.details.toPlainText()
        selected_block_id = (
            self.selected_block.block_id
            if self.selected_block
            else None
        )
        copied = deepcopy(self.current_function)
        copied.function_id = str(uuid4())
        copied.name = name
        copied.saved = False
        copied.revision = 1
        copied.used = []
        copied.used_by = []
        copied.history = []
        self.logger.info(
            "Function Copied",
            source_function_id=self.current_function.function_id,
            new_function_id=copied.function_id,
            name=name,
        )
        self.current_function = copied
        self.canvas.current_function = copied
        self.selected_block = next(
            (
                block
                for block in copied.walk_blocks()
                if block.block_id == selected_block_id
            ),
            None,
        )
        self.function_name.setText(name)
        def completed(success):
            target = self.current_function if success else original
            self.current_function = target
            self.canvas.current_function = target
            self.selected_block = next(
                (block for block in target.walk_blocks() if block.block_id == selected_block_id), None,
            )
            self.function_name.setText(target.name if success else original_name)
            self.details.setPlainText(target.description if success else original_description)
            self.canvas.set_layout_positions(target.layout_positions)
            self.canvas.draw_blocks(target.blocks, target.connections)
            self._apply_edit_permissions()
            self._refresh_details()

        return self.save_function(on_finished=completed)

    def debug_block(self):

        if not self.selected_block:
            self.block_error.setText(
                t("function.block_required")
            )
            return

        self._start_debug_run(
            self.selected_block.block_id
        )

    def debug_chain(self):

        self._start_debug_run(
            None
        )

    def _debug_field_descriptors(self):
        if not self.current_function:
            return []

        functions = self.function_service.list_functions(
            self.current_function.game_id
        )
        functions = [
            function
            for function in functions
            if function.function_id != self.current_function.function_id
        ]
        functions.append(self.current_function)
        declarations = self.scheduler_field_service.collect_function_declarations(
            self.current_function.game_id,
            self.current_function.function_id,
            functions=functions,
        )
        cached = self._debug_values_by_function.get(
            self.current_function.function_id,
            {},
        )
        fields_by_name = {}
        for declaration in declarations:
            name = str(
                declaration.get("variable_name") or ""
            ).strip().lstrip("$")
            if not name or name.casefold() in fields_by_name:
                continue
            definition = self.variable_service.get_definition(
                self.current_function.game_id,
                name,
            ) or {}
            canonical_name = definition.get("name", name)
            value_type = str(
                declaration.get("value_type")
                or definition.get("value_type")
                or VALUE_TYPE_TEXT
            )
            try:
                default = self.variable_service.coerce_value(
                    value_type,
                    declaration.get(
                        "initial_value",
                        definition.get("initial_value"),
                    ),
                )
            except (TypeError, ValueError):
                default = definition.get("initial_value")
            fixed_options = declaration.get("fixed_options") or []
            if isinstance(fixed_options, str):
                try:
                    fixed_options = json.loads(fixed_options)
                except json.JSONDecodeError:
                    fixed_options = []
            fields_by_name[name.casefold()] = {
                "name": canonical_name,
                "description": str(
                    declaration.get("description")
                    or definition.get("description")
                    or ""
                ),
                "value_type": value_type,
                "default": default,
                "value": cached.get(canonical_name, default),
                "options_source": str(
                    declaration.get("options_source") or "none"
                ),
                "fixed_options": list(fixed_options),
                "options_variable": str(
                    declaration.get("options_variable") or ""
                ).lstrip("$"),
            }

        current_values = {
            field["name"].casefold(): field["value"]
            for field in fields_by_name.values()
        }
        for field in fields_by_name.values():
            if field["options_source"] == "fixed":
                field["options"] = field["fixed_options"]
            elif field["options_source"] == "variable":
                options = current_values.get(
                    field["options_variable"].casefold(),
                    [],
                )
                field["options"] = options if isinstance(options, list) else []
            else:
                field["options"] = []
        return sorted(
            fields_by_name.values(),
            key=lambda field: field["name"].casefold(),
        )

    def configure_debug_values(self):
        if not self.current_function:
            return
        self.save_function(on_finished=self._configure_debug_values_after_save)

    def _configure_debug_values_after_save(self, success):
        if not success:
            return
        fields = self._debug_field_descriptors()
        if not fields:
            QMessageBox.information(
                self,
                t("function.debug_values"),
                t("function.debug_values_none"),
            )
            return
        dialog = DebugValuesDialog(fields, self)
        if dialog.exec() != QDialog.Accepted:
            return
        self._debug_values_by_function[
            self.current_function.function_id
        ] = dialog.values()
        self.block_error.setText(
            t("function.debug_values_ready").format(
                count=len(fields),
            )
        )

    def _debug_values_for_run(self):
        return {
            field["name"]: field["value"]
            for field in self._debug_field_descriptors()
        }

    def _start_debug_run(
        self,
        block_id,
    ):

        if not self.current_function:
            return

        self._last_runtime_coordinates = {}
        self._last_runtime_matches = {}
        self.live_image.set_overlay_region(None)

        profile_id = self.profile_combo.currentData()

        if not profile_id:
            self.logger.warning(
                "Debug Run Rejected",
                function_id=self.current_function.function_id,
                block_id=block_id,
                reason="no profile selected",
            )
            self.block_error.setText(
                t("function.profile_required")
            )
            return

        if self.runtime_service is not None and self.runtime_service.running:
            answer = QMessageBox.question(
                self,
                "Runtime läuft",
                "Die Runtime läuft bereits. Soll sie gestoppt und der Debuglauf gestartet werden?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                self.block_error.setText("Debuglauf abgebrochen: Runtime läuft weiter.")
                return
            self.runtime_service.stop()

        self.save_function(
            on_finished=lambda success: self._debug_run_after_save(success, profile_id, block_id),
        )

    def _debug_run_after_save(self, success, profile_id, block_id):
        if not success:
            return

        debug_values = self._debug_values_for_run()

        try:
            started = self.supervisor.start_function(
                profile_id,
                self.current_function.function_id,
                block_id,
                allow_disabled=True,
                debug_values=debug_values,
            )
        except RuntimeError as error:
            self.logger.error(
                "Debug Run Start Failed",
                function_id=self.current_function.function_id,
                profile=profile_id,
                error=str(error),
            )
            self.block_error.setText(f"{t('function.debug_start_failed')}: {error}")
            return

        if started:
            self.logger.info(
                "Debug Run Started",
                function_id=self.current_function.function_id,
                profile=profile_id,
                block_id=block_id or "",
            )
            self.block_error.setText(
                t("function.debug_started")
            )
        else:
            self.logger.error(
                "Debug Run Start Failed",
                function_id=self.current_function.function_id,
                profile=profile_id,
                block_id=block_id or "",
            )
            self.block_error.setText(
                t("function.debug_start_failed")
            )

    def stop_debug(self):

        profile_id = self.profile_combo.currentData()

        if profile_id:
            self.logger.info(
                "Debug Run Stopped",
                profile=profile_id,
                function_id=self.current_function.function_id if self.current_function else "",
            )
            self.supervisor.stop_profile(
                profile_id
            )

        self.block_error.setText(
            t("function.debug_stopped")
        )

    def poll_worker_events(self):

        for event in self.supervisor.poll_events():

            if event.get("event") == "function_finished":
                self.logger.info(
                    "Debug Run Finished",
                    function_id=event.get("function_id", ""),
                    profile=event.get("profile_id", ""),
                    outcome=event.get("outcome", ""),
                )
                self._remember_runtime_coordinates(event)
                self._refresh_live_ocr_overlay()
                self.block_error.setText(
                    self._format_run_result(event)
                )

            elif event.get("event") == "function_failed":
                self.logger.error(
                    "Debug Run Failed",
                    function_id=event.get("function_id", ""),
                    profile=event.get("profile_id", ""),
                    error=event.get("error", ""),
                )
                self.block_error.setText(
                    event.get(
                        "error",
                        t("function.debug_start_failed"),
                    )
                )

    def _remember_runtime_coordinates(self, event):

        if not self.current_function:
            return

        trace = event.get("trace") or []
        by_block_id = {
            step.get("block_id"): step
            for step in trace
            if step.get("block_id")
        }

        for connection in self.current_function.connections:
            if connection.target_input != "coordinates":
                continue

            source_step = by_block_id.get(connection.source_block_id)
            value = source_step.get("value") if source_step else None
            if isinstance(value, dict) and "x" in value and "y" in value:
                self._last_runtime_coordinates[connection.target_block_id] = value

        self._last_runtime_matches = {
            block_id: match
            for block_id, step in by_block_id.items()
            if (match := self._normalize_runtime_match(step.get("value")))
        }

        for block_id, step in by_block_id.items():
            value = step.get("value")
            if isinstance(value, dict) and {"x", "y", "width", "height"}.issubset(value):
                self._last_runtime_coordinates[block_id] = value

    def _format_run_result(self, event):

        trace = event.get("trace") or []
        steps = FunctionRunner.format_trace(trace)

        outcome = event.get("outcome", "finished")
        value = event.get("value")

        if not trace:
            return f"{outcome}: {value or event.get('variables', {})}"

        return f"{outcome} [{steps}]"
