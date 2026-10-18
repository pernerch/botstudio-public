import ctypes
import subprocess
from ctypes import wintypes


class WindowsProcessHandle:
    SYNCHRONIZE = 0x00100000
    PROCESS_TERMINATE = 0x0001
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    WAIT_OBJECT_0 = 0
    WAIT_TIMEOUT = 258
    INFINITE = 0xFFFFFFFF

    def __init__(self, pid, kernel32=None):
        if type(pid) is not int or pid <= 0:
            raise ValueError("pid must be a positive integer")
        self.pid = pid
        if kernel32 is None:
            windows_loader = getattr(ctypes, "WinDLL", None)
            if windows_loader is None:
                raise OSError("Attaching to a process is available only on Windows")
            kernel32 = windows_loader("kernel32", use_last_error=True)
        self._kernel32 = kernel32
        self._configure_api(self._kernel32)
        self._handle = self._kernel32.OpenProcess(
            self.SYNCHRONIZE
            | self.PROCESS_TERMINATE
            | self.PROCESS_QUERY_LIMITED_INFORMATION,
            False,
            pid,
        )
        if not self._handle:
            raise ctypes.WinError(ctypes.get_last_error())

    @staticmethod
    def _configure_api(api):
        api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        api.OpenProcess.restype = wintypes.HANDLE
        api.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        api.WaitForSingleObject.restype = wintypes.DWORD
        api.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        api.GetExitCodeProcess.restype = wintypes.BOOL
        api.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        api.TerminateProcess.restype = wintypes.BOOL
        api.CloseHandle.argtypes = [wintypes.HANDLE]
        api.CloseHandle.restype = wintypes.BOOL

    def poll(self):
        result = self._kernel32.WaitForSingleObject(self._handle, 0)
        if result == self.WAIT_TIMEOUT:
            return None
        if result != self.WAIT_OBJECT_0:
            raise ctypes.WinError(ctypes.get_last_error())
        exit_code = wintypes.DWORD()
        if not self._kernel32.GetExitCodeProcess(
            self._handle,
            ctypes.byref(exit_code),
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        return exit_code.value

    def terminate(self):
        if self.poll() is not None:
            return
        if not self._kernel32.TerminateProcess(self._handle, 1):
            raise ctypes.WinError(ctypes.get_last_error())

    def kill(self):
        self.terminate()

    def wait(self, timeout=None):
        milliseconds = (
            self.INFINITE
            if timeout is None
            else max(0, min(int(timeout * 1000), self.INFINITE - 1))
        )
        result = self._kernel32.WaitForSingleObject(self._handle, milliseconds)
        if result == self.WAIT_TIMEOUT:
            raise subprocess.TimeoutExpired(str(self.pid), timeout)
        if result != self.WAIT_OBJECT_0:
            raise ctypes.WinError(ctypes.get_last_error())
        exit_code = wintypes.DWORD()
        if not self._kernel32.GetExitCodeProcess(
            self._handle,
            ctypes.byref(exit_code),
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        return exit_code.value

    def close(self):
        if self._handle:
            self._kernel32.CloseHandle(self._handle)
            self._handle = None
