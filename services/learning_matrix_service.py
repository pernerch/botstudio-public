import json
from pathlib import Path

import cv2


class LearningMatrixService:

    def build_matrix(
        self,
        entry_root: str | Path,
    ) -> dict:

        entry_root = Path(entry_root)

        reference_file = (
            entry_root
            / "pattern_reference.png"
        )

        reference = cv2.imread(
            str(reference_file)
        )

        if reference is None:

            raise FileNotFoundError(
                reference_file
            )

        matrix = {
            "accepted": {},
            "rejected": {},
        }

        self._process_folder(
            matrix["accepted"],
            reference,
            entry_root / "accepted",
        )

        self._process_folder(
            matrix["rejected"],
            reference,
            entry_root / "rejected",
        )

        with open(
            entry_root / "matrix.json",
            "w",
            encoding="utf-8",
        ) as fp:

            json.dump(
                matrix,
                fp,
                indent=4,
            )

        return matrix

    def _process_folder(
        self,
        target,
        reference,
        folder,
    ):

        for crop_file in sorted(
            folder.glob("*_crop.png")
        ):

            crop = cv2.imread(
                str(crop_file)
            )

            if crop is None:
                continue

            resized = cv2.resize(
                crop,
                (
                    reference.shape[1],
                    reference.shape[0],
                ),
            )

            score = cv2.matchTemplate(
                resized,
                reference,
                cv2.TM_CCOEFF_NORMED,
            )[0][0]

            target[
                crop_file.name
            ] = round(
                float(score),
                4,
            )