param(
    [Parameter(Mandatory = $true)]
    [string]$Tag
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$version = & (Join-Path $PSScriptRoot "get_release_version.ps1") -Tag $Tag
$installerName = "BotStudio-Setup-$version.exe"
$installer = Join-Path $root ("dist\installer\" + $installerName)
if (-not (Test-Path -LiteralPath $installer -PathType Leaf)) {
    throw "Build the normal installer before preparing release assets."
}
foreach ($file in @(
    $installer,
    (Join-Path $root "dist\BotStudio\BotStudio.exe"),
    (Join-Path $root "dist\BotStudio\BotStudioWatchdog.exe")
)) {
    $info = (Get-Item -LiteralPath $file).VersionInfo
    $fileVersion = @($info.FileMajorPart, $info.FileMinorPart, $info.FileBuildPart, $info.FilePrivatePart) -join "."
    if ($fileVersion -cne $version) {
        throw "Release payload version differs from ${version}: $file"
    }
}
$hash = (Get-FileHash -LiteralPath $installer -Algorithm SHA256).Hash.ToLowerInvariant()
$output = Join-Path $root "dist\release"
New-Item -ItemType Directory -Path $output -Force | Out-Null
Copy-Item -LiteralPath $installer -Destination (Join-Path $output $installerName) -Force
$utf8 = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText((Join-Path $output "SHA256SUMS.txt"), "$hash  $installerName`n", $utf8)
$metadata = [ordered]@{
    schema_version = 1
    version = $version
    tag = $Tag
    platform = "windows-x64"
    installer = $installerName
    sha256 = $hash
    size_bytes = (Get-Item -LiteralPath $installer).Length
    signed = $false
}
[System.IO.File]::WriteAllText(
    (Join-Path $output "release.json"),
    (($metadata | ConvertTo-Json) + "`n"), $utf8
)
Write-Output "RELEASE ASSETS RESULT: SUCCESS"
Write-Output $output
