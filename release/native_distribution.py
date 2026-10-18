from pathlib import Path
import argparse
import hashlib
import json
import re


UNUSED_NATIVE_FILES = frozenset({
    "qpdf.dll",
    "qt6pdf.dll",
    "opencv_videoio_ffmpeg500_64.dll",
})


def exclude_unused_native_entries(entries):
    return [
        entry for entry in entries
        if Path(str(entry[0]).replace("\\", "/")).name.casefold() not in UNUSED_NATIVE_FILES
    ]


def native_owner(path: str) -> str:
    normalized = path.replace("\\", "/").lower()
    if normalized in ("botstudio.exe", "botstudiowatchdog.exe"):
        return "botstudio"
    if not normalized.startswith("_internal/"):
        raise ValueError(f"Unmapped native distribution file: {path}")
    relative = normalized.removeprefix("_internal/")
    for prefixes, owner in (
        (("pyside6/",), "pyside6-essentials"),
        (("shiboken6/",), "shiboken6"),
        (("numpy/", "numpy.libs/"), "numpy"),
        (("shapely/", "shapely.libs/"), "shapely"),
        (("pil/", "pillow.libs/"), "pillow"),
        (("onnxruntime/",), "onnxruntime"),
        (("cv2/",), "opencv-python"),
        (("charset_normalizer/",), "charset-normalizer"),
        (("pyclipper/",), "pyclipper"),
        (("yaml/", "_yaml/"), "pyyaml"),
        (("google/_upb/",), "protobuf"),
    ):
        if relative.startswith(prefixes):
            return owner
    if re.fullmatch(r"(?:api-ms-win-[\w-]+|ucrtbase|vcruntime140(?:_1)?|msvcp140(?:_[12])?)\.dll", relative):
        return "microsoft-runtime"
    if "/" not in relative and (
        relative in {
            "pyexpat.pyd", "select.pyd", "unicodedata.pyd", "_asyncio.pyd",
            "_bz2.pyd", "_ctypes.pyd", "_decimal.pyd", "_elementtree.pyd",
            "_hashlib.pyd", "_lzma.pyd", "_multiprocessing.pyd", "_overlapped.pyd",
            "_queue.pyd", "_socket.pyd", "_ssl.pyd", "_uuid.pyd", "_wmi.pyd", "_zstd.pyd",
        }
        or re.fullmatch(r"(?:python3(?:14)?|libcrypto-3|libssl-3|libffi-8)\.dll", relative)
    ):
        return "cpython"
    raise ValueError(f"Unmapped native distribution file: {path}")


def prepare_native_licenses(bundle: Path) -> dict:
    notices = bundle / "_internal" / "third_party"
    inventory = json.loads((notices / "inventory.json").read_text(encoding="utf-8"))
    packages = {item["name"]: item for item in inventory["packages"]}
    sources = json.loads((notices / "source-index.json").read_text(encoding="utf-8"))
    source_groups = {
        "pyside6-essentials": {"qtbase", "qtsvg", "qtimageformats", "pyside-setup", "mesa"},
        "shiboken6": {"pyside-setup"},
        "shapely": {"geos", "shapely-wheel-build"},
    }
    records = []
    for path in sorted(bundle.rglob("*")):
        if path.suffix.lower() not in (".dll", ".pyd", ".exe"):
            continue
        relative = path.relative_to(bundle).as_posix()
        owner = native_owner(relative)
        if owner == "botstudio":
            evidence = ["../licenses/BotStudio/LICENSE", "../licenses/BotStudio/LICENSE-NOTICE.txt"]
            evidence += ["pyinstaller/" + item["path"] for item in packages["pyinstaller"]["notice_files"]]
        elif owner in ("cpython", "microsoft-runtime"):
            evidence = ["Python-LICENSE.txt"]
        else:
            evidence = [owner + "/" + item["path"] for item in packages[owner]["notice_files"]]
        source_ids = sorted(source_groups.get(owner, set()))
        if source_ids:
            if not set(source_ids) <= {item["id"] for item in sources["archives"]}:
                raise ValueError(f"Missing native library source/notice group: {owner}")
            evidence.append("source-index.json")
        for name in evidence:
            if not (notices / name).is_file():
                raise FileNotFoundError(f"Missing native license evidence: {name}")
        with path.open("rb") as stream:
            checksum = hashlib.file_digest(stream, "sha256").hexdigest()
        records.append({
            "path": relative, "size_bytes": path.stat().st_size,
            "sha256": checksum, "owner": owner, "notice_files": evidence,
            "source_archive_ids": source_ids,
        })
    if not records:
        raise ValueError("Native distribution inventory is empty")
    report = {
        "schema_version": 1, "unmapped_files": [], "files": records,
        "scope": "Native file owners and accompanying notices; vendored components retain their individual licenses.",
    }
    (notices / "native-licenses.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Map shipped native files to retained license evidence.")
    parser.add_argument("--bundle", required=True, type=Path)
    args = parser.parse_args()
    result = prepare_native_licenses(args.bundle)
    print(f"NATIVE LICENSE MAP: SUCCESS ({len(result['files'])} files)", flush=True)
