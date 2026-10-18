"""Dependencies screen: usage inventory with list and graph views.

Shows which functions/tasks/events use variables, helper functions and
library entries. The graph view draws a layered picture (event -> task ->
function -> helper/library) with preview images; the variables tab offers a
guarded cleanup of manually created, never-used variables.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QRectF, Signal
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QGraphicsScene,
    QGraphicsView,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTabWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from services.dependency_service import DependencyService
from services.function_service import FunctionService
from services.game_service import GameService
from services.library_service import LibraryService
from ui.i18n.translator import t
from ui.image_utils import load_pixmap
from ui.widgets.searchable_combo import SearchableComboBox


class _DependencyGraph(QGraphicsView):
    """Layered dependency picture with library preview thumbnails."""

    NODE_W = 190
    NODE_H = 64
    LAYER_GAP = 260
    ROW_GAP = 84

    LAYER_COLORS = {
        "event": QColor("#173A6B"),
        "task": QColor("#303846"),
        "function": QColor("#3D3A50"),
        "library": QColor("#1F4D3A"),
        "missing": QColor("#8B0000"),
    }

    def __init__(self, parent=None):
        self.scene_model = QGraphicsScene()
        super().__init__(self.scene_model, parent)
        self.setRenderHint(QPainter.Antialiasing)
        self.setMinimumHeight(320)
        self.setDragMode(QGraphicsView.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self._zoom = 1.0
        self._filter_key = None
        self._node_items = {}
        self._edge_items = []
        self._edges = []

    def wheelEvent(self, event):
        """Zoom with the mouse wheel, anchored under the cursor."""

        factor = 1.25 if event.angleDelta().y() > 0 else 0.8
        next_zoom = self._zoom * factor
        if not 0.05 <= next_zoom <= 8.0:
            return
        self._zoom = next_zoom
        self.scale(factor, factor)

    def fit_graph(self):
        """Show content at real size; a fit only happens on explicit request."""

        rect = self.scene_model.itemsBoundingRect()
        if rect.isNull():
            return
        self.resetTransform()
        self._zoom = 1.0
        # start at 100% real pixels; scrollbars and wheel zoom do the rest
        self.setSceneRect(rect.adjusted(-60, -60, 60, 60))
        self.centerOn(rect.center())

    def zoom_to_fit(self):
        """Explicitly shrink the whole graph into the current window."""

        rect = self.scene_model.itemsBoundingRect()
        if rect.isNull():
            return
        self.resetTransform()
        self.fitInView(rect.adjusted(-30, -30, 30, 30), Qt.KeepAspectRatio)
        self._zoom = self.transform().m11()

    def draw_graph(self, nodes, edges, images):
        """nodes: list of {key, label, kind, layer}; edges: (from_key, to_key)."""

        self.scene_model.clear()
        self._node_items = {}
        self._edge_items = []
        self._edges = list(edges)
        self._filter_key = None
        by_layer = {}
        for node in nodes:
            by_layer.setdefault(node["layer"], []).append(node)

        positions = {}
        for layer, layer_nodes in sorted(by_layer.items()):
            for row, node in enumerate(
                sorted(layer_nodes, key=lambda n: n["label"].casefold())
            ):
                positions[node["key"]] = (
                    20 + layer * self.LAYER_GAP,
                    20 + row * self.ROW_GAP,
                )

        for source_key, target_key in edges:
            if source_key not in positions or target_key not in positions:
                continue
            x1, y1 = positions[source_key]
            x2, y2 = positions[target_key]
            start = QRectF(x1, y1, self.NODE_W, self.NODE_H).center()
            end = QRectF(x2, y2, self.NODE_W, self.NODE_H).center()
            path = QPainterPath(start)
            offset = max(60.0, (end.x() - start.x()) * 0.45)
            path.cubicTo(
                start.x() + offset, start.y(),
                end.x() - offset, end.y(),
                end.x(), end.y(),
            )
            edge_item = self.scene_model.addPath(
                path,
                QPen(QColor("#7d8791"), 2),
            )
            self._edge_items.append((source_key, target_key, edge_item))

        for node in nodes:
            x, y = positions[node["key"]]
            rect = self.scene_model.addRect(
                x, y, self.NODE_W, self.NODE_H,
                QPen(QColor("#ff4040"), 3)
                if node["kind"] == "missing"
                else QPen(QColor("#7d8791"), 1),
            )
            rect.setBrush(self.LAYER_COLORS.get(node["kind"], QColor("#333")))
            text = self.scene_model.addText(node["label"])
            text.setDefaultTextColor(Qt.GlobalColor.white)
            text.setPos(x + 8, y + 8)
            text.setTextWidth(self.NODE_W - 16)

            items = [rect, text]
            pixmap = images.get(node["key"])
            if pixmap is not None and not pixmap.isNull():
                thumb = self.scene_model.addPixmap(
                    pixmap.scaled(
                        48, 48,
                        Qt.KeepAspectRatio,
                        Qt.SmoothTransformation,
                    )
                )
                thumb.setPos(x + self.NODE_W - 56, y + self.NODE_H - 56)
                items.append(thumb)
            self._node_items[node["key"]] = items

        if positions:
            self.fit_graph()

    def mousePressEvent(self, event):
        """Click a card to filter to its neighborhood; click again to clear."""

        item = self.itemAt(event.position().toPoint())
        clicked_key = None
        if item is not None:
            for key, items in self._node_items.items():
                if item in items:
                    clicked_key = key
                    break

        if clicked_key is None:
            super().mousePressEvent(event)
            return

        # same card toggles the filter off, a different card replaces it
        self._filter_key = (
            None if self._filter_key == clicked_key else clicked_key
        )
        self._apply_filter()
        event.accept()

    def _apply_filter(self):
        """Show the directed chain: ancestors up to the events and descendants
        down to the images, without pulling in unrelated branches that only
        share a helper or event with the selected card's neighborhood."""

        if self._filter_key is None:
            visible_keys = set(self._node_items)
        else:
            # edges point downstream: event -> task -> function -> library
            downstream = {}
            upstream = {}
            for source_key, target_key in self._edges:
                downstream.setdefault(source_key, []).append(target_key)
                upstream.setdefault(target_key, []).append(source_key)

            visible_keys = {self._filter_key}

            frontier = [self._filter_key]
            while frontier:
                current = frontier.pop()
                for neighbor in upstream.get(current, []):
                    if neighbor not in visible_keys:
                        visible_keys.add(neighbor)
                        frontier.append(neighbor)

            frontier = [self._filter_key]
            while frontier:
                current = frontier.pop()
                for neighbor in downstream.get(current, []):
                    if neighbor not in visible_keys:
                        visible_keys.add(neighbor)
                        frontier.append(neighbor)

        for key, items in self._node_items.items():
            for item in items:
                item.setVisible(key in visible_keys)

        for source_key, target_key, edge_item in self._edge_items:
            edge_item.setVisible(
                source_key in visible_keys and target_key in visible_keys
            )


