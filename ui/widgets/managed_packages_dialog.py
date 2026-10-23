from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Qt, QUrl, Signal, Slot
from PySide6.QtGui import QDesktopServices, QTextCursor, QTextDocument
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QDialog, QFileDialog, QHBoxLayout, QHeaderView,
    QLabel, QInputDialog, QLineEdit, QMessageBox, QPushButton, QTabWidget,
    QTableWidget, QTableWidgetItem, QTextBrowser, QTreeWidget, QTreeWidgetItem,
    QVBoxLayout, QWidget,
)

from services.logging_service import get_logger
from services.catalog_submission_service import CatalogSubmissionService
from services.github_submission_service import GitHubSubmissionService
from services.github_catalog_service import GitHubCatalogService
from services.game_registry_service import GameRegistryService
from services.managed_package_service import ManagedPackageService
from services.package_service import PackageError, PackageService
from services.package_description_service import PackageDescriptionService
from services.package_outbox_service import PackageOutboxService
from services.package_activation_service import PackageActivationService
from services.file_io import read_json
from services.application_paths import ApplicationPaths
from services.reinitialize_service import get_default_reinitialize_service
from ui.i18n.translator import t
from ui.widgets.package_preparation_dialog import PackagePreparationDialog


class _PackageDetails(QTextBrowser):
    def __init__(self):
        super().__init__()
        self.setOpenLinks(False)
        self.setOpenExternalLinks(False)
        self.images = {}

    def loadResource(self, resource_type, url):
        if (resource_type != QTextDocument.ImageResource or url.scheme()
                or url.authority() or url.hasQuery() or url.hasFragment()):
            return None
        return self.images.get(url.path())

    def clear(self):
        self.images = {}
        self.setDocument(QTextDocument(self))

    def appendPlainText(self, text):
        cursor = QTextCursor(self.document())
        cursor.movePosition(QTextCursor.End)
        cursor.insertText("\n" + text)


class _PackageSignals(QObject):
    finished = Signal(object, str)


class _PackageTask(QRunnable):
    def __init__(self, operation):
        super().__init__()
        self.operation = operation
        self.signals = _PackageSignals()

    def run(self):
        try:
            result = self.operation()
        except Exception as error:
            # The GUI boundary must complete even when a persistence operation fails.
            self.signals.finished.emit(None, str(error) or type(error).__name__)
        else:
            self.signals.finished.emit(result, "")


class _ActivationProgress(QDialog):
    def reject(self):
        pass

    def closeEvent(self, event):
        event.ignore()


