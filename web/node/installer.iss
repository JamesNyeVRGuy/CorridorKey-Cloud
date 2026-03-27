; Inno Setup script for CorridorKey Node Agent
;
; Packages the PyInstaller --onedir output into a Windows installer.
; Creates Start Menu shortcut, optional auto-start, and clean uninstaller.
;
; Build (after PyInstaller):
;   iscc web/node/installer.iss
;
; Requires: Inno Setup 6+ (https://jrsoftware.org/isinfo.php)

#define MyAppName "CorridorKey Node"
#define MyAppVersion "1.0.0"
#define MyAppPublisher "Corridor Digital"
#define MyAppURL "https://corridorkey.cloud"
#define MyAppExeName "corridorkey-node.exe"

[Setup]
AppId={{B7E8A3F1-4D2C-4E5A-9F1B-3C8D7E2A6F09}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
DefaultDirName={localappdata}\CorridorKey Node
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
SetupIconFile=icon.ico
UninstallDisplayIcon={app}\corridorkey-node.exe
OutputBaseFilename=corridorkey-node-setup
OutputDir=..\..\dist
Compression=lzma2/ultra64
SolidCompression=yes
SetupLogging=yes
PrivilegesRequired=lowest
; No admin needed — installs to user's AppData

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "autostart"; Description: "Start CorridorKey Node when Windows starts"; GroupDescription: "Additional options:"
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional options:"; Flags: unchecked

[Files]
; Bundle the entire PyInstaller --onedir output
Source: "..\..\dist\corridorkey-node\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
; Include a template config file
Source: "node.env.example"; DestDir: "{app}"; DestName: "node.env"; Flags: onlyifdoesntexist

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Registry]
; Auto-start on login (only if task selected)
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "CorridorKeyNode"; ValueData: """{app}\{#MyAppExeName}"""; Flags: uninsdeletevalue; Tasks: autostart

[Run]
; Launch after install
Filename: "{app}\{#MyAppExeName}"; Description: "Launch {#MyAppName}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Clean up config and cache on uninstall
Type: filesandordirs; Name: "{app}\node.env"
Type: filesandordirs; Name: "{app}\__pycache__"

[Code]
// Remove auto-start registry entry on uninstall
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usPostUninstall then
  begin
    RegDeleteValue(HKEY_CURRENT_USER, 'Software\Microsoft\Windows\CurrentVersion\Run', 'CorridorKeyNode');
  end;
end;
