; 合枢 HS - Inno Setup 安装脚本
; 展示名改品牌名，但安装目录、exe 文件名、AppId 保持不变，保证老用户原地升级
#define MyAppName "合枢 HS"
#define MyAppVersion "2.0.0"
#define MyAppPublisher "Trae Software"
#define MyAppExeName "C盘清理大师.exe"

[Setup]
AppId={{B5C7E8A2-1F3D-4E6B-9C2A-7D8E1F3B5C7A}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\C盘清理大师
DefaultGroupName={#MyAppName}
; 升级时不沿用旧开始菜单目录名，改用新品牌名
UsePreviousGroup=no
DisableProgramGroupPage=yes
OutputDir=installer_output
OutputBaseFilename=合枢HS_安装包
Compression=lzma2/ultra64
SolidCompression=yes
ArchitecturesInstallIn64BitMode=x64
ArchitecturesAllowed=x64
PrivilegesRequired=admin
WizardStyle=modern
UninstallDisplayIcon={app}\{#MyAppExeName}
UninstallDisplayName={#MyAppName}
DisableDirPage=no
DisableReadyPage=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加选项:"

[Files]
Source: "dist\C盘清理大师.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "android\CleanMasterLink.apk"; DestDir: "{app}"; Flags: ignoreversion

; 旧品牌留下的快捷方式：装新版时顺手清掉，避免桌面/开始菜单出现两个入口
[InstallDelete]
Type: files; Name: "{commondesktop}\C盘清理大师.lnk"
Type: filesandordirs; Name: "{commonprograms}\C盘清理大师"

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\卸载{#MyAppName}"; Filename: "{uninstallexe}"
Name: "{commondesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "立即启动{#MyAppName}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
Type: filesandordirs; Name: "{app}"

[Code]
function InitializeSetup(): Boolean;
begin
  Result := True;
end;
