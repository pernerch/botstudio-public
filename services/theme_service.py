from pathlib import Path
import json
from services.application_paths import ApplicationPaths

# must match qss_generator.ICON_DIR_TOKEN in sandbox/designer (kept separate
# so services/ has no dependency on the sandbox design tool)
_ICON_DIR_TOKEN = "@ICON_DIR@"

POINTS_TO_PIXELS = 4 / 3


class ThemeService:

    COMPACT_FONT_SCALE = 0.5
    COMPACT_FONT_MIN_PX = 12

    def __init__(
        self,
        file_name: str | None = None,
    ):

        self.file_name = (
            Path(file_name) if file_name is not None
            else ApplicationPaths.discover().resource("ui", "styles", "generated.qss")
        )

        self.theme_file = (
            self.file_name.parent
            / "theme.json"
        )

    def load_theme(
        self,
    ) -> dict:

        if not self.theme_file.exists():
            return {}

        try:
            with open(
                self.theme_file,
                "r",
                encoding="utf-8",
            ) as fp:

                return json.load(fp)

        except (json.JSONDecodeError, OSError):
            return {}

    def get_font_size_px(
        self,
        scale: float = 1.0,
        minimum_px: int = 1,
    ) -> int:
        # the theme stores points while the floor is defined in pixels
        points = self.load_theme().get(
            "font_size",
            11,
        )

        pixels = (
            float(points)
            * POINTS_TO_PIXELS
            * scale
        )

        return max(
            minimum_px,
            int(round(pixels)),
        )

    def get_compact_font_size_px(
        self,
    ) -> int:

        return self.get_font_size_px(
            self.COMPACT_FONT_SCALE,
            self.COMPACT_FONT_MIN_PX,
        )

    def load_stylesheet(
        self,
    ) -> str:

        if not self.file_name.exists():
            return ""

        with open(
            self.file_name,
            "r",
            encoding="utf-8",
        ) as fp:

            qss = fp.read()

        icon_dir = (
            self.file_name.resolve().parent / "icons"
        ).as_posix()

        return qss.replace(_ICON_DIR_TOKEN, icon_dir)
