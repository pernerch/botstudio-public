"""Collect distribution evidence, not legal approval, from the build interpreter."""

import hashlib
from importlib import metadata
import json
from pathlib import Path
import platform
import re
import sys


def pinned_packages(requirements: Path) -> list[tuple[str, str]]:
    packages = []
    for line in requirements.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = re.fullmatch(r"([A-Za-z0-9_.-]+)==([A-Za-z0-9_.+-]+)", line)
        if not match:
            raise ValueError(f"Expected an exact package pin: {line}")
        packages.append((match[1], match[2]))
    return packages


def is_notice(path: Path) -> bool:
    name = path.name.lower()
    return (
        any(word in name for word in ("license", "licence", "copying", "notice", "copyright"))
        and path.suffix.lower() in ("", ".txt", ".md", ".rst")
    )


def distribution_materials(requirements: Path) -> dict:
    path = requirements.parent / "distribution_materials.json"
    materials = json.loads(path.read_text(encoding="utf-8"))
    if materials["schema_version"] != 1:
        raise ValueError("Unsupported distribution materials schema")
    pins = dict(pinned_packages(requirements))
    for item in materials["license_files"]:
        filename = item["file"]
        if Path(filename).name != filename:
            raise ValueError(f"Invalid supplemental license path: {filename}")
        source = requirements.parent / "licenses" / filename
        if hashlib.sha256(source.read_bytes()).hexdigest() != item["sha256"]:
            raise ValueError(f"Supplemental license hash mismatch: {filename}")
        for name, version in item["packages"].items():
            if pins.get(name) != version:
                raise ValueError(f"Supplemental license version mismatch: {name}")
    models = materials["models"]
    if pins.get(models["distribution"]) != models["version"]:
        raise ValueError("Model provenance version mismatch")
    distribution = metadata.distribution(models["distribution"])
    if distribution.version != models["version"]:
        raise ValueError("Installed model distribution version mismatch")
    directory = Path(distribution.locate_file("rapidocr/models"))
    expected = {item["name"] for item in models["files"]}
    if {path.name for path in directory.glob("*.onnx")} != expected:
        raise ValueError("Bundled OCR model set differs from reviewed provenance")
    for model in models["files"]:
        if Path(model["name"]).name != model["name"]:
            raise ValueError("Invalid model filename")
        source = directory / model["name"]
        if (source.stat().st_size != model["size_bytes"] or
                hashlib.sha256(source.read_bytes()).hexdigest() != model["sha256"]):
            raise ValueError(f"Model provenance hash mismatch: {model['name']}")
    return materials


def collect_notices(requirements: Path, output: Path) -> list[tuple[str, str]]:
    materials = distribution_materials(requirements)
    output.mkdir(parents=True, exist_ok=True)
    records = []
    datas = []
    packages = pinned_packages(requirements)
    # The bootloader is shipped, and setuptools code is collected by hooks.
    packages.extend((name, metadata.version(name)) for name in ("pyinstaller", "setuptools"))
    for name, expected_version in packages:
        distribution = metadata.distribution(name)
        if distribution.version != expected_version:
            raise ValueError(f"Version mismatch for {name}: {distribution.version} != {expected_version}")
        directory = output / name
        directory.mkdir(parents=True, exist_ok=True)
        info = distribution.metadata
        metadata_file = directory / "METADATA.txt"
        metadata_text = distribution.read_text("METADATA")
        if metadata_text is None:
            raise FileNotFoundError(f"Distribution METADATA is missing: {name}")
        metadata_file.write_text(metadata_text, encoding="utf-8")
        datas.append((str(metadata_file), f"third_party/{name}"))
        notices = []
        for entry in distribution.files or []:
            relative = Path(str(entry))
            if relative.is_absolute() or ".." in relative.parts or not is_notice(relative):
                continue
            source = Path(distribution.locate_file(entry))
            if not source.is_file():
                raise FileNotFoundError(f"Declared notice file is missing: {source}")
            payload = source.read_bytes()
            target = directory / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)
            datas.append((str(target), str(Path("third_party") / name / relative.parent)))
            notices.append({
                "path": relative.as_posix(),
                "sha256": hashlib.sha256(payload).hexdigest(),
            })
        for item in materials["license_files"]:
            if name not in item["packages"]:
                continue
            relative = Path("upstream") / item["file"]
            target = directory / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((requirements.parent / "licenses" / item["file"]).read_bytes())
            datas.append((str(target), f"third_party/{name}/upstream"))
            notices.append({
                "path": relative.as_posix(),
                "sha256": item["sha256"],
                "source": item["source"],
            })
        records.append({
            "name": name,
            "version": distribution.version,
            "declared_license": info.get("License-Expression") or info.get("License"),
            "license_classifiers": [
                value for value in info.get_all("Classifier", []) if value.startswith("License ::")
            ],
            "project_urls": info.get_all("Project-URL", []),
            "notice_files": notices,
            "review_required": True,
        })
    missing = [item["name"] for item in records if not item["notice_files"]]
    if missing:
        raise ValueError(f"Missing third-party license texts: {', '.join(missing)}")
    for filename in ("distribution_materials.json", "DISTRIBUTION.md"):
        target = output / filename
        target.write_bytes((requirements.parent / filename).read_bytes())
        datas.append((str(target), "third_party"))
    python_license = Path(sys.base_prefix) / "LICENSE.txt"
    if not python_license.is_file():
        raise FileNotFoundError(f"CPython license is missing: {python_license}")
    python_target = output / "Python-LICENSE.txt"
    python_target.write_bytes(python_license.read_bytes())
    datas.append((str(python_target), "third_party"))
    report = output / "inventory.json"
    report.write_text(json.dumps({
        "schema_version": 1,
        "status": "review_required",
        "python_version": platform.python_version(),
        "packages": records,
        "missing_notice_packages": missing,
        "model_provenance": materials["models"],
        "limitations": [
            "Declared wheel licenses are evidence, not distribution approval.",
            "Native dependencies, model weights, fonts and other assets require separate review.",
            "Packages listed include the pinned dependency closure, not only imported modules.",
        ],
    }, indent=2) + "\n", encoding="utf-8")
    datas.append((str(report), "third_party"))
    index = output / "THIRD_PARTY_NOTICES.txt"
    lines = [
        "BotStudio - third-party distribution evidence",
        "",
        "This directory preserves wheel notices and version/hash-bound upstream supplements.",
        "inventory.json records their relative paths and SHA-256 hashes.",
        "Python-LICENSE.txt contains the license supplied with the Python interpreter.",
        "",
        "IMPORTANT: This collection is incomplete and is NOT distribution approval.",
        "Model provenance and exact model hashes are recorded in distribution_materials.json.",
        "Qt/native attribution and complete dependency source delivery still need review.",
        "See DISTRIBUTION.md for source locations, LGPL replacement instructions and limits.",
        "The repository distribution_review.json remains the public release approval gate.",
        "",
        "Collected distributions (including dependency closure and bootloader/tool notices):",
    ]
    for record in records:
        lines.append(f"- {record['name']} {record['version']}: {len(record['notice_files'])} notice files")
    index.write_text("\n".join(lines) + "\n", encoding="utf-8")
    datas.append((str(index), "third_party"))
    return datas
