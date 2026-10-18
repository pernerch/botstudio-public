from dataclasses import dataclass, field


@dataclass
class LibraryEntry:

    entry_id: str

    name: str

    boundary: dict

    pattern: dict

    verify_best: float

    verify_worst: float

    verify_average: float

    verify_success: float

    sample_count: int

    accepted_samples: list

    rejected_samples: list

    used_by: list[str]

    schema_version: int = 2

    detection_type: str = "template"

    detection_profile: dict = field(
        default_factory=lambda: {
            "mode": "template",
            "color_sensitive": True,
            "use_edges": False,
            "use_mask": False,
            "find_all": False,
        }
    )