param(
    [Parameter(Mandatory = $true)]
    [string]$Destination
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$destinationPath = [System.IO.Path]::GetFullPath($Destination)
if (Test-Path -LiteralPath $destinationPath) {
    throw "Public snapshot destination must not already exist."
}
$archivePath = $destinationPath + ".zip"
if (Test-Path -LiteralPath $archivePath) { throw "Public snapshot archive already exists." }
git -C $root diff --quiet HEAD --
if ($LASTEXITCODE -ne 0) { throw "Commit the reviewed source before exporting a public snapshot." }
$scopes = @(
    ".github/workflows/windows-release.yml", ".gitattributes", ".gitignore",
    "LICENSE", "LICENSE-NOTICE.txt", "main.py", "watchdog_app.py",
    "assets/flags", "core", "functions", "gui", "interfaces", "models",
    "scheduler", "services", "ui", "tests", "release", "tools/migrate_library_schema.py"
)
$tracked = @(git -C $root -c core.quotePath=false ls-tree -r --name-only HEAD -- @scopes)
if ($LASTEXITCODE -ne 0 -or $tracked.Count -eq 0) { throw "Could not identify public source files." }
foreach ($path in $tracked) {
    if ($path -match '(?i)(^|/)\.env($|\.)|\.(key|pem|p12|pfx|onnx|pt|zip|exe|dll)$') {
        throw "Unexpected private/binary payload in public source scope: $path"
    }
}
$includedScopes = @($scopes | Where-Object {
    $scope = $_
    @($tracked | Where-Object { $_ -ceq $scope -or $_.StartsWith($scope + "/") }).Count -gt 0
})
$tree = @(git -C $root ls-tree -r HEAD -- @includedScopes)
if ($LASTEXITCODE -ne 0 -or @($tree | Where-Object { $_ -match '^(160000|120000) ' }).Count -gt 0) {
    throw "Public snapshots cannot silently follow symlinks or omit submodule contents."
}
try {
    git -C $root archive --format=zip "--output=$archivePath" HEAD -- @includedScopes
    if ($LASTEXITCODE -ne 0) { throw "Public source export failed." }
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $archive = [System.IO.Compression.ZipFile]::OpenRead($archivePath)
    try {
        $archived = @($archive.Entries | Where-Object { -not $_.FullName.EndsWith("/") } |
            ForEach-Object { $_.FullName })
        if (@(Compare-Object $tracked $archived).Count -ne 0) {
            throw "Public source archive omits reviewed source files."
        }
    } finally {
        $archive.Dispose()
    }
    [System.IO.Compression.ZipFile]::ExtractToDirectory($archivePath, $destinationPath)
    foreach ($path in @($tracked | Where-Object { $_ -match '(?i)\.(bak|old|orig|rej)$' })) {
        Remove-Item -LiteralPath (Join-Path $destinationPath $path) -Force
    }
    Copy-Item -LiteralPath (Join-Path $destinationPath "release\PUBLIC_README.md") `
        -Destination (Join-Path $destinationPath "README.md")
    $reviewPath = Join-Path $destinationPath "release\distribution_review.json"
    $review = Get-Content -LiteralPath $reviewPath -Raw | ConvertFrom-Json
    $review.status = "blocked"
    $review.approved_runtime_manifest_sha256 = $null
    $review.approved_materials_sha256 = $null
    $review.approved_application_version = $null
    $review.observations = @(
        "Clean public application source snapshot; private development history and test binaries are excluded."
        "GPL-3.0-only applies to the applicable BotStudio material."
        "Version/hash-bound upstream license supplements and model provenance are supplied."
        "Complete Qt/native attribution and copyleft dependency source delivery remain unresolved."
        "This source publication does not approve public installer distribution."
    )
    $utf8 = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($reviewPath, (($review | ConvertTo-Json -Depth 10) + "`n"), $utf8)
} finally {
    if (Test-Path -LiteralPath $archivePath -PathType Leaf) {
        Remove-Item -LiteralPath $archivePath -Force
    }
}
Write-Output "PUBLIC SOURCE SNAPSHOT RESULT: SUCCESS"
Write-Output $destinationPath
