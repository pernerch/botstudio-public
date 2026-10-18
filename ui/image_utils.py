from pathlib import Path

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QImageReader, QPixmap


def load_pixmap(
    path: str | Path | None,
    max_size: QSize | None = None,
) -> QPixmap:
    if not path:
        return QPixmap()

    reader = QImageReader(str(path))
    reader.setAutoTransform(True)
    if max_size and max_size.width() > 0 and max_size.height() > 0:
        image_size = reader.size()
        if image_size.isValid():
            reader.setScaledSize(image_size.scaled(max_size, Qt.KeepAspectRatio))
    return QPixmap.fromImage(reader.read())