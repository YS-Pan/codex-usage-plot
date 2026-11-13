# 安装指南

[返回首页](../README.md) · [日常使用](usage.md) · [常见问题](troubleshooting.md)

本文面向协助安装的 AI agent，也可以手动照做。0.2.0 是普通 Python 安装包：发行名 `codex-usage-plot`，导入包 `codex_usage`，命令 `codex-usage`。安装完成后可以从任意目录使用，无需源码检出、basePack、AgentTools 或其他工具。

## 1. 选择 Python 和功能

需要 Python 3.10+。以下 `python` 均表示使用者选定的同一个解释器；有多个 Python 时使用其绝对路径。Windows 可先用 `py -0p` 查看安装位置；macOS / Linux 通常使用 `python3`。使用系统 Python 或自行管理的虚拟环境均可，工具不会创建 `.venv` 或在启动时修补依赖。

| 安装内容 | 能力与依赖 |
| --- | --- |
| 基础包 | 标准库入口、帮助、路径与环境检查 |
| `data` extra | 查询用量；pandas、NumPy，Windows 的 `tzdata`，Python 3.14 之前的 `backports.zstd` |
| `plotting` extra | 包含 `data`，另加 Matplotlib、Pillow，供图片、费用图、GUI 和演示使用 |

GUI 还需要该 Python 的 Tcl/Tk（`tkinter`）及桌面显示环境。Tcl/Tk 通过 Python 安装程序或系统包管理器提供，不能用 pip 安装。仅查询和 CLI 图片生成无需 Tkinter。

MP4 动画另需外部 FFmpeg，见下节。安装 Python 依赖不会自动下载编码器。

## 2. 安装 wheel

取得提供的 `codex_usage_plot-0.2.0-py3-none-any.whl`，按用途安装；把示例路径换成实际路径：

```sh
python -m pip install "/path/to/codex_usage_plot-0.2.0-py3-none-any.whl[data]"
```

