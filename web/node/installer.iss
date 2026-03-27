; Inno Setup script for CorridorKey Node Agent
;
; Packages the PyInstaller --onedir output into a Windows installer.
; Auto-detects GPU vendor (NVIDIA/AMD) and downloads the correct torch
; acceleration during install — first launch is instant.
;
; Build (after PyInstaller):
;   iscc web/node/installer.iss
;
; Requires: Inno Setup 6+ (https://jrsoftware.org/isinfo.php)

#define MyAppName "CorridorKey Node"
#define MyAppVersion "1.0.0"
#define MyAppPublisher "CorridorKey Cloud"
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
; Bundle the entire PyInstaller --onedir output (no torch — downloaded during install)
Source: "..\..\dist\corridorkey-node\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
; Include a template config file
Source: "node.env.example"; DestDir: "{app}"; DestName: "node.env"; Flags: onlyifdoesntexist

[Dirs]
; Hide the _internal folder (PyInstaller runtime — users don't need to see it)
Name: "{app}\_internal"; Attribs: hidden system
; GPU addon directory for downloaded torch
Name: "{app}\gpu_addon"; Attribs: hidden

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Registry]
; Auto-start on login (only if task selected)
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "CorridorKeyNode"; ValueData: """{app}\{#MyAppExeName}"""; Flags: uninsdeletevalue; Tasks: autostart

[Run]
; Launch after install — shellexec avoids opening a console window
Filename: "{app}\{#MyAppExeName}"; Description: "Launch {#MyAppName}"; Flags: nowait postinstall skipifsilent shellexec

[UninstallDelete]
; Clean up config, cache, and GPU addon on uninstall
Type: filesandordirs; Name: "{app}\node.env"
Type: filesandordirs; Name: "{app}\__pycache__"
Type: filesandordirs; Name: "{app}\gpu_addon"

[Code]
// GPU vendor detection and torch download during install.
// Detects NVIDIA (nvidia-smi) or AMD (registry) and downloads the
// correct torch wheel from the internet, extracts into gpu_addon/.

var
  GPUVendor: String;
  DownloadPage: TDownloadWizardPage;

// --- GPU Detection ---

function DetectNvidia(): Boolean;
var
  ResultCode: Integer;
begin
  Result := Exec('nvidia-smi', '--query-gpu=name --format=csv,noheader',
                 '', SW_HIDE, ewWaitUntilTerminated, ResultCode) and (ResultCode = 0);
end;

function DetectAMD(): Boolean;
var
  SubKey: String;
  Provider: String;
  I: Integer;
begin
  Result := False;
  // Check C:\hip directory
  if DirExists('C:\hip') then
  begin
    Result := True;
    Exit;
  end;
  // Check display adapter registry for AMD/ATI
  for I := 0 to 19 do
  begin
    SubKey := 'SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}\' + Format('%.4d', [I]);
    if RegQueryStringValue(HKEY_LOCAL_MACHINE, SubKey, 'ProviderName', Provider) then
    begin
      if (Pos('AMD', Uppercase(Provider)) > 0) or (Pos('ATI', Uppercase(Provider)) > 0) then
      begin
        Result := True;
        Exit;
      end;
    end;
  end;
end;

function DetectGPU(): String;
begin
  if DetectNvidia() then
    Result := 'nvidia'
  else if DetectAMD() then
    Result := 'amd'
  else
    Result := 'none';
end;

// --- Download Setup ---

procedure InitializeWizard();
begin
  // Create a download page that shows during install
  DownloadPage := CreateDownloadPage(
    'Downloading GPU Acceleration',
    'CorridorKey is downloading the correct GPU drivers for your hardware...',
    nil
  );
end;

// --- Post-Install: Download + Extract Torch ---

procedure ExtractWhl(const WhlFile, DestDir: String);
var
  ResultCode: Integer;
  PSCommand: String;
begin
  // Use PowerShell to extract (wheels are zip files)
  PSCommand := Format(
    'Expand-Archive -Path "%s" -DestinationPath "%s" -Force',
    [WhlFile, DestDir]
  );
  Exec('powershell.exe', '-NoProfile -ExecutionPolicy Bypass -Command "' + PSCommand + '"',
       '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  DeleteFile(WhlFile);
end;

procedure DownloadAndInstallTorch();
var
  AddonDir: String;
  MarkerFile: String;
begin
  GPUVendor := DetectGPU();
  AddonDir := ExpandConstant('{app}\gpu_addon');
  MarkerFile := AddonDir + '\.installed';

  // Skip if already installed (upgrade scenario)
  if FileExists(MarkerFile) then
    Exit;

  ForceDirectories(AddonDir);

  if GPUVendor = 'nvidia' then
  begin
    DownloadPage.Clear;
    DownloadPage.Add(
      'https://download.pytorch.org/whl/cu128/torch-2.8.0%2Bcu128-cp311-cp311-win_amd64.whl',
      'torch-cuda.whl', ''
    );
    DownloadPage.Add(
      'https://download.pytorch.org/whl/cu128/torchvision-0.23.0%2Bcu128-cp311-cp311-win_amd64.whl',
      'torchvision-cuda.whl', ''
    );
    DownloadPage.Show;
    try
      DownloadPage.Download;

      ExtractWhl(ExpandConstant('{tmp}\torch-cuda.whl'), AddonDir);
      ExtractWhl(ExpandConstant('{tmp}\torchvision-cuda.whl'), AddonDir);

      SaveStringToFile(MarkerFile, 'nvidia', False);
    except
      Log('CUDA torch download failed — app will run in CPU mode');
    end;
    DownloadPage.Hide;
  end
  else if GPUVendor = 'amd' then
  begin
    DownloadPage.Clear;
    DownloadPage.Add(
      'https://repo.radeon.com/rocm/windows/rocm-rel-7.2/torch-2.9.1%2Brocmsdk20260116-cp312-cp312-win_amd64.whl',
      'torch-rocm.whl', ''
    );
    DownloadPage.Add(
      'https://repo.radeon.com/rocm/windows/rocm-rel-7.2/torchvision-0.24.1%2Brocmsdk20260116-cp312-cp312-win_amd64.whl',
      'torchvision-rocm.whl', ''
    );
    DownloadPage.Show;
    try
      DownloadPage.Download;

      ExtractWhl(ExpandConstant('{tmp}\torch-rocm.whl'), AddonDir);
      ExtractWhl(ExpandConstant('{tmp}\torchvision-rocm.whl'), AddonDir);

      SaveStringToFile(MarkerFile, 'amd', False);
    except
      Log('ROCm torch download failed — app will run in CPU mode');
    end;
    DownloadPage.Hide;
  end
  else
  begin
    Log('No GPU detected — skipping torch download, will run in CPU mode');
    SaveStringToFile(MarkerFile, 'cpu', False);
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
  begin
    DownloadAndInstallTorch();
  end;
end;

// Remove auto-start registry entry on uninstall
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usPostUninstall then
  begin
    RegDeleteValue(HKEY_CURRENT_USER, 'Software\Microsoft\Windows\CurrentVersion\Run', 'CorridorKeyNode');
  end;
end;
