; Inno Setup script - builds "Mall Audio Scheduler Setup.exe" from the
; PyInstaller output in dist\Mall Audio Scheduler\.
; Install Inno Setup 6 (https://jrsoftware.org/isinfo.php), then either open
; this file in it and press Build, or run:  iscc installer\windows.iss

#define AppName      "Mall Audio Scheduler"
#define AppVersion   "0.1.0"
#define AppPublisher "Asgabat SDAM"
#define AppExe       "Mall Audio Scheduler.exe"

[Setup]
AppId={{7D0C2E8B-6D2E-4E5A-9C0B-5B1F4A1A7C21}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
UninstallDisplayIcon={app}\{#AppExe}
OutputDir=..\dist
OutputBaseFilename=Mall Audio Scheduler Setup
SetupIconFile=..\mall_audio\assets\logo.ico
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequiredOverridesAllowed=dialog

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"
; The app can also register itself from Settings; this just offers it at install.
Name: "autostart"; Description: "Start {#AppName} automatically when Windows starts"; GroupDescription: "Unattended operation:"

[Files]
Source: "..\dist\{#AppName}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Registry]
; Per-user Run entry: the same key the in-app "start at login" setting uses.
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "MallAudioScheduler"; ValueData: """{app}\{#AppExe}"""; Flags: uninsdeletevalue; Tasks: autostart

[Run]
Filename: "{app}\{#AppExe}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent
