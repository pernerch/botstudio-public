param(
    [ValidateRange(1, 600)]
    [int]$TimeoutSeconds = 180
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$version = (Get-Item -LiteralPath (Join-Path $root "dist\BotStudio\BotStudio.exe")).VersionInfo.FileVersion
$setup = Join-Path $root ("dist\installer\BotStudio-Setup-" + $version + "-validation.exe")
if (-not (Test-Path -LiteralPath $setup -PathType Leaf)) {
    throw "Build the isolated installer first: .\release\build_installer.ps1 -ValidationBuild"
}
$testRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("botstudio-installer-test-" + [guid]::NewGuid().ToString("N"))
$install = Join-Path $testRoot "Program Files"
$data = Join-Path $testRoot "User Data"
$oldData = $env:BOTSTUDIO_DATA_DIR
$oldTestKey = $env:BOTSTUDIO_INSTALLER_TEST_RUN_KEY
$testRunKey = "Software\BotStudio\InstallerValidation\" + [guid]::NewGuid().ToString("N")
$registryPath = "HKCU:\" + $testRunKey
$newRegistryParents = @("Software\BotStudio\InstallerValidation", "Software\BotStudio") |
    Where-Object { -not (Test-Path -LiteralPath ("HKCU:\" + $_)) }
$started = Get-Date

function Invoke-BoundedProcess([string]$File, [string[]]$Arguments, [int]$ExpectedExit = 0) {
    Write-Output ("PROCESS START: " + $File + " " + ($Arguments -join " "))
    $process = Start-Process -FilePath $File -ArgumentList $Arguments -PassThru
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    $pending = New-Object 'System.Collections.Generic.Queue[System.Diagnostics.Process]'
    $seen = New-Object 'System.Collections.Generic.HashSet[int]'
    $pending.Enqueue($process)
    $null = $seen.Add($process.Id)
    while ($pending.Count -gt 0) {
        $current = $pending.Dequeue()
        # Inno's uninstaller exits its first phase while its child still runs.
        $children = @(Get-CimInstance Win32_Process -Filter "ParentProcessId=$($current.Id)")
        $remaining = [Math]::Max(0, [int](($deadline - (Get-Date)).TotalMilliseconds))
        if (-not $current.WaitForExit($remaining)) {
            Stop-Process -Id $current.Id -Force
            throw [System.TimeoutException]::new("Installer validation timed out: $File")
        }
        $children += @(Get-CimInstance Win32_Process -Filter "ParentProcessId=$($current.Id)")
        foreach ($child in $children) {
            if ($seen.Add([int]$child.ProcessId)) {
                try {
                    $pending.Enqueue([System.Diagnostics.Process]::GetProcessById([int]$child.ProcessId))
                } catch [System.ArgumentException] {
                    Write-Output ("PROCESS ALREADY EXITED: " + $child.ProcessId)
                }
            }
        }
    }
    $process.Refresh()
    if ($process.ExitCode -ne $ExpectedExit) {
        throw "Unexpected process exit $($process.ExitCode), expected ${ExpectedExit}: $File. See logs in $testRoot"
    }
    Write-Output ("PROCESS END: " + $File + " exit " + $process.ExitCode)
}

function Get-StartupValue {
    $key = Get-Item -LiteralPath "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run" -ErrorAction SilentlyContinue
    if ($null -eq $key) { return $null }
    return $key.GetValue("BotStudioWatchdog", $null, [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)
}

function Assert-ProcessGuard([string]$File, [string[]]$Arguments) {
    $guardProcess = Start-Process -FilePath $placeholder -PassThru
    try {
        if ($guardProcess.WaitForExit(300)) { throw "Process guard fixture exited early." }
        Invoke-BoundedProcess $File $Arguments 1
        $guardProcess.Refresh()
        if ($guardProcess.HasExited) { throw "Installer terminated the Watchdog fixture." }
    } finally {
        $guardProcess.Refresh()
        if (-not $guardProcess.HasExited) {
            Stop-Process -Id $guardProcess.Id -Force
            if (-not $guardProcess.WaitForExit(5000)) { throw "Guard fixture failed to stop." }
        }
    }
}

function Remove-TestDirectory {
    $deadline = (Get-Date).AddSeconds(10)
    while (Test-Path -LiteralPath $testRoot) {
        try {
            Remove-Item -LiteralPath $testRoot -Recurse -Force
        } catch [System.IO.IOException] {
            if ((Get-Date) -ge $deadline) { throw }
            # Inno's uninstaller helper can hold its log briefly after exit.
            Start-Sleep -Milliseconds 200
        }
    }
}

$sentinel = Join-Path $data "user-content.txt"
$startupBefore = Get-StartupValue
try {
    Write-Output ("TEST START: isolated installer lifecycle " + $started.ToString("o"))
    New-Item -ItemType Directory -Path $data -Force | Out-Null
    Set-Content -LiteralPath $sentinel -Value "Existing user content must survive." -Encoding UTF8
    $sentinelHash = (Get-FileHash -LiteralPath $sentinel -Algorithm SHA256).Hash
    $env:BOTSTUDIO_DATA_DIR = $data
    $env:BOTSTUDIO_INSTALLER_TEST_RUN_KEY = $testRunKey
    $common = @("/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/SP-")
    $placeholder = Join-Path $testRoot "BotStudioWatchdog.exe"
    Add-Type -TypeDefinition @'
public static class InstallerGuardFixture {
    public static void Main(string[] args) {
        if (args.Length == 2 && args[0] == "--spawn-child") {
            System.Diagnostics.Process.Start(
                System.Reflection.Assembly.GetExecutingAssembly().Location,
                "--write-after-delay \"" + args[1] + "\"");
        } else if (args.Length == 2 && args[0] == "--write-after-delay") {
            System.Threading.Thread.Sleep(2000);
            System.IO.File.WriteAllText(args[1], "Child completed");
        } else {
            System.Threading.Thread.Sleep(60000);
        }
    }
}
'@ -OutputAssembly $placeholder -OutputType WindowsApplication
    $childSentinel = Join-Path $testRoot "child-completed.txt"
    Invoke-BoundedProcess $placeholder @("--spawn-child", ('"' + $childSentinel + '"'))
    if (-not (Test-Path -LiteralPath $childSentinel)) {
        throw "Process waiter returned before the child completed."
    }
    Assert-ProcessGuard $setup ($common + @(
        ('/DIR="' + $install + '"'), ('/LOG="' + (Join-Path $testRoot "process-guard.log") + '"')
    ))
    if (Test-Path -LiteralPath (Join-Path $install "BotStudio.exe")) {
        throw "Setup wrote files despite a running Watchdog."
    }
    Invoke-BoundedProcess $setup ($common + @(
        ('/DIR="' + $install + '"'), ('/LOG="' + (Join-Path $testRoot "install.log") + '"')
    ))
    if (-not (Test-Path -LiteralPath (Join-Path $install "unins000.exe"))) {
        throw "Installed uninstaller is missing."
    }
    & (Join-Path $PSScriptRoot "verify_windows.ps1") -BundleDirectory $install -TimeoutSeconds 90 -Installed
    New-Item -Path $registryPath -Force | Out-Null
    $foreignCommand = '"C:\Other Installation\BotStudioWatchdog.exe" --working-directory "C:\Other Data"'
    New-ItemProperty -LiteralPath $registryPath -Name "BotStudioWatchdog" -Value $foreignCommand -PropertyType String -Force | Out-Null
    $stale = Join-Path $install "_internal\obsolete-validation.txt"
    Set-Content -LiteralPath $stale -Value "Old program runtime file" -Encoding ASCII
    Invoke-BoundedProcess $setup ($common + @(
        ('/DIR="' + $install + '"'), ('/LOG="' + (Join-Path $testRoot "update.log") + '"')
    ))
    if (Test-Path -LiteralPath $stale) {
        throw "Upgrade retained obsolete runtime files."
    }
    if ((Get-Item -LiteralPath $registryPath).GetValue("BotStudioWatchdog") -cne $foreignCommand) {
        throw "Upgrade changed the existing startup entry."
    }
    if ((Get-FileHash -LiteralPath $sentinel -Algorithm SHA256).Hash -ne $sentinelHash) {
        throw "Upgrade changed user content."
    }
    & (Join-Path $PSScriptRoot "verify_windows.ps1") -BundleDirectory $install -TimeoutSeconds 90 -Installed
    # PrepareToInstall must reject an explicit program/data directory overlap.
    Invoke-BoundedProcess $setup ($common + @(
        ('/DIR="' + $data + '"'), ('/LOG="' + (Join-Path $testRoot "overlap.log") + '"')
    )) 7
    if (Test-Path -LiteralPath (Join-Path $data "BotStudio.exe")) {
        throw "Directory guard wrote program files into user data."
    }
    Invoke-BoundedProcess (Join-Path $install "unins000.exe") ($common + @(
        ('/LOG="' + (Join-Path $testRoot "uninstall-foreign.log") + '"'), ('_?=' + $install)
    ))
    if ((Get-Item -LiteralPath $registryPath).GetValue("BotStudioWatchdog") -cne $foreignCommand) {
        throw "Uninstall removed another installation's startup entry."
    }
    Invoke-BoundedProcess $setup ($common + @(
        ('/DIR="' + $install + '"'), ('/LOG="' + (Join-Path $testRoot "reinstall.log") + '"')
    ))
    $ownCommand = '"' + (Join-Path $install "BotStudioWatchdog.exe") + '" --working-directory "' + $data + '"'
    Set-ItemProperty -LiteralPath $registryPath -Name "BotStudioWatchdog" -Value $ownCommand
    Assert-ProcessGuard (Join-Path $install "unins000.exe") ($common + @(
        ('/LOG="' + (Join-Path $testRoot "uninstall-process-guard.log") + '"'), ('_?=' + $install)
    ))
    if (-not (Test-Path -LiteralPath (Join-Path $install "BotStudio.exe"))) {
        throw "Uninstall removed program files despite a running Watchdog."
    }
    if ((Get-Item -LiteralPath $registryPath).GetValue("BotStudioWatchdog") -cne $ownCommand) {
        throw "Blocked uninstall changed the startup entry."
    }
    Invoke-BoundedProcess (Join-Path $install "unins000.exe") ($common + @(
        ('/LOG="' + (Join-Path $testRoot "uninstall.log") + '"'), ('_?=' + $install)
    ))
    foreach ($name in @("BotStudio.exe", "BotStudioWatchdog.exe", "_internal\python314.dll")) {
        if (Test-Path -LiteralPath (Join-Path $install $name)) {
            throw "Uninstall retained a program file: $name"
        }
        if ($null -ne (Get-Item -LiteralPath $registryPath).GetValue("BotStudioWatchdog")) {
            throw "Uninstall retained its own Watchdog startup entry."
        }
    }
    if ((Get-FileHash -LiteralPath $sentinel -Algorithm SHA256).Hash -ne $sentinelHash) {
        throw "Uninstall changed user content."
    }
    if ((Get-StartupValue) -cne $startupBefore) {
        throw "Isolated validation changed the productive Watchdog startup entry."
    }
} catch {
    if (Test-Path -LiteralPath $testRoot) {
        Get-ChildItem -LiteralPath $testRoot -Filter "*.log" -File | ForEach-Object {
            Write-Output ("LOG: " + $_.Name)
            Get-Content -LiteralPath $_.FullName -Tail 15
        }
    }
    if ($_.Exception -is [System.TimeoutException]) {
        Write-Output "TEST RESULT: TIMEOUT"
    } else {
        Write-Output "TEST RESULT: FAILURE"
    }
    throw
} finally {
    $env:BOTSTUDIO_DATA_DIR = $oldData
    $env:BOTSTUDIO_INSTALLER_TEST_RUN_KEY = $oldTestKey
    try {
        try {
            Remove-TestDirectory
        } finally {
            if (Test-Path -LiteralPath $registryPath) {
                Remove-Item -LiteralPath $registryPath -Force
            }
            foreach ($parent in $newRegistryParents) {
                $key = Get-Item -LiteralPath ("HKCU:\" + $parent) -ErrorAction SilentlyContinue
                if ($key -and $key.SubKeyCount -eq 0 -and $key.ValueCount -eq 0) {
                    $key.Close()
                    [Microsoft.Win32.Registry]::CurrentUser.DeleteSubKey($parent, $false)
                }
            }
        }
    } catch {
        Write-Output "TEST RESULT: FAILURE"
        throw
    }
    Write-Output ("TEST END: duration " + ((Get-Date) - $started).TotalSeconds + " seconds")
}
Write-Output "TEST RESULT: SUCCESS"
