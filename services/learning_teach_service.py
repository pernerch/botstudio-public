# services/learning_teach_service.py

import json
import shutil
import uuid
from pathlib import Path

import cv2

from models.library_entry import LibraryEntry
from services.library_service import LibraryService
from services.learning_matrix_service import (
    LearningMatrixService,
)

from services.learning_matrix_analysis_service import (
    LearningMatrixAnalysisService,
)

from services.learning_reference_service import (
    LearningReferenceService,
    REFERENCE_THRESHOLD_DEFAULT,
)


class LearningTeachService:

    def __init__(self):
        self.library_service = LibraryService()
        self.reference_service = LearningReferenceService()

    def teach(
        self,
        *,
        verify_bundle,
        name,
        verify_best,
        verify_worst,
        verify_average,
        verify_success,
        reference_threshold: float = REFERENCE_THRESHOLD_DEFAULT,
        entry_id: str | None = None,
    ) -> LibraryEntry:

        # geometry and reference come from the bundle, never from live UI state
        verify_results = verify_bundle[
            "verify_results"
        ]

        reference = verify_bundle[
            "reference"
        ]

        boundary = verify_bundle[
            "boundary"
        ]

        pattern = verify_bundle[
            "pattern"
        ]

        if reference is None:

            raise ValueError(
                "Verify bundle has no master reference"
            )

        if not verify_results:

            raise ValueError(
                "Verify bundle has no verify results"
            )

        existing = self.library_service.load(entry_id) if entry_id else None
        entry_id = entry_id or str(uuid.uuid4())

        if not name:
            name = (
                f"{pattern.region_type}_"
                f"{entry_id[:8]}"
            )

        (
            entry_root,
            accepted_dir,
            rejected_dir,
        ) = (
            self.library_service.create_entry_structure(
                entry_id
            )
        )

        if existing:
            for sample_dir in (accepted_dir, rejected_dir):
                for child in sample_dir.iterdir():
                    if child.is_file() or child.is_symlink():
                        child.unlink()
                    elif child.is_dir():
                        shutil.rmtree(child)

        self._save_pattern_reference(
            entry_root=entry_root,
            reference=reference,
        )

        accepted_files = []
        rejected_files = []

        self._process_samples(
            accepted_dir=accepted_dir,
            rejected_dir=rejected_dir,
            verify_results=verify_results,
            pattern=pattern,
            label=name,
            accepted_files=accepted_files,
            rejected_files=rejected_files,
            reference=reference,
            reference_threshold=reference_threshold,
        )

        entry = self._build_library_entry(
            entry_id=entry_id,
            entry_root=entry_root,
            name=name,
            boundary=boundary,
            pattern=pattern,
            verify_results=verify_results,
            verify_best=verify_best,
            verify_worst=verify_worst,
            verify_average=verify_average,
            verify_success=verify_success,
            accepted_files=accepted_files,
            rejected_files=rejected_files,
            used_by=(existing or {}).get("used_by", []),
        )

        self.library_service.save(
            entry
        )

        matrix_service = (
            LearningMatrixService()
        )

        matrix = (
            matrix_service.build_matrix(
                entry_root
            )
        )

        analysis_service = (
            LearningMatrixAnalysisService()
        )

        analysis_service.analyze(
            entry_root,
            matrix,
        )

        return entry

    def replace(
        self,
        *,
        entry_id,
        verify_bundle,
        name,
        verify_best,
        verify_worst,
        verify_average,
        verify_success,
        reference_threshold: float = REFERENCE_THRESHOLD_DEFAULT,
    ) -> LibraryEntry:
        if not self.library_service.load(entry_id):
            raise ValueError("Library entry does not exist")
        return self.teach(
            verify_bundle=verify_bundle,
            name=name,
            verify_best=verify_best,
            verify_worst=verify_worst,
            verify_average=verify_average,
            verify_success=verify_success,
            reference_threshold=reference_threshold,
            entry_id=entry_id,
        )

    def _process_samples(
        self,
        *,
        accepted_dir,
        rejected_dir,
        verify_results,
        pattern,
        label,
        accepted_files,
        rejected_files,
        used_by=None,
        reference,
        reference_threshold,
    ):
        sample_counter = 1

        for result in verify_results:

            screenshot = (
                result["screenshot"]
            )

            matches = (
                result["matches"]
            )

            if not screenshot:
                continue

            if not matches:
                continue

            img = cv2.imread(
                screenshot
            )

            if img is None:
                continue

            match_rect = matches[0]

            crop = img[
                match_rect.y():
                match_rect.y()
                + match_rect.height(),

                match_rect.x():
                match_rect.x()
                + match_rect.width()
            ]

            if crop.size == 0:
                continue

            (
                reference_ok,
                reference_score,
                reference_reason,
            ) = (
                self.reference_service.validate(
                    crop,
                    reference,
                    reference_threshold,
                )
            )

            boundary_ok = bool(
                result["accepted"]
            )

            if not reference_ok:
                decision_reason = (
                    reference_reason
                )

            elif not boundary_ok:
                decision_reason = (
                    "boundary_below_threshold"
                )

            else:
                decision_reason = "ok"

            accepted = (
                boundary_ok
                and reference_ok
            )

            sample_data = (
                self._create_sample_data(
                    img=img,
                    match_rect=match_rect,
                    result=result,
                    pattern=pattern,
                    label=label,
                    accepted=accepted,
                    reference_score=reference_score,
                    reference_threshold=reference_threshold,
                    decision_reason=decision_reason,
                )
            )

            base_name = (
                f"{sample_counter:03}"
            )

            if accepted:
                file_data = (
                    self._save_sample(
                        target_dir=accepted_dir,
                        base_name=base_name,
                        screenshot=screenshot,
                        crop=crop,
                        sample_data=sample_data,
                    )
                )

                accepted_files.append(
                    file_data
                )

            else:
                file_data = (
                    self._save_sample(
                        target_dir=rejected_dir,
                        base_name=base_name,
                        screenshot=screenshot,
                        crop=crop,
                        sample_data=sample_data,
                    )
                )

                rejected_files.append(
                    file_data
                )

            sample_counter += 1

    def _save_sample(
        self,
        *,
        target_dir,
        base_name,
        screenshot,
        crop,
        sample_data,
    ):
        crop_target = (
            target_dir
            / f"{base_name}_crop.png"
        )

        source_target = (
            target_dir
            / f"{base_name}_source.png"
        )

        match_target = (
            target_dir
            / f"{base_name}_match.json"
        )

        self.library_service.copy_sample(
            screenshot,
            source_target,
        )

        cv2.imwrite(
            str(crop_target),
            crop,
        )

        with open(
            match_target,
            "w",
            encoding="utf-8",
        ) as fp:

            json.dump(
                sample_data,
                fp,
                indent=4,
            )

        return {
            "source": str(source_target),
            "crop": str(crop_target),
            "match": str(match_target),
            "score": sample_data["score"],
        }

    def _create_sample_data(
        self,
        *,
        img,
        match_rect,
        result,
        pattern,
        label,
        accepted,
        reference_score,
        reference_threshold,
        decision_reason,
    ):
        img_height = int(
            img.shape[0]
        )

        img_width = int(
            img.shape[1]
        )

        center_x = (
            match_rect.x()
            + match_rect.width() / 2
        )

        center_y = (
            match_rect.y()
            + match_rect.height() / 2
        )

        return {
            "score":
                result["score"],

            "accepted":
                accepted,

            "boundary_score":
                result["score"],

            "boundary_accepted":
                bool(
                    result["accepted"]
                ),

            "reference_score":
                reference_score,

            "reference_threshold":
                reference_threshold,

            "decision_reason":
                decision_reason,

            "matches":
                len(
                    result["matches"]
                ),

            "label":
                label,

            "pattern_type":
                pattern.region_type,

            "resolution": {
                "width": img_width,
                "height": img_height,
            },

            "bbox": {
                "x": match_rect.x(),
                "y": match_rect.y(),
                "width": match_rect.width(),
                "height": match_rect.height(),
            },

            "yolo": {
                "center_x":
                    center_x / img_width,

                "center_y":
                    center_y / img_height,

                "width":
                    match_rect.width()
                    / img_width,

                "height":
                    match_rect.height()
                    / img_height,
            },
        }

    def _build_library_entry(
        self,
        *,
        entry_id,
        entry_root,
        name,
        boundary,
        pattern,
        verify_results,
        verify_best,
        verify_worst,
        verify_average,
        verify_success,
        accepted_files,
        rejected_files,
        used_by=None,
    ):
        reference_path = str(
            entry_root / "pattern_reference.png"
        )

        return LibraryEntry(
            entry_id=entry_id,

            name=name,

            boundary={
                "x": boundary.x,
                "y": boundary.y,
                "width": boundary.width,
                "height": boundary.height,
                "metadata": boundary.metadata,
            },

            pattern={
                "type":
                    pattern.region_type,

                "x":
                    pattern.x,

                "y":
                    pattern.y,

                "width":
                    pattern.width,

                "height":
                    pattern.height,

                "reference_path":
                    reference_path,
            },

            verify_best=float(
                verify_best
            ),

            verify_worst=float(
                verify_worst
            ),

            verify_average=float(
                verify_average
            ),

            verify_success=float(
                verify_success
            ),

            sample_count=len(
                verify_results
            ),

            accepted_samples= accepted_files,

            rejected_samples= rejected_files,

            used_by=list(used_by or []),
        )

    def _save_pattern_reference(
        self,
        *,
        entry_root,
        reference,
    ):

        # persists exactly the array that was used as verify template
        cv2.imwrite(
            str(
                entry_root
                / "pattern_reference.png"
            ),
            reference,
        )
