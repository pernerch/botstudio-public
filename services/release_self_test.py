import argparse
from pathlib import Path
import sys
import traceback
from types import SimpleNamespace
from uuid import uuid4

from services.application_bootstrap import initialize_application
from services.application_info import APP_VERSION, STUDIO_APP_USER_MODEL_ID
from services.application_paths import ApplicationPaths
from services.file_io import atomic_write_json


def _check_package_features(data_root: Path) -> None:
    from PySide6.QtGui import QImage
    from models.bot_function import BotFunction
    from services.package_service import PackageService
    from ui.widgets.package_markdown_editor import PackageMarkdownEditor
    from ui.widgets.package_preparation_dialog import PackagePreparationDialog

    root = data_root / "package-self-test"
    function_root = root / "functions"
    function = BotFunction(
        function_id="synthetic", game_id="synthetic", name="SYNTHETIC SELF TEST",
        saved=True,
    )
    atomic_write_json(function_root / "synthetic" / "synthetic.json", function.to_dict())
    image_path = root / "screenshot.png"
    image = QImage(2, 2, QImage.Format.Format_RGBA8888)
    image.fill(0xFF336699)
    if not image.save(str(image_path), "PNG"):
        raise RuntimeError("Package self-test image could not be saved")
    service = PackageService(
        function_root=str(function_root),
        library_service=SimpleNamespace(root=root / "library"),
        variable_service=SimpleNamespace(root=root / "variables"),
        game_service=SimpleNamespace(get_game=lambda game_id: {}),
    )
    metadata = {
        "package_id": "function-synthetic-self-test", "package_version": "1.0.1",
        "publisher": {"provider": "github", "account_id": "1", "login": "self-test"},
        "author": "SYNTHETIC SELF TEST", "license_id": "LicenseRef-SelfTest",
        "license_text": "Synthetic local executable test data. Not a catalog submission.",
        "rights_confirmed": True,
    }
    markdown = "## Package self-test\n\n![Screenshot](content/package-assets/image-01.png)"
    assets = {"content/package-assets/image-01.png": image_path}
    preview = service.preview_function_package(
        function, description=markdown, profile_id="",
        package_metadata=metadata, package_assets=assets,
    )
    ready = service.prepare_function_directory(
        function, str(root / "ready"), description=markdown, profile_id="",
        package_metadata=metadata, package_assets=assets,
        expected_checksums=preview["checksums"],
    )
    archive = service.create_ready_archive(
        str(ready), str(root / "synthetic.botstudio.zip"),
        expected_checksums=preview["checksums"],
    )
    inspected = service.inspect_package(str(archive))
    if inspected["description"] != markdown or inspected["checksums"] != preview["checksums"]:
        raise RuntimeError("Bundled package description/image roundtrip failed")
    editor = PackageMarkdownEditor(markdown, assets)
    try:
        if "Package self-test" not in editor.preview.toPlainText():
            raise RuntimeError("Bundled Markdown editor failed to render its preview")
        if PackagePreparationDialog.__name__ != "PackagePreparationDialog":
            raise RuntimeError("Bundled package preparation dialog is missing")
    finally:
        editor.deleteLater()


