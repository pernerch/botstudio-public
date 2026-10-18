from dataclasses import dataclass
from pathlib import Path

from models.element_definition import (
    ElementDefinition,
)


@dataclass
class LearningSession:

    screenshot_path: str

    element: ElementDefinition

    zoom_factor: float = 1.0

    is_dirty: bool = False

    verified: bool = False

    released: bool = False