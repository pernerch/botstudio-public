param()

$ErrorActionPreference = "Stop"
$review = Get-Content -LiteralPath (Join-Path $PSScriptRoot "distribution_review.json") -Raw | ConvertFrom-Json
$manifestHash = (Get-FileHash -LiteralPath (Join-Path $PSScriptRoot "requirements-runtime.txt") -Algorithm SHA256).Hash
$materialsHash = (Get-FileHash -LiteralPath (Join-Path $PSScriptRoot "distribution_materials.json") -Algorithm SHA256).Hash
$version = & (Join-Path $PSScriptRoot "get_release_version.ps1")
if ($review.schema_version -ne 1 -or $review.status -cne "approved" -or
    $review.approved_runtime_manifest_sha256 -ine $manifestHash -or
    $review.approved_materials_sha256 -ine $materialsHash -or
    $review.approved_application_version -cne $version -or
    -not $review.blockers -or @($review.blockers | Where-Object {
        $_.resolved -isnot [bool] -or $_.resolved -ne $true
    }).Count -gt 0) {
    throw "Public release blocked: distribution/license review is not complete. See release\distribution_review.json."
}
Write-Output "DISTRIBUTION REVIEW: APPROVED"
