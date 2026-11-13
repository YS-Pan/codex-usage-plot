# 参与贡献

欢迎提交问题反馈、使用体验建议和代码改进。开始前请阅读 [README](README.md)；维护代码时从 [开发文档入口](dev_doc/README.md) 按任务查阅，项目协作要求见 [AGENTS.md](AGENTS.md)。

## 本地开发

使用 Python 3.10 或更新版本。选择用于开发的解释器，也可自行创建虚拟环境；以下 `python` 始终指向同一个解释器。在仓库根目录执行：

```sh
python -m pip install ".[test]" build
python -m unittest -v
python -m build --wheel
python -m pip install --force-reinstall --no-deps dist/codex_usage_plot-0.2.0-py3-none-any.whl
```

依赖和 extras 以 `pyproject.toml` 为准。`test` extra 包含绘图依赖及供合成 MP4 测试使用的 `imageio-ffmpeg`；生产的 `data` / `plotting` extras 不捆绑编码器。

随后切换到与源码无关的可写目录，确认 wheel 安装独立可用：

```sh
python -m codex_usage --help
codex-usage paths
codex-usage check
codex-usage demo --output codex_usage_demo
```

使用一个新的演示目录；不要把演示写进 README 的已跟踪图片目录。源码用于开发，普通 wheel 用于安装；每次修改后按测试、构建、重新安装、无关目录验证的顺序更新，不直接编辑 `site-packages`。源码测试可能掩盖安装文件遗漏，因此不能替代 wheel 验收。

所有测试使用合成数据，不需要登录 Codex、API key 或个人日志。GitHub Actions 在 Windows / Ubuntu、Python 3.10 / 3.14 上运行测试与演示。GUI 需要带 Tcl/Tk 的桌面环境；CI 不验证窗口交互。

## 改动范围与验证

- 尽量让每个 PR 聚焦一个问题，描述改动前后的行为。
- 统计和解析改动要覆盖相关边界，例如空闲日期、零输入、时区、重复日志、加权比例。
- 图表沿用英文标注；README、错误提示和 GUI 使用中文。
- 图表改动后运行 `python -m codex_usage demo` 并检查图片；无数据分支也需检查。测试与演示继续使用合成数据。
- 纯文档修改无需重新读取日志或生成真实图片。
- 沿用已有图表编号、原子替换写图逻辑、CSV UTF-8 BOM 和 JSON UTF-8 编码。

## 提交内容

README 直接引用 `codex_usage_report/` 中的 `02_cumulative_tokens.png` 和 `10_daily_running_time.png`。GitHub 保留现有图片作为历史预览；本地仍通过正常生成流程更新，不另建预览图片或复制到其他目录。

**以后不再提交或上传这两张图片的更新**，包括普通 Git push 和 GitHub 插件提交，也不再反复询问上传授权。原因是减少自动审批带来的操作负担；作者明确表示原因并非图片内容敏感。

这两张图片已经被 Git 跟踪，`.gitignore` 无法屏蔽它们的修改。暂存代码和文档时显式排除图片，例如下面的命令适用于 PowerShell 和常见 shell：

```sh
git add -A -- . ':(exclude)codex_usage_report/02_cumulative_tokens.png' ':(exclude)codex_usage_report/10_daily_running_time.png'
git diff --cached --name-only
```

提交前确认暂存区不含这两个路径；通过 GitHub 插件提交时同样从文件或 tree 清单中排除。图片的本地修改可以继续保留在工作区。

`codex_usage_report/02_cumulative_tokens.mp4` 和 `codex_usage_report/02_cumulative_tokens.gif` 作为 README 的动画示例一同跟踪。两张 PNG 的后续修改继续按上述约定排除。`.gitignore` 排除其余默认输出、演示输出、本地设置、虚拟环境、JSONL 日志和 SQLite 缓存。自定义输出目录仍需自行检查。提交前运行 `git status --short` 和 `git diff --check`，确认文件清单符合上述约定。

输出文件虽然不包含消息正文，仍可能含有任务标题、工作目录、会话 ID 和活动时间。反馈问题时只附必要且已脱敏的错误片段，优先提供合成日志复现；不要直接上传真实 `events.csv`、缓存数据库或 rollout 日志。

## 开发参考

模块职责和调用关系见 [架构与数据流](dev_doc/architecture.md)，计算定义见 [统计与数据约定](dev_doc/statistics.md)，按改动选择测试和同步文档的方法见 [修改与验证](dev_doc/maintenance.md)。费用单价、来源和核实日期应一起维护，不要在无关改动中调整价格或统计口径。
