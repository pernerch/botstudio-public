"""Install in the catalog's automation directory; uses only Python's stdlib."""

import argparse
import base64
import hashlib
from io import BytesIO
import json
import os
from pathlib import PurePosixPath
import re
from time import monotonic
import urllib.error
import urllib.request
from urllib.parse import quote
import unicodedata
import uuid
from zipfile import ZipFile


REPOSITORY = "pernerch/botstudio-catalog"
MAX_ARCHIVE = 100 * 1024 * 1024
MAX_EXPANDED = 512 * 1024 * 1024
MAX_JSON = 2 * 1024 * 1024
MAX_REGISTRY = 512 * 1024
MAX_REGISTRY_GAMES = 10000
REGISTRY_ATTEMPTS = 3
GAME_ISSUE_PREFIX = "[BotStudio Game] "
REGISTRY_BRANCH_PREFIX = "botstudio-game-registration/"


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


def game_name(value):
    require(
        isinstance(value, str) and 0 < len(value) <= 120
        and all(not unicodedata.category(char).startswith("C") for char in value),
        "Game name must be 1-120 plain characters without controls",
    )
    name = " ".join(unicodedata.normalize("NFKC", value).split())
    require(0 < len(name) <= 120, "invalid normalized Game name")
    return name


def normalized_game_name(value):
    return " ".join(game_name(value).casefold().split())


def android_package(value):
    require(
        isinstance(value, str) and len(value) <= 255
        and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+", value),
        "invalid Android package name",
    )
    return value


