# 项目协作说明

## 项目与文件

这是一个可独立安装的本地 Codex 用量统计工具，使用 Python 3.10+。发行名 `codex-usage-plot`，包名 `codex_usage`，命令 `codex-usage`；基础入口仅用标准库，`data` / `plotting` extras 的依赖由 `pyproject.toml` 维护。面向用户的说明及 GUI 文案使用中文，图表沿用已有英文标注风格。

README 首先面向决定是否安装的用户，优先展示真实用量图片；安装和环境准备面向 AI agent，步骤集中在 `docs/installation.md`；日常使用面向手动打开 GUI 的用户，主操作为增量「更新图片」。面向用户统一使用「图片」称呼，CSV / JSON 称为数据文件；保留既有命令与文件名。开发参考从 [dev_doc/README.md](dev_doc/README.md) 进入，详细技术说明维护在对应专题中。

本机部署、路径或实测记录放在根 `private/`，由独立本地 Git 保存。处理本机运行副本、归档或历史实测时，若存在则查阅 `private/README.md`。公开文档、安装和测试独立可用。

- `codex_usage/charts.py`：流式解析 rollout JSONL 元数据，聚合 token 用量，导出图表、CSV 和 `summary.json`。
- `codex_usage/query.py`、`codex_usage/query_cli.py`：供其他项目调用的纯数据查询，默认 JSON 标准输出；接口与返回协议见 [集成说明](docs/integration.md)。
- `codex_usage/animations.py`、`tests/test_animations.py`：按日期重放图片并生成 MP4；默认仅生成 02 动画，其余编号按需生成，整个动画固定模型颜色。
- `codex_usage/running_time.py`：解析回合运行区间、去重、按本地日期拆分并计算合并和相加时长。
- `codex_usage/cost.py`：基于逐请求用量和记录的单价计算 API 费用估算。
- `codex_usage/gui.py`、`启动用量统计.cmd`：Windows GUI 与启动器。
- `codex_usage/environment.py`、`codex_usage/demo.py`：当前解释器的功能检查、显式 extra 安装与合成数据演示。
- `tests/test_codex_usage_charts.py`、`tests/test_codex_running_time.py`：基于合成数据的 unittest 测试。
- `tests/test_codex_usage_cache.py`、`tests/test_user_entrypoints.py`：增量缓存及启动、选项、错误处理的合成数据测试。
- `codex_usage/__main__.py`、`tests/test_cli.py`：统一命令入口、参数传递与目录定位验证。
- GUI 设置保存在状态目录，CLI 默认报告随调用目录、缓存使用平台用户缓存目录；根启动器以自身目录为运行根。`codex-usage paths` 显示实际位置，路径优先级与旧文件迁移见 [安装指南](docs/installation.md#4-状态目录与旧版迁移)。生成结果通过脚本更新，避免手工修补输出。

README 固定展示媒体保存在 `docs/assets/`：`02_cumulative_tokens.png`、`10_daily_running_time.png` 及图 2 的 MP4、GIF，与日常运行结果分开，不新增预览生成脚本。GitHub 保留两张 PNG 的现有内容；所有提交、push 及 GitHub 插件上传均排除它们的后续内容更新，也不再反复询问上传授权。此约定用于减少自动审批带来的操作负担，作者明确表示原因并非图片内容敏感。两张 PNG 已被 Git 跟踪，不能依赖 `.gitignore` 屏蔽更新；纯路径迁移按实施前已提交 blob 等值核验，提交方法见 [贡献说明](CONTRIBUTING.md#提交内容)。其余生成结果继续忽略，测试和演示使用合成数据。

## 运行与验证

使用选定的同一个 Python。开发依赖、测试、构建普通 wheel、重新安装和无关工作目录验收见 [贡献说明](CONTRIBUTING.md#本地开发)；源码和已安装包分别验证，修改保留在源码中。安装后可从任意目录执行：

```powershell
codex-usage paths
codex-usage report --timezone Asia/Shanghai
codex-usage cost
codex-usage gui
```

GUI 默认包含归档记录。`setup --state-dir` / `--report-dir` 保存明确的目录选择，环境变量可覆盖，显式输出和缓存参数保持有效。根目录 `启动用量统计.cmd` 是唯一维护源，复制后使用脚本所在目录的设置、`cache/` 与 `reports/`，仅在 `setlocal` 内覆盖状态和报告环境变量，不写全局 `paths.json`；更新运行副本时从根维护源同步。命令行仍应支持自定义数据目录、输出目录、时区、日期范围和 PNG/SVG/PDF 格式。

启动及 `setup` / `check` 只检查环境；依赖安装仅通过用户显式的 `setup --install data|plotting` 或 pip 完成。工具不自动创建 `.venv`。生产安装的动画使用外部 FFmpeg，路径约定见安装指南；缺失时给出操作指引。

统计或解析逻辑变更后运行相关测试；新增统计应覆盖真实边界，如零分母、空闲日期、加权计算、时区和重复日志。不要在测试中依赖用户真实日志。图表修改后生成并查看图片，检查坐标、图例、文字和无数据情况。纯文档修改无需运行统计脚本。

## 数据与统计约定

- 只读取会话元数据、模型/turn ID、token 计数、活动类型和时间戳；不使用或导出用户消息、助手回答、推理或工具输出正文，不修改原始日志。
- 用量仅代表本机仍保留的日志。不要表述为服务端账户用量或剩余额度。
- 纯数据查询按实际 session / turn 清单记账，用量事件数不直接称为服务端请求数；保留未匹配、缺失和未完成诊断。按模型筛选时，混合或未知模型回合的时长不按 token 比例推算。
- token 事件优先采用 `last_token_usage`，缺失时才采用相邻 `total_token_usage` 差值。保留已有分叉、迁移重复事件去重规则。
- 缓存输入是输入的子集，推理输出是输出的子集；构成图需要拆成互斥部分，不能重复相加。
- 缓存图展示未命中率，即 `100% − cached_input_tokens / input_tokens × 100%`，纵轴按实际范围缩放。跨请求或日期先汇总分子和分母；7 日趋势使用日历日窗口，不平均每日百分比。零输入对应缺失值，不能当作 0% 未命中率；缓存写入计入未命中部分。
- 运行时间区分重叠区间合并时长和各 session 直接相加时长；后者允许超过每日小时数。跨日按指定时区拆分，分母为完整本地日的实际秒数。
- 缺少结束标记的回合只计算到最后一条回合内活动，不自动延长至当前时间。
- 费用估算保留单价来源、核实日期、长上下文规则和未知模型的情景假设。修改价格时核对官方来源，并同步说明；不将估算表述为实际账单。

## 修改约定

- 复用现有聚合、字体、日期轴和 `save_figure` 逻辑；图表先渲染到输出目录的临时文件再替换目标，保持 Windows 查看器兼容。
- 保留既有图表编号和文件名。新增图表接入 `export_report`、`summary.json` 的图表清单，并按需增加 GUI 入口、更新 README。
- CSV 使用 UTF-8 BOM，JSON 使用 UTF-8；日期导出保留时区。无定义的 JSON 比例使用 `null`，不输出 NaN 或 Infinity。
- GUI 中耗时工作放在工作线程或子进程，通过队列交回 Tk 主线程；维持取消、错误日志和更新期间禁用参数及更新按钮的行为。图表统一从结果目录查看，不设置单图按钮。
- 保持修改聚焦，不添加无关依赖或顺带改动费用单价、统计范围及原有输出含义。
