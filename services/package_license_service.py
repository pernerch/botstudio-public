from __future__ import annotations

from dataclasses import dataclass
import hashlib
import http.client
from pathlib import Path
import re
from urllib.parse import quote

from models.bot_function import BotFunction
from models.github_account import GitHubAccount
from services.app_config_service import AppConfigService
from services.file_io import atomic_write_json, read_json
from services.reinitialize_service import get_default_reinitialize_service


@dataclass(frozen=True)
class PackageLicense:
    spdx_id: str
    name: str
    description_key: str
    url: str


PACKAGE_LICENSES = (
    PackageLicense("MIT", "MIT", "package_license.mit", "https://spdx.org/licenses/MIT.html"),
    PackageLicense(
        "Apache-2.0", "Apache 2.0", "package_license.apache",
        "https://spdx.org/licenses/Apache-2.0.html",
    ),
    PackageLicense(
        "BSD-3-Clause", "BSD 3-Clause", "package_license.bsd",
        "https://spdx.org/licenses/BSD-3-Clause.html",
    ),
    PackageLicense(
        "MPL-2.0", "MPL 2.0", "package_license.mpl",
        "https://spdx.org/licenses/MPL-2.0.html",
    ),
    PackageLicense(
        "GPL-3.0-only", "GPL 3.0 only", "package_license.gpl",
        "https://spdx.org/licenses/GPL-3.0-only.html",
    ),
    PackageLicense(
        "LGPL-3.0-only", "LGPL 3.0 only", "package_license.lgpl",
        "https://spdx.org/licenses/LGPL-3.0-only.html",
    ),
    PackageLicense(
        "CC0-1.0", "CC0 1.0", "package_license.cc0",
        "https://spdx.org/licenses/CC0-1.0.html",
    ),
    PackageLicense(
        "CC-BY-4.0", "CC BY 4.0", "package_license.cc_by",
        "https://spdx.org/licenses/CC-BY-4.0.html",
    ),
    PackageLicense(
        "CC-BY-SA-4.0", "CC BY-SA 4.0", "package_license.cc_by_sa",
        "https://spdx.org/licenses/CC-BY-SA-4.0.html",
    ),
)
LICENSES_BY_ID = {license_option.spdx_id: license_option for license_option in PACKAGE_LICENSES}


class PackageLicenseError(ValueError):
    """Raised when a package license choice or source text is invalid."""


class PackageLicenseService:
    CONFIG_KEY = "package_licenses_by_github_account"
    SPDX_TEXT_RELEASE = "v3.26.0"
    MAX_LICENSE_TEXT_BYTES = 256 * 1024

    def __init__(self, config_service=None, text_fetcher=None):
        self.config_service = config_service or AppConfigService()
        self.text_fetcher = text_fetcher or self._fetch_license_text

    def get_license(self, account_id: str) -> PackageLicense | None:
        if not isinstance(account_id, str) or not re.fullmatch(r"[1-9][0-9]{0,19}", account_id):
            raise PackageLicenseError("a valid GitHub account ID is required")
        config = self.config_service.load()
        if not isinstance(config, dict):
            raise PackageLicenseError("stored Studio settings are invalid")
        preferences = config.get(self.CONFIG_KEY, {})
        if not isinstance(preferences, dict):
            raise PackageLicenseError("stored package license preferences are invalid")
        spdx_id = preferences.get(account_id)
        if spdx_id is None:
            return None
        option = LICENSES_BY_ID.get(spdx_id) if isinstance(spdx_id, str) else None
        if option is None:
            raise PackageLicenseError("the saved package license is not supported")
        return option

    def set_license(self, account_id: str, spdx_id: str) -> PackageLicense:
        if not isinstance(account_id, str) or not re.fullmatch(r"[1-9][0-9]{0,19}", account_id):
            raise PackageLicenseError("a valid GitHub account ID is required")
        option = LICENSES_BY_ID.get(spdx_id) if isinstance(spdx_id, str) else None
        if option is None:
            raise PackageLicenseError("select a supported SPDX license")
        config = self.config_service.load()
        if not isinstance(config, dict):
            raise PackageLicenseError("stored Studio settings are invalid")
        preferences = config.get(self.CONFIG_KEY, {})
        if not isinstance(preferences, dict):
            raise PackageLicenseError("stored package license preferences are invalid")
        updated = dict(preferences)
        updated[account_id] = option.spdx_id
        config[self.CONFIG_KEY] = updated
        self.config_service.save(config)
        return option

    def metadata(
        self,
        account: GitHubAccount,
        package_type: str,
        game_id: str,
        source_id: str,
        revision: int,
    ) -> dict | None:
        if not isinstance(account, GitHubAccount):
            raise PackageLicenseError("a validated GitHub account identity is required")
        if not isinstance(package_type, str) or package_type not in {
            "function", "helper", "event",
        }:
            raise PackageLicenseError("unsupported package type")
        identity_pattern = r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}"
        if (
            not isinstance(game_id, str)
            or not re.fullmatch(identity_pattern, game_id)
            or not isinstance(source_id, str)
            or not re.fullmatch(identity_pattern, source_id)
        ):
            raise PackageLicenseError("game and source identifiers must be safe stable identifiers")
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
            raise PackageLicenseError("package revision must be a positive integer")
        option = self.get_license(account.account_id)
        if option is None:
            return None

        return {
            "package_id": self.package_identity(package_type, game_id, source_id),
            "package_version": f"1.0.{revision}",
            "author": f"@{account.login}",
            "license_id": option.spdx_id,
            "license_text": self.text_fetcher(option.spdx_id),
            "license_url": option.url,
            "rights_confirmed": False,
            "publisher": account.to_dict(),
        }

    @staticmethod
    def package_identity(package_type: str, game_id: str, source_id: str) -> str:
        identity = hashlib.sha256(
            f"{package_type}\0{game_id}\0{source_id}".encode("utf-8")
        ).hexdigest()[:32]
        return f"{package_type}-{identity}"

    @classmethod
    def _fetch_license_text(cls, spdx_id: str) -> str:
        if spdx_id not in LICENSES_BY_ID:
            raise PackageLicenseError("unsupported SPDX license")
        path = f"/spdx/license-list-data/{cls.SPDX_TEXT_RELEASE}/text/{quote(spdx_id)}.txt"
        connection = http.client.HTTPSConnection("raw.githubusercontent.com", timeout=8)
        try:
            connection.request("GET", path, headers={"Accept": "text/plain"})
            response = connection.getresponse()
            if response.status != 200:
                raise PackageLicenseError(
                    f"could not retrieve official {spdx_id} license text (HTTP {response.status})"
                )
            raw = response.read(cls.MAX_LICENSE_TEXT_BYTES + 1)
            if len(raw) > cls.MAX_LICENSE_TEXT_BYTES:
                raise PackageLicenseError("official license text exceeds the size limit")
            text = raw.decode("utf-8").strip()
            if not text:
                raise PackageLicenseError("official license text is empty")
            return text
        except (OSError, UnicodeDecodeError, http.client.HTTPException) as error:
            raise PackageLicenseError(f"could not retrieve official {spdx_id} license text") from error
        finally:
            connection.close()


