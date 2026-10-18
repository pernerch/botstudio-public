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

For the shipped eligible Qt/PySide/Shiboken modules this GPL-3.0-only Studio
release selects the offered GPLv3 option. Embedded permissive third-party
components keep their own terms; this does not relicense them. Qt module
sources and all required license/attribution texts accompany the installer.
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

The archives are now actually acquired, not merely linked. The source lock
`dependency_sources.json` records 38 version/commit-locked archives
covering the runtime/build pins plus CPython, Qt modules, GEOS and OpenBLAS.
Qt/PySide downloads were checked against Qt's published SHA-256 values;
PyPI sdists use PyPI's published digests. Other official HTTPS snapshots are
explicitly identified as locally hashed downloads, not independently signed
publisher checksums. Matching a version is not proof of matching wheel patches.

### Acquired sources, license texts and attribution metadata

Normal Windows builds run the following automatically before PyInstaller:

```powershell
.\.venv\Scripts\python.exe -m release.prepare_dependency_sources
```

The sources remain under `build/dependency-sources`; hash-checked extracted
materials live under `build/dependency-materials`. Source-index schema 2 maps
verbatim attribution, referenced license and build-document files to
their original archive members. Installed copies use short, hash-prefixed
names under `_internal/third_party/source-notices` to avoid Windows MAX_PATH.
The original hierarchy and source bytes remain unchanged inside the archives.
Qt `qt_attribution.json` and every referenced `LicenseFile`/`LicenseFiles` text
are included. PySide's Windows source-build instructions are included too.
Missing references, corrupt archives or changed prepared texts fail the build.
This conservative collection includes notices for optional/unshipped source
features; it is not a claim that every such feature is in the executable.

A standalone source-and-notice delivery can be produced without an installer:

```powershell
.\.venv\Scripts\python.exe -m release.prepare_dependency_sources `
  --archive .\build\BotStudio-Upstream-Sources-and-Notices-0.1.0.5.zip
```

Existing destinations are never overwritten. `--resolve` is a separate,
explicit maintainer operation to update the reviewed lock, not part of builds.
The normal build never substitutes a newer upstream version.

### Mandatory obligations versus non-blocking follow-ups

The release gate's `blockers` contain only applicable license obligations:
retain copyright/license/notices, comply with model and runtime redistribution
terms, provide the conveyed GPL application source and corresponding copyleft
library sources including required build/installation scripts, and impose no
prohibited modification/relinking restrictions. Public source and all three
source/notice ZIPs are offered at no extra charge beside the installer under
GPLv3 section 6(d); no deferred multi-year written offer is used.

Official unmodified Qt/PySide release sources are accompanied by their build
scripts and Qt's release build/provisioning configuration from the immutable
Qt supermodule commit in the lock. Only the shipped module source tarballs
are claimed: gitlinks for unrelated Qt modules are not silently claimed as
delivered source. Shapely's exact upstream Windows wheel recipe builds GEOS
3.13.1 from the provided unmodified source with `BUILD_SHARED_LIBS=ON`;
the recipe, Shapely sources and GEOS sources are all included. No private
BotStudio patches to Qt, PySide or GEOS were made.

The following remain **open, non-blocking** in the review's `follow_ups`:

- Independently rebuild and exercise a compatible Qt/PySide replacement.
- Explore bit-for-bit native build reproducibility beyond source delivery.
- Acquire external ONNX build submodules and scipy-openblas wheel build inputs
  for reproducibility. Their permissive licenses do not generally require
  source disclosure; retain the actual applicable notices and GCC runtime
  exception terms instead of inferring a blanket copyleft source duty.
- Complete real two-machine/two-account game acceptance during Beta 1.

An independent rebuild, a bit-identical result or a separately recorded
replacement test is not itself a license requirement. This does not waive
actual corresponding-source/build-script or LGPL replacement obligations.
`distribution_review.json` keeps the two categories separate and binds final
approval to this application version, runtime pins, materials and source lock.

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
prohibited by BotStudio. This optional verification procedure remains open as
`independent-qt-replacement-test`; it is not a publication blocker.

GEOS remains LGPL-2.1-covered and dynamically loaded from the Shapely DLL
directory (`_internal/shapely.libs` or the wheel's corresponding Shapely
directory). Keep the original installation, replace GEOS and its C API DLL as a
compatible pair in a writable copy, then run the same isolated smoke tests.
GEOS sources, license and its upstream Windows shared-library build recipe
accompany this distribution. Users may modify and replace it; no technical
checksum lock prevents a modified copy from running. A recorded independent
replacement test is optional; the actual replacement mechanism is not.

## Native components and toolchain

Keep CPython's Windows `LICENSE.txt`, including its Microsoft Distributable
Code conditions and bundled-library notices. Those Microsoft-specific
conditions do not relicense BotStudio itself. Preserve the actual
PyInstaller license/bootloader exception, NumPy's bundled native-library
licenses, ONNX Runtime `ThirdPartyNotices.txt`, OpenCV `LICENSE-3RD-PARTY.txt`,
Pillow's native-library notices and Shapely/GEOS notices.

`native-licenses.json` maps every shipped EXE/DLL/PYD to its component owner,
retained notice files and relevant source/attribution groups. Bundle verification
checks coverage and file hashes; unknown files or missing notices fail.
`prepare_source.ps1` also binds the native-file inventory to the installer.
Mesa 11.2.2 license/copyright material and the historical LLVM NCSA license
are included for Qt's software renderer (not a modern Apache-only substitute).

## Source delivery and release visibility

The release workflow prepares an application-source ZIP from the exact Git
commit (never from the working directory), a ZIP of installed notices, a
`BotStudio-Dependency-Sources-<version>.zip` containing the unchanged acquired
source archives and index, and `SOURCE-MANIFEST.json` binding all three ZIP
hashes and the source-lock hash to the installer and native files.
This preserves build scripts, exact dependency pins and the application
source. It is deliberately called **application source**, not automatically
the complete Corresponding Source of all third-party libraries.
The acquisition index's false approval flag only means acquisition itself
does not approve a release. `SOURCE-MANIFEST.json` records the final
version/hash-bound release review result; the adjacent application and
copyleft library sources/build recipes together provide the required delivery.

Before public binary distribution, satisfy the mandatory obligations above
and the version-bound review gate. Private redistribution and beta status
do not exempt redistribution obligations. Do not make a
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
