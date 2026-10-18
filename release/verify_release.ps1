param()

$ErrorActionPreference = "Stop"
$started = Get-Date
try {
    Write-Output ("TEST START: release assets " + $started.ToString("o"))
    $versionScript = Join-Path $PSScriptRoot "get_release_version.ps1"
    $version = & $versionScript
    if ((& $versionScript -Tag ("v" + $version)) -cne $version) {
        throw "Valid version tag was rejected."
    }
    foreach ($invalid in @("", $version, "v999.0.0.0", ("v" + $version + "-preview"))) {
        $rejected = $false
        try {
            & $versionScript -Tag $invalid | Out-Null
        } catch {
            if ($_.Exception.Message -notlike "*does not match application version*") { throw }
            $rejected = $true
        }
        if (-not $rejected) { throw "Invalid release tag was accepted: '$invalid'" }
    }
    & (Join-Path $PSScriptRoot "prepare_release.ps1") -Tag ("v" + $version)
    $root = Split-Path -Parent $PSScriptRoot
    $directory = Join-Path $root "dist\release"
    $metadata = Get-Content -LiteralPath (Join-Path $directory "release.json") -Raw | ConvertFrom-Json
    if ($metadata.schema_version -ne 1 -or $metadata.version -cne $version -or
        $metadata.tag -cne ("v" + $version) -or $metadata.platform -cne "windows-x64" -or
        $metadata.installer -cne "BotStudio-Setup-$version.exe" -or $metadata.signed -ne $false) {
        throw "Release metadata does not satisfy the distribution contract."
    }
    $file = Get-Item -LiteralPath (Join-Path $directory $metadata.installer)
    $original = Join-Path $root ("dist\installer\" + $metadata.installer)
    $hash = (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($hash -cne $metadata.sha256 -or $file.Length -ne $metadata.size_bytes -or
        $hash -cne (Get-FileHash -LiteralPath $original -Algorithm SHA256).Hash.ToLowerInvariant()) {
        throw "Staged installer differs from the original or its metadata."
    }
    $sums = Get-Content -LiteralPath (Join-Path $directory "SHA256SUMS.txt") -Raw
    if ($sums -cne "$hash  $($metadata.installer)`n") {
        throw "Checksum file is not the expected SHA256SUMS format."
    }
    Write-Output "TEST RESULT: SUCCESS"
} catch {
    Write-Output "TEST RESULT: FAILURE"
    throw
} finally {
    Write-Output ("TEST END: duration " + ((Get-Date) - $started).TotalSeconds + " seconds")
}
