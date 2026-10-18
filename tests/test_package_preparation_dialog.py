import json
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
from services.package_service import PackageService
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
