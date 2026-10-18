from PySide6.QtCore import Qt, Signal

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QAbstractItemView,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSlider,
    QSpinBox,
    QSplitter,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ui.i18n.translator import (
    t,
)
from ui.widgets.searchable_combo import SearchableComboBox

from services.profile_service import (
    ProfileGameFrozenError,
    ProfileService,
)

from services.game_service import (
    GameService,
)


class ProfileTreeWidget(QTreeWidget):
    profiles_reordered = Signal(str, list)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setColumnCount(1)
        self.setHeaderHidden(True)
        self.setDragDropMode(QAbstractItemView.InternalMove)
        self.setDefaultDropAction(Qt.MoveAction)
        self.setDropIndicatorShown(True)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)

    def dropEvent(self, event):
        source = self.currentItem()
        target = self.itemAt(event.position().toPoint())
        if (
            source is None
            or source.parent() is None
            or target is None
            or target.parent() is not source.parent()
        ):
            event.ignore()
            return

        game_item = source.parent()
        if source is target:
            event.acceptProposedAction()
            return

        target_rect = self.visualItemRect(target)
        insert_before = event.position().y() < target_rect.center().y()
        source_index = game_item.indexOfChild(source)
        game_item.takeChild(source_index)
        target_index = game_item.indexOfChild(target)
        insert_index = target_index if insert_before else target_index + 1
        game_item.insertChild(insert_index, source)
        event.acceptProposedAction()
        profile_ids = [
            str(game_item.child(index).data(0, Qt.UserRole))
            for index in range(game_item.childCount())
        ]
        self.profiles_reordered.emit(
            str(game_item.data(0, Qt.UserRole)),
            profile_ids,
        )

    def dragMoveEvent(self, event):
        source = self.currentItem()
        target = self.itemAt(event.position().toPoint())
        if (
            source is not None
            and source.parent() is not None
            and target is not None
            and target.parent() is source.parent()
        ):
            event.acceptProposedAction()
            return
        event.ignore()


