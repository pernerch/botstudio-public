import re
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Qt, Signal
from PySide6.QtGui import QTextCursor, QTextDocument
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QFileDialog, QHBoxLayout, QLabel, QListWidget,
    QListWidgetItem, QPushButton, QPlainTextEdit, QTabWidget,
    QTextBrowser, QVBoxLayout,
)

from services.package_service import (
    MAX_PACKAGE_IMAGES,
    MAX_PUBLISH_ARCHIVE_BYTES,
)
from ui.i18n.translator import t
from services.package_description_service import PackageDescriptionService


class _ImageSignals(QObject):
    finished = Signal(object)


class _ImageTask(QRunnable):
    def __init__(self, operation, generation):
        super().__init__()
        self.operation = operation
        self.generation = generation
        self.signals = _ImageSignals()

    def run(self):
        try:
            result = self.operation()
        except Exception as error:
            result = (self.generation, [], [str(error) or type(error).__name__], False)
        self.signals.finished.emit(result)


class _PackageMarkdownPreview(QTextBrowser):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setOpenLinks(False)
        self.setOpenExternalLinks(False)
        self.images = {}

    def loadResource(self, resource_type, url):
        if resource_type != QTextDocument.ImageResource:
            return None
        if url.scheme() or url.authority() or url.hasQuery() or url.hasFragment():
            return None
        return self.images.get(url.path())


