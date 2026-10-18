import re
import time
import unicodedata
from collections import Counter
from difflib import SequenceMatcher
from pathlib import Path

import cv2

_TIME_PATTERN = re.compile(
    r"(?<!\d)(?:(\d+)\s*[dD]\s*)?(\d{1,2})\s*[:.]\s*(\d{2})\s*[:.]\s*(\d{2})(?!\d)"
)
_TIME_DIGIT_TRANSLATION = str.maketrans({
    "O": "0",
    "o": "0",
    "Ö": "6",
    "ö": "6",
})
_COUNTER_PATTERN = re.compile(r"(\d+)\s*/\s*(\d+)")


class OcrReadError(RuntimeError):
    """Raised when no OCR attempt produced a value matching the expected shape."""


class OcrService:
    """Reads text from a selected learned library boundary using RapidOCR.

    The crop handed in here is already scoped to a fachlich gelernte Search
    Boundary (DEC-079), so the text location within the crop is already known.
    RapidOCR's own text detector is therefore skipped by default: on a tight
    boundary crop it reliably finds nothing (no surrounding context to form a
    box) and costs roughly 20x the latency of the recognizer alone. Detection
    and orientation classification are only re-enabled as a fallback when the
    fast recognition-only pass comes back empty, e.g. for a loosely learned
    boundary with extra margin around the text.
    """

    _engine = None

    def __init__(self, app_config_service=None):
        # kept for backwards compatible construction; RapidOCR needs no config
        self.app_config_service = app_config_service

    def read(
        self,
        screenshot_path: str,
        boundary: dict | None,
        expect: str = "text",
        language: str = "",
        counter_part: str = "left",
    ) -> str:

        image = cv2.imread(screenshot_path)
        if image is None:
            raise RuntimeError("OCR screenshot could not be read")

        crop = self._crop(image, boundary)

        if expect == "time":
            return self._read_time(crop)
        if expect == "counter":
            return self._read_counter(crop, part=counter_part)
        if expect == "number":
            return self._read_number(crop)
        if expect == "alphanumeric":
            return self._read_alphanumeric(crop)
        return self._read_text(crop)

    def find_text(
        self,
        screenshot_path: str,
        query: str,
        region: dict | None = None,
    ) -> dict | None:
        """Find a text fragment and return its screen-space OCR region."""

        image = cv2.imread(screenshot_path)
        if image is None:
            raise RuntimeError("OCR screenshot could not be read")

        region = region or {}
        image_height, image_width = image.shape[:2]
        reference_width = max(1, int(region.get("screen_width") or image_width))
        reference_height = max(1, int(region.get("screen_height") or image_height))
        scale_x = image_width / reference_width
        scale_y = image_height / reference_height
        offset_x = max(0, round(max(0, int(region.get("x", 0))) * scale_x))
        offset_y = max(0, round(max(0, int(region.get("y", 0))) * scale_y))
        source_width = max(0, int(region.get("width", 0)))
        source_height = max(0, int(region.get("height", 0)))
        has_region = source_width > 0 and source_height > 0
        width = max(1, round(source_width * scale_x)) if has_region else 0
        height = max(1, round(source_height * scale_y)) if has_region else 0
        crop = image
        if has_region:
            offset_x = min(offset_x, image_width)
            offset_y = min(offset_y, image_height)
            width = min(width, image_width - offset_x)
            height = min(height, image_height - offset_y)
            crop = image[offset_y:offset_y + height, offset_x:offset_x + width]

        if crop.size == 0:
            raise RuntimeError("OCR search region is outside the screenshot")

        wanted = " ".join(str(query or "").casefold().split())
        if not wanted:
            return None

        engine = self._engine_instance()
        candidates = []
        for candidate_image, coordinate_scale in (
            (crop, 1.0),
            (self._upscale(crop), None),
        ):
            if coordinate_scale is None:
                coordinate_scale = candidate_image.shape[1] / max(1, crop.shape[1])
            result = engine(candidate_image, use_det=True, use_cls=True)
            candidates.extend(
                self._line_candidates(
                    result,
                    coordinate_scale,
                )
            )

        query_compact = self._compact_search_text(wanted)
        best = None
        best_score = 0.0
        for candidate in candidates:
            candidate_compact = self._compact_search_text(candidate["text"])
            if not candidate_compact:
                continue
            if query_compact in candidate_compact:
                score = 1.0
                matched_candidate = self._match_token_geometry(
                    candidate,
                    query_compact,
                )
            else:
                score = SequenceMatcher(
                    None,
                    query_compact,
                    candidate_compact,
                ).ratio()
                minimum_score = 0.82 if len(query_compact) < 8 else 0.72
                if score < minimum_score:
                    continue
                matched_candidate = candidate
            if score > best_score:
                best = matched_candidate
                best_score = score

        if best is not None:
            return {
                "x": best["x"] + offset_x,
                "y": best["y"] + offset_y,
                "width": best["width"],
                "height": best["height"],
                "text": best["text"],
                "score": best_score,
            }

        return None

    @staticmethod
    def _compact_search_text(value: str) -> str:
        normalized = unicodedata.normalize("NFKC", str(value or "")).casefold()
        # Keep letters and digits, including non-ASCII names, while removing
        # OCR spacing and punctuation differences such as [RBL] Marty vs RBLMarty.
        return "".join(char for char in normalized if char.isalnum())

    @classmethod
    def _line_candidates(cls, result, coordinate_scale: float) -> list[dict]:
        texts = cls._texts(result)
        boxes_raw = getattr(result, "boxes", None) if result is not None else None
        boxes = list(boxes_raw) if boxes_raw is not None and len(boxes_raw) else []
        tokens = []
        for index, text in enumerate(texts):
            if index >= len(boxes) or not str(text).strip():
                continue
            points = boxes[index]
            xs = [float(point[0]) / coordinate_scale for point in points]
            ys = [float(point[1]) / coordinate_scale for point in points]
            tokens.append({
                "text": str(text).strip(),
                "x": min(xs),
                "y": min(ys),
                "right": max(xs),
                "bottom": max(ys),
            })
        tokens.sort(key=lambda token: (token["y"], token["x"]))

        lines = []
        for token in tokens:
            height = max(1.0, token["bottom"] - token["y"])
            matching_line = next(
                (
                    line
                    for line in reversed(lines)
                    if abs(token["y"] - line["y"]) <= max(height, line["height"]) * 0.65
                ),
                None,
            )
            if matching_line is None:
                lines.append({
                    "tokens": [token],
                    "x": token["x"],
                    "y": token["y"],
                    "right": token["right"],
                    "bottom": token["bottom"],
                    "height": height,
                })
            else:
                matching_line["tokens"].append(token)
                matching_line["right"] = max(matching_line["right"], token["right"])
                matching_line["bottom"] = max(matching_line["bottom"], token["bottom"])
                matching_line["height"] = matching_line["bottom"] - matching_line["y"]

        candidates = []
        for line in lines:
            line["tokens"].sort(key=lambda token: token["x"])
            text = " ".join(token["text"] for token in line["tokens"])
            candidates.append({
                "text": text,
                "x": round(line["x"]),
                "y": round(line["y"]),
                "width": max(1, round(line["right"] - line["x"])),
                "height": max(1, round(line["bottom"] - line["y"])),
                "parts": [
                    {
                        "text": token["text"],
                        "x": round(token["x"]),
                        "y": round(token["y"]),
                        "right": round(token["right"]),
                        "bottom": round(token["bottom"]),
                    }
                    for token in line["tokens"]
                ],
            })
        return candidates

    @classmethod
    def _match_token_geometry(cls, candidate: dict, query_compact: str) -> dict:
        parts = candidate.get("parts") or []
        if len(parts) < 2:
            return candidate
        compact_parts = [cls._compact_search_text(part["text"]) for part in parts]
        combined = "".join(compact_parts)
        start = combined.find(query_compact)
        if start < 0:
            return candidate
        end = start + len(query_compact)
        cursor = 0
        matching = []
        for part, compact_part in zip(parts, compact_parts):
            part_start = cursor
            part_end = cursor + len(compact_part)
            cursor = part_end
            if part_end > start and part_start < end:
                matching.append(part)
        if not matching:
            return candidate
        left = min(part["x"] for part in matching)
        top = min(part["y"] for part in matching)
        right = max(part["right"] for part in matching)
        bottom = max(part["bottom"] for part in matching)
        narrowed = dict(candidate)
        narrowed.update({
            "x": left,
            "y": top,
            "width": max(1, right - left),
            "height": max(1, bottom - top),
        })
        return narrowed

    # note: every _read_* below accepts an optional `engine` override so tests
    # can inject a fake RapidOCR engine instead of loading the real ONNX models

    @staticmethod
    def _texts(result) -> list:
        # result.txts is a numpy array; bool() on it raises once it holds
        # more than one recognized text, so length must be checked explicitly.
        if result is None:
            return []
        txts = getattr(result, "txts", None)
        if txts is None or len(txts) == 0:
            return []
        return list(txts)

    # -- crop handling ---------------------------------------------------

    @staticmethod
    def _crop(image, boundary: dict | None):

        if boundary:
            image_height, image_width = image.shape[:2]
            reference_width = max(1, int(boundary.get("screen_width") or image_width))
            reference_height = max(1, int(boundary.get("screen_height") or image_height))
            scale_x = image_width / reference_width
            scale_y = image_height / reference_height
            x = max(0, round(max(0, int(boundary.get("x", 0))) * scale_x))
            y = max(0, round(max(0, int(boundary.get("y", 0))) * scale_y))
            width = max(0, round(int(boundary.get("width", 0)) * scale_x))
            height = max(0, round(int(boundary.get("height", 0)) * scale_y))
            if width and height:
                image = image[y:y + height, x:x + width]

        if image.size == 0:
            raise RuntimeError("OCR boundary is outside the screenshot")

        return image

    # -- engine ------------------------------------------------------------

    @classmethod
    def _engine_instance(cls):

        if cls._engine is None:
            from rapidocr import RapidOCR
            cls._engine = RapidOCR()
        return cls._engine

    @staticmethod
    def _upscale(image):
        """Small crops (thin timer strips, single digits) need more pixels."""

        height, width = image.shape[:2]
        smallest_side = min(height, width) or 1
        if smallest_side >= 60:
            return image

        factor = 60 / smallest_side
        return cv2.resize(
            image,
            None,
            fx=factor,
            fy=factor,
            interpolation=cv2.INTER_CUBIC,
        )

    @staticmethod
    def _join(texts) -> str:

        return " ".join(text.strip() for text in texts if text.strip())

    def _fast_texts(self, image, engine) -> list[str]:
        """Recognition-only pass on the raw crop and, if small, an upscaled copy."""

        upscaled = self._upscale(image)
        candidate_images = [image] if upscaled is image else [image, upscaled]

        texts = []
        for candidate_image in candidate_images:
            result = engine(candidate_image, use_det=False, use_cls=False)
            found = self._texts(result)
            if found:
                texts.append(self._join(found))
        return texts

    def _escalated_texts(self, image, engine):
        """Full detect(+classify) pipeline, only tried once the fast pass fails."""

        for use_det, use_cls in ((True, False), (True, True)):
            result = engine(image, use_det=use_det, use_cls=use_cls)
            found = self._texts(result)
            if found:
                yield self._join(found)

    @staticmethod
    def _most_common(values: list[str]) -> str:

        if not values:
            return ""
        counts = Counter(values)
        best_value, best_count = counts.most_common(1)[0]
        if best_count > 1:
            return best_value
        # no agreement between the fast passes, fall back to the longest reading
        return max(values, key=len)

    # -- free text ----------------------------------------------------------

    @staticmethod
    def _dump_failed_crop(image, expect: str) -> str:
        """Save the crop the engine failed to read for manual inspection."""

        try:
            out_dir = Path("runs") / "ocr_debug"
            out_dir.mkdir(parents=True, exist_ok=True)
            stamp = time.strftime("%Y%m%d_%H%M%S")
            path = out_dir / f"ocr_fail_{expect}_{stamp}_{time.time_ns() % 10**6:06d}.png"
            cv2.imwrite(str(path), image)
            return str(path)
        except Exception:
            return ""

    def _read_text(self, image, engine=None) -> str:

        engine = engine or self._engine_instance()
        fast = self._fast_texts(image, engine)
        if fast:
            result = self._most_common(fast)
            if result:
                return result

        for text in self._escalated_texts(image, engine):
            if text:
                return text
        raise OcrReadError(
            f"OCR could not resolve any text from the boundary "
            f"(crop saved to {self._dump_failed_crop(image, 'text')})"
        )

    # -- digits ---------------------------------------------------------------

    def _read_number(self, image, engine=None) -> str:

        engine = engine or self._engine_instance()
        digits = [
            "".join(char for char in text if char.isdigit())
            for text in self._fast_texts(image, engine)
        ]
        digits = [value for value in digits if value]
        if digits:
            return self._most_common(digits)

        for text in self._escalated_texts(image, engine):
            value = "".join(char for char in text if char.isdigit())
            if value:
                return value
        raise OcrReadError(
            f"OCR could not resolve a number from the boundary "
            f"(crop saved to {self._dump_failed_crop(image, 'number')})"
        )

    def _read_alphanumeric(self, image, engine=None) -> str:

        engine = engine or self._engine_instance()
        cleaned = [
            "".join(char for char in text if char.isalnum())
            for text in self._fast_texts(image, engine)
        ]
        cleaned = [value for value in cleaned if value]
        if cleaned:
            return self._most_common(cleaned)

        for text in self._escalated_texts(image, engine):
            value = "".join(char for char in text if char.isalnum())
            if value:
                return value
        raise OcrReadError(
            f"OCR could not resolve an alphanumeric value from the boundary "
            f"(crop saved to {self._dump_failed_crop(image, 'alphanumeric')})"
        )

    # -- counters, e.g. "5/5" or "20/100" -------------------------------------

    def _read_counter(self, image, engine=None, part: str = "left") -> str:
        """Read a "current/total" style value and return the selected side.

        The learned boundary typically covers both numbers around the slash;
        part="left" (default) returns the current amount, part="right" the
        total, e.g. 20 of 100 redeemable.
        """

        group_index = 2 if str(part).strip().lower() == "right" else 1

        engine = engine or self._engine_instance()
        matches = []
        for text in self._fast_texts(image, engine):
            match = _COUNTER_PATTERN.search(text)
            if match:
                matches.append(match.group(group_index))

        if not matches:
            for text in self._escalated_texts(image, engine):
                match = _COUNTER_PATTERN.search(text)
                if match:
                    matches.append(match.group(group_index))
                    break

        result = self._most_common(matches)
        if result:
            return result

        raise OcrReadError(
            f"OCR could not resolve a current/total counter from the boundary "
            f"(crop saved to {self._dump_failed_crop(image, 'counter')})"
        )

    # -- time / durations ------------------------------------------------------

    def _read_time(self, image, engine=None) -> str:
        """Read timer text from bright glyphs on changing colored backgrounds."""

        engine = engine or self._engine_instance()
        matches = []
        for text in self._fast_texts(image, engine):
            normalized = self._normalize_time(text)
            if self._is_time(normalized):
                matches.append(normalized)

        if not matches:
            for text in self._escalated_texts(image, engine):
                normalized = self._normalize_time(text)
                if self._is_time(normalized):
                    matches.append(normalized)
                    break

        for match in matches:
            if "d " in match:
                return match

        if matches:
            return self._most_common(matches)

        # never hand a scheduler a string with stray characters: fail loudly
        # so the caller can retry the whole read (fresh screenshot) instead.
        raise OcrReadError(
            f"OCR could not resolve a valid duration from the boundary "
            f"(crop saved to {self._dump_failed_crop(image, 'time')})"
        )

    @staticmethod
    def _normalize_time(value: str) -> str:
        """Keep a duration as an optional day prefix followed by a clock value."""

        compact = value.strip().translate(_TIME_DIGIT_TRANSLATION)
        compact = re.sub(r"\s+", " ", compact)
        match = _TIME_PATTERN.search(compact)
        if not match:
            return compact

        days, hours, minutes, seconds = match.groups()
        clock = f"{int(hours):02d}:{minutes}:{seconds}"
        return f"{int(days)}d {clock}" if days else clock

    @staticmethod
    def _is_time(value: str) -> bool:

        return bool(re.fullmatch(r"(?:\d+d )?\d{2}:\d{2}:\d{2}", value))
