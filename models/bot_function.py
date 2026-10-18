from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import re
from uuid import uuid4

from models.function_block import FunctionBlock
from models.function_connection import FunctionConnection
from models.github_account import GitHubAccount


FUNCTION_KIND_HELPER = "helper"
FUNCTION_KIND_REGULAR = "regular"

FUNCTION_KINDS = (
    FUNCTION_KIND_HELPER,
    FUNCTION_KIND_REGULAR,
)

# helper functions are sealed capsules and never appear in the scheduler
SCHEDULABLE_KINDS = (
    FUNCTION_KIND_REGULAR,
)

EXPERT_ONLY_KINDS = (
    FUNCTION_KIND_HELPER,
)

OUTCOME_SUCCESS = "success"
OUTCOME_FAILED = "failed"

DEFAULT_OUTCOMES = (
    OUTCOME_SUCCESS,
    OUTCOME_FAILED,
)

SCHEDULE_MODE_MANUAL = "manual"
SCHEDULE_MODE_FIXED = "fixed"
SCHEDULE_MODE_DYNAMIC = "dynamic"


def utc_now_iso() -> str:

    return datetime.now(
        timezone.utc
    ).isoformat()


@dataclass
class FunctionInterface:
    """The contract a caller sees; the body stays closed."""

    inputs: list = field(
        default_factory=list
    )

    outcomes: list = field(
        default_factory=lambda: list(DEFAULT_OUTCOMES)
    )

    returns: dict = field(
        default_factory=dict
    )

    def to_dict(
        self,
    ) -> dict:

        return {
            "inputs": self.inputs,
            "outcomes": self.outcomes,
            "returns": self.returns,
        }

    @classmethod
    def from_dict(
        cls,
        data: dict | None,
    ) -> "FunctionInterface":

        data = data or {}

        return cls(
            inputs=data.get("inputs") or [],
            outcomes=data.get("outcomes") or list(DEFAULT_OUTCOMES),
            returns=data.get("returns") or {},
        )


@dataclass
class HelperDependency:
    """Records which helper revision a function was built and tested against."""

    function_id: str

    name: str = ""

    tested_revision: int = 0

    def to_dict(
        self,
    ) -> dict:

        return {
            "function_id": self.function_id,
            "name": self.name,
            "tested_revision": self.tested_revision,
        }

    @classmethod
    def from_dict(
        cls,
        data: dict,
    ) -> "HelperDependency":

        return cls(
            function_id=data.get("function_id", ""),
            name=data.get("name", ""),
            tested_revision=int(
                data.get("tested_revision", 0)
            ),
        )


@dataclass
class FunctionRevision:

    revision: int

    changed_at: str

    author: str

    note: str = ""

    account: GitHubAccount | None = None

    def to_dict(
        self,
    ) -> dict:

        return {
            "revision": self.revision,
            "changed_at": self.changed_at,
            "author": self.author,
            "note": self.note,
            "account": self.account.to_dict() if self.account is not None else None,
        }

    @classmethod
    def from_dict(
        cls,
        data: dict,
    ) -> "FunctionRevision":

        return cls(
            revision=int(
                data.get("revision", 1)
            ),
            changed_at=data.get("changed_at", ""),
            author=data.get("author", ""),
            note=data.get("note", ""),
            account=(
                GitHubAccount.from_dict(data["account"])
                if data.get("account") is not None else None
            ),
        )


@dataclass
class FunctionSchedule:
    """Fixed runs use interval or wall clock, dynamic runs wait for a cooldown."""

    mode: str = SCHEDULE_MODE_MANUAL

    interval_seconds: int = 0

    run_at_local: list = field(
        default_factory=list
    )

    cooldown_variable: str = ""

    cooldown_margin_seconds: int = 0

    def to_dict(
        self,
    ) -> dict:

        return {
            "mode": self.mode,
            "interval_seconds": self.interval_seconds,
            "run_at_local": self.run_at_local,
            "cooldown_variable": self.cooldown_variable,
            "cooldown_margin_seconds": self.cooldown_margin_seconds,
        }

    @classmethod
    def from_dict(
        cls,
        data: dict | None,
    ) -> "FunctionSchedule":

        data = data or {}

        return cls(
            mode=data.get(
                "mode",
                SCHEDULE_MODE_MANUAL,
            ),
            interval_seconds=int(
                data.get("interval_seconds", 0)
            ),
            run_at_local=data.get("run_at_local") or [],
            cooldown_variable=data.get("cooldown_variable", ""),
            cooldown_margin_seconds=int(
                data.get("cooldown_margin_seconds", 0)
            ),
        )


