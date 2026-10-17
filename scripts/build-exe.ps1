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

$sys = "C:\Users/user\AppData\Local\Programs\Python\Python313\python.exe"
if (-not (Test-Path $sys)) {
    Write-Host "[错误] 找不到带 tkinter 的 Python：$sys" -ForegroundColor Red
    exit 1
}
if (-not (Test-Path ".venv-gui\Scripts\python.exe")) {
    Write-Host "创建 .venv-gui ..." -ForegroundColor Cyan
    & $sys -m venv .venv-gui
}
& ".venv-gui\Scripts\python.exe" -m pip install --quiet --upgrade pillow pyinstaller

Write-Host "开始打包 ..." -ForegroundColor Cyan
& ".venv-gui\Scripts\pyinstaller.exe" --noconfirm --clean --onefile --windowed `
    --name room-watch --paths . `
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
