import json
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from services.logging_service import get_logger


class IssueService:
    """Persists support issue drafts until a Git provider submits them."""

    STATUS_OPEN = "Open"
    STATUS_CLOSED = "Closed"
    STATUS_WAITING_FOR_USER = "Waiting for User"
    STATUS_IN_PROGRESS = "In Progress"

    def __init__(self, file_name: str = "config/issues.json", logger=None):
        self.file_name = Path(file_name)
        self.file_name.parent.mkdir(parents=True, exist_ok=True)
        self.logger = logger or get_logger("support")

    def list_issues(self) -> list[dict]:
        if not self.file_name.exists():
            return []
        try:
            with open(self.file_name, "r", encoding="utf-8") as handle:
                issues = json.load(handle)
        except (OSError, ValueError, TypeError):
            return []
        return sorted(
            (issue for issue in issues if isinstance(issue, dict)),
            key=lambda issue: issue.get("created_at", ""),
            reverse=True,
        )

    def create_issue(self, support_package, context=None) -> dict:
        issue = {
            "issue_id": str(uuid4()),
            "status": self.STATUS_OPEN,
            "created_at": datetime.now().astimezone().isoformat(),
            "support_package": str(support_package),
            "context": context or {},
        }
        issues = self.list_issues()
        issues.append(issue)
        self._save(issues)
        self.logger.info(
            "Support Issue Created",
            issue_id=issue["issue_id"],
            status=issue["status"],
            support_package=issue["support_package"],
        )
        return issue

    def update_status(self, issue_id: str, status: str) -> dict | None:
        if status not in {
            self.STATUS_OPEN,
            self.STATUS_CLOSED,
            self.STATUS_WAITING_FOR_USER,
            self.STATUS_IN_PROGRESS,
        }:
            raise ValueError(f"unsupported issue status '{status}'")
        issues = self.list_issues()
        for issue in issues:
            if issue.get("issue_id") == issue_id:
                issue["status"] = status
                self._save(issues)
                return issue
        return None

    def status_counts(self) -> dict[str, int]:
        counts = {
            self.STATUS_OPEN: 0,
            self.STATUS_CLOSED: 0,
            self.STATUS_WAITING_FOR_USER: 0,
            self.STATUS_IN_PROGRESS: 0,
        }
        for issue in self.list_issues():
            status = issue.get("status", self.STATUS_OPEN)
            if status in counts:
                counts[status] += 1
        return counts

    def _save(self, issues: list[dict]) -> None:
        with open(self.file_name, "w", encoding="utf-8") as handle:
            json.dump(issues, handle, indent=4, ensure_ascii=False)