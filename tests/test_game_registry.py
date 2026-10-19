import base64
from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import get_ident
import time
import os
import unittest
from unittest.mock import Mock, patch

from models.github_account import GitHubAccount
from services.file_io import atomic_write_json, read_json
from services.game_registry_service import GameRegistryService, rebind_game_json
from services.package_service import PackageError

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtWidgets import QApplication
from ui.views.game_setup_view import GameSetupView


GAME = {
    "id": "whiteout_survival", "name": "Whiteout Survival",
    "android_packages": ["com.gof.global"],
}


class GameRegistryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.file = self.root / "games" / "games.json"
        atomic_write_json(self.file, [])
        self.account = GitHubAccount("123", "tester")
        self.auth = Mock(restore=Mock(return_value=self.account))
        self.submission = Mock()
        self.submission.publication.credentials.load.return_value = "test_token"
        self.submission.publication._account.return_value = self.account
        self.submission._api.return_value = {"number": 12}
        self.service = GameRegistryService(
            catalog=Mock(), submission=self.submission, auth=self.auth,
        )

    def test_load_reads_central_identity_not_name_or_package_hash(self):
        self.service.catalog.REPOSITORY = "pernerch/botstudio-catalog"
        self.service.catalog._json.return_value = {
            "encoding": "base64", "size": 100,
            "content": base64.b64encode(json.dumps({
                "schema_version": 1, "games": [GAME],
            }).encode()).decode(),
        }
        self.assertEqual([GAME], self.service.load())
        self.assertIn("game-registry.json", self.service.catalog._json.call_args.args[0])

    def test_registry_rejects_ambiguous_or_unsafe_entries(self):
        for games in (
            [GAME, dict(GAME, id="another")],
            [GAME, dict(GAME, id="another", name="Another")],
            [dict(GAME, id="../escape")],
            [dict(GAME, name="")],
            [dict(GAME, android_packages=["invalid"])],
            [dict(GAME, android_packages=["com.gof.global", "com.gof.global"])],
        ):
            with self.subTest(games=games), self.assertRaises(PackageError):
                self.service.validate({"schema_version": 1, "games": games})

    def test_reuse_by_unicode_name_or_package_and_reject_cross_game_conflict(self):
        second = {"id": "different", "name": "Different", "android_packages": ["com.other.game"]}
        self.assertEqual(GAME, self.service.find([GAME], name=" Ｗｈｉｔｅｏｕｔ   SURVIVAL "))
        self.assertEqual(GAME, self.service.find([GAME], package="com.gof.global"))
        with self.assertRaisesRegex(PackageError, "different registered"):
            self.service.find([GAME, second], name=GAME["name"], package="com.other.game")
        with patch.object(self.service, "load", return_value=[GAME]):
            self.assertEqual(GAME, self.service.register("Whiteout Survival", "com.gof.global"))
        self.submission._api.assert_not_called()

    def test_registration_waits_for_server_assigned_identity_without_write_access(self):
        with patch.object(self.service, "load", side_effect=[[], [GAME]]):
            self.assertEqual(GAME, self.service.register("Whiteout Survival", "com.gof.global"))
        method, path, token, request = self.submission._api.call_args.args
        self.assertEqual("POST", method)
        self.assertTrue(path.endswith("/issues"))
        self.assertEqual("test_token", token)
        self.assertEqual({
            "schema_version": 1, "name": GAME["name"],
            "android_package_name": "com.gof.global",
        }, json.loads(request["body"]))
        self.assertNotIn("game_id", request["body"])

    def test_registration_requires_same_authorized_account_and_surfaces_pending(self):
        with patch.object(self.service, "load", return_value=[]):
            self.auth.restore.return_value = GitHubAccount("456", "other")
            with self.assertRaisesRegex(PackageError, "accounts must match"):
                self.service.register("Whiteout Survival", "com.gof.global")
            self.submission._api.assert_not_called()
            self.auth.restore.return_value = self.account
            with patch.object(self.service, "REGISTRATION_SECONDS", 0):
                with self.assertRaisesRegex(PackageError, "#12.*pending"):
                    self.service.register("Whiteout Survival", "com.gof.global")
        self.assertEqual([], read_json(self.file))

    def test_adoption_keeps_old_ids_settings_and_all_existing_content(self):
        original = {
            "id": "my-old-id", "display_name": "Whiteout Survival",
            "package_name": "com.gof.global", "is_default": True,
            "config": {"profile_switch_function_id": "original-function"},
            "settings": {"option": 7},
        }
        atomic_write_json(self.file, [original])
        content = self.root / "data" / "functions" / "my-old-id" / "function.json"
        atomic_write_json(content, {"game_id": "my-old-id"})
        before = content.read_bytes()
        adopted = self.service.adopt_existing(self.file, [GAME])
        self.assertEqual([dict(
            original, catalog_game_id=GAME["id"], catalog_game_name=GAME["name"],
        )], adopted)
        self.assertEqual(before, content.read_bytes())
        self.assertEqual(adopted[0], self.service.ensure_local(self.file, GAME))
        self.assertEqual(1, len(read_json(self.file)))

    def test_download_can_create_missing_game_and_does_not_overwrite_existing_settings(self):
        created = self.service.ensure_local(self.file, GAME)
        self.assertEqual(GAME["id"], created["id"])
        self.assertEqual("com.gof.global", created["package_name"])
        self.assertTrue(created["is_default"])
        created["config"] = {"original": True}
        created["package_name"] = "com.custom.variant"
        atomic_write_json(self.file, [created])
        renamed = dict(GAME, name="New display name", android_packages=["com.updated.game"])
        updated = self.service.ensure_local(self.file, renamed)
        self.assertEqual(dict(created, catalog_game_name="New display name"), updated)
        self.assertEqual([updated], read_json(self.file))

    def test_same_name_different_package_is_not_silently_merged(self):
        legacy = {
            "id": "regional-version", "display_name": GAME["name"],
            "package_name": "com.example.regional", "settings": {"keep": True},
        }
        atomic_write_json(self.file, [legacy])
        self.assertEqual([legacy], self.service.adopt_existing(self.file, [GAME]))
        with patch.object(self.service, "load", return_value=[GAME]):
            with self.assertRaisesRegex(PackageError, "distinct edition"):
                self.service.register(GAME["name"], "com.example.regional")
        self.submission._api.assert_not_called()
        selected = self.service.ensure_local(
            self.file, GAME, local_game_id="regional-version",
        )
        self.assertEqual("regional-version", selected["id"])
        self.assertEqual("whiteout_survival", selected["catalog_game_id"])
        self.assertEqual({"keep": True}, selected["settings"])
        self.assertEqual(1, len(read_json(self.file)))

    def test_duplicate_local_games_fail_without_partial_adoption(self):
        games = [
            {"id": "old-a", "display_name": GAME["name"]},
            {"id": "old-b", "package_name": "com.gof.global"},
        ]
        atomic_write_json(self.file, games)
        before = self.file.read_bytes()
        with self.assertRaisesRegex(PackageError, "Several local Games"):
            self.service.adopt_existing(self.file, [GAME])
        self.assertEqual(before, self.file.read_bytes())

    def test_rebind_only_changes_technical_fields_never_provenance_or_user_text(self):
        payload = {
            "game_id": "old", "name": "old", "text": "old",
            "tasks": [{"game_id": "old"}], "game": "old",
            "package_origins": [{"game_id": "old"}],
        }
        original = deepcopy(payload)
        result = rebind_game_json(payload, "old", "canonical")
        self.assertEqual("canonical", result["game_id"])
        self.assertEqual("old", result["tasks"][0]["game_id"])
        self.assertEqual("old", result["game"])
        self.assertEqual("old", result["name"])
        self.assertEqual([{"game_id": "old"}], result["package_origins"])
        self.assertEqual(original, payload)

    def test_game_setup_load_and_save_are_worker_side_and_have_no_manual_identity(self):
        threads = []
        games = Mock(file_name=self.file)
        games.load.side_effect = lambda: read_json(self.file)

        def load():
            threads.append(get_ident())
            time.sleep(0.03)
            return [GAME]

        with patch("ui.views.game_setup_view.GameService", return_value=games):
            view = GameSetupView()
        self.addCleanup(view.deleteLater)
        view.registry = self.service
        beats = []
        with patch.object(self.service, "load", side_effect=load):
            view.load_directory()
            self.assertIsNotNone(view._worker)
            timer = QTimer()
            timer.timeout.connect(lambda: beats.append(True))
            timer.start(2)
            self.wait_worker(view)
            self.assertTrue(view.game_id.isReadOnly())
            self.assertGreater(len(beats), 0)
            view.catalog_games.setCurrentIndex(1)
            view.save_game()
            self.wait_worker(view)
            timer.stop()
        self.assertTrue(all(thread != get_ident() for thread in threads))
        self.assertEqual(GAME["id"], read_json(self.file)[0]["id"])
        games.update_package.assert_called_once_with(GAME["id"], "com.gof.global")

    def wait_worker(self, view):
        deadline = time.monotonic() + 5
        while view._worker is not None and time.monotonic() < deadline:
            loop = QEventLoop()
            QTimer.singleShot(5, loop.quit)
            loop.exec()
        self.assertIsNone(view._worker)


if __name__ == "__main__":
    unittest.main()
