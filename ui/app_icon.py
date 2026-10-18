import sys

from PySide6.QtGui import QIcon
from services.application_info import STUDIO_APP_USER_MODEL_ID
from services.application_paths import ApplicationPaths


def apply_taskbar_identity() -> None:
    if sys.platform != "win32":
        return
    import ctypes
    from ctypes import wintypes

    setter = ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID
    setter.argtypes = [wintypes.LPCWSTR]
    setter.restype = ctypes.c_long
    result = setter(STUDIO_APP_USER_MODEL_ID)
    if result < 0:
        raise OSError(f"Windows taskbar identity could not be set (HRESULT {result:#x})")


def load_app_icon() -> QIcon:
    directory = ApplicationPaths.discover().resource("ui")
    icon = QIcon()
    for name in ("botstudio.ico", "botstudio.png"):
        path = directory / name
        if path.is_file():
            icon.addFile(str(path))
    if icon.isNull() or icon.pixmap(32, 32).isNull():
        raise RuntimeError(f"Application icon could not be loaded from {directory}")
    return icon
