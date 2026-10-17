@echo off 2>nul
@echo off
rem  room-watch global command. UTF-8 with BOM: keep BOTH echo-off lines.
rem  Line 1 fails silently when cmd ignores the BOM, line 2 always works.
rem  Do NOT put CJK text in this file: cmd may decode it as GBK and split lines.
setlocal
pushd "%~dp0"
set "PY="
if defined ROOMWATCH_PYTHON if exist "%ROOMWATCH_PYTHON%" set "PY=%ROOMWATCH_PYTHON%"
if not defined PY if exist "%USERPROFILE%\.workbuddy\binaries\python\envs\default\Scripts\python.exe" set "PY=%USERPROFILE%\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
if not defined PY set "PY=python"
"%PY%" -m roomwatch %*
set "RC=%ERRORLEVEL%"
popd
exit /b %RC%