class ProfileView(QWidget):
    def __init__(
        self,
    ):
        super().__init__()

        self.profile_service = (
            ProfileService()
        )

        self.game_service = (
            GameService()
        )

        self.current_profile = None

        self.is_loading = False

        self.is_dirty = False

        root = QHBoxLayout(
            self
        )

        splitter = QSplitter(
            Qt.Horizontal
        )

        # --------------------------------------------------
        # LIST
        # --------------------------------------------------

        left_panel = QWidget()

        left_layout = QVBoxLayout(
            left_panel
        )

        left_layout.addWidget(
            QLabel(
                t(
                    "profile.existing"
                )
            )
        )

        self.profile_list = ProfileTreeWidget()
        self.profile_list.profiles_reordered.connect(
            self._save_profile_order
        )

        left_layout.addWidget(
            self.profile_list,
            stretch=1,
        )

        splitter.addWidget(
            left_panel
        )

        # --------------------------------------------------
        # EDITOR
        # --------------------------------------------------

        right_panel = QWidget()

        right_layout = QVBoxLayout(
            right_panel
        )

        right_layout.addWidget(
            QLabel(
                t(
                    "profile.details"
                )
            )
        )

        editor_card = QFrame()

        editor_card.setFrameShape(
            QFrame.StyledPanel
        )

        editor_layout = (
            QVBoxLayout(
                editor_card
            )
        )

        form = QFormLayout()

        self.id_edit = (
            QLineEdit()
        )

        self.name_edit = (
            QLineEdit()
        )

        self.game_combo = SearchableComboBox()

        self.emulator_spin = (
            QSpinBox()
        )

        self.emulator_spin.setRange(
            0,
            999,
        )

        self.priority_slider = (
            QSlider(
                Qt.Horizontal
            )
        )

        self.priority_slider.setRange(
            0,
            100,
        )

        self.priority_spin = (
            QSpinBox()
        )

        self.priority_spin.setRange(
            0,
            100,
        )

        self.priority_slider.valueChanged.connect(
            self.priority_spin.setValue
        )

        self.priority_spin.valueChanged.connect(
            self.priority_slider.setValue
        )

        priority_layout = (
            QHBoxLayout()
        )

        priority_layout.addWidget(
            self.priority_slider,
            stretch=1,
        )

        priority_layout.addWidget(
            self.priority_spin,
        )

        priority_widget = QWidget()

        priority_widget.setLayout(
            priority_layout
        )

        self.enabled_checkbox = (
            QCheckBox()
        )

        form.addRow(
            t(
                "profile.id"
            ),
            self.id_edit,
        )

        form.addRow(
            t(
                "profile.name"
            ),
            self.name_edit,
        )

        form.addRow(
            t(
                "profile.game"
            ),
            self.game_combo,
        )

        form.addRow(
            t(
                "profile.emulator"
            ),
            self.emulator_spin,
        )

        form.addRow(
            t(
                "profile.priority"
            ),
            priority_widget,
        )

        form.addRow(
            t(
                "profile.enabled"
            ),
            self.enabled_checkbox,
        )

        editor_layout.addLayout(
            form
        )

        buttons = QHBoxLayout()

        self.add_button = QPushButton(
            t(
                "common.add"
            )
        )

        self.save_button = QPushButton(
            t(
                "common.save"
            )
        )

        self.clone_button = QPushButton(
            t(
                "common.clone"
            )
        )

        self.delete_button = QPushButton(
            t(
                "common.delete"
            )
        )

        buttons.addWidget(
            self.add_button
        )

        buttons.addWidget(
            self.save_button
        )

        buttons.addWidget(
            self.clone_button
        )

        buttons.addWidget(
            self.delete_button
        )

        buttons.addStretch()

        editor_layout.addLayout(
            buttons
        )

        right_layout.addWidget(
            editor_card
        )

        right_layout.addStretch()

        splitter.addWidget(
            right_panel
        )

        splitter.setStretchFactor(
            0,
            1,
        )

        splitter.setStretchFactor(
            1,
            3,
        )

        root.addWidget(
            splitter
        )

        # --------------------------------------------------
        # EVENTS
        # --------------------------------------------------

        self.add_button.clicked.connect(
            self.on_add
        )

        self.save_button.clicked.connect(
            self.on_save
        )

        self.clone_button.clicked.connect(
            self.on_clone
        )

        self.delete_button.clicked.connect(
            self.on_delete
        )

        self.profile_list.itemClicked.connect(
            self.on_profile_selected
        )

        self.id_edit.textChanged.connect(
            self.on_changed
        )

        self.name_edit.textChanged.connect(
            self.on_changed
        )

        self.game_combo.currentIndexChanged.connect(
            self.on_changed
        )

        self.emulator_spin.valueChanged.connect(
            self.on_changed
        )

        self.priority_spin.valueChanged.connect(
            self.on_changed
        )

        self.enabled_checkbox.stateChanged.connect(
            self.on_changed
        )

        # --------------------------------------------------
        # LOAD
        # --------------------------------------------------

        self.load_games()

        if self.game_combo.count():

            self.game_combo.setCurrentIndex(
                0
            )

        self.refresh_profiles()

    def load_games(
        self,
    ):

        selected_game_id = self.game_combo.currentData()
        self.game_combo.clear()

        games = (
            self.game_service.load()
        )

        for game in sorted(
            games,
            key=lambda item: str(item.get("display_name", item.get("id", ""))).casefold(),
        ):

            self.game_combo.addItem(
                game["display_name"],
                game["id"],
            )

        index = self.game_combo.findData(selected_game_id)
        if index >= 0:
            self.game_combo.setCurrentIndex(index)

    def refresh_profiles(
        self,
    ):

        selected_id = (
            self.current_profile.get("botstudio_profile_id")
            or self.current_profile.get("id")
            if self.current_profile
            else None
        )
        self.profile_list.clear()

        games = {
            str(game.get("id", "")): str(
                game.get("display_name", game.get("id", ""))
            )
            for game in self.game_service.load()
        }
        groups = {}
        for profile in self.profile_service.load_all():
            groups.setdefault(str(profile.get("game", "")), []).append(profile)

        game_ids = sorted(
            groups,
            key=lambda game_id: games.get(game_id, game_id).casefold(),
        )
        selected_item = None
        for game_id in game_ids:
            game_item = QTreeWidgetItem([games.get(game_id, game_id)])
            game_item.setData(0, Qt.UserRole, game_id)
            game_item.setFlags(Qt.ItemIsEnabled)
            self.profile_list.addTopLevelItem(game_item)

            for profile in groups[game_id]:
                profile_id = str(profile.get("botstudio_profile_id") or profile.get("id") or "")
                item = QTreeWidgetItem([
                    f"{profile.get('name', '')} | "
                    f"{t('profile.emulator')} {profile.get('emulator_instance', 0)} | "
                    f"{t('profile.priority')} {profile.get('priority', 0)}"
                ])
                item.setData(0, Qt.UserRole, profile_id)
                game_item.addChild(item)
                if profile_id == selected_id:
                    selected_item = item

            game_item.setExpanded(True)

        if selected_item is not None:
            self.profile_list.setCurrentItem(selected_item)

    def _save_profile_order(self, game_id, profile_ids):
        self.profile_service.reorder_profiles(game_id, profile_ids)
        self.refresh_profiles()

    def load_profile_into_editor(
        self,
        profile,
    ):

        self.is_loading = True

        self.current_profile = profile

        self.id_edit.setText(
            profile.get(
                "id",
                "",
            )
        )

        self.name_edit.setText(
            profile.get(
                "name",
                "",
            )
        )

        game_id = profile.get(
            "game",
            "",
        )

        index = (
            self.game_combo.findData(
                game_id
            )
        )

        if index >= 0:

            self.game_combo.setCurrentIndex(
                index
            )

        self.emulator_spin.setValue(
            profile.get(
                "emulator_instance",
                0,
            )
        )

        self.priority_spin.setValue(
            profile.get(
                "priority",
                50,
            )
        )

        self.enabled_checkbox.setChecked(
            profile.get(
                "enabled",
                True,
            )
        )

        self.is_dirty = False
        self.is_loading = False

    def on_changed(
        self,
    ):
        if self.is_loading:
            return
        
        self.is_dirty = True


    def confirm_discard_changes(
        self,
    ):

        if self.current_profile is None:
            return True

        if not self.is_dirty:
            return True

        result = QMessageBox.question(
            self,
            t(
                "profile.unsaved_title"
            ),
            t(
                "profile.unsaved_text"
            ),
            QMessageBox.Yes
            | QMessageBox.No,
            QMessageBox.No,
        )

        return (
            result
            == QMessageBox.Yes
        )

    def on_profile_selected(
        self,
        item,
        column=0,
    ):

        if item.parent() is None:
            return

        if not self.confirm_discard_changes():
            return

        profile_id = (
            item.data(
                0,
                Qt.UserRole
            )
        )

        profile = (
            self.profile_service.get_profile(
                profile_id
            )
        )

        if profile:

            self.load_profile_into_editor(
                profile
            )

    def on_add(
        self,
    ):

        if not self.confirm_discard_changes():
            return


        profile = (
            self.profile_service
            .get_new_profile()
        )

        self.load_profile_into_editor(
            profile
        )

    def on_save(
        self,
    ):

        if not self.id_edit.text().strip():

            QMessageBox.warning(
                self,
                t(
                    "profile.validation_title"
                ),
                t(
                    "profile.validation_id"
                ),
            )

            return


        if not self.name_edit.text().strip():

            QMessageBox.warning(
                self,
                t(
                    "profile.validation_title"
                ),
                t(
                    "profile.validation_name"
                ),
            )

            return
        

        if not self.current_profile:
            return

        profile = dict(
            self.current_profile
        )

        profile["id"] = (
            self.id_edit.text().strip()
        )

        profile["name"] = (
            self.name_edit.text().strip()
        )

        profile["game"] = (
            self.game_combo.currentData()
        )

        profile["emulator_instance"] = (
            self.emulator_spin.value()
        )

        profile["priority"] = (
            self.priority_spin.value()
        )

        profile["enabled"] = (
            self.enabled_checkbox.isChecked()
        )

        is_new_profile = not profile.get("botstudio_profile_id")
        if is_new_profile:
            answer = QMessageBox.question(
                self,
                t("profile.game_freeze_title"),
                t("profile.game_freeze_text"),
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                return

        try:
            saved_profile = self.profile_service.save_profile(
                profile
            )
        except ProfileGameFrozenError:
            QMessageBox.warning(
                self,
                t("profile.game_frozen_title"),
                t("profile.game_frozen_text"),
            )
            return

        self.current_profile = (
            saved_profile
        )

        self.is_dirty = False

        self.refresh_profiles()

    def on_clone(
        self,
    ):

        if not self.current_profile:
            return

        if not self.confirm_discard_changes():
            return

        clone = dict(
            self.current_profile
        )

        clone["id"] = ""

        clone["name"] = (
            clone.get(
                "name",
                "",
            )
            + f" {t('common.copy')}"
        )

        self.load_profile_into_editor(
            clone
        )

    def on_delete(
        self,
    ):

        if not self.current_profile:
            return

        result = QMessageBox.question(
            self,
            t(
                "profile.delete_title"
            ),
            (
                f"{t('profile.delete_text')}\n\n"
                f"{t('profile.label_id')}: "
                f"{self.current_profile['id']}\n"
                f"{t('profile.label_name')}: "
                f"{self.current_profile['name']}"
            ),
            QMessageBox.Yes
            | QMessageBox.No,
            QMessageBox.No,
        )

        if result != QMessageBox.Yes:
            return

        self.profile_service.delete_profile(
            self.current_profile[
                "id"
            ]
        )

        self.current_profile = None

        self.is_dirty = False

        self.id_edit.clear()

        self.name_edit.clear()

        self.emulator_spin.setValue(
            0
        )

        self.priority_spin.setValue(
            50
        )

        self.enabled_checkbox.setChecked(
            False
        )

        self.refresh_profiles()
