# 输出文件参考

[返回首页](../README.md) · [使用指南](usage.md) · [统计与数据约定](../dev_doc/statistics.md)

GUI 会顺序生成用量与费用图片，并导出数据文件。CLI 需分别运行两个脚本。以下图片默认使用 PNG；`report` 可通过 `--format` 切换 SVG / PDF，包括 13 图；费用脚本单独生成的 09 图固定为 PNG。

## 输出

- `01_daily_tokens.png`：按模型堆叠的每日用量柱状图和总用量的 7 日移动平均，各模型的每日用量累加后对应 02 图；
- `02_cumulative_tokens.png`：按模型堆叠的本地累计 token 用量，使用不透明色块且不绘制边界折线；auto-review 始终放在最上层，其余模型按首次出现时间从下到上排列，颜色与 08、09 对应，图例列出累计用量，保留 unknown 等所有记录中的模型；
- `02_cumulative_tokens.mp4`：默认生成的图 2 动画，从 30 天前按日推进到今天；颜色和堆叠顺序在整个动画中固定。其他编号的 MP4 按需生成，文件名沿用同名图片的主文件名，见 [动画用法](usage.md#动画)；
- `02_cumulative_tokens.gif`：通过 `animate --chart 2 --gif` 按需生成的 GIF 动图，直接用于 README 的动画展示；与 MP4 使用相同的画面、逐帧坐标缩放和播放速度。其他编号同样支持 `--gif`；
- `03_daily_composition.png`：输入、缓存输入、可见输出、推理输出等构成，使用对数 Y 轴独立折线显示；
- `04_weekly_by_source.png`：按 desktop、CLI、subagent、auto-review 等来源分组；
- `05_weekday_hour_heatmap.png`：按本地星期和小时统计的热力图，采用彩虹色阶和对数颜色刻度，零用量的时段显示色阶最低颜色；
- `06_top_threads.png`：按主对话及其所有层级 subagent 的累计 token 总量排序的任务排行，每根横条按模型堆叠，末端标注任务总用量；图例分两列放在绘图区右下角。左侧标签分两行，上行为 Codex 项目名与 thread 开始日期，下行为任务名。各模型色块中央标注美元费用估算，过窄色块省略价格文字；费用汇总紧贴横条末端的总 token 数，上行显示任务总费用，下行显示每百万 token 平均费用。费用和平均价的 token 分母均排除 auto-review，其用量仍保留在横条中；缺少单价或 token 明细时仅汇总可计价部分，详见 [费用规则](api-cost.md)。子代理不再单独占据排名；
- `07_request_sizes_over_time.png`：单次模型请求大小随时间的变化，使用高斯模糊点；重叠区域压缩透明度，并按各来源的局部密度混色，减少颜色饱和和遮挡；Y 轴仍为对数刻度，超过 60,000 次请求时沿用固定种子的抽样；
- `08_weekly_by_model.png`：各模型的周用量，逐一显示日志中记录的模型名称，不再将用量排名靠后的模型合并为 `other`；日志未记录模型名的事件仍显示为 `unknown`；
- `10_daily_running_time.png`：以 24 小时为分界的分段线性双小时轴图，左轴为合并重叠区间后的运行时长及 7 日均线，右轴为各 session 时长直接相加的总时长；
- `11_daily_cache_miss_rate.png`：每日缓存未命中率、最近 7 日按输入 token 加权的未命中率，以及所选时段的整体未命中率；纵轴按实际范围缩放；
- `12_recent_hourly_tokens.png`：最近七个日历日按模型堆叠的逐小时 token 柱状图，以统计结束日期（指定 `--end` 时使用该日期，否则使用本地日志的最后活动日期）为最后一天；按指定时区和 token 事件时间归入各小时，空闲小时补 0；仅使用所选日期范围内的事件，图中标明日期和时区；
- `13_recent_hourly_api_cost.png`：与 12 图使用相同小时窗口的逐小时 API 费用估算，按模型堆叠，单位为美元；逐请求应用记录单价和长上下文规则后再按小时求和，未记录单价的 `unknown`、auto-review 等模型不计入金额，图片注明未计价请求及用量；
- `events.csv`：逐次 token 事件，`session_id` 保留实际会话 ID，`parent_thread_id` 记录直接父会话，`thread_id` 与 `title` 统一归属到最上层主对话；`project_name`、`thread_started_at` 保存主对话的项目名和带时区的开始时间；
- `daily_usage.csv`、`weekly_usage.csv`：按日和按周汇总；每日表同时包含运行时间占比和缓存未命中率；
- `recent_hourly_usage.csv`：与 12 图对应的逐小时 token、请求数和任务数，`hour_start` 保留时区；上海时区共 168 行，夏令时切换时按实际小时数保留或跳过对应小时；
- `recent_hourly_api_cost.csv`：与 13 图对应的逐小时费用，包含保留时区的 `hour_start`、`official_models_cost_usd`、各模型的 `model_<模型名>_cost_usd`，以及未计价的 `unpriced_requests` 和 `unpriced_tokens`；
- `daily_running_time.csv`：每日合并时长、直接相加的总时长、占比和日期是否已结束；
- `running_intervals.csv`：与所选日期相交的原始回合运行区间及时间来源，可用于核对；
- `thread_usage.csv`、`model_usage.csv`：按任务和模型汇总；任务表的 `total_tokens` 包含主代理和所有层级子代理，`main_agent_tokens`、`subagent_tokens` 分别列出两部分，`subagent_sessions` 为有用量的子代理会话数，`project_name` 和 `thread_started_at` 对应 06 图左侧标签；
- `summary.json`：覆盖时间、总量、峰值、整体缓存未命中率和解析统计；`running_time` 包含合并时长、直接相加的总时长、日均占比和峰值日期；`recent_hourly_api_cost` 包含 13 图的币种、估算总额、未计价请求与用量，以及记录单价的来源、核实日期和规则。


01、06、12 图沿用 02 图的模型配色和堆叠顺序：auto-review 在最上层（横条中为最右侧），其余模型按所选时段的首次出现时间排列；06 的任务子集和 12 的最近七日窗口沿用整个所选时段的颜色，不重新分配。保留 `unknown` 等记录中的模型名称，不合并为 `other`；06、12 的图例仅列出当前图片中有用量的模型。13 图沿用相同的模型配色和顺序，仅显示有记录单价且在当前图片中产生费用的模型。

费用脚本另生成 09 图和费用明细，见 [API 费用估算](api-cost.md)。`summary.json` 的 `charts` 列出 `report` 生成的静态图片，包含 13 图；09 静态图片由费用脚本单独管理。`animations` 列出本次 `report` 生成的动画；`animate` 单独运行时更新所选编号的条目并保留其他条目，每项包括 `chart`、`file`、`format`、`codec`、`start_date`、`end_date`、`timezone`、`frames`、`encoded_frames`、`fps`、`end_hold_seconds`、`duration_seconds` 和固定的 `model_colors`。日期为首尾快照的本地日期，`frames` 为日期快照数，`encoded_frames` 包含末尾静止的一秒所用的重复帧；`end_hold_seconds` 为额外停留秒数，`duration_seconds` 为包含停留的总播放秒数。
