import hashlib
import json
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
import zipfile

from release.collect_notices import distribution_materials, pinned_packages


PROJECT = Path(__file__).resolve().parents[1]


class DistributionMaterialsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.release = self.root / "release"
        shutil.copytree(PROJECT / "release" / "licenses", self.release / "licenses")
        shutil.copy(PROJECT / "release" / "requirements-runtime.txt", self.release)
        self.requirements = self.release / "requirements-runtime.txt"
        self.manifest = json.loads(
            (PROJECT / "release" / "distribution_materials.json").read_text(encoding="utf-8")
        )
        self.models = self.root / "rapidocr" / "models"
        self.models.mkdir(parents=True)
        for item in self.manifest["models"]["files"]:
            payload = item["name"].encode("ascii")
            (self.models / item["name"]).write_bytes(payload)
            item["sha256"] = hashlib.sha256(payload).hexdigest()
            item["size_bytes"] = len(payload)
        self.save_manifest()
        self.distribution = unittest.mock.Mock(version="3.9.2")
        self.distribution.locate_file.side_effect = lambda name: self.root / name
        self.metadata = patch("release.collect_notices.metadata.distribution", return_value=self.distribution)
        self.metadata.start()
        self.addCleanup(self.metadata.stop)

    def save_manifest(self):
        (self.release / "distribution_materials.json").write_text(
            json.dumps(self.manifest), encoding="utf-8"
        )

    def test_version_bound_upstream_texts_and_model_files_are_accepted(self):
        result = distribution_materials(self.requirements)
        self.assertEqual("Apache-2.0", result["models"]["license"])
        self.assertEqual(3, len(result["models"]["files"]))
        self.assertEqual(5, len(result["license_files"]))

    def test_changed_license_is_rejected(self):
        path = self.release / "licenses" / self.manifest["license_files"][0]["file"]
        path.write_text("changed", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "license hash mismatch"):
            distribution_materials(self.requirements)

    def test_changed_dependency_pin_requires_new_evidence(self):
        self.requirements.write_text(
            self.requirements.read_text().replace("rapidocr==3.9.2", "rapidocr==3.9.3"),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "license version mismatch"):
            distribution_materials(self.requirements)

    def test_changed_or_added_model_is_rejected(self):
        path = self.models / self.manifest["models"]["files"][0]["name"]
        path.write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "Model provenance hash mismatch"):
            distribution_materials(self.requirements)
        (self.models / "unreviewed.onnx").write_bytes(b"new")
        with self.assertRaisesRegex(ValueError, "model set differs"):
            distribution_materials(self.requirements)

    def test_installed_model_version_must_match(self):
        self.distribution.version = "3.9.3"
        with self.assertRaisesRegex(ValueError, "Installed model distribution version mismatch"):
            distribution_materials(self.requirements)

    def test_real_manifest_records_exact_model_identity_and_existing_pins(self):
        manifest = json.loads(
            (PROJECT / "release" / "distribution_materials.json").read_text(encoding="utf-8")
        )
        pins = dict(pinned_packages(PROJECT / "release" / "requirements-runtime.txt"))
        for item in manifest["license_files"]:
            payload = (PROJECT / "release" / "licenses" / item["file"]).read_bytes()
            self.assertEqual(item["sha256"], hashlib.sha256(payload).hexdigest())
            for name, version in item["packages"].items():
                self.assertEqual(version, pins[name])
        for model in manifest["models"]["files"]:
            self.assertRegex(model["sha256"], r"^[a-f0-9]{64}$")
            self.assertRegex(model["upstream_revision"], r"^[a-f0-9]{40}$")
            self.assertGreater(model["size_bytes"], 0)


