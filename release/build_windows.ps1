param(
    [string]$Python = "python"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$OutputDirectory = Join-Path $root "dist"
$environmentRoot = Join-Path $root "build\release-venv"
$buildPython = Join-Path $environmentRoot "Scripts\python.exe"

& $Python -c "import struct, sys; assert sys.platform == 'win32' and struct.calcsize('P') == 8 and sys.version_info[:3] == (3, 14, 3), 'Build requires Windows x64 and CPython 3.14.3'"
if ($LASTEXITCODE -ne 0) { throw "Unsupported release build interpreter." }
if (-not (Test-Path -LiteralPath $buildPython)) {
    & $Python -m venv $environmentRoot
    if ($LASTEXITCODE -ne 0) { throw "Failed to create isolated build environment." }
}
& $buildPython -m pip install --disable-pip-version-check -r (Join-Path $PSScriptRoot "requirements-build.txt")
if ($LASTEXITCODE -ne 0) { throw "Release dependency installation failed." }
& $buildPython -m pip check
if ($LASTEXITCODE -ne 0) { throw "Release dependencies are incompatible." }
Push-Location $root
try {
    & $buildPython -m release.prepare_dependency_sources
    if ($LASTEXITCODE -ne 0) { throw "Pinned dependency source acquisition failed." }
} finally {
    Pop-Location
}
& $buildPython -m PyInstaller --clean --noconfirm --distpath $OutputDirectory --workpath (Join-Path $root "build\pyinstaller") (Join-Path $PSScriptRoot "botstudio.spec")
if ($LASTEXITCODE -ne 0) { throw "Windows executable build failed." }
Push-Location $root
try {
    & $buildPython -m release.native_distribution --bundle (Join-Path $OutputDirectory "BotStudio")
    if ($LASTEXITCODE -ne 0) { throw "Native license mapping failed." }
} finally {
    Pop-Location
}
Write-Output "BUILD RESULT: SUCCESS"
Write-Output (Join-Path $OutputDirectory "BotStudio")
