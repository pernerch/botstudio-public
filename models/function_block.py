from dataclasses import dataclass, field
from uuid import uuid4


@dataclass
class FunctionBlock:
    """One node inside a function graph.

    Branches are named so a block can fan out into several paths
    (found/not_found, success/failed) without a separate edge model.
    """

    block_id: str

    block_type: str

    name: str = ""

    params: dict = field(
        default_factory=dict
    )

    # values the step-debugger can supply when the block runs in isolation
    inputs: dict = field(
        default_factory=dict
    )

    # variable name the result is published under, referenced as "$name"
    output_as: str = ""

    branches: dict = field(
        default_factory=dict
    )

    @classmethod
    def create(
        cls,
        block_type: str,
        name: str = "",
        params: dict | None = None,
        output_as: str = "",
        branches: dict | None = None,
    ) -> "FunctionBlock":

        return cls(
            block_id=str(uuid4()),
            block_type=block_type,
            name=name,
            params=params or {},
            output_as=output_as,
            branches=branches or {},
        )

    def walk(
        self,
        seen=None,
    ):
        """Yields this block and every nested block, each id only once."""

        if seen is None:
            seen = set()

        if self.block_id in seen:
            return

        seen.add(self.block_id)

        yield self

        for children in self.branches.values():

            for child in children:

                yield from child.walk(seen)

    def to_dict(
        self,
    ) -> dict:

        return {
            "block_id": self.block_id,
            "block_type": self.block_type,
            "name": self.name,
            "params": self.params,
            "inputs": self.inputs,
            "output_as": self.output_as,
            # edges live in the connection list; ids keep one block one instance
            "branches": {
                branch: [
                    child.block_id
                    for child in children
                ]
                for branch, children in self.branches.items()
            },
        }

    @classmethod
    def from_dict(
        cls,
        data: dict,
    ) -> "FunctionBlock":

        return cls(
            block_id=data.get(
                "block_id",
                str(uuid4()),
            ),
            block_type=data.get(
                "block_type",
                "",
            ),
            name=data.get(
                "name",
                "",
            ),
            params=data.get(
                "params",
            ) or {},
            inputs=data.get(
                "inputs",
            ) or {},
            output_as=data.get(
                "output_as",
                "",
            ),
            branches={
                branch: [
                    cls.from_dict(child)
                    for child in children
                    if isinstance(child, dict)
                ]
                for branch, children in (
                    data.get("branches") or {}
                ).items()
            },
        )
