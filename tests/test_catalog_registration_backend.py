"""Offline registry acceptance tests; execute directly for a bounded test run."""

import base64
from copy import deepcopy
import faulthandler
import json
import os
import threading
from time import monotonic
import unittest
from unittest.mock import patch
import uuid


from release.catalog import catalog_automation as catalog

ROOT = "/repositories/123"
REGISTRY = ROOT + "/contents/game-registry.json"
ISSUE = ROOT + "/issues/7"


def game(game_id="whiteout_survival", name="Whiteout Survival", package="com.example.whiteout"):
    return {"id": game_id, "name": name, "android_packages": [package]}


def registry_file(games, sha="a" * 40):
    raw = json.dumps({"schema_version": 1, "games": games}).encode("utf-8")
    return {"type": "file", "encoding": "base64", "content": base64.b64encode(raw).decode("ascii"), "size": len(raw), "sha": sha}


class FakeGitHub:
    def __init__(self, *, name="New Game", package="com.example.new", games=None):
        self.issue = {
            "title": catalog.GAME_ISSUE_PREFIX + name, "state": "open",
            "user": {"type": "User", "id": 42},
            "body": json.dumps({"schema_version": 1, "name": name, "android_package_name": package}),
        }
        self.games = deepcopy(games if games is not None else [game()])
        self.sha = "a" * 40
        self.calls = []
        self.comments = []
        self.puts = []
        self.on_put = None
        self.branches = {}
        self.prs = {}
        self.pr_comments = []
        self.on_merge = None

    def request(self, method, path, data=None, **kwargs):
        self.calls.append((method, path, deepcopy(data), kwargs))
        if method == "GET" and path == ROOT:
            return {"id": 123, "full_name": catalog.REPOSITORY, "private": False, "default_branch": "main"}
        if method == "GET" and path == ISSUE:
            return deepcopy(self.issue)
        if method == "GET" and path == REGISTRY:
            return registry_file(self.games, self.sha)
        if method == "GET" and path == ROOT + "/git/ref/heads/main":
            return {"object": {"type": "commit", "sha": "d" * 40}}
        if method == "POST" and path == ROOT + "/git/refs":
            branch = data["ref"].removeprefix("refs/heads/")
            self.branches[branch] = {"sha": self.sha, "games": deepcopy(self.games)}
            return {}
        if method == "PUT" and path == REGISTRY:
            self.puts.append(deepcopy(data))
            if self.on_put:
                self.on_put(self, data)
            if data["sha"] != self.sha:
                raise catalog.GitHubError(409)
            assert data["branch"] != "main"
            self.branches[data["branch"]]["games"] = json.loads(base64.b64decode(data["content"]))["games"]
            return {"content": {"sha": "b" * 40}, "commit": {"sha": "e" * 40}}
        if method == "POST" and path == ROOT + "/pulls":
            number = 100 + len(self.prs)
            self.prs[number] = {
                "number": number, "state": "open", "changed_files": 1,
                "user": {"type": "Bot", "id": 41898282, "login": "github-actions[bot]"},
                "head": {"ref": data["head"], "sha": "e" * 40, "repo": {"id": 123}},
                "base": {"ref": "main", "repo": {"id": 123}},
            }
            return deepcopy(self.prs[number])
        if path.startswith(ROOT + "/pulls/"):
            number = int(path.removeprefix(ROOT + "/pulls/").split("/")[0])
            pr = self.prs[number]
            if method == "GET" and path.endswith("/files?per_page=2"):
                return [{"filename": "game-registry.json", "status": "modified"}]
            if method == "GET":
                return deepcopy(pr)
            if method == "PATCH":
                pr.update(data)
                return deepcopy(pr)
            if method == "PUT" and path.endswith("/merge"):
                if self.on_merge:
                    self.on_merge(self, data)
                assert data["sha"] == pr["head"]["sha"]
                self.games = deepcopy(self.branches[pr["head"]["ref"]]["games"])
                self.sha = "b" * 40
                pr["state"] = "closed"
                pr["merged"] = True
                return {"merged": True, "sha": "f" * 40}
        if method == "POST" and path.startswith(ROOT + "/issues/") and path != ISSUE + "/comments":
            self.pr_comments.append((path, data))
            return {}
        if method == "POST" and path == ISSUE + "/comments":
            self.comments.append(json.loads(data["body"]))
            return {}
        if method == "PATCH" and path == ISSUE:
            self.issue["state"] = data["state"]
            return deepcopy(self.issue)
        raise AssertionError(f"Unexpected mock request: {method} {path}")


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.started = monotonic()
        faulthandler.dump_traceback_later(15, exit=True)
        self.addCleanup(faulthandler.cancel_dump_traceback_later)
        print(f"Test Start: {self.id()}", flush=True)

    def tearDown(self):
        print(f"Test End: {self.id()} ({monotonic() - self.started:.3f}s)", flush=True)

    def register(self, api):
        return catalog.register_game(api, 7, 123)

    def test_normalization_nfkc_casefold_and_whitespace(self):
        self.assertEqual(catalog.normalized_game_name("  Ｓtraße\u00a0  Survival "), "strasse survival")
        self.assertEqual(catalog.normalized_game_name("Cafe\u0301"), catalog.normalized_game_name("CAFÉ"))

    def test_server_generates_uuid_and_persists_identity(self):
        api = FakeGitHub(name=" Ｎew   Game ")
        result = self.register(api)
        self.assertEqual(uuid.UUID(result["game_id"]).version, 4)
        self.assertEqual(result["name"], "New Game")
        self.assertEqual(api.puts[0]["sha"], "a" * 40)
        self.assertTrue(api.puts[0]["branch"].startswith(catalog.REGISTRY_BRANCH_PREFIX + "7-"))
        self.assertTrue(api.prs[100]["merged"])
        self.assertEqual(api.comments, [result])
        self.assertEqual(api.issue["state"], "closed")
        api.issue["state"] = "open"
        again = self.register(api)
        self.assertEqual(again, result)
        self.assertEqual(len(api.puts), 1)
        self.assertEqual(api.games[0]["id"], "whiteout_survival")

    def test_normalized_name_with_unknown_package_rejects_without_aliasing(self):
        api = FakeGitHub(name="  WHITEOUT\u00a0 Survival ", package="com.example.other")
        with self.assertRaisesRegex(ValueError, "distinct edition name"):
            self.register(api)
        self.assertEqual(api.puts, [])
        self.assertEqual(api.games[0]["android_packages"], ["com.example.whiteout"])
        self.assertIn("distinct edition name", api.comments[0]["error"])

    def test_package_match_returns_existing_name_without_rename(self):
        api = FakeGitHub(name="Alternate display", package="com.example.whiteout")
        result = self.register(api)
        self.assertEqual(result, {"schema_version": 1, "game_id": "whiteout_survival", "name": "Whiteout Survival"})
        self.assertEqual(api.puts, [])

    def test_conflicting_name_and_package_rejected_without_merging(self):
        api = FakeGitHub(name="Whiteout Survival", package="com.example.second", games=[
            game(), game("second", "Second", "com.example.second"),
        ])
        with self.assertRaisesRegex(ValueError, "different registered Games"):
            self.register(api)
        self.assertEqual(api.puts, [])
        self.assertEqual(len(api.comments), 1)
        self.assertIn("error", api.comments[0])
        self.assertEqual(api.issue["state"], "open")

    def test_conflict_rereads_registry_and_uses_winning_uuid(self):
        api = FakeGitHub()
        winner = str(uuid.uuid4())

        def concurrent_write(client, data):
            client.games.append(game(winner, "New Game", "com.example.new"))
            client.sha = "c" * 40
            client.on_put = None

        api.on_put = concurrent_write
        result = self.register(api)
        self.assertEqual(result["game_id"], winner)
        self.assertEqual(len(api.puts), 1)
        self.assertEqual(sum(item["name"] == "New Game" for item in api.games), 1)
        self.assertGreaterEqual(sum(method == "GET" and path == REGISTRY for method, path, _, _ in api.calls), 2)

    def test_conflict_with_unrelated_write_preserves_both_games(self):
        api = FakeGitHub()

        def concurrent_write(client, data):
            client.games.append(game("other", "Other", "com.example.other"))
            client.sha = "c" * 40
            client.on_put = None

        api.on_put = concurrent_write
        self.register(api)
        self.assertEqual(len(api.puts), 2)
        self.assertEqual(api.puts[1]["sha"], "c" * 40)
        self.assertEqual({item["name"] for item in api.games}, {"Whiteout Survival", "Other", "New Game"})

    def test_race_introducing_conflicting_identities_fails_closed(self):
        api = FakeGitHub()

        def concurrent_write(client, data):
            client.games.extend([
                game("first", "New Game", "com.example.first"),
                game("second", "Second", "com.example.new"),
            ])
            client.sha = "c" * 40
            client.on_put = None

        api.on_put = concurrent_write
        with self.assertRaisesRegex(ValueError, "different registered Games"):
            self.register(api)
        self.assertEqual(len(api.puts), 1)
        self.assertIn("error", api.comments[-1])

    def test_conflict_retry_is_bounded(self):
        for status in (409, 422):
            with self.subTest(status=status):
                api = FakeGitHub()

                def conflict(client, data):
                    raise catalog.GitHubError(status)

                api.on_put = conflict
                with self.assertRaises(catalog.GitHubError):
                    self.register(api)
                self.assertEqual(len(api.puts), catalog.REGISTRY_ATTEMPTS)
                self.assertIn("error", api.comments[-1])
                self.assertEqual(api.issue["state"], "open")

    def test_nonconflict_http_failure_is_not_retried(self):
        api = FakeGitHub()

        def forbidden(client, data):
            raise catalog.GitHubError(403)

        api.on_put = forbidden
        with self.assertRaises(catalog.GitHubError):
            self.register(api)
        self.assertEqual(len(api.puts), 1)

    def test_merge_race_closes_superseded_pr_then_reuses_winning_id(self):
        api = FakeGitHub()
        winner = str(uuid.uuid4())

        def conflict(client, data):
            client.games.append(game(winner, "New Game", "com.example.new"))
            client.sha = "c" * 40
            client.on_merge = None
            raise catalog.GitHubError(409)

        api.on_merge = conflict
        result = self.register(api)
        self.assertEqual(result["game_id"], winner)
        self.assertEqual(len(api.puts), 1)
        self.assertEqual(api.prs[100]["state"], "closed")
        self.assertFalse(api.prs[100].get("merged", False))
        self.assertEqual(len(api.pr_comments), 1)
        self.assertEqual(len(api.games), 2)

    def test_merge_failures_are_bounded_and_leave_diagnostic_closed_prs(self):
        for status in (403, 405, 409):
            with self.subTest(status=status):
                api = FakeGitHub()

                def fail(client, data):
                    raise catalog.GitHubError(status)

                api.on_merge = fail
                with self.assertRaises(catalog.GitHubError):
                    self.register(api)
                count = 1 if status == 403 else catalog.REGISTRY_ATTEMPTS
                self.assertEqual(len(api.prs), count)
                self.assertEqual(len(api.pr_comments), count)
                self.assertTrue(all(pr["state"] == "closed" for pr in api.prs.values()))
                self.assertEqual(api.comments[-1]["registry_pull_requests"], list(api.prs))
                self.assertEqual(api.issue["state"], "open")
                self.assertEqual(api.games, [game()])

    def test_stale_registry_before_merge_retries_without_overwriting(self):
        api = FakeGitHub()
        request = api.request
        changed = False

        def stale(method, path, data=None, **kwargs):
            nonlocal changed
            result = request(method, path, data, **kwargs)
            if not changed and method == "POST" and path == ROOT + "/pulls":
                changed = True
                api.games.append(game("other", "Other", "com.example.other"))
                api.sha = "c" * 40
            return result

        api.request = stale
        self.register(api)
        self.assertEqual(len(api.puts), 2)
        self.assertEqual(api.prs[100]["state"], "closed")
        self.assertFalse(api.prs[100].get("merged", False))
        self.assertTrue(api.prs[101]["merged"])
        self.assertEqual({item["name"] for item in api.games}, {"Whiteout Survival", "Other", "New Game"})

    def test_registry_pr_skip_requires_exact_bot_repository_branch_and_only_path(self):
        api = FakeGitHub()
        self.register(api)
        self.assertTrue(catalog.trusted_registry_pr(api, 100, 123))
        for change in (
            {"user": {"type": "User", "login": "github-actions[bot]", "id": 41898282}},
            {"user": {"type": "Bot", "login": "other[bot]", "id": 41898282}},
            {"user": {"type": "Bot", "login": "github-actions[bot]", "id": 123}},
            {"head": {"ref": "contributor", "repo": {"id": 123}}},
            {"head": {"ref": api.prs[100]["head"]["ref"], "repo": {"id": 999}}},
            {"changed_files": 2},
        ):
            with self.subTest(change=change):
                previous = deepcopy(api.prs[100])
                api.prs[100].update(change)
                self.assertFalse(catalog.trusted_registry_pr(api, 100, 123))
                api.prs[100] = previous
        request = api.request

        def extra_file(method, path, data=None, **kwargs):
            if path.endswith("/files?per_page=2"):
                return [{"filename": "game-registry.json", "status": "modified"},
                        {"filename": "automation/catalog_automation.py", "status": "modified"}]
            return request(method, path, data, **kwargs)

        api.request = extra_file
        self.assertFalse(catalog.trusted_registry_pr(api, 100, 123))

    def test_changed_registry_pr_head_is_never_merged(self):
        api = FakeGitHub()
        request = api.request

        def changed_head(method, path, data=None, **kwargs):
            result = request(method, path, data, **kwargs)
            if method == "GET" and path == ROOT + "/pulls/100":
                result["head"]["sha"] = "9" * 40
            return result

        api.request = changed_head
        with self.assertRaisesRegex(ValueError, "identity/files changed"):
            self.register(api)
        self.assertFalse(any(method == "PUT" and path.endswith("/merge") for method, path, _, _ in api.calls))
        self.assertEqual(api.prs[100]["state"], "closed")
        self.assertEqual(api.games, [game()])

    def test_cli_skips_only_verified_registry_pr_in_both_package_modes(self):
        api = FakeGitHub()
        self.register(api)
        for mode in ("validate", "publish"):
            with self.subTest(mode=mode):
                with patch.dict(os.environ, {
                    "GITHUB_REPOSITORY": catalog.REPOSITORY,
                    "GITHUB_REPOSITORY_ID": "123", "GH_TOKEN": "fixture",
                }), patch("sys.argv", ["catalog_automation.py", mode, "--pr", "100"]), \
                        patch.object(catalog, "GitHub", return_value=api), \
                        patch.object(catalog, "validate_pr") as validate, \
                        patch.object(catalog, "publish") as publish:
                    catalog.main()
                    validate.assert_not_called()
                    publish.assert_not_called()

    def test_partial_comment_failure_reuses_committed_id_on_retry(self):
        api = FakeGitHub()
        request = api.request

        def comment_failure(method, path, data=None, **kwargs):
            if method == "POST" and path == ISSUE + "/comments" and "game_id" in data["body"]:
                raise catalog.GitHubError(503)
            return request(method, path, data, **kwargs)

        api.request = comment_failure
        with self.assertRaises(catalog.GitHubError):
            self.register(api)
        self.assertEqual(len(api.puts), 1)
        self.assertEqual(api.issue["state"], "open")
        self.assertIn("error", api.comments[0])
        committed_id = api.games[-1]["id"]
        api.request = request
        self.assertEqual(self.register(api)["game_id"], committed_id)
        self.assertEqual(len(api.puts), 1)

    def test_missing_registry_fails_closed_without_creating_seed(self):
        api = FakeGitHub()
        request = api.request

        def missing_registry(method, path, data=None, **kwargs):
            if method == "GET" and path == REGISTRY:
                raise catalog.GitHubError(404)
            return request(method, path, data, **kwargs)

        api.request = missing_registry
        with self.assertRaises(catalog.GitHubError):
            self.register(api)
        self.assertEqual(api.puts, [])
        self.assertIn("error", api.comments[0])

    def test_malformed_requests_are_commented_without_registry_access(self):
        bodies = [
            None, "", "not JSON", "[]", '{"schema_version":1,"schema_version":1}',
            "[" * 1100 + "]" * 1100,
            json.dumps({"schema_version": True, "name": "New Game", "android_package_name": "com.example.new"}),
            json.dumps({"schema_version": 1, "name": "New Game", "android_package_name": "com.example.new", "id": "chosen"}),
            json.dumps({"schema_version": 1, "name": ["New Game"], "android_package_name": "com.example.new"}),
            json.dumps({"schema_version": 1, "name": " \u00a0 ", "android_package_name": "com.example.new"}),
            json.dumps({"schema_version": 1, "name": "x" * 121, "android_package_name": "com.example.new"}),
            json.dumps({"schema_version": 1, "name": "New\nGame", "android_package_name": "com.example.new"}),
            json.dumps({"schema_version": 1, "name": "New\u202eGame", "android_package_name": "com.example.new"}),
            json.dumps({"schema_version": 1, "name": "New Game", "android_package_name": "invalid/package"}),
            json.dumps({"schema_version": 1, "name": "New Game", "android_package_name": ["com.example.new"]}),
            " " * 4097,
        ]
        for body in bodies:
            with self.subTest(body=repr(body)[:80]):
                api = FakeGitHub()
                api.issue["body"] = body
                with self.assertRaises(ValueError):
                    self.register(api)
                self.assertEqual(api.puts, [])
                self.assertEqual(len(api.comments), 1)
                self.assertIn("error", api.comments[0])
                self.assertFalse(any(path == REGISTRY for _, path, _, _ in api.calls))

    def test_invalid_issue_authentication_and_title_are_rejected(self):
        for change in (
            {"user": {"id": 42, "type": "Bot"}},
            {"user": {"id": True, "type": "User"}},
            {"pull_request": {}},
            {"title": catalog.GAME_ISSUE_PREFIX + "Different Name"},
            {"state": "closed"},
        ):
            with self.subTest(change=change):
                api = FakeGitHub()
                api.issue.update(change)
                with self.assertRaises(ValueError):
                    self.register(api)
                self.assertIn("error", api.comments[0])
                self.assertEqual(api.puts, [])

    def test_unrelated_issue_is_ignored_after_api_read(self):
        api = FakeGitHub()
        api.issue["title"] = "An ordinary bug"
        self.assertIsNone(self.register(api))
        self.assertEqual([(method, path) for method, path, _, _ in api.calls], [("GET", ISSUE)])

    def test_shell_metacharacters_remain_data_only(self):
        api = FakeGitHub(name="$(echo harmless); `plain`")
        with patch.object(catalog.uuid, "uuid4", return_value=uuid.UUID("bdf257e0-cb2c-46b2-83f8-f7a61e4b0999")):
            result = self.register(api)
        self.assertEqual(result["name"], "$(echo harmless); `plain`")
        self.assertEqual(api.puts[0]["message"], "Register Game from issue #7")
        self.assertEqual(api.comments[0]["name"], result["name"])

    def test_invalid_registry_never_authorizes_or_overwrites(self):
        cases = [
            [game(), game()],
            [game(), game("WHITEOUT_SURVIVAL", "Other", "com.example.other")],
            [game(), game("second", "Other", "com.example.whiteout")],
            [game(), game("second", "ＷHITEOUT SURVIVAL", "com.example.other")],
            [{"id": "whiteout_survival", "name": "Whiteout Survival", "android_packages": "com.example.whiteout"}],
        ]
        for games in cases:
            with self.subTest(games=games):
                api = FakeGitHub(games=games)
                with self.assertRaises(ValueError):
                    self.register(api)
                self.assertEqual(api.puts, [])
                self.assertIn("error", api.comments[0])

    def test_registry_size_encoding_and_schema_are_bounded(self):
        for fields in (
            {"size": catalog.MAX_REGISTRY + 1},
            {"content": "!invalid-base64!"},
            {"sha": "../untrusted"},
            {"type": "symlink"},
        ):
            with self.subTest(fields=fields):
                with patch.object(FakeGitHub, "request", return_value={**registry_file([game()]), **fields}):
                    with self.assertRaises(ValueError):
                        catalog.read_registry(FakeGitHub(), 123)
        with self.assertRaises(ValueError):
            catalog.validate_registry({"schema_version": True, "games": []})

    def test_pr_validation_requires_live_registered_game_not_head_registry(self):
        prefix = f"games/{'0' * 64}/publishers/42/packages/{'1' * 64}/1.0.0/"
        metadata, archive = prefix + "submission.json", prefix + "package.botstudio.zip"
        head, tree = "d" * 40, "e" * 40
        pr = {
            "state": "open", "draft": False, "base": {"repo": {"id": 123}, "ref": "main"},
            "user": {"id": 42, "type": "User"}, "head": {"sha": head}, "changed_files": 2,
        }
        for game_id, accepted in (("whiteout_survival", True), ("client-invented", False)):
            with self.subTest(game_id=game_id):
                api = FakeGitHub()
                base_request = api.request

                def request(method, path, data=None, **kwargs):
                    if path == ROOT + "/pulls/8":
                        return deepcopy(pr)
                    if path == ROOT + "/pulls/8/files?per_page=3":
                        return [{"filename": name, "status": "added"} for name in (metadata, archive)]
                    if path == ROOT + f"/git/commits/{head}":
                        return {"tree": {"sha": tree}}
                    if path == ROOT + f"/git/trees/{tree}?recursive=1":
                        return {"truncated": False, "tree": [
                            {"path": name, "mode": "100644", "type": "blob", "sha": sha}
                            for name, sha in ((metadata, "metadata-blob"), (archive, "archive-blob"))
                        ]}
                    if path.endswith("/git/blobs/metadata-blob"):
                        return b'{"game_id":"ignored-by-mocked-archive-validator"}'
                    if path.endswith("/git/blobs/archive-blob"):
                        return b"mock archive"
                    if path in (ROOT + "/contents/" + metadata, ROOT + "/contents/" + archive):
                        return None
                    return base_request(method, path, data, **kwargs)

                api.request = request
                with patch.object(catalog, "validate_package", return_value={"game_id": game_id}):
                    if accepted:
                        catalog.validate_pr(api, 8, 123)
                    else:
                        with self.assertRaisesRegex(ValueError, "unregistered Game ID"):
                            catalog.validate_pr(api, 8, 123)
                self.assertTrue(any(path == REGISTRY and kwargs == {} for _, path, _, kwargs in api.calls))


if __name__ == "__main__":
    def timeout():
        print("TEST RESULT: TIMEOUT", flush=True)
        os._exit(124)

    watchdog = threading.Timer(30, timeout)
    watchdog.daemon = True
    watchdog.start()
    try:
        result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(RegistryTests))
        print("TEST RESULT: SUCCESS" if result.wasSuccessful() else "TEST RESULT: FAILURE", flush=True)
    finally:
        watchdog.cancel()
    raise SystemExit(0 if result.wasSuccessful() else 1)
