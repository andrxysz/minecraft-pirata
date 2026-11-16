#define AppName "Mine Pirata"
#define AppVersion "1.0"

[Setup]
AppId={{B69E2862-E143-4709-9985-C392D6874F8E}
AppName={#AppName}
AppVersion={#AppVersion}
VersionInfoVersion={#AppVersion}
VersionInfoTextVersion={#AppVersion}
AppPublisher=Mine Pirata
DefaultDirName={localappdata}\Programs\MinePirata
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..
OutputBaseFilename=MinePirata-Setup-{#AppVersion}
SetupIconFile=MinePirata.ico
UninstallDisplayIcon={app}\MinePirata.exe
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
WizardSizePercent=110
CloseApplications=yes
RestartApplications=no
SetupLogging=yes

[Languages]
Name: "brazilianportuguese"; MessagesFile: "compiler:Languages\BrazilianPortuguese.isl"

[Tasks]
Name: "desktopicon"; Description: "Criar um atalho na área de trabalho"; GroupDescription: "Atalhos:"; Flags: unchecked

[Files]
Source: "..\dist\MinePirata\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\README.md"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\MinePirata.exe"; WorkingDir: "{app}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\MinePirata.exe"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\MinePirata.exe"; Description: "Abrir {#AppName}"; Flags: nowait postinstall skipifsilent
