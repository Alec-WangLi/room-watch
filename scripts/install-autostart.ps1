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

Write-Host "即将创建计划任务 RoomWatchGuardian：" -ForegroundColor Cyan
Write-Host "  程序    : $py"
Write-Host "  参数    : -m roomwatch guard --once"
Write-Host "  目录    : $wd"
Write-Host "  触发    : 登录时 + 每 3 分钟"
Write-Host ""

$action = New-ScheduledTaskAction -Execute $py -Argument "-m roomwatch guard --once" -WorkingDirectory $wd
$t1 = New-ScheduledTaskTrigger -AtLogOn
$t2 = New-ScheduledTaskTrigger -Once -At (Get-Date) `
        -RepetitionInterval (New-TimeSpan -Minutes 3) `
        -RepetitionDuration (New-TimeSpan -Days 3650)
$settings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -StartWhenAvailable `
        -MultipleInstances IgnoreNew `
        -ExecutionTimeLimit (New-TimeSpan -Minutes 10)

Register-ScheduledTask -TaskName "RoomWatchGuardian" `
    -Action $action -Trigger @($t1, $t2) -Settings $settings `
    -Description "room-watch 守护：登录时启动，此后每3分钟检查监控是否存活，挂了自动重启" `
    -Force | Out-Null

$task = Get-ScheduledTask -TaskName "RoomWatchGuardian"
Write-Host ""
Write-Host "已创建：$($task.TaskName)  状态：$($task.State)" -ForegroundColor Green
Write-Host ""
Write-Host "验证一下（应输出守护状态）：" -ForegroundColor Cyan
& $py -m roomwatch guard --config (Join-Path $wd "config.yaml") --status
Write-Host ""
Write-Host "卸载命令：Unregister-ScheduledTask -TaskName RoomWatchGuardian -Confirm:`$false" -ForegroundColor DarkGray
pause
