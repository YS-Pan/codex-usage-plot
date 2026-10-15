# 安装指南

[返回首页](../README.md) · [日常使用](usage.md) · [常见问题](troubleshooting.md)

本文面向协助安装的 AI agent，也可以手动照做。目标是把源码和独立的 `.venv` 环境准备好，再由用户手动打开 GUI。所有命令均在**项目根目录**执行；无需激活虚拟环境。

## 1. 获取源码

如果用户已提供本地项目目录，直接进入该目录，确认包含 `AGENTS.md`、`requirements.txt` 和 `codex_usage/`。

如果尚未下载，在准备存放项目的目录执行：

```sh
git clone https://github.com/YS-Pan/codex-usage-plot.git
cd codex-usage-plot
```

如果 `codex-usage-plot` 已存在，检查并复用对应项目，或换一个空目录。没有 Git 时也可使用仓库页面的 **Code → Download ZIP** 并完整解压。不要直接在压缩包里运行。

## 2. 检查基础环境

需要 Python 3.10 或更新版本。GUI 需要 Python 附带的 Tcl/Tk（`tkinter`），它不能通过 pip 安装。Windows 安装 Python 时应包含 Tcl/Tk 和 Python Launcher，或把 Python 加入 PATH。

Windows PowerShell：

```powershell
py -3 --version
py -3 -c "import sys, tkinter; assert sys.version_info >= (3, 10); print('Python 和 Tkinter 可用')"
```

若没有 `py`，但 `python` 指向满足要求的 Python，则把上述命令及下节的 `py -3` 替换为 `python`。

macOS / Linux：

```sh
python3 --version
python3 -c "import sys; assert sys.version_info >= (3, 10); print('Python 可用')"
```

要使用 GUI，再检查 `python3 -c "import tkinter"`，并确认使用者有桌面显示环境。缺失时通过该 Python 的安装程序或系统包管理器补装；部分 Linux 系统还需要单独安装 `venv` / `ensurepip` 组件。仅使用命令行不要求 Tkinter。

## 3. 准备依赖并验证

`setup` 使用标准库启动，在本项目创建或检查 `.venv`，必要时安装 `requirements.txt` 中的依赖。可以重复执行；它不会读取 Codex 日志，也不会打开窗口。

### Windows / GUI

在项目根目录的 PowerShell 中执行，某一步失败时先处理错误：

```powershell
py -3 -m codex_usage setup
if ($LASTEXITCODE -ne 0) { throw '环境准备失败，请检查上方错误。' }

.\.venv\Scripts\python.exe -m codex_usage check
if ($LASTEXITCODE -ne 0) { throw '环境检查失败，请检查依赖和 Tkinter。' }

.\.venv\Scripts\python.exe -m codex_usage demo
if ($LASTEXITCODE -ne 0) { throw '演示图片生成失败，请检查上方错误。' }
```

`check` 检查依赖、时区支持、压缩日志支持与 Tkinter，不会创建窗口。不要在安装验收中使用 `setup --launch` 或 `gui`；用户随后手动启动即可。

### macOS / Linux

```sh
python3 -m codex_usage setup &&
.venv/bin/python -m codex_usage demo
```

上述步骤验证命令行所需依赖和图片生成。如果准备使用 GUI，再执行：

```sh
.venv/bin/python -m codex_usage check
```

`check` 也会检查 Tkinter；仅使用 CLI 的机器无需运行它。导入 Tkinter 成功不等于当前会话有桌面显示环境，远程终端或无桌面的服务器请使用命令行。

### 验收结果

- 安装命令成功，依赖位于项目 `.venv`，没有安装到系统 Python。
- GUI 用户的 `check` 成功；命令行用户的 `setup` 与 `demo` 成功。
- `codex_usage_demo/` 中生成 12 张 PNG、CSV、`summary.json` 和标明合成数据的 `DEMO.txt`。
- 向用户交代项目的绝对路径、启动方式和验证结果。

演示使用固定的合成日志，不读取用户的真实日志，不改动正式输出或统计缓存。首次安装和下载依赖需要网络；此后的图片生成在本地完成。合成演示仅用于验证，与 README 中作者的真实用量预览是两组不同的数据。

## 4. 交给用户日常使用

Windows：在文件管理器中双击项目根目录的 **`启动用量统计.cmd`**。用户也可以自行创建它的桌面快捷方式。启动器会复用项目的 `.venv`。

macOS / Linux 的桌面环境：

```sh
.venv/bin/python -m codex_usage gui
```

用户打开 GUI 后点击 **「更新图片」**，首次完整读取、以后增量读取；完成后点击 **「打开图片文件夹」**。数据目录、时区、归档选项以及故障排查用的完整读取位于 **「设置」** 中。

需要纯命令行运行时，见 [使用指南](usage.md#命令行)。完整验证无需读取真实日志；首次真实统计由用户按自己的目录与设置启动。

## 更新项目或修复环境

先关闭 GUI，再获取新版源码。如果使用 Git，先检查本地修改并保留它们；如果使用 ZIP，替换应用源码时保留自己的设置与输出文件。不要从另一台电脑复制 `.venv`。

更新后重新执行本页的 `setup`、环境检查和合成演示即可。普通更新继续使用 **「更新图片」**；若怀疑缓存或历史统计异常，再到设置中执行 **「重新完整读取」**。环境损坏时的恢复方法见 [常见问题](troubleshooting.md)。
