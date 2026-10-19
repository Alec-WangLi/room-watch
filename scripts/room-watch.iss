; room-watch 安装包定义（Inno Setup 6）
;
; 作用：把 scripts\build-portable.ps1 产出的 dist\room-watch-<版本>\ 打成单个 setup.exe。
; 编译：scripts\build-installer.ps1（会自动找 ISCC.exe 并传版本号进来）
;
; 几个刻意的选择
; --------------
; 1. PrivilegesRequired=lowest + {autopf}：普通用户装到 %LocalAppData%\Programs，
;    不需要管理员密码，也不会遇到「程序装在 Program Files 但写不了 data\」。
; 2. 卸载不删用户数据（config.local.yaml / data\ / var\）：Inno 升级时是先跑旧版
;    卸载器再装新版，卸载器要是删配置，用户每次升级都要重配摄像头和 webhook。
;    真要清干净，卸载后手动删安装目录。
; 3. 装完可选立刻跑配置向导：客户少一步，也不会装完不知道下一步做什么。
; 4. 简体中文语言文件不在 Inno 官方包里，用 scripts\inno\ChineseSimplified.isl
;    （取自 issrc 的 Unofficial 目录，已转 UTF-8 BOM）。

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
UninstallDisplayIcon={app}\room-watch.exe
SetupIconFile=..\assets\room-watch.ico
; 首次装完自动弹向导，不额外显示「是否立即运行」的勾选页
DisableReadyPage=no
ShowLanguageDialog=no

[Languages]
Name: "chinesesimp"; MessagesFile: "inno\ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加任务："; Flags: unchecked

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\room-watch.exe"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\room-watch.exe"; Tasks: desktopicon

[Run]
; 装完直接进配置向导。四问里的第 4 问就是「开机自启」，由向导决定，
; 安装器这里不传 --autostart，避免「取消勾选仍然自启」的矛盾。
Filename: "{app}\runtime\python\python.exe"; \
  Parameters: "-m roomwatch setup"; \
  WorkingDir: "{app}"; \
  Description: "立即配置摄像头与微信推送"; \
  Flags: postinstall nowait skipifsilent

[UninstallRun]
; 取消开机自启（reg delete 找不到键会返回非 0，不当作失败）。
; RunOnceId 必填：升级时新旧两份卸载器都会跑，没有它同一条会被执行两次。
Filename: "{cmd}"; Parameters: "/c reg delete ""HKCU\Software\Microsoft\Windows\CurrentVersion\Run"" /v RoomWatch /f >nul 2>&1"; \
  RunOnceId: "RoomWatchRemoveAutostart"; Flags: runhidden
; 新版优先用计划任务；旧 Run 值也要删，避免升级后两套自启同时拉起 guardian。
Filename: "{cmd}"; Parameters: "/c schtasks /Delete /TN RoomWatchGuardian /F >nul 2>&1"; \
  RunOnceId: "RoomWatchRemoveTask"; Flags: runhidden

[UninstallDelete]
; runtime\ 整体强删：comtypes 运行时会往 site-packages\comtypes\gen\ 生成
; SpeechLib.py 等 5 个文件，它们不在安装清单里，不显式删就留一堆残骸。
; config.local.yaml、data\（抓拍/录像/人脸）、var\（数据库/媒体）一律保留 ——
; 升级时先跑旧版卸载器，删了就等于每次升级都丢配置与素材。
Type: filesandordirs; Name: "{app}\runtime"
Type: filesandordirs; Name: "{app}\var\logs"

[Code]
// 装完在完成页说清下一步，客户不用猜「然后呢」。
procedure CurPageChanged(CurPageID: Integer);
begin
  if CurPageID = wpFinished then
    WizardForm.FinishedLabel.Caption :=
      '安装完成。' + #13#10 + #13#10 +
      '如果刚才弹出的配置向导没配完，可以稍后双击桌面上的 room-watch，' + #13#10 +
      '在界面里点「配置」重跑；或用开始菜单里的快捷方式启动。';
end;
