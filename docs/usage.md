# 使用指南

[返回首页](../README.md) · [安装指南](installation.md) · [输出文件参考](reference.md) · [统计与数据约定](../dev_doc/statistics.md) · [常见问题](troubleshooting.md)

## 图形界面

安装通常只需进行一次，可以交给 AI agent 按 [安装指南](installation.md) 完成。日常双击 Windows 的 `启动用量统计.cmd`，或在装好依赖的环境运行 `python -m codex_usage gui`。

1. 点击 **「更新图片」**：首次自动完整读取以建立缓存，之后复用未变化的日志。图片每次都会重新生成。
2. 更新完成后点击 **「打开图片文件夹」**，在其中选择需要的图片、CSV 或汇总文件。

通常无需调整选项。要更换数据来源或排查问题，展开 **「设置」**：

| 设置 | 用途 |
| --- | --- |
| 数据目录 | 默认使用 `CODEX_HOME`，未设置时使用用户目录下的 `.codex`。选择包含 `sessions` / `archived_sessions` 的上一级目录，不是某个 JSONL 文件或 `sessions` 本身。 |
| 统计时区 | 默认 `Asia/Shanghai`。可选择或填写 IANA 名称，例如 `UTC`、`Europe/London`。 |
| 包含归档 | 默认勾选，同时统计 `archived_sessions` 中仍保留的日志。 |
| 重新完整读取 | 重新核对所有历史文件，同时刷新缓存；怀疑缓存或统计异常时使用。日常点击主界面的「更新图片」即可。 |

设置在开始更新时保存到工具目录下的 `codex_usage_settings.json`，下次自动恢复。删除该文件可恢复默认设置。它包含本机路径，默认不会被 Git 提交。

结果始终位于工具目录的 `codex_usage_report/`，从快捷方式或其他工作目录启动也一样。更新期间设置和更新按钮禁用；耗时工作在后台子进程执行，窗口可响应。

更新期间显示进度条和「停止」按钮，点击「停止」可取消，关闭更新中的窗口也会询问是否停止。取消或失败可能留下部分已更新文件，重新生成即可。底部「运行日志」打开 `codex_usage_report/gui_run.log`，每次更新覆盖；「使用说明」打开本指南。只记录运行时间而没有 token 时，部分用量图不会生成。

## 命令行

在项目根目录通过 `python -m codex_usage` 运行。旧版根目录脚本已归入 `codex_usage/` 包；原用量、费用和 GUI 入口分别改为 `report`、`cost`、`gui` 子命令，原有统计参数继续跟在子命令后。

以下示例使用已安装依赖的 `python`。若未激活环境，Windows 将其替换为 `.\.venv\Scripts\python.exe`，macOS / Linux 替换为 `.venv/bin/python`。

```sh
python -m codex_usage report --timezone Asia/Shanghai --incremental
python -m codex_usage cost
```

CLI 的数据目录默认读取 `CODEX_HOME` 或 `~/.codex`；输出目录默认是**当前工作目录**下的 `codex_usage_report`。CLI 的时区默认 `local`，表示系统当前 UTC 偏移；统计跨夏令时日期时请明确指定 IANA 时区。

指定目录与日期（首尾日期均包含）：

```sh
python -m codex_usage report --codex-home "/path/to/.codex" --start 2026-09-01 --end 2026-09-30 --timezone Asia/Shanghai --output report
python -m codex_usage cost --events report/events.csv --output report
```

将 `/path/to/.codex` 替换为自己的目录。Windows 路径也可以写成 `"D:/CodexData"`；有空格时保留引号。

| 参数 | 用途 / 默认值 |
| --- | --- |
| `--codex-home PATH` | Codex 数据目录 |
| `--output PATH` | 图表、CSV 与汇总的输出目录 |
| `--timezone NAME` | IANA 时区，CLI 默认 `local` |
| `--start / --end` | `YYYY-MM-DD` 日期范围，默认全部 |
| `--no-include-archived` | 不读取归档，默认包含 |
| `--format png\|svg\|pdf` | 用量图格式，默认 PNG；费用图始终为 PNG |
| `--dpi NUMBER` | 图片 DPI，默认 160 |
| `--top-threads NUMBER` | 任务排行数量，默认 15 |
| `--incremental` | 增量扫描，缓存缺失时自动完整读取 |
| `--cache-file PATH` | 缓存位置，默认工具目录中的 `codex_usage_cache.sqlite3` |
| `--quiet` | 隐藏扫描进度 |

查看完整参数：`python -m codex_usage report --help`、`python -m codex_usage cost --help`。

## 增量缓存

默认缓存为工具目录下的 `codex_usage_cache.sqlite3`。首次自动建立，以后复用未变化的日志；普通 JSONL 追加时继续读取，压缩日志变化时重新读取该文件。图表仍会使用全部选中数据重新绘制。

怀疑统计异常时，展开「设置」并点击「重新完整读取」核对历史。缓存损坏或格式过期会保留旧文件并重新建立。切换数据目录会重建缓存；长期统计多个目录时，可通过 CLI 的 `--cache-file` 为它们分别指定缓存。

缓存只保存统计元数据与读取进度，不保存消息正文。实现、恢复行为和边界条件见 [架构与数据流](../dev_doc/architecture.md#增量缓存与恢复)。

## 合成演示与安装验证

没有本地日志也能验证图片生成。依赖准备好后，在项目根目录运行：

```sh
python -m codex_usage demo
```

未激活环境时，Windows 使用 `.\.venv\Scripts\python.exe -m codex_usage demo`，macOS / Linux 使用 `.venv/bin/python -m codex_usage demo`。

演示会在 `codex_usage_demo/` 生成全部 12 张图和数据表，使用固定的合成会话、时间与 token 计数。它不读取真实 Codex 目录，也不改动正式输出或统计缓存。重复运行可刷新演示；自定义位置使用 `--output` 指定空目录。

README 的公开预览来自作者的真实聚合统计，`demo` 的输出则专门用于体验、安装验证和开发测试。
