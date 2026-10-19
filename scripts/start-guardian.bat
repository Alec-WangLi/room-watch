@echo off 2>nul
@echo off
rem  UTF-8 with BOM. Keep BOTH echo-off lines: see scripts\setup-hooks.bat.
chcp 65001 >nul
title 房间监控 - 守护模式
setlocal
cd /d "%~dp0.."

set RW=%~dp0..\rw.cmd

if not exist "%RW%" (
  echo [错误] 找不到 rw.cmd，仓库可能不完整
  pause
  exit /b 1
)

echo ==========================================================
echo   房间监控 - 守护模式
echo.
echo   本窗口运行的是「守护进程」，它负责：
echo     - 监控进程死掉 / 卡死时自动重启
echo     - 每 60 秒检查一次
echo   监控日志：var\logs\watcher.log
echo.
echo   关闭本窗口 = 停止守护
echo   （已在运行的监控不会被关掉，只是没人看着它了）
echo ==========================================================
echo.

"%RW%" guard

echo.
echo 守护已退出。
pause
