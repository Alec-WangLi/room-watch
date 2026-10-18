; room-watch 安装包定义（Inno Setup 6）
;
; 作用：把 scripts\build-portable.ps1 产出的 dist\room-watch-<版本>\ 打成单个 setup.exe。
; 编译：scripts\build-installer.ps1（会自动找 ISCC.exe 并传版本号进来）
;
; 三个刻意的选择
; --------------
; 1. PrivilegesRequired=lowest + {autopf}：普通用户装到 %LocalAppData%\Programs，
;    不需要管理员密码，也不会遇到「程序装在 Program Files 但写不了 data\」。
; 2. 不删除 data\：卸载时保留抓拍与人脸模板，避免「卸了装回来记录全没」。
; 3. 装完可选立刻跑配置向导：客户少一步，也不会装完不知道下一步做什么。

#define AppName "room-watch"
#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef SourceDir
  #define SourceDir "..\dist\room-watch-" + AppVersion
#endif

[Setup]
AppId={{8A3F1C42-6B7D-4E52-9C1A-7D3E5F2B8A61}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=room-watch
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=room-watch-{#AppVersion}-setup
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayName={#AppName} {#AppVersion}
SetupIconFile=..\assets\room-watch.ico
; 磁盘需求按解压后 400MB 估，留余量
ExtraDiskSpaceRequired=0

[Languages]
Name: "chinesesimp"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"

[Tasks]
Name: "autostart"; Description: "开机自动启动监控（推荐）"; GroupDescription: "附加任务："; Flags: checkedonce

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\room-watch.exe"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\room-watch.exe"; Tasks: autostart

[Run]
; 装完直接进配置向导（勾了「开机自启」就顺带把自启也配上）
Filename: "{app}\runtime\python\python.exe"; \
  Parameters: "-m roomwatch setup --autostart"; \
  WorkingDir: "{app}"; \
  Description: "立即配置摄像头与微信推送"; \
  Flags: postinstall nowait skipifsilent

[UninstallRun]
; 取消开机自启（reg delete 找不到键会返回非 0，不当作失败）
Filename: "{cmd}"; Parameters: "/c reg delete ""HKCU\Software\Microsoft\Windows\CurrentVersion\Run"" /v RoomWatch /f >nul 2>&1"; \
  Flags: runhidden

[UninstallDelete]
; 只清理可重建的运行时产物；data\（抓拍、录像、人脸模板）保留
Type: filesandordirs; Name: "{app}\var"
Type: files; Name: "{app}\config.local.yaml"
