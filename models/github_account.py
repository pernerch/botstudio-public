from dataclasses import dataclass
import re


@dataclass(frozen=True)
class GitHubAccount:
    """Account attribution; serialized values alone are not authentication."""

    account_id: str
    login: str

    def __post_init__(self):
        if not isinstance(self.account_id, str) or not re.fullmatch(
            r"[1-9][0-9]{0,19}", self.account_id,
        ):
            raise ValueError("invalid GitHub account ID")
        if (
            not isinstance(self.login, str)
            or not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?", self.login)
            or "--" in self.login
        ):
            raise ValueError("invalid GitHub login")

    def to_dict(self) -> dict:
        return {"provider": "github", "account_id": self.account_id, "login": self.login}

    @classmethod
    def from_dict(cls, data: dict) -> "GitHubAccount":
        if not isinstance(data, dict) or set(data) != {"provider", "account_id", "login"}:
            raise ValueError("invalid GitHub account attribution")
        if data["provider"] != "github":
            raise ValueError("account provider must be github")
        return cls(data["account_id"], data["login"])


@dataclass(frozen=True)
class GitHubAssetUploader:
    """Release transport attribution, not a signed-in user or package publisher."""

    account_id: str
    login: str

    def __post_init__(self):
        if not isinstance(self.login, str):
            raise ValueError("invalid GitHub uploader login")
        GitHubAccount(self.account_id, self.login.removesuffix("[bot]"))
