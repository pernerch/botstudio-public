# services/learning_reference_service.py

import cv2


REFERENCE_THRESHOLD_DEFAULT = 0.85

REASON_OK = "ok"
REASON_REFERENCE_MISSING = "reference_missing"
REASON_BELOW_THRESHOLD = "reference_below_threshold"
REASON_CROP_UNREADABLE = "crop_unreadable"


class LearningReferenceService:
    """Single owner of the master reference (pattern_reference.png)."""

    def build_reference(
        self,
        screenshot,
        pattern,
    ):

        if not screenshot:
            return None

        img = cv2.imread(
            screenshot
        )

        if img is None:
            return None

        reference = img[
            pattern.y:
            pattern.y + pattern.height,

            pattern.x:
            pattern.x + pattern.width,
        ]

        if reference.size == 0:
            return None

        return reference

    def score(
        self,
        crop,
        reference,
    ) -> float:

        if crop is None or reference is None:
            return 0.0

        if crop.size == 0 or reference.size == 0:
            return 0.0

        resized = cv2.resize(
            reference,
            (
                crop.shape[1],
                crop.shape[0],
            ),
        )

        result = cv2.matchTemplate(
            crop,
            resized,
            cv2.TM_CCOEFF_NORMED,
        )

        return float(
            result.max()
        )

    def validate(
        self,
        crop,
        reference,
        threshold: float = REFERENCE_THRESHOLD_DEFAULT,
    ):

        if reference is None:

            return (
                False,
                0.0,
                REASON_REFERENCE_MISSING,
            )

        if crop is None or crop.size == 0:

            return (
                False,
                0.0,
                REASON_CROP_UNREADABLE,
            )

        reference_score = self.score(
            crop,
            reference,
        )

        if reference_score < threshold:

            return (
                False,
                reference_score,
                REASON_BELOW_THRESHOLD,
            )

        return (
            True,
            reference_score,
            REASON_OK,
        )
