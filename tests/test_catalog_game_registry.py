import base64
import faulthandler
import hashlib
import json
import time
import unittest
import uuid
from copy import deepcopy
from unittest.mock import Mock, patch

from release.catalog import catalog_automation as automation


class CatalogGameRegistryTests(unittest.TestCase):
    def setUp(self):
        started = time.monotonic()
        print(f"TEST START: {self.id()}")
        faulthandler.dump_traceback_later(15, exit=True)
        self.addCleanup(faulthandler.cancel_dump_traceback_later)
        self.addCleanup(
            lambda: print(
                f"TEST END: {self.id()} duration {time.monotonic() - started:.3f}s"
            )
        )
        self.registry = {
            "schema_version": 1,
            "games": [{
                "id": "whiteout_survival",
                "name": "Whiteout Survival",
                "android_packages": ["com.gof.global"],
            }],
        }
        self.issue = {
            "state": "open",
            "title": "[BotStudio Game] Whiteout Survival",
            "body": json.dumps({
                "schema_version": 1,
                "name": "Whiteout Survival",
                "android_package_name": "com.gof.oversea",
            }),
            "user": {"id": 123, "type": "User"},
        }
        self.sha = "a" * 40
        self.calls = []
        self.missing_registry = False
        self.api = Mock(request=Mock(side_effect=self.request))

    def request(self, method, path, data=None, **kwargs):
        self.calls.append((method, path, data, kwargs))
        root = "/repositories/10"
        if method == "GET" and path == root + "/issues/7":
            return deepcopy(self.issue)
        if path == root:
            return {
                "id": 10, "full_name": automation.REPOSITORY,
                "private": False, "default_branch": "main",
            }
        if method == "GET" and path == root + "/contents/game-registry.json":
            if self.missing_registry:
                raise automation.GitHubError(404)
            raw = json.dumps(self.registry, ensure_ascii=False).encode("utf-8")
            return {
                "type": "file", "encoding": "base64",
                "content": base64.b64encode(raw).decode("ascii"),
                "size": len(raw), "sha": self.sha,
            }
        if method == "PUT" and path == root + "/contents/game-registry.json":
            raw = base64.b64decode(data["content"], validate=True)
            self.registry = json.loads(raw)
            self.sha = hashlib.sha1(raw).hexdigest()
            return {"content": {"sha": self.sha}}
        if method == "POST" and path == root + "/issues/7/comments":
            return {"id": 5}
        if method == "PATCH" and path == root + "/issues/7":
            self.issue["state"] = data["state"]
            return deepcopy(self.issue)
        raise AssertionError(f"unexpected request {method} {path}")

    def test_registration_rejects_unknown_edition_without_adding_alias(self):
        with self.assertRaisesRegex(ValueError, "distinct edition name"):
            automation.register_game(self.api, 7, 10)
        self.assertEqual(
            ["com.gof.global"],
            self.registry["games"][0]["android_packages"],
        )
        self.assertEqual("whiteout_survival", self.registry["games"][0]["id"])
        self.assertEqual("open", self.issue["state"])
        writes = [call for call in self.calls if call[0] == "PUT"]
        self.assertEqual([], writes)

    def test_normalized_name_deduplicates_and_exact_package_is_required(self):
        self.issue["body"] = json.dumps({
            "schema_version": 1,
            "name": "  ＷＨＩＴＥＯＵＴ   SURVIVAL ",
            "android_package_name": "com.gof.global",
        })
        self.issue["title"] = "[BotStudio Game] WHITEOUT SURVIVAL"

        result = automation.register_game(self.api, 7, 10)

        self.assertEqual("whiteout_survival", result["game_id"])
        self.assertEqual(["com.gof.global"], self.registry["games"][0]["android_packages"])
        self.assertFalse(any(call[0] == "PUT" for call in self.calls))

    def test_new_game_receives_uuid4_identity(self):
        self.issue.update({
            "title": "[BotStudio Game] New Game",
            "body": json.dumps({
                "schema_version": 1,
                "name": "New Game",
                "android_package_name": "com.example.newgame",
            }),
        })

        def commit_registry(api, number, repository_id, repo, sha, raw, diagnostics):
            self.registry = json.loads(raw)
            self.sha = hashlib.sha1(raw).hexdigest()

        with patch.object(automation, "write_registry_pr", side_effect=commit_registry):
            result = automation.register_game(self.api, 7, 10)

        parsed = uuid.UUID(result["game_id"])
        self.assertEqual(4, parsed.version)
        self.assertEqual("New Game", self.registry["games"][-1]["name"])

    def test_conflicting_name_and_package_fail_closed(self):
        self.registry["games"].append({
            "id": "other_game",
            "name": "Different Game",
            "android_packages": ["com.gof.oversea"],
        })

        with self.assertRaisesRegex(ValueError, "different registered Games"):
            automation.register_game(self.api, 7, 10)

        self.assertFalse(any(call[0] == "PUT" for call in self.calls))
        self.assertEqual("open", self.issue["state"])

    def test_missing_parent_seed_fails_closed_without_creating_registry(self):
        self.missing_registry = True

        with self.assertRaisesRegex(ValueError, "HTTP 404"):
            automation.register_game(self.api, 7, 10)

        self.assertFalse(any(call[0] == "PUT" for call in self.calls))
        self.assertEqual("open", self.issue["state"])

    def test_issue_request_requires_exact_fields_and_valid_android_package(self):
        for body in (
            '{"schema_version":1,"name":"Good","android_package_name":"com.example.good","extra":true}',
            '{"schema_version":1,"schema_version":1,"name":"Good","android_package_name":"com.example.good"}',
            '{"schema_version":1,"name":"Good","android_package_name":"com.example"}',
            '{"schema_version":true,"name":"Good","android_package_name":"com.example.good"}',
        ):
            with self.subTest(body=body):
                self.issue["body"] = body
                with self.assertRaises(ValueError):
                    automation.registration_request(self.issue)


if __name__ == "__main__":
    result = unittest.main(exit=False).result
    print("TEST RESULT: SUCCESS" if result.wasSuccessful() else "TEST RESULT: FAILURE")
    raise SystemExit(0 if result.wasSuccessful() else 1)
