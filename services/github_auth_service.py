import re
from threading import Event
from time import monotonic

import requests

from services.windows_credential_service import WindowsCredentialService
from models.github_account import GitHubAccount


class GitHubAuthError(RuntimeError):
    pass


class GitHubCredentials(WindowsCredentialService):
    TARGET = "BotStudio.GitHub.OAuthToken"


class GitHubPublicationCredentials(WindowsCredentialService):
    TARGET = "BotStudio.GitHub.PublicationOAuthToken"


class GitHubAuthService:
    """Blocking GitHub.com device flow. Call only from a worker, never the GUI."""

    DEVICE_URL = "https://github.com/login/device/code"
    TOKEN_URL = "https://github.com/login/oauth/access_token"
    USER_URL = "https://api.github.com/user"
    VERIFICATION_URL = "https://github.com/login/device"
    REQUIRED_SCOPE = ""

    def __init__(self, credentials=None, request=None, clock=monotonic):
        self.credentials = credentials if credentials is not None else GitHubCredentials()
        self.request = request if request is not None else requests.request
        self.clock = clock

    def _json(self, method: str, url: str, *, token=None, data=None) -> dict:
        headers = {"Accept": "application/json", "User-Agent": "BotStudio"}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        response = None
        try:
            response = self.request(
                method, url, headers=headers, data=data,
                timeout=(5, 10), allow_redirects=False,
            )
            if response.status_code != 200:
                raise GitHubAuthError(f"GitHub HTTP {response.status_code}")
            if token is not None and self.REQUIRED_SCOPE:
                scopes = response.headers.get("X-OAuth-Scopes")
                if not isinstance(scopes, str) or {
                    scope.strip() for scope in scopes.split(",") if scope.strip()
                } != {self.REQUIRED_SCOPE}:
                    raise GitHubAuthError(
                        "GitHub publication requires exactly public_repo permission; "
                        "check GitHub app authorization and authorize publication again"
                    )
            payload = response.json()
        except (requests.RequestException, ValueError):
            # Do not expose request bodies, Authorization headers or raw responses.
            raise GitHubAuthError("GitHub request failed or returned invalid JSON") from None
        finally:
            if response is not None:
                response.close()
        if not isinstance(payload, dict):
            raise GitHubAuthError("GitHub returned an invalid response")
        return payload

    def _account(self, token: str) -> GitHubAccount:
        payload = self._json("GET", self.USER_URL, token=token)
        account_id, login = payload.get("id"), payload.get("login")
        if type(account_id) is not int or not 1 <= account_id < 10**20:
            raise GitHubAuthError("GitHub returned an invalid account ID")
        if (
            not isinstance(login, str)
            or not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?", login)
            or "--" in login
        ):
            raise GitHubAuthError("GitHub returned an invalid login")
        return GitHubAccount(str(account_id), login)

    @staticmethod
    def _check_cancel(cancel: Event) -> None:
        if cancel.is_set():
            raise GitHubAuthError("GitHub sign-in cancelled")

    def sign_in(self, client_id: str, cancel: Event, on_code) -> GitHubAccount:
        if not isinstance(client_id, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", client_id):
            raise GitHubAuthError("A registered GitHub OAuth client ID is required")
        self._check_cancel(cancel)
        payload = self._json("POST", self.DEVICE_URL, data={
            "client_id": client_id, "scope": self.REQUIRED_SCOPE,
        })
        device_code, user_code = payload.get("device_code"), payload.get("user_code")
        expires, interval = payload.get("expires_in"), payload.get("interval")
        if (
            not isinstance(device_code, str) or not device_code
            or not isinstance(user_code, str) or not re.fullmatch(r"[A-Za-z0-9-]{4,32}", user_code)
            or payload.get("verification_uri") != self.VERIFICATION_URL
            or type(expires) is not int or not 1 <= expires <= 900
            or type(interval) is not int or not 1 <= interval <= 900
        ):
            raise GitHubAuthError("GitHub returned an invalid device authorization")
        deadline = self.clock() + expires
        self._check_cancel(cancel)
        on_code(user_code, self.VERIFICATION_URL)
        while self.clock() < deadline:
            if cancel.wait(min(interval, max(0, deadline - self.clock()))):
                self._check_cancel(cancel)
            if self.clock() >= deadline:
                break
            result = self._json("POST", self.TOKEN_URL, data={
                "client_id": client_id,
                "device_code": device_code,
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            })
            self._check_cancel(cancel)
            if self.clock() >= deadline:
                break
            error = result.get("error")
            if error == "authorization_pending":
                continue
            if error == "slow_down":
                interval += 5
                continue
            if error:
                if not isinstance(error, str):
                    raise GitHubAuthError("GitHub returned an invalid authorization error")
                messages = {
                    "access_denied": "GitHub authorization was denied",
                    "expired_token": "GitHub device code expired; start sign-in again",
                    "incorrect_client_credentials": "GitHub OAuth client ID is invalid",
                    "device_flow_disabled": "Enable device flow in the GitHub OAuth app",
                }
                raise GitHubAuthError(messages.get(error, "GitHub authorization failed"))
            token = result.get("access_token")
            token_type = result.get("token_type")
            if (
                not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_]+", token)
                or not isinstance(token_type, str) or token_type.lower() != "bearer"
            ):
                raise GitHubAuthError("GitHub returned an invalid access token")
            account = self._account(token)
            self._check_cancel(cancel)
            if self.clock() >= deadline:
                break
            previous_token = self.credentials.load()
            self._check_cancel(cancel)
            self.credentials.save(token)
            # Roll back only our new credential, preserving an existing local login.
            if cancel.is_set():
                if previous_token is None:
                    self.credentials.delete()
                else:
                    self.credentials.save(previous_token)
                self._check_cancel(cancel)
            return account
        raise GitHubAuthError("GitHub device code expired; start sign-in again")

    def restore(self) -> GitHubAccount | None:
        token = self.credentials.load()
        if token is None:
            return None
        return self._account(token)

    def sign_out(self) -> None:
        """Remove local authorization; GitHub-side revocation is a separate user action."""
        self.credentials.delete()


class GitHubPublicationAuthService(GitHubAuthService):
    """Opt-in public repository grant, stored separately from normal sign-in."""

    REQUIRED_SCOPE = "public_repo"

    def __init__(self, credentials=None, request=None, clock=monotonic):
        super().__init__(
            credentials if credentials is not None else GitHubPublicationCredentials(),
            request, clock,
        )
