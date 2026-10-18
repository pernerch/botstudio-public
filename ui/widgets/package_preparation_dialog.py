from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal, Slot
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QFileDialog, QFormLayout, QHBoxLayout,
    QLabel, QLineEdit, QMessageBox, QPushButton, QTextBrowser, QVBoxLayout,
)

from services.package_service import PackageError, PackageService
from models.bot_function import FUNCTION_KIND_HELPER
from services.package_description_service import PackageDescriptionService
from services.package_license_service import PackageLicenseError
from services.github_auth_service import GitHubAuthError, GitHubAuthService
from services.github_catalog_service import GitHubCatalogService
from services.scheduler_service import SchedulerService
from ui.i18n.translator import t
from ui.widgets.package_markdown_editor import PackageMarkdownEditor


class _PreparationSignals(QObject):
    finished = Signal(object, str)


class _PreparationTask(QRunnable):
    def __init__(self, operation):
        super().__init__()
        self.operation = operation
        self.signals = _PreparationSignals()

    def run(self):
        try:
            result = self.operation()
        except Exception as error:
            self.signals.finished.emit(None, str(error) or type(error).__name__)
        else:
            self.signals.finished.emit(result, "")


class PackagePreparationDialog(QDialog):
    """Prepare, checksum and zip an explicitly selected Function or Event."""

    def __init__(
        self,
        parent=None,
        package_service=None,
        game_id=None,
        scheduler_service_factory=None,
        description_service=None,
        auth_service=None,
        catalog_service=None,
    ):
        super().__init__(parent)
        self.package_service = package_service or PackageService()
        self.game_id = game_id
        self.scheduler_service_factory = scheduler_service_factory or SchedulerService
        self.description_service = description_service or PackageDescriptionService()
        self.auth_service = auth_service or GitHubAuthService()
        self.catalog_service = catalog_service or GitHubCatalogService()
        self._source_descriptions = {}
        self._source_names = {}
        self._draft_key = None
        self._draft_ready = False
        self.worker = None
        self._busy = False
        self._completion = None
        self._preview = None
        self._ready = False
        self.description = ""
        self.package_assets = {}
        self.setWindowTitle(t("packages.prepare_title"))
        self.resize(760, 640)

        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.game = QComboBox()
        self.kind = QComboBox()
        self.kind.addItem(t("packages.prepare_function"), "function")
        self.kind.addItem(t("packages.catalog_type_helper"), "helper")
        self.kind.addItem(t("packages.prepare_event"), "event")
        self.source = QComboBox()
        self.name = QLineEdit()
        self.version = QLineEdit()
        self.version.setPlaceholderText(t("packages.prepare_version_auto"))
        description_row = QHBoxLayout()
        self.description_button = QPushButton(t("packages.markdown_open"))
        self.description_summary = QLabel(t("packages.markdown_empty"))
        self.description_summary.setWordWrap(True)
        description_row.addWidget(self.description_button)
        description_row.addWidget(self.description_summary, stretch=1)
        form.addRow(t("settings.game"), self.game)
        form.addRow(t("packages.prepare_kind"), self.kind)
        form.addRow(t("packages.prepare_source"), self.source)
        form.addRow(t("packages.prepare_name"), self.name)
        form.addRow(t("packages.version"), self.version)
        form.addRow(t("packages.prepare_description"), description_row)
        layout.addLayout(form)
        self.show_published = QCheckBox(t("packages.prepare_show_published"))
        layout.addWidget(self.show_published)
        self.catalog_status = QLabel()
        self.catalog_status.setWordWrap(True)
        layout.addWidget(self.catalog_status)

        self.preview_button = QPushButton(t("packages.prepare_preview"))
        layout.addWidget(self.preview_button)
        self.details = QTextBrowser()
        self.details.setOpenLinks(False)
        self.details.setOpenExternalLinks(False)
        self.details.setReadOnly(True)
        layout.addWidget(self.details, stretch=1)

        self.rights = QCheckBox(t("packages.prepare_rights"))
        self.rights.setEnabled(False)
        layout.addWidget(self.rights)
        self.status = QLabel()
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        buttons = QHBoxLayout()
        self.prepare_button = QPushButton(t("packages.prepare_zip"))
        self.prepare_button.setEnabled(False)
        self.close_button = QPushButton(t("common.close"))
        buttons.addWidget(self.prepare_button)
        buttons.addWidget(self.close_button)
        layout.addLayout(buttons)

        self.game.currentIndexChanged.connect(self._sources_changed)
        self.kind.currentIndexChanged.connect(self._sources_changed)
        self.show_published.toggled.connect(self._sources_changed)
        self.source.currentIndexChanged.connect(self._source_selected)
        self.name.textChanged.connect(self._invalidate_preview)
        self.version.textChanged.connect(self._invalidate_preview)
        self.description_button.clicked.connect(self._edit_description)
        self.rights.toggled.connect(self._update_controls)
        self.preview_button.clicked.connect(self._preview_package)
        self.prepare_button.clicked.connect(self._choose_destination)
        self.close_button.clicked.connect(self.reject)

        self._run(self.package_service.game_service.load, self._games_loaded)
        self._update_controls()

    def _run(self, operation, completed):
        self.worker = _PreparationTask(operation)
        self._completion = completed
        self.worker.signals.finished.connect(self._finished)
        self._set_busy(True)
        QThreadPool.globalInstance().start(self.worker)

    @Slot(object, str)
    def _finished(self, result, error):
        self._set_busy(False)
        if error:
            self.status.setText(t("packages.prepare_failed").format(error=error))
            return
        if self._completion:
            self._completion(result)

    def _set_busy(self, busy):
        self._busy = busy
        for control in (
            self.game, self.kind, self.source, self.name, self.version, self.description_button,
            self.show_published,
        ):
            control.setEnabled(not busy)
        self.close_button.setEnabled(not busy)
        if busy:
            self.status.setText(t("packages.prepare_working"))
        else:
            self.status.clear()
        self._update_controls()

    def _games_loaded(self, games):
        selected_index = 0
        self.game.blockSignals(True)
        for game in games:
            game_id = str(game.get("id") or game.get("game_id") or "")
            if not game_id:
                continue
            label = str(game.get("display_name") or game_id)
            self.game.addItem(label, game_id)
            if game_id == self.game_id:
                selected_index = self.game.count() - 1
        if self.game.count():
            self.game.setCurrentIndex(selected_index)
            self.game.blockSignals(False)
            self._sources_changed()
        else:
            self.game.blockSignals(False)
            self.status.setText(t("packages.prepare_no_games"))

    def _sources_changed(self, *_):
        self._invalidate_preview()
        self._draft_ready = False
        self._draft_key = None
        self.description = ""
        self.package_assets = {}
        self.name.clear()
        self.version.clear()
        self._update_description_summary()
        self.source.blockSignals(True)
        self.source.clear()
        self.source.blockSignals(False)
        game_id = self.game.currentData()
        if not game_id:
            self._update_controls()
            return

        kind = self.kind.currentData()
        if kind in ("function", "helper"):
            def load_sources():
                return [
                    (function.function_id, function.name or function.function_id,
                     function.name, function.description)
                    for function in self.package_service.list_saved_functions(game_id)
                    if (function.kind == FUNCTION_KIND_HELPER) == (kind == "helper")
                ]
        else:
            def load_sources():
                service = self.scheduler_service_factory()
                return self.package_service.preparation_event_sources(
                    service.list_events(game_id),
                )
        show_published = self.show_published.isChecked()
        self.catalog_status.setText(t("packages.prepare_catalog_checking"))

        def checked_sources():
            sources = load_sources()
            if show_published or not sources:
                return sources, "", None
            try:
                account = self.auth_service.restore()
                if account is None:
                    raise PackageError(t("packages.prepare_catalog_sign_in"))
                published = self.catalog_service.published_content(
                    game_id, account, self.package_service,
                )
            except (PackageError, GitHubAuthError, OSError) as error:
                return sources, str(error), 0
            if not published:
                return sources, "", 0
            visible, errors = [], []
            started = monotonic()
            for source_id, label, name, seed in sources:
                if monotonic() - started > 30:
                    return sources, t("packages.prepare_catalog_local_timeout"), 0
                if (name or label, kind) not in published.values():
                    visible.append((source_id, label, name, seed))
                    continue
                try:
                    draft_kind = "function" if kind == "helper" else kind
                    draft = self._load_source_draft((game_id, draft_kind, source_id), seed)
                    preview = self._source_preview(
                        game_id, kind, source_id, name or label, draft["markdown"],
                        draft["assets"], account,
                    )
                    if preview["content_fingerprint"] in published:
                        continue
                except (PackageError, PackageLicenseError, OSError) as error:
                    errors.append(f"{label}: {error}")
                visible.append((source_id, label, name, seed))
            return visible, "\n".join(errors), len(sources) - len(visible)

        self._run(checked_sources, self._checked_sources_loaded)

    def _checked_sources_loaded(self, result):
        sources, error, hidden = result
        self.catalog_status.setText(
            t("packages.prepare_catalog_failed").format(error=error)
            if error else (
                t("packages.prepare_catalog_not_filtered") if hidden is None
                else t("packages.prepare_catalog_checked").format(count=hidden)
            )
        )
        self._sources_loaded(sources)

    def _source_preview(self, game_id, kind, source_id, name, description, assets, account, version=None):
        options = {
            "package_name": name, "description": description, "profile_id": "",
            "package_assets": assets, "publication_account": account, "package_version": version,
        }
        if kind in ("function", "helper"):
            function = self.package_service.load_saved_function(game_id, source_id)
            return self.package_service.preview_function_package(function, **options)
        event = self.scheduler_service_factory().load_event(game_id, source_id)
        if event is None:
            raise PackageError("the selected Event no longer exists")
        return self.package_service.preview_event_package(event, **options)

    def _load_source_draft(self, key, seed):
        game_id, kind, source_id = key
        if self.description_service.has_draft(key):
            return self.description_service.load(key, seed)
        if kind == "function":
            functions = [self.package_service.load_saved_function(game_id, source_id)]
        else:
            event = self.scheduler_service_factory().load_event(game_id, source_id)
            if event is None:
                raise PackageError("the selected Event no longer exists")
            functions = [
                self.package_service.load_saved_function(game_id, function_id)
                for task in event.tasks for function_id in task.function_ids
            ]
        matching = [
            (manifest, path)
            for manifest, path in self.package_service.publication_sources(functions)
            if manifest.get("package_type") in (
                ("function", "helper") if kind == "function" else ("event",)
            ) and manifest.get("source_id") == source_id
        ]
        if len(matching) > 1:
            raise PackageError("multiple source package channels match the description")
        return self.description_service.load(
            key, seed, package_source=matching[0] if matching else None,
        )

    def _sources_loaded(self, sources):
        sources = sorted(sources, key=lambda source: (source[1].casefold(), source[0]))
        self._source_names = {
            source_id: name or label for source_id, label, name, _ in sources
        }
        self._source_descriptions = {
            source_id: description for source_id, _, _, description in sources
        }
        self.source.blockSignals(True)
        for source_id, label, _, _ in sources:
            self.source.addItem(label, source_id)
        self.source.blockSignals(False)
        if not sources:
            self.status.setText(t("packages.prepare_no_sources"))
            self._update_controls()
            return
        self._source_selected()

    def _source_selected(self, *_):
        self._invalidate_preview()
        self._draft_ready = False
        source_id = self.source.currentData()
        self._draft_key = None
        self.description = ""
        self.package_assets = {}
        self.version.clear()
        self.name.setText(self._source_names.get(source_id, "") if source_id else "")
        self._update_description_summary()
        if source_id:
            kind = "function" if self.kind.currentData() == "helper" else self.kind.currentData()
            key = (self.game.currentData(), kind, source_id)
            self._draft_key = key
            seed = self._source_descriptions.get(source_id, "")

            self._run(lambda: self._load_source_draft(key, seed), self._draft_loaded)
        self._update_controls()

    def _draft_loaded(self, draft):
        self.description = draft["markdown"]
        self.package_assets = draft["assets"]
        self._draft_ready = True
        self._update_description_summary()
        self.status.setText(t("packages.prepare_draft_loaded"))
        self._update_controls()

    def _edit_description(self):
        editor = PackageMarkdownEditor(
            self.description,
            self.package_assets,
            parent=self,
        )
        if editor.exec() != QDialog.Accepted:
            return
        self._invalidate_preview()
        key = self._draft_key
        markdown, assets = editor.markdown, dict(editor.package_assets)
        image_bytes = dict(editor.package_asset_bytes)
        self._run(
            lambda: self.description_service.save(key, markdown, assets, image_bytes=image_bytes),
            self._draft_loaded,
        )

    def _update_description_summary(self):
        if not self.description.strip() and not self.package_assets:
            self.description_summary.setText(t("packages.markdown_empty"))
            return
        self.description_summary.setText(
            t("packages.markdown_summary").format(
                characters=len(self.description),
                images=len(self.package_assets),
            )
        )

    def _invalidate_preview(self, *_):
        self._preview = None
        self._ready = False
        self.rights.blockSignals(True)
        self.rights.setChecked(False)
        self.rights.blockSignals(False)
        self.rights.setEnabled(False)
        self.details.clear()
        self._update_controls()

    def _preview_package(self):
        game_id = self.game.currentData()
        source_id = self.source.currentData()
        if not game_id or not source_id:
            return
        self._invalidate_preview()
        kind = self.kind.currentData()
        package_name = self.name.text().strip()
        description = self.description
        package_assets = dict(self.package_assets)
        package_version = self.version.text().strip() or None

        def create_preview():
            publication_account = self.auth_service.restore()
            preview = self._source_preview(
                game_id, kind, source_id, package_name, description, package_assets,
                publication_account, package_version,
            )
            if publication_account is not None and preview["content_fingerprint"] in (
                self.catalog_service.published_content(game_id, publication_account, self.package_service)
            ):
                raise PackageError(t("packages.prepare_already_published"))

            candidate = deepcopy(preview["manifest"])
            metadata = candidate.get("package_metadata")
            if isinstance(metadata, dict):
                metadata["rights_confirmed"] = True
            try:
                PackageService.github_release_metadata(candidate)
                readiness_error = ""
            except PackageError as error:
                readiness_error = str(error)
            return preview, readiness_error

        self._run(create_preview, self._preview_loaded)

    def _preview_loaded(self, result):
        self._preview, readiness_error = result
        self._ready = not readiness_error
        manifest = self._preview["manifest"]
        self.version.blockSignals(True)
        self.version.setText(str((manifest.get("package_metadata") or {}).get("package_version", "")))
        self.version.blockSignals(False)
        lines = [
            PackageService.manifest_markdown(manifest),
            "",
            t("packages.prepare_files"),
            *[f"- {path}" for path in self._preview["files"]],
            "",
            t("packages.prepare_checksums"),
            *[
                f"- {path}: {checksum}"
                for path, checksum in sorted(self._preview["checksums"].items())
            ],
        ]
        if readiness_error:
            lines.extend(["", t("packages.prepare_not_ready").format(reason=readiness_error)])
        else:
            lines.extend(["", t("packages.prepare_ready")])
        self.details.setPlainText("\n".join(lines))
        self.rights.setEnabled(self._ready)
        self.status.setText(
            t("packages.prepare_review")
            if self._ready
            else t("packages.prepare_not_ready").format(reason=readiness_error)
        )
        self._update_controls()

    def _choose_destination(self):
        if not self._preview or not self._ready or not self.rights.isChecked():
            return
        package_name = self.name.text().strip()
        default_name = "".join(
            character if character.isalnum() or character in "-_ ." else "_"
            for character in package_name
        ).strip(" .")
        suggested_name = f"{default_name or 'package'}.botstudio.zip"
        destination, _ = QFileDialog.getSaveFileName(
            self,
            t("packages.prepare_destination"),
            suggested_name,
            "BotStudio (*.botstudio.zip)",
        )
        if not destination:
            return
        output = Path(destination)
        if not output.name.lower().endswith(".botstudio.zip"):
            output = output.with_name(output.name.removesuffix(".zip") + ".botstudio.zip")
        preview = self._preview
        manifest_metadata = deepcopy(preview["manifest"].get("package_metadata"))
        if not isinstance(manifest_metadata, dict):
            self.status.setText(
                t("packages.prepare_not_ready").format(
                    reason="package publication metadata is missing"
                )
            )
            return
        manifest_metadata["rights_confirmed"] = True
        game_id = self.game.currentData()
        source_id = self.source.currentData()
        kind = self.kind.currentData()
        package_name = self.name.text().strip()
        description = self.description
        package_assets = dict(self.package_assets)

        def create_archive():
            account = self.auth_service.restore()
            if account is None or account.to_dict() != manifest_metadata["publisher"]:
                raise PackageError(t("packages.prepare_catalog_sign_in"))
            if preview["content_fingerprint"] in self.catalog_service.published_content(
                game_id, account, self.package_service,
            ):
                raise PackageError(t("packages.prepare_already_published"))
            with TemporaryDirectory(prefix="botstudio-package-") as temporary:
                ready_directory = Path(temporary) / "ready"
                if kind in ("function", "helper"):
                    function = self.package_service.load_saved_function(game_id, source_id)
                    self.package_service.prepare_function_directory(
                        function,
                        str(ready_directory),
                        package_name=package_name,
                        description=description,
                        profile_id="",
                        package_assets=package_assets,
                        package_metadata=manifest_metadata,
                        expected_checksums=preview["checksums"],
                    )
                else:
                    event = self.scheduler_service_factory().load_event(game_id, source_id)
                    if event is None:
                        raise PackageError("the selected Event no longer exists")
                    self.package_service.prepare_event_directory(
                        event,
                        str(ready_directory),
                        package_name=package_name,
                        description=description,
                        profile_id="",
                        package_assets=package_assets,
                        package_metadata=manifest_metadata,
                        expected_checksums=preview["checksums"],
                    )
                return self.package_service.create_ready_archive(
                    str(ready_directory),
                    str(output),
                    expected_checksums=preview["checksums"],
                )

        self._run(create_archive, self._archive_created)

    def _archive_created(self, output):
        self.status.setText(
            t("packages.prepare_complete").format(path=output)
        )
        QMessageBox.information(
            self,
            t("packages.prepare_title"),
            t("packages.prepare_complete").format(path=output),
        )
        self.accept()

    def _update_controls(self):
        has_source = bool(self.source.currentData()) and self._draft_ready
        self.description_button.setEnabled(not self._busy and has_source)
        self.preview_button.setEnabled(not self._busy and has_source)
        self.prepare_button.setEnabled(
            not self._busy and self._preview is not None and self._ready
            and self.rights.isChecked()
        )
        self.rights.setEnabled(not self._busy and self._ready)
        self.close_button.setEnabled(not self._busy)
