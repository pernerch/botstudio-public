from PySide6.QtCore import QSize, Qt
from PySide6.QtWidgets import (
    QWidget,
    QLabel,
    QLineEdit,
    QComboBox,
    QCheckBox,
    QVBoxLayout,
    QPushButton,
    QGroupBox,
    QHBoxLayout,
    QSpinBox,
    QGridLayout,
    QTabWidget,
)

from ui.i18n.translator import (
    t,
)
from ui.image_utils import load_pixmap
from ui.widgets.searchable_combo import SearchableComboBox


class PropertiesPanel(QWidget):

    def __init__(
        self,
        parent=None,
    ):
        super().__init__(parent)

        layout = QVBoxLayout(
            self
        )

        #
        # Element Name
        #

        label_group = QGroupBox(
            t("learning.label")
        )

        label_layout = QVBoxLayout()

        self.label = QLineEdit()
        self.label.textChanged.connect(self._update_required_field_style)

        label_layout.addWidget(
            QLabel(
                t(
                    "learning.element_name"
                )
            )
        )

        label_layout.addWidget(
            self.label
        )

        label_group.setLayout(
            label_layout
        )


        #
        # Search Boundary
        #

        boundary_group = QGroupBox(
            "Search Boundary"
        )

        boundary_layout = QVBoxLayout()

        self.search_scope = SearchableComboBox()

        self.search_scope.addItems(
            sorted([
                "Bereich",
                "Bildausschnitt",
                "Ganzer Screen",
            ], key=str.casefold)
        )

        boundary_layout.addWidget(
            QLabel(
                "Search Scope"
            )
        )

        boundary_layout.addWidget(
            self.search_scope
        )

        boundary_group.setLayout(
            boundary_layout
        )

        detection_group = QGroupBox("Detection Profile")
        detection_layout = QGridLayout()

        self.detection_mode = "detection"
        self.detection_type_label = QLabel("Detection Pattern")
        detection_layout.addWidget(QLabel("Mode"), 0, 0)
        detection_layout.addWidget(self.detection_type_label, 0, 1)

        self.shape_detection = QCheckBox("Use shape detection")
        detection_layout.addWidget(self.shape_detection, 1, 0, 1, 2)

        self.ignore_colors = QCheckBox("Ignore colors")
        detection_layout.addWidget(self.ignore_colors, 2, 0, 1, 2)

        self.detection_threshold = QSpinBox()
        self.detection_threshold.setRange(1, 100)
        self.detection_threshold.setValue(80)
        detection_layout.addWidget(QLabel("Threshold"), 3, 0)
        detection_layout.addWidget(self.detection_threshold, 3, 1)

        self.color_tolerance = QSpinBox()
        self.color_tolerance.setRange(0, 100)
        self.color_tolerance.setValue(20)
        self.color_tolerance_label = QLabel("Color tolerance")
        detection_layout.addWidget(self.color_tolerance_label, 4, 0)
        detection_layout.addWidget(self.color_tolerance, 4, 1)

        self.minimum_area = QSpinBox()
        self.minimum_area.setRange(1, 1000000)
        self.minimum_area.setValue(20)
        self.minimum_area_label = QLabel("Minimum area")
        detection_layout.addWidget(self.minimum_area_label, 5, 0)
        detection_layout.addWidget(self.minimum_area, 5, 1)

        self.btn_pick_color = QPushButton("Pick color from screenshot")
        detection_layout.addWidget(self.btn_pick_color, 6, 0, 1, 2)
        self.lbl_picked_color = QLabel("No color selected")
        detection_layout.addWidget(self.lbl_picked_color, 7, 0, 1, 2)

        detection_group.setLayout(detection_layout)

     


        #
        # Region Info
        #

        info_group = QGroupBox(
            "Region Info"
        )

        info_layout = QVBoxLayout()

        self.lbl_position = QLabel(
            "-"
        )

        self.lbl_size = QLabel(
            "-"
        )

        self.region_preview = QLabel(
            "No region preview"
        )
        self.region_preview.setMinimumHeight(120)
        self.region_preview.setAlignment(
            Qt.AlignCenter
        )
        self.region_preview.setStyleSheet(
            "border: 1px solid #555; background: #111;"
        )

        self.lbl_detection_preview = QLabel("No detection preview")
        self.lbl_detection_preview.setWordWrap(True)

        info_layout.addWidget(
            QLabel(
                t(
                    "learning.position"
                )
            )
        )

        info_layout.addWidget(
            self.lbl_position
        )

        info_layout.addWidget(
            QLabel(
                t("learning.size")
            )
        )

        info_layout.addWidget(
            self.lbl_size
        )

        info_layout.addWidget(
            QLabel(
                "Crop Preview"
            )
        )

        info_layout.addWidget(
            self.region_preview
        )

        info_layout.addWidget(
            QLabel("Live Detection")
        )

        info_layout.addWidget(
            self.lbl_detection_preview
        )

        info_group.setLayout(
            info_layout
        )

        #
        # Verify
        #

        self.result_tabs = QTabWidget()

        self.btn_verify = QPushButton(t("common.verify"))
        self.btn_teach = QPushButton(t("common.teach"))
        self.btn_teach.setEnabled(False)

        verify_group = QWidget()

        verify_layout = QGridLayout()

        self.verify_samples = (
            QSpinBox()
        )

        self.verify_samples.setRange(
            5,
            20
        )

        self.verify_samples.setValue(
            5
        )

        self.lbl_duration = QLabel(
            "~ 5 sec"
        )

        self.lbl_score = QLabel(
            "-"
        )

        self.lbl_status = QLabel(
            t(
                "verify.not_verified"
            )
        )

        self.lbl_best = QLabel("-")

        self.lbl_worst = QLabel("-")

        self.lbl_average = QLabel("-")

        self.lbl_success = QLabel("-")

        verify_metrics = (
            ("Samples", self.verify_samples),
            ("Estimated Duration", self.lbl_duration),
            ("Score", self.lbl_score),
            ("Best", self.lbl_best),
            ("Worst", self.lbl_worst),
            ("Average", self.lbl_average),
            ("Success Rate", self.lbl_success),
        )
        for index, (label, value) in enumerate(verify_metrics):
            row = index // 2
            column = (index % 2) * 2
            verify_layout.addWidget(QLabel(label), row, column)
            verify_layout.addWidget(value, row, column + 1)
        verify_layout.addWidget(self.lbl_status, len(verify_metrics) // 2 + 1, 0, 1, 4)

        verify_group.setLayout(
            verify_layout
        )

        teach_group = QWidget()

        teach_layout = QGridLayout()

        self.lbl_teach_entry_id = QLabel("-")

        self.lbl_teach_entry_name = QLabel("-")

        self.lbl_teach_pattern_type = QLabel("-")

        self.lbl_teach_accepted = QLabel("-")

        self.lbl_teach_rejected = QLabel("-")

        self.lbl_teach_best = QLabel("-")

        self.lbl_teach_average = QLabel("-")

        self.lbl_teach_worst = QLabel("-")

        self.lbl_teach_success = QLabel("-")

        self.lbl_teach_matrix = QLabel("-")
        self.lbl_teach_preview = QLabel(t("teach.no_preview"))
        self.lbl_teach_preview.setMinimumHeight(120)
        self.lbl_teach_preview.setAlignment(Qt.AlignCenter)
        self.lbl_teach_preview.setStyleSheet("border: 1px solid #555; background: #111;")

        teach_metrics = (
            (t("teach.entry_id"), self.lbl_teach_entry_id),
            (t("teach.entry_name"), self.lbl_teach_entry_name),
            (t("teach.pattern_type"), self.lbl_teach_pattern_type),
            (t("teach.accepted_samples"), self.lbl_teach_accepted),
            (t("teach.rejected_samples"), self.lbl_teach_rejected),
            (t("teach.verify_best"), self.lbl_teach_best),
            (t("teach.verify_average"), self.lbl_teach_average),
            (t("teach.verify_worst"), self.lbl_teach_worst),
            (t("teach.success_rate"), self.lbl_teach_success),
            (t("teach.matrix"), self.lbl_teach_matrix),
        )
        for index, (label, value) in enumerate(teach_metrics):
            row = index // 2
            column = (index % 2) * 2
            teach_layout.addWidget(QLabel(label), row, column)
            teach_layout.addWidget(value, row, column + 1)
        teach_layout.addWidget(QLabel("Teach Preview"), len(teach_metrics) // 2 + 1, 0, 1, 4)
        teach_layout.addWidget(self.lbl_teach_preview, len(teach_metrics) // 2 + 2, 0, 1, 4)

        teach_group.setLayout(
            teach_layout
        )

        self.result_tabs.addTab(
            verify_group,
            t("common.verify"),
        )

        self.result_tabs.addTab(
            teach_group,
            t("common.teach"),
        )

        #
        # Layout
        #

        layout.addWidget(
            label_group
        )

        layout.addWidget(
            boundary_group
        )

        layout.addWidget(
            detection_group
        )

        layout.addWidget(
            info_group
        )

        action_layout = QVBoxLayout()
        action_layout.addWidget(self.btn_verify)
        action_layout.addWidget(self.btn_teach)
        layout.addLayout(action_layout)

        layout.addWidget(
            self.result_tabs
        )

        layout.addStretch()

        self.set_detection_mode("detection")
        self._update_required_field_style()

    def set_detection_mode(self, mode):
        self.detection_mode = mode
        is_color = mode == "color"
        self.detection_type_label.setText(
            "Color Trigger" if is_color else "Detection Pattern"
        )
        self.shape_detection.setVisible(not is_color)
        self.ignore_colors.setVisible(not is_color)
        self.detection_threshold.setVisible(not is_color)
        self.color_tolerance_label.setVisible(is_color)
        self.color_tolerance.setVisible(is_color)
        self.minimum_area_label.setVisible(is_color)
        self.minimum_area.setVisible(is_color)
        self.btn_pick_color.setVisible(is_color)
        self.lbl_picked_color.setVisible(is_color)

    def detection_profile(self):
        detection_type = "color" if self.detection_mode == "color" else (
            "shape" if self.shape_detection.isChecked() else "template"
        )
        return detection_type, {
            "mode": detection_type,
            "color_sensitive": not self.ignore_colors.isChecked(),
            "use_grayscale": self.ignore_colors.isChecked(),
            "use_edges": detection_type == "shape",
            "find_all": detection_type == "color",
            "tolerance_hsv": [
                self.color_tolerance.value(),
                self.color_tolerance.value(),
                self.color_tolerance.value(),
            ],
            "min_area": self.minimum_area.value(),
            "threshold": self.detection_threshold.value() / 100,
            "target_hsv": getattr(self, "_target_hsv", None),
        }

    def set_picked_color(self, rgb, hsv):
        self._target_hsv = [int(value) for value in hsv]
        self.lbl_picked_color.setText(
            f"RGB {tuple(int(value) for value in rgb)} / "
            f"HSV {tuple(self._target_hsv)}"
        )

    def set_detection_profile(self, detection_type="template", profile=None):
        profile = profile or {}
        self.set_detection_mode("color" if detection_type == "color" else "detection")
        self.shape_detection.setChecked(detection_type == "shape")
        self.ignore_colors.setChecked(
            bool(profile.get("use_grayscale", not profile.get("color_sensitive", True)))
        )
        self.detection_threshold.setValue(
            int(float(profile.get("threshold", 0.8)) * 100)
        )
        tolerance = profile.get("tolerance_hsv", [20])
        self.color_tolerance.setValue(int(tolerance[0] if tolerance else 20))
        self.minimum_area.setValue(int(profile.get("min_area", 20)))
        target_hsv = profile.get("target_hsv")
        self._target_hsv = list(target_hsv) if isinstance(target_hsv, list) else None
        self.lbl_picked_color.setText(
            f"HSV {tuple(self._target_hsv)}"
            if self._target_hsv
            else "No color selected"
        )

    def _update_required_field_style(self):
        self.label.setStyleSheet(
            "border: 1px solid #d9534f;"
            if not self.label.text().strip()
            else ""
        )

    def update_region_info(
        self,
        region,
    ):

        if not region:

            self.lbl_position.setText(
                "-"
            )

            self.lbl_size.setText(
                "-"
            )

            self.region_preview.clear()
            self.region_preview.setText(
                "No region preview"
            )

            return

        x = int(getattr(region, "x", None)()) if callable(getattr(region, "x", None)) else int(getattr(region, "x", 0))
        y = int(getattr(region, "y", None)()) if callable(getattr(region, "y", None)) else int(getattr(region, "y", 0))
        width = int(getattr(region, "width", None)()) if callable(getattr(region, "width", None)) else int(getattr(region, "width", 0))
        height = int(getattr(region, "height", None)()) if callable(getattr(region, "height", None)) else int(getattr(region, "height", 0))

        self.lbl_position.setText(
            f"x={x} y={y}"
        )

        self.lbl_size.setText(
            f"w={width} h={height}"
        )

    def update_detection_preview(self, text):
        self.lbl_detection_preview.setText(str(text or "No detection preview"))

    def update_region_preview(
        self,
        screenshot_path: str | None,
        region,
    ):

        if not screenshot_path or not region:
            self.region_preview.clear()
            self.region_preview.setText(
                "No region preview"
            )
            return

        pixmap = load_pixmap(screenshot_path)

        if pixmap.isNull():
            self.region_preview.clear()
            self.region_preview.setText(
                "No region preview"
            )
            return

        x = int(getattr(region, "x", None)()) if callable(getattr(region, "x", None)) else int(getattr(region, "x", 0))
        y = int(getattr(region, "y", None)()) if callable(getattr(region, "y", None)) else int(getattr(region, "y", 0))
        width = int(getattr(region, "width", None)()) if callable(getattr(region, "width", None)) else int(getattr(region, "width", 0))
        height = int(getattr(region, "height", None)()) if callable(getattr(region, "height", None)) else int(getattr(region, "height", 0))

        crop = pixmap.copy(
            max(0, x),
            max(0, y),
            max(1, width),
            max(1, height),
        )

        if crop.isNull():
            self.region_preview.clear()
            self.region_preview.setText(
                "No region preview"
            )
            return

        self.region_preview.setPixmap(
            crop.scaled(
                220,
                120,
                Qt.KeepAspectRatio,
                Qt.SmoothTransformation,
            )
        )

    def update_verification(
        self,
        score: float,
    ):

        self.result_tabs.setCurrentIndex(
            0
        )

        self.lbl_score.setText(
            f"{score:.4f}"
        )

        self.btn_teach.setEnabled(
            score >= 0.95
        )

        if score >= 0.95:

            self.lbl_status.setText(
                "🟢 "
                + t(
                    "verify.excellent"
                )
            )

        elif score >= 0.80:

            self.lbl_status.setText(
                "🟡 "
                + t(
                    "verify.good"
                )
            )

        else:

            self.lbl_status.setText(
                "🔴 "
                + t(
                    "verify.poor"
                )
            )

    def update_statistics(
        self,
        best: float,
        worst: float,
        average: float,
        success_rate: float,
    ):

        self.lbl_best.setText(
            f"{best:.4f}"
        )

        self.lbl_worst.setText(
            f"{worst:.4f}"
        )

        self.lbl_average.setText(
            f"{average:.4f}"
        )

        self.lbl_success.setText(
            f"{success_rate:.1f}%"
        )

    def update_teach_result(
        self,
        entry,
    ):

        self.result_tabs.setCurrentIndex(
            1
        )

        preview_path = None
        if entry.accepted_samples:
            preview_path = entry.accepted_samples[0].get("crop")
        if not preview_path:
            preview_path = entry.pattern.get("reference_path")
        if not preview_path:
            preview_path = None

        if preview_path:
            pixmap = load_pixmap(preview_path, QSize(440, 240))
            if not pixmap.isNull():
                self.lbl_teach_preview.setPixmap(
                    pixmap.scaled(
                        220,
                        120,
                        Qt.KeepAspectRatio,
                        Qt.SmoothTransformation,
                    )
                )
                self.lbl_teach_preview.setText("")
            else:
                self.lbl_teach_preview.clear()
                self.lbl_teach_preview.setText(t("teach.no_preview"))
        else:
            self.lbl_teach_preview.clear()
            self.lbl_teach_preview.setText(t("teach.no_preview"))

        self.lbl_teach_entry_id.setText(
            entry.entry_id
        )

        self.lbl_teach_entry_name.setText(
            entry.name
        )

        self.lbl_teach_pattern_type.setText(
            entry.pattern.get(
                "type",
                "-",
            )
        )

        self.lbl_teach_accepted.setText(
            str(
                len(
                    entry.accepted_samples
                )
            )
        )

        self.lbl_teach_rejected.setText(
            str(
                len(
                    entry.rejected_samples
                )
            )
        )

        self.lbl_teach_best.setText(
            f"{entry.verify_best:.4f}"
        )

        self.lbl_teach_average.setText(
            f"{entry.verify_average:.4f}"
        )

        self.lbl_teach_worst.setText(
            f"{entry.verify_worst:.4f}"
        )

        self.lbl_teach_success.setText(
            f"{entry.verify_success:.1f}%"
        )

        self.lbl_teach_matrix.setText(
            t("teach.matrix_generated")
        )

    def clear_content(self):
        self.label.clear()
        self.update_region_info(None)
        self.update_verification(0)
        self.lbl_score.setText("-")
        self.lbl_status.setText(t("verify.not_verified"))
        for label in (
            self.lbl_best,
            self.lbl_worst,
            self.lbl_average,
            self.lbl_success,
            self.lbl_teach_entry_id,
            self.lbl_teach_entry_name,
            self.lbl_teach_pattern_type,
            self.lbl_teach_accepted,
            self.lbl_teach_rejected,
            self.lbl_teach_best,
            self.lbl_teach_average,
            self.lbl_teach_worst,
            self.lbl_teach_success,
            self.lbl_teach_matrix,
        ):
            label.setText("-")
        self.lbl_teach_preview.clear()
        self.lbl_teach_preview.setText(t("teach.no_preview"))
        self.btn_teach.setEnabled(False)