def run_self_test(role: str) -> int:
    """Opt-in packaging check without IPC, autostart, credentials or emulators."""
    parser = argparse.ArgumentParser(description="Isolated BotStudio executable self-test")
    parser.add_argument("--self-test", action="store_true", required=True)
    parser.add_argument("--data-directory", required=True)
    args = parser.parse_args()
    data_root = Path(args.data_directory)
    if not data_root.is_absolute():
        parser.error("--data-directory must be absolute")
    if data_root.exists() and any(data_root.iterdir()):
        parser.error("--data-directory must be empty or not yet exist")
    paths = ApplicationPaths.discover()
    report = {"role": role, "version": APP_VERSION, "frozen": bool(getattr(sys, "frozen", False))}
    try:
        initialize_application(ApplicationPaths(data_root, paths.resource_root, paths.program_root))
        from PySide6.QtCore import QTimer
        from PySide6.QtWidgets import QApplication, QWidget
        from ui.app_icon import apply_taskbar_identity, load_app_icon
        from services.theme_service import ThemeService
        from ui.i18n.translator import set_language, t

        if role == "studio":
            apply_taskbar_identity()
        app = QApplication.instance() or QApplication(sys.argv[:1])
        app.setStyle("Fusion")
        app.setApplicationName("BotStudio")
        app.setApplicationVersion(APP_VERSION)
        icon = load_app_icon()
        if icon.isNull():
            raise RuntimeError("Bundled application icon is missing")
        for size in (16, 32, 48, 256):
            if icon.pixmap(size, size).isNull():
                raise RuntimeError(f"Bundled application icon could not be decoded at size {size}")
        app.setWindowIcon(icon)
        report["application_icon_loaded"] = True
        stylesheet = ThemeService().load_stylesheet()
        if not stylesheet or "@ICON_DIR@" in stylesheet:
            raise RuntimeError("Bundled stylesheet is missing or unresolved")
        for resource in (
            ("ui", "styles", "icons"),
            ("assets", "flags"),
            ("ui", "i18n", "locales"),
        ):
            if not paths.resource(*resource).is_dir():
                raise RuntimeError(f"Bundled resource directory is missing: {resource}")
        set_language("de")
        if t("common.cancel") != "Abbrechen":
            raise RuntimeError("Bundled German translation is missing")
        set_language("en")
        app.setStyleSheet(stylesheet)
        if role == "studio":
            from ui.views.main_window import MainWindow
            import cv2
            import numpy as np
            from services.ocr_service import OcrService

            report["main_window_import"] = MainWindow.__name__
            image = np.full((64, 320, 3), 255, dtype=np.uint8)
            cv2.putText(
                image, "01:23:45", (4, 48), cv2.FONT_HERSHEY_SIMPLEX,
                1.4, (0, 0, 0), 2, cv2.LINE_AA,
            )
            image_path = data_root / "ocr-self-test.png"
            if not cv2.imwrite(str(image_path), image):
                raise RuntimeError("OCR self-test image could not be saved")
            # Network is deliberately forbidden: a release must ship its models.
            import requests
            original_request = requests.sessions.Session.request

            def reject_network(*args, **kwargs):
                raise RuntimeError("OCR self-test attempted a network download")

            requests.sessions.Session.request = reject_network
            try:
                value = OcrService().read(str(image_path), None, expect="time")
            finally:
                requests.sessions.Session.request = original_request
            if value != "01:23:45":
                raise RuntimeError(f"Bundled OCR recognition failed: {value!r}")
            report["offline_ocr"] = value
            from services.watchdog_ipc_service import WatchdogIpcServer

            # The real window starts IPC and scan workers. Empty test profiles
            # and a unique endpoint keep these away from any productive Studio.
            WatchdogIpcServer.SERVER_NAME = f"botstudio-self-test-{uuid4().hex}"
            window = MainWindow()
            if window.game_service.load() or window.profile_runtime_service.profile_service.load_all():
                raise RuntimeError("Self-test unexpectedly contains personal game/profile data")
            report["main_window_created"] = True
            _check_package_features(data_root)
            report["package_roundtrip"] = True
            report["markdown_editor"] = True
        elif role == "watchdog":
            from services.watchdog_autostart_service import WatchdogAutoStartService
            from ui.watchdog_tray import WatchdogTray

            report["tray_import"] = WatchdogTray.__name__
            startup, _ = WatchdogAutoStartService().build_commands(
                paths.program_root, 1, data_root,
            )
            report["startup_command"] = startup
            window = QWidget()
        else:
            raise ValueError(f"Unsupported self-test role: {role}")
        window.setWindowIcon(icon)
        window.show()
        if not window.isVisible():
            raise RuntimeError("Qt window could not be shown")
        app.processEvents()
        if role == "studio" and sys.platform == "win32":
            import ctypes
            from ctypes import wintypes

            shell = ctypes.windll.shell32
            getter = shell.GetCurrentProcessExplicitAppUserModelID
            getter.argtypes = [ctypes.POINTER(ctypes.c_void_p)]
            getter.restype = ctypes.c_long
            identifier = ctypes.c_void_p()
            if getter(ctypes.byref(identifier)) < 0:
                raise RuntimeError("Windows process has no taskbar identity")
            free = ctypes.windll.ole32.CoTaskMemFree
            free.argtypes = [ctypes.c_void_p]
            free.restype = None
            try:
                report["taskbar_app_id"] = ctypes.wstring_at(identifier)
            finally:
                free(identifier)
            if report["taskbar_app_id"] != STUDIO_APP_USER_MODEL_ID:
                raise RuntimeError("Windows taskbar identity does not match the Studio shortcut")
            if app.platformName() == "windows":
                send = ctypes.windll.user32.SendMessageW
                send.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
                send.restype = ctypes.c_ssize_t
                # WM_GETICON validates the actual HWND, not just Qt's icon object.
                for kind, name in ((0, "native_small_icon"), (1, "native_large_icon")):
                    report[name] = bool(send(int(window.winId()), 0x007F, kind, 0))
                    if not report[name]:
                        raise RuntimeError(f"Native Studio window has no {name}")
        QTimer.singleShot(150, window.close)
        app.exec()
        report["result"] = "SUCCESS"
    except Exception as error:
        report.update(result="FAILURE", error=str(error), traceback=traceback.format_exc())
    atomic_write_json(data_root / "self-test-result.json", report)
    return 0 if report["result"] == "SUCCESS" else 1
