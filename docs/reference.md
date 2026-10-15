# 输出文件参考

[返回首页](../README.md) · [使用指南](usage.md) · [统计与数据约定](../dev_doc/statistics.md)

GUI 会顺序生成用量与费用图片，并导出数据文件。CLI 需分别运行两个脚本。以下图表默认使用 PNG；用量脚本可通过 `--format` 切换 SVG / PDF，费用图固定为 PNG。

## 输出

- `01_daily_tokens.png`：每日用量和 7 日移动平均；
- `02_cumulative_tokens.png`：按模型堆叠的本地累计 token 用量，使用不透明色块且不绘制边界折线；auto-review 始终放在最上层，其余模型按首次出现时间从下到上排列，颜色与 08、09 对应，图例列出累计用量，保留 unknown 等所有记录中的模型；
- `03_daily_composition.png`：输入、缓存输入、可见输出、推理输出等构成，使用对数 Y 轴独立折线显示；
- `04_weekly_by_source.png`：按 desktop、CLI、subagent、auto-review 等来源分组；
- `05_weekday_hour_heatmap.png`：按本地星期和小时统计的热力图，采用彩虹色阶和对数颜色刻度，零用量的时段显示色阶最低颜色；
- `06_top_threads.png`：按主对话及其所有层级 subagent 的累计 token 总量排序的任务排行，子代理不再单独占据排名；
- `07_request_sizes_over_time.png`：单次模型请求大小随时间的变化，使用高斯模糊点；重叠区域压缩透明度，并按各来源的局部密度混色，减少颜色饱和和遮挡；Y 轴仍为对数刻度，超过 60,000 次请求时沿用固定种子的抽样；
- `08_weekly_by_model.png`：各模型的周用量，逐一显示日志中记录的模型名称，不再将用量排名靠后的模型合并为 `other`；日志未记录模型名的事件仍显示为 `unknown`；
- `10_daily_running_time.png`：以 24 小时为分界的分段线性双小时轴图，左轴为合并重叠区间后的运行时长及 7 日均线，右轴为各 session 时长直接相加的总时长；
- `11_daily_cache_miss_rate.png`：每日缓存未命中率、最近 7 日按输入 token 加权的未命中率，以及所选时段的整体未命中率；纵轴按实际范围缩放；
- `12_recent_hourly_tokens.png`：最近七个日历日的逐小时总 token 柱状图，以统计结束日期（指定 `--end` 时使用该日期，否则使用本地日志的最后活动日期）为最后一天；按指定时区和 token 事件时间归入各小时，空闲小时补 0；仅使用所选日期范围内的事件，图中标明日期和时区；
- `events.csv`：逐次 token 事件，`session_id` 保留实际会话 ID，`parent_thread_id` 记录直接父会话，`thread_id` 与 `title` 统一归属到最上层主对话；
- `daily_usage.csv`、`weekly_usage.csv`：按日和按周汇总；每日表同时包含运行时间占比和缓存未命中率；
- `recent_hourly_usage.csv`：与 12 图对应的逐小时 token、请求数和任务数，`hour_start` 保留时区；上海时区共 168 行，夏令时切换时按实际小时数保留或跳过对应小时；
- `daily_running_time.csv`：每日合并时长、直接相加的总时长、占比和日期是否已结束；
- `running_intervals.csv`：与所选日期相交的原始回合运行区间及时间来源，可用于核对；
- `thread_usage.csv`、`model_usage.csv`：按任务和模型汇总；任务表的 `total_tokens` 包含主代理和所有层级子代理，`main_agent_tokens`、`subagent_tokens` 分别列出两部分，`subagent_sessions` 为有用量的子代理会话数；
- `summary.json`：覆盖时间、总量、峰值、整体缓存未命中率和解析统计，`running_time` 包含合并时长、直接相加的总时长、日均占比和峰值日期。


费用脚本另生成 09 图和费用明细，见 [API 费用估算](api-cost.md)。`summary.json` 的 `charts` 当前只列出用量脚本生成的图；09 图由费用脚本单独管理。
