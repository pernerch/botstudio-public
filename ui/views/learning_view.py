

from pathlib import Path

import cv2
import numpy as np

from PySide6.QtWidgets import (
    QWidget,
    QHBoxLayout,
    QVBoxLayout,
    QPushButton,
    QScrollArea,
    QLabel,
    QComboBox,
    QCompleter,
    QMessageBox,
    QSplitter,
)
from PySide6.QtGui import QIcon

from PySide6.QtCore import (
    QRect,
    QSize,
    Qt,
    Signal,
)

from ui.i18n.translator import t
from ui.image_utils import load_pixmap

from ui.widgets.screenshot_canvas import (
    ScreenshotCanvas,
)

from ui.widgets.properties_panel import (
    PropertiesPanel,
)

from ui.widgets.profile_emulator_bar import (
    ProfileEmulatorBar,
)
from ui.widgets.searchable_combo import SearchableComboBox

from services.profile_service import (
    ProfileService,
)

from services.screen_service import (
    ScreenService,
)

from services.learning_verify_service import (
    LearningVerifyService,
)


from services.learning_verify_runner import (
    LearningVerifyRunner,
)

from models.canvas_region import CanvasRegion

from services.learning_teach_service import (
    LearningTeachService,
)

from services.logging_service import get_logger
from services.opencv_service import OpenCVService

