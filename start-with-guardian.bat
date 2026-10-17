@echo off 2>nul
@echo off
rem  UTF-8 with BOM. Keep BOTH echo-off lines: cmd.exe does not always
rem  honour the BOM, and then line 1 fails silently while line 2 still
rem  turns echo off. Full explanation in setup-hooks.bat.
chcp 65001 >nul
title 房间监控 - 守护模式
cd /d "%~dp0"

set PY=C:\Users/user\.workbuddy\binaries\python\envs\default\Scripts\python.exe

if not exist "%PY%" (
  echo [错误] 找不到 Python 解释器：
  echo   %PY%
  echo 请确认路径正确，或改成你环境里的 python.exe
  pause
  exit /b 1
)

echo ==========================================================
echo   房间监控 - 守护模式
echo.
echo   本窗口运行的是「守护进程」，它负责：
echo     - 监控进程死掉 / 卡死时自动重启
echo     - 每 60 秒检查一次
echo   监控本身的日志在 watcher.log
echo.
echo   关闭本窗口 = 停止守护
echo   （已在运行的监控不会被关掉，只是没人看着它了）
echo ==========================================================
echo.

"%PY%" guardian.py --config config.yaml

echo.
echo 守护已退出。
pause
