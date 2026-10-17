@echo off 2>nul
@echo off
rem  UTF-8 with BOM. Keep BOTH echo-off lines: cmd.exe does not always
rem  honour the BOM, and then line 1 fails silently while line 2 still
rem  turns echo off. Full explanation in setup-hooks.bat.
chcp 65001 >nul
cd /d "%~dp0"

rem 独立运行的监控启动脚本：双击即可，不依赖任何对话窗口。
rem 想开机自启：Win+R 输入 shell:startup，把这个文件的快捷方式放进去。

set "PY=C:\Users/user\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1

echo [%date% %time%] 启动房间监控 >> watcher.log
"%PY%" watcher.py --config config.yaml >> watcher.log 2>&1
