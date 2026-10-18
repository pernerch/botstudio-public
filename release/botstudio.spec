from pathlib import Path
import runpy

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, collect_submodules, copy_metadata
from PyInstaller.utils.win32.versioninfo import (
    FixedFileInfo, StringFileInfo, StringTable, StringStruct, VarFileInfo,
    VarStruct, VSVersionInfo,
)


root = Path(SPECPATH).parent
info = runpy.run_path(str(root / "services" / "application_info.py"))
notice_collector = runpy.run_path(str(root / "release" / "collect_notices.py"))
native_distribution = runpy.run_path(str(root / "release" / "native_distribution.py"))
notices = notice_collector["collect_notices"](
    root / "release" / "requirements-runtime.txt", root / "build" / "third-party",
)
version = info["APP_VERSION"]
version_tuple = tuple(int(part) for part in version.split("."))
if len(version_tuple) != 4:
    raise ValueError("APP_VERSION must contain four numeric components")

resources = [
    (str(root / "LICENSE"), "licenses/BotStudio"),
    (str(root / "LICENSE-NOTICE.txt"), "licenses/BotStudio"),
    (str(root / "ui" / "botstudio.ico"), "ui"),
    (str(root / "ui" / "botstudio.png"), "ui"),
    (str(root / "ui" / "styles" / "generated.qss"), "ui/styles"),
    (str(root / "ui" / "styles" / "theme.json"), "ui/styles"),
    (str(root / "ui" / "styles" / "icons"), "ui/styles/icons"),
    (str(root / "ui" / "i18n" / "locales"), "ui/i18n/locales"),
    (str(root / "assets" / "flags"), "assets/flags"),
]
# Only the implemented ONNX OCR backend is shipped, not optional training stacks.
ocr_data = collect_data_files("rapidocr") + copy_metadata("rapidocr")
ocr_binaries = collect_dynamic_libs("rapidocr")
ocr_imports = collect_submodules(
    "rapidocr",
    filter=lambda name: not any(
        backend in name for backend in (".pytorch", ".paddle", ".openvino", ".tensorrt", ".mnn")
    ),
)
excluded = ["torch", "torchvision", "ultralytics", "paddle", "openvino", "pytest", "tkinter"]


def executable_metadata(name):
    return VSVersionInfo(
        ffi=FixedFileInfo(
            filevers=version_tuple, prodvers=version_tuple,
            mask=0x3F, flags=0, OS=0x40004, fileType=0x1, subtype=0, date=(0, 0),
        ),
        kids=[
            StringFileInfo([StringTable("040904B0", [
                StringStruct("FileDescription", name),
                StringStruct("FileVersion", version),
                StringStruct("InternalName", name),
                StringStruct("OriginalFilename", name + ".exe"),
                StringStruct("ProductName", info["APP_NAME"]),
                StringStruct("ProductVersion", version),
            ])]),
            VarFileInfo([VarStruct("Translation", [1033, 1200])]),
        ],
    )


studio = Analysis(
    [str(root / "main.py")], pathex=[str(root)],
    binaries=ocr_binaries, datas=resources + ocr_data + notices,
    hookspath=[str(root / "release" / "hooks")],
    hiddenimports=ocr_imports + ["onnxruntime"],
    excludes=excluded, noarchive=False,
)
studio.binaries = native_distribution["exclude_unused_native_entries"](studio.binaries)
studio.datas = native_distribution["exclude_unused_native_entries"](studio.datas)
watchdog = Analysis(
    [str(root / "watchdog_app.py")], pathex=[str(root)],
    binaries=[], datas=resources, hiddenimports=[],
    hookspath=[str(root / "release" / "hooks")],
    excludes=excluded, noarchive=False,
)
watchdog.binaries = native_distribution["exclude_unused_native_entries"](watchdog.binaries)
watchdog.datas = native_distribution["exclude_unused_native_entries"](watchdog.datas)
studio_exe = EXE(
    PYZ(studio.pure), studio.scripts, [],
    exclude_binaries=True, name="BotStudio",
    console=False, upx=False, icon=str(root / "ui" / "botstudio.ico"),
    version=executable_metadata("BotStudio"),
)
watchdog_exe = EXE(
    PYZ(watchdog.pure), watchdog.scripts, [],
    exclude_binaries=True, name="BotStudioWatchdog",
    console=False, upx=False, icon=str(root / "ui" / "botstudio.ico"),
    version=executable_metadata("BotStudioWatchdog"),
)
COLLECT(
    studio_exe, watchdog_exe, studio.binaries, studio.datas,
    watchdog.binaries, watchdog.datas,
    name="BotStudio", upx=False,
)
