import json
from pathlib import Path


class LearningMatrixAnalysisService:

    def analyze(
        self,
        entry_root,
        matrix,
    ):

        accepted = list(
            matrix["accepted"].values()
        )

        rejected = list(
            matrix["rejected"].values()
        )

        analysis = {
            "accepted_count": len(
                accepted
            ),
            "rejected_count": len(
                rejected
            ),
            "accepted_min": 0.0,
            "accepted_avg": 0.0,
            "accepted_max": 0.0,
            "rejected_min": 0.0,
            "rejected_avg": 0.0,
            "rejected_max": 0.0,
            "recommended_threshold": 0.0,
        }

        if accepted:

            analysis[
                "accepted_min"
            ] = round(
                min(accepted),
                4,
            )

            analysis[
                "accepted_avg"
            ] = round(
                sum(accepted)
                / len(accepted),
                4,
            )

            analysis[
                "accepted_max"
            ] = round(
                max(accepted),
                4,
            )

            analysis[
                "recommended_threshold"
            ] = round(
                min(accepted),
                4,
            )

        if rejected:

            analysis[
                "rejected_min"
            ] = round(
                min(rejected),
                4,
            )

            analysis[
                "rejected_avg"
            ] = round(
                sum(rejected)
                / len(rejected),
                4,
            )

            analysis[
                "rejected_max"
            ] = round(
                max(rejected),
                4,
            )

        with open(
            Path(entry_root)
            / "matrix_analysis.json",
            "w",
            encoding="utf-8",
        ) as fp:

            json.dump(
                analysis,
                fp,
                indent=4,
            )

        return analysis