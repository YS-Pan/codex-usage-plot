# 安装指南

[返回首页](../README.md) · [日常使用](usage.md) · [常见问题](troubleshooting.md)

本文面向协助安装的 AI agent，也可以手动照做。0.2.1 是普通 Python 安装包：发行名 `codex-usage-plot`，导入包 `codex_usage`，命令 `codex-usage`。安装完成后可以从任意目录使用，无需检出源码。

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

取得提供的 `codex_usage_plot-0.2.1-py3-none-any.whl`，按用途安装；把示例路径换成实际路径：

```sh
python -m pip install "/path/to/codex_usage_plot-0.2.1-py3-none-any.whl[data]"
```

需要图片或 GUI 时，将 `[data]` 换成 `[plotting]`。只需基础入口时省略方括号。Python 依赖由该 Python 的 pip 安装；源码仅用于开发，构建方法见 [贡献说明](../CONTRIBUTING.md#本地开发)。

已装基础包后，也可显式安装 extra：

```sh
codex-usage setup --install plotting
```

`--install data` 只安装查询依赖。安装由运行工具的同一个 Python 调用 pip，默认使用 `codex-usage-plot[extra]==当前版本`，复用已装同版本包的元数据，无需保留源码或初次安装的 wheel。需要显式覆盖来源时，可加 `--wheel "/path/to/codex_usage_plot-0.2.1-py3-none-any.whl"`。单独运行 `setup` 或 `check` 只检查，不安装。

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

验收时确认安装版本为 0.2.1，`codex-usage` 和 `python -m codex_usage` 使用同一个安装，从无关工作目录也能运行；向用户交代解释器、功能检查和演示的实际结果。命令不在 PATH 时，用该 Python 的 `-m codex_usage`，或把其 Scripts / bin 目录加入 PATH。

## 4. 状态目录与旧版迁移

`codex-usage paths` 显示当前解析路径和保存路径选择的 `config_file`，不会创建目录或缓存。直接运行 CLI 时，报告默认写到调用方当前目录的 `codex_usage_report/`；设置保存在平台用户状态目录，统计缓存保存在平台用户缓存目录。`--help` 同样只读。源码和 Python 安装目录不用于存放默认运行数据。

已有明确的持久目录选择时可运行：

```sh
codex-usage setup --state-dir "/path/to/codex-usage-state" --report-dir "/path/to/existing-report"
```

只配置路径时无需可选依赖。命令仅保存明确指定的根目录，不移动已有文件；未指定的项保持原值。选择保存在本工具用户配置目录的 `paths.json`，Windows 为 `%APPDATA%/codex-usage/paths.json`，无需设置环境变量或修改启动脚本。管理或隔离验证可用 `CODEX_USAGE_CONFIG_FILE` 指定配置文件。

| 内容 | 位置 / 覆盖方式 |
| --- | --- |
| 状态根目录 | Windows：`%LOCALAPPDATA%/codex-usage/`；macOS：`~/Library/Application Support/codex-usage/`；Linux：`$XDG_STATE_HOME/codex-usage/`，未设置时为 `~/.local/state/codex-usage/` |
| GUI 设置 | `<状态根>/codex_usage_settings.json` |
| 默认缓存目录 | Windows：`%LOCALAPPDATA%/codex-usage/cache/`；macOS：`~/Library/Caches/codex-usage/`；Linux：`$XDG_CACHE_HOME/codex-usage/`，未设置时为 `~/.cache/codex-usage/` |
| 图片增量缓存 | 有效缓存目录中的 `codex_usage_cache.sqlite3` |
| 查询增量缓存 | 有效缓存目录中的 `codex_usage_query_cache.sqlite3`；仅显式增量查询使用，默认查询不建缓存 |
| 结果 | 调用方当前目录中的 `codex_usage_report/` |
| 演示 | 有效状态根下的 `demo/`；`demo --output` 可指定其他目录 |

报告位置按以下顺序解析：显式 `--output` → `CODEX_USAGE_REPORT_DIR` → `setup --report-dir` 保存的选择 → 当前目录的 `codex_usage_report/`。`report`、`cost`、`animate` 和直接启动的 GUI 使用同一有效报告目录；`cost` / `animate` 读取该处已有数据。

缓存以显式 `--cache-file` 为最高优先级；设置了 `CODEX_USAGE_STATE_DIR` 或保存了 `state_dir` 时，使用该状态根的 `cache/`，否则使用表中的平台用户缓存目录。状态根本身也是环境变量优先于保存的选择。相对输出和缓存参数以调用方当前目录为准。

旧版把设置、缓存和 `codex_usage_report/` 放在源码目录。升级前先关闭 GUI，并保留旧文件；新版不会自动移动、覆盖或导入它们。确认需要沿用的内容后：

1. 运行 `paths` 并记录旧配置、设置和缓存的实际位置。关闭会写入本工具缓存的 GUI / CLI 后，复制需沿用的设置及缓存，或用 SQLite 的一致性备份保留缓存。存在同名文件时先核对来源与有效内容，保留双方，避免覆盖较新的数据。有效缓存应随路径选择一起接入，避免改目录后重新扫描全历史。
2. 要继续使用已有结果目录，用 `setup --report-dir` 保存其绝对路径，或设置 `CODEX_USAGE_REPORT_DIR`；CLI 也可显式传旧 `--output` / `--cache-file`。使用可复制启动器时，把需沿用的数据放进其运行根对应位置，见下节。
3. 若旧 `paths.json` 中的 `state_dir` / `report_dir` 是前次迁移写入的固定目录，而现在选择恢复 CLI 默认行为，先备份原值，再仅移除这两项；已有用户明确选择则保留。只修改本工具配置，不删除原运行目录。再次运行 `paths` 核对实际结果，并验证增量读取可以复用旧缓存。

也可用 `setup --state-dir` 保存使用者选定的持久目录，或用 `CODEX_USAGE_STATE_DIR` 覆盖。其缓存仍位于 `cache/` 子目录，因此仅指向旧源码根目录不能自动沿用根目录中的旧缓存。

## 5. 日常启动与更新

用户手动运行 `codex-usage gui` 或 `python -m codex_usage gui`，也可创建快捷方式。Windows 还可将项目根的 `启动用量统计.cmd` 复制到自己选择的运行目录 R：

```text
R/
  启动用量统计.cmd
  codex_usage_settings.json
  cache/
    codex_usage_cache.sqlite3
    codex_usage_query_cache.sqlite3  # 仅显式共用此根的增量查询才会创建
  reports/
```

脚本仅在自身 `setlocal` 环境中设置 `CODEX_USAGE_STATE_DIR=R` 和 `CODEX_USAGE_REPORT_DIR=R/reports`，覆盖调用方同名变量及保存的目录选择，不修改全局 `paths.json`。GUI 和统计子进程继承它们，脚本退出后调用方环境恢复。独立 `query` 不继承已经结束的脚本环境，需要显式指定相同状态根或缓存路径才能共用 R 中的查询缓存。

移动整个 R 会带走设置、缓存和结果；只复制 cmd 到空目录会建立独立的运行数据，不自动寻找或搬运原文件。两个目录中的副本使用同一个已安装包，各自保存数据。根目录脚本是唯一版本维护源，更新时从该源同步运行副本；双击项目根脚本也会在项目根生成上述数据，日常可使用源码外的副本。

启动器通过 `CODEX_USAGE_PYTHON` 选择已安装本工具的 console Python，未设置时查找系统 Python。它先从无关工作目录用同一解释器运行 `gui --help` 预检，再以同目录的 `pythonw.exe` 无等待启动 GUI，启动 cmd 随后退出。缺解释器、`pythonw`、包或 Tk 时显示错误与操作指引，不自动安装或切换环境。`start` 返回只代表已派出进程；安装验收仍需实际确认 GUI 窗口存活。启动器是可选入口，无需保留它才能使用安装包。

更新时关闭 GUI，用同一个 Python 安装新版 wheel，并重复所用功能的检查和合成验证。状态与结果继续沿用已配置的位置。开发者修改源码后先测试、构建普通 wheel、重新安装，再从无关工作目录验证；直接编辑 `site-packages` 不属于更新流程。

完整安装验收无需读取真实日志；首次真实统计由用户按自己的目录与设置启动。安装依赖需要网络，日常读取与生成在本地完成。
