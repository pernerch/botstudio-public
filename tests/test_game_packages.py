import hashlib
import faulthandler
import json
import sys
import time
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock
import zipfile

from copy import deepcopy

from models.bot_function import BotFunction, FunctionBlock, FunctionPackageOrigin, HelperDependency
from models.github_account import GitHubAccount
from scheduler.models import Event, Task
from services.package_service import PackageError, PackageService


class GamePackageTests(unittest.TestCase):
    def setUp(self):
        started = time.monotonic()
        print(f"TEST START: {self.id()}")
        faulthandler.dump_traceback_later(15, exit=True)
        self.addCleanup(faulthandler.cancel_dump_traceback_later)
        self.addCleanup(lambda: print(
            f"TEST END: {self.id()} duration {time.monotonic() - started:.3f}s",
        ))
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.functions = self.root / "functions"
        self.function = BotFunction(function_id="example", name="Example", game_id="game", saved=True)
        source = self.functions / "game" / f"{self.function.function_id}.json"
        source.parent.mkdir(parents=True)
        source.write_text(json.dumps(self.function.to_dict()), encoding="utf-8")
        self.service = PackageService(
            function_root=str(self.functions),
            library_service=Mock(root=self.root / "library"),
            variable_service=Mock(root=self.root / "variables"),
            game_service=Mock(get_game=Mock(return_value={"display_name": "Example Game"})),
        )
        self.archive = self.root / "example.botstudio.zip"
        self.details = {
            "package_id": "author.example",
            "package_version": "1.0.0",
            "author": "Package Author",
            "license_id": "LicenseRef-Author-Terms",
            "license_text": "Local use and modification allowed. No redistribution.",
            "third_party_notices": "Images are separately licensed.",
            "support": "Support covers the original version only.",
            "rights_confirmed": True,
            "publisher": {
                "provider": "github",
                "account_id": "12345",
                "login": "chris",
            },
        }

    def export(self, details=None):
        self.service.export_function(
            self.function, str(self.archive), package_metadata=details,
        )
        return self.service.inspect_package(str(self.archive))

    def test_author_license_round_trip_and_preview(self):
        manifest = self.export(self.details)
        self.assertEqual(self.details, manifest["package_metadata"])
        from services.application_info import APP_VERSION
        self.assertEqual(APP_VERSION, manifest["botstudio_version"])
        preview = self.service.manifest_markdown(manifest)
        for value in ("Package Author", "1.0.0", self.details["license_text"],
                      self.details["support"], self.details["third_party_notices"]):
            self.assertIn(value, preview)
        publication = self.service.github_release_metadata(manifest, str(self.archive))
        self.assertEqual(preview, publication["body"])

    def _sample_library(self):
        self.function.blocks = [
            FunctionBlock.create("find_element", params={"library_entry_id": "image"}),
        ]
        (self.functions / "game/example.json").write_text(json.dumps(self.function.to_dict()), encoding="utf-8")
        root = self.root / "library/image"
        (root / "accepted").mkdir(parents=True)
        (root / "rejected").mkdir()
        from PySide6.QtGui import QImage
        for name, color in (("pattern_reference.png", 0xFFFFFF),
                            ("accepted/001_source.png", 0xFFFFFF),
                            ("accepted/002_source.png", 0xFFFFFF),
                            ("rejected/003_source.png", 0x111111)):
            image = QImage(4, 4, QImage.Format_RGB32)
            image.fill(color)
            self.assertTrue(image.save(str(root / name)))
        samples = []
        for folder, number in (("accepted", "001"), ("accepted", "002"), ("rejected", "003")):
            crop = root / folder / f"{number}_crop.png"
            crop.write_bytes((root / folder / f"{number}_source.png").read_bytes())
            match = root / folder / f"{number}_match.json"
            match.write_text(json.dumps({"accepted": folder == "accepted", "bbox": {"x": 0, "y": 0, "width": 4, "height": 4},
                                         "yolo": {"center_x": 0.5, "center_y": 0.5, "width": 1, "height": 1}}))
            samples.append({key: str(path) for key, path in (
                ("source", root / folder / f"{number}_source.png"), ("crop", crop), ("match", match),
            )})
        entry = {"entry_id": "image", "name": "Image", "boundary": {},
                 "pattern": {"reference_path": str(root / "pattern_reference.png")},
                 "accepted_samples": samples[:2], "rejected_samples": samples[2:], "sample_count": 3}
        (root / "entry.json").write_text(json.dumps(entry), encoding="utf-8")
        return root

    def test_lossless_library_export_ready_roundtrip_and_legacy_fingerprint(self):
        root = self._sample_library()
        before = {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}
        preview = self.service.preview_function_package(self.function, package_metadata=self.details)
        self.export(self.details)
        with zipfile.ZipFile(self.archive) as archive:
            self.assertNotIn("content/library/image/accepted/002_source.png", archive.namelist())
            self.assertIn("content/library/image/rejected/003_source.png", archive.namelist())
            entry = json.loads(archive.read("content/library/image/entry.json"))
            self.assertEqual(3, entry["sample_count"])
            self.assertEqual(entry["accepted_samples"][0]["source"], entry["accepted_samples"][1]["source"])
            for field in ("accepted_samples", "rejected_samples"):
                for sample in entry[field]:
                    for key in ("source", "crop", "match"):
                        self.assertIn("content/library/" + sample[key], archive.namelist())
            for relative, raw in before.items():
                if relative not in ("entry.json", "accepted/002_source.png"):
                    self.assertEqual(raw, archive.read("content/library/image/" + relative))
            legacy_files = {name: archive.read(name) for name in archive.namelist() if name.startswith("content/")}
            manifest = json.loads(archive.read("manifest.json"))
        for relative, raw in before.items():
            legacy_files["content/library/image/" + relative] = raw
        manifest["checksums"] = {name: hashlib.sha256(raw).hexdigest() for name, raw in legacy_files.items()}
        legacy = self.root / "legacy.zip"
        with zipfile.ZipFile(legacy, "w") as archive:
            archive.writestr("manifest.json", json.dumps(manifest))
            archive.writestr("checksums.json", json.dumps(manifest["checksums"]))
            for name, raw in legacy_files.items():
                archive.writestr(name, raw)
        self.assertEqual(preview["content_fingerprint"], self.service.archive_content_fingerprint(legacy))
        ready = self.root / "ready"
        self.service.prepare_function_directory(
            self.function, str(ready), package_metadata=self.details, expected_checksums=preview["checksums"],
        )
        staged_zip = self.root / "ready.botstudio.zip"
        self.service.create_ready_archive(str(ready), str(staged_zip), preview["checksums"])
        self.assertEqual(preview["content_fingerprint"], self.service.archive_content_fingerprint(staged_zip))
        imported = self.root / "imported"
        self.service.import_package(str(staged_zip), str(imported))
        imported_service = PackageService(
            function_root=str(imported / "content/functions"),
            library_service=Mock(root=imported / "content/library"),
            variable_service=Mock(root=imported / "content/variables"),
            game_service=self.service.game_service,
        )
        reexport = self.root / "again.zip"
        imported_service.export_function(self.function, str(reexport), package_metadata=self.details)
        self.assertEqual(preview["content_fingerprint"], imported_service.archive_content_fingerprint(reexport))
        self.assertEqual(before, {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()})

    def test_event_and_helper_include_optimized_library(self):
        self._sample_library()
        for kind in ("event", "helper"):
            with self.subTest(kind=kind):
                if kind == "event":
                    event = Event(game_id="game", name="Event", tasks=[Task(function_ids=["example"])])
                    self.service.export_event(event, str(self.archive), package_metadata=self.details)
                else:
                    self.function.kind = "helper"
                    self.export(self.details)
                with zipfile.ZipFile(self.archive) as archive:
                    self.assertIn("content/library/image/entry.json", archive.namelist())
                    self.assertNotIn("content/library/image/accepted/002_source.png", archive.namelist())

    def test_library_deduplication_preserves_cross_entry_and_rejects_missing_assets(self):
        root = self._sample_library()
        files = {"content/library/image/" + p.relative_to(root).as_posix(): p.read_bytes()
                 for p in root.rglob("*") if p.is_file()}
        files["content/library/other/accepted/001_source.png"] = files["content/library/image/accepted/001_source.png"]
        overrides, removed = self.service._portable_library_files(files, files.__getitem__)
        self.assertEqual({"content/library/image/accepted/002_source.png"}, removed)
        self.assertIn("content/library/image/entry.json", overrides)
        del files["content/library/image/accepted/002_crop.png"]
        with self.assertRaisesRegex(PackageError, "missing Library asset"):
            self.service._portable_library_files(files, files.__getitem__)

    def test_content_fingerprint_ignores_versions_but_keeps_description_images_and_logic(self):
        original = self.service.preview_function_package(
            self.function, description="Instructions", package_metadata=self.details,
        )["content_fingerprint"]
        self.service.export_function(
            self.function, str(self.archive), description="Instructions", package_metadata=self.details,
        )
        self.assertEqual(original, self.service.archive_content_fingerprint(self.archive))
        function_data = self.function.to_dict()
        function_data.update(revision=99, revised_at="2030-01-01", used_by=["other"],
                             layout_positions={"a": [30, 40]})
        source = self.functions / "game" / "example.json"
        source.write_text(json.dumps(function_data), encoding="utf-8")
        metadata = {**self.details, "package_version": "9.0.0", "rights_confirmed": False}
        preview = self.service.preview_function_package(
            self.function, description="Instructions", package_metadata=metadata,
        )
        self.assertEqual(original, preview["content_fingerprint"])
        changed_description = self.service.preview_function_package(
            self.function, description="New instructions", package_metadata=metadata,
        )
        self.assertNotEqual(original, changed_description["content_fingerprint"])
        function_data["description"] = "Different function instructions"
        source.write_text(json.dumps(function_data), encoding="utf-8")
        self.assertNotEqual(original, self.service.preview_function_package(
            self.function, description="Instructions", package_metadata=metadata,
        )["content_fingerprint"])
        function_data["description"] = self.function.description
        function_data["blocks"] = [{"block_id": "a", "block_type": "logic"}]
        source.write_text(json.dumps(function_data), encoding="utf-8")
        self.assertNotEqual(original, self.service.preview_function_package(
            self.function, description="Instructions", package_metadata=metadata,
        )["content_fingerprint"])
        source.write_text(json.dumps(self.function.to_dict()), encoding="utf-8")
        image = self.root / "image.png"
        image.write_bytes(b"first-image")
        options = {
            "description": "Instructions", "package_metadata": metadata,
            "package_assets": {"content/package-assets/image-01.png": image},
        }
        with_image = self.service.preview_function_package(self.function, **options)["content_fingerprint"]
        self.assertNotEqual(original, with_image)
        image.write_bytes(b"second-image")
        self.assertNotEqual(with_image, self.service.preview_function_package(
            self.function, **options,
        )["content_fingerprint"])

    def test_event_content_fingerprint_ignores_profile_settings_and_ids(self):
        first = Event(event_id="a", game_id="game", name="Example", tasks=[
            Task(task_id="a", name="Task", function_ids=["example"]),
        ])
        second = Event.from_dict(first.to_dict())
        second.event_id = "b"
        second.profile_id = "other"
        second.interval_seconds = 90
        second.tasks[0].task_id = "b"
        second.tasks[0].name = "Another label"
        options = {"profile_id": "", "package_metadata": self.details}
        expected = self.service.preview_event_package(first, **options)["content_fingerprint"]
        actual = self.service.preview_event_package(second, **options)["content_fingerprint"]
        self.assertEqual(expected, actual)
        self.service.export_event(second, str(self.archive), **options)
        self.assertEqual(expected, self.service.archive_content_fingerprint(self.archive))
        second.output_variable = "different-output"
        self.assertNotEqual(expected, self.service.preview_event_package(
            second, **options,
        )["content_fingerprint"])

    def test_legacy_package_is_inspectable_but_not_publishable(self):
        manifest = self.export()
        self.assertIn("UNSPECIFIED", self.service.manifest_markdown(manifest))
        with self.assertRaisesRegex(PackageError, "license"):
            self.service.github_release_metadata(manifest)
        self.service.import_package(str(self.archive), str(self.root / "legacy"))

    def test_display_name_is_not_identity_and_renames_preserve_identity(self):
        publisher = self.details["publisher"]
        key = self.service.publisher_identity_key(publisher)
        renamed = dict(publisher, login="new-name")
        different_account = dict(publisher, account_id="67890")
        self.assertEqual(key, self.service.publisher_identity_key(renamed))
        self.assertNotEqual(key, self.service.publisher_identity_key(different_account))

    def test_declared_identity_round_trip_does_not_claim_verification(self):
        details = dict(
            self.details,
            original_author=dict(self.details["publisher"]),
            modified_by=dict(self.details["publisher"], account_id="67890"),
        )
        manifest = self.export(details)
        self.assertEqual(details, manifest["package_metadata"])
        preview = self.service.manifest_markdown(manifest)
        for text in ("Publisher (declared, unverified)", "Original author",
                     "Modified by", "github:12345", "github:67890"):
            self.assertIn(text, preview)

    def test_legacy_author_cannot_be_implicitly_assigned_to_a_publisher(self):
        details = dict(self.details)
        del details["publisher"]
        manifest = self.export(details)
        self.assertNotIn("publisher", manifest["package_metadata"])
        self.assertIn("Publisher identity:** UNSPECIFIED", self.service.manifest_markdown(manifest))
        with self.assertRaisesRegex(PackageError, "publisher"):
            self.service.github_release_metadata(manifest)
        self.service.import_package(str(self.archive), str(self.root / "author-only"))

    def test_identity_rejects_names_as_ids_and_self_asserted_verification(self):
        publisher = self.details["publisher"]
        invalid = [
            dict(publisher, account_id=value)
            for value in ("Chris", "", "0123", "-1", "1.0", " 123", 123, True, None)
        ] + [
            dict(publisher, verified=True),
            dict(publisher, provider="discord"),
            dict(publisher, login=" "),
            dict(publisher, login="@chris"),
            dict(publisher, login="bad--login"),
            None,
        ]
        for identity in invalid:
            with self.subTest(identity=identity):
                with self.assertRaises(PackageError):
                    self.export(dict(self.details, publisher=identity))

    def test_legacy_display_name_is_local_only_and_not_a_publication_login(self):
        identity = {"provider": "github", "account_id": "12345", "display_name": "Chris"}
        manifest = self.export(dict(self.details, publisher=identity))
        self.assertIn("Chris", self.service.manifest_markdown(manifest))
        with self.assertRaisesRegex(PackageError, "login"):
            self.service.github_release_metadata(manifest)

    def test_publication_requires_explicit_boolean_rights_confirmation(self):
        for confirmation in (False, "true", 1, None):
            with self.subTest(confirmation=confirmation):
                details = dict(self.details, rights_confirmed=confirmation)
                if isinstance(confirmation, bool):
                    manifest = self.export(details)
                    with self.assertRaisesRegex(PackageError, "rights"):
                        self.service.github_release_metadata(manifest)
                else:
                    with self.assertRaises(PackageError):
                        self.export(details)

    def test_incomplete_metadata_is_rejected_before_export(self):
        for field in ("author", "license_id", "license_text", "package_id", "package_version"):
            with self.subTest(field=field):
                details = dict(self.details)
                details[field] = " "
                with self.assertRaises(PackageError):
                    self.export(details)

    def test_event_export_carries_the_same_license_metadata(self):
        event = Event(game_id="game", name="Example event", tasks=[])
        self.service.export_event(
            event, str(self.archive), package_metadata=self.details,
        )
        manifest = self.service.inspect_package(str(self.archive))
        self.assertEqual(self.details, manifest["package_metadata"])
        self.assertIn(self.details["license_text"], self.service.manifest_markdown(manifest))

    def test_replace_preserves_unrelated_packages_and_reports_conflicts(self):
        self.export(self.details)
        target = self.root / "import"
        unrelated = target / "content" / "functions" / "other" / "keep.json"
        unrelated.parent.mkdir(parents=True)
        unrelated.write_text("keep", encoding="utf-8")
        self.service.import_package(str(self.archive), str(target))
        with self.assertRaisesRegex(PackageError, "conflicts"):
            self.service.import_package(str(self.archive), str(target))
        self.service.import_package(str(self.archive), str(target), replace=True)
        self.assertEqual("keep", unrelated.read_text(encoding="utf-8"))

    def test_import_rejects_unlisted_or_traversing_content_without_writes(self):
        for name, checksum_included in (
            ("content/unlisted.txt", False),
            ("content/../escape.txt", True),
            ("content/C:/escape.txt", True),
            ("content/functions/game/file.json:stream", True),
            ("content/functions/game/file.json.", True),
            ("content//file.json", True),
        ):
            with self.subTest(name=name):
                manifest = self.export(self.details)
                with zipfile.ZipFile(self.archive) as archive:
                    payload = {item: archive.read(item) for item in archive.namelist()}
                payload[name] = b"unexpected"
                checksums = json.loads(payload["checksums.json"])
                if checksum_included:
                    checksums[name] = hashlib.sha256(payload[name]).hexdigest()
                manifest["checksums"] = checksums
                payload["manifest.json"] = json.dumps(manifest).encode()
                payload["checksums.json"] = json.dumps(checksums).encode()
                with zipfile.ZipFile(self.archive, "w") as archive:
                    for item, value in payload.items():
                        archive.writestr(item, value)
                target = self.root / "rejected"
                with self.assertRaises(PackageError):
                    self.service.import_package(str(self.archive), str(target))
                self.assertFalse(target.exists())

    def test_inspection_rejects_corrupt_payload_or_disagreeing_checksums(self):
        for corruption in ("payload", "manifest"):
            with self.subTest(corruption=corruption):
                manifest = self.export(self.details)
                with zipfile.ZipFile(self.archive) as archive:
                    payload = {item: archive.read(item) for item in archive.namelist()}
                name = next(iter(manifest["checksums"]))
                if corruption == "payload":
                    payload[name] = b"changed"
                else:
                    manifest["checksums"][name] = "0" * 64
                    payload["manifest.json"] = json.dumps(manifest).encode()
                with zipfile.ZipFile(self.archive, "w") as archive:
                    for item, value in payload.items():
                        archive.writestr(item, value)
                with self.assertRaisesRegex(PackageError, "checksum"):
                    self.service.inspect_package(str(self.archive))

    def test_publication_does_not_treat_unspecified_license_as_permission(self):
        for license_id in ("UNSPECIFIED", "NOASSERTION", "NONE"):
            with self.subTest(license_id=license_id):
                manifest = self.export(dict(self.details, license_id=license_id))
                with self.assertRaisesRegex(PackageError, "license"):
                    self.service.github_release_metadata(manifest)

    def imported_function(self, target):
        path = target / "content" / "functions" / "game" / "example.json"
        return BotFunction.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def test_function_origin_records_exact_source_without_changing_author_or_original(self):
        self.function.author = "Original local label"
        source = self.functions / "game" / "example.json"
        source.write_text(json.dumps(self.function.to_dict()), encoding="utf-8")
        before = source.read_bytes()
        self.export(self.details)
        archive_before = self.archive.read_bytes()
        target = self.root / "with-origin"
        self.service.import_package(str(self.archive), str(target))
        function = self.imported_function(target)
        self.assertEqual("Original local label", function.author)
        self.assertEqual([FunctionPackageOrigin(
            publisher_key="github:12345", package_id="author.example",
            package_version="1.0.0", game_id="game", function_id="example",
            revision=1, source_sha256=hashlib.sha256(before).hexdigest(),
        )], function.package_origins)
        self.assertEqual(before, source.read_bytes())
        self.assertEqual(archive_before, self.archive.read_bytes())

    def test_edits_copies_and_reexport_preserve_the_origin_chain(self):
        creator = GitHubAccount("12345", "creator")
        self.function.creator_account = creator
        self.function.bump_revision("", account=creator)
        source = self.functions / "game" / "example.json"
        source.write_text(json.dumps(self.function.to_dict()), encoding="utf-8")
        self.export(self.details)
        first = self.root / "first"
        self.service.import_package(str(self.archive), str(first))
        function = self.imported_function(first)
        original_origins = list(function.package_origins)
        copied = deepcopy(function)
        copied.function_id = "copy"
        modifier = GitHubAccount("67890", "modifier")
        copied.bump_revision("", "Bugfix", account=modifier)
        self.assertEqual(original_origins, copied.package_origins)
        self.assertEqual("example", copied.package_origins[0].function_id)
        source = self.functions / "game" / "copy.json"
        source.write_text(json.dumps(copied.to_dict()), encoding="utf-8")
        fork_archive = self.root / "fork.zip"
        fork_details = dict(self.details, package_id="author.fork", package_version="1.1.0")
        self.service.export_function(copied, str(fork_archive), package_metadata=fork_details)
        second = self.root / "second"
        self.service.import_package(str(fork_archive), str(second))
        restored = BotFunction.from_dict(json.loads(
            (second / "content/functions/game/copy.json").read_text(encoding="utf-8"),
        ))
        self.assertEqual(original_origins, restored.package_origins[:-1])
        self.assertEqual("author.fork", restored.package_origins[-1].package_id)
        self.assertEqual("copy", restored.package_origins[-1].function_id)
        self.assertEqual(3, restored.package_origins[-1].revision)
        self.assertEqual(creator, restored.creator_account)
        self.assertEqual(creator, restored.history[-2].account)
        self.assertEqual(modifier, restored.history[-1].account)
        self.assertEqual("@modifier", restored.history[-1].author)
        self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(),
                         restored.package_origins[-1].source_sha256)

    def test_event_and_helper_functions_each_receive_their_own_source_revision(self):
        helper = BotFunction(function_id="helper", name="Helper", game_id="game",
                             kind="helper", revision=7, saved=True)
        (self.functions / "game/helper.json").write_text(
            json.dumps(helper.to_dict()), encoding="utf-8",
        )
        self.function.helper_dependencies = [HelperDependency("helper", tested_revision=7)]
        (self.functions / "game/example.json").write_text(
            json.dumps(self.function.to_dict()), encoding="utf-8",
        )
        event = Event(game_id="game", name="Event",
                      tasks=[Task(function_ids=["example"])])
        self.service.export_event(event, str(self.archive), package_metadata=self.details)
        target = self.root / "event"
        self.service.import_package(str(self.archive), str(target))
        for identifier, revision in (("example", 1), ("helper", 7)):
            data = json.loads((target / f"content/functions/game/{identifier}.json").read_text())
            origin = BotFunction.from_dict(data).package_origins[0]
            self.assertEqual(identifier, origin.function_id)
            self.assertEqual(revision, origin.revision)

    def test_legacy_function_and_unidentified_package_do_not_invent_origin(self):
        old = self.function.to_dict()
        del old["package_origins"]
        self.assertEqual([], BotFunction.from_dict(old).package_origins)
        self.export()
        target = self.root / "legacy-origin"
        self.service.import_package(str(self.archive), str(target))
        self.assertEqual([], self.imported_function(target).package_origins)

    def test_invalid_origin_is_rejected_instead_of_silently_dropped(self):
        origin = FunctionPackageOrigin(
            "github:12345", "example", "1.0.0", "game", "example", 1, "a" * 64,
        ).to_dict()
        invalid = [
            dict(origin, verified=True), dict(origin, revision=True),
            dict(origin, revision=0), dict(origin, source_sha256="wrong"),
            dict(origin, publisher_key="Chris"),
        ]
        for value in invalid + [None, "invalid"]:
            with self.subTest(origin=value):
                with self.assertRaises(ValueError):
                    BotFunction.from_dict(dict(self.function.to_dict(), package_origins=[value]))
        with self.assertRaises(ValueError):
            BotFunction.from_dict(dict(self.function.to_dict(), package_origins={}))

    def test_mismatched_function_identity_rejects_import_before_target_writes(self):
        for field, value in (("function_id", "different"), ("game_id", "other"),
                             ("revision", True), ("package_origins", [{"verified": True}])):
            with self.subTest(field=field):
                data = dict(self.function.to_dict(), **{field: value})
                (self.functions / "game/example.json").write_text(json.dumps(data), encoding="utf-8")
                self.export(self.details)
                target = self.root / "invalid-function"
                with self.assertRaisesRegex(PackageError, "invalid packaged Function"):
                    self.service.import_package(str(self.archive), str(target))
                self.assertFalse(target.exists())


if __name__ == "__main__":
    result = unittest.main(exit=False).result
    print("TEST RESULT: SUCCESS" if result.wasSuccessful() else "TEST RESULT: FAILURE")
    sys.exit(0 if result.wasSuccessful() else 1)
