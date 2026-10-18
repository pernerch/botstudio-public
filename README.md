# BotStudio

BotStudio is a game-independent desktop studio for visual automation of
Android emulators. It provides image recognition, offline OCR, a visual
Function editor, scheduling and profile-scoped runtime data.

This is the public source repository, licensed **GPL-3.0-only** for the
applicable BotStudio material. See [LICENSE](LICENSE) and
[LICENSE-NOTICE.txt](LICENSE-NOTICE.txt). Third-party components and
user-created packages retain their own terms.

## Value or variable inputs

Variable-capable Function parameters, OCR queries and Helper arguments use a
leading **Fixed value / Variable** selector with one active input. Switching
modes clears the previous value. Existing `$name` references open in variable
mode; entering a leading `$` also switches to variable mode to match runtime
semantics. Output names and Variable-block targets remain variable-only.

## Beta 1 for Windows

This repository starts with a clean snapshot of the current application
source. It does not contain the private development history, credentials,
profiles, game accounts, user packages, logs, backups or old test installers.

Beta 1 uses version **0.1.0.5** and is published as a GitHub **prerelease**,
not a stable release. Download the installer and its source/notice companions
from [Releases](https://github.com/pernerch/botstudio-public/releases).
Keep backups and use separate test data. Real two-machine/two-account game
acceptance is an open beta task.

Thirty-eight hash-locked upstream archives include copyleft library sources,
Qt release build configuration and Shapely's shared GEOS build recipe.
Windows builds retain the extracted license/attribution texts, a complete
native-file notice map and a separate dependency-source ZIP beside exact
application source and installed notices. Independent library rebuild and
replacement tests remain **non-blocking open follow-ups**, not additional
license prerequisites. See
[distribution materials](release/DISTRIBUTION.md) and the
[distribution review](release/distribution_review.json). Publishing the
application source alone does not substitute for these license obligations.

## Run from source

Use Windows x64 with CPython 3.14.3:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r .\release\requirements-runtime.txt
.\.venv\Scripts\python.exe .\main.py
```

For the companion watchdog:

```powershell
.\.venv\Scripts\python.exe .\watchdog_app.py
```

MuMu, ADB and game packages are not included. Configure your own emulator
and provide your own content. Game images and third-party packages require
their own redistribution rights.

## Build

The exact build/runtime pins and Windows packaging scripts are under
[release](release). With the required CPython installation:

```powershell
.\release\build_windows.ps1 -Python (Get-Command python.exe).Source
.\release\verify_windows.ps1
```

Building an installer additionally requires Inno Setup 6.4.3; its pinned
download and validation are documented in the
[Windows workflow](.github/workflows/windows-release.yml).
The public distribution gate intentionally rejects installer publication
until the version-specific review is complete.

The [package catalog](https://github.com/pernerch/botstudio-catalog) is
separate from the Studio source and groups community Functions, Helpers and
Events by game. Catalog downloads do not grant rights to unrelated content.

## Tests

Tests use Python's unittest discovery. For example:

```powershell
$env:QT_QPA_PLATFORM = "offscreen"
.\.venv\Scripts\python.exe -m unittest discover -s tests -p test_distribution_materials.py
```

Tests must use temporary data directories, bounded execution and cleanup;
never use real profiles or game data as test fixtures.