class LearningView(QWidget):

    teach_completed = Signal(object)

    def __init__(
        self,
        parent=None,
    ):
        super().__init__(parent)

        self.profile_service = ProfileService()

        self.screen_service = ScreenService()

        self.verify_service = LearningVerifyService()

        self.verify_runner = LearningVerifyRunner(
            self.verify_service,
            self.screen_service,
        )

        self.teach_service = LearningTeachService()
        self.opencv_service = OpenCVService()
        self.logger = get_logger("learning")
        
        self.current_screenshot = None
        self.current_crop_path = None
        self.selected_library_entry_id = ""
        self.edit_mode = False
        self.edit_entry = None
        self._loading_edit_entry = False

        self.verify_bundle = None

        self.verify_results = []

        self.accepted_samples = []
        self.rejected_samples = []

        root_layout = QHBoxLayout(
            self
        )

        splitter = QSplitter(
            Qt.Horizontal
        )

        # --------------------------------
        # LEFT
        # --------------------------------

        left_panel = QWidget()

        left_layout = QVBoxLayout(
            left_panel
        )

        left_layout.addWidget(
            QLabel(
                t(
                    "learning.profile"
                    )
            )
        )

        self.profile_combo = SearchableComboBox()
        self.profile_combo.addItem("", None)

        profiles = (
            # live capture needs every profile of the game, not only enabled ones
            self.profile_service
            .load_all()
        )

        for profile in profiles:

            text = (
                f"[{profile.get('game', '')}] "
                f"{profile.get('name', '')} "
                f"(E{profile.get('emulator_instance', 0)} "
                f"P{profile.get('priority', 0)})"
            )

            self.profile_combo.addItem(
                text,
                profile,
            )

        left_layout.addWidget(
            self.profile_combo
        )

        self.profile_emulator_bar = ProfileEmulatorBar()
        left_layout.addWidget(
            self.profile_emulator_bar
        )

        self.btn_capture = (
            QPushButton(
                t(
                    "learning.capture"
                    )
            )
        )

        left_layout.addWidget(
            self.btn_capture
        )

        self.btn_boundary = (
            QPushButton(
                t(
                    "learning.search_boundary"
                    )
            )
        )

        left_layout.addWidget(
            self.btn_boundary
        )

        self.btn_detection = (
            QPushButton(
                t(
                    "learning.detection_pattern"
                    )
            )
        )

        self.btn_color_trigger = (
            QPushButton(
                t(
                    "learning.color_trigger"
                    )
            )
        )

        self.btn_delete = (
            QPushButton(
                t(
                    "learning.delete_region"
                    )
            )
        )

        left_layout.addWidget(
            self.btn_detection
        )

        left_layout.addWidget(
            self.btn_color_trigger
        )

        left_layout.addWidget(
            self.btn_delete
        )

        left_layout.addWidget(QLabel("Edit existing library item"))
        self.library_combo = SearchableComboBox()
        self.library_combo.setEditable(True)
        self.library_combo.setInsertPolicy(QComboBox.NoInsert)
        self.library_combo.addItem("Select learned item", "")
        self._populate_library_combo()
        completer = self.library_combo.completer()
        completer.setCaseSensitivity(Qt.CaseInsensitive)
        completer.setFilterMode(Qt.MatchContains)
        completer.setCompletionMode(QCompleter.PopupCompletion)
        left_layout.addWidget(self.library_combo)
        self.btn_edit_existing = QPushButton("Edit existing item")
        self.btn_edit_existing.setEnabled(False)
        left_layout.addWidget(self.btn_edit_existing)

        left_layout.addWidget(QLabel("Unused library items (no function link)"))
        self.orphan_combo = SearchableComboBox()
        self.orphan_combo.addItem("Select unused item", "")
        left_layout.addWidget(self.orphan_combo)

        self.orphan_preview = QLabel("No preview")
        self.orphan_preview.setMinimumHeight(100)
        self.orphan_preview.setAlignment(Qt.AlignCenter)
        self.orphan_preview.setStyleSheet("border: 1px solid #555; background: #111;")
        left_layout.addWidget(self.orphan_preview)

        self.btn_delete_orphan = QPushButton("Delete unused item")
        self.btn_delete_orphan.setEnabled(False)
        left_layout.addWidget(self.btn_delete_orphan)
        self._populate_orphan_combo()

        left_layout.addStretch()

        # --------------------------------
        # CENTER
        # --------------------------------

        self.canvas = (
            ScreenshotCanvas()
        )

        self.scroll_area = (
            QScrollArea()
        )

        self.scroll_area.setWidget(
            self.canvas
        )

        self.scroll_area.setWidgetResizable(False)
        self.scroll_area.setHorizontalScrollBarPolicy(
            Qt.ScrollBarAsNeeded
        )
        self.scroll_area.setVerticalScrollBarPolicy(
            Qt.ScrollBarAsNeeded
        )

        # --------------------------------
        # RIGHT
        # --------------------------------

        self.properties = (
            PropertiesPanel()
        )

        properties_scroll = (
            QScrollArea()
        )

        properties_scroll.setWidget(
            self.properties
        )

        properties_scroll.setWidgetResizable(
            True
        )

        # --------------------------------
        # LAYOUT
        # --------------------------------

        splitter.addWidget(
            left_panel
        )

        splitter.addWidget(
            self.scroll_area
        )

        splitter.addWidget(
            properties_scroll
        )

        splitter.setStretchFactor(
            0,
            2,
        )

        splitter.setStretchFactor(
            1,
            8,
        )

        splitter.setStretchFactor(
            2,
            3,
        )

        root_layout.addWidget(
            splitter
        )

        splitter.setSizes(
            [
                250,
                1000,
                350,
            ]
        )

       # self.capture_screen()
        self._connect_events()

    #
    # --------------------------------
    # Events
    # --------------------------------
    #

    def _connect_events(
        self,
    ):

        self.btn_capture.clicked.connect(
            self.capture_screen
        )

        self.btn_boundary.clicked.connect(
            lambda:
            self.canvas.set_region_type(
                "boundary"
            )
        )

        self.btn_detection.clicked.connect(
            self._start_detection
        )

        self.btn_color_trigger.clicked.connect(
            self._start_color_trigger
        )

        self.btn_delete.clicked.connect(
            self._delete_selected_region
        )

        self.canvas.region_selected.connect(
            self._on_region_selected
        )

        self.canvas.color_picked.connect(
            self._on_color_picked
        )

        self.properties.btn_pick_color.clicked.connect(
            lambda: self.canvas.set_color_pick_mode(True)
        )

        self.canvas.region_updated.connect(
            self._on_region_updated
        )

        self.properties.btn_verify.clicked.connect(
            self.run_verify
        )

        self.properties.btn_teach.clicked.connect(
            self.teach_pattern
        )
        self.properties.label.editingFinished.connect(
            self._finish_name_edit
        )
        self.library_combo.currentIndexChanged.connect(
            self._select_library_entry
        )
        self.btn_edit_existing.clicked.connect(
            self._enter_edit_mode
        )
        self.orphan_combo.currentIndexChanged.connect(
            self._select_orphan_entry
        )
        self.btn_delete_orphan.clicked.connect(
            self._delete_orphan_entry
        )

        self.profile_combo.currentIndexChanged.connect(
            self._invalidate_verify
        )

        self.profile_combo.currentIndexChanged.connect(
            self._capture_if_profile_selected
        )

        self.profile_combo.currentIndexChanged.connect(
            self._sync_profile_emulator_bar
        )
        self._sync_profile_emulator_bar()

    def refresh_library_items(self):
        selected_library_id = self.library_combo.currentData() or ""
        selected_orphan_id = self.orphan_combo.currentData() or ""

        self.library_combo.blockSignals(True)
        self.library_combo.clear()
        self.library_combo.addItem("Select learned item", "")
        self._populate_library_combo()
        library_index = self.library_combo.findData(selected_library_id)
        self.library_combo.setCurrentIndex(library_index if library_index >= 0 else 0)
        self.library_combo.blockSignals(False)
        self.selected_library_entry_id = self.library_combo.currentData() or ""
        self.btn_edit_existing.setEnabled(bool(self.selected_library_entry_id))

        self._populate_orphan_combo()
        orphan_index = self.orphan_combo.findData(selected_orphan_id)
        self.orphan_combo.setCurrentIndex(orphan_index if orphan_index >= 0 else 0)
        self._select_orphan_entry()

    def _populate_library_combo(self):
        service = self.teach_service.library_service
        rows = []
        for entry_file in service.list_entries():
            entry_id = entry_file.parent.name
            entry = service.load(entry_id) or {}
            name = entry.get("name") or entry_id
            rows.append((name, entry_id, entry, entry_file))

        for name, entry_id, entry, entry_file in sorted(rows, key=lambda row: row[0].casefold()):
            samples = entry.get("accepted_samples") or []
            image_path = samples[0].get("crop") if samples else None
            image_path = image_path or entry.get("pattern", {}).get("reference_path")
            if not image_path:
                image_path = str(entry_file.parent / "pattern_reference.png")
            pixmap = load_pixmap(image_path, QSize(48, 48))
            self.library_combo.addItem(
                QIcon(pixmap) if not pixmap.isNull() else QIcon(),
                name,
                entry_id,
            )

    def _select_library_entry(self):
        if self.edit_mode:
            self._finish_edit_mode()
        self.selected_library_entry_id = self.library_combo.currentData() or ""
        self.btn_edit_existing.setEnabled(bool(self.selected_library_entry_id))

    def set_library_entry(self, entry_id):
        index = self.library_combo.findData(entry_id)
        if index >= 0:
            self.library_combo.setCurrentIndex(index)

    def _orphan_entry_preview_path(self, entry):
        samples = entry.get("accepted_samples") or []
        image_path = samples[0].get("crop") if samples else None
        return image_path or entry.get("pattern", {}).get("reference_path")

    def _populate_orphan_combo(self):
        service = self.teach_service.library_service
        self.orphan_combo.blockSignals(True)
        self.orphan_combo.clear()
        self.orphan_combo.addItem("Select unused item", "")

        rows = []
        for entry_file in service.list_entries():
            entry_id = entry_file.parent.name
            entry = service.load(entry_id) or {}
            if entry.get("used_by"):
                continue
            rows.append((entry.get("name") or entry_id, entry_id, entry))

        for name, entry_id, entry in sorted(rows, key=lambda row: row[0].casefold()):
            image_path = self._orphan_entry_preview_path(entry)
            pixmap = load_pixmap(image_path, QSize(48, 48))
            self.orphan_combo.addItem(
                QIcon(pixmap) if not pixmap.isNull() else QIcon(),
                name,
                entry_id,
            )
        self.orphan_combo.blockSignals(False)
        self._select_orphan_entry()

    def _select_orphan_entry(self):
        entry_id = self.orphan_combo.currentData() or ""
        self.btn_delete_orphan.setEnabled(bool(entry_id))

        if not entry_id:
            self.orphan_preview.clear()
            self.orphan_preview.setText("No preview")
            return

        entry = self.teach_service.library_service.load(entry_id) or {}
        image_path = self._orphan_entry_preview_path(entry)
        pixmap = load_pixmap(image_path, QSize(440, 240))
        if pixmap.isNull():
            self.orphan_preview.clear()
            self.orphan_preview.setText("No preview")
            return

        self.orphan_preview.setPixmap(
            pixmap.scaled(
                220,
                120,
                Qt.KeepAspectRatio,
                Qt.SmoothTransformation,
            )
        )

    def _delete_orphan_entry(self):
        entry_id = self.orphan_combo.currentData() or ""
        if not entry_id:
            return

        entry = self.teach_service.library_service.load(entry_id) or {}
        # manual decision required; no automatic cleanup of learned images
        answer = QMessageBox.question(
            self,
            "Delete unused item",
            f"Delete '{entry.get('name') or entry_id}' permanently?\n"
            "This item is not linked to any function.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return

        if not self.teach_service.library_service.delete_entry(entry_id):
            return

        self.logger.info("Library Entry Deleted", entry_id=entry_id, reason="unused")

        if self.edit_entry and self.edit_entry.entry_id == entry_id:
            self._finish_edit_mode()

        self._reload_library_combo()
        self._populate_orphan_combo()

    def _entry_region(self, data, region_type, color):
        return CanvasRegion(
            region_id=f"{self.selected_library_entry_id}:{region_type}",
            region_type=region_type,
            x=int(data.get("x", 0)),
            y=int(data.get("y", 0)),
            width=int(data.get("width", 1)),
            height=int(data.get("height", 1)),
            color=color,
            metadata=dict(data.get("metadata", {})),
        )

    def _enter_edit_mode(self):
        entry_id = self.selected_library_entry_id
        entry = self.teach_service.library_service.load_model(entry_id)
        if not entry:
            return

        self.edit_mode = True
        self.edit_entry = entry
        self.verify_bundle = None
        self.verify_results = []

        boundary = self._entry_region(
            entry.boundary,
            "boundary",
            "#ffff00",
        )
        pattern = self._entry_region(
            entry.pattern,
            entry.pattern.get("type", "detection"),
            "#ff4444",
        )

        image_path = None
        if entry.accepted_samples:
            image_path = entry.accepted_samples[0].get("source")
        image_path = image_path or self.current_screenshot
        if image_path:
            self.current_screenshot = image_path
            self.canvas.load_image(image_path)

        self._loading_edit_entry = True
        self.properties.label.setText(entry.name)
        self._loading_edit_entry = False
        self.canvas.load_regions(boundary, pattern)
        self.properties.update_statistics(
            entry.verify_best,
            entry.verify_worst,
            entry.verify_average,
            entry.verify_success,
        )
        self.properties.update_teach_result(entry)
        self.properties.set_detection_profile(
            entry.detection_type,
            entry.detection_profile,
        )
        self.logger.info("Library Edit Started", entry_id=entry.entry_id)

    def _start_color_trigger(self):
        self.properties.set_detection_mode("color")
        self.canvas.set_color_pick_mode(False)
        self.canvas.set_region_type("color_trigger")

    def _on_color_picked(self, color):
        rgb = color.get("rgb", [0, 0, 0])
        pixel = cv2.cvtColor(
            np.array([[rgb[::-1]]], dtype=np.uint8),
            cv2.COLOR_BGR2HSV,
        )[0, 0]
        self.properties.set_picked_color(rgb, pixel.tolist())
        self.canvas.set_color_pick_mode(False)
        self._update_detection_preview()

    def _start_detection(self):
        self.properties.set_detection_mode("detection")
        self.canvas.set_color_pick_mode(False)
        self.canvas.set_region_type("detection")

    def _finish_name_edit(self):
        if (
            not self.edit_mode
            or self._loading_edit_entry
            or not self.edit_entry
        ):
            return

        new_name = self.properties.label.text().strip()
        if not new_name or new_name == self.edit_entry.name:
            return

        answer = QMessageBox.question(
            self,
            "Name ändern",
            "Soll nur der Name geändert werden?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.Yes,
        )
        if answer == QMessageBox.Yes:
            if self.teach_service.library_service.rename_entry(
                self.edit_entry.entry_id,
                new_name,
            ):
                self._reload_edit_image()
                self._finish_edit_mode(reset_library=True)

    def _reload_edit_image(self, entry=None):
        entry = entry or self.edit_entry
        samples = getattr(entry, "accepted_samples", None) or []
        image_path = samples[0].get("source") if samples else None
        image_path = image_path or self.current_screenshot
        if image_path:
            self.current_screenshot = image_path
            self.canvas.load_image(image_path)

    def _image_changed(self):
        if not self.edit_entry:
            return False
        boundary = self.canvas.search_boundary
        pattern = self.canvas.selected_region
        if not boundary or not pattern:
            return True
        return (
            self._region_values(boundary)
            != self._stored_region_values(self.edit_entry.boundary)
            or self._region_values(pattern)
            != self._stored_region_values(self.edit_entry.pattern)
        )

    @staticmethod
    def _region_values(region):
        return (
            int(region.x),
            int(region.y),
            int(region.width),
            int(region.height),
            region.region_type,
        )

    @staticmethod
    def _stored_region_values(data):
        return (
            int(data.get("x", 0)),
            int(data.get("y", 0)),
            int(data.get("width", 0)),
            int(data.get("height", 0)),
            data.get("type", "detection"),
        )

    def _confirm_dependency_change(self):
        used_by = self.edit_entry.used_by if self.edit_entry else []
        if not used_by:
            return True
        dependencies = "\n".join(f"- {item}" for item in used_by)
        answer = QMessageBox.question(
            self,
            "Abhängigkeiten bestätigen",
            "Das Bild wird für folgende Funktionen geändert:\n\n"
            f"{dependencies}\n\nFortfahren?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        return answer == QMessageBox.Yes

    def _finish_edit_mode(self, reset_library=False):
        self.edit_mode = False
        self.edit_entry = None
        self.selected_library_entry_id = ""
        self.verify_bundle = None
        self.verify_results = []
        self.canvas.clear_regions()
        self.properties.clear_content()
        if reset_library:
            self._reload_library_combo()
        else:
            self.library_combo.setCurrentIndex(0)

    def _reload_library_combo(self):
        self.library_combo.blockSignals(True)
        self.library_combo.clear()
        self.library_combo.addItem("Select learned item", "")
        self._populate_library_combo()
        self.library_combo.blockSignals(False)
        self._populate_orphan_combo()

    def _delete_selected_region(self):
        region = self.canvas.selected_region
        self.canvas.delete_selected_region()
        self.logger.info(
            "Region Deleted",
            region_id=getattr(region, "region_id", "") if region else "",
        )

    def _invalidate_verify(
        self,
        *args,
    ):

        self.verify_bundle = None
        self.verify_results = []

    def _extract_region_crop(
        self,
        region,
    ):

        if not self.current_screenshot or not region:
            return None

        img = cv2.imread(
            self.current_screenshot
        )

        if img is None:
            return None

        x = int(getattr(region, "x", None)()) if callable(getattr(region, "x", None)) else int(getattr(region, "x", 0))
        y = int(getattr(region, "y", None)()) if callable(getattr(region, "y", None)) else int(getattr(region, "y", 0))
        width = int(getattr(region, "width", None)()) if callable(getattr(region, "width", None)) else int(getattr(region, "width", 1))
        height = int(getattr(region, "height", None)()) if callable(getattr(region, "height", None)) else int(getattr(region, "height", 1))

        x = max(0, x)
        y = max(0, y)
        width = max(1, width)
        height = max(1, height)

        max_x = img.shape[1]
        max_y = img.shape[0]

        width = min(width, max_x - x)
        height = min(height, max_y - y)

        if width <= 0 or height <= 0:
            return None

        return img[y : y + height, x : x + width]

    def _save_region_crop(
        self,
        region,
    ):

        crop = self._extract_region_crop(region)

        if crop is None:
            self.current_crop_path = None
            return None

        output_dir = Path("data/learning/crops")
        output_dir.mkdir(
            parents=True,
            exist_ok=True,
        )

        region_id = getattr(region, "region_id", "selected")
        file_name = f"{region_id[:8]}_crop.png" if region_id else "selected_crop.png"
        target_path = output_dir / file_name

        ok = cv2.imwrite(
            str(target_path),
            crop,
        )

        if not ok:
            self.current_crop_path = None
            return None

        self.current_crop_path = str(target_path)
        return self.current_crop_path

    #
    # --------------------------------
    # Profile / Capture
    # --------------------------------
    #

    def get_selected_profile(
        self,
    ):

        return (
            self.profile_combo.currentData()
        )

    def _capture_if_profile_selected(self, *_):
        if self.get_selected_profile():
            self.capture_screen()

    def select_profile(self, profile_id):
        wanted = str(profile_id or "")
        for index in range(self.profile_combo.count()):
            profile = self.profile_combo.itemData(index) or {}
            candidate_ids = {
                str(profile.get("id", "")),
                str(profile.get("botstudio_profile_id", "")),
            } if isinstance(profile, dict) else {str(profile)}
            if wanted in candidate_ids:
                self.profile_combo.setCurrentIndex(index)
                return True
        return False

    def refresh_profiles(self):
        current = self.get_selected_profile()
        current_id = ""
        if isinstance(current, dict):
            current_id = str(
                current.get("botstudio_profile_id") or current.get("id") or ""
            )

        self.profile_combo.blockSignals(True)
        self.profile_combo.clear()
        self.profile_combo.addItem("", None)
        for profile in self.profile_service.load_all():
            text = (
                f"[{profile.get('game', '')}] "
                f"{profile.get('name', '')} "
                f"(E{profile.get('emulator_instance', 0)} "
                f"P{profile.get('priority', 0)})"
            )
            self.profile_combo.addItem(text, profile)
        self.select_profile(current_id)
        self.profile_combo.blockSignals(False)
        self._sync_profile_emulator_bar()

    def _sync_profile_emulator_bar(self):
        self.profile_emulator_bar.set_profile(
            self.get_selected_profile()
        )

    def capture_screen(
        self,
    ):
     
        profile = (
            self.get_selected_profile()
        )

        if not profile:
            self.logger.warning(
                "Capture Rejected",
                reason="no profile selected",
            )
            QMessageBox.warning(
                self,
                t("learning.profile"),
                t("function.profile_required"),
            )
            return

        screenshot = (
            self.screen_service.capture(
                profile
            )
        )

        self.logger.info(
            "Screen Captured",
            profile=profile,
            path=str(screenshot),
        )

        self.current_screenshot = (
            screenshot
        )
        self.current_crop_path = None

        self._invalidate_verify()

        self.canvas.load_image(
            screenshot
        )

    #
    # --------------------------------
    # Canvas
    # --------------------------------
    #

    def _on_region_selected(
        self,
        region,
    ):

        if region:
            self._save_region_crop(region)

        self.properties.update_region_info(
            region
        )
        self.properties.update_region_preview(
            self.current_screenshot,
            region,
        )
        self._update_detection_preview()

    def _on_region_updated(
        self,
        region,
    ):

        self._invalidate_verify()

        if region:
            self._save_region_crop(region)

        self.properties.update_region_info(
            region
        )
        self.properties.update_region_preview(
            self.current_screenshot,
            region,
        )
        self._update_detection_preview()

    def _update_detection_preview(self):
        if not self.current_screenshot:
            self.properties.update_detection_preview("No detection preview")
            return
        profile = self.properties.detection_profile()[1]
        boundary = self.canvas.search_boundary
        region = None
        if boundary:
            region = {
                "x": int(boundary.x),
                "y": int(boundary.y),
                "width": int(boundary.width),
                "height": int(boundary.height),
            }
        try:
            if profile.get("mode") == "color":
                if not profile.get("target_hsv"):
                    self.properties.update_detection_preview("Pick a color first")
                    return
                detections = self.opencv_service.find_color_all(
                    self.current_screenshot,
                    profile,
                    region,
                )
                text = f"Color matches: {len(detections)}\nPixels/area: " + \
                    ", ".join(str(int(item.score)) for item in detections[:5])
            else:
                if not self.current_crop_path:
                    self.properties.update_detection_preview("Select a detection pattern first")
                    return
                detections = self.opencv_service.find_all(
                    self.current_screenshot,
                    self.current_crop_path,
                    float(profile.get("threshold", 0.8)),
                    profile,
                    region,
                )
                scores = [f"{item.score * 100:.1f}%" for item in detections[:5]]
                text = f"Matches: {len(detections)}\nScores: " + \
                    (", ".join(scores) if scores else "none")
            self.properties.update_detection_preview(text)
        except (OSError, ValueError, TypeError) as error:
            self.properties.update_detection_preview(f"Preview unavailable: {error}")

    def run_verify(
        self,
    ):
        detection_type, detection_profile = self.properties.detection_profile()
        is_color_trigger = detection_type == "color"
        boundary = (
            self.canvas.search_boundary
        )

        if not boundary:
            self.logger.warning(
                "Verify Rejected",
                reason="no search boundary selected",
            )
            return

        pattern = None if is_color_trigger else self.canvas.selected_region

        if not pattern:
            if is_color_trigger and detection_profile.get("target_hsv"):
                pattern = None
            else:
                self.logger.warning(
                    "Verify Rejected",
                    reason="no color selected" if is_color_trigger else "no pattern region selected",
                )
                return

        if not pattern and not is_color_trigger:
            self.logger.warning(
                "Verify Rejected",
                reason="no pattern region selected",
            )
            return

        if (
            pattern
            == self.canvas.search_boundary
        ):
            self.logger.warning(
                "Verify Rejected",
                reason="pattern equals boundary",
            )
            return

        profile = (
            self.get_selected_profile()
        )

        if not profile:
            self.logger.warning(
                "Verify Rejected",
                reason="no profile selected",
            )
            return

        samples = (
            self.properties.verify_samples.value()
        )

        self.logger.info(
            "Verify Started",
            profile=profile,
            samples=samples,
        )

        if is_color_trigger:
            result = self.verify_runner.run_color(
                profile=profile,
                boundary=boundary,
                detection_profile=detection_profile,
                samples=samples,
            )
        else:
            result = self.verify_runner.run(
                profile=profile,
                boundary=boundary,
                pattern=pattern,
                samples=samples,
                source_screenshot=self.current_screenshot,
            )

        result["crop_path"] = self.current_crop_path
        self.verify_bundle = result

        self.verify_results = (
            result["verify_results"]
        )

        self.logger.info(
            "Verify Finished",
            profile=profile,
            best=result["best"],
            worst=result["worst"],
            average=result["average"],
            success_rate=result["success_rate"],
        )

        self.properties.update_statistics(
            result["best"],
            result["worst"],
            result["average"],
            result["success_rate"],
        )

        self.properties.update_verification(
            result["average"]
        )

        self.canvas.set_verify_matches(
            result["best_matches"]
        )

    def teach_pattern(
        self,
    ):
        if not self.verify_bundle:
            self.logger.warning(
                "Teach Rejected",
                reason="no verify result available",
            )
            return

        if not self.verify_bundle["verify_results"]:
            self.logger.warning(
                "Teach Rejected",
                reason="verify produced no results",
            )
            return

        detection_type, detection_profile = self.properties.detection_profile()
        current_context = (
            LearningVerifyRunner.build_color_context(
                self.get_selected_profile(),
                self.canvas.search_boundary,
                detection_profile,
            )
            if detection_type == "color"
            else LearningVerifyRunner.build_context(
                self.get_selected_profile(),
                self.canvas.search_boundary,
                self.canvas.selected_region,
            )
        )

        if (
            current_context
            != self.verify_bundle["context"]
        ):
            self.logger.warning(
                "Teach Aborted",
                reason="selection changed since verify, run verify again",
            )
            return

        name = (
    self.properties.label.text()
    .strip()
)

        if self.edit_mode and not self._image_changed():
            if name and name != self.edit_entry.name:
                if self.teach_service.library_service.rename_entry(
                    self.edit_entry.entry_id,
                    name,
                ):
                    self._reload_edit_image()
                    self._finish_edit_mode(reset_library=True)
            return

        if self.edit_mode and not self._confirm_dependency_change():
            self.logger.info(
                "Teach Cancelled",
                reason="library dependencies rejected",
                entry_id=self.edit_entry.entry_id,
            )
            return

        if not name:
            result = QMessageBox.question(
                self,
                t("common.teach"),
                t("teach.missing_name_text"),
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if result != QMessageBox.Yes:
                self.logger.info(
                    "Teach Cancelled",
                    reason="name missing, user declined to continue",
                )
                return

        teach_method = (
            self.teach_service.replace
            if self.edit_mode
            else self.teach_service.teach
        )
        entry = (
            teach_method(
                verify_bundle=self.verify_bundle,
                name=name,
                verify_best=(
                    self.properties.lbl_best.text()
                    or 0
                ),
                verify_worst=(
                    self.properties.lbl_worst.text()
                    or 0
                ),
                verify_average=(
                    self.properties.lbl_average.text()
                    or 0
                ),
                verify_success=(
                    self.properties.lbl_success.text()
                    .replace("%", "")
                    or 0
                ),
                     **({"entry_id": self.edit_entry.entry_id}
                         if self.edit_mode else {}),
            )
        )

        detection_type, detection_profile = self.properties.detection_profile()
        if detection_type == "color":
            crop = self._extract_region_crop(self.canvas.selected_region)
            if crop is not None and crop.size:
                hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
                mean_hsv = hsv.reshape(-1, 3).mean(axis=0)
                detection_profile["target_hsv"] = [
                    int(value) for value in mean_hsv
                ]
        entry.detection_type = detection_type
        entry.detection_profile = detection_profile
        self.teach_service.library_service.save(entry)

        self.logger.info(
            "Teach Completed",
            profile=self.get_selected_profile(),
            entry_id=entry.entry_id,
            name=name,
        )

        self.teach_completed.emit(entry)

        self.properties.update_teach_result(
            entry
        )

        if self.edit_mode:
            self._reload_edit_image(entry)
            self._finish_edit_mode(reset_library=True)