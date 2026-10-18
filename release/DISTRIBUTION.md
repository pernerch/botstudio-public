# BotStudio distribution materials

## Application license

BotStudio's own applicable material is GPL-3.0-only, copyright Christian
Perner. See the accompanying `LICENSE` and `LICENSE-NOTICE.txt`. Third-party
software and user packages retain their respective terms. A private GitHub
repository does not change that license; it restricts access to its contents.

The GPL text is displayed by the installer and installed under
`_internal/licenses/BotStudio`. Do not describe this already implemented part
as a missing license choice.

## Installed third-party materials

`_internal/third_party/inventory.json` identifies the exact build-interpreter
packages and checksums of their collected license files. Each package
directory contains its wheel metadata, wheel-supplied notices and, where
needed, an `upstream` directory of supplementary license texts.
`distribution_materials.json` records the version-specific upstream URLs,
text digests and the three bundled OCR model identities.

The supplemental texts are copied verbatim from the referenced upstream
releases. ANTLR's upstream text also contains its JavaScript-related MIT
notices; those notices are retained, not removed. No root `NOTICE` file was
listed in the Flatbuffers v25.12.19 or RapidOCR v3.9.2 release trees when
checked on 2026-10-18. This does not establish the absence of obligations in
their dependencies.

The RapidAI ModelScope model repository declares Apache License 2.0.
Its v3.9.2 file API identifies the detection, recognition and classifier
bytes recorded in the materials manifest. These match the three installed
models. This is model-host evidence, not an inference from RapidOCR's code
license. Preserve the Apache text, author attribution and provenance when
redistributing the unmodified model files. It is not a warranty of training
data, patent or trademark clearance.

## Qt and PySide

The desktop bundle uses Qt Core, Gui, Network, Svg and Widgets with
PySide6/Shiboken 6.11.2. The optional Qt PDF image-format plugin and Qt6Pdf
library are excluded: BotStudio has no PDF import or PDF-rendering workflow.
Likewise, OpenCV's optional FFmpeg video backend is excluded; BotStudio uses
OpenCV for still-image processing and has no video capture or encoding path.
`verify_windows.ps1` rejects those binaries if a future build accidentally
reintroduces them. The image-preview and offline-OCR executable self-tests
remain required after this packaging change.

The GPLv3 and LGPLv3 texts are supplied in each Qt/PySide package's `upstream`
directory. These texts alone do not replace the modules' copyright and
third-party attributions. The wheel's commercial-license placeholder is not
treated as the complete open-source notice set.

Exact-version upstream source locations for the Qt/PySide components that
remain in the bundle:

- [Qt 6.11.2 source modules](https://download.qt.io/official_releases/qt/6.11/6.11.2/submodules/)
  (qtbase, qtsvg and qtimageformats).
- [PySide/Shiboken 6.11.2 source](https://download.qt.io/official_releases/QtForPython/pyside6/PySide6-6.11.2-src/)
- [QtBase source commit](https://github.com/qt/qtbase/tree/ef55f427f2c8b410d34f8a7681020a3000cf6866)
- [PySide source commit](https://github.com/pyside/pyside-setup/tree/24627cd36e1593adf22eb1f2950e4248e7bcc1ec)
- [Qt third-party attribution documentation](https://doc.qt.io/qt-6/licenses-used-in-qt.html)
- [Qt for Python license documentation](https://doc.qt.io/qtforpython-6/licenses.html)

Source locations are navigation aids, not a claim that a binary-matched
complete source delivery has already been assembled and verified.

### Replacing dynamically loaded Qt/PySide libraries

BotStudio uses a PyInstaller **onedir** bundle, not a signed or encrypted
single-file Qt library archive. Close Studio and Watchdog and copy the
installation to a separate writable test directory. Keep the originals.
Compatible replacement Qt DLLs/plugins are loaded from `_internal/PySide6`;
PySide extension modules live there, and Shiboken lives in `_internal/shiboken6`.
Replace these as a compatible set for Qt/PySide 6.11.2, Windows x64 and the
CPython 3.14 ABI. A rebuilt incompatible DLL is not expected to work merely
because its file name matches.

Run the copied `BotStudio.exe --self-test --data-directory "<new-empty-path>"`
and the corresponding Watchdog self-test with another new empty path.
Never point self-tests at real user data. The normal checksum checks test the
original release; they are not an anti-modification restriction. Reverse
engineering to debug modifications to LGPL-covered components is not
prohibited by BotStudio. This replacement procedure still needs validation
with an independently rebuilt compatible Qt/PySide set.

## Native components and toolchain

Keep CPython's Windows `LICENSE.txt`, including its Microsoft Distributable
Code conditions and bundled-library notices. Those Microsoft-specific
conditions do not relicense BotStudio itself. Preserve the actual
PyInstaller license/bootloader exception, NumPy's bundled native-library
licenses, ONNX Runtime `ThirdPartyNotices.txt`, OpenCV `LICENSE-3RD-PARTY.txt`,
Pillow's native-library notices and Shapely/GEOS notices.

`prepare_source.ps1` emits a checksum-bound native-file inventory from the
actual assembled bundle. An inventory is not a license compatibility verdict.
Unmapped DLLs and missing attributions must be resolved before approval.

## Source delivery and release visibility

The release workflow prepares an application-source ZIP from the exact Git
commit (never from the working directory), a ZIP of installed notices and
`SOURCE-MANIFEST.json` binding their hashes to the installer and native files.
This preserves build scripts, exact dependency pins and the application
source. It is deliberately called **application source**, not automatically
the complete Corresponding Source of all third-party libraries.

Before public binary distribution, assemble and verify the additional
copyleft dependency source/build material, complete native/Qt attributions,
and satisfy equivalent source access for binary recipients. Do not make a
written multi-year source offer without a real process to fulfill it.

Changing a repository from private to public exposes **all its existing
releases and Git history**, not just the newest source. Older private
installers do not gain missing notices retroactively. Review that scope
before changing visibility; approval of one future build is not approval of
all historic binaries. `distribution_review.json` remains the approval gate.

The selected publication route is a separate clean public source repository,
`pernerch/botstudio-public`. The original development repository and its old
test installers stay private. A source ZIP generated by a private build may
contain internal development documents; do not transfer that ZIP or any old
binary to the public repository. Public snapshots exclude those documents
and all user stores; a future public build must originate from its reviewed
public source commit.
