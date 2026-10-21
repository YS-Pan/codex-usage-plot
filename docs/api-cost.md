# API 费用估算

[返回首页](../README.md) · [使用指南](usage.md)

以下保留项目已有的单价来源、核实日期和假设。价格记录不保证持续有效；此计算仅用于假设性比较，不代表订阅账单或实际应付费用。

`report` 在生成用量图片与数据文件时，同时在 06 任务排行中标注费用，并生成 13 图的逐小时费用估算。需要累计费用图片与完整费用明细时，再运行：

```powershell
python -m codex_usage cost
```

费用计算器采用 [OpenAI 官方标准 API 文本 token 单价](https://developers.openai.com/api/docs/pricing)，单位为每百万 token（美元）。GPT-6.1 Sol 单价于 2026-10-07 核实，其他模型沿用 2026-09-23 的核实记录：

| 模型 | 输入 | 缓存输入 | 缓存写入 | 输出 |
| --- | ---: | ---: | ---: | ---: |
| [GPT-6 Astra](https://developers.openai.com/api/docs/models/gpt-6-astra) | $10.00 | $1.00 | $12.50 | $50.00 |
| [GPT-6.1 Sol](https://developers.openai.com/api/docs/models/gpt-6.1-sol) | $2.00 | $0.10 | $2.50 | $10.00 |
| [GPT-6 Sol](https://developers.openai.com/api/docs/models/gpt-6-sol) | $2.00 | $0.20 | $2.50 | $10.00 |
| [GPT-6 Luna](https://developers.openai.com/api/docs/models/gpt-6-luna) | $0.10 | $0.01 | $0.125 | $0.50 |
| [GPT-5.6 Sol](https://developers.openai.com/api/docs/models/gpt-5.6-sol) | $4.00 | $0.40 | $5.00 | $20.00 |
| [GPT-5.6 Terra](https://developers.openai.com/api/docs/models/gpt-5.6-terra) | $2.00 | $0.20 | $2.50 | $12.00 |
| [GPT-5.6 Luna](https://developers.openai.com/api/docs/models/gpt-5.6-luna) | $0.20 | $0.02 | $0.25 | $1.20 |
| [GPT-5.5](https://developers.openai.com/api/docs/models/gpt-5.5) | $5.00 | $0.50 | 按输入价，无额外费用 | $30.00 |

[GPT-6 Sol 和 Luna 于 2026-09-22 发布](https://developers.openai.com/api/docs/changelog)。各模型输入超过 272K token 时，整次请求的输入与缓存部分按 2 倍、输出按 1.5 倍估算。GPT-5.6 及更新模型的缓存写入按普通输入价的 1.25 倍；[GPT-5.5 没有额外缓存写入费](https://developers.openai.com/api/docs/guides/prompt-caching)。[GPT-5.6 Sol 的促销价](https://developers.openai.com/api/docs/models/gpt-5.6-sol) 官方表示至少持续至 2026-11-21，日后重算历史记录时应留意价格变动。它会生成：

- `api_cost_by_model.csv`：具有官方单价的模型明细；
- `api_cost_scenarios.csv`：`codex-auto-review` 分别假设采用 GPT-5.6 Luna、Terra、Sol 和 GPT-6 Astra 单价时的情景；
- `daily_api_cost.csv`：逐日及累计费用，包含各个公开定价模型的 `model_<模型名>_cost_usd` 和 `cumulative_model_<模型名>_cost_usd`；
- `api_cost_estimate.json`：单价、来源、各模型单价核实日期、假设和完整结果；
- `09_cumulative_api_cost.png`：按模型堆叠的累计费用面积图，使用不透明色块且不绘制边界折线，最上方为总额，图例列出各模型的累计估算费用；颜色与 08 图一致，堆叠从底部到顶部按各模型在统计日志中首次出现的时间排序，去掉 auto-review 等没有公开单价的模型及其费用情景。

`codex-auto-review` 没有公开 API 单价，因此情景值不是官方报价。费用也不包含 Web Search、Computer Use、存储等工具费用。

`06_top_threads.png` 在各模型色块中央标注美元费用估算；色块宽度容不下数字时省略价格文字，费用仍参与汇总。费用汇总紧贴每根横条末端的总 token 数，上行显示任务总费用，下行显示每百万 token 平均费用（`/ 1M`）。先按每次请求应用已有单价、缓存和长上下文规则，再汇总到主对话及其各级子代理。平均费用按任务费用之和 ÷ 对应 token 总数 × 1,000,000 计算，不平均各模型的单价。`codex-auto-review` 以及来源为 `auto_review` 的记录，其费用与 token 均不参与这两项统计，也不标注色块费用，原有 token 横条长度和排名仍包含这些用量。

其他没有记录单价的模型，以及只有 `total_tokens`、没有输入 / 输出明细的记录，费用显示为 `N/A`；部分缺失时，仅汇总可计价部分的费用与 token，不把未计价部分当作免费用量。有输入 / 输出明细的部分按其 token 数计入平均价分母，不重复计入缓存输入或推理输出。无可计价 token 时平均价显示为 `N/A`；任务仅包含 auto-review 时右侧显示「—」。06 图直接使用本轮所选日期范围的用量事件，随 `report` 支持 PNG / SVG / PDF；单价来源与核实日期沿用本页记录。

`report` 生成的 `13_recent_hourly_api_cost.png` 是最近七个日历日的逐小时费用柱状图，按模型堆叠。它与 12 图使用相同的本地小时索引、日期范围、时区、模型配色和顺序，空闲小时补 0；每次请求先复用费用计算器的记录单价、缓存和长上下文规则计算美元估算，再按小时与模型求和。`unknown`、auto-review 等没有记录单价的模型不计入金额，也不采用费用情景补值；图片注明这些未计价请求及 token 用量。

对应的 `recent_hourly_api_cost.csv` 保留带时区的 `hour_start`，导出 `official_models_cost_usd`、各模型的 `model_<模型名>_cost_usd`、`unpriced_requests` 和 `unpriced_tokens`。`summary.json` 的 `recent_hourly_api_cost` 记录币种、估算总额、未计价请求与用量，并附 `price_date`、`official_prices` 中的单价来源、各模型核实日期和适用规则；13 图同时列入其 `charts` 清单。GUI「更新图片」和演示流程会随 `report` 自动生成 13 图。

如果输出在自定义目录，需同时指定输入和输出，例如：

```sh
python -m codex_usage cost --events report/events.csv --output report
```

本页命令需使用已安装项目依赖的 Python；Windows 默认虚拟环境为 `.venv/Scripts/python.exe`，macOS / Linux 为 `.venv/bin/python`。09 图固定为 PNG；13 图随 `report --format` 支持 PNG / SVG / PDF。
