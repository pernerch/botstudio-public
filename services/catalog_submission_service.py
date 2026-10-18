import hashlib
from pathlib import Path
import tempfile

from services.github_auth_service import GitHubAuthService
from services.github_catalog_service import GitHubCatalogService
from services.managed_package_service import ManagedPackageService
from services.package_service import PackageError


class CatalogSubmissionService:
    """Preflight exact archive bytes for a future GitHub fork/PR submission."""

    MAX_PACKAGE_BYTES = 100 * 1024 * 1024

    def __init__(self, managed=None, auth=None):
        self.managed = managed if managed is not None else ManagedPackageService()
        self.auth = auth if auth is not None else GitHubAuthService()

    def prepare(self, package_path: Path, *, expected_sha256: str) -> dict:
        """Worker-only; no uploads, scope changes or persistent writes."""
        package_path = Path(package_path)
        if not 0 < package_path.stat().st_size <= self.MAX_PACKAGE_BYTES:
            raise PackageError("catalog submission exceeds the GitHub file size limit or is empty")
        with tempfile.TemporaryDirectory(prefix="botstudio-submission-") as temporary:
            snapshot = Path(temporary) / "package.botstudio.zip"
            digest = hashlib.sha256()
            size = 0
            with package_path.open("rb") as source, snapshot.open("xb") as target:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    size += len(chunk)
                    if size > self.MAX_PACKAGE_BYTES:
                        raise PackageError("catalog submission exceeds the GitHub file size limit")
                    digest.update(chunk)
                    target.write(chunk)
            if digest.hexdigest() != expected_sha256:
                raise PackageError("package changed since preview; inspect it again before submission")
            GitHubCatalogService().check_archive_limits(snapshot)
            preview = self.managed.preview(snapshot)
            manifest = preview["manifest"]
            self.managed.packages.github_release_metadata(manifest, str(snapshot))
            publisher, package_id, game_id = self.managed._identity(manifest)
            version = manifest["package_metadata"]["package_version"]
            self.managed._version(version)
            # Identity must come from a fresh authenticated response, never the manifest.
            account = self.auth.restore()
            if account is None:
                raise PackageError("Sign in to GitHub under Telegram/GitHub before preparing a submission")
            if publisher != f"github:{account.account_id}":
                raise PackageError("package publisher does not match the signed-in GitHub account; export an attributed variant")
            game_key = hashlib.sha256(game_id.encode("utf-8")).hexdigest()
            package_key = hashlib.sha256(package_id.encode("utf-8")).hexdigest()
            path = f"games/{game_key}/publishers/{account.account_id}/packages/{package_key}/{version}"
            return {
                "schema_version": 1,
                "repository": GitHubCatalogService.REPOSITORY,
                "game_id": game_id,
                "package_id": package_id,
                "package_version": version,
                "submitter": account.to_dict(),
                "archive_sha256": digest.hexdigest(),
                "archive_size": size,
                "archive_path": f"{path}/package.botstudio.zip",
                "metadata_path": f"{path}/submission.json",
                "fork_of": manifest["package_metadata"].get("fork_of"),
            }
