# 修改与验证

[开发文档入口](README.md) · [架构与数据流](architecture.md) · [统计与数据约定](statistics.md)

## 开发环境

环境建立、依赖安装和完整测试命令见 [贡献说明](../CONTRIBUTING.md)。优先使用仓库 `.venv` 的解释器；依赖范围以 `requirements.txt` 为准。项目直接运行源码，不需要打包或安装到全局环境。

CLI / 统计测试无需桌面显示；GUI 人工验收需要 Tcl/Tk 和桌面。GitHub Actions 配置覆盖 Windows / Ubuntu、Python 3.10 / 3.14，分别经过压缩支持的回退库和标准库路径；实际 CI 结果以对应运行记录为准。

## 按改动定位与验证

| 改动 | 主要位置 | 需要的验证 |
| --- | --- | --- |
| 日志字段、token 取值、父子任务归属 | `parse_rollout`、`frame_from_records`、`resolve_thread_roots` | `tests/test_codex_usage_charts.py`；确认不采集正文、去重不变、总量守恒 |
| 回合起止与跨日运行时间 | `codex_usage/running_time.py` | `tests/test_codex_running_time.py`；覆盖重叠、重复、跨午夜、夏令时和未完成回合 |
| 增量缓存或压缩读取 | `codex_usage/cache.py`、`collect_cached_usage_data`、`rollout_lines` | `tests/test_codex_usage_cache.py`；与完整读取对比，检查追加、半行、替换、移动、删除和回滚 |
| 新统计、字段或图表 | `aggregate_*`、`plot_*`、`export_report` | 相关合成数据测试；生成并查看正常、空闲和无数据图 |
| 动画、模型配色或 MP4 编码 | `codex_usage/animations.py`、绘图函数的固定颜色参数、`figure_sink` | `tests/test_animations.py`；检查新模型与排名反转、空白帧、跨日截断、真实 MP4 解码及失败保留旧视频；运行 demo 并查看首、中、末帧 |
| 费用估算 | `codex_usage/cost.py` | `tests/test_codex_usage_charts.py` 中费用相关测试，以及 `tests/test_historical_costs.py`；校验未知模型、长上下文阈值、缓存写入、UTC 生效边界和各费用入口一致性 |
| 启动、选项和错误处理 | 启动器、`codex_usage/environment.py`、`codex_usage/gui.py` | `tests/test_user_entrypoints.py`；实际启动、生成、停止以及错误后重试 |
| 包结构、命令入口和默认路径 | `codex_usage/__main__.py`、各模块的路径常量 | `tests/test_cli.py`；无依赖帮助、参数传递、源码根目录定位和标准 unittest 自动发现 |
| 纯数据查询、外部项目调用和元数据清单 | `codex_usage/query.py`、`query_cli.py`、采集函数的 `inventory` | 查询 / CLI / inventory 合成测试；核对 ID 与后代、精确时间、混合模型时长、缺失诊断、缓存完整性及 JSON / CSV 编码 |
| 纯文档文字与链接 | README、`docs/`、`dev_doc/` | 检查链接、路径、UTF-8 与 `git diff --check`，无需读取真实日志 |

新增测试应覆盖真实边界，不为按钮文案或实现细节逐项建立机械测试。自动测试只使用合成日志，不依赖开发者个人数据。运行相关测试后，如果涉及多个模块或准备交付，再运行完整 `python -m unittest -v`。

## 添加统计或图表

1. 先写清分子、分母、单位、日期范围和无数据含义，检查 [统计与数据约定](statistics.md)。跨请求 / 日期的比例先合计分子分母；不同口径不能直接相加。
2. 复用现有事件表与聚合函数，不重新从消息正文推断数据。GUI 继续调用 CLI，让统计实现集中在脚本层。
3. 在 `export_report` 接入图表及汇总字段，更新 `summary.json` 图表清单；保留已有编号、文件名、字段含义和输出编码。09 费用图当前由费用脚本单独管理。
4. GUI 统一通过结果目录查看图表；如需调整图片生成选项，维持后台执行、取消、日志和更新期间禁用参数及更新按钮的行为。
5. 执行相关测试，再运行 `python -m codex_usage demo` 生成演示图片，检查坐标、图例、单位、文字、空闲日期和无数据状态。
6. 同步输出文件参考、统计约定及相关使用说明。生成结果通过脚本更新，不手工修补 CSV / JSON / 图片。

## 更新价格

修改价格前核对官方来源及生效时间。在 `OFFICIAL_PRICE_HISTORY` 中追加已确认的费率档，保留兼容基线和旧档，避免重算旧日志时套用新价格。`OFFICIAL_PRICES` 是最新记录单价的兼容视图；内部计价使用 `calculate_model_event_costs`。单价、`verified_on` / `PRICE_DATE`、长上下文规则、缓存写入规则和未知模型假设要一起检查，并同步 [费用说明](../docs/api-cost.md#按事件时间选择费率)。核实日期不能当作生效日期；跨费率测试也应覆盖 09 动画、06 / 13 汇总和 auto-review 情景。不要把情景估算表述为实际账单，也不要在无关的界面或文档整理中顺带改价。

## 新手流程验收

- 全新可写目录：准备 `.venv`、安装依赖、再启动；路径包含中文或空格仍能工作。
- 无本地日志：说明如何选目录，以及如何运行合成演示。
- GUI 生成：设置正确传给用量脚本，费用脚本读取同一输出目录的 `events.csv`；更新中窗口可响应。
- 取消或出错：结束当前子进程，恢复按钮，保留可读日志；再次运行可恢复。
- 二次更新：复用缓存，统计结果与完整读取一致。
- 查看结果：从结果目录打开生成的图表；输出位置与文档一致。

可使用临时目录中的合成日志进行 GUI 验收。`codex_usage/demo.py` 生成固定的合成会话与时间，不读取正式数据根目录或统计缓存；其输出不能覆盖已有的非演示目录。

## 文档维护

| 行为变化 | 同步位置 |
| --- | --- |
| 安装和环境准备 | `docs/installation.md`、根 README 的安装入口 |
| 默认值、CLI 参数、GUI 操作 | `docs/usage.md`、根 README，必要时补常见问题 |
| 纯数据查询参数、schema、退出码与 Python 接口 | `docs/integration.md`、`statistics.md`、相关命令帮助 |
| 模块职责、数据流、持久化或线程边界 | `architecture.md` |
| 字段计算含义、去重、分组或图表坐标约定 | `statistics.md`、`docs/reference.md` |
| API 单价或估算假设 | `docs/api-cost.md`，对应源码与测试 |
| 项目入口、测试方法或协作约束 | `CONTRIBUTING.md`、本页、`AGENTS.md` 中对应入口 |

文档保持单一来源：涉及计算规则时从用户页面链接到统计文档；涉及单价时链接到费用说明。无需为每个源码文件另建说明页，也无需为小改动建立独立历史文档。

README 直接引用 `codex_usage_report/` 中的 `02_cumulative_tokens.png` 和 `10_daily_running_time.png`，GitHub 保留现有版本。本地图片继续通过正常生成流程更新；以后提交、push 和 GitHub 插件上传都排除这两张图片的修改，不再反复询问上传授权。原因及暂存方法集中维护在 [贡献说明](../CONTRIBUTING.md#提交内容)。不另建预览图片或复制文件；其余生成结果仍忽略，测试和自动验收继续使用合成数据。

提交前检查 `git status --short` 与最终 diff，避免纳入 JSONL、SQLite、本机设置和其他个人数据文件。默认忽略规则不覆盖所有任意命名的自定义输出目录。
