@echo off
setlocal
chcp 65001 >nul
set "PYTHONUTF8=1"

rem CODEX_USAGE_PYTHON selects the same interpreter used for the ordinary install.
if defined CODEX_USAGE_PYTHON goto launch
where py.exe >nul 2>nul
if not errorlevel 1 (
    for /f "delims=" %%P in ('py -3 -c "import sys; print(sys.executable)" 2^>nul') do set "CODEX_USAGE_PYTHON=%%P"
)
if defined CODEX_USAGE_PYTHON goto launch
for /f "delims=" %%P in ('where python.exe 2^>nul') do if not defined CODEX_USAGE_PYTHON set "CODEX_USAGE_PYTHON=%%P"
if defined CODEX_USAGE_PYTHON goto launch
echo 未找到 Python。请安装 Python 3.10 或更新版本，并包含 Tcl/Tk。
pause
exit /b 1

:launch
rem Run the installed package even when this launcher is beside a development checkout.
pushd "%TEMP%"
if errorlevel 1 exit /b 1
"%CODEX_USAGE_PYTHON%" -m codex_usage gui
set "CODEX_USAGE_EXIT=%ERRORLEVEL%"
popd
if not "%CODEX_USAGE_EXIT%"=="0" (
    echo 请确认此 Python 已安装 codex-usage-plot，并运行 python -m codex_usage setup --install plotting。
    pause
    exit /b 1
)
