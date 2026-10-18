from dataclasses import dataclass
from uuid import uuid4


@dataclass
class CanvasRegion:

    region_id: str

    region_type: str

    x: int
    y: int

    width: int
    height: int

    color: str

    selected: bool = False

    metadata: dict | None = None

    @classmethod
    def create(
        cls,
        region_type: str,
        x: int,
        y: int,
        width: int,
        height: int,
        color: str,
    ):

        metadata = {}

        #
        # Search Boundary
        #

        if region_type == "boundary":

            metadata = {

                "scope": "region",
            }

        return cls(
            region_id=str(uuid4()),
            region_type=region_type,
            x=x,
            y=y,
            width=width,
            height=height,
            color=color,
            selected=False,
            metadata=metadata,
        )

    @property
    def rect(self):

        from PySide6.QtCore import QRect

        return QRect(
            self.x,
            self.y,
            self.width,
            self.height,
        )

    def contains(
        self,
        other,
    ):

        return self.rect.contains(
            other.rect
        )

    def move_to(
        self,
        x: int,
        y: int,
    ):

        self.x = x
        self.y = y

    def resize_to(
        self,
        width: int,
        height: int,
    ):

        self.width = max(
            10,
            width,
        )

        self.height = max(
            10,
            height,
        )
