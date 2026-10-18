"""Debug screen: observe the running runtime with pacing controls.

Only active while the runtime runs. The Debug Mode switch enables the
pacing/observation hooks; when off, the runtime behaves exactly like in
production and no extra work happens in the background.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSlider,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from services.debug_pacing_service import DebugPacingService
from services.function_service import FunctionService
from services.logging_service import get_logger
from ui.i18n.translator import t
from ui.views.function_editor_view import FunctionCanvas
from ui.widgets.app_header_bar import AppHeaderBar
from ui.widgets.toggle_switch import ToggleSwitch


class DebugView(QWidget):
    """Runtime observation screen with a global Debug Mode switch."""

    def __init__(self, runtime_service=None, parent=None):
        super().__init__(parent)
        self.runtime_service = runtime_service
        self.supervisor = None
        self.logger = get_logger("function")

        layout = QVBoxLayout(self)

        header = QHBoxLayout()
        header.addWidget(QLabel(t("debug.title")))
        header.addStretch(1)

        self.debug_switch = ToggleSwitch(active_color="#B91C1C")
        self.debug_switch.setToolTip(t("debug.mode_tooltip"))
        self.debug_switch.toggled.connect(self._on_debug_toggled)
        header.addWidget(QLabel(t("debug.mode")))
        header.addWidget(self.debug_switch)
        layout.addLayout(header)

        self.status_label = QLabel(t("debug.inactive"))
        self.status_label.setStyleSheet("color: #AAB4C4; padding: 4px;")
        layout.addWidget(self.status_label)

        controls = QHBoxLayout()
        controls.addWidget(QLabel(t("debug.speed")))
        self.speed_slider = QSlider(Qt.Horizontal)
        self.speed_slider.setRange(1, 10)
        self.speed_slider.setValue(1)
        self.speed_slider.setEnabled(False)
        self.speed_value_label = QLabel("1×")
        self.speed_slider.valueChanged.connect(self._on_speed_changed)
        controls.addWidget(self.speed_slider, stretch=1)
        controls.addWidget(self.speed_value_label)
        layout.addLayout(controls)

        run_controls = QHBoxLayout()
        self.pause_button = QPushButton(t("debug.pause"))
        self.pause_button.setEnabled(False)
        self.pause_button.clicked.connect(self._on_pause_clicked)
        run_controls.addWidget(self.pause_button)

        self.freeze_button = QPushButton(t("debug.freeze"))
        self.freeze_button.setEnabled(False)
        self.freeze_button.setCheckable(True)
        self.freeze_button.clicked.connect(self._on_freeze_clicked)
        run_controls.addWidget(self.freeze_button)

        self.editor_button = QPushButton(t("debug.open_in_editor"))
        self.editor_button.setEnabled(False)
        self.editor_button.clicked.connect(self._open_in_editor)
        run_controls.addWidget(self.editor_button)

        run_controls.addStretch(1)
        layout.addLayout(run_controls)

        splitter = QSplitter(Qt.Horizontal)

        canvas_panel = QWidget()
        canvas_layout = QVBoxLayout(canvas_panel)
        canvas_layout.setContentsMargins(0, 0, 0, 0)
        self.function_headline = QLabel("")
        self.function_headline.setStyleSheet(
            "font-weight: bold; padding: 4px; color: #E8ECF1;"
        )
        canvas_layout.addWidget(self.function_headline)

        self.canvas = FunctionCanvas()
        self.canvas.setAcceptDrops(False)
        self.canvas.setInteractive(False)
        canvas_layout.addWidget(self.canvas, stretch=1)
        splitter.addWidget(canvas_panel)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.addWidget(QLabel(t("debug.variables")))
        self.variables_table = QTableWidget(0, 2)
        self.variables_table.setHorizontalHeaderLabels([
            t("dependencies.name"),
            t("dependencies.value"),
        ])
        self.variables_table.horizontalHeader().setStretchLastSection(True)
        right_layout.addWidget(self.variables_table, stretch=2)
        right_layout.addWidget(QLabel(t("debug.log")))
        self.log_view = QTextEdit()
        self.log_view.setReadOnly(True)
        right_layout.addWidget(self.log_view, stretch=1)
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 1)
        layout.addWidget(splitter, stretch=1)

        self._active_execution_id = None
        self._active_function_id = None
        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(200)
        self._poll_timer.timeout.connect(self._poll_block_events)
        self._poll_timer.start()

        layout.addStretch(0)

    def _on_debug_toggled(self, checked: bool):
        AppHeaderBar.set_debug_active(checked)
        pacing = DebugPacingService.instance()
        pacing.set_enabled(checked)
        pacing.resume()
        self.speed_slider.setEnabled(checked)
        self.pause_button.setEnabled(checked)
        self.pause_button.setText(t("debug.pause"))
        self.status_label.setText(
            t("debug.active") if checked else t("debug.inactive")
        )
        self.logger.info(
            "Debug Mode Toggled",
            active=checked,
        )

    def _on_speed_changed(self, value: int):
        self.speed_value_label.setText(f"{value}×")
        DebugPacingService.instance().set_factor(value)

    def _on_pause_clicked(self):
        pacing = DebugPacingService.instance()
        if pacing.paused:
            pacing.resume()
            self.pause_button.setText(t("debug.pause"))
        else:
            pacing.pause()
            self.pause_button.setText(t("debug.resume"))

    def _poll_block_events(self):
        """Pull block_started events from the global hub and paint the state."""

        if not DebugPacingService.instance().enabled:
            return

        from services.debug_pacing_service import DebugEventHub

        for event in DebugEventHub.instance().poll():
            if event.get("event") == "function_started":
                self._active_execution_id = event.get("execution_id")
                self._active_function_id = event.get("function_id")
                self._load_function_canvas(event.get("function_id"))
            elif event.get("event") == "block_started":
                function_id = event.get("function_id")
                if function_id and function_id != self._active_function_id:
                    # the canvas follows whichever function the scheduler runs
                    self._active_function_id = function_id
                    self._load_function_canvas(function_id)
                # a call_function step shows the helper's canvas instead
                if event.get("block_type") == "call_function":
                    helper_id = self._called_helper_id(event.get("block_id"))
                    if helper_id and helper_id != self._active_function_id:
                        self._active_function_id = helper_id
                        self._load_function_canvas(helper_id)
                self._highlight_block(event.get("block_id"))
                self._render_variables(event.get("variables") or {})
                self._append_log(event)
            elif event.get("event") in ("function_finished", "function_failed"):
                self._append_log(event)

    def _load_function_canvas(self, function_id):
        """Load the function fresh from disk so edits show on the next run."""

        function = self._find_function(function_id)
        if not function:
            return
        self.function_headline.setText(
            function.name or function.function_id[:8]
        )
        self.canvas.set_layout_positions(function.layout_positions)
        self.canvas.draw_blocks(function.blocks, function.connections)
        self.editor_button.setEnabled(True)
        self.freeze_button.setEnabled(True)

    def _find_function(self, function_id):
        service = FunctionService()
        for game_dir in service.root.iterdir() if service.root.exists() else []:
            if game_dir.is_dir():
                found = service.load(game_dir.name, function_id)
                if found:
                    return found
        return None

    def _called_helper_id(self, block_id):
        """Resolve which helper a call_function block points at."""

        function = self._find_function(self._active_function_id)
        if not function or not block_id:
            return None
        block = next(
            (b for b in function.walk_blocks() if b.block_id == block_id),
            None,
        )
        if block is None or block.block_type != "call_function":
            return None
        return str(block.params.get("function_id") or "") or None

    def _on_freeze_clicked(self, checked):
        """Freeze the running event into the debug loop at priority 500."""

        if not self._active_function_id:
            return
        from scheduler.models import DEBUG_LOOP_PRIORITY, SchedulerService

        scheduler = SchedulerService()
        for game_dir in scheduler.base_path.iterdir() if scheduler.base_path.exists() else []:
            if not game_dir.is_dir():
                continue
            for event in scheduler.list_events(game_dir.name):
                task_ids = {
                    fid
                    for task in event.tasks
                    for fid in task.function_ids
                }
                if self._active_function_id not in task_ids:
                    continue
                event.debug_frozen = checked
                if checked:
                    event.priority = DEBUG_LOOP_PRIORITY
                scheduler.save_event(event)
        self.freeze_button.setText(
            t("debug.unfreeze") if checked else t("debug.freeze")
        )
        self.logger.info(
            "Debug Freeze Toggled",
            function_id=self._active_function_id,
            frozen=checked,
        )

    def _open_in_editor(self):
        if not self._active_function_id:
            return
        window = self.window()
        navigation = getattr(window, "navigation", None)
        editor = getattr(window, "function_editor_view", None)
        if navigation is None or editor is None:
            return
        menu = navigation.menu
        for index in range(menu.count()):
            if menu.item(index).data(Qt.UserRole) == "functions":
                menu.setCurrentRow(index)
                break
        combo = editor.function_combo
        index = combo.findData(self._active_function_id)
        if index >= 0:
            combo.setCurrentIndex(index)
            editor.open_selected_function()

    def _highlight_block(self, block_id):
        if not block_id:
            return
        self.canvas._selected_block_id = block_id
        # show the overlay directly: the scene selection is empty in the
        # read-only debug canvas, so a refresh would hide it again
        for key, overlay in self.canvas._block_highlights.items():
            try:
                overlay.setVisible(key == block_id)
            except RuntimeError:
                continue
        # keep the active block centered so large functions stay visible;
        # the highlight overlay keeps a stable scene position
        overlay = self.canvas._block_highlights.get(block_id)
        if overlay is not None:
            try:
                self.canvas.centerOn(overlay.sceneBoundingRect().center())
            except RuntimeError:
                pass

    def _render_variables(self, variables):
        self.variables_table.setRowCount(len(variables))
        for row, (name, value) in enumerate(sorted(variables.items())):
            self.variables_table.setItem(row, 0, QTableWidgetItem(str(name)))
            self.variables_table.setItem(row, 1, QTableWidgetItem(str(value)))

    def _append_log(self, event):
        block_type = event.get("block_type", "")
        outcome = event.get("outcome", "")
        text = f"{event.get('event')}: {block_type}"
        if outcome:
            text += f" = {outcome}"
        if event.get("error"):
            text += f" ({event['error']})"
        self.log_view.append(text)

    def closeEvent(self, event):
        # never leave the red banner on when the screen goes away
        if self.debug_switch.isChecked():
            self.debug_switch.setChecked(False)
        super().closeEvent(event)
