from copy import deepcopy
import faulthandler
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import time
import unittest
from unittest.mock import Mock, patch
import urllib.error
from zipfile import ZipFile

from release.catalog import catalog_automation as automation


class CatalogAutomationTests(unittest.TestCase):
    def setUp(self):
        started = time.monotonic()
        print(f"TEST START: {self.id()}")
        faulthandler.dump_traceback_later(15, exit=True)
        self.addCleanup(faulthandler.cancel_dump_traceback_later)
        self.addCleanup(lambda: print(f"TEST END: {self.id()} duration {time.monotonic() - started:.3f}s"))
        identity = {"provider": "github", "account_id": "123", "login": "author"}
        self.payload = b'{"function_id":"example","game_id":"game"}'
        self.content_path = "content/functions/game/example.json"
        self.checksums = {self.content_path: hashlib.sha256(self.payload).hexdigest()}
        self.manifest = {
            "format": "botstudio-package", "format_version": 1,
            "package_name": "Example", "description": "", "game": {"id": "game"},
            "package_type": "function",
            "contents": {"functions": 1}, "checksums": self.checksums,
            "package_metadata": {
                "publisher": identity, "package_id": "example", "package_version": "1.0.0",
                "author": "Declared Author", "license_id": "MIT",
                "license_text": "Fixture terms", "rights_confirmed": True,
            },
        }
        prefix = (
            f"games/{hashlib.sha256(b'game').hexdigest()}/publishers/123/"
            f"packages/{hashlib.sha256(b'example').hexdigest()}/1.0.0"
        )
        self.descriptor = {
            "schema_version": 1, "repository": automation.REPOSITORY,
            "game_id": "game", "package_id": "example", "package_version": "1.0.0",
            "submitter": identity, "archive_sha256": "", "archive_size": 0,
            "archive_path": f"{prefix}/package.botstudio.zip",
            "metadata_path": f"{prefix}/submission.json", "fork_of": None,
        }
        self.raw = self.archive()
        self.bind(self.raw)
        self.pr = {
            "state": "open", "draft": False, "merged": False, "changed_files": 2,
            "user": {"id": 123, "type": "User"}, "head": {"sha": "a" * 40},
            "base": {"repo": {"id": 10}, "ref": "main"}, "merge_commit_sha": "b" * 40,
        }
        self.calls = []
        self.api = Mock(request=Mock(side_effect=self.request))
        self.changed_head = False
        self.existing = False
        self.bad_asset = False

    def archive(self, *, extra=None, content=None):
        output = BytesIO()
        with ZipFile(output, "w") as archive:
            archive.writestr("manifest.json", json.dumps(self.manifest))
            archive.writestr("checksums.json", json.dumps(self.checksums))
            archive.writestr(self.content_path, self.payload if content is None else content)
            if extra is not None:
                archive.writestr(extra, b"unexpected")
        return output.getvalue()

    def bind(self, raw):
        self.descriptor["archive_size"] = len(raw)
        self.descriptor["archive_sha256"] = hashlib.sha256(raw).hexdigest()

    def request(self, method, path, data=None, **kwargs):
        self.calls.append((method, path, data, kwargs))
        root = "/repositories/10"
        if path == root:
            return {"id": 10, "full_name": automation.REPOSITORY, "private": False, "default_branch": "main"}
        if path == root + "/pulls/7":
            result = deepcopy(self.pr)
            if self.changed_head and sum(p == path for _m, p, _d, _k in self.calls) > 1:
                result["head"]["sha"] = "c" * 40
            return result
        if path.endswith("/files?per_page=3"):
            return [{"filename": self.descriptor[key], "status": "added"} for key in ("archive_path", "metadata_path")]
        if "/git/commits/" in path:
            return {"tree": {"sha": "d" * 40}}
        if "/git/trees/" in path:
            return {"truncated": False, "tree": [
                {"path": self.descriptor[key], "mode": "100644", "type": "blob", "sha": key}
                for key in ("archive_path", "metadata_path")
            ]}
        if "/git/blobs/" in path:
            return self.raw if path.endswith("archive_path") else json.dumps(self.descriptor).encode()
        if "/contents/" in path:
            if "?ref=" in path:
                return {"type": "file", "sha": "archive_path" if "package.botstudio.zip" in path else "metadata_path"}
            return {"type": "file"} if self.existing else None
        if path.endswith("/merge"):
            self.assertEqual({"sha": "a" * 40, "merge_method": "merge", "commit_title": "Accept catalog submission #7"}, data)
            return {"merged": True, "sha": "b" * 40}
        if "/releases/tags/" in path:
            return None
        if "/git/ref/tags/" in path:
            if kwargs.get("missing"):
                return None
            return {"object": {"type": "commit", "sha": "b" * 40}}
        if path == root + "/releases":
            return {"id": 50, "draft": True, "tag_name": data["tag_name"]}
        if path == root + "/git/refs":
            return {"ref": data["ref"], "object": {"type": "commit", "sha": data["sha"]}}
        if "/assets?" in path:
            self.assertEqual(self.raw, data)
            self.assertTrue(kwargs["upload"])
            return {"state": "uploaded", "size": len(data), "digest": "sha256:" + ("0" * 64 if self.bad_asset else self.descriptor["archive_sha256"])}
        if path == root + "/releases/50":
            return {"id": 50, "draft": False}
        raise AssertionError(f"unexpected request {method} {path}")

    def test_valid_archive_and_descriptor_bind_game_publisher_version_and_exact_bytes(self):
        self.assertEqual(self.descriptor, automation.validate_package(self.raw, self.descriptor, 123))
        self.assertEqual(self.pr, automation.validate_pr(self.api, 7, 10)[0])
        self.assertTrue(all(method == "GET" for method, _path, _data, _kwargs in self.calls))

    def test_actor_spoof_paths_metadata_hash_and_schema_are_rejected(self):
        for changes in (
            {"submitter": {**self.descriptor["submitter"], "account_id": "456"}},
            {"archive_path": "../package.zip"}, {"metadata_path": "submission.json"},
            {"archive_sha256": "0" * 64}, {"archive_size": True},
            {"schema_version": True}, {"game_id": "different"},
            {"repository": "attacker/catalog"}, {"fork_of": {}}, {"trusted": True},
        ):
            with self.subTest(changes=changes), self.assertRaises((ValueError, KeyError, TypeError)):
                automation.validate_package(self.raw, {**self.descriptor, **changes}, 123)

    def test_license_rights_publisher_version_and_corrupt_content_fail_real_validation(self):
        original = deepcopy(self.manifest)
        for changes in (
            {"license_id": "UNSPECIFIED"}, {"rights_confirmed": False},
            {"publisher": {**original["package_metadata"]["publisher"], "account_id": "456"}},
            {"package_version": "01.0.0"},
        ):
            with self.subTest(changes=changes):
                self.manifest = deepcopy(original)
                self.manifest["package_metadata"].update(changes)
                raw = self.archive()
                self.bind(raw)
                with self.assertRaises(ValueError):
                    automation.validate_package(raw, self.descriptor, 123)
        self.manifest = original
        raw = self.archive(content=b"corrupt")
        self.bind(raw)
        with self.assertRaisesRegex(ValueError, "checksum"):
            automation.validate_package(raw, self.descriptor, 123)

    def test_archive_paths_extra_code_duplicates_and_limits_are_rejected(self):
        for extra in ("../escape", "script.py", "content/../bad", "content/functions/game/EXAMPLE.json"):
            with self.subTest(extra=extra):
                raw = self.archive(extra=extra)
                self.bind(raw)
                with self.assertRaises(ValueError):
                    automation.validate_package(raw, self.descriptor, 123)
        for limit in ("MAX_ARCHIVE", "MAX_EXPANDED", "MAX_JSON"):
            with self.subTest(limit=limit), patch.object(automation, limit, 1):
                with self.assertRaises(ValueError):
                    automation.validate_package(self.raw, self.descriptor, 123)
        with self.assertRaisesRegex(ValueError, "duplicate JSON"):
            automation.load_json(b'{"x":1,"x":2}')

    def test_pr_changed_head_existing_version_or_extra_files_block_every_write(self):
        for reason in ("head", "existing", "files", "draft", "bot"):
            with self.subTest(reason=reason):
                self.changed_head, self.existing = reason == "head", reason == "existing"
                self.pr["changed_files"] = 3 if reason == "files" else 2
                self.pr["draft"] = reason == "draft"
                self.pr["user"]["type"] = "Bot" if reason == "bot" else "User"
                self.calls.clear()
                with self.assertRaises(ValueError):
                    automation.publish(self.api, 7, 10)
                self.assertTrue(all(method == "GET" for method, _path, _data, _kwargs in self.calls))

    def test_publish_revalidates_merges_exact_sha_checks_merged_bytes_and_draft_digest(self):
        automation.publish(self.api, 7, 10)
        writes = [(method, path) for method, path, _data, _kwargs in self.calls if method != "GET"]
        self.assertEqual(["PUT", "POST", "POST", "POST", "PATCH"], [method for method, _path in writes])
        self.assertTrue(writes[0][1].endswith("/merge"))
        self.assertEqual({"draft": False}, self.calls[-1][2])
        release = next(data for method, path, data, _kwargs in self.calls if method == "POST" and path.endswith("/releases"))
        self.assertEqual("b" * 40, release["target_commitish"])
        self.assertTrue(release["draft"])
        metadata = json.loads(release["body"].split("<!-- botstudio-publication-v1\n")[1].split("\n-->")[0])
        self.assertEqual("123", metadata["submitter"]["account_id"])
        self.assertEqual(self.descriptor, metadata["submission"])
        self.assertEqual("game | Example 1.0.0", release["name"])
        self.assertIn("Download **package.botstudio.zip**", release["body"])

    def test_release_renders_package_description_and_escapes_metadata_comment(self):
        self.manifest["package_name"] = "Journey of Light"
        self.manifest["description"] = "**Journey of Light**\n\nMission notes."
        self.manifest["game"]["name"] = "Whiteout Survival"
        publication = {"submission": self.descriptor, "notice": "--> <script>"}
        title, body = automation.release_presentation(self.archive(), publication)
        self.assertEqual("Whiteout Survival | Journey of Light 1.0.0", title)
        self.assertTrue(body.startswith(self.manifest["description"]))
        self.assertEqual(1, body.count("\n-->"))
        metadata = body.split("<!-- botstudio-publication-v1\n")[1].split("\n-->")[0]
        self.assertEqual({**publication, "display": automation.package_display(self.archive())}, json.loads(metadata))
        self.manifest["description"] = "<!-- botstudio-publication-v1\nspoof"
        with self.assertRaisesRegex(ValueError, "reserved metadata"):
            automation.release_presentation(self.archive(), publication)

    def test_failed_asset_stays_draft_and_merged_pr_can_be_recovered_without_second_merge(self):
        self.bad_asset = True
        with self.assertRaisesRegex(ValueError, "draft retained"):
            automation.publish(self.api, 7, 10)
        self.assertFalse(any(method == "PATCH" for method, _p, _d, _k in self.calls))
        self.bad_asset = False
        self.pr.update(state="closed", merged=True)
        self.calls.clear()
        automation.publish(self.api, 7, 10)
        self.assertFalse(any(method == "PUT" for method, _p, _d, _k in self.calls))

    def test_malformed_tree_symlinks_or_changed_files_never_reach_merge(self):
        original = self.request
        for reason in ("truncated", "symlink", "removed", "unknown"):
            with self.subTest(reason=reason):
                self.calls.clear()
                def changed(method, path, data=None, **kwargs):
                    result = original(method, path, data, **kwargs)
                    if "/git/trees/" in path:
                        if reason == "truncated":
                            result["truncated"] = True
                        if reason == "symlink":
                            result["tree"][0]["mode"] = "120000"
                    if "/files?" in path:
                        if reason == "removed":
                            result[0]["status"] = "removed"
                        if reason == "unknown":
                            result[0]["filename"] = ".github/workflows/attack.yml"
                    return result
                self.api.request.side_effect = changed
                with self.assertRaises(ValueError):
                    automation.publish(self.api, 7, 10)
                self.assertTrue(all(method == "GET" for method, _p, _d, _k in self.calls))

    def test_merge_failure_merged_corruption_existing_release_or_wrong_tag_never_publish(self):
        original = self.request
        for reason in ("merge", "corrupt", "release", "tag", "publish"):
            with self.subTest(reason=reason):
                self.calls.clear()
                def changed(method, path, data=None, **kwargs):
                    result = original(method, path, data, **kwargs)
                    if reason == "merge" and path.endswith("/merge"):
                        return {"merged": False}
                    if reason == "corrupt" and "/git/blobs/archive_path" in path:
                        if any(m == "PUT" for m, _p, _d, _k in self.calls):
                            return b"corrupt"
                    if reason == "release" and "/releases/tags/" in path:
                        return {"id": 99}
                    if reason == "tag" and "/git/ref/tags/" in path and not kwargs.get("missing"):
                        result["object"]["sha"] = "f" * 40
                    if reason == "publish" and method == "PATCH":
                        result["draft"] = True
                    return result
                self.api.request.side_effect = changed
                with self.assertRaises(ValueError):
                    automation.publish(self.api, 7, 10)
                if reason != "publish":
                    self.assertFalse(any(method == "PATCH" for method, _p, _d, _k in self.calls))

    def test_http_redirects_are_disabled_and_errors_do_not_leak_tokens_or_bodies(self):
        api = automation.GitHub("secret_fixture")
        api.opener = Mock()
        error = urllib.error.HTTPError("https://api.github.com", 403, "secret_fixture", {}, BytesIO(b"secret"))
        api.opener.open.side_effect = error
        with self.assertRaisesRegex(ValueError, "HTTP 403") as caught:
            api.request("GET", "/repositories/10")
        self.assertNotIn("secret", str(caught.exception))
        error = urllib.error.HTTPError("https://api.github.com", 404, "missing", {}, BytesIO())
        api.opener.open.side_effect = error
        self.assertIsNone(api.request("GET", "/repositories/10", missing=True))
        self.assertIsNone(automation.NoRedirect().redirect_request(None, None, 302, "", {}, "https://attacker.example"))

    def test_cli_requires_expected_repository_and_positive_pull_request(self):
        with patch.dict(os.environ, {"GITHUB_REPOSITORY": "wrong/repo"}), patch("sys.argv", ["script", "validate", "--pr", "7"]):
            with self.assertRaisesRegex(ValueError, "unexpected"):
                automation.main()

    def test_workflow_uses_only_trusted_checkout_and_separate_read_write_jobs(self):
        workflow = Path(automation.__file__).with_name("catalog-submissions.yml").read_text()
        self.assertIn("pull_request_target:", workflow)
        self.assertEqual(2, workflow.count("ref: ${{ github.event.repository.default_branch }}"))
        self.assertEqual(2, workflow.count("persist-credentials: false"))
        self.assertNotIn("pull_request.head", workflow)
        self.assertNotIn("download-artifact", workflow)
        self.assertIn("contents: read", workflow)
        self.assertIn("contents: write", workflow)
        self.assertIn("cancel-in-progress: false", workflow)
        self.assertIn('python3 -I automation/catalog_automation.py publish --pr "$PR_NUMBER"', workflow)


if __name__ == "__main__":
    result = unittest.main(exit=False).result
    print("TEST RESULT: SUCCESS" if result.wasSuccessful() else "TEST RESULT: FAILURE")
    raise SystemExit(0 if result.wasSuccessful() else 1)
