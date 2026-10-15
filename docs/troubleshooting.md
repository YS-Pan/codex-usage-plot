# 常见问题

[返回首页](../README.md) · [安装指南](installation.md) · [使用指南](usage.md)

## 双击后没有窗口，或提示找不到 Python

先完整解压仓库，再运行 `启动用量统计.cmd`。安装 Python 3.10 或更新版本时，选择 Tcl/Tk 并勾选 Add python.exe to PATH。启动器依次寻找项目的 `.venv`、`py -3` 和 PATH 中的 Python。

让 AI agent 协助安装时，优先按 [安装指南](installation.md) 准备环境并验证，不需要自动打开窗口。也可在项目目录的 PowerShell 中运行，保留完整错误信息：

```powershell
py -3 -m codex_usage setup
```

只检查当前环境而不安装或启动：

```powershell
.\.venv\Scripts\python.exe -m codex_usage check
```

检查通过后再次双击启动器。`check` 包含 Tkinter 检查；仅使用 CLI 的 macOS / Linux 用户可以用 `setup` 和合成 `demo` 验证，无需安装 Tkinter。

若 `.venv` 来自另一台机器、已损坏或 Python 卸载后不可用，关闭 GUI，将 `.venv` 重命名为一个未占用的备份目录（如 `.venv-old`），再运行 `py -3 -m codex_usage setup`。虚拟环境不能直接复制到其他机器使用；重新创建不会修改本地 Codex 原始日志。

## 安装依赖失败，或提示 ModuleNotFoundError

首次安装需要可访问 Python 包索引的网络。查看启动窗口中的具体报错，恢复网络后重新双击即可；启动器只在项目的 `.venv` 安装依赖。

手动修复请使用**实际运行工具的同一个 Python**，例如：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

macOS / Linux 使用 `.venv/bin/python -m pip install -r requirements.txt`。不要将依赖安装到一个 Python，却用另一个 Python 启动。

如果提示 `tkinter`，请通过 Python 安装程序或系统包管理器补装 Tcl/Tk；它不是用 pip 安装的包。Linux 的 `venv` / `ensurepip` 也可能需要系统单独提供。仅使用 CLI 时不需要 Tkinter。

## 提示没有日志，或者数据目录不存在

GUI 中展开「设置」，点击数据目录旁的「浏览…」，选择**包含 `sessions` 或 `archived_sessions` 的上一级目录**。默认目录是 `CODEX_HOME`，未设置时为 `~/.codex`；Windows 通常为 `%USERPROFILE%\.codex`。

确认当前设备运行过会保存本地日志的 Codex 会话，且目录中有 `.jsonl` 或 `.jsonl.zst` 文件。工具无法从账户补回其他机器、云端或已经删除的日志。只有归档记录时，请勾选「包含归档」。CLI 可用 `--codex-home` 明确指定目录。

尚无日志时，可以先运行 [合成演示](usage.md#合成演示与安装验证) 体验完整的图片生成流程。

## 历史记录突然只剩最近几天

检查 `sessions` 和 `archived_sessions` 是否有较早的 `.jsonl.zst` 文件。旧版只搜索普通 JSONL，会漏掉压缩后的历史记录。更新工具和依赖后，展开「设置」并点击「重新完整读取」，无需手工解压。

Python 3.14+ 使用标准库 `compression.zstd`；较早版本通过 requirements.txt 安装 `backports.zstd`。如果原始历史日志已被清理，需自行从备份恢复；缓存只反映本机仍保留的日志。

## 结果目录中没有图表，或更新看起来很慢

首次使用需先点击「更新图片」，完成后再点击「打开图片文件夹」。更新期间，设置和更新按钮保持禁用。只记录运行时间、没有 token 的数据可能没有部分用量图。

日志较多时首次完整读取需要时间；增量模式可减少读取，但仍需重新绘图。底部「运行日志」可查看当前阶段，更新期间显示的「停止」按钮可取消。取消或失败可能留下部分更新的结果，重试即可。

## 更新失败，或费用估算失败

在 GUI 点击「运行日志」，查看 `codex_usage_report/gui_run.log`。每次更新都会覆盖该文件。确认项目目录可写、磁盘有空间；若其他软件占用了输出文件，请关闭文件后重试。

仅费用步骤失败时，用量图表可能已经完成。命令行费用计算需要先生成 `events.csv`，自定义路径示例：

```sh
python -m codex_usage cost --events report/events.csv --output report
```

提交问题时只分享必要的错误片段，并隐去用户名、路径、任务标题、会话 ID 等个人信息。

## 为什么与账户用量或账单不一致

本工具统计本机保留的日志，不代表服务端账户总量或剩余额度。其他设备、云端任务和已删除日志可能不包含在内。

费用根据项目中记录的 API 单价估算，不代表订阅费用或实际账单；缓存、长上下文、未定价模型和工具费用的处理见 [费用说明](api-cost.md)。
