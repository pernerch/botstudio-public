import base64
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock
import zipfile

from models.bot_function import BotFunction, FunctionPackageOrigin, HelperDependency
from models.github_account import GitHubAccount
from scheduler.models import Event, Task
from services.package_license_service import (
    PackageLicenseError,
    PackageLicenseService,
    PackagePreparationService,
)
from services.package_service import PackageError, PackageService


class MemoryConfigService:
    def __init__(self):
        self.value = {"unrelated": "preserved"}

    def load(self):
        return dict(self.value)

    def save(self, config):
        self.value = dict(config)


class PackageLicenseServiceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.config = MemoryConfigService()
        self.license_service = PackageLicenseService(
            config_service=self.config,
            text_fetcher=lambda spdx_id: f"Official text for {spdx_id}",
        )
        self.account = GitHubAccount("120588088", "pernerch")

    def package_service(self, function_root):
        game_service = Mock()
        game_service.get_game.return_value = {}
        service = PackageService(
            function_root=str(function_root),
            library_service=Mock(root=self.root / "library"),
            variable_service=Mock(root=self.root / "variables"),
            game_service=game_service,
        )
        service.preparation_service = PackagePreparationService(
            function_root=str(function_root),
            license_service=self.license_service,
        )
        return service

    def test_license_preference_is_account_scoped_and_preserves_other_config(self):
        self.license_service.set_license(self.account.account_id, "Apache-2.0")
        self.license_service.set_license("98765", "MIT")

        self.assertEqual("preserved", self.config.value["unrelated"])
        self.assertEqual(
            "Apache-2.0",
            self.license_service.get_license(self.account.account_id).spdx_id,
        )
        self.assertEqual("MIT", self.license_service.get_license("98765").spdx_id)
        self.assertIsNone(self.license_service.get_license("11111"))

    def test_unknown_license_is_rejected_without_changing_preference(self):
        self.license_service.set_license(self.account.account_id, "MIT")

        with self.assertRaises(PackageLicenseError):
            self.license_service.set_license(self.account.account_id, "LicenseRef-custom")

        self.assertEqual("MIT", self.license_service.get_license(self.account.account_id).spdx_id)

    def test_license_link_must_be_the_matching_official_spdx_page(self):
        invalid = {
            "package_id": "test.package",
            "package_version": "1.0.1",
            "author": "Author",
            "license_id": "MIT",
            "license_text": "License terms",
            "license_url": "https://example.com/license",
        }

        with self.assertRaisesRegex(PackageError, "official SPDX"):
            PackageService._validate_package_metadata(invalid)

    def test_function_metadata_is_stable_and_never_confirms_publication_rights(self):
        self.license_service.set_license(self.account.account_id, "MPL-2.0")
        metadata = self.license_service.metadata(
            self.account, "function", "game", "function-id", 7,
        )

        self.assertEqual(
            self.license_service.metadata(self.account, "function", "game", "function-id", 7),
            metadata,
        )
        self.assertEqual("1.0.7", metadata["package_version"])
        self.assertEqual("MPL-2.0", metadata["license_id"])
        self.assertEqual("Official text for MPL-2.0", metadata["license_text"])
        self.assertEqual("https://spdx.org/licenses/MPL-2.0.html", metadata["license_url"])
        self.assertFalse(metadata["rights_confirmed"])
        self.assertEqual(self.account.to_dict(), metadata["publisher"])

    def test_function_export_uses_saved_creator_and_account_license(self):
        self.license_service.set_license(self.account.account_id, "MIT")
        function = BotFunction(
            function_id="example",
            name="Example",
            game_id="game",
            revision=4,
            saved=True,
            creator_account=self.account,
        )
        function_root = self.root / "functions"
        source = function_root / "game" / "example.json"
        source.parent.mkdir(parents=True)
        source.write_text(json.dumps(function.to_dict()), encoding="utf-8")
        package_service = self.package_service(function_root)

        archive = self.root / "example.botstudio.zip"
        package_service.export_function(function, str(archive))
        manifest = package_service.inspect_package(str(archive))
        metadata = manifest["package_metadata"]

        self.assertEqual("function", metadata["package_id"].split("-")[0])
        self.assertEqual("1.0.4", metadata["package_version"])
        self.assertFalse(metadata["rights_confirmed"])
        self.assertIn(metadata["license_url"], package_service.manifest_markdown(manifest))
        manifest["contents"]["assets"] = 1
        self.assertIn(
            "Mixed code and assets: license review required",
            package_service.manifest_markdown(manifest),
        )
        with self.assertRaisesRegex(ValueError, "rights"):
            package_service.github_release_metadata(manifest)

    def test_ready_function_package_stages_root_files_without_creating_zip(self):
        self.license_service.set_license(self.account.account_id, "MIT")
        function = BotFunction(
            function_id="stage-example",
            name="Stage Example",
            game_id="game",
            revision=3,
            saved=True,
            creator_account=self.account,
        )
        function_root = self.root / "stage-functions"
        source = function_root / "game" / "stage-example.json"
        source.parent.mkdir(parents=True)
        source.write_text(json.dumps(function.to_dict()), encoding="utf-8")
        package_service = self.package_service(function_root)
        preview = package_service.preview_function_package(function)
        metadata = dict(preview["manifest"]["package_metadata"])
        with self.assertRaisesRegex(PackageError, "rights confirmation"):
            package_service.prepare_function_directory(
                function,
                str(self.root / "not-confirmed"),
                package_metadata=metadata,
                expected_checksums=preview["checksums"],
            )

        metadata["rights_confirmed"] = True
        output = self.root / "ready" / metadata["package_id"]
        package_service.prepare_function_directory(
            function,
            str(output),
            package_metadata=metadata,
            expected_checksums=preview["checksums"],
        )

        self.assertTrue((output / "manifest.json").is_file())
        self.assertTrue((output / "checksums.json").is_file())
        self.assertTrue(
            (output / "content" / "functions" / "game" / "stage-example.json").is_file()
        )
        self.assertFalse(any(output.rglob("*.zip")))
        manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
        checksums = json.loads((output / "checksums.json").read_text(encoding="utf-8"))
        self.assertTrue(manifest["package_metadata"]["rights_confirmed"])
        self.assertEqual(preview["checksums"], checksums)
        self.assertEqual(checksums, manifest["checksums"])
        self.assertEqual(
            "Stage Example",
            package_service.github_release_metadata(manifest)["title"],
        )

    def test_ready_package_rejects_content_changed_since_preview_and_existing_folder(self):
        self.license_service.set_license(self.account.account_id, "MIT")
        function = BotFunction(
            function_id="mutable",
            name="Mutable",
            game_id="game",
            revision=1,
            saved=True,
            creator_account=self.account,
        )
        function_root = self.root / "mutable-functions"
        source = function_root / "game" / "mutable.json"
        source.parent.mkdir(parents=True)
        source.write_text(json.dumps(function.to_dict()), encoding="utf-8")
        package_service = self.package_service(function_root)
        preview = package_service.preview_function_package(function)
        metadata = dict(preview["manifest"]["package_metadata"])
        metadata["rights_confirmed"] = True
        source.write_text(json.dumps(function.to_dict() | {"description": "changed"}), encoding="utf-8")

        with self.assertRaisesRegex(PackageError, "changed after preview"):
            package_service.prepare_function_directory(
                function,
                str(self.root / "changed"),
                package_metadata=metadata,
                expected_checksums=preview["checksums"],
            )
        occupied = self.root / "occupied"
        occupied.mkdir()
        with self.assertRaisesRegex(PackageError, "already exists"):
            package_service.prepare_function_directory(
                function,
                str(occupied),
                package_metadata=metadata,
                expected_checksums=package_service.preview_function_package(function)["checksums"],
            )

    def test_ready_zip_is_inspected_and_refuses_changed_content_or_overwrite(self):
        self.license_service.set_license(self.account.account_id, "MIT")
        function = BotFunction(
            function_id="archive-example",
            name="Archive example",
            game_id="game",
            revision=1,
            saved=True,
            creator_account=self.account,
        )
        function_root = self.root / "archive-functions"
        game_root = function_root / "game"
        game_root.mkdir(parents=True)
        (game_root / "archive-example.json").write_text(
            json.dumps(function.to_dict()),
            encoding="utf-8",
        )
        service = self.package_service(function_root)
        preview = service.preview_function_package(function)
        metadata = dict(preview["manifest"]["package_metadata"])
        metadata["rights_confirmed"] = True
        ready = service.prepare_function_directory(
            function,
            str(self.root / "ready"),
            package_metadata=metadata,
            expected_checksums=preview["checksums"],
        )
        archive = self.root / "archive.botstudio.zip"

        created = service.create_ready_archive(
            str(ready),
            str(archive),
            expected_checksums=preview["checksums"],
        )

        self.assertEqual(archive, created)
        self.assertEqual(
            preview["checksums"],
            service.inspect_package(str(archive))["checksums"],
        )
        with self.assertRaisesRegex(PackageError, "already exists"):
            service.create_ready_archive(
                str(ready),
                str(archive),
                expected_checksums=preview["checksums"],
            )
        self.assertEqual(
            preview["checksums"],
            service.inspect_package(str(archive))["checksums"],
        )
        changed_ready = self.root / "changed-ready"
        service.prepare_function_directory(
            function,
            str(changed_ready),
            package_metadata=metadata,
            expected_checksums=preview["checksums"],
        )
        packaged_function = next((changed_ready / "content").rglob("*.json"))
        packaged_function.write_text("{}", encoding="utf-8")
        rejected_archive = self.root / "changed.botstudio.zip"
        with self.assertRaisesRegex(PackageError, "does not match"):
            service.create_ready_archive(
                str(changed_ready),
                str(rejected_archive),
                expected_checksums=preview["checksums"],
            )
        self.assertFalse(rejected_archive.exists())

    def test_helper_package_uses_helper_type_and_identity(self):
        self.license_service.set_license(self.account.account_id, "MIT")
        helper = BotFunction(
            function_id="helper-logic",
            name="Helper logic",
            game_id="game",
            revision=2,
            saved=True,
            creator_account=self.account,
            kind="helper",
        )
        function_root = self.root / "helper-functions"
        game_root = function_root / "game"
        game_root.mkdir(parents=True)
        (game_root / "helper-logic.json").write_text(
            json.dumps(helper.to_dict()),
            encoding="utf-8",
        )
        service = self.package_service(function_root)

        preview = service.preview_function_package(helper)

        self.assertEqual("helper", preview["manifest"]["package_type"])
        self.assertTrue(
            preview["manifest"]["package_metadata"]["package_id"].startswith("helper-")
        )

    def test_markdown_image_attachments_are_previewed_and_archived_with_checksums(self):
        self.license_service.set_license(self.account.account_id, "MIT")
        function = BotFunction(
            function_id="with-image",
            name="With image",
            game_id="game",
            saved=True,
            creator_account=self.account,
        )
        function_root = self.root / "image-functions"
        game_root = function_root / "game"
        game_root.mkdir(parents=True)
        (game_root / "with-image.json").write_text(
            json.dumps(function.to_dict()),
            encoding="utf-8",
        )
        image = self.root / "screen.png"
        image_bytes = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+j9XcAAAAASUVORK5CYII="
        )
        image.write_bytes(image_bytes)
        service = self.package_service(function_root)
        markdown = "## How to use\n\n![Screenshot](content/package-assets/image-01.png)"
        package_assets = {"content/package-assets/image-01.png": image}

        preview = service.preview_function_package(
            function,
            description=markdown,
            package_assets=package_assets,
        )
        metadata = dict(preview["manifest"]["package_metadata"])
        metadata["rights_confirmed"] = True
        ready = service.prepare_function_directory(
            function,
            str(self.root / "ready-with-image"),
            description=markdown,
            package_metadata=metadata,
            expected_checksums=preview["checksums"],
            package_assets=package_assets,
        )
        archive = service.create_ready_archive(
            str(ready),
            str(self.root / "with-image.botstudio.zip"),
            expected_checksums=preview["checksums"],
        )

        self.assertEqual(markdown, preview["manifest"]["description"])
        self.assertEqual(
            image_bytes,
            zipfile.ZipFile(archive).read("content/package-assets/image-01.png"),
        )
        self.assertIn("content/package-assets/image-01.png", preview["checksums"])
        self.assertEqual(
            preview["checksums"],
            service.inspect_package(str(archive))["checksums"],
        )

    def test_function_preview_rejects_unresolved_helper_dependencies(self):
        function = BotFunction(
            function_id="root",
            name="Root",
            game_id="game",
            saved=True,
            creator_account=self.account,
            helper_dependencies=[HelperDependency("missing-helper")],
        )
        package_service = self.package_service(self.root / "unresolved-functions")

        with self.assertRaisesRegex(PackageError, "missing Function dependency"):
            package_service.preview_function_package(function)

    def test_event_export_uses_highest_revision_of_same_account_functions(self):
        self.license_service.set_license(self.account.account_id, "Apache-2.0")
        function_root = self.root / "functions"
        game_root = function_root / "game"
        game_root.mkdir(parents=True)
        functions = [
            BotFunction(
                function_id="first", name="First", game_id="game", revision=2,
                saved=True, creator_account=self.account,
            ),
            BotFunction(
                function_id="second", name="Second", game_id="game", revision=8,
                saved=True, creator_account=self.account,
            ),
        ]
        for function in functions:
            (game_root / f"{function.function_id}.json").write_text(
                json.dumps(function.to_dict()),
                encoding="utf-8",
            )
        event = Event(
            game_id="game",
            event_id="event-id",
            name="Example event",
            tasks=[Task(function_ids=["first", "second"])],
        )
        package_service = self.package_service(function_root)
        archive = self.root / "event.botstudio.zip"

        package_service.export_event(event, str(archive))
        metadata = package_service.inspect_package(str(archive))["package_metadata"]

        self.assertEqual("1.0.8", metadata["package_version"])
        self.assertEqual("Apache-2.0", metadata["license_id"])
        self.assertFalse(metadata["rights_confirmed"])

    def test_event_preview_excludes_profile_and_runtime_state(self):
        self.license_service.set_license(self.account.account_id, "MIT")
        function_root = self.root / "event-functions"
        game_root = function_root / "game"
        game_root.mkdir(parents=True)
        function = BotFunction(
            function_id="event-function",
            name="Event function",
            game_id="game",
            saved=True,
            creator_account=self.account,
        )
        (game_root / "event-function.json").write_text(
            json.dumps(function.to_dict()),
            encoding="utf-8",
        )
        variables = self.root / "variables" / "game"
        (variables / "profiles").mkdir(parents=True)
        (variables / "profiles" / "private-profile.json").write_text(
            '{"secret":"profile-only"}',
            encoding="utf-8",
        )
        package_service = PackageService(
            function_root=str(function_root),
            library_service=Mock(root=self.root / "library"),
            variable_service=Mock(root=self.root / "variables"),
            game_service=Mock(get_game=Mock(return_value={})),
        )
        package_service.preparation_service = PackagePreparationService(
            function_root=str(function_root),
            license_service=self.license_service,
        )
        event = Event(
            event_id="event",
            game_id="game",
            profile_id="private-profile",
            name="Event",
            tasks=[
                Task(
                    function_ids=["event-function"],
                    last_started_at="2026-01-01T00:00:00+00:00",
                    last_finished_at="2026-01-01T00:01:00+00:00",
                    duration_samples_seconds=[60.0],
                    status="running",
                )
            ],
            last_started_at="2026-01-01T00:00:00+00:00",
            last_finished_at="2026-01-01T00:01:00+00:00",
            duration_samples_seconds=[60.0],
            last_executed=["event-function"],
            next_due_at="2026-01-02T00:00:00+00:00",
            status="running",
        )

        image = self.root / "event-screenshot.png"
        image_bytes = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+j9XcAAAAASUVORK5CYII="
        )
        image.write_bytes(image_bytes)
        package_assets = {"content/package-assets/image-01.png": image}
        preview = package_service.preview_event_package(
            event,
            profile_id="",
            package_assets=package_assets,
        )
        metadata = dict(preview["manifest"]["package_metadata"])
        metadata["rights_confirmed"] = True
        output = self.root / "ready-event"
        ready = package_service.prepare_event_directory(
            event,
            str(output),
            profile_id="",
            package_metadata=metadata,
            expected_checksums=preview["checksums"],
            package_assets=package_assets,
        )
        archive = package_service.create_ready_archive(
            str(ready),
            str(self.root / "event-ready.botstudio.zip"),
            expected_checksums=preview["checksums"],
        )
        archived_manifest = package_service.inspect_package(str(archive))
        self.assertEqual("event", archived_manifest["package_type"])
        self.assertTrue(archived_manifest["package_metadata"]["rights_confirmed"])
        self.assertNotIn(
            "content/variables/game/profiles/private-profile.json",
            archived_manifest["checksums"],
        )
        with zipfile.ZipFile(archive) as zipped:
            archived_event = json.loads(zipped.read("content/scheduler/game/event.json"))
            self.assertEqual(
                image_bytes,
                zipped.read("content/package-assets/image-01.png"),
            )
        self.assertIsNone(archived_event["next_due_at"])
        event_data = json.loads(
            (output / "content" / "scheduler" / "game" / "event.json")
            .read_text(encoding="utf-8")
        )
        self.assertNotIn(
            "content/variables/game/profiles/private-profile.json",
            preview["files"],
        )
        self.assertEqual("1.0.1", preview["manifest"]["package_metadata"]["package_version"])
        self.assertTrue(any(name.startswith("content/scheduler/game/") for name in preview["files"]))
        self.assertEqual("", event_data["profile_id"])
        self.assertIsNone(event_data["last_started_at"])
        self.assertIsNone(event_data["last_finished_at"])
        self.assertEqual([], event_data["duration_samples_seconds"])
        self.assertEqual([], event_data["last_executed"])
        self.assertIsNone(event_data["next_due_at"])
        self.assertEqual("idle", event_data["status"])
        self.assertIsNone(event_data["tasks"][0]["last_started_at"])
        self.assertIsNone(event_data["tasks"][0]["last_finished_at"])
        self.assertEqual([], event_data["tasks"][0]["duration_samples_seconds"])
        self.assertEqual("idle", event_data["tasks"][0]["status"])

    def test_function_export_does_not_attribute_a_dependency_owned_by_another_account(self):
        self.license_service.set_license(self.account.account_id, "MIT")
        other = GitHubAccount("98765", "other")
        function_root = self.root / "functions"
        game_root = function_root / "game"
        game_root.mkdir(parents=True)
        root_function = BotFunction(
            function_id="root", name="Root", game_id="game", saved=True,
            creator_account=self.account,
            helper_dependencies=[HelperDependency("helper")],
        )
        helper = BotFunction(
            function_id="helper", name="Helper", game_id="game", saved=True,
            creator_account=other,
        )
        for function in (root_function, helper):
            (game_root / f"{function.function_id}.json").write_text(
                json.dumps(function.to_dict()),
                encoding="utf-8",
            )
        package_service = self.package_service(function_root)
        archive = self.root / "mixed-creators.botstudio.zip"

        package_service.export_function(root_function, str(archive))

        self.assertNotIn(
            "package_metadata",
            package_service.inspect_package(str(archive)),
        )

    def test_event_metadata_requires_one_attributed_account_and_uses_highest_revision(self):
        self.license_service.set_license(self.account.account_id, "CC-BY-4.0")
        event = Event(game_id="game", event_id="event-id", name="Event", tasks=[])
        lower = BotFunction(
            function_id="lower", name="Lower", game_id="game", revision=3, saved=True,
            creator_account=self.account,
        )
        higher = BotFunction(
            function_id="higher", name="Higher", game_id="game", revision=9, saved=True,
            creator_account=self.account,
        )
        prep = PackagePreparationService(
            function_root=str(self.root / "functions"),
            license_service=self.license_service,
        )

        metadata = prep.event_metadata(event, [lower, higher])
        self.assertEqual("1.0.9", metadata["package_version"])
        self.assertEqual("CC-BY-4.0", metadata["license_id"])
        self.assertIsNone(prep.event_metadata(event, [lower, BotFunction(
            function_id="legacy", name="Legacy", game_id="game", saved=True,
        )]))
        other = GitHubAccount("98765", "another-user")
        self.assertIsNone(prep.event_metadata(event, [
            lower,
            BotFunction(
                function_id="other", name="Other", game_id="game", saved=True,
                creator_account=other,
            ),
        ]))
        self.assertIsNone(prep.function_metadata(lower, [
            lower,
            BotFunction(
                function_id="other-helper", name="Other helper", game_id="game",
                saved=True, creator_account=other,
            ),
        ]))

    def test_legacy_attribution_updates_only_selected_functions_and_preserves_revision(self):
        function_root = self.root / "functions"
        game_root = function_root / "game"
        game_root.mkdir(parents=True)
        legacy = BotFunction(
            function_id="legacy", name="Legacy", game_id="game",
            revision=6, author="Old author", saved=True,
        )
        already_attributed = BotFunction(
            function_id="owned", name="Owned", game_id="game", saved=True,
            creator_account=GitHubAccount("98765", "other"),
        )
        imported = BotFunction(
            function_id="imported", name="Imported", game_id="game", saved=True,
            package_origins=[FunctionPackageOrigin(
                publisher_key="github:98765",
                package_id="other.package",
                package_version="1.0.0",
                game_id="game",
                function_id="imported",
                revision=1,
                source_sha256="a" * 64,
            )],
        )
        for function in (legacy, already_attributed, imported):
            (game_root / f"{function.function_id}.json").write_text(
                json.dumps(function.to_dict()),
                encoding="utf-8",
            )
        prep = PackagePreparationService(function_root=str(function_root))

        candidates = prep.list_unattributed_functions()
        self.assertEqual(
            ["legacy"],
            [function.function_id for function in candidates],
        )
        changed = prep.attribute_functions([("game", "legacy")], self.account)

        saved = json.loads((game_root / "legacy.json").read_text(encoding="utf-8"))
        untouched = json.loads((game_root / "owned.json").read_text(encoding="utf-8"))
        imported_after = json.loads((game_root / "imported.json").read_text(encoding="utf-8"))
        self.assertEqual(1, changed)
        self.assertEqual("Old author", saved["author"])
        self.assertEqual(6, saved["revision"])
        self.assertEqual(self.account.to_dict(), saved["creator_account"])
        self.assertEqual(already_attributed.creator_account.to_dict(), untouched["creator_account"])
        self.assertIsNone(imported_after["creator_account"])
        self.assertTrue(imported_after["package_origins"])

    def test_legacy_attribution_refuses_to_overwrite_an_existing_creator(self):
        function_root = self.root / "functions"
        game_root = function_root / "game"
        game_root.mkdir(parents=True)
        function = BotFunction(
            function_id="owned", name="Owned", game_id="game", saved=True,
            creator_account=GitHubAccount("98765", "other"),
        )
        path = game_root / "owned.json"
        path.write_text(json.dumps(function.to_dict()), encoding="utf-8")
        prep = PackagePreparationService(function_root=str(function_root))

        with self.assertRaisesRegex(PackageLicenseError, "already has a creator account"):
            prep.attribute_functions([("game", "owned")], self.account)

        self.assertEqual(
            function.creator_account.to_dict(),
            json.loads(path.read_text(encoding="utf-8"))["creator_account"],
        )

    def test_empty_revision_and_invalid_identity_are_rejected(self):
        with self.assertRaises(PackageLicenseError):
            self.license_service.metadata(self.account, "function", "game", "id", 0)
        with self.assertRaises(PackageLicenseError):
            self.license_service.metadata(self.account, "other", "game", "id", 1)
        with self.assertRaises(PackageLicenseError):
            self.license_service.metadata(self.account, "function", "../game", "id", 1)
