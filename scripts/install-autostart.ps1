# 把「房间监控守护」注册成 Windows 计划任务
#
# 为什么需要你亲手跑这一步
# ------------------------
# 计划任务属于「持久化自启」，必须在你自己许可下创建。
# 这个脚本把要做的事完整列出来，你看得见每一步。
#
# 它会创建两个触发器：
#   1. 你登录 Windows 时立刻跑一次
#   2. 此后每 3 分钟跑一次（rw guard --once 是幂等的：
#      监控活着就什么都不做，所以跑多少次都安全）
#
# 怎么用
# ------
# 右键这个文件 → 「使用 PowerShell 运行」
# 如果提示执行策略限制，先运行：
#   Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
#
# 卸载
# ----
#   Unregister-ScheduledTask -TaskName "RoomWatchGuardian" -Confirm:$false

$ErrorActionPreference = "Stop"

# 项目根由脚本位置推导，换机器/换目录都不用改这里
$wd = Split-Path -Parent $PSScriptRoot
$py = Join-Path $env:USERPROFILE ".workbuddy\binaries\python\envs\default\Scripts\python.exe"
if (-not (Test-Path $py)) { $py = "python" }

if (-not (Test-Path (Join-Path $wd "roomwatch\cli.py"))) {
    Write-Host "[错误] 找不到 $wd\roomwatch\cli.py，仓库可能不完整" -ForegroundColor Red
    pause
    exit 1
}

Write-Host "正在注册开机自启（优先计划任务，失败回退 Run 键）..." -ForegroundColor Cyan
Write-Host ""

& (Join-Path $wd "rw.cmd") autostart --enable
if ($LASTEXITCODE -ne 0) {
    Write-Host "[错误] 自启注册失败，请看上方输出。" -ForegroundColor Red
    pause
    exit 1
}

Write-Host ""
& (Join-Path $wd "rw.cmd") autostart
Write-Host ""
Write-Host "取消：& (Join-Path $wd 'rw.cmd') autostart --disable" -ForegroundColor DarkGray
pause
