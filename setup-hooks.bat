@echo off 2>nul
@echo off
chcp 65001 >nul
cd /d "%~dp0"
setlocal

rem ============================================================
rem  setup-hooks.bat - enable the Git pre-commit hook
rem
rem  WHY THIS EXISTS:
rem  core.hooksPath is a LOCAL setting. It is NOT distributed with
rem  the repository, so every fresh clone starts with ZERO protection.
rem  Without it, push credentials and private files can silently get
rem  into Git history (and cannot be removed afterwards).
rem
rem  Idempotent: running it 100 times gives the same result as once.
rem  Scope: hooks only. It never starts watcher or gallery.
rem
rem  NOTE ON ENCODING -- read this before touching the first two lines.
rem  This file is UTF-8 and MUST keep its BOM (EF BB BF). Two separate
rem  cmd.exe traps are being worked around here:
rem
rem  Trap 1 (no BOM): cmd decodes the file with the machine ANSI code
rem  page (936/GBK here). UTF-8 CJK bytes then get mis-aligned as GBK
rem  lead/trail bytes, and a line INSIDE A PARENTHESISED BLOCK can be
rem  split in the middle: the tail runs as a command and prints
rem  "'...' is not recognized as an internal or external command".
rem  Observed on the failure branch before this was fixed.
rem
rem  Trap 2 (with BOM, but cmd ignores it): cmd does NOT always honour
rem  the BOM. It reproducibly ignores it on the very first cmd.exe
rem  launch of a fresh session, which is exactly what "double-click the
rem  file" does. The three BOM bytes then end up glued to line 1, so
rem  "@echo off" is no longer the first token: echo stays ON for the
rem  whole file and you get "'@echo' is not recognized".
rem
rem  Hence BOTH of these conventions, and both are load-bearing:
rem    lines 1-2  @echo off 2>nul  then  @echo off
rem               (line 1 fails silently when the BOM is glued on;
rem                line 2 always turns echo off)
rem    everywhere  no CJK inside ( ... ) blocks -- use goto instead.
rem               When the BOM is ignored the file is decoded as GBK,
rem               and only the inside-a-block lines were seen to split.
rem
rem  Keep rem/:: comment lines ASCII (cheap insurance). Never save this
rem  file as ANSI/GBK.
rem ============================================================

echo.
echo ==== room-watch：启用提交前检查 ====
echo.

rem ---- 1. Make sure we are inside a Git repository ----
git rev-parse --is-inside-work-tree >nul 2>&1
if not errorlevel 1 goto :find_python
echo [失败] 当前目录不是 Git 仓库：%CD%
echo.
echo        请在本仓库的根目录下运行本脚本。
exit /b 1

rem ---- 2. Find a python that actually executes ----
rem Do not trust "command exists": WindowsApps\python.exe is a stub
rem that exists but cannot run. Only accept one that passes -c.
:find_python
set "PY="
set "PYARGS="
rem CAND is assigned at top level (not inside any block) so that %CAND%
rem expands normally; inside a block it would expand at parse time.
set "CAND=C:\Users/user\.workbuddy\binaries\python\envs\default\Scripts\python.exe"

if defined ROOMWATCH_PYTHON (
    "%ROOMWATCH_PYTHON%" -c "import sys" >nul 2>&1
    if not errorlevel 1 set "PY=%ROOMWATCH_PYTHON%"
)
if defined PY goto :set_hookspath

if exist "%CAND%" (
    "%CAND%" -c "import sys" >nul 2>&1
    if not errorlevel 1 set "PY=%CAND%"
)
if defined PY goto :set_hookspath

python -c "import sys" >nul 2>&1
if not errorlevel 1 set "PY=python"
if defined PY goto :set_hookspath

python3 -c "import sys" >nul 2>&1
if not errorlevel 1 set "PY=python3"
if defined PY goto :set_hookspath

py -3 -c "import sys" >nul 2>&1
if errorlevel 1 goto :no_python
set "PY=py"
set "PYARGS=-3"
goto :set_hookspath

:no_python
echo [阻断] 找不到可用的 python，无法启用提交前检查。
echo.
echo        请把解释器加入 PATH，或显式指定，例如：
echo        ROOMWATCH_PYTHON=C:\Users/user\.workbuddy\binaries\python\envs\default\Scripts\python.exe
echo.
echo        自检命令：python .githooks\check_secrets.py --self-test
exit /b 1

rem ---- 3. Set core.hooksPath (idempotent) ----
:set_hookspath
echo 解释器    : %PY% %PYARGS%
git config core.hooksPath .githooks
if not errorlevel 1 goto :read_back
echo [失败] git config core.hooksPath .githooks 执行失败。
exit /b 1

rem ---- 4. Read it back and show it: must be .githooks, not empty ----
:read_back
set "HOOKSPATH="
for /f "delims=" %%V in ('git config core.hooksPath') do set "HOOKSPATH=%%V"
echo hooksPath : %HOOKSPATH%
if "%HOOKSPATH%" == ".githooks" goto :check_files
echo [失败] 回读到的 core.hooksPath 是「%HOOKSPATH%」，不是预期的 .githooks。
exit /b 1

rem ---- 5. Both hook files must exist ----
:check_files
if exist ".githooks\pre-commit" goto :check_py_file
echo [失败] 找不到 .githooks\pre-commit，仓库可能不完整。
exit /b 1

:check_py_file
if exist ".githooks\check_secrets.py" goto :self_check
echo [失败] 找不到 .githooks\check_secrets.py，仓库可能不完整。
exit /b 1

rem ---- 6. Self-check: is the current config.yaml passing or blocked? ----
:self_check
echo 钩子脚本  : .githooks\pre-commit + .githooks\check_secrets.py 均已就位
echo.
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
"%PY%" %PYARGS% ".githooks\check_secrets.py" --config config.yaml --no-git
if not errorlevel 1 goto :done
echo.
echo [未通过] 上面的检查没通过，现在提交会被拦截。按上面打印的修改方法处理后再提交。
echo          钩子本身已启用成功，这一步只是告诉你当前 config.yaml 的状态。
exit /b 1

:done
echo.
echo ==== 完成 ====
echo core.hooksPath 已设为 .githooks，之后每次 git commit 都会自动拦截
echo 真实凭据与隐私文件。本脚本可以重复运行，结果不变。
echo.
exit /b 0
