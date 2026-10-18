"""Install in the catalog's automation directory; uses only Python's stdlib."""

import argparse
import hashlib
from io import BytesIO
import json
import os
from pathlib import PurePosixPath
import re
from time import monotonic
import urllib.error
import urllib.request
from zipfile import ZipFile


REPOSITORY = "pernerch/botstudio-catalog"
MAX_ARCHIVE = 100 * 1024 * 1024
MAX_EXPANDED = 512 * 1024 * 1024
MAX_JSON = 2 * 1024 * 1024


def require(condition, message):
    if not condition:
        raise ValueError(message)


def load_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, "duplicate JSON keys are not accepted")
            result[key] = value
        return result
    return json.loads(raw, object_pairs_hook=pairs)


def account(value):
    require(isinstance(value, dict), "invalid account")
    require(set(value) == {"provider", "account_id", "login"}, "invalid account fields")
    require(value["provider"] == "github", "invalid account provider")
    require(isinstance(value["account_id"], str) and re.fullmatch(r"[1-9][0-9]{0,19}", value["account_id"]), "invalid account ID")
    login = value["login"]
    require(isinstance(login, str) and re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?", login) and "--" not in login, "invalid account login")
    return value["account_id"]


def validate_package(raw, descriptor, actor_id):
    """Never extract, import or execute any submitted file."""
    require(0 < len(raw) <= MAX_ARCHIVE, "archive size limit exceeded")
    require(isinstance(descriptor, dict) and set(descriptor) == {
        "schema_version", "repository", "game_id", "package_id", "package_version",
        "submitter", "archive_sha256", "archive_size", "archive_path", "metadata_path", "fork_of",
    }, "invalid submission descriptor")
    require(type(descriptor["schema_version"]) is int and descriptor["schema_version"] == 1, "invalid descriptor version")
    require(descriptor["repository"] == REPOSITORY, "wrong catalog")
    require(type(actor_id) is int and actor_id > 0 and account(descriptor["submitter"]) == str(actor_id), "PR actor does not match submitter")
    require(type(descriptor["archive_size"]) is int and descriptor["archive_size"] == len(raw), "archive size mismatch")
    require(descriptor["archive_sha256"] == hashlib.sha256(raw).hexdigest(), "archive digest mismatch")
    with ZipFile(BytesIO(raw)) as archive:
        entries = archive.infolist()
        require(len(entries) <= 10000 and sum(item.file_size for item in entries) <= MAX_EXPANDED, "ZIP expansion limit exceeded")
        names = [entry.filename for entry in entries]
        require(len(names) == len({name.casefold() for name in names}), "duplicate ZIP paths")
        for entry in entries:
            name = entry.filename
            path = PurePosixPath(name)
            require(
                name and "\\" not in name and ":" not in name and not path.is_absolute()
                and all(part not in ("", ".", "..") and not part.endswith((" ", ".")) for part in name.rstrip("/").split("/")),
                "unsafe ZIP path",
            )
            require(not entry.flag_bits & 1, "encrypted ZIP content is not supported")
            require((entry.external_attr >> 16) & 0o170000 not in (0o120000, 0o060000, 0o020000), "ZIP links/devices are not supported")
            require(name in ("manifest.json", "checksums.json") or name.startswith("content/"), "unexpected ZIP file")
        require(archive.getinfo("manifest.json").file_size <= MAX_JSON and archive.getinfo("checksums.json").file_size <= MAX_JSON, "package JSON metadata too large")
        manifest = load_json(archive.read("manifest.json"))
        checksums = load_json(archive.read("checksums.json"))
        require(isinstance(manifest, dict) and manifest.get("format") == "botstudio-package", "invalid package format")
        require(type(manifest.get("format_version")) is int and manifest["format_version"] == 1, "invalid package version")
        require(all(key in manifest for key in ("package_name", "description", "game", "contents", "package_metadata")), "missing manifest fields")
        details = manifest["package_metadata"]
        require(isinstance(details, dict), "invalid package metadata")
        for key in ("package_id", "package_version", "author", "license_id", "license_text"):
            require(isinstance(details.get(key), str) and details[key].strip(), "publication metadata is incomplete")
        require(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", details["package_id"]), "invalid package ID")
        require(re.fullmatch(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", details["package_version"]), "noncanonical version")
        require(details["license_id"].strip().upper() not in ("UNSPECIFIED", "NOASSERTION", "NONE") and details.get("rights_confirmed") is True, "declared publication rights are required")
        require(account(details.get("publisher")) == str(actor_id), "PR actor is not the declared publisher")
        for key in ("original_author", "modified_by"):
            if key in details:
                account(details[key])
        for key in ("support", "third_party_notices"):
            require(key not in details or isinstance(details[key], str), "invalid publication text")
        parent = details.get("fork_of")
        require(parent == descriptor["fork_of"], "fork declaration mismatch")
        if parent is not None:
            require(isinstance(parent, dict) and set(parent) == {"publisher", "package_id", "package_version"}, "invalid fork declaration")
            parent_id = account(parent["publisher"])
            require(isinstance(parent["package_id"], str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", parent["package_id"]), "invalid parent package")
            require(isinstance(parent["package_version"], str) and parent["package_version"].strip(), "invalid parent version")
            require((parent_id, parent["package_id"]) != (str(actor_id), details["package_id"]), "fork cannot replace its own identity")
        require(isinstance(manifest["game"], dict), "invalid Game")
        game = manifest["game"].get("id")
        require(isinstance(game, str) and game.strip(), "missing Game ID")
        require((game, details["package_id"], details["package_version"]) == (
            descriptor["game_id"], descriptor["package_id"], descriptor["package_version"],
        ), "descriptor does not match archive")
        game_key = hashlib.sha256(game.encode()).hexdigest()
        package_key = hashlib.sha256(details["package_id"].encode()).hexdigest()
        prefix = f"games/{game_key}/publishers/{actor_id}/packages/{package_key}/{details['package_version']}"
        require(descriptor["archive_path"] == f"{prefix}/package.botstudio.zip" and descriptor["metadata_path"] == f"{prefix}/submission.json", "invalid submission paths")
        content = {name for name in names if name.startswith("content/") and not name.endswith("/")}
        require(content and isinstance(checksums, dict) and set(checksums) == content, "checksum coverage mismatch")
        require(manifest.get("checksums", checksums) == checksums, "manifest checksum mismatch")
        for name in content:
            digest = hashlib.sha256()
            with archive.open(name) as source:
                for chunk in iter(lambda: source.read(65536), b""):
                    digest.update(chunk)
            require(digest.hexdigest() == checksums[name], "content checksum mismatch")
    return descriptor


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, file, code, message, headers, url):
        return None


class GitHub:
    def __init__(self, token):
        self.token = token
        self.opener = urllib.request.build_opener(NoRedirect)

    def request(self, method, path, data=None, *, raw=False, upload=False, missing=False):
        require(path.startswith("/") and not path.startswith("//"), "invalid API path")
        root = "https://uploads.github.com" if upload else "https://api.github.com"
        headers = {
            "Authorization": f"Bearer {self.token}", "User-Agent": "BotStudio-Catalog",
            "X-GitHub-Api-Version": "2022-11-28",
            "Accept": "application/vnd.github.raw+json" if raw else "application/vnd.github+json",
            "Content-Type": "application/zip" if upload else "application/json",
        }
        body = data if upload else json.dumps(data).encode() if data is not None else None
        request = urllib.request.Request(root + path, body, headers, method=method)
        started = monotonic()
        try:
            with self.opener.open(request, timeout=30) as response:
                limit = MAX_ARCHIVE if raw else MAX_JSON
                result = bytearray()
                while True:
                    chunk = response.read(65536)
                    if not chunk:
                        break
                    require(monotonic() - started <= 120 and len(result) + len(chunk) <= limit, "API response exceeds limits")
                    result.extend(chunk)
        except urllib.error.HTTPError as error:
            code = error.code
            error.close()
            if missing and code == 404:
                return None
            raise ValueError(f"GitHub catalog automation HTTP {code}; inspect partial state before retry") from None
        except urllib.error.URLError:
            raise ValueError("GitHub request failed; inspect partial state before retry") from None
        return bytes(result) if raw else load_json(result)


def validate_pr(api, number, repository_id, *, allow_merged=False):
    root = f"/repositories/{repository_id}"
    repo = api.request("GET", root)
    require(repo["id"] == repository_id and repo["full_name"] == REPOSITORY and repo["private"] is False, "catalog identity mismatch")
    pr = api.request("GET", f"{root}/pulls/{number}")
    require(pr["state"] == "open" or allow_merged and pr.get("merged") is True, "PR is not open or merged")
    require(not pr["draft"] and pr["base"]["repo"]["id"] == repository_id and pr["base"]["ref"] == repo["default_branch"], "invalid PR base/draft")
    actor = pr["user"]["id"]
    require(type(actor) is int and actor > 0 and pr["user"]["type"] == "User", "invalid PR author")
    head = pr["head"]["sha"]
    require(isinstance(head, str) and re.fullmatch(r"[0-9a-f]{40}", head), "invalid PR head")
    files = api.request("GET", f"{root}/pulls/{number}/files?per_page=3")
    require(pr["changed_files"] == 2 and isinstance(files, list) and len(files) == 2, "PR must add exactly two files")
    require(all(item["status"] == "added" for item in files), "existing files cannot be changed")
    names = {item["filename"] for item in files}
    metadata = [name for name in names if re.fullmatch(r"games/[0-9a-f]{64}/publishers/[1-9][0-9]{0,19}/packages/[0-9a-f]{64}/[0-9]+\.[0-9]+\.[0-9]+/submission\.json", name)]
    require(len(metadata) == 1, "invalid metadata path")
    metadata_path = metadata[0]
    archive_path = metadata_path.removesuffix("submission.json") + "package.botstudio.zip"
    require(names == {metadata_path, archive_path}, "unexpected PR paths")
    # Read tree entries by exact head, never check out or execute contributor files.
    commit = api.request("GET", f"{root}/git/commits/{head}")
    tree_sha = commit["tree"]["sha"]
    require(isinstance(tree_sha, str) and re.fullmatch(r"[0-9a-f]{40}", tree_sha), "invalid PR tree")
    tree = api.request("GET", f"{root}/git/trees/{tree_sha}?recursive=1")
    require(tree.get("truncated") is False, "catalog tree truncated; shard catalog before growing further")
    entries = {item["path"]: item for item in tree["tree"] if item["path"] in names}
    require(set(entries) == names and all(item["mode"] == "100644" and item["type"] == "blob" for item in entries.values()), "PR files must be regular blobs")
    descriptor_bytes = api.request("GET", f"{root}/git/blobs/{entries[metadata_path]['sha']}", raw=True)
    require(len(descriptor_bytes) <= MAX_JSON, "descriptor too large")
    raw = api.request("GET", f"{root}/git/blobs/{entries[archive_path]['sha']}", raw=True)
    descriptor = validate_package(raw, load_json(descriptor_bytes), actor)
    if pr["state"] == "open":
        # Contents ref omitted deliberately: check the current default branch.
        for name in names:
            require(api.request("GET", f"{root}/contents/{name}", missing=True) is None, "version already exists")
    fresh = api.request("GET", f"{root}/pulls/{number}")
    require(fresh["head"]["sha"] == head and fresh["user"]["id"] == actor and fresh["state"] == pr["state"], "PR changed during validation")
    return pr, descriptor, raw


def publish(api, number, repository_id):
    pr, descriptor, raw = validate_pr(api, number, repository_id, allow_merged=True)
    root = f"/repositories/{repository_id}"
    if not pr.get("merged"):
        result = api.request("PUT", f"{root}/pulls/{number}/merge", {
            "sha": pr["head"]["sha"], "merge_method": "merge",
            "commit_title": f"Accept catalog submission #{number}",
        })
        require(result.get("merged") is True, "GitHub did not merge the validated head")
        merged = result["sha"]
    else:
        merged = pr["merge_commit_sha"]
    require(isinstance(merged, str) and re.fullmatch(r"[0-9a-f]{40}", merged), "invalid merge commit")
    # Confirm the merged regular blobs contain exactly the checked data.
    for path, expected in (
        (descriptor["archive_path"], raw),
        (descriptor["metadata_path"], None),
    ):
        content = api.request("GET", f"{root}/contents/{path}?ref={merged}")
        require(content["type"] == "file", "merged content is not a file")
        actual = api.request("GET", f"{root}/git/blobs/{content['sha']}", raw=True)
        require(actual == expected if expected is not None else load_json(actual) == descriptor, "merged bytes differ")
    tag = f"catalog-pr-{number}-{pr['head']['sha']}"
    # Do not mutate or republish an existing draft/release on uncertain retries.
    require(api.request("GET", f"{root}/releases/tags/{tag}", missing=True) is None, "release already exists; inspect manually")
    require(api.request("GET", f"{root}/git/ref/tags/{tag}", missing=True) is None, "release tag already exists; inspect manually")
    api.request("POST", f"{root}/git/refs", {"ref": f"refs/tags/{tag}", "sha": merged})
    release = api.request("POST", f"{root}/releases", {
        "tag_name": tag, "target_commitish": merged, "draft": True,
        "name": f"{descriptor['game_id']} | {descriptor['package_id']} {descriptor['package_version']}"[:200],
        "body": json.dumps({
            "schema_version": 1, "pull_request": number, "head": pr["head"]["sha"],
            "submitter": descriptor["submitter"], "archive_sha256": descriptor["archive_sha256"],
            "submission": descriptor,
            "notice": "PR author/account binding checked; other authorship/license claims remain declared.",
        }, sort_keys=True),
    })
    release_id = release["id"]
    require(type(release_id) is int and release_id > 0, "invalid release ID")
    require(release.get("draft") is True and release.get("tag_name") == tag, "unexpected draft release")
    tag_ref = api.request("GET", f"{root}/git/ref/tags/{tag}")
    require(tag_ref["object"]["type"] == "commit" and tag_ref["object"]["sha"] == merged, "release tag does not match merge")
    repo = api.request("GET", root)
    require(repo["id"] == repository_id and repo["full_name"] == REPOSITORY, "catalog changed before asset upload")
    asset = api.request(
        "POST", f"/repos/{REPOSITORY}/releases/{release_id}/assets?name=package.botstudio.zip", upload=True, data=raw,
    )
    require(asset["state"] == "uploaded" and asset["size"] == len(raw) and asset.get("digest") == "sha256:" + descriptor["archive_sha256"], "release asset integrity mismatch; draft retained")
    published = api.request("PATCH", f"{root}/releases/{release_id}", {"draft": False})
    require(published.get("id") == release_id and published.get("draft") is False, "release publication was not confirmed")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("validate", "publish"))
    parser.add_argument("--pr", required=True, type=int)
    args = parser.parse_args()
    require(args.pr > 0 and os.environ["GITHUB_REPOSITORY"] == REPOSITORY, "unexpected repository/PR")
    repository_id = int(os.environ["GITHUB_REPOSITORY_ID"])
    require(repository_id > 0, "invalid repository ID")
    api = GitHub(os.environ["GH_TOKEN"])
    if args.mode == "validate":
        validate_pr(api, args.pr, repository_id, allow_merged=True)
    else:
        publish(api, args.pr, repository_id)
    print("Catalog operation completed successfully.")


if __name__ == "__main__":
    main()
