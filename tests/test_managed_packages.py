import faulthandler
from contextlib import nullcontext
import hashlib
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest
import zipfile
from threading import Event as ThreadEvent, get_ident
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtWidgets import QApplication, QMessageBox, QPushButton, QWidget

from models.github_account import GitHubAccount
from services.package_service import PackageError, PackageService
from services.managed_package_service import ManagedPackageService
from ui.i18n.translator import set_language, t
from ui.widgets.managed_packages_dialog import ManagedPackagesDialog


class ManagedPackageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        started = time.monotonic()
        print(f"TEST START: {self.id()}")
        faulthandler.dump_traceback_later(15, exit=True)
        self.addCleanup(faulthandler.cancel_dump_traceback_later)
        self.addCleanup(lambda: print(f"TEST END: {self.id()} duration {time.monotonic() - started:.3f}s"))
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.packages = PackageService(
            function_root=str(self.root / "functions"),
            library_service=Mock(), variable_service=Mock(), game_service=Mock(),
        )
        self.store = ManagedPackageService(self.root / "store", self.packages)

    def dialog(self):
        with patch("ui.widgets.managed_packages_dialog.get_logger", return_value=Mock()):
            dialog = ManagedPackagesDialog(service=self.store)
        self.addCleanup(dialog.deleteLater)
        return dialog

    def wait_for_idle(self, dialog):
        loop = QEventLoop()
        timer = QTimer()
        timer.setInterval(5)
        timer.timeout.connect(lambda: loop.quit() if dialog.worker is None else None)
        timeout = QTimer()
        timeout.setSingleShot(True)
        timeout.timeout.connect(loop.quit)
        timer.start()
        timeout.start(3000)
        try:
            loop.exec()
        finally:
            timer.stop()
            timeout.stop()
        self.assertIsNone(dialog.worker, "package operation did not finish within 3 seconds")

    def archive(
        self, version="1.0.0", publisher="123", package_id="example", payload=b"original",
        fork_of=None, login=None, metadata_changes=None,
    ):
        metadata = {
            "package_id": package_id, "package_version": version, "author": "Author",
            "publisher": {"provider": "github", "account_id": publisher, "display_name": "author"},
            "license_id": "MIT", "license_text": "Fixture terms", "rights_confirmed": True,
        }
        if fork_of is not None:
            metadata["fork_of"] = fork_of
        if login is not None:
            metadata["publisher"] = {"provider": "github", "account_id": publisher, "login": login}
        if metadata_changes is not None:
            metadata.update(metadata_changes)
        payload = json.dumps({
            "function_id": "function", "game_id": "game", "revision": 1,
            "name": payload.decode("utf-8"),
        }).encode("utf-8")
        name = "content/functions/game/function.json"
        checksums = {name: hashlib.sha256(payload).hexdigest()}
        manifest = {
            "format": "botstudio-package", "format_version": 1,
            "package_name": "Example", "description": "", "game": {"id": "game"},
            "contents": {"functions": 1}, "checksums": checksums,
            "package_metadata": metadata,
        }
        path = self.root / f"package-{time.monotonic_ns()}.zip"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("manifest.json", json.dumps(manifest))
            archive.writestr("checksums.json", json.dumps(checksums))
            archive.writestr(name, payload)
        return path

    def test_original_and_editable_copy_are_separate_and_persistent(self):
        result = self.store.install(self.archive())
        workspace = Path(result["working_directory"])
        original = Path(result["original_archive"])
        self.assertTrue(original.is_file())
        self.assertFalse(result["modified"])
        (workspace / "content/functions/game/function.json").write_bytes(b"my fix")
        restarted = ManagedPackageService(self.root / "store", self.packages)
        status = restarted.status(result["installation_id"])
        self.assertTrue(status["modified"])
        self.assertEqual("modified", status["integrity"])
        self.assertEqual("unverified", status["publisher_verification"])
        with zipfile.ZipFile(original) as archive:
            self.assertEqual("original", json.loads(archive.read("content/functions/game/function.json"))["name"])

    @staticmethod
    def source(path, repository_id=10, asset_id=22, login="automation"):
        return {
            "repository": "pernerch/botstudio-catalog", "repository_id": repository_id,
            "release_id": 11, "asset_id": asset_id,
            "archive_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "uploader": {"account_id": "999", "login": login},
        }

    def install_catalog(self, path, **changes):
        source = self.source(path, **changes)
        return self.store.install(
            path, expected_sha256=source["archive_sha256"], catalog_source=source,
        )

    def test_catalog_receipt_persists_without_authenticating_declared_publisher(self):
        path = self.archive()
        source = self.source(path)
        installed = self.install_catalog(path)
        source["uploader"]["login"] = "changed"
        restarted = ManagedPackageService(self.root / "store", self.packages)
        status = restarted.status(installed["installation_id"])
        self.assertEqual(self.source(path), status["catalog_source"])
        self.assertEqual("unverified", status["publisher_verification"])
        self.assertEqual("github:123", status["publisher_key"])
        self.assertEqual("999", status["catalog_source"]["uploader"]["account_id"])
        self.assertIsNone(self.store.preview(path).get("catalog_source"))
        dialog = self.dialog()
        dialog._loaded([status])
        dialog.table.selectRow(0)
        self.assertIn("10", dialog.table.item(0, 0).toolTip())
        dialog.inspect_original.click()
        self.wait_for_idle(dialog)
        self.assertEqual(self.source(path), dialog.preview["catalog_source"])
        self.assertIn("@automation", dialog.details.toPlainText())

    def test_actions_bot_catalog_receipt_survives_restart_without_publisher_authority(self):
        path = self.archive()
        installed = self.install_catalog(path, login="github-actions[bot]")
        restarted = ManagedPackageService(self.root / "store", self.packages)
        status = restarted.status(installed["installation_id"])
        self.assertEqual(self.source(path, login="github-actions[bot]"), status["catalog_source"])
        self.assertEqual("unverified", status["publisher_verification"])
        self.assertEqual("github:123", status["publisher_key"])
        dialog = self.dialog()
        dialog._loaded([status])
        dialog.table.selectRow(0)
        dialog.inspect_original.click()
        self.wait_for_idle(dialog)
        self.assertIn("@github-actions[bot]", dialog.details.toPlainText())
        self.assertEqual(status["catalog_source"], dialog.preview["catalog_source"])

    def test_submission_preflight_uses_live_numeric_identity_and_game_scoped_safe_paths(self):
        from services.catalog_submission_service import CatalogSubmissionService
        archive = self.archive(login="old-login", package_id="author.example")
        auth = Mock(restore=Mock(return_value=GitHubAccount("123", "renamed-login")))
        service = CatalogSubmissionService(self.store, auth)
        result = service.prepare(archive, expected_sha256=self.store._digest(archive))
        self.assertEqual("renamed-login", result["submitter"]["login"])
        self.assertEqual("author.example", result["package_id"])
        self.assertEqual("game", result["game_id"])
        self.assertNotIn("..", result["archive_path"])
        self.assertIn("/publishers/123/packages/", result["archive_path"])
        self.assertTrue(result["archive_path"].endswith("/1.0.0/package.botstudio.zip"))
        self.assertEqual(len(archive.read_bytes()), result["archive_size"])
        self.assertEqual(self.store._digest(archive), result["archive_sha256"])
        auth.restore.assert_called_once_with()
        self.assertEqual([], list(self.root.glob("store/*")))

    def test_submission_preflight_rejects_forged_publishers_and_missing_login(self):
        from services.catalog_submission_service import CatalogSubmissionService
        archive = self.archive(login="publisher")
        auth = Mock(restore=Mock(return_value=GitHubAccount("456", "different-user")))
        service = CatalogSubmissionService(self.store, auth)
        with self.assertRaisesRegex(PackageError, "does not match"):
            service.prepare(archive, expected_sha256=self.store._digest(archive))
        auth.restore.return_value = None
        with self.assertRaisesRegex(PackageError, "Sign in"):
            service.prepare(archive, expected_sha256=self.store._digest(archive))
        auth.restore.side_effect = RuntimeError("GitHub HTTP 401")
        with self.assertRaisesRegex(RuntimeError, "HTTP 401"):
            service.prepare(archive, expected_sha256=self.store._digest(archive))

    def test_submission_formal_checks_fail_before_authentication(self):
        from services.catalog_submission_service import CatalogSubmissionService
        auth = Mock()
        service = CatalogSubmissionService(self.store, auth)
        for archive, expected, limit in (
            (self.archive(login="publisher"), "0" * 64, service.MAX_PACKAGE_BYTES),
            (self.archive(), None, service.MAX_PACKAGE_BYTES),
            (self.archive(version="01.0.0", login="publisher"), None, service.MAX_PACKAGE_BYTES),
            (self.archive(login="publisher"), None, 1),
            (self.archive(login="publisher", package_id="../unsafe"), None, service.MAX_PACKAGE_BYTES),
            (self.archive(login="publisher", metadata_changes={"rights_confirmed": False}), None, service.MAX_PACKAGE_BYTES),
            (self.archive(login="publisher", metadata_changes={"license_id": "UNSPECIFIED"}), None, service.MAX_PACKAGE_BYTES),
        ):
            with self.subTest(archive=archive, expected=expected, limit=limit):
                with patch.object(service, "MAX_PACKAGE_BYTES", limit):
                    with self.assertRaises(PackageError):
                        service.prepare(archive, expected_sha256=expected or self.store._digest(archive))
        auth.restore.assert_not_called()

    def test_submission_archive_limits_and_real_internal_checksums_precede_account_check(self):
        from services.catalog_submission_service import CatalogSubmissionService
        from services.github_catalog_service import GitHubCatalogService
        archive = self.archive(login="publisher")
        auth = Mock()
        service = CatalogSubmissionService(self.store, auth)
        for limit in ("MAX_EXPANDED_BYTES", "MAX_ARCHIVE_FILES"):
            with self.subTest(limit=limit), patch.object(GitHubCatalogService, limit, 0):
                with self.assertRaisesRegex(PackageError, "expansion"):
                    service.prepare(archive, expected_sha256=self.store._digest(archive))
        corrupt = self.root / "corrupt.zip"
        with zipfile.ZipFile(archive) as original, zipfile.ZipFile(corrupt, "w") as target:
            for name in original.namelist():
                payload = original.read(name)
                if name == "content/functions/game/function.json":
                    payload += b"changed"
                target.writestr(name, payload)
        with self.assertRaisesRegex(PackageError, "[Cc]hecksum"):
            service.prepare(corrupt, expected_sha256=self.store._digest(corrupt))
        auth.restore.assert_not_called()

    def test_submission_preflight_preserves_declared_fork_without_authenticating_parent(self):
        from services.catalog_submission_service import CatalogSubmissionService
        parent = {
            "publisher": {"provider": "github", "account_id": "456", "login": "original"},
            "package_id": "example", "package_version": "1.0.0",
        }
        archive = self.archive(login="contributor", fork_of=parent)
        auth = Mock(restore=Mock(return_value=GitHubAccount("123", "contributor")))
        result = CatalogSubmissionService(self.store, auth).prepare(
            archive, expected_sha256=self.store._digest(archive),
        )
        self.assertEqual(parent, result["fork_of"])
        self.assertEqual("123", result["submitter"]["account_id"])

    def test_submission_ui_runs_live_check_in_worker_without_storage_or_upload(self):
        from services.catalog_submission_service import CatalogSubmissionService
        archive = self.archive(login="publisher")
        dialog = self.dialog()
        self.assertFalse(dialog.submission_check.isEnabled())
        dialog._previewed(self.store.preview(archive))
        dialog._controls()
        threads = []
        def restore():
            threads.append(get_ident())
            return GitHubAccount("123", "current-login")
        service = CatalogSubmissionService(self.store, Mock(restore=restore))
        with patch("ui.widgets.managed_packages_dialog.CatalogSubmissionService", return_value=service):
            dialog.submission_check.click()
            self.assertFalse(dialog.submission_check.isEnabled())
            self.wait_for_idle(dialog)
        self.assertNotIn(get_ident(), threads)
        self.assertEqual(1, len(threads))
        self.assertIn("@current-login", dialog.details.toPlainText())
        self.assertIn(dialog.preview["archive_sha256"], dialog.details.toPlainText())
        self.assertEqual(t("packages.submission_checked"), dialog.status.text())
        self.assertIsNone(dialog.confirmation)
        self.assertTrue(dialog.install.isEnabled())
        self.assertEqual([], self.store.list_installations())
        service.auth.restore = Mock(side_effect=RuntimeError("GitHub HTTP 401"))
        with patch("ui.widgets.managed_packages_dialog.CatalogSubmissionService", return_value=service):
            dialog.submission_check.click()
            self.wait_for_idle(dialog)
        self.assertIn("HTTP 401", dialog.status.text())
        self.assertNotIn("@current-login", dialog.details.toPlainText())

    def test_catalog_update_retains_repository_and_history_but_not_uploader_authority(self):
        first = self.install_catalog(self.archive())
        candidate = self.archive("1.1.0")
        source = self.source(candidate, asset_id=23, login="new-automation")
        updated = self.store.update(
            first["installation_id"], candidate,
            expected_sha256=source["archive_sha256"], catalog_source=source,
        )
        self.assertEqual(source, updated["catalog_source"])
        self.assertTrue(Path(first["original_archive"]).is_file())
        self.assertEqual("unverified", updated["publisher_verification"])
        receipt = Path(first["original_archive"]).parent / "catalog-source.json"
        self.assertEqual(22, json.loads(receipt.read_text())["asset_id"])

    def test_catalog_source_replacement_and_local_fallback_cannot_update(self):
        first = self.install_catalog(self.archive())
        candidate = self.archive("1.1.0")
        for source in (None, self.source(candidate, repository_id=99, asset_id=23)):
            with self.subTest(source=source):
                with self.assertRaisesRegex(PackageError, "source changed"):
                    self.store.update(
                        first["installation_id"], candidate,
                        expected_sha256=self.store._digest(candidate), catalog_source=source,
                    )
        with self.assertRaisesRegex(PackageError, "new package asset ID"):
            self.store.update(
                first["installation_id"], candidate,
                expected_sha256=self.store._digest(candidate), catalog_source=self.source(candidate),
            )
        self.assertEqual("1.0.0", self.store.status(first["installation_id"])["version"])
        self.assertEqual(1, len(list((self.store.root / first["installation_id"] / "generations").iterdir())))

    def test_local_installation_does_not_implicitly_acquire_a_catalog_source(self):
        first = self.store.install(self.archive())
        candidate = self.archive("1.1.0")
        with self.assertRaisesRegex(PackageError, "source changed"):
            self.store.update(
                first["installation_id"], candidate,
                expected_sha256=self.store._digest(candidate), catalog_source=self.source(candidate),
            )
        self.assertIsNone(self.store.status(first["installation_id"])["catalog_source"])

    def test_invalid_receipts_and_unbound_hash_never_install(self):
        path = self.archive()
        source = self.source(path)
        for change in (
            {"repository_id": True}, {"asset_id": 0}, {"archive_sha256": "0" * 64},
            {"repository": "attacker/catalog"}, {"uploader": {"account_id": "999", "login": "bad/user"}},
            {"verified": True},
        ):
            with self.subTest(change=change):
                with self.assertRaises(PackageError):
                    self.store.install(
                        path, expected_sha256=self.store._digest(path), catalog_source={**source, **change},
                    )
                self.assertEqual([], self.store.list_installations())
        with self.assertRaisesRegex(PackageError, "hash-bound"):
            self.store.install(path, catalog_source=source)

    def test_corrupt_persisted_receipt_is_reported_not_treated_as_local(self):
        first = self.install_catalog(self.archive())
        state_path = self.store.root / first["installation_id"] / "current.json"
        state = json.loads(state_path.read_text())
        state["catalog_source"] = None
        state_path.write_text(json.dumps(state), encoding="utf-8")
        entries = self.store.list_installations()
        self.assertEqual(1, len(entries))
        self.assertIn("source receipt", entries[0]["error"])

    def test_catalog_updates_preserve_local_edits_and_reject_foreign_publishers(self):
        first = self.install_catalog(self.archive())
        candidate = self.archive("1.1.0", publisher="456")
        with self.assertRaisesRegex(PackageError, "different package"):
            self.store.update(
                first["installation_id"], candidate, expected_sha256=self.store._digest(candidate),
                catalog_source=self.source(candidate, asset_id=23),
            )
        candidate = self.archive("1.1.0")
        edited = Path(first["working_directory"]) / "content/functions/game/function.json"
        edited.write_bytes(b"my changes")
        with self.assertRaisesRegex(PackageError, "local changes"):
            self.store.update(
                first["installation_id"], candidate, expected_sha256=self.store._digest(candidate),
                catalog_source=self.source(candidate, asset_id=23),
            )
        self.assertEqual(b"my changes", edited.read_bytes())

    def test_missing_changed_or_stripped_generation_receipts_fail_explicitly(self):
        first = self.install_catalog(self.archive())
        state_path = self.store.root / first["installation_id"] / "current.json"
        receipt_path = Path(first["original_archive"]).parent / "catalog-source.json"
        state_bytes, receipt_bytes = state_path.read_bytes(), receipt_path.read_bytes()
        for outcome in ("missing", "changed", "stripped"):
            with self.subTest(outcome=outcome):
                state_path.write_bytes(state_bytes)
                receipt_path.write_bytes(receipt_bytes)
                if outcome == "missing":
                    receipt_path.unlink()
                elif outcome == "changed":
                    receipt = json.loads(receipt_bytes)
                    receipt["asset_id"] = 99
                    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
                else:
                    state = json.loads(state_bytes)
                    del state["catalog_source"]
                    state_path.write_text(json.dumps(state), encoding="utf-8")
                self.assertIn("error", self.store.list_installations()[0])

    def test_catalog_update_ui_carries_source_consent_and_rejects_changed_bytes(self):
        first = self.install_catalog(self.archive())
        candidate = self.archive("1.1.0")
        preview = self.store.preview(candidate)
        preview["catalog_source"] = self.source(candidate, asset_id=23)
        dialog = self.dialog()
        dialog._loaded([first])
        dialog.table.selectRow(0)
        dialog._previewed(preview)
        dialog._controls()
        dialog.update.click()
        self.assertIn("23", dialog.confirmation.text())
        self.assertEqual(QMessageBox.No, dialog.confirmation.standardButton(dialog.confirmation.defaultButton()))
        original_bytes = candidate.read_bytes()
        candidate.write_bytes(b"changed")
        dialog.confirmation.done(QMessageBox.Yes)
        self.wait_for_idle(dialog)
        self.assertIn("changed since preview", dialog.status.text())
        self.assertEqual("1.0.0", self.store.status(first["installation_id"])["version"])
        candidate.write_bytes(original_bytes)
        dialog.update.click()
        dialog.confirmation.done(QMessageBox.Yes)
        self.wait_for_idle(dialog)
        updated = self.store.status(first["installation_id"])
        self.assertEqual("1.1.0", updated["version"])
        self.assertEqual(preview["catalog_source"], updated["catalog_source"])

    def test_clean_update_retains_original_history_and_new_working_copy(self):
        first = self.store.install(self.archive())
        second = self.store.update(first["installation_id"], self.archive("1.1.0", payload=b"fixed"))
        self.assertEqual("1.1.0", second["version"])
        self.assertFalse(second["modified"])
        self.assertNotEqual(first["working_directory"], second["working_directory"])
        self.assertTrue(Path(first["original_archive"]).exists())
        for state, name in ((first, "original"), (second, "fixed")):
            payload = json.loads((Path(state["working_directory"]) / "content/functions/game/function.json").read_text())
            self.assertEqual(name, payload["name"])
            self.assertEqual(state["version"], payload["package_origins"][-1]["package_version"])

    def test_dirty_workspace_blocks_update_and_preserves_changes(self):
        first = self.store.install(self.archive())
        edited = Path(first["working_directory"]) / "content/functions/game/function.json"
        edited.write_bytes(b"local changes")
        with self.assertRaisesRegex(PackageError, "local changes"):
            self.store.update(first["installation_id"], self.archive("1.1.0"))
        self.assertEqual(b"local changes", edited.read_bytes())
        self.assertEqual("1.0.0", self.store.status(first["installation_id"])["version"])

    def test_deleted_and_added_files_are_local_changes(self):
        first = self.store.install(self.archive())
        workspace = Path(first["working_directory"])
        (workspace / "content/functions/game/function.json").unlink()
        self.assertTrue(self.store.status(first["installation_id"])["modified"])
        (workspace / "extra.txt").write_text("local", encoding="utf-8")
        self.assertTrue(self.store.status(first["installation_id"])["modified"])

    def test_foreign_publisher_and_fork_cannot_replace_original(self):
        first = self.store.install(self.archive())
        for account, package in (("456", "example"), ("123", "example-fork")):
            with self.subTest(account=account, package=package):
                candidate = self.archive("1.1.0", account, package)
                with self.assertRaisesRegex(PackageError, "different package"):
                    self.store.update(first["installation_id"], candidate)
                alternative = self.store.install(candidate)
                self.assertNotEqual(first["installation_id"], alternative["installation_id"])
        self.assertEqual("1.0.0", self.store.status(first["installation_id"])["version"])

    def test_same_version_and_downgrade_are_rejected(self):
        first = self.store.install(self.archive("2.0.0"))
        for version in ("2.0.0", "1.9.9", "02.0.1", "next"):
            with self.subTest(version=version):
                with self.assertRaises(PackageError):
                    self.store.update(first["installation_id"], self.archive(version))

    def test_duplicate_install_requires_explicit_update(self):
        self.store.install(self.archive())
        with self.assertRaisesRegex(PackageError, "already installed"):
            self.store.install(self.archive("1.1.0"))

    def test_changes_during_update_preparation_are_preserved(self):
        first = self.store.install(self.archive())
        edited = Path(first["working_directory"]) / "content/functions/game/function.json"
        import_package = self.packages.import_package

        def edit_during_staging(*args, **kwargs):
            result = import_package(*args, **kwargs)
            edited.write_bytes(b"concurrent edit")
            return result

        with patch.object(self.packages, "import_package", side_effect=edit_during_staging):
            with self.assertRaisesRegex(PackageError, "local changes"):
                self.store.update(first["installation_id"], self.archive("1.1.0"))
        self.assertEqual(b"concurrent edit", edited.read_bytes())
        self.assertEqual("1.0.0", self.store.status(first["installation_id"])["version"])

    def test_fork_cannot_claim_same_publisher_and_package_identity(self):
        parent = {
            "package_id": "example", "package_version": "1.0.0",
            "publisher": {"provider": "github", "account_id": "123", "display_name": "author"},
        }
        with self.assertRaisesRegex(PackageError, "fork"):
            self.store.install(self.archive("1.1.0", fork_of=parent))

    def test_tampered_original_is_not_reported_as_supported_or_used_for_update(self):
        first = self.store.install(self.archive())
        Path(first["original_archive"]).write_bytes(b"tampered")
        with self.assertRaisesRegex(PackageError, "original"):
            self.store.status(first["installation_id"])
        with self.assertRaises(PackageError):
            self.store.update(first["installation_id"], self.archive("1.1.0"))

    def test_failed_activation_preserves_previous_version(self):
        first = self.store.install(self.archive())
        with patch("services.file_io.os.replace", side_effect=OSError("test disk error")):
            with self.assertRaisesRegex(OSError, "test disk error"):
                self.store.update(first["installation_id"], self.archive("1.1.0"))
        self.assertEqual("1.0.0", self.store.status(first["installation_id"])["version"])

    def test_invalid_installation_id_never_reads_outside_store(self):
        for value in ("../outside", "", "a" * 63, "A" * 64):
            with self.subTest(value=value):
                with self.assertRaises(PackageError):
                    self.store.status(value)

    def test_fork_relationship_is_preserved_and_shown_not_auto_selected(self):
        parent = {
            "package_id": "example", "package_version": "1.0.0",
            "publisher": {"provider": "github", "account_id": "123", "display_name": "author"},
        }
        archive = self.archive("1.0.1", "456", "example-fixed", fork_of=parent)
        manifest = self.packages.inspect_package(str(archive))
        self.assertEqual(parent, manifest["package_metadata"]["fork_of"])
        self.assertIn("github:123", self.packages.manifest_markdown(manifest))
        self.assertIn("Fork of", self.packages.manifest_markdown(manifest))
        result = self.store.install(archive)
        self.assertEqual(parent, result["fork_of"])

    def test_list_checks_live_changes_and_reports_damage_without_hiding_other_packages(self):
        self.assertEqual([], self.store.list_installations())
        first = self.store.install(self.archive())
        other = self.store.install(self.archive(package_id="other"))
        Path(first["working_directory"], "extra.txt").write_text("local", encoding="utf-8")
        entries = {entry["installation_id"]: entry for entry in self.store.list_installations()}
        self.assertTrue(entries[first["installation_id"]]["modified"])
        self.assertEqual("game", entries[first["installation_id"]]["game_id"])
        Path(first["original_archive"]).write_bytes(b"broken")
        entries = {entry["installation_id"]: entry for entry in self.store.list_installations()}
        self.assertIn("error", entries[first["installation_id"]])
        self.assertNotIn("error", entries[other["installation_id"]])
        state = self.store.root / other["installation_id"] / "current.json"
        state.write_text("[]", encoding="utf-8")
        entries = self.store.list_installations()
        self.assertTrue(all("error" in entry for entry in entries))

    def test_preview_digest_prevents_changed_file_from_being_installed_or_updated(self):
        path = self.archive()
        preview = self.store.preview(path)
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), preview["archive_sha256"])
        path.write_bytes(self.archive("1.1.0").read_bytes())
        with self.assertRaisesRegex(PackageError, "changed since preview"):
            self.store.install(path, expected_sha256=preview["archive_sha256"])
        self.assertEqual([], self.store.list_installations())
        first = self.store.install(self.archive())
        preview = self.store.preview(path)
        path.write_bytes(self.archive("1.2.0").read_bytes())
        with self.assertRaisesRegex(PackageError, "changed since preview"):
            self.store.update(first["installation_id"], path, expected_sha256=preview["archive_sha256"])
        self.assertEqual("1.0.0", self.store.status(first["installation_id"])["version"])

    def test_preview_accepts_legacy_without_inventing_managed_identity(self):
        path = self.archive()
        with zipfile.ZipFile(path) as source:
            entries = {name: source.read(name) for name in source.namelist()}
        manifest = json.loads(entries["manifest.json"])
        del manifest["package_metadata"]
        entries["manifest.json"] = json.dumps(manifest).encode()
        with zipfile.ZipFile(path, "w") as target:
            for name, content in entries.items():
                target.writestr(name, content)
        preview = self.store.preview(path)
        self.assertNotIn("package_metadata", preview["manifest"])
        with self.assertRaisesRegex(PackageError, "metadata"):
            self.store.install(path, expected_sha256=preview["archive_sha256"])
        self.assertEqual([], self.store.list_installations())

    def test_dialog_install_requires_preview_and_explicit_consent_then_survives_restart(self):
        dialog = self.dialog()
        self.assertFalse(dialog.install.isEnabled())
        self.assertFalse(dialog.update.isEnabled())
        path = self.archive()
        with patch("ui.widgets.managed_packages_dialog.QFileDialog.getOpenFileName",
                   return_value=(str(path), "")):
            dialog._choose_archive()
        self.wait_for_idle(dialog)
        self.assertIn("Fixture terms", dialog.details.toPlainText())
        self.assertIn("unverified", dialog.details.toPlainText())
        self.assertTrue(dialog.install.isEnabled())
        dialog._confirm("install")
        self.assertEqual(
            QMessageBox.No, dialog.confirmation.standardButton(dialog.confirmation.defaultButton()),
        )
        self.assertEqual([], self.store.list_installations())
        dialog.confirmation.done(QMessageBox.No)
        self.assertEqual([], self.store.list_installations())
        dialog._confirm("install")
        dialog.confirmation.done(QMessageBox.Yes)
        self.wait_for_idle(dialog)
        self.assertEqual(1, dialog.table.rowCount())
        self.assertIsNone(dialog.preview)
        restarted = self.dialog()
        restarted.reload()
        self.wait_for_idle(restarted)
        self.assertEqual("example", restarted.table.item(0, 0).text())
        restarted.table.selectRow(0)
        restarted._inspect_installed()
        self.wait_for_idle(restarted)
        self.assertIn("Fixture terms", restarted.details.toPlainText())
        self.assertIn("original.botstudio.zip", restarted.details.toPlainText())

    def test_dialog_updates_recheck_local_edits_and_never_switch_to_fork(self):
        first = self.store.install(self.archive())
        dialog = self.dialog()
        dialog.reload()
        self.wait_for_idle(dialog)
        dialog.table.selectRow(0)
        path = self.archive("1.1.0")
        dialog._previewed(self.store.preview(path))
        dialog._controls()
        self.assertTrue(dialog.update.isEnabled())
        changed = Path(first["working_directory"]) / "new.txt"
        changed.write_text("local", encoding="utf-8")
        dialog._confirm("update")
        dialog.confirmation.done(QMessageBox.Yes)
        self.wait_for_idle(dialog)
        self.assertIn("local changes", dialog.status.text())
        self.assertEqual("local", changed.read_text(encoding="utf-8"))
        changed.unlink()
        dialog._confirm("update")
        dialog.confirmation.done(QMessageBox.Yes)
        self.wait_for_idle(dialog)
        self.assertEqual("1.1.0", dialog.table.item(0, 1).text())
        self.assertTrue(Path(first["original_archive"]).is_file())
        dialog.table.selectRow(0)
        dialog._previewed(self.store.preview(self.archive("2.0.0", publisher="456")))
        dialog._confirm("update")
        dialog.confirmation.done(QMessageBox.Yes)
        self.wait_for_idle(dialog)
        self.assertIn("different package", dialog.status.text())
        self.assertEqual("1.1.0", self.store.status(first["installation_id"])["version"])
        dialog._confirm("install")
        dialog.confirmation.done(QMessageBox.Yes)
        self.wait_for_idle(dialog)
        self.assertEqual(2, dialog.table.rowCount())
        self.assertEqual("1.1.0", self.store.status(first["installation_id"])["version"])

    def test_dialog_rejects_replaced_preview_and_clears_failed_new_preview(self):
        dialog = self.dialog()
        path = self.archive()
        dialog._previewed(self.store.preview(path))
        path.write_bytes(self.archive("1.1.0").read_bytes())
        dialog._confirm("install")
        dialog.confirmation.done(QMessageBox.Yes)
        self.wait_for_idle(dialog)
        self.assertIn("changed since preview", dialog.status.text())
        self.assertEqual([], self.store.list_installations())
        path.write_bytes(b"not a ZIP")
        with patch("ui.widgets.managed_packages_dialog.QFileDialog.getOpenFileName",
                   return_value=(str(path), "")):
            dialog._choose_archive()
        self.wait_for_idle(dialog)
        self.assertIsNone(dialog.preview)
        self.assertFalse(dialog.install.isEnabled())
        self.assertFalse(dialog.details.toPlainText())
        dialog.logger.error.assert_called()

    def test_dialog_io_is_off_gui_thread_and_completion_remains_responsive(self):
        dialog = self.dialog()
        dialog.open()
        entered, released = ThreadEvent(), ThreadEvent()
        self.addCleanup(released.set)
        worker_threads, completion_threads = [], []
        main_thread = get_ident()

        def read():
            worker_threads.append(get_ident())
            entered.set()
            if not released.wait(2):
                raise RuntimeError("fixture timeout")
            return []

        original_loaded = dialog._loaded
        def loaded(result):
            completion_threads.append(get_ident())
            original_loaded(result)
        dialog._loaded = loaded
        dialog.service = Mock(list_installations=Mock(side_effect=read))
        ticker = QTimer()
        ticks = []
        def tick():
            ticks.append(True)
            if entered.is_set():
                released.set()
        ticker.timeout.connect(tick)
        ticker.start(5)
        self.addCleanup(ticker.stop)
        dialog.reload()
        self.assertFalse(dialog.choose.isEnabled())
        dialog.reject()
        self.assertTrue(dialog.isVisible())
        self.assertIsNotNone(dialog.worker)
        self.wait_for_idle(dialog)
        self.assertTrue(ticks)
        self.assertNotEqual([main_thread], worker_threads)
        self.assertEqual([main_thread], completion_threads)
        self.assertTrue(dialog.choose.isEnabled())

    def test_dialog_damaged_rows_and_open_failures_are_visible(self):
        first = self.store.install(self.archive())
        dialog = self.dialog()
        dialog.reload()
        self.wait_for_idle(dialog)
        dialog.table.selectRow(0)
        with patch("ui.widgets.managed_packages_dialog.QDesktopServices.openUrl", return_value=False):
            dialog._open_location("working_directory")
            self.wait_for_idle(dialog)
        self.assertIn(t("packages.open_failed"), dialog.status.text())
        Path(first["original_archive"]).write_bytes(b"broken")
        with patch("ui.widgets.managed_packages_dialog.QDesktopServices.openUrl") as opened:
            dialog._open_location("original_archive")
            self.wait_for_idle(dialog)
            opened.assert_not_called()
        dialog.reload()
        self.wait_for_idle(dialog)
        dialog.table.selectRow(0)
        self.assertEqual(t("packages.damaged"), dialog.table.item(0, 4).text())
        self.assertFalse(dialog.open_original.isEnabled())
        self.assertIn("original", dialog.status.text())

    def test_dialog_english_german_text_and_rendered_untrusted_preview(self):
        from ui.i18n import translator
        self.addCleanup(set_language, translator.LANG)
        for language in ("en", "de"):
            with self.subTest(language=language):
                set_language(language)
                dialog = self.dialog()
                self.assertNotEqual("packages.title", dialog.windowTitle())
                self.assertEqual(t("packages.inspect"), dialog.choose.text())
                self.assertEqual(t("packages.catalog_load"), dialog.catalog.text())
                for key in (
                    "catalog_load", "catalog_download", "catalog_source", "catalog_rules", "catalog_entry",
                    "catalog_prerelease", "catalog_empty", "catalog_loaded",
                    "catalog_tree", "catalog_unclassified", "catalog_declared",
                    "catalog_type_all", "catalog_type_function",
                    "catalog_type_helper", "catalog_type_event",
                    "catalog_type_unknown", "catalog_scope", "catalog_search",
                    "catalog_access", "catalog_auth_failed", "catalog_credentials_failed",
                    "catalog_rate_limit_at", "catalog_rate_limit",
                ):
                    self.assertNotEqual(f"packages.{key}", t(f"packages.{key}"))
                dialog._catalog_loaded({
                    "repository": "pernerch/botstudio-catalog", "repository_id": 123, "entries": [],
                })
                self.assertIn(t("packages.catalog_empty"), dialog.details.toPlainText())
                preview = self.store.preview(self.archive())
                preview["manifest"]["description"] = '<a href="https://invalid.example">click</a>'
                dialog._previewed(preview)
                self.assertIn('<a href=', dialog.details.toPlainText())
                self.assertTrue(dialog.details.isReadOnly())
                self.assertFalse(dialog.details.openLinks())
                self.assertFalse(dialog.details.openExternalLinks())
                self.assertNotIn("## Third-party notices", dialog.details.toPlainText())
                self.assertIn("Third-party notices", dialog.details.toPlainText())
                self.assertIn('href="https://invalid.example"', dialog.details.toPlainText())
                self.assertNotIn('<a href="https://invalid.example">', dialog.details.toHtml())
                block = dialog.details.document().begin()
                headings = []
                while block.isValid():
                    if block.blockFormat().headingLevel():
                        headings.append(block.text())
                    block = block.next()
                self.assertIn("Third-party notices", headings)
                self.assertIn("Support", headings)

    def test_package_markdown_never_loads_local_or_remote_resources(self):
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QTextDocument
        dialog = self.dialog()
        preview = self.store.preview(self.archive())
        preview["manifest"]["description"] = (
            "![remote](https://invalid.example/tracker.png)\n\n"
            "![local](file:///C:/private.png)\n\n"
            "[website](https://invalid.example)"
        )
        dialog._previewed(preview)
        for url in ("https://invalid.example/tracker.png", "file:///C:/private.png"):
            self.assertIsNone(dialog.details.loadResource(QTextDocument.ImageResource, QUrl(url)))
        dialog._submission_sent({"url": "https://github.com/example/<b>literal</b>"})
        self.assertIn("<b>literal</b>", dialog.details.toPlainText())
        self.assertIn("Support", dialog.details.toPlainText())

    def test_dialog_publisher_uses_declared_github_login_not_local_display_name(self):
        dialog = self.dialog()
        first = self.store.install(self.archive())
        self.assertIsNone(first["publisher_login"])
        dialog._loaded([first])
        self.assertEqual("github:123", dialog.table.item(0, 3).text())
        dialog._loaded([dict(first, publisher_login="github-user")])
        self.assertEqual("@github-user (github:123)", dialog.table.item(0, 3).text())

    def test_games_has_embedded_packages_tab_with_refresh_and_activation_guard(self):
        from ui.views.active_game_view import ActiveGameView
        from ui.views.main_window import MainWindow
        game = ActiveGameView("game")
        self.addCleanup(game.deleteLater)
        game.set_tab("Overview", QWidget())
        window = Mock(game_view=game)
        window._game_tab_changed.side_effect = lambda index: MainWindow._game_tab_changed(window, index)
        with (
            patch("ui.widgets.managed_packages_dialog.get_logger", return_value=Mock()),
            patch("ui.widgets.managed_packages_dialog.ManagedPackageService", return_value=self.store),
            patch("ui.views.main_window.PackageActivationService") as activation,
        ):
            MainWindow._add_packages_tab(window)
        activation.return_value.active_status.return_value = {
            "state": "not_active", "version": "", "update_available": False, "conflicts": [],
        }
        view = window.packages_view
        self.assertEqual(2, game.tabs.count())
        self.assertEqual(t("packages.tab"), game.tabs.tabText(1))
        self.assertIs(view, game.tabs.widget(1))
        self.assertFalse(view.isWindow())
        self.assertTrue(view.close_button.isHidden())
        with patch("PySide6.QtWidgets.QDialog.reject") as reject:
            view.reject()
            reject.assert_not_called()
        activation.assert_called_once_with(
            managed=self.store, execution_guard=window._package_execution_guard, logger=view.logger,
        )
        self.store.install(self.archive())
        game.tabs.setCurrentIndex(1)
        self.wait_for_idle(view)
        self.assertEqual(1, view.table.rowCount())
        self.store.install(self.archive(package_id="second"))
        game.tabs.setCurrentIndex(0)
        game.tabs.setCurrentIndex(1)
        self.wait_for_idle(view)
        self.assertEqual(2, view.table.rowCount())
        with patch.object(view, "reload") as reload:
            view.confirmation = Mock()
            MainWindow._game_tab_changed(window, 1)
            reload.assert_not_called()
            view.confirmation = None
        item = Mock()
        item.data.return_value = "game"
        window.navigation.menu.item.return_value = item
        window.view_indexes = {"game": 0}
        MainWindow.on_navigation_changed(window, 0)
        self.wait_for_idle(view)
        self.assertEqual(2, view.table.rowCount())

    def test_settings_has_no_account_or_package_entry(self):
        from ui.views.settings_view import SettingsView
        from ui.widgets.github_account_widget import GitHubAccountWidget
        with (
            patch("ui.views.settings_view.GameService") as games,
            patch("ui.views.settings_view.AppConfigService") as config,
            patch("ui.views.settings_view.get_logger", return_value=Mock()),
        ):
            games.return_value.load.return_value = []
            config.return_value.get_studio_settings.return_value = {}
            settings = SettingsView()
        self.addCleanup(settings.deleteLater)
        self.assertEqual([], settings.findChildren(GitHubAccountWidget))
        self.assertNotIn(t("packages.title"), [button.text() for button in settings.findChildren(QPushButton)])

    def test_catalog_is_read_in_worker_without_enabling_archive_install_or_writes(self):
        from models.github_account import GitHubAccount
        threads = []
        result = {
            "repository": "pernerch/botstudio-catalog", "repository_id": 123,
            "entries": [{
                "name": "<b>game.botstudio.zip</b>", "tag": "v1", "prerelease": True,
                "release_id": 11, "asset_id": 12, "size": 100,
                "uploader": GitHubAccount("123", "uploader"),
            }],
        }
        def load():
            threads.append(get_ident())
            return result
        dialog = self.dialog()
        dialog.preview = {"old": "preview"}
        with patch("ui.widgets.managed_packages_dialog.GitHubCatalogService") as catalog:
            catalog.return_value.load.side_effect = load
            dialog.catalog.click()
            self.assertFalse(dialog.catalog.isEnabled())
            self.wait_for_idle(dialog)
        self.assertNotIn(get_ident(), threads)
        self.assertEqual(1, len(threads))
        self.assertIsNone(dialog.preview)
        self.assertFalse(dialog.install.isEnabled())
        self.assertFalse(dialog.update.isEnabled())
        self.assertIn("<b>game.botstudio.zip</b>", dialog.details.toPlainText())
        self.assertIn("@uploader", dialog.details.toPlainText())
        self.assertIn("pernerch/botstudio-catalog", dialog.details.toPlainText())
        self.assertEqual([], self.store.list_installations())
        with patch("ui.widgets.managed_packages_dialog.GitHubCatalogService") as catalog:
            catalog.return_value.load.side_effect = PackageError("GitHub catalog HTTP 404")
            dialog.catalog.click()
            self.wait_for_idle(dialog)
        self.assertIn("HTTP 404", dialog.status.text())
        self.assertEqual("", dialog.details.toPlainText())
        self.assertTrue(dialog.catalog.isEnabled())

    def test_catalog_download_without_safe_activation_fails_before_network_or_file_selection(self):
        from services.github_catalog_service import GitHubCatalogService
        from models.github_account import GitHubAccount
        archive = self.archive()
        payload = archive.read_bytes()
        entry = {
            "asset_id": 22, "release_id": 11, "tag": "v1", "prerelease": False,
            "name": "example.botstudio.zip", "size": len(payload),
            "digest": "sha256:" + hashlib.sha256(payload).hexdigest(),
            "uploader": GitHubAccount("123", "uploader"),
        }
        catalog = {
            "repository": GitHubCatalogService.REPOSITORY, "repository_id": 10, "entries": [entry],
        }
        request = Mock(return_value=Mock(status_code=200, iter_content=Mock(return_value=[payload])))
        service = GitHubCatalogService(request=request, credentials=Mock(load=Mock(return_value=None)))
        dialog = self.dialog()
        dialog._catalog_loaded(catalog)
        self.assertFalse(dialog.catalog_download.isEnabled())
        unknown = dialog.catalog_entries.topLevelItem(0)
        self.assertEqual(t("packages.catalog_type_unknown"), unknown.text(0))
        dialog.catalog_entries.setCurrentItem(unknown.child(0).child(0))
        dialog._controls()
        target = self.root / "download.botstudio.zip"
        with patch(
            "ui.widgets.managed_packages_dialog.QFileDialog.getSaveFileName", return_value=("", ""),
        ), patch("ui.widgets.managed_packages_dialog.GitHubCatalogService") as unused:
            dialog.catalog_download.click()
            unused.assert_not_called()
        self.assertFalse(target.exists())
        self.assertIsNone(dialog.preview)
        self.assertIn(t("packages.download_unavailable"), dialog.status.text())
        request.assert_not_called()
        self.assertEqual([], self.store.list_installations())
        self.assertEqual([], list(self.root.glob("botstudio-download-*")))

    def test_catalog_tree_groups_variants_and_requires_explicit_version_selection(self):
        from models.github_account import GitHubAccount, GitHubAssetUploader
        dialog = self.dialog()
        entries = []
        for asset_id, game, account_id, login, version in (
            (1, "Game A", "123", "alice", "1.0.0"),
            (2, "Game A", "456", "bob", "2.0.0"),
            (3, "Game B", "123", "alice", "1.0.0"),
            (4, "Game A", "123", "alice-renamed", "1.1.0"),
        ):
            entries.append({
                "name": "package.botstudio.zip", "tag": f"release-{asset_id}",
                "prerelease": asset_id == 2, "release_id": asset_id,
                "asset_id": asset_id, "size": 100,
                "uploader": GitHubAssetUploader("41898282", "github-actions[bot]"),
                "package": {
                    "game_id": game, "package_id": "function-example", "version": version,
                    "publisher": GitHubAccount(account_id, login),
                },
            })
        for asset_id, package_id in ((5, "helper-example"), (6, "event-example")):
            entries.append({
                "name": f"{package_id}.botstudio.zip", "tag": f"release-{asset_id}",
                "prerelease": False, "release_id": asset_id, "asset_id": asset_id,
                "size": 100,
                "uploader": GitHubAssetUploader("41898282", "github-actions[bot]"),
                "package": {
                    "game_id": "Game A", "package_id": package_id, "version": "1.0.0",
                    "publisher": GitHubAccount("123", "alice"),
                },
            })
        entries.append({
            "name": "legacy.botstudio.zip", "tag": "legacy", "prerelease": False,
            "release_id": 7, "asset_id": 7, "size": 100,
            "uploader": GitHubAssetUploader("41898282", "github-actions[bot]"),
        })
        dialog._catalog_loaded({
            "repository": "pernerch/botstudio-catalog", "repository_id": 123, "entries": entries,
        })
        dialog._controls()
        self.assertEqual(4, dialog.catalog_entries.topLevelItemCount())
        self.assertEqual(
            {
                t("packages.catalog_type_function"),
                t("packages.catalog_type_helper"),
                t("packages.catalog_type_event"),
                t("packages.catalog_type_unknown"),
            },
            {
                dialog.catalog_entries.topLevelItem(index).text(0)
                for index in range(dialog.catalog_entries.topLevelItemCount())
            },
        )
        function_group = dialog.catalog_entries.topLevelItem(0)
        self.assertEqual(t("packages.catalog_type_function"), function_group.text(0))
        game = function_group.child(0)
        package = game.child(0)
        self.assertEqual("Game A", game.text(0))
        self.assertEqual("function-example", package.text(0))
        self.assertEqual(2, package.childCount())
        self.assertEqual(2, package.child(0).childCount())
        self.assertFalse(dialog.catalog_download.isEnabled())
        dialog.catalog_entries.setCurrentItem(package.child(1))
        self.assertFalse(dialog.catalog_download.isEnabled())
        version = package.child(1).child(0)
        dialog.catalog_entries.setCurrentItem(version)
        self.assertTrue(dialog.catalog_download.isEnabled())
        self.assertEqual(2, dialog._catalog_entry()["asset_id"])
        self.assertIn("@bob", dialog.details.toPlainText())
        self.assertIn("@github-actions[bot]", dialog.details.toPlainText())
        self.assertIsNone(dialog.preview)
        self.assertFalse(dialog.install.isEnabled())
        self.assertIn(t("packages.catalog_prerelease"), version.text(0))
        dialog.activation_service = Mock()
        with (
            patch("ui.widgets.managed_packages_dialog.QFileDialog.getSaveFileName",
                  return_value=(str(self.root / "selected.zip"), "")),
            patch("ui.widgets.managed_packages_dialog.GitHubCatalogService") as service,
        ):
            service.return_value.download_preview.side_effect = PackageError("deliberate test stop")
            dialog.catalog_download.click()
            self.wait_for_idle(dialog)
            self.assertEqual(entries[1], service.return_value.download_preview.call_args.args[1])
        self.assertIn("deliberate test stop", dialog.status.text())
        dialog.preview = {"old": "downloaded"}
        dialog.catalog_entries.setCurrentItem(game)
        self.assertIsNone(dialog.preview)
        self.assertFalse(dialog.install.isEnabled())
        self.assertFalse(dialog.catalog_download.isEnabled())
        self.assertEqual([], self.store.list_installations())
        dialog.catalog_type.setCurrentIndex(dialog.catalog_type.findData("helper"))
        self.assertEqual(1, dialog.catalog_entries.topLevelItemCount())
        self.assertEqual(
            t("packages.catalog_type_helper"),
            dialog.catalog_entries.topLevelItem(0).text(0),
        )
        dialog.catalog_search.setText("helper-example")
        self.assertEqual(1, dialog.catalog_entries.topLevelItemCount())
        self.assertIn("1 matches among 7", dialog.catalog_scope.text())

    def test_catalog_filters_event_display_name_author_and_contents(self):
        from models.github_account import GitHubAssetUploader
        dialog = self.dialog()
        entry = {
            "name": "package.botstudio.zip", "tag": "catalog-pr-3-head",
            "prerelease": False, "release_id": 3, "asset_id": 4, "size": 200,
            "uploader": GitHubAssetUploader("41898282", "github-actions[bot]"),
            "package": {
                "game_id": "whiteout_survival", "game_name": "Whiteout Survival",
                "package_id": "opaque-stable-id", "package_name": "Journey of Light",
                "package_type": "event", "version": "1.0.36",
                "publisher": GitHubAccount("120588088", "pernerch"),
                "author": "@pernerch", "description": "Run the mission unattended",
                "contents": {"functions": 12, "library": 4},
            },
        }
        dialog._catalog_loaded({
            "repository": "pernerch/botstudio-catalog", "repository_id": 10,
            "entries": [entry], "next_page": None,
        })
        dialog.catalog_type.setCurrentIndex(dialog.catalog_type.findData("event"))
        for query in ("Light", "Journey", "pernerch", "unattended", ""):
            dialog.catalog_search.setText(query)
            self.assertEqual(1, dialog.catalog_entries.topLevelItemCount(), query)
        group = dialog.catalog_entries.topLevelItem(0)
        self.assertEqual(t("packages.catalog_type_event"), group.text(0))
        self.assertEqual("Whiteout Survival", group.child(0).text(0))
        self.assertEqual("Journey of Light", group.child(0).child(0).text(0))
        details = dialog._catalog_entry_text(entry)
        self.assertIn("@pernerch", details)
        self.assertIn("functions: 12", details)
        self.assertIn("Run the mission unattended", details)
        dialog.catalog_type.setCurrentIndex(dialog.catalog_type.findData("helper"))
        self.assertEqual(0, dialog.catalog_entries.topLevelItemCount())
        variant = {
            **entry, "release_id": 5, "asset_id": 6,
            "package": {**entry["package"], "package_id": "different-stable-id"},
        }
        dialog.catalog_type.setCurrentIndex(dialog.catalog_type.findData("event"))
        dialog._catalog_loaded({
            **dialog.catalog_result, "entries": [entry, variant],
        })
        game = dialog.catalog_entries.topLevelItem(0).child(0)
        self.assertEqual(2, game.childCount())

    def test_loading_older_catalog_pages_merges_deduplicates_and_retains_on_error(self):
        from models.github_account import GitHubAssetUploader
        entry = {
            "name": "package.botstudio.zip", "tag": "old", "prerelease": False,
            "release_id": 50, "asset_id": 60, "size": 100,
            "uploader": GitHubAssetUploader("123", "uploader"),
        }
        first = {
            "repository": "pernerch/botstudio-catalog", "repository_id": 10,
            "entries": [entry], "next_page": 2,
        }
        dialog = self.dialog()
        dialog._catalog_loaded(first)
        dialog._controls()
        self.assertTrue(dialog.catalog_more.isEnabled())
        second = {**first, "entries": [entry, {**entry, "asset_id": 61}], "next_page": None}
        with patch("ui.widgets.managed_packages_dialog.GitHubCatalogService") as service:
            service.return_value.load.return_value = second
            dialog.catalog_more.click()
            self.assertFalse(dialog.catalog_more.isEnabled())
            self.wait_for_idle(dialog)
            service.return_value.load.assert_called_once_with(page=2)
        self.assertEqual(2, len(dialog.catalog_result["entries"]))
        self.assertFalse(dialog.catalog_more.isEnabled())
        self.assertFalse(dialog.catalog_download.isEnabled())
        self.assertFalse(dialog.install.isEnabled())
        retained = dialog.catalog_result
        dialog._catalog_page_loaded({**second, "repository_id": 99})
        self.assertIs(retained, dialog.catalog_result)
        self.assertIn(t("packages.catalog_page_changed"), dialog.status.text())
        dialog._catalog_page_loaded({**second, "entries": [{**entry, "size": 101}]})
        self.assertIs(retained, dialog.catalog_result)
        dialog._catalog_loaded(first)
        with patch("ui.widgets.managed_packages_dialog.GitHubCatalogService") as service:
            service.return_value.load.side_effect = PackageError("HTTP 429")
            dialog._controls()
            dialog.catalog_more.click()
            self.wait_for_idle(dialog)
        self.assertEqual(first, dialog.catalog_result)
        self.assertTrue(dialog.catalog_more.isEnabled())
        self.assertIn("HTTP 429", dialog.status.text())

    def test_catalog_download_validates_internal_checksums_not_only_github_digest(self):
        from io import BytesIO
        from services.github_catalog_service import GitHubCatalogService
        source = self.archive()
        content = BytesIO()
        with zipfile.ZipFile(source) as original, zipfile.ZipFile(content, "w") as changed:
            for name in original.namelist():
                value = original.read(name)
                if name == "content/functions/game/function.json":
                    value += b" "
                changed.writestr(name, value)
        payload = content.getvalue()
        entry = {
            "asset_id": 22, "release_id": 11, "size": len(payload),
            "digest": "sha256:" + hashlib.sha256(payload).hexdigest(),
        }
        catalog = {
            "repository": GitHubCatalogService.REPOSITORY, "repository_id": 10, "entries": [entry],
        }
        service = GitHubCatalogService(request=Mock(return_value=Mock(
            status_code=200, iter_content=Mock(return_value=[payload]),
        )), credentials=Mock(load=Mock(return_value=None)))
        target = self.root / "invalid.botstudio.zip"
        with patch.object(service, "load", return_value=catalog):
            with self.assertRaisesRegex(PackageError, "[Cc]hecksum"):
                service.download_preview(catalog, entry, target, self.store)
        self.assertFalse(target.exists())
        self.assertEqual([], list(self.root.glob("botstudio-download-*")))
        self.assertEqual([], self.store.list_installations())

    def test_catalog_download_failure_never_publishes_target_or_leaves_staging(self):
        from services.github_catalog_service import GitHubCatalogService
        payload = self.archive().read_bytes()
        entry = {
            "asset_id": 22, "release_id": 11, "size": len(payload),
            "digest": "sha256:" + hashlib.sha256(payload).hexdigest(),
            "uploader": GitHubAccount("999", "automation"),
        }
        catalog = {
            "repository": GitHubCatalogService.REPOSITORY, "repository_id": 10, "entries": [entry],
        }
        target = self.root / "download.zip"
        for outcome in ("checksum", "invalid-package", "changed", "concurrent-target", "expansion"):
            with self.subTest(outcome=outcome):
                request = Mock(return_value=Mock(
                    status_code=200, iter_content=Mock(return_value=[payload]),
                ))
                service = GitHubCatalogService(request=request, credentials=Mock(load=Mock(return_value=None)))
                statuses = [catalog, {**catalog, "entries": []}] if outcome == "changed" else [catalog, catalog]
                def inspect(path):
                    if outcome == "invalid-package":
                        raise PackageError("fixture invalid package")
                    if outcome == "concurrent-target":
                        target.write_bytes(b"local")
                    return self.store.preview(path)
                selected = {**entry, "digest": "sha256:" + "0" * 64} if outcome == "checksum" else entry
                listing = {**catalog, "entries": [selected]}
                if outcome == "checksum":
                    statuses = [listing]
                with (
                    patch.object(service, "load", side_effect=statuses),
                    patch.object(service, "MAX_EXPANDED_BYTES", 0 if outcome == "expansion" else 512 * 1024 * 1024),
                ):
                    managed = Mock(preview=Mock(side_effect=inspect))
                    with self.assertRaises((PackageError, FileExistsError)):
                        service.download_preview(listing, selected, target, managed)
                self.assertEqual([], list(self.root.glob("botstudio-download-*")))
                if outcome == "concurrent-target":
                    self.assertEqual(b"local", target.read_bytes())
                    target.unlink()
                else:
                    self.assertFalse(target.exists())
                self.assertEqual([], self.store.list_installations())

    def test_games_activation_guard_still_requires_idle_debug_and_save(self):
        from ui.views.main_window import MainWindow
        for idle in (True, False):
            with self.subTest(debug_idle=idle):
                window = Mock()
                window.runtime_service.content_import_guard.return_value = nullcontext()
                window._package_debug_idle.return_value = idle
                if idle:
                    with MainWindow._package_execution_guard(window):
                        pass
                else:
                    with self.assertRaisesRegex(PackageError, "Stop debug runs"):
                        with MainWindow._package_execution_guard(window):
                            self.fail("busy editor must block activation")

    def test_main_window_does_not_close_during_package_work_or_confirmation(self):
        from ui.views.main_window import MainWindow
        for worker, confirmation in ((Mock(), None), (None, Mock())):
            with self.subTest(worker=worker, confirmation=confirmation):
                window = Mock(packages_view=Mock(worker=worker, confirmation=confirmation))
                event = Mock()
                MainWindow.closeEvent(window, event)
                event.ignore.assert_called_once()
                window.watchdog_ipc_server.stop.assert_not_called()

    def test_dialog_file_cancel_is_non_destructive_and_original_open_uses_local_url(self):
        first = self.store.install(self.archive())
        dialog = self.dialog()
        preview = self.store.preview(self.archive("1.1.0"))
        dialog._previewed(preview)
        with patch("ui.widgets.managed_packages_dialog.QFileDialog.getOpenFileName",
                   return_value=("", "")):
            dialog._choose_archive()
        self.assertIs(preview, dialog.preview)
        self.assertIsNone(dialog.worker)
        dialog.reload()
        self.wait_for_idle(dialog)
        dialog.table.selectRow(0)
        with patch("ui.widgets.managed_packages_dialog.QDesktopServices.openUrl",
                   return_value=True) as opened:
            dialog._open_location("original_archive")
            self.wait_for_idle(dialog)
        url = opened.call_args.args[0]
        self.assertTrue(url.isLocalFile())
        self.assertEqual(Path(first["original_archive"]), Path(url.toLocalFile()))


if __name__ == "__main__":
    result = unittest.main(exit=False).result
    print("TEST RESULT: SUCCESS" if result.wasSuccessful() else "TEST RESULT: FAILURE")
    raise SystemExit(0 if result.wasSuccessful() else 1)
