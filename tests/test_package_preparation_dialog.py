import json
import hashlib
import faulthandler
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest
import zipfile
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QUrl
from PySide6.QtGui import QImage, QTextDocument
from PySide6.QtWidgets import QApplication, QDialog

from models.bot_function import BotFunction
from models.github_account import GitHubAccount
from services.package_license_service import (
    PackageLicenseService,
    PackagePreparationService,
)
from services.package_service import PackageError, PackageService
from services.package_outbox_service import PackageOutboxService
from services.scheduler_service import Event, Task, PROTECTED_TASK_KINDS
from services.package_description_service import PackageDescriptionService
from ui.widgets.managed_packages_dialog import _PackageDetails
from ui.i18n.translator import t
from ui.widgets.package_markdown_editor import PackageMarkdownEditor
from ui.widgets.package_preparation_dialog import PackagePreparationDialog


class MemoryConfigService:
    def __init__(self):
        self.values = {}

    def load(self):
        return dict(self.values)

    def save(self, values):
        self.values = dict(values)


class PackagePreparationDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        started = time.monotonic()
        print(f"TEST START: {self.id()}")
        faulthandler.dump_traceback_later(15, exit=True)
        self.addCleanup(faulthandler.cancel_dump_traceback_later)
        self.addCleanup(lambda: print(
            f"TEST END: {self.id()} duration {time.monotonic() - started:.3f}s",
        ))
        self.temporary = TemporaryDirectory(dir=Path(__file__).resolve().parents[1])
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.description_service = PackageDescriptionService(self.root / "drafts")
        self.auth_service = Mock(
            restore=Mock(return_value=GitHubAccount("120588088", "pernerch")),
        )
        self.catalog_service = Mock(published_content=Mock(return_value={}))
        catalog_patch = patch(
            "ui.widgets.package_preparation_dialog.GitHubCatalogService",
            return_value=self.catalog_service,
        )
        catalog_patch.start()
        self.addCleanup(catalog_patch.stop)
        credential_patch = patch(
            "ui.widgets.package_preparation_dialog.GitHubAuthService",
            return_value=self.auth_service,
        )
        credential_patch.start()
        self.addCleanup(credential_patch.stop)
        drafts_patch = patch(
            "ui.widgets.package_preparation_dialog.PackageDescriptionService",
            return_value=self.description_service,
        )
        drafts_patch.start()
        self.addCleanup(drafts_patch.stop)
        self.function_root = self.root / "functions"
        self.license_service = PackageLicenseService(
            config_service=MemoryConfigService(),
            text_fetcher=lambda spdx_id: f"Official text for {spdx_id}",
        )
        self.license_service.set_license("120588088", "MIT")
        self.package_service = PackageService(
            function_root=str(self.function_root),
            library_service=Mock(root=self.root / "library"),
            variable_service=Mock(root=self.root / "variables"),
            game_service=Mock(
                load=Mock(return_value=[{"id": "game", "display_name": "Game"}]),
                get_game=Mock(return_value={}),
            ),
        )
        self.package_service.preparation_service = PackagePreparationService(
            function_root=str(self.function_root),
            license_service=self.license_service,
        )
        function = BotFunction(
            function_id="example",
            name="Example",
            description="An example Function",
            game_id="game",
            revision=2,
            saved=True,
            creator_account=GitHubAccount("120588088", "pernerch"),
        )
        game_root = self.function_root / "game"
        game_root.mkdir(parents=True)
        (game_root / "example.json").write_text(
            json.dumps(function.to_dict()),
            encoding="utf-8",
        )
        draft = BotFunction(
            function_id="draft",
            name="Draft",
            game_id="game",
            saved=False,
        )
        (game_root / "draft.json").write_text(
            json.dumps(draft.to_dict()),
            encoding="utf-8",
        )

    def wait_for_idle(self, dialog):
        deadline = time.monotonic() + 5
        while dialog._busy and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.005)
        self.app.processEvents()
        self.assertFalse(dialog._busy, "preparation worker did not finish")

    def write_test_image(self, path):
        image = QImage(1, 1, QImage.Format.Format_RGBA8888)
        image.fill(0xFF336699)
        self.assertTrue(image.save(str(path), "PNG"))
        return path

    def test_editor_publication_selects_exact_source_and_uploads_only_after_confirmation(self):
        other = BotFunction(
            function_id="aaa", name="AAA first", game_id="game", saved=True,
            creator_account=GitHubAccount("120588088", "pernerch"),
        )
        (self.function_root / "game" / "aaa.json").write_text(
            json.dumps(other.to_dict()), encoding="utf-8",
        )
        outbox = PackageOutboxService(self.package_service, self.root / "outbox")
        self.catalog_service.next_publication_version.return_value = "1.0.100"
        publisher = Mock()
        publisher.submit.return_value = {"url": "https://github.com/pernerch/botstudio-catalog/pull/123"}
        with patch("ui.widgets.package_preparation_dialog.GitHubSubmissionService", return_value=publisher):
            dialog = PackagePreparationDialog(
                package_service=self.package_service, game_id="game", simple_flow=True,
                source_id="example", source_kind="function", publish_after_pack=True,
                outbox_service=outbox,
            )
            self.addCleanup(dialog.deleteLater)
            self.wait_for_idle(dialog)
            self.assertEqual("example", dialog.source.currentData())
            self.assertEqual("function", dialog.kind.currentData())
            self.assertFalse(dialog.source.isEnabled())
            self.assertFalse(dialog.game.isEnabled())
            self.assertFalse(dialog.kind.isEnabled())
            self.assertFalse(dialog.preview_button.isEnabled())
            publisher.submit.assert_not_called()
            dialog.rights.setChecked(True)
            dialog.preview_button.click()
            self.wait_for_idle(dialog)
        publisher.submit.assert_called_once()
        archives = outbox.list_packages()
        self.assertEqual(1, len(archives))
        self.assertEqual("1.0.100", archives[0]["version"])
        self.assertEqual(publisher.submit.return_value, archives[0]["publication"])
        manifest = self.package_service.inspect_package(archives[0]["path"])
        self.assertEqual("example", manifest["source_id"])
        self.assertIn(publisher.submit.return_value["url"], dialog.status.text())
        self.assertFalse(dialog.preview_button.isEnabled())

    def test_editor_upload_failure_keeps_prepared_package_and_never_retries_automatically(self):
        outbox = PackageOutboxService(self.package_service, self.root / "outbox")
        self.catalog_service.next_publication_version.return_value = "1.0.100"
        publisher = Mock()
        publisher.submit.side_effect = PackageError("GitHub HTTP 401")
        with patch("ui.widgets.package_preparation_dialog.GitHubSubmissionService", return_value=publisher):
            dialog = PackagePreparationDialog(
                package_service=self.package_service, game_id="game", simple_flow=True,
                source_id="example", publish_after_pack=True, outbox_service=outbox,
            )
            self.addCleanup(dialog.deleteLater)
            self.wait_for_idle(dialog)
            dialog.rights.setChecked(True)
            dialog.preview_button.click()
            self.wait_for_idle(dialog)
        publisher.submit.assert_called_once()
        self.assertIn("401", dialog.status.text())
        archives = outbox.list_packages()
        self.assertEqual(1, len(archives))
        self.assertIsNone(archives[0]["publication"])
        self.assertNotEqual(QDialog.Accepted, dialog.result())

    def test_missing_editor_source_never_selects_a_different_function(self):
        dialog = PackagePreparationDialog(
            package_service=self.package_service, game_id="game", simple_flow=True,
            source_id="missing", publish_after_pack=True,
        )
        self.addCleanup(dialog.deleteLater)
        self.wait_for_idle(dialog)
        self.assertIsNone(dialog.source.currentData())
        self.assertFalse(dialog.preview_button.isEnabled())

    def test_editor_publication_rechecks_online_presence_before_packing(self):
        self.catalog_service.function_publication.return_value = None
        outbox = PackageOutboxService(self.package_service, self.root / "outbox")
        dialog = PackagePreparationDialog(
            package_service=self.package_service, game_id="game", simple_flow=True,
            source_id="example", publish_after_pack=True, outbox_service=outbox,
        )
        self.addCleanup(dialog.deleteLater)
        self.wait_for_idle(dialog)
        dialog.rights.setChecked(True)
        dialog.preview_button.click()
        self.wait_for_idle(dialog)
        self.assertEqual([], outbox.list_packages())
        self.assertIn(t("function.publication_no_longer_online"), dialog.status.text())

    def test_editor_publication_does_not_fall_back_to_another_game(self):
        dialog = PackagePreparationDialog(
            package_service=self.package_service, game_id="missing", simple_flow=True,
            source_id="example", publish_after_pack=True,
        )
        self.addCleanup(dialog.deleteLater)
        self.wait_for_idle(dialog)
        self.assertIsNone(dialog.game.currentData())
        self.assertIsNone(dialog.source.currentData())
        self.assertFalse(dialog.preview_button.isEnabled())

    def test_simple_check_and_pack_persists_archive_without_preview_or_save_dialog(self):
        outbox = PackageOutboxService(self.package_service, self.root / "outbox")
        dialog = PackagePreparationDialog(
            package_service=self.package_service,
            game_id="game",
            simple_flow=True,
            outbox_service=outbox,
        )
        self.addCleanup(dialog.deleteLater)
        self.wait_for_idle(dialog)
        self.assertTrue(dialog.rights.isEnabled())
        self.assertFalse(dialog.preview_button.isEnabled())
        dialog.rights.setChecked(True)
        self.assertTrue(dialog.preview_button.isEnabled())
        with (
            patch("ui.widgets.package_preparation_dialog.QFileDialog.getSaveFileName") as picker,
            patch("ui.widgets.package_preparation_dialog.QMessageBox.information") as message,
        ):
            dialog.preview_button.click()
            self.wait_for_idle(dialog)
            picker.assert_not_called()
            message.assert_not_called()
        self.assertEqual(QDialog.Accepted, dialog.result(), dialog.status.text())
        packages = outbox.list_packages()
        self.assertEqual(1, len(packages))
        self.assertEqual("Example", packages[0]["name"])
        self.assertIsNone(packages[0]["publication"])
        restarted = PackageOutboxService(self.package_service, self.root / "outbox")
        self.assertEqual(packages, restarted.list_packages())
        manifest = self.package_service.inspect_package(packages[0]["path"])
        self.assertTrue(manifest["package_metadata"]["rights_confirmed"])
        self.assertNotIn("SHA-256", dialog.details.toPlainText())

    def test_packed_list_uploads_selected_archive_without_technical_confirmation(self):
        outbox = PackageOutboxService(self.package_service, self.root / "outbox")
        dialog = PackagePreparationDialog(
            package_service=self.package_service, simple_flow=True, outbox_service=outbox,
        )
        self.addCleanup(dialog.deleteLater)
        self.wait_for_idle(dialog)
        dialog.rights.setChecked(True)
        dialog.preview_button.click()
        self.wait_for_idle(dialog)
        managed = Mock(root=self.root / "managed", packages=self.package_service)
        managed.list_installations.return_value = []
        managed.preview.side_effect = lambda path: {
            "path": str(path),
            "archive_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
        from ui.widgets.managed_packages_dialog import ManagedPackagesDialog
        upload = ManagedPackagesDialog(service=managed, outbox_service=outbox)
        self.addCleanup(upload.deleteLater)
        upload._outbox_loaded(outbox.list_packages())
        upload.packed_table.selectRow(0)
        self.assertTrue(upload.upload_packed.isEnabled())
        publisher = Mock()
        publisher.submit.return_value = {"url": "https://github.com/pernerch/botstudio-catalog/pull/1"}
        with patch("ui.widgets.managed_packages_dialog.GitHubSubmissionService", return_value=publisher):
            upload.upload_packed.click()
            deadline = time.monotonic() + 5
            while upload.worker is not None and time.monotonic() < deadline:
                self.app.processEvents()
                time.sleep(0.005)
            self.assertIsNone(upload.worker)
        publisher.prepare.assert_called_once()
        publisher.submit.assert_called_once()
        self.assertIsNone(upload.confirmation)
        self.assertEqual(publisher.submit.return_value, outbox.list_packages()[0]["publication"])
        self.assertFalse(upload.upload_packed.isEnabled())

    def test_event_sources_group_profiles_and_history_but_keep_content_variants(self):
        first = Event(
            event_id="event-a", game_id="game", profile_id="account-a", name="Pet Skill",
            tasks=[Task(task_id="task-a", name="Read", function_ids=["example"])],
        )
        copy = Event.from_dict(first.to_dict())
        copy.event_id = "event-b"
        copy.profile_id = "account-b"
        copy.sort_order = 10
        copy.next_due_at = "2030-01-01T00:00:00"
        copy.status = "running"
        copy.tasks[0].task_id = "task-b"
        copy.tasks[0].duration_samples_seconds = [12]
        variant = Event.from_dict(copy.to_dict())
        variant.event_id = "event-c"
        variant.tasks[0].function_ids = ["different"]
        before = [item.to_dict() for item in (first, copy, variant)]
        sources = self.package_service.preparation_event_sources([copy, variant, first])
        self.assertEqual(["event-a", "event-c"], [item[0] for item in sources])
        self.assertEqual(2, len({item[1] for item in sources}))
        self.assertEqual(before, [item.to_dict() for item in (first, copy, variant)])
        self.assertEqual(
            [("event-a", "Pet Skill", "Pet Skill", "")],
            self.package_service.preparation_event_sources([copy, first]),
        )

    def test_same_event_with_local_settings_is_not_a_separate_variant(self):
        first = Event(event_id="a", game_id="game", name="Pet Skill")
        second = Event.from_dict(first.to_dict())
        second.event_id = "b"
        second.interval_seconds = 3600
        second.enabled = False
        second.priority = 5
        second.schedule_mode = "interval"
        second.run_at_local = ["12:00"]
        second.cooldown_seconds = 90
        second.retry_minutes = 10
        first.tasks = [Task(task_id="a", name="Run", function_ids=["example"])]
        second.tasks = [Task(
            task_id="b", name="Run", function_ids=["example"], enabled=False,
            priority=1, sort_order=3,
        )]
        before = [event.to_dict() for event in (first, second)]
        self.assertEqual(1, len(self.package_service.preparation_event_sources([first, second])))
        self.assertEqual(before, [event.to_dict() for event in (first, second)])
        second.output_variable = "different-output"
        self.assertEqual(2, len(self.package_service.preparation_event_sources([first, second])))

    def test_task_labels_do_not_duplicate_intel_and_empty_pet_skill_is_hidden(self):
        intel = Event(event_id="intel-a", game_id="game", name="Intel", tasks=[
            Task(name="Intel Mission Available", function_ids=["check-intel"]),
            Task(name="Intel Lighthouse", function_ids=["lighthouse"]),
        ])
        other = Event.from_dict(intel.to_dict())
        other.event_id = "intel-b"
        other.profile_id = "other-profile"
        other.tasks[0].name = "Intel Missions Available"
        pet = Event(event_id="pet-a", game_id="game", name="Pet Skill", tasks=[
            Task(name="Pet Skill", function_ids=["pet-skill"]),
        ])
        empty = Event(event_id="pet-b", game_id="game", name="Pet Skill")
        no_functions = Event(event_id="pet-c", game_id="game", name="Pet Skill",
                             tasks=[Task(name="Not configured")])
        events = [intel, other, pet, empty, no_functions]
        before = [event.to_dict() for event in events]
        sources = self.package_service.preparation_event_sources(events)
        self.assertEqual(["Intel", "Pet Skill"], [source[1] for source in sources])
        self.assertEqual(["intel-a", "pet-a"], [source[0] for source in sources])
        self.assertEqual(before, [event.to_dict() for event in events])
        other.tasks[0].function_ids = ["different-function"]
        self.assertEqual(2, len(self.package_service.preparation_event_sources([intel, other])))
        other.tasks[0].function_ids = ["check-intel"]
        other.tasks.reverse()
        self.assertEqual(2, len(self.package_service.preparation_event_sources([intel, other])))

    def test_helper_selector_preserves_type_drafts_and_exports(self):
        function = self.package_service.load_saved_function("game", "example")
        function.function_id = "helper"
        function.name = "Helper example"
        function.kind = "helper"
        (self.function_root / "game" / "helper.json").write_text(
            json.dumps(function.to_dict()), encoding="utf-8",
        )
        key = ("game", "function", "helper")
        self.description_service.save(key, "Existing helper draft", {})
        dialog = PackagePreparationDialog(package_service=self.package_service)
        self.addCleanup(dialog.deleteLater)
        self.wait_for_idle(dialog)
        self.assertEqual(1, dialog.source.count())
        self.assertEqual("example", dialog.source.currentData())
        dialog.kind.setCurrentIndex(dialog.kind.findData("helper"))
        self.wait_for_idle(dialog)
        self.assertEqual(1, dialog.source.count())
        self.assertEqual("helper", dialog.source.currentData())
        self.assertEqual("Existing helper draft", dialog.description)
        dialog._preview_package()
        self.wait_for_idle(dialog)
        self.assertIsNotNone(dialog._preview, dialog.status.text())
        self.assertTrue(dialog._preview["manifest"]["package_metadata"]["package_id"].startswith("helper-"))
        dialog.rights.setChecked(True)
        output = self.root / "helper.botstudio.zip"
        with patch("ui.widgets.package_preparation_dialog.QFileDialog.getSaveFileName",
                   return_value=(str(output), "")), patch(
                       "ui.widgets.package_preparation_dialog.QMessageBox.information",
                   ):
            dialog._choose_destination()
            self.wait_for_idle(dialog)
        self.assertTrue(output.is_file(), dialog.status.text())
        with zipfile.ZipFile(output) as archive:
            content = json.loads(archive.read("content/functions/game/helper.json"))
        self.assertEqual("helper", content["kind"])

    def test_system_events_are_not_package_sources_and_keep_their_protection(self):
        events = [
            Event(event_id="init", game_id="game", name="Renamed Init", is_init=True),
            Event(event_id="interrupt", game_id="game", name="Renamed Interrupt", is_interrupt=True),
            Event(event_id="protected", game_id="game", tasks=[Task(protected=True)]),
            *[
                Event(event_id=kind, game_id="game", tasks=[Task(kind=kind)])
                for kind in PROTECTED_TASK_KINDS
            ],
        ]
        regular = Event(event_id="normal", game_id="game", name="Journey of Light",
                        tasks=[Task(function_ids=["example"])])
        before = [event.to_dict() for event in events]
        self.assertEqual(
            [("normal", "Journey of Light", "Journey of Light", "")],
            self.package_service.preparation_event_sources([*events, regular]),
        )
        self.assertEqual([], self.package_service.preparation_event_sources(events))
        for event in events:
            for operation in (
                lambda: self.package_service.preview_event_package(event),
                lambda: self.package_service.export_event(event, str(self.root / "blocked.zip")),
                lambda: self.package_service.prepare_event_directory(event, str(self.root / "blocked")),
            ):
                with self.subTest(event=event.event_id, operation=operation):
                    with self.assertRaisesRegex(PackageError, "protected|system"):
                        operation()
        self.assertFalse((self.root / "blocked.zip").exists())
        self.assertFalse((self.root / "blocked").exists())
        self.assertEqual(before, [event.to_dict() for event in events])

    def test_system_events_hidden_but_their_functions_can_be_packaged(self):
        events = [
            Event(event_id="init", game_id="game", is_init=True, tasks=[
                Task(protected=True, function_ids=["example"]),
            ]),
            Event(event_id="interrupt", game_id="game", is_interrupt=True, tasks=[
                Task(function_ids=["example"]),
            ]),
        ]
        scheduler = Mock(list_events=Mock(return_value=events))
        dialog = PackagePreparationDialog(
            package_service=self.package_service,
            scheduler_service_factory=lambda: scheduler,
        )
        self.addCleanup(dialog.deleteLater)
        self.wait_for_idle(dialog)
        dialog.kind.setCurrentIndex(dialog.kind.findData("event"))
        self.wait_for_idle(dialog)
        self.assertEqual(0, dialog.source.count())
        self.assertFalse(dialog.preview_button.isEnabled())
        dialog.kind.setCurrentIndex(dialog.kind.findData("function"))
        self.wait_for_idle(dialog)
        self.assertEqual("example", dialog.source.currentData())
        function = self.package_service.load_saved_function("game", "example")
        preview = self.package_service.preview_function_package(
            function, profile_id="", publication_account=GitHubAccount("120588088", "pernerch"),
        )
        self.assertEqual("function", preview["manifest"]["package_type"])
        archive = self.root / "function.zip"
        self.package_service.export_function(function, str(archive), profile_id="")
        self.assertTrue(archive.is_file())
        self.assertTrue(events[0].is_init)
        self.assertTrue(events[0].tasks[0].protected)
        self.assertTrue(events[1].is_interrupt)

    def test_event_selector_shows_identical_account_content_only_once(self):
        first = Event(event_id="a", game_id="game", profile_id="one", name="Pet Skill",
                      tasks=[Task(function_ids=["example"])])
        second = Event.from_dict(first.to_dict())
        second.event_id = "b"
        second.profile_id = "two"
        scheduler = Mock(
            list_events=Mock(return_value=[second, first]),
            load_event=Mock(return_value=first),
        )
        dialog = PackagePreparationDialog(
            package_service=self.package_service,
            scheduler_service_factory=lambda: scheduler,
        )
        self.addCleanup(dialog.deleteLater)
        self.wait_for_idle(dialog)
        dialog.kind.setCurrentIndex(dialog.kind.findData("event"))
        self.wait_for_idle(dialog)
        self.assertEqual(1, dialog.source.count())
        self.assertEqual("a", dialog.source.currentData())
        self.assertEqual("Pet Skill", dialog.name.text())

    def test_loading_source_does_not_leave_working_status_after_worker_finishes(self):
        dialog = PackagePreparationDialog(package_service=self.package_service)
        self.addCleanup(dialog.deleteLater)
        self.wait_for_idle(dialog)
        self.assertTrue(dialog._draft_ready)
        self.assertTrue(dialog.preview_button.isEnabled())
        self.assertTrue(dialog.close_button.isEnabled())
        self.assertEqual(t("packages.prepare_draft_loaded"), dialog.status.text())
        self.assertNotEqual(t("packages.prepare_working"), dialog.status.text())

    def test_saved_sources_are_sorted_alphabetically_for_every_package_type(self):
        dialog = PackagePreparationDialog(package_service=self.package_service)
        self.addCleanup(dialog.deleteLater)
        self.wait_for_idle(dialog)
        sources = [
            ("z", "Zulu", "Zulu", ""),
            ("b", "beta", "beta", ""),
            ("a2", "Alpha", "Alpha", ""),
            ("a1", "alpha", "alpha", ""),
        ]
        for kind in ("event", "function", "helper"):
            with self.subTest(kind=kind):
                dialog.kind.blockSignals(True)
                dialog.kind.setCurrentIndex(dialog.kind.findData(kind))
                dialog.kind.blockSignals(False)
                dialog.source.clear()
                with patch.object(dialog, "_source_selected"):
                    dialog._sources_loaded(sources)
                self.assertEqual(
                    ["a1", "a2", "b", "z"],
                    [dialog.source.itemData(index) for index in range(dialog.source.count())],
                )
        self.assertEqual(["z", "b", "a2", "a1"], [source[0] for source in sources])

    def published_function_fingerprint(self):
        function = self.package_service.load_saved_function("game", "example")
        return self.package_service.preview_function_package(
            function, description="An example Function", profile_id="",
            publication_account=GitHubAccount("120588088", "pernerch"),
        )["content_fingerprint"]

    def test_unchanged_published_source_is_hidden_but_can_be_shown_for_editing(self):
        self.catalog_service.published_content.return_value = {
            self.published_function_fingerprint(): ("Example", "function"),
        }
        dialog = PackagePreparationDialog(package_service=self.package_service)
        self.addCleanup(dialog.deleteLater)
        self.wait_for_idle(dialog)
        self.assertEqual(0, dialog.source.count())
        self.assertIn("1", dialog.catalog_status.text())
        dialog.show_published.setChecked(True)
        self.wait_for_idle(dialog)
        self.assertEqual("example", dialog.source.currentData())
        dialog.version.setText("9.0.0")
        dialog._preview_package()
        self.wait_for_idle(dialog)
        self.assertIsNone(dialog._preview)
        self.assertIn(t("packages.prepare_already_published"), dialog.status.text())
        dialog.description = "Changed instructions"
        dialog._preview_package()
        self.wait_for_idle(dialog)
        self.assertIsNotNone(dialog._preview, dialog.status.text())
        self.assertTrue(dialog._ready)

    def test_published_check_failure_leaves_sources_visible_with_explicit_warning(self):
        self.catalog_service.published_content.side_effect = PackageError("network unavailable")
        dialog = PackagePreparationDialog(package_service=self.package_service)
        self.addCleanup(dialog.deleteLater)
        self.wait_for_idle(dialog)
        self.assertEqual("example", dialog.source.currentData())
        self.assertIn("network unavailable", dialog.catalog_status.text())
        dialog._preview_package()
        self.wait_for_idle(dialog)
        self.assertIsNone(dialog._preview)
        self.assertIn("network unavailable", dialog.status.text())

    def test_fresh_publication_check_prevents_export_after_another_client_published(self):
        dialog = PackagePreparationDialog(package_service=self.package_service)
        self.addCleanup(dialog.deleteLater)
        self.wait_for_idle(dialog)
        dialog._preview_package()
        self.wait_for_idle(dialog)
        fingerprint = dialog._preview["content_fingerprint"]
        dialog.rights.setChecked(True)
        self.catalog_service.published_content.return_value = {fingerprint: ("Example", "function")}
        output = self.root / "no-update.botstudio.zip"
        with patch("ui.widgets.package_preparation_dialog.QFileDialog.getSaveFileName",
                   return_value=(str(output), "")):
            dialog._choose_destination()
        self.wait_for_idle(dialog)
        self.assertFalse(output.exists())
        self.assertIn(t("packages.prepare_already_published"), dialog.status.text())

    def test_changed_saved_description_remains_in_default_source_list(self):
        self.catalog_service.published_content.return_value = {
            self.published_function_fingerprint(): ("Example", "function"),
        }
        self.description_service.save(("game", "function", "example"), "New description", {})
        dialog = PackagePreparationDialog(package_service=self.package_service)
        self.addCleanup(dialog.deleteLater)
        self.wait_for_idle(dialog)
        self.assertEqual("example", dialog.source.currentData())
        self.assertEqual("New description", dialog.description)

    def test_published_event_is_hidden_despite_different_local_scheduling(self):
        event = Event(event_id="event", game_id="game", name="Pet Skill", tasks=[
            Task(task_id="task", name="Run", function_ids=["example"]),
        ])
        preview = self.package_service.preview_event_package(
            event, profile_id="", publication_account=GitHubAccount("120588088", "pernerch"),
        )
        self.catalog_service.published_content.return_value = {
            preview["content_fingerprint"]: ("Pet Skill", "event"),
        }
        event.interval_seconds = 900
        event.cooldown_seconds = 100
        scheduler = Mock(list_events=Mock(return_value=[event]), load_event=Mock(return_value=event))
        dialog = PackagePreparationDialog(
            package_service=self.package_service, scheduler_service_factory=lambda: scheduler,
        )
        self.addCleanup(dialog.deleteLater)
        self.wait_for_idle(dialog)
        self.assertEqual("example", dialog.source.currentData())
        dialog.kind.setCurrentIndex(dialog.kind.findData("event"))
        self.wait_for_idle(dialog)
        self.assertEqual(0, dialog.source.count())
        self.assertIn("1", dialog.catalog_status.text())
        self.assertEqual(900, event.interval_seconds)
        self.assertEqual(100, event.cooldown_seconds)

    def test_markdown_editor_attaches_and_removes_an_image_reference(self):
        image_path = self.write_test_image(self.root / "screenshot.png")
        editor = PackageMarkdownEditor()
        self.addCleanup(editor.deleteLater)

        with patch(
            "ui.widgets.package_markdown_editor.QFileDialog.getOpenFileNames",
            return_value=([str(image_path)], ""),
        ):
            editor._attach_images()

        self.wait_for_idle(editor)
        self.assertEqual(1, editor.attachments.count())
        self.assertIn(
            "![screenshot](content/package-assets/image-01.png)",
            editor.editor.toPlainText(),
        )
        editor.tabs.setCurrentIndex(1)
        self.app.processEvents()
        preview_image = editor.preview.document().resource(
            QTextDocument.ImageResource,
            QUrl("content/package-assets/image-01.png"),
        )
        self.assertIsNotNone(preview_image)
        editor.editor.setPlainText(
            "![Angepasster Alternativtext](content/package-assets/image-01.png)\n"
        )
        editor.attachments.setCurrentRow(0)
        editor._remove_image()
        self.assertEqual(0, editor.attachments.count())
        self.assertNotIn("package-assets", editor.editor.toPlainText())

    def test_drafts_own_images_and_restore_by_source(self):
        source = self.write_test_image(self.root / "owned.png")
        key = ("game", "function", "one")
        saved = self.description_service.save(key, "![x](content/package-assets/image-01.png)", {
            "content/package-assets/image-01.png": source,
        })
        source.unlink()
        self.assertTrue(saved["assets"]["content/package-assets/image-01.png"].is_file())
        self.assertEqual(saved, self.description_service.load(key))
        self.assertEqual({"markdown": "seed", "assets": {}},
                         self.description_service.load(("game", "event", "one"), "seed"))

    def test_saved_draft_reopens_in_next_preparation_dialog(self):
        self.description_service.save(("game", "function", "example"), "## Saved instructions", {})
        dialog = PackagePreparationDialog(package_service=self.package_service)
        self.addCleanup(dialog.deleteLater)
        self.wait_for_idle(dialog)
        self.assertEqual("## Saved instructions", dialog.description)
        dialog.version.setText("1.0.42")
        self.auth_service.restore.reset_mock()
        dialog.preview_button.click()
        self.wait_for_idle(dialog)
        self.assertEqual("1.0.42", dialog._preview["manifest"]["package_metadata"]["package_version"])
        self.auth_service.restore.assert_called_once_with()
        dialog.rights.setChecked(True)
        dialog.version.setText("1.0.43")
        self.assertIsNone(dialog._preview)
        self.assertFalse(dialog.rights.isChecked())

    def test_editor_save_persists_owned_snapshot_and_invalidates_rights(self):
        source = self.write_test_image(self.root / "save.png")
        name = "content/package-assets/image-01.png"
        markdown = f"![saved]({name})"
        dialog = PackagePreparationDialog(package_service=self.package_service)
        self.addCleanup(dialog.deleteLater)
        self.wait_for_idle(dialog)
        dialog._preview = {"old": True}
        dialog._ready = True
        dialog.rights.setChecked(True)
        accepted_editor = Mock(
            markdown=markdown, package_assets={name: source},
            package_asset_bytes={name: source.read_bytes()},
        )
        accepted_editor.exec.return_value = QDialog.Accepted
        with patch(
            "ui.widgets.package_preparation_dialog.PackageMarkdownEditor",
            return_value=accepted_editor,
        ):
            dialog._edit_description()
            self.wait_for_idle(dialog)
        self.assertIsNone(dialog._preview)
        self.assertFalse(dialog.rights.isChecked())
        self.assertEqual(markdown, dialog.description)
        self.assertNotEqual(source, dialog.package_assets[name])
        self.assertEqual(markdown, self.description_service.load(
            ("game", "function", "example"),
        )["markdown"])

    def test_imported_root_description_and_images_seed_owned_draft(self):
        import hashlib
        source = self.write_test_image(self.root / "imported.png")
        data = source.read_bytes()
        name = "content/package-assets/image-01.png"
        manifest = {
            "package_type": "function", "source_id": "example",
            "description": f"## Original instructions\n![x]({name})",
            "checksums": {name: hashlib.sha256(data).hexdigest()},
        }
        archive = self.root / "preserved.zip"
        with zipfile.ZipFile(archive, "w") as package:
            package.writestr(name, data)
        with patch.object(self.package_service, "publication_sources", return_value=[(manifest, archive)]):
            dialog = PackagePreparationDialog(package_service=self.package_service)
            self.addCleanup(dialog.deleteLater)
            self.wait_for_idle(dialog)
        self.assertEqual(manifest["description"], dialog.description)
        owned = dialog.package_assets[name]
        archive.unlink()
        source.unlink()
        self.assertEqual(data, owned.read_bytes())
        restored = self.description_service.load(("game", "function", "example"))
        self.assertEqual(manifest["description"], restored["markdown"])

    def test_imported_seed_rejects_changed_image_bytes(self):
        archive = self.root / "changed-source.zip"
        name = "content/package-assets/image-01.png"
        with zipfile.ZipFile(archive, "w") as package:
            package.writestr(name, b"changed")
        manifest = {"description": "source", "checksums": {name: "0" * 64}}
        key = ("game", "function", "example")
        with self.assertRaisesRegex(Exception, "changed"):
            self.description_service.load(key, package_source=(manifest, archive))
        self.assertFalse(self.description_service.has_draft(key))

    def test_authentication_error_blocks_preview_and_keeps_draft(self):
        self.description_service.save(("game", "function", "example"), "Saved", {})
        self.auth_service.restore.side_effect = RuntimeError("expired test credentials")
        dialog = PackagePreparationDialog(package_service=self.package_service)
        self.addCleanup(dialog.deleteLater)
        self.wait_for_idle(dialog)
        dialog.preview_button.click()
        self.wait_for_idle(dialog)
        self.assertIsNone(dialog._preview)
        self.assertIn("expired test credentials", dialog.status.text())
        self.assertEqual("Saved", self.description_service.load(
            ("game", "function", "example"),
        )["markdown"])

    def test_draft_rejects_changed_owned_images_and_cleans_removed_images(self):
        source = self.write_test_image(self.root / "changed.png")
        key = ("game", "function", "example")
        saved = self.description_service.save(key, "image", {
            "content/package-assets/image-01.png": source,
        })
        owned = saved["assets"]["content/package-assets/image-01.png"]
        image = QImage(2, 2, QImage.Format.Format_RGBA8888)
        image.fill(0xFF112233)
        self.assertTrue(image.save(str(owned), "PNG"))
        with self.assertRaisesRegex(Exception, "checksum"):
            self.description_service.load(key)
        self.description_service.save(key, "without image", {})
        self.assertFalse(owned.exists())
        self.assertEqual("without image", self.description_service.load(key)["markdown"])

    def test_image_decode_and_file_access_run_outside_gui_thread(self):
        import threading
        source = self.write_test_image(self.root / "thread.png")
        editor = PackageMarkdownEditor()
        self.addCleanup(editor.deleteLater)
        main_thread = threading.get_ident()
        threads = []
        original = PackageDescriptionService.read_image

        def read_image(path):
            threads.append(threading.get_ident())
            return original(path)

        with patch.object(PackageDescriptionService, "read_image", side_effect=read_image):
            editor._load_images([source])
            self.wait_for_idle(editor)
        self.assertTrue(threads)
        self.assertNotIn(main_thread, threads)

    def test_selected_image_snapshot_survives_later_source_changes(self):
        source = self.write_test_image(self.root / "snapshot.png")
        original = source.read_bytes()
        editor = PackageMarkdownEditor()
        self.addCleanup(editor.deleteLater)
        editor._load_images([source])
        self.wait_for_idle(editor)
        source.write_bytes(b"changed after selection")
        saved = self.description_service.save(
            ("game", "function", "snapshot"),
            editor.editor.toPlainText(), editor.package_assets,
            image_bytes=editor.package_asset_bytes,
        )
        self.assertEqual(original, next(iter(saved["assets"].values())).read_bytes())

    def test_switching_source_invalidates_preview_and_restores_description(self):
        dialog = PackagePreparationDialog(package_service=self.package_service)
        self.addCleanup(dialog.deleteLater)
        self.wait_for_idle(dialog)
        self.assertEqual("An example Function", dialog.description)
        self.description_service.save(("game", "function", "second"), "second draft", {})
        dialog._source_descriptions["second"] = "second seed"
        dialog.source.addItem("Second", "second")
        dialog._preview = {"old": True}
        dialog._ready = True
        dialog.rights.setChecked(True)
        dialog.source.setCurrentIndex(1)
        self.wait_for_idle(dialog)
        self.assertIsNone(dialog._preview)
        self.assertFalse(dialog.rights.isChecked())
        self.assertEqual("second draft", dialog.description)
        dialog.source.setCurrentIndex(0)
        self.wait_for_idle(dialog)
        self.assertEqual("An example Function", dialog.description)

    def test_image_selection_preserves_valid_files_and_reports_invalid_files(self):
        source = self.write_test_image(self.root / "valid.png")
        invalid = self.root / "invalid.png"
        invalid.write_bytes(b"not an image")
        editor = PackageMarkdownEditor()
        self.addCleanup(editor.deleteLater)
        editor._load_images([invalid, source])
        self.wait_for_idle(editor)
        self.assertEqual(1, len(editor.package_assets))
        self.assertIn("invalid.png", editor.status.text())
        for url in ("file:///content/package-assets/image-01.png",
                    "https://example.org/content/package-assets/image-01.png",
                    "/content/package-assets/image-01.png"):
            self.assertIsNone(editor.preview.loadResource(QTextDocument.ImageResource, QUrl(url)))

    def test_cancel_pending_images_does_not_apply_results(self):
        editor = PackageMarkdownEditor()
        self.addCleanup(editor.deleteLater)
        generation = editor._generation
        editor.reject()
        editor._images_loaded((generation, [(self.root / "x.png", QImage())], [], False))
        self.assertEqual({}, editor.package_assets)

    def test_recipient_uses_verified_attachment_bytes_only(self):
        source = self.write_test_image(self.root / "recipient.png")
        data = source.read_bytes()
        import hashlib
        archive = self.root / "images.zip"
        name = "content/package-assets/image-01.png"
        with zipfile.ZipFile(archive, "w") as package:
            package.writestr(name, data)
            package.writestr("checksums.json", json.dumps({name: hashlib.sha256(data).hexdigest()}))
        preview = {"path": str(archive), "archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest()}
        images = PackageDescriptionService.preview_images(preview)
        browser = _PackageDetails()
        self.addCleanup(browser.deleteLater)
        browser.images = images
        self.assertFalse(browser.loadResource(QTextDocument.ImageResource, QUrl(name)).isNull())
        for url in ("file:///" + name, "https://example.org/" + name, "../" + name):
            self.assertIsNone(browser.loadResource(QTextDocument.ImageResource, QUrl(url)))
        browser.clear()
        self.assertIsNone(browser.loadResource(QTextDocument.ImageResource, QUrl(name)))
        archive.write_bytes(b"changed")
        with self.assertRaisesRegex(Exception, "changed"):
            PackageDescriptionService.preview_images(preview)

    def test_function_is_previewed_confirmed_and_created_as_verified_zip(self):
        dialog = PackagePreparationDialog(
            package_service=self.package_service,
        )
        self.addCleanup(dialog.deleteLater)
        self.wait_for_idle(dialog)

        self.assertEqual("example", dialog.source.currentData())
        self.assertTrue((self.function_root / "game" / "draft.json").is_file())
        dialog.preview_button.click()
        self.wait_for_idle(dialog)
        self.assertIsNotNone(dialog._preview)
        self.assertTrue(dialog._ready)
        self.assertFalse(dialog.rights.isChecked())
        self.assertFalse(dialog.prepare_button.isEnabled())

        dialog.rights.setChecked(True)
        self.assertTrue(dialog.prepare_button.isEnabled())
        dialog.name.setText("Example package")
        self.assertIsNone(dialog._preview)
        self.assertFalse(dialog.rights.isChecked())
        self.assertFalse(dialog.prepare_button.isEnabled())
        dialog.preview_button.click()
        self.wait_for_idle(dialog)
        self.assertTrue(dialog._ready)
        image_path = self.write_test_image(self.root / "package-image.png")
        dialog.description = (
            "## Instructions\n\n"
            "![Screenshot](content/package-assets/image-01.png)"
        )
        dialog.package_assets = {
            "content/package-assets/image-01.png": image_path,
        }
        dialog._update_description_summary()
        dialog.preview_button.click()
        self.wait_for_idle(dialog)
        self.assertIn("content/package-assets/image-01.png", dialog._preview["checksums"])
        dialog.rights.setChecked(True)
        destination = self.root / "destination"
        destination.mkdir()
        with patch(
            "ui.widgets.package_preparation_dialog.QFileDialog.getSaveFileName",
            return_value=(str(destination / "Example package.botstudio.zip"), ""),
        ), patch(
            "ui.widgets.package_preparation_dialog.QMessageBox.information",
        ):
            dialog.prepare_button.click()
            self.wait_for_idle(dialog)

        archive = destination / "Example package.botstudio.zip"
        self.assertEqual(QDialog.Accepted, dialog.result())
        manifest = self.package_service.inspect_package(str(archive))
        self.assertTrue(manifest["package_metadata"]["rights_confirmed"])
        self.assertEqual(
            dialog.description,
            manifest["description"],
        )
        with zipfile.ZipFile(archive) as package:
            self.assertEqual(
                image_path.read_bytes(),
                package.read("content/package-assets/image-01.png"),
            )

    def test_missing_publisher_metadata_keeps_preview_unready(self):
        self.auth_service.restore.return_value = None
        source = self.function_root / "game" / "example.json"
        function_data = json.loads(source.read_text(encoding="utf-8"))
        function_data["creator_account"] = None
        source.write_text(json.dumps(function_data), encoding="utf-8")
        dialog = PackagePreparationDialog(package_service=self.package_service)
        self.addCleanup(dialog.deleteLater)
        self.wait_for_idle(dialog)

        dialog.preview_button.click()
        self.wait_for_idle(dialog)

        self.assertIsNotNone(dialog._preview)
        self.assertFalse(dialog._ready)
        self.assertFalse(dialog.rights.isEnabled())
        self.assertFalse(dialog.prepare_button.isEnabled())
        ready_prefix = t("packages.prepare_not_ready").split("{reason}")[0]
        self.assertIn(ready_prefix, dialog.details.toPlainText())
