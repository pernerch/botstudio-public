from contextlib import contextmanager
import faulthandler
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock

from models.bot_function import BotFunction
from models.github_account import GitHubAccount
from scheduler.models import Event, Task
from services.managed_package_service import ManagedPackageService
from services.catalog_submission_service import CatalogSubmissionService
from services.package_activation_service import PackageActivationService
from services.package_license_service import PackageLicenseService, PackagePreparationService
from services.package_service import PackageError, PackageService


class MemoryConfig:
    def __init__(self):
        self.value = {}

    def load(self):
        return dict(self.value)

    def save(self, value):
        self.value = dict(value)


class PackagePublicationTests(unittest.TestCase):
    def setUp(self):
        faulthandler.dump_traceback_later(30, exit=True)
        self.addCleanup(faulthandler.cancel_dump_traceback_later)
        self.addCleanup(lambda: print(f"TEST END: {self.id()}"))
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.owner = GitHubAccount("123", "owner")
        self.contributor = GitHubAccount("456", "contributor")
        self.licenses = PackageLicenseService(
            config_service=MemoryConfig(), text_fetcher=lambda name: f"Original {name} terms",
        )
        self.licenses.set_license("123", "MIT")
        self.licenses.set_license("456", "Apache-2.0")
        self.a = self.client("a")
        self.b = self.client("b")
        self.function = BotFunction(
            function_id="function", name="Example", game_id="game",
            revision=7, saved=True, creator_account=self.owner,
        )
        self.save(self.a, self.function)

    def client(self, name):
        root = self.root / name
        service = PackageService(
            function_root=str(root / "data" / "functions"),
            library_service=Mock(root=root / "data" / "library"),
            variable_service=Mock(root=root / "data" / "variables"),
            game_service=Mock(get_game=Mock(return_value={})),
        )
        service.preparation_service = PackagePreparationService(
            function_root=str(service.function_root), license_service=self.licenses,
        )
        return service

    @staticmethod
    def save(service, function):
        target = service.function_root / "game" / "function.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(function.to_dict()), encoding="utf-8")

    def archive(self, service, function, account, name, version=None):
        preview = service.preview_function_package(
            function, profile_id="", publication_account=account, package_version=version,
        )
        metadata = dict(preview["manifest"]["package_metadata"], rights_confirmed=True)
        ready = service.prepare_function_directory(
            function, str(self.root / f"{name}-ready"), profile_id="",
            package_metadata=metadata, expected_checksums=preview["checksums"],
        )
        archive = service.create_ready_archive(
            str(ready), str(self.root / f"{name}.botstudio.zip"), expected_checksums=preview["checksums"],
        )
        return archive, preview["manifest"]

    def install_on_b(self, archive):
        root = self.b.function_root.parent.parent
        for path, data in (
            (root / "games" / "games.json", [{"id": "game"}]),
            (root / "profiles" / "profiles.json", []),
        ):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data), encoding="utf-8")
        managed = ManagedPackageService(root / "data" / "packages", self.b)
        status = managed.install(archive)

        @contextmanager
        def guard():
            yield

        activation = PackageActivationService(
            root=root, managed=managed, execution_guard=guard, logger=Mock(),
        )
        preview = activation.prepare(status["installation_id"])
        activation.activate(status["installation_id"], "", preview["plan_sha256"])
        return self.b.load_saved_function("game", "function")

    def test_same_account_roundtrip_preserves_identity_and_original_terms(self):
        archive, original = self.archive(self.a, self.function, self.owner, "a")
        imported = self.install_on_b(archive)
        imported.bump_revision("", account=self.owner)
        imported.description = "Small correction on client B"
        self.save(self.b, imported)
        updated_archive, updated = self.archive(self.b, imported, self.owner, "b")
        old = original["package_metadata"]
        new = updated["package_metadata"]
        self.assertEqual(old["package_id"], new["package_id"])
        self.assertEqual("1.0.8", new["package_version"])
        self.assertEqual(old["license_text"], new["license_text"])
        self.assertNotIn("fork_of", new)
        self.assertEqual(self.owner, self.b.load_saved_function("game", "function").creator_account)
        self.b.inspect_package(str(updated_archive))

    def test_same_account_roundtrip_version_bump_is_not_new_publication_content(self):
        archive, _ = self.archive(self.a, self.function, self.owner, "unchanged-a")
        published = self.a.archive_content_fingerprint(archive)
        imported = self.install_on_b(archive)
        imported.bump_revision("No code changes", account=self.owner)
        self.save(self.b, imported)
        preview = self.b.preview_function_package(
            imported, profile_id="", publication_account=self.owner,
        )
        self.assertEqual(published, preview["content_fingerprint"])
        imported.description = "Changed instructions"
        self.save(self.b, imported)
        changed = self.b.preview_function_package(
            imported, profile_id="", publication_account=self.owner,
        )
        self.assertNotEqual(published, changed["content_fingerprint"])

    def test_signed_in_account_and_license_do_not_replace_missing_creator_attribution(self):
        self.function.creator_account = None
        self.save(self.a, self.function)
        with self.assertRaisesRegex(PackageError, "Missing creator attribution for: Example"):
            self.a.preview_function_package(
                self.function, profile_id="", publication_account=self.owner,
            )
        self.assertIsNone(self.a.load_saved_function("game", "function").creator_account)
        self.assertIsNotNone(self.licenses.get_license(self.owner.account_id))

    def test_attribution_action_makes_own_existing_function_ready(self):
        self.function.creator_account = None
        self.save(self.a, self.function)
        self.a.preparation_service.attribute_functions(
            [("game", "function")], self.owner,
        )
        attributed = self.a.load_saved_function("game", "function")
        preview = self.a.preview_function_package(
            attributed, profile_id="", publication_account=self.owner,
        )
        self.assertEqual(self.owner.to_dict(), preview["manifest"]["package_metadata"]["publisher"])

    def test_missing_creator_license_reports_account_instead_of_sign_in(self):
        self.function.creator_account = GitHubAccount("789", "another")
        self.save(self.a, self.function)
        with self.assertRaisesRegex(PackageError, "No package license is selected for creator @another"):
            self.a.preview_function_package(
                self.function, profile_id="", publication_account=self.owner,
            )

    def test_second_account_creates_explicit_variant_without_replacing_creator(self):
        archive, original = self.archive(self.a, self.function, self.owner, "a")
        imported = self.install_on_b(archive)
        imported.bump_revision("", account=self.contributor)
        self.save(self.b, imported)
        variant, updated = self.archive(self.b, imported, self.contributor, "variant")
        details = updated["package_metadata"]
        self.assertEqual(self.contributor.to_dict(), details["publisher"])
        self.assertEqual(self.owner.to_dict(), details["original_author"])
        self.assertEqual(self.contributor.to_dict(), details["modified_by"])
        self.assertEqual(original["package_metadata"]["package_id"], details["fork_of"]["package_id"])
        self.assertEqual(self.owner.to_dict(), details["fork_of"]["publisher"])
        self.assertEqual("MIT", details["license_id"])
        self.assertEqual(self.owner, self.b.load_saved_function("game", "function").creator_account)
        submission = CatalogSubmissionService(
            managed=ManagedPackageService(self.root / "submission-store", self.b),
            auth=Mock(restore=Mock(return_value=self.contributor)),
        ).prepare(variant, expected_sha256=hashlib.sha256(variant.read_bytes()).hexdigest())
        self.assertEqual(self.contributor.to_dict(), submission["submitter"])
        self.assertEqual(details["fork_of"], submission["fork_of"])

    def test_imported_version_requires_increase_but_description_only_release_can_override(self):
        archive, _ = self.archive(self.a, self.function, self.owner, "a")
        imported = self.install_on_b(archive)
        with self.assertRaisesRegex(PackageError, "newer"):
            self.b.preview_function_package(
                imported, publication_account=self.owner, package_version="1.0.7",
            )
        _, updated = self.archive(self.b, imported, self.owner, "description-only", "1.0.8")
        self.assertEqual("1.0.8", updated["package_metadata"]["package_version"])

    def test_imported_license_is_not_replaced_by_changed_account_preference(self):
        archive, original = self.archive(self.a, self.function, self.owner, "a")
        imported = self.install_on_b(archive)
        self.licenses.set_license("123", "GPL-3.0-only")
        self.licenses.text_fetcher = Mock(side_effect=AssertionError("must reuse preserved terms"))
        preview = self.b.preview_function_package(imported, publication_account=self.owner)
        self.assertEqual(
            original["package_metadata"]["license_text"],
            preview["manifest"]["package_metadata"]["license_text"],
        )

    def test_missing_preserved_original_blocks_republication_instead_of_dropping_terms(self):
        archive, _ = self.archive(self.a, self.function, self.owner, "a")
        imported = self.install_on_b(archive)
        managed = ManagedPackageService(self.b.function_root.parent / "packages", self.b)
        status = managed.list_installations()[0]
        Path(status["original_archive"]).unlink()
        with self.assertRaisesRegex(PackageError, "preserved original"):
            self.b.preview_function_package(imported, publication_account=self.owner)

    def test_variant_cannot_be_submitted_as_original_publisher(self):
        archive, _ = self.archive(self.a, self.function, self.owner, "a")
        imported = self.install_on_b(archive)
        variant, _ = self.archive(self.b, imported, self.contributor, "variant")
        with self.assertRaisesRegex(PackageError, "publisher does not match"):
            CatalogSubmissionService(
                managed=ManagedPackageService(self.root / "submission-store", self.b),
                auth=Mock(restore=Mock(return_value=self.owner)),
            ).prepare(variant, expected_sha256=hashlib.sha256(variant.read_bytes()).hexdigest())

    def test_event_republication_increments_version_even_if_highest_revision_did_not_change(self):
        event = Event(
            event_id="event", game_id="game", name="Event",
            tasks=[Task(task_id="task", function_ids=["function"])],
        )
        preview = self.a.preview_event_package(event, publication_account=self.owner)
        ready = self.a.prepare_event_directory(
            event, str(self.root / "event-ready"),
            package_metadata=dict(preview["manifest"]["package_metadata"], rights_confirmed=True),
            expected_checksums=preview["checksums"],
        )
        archive = self.a.create_ready_archive(
            str(ready), str(self.root / "event.botstudio.zip"),
            expected_checksums=preview["checksums"],
        )
        # Event activation needs an explicitly selected local profile.
        root = self.b.function_root.parent.parent
        (root / "games").mkdir(parents=True)
        (root / "games" / "games.json").write_text('[{"id":"game"}]', encoding="utf-8")
        (root / "profiles").mkdir()
        (root / "profiles" / "profiles.json").write_text("[]", encoding="utf-8")
        managed = ManagedPackageService(root / "data" / "packages", self.b)
        status = managed.install(archive)
        working = Path(status["working_directory"])
        function_path = working / "content" / "functions" / "game" / "function.json"
        imported = BotFunction.from_dict(json.loads(function_path.read_text(encoding="utf-8")))
        self.save(self.b, imported)
        event.name = "Description-only correction"
        updated = self.b.preview_event_package(event, publication_account=self.owner)
        self.assertEqual("1.0.8", updated["manifest"]["package_metadata"]["package_version"])
        self.assertEqual(
            preview["manifest"]["package_metadata"]["package_id"],
            updated["manifest"]["package_metadata"]["package_id"],
        )
