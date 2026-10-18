param()

$ErrorActionPreference = "Stop"
if ([string]::IsNullOrWhiteSpace($env:GH_REPO)) {
    throw "Release blocked: GH_REPO must identify the target repository."
}
$repositoryJson = gh api "repos/$($env:GH_REPO)" --jq '{private: .private, visibility: .visibility}'
if ($LASTEXITCODE -ne 0) {
    throw "Release blocked: could not read current repository visibility from GitHub."
}
$repository = $repositoryJson | ConvertFrom-Json
if ($repository.private -isnot [bool]) {
    throw "Release blocked: GitHub returned invalid repository visibility."
}
if ($repository.private -eq $true -and $repository.visibility -ceq "private") {
    Write-Output "PRIVATE TEST RELEASE: repository access restrictions apply; distribution review remains unresolved."
} elseif ($repository.private -eq $false -and $repository.visibility -ceq "public") {
    & (Join-Path $PSScriptRoot "assert_distribution_ready.ps1")
} else {
    throw "Release blocked: unsupported or inconsistent repository visibility."
}
