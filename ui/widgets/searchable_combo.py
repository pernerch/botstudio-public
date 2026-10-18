from PySide6.QtCore import QEvent, QTimer, Qt
from PySide6.QtWidgets import QComboBox, QCompleter


class SearchableComboBox(QComboBox):
    """Editable selector with case-insensitive substring search."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setEditable(True)
        self.setInsertPolicy(QComboBox.NoInsert)
        self.setMaxVisibleItems(20)

        completer = self.completer()
        completer.setCaseSensitivity(Qt.CaseInsensitive)
        completer.setFilterMode(Qt.MatchContains)
        completer.setCompletionMode(QCompleter.PopupCompletion)
        self.lineEdit().setPlaceholderText("Search or select...")
        self.lineEdit().textEdited.connect(self._show_matches)
        self.lineEdit().installEventFilter(self)

    def focusInEvent(self, event):
        super().focusInEvent(event)
        QTimer.singleShot(0, self._prepare_search)

    def eventFilter(self, watched, event):
        if watched is self.lineEdit() and event.type() == QEvent.MouseButtonPress:
            QTimer.singleShot(0, self._prepare_search)
        if (
            watched is self.lineEdit()
            and event.type() == QEvent.KeyPress
            and event.key() in (Qt.Key_Return, Qt.Key_Enter)
            and self.completer().popup().isVisible()
        ):
            completion = self.completer().currentCompletion()
            if not completion and self.completer().completionCount() == 1:
                completion = self.completer().completionModel().index(0, 0).data()
            self._activate_completion(str(completion or ""))
            return True
        return super().eventFilter(watched, event)

    def _prepare_search(self):
        if self.currentData() in (None, ""):
            self.lineEdit().clear()
        else:
            self.lineEdit().selectAll()

    def _show_matches(self, text):
        if not text.strip():
            self.completer().popup().hide()
            return
        self.hidePopup()
        completer = self.completer()
        completer.setCompletionPrefix(text)
        completer.complete()

    def _activate_completion(self, text):
        index = self.findText(text, Qt.MatchExactly)
        if index < 0:
            return
        self.setCurrentIndex(index)
        self.setEditText(self.itemText(index))
        self.activated.emit(index)
        self.textActivated.emit(self.itemText(index))

    def showPopup(self):
        # keep the current text visible when opening the dropdown so the
        # user sees the active value instead of an empty edit line
        if self.currentData() not in (None, ""):
            self.lineEdit().setText(self.currentText())
        super().showPopup()

    def hidePopup(self):
        super().hidePopup()
        # after a pick the line edit must show the committed value, not the
        # half-typed search prefix
        self.lineEdit().setText(self.currentText())
