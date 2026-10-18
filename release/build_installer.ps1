param(
    [string]$Compiler,
    [switch]$ValidationBuild
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$bundle = Join-Path $root "dist\BotStudio"
if ([string]::IsNullOrWhiteSpace($Compiler)) {
    $candidate = Get-Command "ISCC.exe" -ErrorAction SilentlyContinue
    if ($candidate) {
        $Compiler = $candidate.Source
    } else {
        $Compiler = Join-Path ${env:ProgramFiles(x86)} "Inno Setup 6\ISCC.exe"
    }
}
if (-not (Test-Path -LiteralPath $Compiler -PathType Leaf)) {
    throw "Inno Setup 6 compiler not found. Install Inno Setup or provide -Compiler."
}
foreach ($name in @("BotStudio.exe", "BotStudioWatchdog.exe")) {
    if (-not (Test-Path -LiteralPath (Join-Path $bundle $name) -PathType Leaf)) {
        throw "Build the executable bundle first: $name is missing."
    }
}
$version = (Get-Item -LiteralPath (Join-Path $bundle "BotStudio.exe")).VersionInfo.FileVersion
$watchdogVersion = (Get-Item -LiteralPath (Join-Path $bundle "BotStudioWatchdog.exe")).VersionInfo.FileVersion
if ($version -notmatch '^\d+\.\d+\.\d+\.\d+$' -or $watchdogVersion -ne $version) {
    throw "Studio and Watchdog must have matching four-part Windows versions."
}
# Verify the exact payload, not a separately installed or development interpreter.
& (Join-Path $PSScriptRoot "verify_windows.ps1") -BundleDirectory $bundle
$output = Join-Path $root "dist\installer"
$arguments = @("/DBundleDir=$bundle", "/DSetupVersion=$version", "/DSetupOutputDir=$output")
if ($ValidationBuild) {
    $arguments += "/DValidationBuild=1"
}
& $Compiler @arguments (Join-Path $PSScriptRoot "botstudio.iss")
if ($LASTEXITCODE -ne 0) { throw "Inno Setup compilation failed." }
$suffix = ""
if ($ValidationBuild) { $suffix = "-validation" }
$setup = Join-Path $output ("BotStudio-Setup-" + $version + $suffix + ".exe")
if (-not (Test-Path -LiteralPath $setup -PathType Leaf)) {
    throw "Compiler did not produce the expected installer."
}
Get-FileHash -LiteralPath $setup -Algorithm SHA256 | Format-List
Write-Output "INSTALLER BUILD RESULT: SUCCESS"
Write-Output $setup
