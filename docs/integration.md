# 供其他项目调用

[返回首页](../README.md) · [使用指南](usage.md) · [统计与数据约定](../dev_doc/statistics.md#纯数据查询)

`query` 从本机仍保留的 Codex 日志返回筛选后的用量数据，适合记录实验、子代理或批次的 token 用量。默认向标准输出写一个 JSON，不生成图片、动画或数据文件，也不创建缓存。它与 `report` 复用解析、去重和父子会话归属。

## 按子代理会话查询

在工具根目录使用已安装依赖的 Python：

```sh
python -m codex_usage query --session-id child-session-a child-session-b --model gpt-6-luna --require-match
```

将示例 ID 换成宿主实际返回、并且已经写入本地日志的会话 ID。Windows 未激活环境时使用 `.\.venv\Scripts\python.exe`；macOS / Linux 使用 `.venv/bin/python`。工具目录是可配置的外部依赖，无需复制解析器到调用方项目。

例如，对一次重排保存 `run/query/batch → session_id/turn_id` 映射，再将明确属于重排的 ID 传入。主代理编排、作答、裁判和格式修复分别登记；重试产生的新会话也要登记。仅筛选同一时间段的所有 `gpt-6-luna`，无法保证得到这次重排的用量。

### 筛选规则

同一参数内的多个值取并集，不同筛选维度取交集。ID 和模型名均精确匹配，模型名不自动解析别名。

| 参数 | 含义 |
| --- | --- |
| `--session-id ID ...`，别名 `--session` | 实际会话清单；默认只统计这些会话 |
| `--include-descendants` | 将 session 清单扩展到已知的各级子会话 |
| `--thread-id ID ...`，别名 `--thread` | 根任务清单，包含归属到这些根任务的所有子会话 |
| `--turn-id ID ...`，别名 `--turn` | 实际回合清单；不从父回合推测子代理回合 |
| `--model NAME ...` | 模型清单，例如 `gpt-6-luna` |
| `--start / --end` | 精确时间范围，采用 `[start, end)`，包含开始、不包含结束 |
| `--timezone NAME` | 显示时区及无偏移时间的解释时区，默认 `UTC` |
| `--ids-file PATH` | 读取 JSON 中的 ID / 模型数组，与命令行同维度的值合并 |

清单参数均可重复。`session_id` 保留实际会话身份，`thread_id` 是沿父子关系归属后的根 ID；根任务筛选容易包含与当前实验无关的其他工作。精确记账优先使用调用回执中的 session / turn 清单。

时间示例：

```sh
python -m codex_usage query --session-id child-session-a --start 2026-10-27T10:00:00+08:00 --end 2026-10-27T10:05:00+08:00 --timezone Asia/Shanghai
```

用量事件按日志时间戳归入范围，不把一个事件的 token 按时长分摊。运行区间按范围边界截取。没有 UTC 偏移的时间按 `--timezone` 解释；夏令时切换导致的歧义或不存在的本地时间会被拒绝，请显式给出偏移。与此不同，`report --start / --end` 接受日期并包含首尾整天。

批次较多时，可保存清单文件：

```json
{
  "session_ids": ["child-session-a", "child-session-b"],
  "turn_ids": [],
  "models": ["gpt-6-luna"]
}
```

```sh
python -m codex_usage query --ids-file rerank-usage-ids.json --require-match
```

文件只接受 `session_ids`、`thread_ids`、`turn_ids`、`models` 四个字段，各字段值为字符串数组；无需提供空字段。它保存筛选条件，调用方仍需另存 run / query / batch 的业务映射。

## 输出与退出码

默认标准输出始终是一个 JSON，运行诊断写到标准错误。`--format csv` 将标准输出改为事件 CSV，采用 UTF-8 BOM。需要同时保存完整元信息和表格时使用 `--output`：

```sh
python -m codex_usage query --ids-file rerank-usage-ids.json --output rerank-usage --include-cost --require-match
```

输出目录包含 `usage.json`、`events.csv`、`sessions.csv`、`turns.csv`、`running_intervals.csv`；标准输出仍遵循 `--format`。JSON 使用 UTF-8，CSV 使用 UTF-8 BOM，日期时间保留时区，CSV 中的列表 / 对象编码为 JSON。不要将输出目录放入原始日志目录或覆盖输入清单。

结果的 `schema_version` 为 `1`，主要字段如下：

| 字段 | 用途 |
| --- | --- |
| `tool`、`generated_at`、`scope` | 工具版本、生成时刻及本地日志统计范围 |
| `filters` | 实际数据目录、时区、筛选清单、时间范围和扫描选项 |
| `summary` | token 分项、`usage_event_count`、命中会话 / 回合数、缓存未命中率及两种运行秒数 |
| `sessions`、`turns` | 按实际会话、实际会话与回合组合分组的用量与时间 |
| `events` | 筛选后的去重用量事件及模型 / 回合元数据 |
| `running_intervals` | 可归属且按时间范围截取的运行区间 |
| `matches` | 本次筛选命中的身份 |
| `diagnostics` | 解析完整性、未匹配 ID、缺失用量 / 时间及未完成状态 |
| `cost` | 仅 `--include-cost` / `include_cost=True` 时出现，保存逐模型估算、记录单价和未计价用量 |

`summary.usage_event_count` 是本地日志用量事件数，不保证等于服务端请求数。缓存输入是输入的子集，推理输出是输出的子集，不能把这些分项再相加得到总量。零输入的 `cache_miss_percent` 为 `null`；JSON 不输出 NaN 或 Infinity。

`summary.running_seconds` 合并所选区间的重叠部分，`summary.summed_running_seconds` 直接相加各区间，保留并行时间。模型筛选只纳入整回合已知模型全部属于所选模型的区间；混合模型和未知模型回合不按比例分摊，诊断中保留缺测。未完成回合只算到最后一条已观察到的回合内活动。两种时长都不代表纯模型推理或输出解码耗时；重排端到端 p50 / p95 应由调用方自己的流程计时取得。

无匹配时，已观察到的用量合计为零，同时保留未匹配或缺失诊断；这不能证明未记录的调用没有消耗 token。只含会话头或运行区间而没有 token 的会话也有可能被识别。读取失败或存在无法解析的行时，`diagnostics.complete` 为 `false`，不能把部分结果当完整记账。

诊断中，`unmatched_ids` 表示指定身份在扫描数据中不存在，`filtered_out_ids` 表示身份存在但未通过本次其他筛选；`sessions_without_usage` / `turns_without_usage` 保留已识别但无用量事件的身份，`missing_runtime` / `excluded_runtime` 分别表示缺少可用时长和无法按模型归属的时长。`incomplete_turns` / `aborted_turns` 保留未完成与中止状态，`runtime_included` 标明该区间是否纳入时长；模型混合或未知导致区间排除时仍保留状态。`events_without_session_id` / `events_without_turn_id` / `events_without_model` 标明事件身份缺项，`parse_stats` 提供扫描、解析和去重计数。

分叉或迁移可能在多个 session 中保留同一个 turn。查询继续使用全局去重结果，不把复制的历史再计一次；`ownership_ambiguities` 列出这些回合的已观察会话、去重后用量所属会话和运行时间所属会话。完成副本可能取得运行时间，而用量保留在先读取的副本中，因此精确 session 记账遇到此诊断时须由调用方核对，不能把副本中的零事件解释为没有消耗。`--require-match` 只验证身份命中，不能消除复制记录的归属歧义。

| 退出码 | 含义 |
| --- | --- |
| `0` | 查询完成；默认允许空结果，请同时检查诊断 |
| `1` | 读取 / 写入失败，或返回的 `diagnostics.complete` 为 `false` |
| `2` | 参数、时间或输入清单无效 |
| `3` | 启用 `--require-match` 后无命中，或有指定身份不存在 / 被其他条件排除 |

可选 `--include-cost` 复用工具记录的价格与规则，保留来源、核实日期及未计价情况，属于 API 费用估算。每个事件按其 UTC 时间选择当时适用的价格；基线覆盖更早或缺失时间，后续费率从各自生效时间起沿用，直到下一次变更。`cost.estimated_cost_usd` 只合计可计价部分；没有可计价事件时为 `null`。逐模型的 `recorded_price` 是最新记录单价，`official_price_history` 保存用于计算的完整费率历史、来源和规则；未知模型的历史为空。未知模型及缺少输入输出明细的用量不会自动视为免费，相关数量保存在未计价字段中。现有价格说明见 [API 费用估算](api-cost.md)。

## 从其他项目运行 CLI

使用绝对 Python 路径，并将子进程的 `cwd` 指向工具根目录，不依赖调用方的当前目录。下面的路径应来自项目配置，清单文件由调用方准备：

```python
import json
import os
from pathlib import Path
import subprocess

tool_root = Path(config["codex_usage_tool_dir"]).resolve()
python = tool_root / (".venv/Scripts/python.exe" if os.name == "nt" else ".venv/bin/python")
ids_file = Path(config["usage_ids_file"]).resolve()
completed = subprocess.run(
    [str(python), "-m", "codex_usage", "query",
     "--ids-file", str(ids_file), "--require-match"],
    cwd=tool_root,
    capture_output=True,
    text=True,
    encoding="utf-8",
    env={**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
)
result = json.loads(completed.stdout) if completed.stdout.strip() else None
if completed.returncode != 0:
    raise RuntimeError(f"用量查询失败：{completed.returncode}\n{completed.stderr}\n{result}")
if result["schema_version"] != 1 or not result["diagnostics"]["complete"]:
    raise RuntimeError("用量结果版本不受支持或日志读取不完整")
tokens = result["summary"]["total_tokens"]
```

同时保存命令参数、完整 JSON、工具版本 / Git 提交和调用映射。外部项目不必把某个使用者的绝对工具路径写进通用内核。

## Python 接口

调用方环境需具备本项目 `requirements.txt` 中的依赖。源码无需安装成全局包；可以将配置的工具目录加入模块搜索路径：

```python
from pathlib import Path
import sys

sys.path.insert(0, str(Path(config["codex_usage_tool_dir"]).resolve()))
from codex_usage.query import query_usage

result = query_usage(
    codex_home=Path(config["codex_home"]),
    session_ids=["child-session-a", "child-session-b"],
    models=["gpt-6-luna"],
    start="2026-10-27T10:00:00+08:00",
    end="2026-10-27T10:05:00+08:00",
    timezone="Asia/Shanghai",
)
```

函数返回与 CLI 相同的可 JSON 序列化字典。关键字参数还包括 `thread_ids`、`turn_ids`、`include_descendants=False`、`include_archived=True`、`cache_path=None`、`incremental=False`、`progress=False`、`include_cost=False`；`start` / `end` 也接受 `datetime`。省略 `codex_home` 时读取 `CODEX_HOME` 或 `~/.codex`，时区默认 `UTC`。

Python 调用同样需要检查 `diagnostics`，参数无效或无法读取 / 写入时按异常处理。`--require-match` 是 CLI 的验收策略，Python 调用方按自身登记的 ID 清单核验结果。需要保存与 CLI 相同的五个数据文件时，可调用：

```python
from codex_usage.query_cli import export_query_result

export_query_result(result, Path(config["usage_output_dir"]))
```

## 缓存与并发

默认不使用缓存。需要重复查询时显式指定 `--cache-file PATH --incremental`；只给 `--incremental` 时，CLI 使用工具目录下的 `codex_usage_query_cache.sqlite3`，与日常图片缓存分开。Python 调用请显式提供 `cache_path`。

缓存保存完整扫描元数据，不会因为本次只筛选一个子代理而删除其他会话。更换筛选条件可复用同一数据根的缓存；不同 `CODEX_HOME` 应分别指定缓存文件。多个进程同时查询时，分别使用缓存或采用默认无缓存扫描，避免竞争同一个 SQLite 写事务。完整扫描仍会检查数据根中的日志，筛选参数限定返回结果，不承诺只打开指定会话文件。
