from dataclasses import dataclass
from pathlib import Path
import random

import cv2
import numpy as np


@dataclass
class DetectionResult:

    found: bool

    score: float

    x: int
    y: int

    w: int
    h: int


class OpenCVService:

    DEFAULT_THRESHOLD = 0.80

    def find_best(
        self,
        screenshot_path: str,
        template_path: str,
        threshold: float = DEFAULT_THRESHOLD,
        region: dict | None = None,
        detection_profile: dict | None = None,
    ) -> DetectionResult:

        screenshot = cv2.imread(screenshot_path)
        template = cv2.imread(template_path)

        if screenshot is None or template is None:
            return DetectionResult(False, 0.0, 0, 0, 0, 0)

        screenshot, template = self._prepare_images(
            screenshot,
            template,
            detection_profile,
        )

        offset_x = 0
        offset_y = 0
        if region:
            offset_x = max(0, int(region.get("x", 0)))
            offset_y = max(0, int(region.get("y", 0)))
            width = max(0, int(region.get("width", 0)))
            height = max(0, int(region.get("height", 0)))
            if width and height:
                screenshot = screenshot[
                    offset_y:offset_y + height,
                    offset_x:offset_x + width,
                ]

        if (
            screenshot.shape[0] < template.shape[0]
            or screenshot.shape[1] < template.shape[1]
        ):
            return DetectionResult(False, 0.0, 0, 0, 0, 0)

        result = cv2.matchTemplate(
            screenshot,
            template,
            cv2.TM_CCORR_NORMED,
        )

        (
            _,
            max_val,
            _,
            max_loc,
        ) = cv2.minMaxLoc(
            result
        )

        h, w = template.shape[:2]

        return DetectionResult(
            found=max_val >= threshold,
            score=float(max_val),
            x=max_loc[0] + offset_x,
            y=max_loc[1] + offset_y,
            w=w,
            h=h,
        )

    def find_all(
        self,
        screenshot_path: str,
        template_path: str,
        threshold: float = DEFAULT_THRESHOLD,
        detection_profile: dict | None = None,
        region: dict | None = None,
    ):

        screenshot = cv2.imread(
            screenshot_path
        )

        template = cv2.imread(
            template_path
        )

        if screenshot is None or template is None:
            return []

        offset_x = 0
        offset_y = 0
        if region:
            offset_x = max(0, int(region.get("x", 0)))
            offset_y = max(0, int(region.get("y", 0)))
            width = max(0, int(region.get("width", 0)))
            height = max(0, int(region.get("height", 0)))
            if width and height:
                screenshot = screenshot[
                    offset_y:offset_y + height,
                    offset_x:offset_x + width,
                ]

        screenshot, template = self._prepare_images(
            screenshot,
            template,
            detection_profile,
        )

        if (
            screenshot.shape[0] < template.shape[0]
            or screenshot.shape[1] < template.shape[1]
        ):
            return []

        result = cv2.matchTemplate(
            screenshot,
            template,
            cv2.TM_CCORR_NORMED,
        )

        y_locations, x_locations = np.where(
            result >= threshold
        )

        h, w = template.shape[:2]

        detections = []

        for x, y in zip(
            x_locations,
            y_locations,
        ):
            detections.append(
                DetectionResult(
                    found=True,
                    score=float(
                        result[y, x]
                    ),
                    x=int(x) + offset_x,
                    y=int(y) + offset_y,
                    w=w,
                    h=h,
                )
            )

        return detections

    def find_color_all(
        self,
        screenshot_path: str,
        detection_profile: dict,
        region: dict | None = None,
    ):
        screenshot = cv2.imread(screenshot_path)
        if screenshot is None:
            return []

        offset_x = 0
        offset_y = 0
        if region:
            offset_x = max(0, int(region.get("x", 0)))
            offset_y = max(0, int(region.get("y", 0)))
            width = max(0, int(region.get("width", 0)))
            height = max(0, int(region.get("height", 0)))
            if width and height:
                screenshot = screenshot[
                    offset_y:offset_y + height,
                    offset_x:offset_x + width,
                ]

        hsv = cv2.cvtColor(screenshot, cv2.COLOR_BGR2HSV)
        target = detection_profile.get("target_hsv", [0, 0, 0])
        tolerance = detection_profile.get("tolerance_hsv", [10, 40, 40])
        hue, saturation, value = [int(item) for item in target]
        hue_tolerance, saturation_tolerance, value_tolerance = [
            int(item) for item in tolerance
        ]
        lower_saturation = max(0, saturation - saturation_tolerance)
        upper_saturation = min(255, saturation + saturation_tolerance)
        lower_value = max(0, value - value_tolerance)
        upper_value = min(255, value + value_tolerance)

        lower_hue = hue - hue_tolerance
        upper_hue = hue + hue_tolerance
        if lower_hue < 0:
            first = cv2.inRange(
                hsv,
                (0, lower_saturation, lower_value),
                (upper_hue, upper_saturation, upper_value),
            )
            second = cv2.inRange(
                hsv,
                (180 + lower_hue, lower_saturation, lower_value),
                (179, upper_saturation, upper_value),
            )
            mask = cv2.bitwise_or(first, second)
        elif upper_hue > 179:
            first = cv2.inRange(
                hsv,
                (lower_hue, lower_saturation, lower_value),
                (179, upper_saturation, upper_value),
            )
            second = cv2.inRange(
                hsv,
                (0, lower_saturation, lower_value),
                (upper_hue - 180, upper_saturation, upper_value),
            )
            mask = cv2.bitwise_or(first, second)
        else:
            mask = cv2.inRange(
                hsv,
                (max(0, hue - hue_tolerance), lower_saturation, lower_value),
                (min(179, hue + hue_tolerance), upper_saturation, upper_value),
            )

        kernel_size = max(1, int(detection_profile.get("morphology_kernel", 1)))
        if kernel_size > 1:
            kernel = cv2.getStructuringElement(
                cv2.MORPH_ELLIPSE,
                (kernel_size, kernel_size),
            )
            mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)
            mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)

        minimum_area = max(1, int(detection_profile.get("min_area", 1)))
        component_count, labels, stats, _ = cv2.connectedComponentsWithStats(mask)
        detections = []
        for component in range(1, component_count):
            area = int(stats[component, cv2.CC_STAT_AREA])
            if area < minimum_area:
                continue
            x = int(stats[component, cv2.CC_STAT_LEFT]) + offset_x
            y = int(stats[component, cv2.CC_STAT_TOP]) + offset_y
            width = int(stats[component, cv2.CC_STAT_WIDTH])
            height = int(stats[component, cv2.CC_STAT_HEIGHT])
            detections.append(
                DetectionResult(True, float(area), x, y, width, height)
            )
        return sorted(detections, key=lambda item: item.score, reverse=True)

    def find_color_best(
        self,
        screenshot_path: str,
        detection_profile: dict,
        region: dict | None = None,
    ) -> DetectionResult:
        detections = self.find_color_all(
            screenshot_path,
            detection_profile,
            region,
        )
        return detections[0] if detections else DetectionResult(False, 0.0, 0, 0, 0, 0)

    @staticmethod
    def _prepare_images(screenshot, template, detection_profile=None):
        profile = detection_profile or {}
        color_sensitive = bool(profile.get("color_sensitive", True))
        use_grayscale = bool(profile.get("use_grayscale", not color_sensitive))
        use_edges = bool(profile.get("use_edges", False))

        if use_grayscale or use_edges:
            screenshot = cv2.cvtColor(screenshot, cv2.COLOR_BGR2GRAY)
            template = cv2.cvtColor(template, cv2.COLOR_BGR2GRAY)

        if use_edges:
            screenshot = cv2.Canny(screenshot, 50, 150)
            template = cv2.Canny(template, 50, 150)

        return screenshot, template

    def get_detection_region(
        self,
        detection: DetectionResult,
    ):

        return (
            detection.x,
            detection.y,
            detection.w,
            detection.h,
        )

    def get_tap_region(
        self,
        detection: DetectionResult,
        padding: int = 3,
    ):

        return (
            detection.x + padding,
            detection.y + padding,
            detection.w - padding * 2,
            detection.h - padding * 2,
        )

    def get_random_tap_point(
        self,
        detection: DetectionResult,
        padding: int = 3,
    ):

        x = random.randint(
            detection.x + padding,
            detection.x + detection.w - padding,
        )

        y = random.randint(
            detection.y + padding,
            detection.y + detection.h - padding,
        )

        return x, y

    def draw_detection(
        self,
        screenshot_path: str,
        detection: DetectionResult,
        output_file: str,
    ):

        output = Path(
            output_file
        )

        output.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        image = cv2.imread(
            screenshot_path
        )

        # ----------------------------------
        # Detection Region (rot)
        # ----------------------------------

        cv2.rectangle(
            image,
            (
                detection.x,
                detection.y,
            ),
            (
                detection.x + detection.w,
                detection.y + detection.h,
            ),
            (0, 0, 255),
            2,
        )

        # ----------------------------------
        # Tap Region (grün)
        # ----------------------------------

        tap_x, tap_y, tap_w, tap_h = (
            self.get_tap_region(
                detection
            )
        )

        cv2.rectangle(
            image,
            (
                tap_x,
                tap_y,
            ),
            (
                tap_x + tap_w,
                tap_y + tap_h,
            ),
            (0, 255, 0),
            1,
        )

        # ----------------------------------
        # Beispiel Tap Punkt (gelb)
        # ----------------------------------

        random_x, random_y = (
            self.get_random_tap_point(
                detection
            )
        )

        cv2.circle(
            image,
            (
                random_x,
                random_y,
            ),
            5,
            (0, 255, 255),
            -1,
        )

        cv2.putText(
            image,
            "TAP",
            (
                random_x + 8,
                random_y,
            ),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.4,
            (0, 255, 255),
            1,
        )

        # ----------------------------------
        # Match Score
        # ----------------------------------

        cv2.putText(
            image,
            f"{detection.score:.4f}",
            (
                detection.x,
                max(
                    20,
                    detection.y - 5,
                ),
            ),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.5,
            (0, 0, 255),
            1,
        )

        cv2.imwrite(
            str(output),
            image,
        )

        return output
