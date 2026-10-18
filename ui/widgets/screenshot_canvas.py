# ui/widgets/screenshot_canvas.py
#
# Learning Mode V2
# Search Boundary + Pattern System
#


from PySide6.QtCore import (
    QPoint,
    QRect,
    Qt,
    Signal,
    QSize,
)

from PySide6.QtGui import (
    QColor,
    QPainter,
    QPen,
    QPixmap,
)

from PySide6.QtWidgets import (
    QWidget,
)

from models.canvas_region import (
    CanvasRegion,
)
from ui.image_utils import load_pixmap


class ScreenshotCanvas(QWidget):

    region_changed = Signal(QRect)

    region_selected = Signal(object)

    region_updated = Signal(object)
    color_picked = Signal(object)

    HANDLE_SIZE = 10

    def __init__(
        self,
        parent=None,
    ):
        super().__init__(parent)

        self.pixmap = None

        self.selection = QRect()

        self.drag_start = QPoint()

        self.is_drawing = False

        self.edit_boundary_mode = False

        #
        # Learning V2
        #

        self.search_boundary = None

        self.patterns = []

        self.selected_region = None

        self.active_region_type = (
            "boundary"
        )

        self.color_pick_mode = False

        #
        # Move
        #

        self.drag_region = None

        self.drag_offset = QPoint()

        self.is_moving_region = False

        #
        # Resize
        #

        self.is_resizing = False

        self.resize_region = None

        self.resize_handle = None

        self.setMouseTracking(True)

        self.setFocusPolicy(
            Qt.StrongFocus
        )

        self.verify_matches = [] ############################### Test only

    # ---------------------------------
    # Screenshot
    # ---------------------------------

    def sizeHint(self):
        if self.pixmap is not None:
            return self.pixmap.size()
        return QSize(0, 0)

    def load_image(
        self,
        image_path: str,
    ):

        self.pixmap = load_pixmap(image_path)

        self.resize(
            self.pixmap.size()
        )

        self.updateGeometry()

        self.update()

    # ---------------------------------
    # Region Type
    # ---------------------------------

    def set_region_type(
        self,
        region_type: str,
    ):

        self.active_region_type = (
            region_type
        )

        self.edit_boundary_mode = (
            region_type == "boundary"
        )

    def set_color_pick_mode(self, enabled):
        self.color_pick_mode = bool(enabled)
        self.setCursor(
            Qt.CrossCursor if self.color_pick_mode else Qt.ArrowCursor
        )

    # ---------------------------------
    # Color Helper
    # ---------------------------------

    def _get_color(
        self,
        region_type: str,
    ):

        colors = {

            "boundary":
                "#ffff00",

            "detection":
                "#ff4444",

            "tap":
                "#44ff44",

            "ocr":
                "#4488ff",

            "color_trigger":
                "#bb44ff",
        }

        return colors.get(
            region_type,
            "#ffffff",
        )

    # ---------------------------------
    # Region Search
    # ---------------------------------

    def _all_regions(
        self,
    ):

        regions = []

        if self.search_boundary:

            regions.append(
                self.search_boundary
            )

        regions.extend(
            self.patterns
        )

        return regions

    def _find_region_at(
        self,
        position,
    ):

        #
        # Pattern First
        #

        for region in reversed(
            self.patterns
        ):

            if region.rect.contains(
                position
            ):
                return region

        #
        # Boundary Last
        #

        if (
            self.search_boundary
            and self.edit_boundary_mode
        ):

            if (
                self.search_boundary.rect.contains(
                    position
                )
            ):
                return (
                    self.search_boundary
                )

        return None

    # ---------------------------------
    # Resize Handles
    # ---------------------------------

    def _get_handles(
        self,
        region,
    ):

        s = self.HANDLE_SIZE

        return {

            "tl": QRect(
                region.x - s // 2,
                region.y - s // 2,
                s,
                s,
            ),

            "tr": QRect(
                region.x
                + region.width
                - s // 2,
                region.y - s // 2,
                s,
                s,
            ),

            "bl": QRect(
                region.x - s // 2,
                region.y
                + region.height
                - s // 2,
                s,
                s,
            ),

            "br": QRect(
                region.x
                + region.width
                - s // 2,

                region.y
                + region.height
                - s // 2,

                s,
                s,
            ),
        }

    def _find_handle(
        self,
        position,
    ):

        if not self.selected_region:
            return None

        handles = self._get_handles(
            self.selected_region
        )

        for name, rect in (
            handles.items()
        ):

            if rect.contains(
                position
            ):
                return name

        return None

    # ---------------------------------
    # Public API
    # ---------------------------------

    def clear_regions(
        self,
    ):

        self.search_boundary = None

        self.patterns.clear()

        self.selected_region = None

        self.region_selected.emit(
            None
        )

        self.update()

    def load_regions(self, boundary, pattern):
        self.clear_regions()
        if boundary:
            self.search_boundary = boundary
        if pattern:
            self.patterns.append(pattern)
            self.selected_region = pattern
            self.region_selected.emit(pattern)
        self.update()

    def delete_selected_region(
        self,
    ):

        if not self.selected_region:
            return

        self.clear_verify_matches()
        #
        # Delete Boundary
        #

        if (
            self.selected_region
            == self.search_boundary
        ):

            self.search_boundary = None

            self.patterns.clear()

        #
        # Delete Pattern
        #

        else:

            if (
                self.selected_region
                in self.patterns
            ):

                self.patterns.remove(
                    self.selected_region
                )

        self.selected_region = None

        self.region_selected.emit(
            None
        )

        self.update()

    # ---------------------------------
    # Paint
    # ---------------------------------

    def paintEvent(
        self,
        event,
    ):
        
        painter = QPainter(self)

        if self.pixmap:

            painter.drawPixmap(
                0,
                0,
                self.pixmap,
            )

        #
        # Preview
        #

        if not self.selection.isNull():

            preview_color = (
                self._get_color(
                    self.active_region_type
                )
            )

            pen = QPen(
                QColor(
                    preview_color
                )
            )

            pen.setWidth(2)

            painter.setPen(
                pen
            )

            painter.drawRect(
                self.selection
            )

        #
        # Draw Regions
        #

        #
        # Verify Overlay
        #

        for index, rect in enumerate(
            self.verify_matches
        ):

            pen = QPen(
                QColor(
                    "#00ffff"
                )
            )

            pen.setWidth(3)

            painter.setPen(
                pen
            )

            painter.drawRect(
                rect
            )

            painter.drawText(
                rect.x(),
                rect.y() - 5,
                f"#{index + 1}"
            )

        for region in self._all_regions():

            pen = QPen(
                QColor(
                    region.color
                )
            )

            if region.selected:

                pen.setWidth(4)

            else:

                pen.setWidth(2)

            painter.setPen(
                pen
            )

            painter.drawRect(
                region.x,
                region.y,
                region.width,
                region.height,
            )

            #
            # Boundary nur im Edit Mode
            #

            show_handles = True

            if (
                region == self.search_boundary
                and not self.edit_boundary_mode
            ):
                show_handles = False

            if (
                region.selected
                and show_handles
            ):

                painter.setBrush(
                    QColor(
                        "white"
                    )
                )

                handles = (
                    self._get_handles(
                        region
                    )
                )

                for rect in (
                    handles.values()
                ):

                    painter.drawRect(
                        rect
                    )

                painter.setBrush(
                    Qt.NoBrush
                )

        #
        # Draw Regions
        #

        for region in self._all_regions():

            pen = QPen(
                QColor(
                    region.color
                )
            )

            if region.selected:

                pen.setWidth(
                    4
                )

            else:

                pen.setWidth(
                    2
                )

            painter.setPen(
                pen
            )

            #
            # Boundary Visual
            #

            if region == self.search_boundary:

                painter.fillRect(
                    region.x,
                    region.y,
                    region.width,
                    region.height,
                    QColor(
                        255,
                        255,
                        0,
                        30,
                    ),
                )


            painter.drawRect(
                region.x,
                region.y,
                region.width,
                region.height,
            )

            #
            # Handles
            #

            if region.selected:

                painter.setBrush(
                    #QColor(
                    #    "white"
                    Qt.NoBrush
                    
                )

                handles = (
                    self._get_handles(
                        region
                    )
                )

                for rect in (
                    handles.values()
                ):

                    painter.drawRect(
                        rect
                    )
        #
        # Verify Overlay
        #

        for idx, rect in enumerate(
                self.verify_matches
            ):

                pen = QPen(
                    QColor(
                        "#00ffff"
                    )
                )

                pen.setWidth(3)

                painter.setPen(
                    pen
                )

                painter.drawRect(
                    rect
                )

                painter.drawText(
                    rect.x(),
                    rect.y() - 5,
                    f"#{idx + 1}"
                )


    # ---------------------------------
    # Keyboard
    # ---------------------------------

    def keyPressEvent(
        self,
        event,
    ):

        if (

            event.key()
            == Qt.Key_Delete

        ):

            self.delete_selected_region()

    # ---------------------------------
    # Mouse Press
    # ---------------------------------

    def mousePressEvent(
        self,
        event,
    ):

        if (
            event.button()
            != Qt.LeftButton
        ):
            return

        if self.color_pick_mode and self.pixmap:
            position = event.pos()
            if self.pixmap.rect().contains(position):
                color = self.pixmap.toImage().pixelColor(position)
                self.color_picked.emit({
                    "x": position.x(),
                    "y": position.y(),
                    "rgb": [color.red(), color.green(), color.blue()],
                    "rgba": [color.red(), color.green(), color.blue(), color.alpha()],
                })
            self.set_color_pick_mode(False)
            return

        handle = (
            self._find_handle(
                event.pos()
            )
        )

        if handle:

            self.resize_region = (
                self.selected_region
            )

            self.resize_handle = (
                handle
            )

            self.is_resizing = True

            return

        #
        # Selection Reset
        #

        for region in (
            self._all_regions()
        ):
            region.selected = False

        clicked_region = (
            self._find_region_at(
                event.pos()
            )
        )

        if clicked_region:

            clicked_region.selected = True

            self.selected_region = (
                clicked_region
            )

            self.region_selected.emit(
                clicked_region
            )

            self.drag_region = (
                clicked_region
            )

            self.drag_offset = (
                event.pos()
                - QPoint(
                    clicked_region.x,
                    clicked_region.y,
                )
            )

            self.is_moving_region = True

            self.update()

            return

        self.selected_region = None

        self.region_selected.emit(
            None
        )

        self.drag_start = (
            event.pos()
        )

        self.selection = QRect(
            self.drag_start,
            self.drag_start,
        )

        self.is_drawing = True

        self.update()

    def leaveEvent(self, event):
        if self.color_pick_mode:
            self.set_color_pick_mode(False)
        super().leaveEvent(event)

    # ---------------------------------
    # Mouse Move
    # ---------------------------------

    def mouseMoveEvent(
        self,
        event,
    ):

        #
        # Resize
        #

        if self.is_resizing:

            region = (
                self.resize_region
            )

            old_x = region.x
            old_y = region.y
            old_w = region.width
            old_h = region.height

            x = event.pos().x()

            y = event.pos().y()

            if (
                self.resize_handle
                == "br"
            ):

                region.resize_to(
                    x - region.x,
                    y - region.y,
                )

            #
            # Boundary Validation
            #

            if (
                region == self.search_boundary
            ):

                boundary_rect = (
                    region.rect
                )

                for pattern in (
                    self.patterns
                ):

                    if not (
                        boundary_rect.contains(
                            pattern.rect
                        )
                    ):

                        region.x = old_x
                        region.y = old_y
                        region.width = old_w
                        region.height = old_h

                        return

            self.clear_verify_matches()
            
            self.region_updated.emit(
                region
            )

            self.update()

            return

        #
        # Move
        #

        if self.is_moving_region:

            old_x = self.drag_region.x
            old_y = self.drag_region.y

            self.drag_region.move_to(

                event.pos().x()
                - self.drag_offset.x(),

                event.pos().y()
                - self.drag_offset.y(),
            )

            #
            # Boundary darf Pattern nicht verlieren
            #

            if (
                self.drag_region
                == self.search_boundary
            ):

                boundary_rect = (
                    self.search_boundary.rect
                )

                for pattern in (
                    self.patterns
                ):

                    if not (
                        boundary_rect.contains(
                            pattern.rect
                        )
                    ):

                        self.drag_region.x = old_x
                        self.drag_region.y = old_y

                        return

            self.clear_verify_matches()

            self.region_updated.emit(
                self.drag_region
            )

            self.update()

            return

        #
        # Draw
        #

        if not self.is_drawing:
            return

        self.selection = QRect(
            self.drag_start,
            event.pos(),
        ).normalized()

        self.update()

    # ---------------------------------
    # Mouse Release
    # ---------------------------------

    def mouseReleaseEvent(
        self,
        event,
    ):

        if (
            event.button()
            != Qt.LeftButton
        ):
            return

        #
        # Resize
        #

        if self.is_resizing:

            self.is_resizing = False

            self.resize_region = None

            self.resize_handle = None

            return

        #
        # Move
        #

        if self.is_moving_region:

            self.is_moving_region = False

            self.drag_region = None

            return

        #
        # Draw
        #

        self.is_drawing = False

        if (
            self.selection.width() < 5
            or self.selection.height() < 5
        ):
            return

        region = CanvasRegion.create(
            region_type=self.active_region_type,
            x=self.selection.x(),
            y=self.selection.y(),
            width=self.selection.width(),
            height=self.selection.height(),
            color=self._get_color(
                self.active_region_type
            ),
        )

        #
        # Boundary
        #

        if (
            self.active_region_type
            == "boundary"
        ):

            self.search_boundary = region

            self.clear_verify_matches()

            #
            # Boundary wird sofort gelockt
            #

            self.edit_boundary_mode = False

        #
        # Pattern
        #

        else:

            if not self.search_boundary:

                self.selection = QRect()

                self.update()

                return

            if not (
                self.search_boundary.contains(
                    region
                )
            ):

                self.selection = QRect()

                self.update()

                return

            self.patterns.append(
                region
            )

            self.clear_verify_matches()

        self.selection = QRect()

        self.region_changed.emit(
            region.rect
        )

        self.update()

    def set_verify_matches(
        self,
        matches,
    ):

        self.verify_matches = matches

        self.update()

###Test only
    def set_verify_matches(
        self,
        matches,
    ):

        self.verify_matches = matches

        self.update()
### Test only

    def clear_verify_matches(
        self,
        ):

        self.verify_matches = []

        self.update()

    def sizeHint(
        self,
    ):

        if self.pixmap:

            return QSize(
                self.pixmap.width(),
                self.pixmap.height(),
            )

        return super().sizeHint()