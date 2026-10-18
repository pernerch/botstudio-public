param()

$ErrorActionPreference = "Stop"
$started = Get-Date
$temporary = Join-Path ([System.IO.Path]::GetTempPath()) ("botstudio-license-gate-" + [guid]::NewGuid().ToString("N"))
try {
    Write-Output ("TEST START: distribution publication gate " + $started.ToString("o"))
    New-Item -ItemType Directory -Path (Join-Path $temporary "release") -Force | Out-Null
    New-Item -ItemType Directory -Path (Join-Path $temporary "services") -Force | Out-Null
    foreach ($file in @("assert_distribution_ready.ps1", "assert_release_access.ps1", "get_release_version.ps1", "requirements-runtime.txt", "distribution_materials.json", "dependency_sources.json")) {
        Copy-Item -LiteralPath (Join-Path $PSScriptRoot $file) -Destination (Join-Path $temporary "release")
    }
    $root = Split-Path -Parent $PSScriptRoot
    Copy-Item -LiteralPath (Join-Path $root "services\application_info.py") -Destination (Join-Path $temporary "services")
    $gate = Join-Path $temporary "release\assert_distribution_ready.ps1"
    $reviewFile = Join-Path $temporary "release\distribution_review.json"
    $review = Get-Content -LiteralPath (Join-Path $PSScriptRoot "distribution_review.json") -Raw | ConvertFrom-Json
    function Assert-Gate([bool]$ExpectedApproval) {
        $review | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $reviewFile -Encoding UTF8
        $approved = $false
        try {
            & $gate | Out-Null
            $approved = $true
        } catch {
            if ($_.Exception.Message -notlike "Public release blocked:*") { throw }
        }
        if ($approved -ne $ExpectedApproval) { throw "Distribution gate accepted/rejected an unexpected review state." }
    }
    # Only the temporary fixture is approved; the repository review is unchanged.
    $review.status = "blocked"
    Assert-Gate $false
    $accessGate = Join-Path $temporary "release\assert_release_access.ps1"
    $previousRepository = $env:GH_REPO
    try {
        $env:GH_REPO = "fixture/private-release"
        $script:visibilityJson = '{"private":true,"visibility":"private"}'
        $script:githubExitCode = 0
        function gh {
            $global:LASTEXITCODE = $githubExitCode
            $visibilityJson
        }
        function Assert-Access([bool]$ExpectedApproval) {
            $approved = $false
            try {
                & $accessGate | Out-Null
                $approved = $true
            } catch {
                if ($_.Exception.Message -notlike "Release blocked:*" -and
                    $_.Exception.Message -notlike "Public release blocked:*" -and
                    $_.Exception.Message -notlike "*Invalid JSON*") { throw }
            }
            if ($approved -ne $ExpectedApproval) {
                throw "Repository access gate accepted/rejected an unexpected state: $visibilityJson (exit $githubExitCode)."
            }
        }
        Assert-Access $true
        $script:visibilityJson = '{"private":false,"visibility":"public"}'
        Assert-Access $false
        $script:visibilityJson = '{"private":"true","visibility":"private"}'
        Assert-Access $false
        $script:visibilityJson = '{"private":true,"visibility":"public"}'
        Assert-Access $false
        $script:visibilityJson = '{"private":true,"visibility":"internal"}'
        Assert-Access $false
        $script:visibilityJson = '{}'
        Assert-Access $false
        $script:visibilityJson = '{"private":true,"visibility":"private"}'
        $script:githubExitCode = 1
        Assert-Access $false
        $script:githubExitCode = 0
        $env:GH_REPO = ""
        Assert-Access $false
    } finally {
        $env:GH_REPO = $previousRepository
        Remove-Item Function:\gh
    }
    $review.status = "approved"
    $review.approved_runtime_manifest_sha256 = (Get-FileHash -LiteralPath (Join-Path $temporary "release\requirements-runtime.txt") -Algorithm SHA256).Hash
    $review.approved_materials_sha256 = (Get-FileHash -LiteralPath (Join-Path $temporary "release\distribution_materials.json") -Algorithm SHA256).Hash
    $review.approved_dependency_sources_sha256 = (Get-FileHash -LiteralPath (Join-Path $temporary "release\dependency_sources.json") -Algorithm SHA256).Hash
    $review.approved_application_version = & (Join-Path $temporary "release\get_release_version.ps1")
    $review.blockers = @([pscustomobject]@{ id = "test-fixture"; resolved = $false })
    Assert-Gate $false
    $review.blockers[0].resolved = $true
    $review.follow_ups = @([pscustomobject]@{ id = "optional-replacement-test"; status = "open"; blocks_release = $false })
    Assert-Gate $true
    $previousRepository = $env:GH_REPO
    try {
        $env:GH_REPO = "fixture/approved-public-release"
        function gh {
            $global:LASTEXITCODE = 0
            '{"private":false,"visibility":"public"}'
        }
        & $accessGate | Out-Null
    } finally {
        $env:GH_REPO = $previousRepository
        Remove-Item Function:\gh
    }
    $review.blockers[0].resolved = "true"
    Assert-Gate $false
    $review.blockers[0].resolved = $true
    $review.approved_application_version = "999.0.0.0"
    Assert-Gate $false
    $review.approved_application_version = & (Join-Path $temporary "release\get_release_version.ps1")
    $review.approved_materials_sha256 = $null
    Assert-Gate $false
    $review.approved_materials_sha256 = (Get-FileHash -LiteralPath (Join-Path $temporary "release\distribution_materials.json") -Algorithm SHA256).Hash
    $review.approved_dependency_sources_sha256 = $null
    Assert-Gate $false
    $review.approved_dependency_sources_sha256 = (Get-FileHash -LiteralPath (Join-Path $temporary "release\dependency_sources.json") -Algorithm SHA256).Hash
    Add-Content -LiteralPath (Join-Path $temporary "release\requirements-runtime.txt") -Value "# Changed after review"
    Assert-Gate $false
} catch {
    Write-Output "TEST RESULT: FAILURE"
    throw
} finally {
    Remove-Item -LiteralPath $temporary -Recurse -Force
    Write-Output ("TEST END: duration " + ((Get-Date) - $started).TotalSeconds + " seconds")
}
Write-Output "TEST RESULT: SUCCESS"
