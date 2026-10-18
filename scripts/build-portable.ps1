# 构建「免安装分发包」：dist\room-watch-<版本>\
#
# 目标
# ----
# 客户拿到一个文件夹（或一个 zip），解压、双击「配置.bat」回答四个问题就能长期运行。
# **客户机上不需要装 Python、不需要装 ffmpeg、不需要联网下载依赖。**
#
# 为什么不是单文件 exe
# --------------------
# PyInstaller onefile 每次启动都要把 300MB 解压到 %TEMP%，冷启动 10~20 秒，
# 而且反复落地同名临时文件极易被杀软判为可疑。这里的形态是：
# 一个目录 + 一个 zip。要再压成 setup.exe，用 Inno Setup 包一层即可（见 docs/）。
#
# 体积构成（实测）
#   runtime\python        约 27MB   Python 3.13 嵌入式 + 标准库
#   runtime ...\site-packages 约 200MB  cv2 / numpy / requests / yaml / pillow ...
#   tools\ffmpeg.exe      约 90MB   转码 + 合音轨
#   models\*.onnx         约 51MB   人体 + 人脸
#
# 用法
# ----
#   powershell -ExecutionPolicy Bypass -File scripts\build-portable.ps1
#   powershell ... -SkipDeps      # 复用上次装好的 site-packages，只换源码（改代码后常用）
#   powershell ... -NoZip         # 不生成 zip

