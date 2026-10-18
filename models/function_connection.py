from dataclasses import dataclass


@dataclass(frozen=True)
class FunctionConnection:
    source_block_id: str
    source_outcome: str
    target_block_id: str
    target_input: str = "input"
    negated: bool = False

    def to_dict(self) -> dict:
        return {
            "source_block_id": self.source_block_id,
            "source_outcome": self.source_outcome,
            "target_block_id": self.target_block_id,
            "target_input": self.target_input,
            "negated": self.negated,
        }

    @classmethod
    def from_dict(cls, data: dict | None) -> "FunctionConnection":
        data = data or {}
        return cls(
            source_block_id=data.get("source_block_id", ""),
            source_outcome=data.get("source_outcome", ""),
            target_block_id=data.get("target_block_id", ""),
            target_input=data.get("target_input", "input"),
            negated=bool(data.get("negated", False)),
        )

    def matches_source(
        self,
        source_block_id: str,
        source_outcome: str | None = None,
    ) -> bool:
        if source_outcome is not None:
            return (
                self.source_block_id == source_block_id
                and self.source_outcome == source_outcome
            )
        return self.source_block_id == source_block_id

    def matches_target(
        self,
        target_block_id: str,
        target_input: str | None = None,
    ) -> bool:
        if target_input is not None:
            return (
                self.target_block_id == target_block_id
                and self.target_input == target_input
            )
        return self.target_block_id == target_block_id

    def involves_block(self, block_id: str) -> bool:
        return (
            self.source_block_id == block_id
            or self.target_block_id == block_id
        )

