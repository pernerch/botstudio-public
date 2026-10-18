import ctypes
from ctypes import wintypes


class _Credential(ctypes.Structure):
    _fields_ = [
        ("Flags", wintypes.DWORD),
        ("Type", wintypes.DWORD),
        ("TargetName", wintypes.LPWSTR),
        ("Comment", wintypes.LPWSTR),
        ("LastWritten", wintypes.FILETIME),
        ("CredentialBlobSize", wintypes.DWORD),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
        ("Persist", wintypes.DWORD),
        ("AttributeCount", wintypes.DWORD),
        ("Attributes", ctypes.c_void_p),
        ("TargetAlias", wintypes.LPWSTR),
        ("UserName", wintypes.LPWSTR),
    ]


class WindowsCredentialService:
    TARGET = "BotStudio.Telegram.BotToken"
    CRED_TYPE_GENERIC = 1
    CRED_PERSIST_LOCAL_MACHINE = 2
    ERROR_NOT_FOUND = 1168

    def __init__(self, api=None):
        if api is not None:
            self._api = api
        else:
            windows_loader = getattr(ctypes, "WinDLL", None)
            if windows_loader is None:
                self._api = None
                return
            self._api = windows_loader("Advapi32.dll", use_last_error=True)
            self._configure_api(self._api)

    def _require_api(self):
        if self._api is None:
            raise OSError("Windows Credential Manager is available only on Windows")

    @staticmethod
    def _configure_api(api):
        api.CredWriteW.argtypes = [ctypes.POINTER(_Credential), wintypes.DWORD]
        api.CredWriteW.restype = wintypes.BOOL
        api.CredReadW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(ctypes.POINTER(_Credential)),
        ]
        api.CredReadW.restype = wintypes.BOOL
        api.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
        api.CredDeleteW.restype = wintypes.BOOL
        api.CredFree.argtypes = [ctypes.c_void_p]
        api.CredFree.restype = None

    def save(self, secret):
        self._require_api()
        if not isinstance(secret, str) or not secret:
            raise ValueError("Credential must be a non-empty string")
        blob = secret.encode("utf-8")
        buffer = (ctypes.c_ubyte * len(blob)).from_buffer_copy(blob)
        credential = _Credential(
            Flags=0,
            Type=self.CRED_TYPE_GENERIC,
            TargetName=self.TARGET,
            Comment=None,
            LastWritten=wintypes.FILETIME(),
            CredentialBlobSize=len(blob),
            CredentialBlob=ctypes.cast(
                buffer,
                ctypes.POINTER(ctypes.c_ubyte),
            ),
            Persist=self.CRED_PERSIST_LOCAL_MACHINE,
            AttributeCount=0,
            Attributes=None,
            TargetAlias=None,
            UserName="BotStudio",
        )
        if not self._api.CredWriteW(ctypes.byref(credential), 0):
            raise ctypes.WinError(ctypes.get_last_error())

    def load(self):
        self._require_api()
        credential_pointer = ctypes.POINTER(_Credential)()
        if not self._api.CredReadW(
            self.TARGET,
            self.CRED_TYPE_GENERIC,
            0,
            ctypes.byref(credential_pointer),
        ):
            error = ctypes.get_last_error()
            if error == self.ERROR_NOT_FOUND:
                return None
            raise ctypes.WinError(error)
        try:
            credential = credential_pointer.contents
            blob = ctypes.string_at(
                credential.CredentialBlob,
                credential.CredentialBlobSize,
            )
            return blob.decode("utf-8")
        finally:
            self._api.CredFree(credential_pointer)

    def delete(self):
        self._require_api()
        if not self._api.CredDeleteW(self.TARGET, self.CRED_TYPE_GENERIC, 0):
            error = ctypes.get_last_error()
            if error != self.ERROR_NOT_FOUND:
                raise ctypes.WinError(error)
