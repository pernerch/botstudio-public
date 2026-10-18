"""Acquire pinned upstream sources and preserve their attribution bytes.

--resolve is a maintainer operation: review and commit the resulting lock.
Normal builds only accept the already locked URLs, versions and SHA256 values.
"""

import argparse
from hashlib import sha256
import json
from pathlib import Path, PurePosixPath
import posixpath
import re
import shutil
import tarfile
import time
from urllib.parse import urlparse
from urllib.request import Request, urlopen
import zipfile

from release.collect_notices import pinned_packages


ROOT = Path(__file__).resolve().parents[1]
MAX_DOWNLOAD = 1024 * 1024 * 1024
MAX_TEXT = 16 * 1024 * 1024


def source_pins(requirements: Path) -> dict[str, str]:
    pins = dict(pinned_packages(requirements))
    for line in requirements.with_name("requirements-build.txt").read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.startswith("#") or line == "-r requirements-runtime.txt":
            continue
        match = re.fullmatch(r"([A-Za-z0-9_.-]+)==([A-Za-z0-9_.+-]+)", line.strip())
        if not match:
            raise ValueError(f"Expected exact build package pin: {line}")
        pins[match[1]] = match[2]
    return pins


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return sha256_file(stream)


def sha256_file(stream) -> str:
    result = sha256()
    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
        result.update(chunk)
    return result.hexdigest()


def fetch(url: str, target: Path) -> None:
    if urlparse(url).scheme != "https":
        raise ValueError(f"Source URL must use HTTPS: {url}")
    partial = target.with_suffix(target.suffix + ".partial")
    started = time.monotonic()
    created = False
    try:
        with urlopen(Request(url, headers={"User-Agent": "BotStudio-source-evidence/1"}), timeout=45) as response:
            if urlparse(response.url).scheme != "https":
                raise ValueError(f"Insecure source redirect: {response.url}")
            size = 0
            with partial.open("xb") as output:
                created = True
                while chunk := response.read(1024 * 1024):
                    size += len(chunk)
                    if size > MAX_DOWNLOAD or time.monotonic() - started > 300:
                        raise TimeoutError(f"Source download limit exceeded: {url}")
                    output.write(chunk)
        partial.replace(target)
    finally:
        if created:
            partial.unlink(missing_ok=True)


def json_from_url(url: str) -> dict:
    with urlopen(url, timeout=45) as response:
        data = response.read(MAX_TEXT + 1)
    if len(data) > MAX_TEXT:
        raise ValueError(f"Metadata too large: {url}")
    return json.loads(data)


