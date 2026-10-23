import sys
import traceback
from services.application_bootstrap import initialize_application
from services.application_info import APP_NAME, APP_VERSION


def install_exception_logging() -> None:

    from services.logging_service import get_logger

    logger = get_logger("application")

    def handle_exception(
        exception_type,
        exception_value,
        exception_traceback,
    ):

        logger.critical(
            "Unhandled Application Exception",
            error_type=exception_type.__name__,
            error=str(exception_value),
            traceback="".join(
                traceback.format_exception(
                    exception_type,
                    exception_value,
                    exception_traceback,
                )
            ),
            next_action="inspect source and reproduce with the affected profile",
        )

        sys.__excepthook__(
            exception_type,
            exception_value,
            exception_traceback,
        )

    sys.excepthook = handle_exception


def _apply_taskbar_identity() -> None:
    from ui.app_icon import apply_taskbar_identity

    apply_taskbar_identity()


def main():

    try:
        initialize_application()
    except (OSError, ValueError) as error:
        from PySide6.QtWidgets import QApplication, QMessageBox
        from ui.i18n.translator import t

        print(f"BotStudio data initialization failed: {error}", file=sys.stderr)
        error_app = QApplication.instance() or QApplication(sys.argv)
        error_app.setApplicationName(APP_NAME)
        QMessageBox.critical(
            None, APP_NAME, t("startup.initialization_failed").format(error=str(error)),
        )
        sys.exit(1)

    from PySide6.QtWidgets import QApplication
    from PySide6.QtCore import QTimer
    from services.theme_service import ThemeService
    from ui.views.main_window import MainWindow
    from ui.app_icon import load_app_icon
    from ui.windows_taskbar import apply_window_taskbar_identity, clear_window_taskbar_identity

    install_exception_logging()

    _apply_taskbar_identity()

    # leftover screenshots from earlier runs must not pile up in TEMP (DEC-162)
    import threading
    from core.emulator import cleanup_screenshots, trim_adb_log

    def _startup_cleanup():
        trim_adb_log()
        cleanup_screenshots(60.0)

    threading.Thread(
        target=_startup_cleanup,
        name="screenshot-startup-cleanup",
        daemon=True,
    ).start()

    app = QApplication(
        sys.argv
    )

    # Fusion is required so the designer-generated QSS (spin/combo/scrollbar
    # arrow icons, radii, ...) renders identically to the designer preview
    app.setStyle("Fusion")

    app.setApplicationName(
        APP_NAME
    )

    app.setApplicationVersion(
        APP_VERSION
    )

    icon = load_app_icon()

    app.setWindowIcon(
        icon
    )

    stylesheet = ThemeService().load_stylesheet()

    if stylesheet:
        app.setStyleSheet(stylesheet)

    window = MainWindow()

    window.setWindowIcon(
        icon
    )

    window.setWindowTitle(
        f"{APP_NAME} --V{APP_VERSION}--"
    )

    window.resize(
        1800,
        1000,
    )

    if app.platformName() == "windows":
        apply_window_taskbar_identity(int(window.winId()))
        app.aboutToQuit.connect(lambda: clear_window_taskbar_identity(int(window.winId())))

    window.show()
    QTimer.singleShot(0, window.telegram_remote_control_view.github_account.restore_session)

    sys.exit(
        app.exec()
    )


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        from services.release_self_test import run_self_test
        sys.exit(run_self_test("studio"))
    else:
        main()