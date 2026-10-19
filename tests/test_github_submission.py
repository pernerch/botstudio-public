import base64
from copy import deepcopy
import faulthandler
import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import get_ident
import time
import unittest
from unittest.mock import Mock, patch
from zipfile import ZipFile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtWidgets import QApplication, QMessageBox
import requests

from models.github_account import GitHubAccount
from services.catalog_submission_service import CatalogSubmissionService
from services.github_submission_service import GitHubSubmissionService
from services.managed_package_service import ManagedPackageService
from services.package_service import PackageError, PackageService
from ui.widgets.managed_packages_dialog import ManagedPackagesDialog


class GitHubSubmissionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        started = time.monotonic()
        print(f"TEST START: {self.id()}")
        faulthandler.dump_traceback_later(20, exit=True)
        self.addCleanup(faulthandler.cancel_dump_traceback_later)
        self.addCleanup(lambda: print(f"TEST END: {self.id()} duration {time.monotonic() - started:.3f}s"))
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        packages = PackageService(
            function_root=str(self.root / "functions"),
            library_service=Mock(), variable_service=Mock(), game_service=Mock(),
        )
        self.managed = ManagedPackageService(self.root / "store", packages)
        payload = b'{"game_id":"game","function_id":"function"}'
        checksums = {"content/functions/game/function.json": hashlib.sha256(payload).hexdigest()}
        manifest = {
            "format": "botstudio-package", "format_version": 1,
            "package_name": "Example", "description": "", "game": {"id": "game"}, "contents": {"functions": 1},
            "checksums": checksums,
            "package_metadata": {
                "package_id": "example", "package_version": "1.0.0", "author": "Fixture",
                "publisher": {"provider": "github", "account_id": "123", "login": "contributor"},
                "license_id": "MIT", "license_text": "Fixture terms", "rights_confirmed": True,
            },
        }
        self.archive = self.root / "package.botstudio.zip"
        with ZipFile(self.archive, "w") as archive:
            archive.writestr("manifest.json", json.dumps(manifest))
            archive.writestr("checksums.json", json.dumps(checksums))
            archive.writestr(next(iter(checksums)), payload)
        self.account = GitHubAccount("123", "contributor")
        auth = Mock(restore=Mock(return_value=self.account))
        self.publication = Mock()
        self.publication.credentials.load.return_value = "fixture_token"
        self.publication._account.return_value = self.account
        self.preflight = CatalogSubmissionService(self.managed, auth)
        self.calls = []
        self.owner = 999
        self.base = "a" * 40
        self.fork_exists = False
        self.existing_version = False
        self.fail_path = None
        self.blob_too_large = False
        self.registered_game = True
        self.transport = Mock(side_effect=self.respond)
        self.service = GitHubSubmissionService(self.preflight, self.publication, self.transport)

    @staticmethod
    def response(payload, status=200, scopes="public_repo"):
        return Mock(
            status_code=status, headers={"X-OAuth-Scopes": scopes},
            iter_content=Mock(return_value=[json.dumps(payload).encode("utf-8")]),
        )

    def fork(self):
        return {
            "id": 20, "name": "botstudio-catalog-contributions", "private": False, "fork": True,
            "owner": {"id": 123}, "parent": {"id": 10},
        }

    def respond(self, method, url, **kwargs):
        path = url.removeprefix("https://api.github.com")
        data = kwargs.get("json")
        self.calls.append((method, path, deepcopy(data)))
        if self.fail_path and self.fail_path in path:
            return self.response({"private_error": "never shown"}, 500)
        if path == "/repos/pernerch/botstudio-catalog":
            return self.response({
                "id": 10, "full_name": "pernerch/botstudio-catalog", "private": False, "fork": False,
                "default_branch": "main", "owner": {"id": self.owner},
            })
        if path == "/repositories/10/git/ref/heads/main":
            return self.response({"object": {"sha": self.base}})
        if path == f"/repositories/10/git/commits/{self.base}":
            return self.response({"tree": {"sha": "b" * 40}})
        if "/contents/game-registry.json?" in path:
            return self.response({
                "encoding": "base64", "content": base64.b64encode(json.dumps({
                    "schema_version": 1, "games": [
                        {"id": "game", "name": "Game", "android_packages": []},
                    ] if self.registered_game else [],
                }).encode()).decode(),
            })
        if "/contents/" in path:
            return self.response({"sha": "e" * 40} if self.existing_version else {}, 200 if self.existing_version else 404)
        if path == "/repos/contributor/botstudio-catalog-contributions":
            return self.response(self.fork() if self.fork_exists else {}, 200 if self.fork_exists else 404)
        if path.endswith("/forks"):
            return self.response(self.fork(), 202)
        if path.endswith("/git/refs"):
            return self.response({"ref": data["ref"]}, 201)
        if path.endswith("/git/blobs"):
            if self.blob_too_large:
                return self.response({"message": (
                    "Sorry, your input was too large to process. Consider creating the blob "
                    "in a local clone of the repository and then pushing it to GitHub."
                )}, 422)
            payload = base64.b64decode(data["content"])
            sha = hashlib.sha1(f"blob {len(payload)}\0".encode() + payload, usedforsecurity=False).hexdigest()
            return self.response({"sha": sha}, 201)
        if path.endswith("/git/trees"):
            return self.response({"sha": "c" * 40}, 201)
        if path.endswith("/git/commits"):
            return self.response({"sha": "d" * 40}, 201)
        if "/git/refs/heads/" in path:
            return self.response({"object": {"sha": "d" * 40}})
        if "/git/ref/heads/botstudio-submit-" in path:
            return self.response({"object": {"sha": "d" * 40}})
        if path == "/repositories/10/pulls":
            return self.response({
                "number": 7, "head": {"sha": "d" * 40}, "base": {"repo": {"id": 10}},
            }, 201)
        raise AssertionError(f"Unexpected request: {method} {path}")

    def prepare(self):
        return self.service.prepare(self.archive, expected_sha256=self.managed._digest(self.archive))

    def test_unregistered_game_is_rejected_before_any_public_write(self):
        self.registered_game = False
        with self.assertRaisesRegex(PackageError, "Game is not registered"):
            self.prepare()
        self.assertTrue(all(method == "GET" for method, _path, _data in self.calls))

    def test_prepare_is_read_only_and_checks_both_live_accounts(self):
        plan = self.prepare()
        self.assertEqual(10, plan["target"]["repository_id"])
        self.assertEqual(self.base, plan["target"]["base_commit"])
        self.assertTrue(all(method == "GET" for method, _path, _data in self.calls))
        self.publication._account.assert_called_once_with("fixture_token")
        self.preflight.auth.restore.assert_called_once_with()
        self.publication._account.return_value = GitHubAccount("456", "other")
        with self.assertRaisesRegex(PackageError, "do not match"):
            self.prepare()
        self.publication.credentials.load.return_value = None
        with self.assertRaisesRegex(PackageError, "Authorize"):
            self.prepare()

    def test_blob_input_limit_uses_exact_byte_git_push_then_checks_head_before_pr(self):
        plan = self.prepare()
        self.blob_too_large = True
        with patch.object(self.service, "_push_git_commit", return_value="d" * 40) as push:
            result = self.service.submit(self.archive, plan=plan)
        self.assertEqual(7, result["number"])
        remote, token, target, branch, payloads = push.call_args.args
        self.assertEqual("https://github.com/contributor/botstudio-catalog-contributions.git", remote)
        self.assertEqual("fixture_token", token)
        self.assertEqual(plan["target"], target)
        self.assertEqual(result["branch"], branch)
        self.assertEqual(self.archive.read_bytes(), payloads[0][1])
        self.assertEqual(plan["submission"], json.loads(payloads[1][1]))
        self.assertFalse(any(path.endswith("/git/trees") for _, path, _ in self.calls))
        self.assertTrue(any("/git/ref/heads/botstudio-submit-" in path for _, path, _ in self.calls))

    def test_other_422_error_does_not_push_or_disclose_response(self):
        self.service.request = Mock(return_value=self.response({"message": "private_error"}, 422))
        with self.assertRaisesRegex(PackageError, "HTTP 422 during POST blobs") as caught:
            self.service._api("POST", "/repositories/10/git/blobs", "fixture_token")
        self.assertNotIn("private_error", str(caught.exception))

    def test_large_archive_without_git_has_actionable_error(self):
        with patch("services.github_submission_service.shutil.which", return_value=None):
            with self.assertRaisesRegex(PackageError, "Install Git for Windows"):
                self.service._push_git_commit("unused", "fixture_token", {}, "branch", [])

    @unittest.skipUnless(shutil.which("git"), "Git integration requires an existing Git installation")
    def test_git_transport_preserves_base_tree_and_both_payloads_without_credentials_on_disk(self):
        remote = self.root / "remote.git"
        env = dict(os.environ, GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
                   GIT_AUTHOR_NAME="Test", GIT_AUTHOR_EMAIL="test@example.invalid",
                   GIT_COMMITTER_NAME="Test", GIT_COMMITTER_EMAIL="test@example.invalid")

        def git(*args, data=None):
            return subprocess.run(
                [shutil.which("git"), *args], cwd=self.root, env=env, input=data,
                capture_output=True, check=True, timeout=10,
            ).stdout.strip().decode()

        git("init", "--bare", str(remote))
        blob = git("--git-dir", str(remote), "hash-object", "-w", "--stdin", data=b"keep me")
        tree = git("--git-dir", str(remote), "mktree",
                   data=f"100644 blob {blob}\tREADME.txt\n".encode())
        base = git("--git-dir", str(remote), "commit-tree", tree, data=b"Base\n")
        git("--git-dir", str(remote), "update-ref", "refs/heads/submission", base)
        payloads = [("packages/package.botstudio.zip", self.archive.read_bytes()),
                    ("packages/submission.json", b'{"schema_version":1}')]
        head = self.service._push_git_commit(
            str(remote), "fixture_secret_token", {"base_commit": base, "base_tree": tree},
            "submission", payloads,
        )
        self.assertEqual(head, git("--git-dir", str(remote), "rev-parse", "refs/heads/submission"))
        self.assertEqual(base, git("--git-dir", str(remote), "rev-parse", f"{head}^"))
        self.assertEqual("keep me", git("--git-dir", str(remote), "show", f"{head}:README.txt"))
        for path, payload in payloads:
            actual = subprocess.run(
                [shutil.which("git"), "--git-dir", str(remote), "show", f"{head}:{path}"],
                env=env, capture_output=True, check=True, timeout=10,
            ).stdout
            self.assertEqual(payload, actual)
        self.assertNotIn("fixture_secret_token", (remote / "config").read_text())
    def test_submit_creates_exact_two_file_pr_and_never_merges_or_releases(self):
        plan = self.prepare()
        self.calls.clear()
        result = self.service.submit(self.archive, plan=plan)
        self.assertEqual("https://github.com/pernerch/botstudio-catalog/pull/7", result["url"])
        writes = [(method, path, data) for method, path, data in self.calls if method != "GET"]
        self.assertEqual(8, len(writes))
        blobs = [base64.b64decode(data["content"]) for _method, path, data in writes if path.endswith("/git/blobs")]
        self.assertEqual(self.archive.read_bytes(), blobs[0])
        self.assertEqual(plan["submission"], json.loads(blobs[1]))
        tree = next(data for _method, path, data in writes if path.endswith("/git/trees"))
        self.assertEqual("b" * 40, tree["base_tree"])
        self.assertEqual(
            {plan["submission"]["archive_path"], plan["submission"]["metadata_path"]},
            {entry["path"] for entry in tree["tree"]},
        )
        self.assertFalse(next(data for _method, path, data in writes if "/git/refs/heads/" in path)["force"])
        self.assertFalse(any("/merge" in path or "/releases" in path for _method, path, _data in writes))
        self.assertEqual([], self.managed.list_installations())
        for call in self.transport.call_args_list:
            self.assertFalse(call.kwargs["allow_redirects"])
            self.assertEqual((5, 30), call.kwargs["timeout"])

    def test_owner_uses_new_catalog_branch_and_existing_valid_fork_is_reused(self):
        for owner, existing in ((123, False), (999, True)):
            with self.subTest(owner=owner, existing=existing):
                self.owner, self.fork_exists = owner, existing
                plan = self.prepare()
                self.calls.clear()
                self.service.submit(self.archive, plan=plan)
                self.assertFalse(any(path.endswith("/forks") for _method, path, _data in self.calls))
                refs = next(path for method, path, _data in self.calls if method == "POST" and path.endswith("/git/refs"))
                self.assertIn(f"/repositories/{10 if owner == 123 else 20}/", refs)

    def test_changed_bytes_target_or_account_prevent_all_writes(self):
        plan = self.prepare()
        original = self.archive.read_bytes()
        for change in ("bytes", "target", "account", "existing"):
            with self.subTest(change=change):
                self.archive.write_bytes(original)
                self.base = plan["target"]["base_commit"]
                self.preflight.auth.restore.return_value = self.account
                self.existing_version = False
                if change == "bytes":
                    self.archive.write_bytes(original + b"changed")
                elif change == "target":
                    self.base = "e" * 40
                elif change == "account":
                    self.preflight.auth.restore.return_value = GitHubAccount("456", "other")
                else:
                    self.existing_version = True
                self.calls.clear()
                with self.assertRaises(PackageError):
                    self.service.submit(self.archive, plan=plan)
                self.assertTrue(all(method == "GET" for method, _path, _data in self.calls))

    def test_wrong_fork_and_partial_write_failures_never_force_or_cleanup(self):
        plan = self.prepare()
        with patch.object(self, "fork", return_value={**self.fork(), "parent": {"id": 99}}):
            with self.assertRaisesRegex(PackageError, "fork identity"):
                self.service.submit(self.archive, plan=plan)
        self.calls.clear()
        self.fail_path = "/git/blobs"
        with self.assertRaisesRegex(PackageError, "branch botstudio-submit-") as caught:
            self.service.submit(self.archive, plan=plan)
        self.assertNotIn("private_error", str(caught.exception))
        self.assertIn("HTTP 500", str(caught.exception))
        self.assertFalse(any(method == "DELETE" for method, _path, _data in self.calls))
        self.assertFalse(any(path.endswith("/pulls") for _method, path, _data in self.calls))

    def test_http_and_scopes_fail_without_secret_or_response_leaks_and_close_responses(self):
        for status, scopes in ((302, "public_repo"), (403, "public_repo"), (200, "repo"), (200, "")):
            with self.subTest(status=status, scopes=scopes):
                response = self.response({"secret": "fixture_token"}, status, scopes)
                service = GitHubSubmissionService(
                    preflight=self.preflight, publication=self.publication,
                    request=Mock(return_value=response),
                )
                with self.assertRaises(PackageError) as caught:
                    service._api("GET", "/user", "fixture_token")
                self.assertNotIn("fixture_token", str(caught.exception))
                response.close.assert_called_once_with()
        service = GitHubSubmissionService(
            preflight=self.preflight, publication=self.publication,
            request=Mock(side_effect=requests.ConnectionError("fixture_token")),
        )
        with self.assertRaises(PackageError) as caught:
            service._api("GET", "/user", "fixture_token")
        self.assertNotIn("fixture_token", str(caught.exception))

    def test_wrong_blob_or_branch_or_pull_request_identity_is_never_reported_as_success(self):
        plan = self.prepare()
        original = self.respond
        for invalid in ("blob", "branch", "pull"):
            with self.subTest(invalid=invalid):
                def changed(method, url, **kwargs):
                    response = original(method, url, **kwargs)
                    if invalid == "blob" and url.endswith("/git/blobs"):
                        return self.response({"sha": "0" * 40}, 201)
                    if invalid == "branch" and method == "PATCH":
                        return self.response({"object": {"sha": "0" * 40}})
                    if invalid == "pull" and url.endswith("/pulls"):
                        return self.response({
                            "number": 7, "head": {"sha": "0" * 40}, "base": {"repo": {"id": 10}},
                        }, 201)
                    return response
                self.transport.side_effect = changed
                with self.assertRaisesRegex(PackageError, "does not match"):
                    self.service.submit(self.archive, plan=plan)
        self.transport.side_effect = original

    def test_catalog_change_after_upload_prevents_pull_request_creation(self):
        plan = self.prepare()
        original = self.respond
        def changed(method, url, **kwargs):
            response = original(method, url, **kwargs)
            if method == "PATCH":
                self.base = "e" * 40
            return response
        self.transport.side_effect = changed
        self.calls.clear()
        with self.assertRaisesRegex(PackageError, "catalog changed during upload"):
            self.service.submit(self.archive, plan=plan)
        self.assertFalse(any(path.endswith("/pulls") for _method, path, _data in self.calls))

    def test_response_size_time_invalid_json_and_shape_fail_with_response_cleanup(self):
        for payload in (b"not-json", b"[]", b"x" * (2 * 1024 * 1024 + 1)):
            with self.subTest(size=len(payload)):
                response = self.response({})
                response.iter_content.return_value = [payload]
                self.transport.side_effect = None
                self.transport.return_value = response
                with self.assertRaises(PackageError):
                    self.service._api("GET", "/user", "fixture_token")
                response.close.assert_called_once_with()
        response = self.response({})
        self.transport.return_value = response
        with patch("services.github_submission_service.monotonic", side_effect=[0, 46]):
            with self.assertRaisesRegex(PackageError, "size/time"):
                self.service._api("GET", "/user", "fixture_token")
        response.close.assert_called_once_with()

    def wait(self, dialog):
        loop = QEventLoop()
        timer, deadline = QTimer(), QTimer()
        timer.timeout.connect(lambda: loop.quit() if dialog.worker is None else None)
        timer.start(5)
        deadline.setSingleShot(True)
        deadline.timeout.connect(loop.quit)
        deadline.start(3000)
        try:
            loop.exec()
        finally:
            timer.stop()
            deadline.stop()
        self.assertIsNone(dialog.worker)

    def test_ui_prepares_then_defaults_no_and_only_yes_sends_in_worker(self):
        with patch("ui.widgets.managed_packages_dialog.get_logger", return_value=Mock()):
            dialog = ManagedPackagesDialog(service=self.managed)
        self.addCleanup(dialog.deleteLater)
        dialog._previewed(self.managed.preview(self.archive))
        dialog._controls()
        threads = []
        submit = self.service.submit
        def send(*args, **kwargs):
            threads.append(get_ident())
            return submit(*args, **kwargs)
        with (
            patch("ui.widgets.managed_packages_dialog.GitHubSubmissionService", return_value=self.service),
            patch.object(self.service, "submit", side_effect=send) as sent,
        ):
            dialog.submission_send.click()
            self.wait(dialog)
            self.assertIsNotNone(dialog.confirmation)
            self.assertEqual(QMessageBox.No, dialog.confirmation.standardButton(dialog.confirmation.defaultButton()))
            self.assertIn(self.managed._digest(self.archive), dialog.confirmation.text())
            dialog.confirmation.done(QMessageBox.No)
            sent.assert_not_called()
            self.assertTrue(all(method == "GET" for method, _path, _data in self.calls))
            dialog.submission_send.click()
            self.wait(dialog)
            dialog.confirmation.done(QMessageBox.Yes)
            self.wait(dialog)
        self.assertEqual(1, len(threads))
        self.assertNotIn(get_ident(), threads)
        self.assertIn("/pull/7", dialog.status.text())
        self.assertIsNone(dialog.confirmation)
        self.assertEqual([], self.managed.list_installations())


if __name__ == "__main__":
    result = unittest.main(exit=False).result
    print("TEST RESULT: SUCCESS" if result.wasSuccessful() else "TEST RESULT: FAILURE")
    raise SystemExit(0 if result.wasSuccessful() else 1)
