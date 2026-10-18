import base64
import hashlib
import json
from pathlib import Path
import re
import tempfile
from time import monotonic
from urllib.parse import quote
from uuid import uuid4

import requests

from services.catalog_submission_service import CatalogSubmissionService
from services.github_auth_service import GitHubAuthError, GitHubPublicationAuthService
from services.github_catalog_service import GitHubCatalogService
from services.package_service import PackageError


class GitHubSubmissionService:
    """Explicit exact-byte contribution; never merges or publishes a release."""

    def __init__(self, preflight=None, publication=None, request=None):
        self.preflight = preflight if preflight is not None else CatalogSubmissionService()
        self.publication = publication if publication is not None else GitHubPublicationAuthService()
        self.request = request if request is not None else requests.request

    def _api(self, method, path, token, data=None, statuses=(200,)):
        response = None
        started = monotonic()
        try:
            response = self.request(
                method, GitHubCatalogService.API_ROOT + path,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "BotStudio",
                },
                json=data, timeout=(5, 30), allow_redirects=False, stream=True,
            )
            if response.status_code not in statuses:
                raise PackageError(f"GitHub submission HTTP {response.status_code}; no automatic retry")
            scopes = response.headers.get("X-OAuth-Scopes")
            if not isinstance(scopes, str) or {part.strip() for part in scopes.split(",")} != {"public_repo"}:
                raise PackageError("GitHub submission requires exactly public_repo permission")
            if response.status_code == 404:
                return None
            content = bytearray()
            for chunk in response.iter_content(chunk_size=65536):
                if monotonic() - started > 45 or len(content) + len(chunk) > 2 * 1024 * 1024:
                    raise PackageError("GitHub submission response exceeds size/time limit")
                content.extend(chunk)
            result = json.loads(content)
            if not isinstance(result, dict):
                raise PackageError("GitHub submission returned an invalid response")
            return result
        except (requests.RequestException, json.JSONDecodeError, UnicodeDecodeError):
            raise PackageError("GitHub submission request failed; check GitHub before retrying") from None
        finally:
            if response is not None:
                response.close()

    @staticmethod
    def _sha(value):
        if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{40}", value):
            raise PackageError("GitHub returned an invalid Git object identity")
        return value

    @staticmethod
    def _object(payload, key):
        value = payload.get(key)
        if not isinstance(value, dict):
            raise PackageError("GitHub submission returned invalid object metadata")
        return value

    def _identity(self, submission):
        token = self.publication.credentials.load()
        if token is None:
            raise PackageError("Authorize public GitHub contributions under Telegram/GitHub first")
        account = self.publication._account(token)
        if account.to_dict() != submission["submitter"]:
            raise PackageError("normal and publication GitHub accounts changed or do not match; prepare again")
        return token, account

    def _target(self, submission, token):
        repository = self._api("GET", f"/repos/{GitHubCatalogService.REPOSITORY}", token)
        if (
            repository.get("full_name") != GitHubCatalogService.REPOSITORY
            or repository.get("private") is not False
            or repository.get("fork") is not False
        ):
            raise PackageError("catalog must be the expected public original repository")
        repository_id = GitHubCatalogService._id(repository.get("id"))
        branch = repository.get("default_branch")
        if not isinstance(branch, str) or not re.fullmatch(r"[A-Za-z0-9._/-]{1,200}", branch):
            raise PackageError("catalog default branch is invalid or not initialized")
        ref = self._api("GET", f"/repositories/{repository_id}/git/ref/heads/{quote(branch, safe='')}", token)
        base = self._sha(self._object(ref, "object").get("sha"))
        commit = self._api("GET", f"/repositories/{repository_id}/git/commits/{base}", token)
        tree = self._sha(self._object(commit, "tree").get("sha"))
        for key in ("archive_path", "metadata_path"):
            existing = self._api(
                "GET", f"/repositories/{repository_id}/contents/{submission[key]}?ref={base}",
                token, statuses=(200, 404),
            )
            if existing is not None:
                raise PackageError("catalog package version already exists; export a new version")
        owner = self._object(repository, "owner")
        owner_id = GitHubCatalogService._id(owner.get("id"))
        return {
            "repository_id": repository_id, "base_branch": branch,
            "base_commit": base, "base_tree": tree, "owner_id": owner_id,
        }

    def prepare(self, package_path, *, expected_sha256):
        submission = self.preflight.prepare(Path(package_path), expected_sha256=expected_sha256)
        token, _account = self._identity(submission)
        return {"submission": submission, "target": self._target(submission, token)}

    def submit(self, package_path, *, plan):
        """Revalidate consent before writes; partial GitHub work is never deleted."""
        submission = plan["submission"]
        with tempfile.TemporaryDirectory(prefix="botstudio-pr-") as temporary:
            snapshot = Path(temporary) / "package.botstudio.zip"
            # Bounded copy, then run the exact same preflight over these bytes.
            size = 0
            with Path(package_path).open("rb") as source, snapshot.open("xb") as target:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    size += len(chunk)
                    if size > self.preflight.MAX_PACKAGE_BYTES:
                        raise PackageError("submission archive exceeds size limit")
                    target.write(chunk)
            current = self.prepare(snapshot, expected_sha256=submission["archive_sha256"])
            if current != plan:
                raise PackageError("submission/account/catalog changed; prepare and confirm again")
            token, account = self._identity(submission)
            target = plan["target"]
            repository_id = target["repository_id"]
            if target["owner_id"] == int(account.account_id):
                work_id = repository_id
            else:
                fork_name = "botstudio-catalog-contributions"
                fork = self._api(
                    "GET", f"/repos/{account.login}/{fork_name}", token, statuses=(200, 404),
                )
                if fork is None:
                    fork = self._api(
                        "POST", f"/repositories/{repository_id}/forks", token,
                        {"name": fork_name, "default_branch_only": True}, statuses=(202,),
                    )
                if (
                    fork.get("private") is not False or fork.get("fork") is not True
                    or self._object(fork, "owner").get("id") != int(account.account_id)
                    or self._object(fork, "parent").get("id") != repository_id
                    or fork.get("name") != fork_name
                ):
                    raise PackageError("contribution fork identity is invalid; no files uploaded")
                work_id = GitHubCatalogService._id(fork.get("id"))
            branch = "botstudio-submit-" + uuid4().hex
            try:
                ref = self._api("POST", f"/repositories/{work_id}/git/refs", token, {
                    "ref": f"refs/heads/{branch}", "sha": target["base_commit"],
                }, statuses=(201,))
                if ref.get("ref") != f"refs/heads/{branch}":
                    raise PackageError("created submission branch identity does not match")
                payloads = (
                    (submission["archive_path"], snapshot.read_bytes()),
                    (submission["metadata_path"], json.dumps(submission, sort_keys=True, indent=2).encode("utf-8")),
                )
                entries = []
                for path, payload in payloads:
                    blob = self._api("POST", f"/repositories/{work_id}/git/blobs", token, {
                        "content": base64.b64encode(payload).decode("ascii"), "encoding": "base64",
                    }, statuses=(201,))
                    sha = self._sha(blob.get("sha"))
                    expected = hashlib.sha1(
                        f"blob {len(payload)}\0".encode("ascii") + payload, usedforsecurity=False,
                    ).hexdigest()
                    if sha != expected:
                        raise PackageError("GitHub blob identity does not match the submitted bytes")
                    entries.append({"path": path, "mode": "100644", "type": "blob", "sha": sha})
                tree = self._api("POST", f"/repositories/{work_id}/git/trees", token, {
                    "base_tree": target["base_tree"], "tree": entries,
                }, statuses=(201,))
                commit = self._api("POST", f"/repositories/{work_id}/git/commits", token, {
                    "message": "Submit BotStudio game package",
                    "tree": self._sha(tree.get("sha")), "parents": [target["base_commit"]],
                }, statuses=(201,))
                head = self._sha(commit.get("sha"))
                ref = self._api("PATCH", f"/repositories/{work_id}/git/refs/heads/{branch}", token, {
                    "sha": head, "force": False,
                })
                if self._sha(self._object(ref, "object").get("sha")) != head:
                    raise PackageError("updated submission branch identity does not match")
                if self._target(submission, token) != target:
                    raise PackageError("catalog changed during upload; prepare again")
                pr = self._api("POST", f"/repositories/{repository_id}/pulls", token, {
                    "title": "BotStudio package submission",
                    "head": f"{account.login}:{branch}", "base": target["base_branch"],
                    "body": (
                        "Automated BotStudio submission. Catalog automation must independently validate "
                        "the PR actor, exact head, archive and immutable version before publication.\n\n"
                        f"Archive SHA-256: `{submission['archive_sha256']}`"
                    ), "maintainer_can_modify": False,
                }, statuses=(201,))
                number = GitHubCatalogService._id(pr.get("number"))
                if (
                    self._object(pr, "head").get("sha") != head
                    or self._object(self._object(pr, "base"), "repo").get("id") != repository_id
                ):
                    raise PackageError("created pull request identity does not match")
                return {
                    "url": f"https://github.com/{GitHubCatalogService.REPOSITORY}/pull/{number}",
                    "number": number, "branch": branch, "head": head,
                }
            except (PackageError, GitHubAuthError) as error:
                raise PackageError(
                    f"{error}. Submission incomplete; branch {branch} or a pull request may already exist. "
                    "Check GitHub before retrying. No automatic cleanup, merge or release."
                ) from None
