from pathlib import Path


UNUSED_NATIVE_FILES = frozenset({
    "qpdf.dll",
    "qt6pdf.dll",
    "opencv_videoio_ffmpeg500_64.dll",
})


def exclude_unused_native_entries(entries):
    return [
        entry for entry in entries
        if Path(str(entry[0]).replace("\\", "/")).name.casefold() not in UNUSED_NATIVE_FILES
    ]
