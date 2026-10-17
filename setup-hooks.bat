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
rem  NOTE ON ENCODING -- do not put CJK text in rem/:: lines here.
rem  cmd.exe mis-parses multi-byte characters in comment lines: the
rem  tail of the line gets executed as a command ("... is not
rem  recognized as an internal or external command"). CJK is safe in
rem  echo lines, so all user-facing text below is Chinese via echo,
rem  and comments stay ASCII. Verified by running this file.
rem ============================================================

echo.
echo ==== room-watch：启用提交前检查 ====
echo.

rem ---- 1. Make sure we are inside a Git repository ----
git rev-parse --is-inside-work-tree >nul 2>&1
if errorlevel 1 (
    echo [失败] 当前目录不是 Git 仓库：%CD%
    echo.
    echo        请在本仓库的根目录下运行本脚本。
    exit /b 1
)

rem ---- 2. Find a python that actually executes ----
rem Do not trust "command exists": WindowsApps\python.exe is a stub
rem that exists but cannot run. Only accept one that passes -c.
set "PY="
set "PYARGS="
rem CAND must be assigned OUTSIDE any parenthesized block: %CAND%
rem inside the same block would expand to empty at parse time.
set "CAND=C:\Users/user\.workbuddy\binaries\python\envs\default\Scripts\python.exe"

if defined ROOMWATCH_PYTHON (
    "%ROOMWATCH_PYTHON%" -c "import sys" >nul 2>&1
    if not errorlevel 1 set "PY=%ROOMWATCH_PYTHON%"
)

if not defined PY (
    if exist "%CAND%" (
        "%CAND%" -c "import sys" >nul 2>&1
        if not errorlevel 1 set "PY=%CAND%"
    )
)

if not defined PY (
    python -c "import sys" >nul 2>&1
    if not errorlevel 1 set "PY=python"
)

if not defined PY (
    python3 -c "import sys" >nul 2>&1
    if not errorlevel 1 set "PY=python3"
)

if not defined PY (
    py -3 -c "import sys" >nul 2>&1
    if not errorlevel 1 (
        set "PY=py"
        set "PYARGS=-3"
    )
)

if not defined PY (
    echo [阻断] 找不到可用的 python，无法启用提交前检查。
    echo.
    echo        请把解释器加入 PATH，或显式指定，例如：
    echo        ROOMWATCH_PYTHON=C:\Users/user\.workbuddy\binaries\python\envs\default\Scripts\python.exe
    echo.
    echo        自检命令：python .githooks\check_secrets.py --self-test
    exit /b 1
)

echo 解释器    : %PY% %PYARGS%

rem ---- 3. Set core.hooksPath (idempotent) ----
git config core.hooksPath .githooks
if errorlevel 1 (
    echo [失败] git config core.hooksPath .githooks 执行失败。
    exit /b 1
)

rem ---- 4. Read it back and show it: must be .githooks, not empty ----
set "HOOKSPATH="
for /f "delims=" %%V in ('git config core.hooksPath') do set "HOOKSPATH=%%V"
echo hooksPath : %HOOKSPATH%

if not "%HOOKSPATH%" == ".githooks" (
    echo [失败] 回读到的 core.hooksPath 是「%HOOKSPATH%」，不是预期的 .githooks。
    exit /b 1
)

rem ---- 5. Both hook files must exist ----
if not exist ".githooks\pre-commit" (
    echo [失败] 找不到 .githooks\pre-commit，仓库可能不完整。
    exit /b 1
)
if not exist ".githooks\check_secrets.py" (
    echo [失败] 找不到 .githooks\check_secrets.py，仓库可能不完整。
    exit /b 1
)

echo 钩子脚本  : .githooks\pre-commit + .githooks\check_secrets.py 均已就位
echo.

rem ---- 6. Self-check: is the current config.yaml passing or blocked? ----
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
"%PY%" %PYARGS% ".githooks\check_secrets.py" --config config.yaml --no-git
if errorlevel 1 (
    echo.
    echo [未通过] 上面的检查没通过，现在提交会被拦截。按上面打印的修改方法处理后再提交。
    echo          钩子本身已启用成功，这一步只是告诉你当前 config.yaml 的状态。
    exit /b 1
)

echo.
echo ==== 完成 ====
echo core.hooksPath 已设为 .githooks，之后每次 git commit 都会自动拦截
echo 真实凭据与隐私文件。本脚本可以重复运行，结果不变。
echo.
exit /b 0
