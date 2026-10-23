import ctypes
from ctypes import wintypes
import subprocess
import sys
from uuid import UUID

from services.application_info import APP_NAME, STUDIO_APP_USER_MODEL_ID
from services.application_paths import ApplicationPaths


class _PropertyKey(ctypes.Structure):
    _fields_ = [("fmtid", ctypes.c_byte * 16), ("pid", wintypes.DWORD)]


class _VariantValue(ctypes.Union):
    _fields_ = [("text", ctypes.c_wchar_p), ("storage", ctypes.c_void_p * 2)]


class _PropVariant(ctypes.Structure):
    _anonymous_ = ("value",)
    _fields_ = [
        ("vt", ctypes.c_ushort),
        ("reserved", ctypes.c_ushort * 3),
        ("value", _VariantValue),
    ]


_APP_PROPERTIES = UUID("9f4c2855-9f79-4b39-a8d0-e1d42de1d5f3").bytes_le
_PROPERTY_STORE_IID = UUID("886d8eeb-8cf2-4446-8d02-cdba1dbdcf99").bytes_le
_PROPERTY_IDS = (2, 3, 4, 5)


def _check_result(result: int, operation: str) -> None:
    if result < 0:
        raise OSError(f"Windows taskbar {operation} failed (HRESULT {result:#x})")


class _WindowPropertyStore:
    def __init__(self, hwnd: int):
        getter = ctypes.windll.shell32.SHGetPropertyStoreForWindow
        getter.argtypes = [wintypes.HWND, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
        getter.restype = ctypes.c_long
        self.pointer = ctypes.c_void_p()
        iid = (ctypes.c_byte * 16).from_buffer_copy(_PROPERTY_STORE_IID)
        _check_result(getter(hwnd, ctypes.byref(iid), ctypes.byref(self.pointer)), "property store")
        self.vtable = ctypes.cast(
            self.pointer, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)),
        ).contents

    def __enter__(self):
        return self

    def __exit__(self, *_):
        release = ctypes.WINFUNCTYPE(wintypes.ULONG, ctypes.c_void_p)(self.vtable[2])
        release(self.pointer)

    def _access(self, index: int, pid: int, value: _PropVariant) -> None:
        key = _PropertyKey((ctypes.c_byte * 16).from_buffer_copy(_APP_PROPERTIES), pid)
        method = ctypes.WINFUNCTYPE(
            ctypes.c_long, ctypes.c_void_p,
            ctypes.POINTER(_PropertyKey), ctypes.POINTER(_PropVariant),
        )(self.vtable[index])
        _check_result(method(self.pointer, ctypes.byref(key), ctypes.byref(value)), f"property {pid}")

    def set_string(self, pid: int, text: str | None) -> None:
        value = _PropVariant()
        if text is not None:
            value.vt = 31  # VT_LPWSTR; SetValue copies the caller-owned string.
            value.text = text
        self._access(6, pid, value)

    def get_string(self, pid: int) -> str | None:
        value = _PropVariant()
        clear = ctypes.windll.ole32.PropVariantClear
        clear.argtypes = [ctypes.POINTER(_PropVariant)]
        clear.restype = ctypes.c_long
        try:
            self._access(5, pid, value)
            if value.vt == 0:
                return None
            if value.vt != 31:
                raise RuntimeError(f"Unexpected Windows taskbar property type: {value.vt}")
            return value.text
        finally:
            _check_result(clear(ctypes.byref(value)), "property cleanup")


def apply_window_taskbar_identity(hwnd: int) -> None:
    if sys.platform != "win32":
        return
    paths = ApplicationPaths.discover()
    icon = paths.resource("ui", "botstudio.ico")
    if not icon.is_file():
        raise RuntimeError(f"Windows taskbar icon is missing: {icon}")
    command = [sys.executable]
    if not getattr(sys, "frozen", False):
        command.append(str(paths.program_root / "main.py"))
    with _WindowPropertyStore(hwnd) as store:
        # Relaunch metadata must precede the window AppUserModelID.
        for pid, text in (
            (2, subprocess.list2cmdline(command)),
            (3, f"{icon},0"),
            (4, APP_NAME),
            (5, STUDIO_APP_USER_MODEL_ID),
        ):
            store.set_string(pid, text)


def clear_window_taskbar_identity(hwnd: int) -> None:
    if sys.platform == "win32":
        with _WindowPropertyStore(hwnd) as store:
            for pid in reversed(_PROPERTY_IDS):
                store.set_string(pid, None)


def read_window_taskbar_identity(hwnd: int) -> dict[int, str | None]:
    with _WindowPropertyStore(hwnd) as store:
        return {pid: store.get_string(pid) for pid in _PROPERTY_IDS}
