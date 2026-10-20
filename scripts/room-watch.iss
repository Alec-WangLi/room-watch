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
; 安装前由 [Code] 主动停掉旧进程；不让 Inno 去猜哪个应用占着文件。
CloseApplications=yes
RestartApplications=no

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
; 首次安装直接打开界面：摄像头、推送、相册都在「设置」里完成，
; 不再弹一个黑框配置向导。升级时不重复打开（IsFirstRun 检查配置是否存在）。
; 命令行用户仍可运行 `rw setup` 或「配置.bat」。
Filename: "{app}\room-watch.exe"; \
  Description: "打开配置界面"; \
  Flags: postinstall nowait skipifsilent; \
  Check: IsFirstRun
; 升级时如果原来监控是在跑的，装完自动恢复；升级不该让用户以为监控还在跑，
; 实际却停在暂停状态。
Filename: "{app}\runtime\python\python.exe"; \
  Parameters: "-m roomwatch start --config ""{app}\config.yaml"""; \
  WorkingDir: "{app}"; \
  Flags: runhidden nowait postinstall skipifsilent; \
  Check: ShouldResume

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
var
  WasRunning: Boolean;

// 升级前记住「监控本来是否在跑」，装完据此恢复。
function DesiredIsRunning(): Boolean;
var
  Lines: TStringList;
  I: Integer;
  Path: String;
begin
  Result := False;
  Path := ExpandConstant('{app}\var\state\desired_state.json');
  if not FileExists(Path) then
    exit;
  Lines := TStringList.Create;
  try
    try
      Lines.LoadFromFile(Path);
      for I := 0 to Lines.Count - 1 do
        if Pos('"state"', Lines[I]) > 0 then
          Result := Pos('running', Lines[I]) > 0;
    except
      Result := False;
    end;
  finally
    Lines.Free;
  end;
end;

function ShouldResume(): Boolean;
begin
  Result := WasRunning;
end;

// 升级前先停掉旧进程。否则 watcher/guardian 锁着 runtime\python，Inno 会弹
// 「无法自动关闭所有应用程序」，用户只能手动去任务管理器里找。
// 顺序：先用程序自己的 stop（让正在写的 mp4/音轨收尾），再按安装目录路径
// 强停剩余进程（不碰用户自己的 Python）。
function StopOldProcesses(): Boolean;
var
  ResultCode: Integer;
  Py, Config, ScriptPath, Script, EscapedRoot, Cmd: String;
begin
  Result := True;
  Py := ExpandConstant('{app}\runtime\python\python.exe');
  Config := ExpandConstant('{app}\config.yaml');

  if FileExists(Py) and FileExists(Config) then
  begin
    Cmd := AddQuotes(Py) + ' -m roomwatch stop --config ' + AddQuotes(Config);
    Exec(ExpandConstant('{cmd}'), '/c ' + Cmd, ExpandConstant('{app}'),
         SW_HIDE, ewWaitUntilTerminated, ResultCode);
  end;

  EscapedRoot := ExpandConstant('{app}');
  StringChangeEx(EscapedRoot, '''', '''''', True);
  ScriptPath := ExpandConstant('{tmp}\roomwatch-stop.ps1');
  Script :=
    '$root = ''' + EscapedRoot + '''' + #13#10 +
    'Get-CimInstance Win32_Process | ForEach-Object {' + #13#10 +
    '  if ($_.ExecutablePath -and $_.ExecutablePath -like "$root*") {' + #13#10 +
    '    Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue' + #13#10 +
    '  }' + #13#10 +
    '}' + #13#10;
  if SaveStringToFile(ScriptPath, Script, False) then
  begin
    Cmd := 'powershell.exe -NoProfile -ExecutionPolicy Bypass -File ' + AddQuotes(ScriptPath);
    Exec(ExpandConstant('{cmd}'), '/c ' + Cmd, '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
    // 再杀一遍：第一次杀完到 Inno 开始替换文件之间，guardian 可能又拉起
    // watcher 锁住 runtime\python（0.1.21 真机安装器返回 5 的一个来源）。
    Sleep(2000);
    Exec(ExpandConstant('{cmd}'), '/c ' + Cmd, '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
    DeleteFile(ScriptPath);
  end;

  // 兜底：界面 exe 的路径信息拿不到时也能关掉。
  Exec(ExpandConstant('{cmd}'), '/c taskkill /F /IM room-watch.exe /T >nul 2>&1',
       '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Sleep(800);
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  WasRunning := DesiredIsRunning();
  StopOldProcesses();
  Result := '';
end;

// [Run] 的 Check：只有真正没配置过才弹首次向导。
// config.local.yaml 是用户配置，升级卸载时会被保留。
function IsFirstRun(): Boolean;
begin
  Result := not FileExists(ExpandConstant('{app}\config.local.yaml'));
end;

// 装完在完成页说清下一步，客户不用猜「然后呢」。
procedure CurPageChanged(CurPageID: Integer);
begin
  if CurPageID = wpFinished then
    WizardForm.FinishedLabel.Caption :=
      '安装完成。' + #13#10 + #13#10 +
      '如果刚才打开的界面还没配完，可以稍后双击桌面上的 room-watch，' + #13#10 +
      '在界面里点「设置」继续；或用开始菜单里的快捷方式启动。';
end;
