# 把免安装目录打成单个 setup.exe（需要 Inno Setup 6）
#
# 前置
# ----
# 1. 先跑 build-portable.ps1（产出 dist\room-watch-<版本>\）
# 2. 装 Inno Setup 6：winget install -e --id JRSoftware.InnoSetup
#    （只需本机构建用一次，客户机上不需要）
#
# 产物：dist\room-watch-<版本>-setup.exe
#
# 为什么还要这一层
# ----------------
# zip 已经能用，但客户双击 zip 里的「配置.bat」前得先解压到合适的位置、
# 还得自己建快捷方式。setup.exe 把这些一起做完：装到 %LocalAppData%\Programs，
# 建开始菜单快捷方式，装完直接弹配置向导，卸载里也带取消开机自启。

param(
    # 可选：换个输出文件名。正在运行的安装器会锁住同名文件，升级验证时用得上。
    [string]$OutputName = ""
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$version = (Get-Content (Join-Path $root "VERSION") -Raw).Trim()
$src = Join-Path $root "dist\room-watch-$version"
$baseName = if ($OutputName) { $OutputName } else { "room-watch-$version-setup" }

if (-not (Test-Path $src)) {
    Write-Host "[错误] 找不到 $src" -ForegroundColor Red
    Write-Host "先跑：powershell -ExecutionPolicy Bypass -File scripts\build-portable.ps1"
    exit 1
}

$iscc = $null
$candidates = @(
    "C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
    "C:\Program Files\Inno Setup 6\ISCC.exe",
    "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe"
)
foreach ($c in $candidates) {
    if (Test-Path $c) { $iscc = $c; break }
}
if (-not $iscc) {
    $cmd = Get-Command ISCC.exe -ErrorAction SilentlyContinue
    if ($cmd) { $iscc = $cmd.Source }
}

if (-not $iscc) {
    Write-Host "[缺少 Inno Setup]" -ForegroundColor Yellow
    Write-Host "装一次即可（仅构建机需要，客户机不需要）："
    Write-Host "  winget install -e --id JRSoftware.InnoSetup"
    Write-Host "装完重跑本脚本。"
    exit 1
}

Write-Host "用 $iscc 编译安装包 ..." -ForegroundColor Cyan
& $iscc "/DAppVersion=$version" "/DSourceDir=$src" "/F$baseName" (Join-Path $PSScriptRoot "room-watch.iss")
if ($LASTEXITCODE -ne 0) {
    Write-Host "[失败] ISCC 返回 $LASTEXITCODE" -ForegroundColor Red
    exit $LASTEXITCODE
}

$setup = Join-Path $root "dist\$baseName.exe"
if (Test-Path $setup) {
    $mb = (Get-Item $setup).Length / 1MB
    Write-Host ("完成：{0}（{1:N1} MB）" -f $setup, $mb) -ForegroundColor Green
} else {
    Write-Host "[失败] 没找到产物 $setup" -ForegroundColor Red
    exit 1
}