@dataclass(frozen=True)
class FunctionPackageOrigin:
    """A declared delivery source, not verified authorship or update permission."""

    publisher_key: str
    package_id: str
    package_version: str
    game_id: str
    function_id: str
    revision: int
    source_sha256: str

    def __post_init__(self) -> None:
        for name in ("publisher_key", "package_id", "package_version", "game_id",
                     "function_id", "source_sha256"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"Function package origin requires {name}")
        if not re.fullmatch(r"github:[1-9][0-9]{0,19}", self.publisher_key):
            raise ValueError("Function package origin publisher key is invalid")
        if type(self.revision) is not int or self.revision < 1:
            raise ValueError("Function package origin revision must be a positive integer")
        if not re.fullmatch(r"[0-9a-f]{64}", self.source_sha256):
            raise ValueError("Function package origin source hash is invalid")

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "FunctionPackageOrigin":
        if not isinstance(data, dict) or set(data) != set(cls.__dataclass_fields__):
            raise ValueError("invalid Function package origin fields")
        return cls(**data)

    @classmethod
    def from_list(cls, data: list) -> list["FunctionPackageOrigin"]:
        if not isinstance(data, list):
            raise ValueError("Function package_origins must be a list")
        return [cls.from_dict(item) for item in data]


@dataclass
class BotFunction:

    function_id: str

    name: str

    game_id: str

    kind: str = FUNCTION_KIND_REGULAR

    description: str = ""

    author: str = ""

    created_at: str = ""

    revision: int = 1

    revised_at: str = ""

    enabled: bool = True

    saved: bool = False

    interface: FunctionInterface = field(
        default_factory=FunctionInterface
    )

    blocks: list = field(
        default_factory=list
    )

    layout_positions: dict = field(
        default_factory=dict
    )

    connections: list = field(
        default_factory=list
    )

    library_entries: list = field(
        default_factory=list
    )

    helper_dependencies: list = field(
        default_factory=list
    )

    used: list = field(
        default_factory=list
    )

    used_by: list[str] = field(
        default_factory=list
    )

    schedule: FunctionSchedule = field(
        default_factory=FunctionSchedule
    )

    history: list = field(
        default_factory=list
    )

    package_origins: list[FunctionPackageOrigin] = field(default_factory=list)

    creator_account: GitHubAccount | None = None
    _creator_assignment_pending: bool = field(default=False, repr=False, compare=False)

    @classmethod
    def create(
        cls,
        name: str,
        game_id: str,
        author: str,
        kind: str = FUNCTION_KIND_REGULAR,
        description: str = "",
    ) -> "BotFunction":

        stamp = utc_now_iso()

        return cls(
            function_id=str(uuid4()),
            name=name,
            game_id=game_id,
            kind=kind,
            description=description,
            author=author,
            created_at=stamp,
            revision=1,
            revised_at=stamp,
            saved=False,
            _creator_assignment_pending=True,
            history=[
                FunctionRevision(
                    revision=1,
                    changed_at=stamp,
                    author=author,
                    note="created",
                )
            ],
        )

    def is_schedulable(
        self,
    ) -> bool:

        return self.kind in SCHEDULABLE_KINDS

    def requires_expert_mode(
        self,
    ) -> bool:

        return self.kind in EXPERT_ONLY_KINDS

    def walk_blocks(
        self,
    ):

        seen = set()

        for block in self.blocks:

            yield from block.walk(seen)

    def normalize_graph(
        self,
    ) -> None:
        """Collapses duplicated block copies so one block id means one instance."""

        index = {}
        ordered = []

        for block in self.blocks:

            for node in block.walk():

                if node.block_id in index:
                    continue

                index[node.block_id] = node
                ordered.append(node)

        self.blocks = ordered

        for block in ordered:
            block.branches = {}

        for connection in self.connections:

            source = index.get(
                connection.source_block_id
            )

            target = index.get(
                connection.target_block_id
            )

            if not source or not target:
                continue

            targets = source.branches.setdefault(
                connection.source_outcome,
                [],
            )

            if target not in targets:
                targets.append(target)

    def remove_connection(
        self,
        source_block_id: str,
        source_outcome: str | None = None,
        target_block_id: str | None = None,
        target_input: str | None = None,
    ) -> list:
        """Removes matching connection(s) and synchronizes block branches."""

        removed = []
        remaining = []

        for conn in self.connections:
            match = conn.source_block_id == source_block_id
            if source_outcome is not None and conn.source_outcome != source_outcome:
                match = False
            if target_block_id is not None and conn.target_block_id != target_block_id:
                match = False
            if target_input is not None and conn.target_input != target_input:
                match = False

            if match:
                removed.append(conn)
            else:
                remaining.append(conn)

        self.connections = remaining
        self.normalize_graph()
        return removed

    def remove_block_connections(
        self,
        block_id: str,
    ) -> list:
        """Removes all connections involving the given block (source or target)."""

        removed = []
        remaining = []

        for conn in self.connections:
            if conn.source_block_id == block_id or conn.target_block_id == block_id:
                removed.append(conn)
            else:
                remaining.append(conn)

        self.connections = remaining
        self.normalize_graph()
        return removed

    def get_outgoing_connections(
        self,
        block_id: str,
    ) -> list:

        return [
            conn
            for conn in self.connections
            if conn.source_block_id == block_id
        ]

    def get_incoming_connections(
        self,
        block_id: str,
    ) -> list:

        return [
            conn
            for conn in self.connections
            if conn.target_block_id == block_id
        ]


    def collect_library_entries(
        self,
    ) -> list:
        """Library ids referenced anywhere in the graph, so image swaps stay traceable."""

        found = []

        for block in self.walk_blocks():

            entry_id = block.params.get(
                "library_entry_id"
            )

            if entry_id and entry_id not in found:
                found.append(entry_id)

        return found

    def collect_helper_ids(
        self,
    ) -> list:

        found = []

        for block in self.walk_blocks():

            function_id = block.params.get(
                "function_id"
            )

            if function_id and function_id not in found:
                found.append(function_id)

        return found

    def bump_revision(
        self,
        author: str,
        note: str = "",
        account: GitHubAccount | None = None,
    ) -> None:

        stamp = utc_now_iso()
        if (
            self._creator_assignment_pending
            and account is not None and self.creator_account is None
            and not self.saved and not self.package_origins and not self.author
            and self.revision == 1 and len(self.history) == 1
            and self.history[0].note == "created"
        ):
            self.creator_account = account
            self.author = f"@{account.login}"
            self.history[0].author = self.author
            self.history[0].account = account

        self._creator_assignment_pending = False
        self.revision += 1
        self.revised_at = stamp

        self.history.append(
            FunctionRevision(
                revision=self.revision,
                changed_at=stamp,
                author=f"@{account.login}" if account is not None else author,
                note=note,
                account=account,
            )
        )

    def to_dict(
        self,
    ) -> dict:

        return {
            "function_id": self.function_id,
            "name": self.name,
            "game_id": self.game_id,
            "kind": self.kind,
            "description": self.description,
            "author": self.author,
            "creator_account": (
                self.creator_account.to_dict() if self.creator_account is not None else None
            ),
            "created_at": self.created_at,
            "revision": self.revision,
            "revised_at": self.revised_at,
            "enabled": self.enabled,
            "saved": self.saved,
            "interface": self.interface.to_dict(),
            "blocks": [
                block.to_dict()
                for block in self.blocks
            ],
            "layout_positions": self.layout_positions,
            "connections": [
                connection.to_dict()
                for connection in self.connections
            ],
            "library_entries": self.library_entries,
            "helper_dependencies": [
                item.to_dict()
                for item in self.helper_dependencies
            ],
            "used": self.used,
            "used_by": self.used_by,
            "schedule": self.schedule.to_dict(),
            "history": [
                item.to_dict()
                for item in self.history
            ],
            "package_origins": [
                item.to_dict()
                for item in self.package_origins
            ],
        }

    @classmethod
    def from_dict(
        cls,
        data: dict,
    ) -> "BotFunction":

        function = cls(
            function_id=data.get(
                "function_id",
                str(uuid4()),
            ),
            name=data.get("name", ""),
            game_id=data.get("game_id", ""),
            kind=data.get(
                "kind",
                FUNCTION_KIND_REGULAR,
            ),
            description=data.get("description", ""),
            author=data.get("author", ""),
            creator_account=(
                GitHubAccount.from_dict(data["creator_account"])
                if data.get("creator_account") is not None else None
            ),
            created_at=data.get("created_at", ""),
            revision=int(
                data.get("revision", 1)
            ),
            revised_at=data.get("revised_at", ""),
            enabled=bool(
                data.get("enabled", True)
            ),
            saved=bool(
                data.get("saved", True)
            ),
            interface=FunctionInterface.from_dict(
                data.get("interface")
            ),
            blocks=[
                FunctionBlock.from_dict(block)
                for block in data.get("blocks") or []
            ],
            layout_positions=data.get(
                "layout_positions",
                {},
            ) or {},
            connections=[
                FunctionConnection.from_dict(connection)
                for connection in data.get("connections") or []
            ],
            library_entries=data.get("library_entries") or [],
            helper_dependencies=[
                HelperDependency.from_dict(item)
                for item in data.get("helper_dependencies") or []
            ],
            used=data.get("used") or [],
            used_by=data.get("used_by") or [],
            schedule=FunctionSchedule.from_dict(
                data.get("schedule")
            ),
            history=[
                FunctionRevision.from_dict(item)
                for item in data.get("history") or []
            ],
            package_origins=FunctionPackageOrigin.from_list(data.get("package_origins", [])),
        )

        function.normalize_graph()

        return function
