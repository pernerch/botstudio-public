param(
    [string]$Tag
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$source = Get-Content -LiteralPath (Join-Path $root "services\application_info.py") -Raw
$match = [regex]::Match($source, '(?m)^APP_VERSION = "(\d+\.\d+\.\d+\.\d+)"\r?$')
if (-not $match.Success) {
    throw "application_info.py must declare a four-part APP_VERSION."
}
$version = $match.Groups[1].Value
foreach ($part in $version.Split(".")) {
    if ([int]$part -gt 65535 -or ($part.Length -gt 1 -and $part.StartsWith("0"))) {
        throw "APP_VERSION must contain canonical Windows version components (0..65535)."
    }
}
if ($PSBoundParameters.ContainsKey("Tag") -and $Tag -cne ("v" + $version)) {
    throw "Release tag '$Tag' does not match application version v$version."
}
Write-Output $version