需要图片或 GUI 时，将 `[data]` 换成 `[plotting]`。只需基础入口时省略方括号。Python 依赖由该 Python 的 pip 安装；源码仅用于开发，构建方法见 [贡献说明](../CONTRIBUTING.md#本地开发)。

已装基础包后，也可显式安装 extra：

```sh
codex-usage setup --install plotting
```

`--install data` 只安装查询依赖。安装由运行工具的同一个 Python 调用 pip，默认使用 `codex-usage-plot[extra]==当前版本`，复用已装同版本包的元数据，无需保留源码或初次安装的 wheel。需要显式覆盖来源时，可加 `--wheel "/path/to/codex_usage_plot-0.2.0-py3-none-any.whl"`。单独运行 `setup` 或 `check` 只检查，不安装。

动画使用 H.264 编码。自行安装 FFmpeg 并加入 PATH，或在启动命令的环境中指定完整可执行文件路径，例如 PowerShell：

```powershell
$env:CODEX_USAGE_FFMPEG_EXE = 'C:\Tools\ffmpeg\bin\ffmpeg.exe'
```

工具兼容 `IMAGEIO_FFMPEG_EXE`，也支持已经安装的可选 `imageio-ffmpeg`；该包不属于基础或绘图依赖。未发现可用编码器时会报告可操作的错误；工具不会弹出下载向导。只生成静态图片可使用 `report --no-animations`。

## 3. 检查并用合成数据验证

以下命令可以在与源码无关的可写目录运行，不读取真实日志，也不打开 GUI：

```sh
python -m codex_usage --help
codex-usage paths
codex-usage setup --feature data
```

`setup` 与 `check` 支持 `--feature data|plotting|animation`。未指定 `--feature` 时，普通检查默认 `animation`，包含数据、绘图和编码器；`setup --install` 默认检查所选 extra。Tk 仅在加 `--gui` 时检查。静态图片安装用 `check --feature plotting`；完整 GUI 安装用 `check --gui`。Tk 检查不会打开窗口，也不能证明当前会话有桌面显示环境。

完整图片和动画验收需要 `plotting` 与 FFmpeg：

```sh
codex-usage check --gui
codex-usage demo --output codex_usage_demo
```

纯 CLI 安装省略 `--gui`。演示使用固定合成日志，输出图片、图 2 的 MP4、CSV、`summary.json` 和标明合成数据的 `DEMO.txt`。选择一个新的演示目录；不覆盖现有正式结果。只查询的安装可检查 `query --help`，并使用合成日志验证查询，不需要绘图库、FFmpeg 或 Tk。

验收时确认安装版本为 0.2.0，`codex-usage` 和 `python -m codex_usage` 使用同一个安装，从无关工作目录也能运行；向用户交代解释器、功能检查和演示的实际结果。命令不在 PATH 时，用该 Python 的 `-m codex_usage`，或把其 Scripts / bin 目录加入 PATH。

## 4. 状态目录与旧版迁移

`codex-usage paths` 显示当前解析路径和保存路径选择的 `config_file`。默认状态位于平台的本工具用户目录，与源码、Python 安装目录和当前工作目录分离。需要持久选择位置时先运行：

```sh
codex-usage setup --state-dir "/path/to/codex-usage-state" --report-dir "/path/to/existing-report"
```

只配置路径时无需可选依赖。命令仅保存明确指定的根目录，不移动已有文件；未指定的项保持原值。选择保存在本工具用户配置目录的 `paths.json`，Windows 为 `%APPDATA%/codex-usage/paths.json`，无需设置环境变量或修改启动脚本。管理或隔离验证可用 `CODEX_USAGE_CONFIG_FILE` 指定配置文件。

| 内容 | 位置 / 覆盖方式 |
| --- | --- |
| 状态根目录 | 平台用户目录；`setup --state-dir` 可保存选择，`CODEX_USAGE_STATE_DIR` 可覆盖 |
| GUI 设置 | `<状态根>/codex_usage_settings.json` |
| 图片增量缓存 | `<状态根>/cache/codex_usage_cache.sqlite3` |
| 查询增量缓存 | `<状态根>/cache/codex_usage_query_cache.sqlite3`；默认查询仍不使用缓存 |
| 结果 | `<状态根>/reports/`；`setup --report-dir` 可保存选择，`CODEX_USAGE_REPORT_DIR` 可覆盖 |

CLI 显式的 `--output`、`--cache-file` 继续有效，优先于默认路径；环境变量优先于保存的根目录，保存的选择优先于平台默认值。使用环境变量时应设置在日常启动 GUI、快捷方式或调用方使用的同一环境；相对输出和缓存参数仍相对当前工作目录。

旧版把设置、缓存和 `codex_usage_report/` 放在源码目录。升级前先关闭 GUI，并保留旧文件；新版不会自动移动、覆盖或导入它们。确认需要沿用的内容后：

1. 运行 `paths` 确认目标。将旧 `codex_usage_settings.json` 复制到新设置位置；复制图片缓存到新图片缓存位置，查询缓存复制到独立查询缓存位置。存在同名文件时先保留双方副本，再决定使用哪份。
2. 要继续使用已有结果目录，用 `setup --report-dir` 保存其绝对路径，或设置 `CODEX_USAGE_REPORT_DIR`；也可以把需要保留的结果复制到新 `reports/`。CLI 可继续显式传旧 `--output` / `--cache-file`。
3. 再次运行 `paths`，确认设置、缓存和结果路径后交接。保留旧目录直到用户确认迁移结果；不因安装或升级删除旧设置、缓存、结果或日志。

也可用 `setup --state-dir` 保存使用者选定的持久目录，或用 `CODEX_USAGE_STATE_DIR` 覆盖。其缓存仍位于 `cache/` 子目录，因此仅指向旧源码根目录不能自动沿用根目录中的旧缓存。

## 5. 日常启动与更新

用户手动运行 `codex-usage gui` 或 `python -m codex_usage gui`，也可创建快捷方式。源码中的 Windows `启动用量统计.cmd` 是可选入口，不承担安装或修复依赖；无需保留它才能使用安装包。

更新时关闭 GUI，用同一个 Python 安装新版 wheel，并重复所用功能的检查和合成验证。状态与结果继续沿用已配置的位置。开发者修改源码后先测试、构建普通 wheel、重新安装，再从无关工作目录验证；直接编辑 `site-packages` 不属于更新流程。

完整安装验收无需读取真实日志；首次真实统计由用户按自己的目录与设置启动。安装依赖需要网络，日常读取与生成在本地完成。
