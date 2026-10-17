# 在桌面创建「房间监控」快捷方式（不改注册表，不写开机自启）
#
# 用法：右键 → 使用 PowerShell 运行
# 卸载：直接删除桌面上的快捷方式即可。

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$exe = Join-Path $root "dist\room-watch.exe"

if (-not (Test-Path $exe)) {
    Write-Host "[错误] 找不到 $exe" -ForegroundColor Red
    Write-Host "请先运行 scripts\build-exe.ps1 打包" -ForegroundColor Yellow
    exit 1
}

$desktop = [Environment]::GetFolderPath("Desktop")
$lnk = Join-Path $desktop "房间监控.lnk"

$shell = New-Object -ComObject WScript.Shell
$item = $shell.CreateShortcut($lnk)
$item.TargetPath = $exe
$item.WorkingDirectory = $root
$item.IconLocation = "$exe,0"
$item.Description = "房间监控：状态、启停、抓拍与相册"
$item.Save()

Write-Host "已创建：$lnk" -ForegroundColor Green
