import cv2

from services.opencv_service import OpenCVService

from services.learning_reference_service import (
    LearningReferenceService,
    REFERENCE_THRESHOLD_DEFAULT,
)


class LearningVerifyRunner:

    def __init__(
        self,
        verify_service,
        screen_service,
        reference_service=None,
    ):
        self.verify_service = verify_service
        self.screen_service = screen_service

        self.reference_service = (
            reference_service
            or LearningReferenceService()
        )

    @staticmethod
    def build_context(
        profile,
        boundary,
        pattern,
    ):
        # value fingerprint; region_id detects swaps, geometry detects edits
        if (
            profile is None
            or boundary is None
            or pattern is None
        ):
            return None

        return {
            "profile_id":
                profile.get("id"),

            "boundary": (
                boundary.region_id,
                boundary.x,
                boundary.y,
                boundary.width,
                boundary.height,
            ),

            "pattern": (
                pattern.region_id,
                pattern.x,
                pattern.y,
                pattern.width,
                pattern.height,
                pattern.region_type,
            ),
        }

    def run(
        self,
        profile,
        boundary,
        pattern,
        samples,
        source_screenshot=None,
        reference=None,
        reference_threshold: float = REFERENCE_THRESHOLD_DEFAULT,
    ):
        if reference is None:

            reference = (
                self.reference_service
                .build_reference(
                    source_screenshot,
                    pattern,
                )
            )

        if reference is None:
            return self._empty_result()

        verify_results = []

        scores = []
        successful = 0

        best_score = 0.0
        best_matches = []

        for _ in range(samples):

            screenshot = (
                self.screen_service.capture(
                    profile
                )
            )

            result = (
                self.verify_service.verify_pattern(
                    screenshot,
                    boundary,
                    reference,
                )
            )

            score = result["score"]

            (
                reference_ok,
                reference_score,
                reference_reason,
            ) = (
                self._validate_reference(
                    screenshot,
                    result["matches"],
                    reference,
                    reference_threshold,
                )
            )

            boundary_ok = (
                score >= 0.85
            )

            accepted = (
                boundary_ok
                and reference_ok
            )

            verify_results.append(
                {
                    "accepted": accepted,
                    "score": score,
                    "reference_score": reference_score,
                    "reference_reason": reference_reason,
                    "matches": result["matches"],
                    "screenshot": screenshot,
                    "boundary": boundary,
                    "pattern": pattern,
                }
            )

            scores.append(score)

            if accepted:
                successful += 1

            if score > best_score:
                best_score = score
                best_matches = (
                    result["matches"]
                )

        if not scores:
            return self._empty_result()

        return {
            "verify_results": verify_results,
            "best": max(scores),
            "worst": min(scores),
            "average": (
                sum(scores)
                / len(scores)
            ),
            "success_rate": (
                successful
                / len(scores)
            )
            * 100,
            "best_matches": best_matches,
            "reference": reference,
            "reference_threshold": reference_threshold,
            "boundary": boundary,
            "pattern": pattern,
            "context": self.build_context(
                profile,
                boundary,
                pattern,
            ),
        }

    def run_color(
        self,
        profile,
        boundary,
        detection_profile,
        samples,
    ):
        detector = OpenCVService()
        boundary_dict = {
            "x": int(boundary.x),
            "y": int(boundary.y),
            "width": int(boundary.width),
            "height": int(boundary.height),
        }
        verify_results = []
        scores = []
        successful = 0
        best_matches = []

        for _ in range(samples):
            screenshot = self.screen_service.capture(profile)
            detections = detector.find_color_all(
                str(screenshot),
                detection_profile,
                boundary_dict,
            )
            accepted = bool(detections)
            score = 1.0 if accepted else 0.0
            matches = [
                self._detection_rect(detection)
                for detection in detections
            ]
            verify_results.append({
                "accepted": accepted,
                "score": score,
                "matches": matches,
                "screenshot": screenshot,
                "boundary": boundary,
                "pattern": None,
            })
            scores.append(score)
            if accepted:
                successful += 1
                if not best_matches:
                    best_matches = matches

        return {
            "verify_results": verify_results,
            "best": max(scores, default=0.0),
            "worst": min(scores, default=0.0),
            "average": sum(scores) / len(scores) if scores else 0.0,
            "success_rate": (successful / len(scores) * 100) if scores else 0.0,
            "best_matches": best_matches,
            "reference": None,
            "reference_threshold": 0.0,
            "boundary": boundary,
            "pattern": None,
            "context": self.build_color_context(
                profile,
                boundary,
                detection_profile,
            ),
        }

    @staticmethod
    def _detection_rect(detection):
        from PySide6.QtCore import QRect

        return QRect(
            detection.x,
            detection.y,
            detection.w,
            detection.h,
        )

    @staticmethod
    def build_color_context(profile, boundary, detection_profile):
        if profile is None or boundary is None:
            return None
        return {
            "profile_id": profile.get("id"),
            "boundary": (
                boundary.region_id,
                boundary.x,
                boundary.y,
                boundary.width,
                boundary.height,
            ),
            "detection_profile": repr(sorted(detection_profile.items())),
        }

    def _empty_result(
        self,
    ):
        # reference stays None so no bundle can carry a reference
        # without matching verify_results
        return {
            "verify_results": [],
            "best": 0,
            "worst": 0,
            "average": 0,
            "success_rate": 0,
            "best_matches": [],
            "reference": None,
            "reference_threshold": REFERENCE_THRESHOLD_DEFAULT,
            "boundary": None,
            "pattern": None,
            "context": None,
        }

    def _validate_reference(
        self,
        screenshot,
        matches,
        reference,
        reference_threshold,
    ):

        if not matches:

            return (
                False,
                0.0,
                "no_match",
            )

        img = cv2.imread(
            screenshot
        )

        if img is None:

            return (
                False,
                0.0,
                "crop_unreadable",
            )

        match_rect = matches[0]

        crop = img[
            match_rect.y():
            match_rect.y()
            + match_rect.height(),

            match_rect.x():
            match_rect.x()
            + match_rect.width()
        ]

        return (
            self.reference_service.validate(
                crop,
                reference,
                reference_threshold,
            )
        )
