from email.message import Message
import hashlib
import json
from pathlib import Path
import runpy
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch


HELPERS = runpy.run_path(str(Path(__file__).resolve().parents[1] / "release" / "collect_notices.py"))


class ReleaseNoticesTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.requirements = self.root / "requirements.txt"
        self.requirements.write_text("# Exact pins\nexample==1.0\n", encoding="utf-8")
        self.license = self.root / "example.dist-info" / "licenses" / "LICENSE"
        self.license.parent.mkdir(parents=True)
        self.license.write_bytes(b"Test notice\n")
        (self.root / "LICENSE.txt").write_bytes(b"Test Python notice\n")
        info = Message()
        info["Name"] = "example"
        info["License-Expression"] = "MIT"
        self.distribution = SimpleNamespace(
            version="1.0", metadata=info,
            files=[Path("example.dist-info/licenses/LICENSE")],
            locate_file=lambda entry: self.root / entry,
            read_text=lambda name: "Name: example\nLicense-Expression: MIT\n\nExample description\n",
        )
        self.patches = [
            patch("importlib.metadata.distribution", return_value=self.distribution),
            patch("importlib.metadata.version", return_value="1.0"),
            patch("sys.base_prefix", str(self.root)),
        ]
        for mock in self.patches:
            mock.start()
            self.addCleanup(mock.stop)

    def collect(self):
        output = self.root / "output"
        datas = HELPERS["collect_notices"](self.requirements, output)
        return datas, output, json.loads((output / "inventory.json").read_text(encoding="utf-8"))

    def test_collects_exact_notice_bytes_hashes_and_python_license(self):
        datas, output, report = self.collect()
        self.assertEqual("review_required", report["status"])
        self.assertEqual(["example", "pyinstaller", "setuptools"], [p["name"] for p in report["packages"]])
        notice = report["packages"][0]["notice_files"][0]
        self.assertEqual(hashlib.sha256(b"Test notice\n").hexdigest(), notice["sha256"])
        self.assertEqual(b"Test notice\n", (output / "example" / notice["path"]).read_bytes())
        self.assertIn((str(output / "Python-LICENSE.txt"), "third_party"), datas)
        self.assertTrue(all(p["review_required"] for p in report["packages"]))
        self.assertIn("NOT distribution approval", (output / "THIRD_PARTY_NOTICES.txt").read_text())

    def test_missing_wheel_notice_is_reported_not_invented(self):
        self.distribution.files = []
        _, _, report = self.collect()
        self.assertIn("example", report["missing_notice_packages"])
        self.assertEqual([], report["packages"][0]["notice_files"])

    def test_version_mismatch_is_explicit(self):
        self.distribution.version = "2.0"
        with self.assertRaisesRegex(ValueError, "Version mismatch"):
            self.collect()

    def test_missing_declared_notice_is_explicit(self):
        self.license.unlink()
        with self.assertRaisesRegex(FileNotFoundError, "Declared notice"):
            self.collect()

    def test_missing_metadata_is_explicit(self):
        self.distribution.read_text = lambda name: None
        with self.assertRaisesRegex(FileNotFoundError, "METADATA is missing"):
            self.collect()

    def test_missing_python_license_is_explicit(self):
        (self.root / "LICENSE.txt").unlink()
        with self.assertRaisesRegex(FileNotFoundError, "CPython license"):
            self.collect()

    def test_only_exact_pins_are_accepted(self):
        self.requirements.write_text("example>=1.0\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "exact package pin"):
            self.collect()

    def test_paths_outside_distribution_are_not_collected(self):
        self.distribution.files += [Path("../LICENSE.txt"), self.root / "LICENSE.txt"]
        _, _, report = self.collect()
        self.assertEqual(1, len(report["packages"][0]["notice_files"]))