class ManagedPackagesDialog(QDialog):
    """Simple exchange with automatic safe copies and optional advanced actions."""

    def __init__(self, parent=None, service=None, activation_service=None, embedded=False, outbox_service=None):
        super().__init__(parent)
        self.embedded = embedded
        if embedded:
            self.setWindowFlags(Qt.Widget)
        self.activation_progress = None
        self.service = service if service is not None else ManagedPackageService()
        self.logger = get_logger("packages")
        self.activation_service = activation_service
        self.outbox_service = outbox_service or PackageOutboxService(
            self.service.packages, self.service.root.parent / "package-outbox",
        )
        self.packed_packages = []
        self.worker = None
        self._completion = None
        self.confirmation = None
        self.preview = None
        self.catalog_result = None
        self.installations = []
        self.setWindowTitle(t("packages.title"))
        self.resize(960, 680)
        layout = QVBoxLayout(self)
        tabs = QTabWidget()
        self.tabs = tabs
        upload_tab = QWidget()
        upload_layout = QVBoxLayout(upload_tab)
        self.pack_new = QPushButton(t("packages.check_pack"))
        self.packed_table = QTableWidget(0, 4)
        self.packed_table.setHorizontalHeaderLabels([
            t("packages.package"), t("packages.version"), t("settings.game"), t("packages.state"),
        ])
        self.packed_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.packed_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.packed_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.packed_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.packed_table.itemSelectionChanged.connect(self._controls)
        self.upload_packed = QPushButton(t("packages.upload_packed"))
        self.pack_new.clicked.connect(self._prepare_package_for_upload)
        self.upload_packed.clicked.connect(self._upload_packed)
        upload_layout.addWidget(self.pack_new)
        public_notice = QLabel(t("packages.upload_notice"))
        public_notice.setWordWrap(True)
        upload_layout.addWidget(public_notice)
        upload_layout.addWidget(self.packed_table)
        upload_layout.addWidget(self.upload_packed)
        tabs.addTab(upload_tab, t("packages.packed_tab"))
        local_tab = QWidget()
        local_layout = QVBoxLayout(local_tab)
        catalog_tab = QWidget()
        catalog_layout = QVBoxLayout(catalog_tab)
        if embedded:
            sources = QLabel(t("packages.sources_hint"))
            sources.setWordWrap(True)
            local_layout.addWidget(sources)
        notice = QLabel(t("packages.notice"))
        notice.setWordWrap(True)
        local_layout.addWidget(notice)
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels([
            t("packages.package"), t("packages.version"), t("settings.game"),
            t("packages.publisher"), t("packages.state"), t("packages.active_state"),
        ])
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.itemSelectionChanged.connect(self._selection_changed)
        local_layout.addWidget(self.table)
        row = QHBoxLayout()
        self.prepare_upload = QPushButton(t("packages.prepare_open"))
        row.addWidget(self.prepare_upload)
        local_layout.addLayout(row)
        self.more_actions = QPushButton(t("packages.more_actions"))
        self.actions_dialog = QDialog(self)
        self.actions_dialog.setWindowTitle(t("packages.more_actions"))
        action_layout = QVBoxLayout(self.actions_dialog)
        self.refresh = QPushButton(t("packages.refresh"))
        self.choose = QPushButton(t("packages.inspect"))
        self.inspect_original = QPushButton(t("packages.inspect_original"))
        self.open_working = QPushButton(t("packages.open_working"))
        self.open_original = QPushButton(t("packages.open_original"))
        for button in (self.refresh, self.choose, self.inspect_original, self.open_working, self.open_original):
            action_layout.addWidget(button)
        self.install = QPushButton(t("packages.install"))
        self.update = QPushButton(t("packages.update"))
        self.submission_check = QPushButton(t("packages.submission_check"))
        self.submission_send = QPushButton(t("packages.submission_send"))
        self.activate = QPushButton(t("packages.activate"))
        self.active_update = QPushButton(t("packages.active_update"))
        for button in (
            self.install, self.update, self.submission_check, self.submission_send,
            self.activate, self.active_update,
        ):
            action_layout.addWidget(button)
        self.actions_close = QPushButton(t("common.close"))
        action_layout.addWidget(self.actions_close)
        self.actions_close.clicked.connect(self.actions_dialog.accept)
        self.more_actions.clicked.connect(lambda: self.actions_dialog.exec())
        search_row = QHBoxLayout()
        self.catalog_search = QLineEdit()
        self.catalog_search.setPlaceholderText(t("packages.catalog_search"))
        self.catalog_type = QComboBox()
        for label_key, kind in (
            ("packages.catalog_type_all", "all"),
            ("packages.catalog_type_function", "function"),
            ("packages.catalog_type_helper", "helper"),
            ("packages.catalog_type_event", "event"),
            ("packages.catalog_type_unknown", "unknown"),
        ):
            self.catalog_type.addItem(t(label_key), kind)
        self.catalog_scope = QLabel()
        self.catalog_scope.setWordWrap(True)
        search_row.addWidget(self.catalog_search, stretch=1)
        search_row.addWidget(self.catalog_type)
        catalog_layout.addLayout(search_row)
        self.catalog_search.textChanged.connect(self._render_catalog_tree)
        self.catalog_type.currentIndexChanged.connect(self._render_catalog_tree)
        self.catalog = QPushButton(t("packages.catalog_load"))
        catalog_actions_row = QHBoxLayout()
        catalog_actions_row.addWidget(self.catalog)
        self.catalog_entries = QTreeWidget()
        self.catalog_entries.setHeaderLabels([t("packages.catalog_tree")])
        self.catalog_entries.setSelectionMode(QAbstractItemView.SingleSelection)
        self.catalog_entries.setMinimumHeight(160)
        self.catalog_entries.currentItemChanged.connect(self._catalog_selection_changed)
        self.catalog_download = QPushButton(t("packages.catalog_download"))
        self.catalog_download.setText(t("packages.download_use"))
        self.catalog_profile = QComboBox()
        self.catalog_profile.addItem(t("packages.target_profile"), "")
        catalog_layout.addWidget(self.catalog_profile)
        self.catalog_download.clicked.connect(self._download_catalog)
        self.catalog_more = QPushButton(t("packages.catalog_more"))
        self.catalog_more.clicked.connect(self._load_more_catalog)
        catalog_actions_row.addWidget(self.catalog_download)
        catalog_actions_row.addWidget(self.catalog_more)
        catalog_layout.addLayout(catalog_actions_row)
        catalog_layout.addWidget(self.catalog_scope)
        catalog_layout.addWidget(self.catalog_entries, stretch=1)
        tabs.addTab(catalog_tab, t("packages.catalog_tab"))
        tabs.addTab(local_tab, t("packages.more_actions"))
        tabs.currentChanged.connect(self._tab_changed)
        layout.addWidget(tabs, stretch=1)
        action_row = QHBoxLayout()
        action_row.addWidget(self.more_actions)
        action_row.addStretch(1)
        layout.addLayout(action_row)
        self.details = _PackageDetails()
        self.details.setReadOnly(True)
        self.details.setPlaceholderText(t("packages.simple_hint"))
        layout.addWidget(self.details)
        row = QHBoxLayout()
        self.close_button = QPushButton(t("common.close"))
        self.close_button.setVisible(not embedded)
        row.addWidget(self.close_button)
        layout.addLayout(row)
        self.status = QLabel()
        self.status.setTextFormat(Qt.PlainText)
        self.status.setWordWrap(True)
        self.status.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(self.status)
        self.refresh.clicked.connect(self.reload)
        self.catalog.clicked.connect(self._load_catalog)
        self.prepare_upload.clicked.connect(self._prepare_package_for_upload)
        self.choose.clicked.connect(self._choose_archive)
        self.inspect_original.clicked.connect(self._inspect_installed)
        self.install.clicked.connect(lambda: self._confirm("install"))
        self.update.clicked.connect(lambda: self._confirm("update"))
        self.submission_check.clicked.connect(self._prepare_submission)
        self.submission_send.clicked.connect(self._prepare_remote_submission)
        self.open_working.clicked.connect(lambda: self._open_location("working_directory"))
        self.open_original.clicked.connect(lambda: self._open_location("original_archive"))
        self.close_button.clicked.connect(self.reject)
        self.activate.clicked.connect(self._prepare_activation)
        self.active_update.clicked.connect(self._prepare_active_update)
        self._controls()

    def _selected(self):
        row = self.table.currentRow()
        return self.installations[row] if 0 <= row < len(self.installations) else None

    def _prepare_package_for_upload(self):
        selected = self._selected()
        game_id = selected.get("game_id") if selected else None
        PackagePreparationDialog(
            self,
            package_service=self.service.packages,
            game_id=game_id,
            simple_flow=True,
            outbox_service=self.outbox_service,
        ).exec()
        self._reload_outbox()

    def _tab_changed(self, index):
        if index == 0 and self.worker is None:
            self._reload_outbox()

    def _reload_outbox(self):
        self._run(self.outbox_service.list_packages, self._outbox_loaded)

    def _outbox_loaded(self, packages):
        self.packed_packages = packages
        self.packed_table.setRowCount(len(packages))
        for row, entry in enumerate(packages):
            values = (
                entry["name"], entry["version"], entry["game_id"],
                t("packages.uploaded") if entry["publication"] else t("packages.packed_ready"),
            )
            for column, value in enumerate(values):
                self.packed_table.setItem(row, column, QTableWidgetItem(value))
        self._controls()

    def _upload_packed(self):
        row = self.packed_table.currentRow()
        if self.worker is not None or not 0 <= row < len(self.packed_packages):
            return
        entry = self.packed_packages[row]
        if entry["publication"]:
            return

        def upload():
            service = GitHubSubmissionService(preflight=CatalogSubmissionService(managed=self.service))
            return self.outbox_service.upload(entry["path"], self.service, service)

        def uploaded(result):
            self._submission_sent(result)
            self._reload_outbox()

        self._run(upload, uploaded)

    def _controls(self):
        idle = self.worker is None and self.confirmation is None
        selected = self._selected()
        valid = selected is not None and "error" not in selected
        for widget in (
            self.table, self.refresh, self.choose, self.close_button,
            self.catalog, self.prepare_upload, self.more_actions,
            self.catalog_search, self.catalog_type,
            self.pack_new, self.packed_table, self.catalog_profile,
        ):
            widget.setEnabled(idle)
        packed_row = self.packed_table.currentRow()
        self.upload_packed.setEnabled(
            idle and 0 <= packed_row < len(self.packed_packages)
            and not self.packed_packages[packed_row]["publication"]
        )
        self.install.setEnabled(idle and self.preview is not None)
        self.update.setEnabled(idle and self.preview is not None and valid)
        self.submission_check.setEnabled(idle and self.preview is not None)
        self.submission_send.setEnabled(idle and self.preview is not None)
        self.open_working.setEnabled(idle and valid)
        self.open_original.setEnabled(idle and valid)
        self.inspect_original.setEnabled(idle and valid)
        active = selected.get("active") if valid else None
        active_available = valid and self.activation_service is not None and "active_error" not in selected
        self.activate.setEnabled(idle and active_available and (active is None or active["state"] == "not_active"))
        self.active_update.setEnabled(idle and active_available and active is not None and active["update_available"])
        self.catalog_entries.setEnabled(idle and self.catalog_result is not None)
        self.catalog_download.setEnabled(
            idle and self.catalog_result is not None and self._catalog_entry() is not None,
        )
        self.catalog_more.setEnabled(
            idle and self.catalog_result is not None and self.catalog_result.get("next_page") is not None,
        )

    def _catalog_entry(self):
        item = self.catalog_entries.currentItem()
        return item.data(0, Qt.UserRole) if item is not None else None

    def _catalog_selection_changed(self, current, previous):
        entry = self._catalog_entry()
        previous_profile = self.catalog_profile.currentData()
        self.catalog_profile.clear()
        self.catalog_profile.addItem(t("packages.target_profile"), "")
        game_id = ((entry or {}).get("package") or {}).get("game_id")
        for profile in (self.catalog_result or {}).get("local_profiles", []):
            if game_id is None or profile.get("game") == game_id:
                self.catalog_profile.addItem(
                    str(profile.get("name") or profile.get("id") or ""),
                    str(profile.get("botstudio_profile_id") or profile.get("id") or ""),
                )
        index = self.catalog_profile.findData(previous_profile)
        if index > 0:
            self.catalog_profile.setCurrentIndex(index)
        elif self.catalog_profile.count() == 2:
            self.catalog_profile.setCurrentIndex(1)
        if self.worker is None and self.confirmation is None:
            self.preview = None
            self.details.setPlainText(
                self._catalog_entry_text(entry) if entry is not None else t("packages.catalog_rules"),
            )
        self._controls()

    def _selection_changed(self):
        self._controls()
        selected = self._selected()
        if selected and selected.get("error"):
            self._error(selected["error"])
        elif selected and selected.get("active_error"):
            self._error(selected["active_error"])

    def _load_catalog(self):
        if self.worker is not None or self.confirmation is not None:
            return
        self.preview = None
        self.catalog_result = None
        self.catalog_entries.clear()
        self.details.clear()
        def load():
            result = GitHubCatalogService().load()
            if self.activation_service is not None:
                games = GameRegistryService.adopt_existing(
                    self.activation_service.root / "games" / "games.json",
                    GameRegistryService().load(),
                )
                profiles = read_json(self.activation_service.root / "profiles" / "profiles.json")
                if not isinstance(profiles, list):
                    raise PackageError("invalid local profile store")
                identities = {
                    game["id"]: game.get("catalog_game_id") or game["id"] for game in games
                }
                result["local_profiles"] = [
                    dict(profile, game=identities.get(profile.get("game"), profile.get("game")))
                    for profile in profiles
                ]
            return result
        self._run(load, self._catalog_loaded)

    def _catalog_loaded(self, result):
        self.catalog_result = result
        self._render_catalog_tree()
        lines = [
            t("packages.catalog_source").format(
                repository=result["repository"], repository_id=result["repository_id"],
            ),
            t("packages.catalog_rules"),
            t("packages.catalog_access"),
            "",
        ]
        for entry in result["entries"]:
            lines.append(self._catalog_entry_text(entry))
        if not result["entries"]:
            lines.append(t("packages.catalog_empty"))
        self.details.setPlainText("\n\n".join(lines))
        self.status.setText(
            t("packages.catalog_loaded").format(count=len(result["entries"]))
        )

    @staticmethod
    def _catalog_type(entry):
        package = entry.get("package")
        if not package:
            return "unknown"
        if package.get("package_type") in ("function", "helper", "event"):
            return package["package_type"]
        package_id = package.get("package_id", "")
        for kind in ("function", "helper", "event"):
            if package_id.startswith(kind + "-"):
                return kind
        return "unknown"

    def _render_catalog_tree(self, *_):
        result = self.catalog_result
        if result is None:
            self.catalog_entries.clear()
            self.catalog_scope.clear()
            return
        self.catalog_entries.clear()
        groups = {}
        query = self.catalog_search.text().strip().casefold()
        selected_type = self.catalog_type.currentData()
        matched = []
        for entry in result["entries"]:
            kind = self._catalog_type(entry)
            if selected_type not in (None, "all", kind):
                continue
            searchable = "\n".join((
                entry.get("name", ""),
                entry.get("tag", ""),
                self._catalog_entry_text(entry),
            )).casefold()
            if query and query not in searchable:
                continue
            matched.append(entry)
            package = entry.get("package")
            kind_label = t(f"packages.catalog_type_{kind}")
            labels = (
                [
                    kind_label, package.get("game_name") or package["game_id"],
                    package.get("package_name") or package["package_id"],
                    f"@{package['publisher'].login} (github:{package['publisher'].account_id})",
                ] if package else [kind_label, t("packages.catalog_unclassified")]
            )
            parent = self.catalog_entries.invisibleRootItem()
            for depth, label in enumerate(labels):
                # Numeric publisher IDs separate channels even when display names change.
                key = (
                    kind, package["game_id"], package["package_id"],
                    package["publisher"].account_id,
                )[:depth + 1] if package else tuple(labels[:depth + 1])
                if key not in groups:
                    groups[key] = QTreeWidgetItem(parent, [label])
                parent = groups[key]
            version = package["version"] if package else entry["tag"]
            if entry["prerelease"]:
                version += " | " + t("packages.catalog_prerelease")
            leaf = QTreeWidgetItem(parent, [f"{version} | {entry['name']}"])
            leaf.setData(0, Qt.UserRole, entry)
            leaf.setToolTip(0, self._catalog_entry_text(entry))
        self.catalog_entries.expandToDepth(1)
        self.catalog_scope.setText(
            t("packages.catalog_scope").format(
                matches=len(matched), loaded=len(result["entries"]),
                pages=(result["next_page"] - 1 if result.get("next_page") else "all"),
            )
        )

    def _load_more_catalog(self):
        if self.worker is not None or self.confirmation is not None or self.catalog_result is None:
            return
        page = self.catalog_result.get("next_page")
        if page is None:
            return
        self._run(lambda: GitHubCatalogService().load(page=page), self._catalog_page_loaded)

    def _catalog_page_loaded(self, result):
        previous = self.catalog_result
        if (
            previous is None or result["repository"] != previous["repository"]
            or result["repository_id"] != previous["repository_id"]
        ):
            self._error(t("packages.catalog_page_changed"))
            return
        entries = {entry["asset_id"]: entry for entry in previous["entries"]}
        for entry in result["entries"]:
            if entry["asset_id"] in entries and entries[entry["asset_id"]] != entry:
                self._error(t("packages.catalog_page_changed"))
                return
            entries[entry["asset_id"]] = entry
        self.preview = None
        self._catalog_loaded({
            **result, "entries": list(entries.values()),
            "local_profiles": previous.get("local_profiles", []),
        })

    @staticmethod
    def _catalog_entry_text(entry):
        text = t("packages.catalog_entry").format(
            name=entry["name"], tag=entry["tag"],
            prerelease=t("packages.catalog_prerelease") if entry["prerelease"] else "",
            size=entry["size"], login=entry["uploader"].login,
            account_id=entry["uploader"].account_id,
            release_id=entry["release_id"], asset_id=entry["asset_id"],
        )
        package = entry.get("package")
        if package:
            text = t("packages.catalog_declared").format(
                game=package.get("game_name") or package["game_id"],
                package=package.get("package_name") or package["package_id"],
                version=package["version"], login=package["publisher"].login,
                account_id=package["publisher"].account_id,
            ) + "\n\n" + text
            if package.get("author"):
                text += "\n" + t("packages.author") + ": " + package["author"]
            if package.get("contents"):
                text += "\n" + t("packages.prepare_files") + "\n" + "\n".join(
                    f"{name}: {count}" for name, count in package["contents"].items()
                )
            if package.get("description"):
                text += "\n\n" + package["description"]
        return text

    def _download_catalog(self):
        if self.worker is not None or self.confirmation is not None or self.catalog_result is None:
            return
        entry = self._catalog_entry()
        if entry is None:
            return
        catalog = self.catalog_result
        if self.activation_service is None:
            self._error(t("packages.download_unavailable"))
            return
        profile_id = self.catalog_profile.currentData() or ""
        if self._catalog_type(entry) == "event" and not profile_id:
            self._error(t("packages.profile_required"))
            return
        self.preview = None
        self.details.clear()
        if self.embedded:
            self.activation_progress = _ActivationProgress(self)
            self.activation_progress.setWindowTitle(t("packages.download_use"))
            QVBoxLayout(self.activation_progress).addWidget(QLabel(t("packages.busy")))
            self.activation_progress.open()

        def download():
            with TemporaryDirectory(prefix="bs-get-") as temporary:
                directory = Path(temporary)
                path = directory / "package.botstudio.zip"
                preview = GitHubCatalogService().download_preview(catalog, entry, path, self.service)
                registered = GameRegistryService().require(preview["manifest"]["game"]["id"])
                GameRegistryService.ensure_local(
                    self.activation_service.root / "games" / "games.json", registered,
                )
                managed = ManagedPackageService(
                    root=ApplicationPaths.long_path(
                        self.service.root.parent / "package-downloads" / uuid4().hex,
                    ),
                    packages=self.service.packages,
                )
                installed = managed.install(
                    path, expected_sha256=preview["archive_sha256"],
                    catalog_source=preview["catalog_source"],
                )
                activation = PackageActivationService(
                    root=self.activation_service.root, managed=managed,
                    execution_guard=self.activation_service.execution_guard, logger=self.logger,
                )
                return activation.import_copy(installed["installation_id"], profile_id)

        self._run(download, self._downloaded)

    def _downloaded(self, result):
        bus = get_default_reinitialize_service()
        for source in ("game", "library", "variable", "function", "scheduler"):
            bus.publish(source, game_id=result["game_id"])
        self.status.setText(t("packages.download_complete"))
        self.details.setPlainText(t("packages.download_complete"))

    def _error(self, message):
        self.status.setText(t("packages.failed").format(error=message))
        self.logger.error("Package Operation Failed", error=message)

    def _run(self, operation, completed):
        if self.worker is not None:
            return
        def with_verified_images():
            result = operation()
            if (isinstance(result, dict) and "manifest" in result
                    and "archive_sha256" in result and "path" in result):
                result = dict(result)
                result["preview_images"] = PackageDescriptionService.preview_images(result)
            return result

        self.worker = _PackageTask(with_verified_images)
        self._completion = completed
        self.worker.signals.finished.connect(self._finished)
        self.status.setText(t("packages.busy"))
        self._controls()
        QThreadPool.globalInstance().start(self.worker)

    @Slot(object, str)
    def _finished(self, result, error):
        completed = self._completion
        self._completion = None
        self.worker = None
        try:
            if error:
                self._error(error)
            else:
                completed(result)
        finally:
            if self.activation_progress is not None:
                self.activation_progress.done(QDialog.Accepted)
                self.activation_progress.deleteLater()
                self.activation_progress = None
            self._controls()

    def reload(self):
        def load():
            return self._load_installations(), self.outbox_service.list_packages()
        def loaded(result):
            self._loaded(result[0])
            self._outbox_loaded(result[1])
        self._run(load, loaded)

    def _load_installations(self):
        installations = self.service.list_installations()
        if self.activation_service is not None:
            for entry in installations:
                if "error" not in entry:
                    try:
                        entry["active"] = self.activation_service.active_status(entry["installation_id"])
                    except (PackageError, OSError, ValueError) as error:
                        entry["active_error"] = str(error)
        return installations

    def _loaded(self, installations):
        selected = self._selected()
        selected_id = selected["installation_id"] if selected else None
        self.installations = installations
        self.table.setRowCount(0)
        for row, entry in enumerate(installations):
            self.table.insertRow(row)
            state = (
                t("packages.damaged") if "error" in entry
                else t("packages.modified") if entry["modified"] else t("packages.clean")
            )
            publisher = entry.get("publisher_key", "")
            if entry.get("publisher_login"):
                publisher = f"@{entry['publisher_login']} ({publisher})"
            active = entry.get("active")
            active_text = "-"
            if "active_error" in entry:
                active_text = t("packages.damaged")
            elif active is not None:
                active_text = t(f"packages.active_{active['state']}")
                if active["version"]:
                    active_text = f"{active['version']} - {active_text}"
                if active["update_available"]:
                    active_text += " - " + t("packages.active_update_available")
            values = (
                entry.get("package_id", entry["installation_id"]),
                entry.get("version", ""), entry.get("game_id", ""),
                publisher, state, active_text,
            )
            for column, value in enumerate(values):
                self.table.setItem(row, column, QTableWidgetItem(value))
            self.table.item(row, 0).setToolTip(self._source_text(entry.get("catalog_source")))
            self.table.item(row, 5).setToolTip(
                entry.get("active_error", "") or "\n".join(active["conflicts"] if active else []),
            )
            if entry["installation_id"] == selected_id:
                self.table.selectRow(row)
        errors = [
            entry.get("error") or entry["active_error"]
            for entry in installations if "error" in entry or "active_error" in entry
        ]
        if errors:
            self._error("\n".join(errors))
        else:
            self.status.setText(t("packages.loaded").format(count=len(installations)))

    def _choose_archive(self):
        path, _ = QFileDialog.getOpenFileName(
            self, t("packages.inspect"), "", "BotStudio (*.botstudio.zip *.zip)",
        )
        if not path:
            return
        self.preview = None
        self.details.clear()
        self._run(lambda: self.service.preview(Path(path)), self._previewed)

    def _previewed(self, preview):
        self.preview = preview
        self.details.clear()
        self.details.images = dict(preview.get("preview_images", {}))
        self.details.setPlainText(
            f"{preview['path']}\nSHA-256: {preview['archive_sha256']}\n\n"
            + self._source_text(preview.get("catalog_source")) + "\n\n",
        )
        cursor = QTextCursor(self.details.document())
        cursor.movePosition(QTextCursor.End)
        cursor.insertMarkdown(
            PackageService.manifest_markdown(preview["manifest"]),
            QTextDocument.MarkdownNoHTML,
        )
        self.status.setText(t("packages.preview_ready"))

    def _prepare_submission(self):
        if self.worker is not None or self.confirmation is not None or self.preview is None:
            return
        preview = self.preview
        self._previewed(preview)
        self._run(
            lambda: CatalogSubmissionService(managed=self.service).prepare(
                Path(preview["path"]), expected_sha256=preview["archive_sha256"],
            ),
            self._submission_prepared,
        )

    def _submission_prepared(self, submission):
        self.details.appendPlainText("\n" + t("packages.submission_summary").format(
            repository=submission["repository"], game=submission["game_id"],
            package=submission["package_id"], version=submission["package_version"],
            login=submission["submitter"]["login"], account_id=submission["submitter"]["account_id"],
            sha256=submission["archive_sha256"], path=submission["archive_path"],
        ))
        self.status.setText(t("packages.submission_checked"))

    def _prepare_remote_submission(self):
        if self.worker is not None or self.confirmation is not None or self.preview is None:
            return
        preview = self.preview
        service = GitHubSubmissionService(preflight=CatalogSubmissionService(managed=self.service))
        self._run(
            lambda: service.prepare(Path(preview["path"]), expected_sha256=preview["archive_sha256"]),
            lambda plan: self._confirm_remote_submission(service, preview["path"], plan),
        )

    def _confirm_remote_submission(self, service, path, plan):
        submission, target = plan["submission"], plan["target"]
        dialog = QMessageBox(
            QMessageBox.Warning, t("packages.submission_send"),
            t("packages.submission_consent").format(
                repository=submission["repository"], repository_id=target["repository_id"],
                login=submission["submitter"]["login"], game=submission["game_id"],
                package=submission["package_id"], version=submission["package_version"],
                sha256=submission["archive_sha256"], size=submission["archive_size"],
                branch=target["base_branch"], commit=target["base_commit"],
            ), QMessageBox.Yes | QMessageBox.No, self,
        )
        dialog.setTextFormat(Qt.PlainText)
        dialog.setDefaultButton(QMessageBox.No)
        self.confirmation = dialog
        dialog.finished.connect(
            lambda result: self._remote_submission_confirmed(result, service, path, plan),
        )
        self._controls()
        dialog.open()

    def _remote_submission_confirmed(self, result, service, path, plan):
        self.confirmation.deleteLater()
        self.confirmation = None
        self._controls()
        if result != QMessageBox.Yes:
            return
        self._run(lambda: service.submit(Path(path), plan=plan), self._submission_sent)

    def _submission_sent(self, result):
        self.details.appendPlainText("\n" + t("packages.submission_sent").format(url=result["url"]))
        self.status.setText(t("packages.submission_sent").format(url=result["url"]))

    @staticmethod
    def _source_text(source):
        if source is None:
            return t("packages.source_local")
        return t("packages.source_receipt").format(
            repository=source["repository"], repository_id=source["repository_id"],
            release_id=source["release_id"], asset_id=source["asset_id"],
            login=source["uploader"]["login"], account_id=source["uploader"]["account_id"],
        )

    def _inspect_installed(self):
        selected = self._selected()
        if selected is None or "error" in selected:
            return
        installation_id = selected["installation_id"]
        self.preview = None
        self.details.clear()

        def inspect():
            status = self.service.status(installation_id)
            preview = self.service.preview(Path(status["original_archive"]))
            if status.get("catalog_source") is not None:
                if preview["archive_sha256"] != status["catalog_source"]["archive_sha256"]:
                    raise PackageError("stored catalog archive changed during inspection")
                preview["catalog_source"] = status["catalog_source"]
            return preview

        self._run(inspect, self._previewed)

    def _confirm(self, operation):
        if self.worker is not None or self.confirmation is not None or self.preview is None:
            return
        selected = self._selected()
        if operation == "update" and (selected is None or "error" in selected):
            return
        preview = self.preview
        installation_id = selected["installation_id"] if selected else None
        message = t("packages.confirm_install")
        if operation == "update":
            message = t("packages.confirm_update").format(
                package=selected["package_id"], version=selected["version"],
            )
        message += "\n\n" + self._source_text(preview.get("catalog_source"))
        message += "\n\n" + t("packages.notice")
        dialog = QMessageBox(
            QMessageBox.Question, t("packages.title"), message,
            QMessageBox.Yes | QMessageBox.No, self,
        )
        dialog.setTextFormat(Qt.PlainText)
        dialog.setDefaultButton(QMessageBox.No)
        self.confirmation = dialog
        dialog.finished.connect(
            lambda result: self._confirmed(result, operation, preview, installation_id),
        )
        self._controls()
        dialog.open()

    def _confirmed(self, result, operation, preview, installation_id):
        self.confirmation.deleteLater()
        self.confirmation = None
        self._controls()
        if result != QMessageBox.Yes:
            return

        def persist():
            arguments = {"expected_sha256": preview["archive_sha256"]}
            if preview.get("catalog_source") is not None:
                arguments["catalog_source"] = preview["catalog_source"]
            if operation == "update":
                return self.service.update(installation_id, Path(preview["path"]), **arguments)
            return self.service.install(Path(preview["path"]), **arguments)

        self._run(persist, self._stored)

    def _stored(self, result):
        self.preview = None
        self.details.clear()
        self.logger.info(
            "Package Stored Locally", installation_id=result["installation_id"],
            version=result["version"],
        )
        self.reload()

    def _open_location(self, key):
        selected = self._selected()
        if selected is None or "error" in selected:
            return
        installation_id = selected["installation_id"]
        self._run(
            lambda: self.service.status(installation_id),
            lambda status: self._location_checked(status, key),
        )

    def _location_checked(self, status, key):
        self.status.setText(
            t("packages.modified") if status["modified"] else t("packages.clean"),
        )
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(status[key])):
            self._error(t("packages.open_failed"))

    def _prepare_activation(self):
        selected = self._selected()
        if (
            self.worker is not None or self.confirmation is not None or selected is None
            or "error" in selected or self.activation_service is None
        ):
            return
        installation_id = selected["installation_id"]
        self._run(
            lambda: self.activation_service.context(installation_id),
            lambda context: self._activation_context(installation_id, context),
        )

    def _prepare_active_update(self):
        selected = self._selected()
        if (
            self.worker is not None or self.confirmation is not None or selected is None
            or "error" in selected or self.activation_service is None
        ):
            return
        self._run(
            lambda: self.activation_service.prepare_update(selected["installation_id"]),
            self._activation_prepared,
        )

    def _activation_context(self, installation_id, context):
        if not context["has_events"]:
            self._activation_profile(installation_id, "")
            return
        profiles = [profile for profile in context["profiles"] if profile["id"]]
        if not profiles:
            self._error(t("packages.profile_required"))
            return
        dialog = QInputDialog(self)
        dialog.setWindowTitle(t("packages.activate"))
        dialog.setLabelText(t("packages.target_profile"))
        dialog.setComboBoxEditable(False)
        labels = [f"{profile['name']} ({profile['id']})" for profile in profiles]
        dialog.setComboBoxItems(labels)
        self.confirmation = dialog

        def chosen(result):
            text = dialog.textValue()
            self.confirmation = None
            dialog.deleteLater()
            if result == QDialog.Accepted:
                self._activation_profile(installation_id, profiles[labels.index(text)]["id"])
            self._controls()

        dialog.finished.connect(chosen)
        self._controls()
        dialog.open()

    def _activation_profile(self, installation_id, profile_id):
        self._run(
            lambda: self.activation_service.prepare(installation_id, profile_id),
            self._activation_prepared,
        )

    def _activation_prepared(self, plan):
        updating = plan.get("operation") == "update"
        text = t("packages.active_update_summary" if updating else "packages.activation_summary").format(
            package=plan["package_id"], version=plan["version"],
            functions=plan["functions"], library=plan["library_entries"],
            events=plan["events"], profile=plan["profile_id"] or "-",
            previous=plan.get("previous_version", ""),
        )
        if plan["modified"]:
            text += "\n\n" + t("packages.activation_modified")
        text += "\n\n" + t("packages.active_update_rules" if updating else "packages.activation_rules")
        self.details.setPlainText(text + f"\n\nSHA-256: {plan['plan_sha256']}")
        dialog = QMessageBox(
            QMessageBox.Question, t("packages.active_update" if updating else "packages.activate"), text,
            QMessageBox.Yes | QMessageBox.No, self,
        )
        dialog.setTextFormat(Qt.PlainText)
        dialog.setDefaultButton(QMessageBox.No)
        self.confirmation = dialog
        dialog.finished.connect(lambda result: self._activation_confirmed(result, plan))
        self._controls()
        dialog.open()

    def _activation_confirmed(self, result, plan):
        self.confirmation.deleteLater()
        self.confirmation = None
        self._controls()
        if result != QMessageBox.Yes:
            return
        if self.embedded:
            # Preserve the former modal dialog's execution/save exclusion during commit.
            self.activation_progress = _ActivationProgress(self)
            self.activation_progress.setWindowTitle(t("packages.active_update" if plan.get("operation") == "update" else "packages.activate"))
            QVBoxLayout(self.activation_progress).addWidget(QLabel(t("packages.busy")))
            self.activation_progress.open()
        def transfer():
            if plan.get("operation") == "update":
                return self.activation_service.update(plan["installation_id"], plan["plan_sha256"])
            return self.activation_service.activate(
                plan["installation_id"], plan["profile_id"], plan["plan_sha256"],
            )
        self._run(transfer, self._activated)

    def _activated(self, result):
        bus = get_default_reinitialize_service()
        for source in ("library", "variable", "function", "scheduler"):
            bus.publish(source, game_id=result["game_id"])
        self.logger.info(
            "Active Package Updated" if result.get("operation") == "update" else "Package Content Activated",
            installation_id=result["installation_id"],
            game_id=result["game_id"], profile_id=result["profile_id"],
        )
        def reloaded(installations):
            self._loaded(installations)
            if not any("error" in entry or "active_error" in entry for entry in installations):
                self.status.setText(t(
                    "packages.active_update_complete" if result.get("operation") == "update" else "packages.activation_complete",
                ))
        self._run(self._load_installations, reloaded)

    def reject(self):
        if not self.embedded and self.worker is None and self.confirmation is None:
            super().reject()

    def closeEvent(self, event):
        if self.worker is not None or self.confirmation is not None:
            event.ignore()
        else:
            super().closeEvent(event)
