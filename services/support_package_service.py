import json
import platform
import shutil
import sys
import zipfile
from datetime import datetime
from pathlib import Path

from services.logging_service import LOG_DIR, get_logger
from services.profile_service import ProfileService
from services.version_service import version_manifest


class SupportPackageService:

    def __init__(
        self,
        profile_service=None,
        logger=None,
    ):

        self.profile_service = (
            profile_service
            or ProfileService()
        )

        self.logger = (
            logger
            or get_logger("runtime")
        )

    def create_package(
        self,
        target_dir: str = "logs/archive",
        runtime_status=None,
        issue_context=None,
    ):

        target_root = Path(
            target_dir
        )

        target_root.mkdir(
            parents=True,
            exist_ok=True,
        )

        stamp = datetime.now().strftime(
            "%Y%m%d_%H%M%S"
        )

        package_path = (
            target_root
            / f"Support_Package_{stamp}.zip"
        )

        with zipfile.ZipFile(
            package_path,
            "w",
            zipfile.ZIP_DEFLATED,
        ) as archive:

            self._write_logs(
                archive
            )

            self._write_json(
                archive,
                "runtime/status.json",
                runtime_status or {},
            )

            self._write_json(
                archive,
                "support/metadata.json",
                self._metadata(),
            )

            self._write_json(
                archive,
                "support/issue_context.json",
                issue_context or {},
            )

            self._write_json(
                archive,
                "profiles/overview.json",
                self.profile_service.load_all(),
            )

            self._write_config_snapshot(
                archive,
            )

        self.logger.info(
            "Support Package Created",
            package=str(package_path),
        )

        return package_path

    @staticmethod
    def _metadata():
        versions = version_manifest()
        return {
            "application": {
                "name": "BotStudio",
                "version": versions["application"],
            },
            "modules": versions["modules"],
            "system": {
                "platform": platform.platform(),
                "python": sys.version,
            },
            "created_at": datetime.now().astimezone().isoformat(),
        }

    def _write_logs(
        self,
        archive,
    ):

        if not LOG_DIR.exists():
            return

        for log_file in LOG_DIR.glob("*.log*"):

            archive.write(
                log_file,
                f"logs/{log_file.name}",
            )

    def _write_json(
        self,
        archive,
        name,
        data,
    ):

        archive.writestr(
            name,
            json.dumps(
                data,
                indent=4,
                ensure_ascii=False,
            ),
        )

    def _write_config_snapshot(
        self,
        archive,
    ):

        config_dir = Path("config")

        if not config_dir.exists():
            return

        for config_file in config_dir.glob("*.json"):

            archive.write(
                config_file,
                f"config/{config_file.name}",
            )
