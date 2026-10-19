# 重新打包桌面程序：dist\room-watch.exe
#
# 为什么用另一个虚拟环境
# ----------------------
# 主环境（含 cv2 / ultralytics）没有 tkinter，打不出 GUI；
# 这里用系统 Python 3.13 建 .venv-gui（自带 tkinter），只装 pillow + pyinstaller。
# 监控进程仍在主环境跑，两者互不干扰。
#
# 产物：dist\room-watch.exe（单文件，内嵌 tkinter 与图标）

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

# 找一个「带 tkinter」的 Python。机器上常装多个解释器，靠版本号猜不可靠，
# 直接逐个 import tkinter 验证；py.exe 启动器能解析出真实 exe 路径。
$sys = $null
$candidates = @()
foreach ($name in @("py.exe", "python.exe")) {
    $cmd = Get-Command $name -ErrorAction SilentlyContinue
    if ($cmd) { $candidates += $cmd.Source }
}
$candidates += @(
    "$env:LOCALAPPDATA\Programs\Python\Python313\python.exe",
    "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe",
    "$env:LOCALAPPDATA\Programs\Python\Python311\python.exe",
    "C:\Python313\python.exe",
    "C:\Python312\python.exe"
)
foreach ($c in $candidates) {
    if (-not (Test-Path $c)) { continue }
    $resolved = & $c -c "import tkinter,sys; print(sys.executable)" 2>$null
    if ($LASTEXITCODE -eq 0 -and $resolved) { $sys = $resolved.Trim(); break }
}
if (-not $sys) {
    Write-Host "[错误] 找不到带 tkinter 的 Python（需要 3.10+）。" -ForegroundColor Red
    Write-Host "装一个官方安装包并勾选 tcl/tk，或把带 tkinter 的 python.exe 加进 PATH。"
    exit 1
}
Write-Host "界面构建用解释器：$sys" -ForegroundColor Cyan
if (-not (Test-Path ".venv-gui\Scripts\python.exe")) {
    Write-Host "创建 .venv-gui ..." -ForegroundColor Cyan
    & $sys -m venv .venv-gui
}
& ".venv-gui\Scripts\python.exe" -m pip install --quiet --upgrade pillow psutil pyinstaller

Write-Host "开始打包 ..." -ForegroundColor Cyan
& ".venv-gui\Scripts\pyinstaller.exe" --noconfirm --clean --onefile --windowed `
    --name room-watch --paths . `
    --hidden-import psutil `
    --add-data "$root\VERSION;." `
    --icon "$root\assets\room-watch.ico" `
    --distpath dist --workpath build\pyi --specpath build `
    scripts\gui_entry.py

if (-not (Test-Path "dist\room-watch.exe")) {
    Write-Host "[失败] 没有产出 dist\room-watch.exe" -ForegroundColor Red
    exit 1
}

Write-Host "运行自检 ..." -ForegroundColor Cyan
& "dist\room-watch.exe" --selftest
Write-Host "完成：$root\dist\room-watch.exe" -ForegroundColor Green
