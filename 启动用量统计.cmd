@echo off
setlocal
chcp 65001 >nul
set "PYTHONUTF8=1"
set "CODEX_USAGE_STATE_DIR=%~dp0"
set "CODEX_USAGE_REPORT_DIR=%~dp0reports"

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
if errorlevel 1 (
    echo 无法进入临时目录，请检查 TEMP 路径及访问权限。
    pause
    exit /b 1
)
"%CODEX_USAGE_PYTHON%" -m codex_usage gui --help
if errorlevel 1 goto launch_failed
rem Ask this interpreter for its location, including when selected by command name.
set "CODEX_USAGE_CONSOLE="
for /f "delims=" %%P in ('^""%CODEX_USAGE_PYTHON%" -c "import sys; print(sys.executable)"^"') do set "CODEX_USAGE_CONSOLE=%%P"
if not defined CODEX_USAGE_CONSOLE goto launch_failed
for %%P in ("%CODEX_USAGE_CONSOLE%") do set "CODEX_USAGE_PYTHONW=%%~dpPpythonw.exe"
if not exist "%CODEX_USAGE_PYTHONW%" (
    echo 未找到同一 Python 的无控制台解释器："%CODEX_USAGE_PYTHONW%"
    goto launch_failed
)
start "" "%CODEX_USAGE_PYTHONW%" -m codex_usage gui
if errorlevel 1 goto launch_failed
popd
exit /b 0

:launch_failed
popd
echo 启动未完成。请确认所选 Python 已安装 codex-usage-plot，并包含 Tcl/Tk 和 pythonw.exe。
echo 可用同一 Python 检查环境："%CODEX_USAGE_PYTHON%" -m codex_usage check --gui
echo 如需安装绘图依赖，请显式运行："%CODEX_USAGE_PYTHON%" -m codex_usage setup --install plotting
pause
exit /b 1
