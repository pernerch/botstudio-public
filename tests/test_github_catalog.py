import base64
from copy import deepcopy
from datetime import datetime, timezone
import faulthandler
import json
import hashlib
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import Mock, patch

import requests

from models.github_account import GitHubAccount, GitHubAssetUploader
from services.github_catalog_service import GitHubCatalogService
from services.package_service import PackageError
from ui.i18n.translator import t


class GitHubCatalogTests(unittest.TestCase):
    def setUp(self):
        started = time.monotonic()
        print(f"TEST START: {self.id()}")
        faulthandler.dump_traceback_later(15, exit=True)
        self.addCleanup(faulthandler.cancel_dump_traceback_later)
        self.addCleanup(lambda: print(f"TEST END: {self.id()} duration {time.monotonic() - started:.3f}s"))
        self.request = Mock()
        self.credentials = Mock(load=Mock(return_value=None))
        self.service = GitHubCatalogService(self.request, credentials=self.credentials)
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.repository = {"id": 1234, "full_name": "pernerch/botstudio-catalog"}
        self.release = {
            "id": 50, "tag_name": "game-v1.0.0", "draft": False, "prerelease": False,
            "assets": [{
                "id": 60, "name": "game.botstudio.zip", "state": "uploaded", "size": 100,
                "uploader": {"id": 70, "login": "maintainer"},
            }],
        }

    @staticmethod
    def response(payload, status=200):
        return Mock(status_code=status, headers={}, iter_content=Mock(return_value=[json.dumps(payload).encode()]))

    def respond(self, releases):
        responses = [self.response(self.repository), self.response(releases)]
        self.request.side_effect = responses
        return responses

    def test_uses_numeric_repository_identity_and_reads_only_package_assets(self):
        release = deepcopy(self.release)
        release["assets"].append({"name": "README.txt"})
        responses = self.respond([release])
        result = self.service.load()
        self.assertEqual(1234, result["repository_id"])
        self.assertEqual(1, len(result["entries"]))
        self.assertEqual("70", result["entries"][0]["uploader"].account_id)
        self.assertEqual("maintainer", result["entries"][0]["uploader"].login)
        self.assertEqual([
            "https://api.github.com/repos/pernerch/botstudio-catalog",
            "https://api.github.com/repositories/1234/releases?per_page=30",
        ], [call.args[0] for call in self.request.call_args_list])
        for call in self.request.call_args_list:
            self.assertEqual((5, 10), call.kwargs["timeout"])
            self.assertFalse(call.kwargs["allow_redirects"])
            self.assertTrue(call.kwargs["stream"])
            self.assertNotIn("Authorization", call.kwargs["headers"])
        for response in responses:
            response.close.assert_called_once_with()

    def test_publication_comparison_reads_pages_and_downloads_only_latest_own_game_channels(self):
        owner = GitHubAccount("123", "owner")

        def entry(asset, version, game="game", publisher=owner):
            return {
                "asset_id": asset, "size": 10, "package": {
                    "package_id": "example", "game_id": game,
                    "publisher": publisher, "version": version,
                },
            }

        latest = entry(2, "1.0.10")
        self.service.load = Mock(side_effect=[
            {"repository": self.service.REPOSITORY, "repository_id": 1, "next_page": 2,
             "entries": [entry(1, "1.0.9"), entry(3, "1.0.20", game="other")]},
            {"repository": self.service.REPOSITORY, "repository_id": 1, "next_page": None,
             "entries": [latest, entry(4, "2.0.0", publisher=GitHubAccount("456", "other"))]},
        ])
        self.service.download_preview = Mock(return_value={
            "manifest": {"package_name": "Example", "package_type": "function"},
        })
        packages = Mock(archive_content_fingerprint=Mock(return_value="fingerprint"))
        result = self.service.published_content("game", owner, packages)
        self.assertEqual({"fingerprint": ("Example", "function")}, result)
        self.assertEqual([{"page": 1}, {"page": 2}], [
            call.kwargs for call in self.service.load.call_args_list
        ])
        self.service.download_preview.assert_called_once()
        self.assertEqual(latest, self.service.download_preview.call_args.args[1])
        self.assertFalse(self.service.download_preview.call_args.args[2].parent.exists())

    def test_publication_comparison_fails_visibly_on_incomplete_catalog_or_download(self):
        owner = GitHubAccount("123", "owner")
        packages = Mock()
        self.service.load = Mock(return_value={
            "repository": self.service.REPOSITORY, "repository_id": 1,
            "next_page": 2, "entries": [],
        })
        with self.assertRaisesRegex(PackageError, "10 catalog pages"):
            self.service.published_content("game", owner, packages)
        self.assertEqual(10, self.service.load.call_count)
        self.service.load = Mock(return_value={
            "repository": self.service.REPOSITORY, "repository_id": 1, "next_page": None,
            "entries": [{"asset_id": 1, "size": 10, "package": {
                "package_id": "example", "game_id": "game", "publisher": owner,
                "version": "1.0.0",
            }}],
        })
        self.service.download_preview = Mock(side_effect=PackageError("SHA-256 does not match"))
        with self.assertRaisesRegex(PackageError, "SHA-256"):
            self.service.published_content("game", owner, packages)
        packages.archive_content_fingerprint.assert_not_called()

    def test_drafts_are_excluded_prereleases_are_explicit_and_empty_is_valid(self):
        draft = {**self.release, "draft": True}
        self.respond([draft, {**self.release, "prerelease": True}])
        result = self.service.load()
        self.assertEqual(1, len(result["entries"]))
        self.assertTrue(result["entries"][0]["prerelease"])
        self.respond([])
        self.assertEqual([], self.service.load()["entries"])

    def test_catalog_api_uses_only_existing_normal_login_and_rereads_credentials(self):
        self.credentials.load.side_effect = ["fixture_normal_token", None]
        self.respond([])
        self.service.load()
        calls = self.request.call_args_list
        self.assertEqual("Bearer fixture_normal_token", calls[0].kwargs["headers"]["Authorization"])
        self.assertNotIn("Authorization", calls[1].kwargs["headers"])
        self.assertEqual(2, self.credentials.load.call_count)
        self.credentials.save.assert_not_called()
        self.credentials.delete.assert_not_called()
        self.assertTrue(all(not call.kwargs["allow_redirects"] for call in calls))

    def test_invalid_or_unreadable_credentials_fail_without_anonymous_fallback(self):
        for token in ("", "bad\r\ntoken", "token with spaces", 123):
            with self.subTest(token=token):
                self.credentials.load.return_value = token
                with self.assertRaisesRegex(PackageError, "Telegram/GitHub"):
                    self.service.load()
                self.request.assert_not_called()
        self.credentials.load.side_effect = OSError("private credential detail")
        with self.assertRaises(PackageError) as error:
            self.service.load()
        self.assertNotIn("private credential detail", str(error.exception))
        self.request.assert_not_called()

    def test_signed_in_http_errors_do_not_retry_or_leak_token(self):
        self.credentials.load.return_value = "private_fixture_token"
        for status in (301, 302, 401, 403, 429):
            with self.subTest(status=status):
                self.request.reset_mock()
                response = self.response({"private": "raw body secret"}, status)
                self.request.return_value = response
                with self.assertRaises(PackageError) as error:
                    self.service.load()
                self.assertNotIn("private_fixture_token", str(error.exception))
                self.assertNotIn("raw body secret", str(error.exception))
                self.request.assert_called_once()
                response.close.assert_called_once_with()
                response.iter_content.assert_not_called()

    def test_rate_limit_reset_is_localized_and_not_confused_with_permission_denial(self):
        from ui.i18n import translator
        self.addCleanup(translator.set_language, translator.LANG)
        reset = 1792158373
        when = datetime.fromtimestamp(reset, timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S %z")
        for language in ("en", "de"):
            with self.subTest(language=language):
                translator.set_language(language)
                response = self.response({}, 403)
                response.headers = {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": str(reset)}
                self.assertEqual(
                    t("packages.catalog_rate_limit_at").format(when=when),
                    str(self.service._http_error(response)),
                )
                response.headers = {}
                self.assertEqual("GitHub catalog HTTP 403", str(self.service._http_error(response)))
                response.status_code = 429
                self.assertEqual(t("packages.catalog_rate_limit"), str(self.service._http_error(response)))
                response.headers = {"Retry-After": "Fri, 16 Oct 2026 13:46:13 GMT"}
                self.assertIn(when, str(self.service._http_error(response)))
                response.headers = {"Retry-After": "60"}
                self.assertNotEqual(t("packages.catalog_rate_limit"), str(self.service._http_error(response)))
                for malformed in ("secret\nbad", "999999999999999999999999", "-1"):
                    response.headers = {"Retry-After": malformed, "X-RateLimit-Reset": malformed}
                    message = str(self.service._http_error(response))
                    self.assertEqual(t("packages.catalog_rate_limit"), message)
                    self.assertNotIn(malformed, message)

    def test_asset_api_uses_sign_in_but_redirect_host_never_receives_credentials(self):
        self.credentials.load.return_value = "fixture_normal_token"
        first = Mock(status_code=302, headers={
            "Location": "https://release-assets.githubusercontent.com/package",
        })
        final = Mock(status_code=200, iter_content=Mock(return_value=[b"package"]))
        self.request.side_effect = [first, final]
        self.service._download(1234, self.download_entry(), self.root / "anonymous-asset")
        self.credentials.load.assert_called_once_with()
        calls = self.request.call_args_list
        self.assertEqual("Bearer fixture_normal_token", calls[0].kwargs["headers"]["Authorization"])
        self.assertNotIn("Authorization", calls[1].kwargs["headers"])

    def test_api_path_guard_precedes_reading_credentials(self):
        for path in ("https://evil.example", "//evil.example", "/user", "/repos/other/catalog", "/repositories/../user"):
            with self.subTest(path=path), self.assertRaises(PackageError):
                self.service._json(path)
        self.credentials.load.assert_not_called()
        self.request.assert_not_called()

    def structured_release(self):
        publisher = {"provider": "github", "account_id": "123", "login": "publisher"}
        prefix = (
            f"games/{hashlib.sha256(b'game').hexdigest()}/publishers/123/"
            f"packages/{hashlib.sha256(b'example').hexdigest()}/1.0.0"
        )
        descriptor = {
            "schema_version": 1, "repository": self.service.REPOSITORY,
            "game_id": "game", "package_id": "example", "package_version": "1.0.0",
            "submitter": publisher, "archive_sha256": "a" * 64, "archive_size": 100,
            "archive_path": prefix + "/package.botstudio.zip",
            "metadata_path": prefix + "/submission.json", "fork_of": None,
        }
        release = deepcopy(self.release)
        release["tag_name"] = "catalog-pr-7-" + "b" * 40
        release["body"] = json.dumps({
            "schema_version": 1, "pull_request": 7, "head": "b" * 40,
            "submitter": publisher, "archive_sha256": "a" * 64,
        })
        release["assets"][0]["digest"] = "sha256:" + "a" * 64
        return release, descriptor

    def respond_structured(self, release, descriptor):
        self.request.side_effect = [
            self.response(self.repository), self.response([release]),
            self.response([
                {"filename": descriptor[key], "status": "added"}
                for key in ("archive_path", "metadata_path")
            ]),
            self.response({
                "type": "file", "encoding": "base64",
                "content": base64.b64encode(json.dumps(descriptor).encode()).decode(),
            }),
        ]

    def test_grouping_uses_descriptor_at_exact_head_not_release_title_or_uploader(self):
        release, descriptor = self.structured_release()
        release["name"] = "Wrong Game | spoofed name"
        self.respond_structured(release, descriptor)
        entry = self.service.load()["entries"][0]
        self.assertEqual({
            "game_id": "game", "package_id": "example", "version": "1.0.0",
            "publisher": GitHubAccount("123", "publisher"),
        }, entry["package"])
        self.assertEqual("70", entry["uploader"].account_id)
        self.assertTrue(self.request.call_args_list[-1].args[0].endswith("?ref=" + "b" * 40))
        self.assertTrue(all(call.kwargs["allow_redirects"] is False for call in self.request.call_args_list))

    def test_bad_publication_descriptor_fails_without_guessing_a_game(self):
        for change in (
            {"archive_size": True}, {"archive_sha256": "c" * 64},
            {"game_id": "other"}, {"package_version": "01.0.0"},
            {"submitter": {"provider": "github", "account_id": "456", "login": "different"}},
            {"metadata_path": "../submission.json"},
        ):
            with self.subTest(change=change):
                release, descriptor = self.structured_release()
                self.respond_structured(release, {**descriptor, **change})
                with self.assertRaises(PackageError):
                    self.service.load()

    def test_embedded_descriptor_needs_only_repository_and_release_list_requests(self):
        release, descriptor = self.structured_release()
        publication = json.loads(release["body"])
        release["body"] = json.dumps({**publication, "submission": descriptor})
        self.respond([release])
        result = self.service.load()
        self.assertEqual("game", result["entries"][0]["package"]["game_id"])
        self.assertEqual(2, self.request.call_count)
        self.assertIsNone(result["next_page"])

    def test_readable_markdown_release_retains_verified_embedded_descriptor(self):
        release, descriptor = self.structured_release()
        publication = {
            **json.loads(release["body"]), "submission": descriptor,
            "display": {
                "package_name": "Journey of Light", "game_name": "Whiteout Survival",
                "package_type": "event", "author": "@publisher",
                "description": "Mission notes", "contents": {"functions": 5},
            },
        }
        release["body"] = (
            "**Journey of Light**\n\nDescription\n\n"
            "<!-- botstudio-publication-v1\n" + json.dumps(publication) + "\n-->"
        )
        self.respond([release])
        result = self.service.load()
        self.assertEqual("game", result["entries"][0]["package"]["game_id"])
        self.assertEqual("Journey of Light", result["entries"][0]["package"]["package_name"])
        self.assertEqual("event", result["entries"][0]["package"]["package_type"])
        self.assertEqual(2, self.request.call_count)

    def test_windows_line_endings_preserve_structured_publication_metadata(self):
        release, descriptor = self.structured_release()
        publication = {**json.loads(release["body"]), "submission": descriptor}
        release["body"] = (
            "Description\r\n\r\n<!-- botstudio-publication-v1\r\n"
            + json.dumps(publication) + "\r\n-->"
        )
        self.respond([release])
        result = self.service.load()
        self.assertEqual(descriptor["package_id"], result["entries"][0]["package"]["package_id"])

    def test_malformed_marked_release_never_falls_back_to_unstructured_catalog(self):
        for metadata in ("not json\n-->", "{}\n-->", "{}",
                         "{}\n-->\n<!-- botstudio-publication-v1\n{}\n-->"):
            with self.subTest(metadata=metadata):
                release, _ = self.structured_release()
                release["body"] = "Description\n<!-- botstudio-publication-v1\n" + metadata
                self.respond([release])
                with self.assertRaises(PackageError):
                    self.service.load()
                self.request.reset_mock()

    def test_invalid_display_metadata_fails_without_unknown_type_fallback(self):
        for changes in ({"package_type": "unknown"}, {"contents": {"functions": True}},
                        {"package_name": ""}, {"author": []}):
            with self.subTest(changes=changes):
                release, descriptor = self.structured_release()
                display = {
                    "package_name": "Journey", "game_name": "Game",
                    "package_type": "event", "author": "@publisher",
                    "description": "", "contents": {"functions": 1}, **changes,
                }
                release["body"] = json.dumps({
                    **json.loads(release["body"]), "submission": descriptor, "display": display,
                })
                self.respond([release])
                with self.assertRaises(PackageError):
                    self.service.load()
                self.request.reset_mock()

    def test_invalid_embedded_descriptor_fails_without_legacy_fallback(self):
        for change in (
            None, [], {"game_id": "spoof"},
        ):
            with self.subTest(change=change):
                release, _descriptor = self.structured_release()
                release["body"] = json.dumps({**json.loads(release["body"]), "submission": change})
                self.respond([release])
                with self.assertRaises(PackageError):
                    self.service.load()
                self.assertEqual(2, self.request.call_count)
                self.request.reset_mock()
        release, descriptor = self.structured_release()
        for changes in ({"game_id": "other"}, {"archive_size": 101}, {"metadata_path": "../x"}):
            with self.subTest(changes=changes):
                broken = {**descriptor, **changes}
                self.respond([{**release, "body": json.dumps({
                    **json.loads(release["body"]), "submission": broken,
                })}])
                with self.assertRaises(PackageError):
                    self.service.load()

    def test_pages_are_explicit_and_not_limited_to_thirty_total_releases(self):
        releases = []
        for number in range(30):
            release = deepcopy(self.release)
            release["id"] = 100 + number
            release["assets"][0]["id"] = 200 + number
            releases.append(release)
        self.respond(releases)
        first = self.service.load()
        self.assertEqual(30, len(first["entries"]))
        self.assertEqual(2, first["next_page"])
        self.request.reset_mock()
        self.respond([self.release])
        second = self.service.load(page=first["next_page"])
        self.assertEqual(31, len(first["entries"]) + len(second["entries"]))
        self.assertIsNone(second["next_page"])
        self.assertTrue(self.request.call_args_list[-1].args[0].endswith("&page=2"))
        for page in (True, 0, -1, "2", 100001):
            with self.subTest(page=page), self.assertRaises(PackageError):
                self.service.load(page=page)

    def test_old_release_recheck_does_not_depend_on_latest_page_membership(self):
        release, descriptor = self.structured_release()
        release["body"] = json.dumps({**json.loads(release["body"]), "submission": descriptor})
        self.respond([release])
        catalog = self.service.load(page=4)
        entry = catalog["entries"][0]
        self.request.reset_mock()
        self.request.side_effect = [self.response(self.repository), self.response(release)]
        self.service._current_entry(catalog, entry)
        self.assertEqual(2, self.request.call_count)
        self.assertTrue(self.request.call_args_list[-1].args[0].endswith("/releases/50"))
        changed = deepcopy(release)
        changed["assets"][0]["size"] += 1
        self.request.side_effect = [self.response(self.repository), self.response(changed)]
        with self.assertRaisesRegex(PackageError, "changed"):
            self.service._current_entry(catalog, entry)

    def test_legacy_release_text_remains_explicitly_ungrouped_without_extra_requests(self):
        for body in ("# Game description", json.dumps({"schema_version": 2}), ""):
            with self.subTest(body=body):
                self.respond([{**self.release, "body": body}])
                self.assertNotIn("package", self.service.load()["entries"][0])
                self.assertEqual(2, len(self.request.call_args_list))
                self.request.reset_mock()

    def test_actions_bot_uploader_survives_catalog_and_source_receipt_validation(self):
        release = deepcopy(self.release)
        release["assets"][0]["uploader"] = {
            "id": 41898282, "login": "github-actions[bot]", "type": "Bot",
        }
        self.respond([release])
        uploader = self.service.load()["entries"][0]["uploader"]
        self.assertIsInstance(uploader, GitHubAssetUploader)
        self.assertEqual("github-actions[bot]", uploader.login)
        self.assertEqual("41898282", uploader.account_id)
        source = {
            "repository": self.service.REPOSITORY, "repository_id": 1234,
            "release_id": 50, "asset_id": 60, "archive_sha256": "a" * 64,
            "uploader": {"account_id": uploader.account_id, "login": uploader.login},
        }
        self.assertEqual(source, self.service.validate_source(source, "a" * 64))
        with self.assertRaises(ValueError):
            GitHubAccount(uploader.account_id, uploader.login)

    def test_malformed_bot_uploaders_fail_for_catalog_and_persisted_receipts(self):
        for login in ("[bot]", "invalid/user[bot]", "bad--name[bot]", "bot[bot][bot]", "bot[BOT]", "bot[bot]\n"):
            with self.subTest(login=login):
                release = deepcopy(self.release)
                release["assets"][0]["uploader"] = {"id": 70, "login": login}
                self.respond([release])
                with self.assertRaises(PackageError):
                    self.service.load()
                source = {
                    "repository": self.service.REPOSITORY, "repository_id": 1234,
                    "release_id": 50, "asset_id": 60, "archive_sha256": "a" * 64,
                    "uploader": {"account_id": "70", "login": login},
                }
                with self.assertRaises(PackageError):
                    self.service.validate_source(source, "a" * 64)

    def test_redirect_private_missing_and_rate_limit_fail_without_response_leaks(self):
        for status in (301, 302, 401, 403, 404, 500):
            with self.subTest(status=status):
                response = self.response({"private": "secret"}, status)
                self.request.side_effect = None
                self.request.return_value = response
                with self.assertRaisesRegex(PackageError, f"HTTP {status}") as error:
                    self.service.load()
                self.assertNotIn("secret", str(error.exception))
                response.iter_content.assert_not_called()
                response.close.assert_called_once_with()

    def test_transport_invalid_json_and_size_limits_are_explicit(self):
        self.request.side_effect = requests.ConnectionError("secret")
        with self.assertRaisesRegex(PackageError, "request failed") as error:
            self.service.load()
        self.assertNotIn("secret", str(error.exception))
        self.request.side_effect = None
        for chunks in ([b"invalid JSON"], [b"x" * (self.service.MAX_RESPONSE_BYTES + 1)]):
            with self.subTest(size=sum(map(len, chunks))):
                response = Mock(status_code=200, iter_content=Mock(return_value=chunks))
                self.request.return_value = response
                with self.assertRaises(PackageError):
                    self.service.load()
                response.close.assert_called_once_with()
        response = self.response(self.repository)
        self.request.return_value = response
        with patch("services.github_catalog_service.monotonic", side_effect=[0, 21]):
            with self.assertRaisesRegex(PackageError, "time budget"):
                self.service.load()
        response.close.assert_called_once_with()

    def test_repository_identity_and_release_shapes_are_validated(self):
        for repository in (
            [], {**self.repository, "id": True},
            {**self.repository, "full_name": "attacker/catalog"},
            {**self.repository, "private": True},
        ):
            with self.subTest(repository=repository):
                self.request.side_effect = [self.response(repository)]
                with self.assertRaises(PackageError):
                    self.service.load()
        invalid = (
            {}, [None], [{**self.release, "draft": "false"}],
            [{**self.release, "id": False}], [{**self.release, "tag_name": "\nbad"}],
            [{**self.release, "prerelease": "false"}], [{**self.release, "assets": None}],
            [{**self.release, "assets": [None]}], [self.release] * 31,
        )
        for releases in invalid:
            with self.subTest(releases=releases):
                self.respond(releases)
                with self.assertRaises(PackageError):
                    self.service.load()

    def test_invalid_or_duplicate_package_assets_fail_instead_of_partial_success(self):
        invalid = (
            {"id": True}, {"size": 0}, {"size": True}, {"state": "new"},
            {"uploader": None}, {"uploader": {"id": 70, "login": "invalid/user"}},
            {"uploader": {"id": False, "login": "user"}},
        )
        for change in invalid:
            with self.subTest(change=change):
                release = deepcopy(self.release)
                release["assets"][0].update(change)
                self.respond([release])
                with self.assertRaises(PackageError):
                    self.service.load()
        self.respond([self.release, self.release])
        with self.assertRaisesRegex(PackageError, "duplicate"):
            self.service.load()

    def download_entry(self, payload=b"package"):
        return {
            "asset_id": 60, "release_id": 50, "size": len(payload),
            "digest": "sha256:" + hashlib.sha256(payload).hexdigest(),
        }

    def test_download_stream_checks_digest_size_and_follows_only_github_asset_hosts(self):
        first = Mock(status_code=302, headers={
            "Location": "https://release-assets.githubusercontent.com/package?signature=secret",
        })
        final = Mock(status_code=200, iter_content=Mock(return_value=[b"pack", b"age"]))
        self.request.side_effect = [first, final]
        path = self.root / "download.zip"
        self.service._download(1234, self.download_entry(), path)
        self.assertEqual(b"package", path.read_bytes())
        for response in (first, final):
            response.close.assert_called_once_with()
        calls = self.request.call_args_list
        self.assertEqual("https://api.github.com/repositories/1234/releases/assets/60", calls[0].args[0])
        for call in calls:
            self.assertFalse(call.kwargs["allow_redirects"])
            self.assertNotIn("Authorization", call.kwargs["headers"])

    def test_download_rejects_untrusted_redirects_and_hides_signed_urls(self):
        for url in (
            "http://release-assets.githubusercontent.com/file",
            "https://release-assets.githubusercontent.com.evil.test/file",
            "https://user:pass@release-assets.githubusercontent.com/file",
            "https://release-assets.githubusercontent.com:444/file",
            "https://release-assets.githubusercontent.com:invalid/file",
            "https://localhost/file", "/relative", "https://example.com/private?secret=token",
        ):
            with self.subTest(url=url):
                response = Mock(status_code=302, headers={"Location": url})
                self.request.side_effect = [response]
                with self.assertRaises(PackageError) as error:
                    self.service._download(1234, self.download_entry(), self.root / "not-written.zip")
                self.assertNotIn(url, str(error.exception))
                self.assertNotIn("token", str(error.exception))
                response.close.assert_called_once_with()
                self.assertFalse((self.root / "not-written.zip").exists())

    def test_download_rejects_corruption_truncation_oversize_and_timeouts(self):
        for index, payload in enumerate((b"bad-data", b"pack", b"package-too-long")):
            with self.subTest(payload=payload):
                response = Mock(status_code=200, iter_content=Mock(return_value=[payload]))
                self.request.side_effect = [response]
                with self.assertRaises(PackageError):
                    self.service._download(1234, self.download_entry(), self.root / f"file-{index}")
                response.close.assert_called_once_with()
        response = Mock(status_code=200, iter_content=Mock(return_value=[b"package"]))
        self.request.side_effect = [response]
        with patch("services.github_catalog_service.monotonic", side_effect=[0, 61]):
            with self.assertRaisesRegex(PackageError, "time budget"):
                self.service._download(1234, self.download_entry(), self.root / "timed-out")
        response.close.assert_called_once_with()

    def test_download_redirect_loops_and_http_errors_never_report_success(self):
        redirects = [Mock(status_code=302, headers={
            "Location": "https://release-assets.githubusercontent.com/file",
        }) for _ in range(3)]
        self.request.side_effect = redirects
        with self.assertRaisesRegex(PackageError, "redirect"):
            self.service._download(1234, self.download_entry(), self.root / "loop")
        for response in redirects:
            response.close.assert_called_once_with()
        for status in (403, 404, 429, 500):
            with self.subTest(status=status):
                response = Mock(status_code=status, headers={})
                self.request.side_effect = [response]
                with self.assertRaises(PackageError):
                    self.service._download(1234, self.download_entry(), self.root / "error")
                response.iter_content.assert_not_called()
                response.close.assert_called_once_with()
        self.assertEqual([], list(self.root.iterdir()))

    def test_download_refuses_missing_digest_existing_target_and_changed_catalog(self):
        entry = self.download_entry()
        catalog = {"repository": self.service.REPOSITORY, "repository_id": 1234, "entries": [entry]}
        path = self.root / "download.zip"
        managed = Mock()
        with self.assertRaisesRegex(PackageError, "SHA-256"):
            self.service.download_preview(
                {**catalog, "entries": [{**entry, "digest": None}]}, {**entry, "digest": None}, path, managed,
            )
        path.write_bytes(b"local")
        with self.assertRaisesRegex(PackageError, "already exists"):
            self.service.download_preview(catalog, entry, path, managed)
        self.assertEqual(b"local", path.read_bytes())
        with patch.object(self.service, "load", return_value={**catalog, "repository_id": 99}):
            with self.assertRaisesRegex(PackageError, "changed"):
                self.service.download_preview(catalog, entry, self.root / "new.zip", managed)
        managed.preview.assert_not_called()
        self.request.assert_not_called()

    def test_download_rejects_archive_that_disagrees_with_displayed_grouping(self):
        entry = {
            **self.download_entry(), "package": {
                "game_id": "chosen-game", "package_id": "example", "version": "1.0.0",
                "publisher": GitHubAccount("123", "publisher"),
            },
        }
        catalog = {"repository": self.service.REPOSITORY, "repository_id": 1234, "entries": [entry]}
        managed = Mock()
        managed.preview.return_value = {
            "archive_sha256": entry["digest"].removeprefix("sha256:"),
            "manifest": {"game": {"id": "different-game"}, "package_metadata": {}},
        }
        target = self.root / "mismatched.zip"
        with (
            patch.object(self.service, "load", return_value=catalog),
            patch.object(self.service, "check_archive_limits"),
            patch.object(self.service, "_download", side_effect=lambda _r, _e, path: path.write_bytes(b"package")),
        ):
            with self.assertRaisesRegex(PackageError, "grouping"):
                self.service.download_preview(catalog, entry, target, managed)
        self.assertFalse(target.exists())
        self.assertEqual([], list(self.root.iterdir()))


if __name__ == "__main__":
    result = unittest.main(exit=False).result
    print("TEST RESULT: SUCCESS" if result.wasSuccessful() else "TEST RESULT: FAILURE")
    raise SystemExit(0 if result.wasSuccessful() else 1)
