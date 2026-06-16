; AutoClip per-user Windows installer (Inno Setup).
; Per-user (no admin/UAC) so the self-updater can run it silently in place.
; Build:  "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" installer\AutoClip.iss
; Inputs: ..\dist\AutoClip\  (PyInstaller runtime)   ..\obs-runtime\  (recorder bundle)
;         ..\autoclip\       (loose app source — Option A, updated by file replacement)

#define AppVer "0.2.0"

[Setup]
AppId={{C4D7E8F1-3A2B-4C5D-8E9F-1A2B3C4D5E6F}
AppName=AutoClip
AppVersion={#AppVer}
AppPublisher=SmoJa
AppPublisherURL=https://github.com/SmoJa/autoclip
DefaultDirName={localappdata}\Programs\AutoClip
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=..\release
OutputBaseFilename=AutoClip-Setup-{#AppVer}
SetupIconFile=..\autoclip\gui\autoclip.ico
UninstallDisplayIcon={app}\AutoClip.exe
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
; Close a running AutoClip during install/update and restart it after (self-update).
CloseApplications=yes
RestartApplications=yes

[InstallDelete]
; Pre-0.2.0 builds bundled autoclip's data under _internal\autoclip; the package is
; loose now, so remove that stale copy on upgrade (no-op on a fresh install).
Type: filesandordirs; Name: "{app}\_internal\autoclip"

[Files]
Source: "..\dist\AutoClip\*";  DestDir: "{app}";              Flags: recursesubdirs ignoreversion
Source: "..\obs-runtime\*";    DestDir: "{app}\obs-runtime";  Flags: recursesubdirs ignoreversion
; Loose app source — updated in place by the self-updater (no installer needed for
; routine code/UI/plugin changes). Ship .py only; skip caches.
Source: "..\autoclip\*";       DestDir: "{app}\autoclip";     Flags: recursesubdirs ignoreversion; Excludes: "__pycache__\*,*.pyc,*.pyo"

[Icons]
Name: "{userprograms}\AutoClip"; Filename: "{app}\AutoClip.exe"
Name: "{userdesktop}\AutoClip";  Filename: "{app}\AutoClip.exe"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional shortcuts:"

[Run]
Filename: "{app}\AutoClip.exe"; Description: "Launch AutoClip"; Flags: nowait postinstall skipifsilent
