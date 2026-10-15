# 参与贡献

欢迎提交问题反馈、使用体验建议和代码改进。开始前请阅读 [README](README.md)；维护代码时从 [开发文档入口](dev_doc/README.md) 按任务查阅，项目协作要求见 [AGENTS.md](AGENTS.md)。

## 本地开发

使用 Python 3.10 或更新版本。在仓库根目录创建隔离环境：

```sh
python -m venv .venv
```

Windows PowerShell：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m unittest -v
.\.venv\Scripts\python.exe -m codex_usage demo
```

macOS / Linux：

```sh
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m unittest -v
.venv/bin/python -m codex_usage demo
```

所有测试使用合成数据，不需要登录 Codex、API key 或个人日志。GitHub Actions 在 Windows / Ubuntu、Python 3.10 / 3.14 上运行测试与演示。GUI 需要带 Tcl/Tk 的桌面环境；CI 不验证窗口交互。

## 改动范围与验证

- 尽量让每个 PR 聚焦一个问题，描述改动前后的行为。
- 统计和解析改动要覆盖相关边界，例如空闲日期、零输入、时区、重复日志、加权比例。
- 图表沿用英文标注；README、错误提示和 GUI 使用中文。
- 图表改动后运行 `python -m codex_usage demo` 并检查图片；无数据分支也需检查。测试与演示继续使用合成数据。
- 纯文档修改无需重新读取日志或生成真实图片。
- 沿用已有图表编号、原子替换写图逻辑、CSV UTF-8 BOM 和 JSON UTF-8 编码。

## 提交内容

README 直接引用 `codex_usage_report/` 中的 `02_cumulative_tokens.png` 和 `10_daily_running_time.png`。这两张作者确认公开的图片允许提交，不另建预览图片或复制到其他目录。刷新时使用正常图片生成流程。

`.gitignore` 排除其余默认输出、演示输出、本地设置、虚拟环境、JSONL 日志和 SQLite 缓存。自定义输出目录仍需自行检查。提交前运行 `git status --short` 和 `git diff --check`，确认没有误加入其他个人数据。

输出文件虽然不包含消息正文，仍可能含有任务标题、工作目录、会话 ID 和活动时间。反馈问题时只附必要且已脱敏的错误片段，优先提供合成日志复现；不要直接上传真实 `events.csv`、缓存数据库或 rollout 日志。

## 开发参考

模块职责和调用关系见 [架构与数据流](dev_doc/architecture.md)，计算定义见 [统计与数据约定](dev_doc/statistics.md)，按改动选择测试和同步文档的方法见 [修改与验证](dev_doc/maintenance.md)。费用单价、来源和核实日期应一起维护，不要在无关改动中调整价格或统计口径。