@unittest.skipUnless(shutil.which("powershell") and shutil.which("git"), "Windows release tools")
class SourceEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.release = self.root / "release"
        self.release.mkdir()
        for name in ("prepare_source.ps1", "prepare_public_source.ps1", "get_release_version.ps1",
                     "PUBLIC_README.md", "distribution_review.json"):
            shutil.copy(PROJECT / "release" / name, self.release)
        (self.root / "services").mkdir()
        (self.root / "services" / "application_info.py").write_text(
            'APP_VERSION = "0.1.0.3"\n', encoding="utf-8"
        )
        self.git("init", "--quiet")
        self.git("add", "release", "services")
        self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                 "-c", "commit.gpgsign=false", "commit", "--quiet", "-m", "fixture")
        self.output = self.root / "dist" / "release"
        self.output.mkdir(parents=True)
        installer = self.output / "BotStudio-Setup-0.1.0.3.exe"
        installer.write_bytes(b"synthetic installer, never executed")
        (self.output / "release.json").write_text(json.dumps({
            "tag": "v0.1.0.3", "version": "0.1.0.3", "installer": installer.name,
            "sha256": hashlib.sha256(installer.read_bytes()).hexdigest(),
            "size_bytes": installer.stat().st_size,
        }), encoding="utf-8")
        notices = self.root / "dist" / "BotStudio" / "_internal" / "third_party"
        notices.mkdir(parents=True)
        (notices / "inventory.json").write_text(json.dumps({
            "schema_version": 1, "missing_notice_packages": [],
        }), encoding="utf-8")
        (notices.parent / "synthetic.dll").write_bytes(b"synthetic native inventory entry")
        (self.root / "private-untracked.txt").write_text("not source", encoding="utf-8")

    def git(self, *args):
        return subprocess.run(
            ["git", "-C", str(self.root), *args], check=True, timeout=10,
            capture_output=True, text=True,
        ).stdout.strip()

    def prepare(self):
        return subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-File", str(self.release / "prepare_source.ps1")],
            capture_output=True, text=True, timeout=30,
        )

    def test_exact_source_notices_and_native_files_are_bound_to_installer(self):
        result = self.prepare()
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        manifest = json.loads((self.output / "SOURCE-MANIFEST.json").read_text(encoding="utf-8"))
        self.assertEqual(self.git("rev-parse", "HEAD"), manifest["commit"])
        self.assertFalse(manifest["complete_corresponding_source_approved"])
        self.assertEqual(2, len(manifest["artifacts"]))
        self.assertEqual("_internal/synthetic.dll", manifest["native_files"][0]["path"])
        for item in manifest["artifacts"]:
            payload = (self.output / item["file"]).read_bytes()
            self.assertEqual(item["sha256"], hashlib.sha256(payload).hexdigest())
            self.assertEqual(item["size_bytes"], len(payload))
        with zipfile.ZipFile(self.output / "BotStudio-Application-Source-0.1.0.3.zip") as archive:
            self.assertIn("BotStudio-0.1.0.3/services/application_info.py", archive.namelist())
            self.assertFalse(any("private-untracked" in name or "/dist/" in name
                                 for name in archive.namelist()))
        repeated = self.prepare()
        self.assertNotEqual(0, repeated.returncode)
        self.assertIn("Refusing to replace source evidence", repeated.stdout + repeated.stderr)

    def test_dirty_source_is_rejected(self):
        (self.root / "services" / "application_info.py").write_text(
            'APP_VERSION = "0.1.0.3"\n# uncommitted\n', encoding="utf-8"
        )
        result = self.prepare()
        self.assertNotEqual(0, result.returncode)
        self.assertIn("committed build tree", result.stdout + result.stderr)
        self.assertFalse((self.output / "SOURCE-MANIFEST.json").exists())

    def test_export_ignored_source_is_rejected_and_partial_outputs_removed(self):
        (self.root / ".gitattributes").write_text("services export-ignore\n", encoding="utf-8")
        self.git("add", ".gitattributes")
        self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                 "-c", "commit.gpgsign=false", "commit", "--quiet", "-m", "export ignore")
        result = self.prepare()
        self.assertNotEqual(0, result.returncode)
        self.assertIn("omits tracked content", result.stdout + result.stderr)
        self.assertFalse((self.output / "BotStudio-Application-Source-0.1.0.3.zip").exists())

    def test_public_snapshot_excludes_private_history_and_user_stores(self):
        for directory in ("brain", "config", "data", "profiles", "logs", "backups"):
            folder = self.root / directory
            folder.mkdir()
            (folder / "private.txt").write_text("private fixture", encoding="utf-8")
            self.git("add", directory)
        (self.root / "ui").mkdir()
        (self.root / "ui" / "theme.json.bak").write_text("private backup", encoding="utf-8")
        self.git("add", "ui")
        self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                 "-c", "commit.gpgsign=false", "commit", "--quiet", "-m", "private fixtures")
        destination = self.root / "public-snapshot"
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-File", str(self.release / "prepare_public_source.ps1"),
             "-Destination", str(destination)],
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        for directory in ("brain", "config", "data", "profiles", "logs", "backups", ".git", "dist"):
            self.assertFalse((destination / directory).exists(), directory)
        self.assertFalse((destination / "private-untracked.txt").exists())
        self.assertFalse((destination / "ui" / "theme.json.bak").exists())
        self.assertEqual(
            (PROJECT / "release" / "PUBLIC_README.md").read_bytes(),
            (destination / "README.md").read_bytes(),
        )
        review = json.loads((destination / "release" / "distribution_review.json").read_text())
        self.assertEqual("blocked", review["status"])
        self.assertIsNone(review["approved_application_version"])
        self.assertNotIn("39118042837", json.dumps(review["observations"]))

    def test_upstream_license_bytes_survive_windows_line_ending_checkout(self):
        shutil.copy(PROJECT / ".gitattributes", self.root)
        licenses = self.release / "licenses"
        licenses.mkdir()
        source = PROJECT / "release" / "licenses" / "RapidOCR-3.9.2.txt"
        shutil.copy(source, licenses)
        self.git("config", "core.autocrlf", "true")
        self.git("add", ".gitattributes", "release/licenses")
        self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                 "-c", "commit.gpgsign=false", "commit", "--quiet", "-m", "byte exact licenses")
        checkout = self.root / "fresh-checkout"
        subprocess.run(
            ["git", "-c", "core.autocrlf=true", "clone", "--quiet", str(self.root), str(checkout)],
            check=True, capture_output=True, text=True, timeout=15,
        )
        self.assertEqual(source.read_bytes(),
                         (checkout / "release" / "licenses" / source.name).read_bytes())
