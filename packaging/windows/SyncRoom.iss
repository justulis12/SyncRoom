#define MyAppName "SyncRoom"
; Keep MyAppVersion in sync with pyproject.toml and src/syncroom/__init__.py.
#define MyAppVersion "0.1.33"
#define MyAppPublisher "justys"
#define MyAppExeName "SyncRoom.exe"

[Setup]
AppId={{7B262E40-33D8-4E6A-9D00-E5F26AE6F59B}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputDir=installer-dist
OutputBaseFilename=SyncRoom-Setup
Compression=lzma
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\{#MyAppExeName}
SourceDir=..\..

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop icon"; GroupDescription: "Additional icons:"

[Files]
Source: "dist\SyncRoom\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[InstallDelete]
Type: files; Name: "{app}\mpv.exe"
Type: files; Name: "{app}\mpv.com"
Type: files; Name: "{app}\yt-dlp.exe"
Type: files; Name: "{app}\avcodec*.dll"
Type: files; Name: "{app}\avdevice*.dll"
Type: files; Name: "{app}\avfilter*.dll"
Type: files; Name: "{app}\avformat*.dll"
Type: files; Name: "{app}\avutil*.dll"
Type: files; Name: "{app}\d3dcompiler_*.dll"
Type: files; Name: "{app}\libass*.dll"
Type: files; Name: "{app}\libbluray*.dll"
Type: files; Name: "{app}\libdav1d*.dll"
Type: files; Name: "{app}\libmpv*.dll"
Type: files; Name: "{app}\libplacebo*.dll"
Type: files; Name: "{app}\libshaderc*.dll"
Type: files; Name: "{app}\lua*.dll"
Type: files; Name: "{app}\shaderc_shared.dll"
Type: files; Name: "{app}\swresample*.dll"
Type: files; Name: "{app}\swscale*.dll"
Type: files; Name: "{app}\uchardet*.dll"
Type: files; Name: "{app}\vulkan*.dll"
Type: files; Name: "{app}\zimg*.dll"
Type: filesandordirs; Name: "{app}\mpv"
Type: filesandordirs; Name: "{app}\runtime"
Type: filesandordirs; Name: "{app}\portable_config"

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch {#MyAppName}"; Flags: nowait postinstall skipifsilent
