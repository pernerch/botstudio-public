param()

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$output = Join-Path $root "dist\release"
$metadata = Get-Content -LiteralPath (Join-Path $output "release.json") -Raw | ConvertFrom-Json
$version = & (Join-Path $PSScriptRoot "get_release_version.ps1") -Tag $metadata.tag
$commit = git -C $root rev-parse HEAD
if ($LASTEXITCODE -ne 0) { throw "Could not resolve release source commit." }
git -C $root diff --quiet HEAD --
if ($LASTEXITCODE -ne 0) { throw "Release source must match the committed build tree." }
$tree = @(git -C $root ls-tree -r HEAD)
if ($LASTEXITCODE -ne 0 -or @($tree | Where-Object { $_ -match '^160000 ' }).Count -gt 0) {
    throw "Source archive cannot omit submodule contents."
}
$installer = Join-Path $output $metadata.installer
$installerHash = (Get-FileHash -LiteralPath $installer -Algorithm SHA256).Hash.ToLowerInvariant()
if ($installerHash -cne $metadata.sha256 -or
    (Get-Item -LiteralPath $installer).Length -ne $metadata.size_bytes) {
    throw "Source preparation requires the verified staged installer."
}
$sourceName = "BotStudio-Application-Source-$version.zip"
$source = Join-Path $output $sourceName
$noticesName = "BotStudio-Notices-$version.zip"
$notices = Join-Path $output $noticesName
$manifestPath = Join-Path $output "SOURCE-MANIFEST.json"
foreach ($target in @($source, $notices, $manifestPath)) {
    if (Test-Path -LiteralPath $target) { throw "Refusing to replace source evidence: $target" }
}
$noticeRoot = Join-Path $root "dist\BotStudio\_internal\third_party"
$inventory = Get-Content -LiteralPath (Join-Path $noticeRoot "inventory.json") -Raw | ConvertFrom-Json
if ($inventory.schema_version -ne 1 -or @($inventory.missing_notice_packages).Count -ne 0) {
    throw "Missing license text in build inventory."
}
try {
    git -C $root archive --format=zip "--output=$source" "--prefix=BotStudio-$version/" $commit
    if ($LASTEXITCODE -ne 0) { throw "Could not archive exact application source." }
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $archive = [System.IO.Compression.ZipFile]::OpenRead($source)
    try {
        $archived = @($archive.Entries | Where-Object { -not $_.FullName.EndsWith("/") } |
            ForEach-Object { $_.FullName.Substring("BotStudio-$version/".Length) })
        $tracked = @(git -C $root ls-tree -r --name-only HEAD)
        if ($LASTEXITCODE -ne 0 -or @(Compare-Object $tracked $archived).Count -ne 0) {
            throw "Source archive omits tracked content (including possible export-ignore rules)."
        }
    } finally {
        $archive.Dispose()
    }
    [System.IO.Compression.ZipFile]::CreateFromDirectory($noticeRoot, $notices)
    $bundle = (Resolve-Path -LiteralPath (Join-Path $root "dist\BotStudio")).Path
    $nativeFiles = @(Get-ChildItem -LiteralPath $bundle -Recurse -File |
        Where-Object { $_.Extension -in @(".dll", ".pyd", ".exe") } |
        Sort-Object FullName | ForEach-Object {
            [ordered]@{
                path = $_.FullName.Substring($bundle.Length + 1).Replace("\", "/")
                size_bytes = $_.Length
                sha256 = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
            }
        })
    if ($nativeFiles.Count -eq 0) { throw "Native build inventory is empty." }
    $manifest = [ordered]@{
        schema_version = 1
        version = $version
        tag = $metadata.tag
        commit = $commit
        scope = "application-source-and-distribution-evidence"
        complete_corresponding_source_approved = $false
        installer = $metadata.installer
        installer_sha256 = $installerHash
        artifacts = @(
            [ordered]@{
                file = $sourceName
                sha256 = (Get-FileHash -LiteralPath $source -Algorithm SHA256).Hash.ToLowerInvariant()
                size_bytes = (Get-Item -LiteralPath $source).Length
            },
            [ordered]@{
                file = $noticesName
                sha256 = (Get-FileHash -LiteralPath $notices -Algorithm SHA256).Hash.ToLowerInvariant()
                size_bytes = (Get-Item -LiteralPath $notices).Length
            }
        )
        native_files = $nativeFiles
    }
    $utf8 = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($manifestPath, (($manifest | ConvertTo-Json -Depth 8) + "`n"), $utf8)
} catch {
    foreach ($target in @($source, $notices, $manifestPath)) {
        if (Test-Path -LiteralPath $target -PathType Leaf) { Remove-Item -LiteralPath $target -Force }
    }
    throw
}
Write-Output "SOURCE EVIDENCE RESULT: SUCCESS"
