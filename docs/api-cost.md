# API 费用估算

[返回首页](../README.md) · [使用指南](usage.md)

以下保留项目已有的单价来源、核实日期和假设。价格记录不保证持续有效；此计算仅用于假设性比较，不代表订阅账单或实际应付费用。

`report` 在生成用量图片与数据文件时，同时在 06 任务排行中标注费用，并生成 13 图的逐小时费用估算和 14 图的每日费用构成。需要累计费用图片与完整费用明细时，再运行：

```powershell
python -m codex_usage cost
```

费用计算器采用 [OpenAI 官方标准 API 文本 token 单价](https://developers.openai.com/api/docs/pricing)，单位为每百万 token（美元）。GPT-6.1 Terra 单价于 2026-10-29 核实，GPT-6.1 Sol 于 2026-10-07 核实，其他模型沿用 2026-09-23 的核实记录：

| 模型 | 输入 | 缓存输入 | 缓存写入 | 输出 |
| --- | ---: | ---: | ---: | ---: |
| [GPT-6 Astra](https://developers.openai.com/api/docs/models/gpt-6-astra) | $10.00 | $1.00 | $12.50 | $50.00 |
| [GPT-6.1 Sol](https://developers.openai.com/api/docs/models/gpt-6.1-sol) | $2.00 | $0.10 | $2.50 | $10.00 |
| [GPT-6.1 Terra](https://developers.openai.com/api/docs/models/gpt-6.1-terra) | $1.00 | $0.05 | $1.25 | $5.00 |
| [GPT-6 Sol](https://developers.openai.com/api/docs/models/gpt-6-sol) | $2.00 | $0.20 | $2.50 | $10.00 |
| [GPT-6 Luna](https://developers.openai.com/api/docs/models/gpt-6-luna) | $0.10 | $0.01 | $0.125 | $0.50 |
| [GPT-5.6 Sol](https://developers.openai.com/api/docs/models/gpt-5.6-sol) | $4.00 | $0.40 | $5.00 | $20.00 |
| [GPT-5.6 Terra](https://developers.openai.com/api/docs/models/gpt-5.6-terra) | $2.00 | $0.20 | $2.50 | $12.00 |
| [GPT-5.6 Luna](https://developers.openai.com/api/docs/models/gpt-5.6-luna) | $0.20 | $0.02 | $0.25 | $1.20 |
| [GPT-5.5](https://developers.openai.com/api/docs/models/gpt-5.5) | $5.00 | $0.50 | 按输入价，无额外费用 | $30.00 |

[GPT-6 Sol 和 Luna 于 2026-09-22 发布](https://developers.openai.com/api/docs/changelog)。各模型输入超过 272K token 时，整次请求的输入与缓存部分按 2 倍、输出按 1.5 倍估算。GPT-5.6 及更新模型的缓存写入按普通输入价的 1.25 倍；[GPT-5.5 没有额外缓存写入费](https://developers.openai.com/api/docs/guides/prompt-caching)。2026-10-29 再次核对 [GPT-5.6 Sol 官方页面](https://developers.openai.com/api/docs/models/gpt-5.6-sol)，输入 / 缓存输入 / 输出仍为 $4 / $0.40 / $20；官方表示优惠至少持续至 2026-11-21，未确认之后一定涨价。本次只增加历史费率机制，保留已有单价和核实记录，不添加 2026-11-22 的价格条目。

## 按事件时间选择费率

费用计算先按每个事件的 `timestamp` 选择该模型的费率，再应用该档的缓存写入、长上下文阈值和倍率，最后按任务、模型、日期或小时汇总。06 任务排行、09 累计费用、13 逐小时费用、14 每日费用构成及相应动画均使用同一选择规则；auto-review 情景按事件时间选择假设模型的费率，仍不代表 auto-review 的官方价格。

- `codex_usage/model_prices.py` 的 `OFFICIAL_PRICE_HISTORY` 保存每个模型按时间升序排列的 `PricePeriod`。首项 `effective_from=None` 是兼容基线，保留原有算法对旧日志使用的单价；它不声称这就是模型发布以来的完整真实价格历史。目前所有模型都只有这一档；旧模型保留原有费率，新加入 GPT-6.1 Terra 的历史记录采用已核实价格作为兼容基线，并不声称已知其生效时间。
- 后续档的 `effective_from` 是生效瞬间，包含该瞬间，适用到下一档生效前。时间比较统一换算成 UTC；有偏移的时间按同一绝对时刻比较，无时区时间和仅日期值按 UTC 解释。选择费率不会改写事件原时间列，所以原有本地日期、小时和夏令时分桶保持不变。
- Python 的 `calculate_model_event_costs(events, model)` 在缺少时间列、时间为空或无法解析时使用兼容基线，避免以后新增费率改变这些旧记录的估算。它不推测事件发生时间。按日期 / 小时聚合及费用 CLI 仍要求可用于分桶的有效时间戳；上述回退仅决定单价，不为缺失时间补日期。
- `calculate_event_costs(events, price)` 仍是明确指定 `ModelPrice` 的固定单价接口，不要求时间列。`ModelPrice` 字段和 `OFFICIAL_PRICES` 的模型到单价字典结构保持不变；后者由历史表派生，表示最新记录的一档，不能用它直接重算需要区分历史日期的日志。

以后只有在官方确认新单价及其生效时间后，才在对应模型的 tuple 末尾追加 `PricePeriod("带时区的 ISO 8601 生效时间", ModelPrice(...))`，保留基线和所有旧档。每档均保存自己的来源、`verified_on`、缓存和长上下文规则。`verified_on` / `PRICE_DATE` 是核实日期，不是价格生效日期；官方只给日期时，应在记录前确认所采用的生效时区。追加时同步本页并运行跨生效边界的合成测试。

## 输出与兼容

费用命令生成：

- `api_cost_by_model.csv`：模型费用明细，保留原有金额与 token 字段。新增 `price_period_count` 表示实际使用的费率档数（未定价模型为 0）；单档时原有三个 `*_rate_per_million` 列记录该档的单价，跨多档时留空，避免把某一档误标为整段历史的统一单价。`official_source` 保留使用过的来源，多个来源以分号分隔；
- `api_cost_scenarios.csv`：`codex-auto-review` 分别假设采用 GPT-5.6 Luna、Terra、Sol 和 GPT-6 Astra 单价时的情景；
- `daily_api_cost.csv`：逐日及累计费用，包含各个公开定价模型的 `model_<模型名>_cost_usd` 和 `cumulative_model_<模型名>_cost_usd`；
- `api_cost_estimate.json`：保留 `price_date`、`official_prices` 等原有字段，新增 `official_price_history`。每模型对应一个列表，每项包含 `effective_from`（基线为 JSON `null`）和完整 `price` 对象，用于核对跨费率估算；
- `09_cumulative_api_cost.png`：按模型堆叠的累计费用面积图，使用不透明色块且不绘制边界折线，最上方为总额，图例列出各模型的累计估算费用；颜色与 08 图一致，堆叠从底部到顶部按各模型在统计日志中首次出现的时间排序，去掉 auto-review 等没有公开单价的模型及其费用情景。

`codex-auto-review` 没有公开 API 单价，因此情景值不是官方报价。费用也不包含 Web Search、Computer Use、存储等工具费用。

`06_top_threads.png` 在各模型色块中央标注美元费用估算；色块宽度容不下数字时省略价格文字，费用仍参与汇总。费用汇总紧贴每根横条末端的总 token 数，上行显示任务总费用，下行显示每百万 token 平均费用（`/ 1M`）。先按每次请求应用已有单价、缓存和长上下文规则，再汇总到主对话及其各级子代理。平均费用按任务费用之和 ÷ 对应 token 总数 × 1,000,000 计算，不平均各模型的单价。`codex-auto-review` 以及来源为 `auto_review` 的记录，其费用与 token 均不参与这两项统计，也不标注色块费用，原有 token 横条长度和排名仍包含这些用量。

其他没有记录单价的模型，以及只有 `total_tokens`、没有输入 / 输出明细的记录，费用显示为 `N/A`；部分缺失时，仅汇总可计价部分的费用与 token，不把未计价部分当作免费用量。有输入 / 输出明细的部分按其 token 数计入平均价分母，不重复计入缓存输入或推理输出。无可计价 token 时平均价显示为 `N/A`；任务仅包含 auto-review 时右侧显示「—」。06 图直接使用本轮所选日期范围的用量事件，随 `report` 支持 PNG / SVG / PDF；单价来源与核实日期沿用本页记录。

`report` 生成的 `13_recent_hourly_api_cost.png` 是最近七个日历日的逐小时费用柱状图，按模型堆叠。它与 12 图使用相同的本地小时索引、日期范围、时区、模型配色和顺序，空闲小时补 0；每次请求先复用费用计算器的记录单价、缓存和长上下文规则计算美元估算，再按小时与模型求和。`unknown`、auto-review 等没有记录单价的模型不计入金额，也不采用费用情景补值；图片注明这些未计价请求及 token 用量。

对应的 `recent_hourly_api_cost.csv` 保留带时区的 `hour_start`，导出 `official_models_cost_usd`、各模型的 `model_<模型名>_cost_usd`、`unpriced_requests` 和 `unpriced_tokens`。`summary.json` 的 `recent_hourly_api_cost` 记录币种、估算总额、未计价请求与用量，并保留 `price_date`、`official_prices`，新增窗口内已计价模型的完整 `official_price_history`，记录各档生效时间、单价、来源、核实日期和适用规则；13 图同时列入其 `charts` 清单。GUI「更新图片」和演示流程会随 `report` 自动生成 13 图。

## 每日费用构成

`report` 生成的 `14_daily_api_cost_composition.png` 按本地日历日展示未缓存输入、缓存输入、缓存写入、可见输出和推理输出的美元费用估算，分类与配色沿用 03 图。图片采用每日堆叠柱状图，每根柱子代表一天；纵轴从 0 开始，使用线性美元坐标。每层代表当天该类 token 的费用，整根柱子的高度为当天可计价费用总额，金额不累计到下一天，空闲日期补 0。

每条用量事件先按模型和事件时间应用记录费率、缓存写入与长上下文规则，再汇总到所选时区的日期。缓存输入和缓存写入从输入中拆出；输出费用按该事件的推理输出占比拆为推理输出与可见输出，两部分共用输出费率，并包含相同的长上下文倍率。推理计数限定在 0 到该事件输出 token 数之间，零输出不产生拆分费用。五类费用相加等于原费用计算器的可计价总额。

`unknown`、auto-review 等没有记录单价的模型，以及只有总 token、没有输入 / 输出明细的记录，不计入金额，不按情景假设补值；无法分类的 token 保留为未计价用量。`daily_api_cost_composition.csv` 的 `date` 保留时区，`official_models_cost_usd` 为每日可计价费用总额，`unpriced_requests` 和 `unpriced_tokens` 保留未计价事件数与用量。`summary.json` 的 `daily_api_cost_composition` 保存估算总额、币种、未计价统计和费率来源，`components_usd` 保存各分类的时段费用之和；14 图列入 `charts`。GUI「更新图片」和演示流程自动生成，14 动画按需选择。

## 自定义目录与格式

如果输出在自定义目录，需同时指定输入和输出，例如：

```sh
python -m codex_usage cost --events report/events.csv --output report
```

本页命令需使用已安装项目依赖的 Python；Windows 默认虚拟环境为 `.venv/Scripts/python.exe`，macOS / Linux 为 `.venv/bin/python`。09 图固定为 PNG；13、14 图随 `report --format` 支持 PNG / SVG / PDF。
