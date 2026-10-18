from dataclasses import dataclass, field
from typing import Optional


# ----------------------------------
# Regions
# ----------------------------------

@dataclass
class Region:

    x: int
    y: int

    width: int
    height: int


# ----------------------------------
# OCR
# ----------------------------------

@dataclass
class OCRDefinition:

    ocr_type: str

    mask: Optional[str] = None

    whitelist: Optional[str] = None

    upscale: bool = True

    grayscale: bool = True

    threshold: bool = False


# ----------------------------------
# Verification
# ----------------------------------

@dataclass
class VerificationStats:

    runs: int = 0

    success_count: int = 0

    failed_count: int = 0

    average_score: float = 0.0

    best_score: float = 0.0

    worst_score: float = 0.0


# ----------------------------------
# Element
# ----------------------------------

@dataclass
class ElementDefinition:

    #
    # Lifecycle
    #

    status: str = "draft"

    #
    # Assigned after verification
    #

    element_id: Optional[str] = None

    name: Optional[str] = None

    #
    # Type
    #

    element_type: str = "image"

    #
    # Regions
    #

    detection_region: Optional[Region] = None

    tap_region: Optional[Region] = None

    ocr_region: Optional[Region] = None

    #
    # Image recognition
    #

    templates: list[str] = field(
        default_factory=list
    )

    threshold: float = 0.80

    #
    # OCR
    #

    ocr: Optional[OCRDefinition] = None

    #
    # Verification
    #

    verification: VerificationStats = field(
        default_factory=VerificationStats
    )