param(
    [string]$PythonVersion = "3.13.7",
    [switch]$SkipDeps,
    [switch]$NoZip
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$version = (Get-Content (Join-Path $root "VERSION") -Raw).Trim()
$name = "room-watch-$version"
$out = Join-Path $root "dist\$name"
$cache = Join-Path $root "build\cache"
New-Item -ItemType Directory -Force -Path $cache | Out-Null

Write-Host "== 构建 $name ==" -ForegroundColor Cyan
Write-Host "输出目录：$out"

# 复用 site-packages 时必须保留整个 runtime 目录，所以只清源码与配置
if (Test-Path $out) {
    if ($SkipDeps) {
        Write-Host "保留 runtime\（-SkipDeps）"
        Get-ChildItem $out | Where-Object { $_.Name -ne "runtime" } | Remove-Item -Recurse -Force
    } else {
        Remove-Item $out -Recurse -Force
    }
}
New-Item -ItemType Directory -Force -Path $out | Out-Null

# ---------------------------------------------------------------- 1. 内嵌解释器
$pyDir = Join-Path $out "runtime\python"
$pyExe = Join-Path $pyDir "python.exe"
$pyZip = Join-Path $cache "python-$PythonVersion-embed-amd64.zip"

if (-not (Test-Path $pyExe)) {
    if (-not (Test-Path $pyZip)) {
        $url = "https://www.python.org/ftp/python/$PythonVersion/python-$PythonVersion-embed-amd64.zip"
        Write-Host "下载 $url" -ForegroundColor Cyan
        Invoke-WebRequest -Uri $url -OutFile $pyZip -UseBasicParsing
    }
    Write-Host "解压内嵌解释器 ..." -ForegroundColor Cyan
    Expand-Archive -Path $pyZip -DestinationPath $pyDir -Force

    # 嵌入式发行版默认把 site-packages 排除在 sys.path 之外（为了「完全隔离」）。
    # 装第三方包前必须打开：加一行 Lib\site-packages 并启用 import site。
    $pth = Get-ChildItem $pyDir -Filter "python*._pth" | Select-Object -First 1
    if (-not $pth) {
        Write-Host "[失败] 内嵌包里没有 ._pth 文件，发行版结构变了？" -ForegroundColor Red
        exit 1
    }
    $lines = Get-Content $pth.FullName
    if ($lines -notcontains "Lib\site-packages") {
        $lines = @($lines | Where-Object { $_ -ne "#import site" })
        $lines += "Lib\site-packages"
        $lines += "import site"
        Set-Content -Path $pth.FullName -Value $lines -Encoding ASCII
    }
}

# ---------------------------------------------------------------- 2. 依赖
$sitePackages = Join-Path $pyDir "Lib\site-packages"
if (-not $SkipDeps) {
    # 用外面装了 pip 的同版本 Python 往目标目录装：内嵌版没有 pip，
    # 而 get-pip 引导又要额外联网一步，不如直接把 wheel 解到目标目录。
    # wheel 与解释器 ABI 强绑定，所以必须挑到 $PythonVersion 同一个小版本。
    $pipPy = $null
    $candidates = @()
    $mm = ($PythonVersion -split '\.')[0..1] -join ''    # 313
    $mf = ($PythonVersion -split '\.')[0..1] -join '.'   # 3.13

    # py 启动器最可靠：能按版本号挑，且解析出真实 exe 路径
    $launcher = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($launcher) {
        try {
            $resolved = & $launcher.Source "-$mf" -c "import sys;print(sys.executable)" 2>$null
            if ($LASTEXITCODE -eq 0 -and $resolved) { $candidates += $resolved.Trim() }
        } catch { }
    }
    $onPath = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($onPath) { $candidates += $onPath.Source }
    $candidates += @(
        "$env:LOCALAPPDATA\Programs\Python\Python$mm\python.exe",
        "C:\Python$mm\python.exe"
    )

    foreach ($c in $candidates) {
        if (-not (Test-Path $c)) { continue }
        try {
            $v = & $c -c "import sys;print(sys.version_info[:2])" 2>$null
            if ($LASTEXITCODE -eq 0 -and $v -match ($mf -replace '\.', ', ')) { $pipPy = $c; break }
        } catch { }
    }
    if (-not $pipPy) {
        Write-Host "[失败] 找不到 Python $PythonVersion 来装依赖（wheel 必须与解释器 ABI 一致）" -ForegroundColor Red
        Write-Host "提示：装一个 Python $PythonVersion 并加入 PATH，或用 py 启动器注册该版本。"
        exit 1
    }
    Write-Host "依赖安装用解释器：$pipPy" -ForegroundColor Cyan

    New-Item -ItemType Directory -Force -Path $sitePackages | Out-Null
    $pkgs = @(
        "opencv-python>=4.10",
        "numpy>=1.26,<3",
        "requests>=2.32",
        "PyYAML>=6.0",
        "sounddevice>=0.4.6",
        "pyttsx3>=2.90",
        "psutil>=5.9",
        "pycaw>=20240210",
        "comtypes>=1.4",
        "pillow>=10.0"
    )
    Write-Host "安装依赖到 runtime\python\Lib\site-packages ..." -ForegroundColor Cyan
    & $pipPy -m pip install --quiet --disable-pip-version-check --no-warn-script-location `
        --only-binary=:all: --target $sitePackages @pkgs
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[失败] 依赖安装失败" -ForegroundColor Red
        exit 1
    }
    # 装完再清一遍缓存与测试残留，能省十几 MB
    Get-ChildItem $sitePackages -Recurse -Directory -Filter "__pycache__" -ErrorAction SilentlyContinue |
        Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
}

New-Item -ItemType Directory -Force -Path $sitePackages | Out-Null

# 内嵌 Python 一旦存在 ._pth，就进入「隔离模式」：**当前目录不再进 sys.path**。
# 于是 `python -m roomwatch` 在任何目录下都报 No module named roomwatch
# （第一次构建时踩到：自检直接失败）。
# 解法是把包根写进 site-packages 的 .pth；路径相对 .pth 自己，
# 客户把整个文件夹挪到别处也不会失效。
#   runtime\python\Lib\site-packages\..\..\..\..  →  包根
$pthFile = Join-Path $sitePackages "roomwatch-root.pth"
Set-Content -Path $pthFile -Value "..\..\..\.." -Encoding ASCII

# ---------------------------------------------------------------- 2b. 瘦身
# pyttsx3 会拖进整套 pywin32，其中 IDE 与文档运行时用不到。
# 实测（0.1.3）删掉 win32comext 后 pyttsx3.init() 仍能枚举 2 个语音：
# win32comext 是 shell/mapi/bits/axscript 等独立扩展，SAPI5 只走 win32com + win32\lib。
# 但 win32\lib\win32con.py 必须留 —— 删了语音直接不可用（踩过）。
$trim = @(
    "PyWin32.chm", "pythonwin", "win32comext", "numpy\tests", "cv2\samples", "bin"
)
foreach ($rel in $trim) {
    $target = Join-Path $sitePackages $rel
    if (Test-Path $target) { Remove-Item $target -Recurse -Force -ErrorAction SilentlyContinue }
}

# ---------------------------------------------------------------- 3. 源码与资源
Write-Host "复制源码与资源 ..." -ForegroundColor Cyan
$appItems = @("roomwatch", "models", "config.yaml", "VERSION", "VERSION-APPROVED",
              "requirements.txt", "README.md", "CHANGELOG.md", "docs", "assets")
foreach ($item in $appItems) {
    $src = Join-Path $root $item
    if (Test-Path $src) {
        Copy-Item $src -Destination $out -Recurse -Force
    }
}
# pyc 带过来会和新解释器版本不一致，删掉让它重新生成
Get-ChildItem (Join-Path $out "roomwatch") -Recurse -Directory -Filter "__pycache__" -ErrorAction SilentlyContinue |
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue

New-Item -ItemType Directory -Force -Path (Join-Path $out "data\captures") | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $out "var\logs") | Out-Null

# 分发包不能带本机的凭据与隐私数据
$local = Join-Path $root "config.local.yaml"
if (Test-Path $local) {
    Write-Host "跳过 config.local.yaml（含本机 webhook / 密码，不能分发给客户）" -ForegroundColor Yellow
}

# ---------------------------------------------------------------- 4. ffmpeg
$toolsDir = Join-Path $out "tools"
New-Item -ItemType Directory -Force -Path $toolsDir | Out-Null
$ffmpegZip = Join-Path $cache "ffmpeg-essentials.zip"
if (-not (Test-Path $ffmpegZip)) {
    Write-Host "下载 ffmpeg essentials（约 115MB）..." -ForegroundColor Cyan
    Invoke-WebRequest -Uri "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip" `
        -OutFile $ffmpegZip -UseBasicParsing
}
$ffTmp = Join-Path $cache "ffmpeg-extract"
if (-not (Test-Path $ffTmp)) {
    Expand-Archive -Path $ffmpegZip -DestinationPath $ffTmp -Force
}
$ffExe = Get-ChildItem $ffTmp -Recurse -Filter "ffmpeg.exe" | Select-Object -First 1
if ($ffExe) {
    Copy-Item $ffExe.FullName (Join-Path $toolsDir "ffmpeg.exe") -Force
} else {
    Write-Host "[警告] ffmpeg 包里没找到 ffmpeg.exe，录像将没有音轨" -ForegroundColor Yellow
}

# ---------------------------------------------------------------- 5. 启动器
Write-Host "生成启动脚本 ..." -ForegroundColor Cyan

# cmd 的中文在 936 代码页下经常乱码，脚本正文一律用 ASCII，中文只出现在文件名里。
$batEncoding = "ASCII"

$rwCmd = @'
@echo off
cd /d "%~dp0"
"%~dp0runtime\python\python.exe" -m roomwatch %*
'@
Set-Content -Path (Join-Path $out "rw.cmd") -Value $rwCmd -Encoding $batEncoding

$setupBat = @'
@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================================
echo  room-watch first-run setup
echo ============================================================
"%~dp0runtime\python\python.exe" -m roomwatch setup
echo.
pause
'@
Set-Content -Path (Join-Path $out "配置.bat") -Value $setupBat -Encoding $batEncoding

$startBat = @'
@echo off
chcp 65001 >nul
cd /d "%~dp0"
if exist "%~dp0room-watch.exe" (
    start "" "%~dp0room-watch.exe"
) else (
    start "" "%~dp0runtime\python\pythonw.exe" -m roomwatch gui
)
'@
Set-Content -Path (Join-Path $out "启动.bat") -Value $startBat -Encoding $batEncoding

$doctorBat = @'
@echo off
chcp 65001 >nul
cd /d "%~dp0"
"%~dp0runtime\python\python.exe" -m roomwatch doctor
echo.
pause
'@
Set-Content -Path (Join-Path $out "自检.bat") -Value $doctorBat -Encoding $batEncoding

# ---------------------------------------------------------------- 6. 界面
# 必须防「拿旧的 GUI 壳去打包」：界面壳是 PyInstaller 产物，改了 gui.py
# 它不会自己更新，而它恰恰是客户最直接接触的东西。0.1.9 就踩过 ——
# 源码改完只重跑本脚本，结果安装包里还是 0.1.7 的界面（停止、设置全都没有）。
$guiExe = Join-Path $root "dist\room-watch.exe"
$guiSrc = Join-Path $root "roomwatch\gui.py"
if (-not (Test-Path $guiExe)) {
    Write-Host "[失败] 没有 dist\room-watch.exe，先跑 scripts\build-exe.ps1 生成界面壳" -ForegroundColor Red
    exit 1
}
if ((Get-Item $guiExe).LastWriteTime -lt (Get-Item $guiSrc).LastWriteTime) {
    Write-Host "[失败] dist\room-watch.exe 比 roomwatch\gui.py 旧：界面壳没重新打包。" -ForegroundColor Red
    Write-Host "       先跑 scripts\build-exe.ps1，再重跑本脚本。" -ForegroundColor Yellow
    exit 1
}
Copy-Item $guiExe (Join-Path $out "room-watch.exe") -Force

# ---------------------------------------------------------------- 7. 自检
Write-Host "`n运行包内自检 ..." -ForegroundColor Cyan
$pyExe = Join-Path $out "runtime\python\python.exe"
# 故意在包目录之外执行：验证 .pth 让 `-m roomwatch` 在任何 cwd 下都能找到包
Push-Location $env:TEMP
& $pyExe -m roomwatch version
$verCode = $LASTEXITCODE
& $pyExe -c "import cv2, numpy, yaml, requests, PIL, sounddevice, psutil, pyttsx3; print('deps ok cv2', cv2.__version__)"
$depCode = $LASTEXITCODE
Pop-Location
if ($verCode -ne 0 -or $depCode -ne 0) {
    Write-Host "[失败] 包内自检不通过（版本 $verCode / 依赖 $depCode）" -ForegroundColor Red
    exit 1
}
Push-Location $out
& $pyExe -m roomwatch doctor
Pop-Location

$size = (Get-ChildItem $out -Recurse -File | Measure-Object -Property Length -Sum).Sum / 1MB
Write-Host ("包体积：{0:N1} MB" -f $size) -ForegroundColor Green

# ---------------------------------------------------------------- 8. 打包 zip
if (-not $NoZip) {
    $zip = Join-Path $root "dist\$name.zip"
    if (Test-Path $zip) { Remove-Item $zip -Force }
    Write-Host "压缩 ..." -ForegroundColor Cyan
    Compress-Archive -Path (Join-Path $out "*") -DestinationPath $zip -CompressionLevel Optimal
    $zipSize = (Get-Item $zip).Length / 1MB
    Write-Host ("zip：{0}（{1:N1} MB）" -f $zip, $zipSize) -ForegroundColor Green
}

Write-Host "`n完成：$out" -ForegroundColor Green
Write-Host "客户使用顺序：解压 → 双击 配置.bat → 双击 启动.bat" -ForegroundColor Green
