#ifndef BundleDir
  #error BundleDir must point to the verified executable bundle.
#endif
#ifndef SetupVersion
  #error SetupVersion must match the executable version.
#endif
#ifndef SetupOutputDir
  #error SetupOutputDir is required.
#endif

[Setup]
AppId={{2B309B9B-EB76-464C-9050-BEF470F6A7D9}
AppName=BotStudio
AppVersion={#SetupVersion}
AppPublisher=BotStudio
AppPublisherURL=https://github.com/pernerch/botstudio
DefaultDirName={localappdata}\Programs\BotStudio
DefaultGroupName=BotStudio
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
DisableProgramGroupPage=yes
UsePreviousAppDir=yes
SetupIconFile=..\ui\botstudio.ico
UninstallDisplayIcon={app}\BotStudio.exe
OutputDir={#SetupOutputDir}
#ifdef ValidationBuild
OutputBaseFilename=BotStudio-Setup-{#SetupVersion}-validation
CreateUninstallRegKey=no
#else
OutputBaseFilename=BotStudio-Setup-{#SetupVersion}
#endif
Compression=lzma2
SolidCompression=yes
CloseApplications=no
RestartApplications=no
WizardStyle=modern
LicenseFile=..\LICENSE
VersionInfoVersion={#SetupVersion}

[Languages]
Name: "german"; MessagesFile: "compiler:Languages\German.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[CustomMessages]
german.CloseStudio=Bitte Studio und Watchdog vor Installation oder Deinstallation beenden (Watchdog-Tray: Beide beenden). Es werden keine Prozesse automatisch beendet.
english.CloseStudio=Please exit Studio and Watchdog before installing or uninstalling (Watchdog tray: Exit both). No processes will be closed automatically.
german.ProcessCheckFailed=Die laufenden Prozesse konnten nicht sicher geprueft werden. Es werden keine Dateien geaendert.
english.ProcessCheckFailed=Running processes could not be checked safely. No files will be changed.
german.DataDirectoryConflict=Programmordner und Nutzerdatenordner muessen getrennt sein. Bitte einen anderen Programmordner waehlen.
english.DataDirectoryConflict=Program and user data directories must be separate. Please choose a different program directory.
german.DesktopShortcut=Desktop-Verknuepfung erstellen
english.DesktopShortcut=Create a desktop shortcut
german.LaunchStudio=BotStudio starten
english.LaunchStudio=Launch BotStudio
german.StartupCleanupFailed=Der Autostarteintrag des installierten Watchdogs konnte nicht entfernt werden.
english.StartupCleanupFailed=Could not remove the installed Watchdog startup entry.

[Tasks]
#ifndef ValidationBuild
Name: "desktopicon"; Description: "{cm:DesktopShortcut}"; Flags: unchecked
#endif

[Files]
Source: "{#BundleDir}\BotStudio.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#BundleDir}\BotStudioWatchdog.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#BundleDir}\_internal\*"; DestDir: "{app}\_internal"; Flags: ignoreversion recursesubdirs createallsubdirs

[InstallDelete]
Type: filesandordirs; Name: "{app}\_internal"; Check: HasInstalledRuntime

[Icons]
#ifndef ValidationBuild
Name: "{userprograms}\BotStudio\BotStudio"; Filename: "{app}\BotStudio.exe"; WorkingDir: "{app}"; IconFilename: "{app}\BotStudio.exe"; AppUserModelID: "BotStudio.Studio"
Name: "{userprograms}\BotStudio\BotStudio Watchdog"; Filename: "{app}\BotStudioWatchdog.exe"; WorkingDir: "{app}"
Name: "{userdesktop}\BotStudio"; Filename: "{app}\BotStudio.exe"; WorkingDir: "{app}"; Tasks: desktopicon; IconFilename: "{app}\BotStudio.exe"; AppUserModelID: "BotStudio.Studio"
#endif

[Run]
#ifndef ValidationBuild
Filename: "{app}\BotStudio.exe"; Description: "{cm:LaunchStudio}"; Flags: nowait postinstall skipifsilent unchecked
#endif

[Code]
function ProcessGuardMessage: String;
var
  Locator, Services, Processes: Variant;
begin
  Result := '';
  try
    Locator := CreateOleObject('WbemScripting.SWbemLocator');
    Services := Locator.ConnectServer('', 'root\CIMV2');
    Processes := Services.ExecQuery(
      'SELECT ProcessId FROM Win32_Process WHERE Name = ''BotStudio.exe'' OR Name = ''BotStudioWatchdog.exe''');
    if Processes.Count > 0 then
      Result := CustomMessage('CloseStudio');
  except
    Log('Process check failed: ' + GetExceptionMessage);
    Result := CustomMessage('ProcessCheckFailed');
  end;
end;

function PathsOverlap(LeftPath, RightPath: String): Boolean;
begin
  LeftPath := Lowercase(AddBackslash(ExpandFileName(LeftPath)));
  RightPath := Lowercase(AddBackslash(ExpandFileName(RightPath)));
  Result := (Pos(LeftPath, RightPath) = 1) or (Pos(RightPath, LeftPath) = 1);
end;

function DirectoryGuardMessage: String;
var
  DataOverride: String;
begin
  Result := '';
  if PathsOverlap(ExpandConstant('{app}'), ExpandConstant('{localappdata}\BotStudio')) then
    Result := CustomMessage('DataDirectoryConflict');
  DataOverride := GetEnv('BOTSTUDIO_DATA_DIR');
  if DataOverride <> '' then
    if PathsOverlap(ExpandConstant('{app}'), DataOverride) then
      Result := CustomMessage('DataDirectoryConflict');
end;

function HasInstalledRuntime: Boolean;
begin
  Result := FileExists(ExpandConstant('{app}\unins000.exe')) and
    FileExists(ExpandConstant('{app}\BotStudio.exe')) and
    FileExists(ExpandConstant('{app}\BotStudioWatchdog.exe'));
end;

function InitializeSetup: Boolean;
var
  ErrorMessage: String;
begin
  ErrorMessage := ProcessGuardMessage;
  Result := ErrorMessage = '';
  if not Result then
    SuppressibleMsgBox(ErrorMessage, mbError, MB_OK, IDOK);
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  Result := ProcessGuardMessage;
  if Result = '' then
    Result := DirectoryGuardMessage;
end;

function InitializeUninstall: Boolean;
var
  ErrorMessage: String;
begin
  ErrorMessage := ProcessGuardMessage;
  Result := ErrorMessage = '';
  if not Result then
    SuppressibleMsgBox(ErrorMessage, mbError, MB_OK, IDOK);
end;

function CommandExecutable(Command: String): String;
var
  Separator: Integer;
begin
  Command := Trim(Command);
  if Copy(Command, 1, 1) = '"' then
  begin
    Delete(Command, 1, 1);
    Separator := Pos('"', Command);
  end
  else
    Separator := Pos(' ', Command);
  if Separator > 0 then
    Result := Copy(Command, 1, Separator - 1)
  else
    Result := Command;
end;

function StartupRegistryPath: String;
begin
#ifdef ValidationBuild
  Result := GetEnv('BOTSTUDIO_INSTALLER_TEST_RUN_KEY');
  if (Pos('Software\BotStudio\InstallerValidation\', Result) <> 1) or
    (Length(Result) <> Length('Software\BotStudio\InstallerValidation\') + 32) then
    RaiseException('An isolated validation registry key is required.');
#else
  Result := 'Software\Microsoft\Windows\CurrentVersion\Run';
#endif
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  ErrorMessage, Command, RegistryPath: String;
begin
  if CurUninstallStep = usUninstall then
  begin
    ErrorMessage := ProcessGuardMessage;
    if ErrorMessage <> '' then
      RaiseException(ErrorMessage);
    RegistryPath := StartupRegistryPath;
    if RegQueryStringValue(HKCU, RegistryPath,
      'BotStudioWatchdog', Command) then
      if CompareText(CommandExecutable(Command), ExpandConstant('{app}\BotStudioWatchdog.exe')) = 0 then
        if not RegDeleteValue(HKCU, RegistryPath, 'BotStudioWatchdog') then
          RaiseException(CustomMessage('StartupCleanupFailed'));
  end;
end;
