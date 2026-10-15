@echo off
setlocal
chcp 65001 >nul
set "PYTHONUTF8=1"
set "CODEX_GUI_PYTHON="

rem Prefer a project virtual environment, then the Windows Python launcher.
if exist "%~dp0.venv\Scripts\python.exe" (
    set "CODEX_GUI_PYTHON=%~dp0.venv\Scripts\python.exe"
    goto check_python
)
where py.exe >nul 2>nul
if not errorlevel 1 (
    for /f "delims=" %%P in ('py -3 -c "import sys; print(sys.executable)" 2^>nul') do set "CODEX_GUI_PYTHON=%%P"
)
if defined CODEX_GUI_PYTHON goto check_python
for /f "delims=" %%P in ('where python.exe 2^>nul') do if not defined CODEX_GUI_PYTHON set "CODEX_GUI_PYTHON=%%P"
if defined CODEX_GUI_PYTHON goto check_python

echo 未找到 Python。请安装 Python 3.10 或更新版本，并包含 Tcl/Tk。
echo 安装时勾选 Add python.exe to PATH，然后重新双击此文件。
echo 安装说明：https://www.python.org/downloads/
pause
exit /b 1

:check_python
"%CODEX_GUI_PYTHON%" -c "import sys; assert sys.version_info >= (3, 10)" >nul 2>nul
if errorlevel 1 (
    echo 当前 Python 不可用或版本低于 3.10，请更新 Python 后重试。
    pause
    exit /b 1
)
"%CODEX_GUI_PYTHON%" "%~dp0codex_usage\environment.py" --launch
if errorlevel 1 (
    pause
    exit /b 1
)
exit /b 0
