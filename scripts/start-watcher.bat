@echo off 2>nul
@echo off
rem  UTF-8 with BOM. Keep BOTH echo-off lines: see scripts\setup-hooks.bat.
chcp 65001 >nul
setlocal
cd /d "%~dp0.."

rem  只启动监控（前台常驻）。要守护+监控一起管，用 start-guardian.bat。
rem  日志写到 var\logs\watcher.log（.gitignore 已忽略整个 var/）。

set "PY=C://Users/user//.workbuddy//binaries//python//envs//default//Scripts//python.exe"
if not exist "%PY%" set "PY=python"
if not exist "var\logs" mkdir "var\logs"

set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1

echo [%date% %time%] start >> var\logs\watcher.log
"%PY%" -m roomwatch start --config config.yaml >> var\logs\watcher.log 2>&1
