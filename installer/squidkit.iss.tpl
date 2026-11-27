; 由 tools/build_installer.py 渲染生成；请勿直接编译本模板。
; 安装版布局：{app}\squidkit.exe（冻结主程序）+ {app}\src（业务源码与 runtime）
; 用户数据（config / history）在 %APPDATA%\SquidKit，默认下载在「下载」\SquidKit，
; 都不在 {app} 内——升级安装与卸载不会动它们。

#define MyAppName "@@APPNAME@@"
#define MyAppVersion "@@VERSION@@"
#define MyAppPublisher "@@PUBLISHER@@"

[Setup]
AppId=@@APPID@@
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
VersionInfoVersion=@@VERSION4@@
VersionInfoCompany={#MyAppPublisher}
VersionInfoDescription={#MyAppName}
VersionInfoProductName={#MyAppName}
VersionInfoProductVersion=@@VERSION4@@
DefaultDirName={localappdata}\Programs\SquidKit
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
DisableDirPage=yes
UninstallDisplayName={#MyAppName}
UninstallDisplayIcon={app}\squidkit.exe
SetupIconFile="@@ICON@@"
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
CloseApplications=yes
RestartApplications=no
ShowLanguageDialog=no
UsedUserAreasWarning=no

[Languages]
Name: "chinesesimplified"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加选项:"
Name: "autostart"; Description: "登录 Windows 时自动启动"; GroupDescription: "附加选项:"

[Files]
Source: "@@PAYLOAD@@\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion
@@WEBVIEW2FILE@@

[Dirs]
Name: "{app}\src\runtime\downloads"

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\squidkit.exe"; WorkingDir: "{app}"; IconFilename: "{app}\squidkit.ico"; AppUserModelID: "@@AUMID@@"; Comment: "视频下载与资源嗅探"
Name: "{group}\卸载 {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\squidkit.exe"; WorkingDir: "{app}"; IconFilename: "{app}\squidkit.ico"; AppUserModelID: "@@AUMID@@"; Tasks: desktopicon
Name: "{userstartup}\{#MyAppName}"; Filename: "{app}\squidkit.exe"; WorkingDir: "{app}"; IconFilename: "{app}\squidkit.ico"; AppUserModelID: "@@AUMID@@"; Tasks: autostart

[Registry]
Root: HKCU; Subkey: "Software\SquidKit"; ValueType: string; ValueName: "InstallPath"; ValueData: "{app}"; Flags: uninsdeletekey

[Run]
@@WEBVIEW2RUN@@
Filename: "{app}\squidkit.exe"; Description: "启动 {#MyAppName}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
Type: filesandordirs; Name: "{app}\src\runtime\.update_backup_*"
Type: files; Name: "{app}\src\runtime\agent_service.json"
Type: files; Name: "{app}\src\YtDlpGUI\config.json.bak"
Type: files; Name: "{app}\src\YtDlpGUI\history.json.bak"
Type: dirifempty; Name: "{app}"

[Code]
// WebView2 运行时是否缺失：三处登记都要查（每机器 32/64 位视图 + 每用户）
function NeedWebView2(): Boolean;
var
  Pv: String;
begin
  Result := True;
  if RegQueryStringValue(HKLM,
       'SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}',
       'pv', Pv) and (Pv <> '') and (Pv <> '0.0.0.0') then
    Result := False
  else if RegQueryStringValue(HKLM,
       'SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}',
       'pv', Pv) and (Pv <> '') and (Pv <> '0.0.0.0') then
    Result := False
  else if RegQueryStringValue(HKCU,
       'Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}',
       'pv', Pv) and (Pv <> '') and (Pv <> '0.0.0.0') then
    Result := False;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  DataDir: String;
begin
  if CurUninstallStep = usPostUninstall then
  begin
    DataDir := ExpandConstant('{userappdata}\SquidKit');
    if DirExists(DataDir) then
      if SuppressibleMsgBox('是否同时删除用户数据（设置与下载历史）？' + #13#10 + DataDir,
                mbConfirmation, MB_YESNO or MB_DEFBUTTON2, IDNO) = IDYES then
        DelTree(DataDir, True, True, True);
  end;
end;