def validate_registry(value):
    require(
        isinstance(value, dict) and set(value) == {"schema_version", "games"}
        and type(value["schema_version"]) is int and value["schema_version"] == 1
        and isinstance(value["games"], list) and len(value["games"]) <= MAX_REGISTRY_GAMES,
        "invalid Game registry",
    )
    ids, names, packages = set(), set(), set()
    for game in value["games"]:
        require(isinstance(game, dict) and set(game) == {"id", "name", "android_packages"}, "invalid Game registry entry")
        game_id = game["id"]
        require(isinstance(game_id, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", game_id), "invalid registered Game ID")
        name = normalized_game_name(game["name"])
        require(game_id.casefold() not in ids and name not in names, "ambiguous Game registry identity")
        ids.add(game_id.casefold())
        names.add(name)
        aliases = game["android_packages"]
        require(isinstance(aliases, list) and len(aliases) <= 100, "invalid registered Android packages")
        for package in aliases:
            android_package(package)
            require(package not in packages, "ambiguous registered Android package")
            packages.add(package)
    return value


def read_registry(api, repository_id):
    # No PR/head ref: authorization always uses the current default branch.
    file = api.request("GET", f"/repositories/{repository_id}/contents/game-registry.json")
    require(
        isinstance(file, dict) and file.get("type") == "file"
        and file.get("encoding") == "base64" and isinstance(file.get("content"), str)
        and type(file.get("size")) is int and 0 < file["size"] <= MAX_REGISTRY
        and isinstance(file.get("sha"), str) and re.fullmatch(r"[0-9a-f]{40}", file["sha"]),
        "invalid Game registry file",
    )
    try:
        raw = base64.b64decode(file["content"].replace("\n", "").replace("\r", ""), validate=True)
    except ValueError:
        raise ValueError("invalid Game registry encoding") from None
    require(len(raw) == file["size"] and len(raw) <= MAX_REGISTRY, "Game registry size mismatch")
    return validate_registry(load_json(raw)), file["sha"]


def registration_request(issue):
    require(
        isinstance(issue, dict) and "pull_request" not in issue
        and issue.get("state") == "open" and isinstance(issue.get("user"), dict)
        and issue["user"].get("type") == "User"
        and type(issue["user"].get("id")) is int and issue["user"]["id"] > 0,
        "Game registration requires an open issue by an authenticated user",
    )
    body = issue.get("body")
    require(isinstance(body, str) and len(body.encode("utf-8")) <= 4096, "Game registration body is too large or missing")
    try:
        request = load_json(body)
    except (ValueError, TypeError, RecursionError):
        raise ValueError("Game registration body must be duplicate-free JSON") from None
    require(
        isinstance(request, dict) and set(request) == {"schema_version", "name", "android_package_name"}
        and type(request["schema_version"]) is int and request["schema_version"] == 1,
        "invalid Game registration fields",
    )
    name = game_name(request["name"])
    package = android_package(request["android_package_name"])
    title = issue.get("title")
    require(
        isinstance(title, str) and title.startswith(GAME_ISSUE_PREFIX)
        and len(title) <= len(GAME_ISSUE_PREFIX) + 120
        and normalized_game_name(title[len(GAME_ISSUE_PREFIX):]) == normalized_game_name(name),
        "Game registration title does not match its name",
    )
    return name, package


def registered_game(registry, name, package):
    by_name = next((game for game in registry["games"] if normalized_game_name(game["name"]) == normalized_game_name(name)), None)
    by_package = next((game for game in registry["games"] if package in game["android_packages"]), None)
    require(by_name is None or by_package is None or by_name["id"] == by_package["id"], "Game name and Android package identify different registered Games")
    require(by_name is None or by_package is not None,
            "Game name already registered with another Android package. Select the existing Game explicitly for a changed local package, or use a distinct edition name.")
    return by_name or by_package


def trusted_registry_pr(api, number, repository_id, *, expected_head=None):
    root = f"/repositories/{repository_id}"
    pr = api.request("GET", f"{root}/pulls/{number}")
    actor = pr.get("user", {})
    head = pr.get("head", {})
    base = pr.get("base", {})
    if not (
        actor.get("type") == "Bot" and actor.get("login") == "github-actions[bot]"
        and actor.get("id") == 41898282
        and head.get("repo", {}).get("id") == repository_id
        and base.get("repo", {}).get("id") == repository_id
        and isinstance(head.get("ref"), str) and re.fullmatch(
            re.escape(REGISTRY_BRANCH_PREFIX) + r"[1-9][0-9]*-[0-9a-f]{32}", head["ref"],
        )
        and pr.get("changed_files") == 1
    ):
        return False
    files = api.request("GET", f"{root}/pulls/{number}/files?per_page=2")
    return (
        isinstance(files, list) and len(files) == 1
        and files[0].get("filename") == "game-registry.json"
        and files[0].get("status") == "modified"
        and (expected_head is None or (
            head.get("sha") == expected_head and pr.get("state") == "open"
        ))
    )


def write_registry_pr(api, number, repository_id, repo, sha, raw, diagnostics):
    """Respect PR-only protection; never write or force-update the default branch."""
    root = f"/repositories/{repository_id}"
    branch = f"{REGISTRY_BRANCH_PREFIX}{number}-{uuid.uuid4().hex}"
    default_ref = quote(repo["default_branch"], safe="")
    base = api.request("GET", f"{root}/git/ref/heads/{default_ref}")
    base_sha = base["object"]["sha"]
    require(base["object"]["type"] == "commit" and re.fullmatch(r"[0-9a-f]{40}", base_sha), "invalid default branch head")
    api.request("POST", f"{root}/git/refs", {"ref": f"refs/heads/{branch}", "sha": base_sha})
    result = api.request("PUT", f"{root}/contents/game-registry.json", {
        "message": f"Register Game from issue #{number}", "sha": sha,
        "branch": branch, "content": base64.b64encode(raw).decode("ascii"),
    })
    head = result["commit"]["sha"]
    require(isinstance(head, str) and re.fullmatch(r"[0-9a-f]{40}", head), "invalid registry commit")
    pr = api.request("POST", f"{root}/pulls", {
        "title": f"Register Game from issue #{number}", "head": branch,
        "base": repo["default_branch"],
        "body": f"Trusted registry update for issue #{number}. Automatically validated; no package publication.",
    })
    pr_number = pr["number"]
    require(type(pr_number) is int and pr_number > 0, "invalid registry PR number")
    diagnostics.append(pr_number)
    pr_path = f"{root}/pulls/{pr_number}"
    try:
        require(trusted_registry_pr(api, pr_number, repository_id, expected_head=head), "registry PR identity/files changed")
        _, current_sha = read_registry(api, repository_id)
        if current_sha != sha:
            raise GitHubError(409)
        result = api.request("PUT", f"{pr_path}/merge", {
            "sha": head, "merge_method": "merge",
            "commit_title": f"Accept Game registration #{number}",
        })
        if result.get("merged") is not True:
            raise GitHubError(409)
    except (ValueError, KeyError, TypeError):
        # Prevent a stale full-registry proposal from being merged later by accident.
        api.request("POST", f"{root}/issues/{pr_number}/comments", {"body":
            "Automatic registry merge failed or was superseded. This proposal is closed; "
            "the issue workflow re-reads the default-branch registry before retrying. "
            "Inspect the workflow diagnostics; do not manually merge this stale proposal."
        })
        api.request("PATCH", pr_path, {"state": "closed"})
        raise


def register_game(api, number, repository_id):
    require(type(number) is int and number > 0 and type(repository_id) is int and repository_id > 0, "invalid repository/issue ID")
    root = f"/repositories/{repository_id}"
    issue_path = f"{root}/issues/{number}"
    # The event supplies only the number; all other issue data is freshly read.
    issue = api.request("GET", issue_path)
    if not isinstance(issue, dict) or not isinstance(issue.get("title"), str) or not issue["title"].startswith(GAME_ISSUE_PREFIX):
        return None
    diagnostics = []
    try:
        name, package = registration_request(issue)
        for attempt in range(REGISTRY_ATTEMPTS):
            repo = api.request("GET", root)
            require(
                isinstance(repo, dict) and repo.get("id") == repository_id and repo.get("full_name") == REPOSITORY
                and repo.get("private") is False and isinstance(repo.get("default_branch"), str)
                and repo["default_branch"],
                "catalog identity mismatch",
            )
            registry, sha = read_registry(api, repository_id)
            game = registered_game(registry, name, package)
            changed = False
            if game is None:
                game = {"id": str(uuid.uuid4()), "name": name, "android_packages": [package]}
                registry["games"].append(game)
                changed = True
            if changed:
                validate_registry(registry)
                raw = (json.dumps(registry, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
                require(len(raw) <= MAX_REGISTRY, "Game registry capacity exceeded")
                try:
                    write_registry_pr(api, number, repository_id, repo, sha, raw, diagnostics)
                except GitHubError as error:
                    if error.status in (405, 409, 422) and attempt + 1 < REGISTRY_ATTEMPTS:
                        continue
                    raise
                # Confirm the live committed identity, including after a competing write.
                confirmed, _ = read_registry(api, repository_id)
                game = registered_game(confirmed, name, package)
                require(game is not None and package in game["android_packages"], "Game registration was not confirmed")
            result = {"schema_version": 1, "game_id": game["id"], "name": game["name"]}
            api.request("POST", f"{issue_path}/comments", {"body": json.dumps(result, ensure_ascii=True)})
            api.request("PATCH", issue_path, {"state": "closed", "state_reason": "completed"})
            return result
    except (ValueError, KeyError, TypeError, RecursionError):
        # Never reflect untrusted issue text or raw transport errors into Markdown.
        api.request("POST", f"{issue_path}/comments", {"body": json.dumps({
            "schema_version": 1, "error": "Game registration failed. Check the request fields and workflow diagnostics. If the name already exists with another package, select the existing Game explicitly for a changed local package or use a distinct edition name. Reopen to retry.",
            "registry_pull_requests": diagnostics,
        })})
        raise


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


class GitHubError(ValueError):
    def __init__(self, status):
        self.status = status
        super().__init__(f"GitHub catalog automation HTTP {status}; inspect partial state before retry")


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
            raise GitHubError(code) from None
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
    registry, _ = read_registry(api, repository_id)
    require(any(game["id"] == descriptor["game_id"] for game in registry["games"]), "unregistered Game ID")
    if pr["state"] == "open":
        # Contents ref omitted deliberately: check the current default branch.
        for name in names:
            require(api.request("GET", f"{root}/contents/{name}", missing=True) is None, "version already exists")
    fresh = api.request("GET", f"{root}/pulls/{number}")
    require(fresh["head"]["sha"] == head and fresh["user"]["id"] == actor and fresh["state"] == pr["state"], "PR changed during validation")
    return pr, descriptor, raw


def package_display(raw):
    with ZipFile(BytesIO(raw)) as archive:
        manifest = load_json(archive.read("manifest.json"))
    name = manifest.get("package_name")
    description = manifest.get("description")
    require(isinstance(name, str) and name.strip(), "missing package display name")
    require(isinstance(description, str), "invalid package description")
    require("<!-- botstudio-publication-v1" not in description, "description contains reserved metadata marker")
    game = manifest["game"]
    game_name = game.get("name") or game["id"]
    require(isinstance(game_name, str), "invalid Game display name")
    kind = manifest.get("package_type")
    if kind == "function" and manifest["package_metadata"]["package_id"].startswith("helper-"):
        kind = "helper"
    require(kind in ("event", "function", "helper"), "invalid package display type")
    contents = manifest["contents"]
    require(isinstance(contents, dict) and all(
        isinstance(key, str) and type(value) is int and value >= 0
        for key, value in contents.items()
    ), "invalid package contents summary")
    return {
        "package_name": name, "game_name": game_name, "package_type": kind,
        "description": description, "contents": contents,
        "author": manifest["package_metadata"]["author"],
    }


def release_presentation(raw, publication):
    display = package_display(raw)
    name, game_name, description = (
        display["package_name"], display["game_name"], display["description"]
    )
    publication = {**publication, "display": display}
    title = " ".join(f"{game_name} | {name} {publication['submission']['package_version']}".split())[:200]
    # JSON escaping prevents user-controlled metadata from closing the HTML comment.
    metadata = json.dumps(publication, sort_keys=True).replace("<", "\\u003c").replace(">", "\\u003e")
    body = (
        description.strip()
        + "\n\n---\n\n"
        + f"Package type: **{display['package_type']}** | Author: **{display['author']}**\n\n"
        + "Contents: " + ", ".join(f"{key}: {value}" for key, value in display["contents"].items())
        + "\n\n"
        + "Download **package.botstudio.zip** below and import it in BotStudio.\n\n"
        + "<!-- botstudio-publication-v1\n" + metadata + "\n-->"
    )
    require(len(body.encode("utf-8")) <= 120000, "release description is too large")
    return title, body


def publish(api, number, repository_id):
    pr, descriptor, raw = validate_pr(api, number, repository_id, allow_merged=True)
    title, body = release_presentation(raw, {
        "schema_version": 1, "pull_request": number, "head": pr["head"]["sha"],
        "submitter": descriptor["submitter"], "archive_sha256": descriptor["archive_sha256"],
        "submission": descriptor,
        "notice": "PR author/account binding checked; other authorship/license claims remain declared.",
    })
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
        "name": title, "body": body,
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
    parser.add_argument("mode", choices=("validate", "publish", "register-game"))
    parser.add_argument("--pr", type=int)
    parser.add_argument("--issue", type=int)
    args = parser.parse_args()
    require(os.environ["GITHUB_REPOSITORY"] == REPOSITORY, "unexpected repository")
    repository_id = int(os.environ["GITHUB_REPOSITORY_ID"])
    require(repository_id > 0, "invalid repository ID")
    api = GitHub(os.environ["GH_TOKEN"])
    if args.mode == "register-game":
        require(args.issue is not None and args.issue > 0 and args.pr is None, "invalid issue argument")
        register_game(api, args.issue, repository_id)
    elif args.mode == "validate":
        require(args.pr is not None and args.pr > 0 and args.issue is None, "invalid PR argument")
        if trusted_registry_pr(api, args.pr, repository_id):
            print("Trusted registry-only PR; package validation skipped.")
            return
        validate_pr(api, args.pr, repository_id, allow_merged=True)
    else:
        require(args.pr is not None and args.pr > 0 and args.issue is None, "invalid PR argument")
        if trusted_registry_pr(api, args.pr, repository_id):
            print("Trusted registry-only PR; package publication skipped.")
            return
        publish(api, args.pr, repository_id)
    print("Catalog operation completed successfully.")


if __name__ == "__main__":
    main()
