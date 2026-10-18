from pathlib import Path

from PyInstaller.utils.hooks.qt import add_qt6_dependencies


hiddenimports, binaries, datas = add_qt6_dependencies(__file__)
# Studio uses desktop widgets, not Qt's separately licensed virtual keyboard.
binaries = [
    entry for entry in binaries
    if "virtualkeyboard" not in Path(entry[0]).name.lower()
]
