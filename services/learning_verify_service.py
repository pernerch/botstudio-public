import cv2

from PySide6.QtCore import QRect


class LearningVerifyService:
    """Stateless matcher. The template is always supplied by the caller."""

    def verify_pattern(
        self,
        screenshot,
        boundary,
        template,
        threshold: float = 0.85,
    ):

        image = cv2.imread(
            screenshot
        )

        if image is None:

            return {
                "score": 0.0,
                "matches": [],
            }

        #
        # Boundary ausschneiden
        #

        boundary_img = image[
            boundary.y : boundary.y + boundary.height,
            boundary.x : boundary.x + boundary.width,
        ]

        if (
            boundary_img.size == 0
            or template is None
            or template.size == 0
        ):

            return {
                "score": 0.0,
                "matches": [],
            }

        if (
            template.shape[0]
            > boundary_img.shape[0]
            or template.shape[1]
            > boundary_img.shape[1]
        ):

            return {
                "score": 0.0,
                "matches": [],
            }

        #
        # Match
        #

        result = cv2.matchTemplate(
            boundary_img,
            template,
            cv2.TM_CCOEFF_NORMED,
        )

        locations = zip(
            *(
                result >= threshold
            ).nonzero()[::-1]
        )

        matches = []

        best_score = float(
            result.max()
        )

        template_h = (
            template.shape[0]
        )

        template_w = (
            template.shape[1]
        )

        for x, y in locations:

            matches.append(

                QRect(
                    boundary.x + x,
                    boundary.y + y,
                    template_w,
                    template_h,
                )

            )

        return {

            "score": best_score,

            "matches": matches,
        }