param(
    [Parameter(Mandatory = $true)]
    [string]$BundleDirectory,
    [switch]$Installed
)

$ErrorActionPreference = "Stop"
$bundle = (Get-Item -LiteralPath $BundleDirectory).FullName
$notices = Join-Path $bundle "_internal\third_party"
$nativeMap = Get-Content -LiteralPath (Join-Path $notices "native-licenses.json") -Raw | ConvertFrom-Json
$nativeFiles = @(Get-ChildItem -LiteralPath $bundle -Recurse -File |
    Where-Object { $_.Extension -in @(".exe", ".dll", ".pyd") } |
    ForEach-Object { $_.FullName.Substring($bundle.Length + 1).Replace("\", "/") } |
    Where-Object { -not ($Installed -and $_ -ceq "unins000.exe") })
if ($nativeMap.schema_version -ne 1 -or -not $nativeMap.files -or
    @($nativeMap.unmapped_files).Count -ne 0 -or
    @(Compare-Object @($nativeMap.files.path) $nativeFiles).Count -ne 0) {
    throw "Native license map does not cover the assembled bundle. Expected: $($nativeMap.files.path -join ', '); observed: $($nativeFiles -join ', ')."
}
foreach ($file in $nativeMap.files) {
    $path = Join-Path $bundle $file.path
    if (-not $file.owner -or -not $file.notice_files -or
        (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant() -cne $file.sha256 -or
        (Get-Item -LiteralPath $path).Length -ne $file.size_bytes) {
        throw "Native license map identity mismatch."
    }
    foreach ($notice in $file.notice_files) {
        if (-not (Test-Path -LiteralPath (Join-Path $notices $notice) -PathType Leaf)) {
            throw "Missing mapped native notice: $notice"
        }
    }
}
