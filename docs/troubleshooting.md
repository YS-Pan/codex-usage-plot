# 常见问题

[返回首页](../README.md) · [安装指南](installation.md) · [使用指南](usage.md)

## 双击后没有窗口，或提示找不到 Python

先确认已经按 [安装指南](installation.md) 安装 wheel 的 `plotting` extra。GUI 需要 Python 3.10+、Tcl/Tk 和桌面环境；Windows 的 Python 安装程序可提供 Tcl/Tk。可为已安装命令 `codex-usage gui` 创建快捷方式，或使用复制到运行文件夹的 `启动用量统计.cmd`。脚本需要所选 Python 同目录的 `pythonw.exe`；可用 `CODEX_USAGE_PYTHON` 指定已安装本工具的 `python.exe`。报错时保留原错，用同一解释器检查和修复；启动器不自动安装或切换环境。

在终端使用安装工具的同一个 Python 运行并保留错误信息：

```powershell
python -m codex_usage check --gui
```

`setup` / `check` 只检查；`--gui` 才额外检查 Tkinter。仅查询使用 `check --feature data`，静态图片使用 `check --feature plotting`，均无需 Tk。命令不在 PATH 时使用该 Python 的绝对路径加 `-m codex_usage`；不要改用另一个未安装工具的 Python。

## 安装依赖失败，或提示 ModuleNotFoundError

安装 Python 依赖需要可访问包索引的网络。工具启动时不会自动创建环境、安装依赖或修复 pip；失败时查看具体报错，并用实际运行工具的同一个 Python 显式安装所需 extra。

例如，图片或 GUI 安装：

```powershell
python -m codex_usage setup --install plotting
```

仅查询把 `plotting` 换成 `data`。默认复用已装同版本包的依赖元数据，不需要初次安装的 wheel；需要覆盖来源时可按 [安装指南](installation.md#2-安装-wheel) 显式指定本地 wheel。若 Python 环境本身损坏，由使用者修复或重新选择解释器并安装 wheel；原有状态目录与结果可保留。

如果提示 `tkinter`，请通过 Python 安装程序或系统包管理器补装 Tcl/Tk；它不是用 pip 安装的包。

## 找不到 FFmpeg，或无法生成动画

`plotting` extra 提供图片依赖，动画编码器由外部提供。安装支持 H.264 的 FFmpeg 并加入 PATH，或设置 `CODEX_USAGE_FFMPEG_EXE` 为完整可执行文件路径；兼容 `IMAGEIO_FFMPEG_EXE` 和已安装的可选 `imageio-ffmpeg`。运行 `codex-usage check --feature animation` 查看错误，工具不会自动下载编码器。

只需静态图片时运行 `codex-usage report --no-animations`。GUI 默认生成图 2 的 MP4，因此完整 GUI 更新仍需 FFmpeg。

## 升级后看不到旧设置或结果

直接运行命令时，`codex-usage paths` 可查看当前状态、设置、缓存和结果路径：报告默认随当前目录，缓存使用平台用户缓存，保存的目录选择和环境变量可覆盖默认值。cmd 运行时使用脚本所在文件夹中的设置、`cache/` 和 `reports/`，不会修改全局路径偏好；独立运行 `paths` 不继承脚本结束后的局部环境。只复制 cmd 不会自动带走原数据，旧版源码目录中的文件也不会自动导入、移动或删除。按 [迁移步骤](installation.md#4-状态目录与旧版迁移) 核对并复制需沿用的设置、有效缓存，或显式配置已有结果目录，避免改路径后从头重扫。先保留双方文件，不要用重新生成替代需要保留的旧结果。

## 提示没有日志，或者数据目录不存在

GUI 中展开「设置」，点击数据目录旁的「浏览…」，选择**包含 `sessions` 或 `archived_sessions` 的上一级目录**。默认目录是 `CODEX_HOME`，未设置时为 `~/.codex`；Windows 通常为 `%USERPROFILE%\.codex`。

确认当前设备运行过会保存本地日志的 Codex 会话，且目录中有 `.jsonl` 或 `.jsonl.zst` 文件。工具无法从账户补回其他机器、云端或已经删除的日志。只有归档记录时，请勾选「包含归档」。CLI 可用 `--codex-home` 明确指定目录。

尚无日志时，可以先运行 [合成演示](usage.md#合成演示与安装验证) 体验完整的图片生成流程。

## 历史记录突然只剩最近几天

检查 `sessions` 和 `archived_sessions` 是否有较早的 `.jsonl.zst` 文件。旧版只搜索普通 JSONL，会漏掉压缩后的历史记录。更新工具和依赖后，展开「设置」并点击「重新完整读取」，无需手工解压。

Python 3.14+ 使用标准库 `compression.zstd`；较早版本由 `data` / `plotting` extra 安装 `backports.zstd`。如果原始历史日志已被清理，需自行从备份恢复；缓存只反映本机仍保留的日志。

## 结果目录中没有图表，或更新看起来很慢

首次使用需先点击「更新图片」，完成后再点击「打开图片文件夹」。更新期间，设置和更新按钮保持禁用。只记录运行时间、没有 token 的数据可能没有部分用量图。

日志较多时首次完整读取需要时间；增量模式可减少读取，但仍需重新绘图。底部「运行日志」可查看当前阶段，更新期间显示的「停止」按钮可取消。取消或失败可能留下部分更新的结果，重试即可。

## 更新失败，或费用估算失败

在 GUI 点击「运行日志」，查看报告目录的 `gui_run.log`，实际报告目录见 `codex-usage paths`。每次更新都会覆盖该文件。确认状态和报告目录可写、磁盘有空间；若其他软件占用了输出文件，请关闭文件后重试。

仅费用步骤失败时，用量图表可能已经完成。命令行费用计算需要先生成 `events.csv`，自定义路径示例：

```sh
python -m codex_usage cost --events report/events.csv --output report
```

提交问题时只分享必要的错误片段，并隐去用户名、路径、任务标题、会话 ID 等个人信息。

## 为什么与账户用量或账单不一致

本工具统计本机保留的日志，不代表服务端账户总量或剩余额度。其他设备、云端任务和已删除日志可能不包含在内。

费用根据项目中记录的 API 单价估算，不代表订阅费用或实际账单；缓存、长上下文、未定价模型和工具费用的处理见 [费用说明](api-cost.md)。
