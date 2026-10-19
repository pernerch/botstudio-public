"""Canonical public Game identities, independent of Android package names."""

import base64
import binascii
import json
import re
from threading import Event
from time import monotonic
import unicodedata

from services.file_io import atomic_write_json, read_json
from services.github_auth_service import GitHubAuthService
from services.github_catalog_service import GitHubCatalogService
from services.game_service import GAME_STORE_LOCK
from services.package_service import PackageError


def game_name_key(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


class GameRegistryService:
    PATH = "game-registry.json"
    REGISTRATION_SECONDS = 90

    def __init__(self, catalog=None, submission=None, auth=None):
        self.catalog = catalog if catalog is not None else GitHubCatalogService()
        self.submission = submission
        self.auth = auth if auth is not None else GitHubAuthService()

    @staticmethod
    def validate(payload):
        if (
            not isinstance(payload, dict) or type(payload.get("schema_version")) is not int
            or payload["schema_version"] != 1
        ):
            raise PackageError("Invalid Game directory.")
        games = payload.get("games")
        if not isinstance(games, list) or len(games) > 10000:
            raise PackageError("Invalid Game directory size.")
        ids, names, packages = set(), set(), set()
        for game in games:
            if not isinstance(game, dict):
                raise PackageError("Invalid Game directory entry.")
            identity, name, android = game.get("id"), game.get("name"), game.get("android_packages")
            if (
                not isinstance(identity, str)
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", identity)
                or not isinstance(name, str) or not 1 <= len(name.strip()) <= 120
                or any(unicodedata.category(char).startswith("C") for char in name)
                or not isinstance(android, list) or len(android) > 100
                or len(android) != len(set(item for item in android if isinstance(item, str)))
                or any(not isinstance(item, str) or len(item) > 255 or not re.fullmatch(
                    r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+", item,
                ) for item in android)
            ):
                raise PackageError("Invalid Game identity, name or Android package.")
            key = game_name_key(name)
            if identity.casefold() in ids or key in names or packages.intersection(android):
                raise PackageError("Ambiguous Game directory.")
            ids.add(identity.casefold())
            names.add(key)
            packages.update(android)
        return games

    def load(self):
        result = self.catalog._json(
            f"/repos/{self.catalog.REPOSITORY}/contents/{self.PATH}",
        )
        try:
            if result.get("encoding") != "base64" or result.get("size", 0) > 512 * 1024:
                raise ValueError("Invalid Game directory content.")
            raw = base64.b64decode(result["content"].replace("\n", "").replace("\r", ""), validate=True)
            if len(raw) > 512 * 1024:
                raise ValueError("Game directory too large.")
            return self.validate(json.loads(raw))
        except (KeyError, AttributeError, ValueError, binascii.Error) as error:
            raise PackageError("GitHub Game directory could not be read.") from error

    @staticmethod
    def find(games, *, identity="", name="", package=""):
        if identity:
            matches = [game for game in games if game["id"] == identity]
            if matches:
                return matches[0]
        matches = [
            game for game in games
            if (name and game_name_key(game["name"]) == game_name_key(name))
            or (package and package in game["android_packages"])
        ]
        if len(matches) > 1:
            raise PackageError("Game name and Android package refer to different registered Games.")
        return matches[0] if matches else None

    def require(self, identity):
        entry = self.find(self.load(), identity=identity)
        if entry is None:
            raise PackageError(
                "Game is not registered. Open Game Setup, load the GitHub Game list "
                "and select or register the Game before uploading.",
            )
        return entry

    def register(self, name, package):
        name = name.strip()
        self.validate({"schema_version": 1, "games": [{
            "id": "validation", "name": name, "android_packages": [package],
        }]})
        existing = self.find(self.load(), name=name, package=package)
        if existing and package in existing["android_packages"]:
            return existing
        if existing:
            raise PackageError(
                "This Game name already exists with a different Android package. "
                "Select that Game for a changed package, or use a distinct edition name.",
            )
        from services.github_submission_service import GitHubSubmissionService

        submission = self.submission if self.submission is not None else GitHubSubmissionService()
        token = submission.publication.credentials.load()
        if not token:
            raise PackageError("Authorize public GitHub contributions under Telegram/GitHub first.")
        account = submission.publication._account(token)
        if account != self.auth.restore():
            raise PackageError("Normal and publication GitHub accounts must match.")
        result = submission._api(
            "POST", f"/repos/{GitHubCatalogService.REPOSITORY}/issues", token,
            {"title": f"[BotStudio Game] {name}", "body": json.dumps({
                "schema_version": 1, "name": name, "android_package_name": package,
            }, ensure_ascii=True)}, statuses=(201,),
        )
        number = result.get("number")
        if type(number) is not int or number <= 0:
            raise PackageError("GitHub returned an invalid Game registration.")
        deadline = monotonic() + self.REGISTRATION_SECONDS
        wait = Event()
        while monotonic() < deadline:
            existing = self.find(self.load(), name=name, package=package)
            if existing and package in existing["android_packages"]:
                return existing
            wait.wait(min(3, max(0, deadline - monotonic())))
        raise PackageError(
            f"Game registration #{number} is still pending or failed. "
            "Refresh the GitHub Game list before retrying.",
        )

    @classmethod
    def adopt_existing(cls, file_name, registered):
        """Bind old local IDs without moving Functions, profiles or runtime data."""
        with GAME_STORE_LOCK:
            games = read_json(file_name)
            if not isinstance(games, list) or any(
                not isinstance(game, dict) or not isinstance(game.get("id"), str)
                for game in games
            ):
                raise PackageError("Invalid local Game store.")
            before = json.dumps(games, sort_keys=True)
            claimed = set()
            for game in games:
                identity = game.get("catalog_game_id") or game["id"]
                package = game.get("package_name", "")
                entry = cls.find(
                    registered, identity=identity,
                    name=game.get("display_name", ""),
                    package=package,
                )
                if entry:
                    if (
                        identity != entry["id"] and package and entry["android_packages"]
                        and package not in entry["android_packages"]
                    ):
                        continue
                    if entry["id"] in claimed:
                        raise PackageError(
                            "Several local Games match one registered Game; resolve the duplicate before sharing.",
                        )
                    claimed.add(entry["id"])
                    game["catalog_game_id"] = entry["id"]
                    game["catalog_game_name"] = entry["name"]
            if json.dumps(games, sort_keys=True) != before:
                atomic_write_json(file_name, games)
            return games

    @classmethod
    def ensure_local(cls, file_name, entry, package="", *, local_game_id=""):
        cls.validate({"schema_version": 1, "games": [entry]})
        with GAME_STORE_LOCK:
            if local_game_id:
                games = read_json(file_name)
                selected = next((game for game in games if game["id"] == local_game_id), None)
                if selected is None:
                    raise PackageError("The selected local Game no longer exists.")
                if selected.get("catalog_game_id") not in (None, "", entry["id"]):
                    raise PackageError("This local Game is already linked to a different registered Game.")
                if any(
                    game["id"] != local_game_id
                    and (game.get("catalog_game_id") or game["id"]) == entry["id"]
                    for game in games
                ):
                    raise PackageError("Another local Game already uses this registered identity.")
                selected["catalog_game_id"] = entry["id"]
                selected["catalog_game_name"] = entry["name"]
                atomic_write_json(file_name, games)
                return selected
            games = cls.adopt_existing(file_name, [entry])
            matching = [
                game for game in games
                if (game.get("catalog_game_id") or game["id"]) == entry["id"]
            ]
            if matching:
                return matching[0]
            if any(game["id"] == entry["id"] for game in games):
                raise PackageError("Local Game identity conflicts with the Game directory.")
            game = {
                "id": entry["id"], "catalog_game_id": entry["id"],
                "catalog_game_name": entry["name"],
                "display_name": entry["name"],
                "package_name": package or next(iter(entry["android_packages"]), ""),
                "is_default": not games,
            }
            games.append(game)
            atomic_write_json(file_name, games)
            return game


def rebind_game_json(value, source, target):
    """Rebind storage records, not nested block inputs, values or provenance."""
    if isinstance(value, list):
        return [rebind_game_json(item, source, target) for item in value]
    if not isinstance(value, dict):
        return value
    rebound = dict(value)
    if rebound.get("game_id") == source:
        rebound["game_id"] = target
    return rebound
