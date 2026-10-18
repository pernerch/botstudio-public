import faulthandler
from hashlib import sha256
import io
import json
from pathlib import Path
import tarfile
from tempfile import TemporaryDirectory
import time
import unittest
import zipfile
from unittest.mock import patch

from release.prepare_dependency_sources import (
    bundled_materials,
    digest,
    fetch,
    prepare,
    safe_name,
    source_materials,
    source_archive,
    source_pins,
    validate_lock,
)


class DependencySourceTests(unittest.TestCase):
    def setUp(self):
        self.started = time.monotonic()
        print(f"TEST START: {self.id()}", flush=True)
        faulthandler.dump_traceback_later(20, exit=True)
        self.addCleanup(faulthandler.cancel_dump_traceback_later)
        self.addCleanup(lambda: print(f"TEST END: {time.monotonic() - self.started:.3f}s", flush=True))
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.release = self.root / "release"
        self.release.mkdir()
        self.requirements = self.release / "requirements-runtime.txt"
        self.requirements.write_text("example==1.0\n", encoding="utf-8")
        (self.release / "requirements-build.txt").write_text("-r requirements-runtime.txt\n", encoding="utf-8")
        self.cache = self.root / "cache"
        self.cache.mkdir()
        self.archive = self.cache / "example-1.0.tar.gz"
        self.write_archive({
            "example-1.0/LICENSE": b"Verbatim upstream license\r\n",
            "example-1.0/qt_attribution.json": b'{"LicenseFile":"LICENSE","Copyright":"A\\nB"}',
            "example-1.0/src/code.c": b"Source code not copied into notice directory\n",
            "example-1.0/CMakeLists.txt": b"project(example)\n",
        })
        self.lock = {
            "schema_version": 1,
            "archives": [{
                "id": "example", "version": "1.0", "packages": {"example": "1.0"},
                "filename": self.archive.name, "url": "https://example.invalid/source.tar.gz",
                "sha256": digest(self.archive), "size_bytes": self.archive.stat().st_size,
                "scope": "synthetic-source",
            }],
        }
        self.lock_path = self.release / "dependency_sources.json"
        self.save_lock()
        self.output = self.root / "materials"

    def write_archive(self, entries):
        with tarfile.open(self.archive, "w:gz") as archive:
            for name, payload in entries.items():
                member = tarfile.TarInfo(name)
                member.size = len(payload)
                archive.addfile(member, io.BytesIO(payload))

    def save_lock(self):
        self.lock_path.write_text(json.dumps(self.lock), encoding="utf-8")

    def test_real_lock_covers_exact_runtime_and_build_pins(self):
        release = Path(__file__).resolve().parents[1] / "release"
        lock = json.loads((release / "dependency_sources.json").read_text(encoding="utf-8"))
        validate_lock(lock, source_pins(release / "requirements-runtime.txt"))
        ids = {item["id"] for item in lock["archives"]}
        self.assertTrue({"qtbase", "pyside-setup", "geos", "openblas", "numpy", "shapely"} <= ids)
        self.assertTrue({"qt-release-build", "shapely-wheel-build", "mesa"} <= ids)
        self.assertEqual(38, len(ids))

    def test_preserves_license_and_attribution_bytes_and_build_instructions(self):
        materials = source_materials(self.archive)
        self.assertEqual(b"Verbatim upstream license\r\n", materials["example-1.0/LICENSE"])
        self.assertIn("example-1.0/qt_attribution.json", materials)
        self.assertIn("example-1.0/CMakeLists.txt", materials)
        self.assertNotIn("example-1.0/src/code.c", materials)

    def test_qt_license_reference_with_arbitrary_filename_is_included(self):
        self.write_archive({
            "root/sub/qt_attribution.json": b'{"LicenseFile":"../legal-terms"}',
            "root/legal-terms": b"Full upstream terms",
        })
        self.assertEqual(b"Full upstream terms", source_materials(self.archive)["root/legal-terms"])

    def test_qt_multiline_copyright_metadata_is_preserved_without_rewriting(self):
        payload = b'{"LicenseFile":"LICENSE","Copyright":"A\nB"}'
        self.write_archive({"root/qt_attribution.json": payload, "root/LICENSE": b"license"})
        self.assertEqual(payload, source_materials(self.archive)["root/qt_attribution.json"])

    def test_missing_referenced_qt_license_fails(self):
        self.write_archive({"root/qt_attribution.json": b'{"LicenseFile":"missing"}'})
        with self.assertRaisesRegex(ValueError, "Missing referenced Qt license"):
            source_materials(self.archive)

    def test_unsafe_paths_are_rejected_not_extracted(self):
        for name in ("../LICENSE", "/LICENSE", "C:/LICENSE", "root\\LICENSE"):
            with self.subTest(name=name):
                with self.assertRaisesRegex(ValueError, "Unsafe source archive path"):
                    safe_name(name)
        self.write_archive({"../LICENSE": b"not safe"})
        with self.assertRaisesRegex(ValueError, "Unsafe source archive path"):
            source_materials(self.archive)
        self.assertFalse((self.root / "LICENSE").exists())

    def test_source_version_change_requires_new_lock(self):
        with self.assertRaisesRegex(ValueError, "version mismatch"):
            validate_lock(self.lock, {"example": "2.0"})

    def test_uncovered_source_and_unresolved_digest_fail(self):
        with self.assertRaisesRegex(ValueError, "Source coverage missing"):
            validate_lock(self.lock, {"example": "1.0", "other": "2.0"})
        del self.lock["archives"][0]["sha256"]
        with self.assertRaisesRegex(ValueError, "Unresolved source"):
            validate_lock(self.lock, {"example": "1.0"})

    def test_corrupt_source_is_rejected_and_only_new_output_is_removed(self):
        self.archive.write_bytes(b"corrupt")
        with self.assertRaisesRegex(ValueError, "Source SHA256/size mismatch"):
            prepare(self.lock_path, self.cache, self.output, self.requirements)
        self.assertFalse(self.output.exists())
        self.assertTrue(self.archive.exists())

    def test_preparation_is_reusable_and_explicitly_not_complete_source_approval(self):
        with patch("release.prepare_dependency_sources.fetch") as fetch:
            report = prepare(self.lock_path, self.cache, self.output, self.requirements)
            repeated = prepare(self.lock_path, self.cache, self.output, self.requirements)
            fetch.assert_not_called()
        self.assertEqual(report, repeated)
        self.assertFalse(report["complete_corresponding_source_approved"])
        datas, index = bundled_materials(self.lock_path, self.output)
        self.assertEqual(sha256(self.lock_path.read_bytes()).hexdigest(), index["source_lock_sha256"])
        self.assertTrue(any("source-notices" in destination for _, destination in datas))

    def test_modified_prepared_notice_is_not_bundled(self):
        report = prepare(self.lock_path, self.cache, self.output, self.requirements)
        license_entry = next(item for item in report["archives"][0]["materials"] if item["archive_path"].endswith("/LICENSE"))
        (self.output / "notices" / license_entry["path"]).write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "notice hash mismatch"):
            bundled_materials(self.lock_path, self.output)

    def test_modified_lock_or_coverage_is_rejected(self):
        prepare(self.lock_path, self.cache, self.output, self.requirements)
        index_path = self.output / "source-index.json"
        report = json.loads(index_path.read_text())
        report["archives"] = []
        index_path.write_text(json.dumps(report), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "coverage mismatch"):
            bundled_materials(self.lock_path, self.output)

    def test_short_installed_names_preserve_long_upstream_member_mapping(self):
        long_path = "example/" + "very-long-directory/" * 15 + "LICENSE"
        self.write_archive({long_path: b"Exact upstream bytes"})
        self.lock["archives"][0].update(
            sha256=digest(self.archive), size_bytes=self.archive.stat().st_size,
        )
        self.save_lock()
        report = prepare(self.lock_path, self.cache, self.output, self.requirements)
        notice = report["archives"][0]["materials"][0]
        self.assertEqual(long_path, notice["archive_path"])
        self.assertLess(len(notice["path"]), 100)
        self.assertEqual(b"Exact upstream bytes", (self.output / "notices" / notice["path"]).read_bytes())

    def test_standalone_archive_contains_exact_sources_and_notices(self):
        prepare(self.lock_path, self.cache, self.output, self.requirements)
        (self.release / "DISTRIBUTION.md").write_text("Synthetic instructions", encoding="utf-8")
        destination = self.root / "source-delivery.zip"
        source_archive(self.lock_path, self.cache, self.output, destination)
        with zipfile.ZipFile(destination) as archive:
            self.assertEqual(self.archive.read_bytes(), archive.read("sources/" + self.archive.name))
            self.assertIn("source-index.json", archive.namelist())
            self.assertIn("dependency_sources.json", archive.namelist())
            self.assertIn("DISTRIBUTION.md", archive.namelist())
            self.assertFalse(json.loads(archive.read("source-index.json"))["complete_corresponding_source_approved"])
        with self.assertRaisesRegex(FileExistsError, "Refusing to replace"):
            source_archive(self.lock_path, self.cache, self.output, destination)

    def test_standalone_archive_rejects_changed_source_and_removes_partial_delivery(self):
        prepare(self.lock_path, self.cache, self.output, self.requirements)
        self.archive.write_bytes(b"corrupt")
        destination = self.root / "failed-delivery.zip"
        with self.assertRaisesRegex(ValueError, "Source archive changed"):
            source_archive(self.lock_path, self.cache, self.output, destination)
        self.assertFalse(destination.exists())

    def test_failed_download_does_not_remove_an_existing_partial_file(self):
        partial = self.archive.with_suffix(self.archive.suffix + ".partial")
        partial.write_bytes(b"Existing task content")
        with patch("release.prepare_dependency_sources.urlopen", side_effect=OSError("Offline")):
            with self.assertRaisesRegex(OSError, "Offline"):
                fetch("https://example.invalid/source.tar.gz", self.archive)
        self.assertEqual(b"Existing task content", partial.read_bytes())


if __name__ == "__main__":
    result = unittest.main(verbosity=2, exit=False).result
    print("TEST RESULT: SUCCESS" if result.wasSuccessful() else "TEST RESULT: FAILURE", flush=True)
    raise SystemExit(0 if result.wasSuccessful() else 1)