def resolve(lock_path: Path, cache: Path, requirements: Path) -> None:
    """Explicitly refresh source identities, never invoked by the build."""
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    archives = [item for item in lock["archives"] if item.get("scope") != "pypi-sdist"]
    covered = {
        name for item in archives if item["id"] not in ("geos", "openblas")
        for name in item["packages"]
    }
    for name, version in source_pins(requirements).items():
        if name in covered:
            continue
        metadata_url = f"https://pypi.org/pypi/{name}/{version}/json"
        sources = [item for item in json_from_url(metadata_url)["urls"] if item["packagetype"] == "sdist"]
        if len(sources) != 1:
            raise ValueError(f"Expected one exact-version sdist: {name}=={version}")
        source = sources[0]
        archives.append({
            "id": name, "version": version, "packages": {name: version},
            "url": source["url"], "filename": source["filename"],
            "sha256": source["digests"]["sha256"], "size_bytes": source["size"],
            "checksum_source": metadata_url, "scope": "pypi-sdist",
        })
    cache.mkdir(parents=True, exist_ok=True)
    for item in archives:
        path = cache / item["filename"]
        if not path.exists():
            fetch(item["url"], path)
        actual = digest(path)
        if item.get("sha256") and actual != item["sha256"]:
            raise ValueError(f"Source SHA256 mismatch: {item['id']}")
        item["sha256"] = actual
        item["size_bytes"] = path.stat().st_size
        item.setdefault("checksum_source", "SHA256 of archive retrieved over official upstream HTTPS; no separate publisher digest")
        print(f"LOCKED {item['id']} {item['version']} {item['size_bytes']}", flush=True)
    lock["archives"] = archives
    lock_path.write_text(json.dumps(lock, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")


def safe_name(name: str) -> str:
    path = PurePosixPath(name)
    if "\\" in name or ":" in name or path.is_absolute() or ".." in path.parts:
        raise ValueError(f"Unsafe source archive path: {name}")
    return path.as_posix()


def is_material(name: str) -> bool:
    path = PurePosixPath(name)
    base = path.name.casefold()
    return bool(
        re.search(r"license|licence|copying|copyright|notice", base)
        or base in (
            "qt_attribution.json", ".gitmodules", "readme", "readme.md", "readme.rst",
            "pkg-info", "pyproject.toml", "setup.py", "setup.cfg", "cmakelists.txt",
        )
        or "LICENSES" in path.parts
        or "building_from_source" in path.parts
    )


def source_materials(path: Path) -> dict[str, bytes]:
    """Read regular attribution/build files, never extract source archive paths."""
    result = {}
    with tarfile.open(path, "r:*") as archive:
        members = archive.getmembers()
        by_name = {member.name: member for member in members}
        selected = {member.name for member in members if member.isfile() and is_material(member.name)}
        for member in members:
            if not member.isfile() or PurePosixPath(member.name).name != "qt_attribution.json":
                continue
            if member.size > MAX_TEXT:
                raise ValueError(f"Attribution metadata too large: {member.name}")
            stream = archive.extractfile(member)
            if stream is None:
                raise ValueError(f"Attribution cannot be read: {member.name}")
            with stream:
                # Qt's attribution scanner accepts literal newlines in fields.
                records = json.loads(stream.read(), strict=False)
            for record in records if isinstance(records, list) else [records]:
                for key in ("LicenseFile", "LicenseFiles"):
                    files = record.get(key, [])
                    for reference in [files] if isinstance(files, str) else files:
                        resolved = posixpath.normpath(posixpath.join(posixpath.dirname(member.name), reference))
                        safe_name(resolved)
                        if resolved not in by_name or not by_name[resolved].isfile():
                            raise ValueError(f"Missing referenced Qt license: {member.name}: {reference}")
                        selected.add(resolved)
        for name in sorted(selected):
            safe_name(name)
            member = by_name[name]
            if member.size > MAX_TEXT:
                raise ValueError(f"Source material too large: {name}")
            stream = archive.extractfile(member)
            if stream is None:
                raise ValueError(f"Source material cannot be read: {name}")
            with stream:
                result[name] = stream.read()
    if not result:
        raise ValueError(f"Source has no attribution/build material: {path.name}")
    return result


def validate_lock(lock: dict, pins: dict[str, str]) -> None:
    if lock["schema_version"] != 1 or not lock["archives"]:
        raise ValueError("Unsupported or empty dependency source manifest")
    names = set()
    identifiers = set()
    covered = set()
    for item in lock["archives"]:
        for key in ("id", "filename"):
            value = item[key]
            if not re.fullmatch(r"[A-Za-z0-9_.+-]+", value):
                raise ValueError(f"Unsafe source {key}: {value}")
        if item["filename"] in names:
            raise ValueError("Duplicate source filename")
        names.add(item["filename"])
        if item["id"] in identifiers:
            raise ValueError("Duplicate source identifier")
        identifiers.add(item["id"])
        if not re.fullmatch(r"[a-f0-9]{64}", item.get("sha256", "")) or item.get("size_bytes", 0) <= 0:
            raise ValueError(f"Unresolved source digest/size: {item['id']}")
        if urlparse(item["url"]).scheme != "https":
            raise ValueError("Source URL must use HTTPS")
        for name, version in item["packages"].items():
            if pins.get(name) != version:
                raise ValueError(f"Source package version mismatch: {name}")
            covered.add(name)
    if set(pins) - covered:
        raise ValueError(f"Source coverage missing: {sorted(set(pins) - covered)}")


def prepare(lock_path: Path, cache: Path, output: Path, requirements: Path) -> dict:
    lock_bytes = lock_path.read_bytes()
    lock = json.loads(lock_bytes)
    pins = source_pins(requirements)
    validate_lock(lock, pins)
    if output.exists():
        _, report = bundled_materials(lock_path, output)
        for item in lock["archives"]:
            source = cache / item["filename"]
            if source.stat().st_size != item["size_bytes"] or digest(source) != item["sha256"]:
                raise ValueError(f"Cached source SHA256/size mismatch: {item['id']}")
        print("VERIFIED existing dependency materials and source archives", flush=True)
        return report
    cache.mkdir(parents=True, exist_ok=True)
    output.mkdir(parents=True)
    try:
        records = []
        for item in lock["archives"]:
            source = cache / item["filename"]
            if not source.exists():
                fetch(item["url"], source)
            if source.stat().st_size != item["size_bytes"] or digest(source) != item["sha256"]:
                raise ValueError(f"Source SHA256/size mismatch: {item['id']}")
            notices = []
            for name, payload in source_materials(source).items():
                # Keep installed paths below MAX_PATH while retaining the exact
                # upstream member name in the index and full source archive.
                short_name = sha256(name.encode("utf-8")).hexdigest()[:20] + "-" + PurePosixPath(name).name[:60]
                relative = PurePosixPath(item["id"]) / short_name
                target = output / "notices" / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(payload)
                notices.append({
                    "path": relative.as_posix(), "archive_path": name,
                    "sha256": sha256(payload).hexdigest(),
                })
            records.append(dict(item, materials=notices))
            print(f"VERIFIED {item['id']}: {len(notices)} attribution/build files", flush=True)
        report = {
            "schema_version": 2, "source_lock_sha256": sha256(lock_bytes).hexdigest(),
            "complete_corresponding_source_approved": False,
            "limitations": [
                "Version-matched source acquisition is not proof of all wheel build patches/configuration.",
                "Upstream commit archives may omit external submodules and downloaded native build dependencies.",
                "All available source-tree notices are retained, including optional or unshipped features.",
                "Independent compatible-library replacement validation and final native mapping remain separate.",
            ],
            "archives": records,
        }
        (output / "source-index.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        shutil.copyfile(lock_path, output / "dependency_sources.json")
        return report
    except BaseException:
        shutil.rmtree(output)
        raise


def bundled_materials(lock_path: Path, output: Path) -> tuple[list[tuple[str, str]], dict]:
    lock_bytes = lock_path.read_bytes()
    lock = json.loads(lock_bytes)
    report = json.loads((output / "source-index.json").read_text(encoding="utf-8"))
    if (
        report["schema_version"] != 2
        or report["source_lock_sha256"] != sha256(lock_bytes).hexdigest()
        or report["complete_corresponding_source_approved"] is not False
        or (output / "dependency_sources.json").read_bytes() != lock_bytes
    ):
        raise ValueError("Prepared dependency source evidence does not match the locked source")
    locked = {item["id"]: item for item in lock["archives"]}
    if len(report["archives"]) != len(locked) or {item["id"] for item in report["archives"]} != set(locked):
        raise ValueError("Prepared dependency source coverage mismatch")
    datas = []
    for item in report["archives"]:
        if {key: value for key, value in item.items() if key != "materials"} != locked[item["id"]]:
            raise ValueError(f"Prepared source identity mismatch: {item['id']}")
        if not item["materials"]:
            raise ValueError(f"Missing source material: {item['id']}")
        for notice in item["materials"]:
            relative = safe_name(notice["path"])
            source = output / "notices" / relative
            if source.is_symlink() or digest(source) != notice["sha256"]:
                raise ValueError(f"Prepared source notice hash mismatch: {relative}")
            datas.append((str(source), str(PurePosixPath("third_party/source-notices") / PurePosixPath(relative).parent)))
    datas.extend((str(output / name), "third_party") for name in ("source-index.json", "dependency_sources.json"))
    return datas, report


def source_archive(lock_path: Path, cache: Path, output: Path, destination: Path) -> None:
    datas, report = bundled_materials(lock_path, output)
    if destination.exists():
        raise FileExistsError(f"Refusing to replace source delivery: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    stream = destination.open("xb")
    try:
        with stream, zipfile.ZipFile(stream, "w", allowZip64=True) as archive:
            for item in report["archives"]:
                path = cache / item["filename"]
                if path.stat().st_size != item["size_bytes"] or digest(path) != item["sha256"]:
                    raise ValueError(f"Source archive changed: {item['id']}")
                archive.write(path, "sources/" + item["filename"], compress_type=zipfile.ZIP_STORED)
            for path, target in datas:
                relative = PurePosixPath(target).relative_to("third_party") / Path(path).name
                archive.write(path, relative.as_posix(), compress_type=zipfile.ZIP_DEFLATED)
            archive.write(lock_path.with_name("DISTRIBUTION.md"), "DISTRIBUTION.md", compress_type=zipfile.ZIP_DEFLATED)
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
    print(f"SOURCE DELIVERY: {destination} SHA256={digest(destination)}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--resolve", action="store_true")
    parser.add_argument("--lock", type=Path, default=ROOT / "release" / "dependency_sources.json")
    parser.add_argument("--cache", type=Path, default=ROOT / "build" / "dependency-sources")
    parser.add_argument("--output", type=Path, default=ROOT / "build" / "dependency-materials")
    parser.add_argument("--archive", type=Path, help="Also create a standalone source and notice ZIP; never replace an existing file")
    args = parser.parse_args()
    if args.resolve and args.archive:
        parser.error("--archive cannot be combined with --resolve")
    requirements = ROOT / "release" / "requirements-runtime.txt"
    if args.resolve:
        resolve(args.lock, args.cache, requirements)
    else:
        prepare(args.lock, args.cache, args.output, requirements)
        if args.archive:
            source_archive(args.lock, args.cache, args.output, args.archive)
    print("DEPENDENCY SOURCE RESULT: SUCCESS", flush=True)


if __name__ == "__main__":
    main()