class PackageMarkdownEditor(QDialog):
    """A compact Markdown editor for package descriptions and image attachments."""

    def __init__(self, markdown="", package_assets=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle(t("packages.markdown_title"))
        self.resize(700, 520)
        self.markdown = markdown
        self.package_assets = dict(package_assets or {})
        self._busy = False
        self._generation = 0
        self._images = {}
        self.package_asset_bytes = {}
        self._worker = None

        layout = QVBoxLayout(self)
        toolbar = QHBoxLayout()
        for label, prefix, suffix in (
            (t("packages.markdown_bold"), "**", "**"),
            (t("packages.markdown_italic"), "*", "*"),
            (t("packages.markdown_heading"), "## ", ""),
            (t("packages.markdown_list"), "- ", ""),
        ):
            button = QPushButton(label)
            button.clicked.connect(
                lambda _checked=False, opening=prefix, closing=suffix:
                    self._format_selection(opening, closing)
            )
            toolbar.addWidget(button)
        self.attach_button = QPushButton(t("packages.markdown_attach"))
        toolbar.addWidget(self.attach_button)
        layout.addLayout(toolbar)

        self.tabs = QTabWidget()
        self.editor = QPlainTextEdit()
        self.editor.setPlainText(markdown)
        self.editor.setPlaceholderText(t("packages.markdown_placeholder"))
        self.preview = _PackageMarkdownPreview()
        self.tabs.addTab(self.editor, t("packages.markdown_edit"))
        self.tabs.addTab(self.preview, t("packages.markdown_preview"))
        layout.addWidget(self.tabs, stretch=1)

        attachments_row = QHBoxLayout()
        self.attachments = QListWidget()
        self.attachments.setMaximumHeight(76)
        self.remove_button = QPushButton(t("packages.markdown_remove"))
        attachments_row.addWidget(self.attachments, stretch=1)
        attachments_row.addWidget(self.remove_button)
        layout.addLayout(attachments_row)
        self.status = QLabel()
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.Save | QDialogButtonBox.Cancel,
        )
        self.buttons.button(QDialogButtonBox.Save).setText(t("common.save"))
        self.buttons.button(QDialogButtonBox.Cancel).setText(t("common.cancel"))
        layout.addWidget(self.buttons)

        self.attach_button.clicked.connect(self._attach_images)
        self.remove_button.clicked.connect(self._remove_image)
        self.buttons.accepted.connect(self._save)
        self.buttons.rejected.connect(self.reject)
        self.editor.textChanged.connect(self._render_preview)
        self._refresh_attachments()
        self._render_preview()
        if self.package_assets:
            self._load_images(list(self.package_assets.values()), initial=True)

    def _format_selection(self, opening, closing):
        cursor = self.editor.textCursor()
        selected = cursor.selectedText().replace("\u2029", "\n")
        if not selected:
            cursor.insertText(f"{opening}{closing}")
            cursor.movePosition(QTextCursor.Left, QTextCursor.MoveAnchor, len(closing))
        else:
            cursor.insertText(f"{opening}{selected}{closing}")
        self.editor.setTextCursor(cursor)
        self.editor.setFocus()

    def _attach_images(self):
        paths, _ = QFileDialog.getOpenFileNames(
            self,
            t("packages.markdown_attach"),
            "",
            "Images (*.png *.jpg *.jpeg *.webp *.gif *.bmp)",
        )
        if not paths:
            return
        self._load_images(paths)

    def _load_images(self, paths, initial=False):
        if self._busy:
            return
        self._busy = True
        self._generation += 1
        generation = self._generation
        self.attach_button.setEnabled(False)
        self.remove_button.setEnabled(False)
        self.buttons.button(QDialogButtonBox.Save).setEnabled(False)
        self.status.setText(t("packages.markdown_image_working"))

        def load():
            results, errors = [], []
            total_size = sum(len(data) for data in self.package_asset_bytes.values())
            if len(paths) > MAX_PACKAGE_IMAGES:
                errors.append(t("packages.markdown_image_limit").format(limit=MAX_PACKAGE_IMAGES))
            for raw_path in paths[:MAX_PACKAGE_IMAGES]:
                if generation != self._generation:
                    break
                try:
                    source = Path(raw_path).expanduser()
                    data, image = PackageDescriptionService.read_image(source)
                    if not initial:
                        total_size += len(data)
                        if total_size > MAX_PUBLISH_ARCHIVE_BYTES:
                            raise ValueError(t("packages.markdown_image_total"))
                    results.append((source.resolve(), data, image))
                except Exception as error:
                    errors.append(f"{Path(raw_path).name}: {error}")
            return generation, results, errors, initial

        self._worker = _ImageTask(load, generation)
        self._worker.signals.finished.connect(self._images_loaded)
        QThreadPool.globalInstance().start(self._worker)

    def _images_loaded(self, result):
        generation, images, errors, initial = result
        if generation != self._generation:
            return
        self._busy = False
        self.attach_button.setEnabled(True)
        self.buttons.button(QDialogButtonBox.Save).setEnabled(True)
        cursor = self.editor.textCursor()
        for source, data, image in images:
            if initial:
                for name, existing in self.package_assets.items():
                    if existing == source:
                        self._images[name] = image
                        self.package_asset_bytes[name] = data
                continue
            if source in self.package_assets.values():
                continue
            if len(self.package_assets) >= MAX_PACKAGE_IMAGES:
                errors.append(t("packages.markdown_image_limit").format(limit=MAX_PACKAGE_IMAGES))
                break
            extension = source.suffix.lower()
            for index in range(1, MAX_PACKAGE_IMAGES + 1):
                filename = f"image-{index:02d}{extension}"
                archive_path = f"content/package-assets/{filename}"
                if archive_path not in self.package_assets:
                    break
            else:
                self.status.setText(
                    t("packages.markdown_image_limit").format(limit=MAX_PACKAGE_IMAGES)
                )
                return
            self.package_assets[archive_path] = source
            self.package_asset_bytes[archive_path] = data
            self._images[archive_path] = image
            alt_text = source.stem.replace("\\", " ").replace("[", "(").replace("]", ")")
            cursor.insertText(f"![{alt_text}]({archive_path})\n")
        self.editor.setTextCursor(cursor)
        self._refresh_attachments()
        self._render_preview()
        self.status.setText(
            t("packages.markdown_image_error").format(error="\n".join(errors))
            if errors else ""
        )

    def reject(self):
        self._generation += 1
        super().reject()

    def _remove_image(self):
        item = self.attachments.currentItem()
        if item is None:
            return
        archive_path = item.data(Qt.UserRole)
        source = self.package_assets.pop(archive_path, None)
        self._images.pop(archive_path, None)
        self.package_asset_bytes.pop(archive_path, None)
        if source is None:
            return
        markdown = self.editor.toPlainText()
        image_reference = re.compile(
            r"!\[[^\]]*\]\(" + re.escape(archive_path) + r"\)(?:\n|$)?"
        )
        markdown = image_reference.sub("", markdown)
        self.editor.setPlainText(markdown)
        self._refresh_attachments()
        self._render_preview()

    def _refresh_attachments(self):
        self.attachments.clear()
        for archive_path, source in self.package_assets.items():
            item = QListWidgetItem(Path(source).name)
            item.setData(Qt.UserRole, archive_path)
            item.setToolTip(archive_path)
            self.attachments.addItem(item)
        self.remove_button.setEnabled(bool(self.package_assets))

    def _render_preview(self):
        self.preview.images = dict(self._images)
        self.preview.setDocument(QTextDocument(self.preview))
        cursor = self.preview.textCursor()
        cursor.insertMarkdown(
            self.editor.toPlainText(),
            QTextDocument.MarkdownNoHTML,
        )
        self.preview.document().setModified(False)

    def _save(self):
        if self._busy:
            return
        if set(self.package_assets) != set(self._images):
            self.status.setText(t("packages.markdown_image_invalid"))
            return
        self.markdown = self.editor.toPlainText()
        self.accept()