class DependenciesView(QWidget):
    """Standalone screen listing usage of variables, functions and library."""

    open_function_requested = Signal(str, str)
    open_task_requested = Signal(str, str, str)

    def __init__(self, game_id: str = "", parent=None):
        super().__init__(parent)
        self.game_id = game_id
        self.dependency_service = DependencyService()
        self.function_service = FunctionService()
        self.library_service = LibraryService()
        self.game_service = GameService()

        layout = QVBoxLayout(self)

        header = QHBoxLayout()
        header.addWidget(QLabel(t("dependencies.game")))
        self.game_combo = SearchableComboBox()
        for game in sorted(
            self.game_service.load(),
            key=lambda g: str(g.get("display_name", g.get("id", ""))).casefold(),
        ):
            self.game_combo.addItem(
                game.get("display_name", game.get("id", "")),
                game.get("id", ""),
            )
        if game_id:
            index = self.game_combo.findData(game_id)
            if index >= 0:
                self.game_combo.setCurrentIndex(index)
        self.game_combo.currentIndexChanged.connect(self.refresh)
        header.addWidget(self.game_combo, stretch=1)

        self.refresh_button = QPushButton(t("scheduler.refresh"))
        self.refresh_button.clicked.connect(self.refresh)
        header.addWidget(self.refresh_button)
        layout.addLayout(header)

        self.tabs = QTabWidget()
        self.variables_tab = QWidget()
        self.functions_tab = QWidget()
        self.library_tab = QWidget()
        self.graph_tab = QWidget()
        self.missing_tab = QWidget()
        self.tabs.addTab(self.variables_tab, t("dependencies.variables"))
        self.tabs.addTab(self.functions_tab, t("dependencies.unused_functions"))
        self.tabs.addTab(self.library_tab, t("dependencies.unused_library"))
        self.tabs.addTab(
            self.missing_tab,
            t("dependencies.missing_functions").format(count=0),
        )
        self.tabs.addTab(self.graph_tab, t("dependencies.graph"))
        layout.addWidget(self.tabs, stretch=1)

        missing_layout = QVBoxLayout(self.missing_tab)
        missing_hint = QLabel(t("dependencies.missing_hint"))
        missing_hint.setWordWrap(True)
        missing_layout.addWidget(missing_hint)
        self.missing_tree = QTreeWidget()
        self.missing_tree.setHeaderLabels([
            t("dependencies.name"),
            t("dependencies.used_by"),
        ])
        self.missing_tree.setColumnWidth(0, 560)
        self.missing_tree.itemDoubleClicked.connect(self._open_missing_user)
        self.missing_tree.currentItemChanged.connect(
            self._update_missing_open_button
        )
        missing_layout.addWidget(self.missing_tree, stretch=1)
        missing_buttons = QHBoxLayout()
        missing_buttons.addStretch(1)
        self.missing_open_button = QPushButton(t("dependencies.open_function"))
        self.missing_open_button.setEnabled(False)
        self.missing_open_button.clicked.connect(
            lambda: self._open_missing_user(self.missing_tree.currentItem())
        )
        missing_buttons.addWidget(self.missing_open_button)
        missing_layout.addLayout(missing_buttons)

        variables_layout = QVBoxLayout(self.variables_tab)
        self.variables_tree = QTreeWidget()
        self.variables_tree.setHeaderLabels([
            t("dependencies.name"),
            t("dependencies.kind"),
            t("dependencies.type"),
            t("dependencies.used_by"),
        ])
        self.variables_tree.setColumnWidth(0, 220)
        self.variables_tree.itemSelectionChanged.connect(self._sync_delete_state)
        variables_layout.addWidget(self.variables_tree, stretch=1)

        actions = QHBoxLayout()
        self.create_variable_button = QPushButton(t("variables.create"))
        self.create_variable_button.clicked.connect(self._create_variable)
        actions.addWidget(self.create_variable_button)
        self.edit_variable_button = QPushButton(t("dependencies.edit_variable"))
        self.edit_variable_button.setEnabled(False)
        self.edit_variable_button.clicked.connect(self._edit_selected_variable)
        actions.addWidget(self.edit_variable_button)
        self.delete_button = QPushButton(t("dependencies.delete_unused"))
        self.delete_button.setEnabled(False)
        self.delete_button.clicked.connect(self._delete_selected_unused)
        actions.addWidget(self.delete_button)
        actions.addStretch(1)
        variables_layout.addLayout(actions)

        functions_layout = QVBoxLayout(self.functions_tab)
        self.functions_tree = QTreeWidget()
        self.functions_tree.setHeaderLabels([
            t("dependencies.name"),
            t("dependencies.kind"),
            t("dependencies.used_by"),
        ])
        self.functions_tree.setColumnWidth(0, 260)
        self.functions_tree.itemSelectionChanged.connect(self._sync_delete_state)
        functions_layout.addWidget(self.functions_tree, stretch=1)
        functions_actions = QHBoxLayout()
        self.delete_function_button = QPushButton(t("dependencies.delete_function"))
        self.delete_function_button.setEnabled(False)
        self.delete_function_button.clicked.connect(self._delete_selected_function)
        functions_actions.addWidget(self.delete_function_button)
        functions_actions.addStretch(1)
        functions_layout.addLayout(functions_actions)

        library_layout = QVBoxLayout(self.library_tab)
        self.library_tree = QTreeWidget()
        self.library_tree.setHeaderLabels([
            t("dependencies.name"),
            t("dependencies.preview"),
            t("dependencies.used_by"),
        ])
        self.library_tree.setColumnWidth(0, 260)
        self.library_tree.setIconSize(self.library_tree.iconSize())
        self.library_tree.itemSelectionChanged.connect(self._sync_delete_state)
        library_layout.addWidget(self.library_tree, stretch=1)
        library_actions = QHBoxLayout()
        self.delete_library_button = QPushButton(t("dependencies.delete_library"))
        self.delete_library_button.setEnabled(False)
        self.delete_library_button.clicked.connect(self._delete_selected_library_entry)
        library_actions.addWidget(self.delete_library_button)
        library_actions.addStretch(1)
        library_layout.addLayout(library_actions)

        graph_layout = QVBoxLayout(self.graph_tab)
        self.graph = _DependencyGraph(self.graph_tab)
        graph_controls = QHBoxLayout()
        zoom_in_button = QPushButton("+")
        zoom_in_button.setFixedWidth(36)
        zoom_in_button.setToolTip(t("dependencies.zoom_in"))
        zoom_in_button.clicked.connect(lambda: self._zoom_by(1.25))
        zoom_out_button = QPushButton("−")
        zoom_out_button.setFixedWidth(36)
        zoom_out_button.setToolTip(t("dependencies.zoom_out"))
        zoom_out_button.clicked.connect(lambda: self._zoom_by(0.8))
        fit_button = QPushButton(t("dependencies.zoom_fit"))
        fit_button.clicked.connect(self.graph.zoom_to_fit)
        graph_controls.addWidget(zoom_in_button)
        graph_controls.addWidget(zoom_out_button)
        graph_controls.addWidget(fit_button)
        graph_controls.addStretch(1)
        graph_layout.addLayout(graph_controls)
        graph_layout.addWidget(self.graph, stretch=1)

        self.refresh()

    def _zoom_by(self, factor):
        next_zoom = self.graph._zoom * factor
        if not 0.05 <= next_zoom <= 8.0:
            return
        self.graph._zoom = next_zoom
        self.graph.scale(factor, factor)

    def refresh(self):
        game_id = self.game_combo.currentData() or ""
        self.create_variable_button.setEnabled(bool(game_id))
        if not game_id:
            return
        self._inventory = self.dependency_service.build_inventory(game_id)
        self._audit = self.dependency_service.variable_audit(game_id)
        self._render_variables()
        self._render_unused_functions()
        self._render_unused_library()
        self._render_missing_functions()
        self._render_graph()

    # -- variables tab ----------------------------------------------------------

    def _render_variables(self):
        self.variables_tree.clear()
        groups = (
            ("protected", t("dependencies.protected"), "#173A6B"),
            ("used", t("dependencies.used"), "#1F4D3A"),
            ("unused", t("dependencies.unused"), "#5B3A29"),
        )
        for key, label, color in groups:
            records = self._audit[key]
            if not records:
                continue
            header = QTreeWidgetItem([f"{label} ({len(records)})", "", "", ""])
            header.setData(0, Qt.UserRole, {"group": key})
            for column in range(4):
                header.setBackground(column, QColor(color))
                header.setForeground(column, QColor("#FFFFFF"))
            self.variables_tree.addTopLevelItem(header)
            for record in records:
                users = ", ".join(user["name"] for user in record["users"])
                reason = record.get("protected_reason") or ""
                if reason:
                    users = (
                        f"{t(f'dependencies.reason_{reason}')}"
                        + (f" · {users}" if users else "")
                    )
                item = QTreeWidgetItem([
                    record["name"],
                    record["kind"],
                    record["value_type"],
                    users or "-",
                ])
                item.setData(0, Qt.UserRole, {
                    "group": key,
                    "name": record["name"],
                })
                header.addChild(item)
            header.setExpanded(key == "unused")

    def _sync_delete_state(self):
        item = self.variables_tree.currentItem()
        data = item.data(0, Qt.UserRole) if item else None
        self.delete_button.setEnabled(
            bool(data) and data.get("group") == "unused"
        )
        self.edit_variable_button.setEnabled(
            bool(data) and bool(data.get("name"))
        )
        function_item = self.functions_tree.currentItem()
        function_data = (
            function_item.data(0, Qt.UserRole) if function_item else None
        )
        self.delete_function_button.setEnabled(
            bool(function_data) and bool(function_data.get("function_id"))
        )
        library_item = self.library_tree.currentItem()
        library_data = library_item.data(0, Qt.UserRole) if library_item else None
        self.delete_library_button.setEnabled(
            bool(library_data) and bool(library_data.get("entry_id"))
        )

    def _delete_selected_unused(self):
        item = self.variables_tree.currentItem()
        data = item.data(0, Qt.UserRole) if item else None
        if not data or data.get("group") != "unused":
            return
        name = data["name"]
        answer = QMessageBox.question(
            self,
            t("dependencies.delete_unused"),
            t("dependencies.delete_unused_confirm").format(name=name),
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        if self.dependency_service.delete_unused_variable(
            self.game_combo.currentData() or "",
            name,
        ):
            self.refresh()

    def _create_variable(self):
        game_id = self.game_combo.currentData() or ""
        if not game_id:
            return
        from ui.widgets.variable_dialog import VariableCreateDialog

        dialog = VariableCreateDialog(
            game_id,
            self.dependency_service.variable_service,
            parent=self,
        )
        if dialog.exec() == QDialog.Accepted:
            self.refresh()

    def _edit_selected_variable(self):
        item = self.variables_tree.currentItem()
        data = item.data(0, Qt.UserRole) if item else None
        if not data or not data.get("name"):
            return
        from ui.widgets.variable_dialog import VariableCreateDialog

        dialog = VariableCreateDialog(
            self.game_combo.currentData() or "",
            self.dependency_service.variable_service,
            parent=self,
            edit_name=data["name"],
        )
        if dialog.exec() == QDialog.Accepted:
            self.refresh()

    def _delete_selected_function(self):
        item = self.functions_tree.currentItem()
        data = item.data(0, Qt.UserRole) if item else None
        if not data or not data.get("function_id"):
            return
        answer = QMessageBox.question(
            self,
            t("dependencies.delete_function"),
            t("dependencies.delete_function_confirm").format(name=data["name"]),
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        if self.dependency_service.delete_unused_function(
            self.game_combo.currentData() or "",
            data["function_id"],
        ):
            self.refresh()

    def _delete_selected_library_entry(self):
        item = self.library_tree.currentItem()
        data = item.data(0, Qt.UserRole) if item else None
        if not data or not data.get("entry_id"):
            return
        answer = QMessageBox.question(
            self,
            t("dependencies.delete_library"),
            t("dependencies.delete_library_confirm").format(name=data["name"]),
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        if self.dependency_service.delete_unused_library_entry(
            self.game_combo.currentData() or "",
            data["entry_id"],
        ):
            self.refresh()

    # -- unused functions / library tabs ------------------------------------------

    def _render_unused_functions(self):
        """Functions referenced by no task and no caller are cleanup candidates."""

        self.functions_tree.clear()
        inventory = self._inventory
        game_id = self.game_combo.currentData() or ""
        for function in self.function_service.list_functions(game_id):
            users = inventory["functions"].get(function.function_id, [])
            if users:
                continue
            item = QTreeWidgetItem([
                function.name or function.function_id[:8],
                function.kind,
                "-",
            ])
            item.setData(0, Qt.UserRole, {
                "function_id": function.function_id,
                "name": function.name or function.function_id[:8],
            })
            self.functions_tree.addTopLevelItem(item)

    def _render_unused_library(self):
        """Library entries no saved function references, with preview images."""

        from PySide6.QtGui import QIcon

        self.library_tree.clear()
        inventory = self._inventory
        for entry_file in self.library_service.list_entries():
            entry_id = entry_file.parent.name
            if inventory["library"].get(entry_id):
                continue
            entry = self.library_service.load(entry_id) or {}
            item = QTreeWidgetItem([
                entry.get("name") or entry_id[:8],
                "",
                "-",
            ])
            item.setData(0, Qt.UserRole, {
                "entry_id": entry_id,
                "name": entry.get("name") or entry_id[:8],
            })
            samples = entry.get("accepted_samples") or []
            image_path = samples[0].get("crop") if samples else None
            image_path = image_path or entry.get("pattern", {}).get("reference_path")
            if not image_path:
                candidate = entry_file.parent / "pattern_reference.png"
                image_path = str(candidate) if candidate.exists() else None
            if image_path:
                pixmap = load_pixmap(image_path)
                if not pixmap.isNull():
                    item.setIcon(1, QIcon(pixmap))
                    item.setText(1, "")
            self.library_tree.addTopLevelItem(item)

    # -- missing functions tab ----------------------------------------------------

    @staticmethod
    def _missing_label(function_id, entry):
        name = (entry or {}).get("label")
        if not name:
            return t("dependencies.missing_node_unknown")
        return t("dependencies.missing_node").format(name=name)

    @staticmethod
    def _missing_user_text(user):
        if user.get("kind") == "task":
            name = user.get("profile_name")
            profile = f"'{name}'" if name else t("dependencies.missing_no_profile")
            if user.get("profile_enabled") is False:
                profile = t("dependencies.profile_disabled").format(profile=profile)
            return t("dependencies.missing_user_task").format(
                profile=profile,
                event=user.get("event_name", ""),
                task=user.get("name", ""),
            )
        return t("dependencies.missing_user_function").format(
            function=user.get("name", ""),
        )

    def _render_missing_functions(self):
        """Referenced but deleted functions; every user must be re-linked.

        The GUI shows names only; the technical IDs go to the log together
        with an open target (services.reference_log).
        """

        from services.reference_log import (
            log_missing_function_reference,
            open_target_for_user,
        )

        self.missing_tree.clear()
        missing = self._inventory.get("missing_functions", {})
        game_id = self.game_combo.currentData() or ""
        red = QColor("#d00000")
        for function_id, entry in sorted(
            missing.items(),
            key=lambda item: (item[1].get("label") or item[0]).casefold(),
        ):
            parent = QTreeWidgetItem([
                self._missing_label(function_id, entry),
                str(len(entry.get("users", []))),
            ])
            parent.setToolTip(0, t("dependencies.missing_tooltip"))
            for column in range(2):
                parent.setForeground(column, red)
            for user in entry.get("users", []):
                child = QTreeWidgetItem([self._missing_user_text(user), ""])
                child.setForeground(0, red)
                child.setData(0, Qt.UserRole, open_target_for_user(game_id, user))
                child.setToolTip(
                    0,
                    t(
                        "dependencies.open_task_tooltip"
                        if user.get("kind") == "task"
                        else "dependencies.open_function_tooltip"
                    ),
                )
                parent.addChild(child)
                log_missing_function_reference(game_id, function_id, user)
            self.missing_tree.addTopLevelItem(parent)
            parent.setExpanded(True)
        self._update_missing_open_button()

        index = self.tabs.indexOf(self.missing_tab)
        self.tabs.setTabText(
            index,
            t("dependencies.missing_functions").format(count=len(missing)),
        )
        self.tabs.tabBar().setTabTextColor(
            index, red if missing else self.tabs.tabBar().palette().text().color()
        )

    def _update_missing_open_button(self, *_args):
        item = self.missing_tree.currentItem()
        self.missing_open_button.setEnabled(
            bool(item and item.data(0, Qt.UserRole))
        )

    def _open_missing_user(self, item, _column=0):
        target = item.data(0, Qt.UserRole) if item else None
        if not target:
            return
        game_id = target.get("game_id") or self.game_combo.currentData() or ""
        if target.get("kind") == "task":
            self.open_task_requested.emit(
                game_id, target.get("event_id", ""), target.get("task_id", "")
            )
        elif target.get("function_id"):
            self.open_function_requested.emit(game_id, target["function_id"])

    # -- graph tab ---------------------------------------------------------------

    def _render_graph(self):
        inventory = self._inventory
        names = inventory["function_names"]

        # overview mode: same-named nodes of the same kind merge into one card
        # ("does any profile use this"), so profile duplicates never appear
        raw_nodes = []
        raw_edges = []
        images = {}

        missing = inventory.get("missing_functions", {})
        for function_id, users in inventory["functions"].items():
            if function_id in missing:
                label = self._missing_label(function_id, missing[function_id])
                kind = "missing"
            else:
                label = names.get(function_id, function_id[:8])
                kind = "function"
            raw_nodes.append({
                "key": f"function:{function_id}",
                "label": label,
                "kind": kind,
                "layer": 2,
            })
            for user in users:
                if user["kind"] == "function":
                    raw_nodes.append({
                        "key": f"caller:{user['id']}",
                        "label": user["name"],
                        "kind": "function",
                        "layer": 1,
                    })
                    raw_edges.append((f"caller:{user['id']}", f"function:{function_id}"))
                elif user["kind"] == "task":
                    raw_nodes.append({
                        "key": f"task:{user['id']}",
                        "label": user["name"],
                        "kind": "task",
                        "layer": 1,
                    })
                    raw_nodes.append({
                        "key": f"event:{user['event_id']}",
                        "label": user["event_name"],
                        "kind": "event",
                        "layer": 0,
                    })
                    raw_edges.append((f"task:{user['id']}", f"function:{function_id}"))
                    raw_edges.append((f"event:{user['event_id']}", f"task:{user['id']}"))

        for entry_id, users in inventory["library"].items():
            entry = self.library_service.load(entry_id) or {}
            label = entry.get("name") or entry_id[:8]
            raw_nodes.append({
                "key": f"library:{entry_id}",
                "label": label,
                "kind": "library",
                "layer": 3,
            })
            samples = entry.get("accepted_samples") or []
            image_path = samples[0].get("crop") if samples else None
            image_path = image_path or entry.get("pattern", {}).get("reference_path")
            if image_path:
                images[f"library:{entry_id}"] = load_pixmap(image_path)
            for user in users:
                raw_nodes.append({
                    "key": f"function:{user['id']}",
                    "label": names.get(user["id"], user["name"]),
                    "kind": "function",
                    "layer": 2,
                })
                raw_edges.append((f"function:{user['id']}", f"library:{entry_id}"))

        # merge nodes by (kind, label); ids only differ per profile/instance
        merge_key = {}
        merged_nodes = {}
        for node in raw_nodes:
            signature = (node["kind"], node["label"].casefold())
            if signature in merged_nodes:
                merge_key[node["key"]] = merged_nodes[signature]["key"]
                continue
            merged_nodes[signature] = node
            merge_key[node["key"]] = node["key"]

        merged_images = {}
        for node_key, image in images.items():
            target = merge_key.get(node_key, node_key)
            merged_images.setdefault(target, image)

        merged_edges = set()
        for source_key, target_key in raw_edges:
            source = merge_key.get(source_key, source_key)
            target = merge_key.get(target_key, target_key)
            if source != target:
                merged_edges.add((source, target))

        self.graph.draw_graph(
            list(merged_nodes.values()),
            list(merged_edges),
            merged_images,
        )
