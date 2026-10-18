from __future__ import annotations

import math


def resolve_ocr_region(
    params: dict,
    coordinates: dict | None = None,
    library_entry: dict | None = None,
) -> dict | None:
    explicit = params.get("region")
    entry = library_entry or {}
    boundary = explicit if isinstance(explicit, dict) else entry.get("boundary")
    pattern = entry.get("pattern")
    if (
        not isinstance(explicit, dict)
        and coordinates
        and isinstance(pattern, dict)
        and int(pattern.get("width", 0)) > 0
        and int(pattern.get("height", 0)) > 0
    ):
        # A match anchor already gives the position; the taught pattern gives
        # only the OCR window size, not another absolute search offset.
        boundary = {
            "x": 0, "y": 0,
            "width": pattern["width"], "height": pattern["height"],
        }
    if not isinstance(boundary, dict):
        boundary = None
    if not boundary:
        boundary = coordinates
        if not isinstance(boundary, dict) or not all(
            key in boundary for key in ("x", "y", "width", "height")
        ):
            return None
        anchor_x = anchor_y = 0
    elif isinstance(explicit, dict):
        anchor_x = anchor_y = 0
    else:
        anchor_x = int(coordinates.get("x", 0)) if coordinates else 0
        anchor_y = int(coordinates.get("y", 0)) if coordinates else 0
    result = {
        "x": anchor_x + int(boundary.get("x", 0)) + int(params.get("offset_x", 0)),
        "y": anchor_y + int(boundary.get("y", 0)) + int(params.get("offset_y", 0)),
        "width": int(boundary.get("width", 0)),
        "height": int(boundary.get("height", 0)),
    }
    for key in ("screen_width", "screen_height"):
        if key in boundary:
            result[key] = int(boundary[key])
    return result


def build_offset_search_region(
    anchor: dict,
    target_entry: dict,
    offset_x: int = 0,
    offset_y: int = 0,
    padding_ratio: float = 0.05,
) -> dict | None:
    """Build a small absolute search region around an offset target point."""

    pattern = target_entry.get("pattern") if isinstance(target_entry, dict) else None
    if not isinstance(pattern, dict):
        return None

    target_width = max(1, int(pattern.get("width", 0)))
    target_height = max(1, int(pattern.get("height", 0)))
    anchor_x = float(anchor.get("x", 0))
    anchor_y = float(anchor.get("y", 0))
    anchor_width = float(anchor.get("width", 0))
    anchor_height = float(anchor.get("height", 0))
    target_center_x = anchor_x + anchor_width / 2 + int(offset_x)
    target_center_y = anchor_y + anchor_height / 2 + int(offset_y)
    padding_x = max(1, math.ceil(target_width * padding_ratio))
    padding_y = max(1, math.ceil(target_height * padding_ratio))

    return {
        "x": round(target_center_x - target_width / 2 - padding_x),
        "y": round(target_center_y - target_height / 2 - padding_y),
        "width": target_width + 2 * padding_x,
        "height": target_height + 2 * padding_y,
        "tap_x": round(target_center_x),
        "tap_y": round(target_center_y),
    }
