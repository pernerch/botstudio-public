param(
    [string]$BundleDirectory,
    [int]$TimeoutSeconds = 90,
    [switch]$NativeWindow
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
if ([string]::IsNullOrWhiteSpace($BundleDirectory)) {
    $BundleDirectory = Join-Path $root "dist\BotStudio"
}
if ($TimeoutSeconds -lt 1 -or $TimeoutSeconds -gt 300) {
    throw "TimeoutSeconds must be between 1 and 300."
}
$bundle = (Resolve-Path -LiteralPath $BundleDirectory).Path
foreach ($name in @("LICENSE", "LICENSE-NOTICE.txt")) {
    $bundledLicense = Join-Path $bundle ("_internal\licenses\BotStudio\" + $name)
    $sourceLicense = Join-Path $root $name
    if (-not (Test-Path -LiteralPath $bundledLicense) -or
        (Get-FileHash -LiteralPath $bundledLicense -Algorithm SHA256).Hash -cne
        (Get-FileHash -LiteralPath $sourceLicense -Algorithm SHA256).Hash) {
        throw "Bundled BotStudio license is missing or differs from source: $name"
    }
}
$notices = Join-Path $bundle "_internal\third_party"
$inventory = Get-Content -LiteralPath (Join-Path $notices "inventory.json") -Raw | ConvertFrom-Json
if ($inventory.schema_version -ne 1 -or $inventory.status -cne "review_required" -or
    -not $inventory.packages -or @($inventory.missing_notice_packages).Count -ne 0 -or
    -not (Test-Path -LiteralPath (Join-Path $notices "Python-LICENSE.txt"))) {
    throw "Bundled third-party inventory or CPython license is missing."
}
foreach ($package in $inventory.packages) {
    if (-not $package.notice_files -or
        -not (Test-Path -LiteralPath (Join-Path $notices ($package.name + "\METADATA.txt")))) {
        throw "Missing bundled distribution metadata: $($package.name)"
    }
    foreach ($notice in $package.notice_files) {
        $path = Join-Path $notices ($package.name + "\" + $notice.path)
        if ((Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant() -cne $notice.sha256) {
            throw "Bundled notice hash mismatch: $path"
        }
    }
    $materialsPath = Join-Path $notices "distribution_materials.json"
    if ((Get-FileHash -LiteralPath $materialsPath -Algorithm SHA256).Hash -cne
        (Get-FileHash -LiteralPath (Join-Path $PSScriptRoot "distribution_materials.json") -Algorithm SHA256).Hash -or
        (Get-FileHash -LiteralPath (Join-Path $notices "DISTRIBUTION.md") -Algorithm SHA256).Hash -cne
        (Get-FileHash -LiteralPath (Join-Path $PSScriptRoot "DISTRIBUTION.md") -Algorithm SHA256).Hash) {
        throw "Bundled distribution materials differ from the release source."
    }
    $materials = Get-Content -LiteralPath $materialsPath -Raw | ConvertFrom-Json
    $modelsDirectory = Join-Path $bundle "_internal\rapidocr\models"
    $modelFiles = @(Get-ChildItem -LiteralPath $modelsDirectory -Filter "*.onnx" -File)
    if (@(Compare-Object @($materials.models.files.name) @($modelFiles.Name)).Count -ne 0) {
        throw "Bundled OCR model set differs from reviewed provenance."
    }
    foreach ($model in $materials.models.files) {
        $path = Join-Path $modelsDirectory $model.name
        if ((Get-Item -LiteralPath $path).Length -ne $model.size_bytes -or
            (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant() -cne $model.sha256) {
            throw "Bundled OCR model hash differs from reviewed provenance: $($model.name)"
        }
    }
}
$virtualKeyboard = Get-ChildItem -LiteralPath (Join-Path $bundle "_internal\PySide6") -Recurse -Filter "*virtualkeyboard*.dll" -File
if ($virtualKeyboard) {
    throw "Qt Virtual Keyboard must not be distributed in the desktop Studio bundle."
}
foreach ($forbidden in @("config", "data", "games", "profiles", "logs", "runs", ".venv")) {
    if ((Test-Path -LiteralPath (Join-Path $bundle $forbidden)) -or
        (Test-Path -LiteralPath (Join-Path $bundle "_internal\$forbidden"))) {
        throw "Personal data directory must not be bundled: $forbidden"
    }
}
$testRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("botstudio-exe-test-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $testRoot -Force | Out-Null
$oldPlatform = $env:QT_QPA_PLATFORM
$oldData = $env:BOTSTUDIO_DATA_DIR
try {
    $env:QT_QPA_PLATFORM = "offscreen"
    if ($NativeWindow) { $env:QT_QPA_PLATFORM = "windows" }
    $env:BOTSTUDIO_DATA_DIR = $testRoot
    foreach ($entry in @(
        @{ Name = "BotStudio"; Role = "studio" },
        @{ Name = "BotStudioWatchdog"; Role = "watchdog" }
    )) {
        $executable = Join-Path $bundle ($entry.Name + ".exe")
        $data = Join-Path $testRoot $entry.Role
        $process = Start-Process -FilePath $executable -ArgumentList @(
            "--self-test", "--data-directory", ('"' + $data + '"')
        ) -WorkingDirectory $testRoot -PassThru
        if (-not $process.WaitForExit($TimeoutSeconds * 1000)) {
            Stop-Process -Id $process.Id -Force
            throw [System.TimeoutException]::new("Executable self-test timed out: " + $entry.Name)
        }
        $process.Refresh()
        $resultFile = Join-Path $data "self-test-result.json"
        if (-not (Test-Path -LiteralPath $resultFile)) {
            throw ("Self-test produced no result: " + $entry.Name + " (exit " + $process.ExitCode + ")")
        }
        $result = Get-Content -LiteralPath $resultFile -Raw | ConvertFrom-Json
        if ($process.ExitCode -ne 0 -or $result.result -ne "SUCCESS" -or
            -not $result.frozen -or $result.role -ne $entry.Role) {
            throw ($entry.Name + " self-test failed: " + ($result | ConvertTo-Json -Depth 5))
        }
        if ($entry.Role -eq "studio" -and (
            -not $result.application_icon_loaded -or -not $result.taskbar_app_id -or
            -not $result.package_roundtrip -or -not $result.markdown_editor -or
            ($NativeWindow -and (-not $result.native_small_icon -or -not $result.native_large_icon))
        )) {
            throw "Studio self-test did not prove the required taskbar/icon properties."
        }
        Write-Output ($entry.Name + ": SUCCESS (" + $result.version + ")")
        Write-Output ($result | ConvertTo-Json -Depth 5)
    }
    Write-Output "TEST RESULT: SUCCESS"
} catch {
    if ($_.Exception -is [System.TimeoutException]) {
        Write-Output "TEST RESULT: TIMEOUT"
    } else {
        Write-Output "TEST RESULT: FAILURE"
    }
    throw
} finally {
    $env:QT_QPA_PLATFORM = $oldPlatform
    $env:BOTSTUDIO_DATA_DIR = $oldData
    Remove-Item -LiteralPath $testRoot -Recurse -Force
}