class PackagePreparationService:
    def __init__(
        self,
        function_root: str = "data/functions",
        license_service=None,
        reinitialize_service=None,
    ):
        self.function_root = Path(function_root)
        self.license_service = license_service or PackageLicenseService()
        self.reinitialize_service = (
            reinitialize_service or get_default_reinitialize_service()
        )

    def function_metadata(
        self,
        function: BotFunction,
        included_functions: list[BotFunction] | None = None,
    ) -> dict | None:
        included = included_functions or [function]
        if (
            not function or not function.saved or not included
            or any(item.creator_account is None for item in included)
        ):
            return None
        accounts = {item.creator_account for item in included}
        if len(accounts) != 1:
            return None
        account = next(iter(accounts))
        return self.license_service.metadata(
            account,
            "helper" if function.kind == "helper" else "function",
            function.game_id,
            function.function_id,
            function.revision,
        )

    def event_metadata(self, event, functions: list[BotFunction]) -> dict | None:
        if not functions or any(function.creator_account is None for function in functions):
            return None
        accounts = {function.creator_account for function in functions}
        if len(accounts) != 1:
            return None
        account = next(iter(accounts))
        revision = max(function.revision for function in functions)
        return self.license_service.metadata(
            account,
            "event",
            event.game_id,
            event.event_id,
            revision,
        )

    def list_unattributed_functions(self) -> list[BotFunction]:
        if not self.function_root.exists():
            return []
        functions = []
        for game_root in sorted(self.function_root.iterdir()):
            if not game_root.is_dir():
                continue
            for path in sorted(game_root.glob("*.json")):
                function = BotFunction.from_dict(read_json(path))
                if (
                    function.saved
                    and function.creator_account is None
                    and not function.package_origins
                ):
                    functions.append(function)
        return functions

    def attribute_functions(
        self,
        selections: list[tuple[str, str]],
        account: GitHubAccount,
    ) -> int:
        if not isinstance(account, GitHubAccount):
            raise PackageLicenseError("a validated GitHub account identity is required")
        if not selections:
            return 0
        if len(set(selections)) != len(selections):
            raise PackageLicenseError("duplicate Functions were selected")
        component_pattern = r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}"
        paths = []
        for game_id, function_id in selections:
            if (
                not isinstance(game_id, str)
                or not re.fullmatch(component_pattern, game_id)
                or not isinstance(function_id, str)
                or not re.fullmatch(component_pattern, function_id)
            ):
                raise PackageLicenseError("invalid Function selection")
            paths.append((game_id, function_id, self.function_root / game_id / f"{function_id}.json"))

        pending = []
        for game_id, function_id, path in paths:
            if not path.is_file():
                raise PackageLicenseError(f"selected Function no longer exists: {function_id}")
            data = read_json(path)
            function = BotFunction.from_dict(data)
            if function.game_id != game_id or function.function_id != function_id or not function.saved:
                raise PackageLicenseError(f"selected Function changed: {function_id}")
            if function.creator_account is not None:
                raise PackageLicenseError(
                    f"Function already has a creator account and was not changed: {function_id}"
                )
            if function.package_origins:
                raise PackageLicenseError(
                    f"imported Function content cannot be attributed: {function_id}"
                )
            pending.append((path, function_id, game_id))

        for path, function_id, game_id in pending:
            data = read_json(path)
            function = BotFunction.from_dict(data)
            if function.game_id != game_id or function.function_id != function_id or not function.saved:
                raise PackageLicenseError(f"selected Function changed: {function_id}")
            if function.creator_account is not None or function.package_origins:
                raise PackageLicenseError(
                    f"Function attribution changed and was not overwritten: {function_id}"
                )
            updated = dict(data)
            updated["creator_account"] = account.to_dict()
            atomic_write_json(path, updated)
            if self.reinitialize_service:
                self.reinitialize_service.publish(
                    "function",
                    game_id=game_id,
                    function_id=function_id,
                )
        return len(pending)
