# 开发文档

这里保存理解和维护本项目所需的当前设计、统计约定与修改流程。项目规模较小，以本入口和三个专题文件组织，按任务阅读即可。

## 从哪里开始

| 你要做的事 | 先读 |
| --- | --- |
| 理解模块、调用关系和数据如何流转 | [架构与数据流](architecture.md) |
| 修改日志解析、任务归属、缓存或运行时间 | [统计与数据约定](statistics.md)，再看架构中相应部分 |
| 添加统计或调整图表 | [统计与数据约定](statistics.md)、[输出文件参考](../docs/reference.md)、[修改与验证](maintenance.md) |
| 修改启动器或 GUI | [架构与数据流](architecture.md#启动与-gui)、[使用指南](../docs/usage.md)、[修改与验证](maintenance.md) |
| 修改价格、费用情景或费用图 | [费用规则与来源](../docs/api-cost.md)、[修改与验证](maintenance.md) |
| 准备提交改动 | [贡献说明](../CONTRIBUTING.md)、[修改与验证](maintenance.md) |

## 文档分工

| 位置 | 维护什么 |
| --- | --- |
| [根 README](../README.md) | 直接引用输出目录中的真实用量图片，简述用途、日常操作和安装入口 |
| `docs/installation.md` | 给 AI agent 的源码获取、环境准备、合成验证和用户交接步骤 |
| `docs/usage.md`、`docs/troubleshooting.md` | 用户操作、参数和排错步骤 |
| `docs/reference.md`、`docs/api-cost.md` | 输出文件、字段说明、价格来源与费用假设，供用户和开发者共用 |
| `dev_doc/architecture.md` | 当前模块职责、数据流、缓存和 GUI 执行机制 |
| `dev_doc/statistics.md` | 统计含义、去重、分组、时区、分母和图表语义 |
| `dev_doc/maintenance.md` | 改动入口、验证方式和文档同步规则 |
| [AGENTS.md](../AGENTS.md) | 项目协作要求与关键约束 |

同一个定义只在一个专题中详细维护，其他页面通过链接引用。更新文档时描述当前实现；未完成的想法可放到 Issue，已经完成的变化通过 Git / PR 记录。

## 原 README 内容去向

| 原内容 | 现在的位置 |
| --- | --- |
| 安装、双击 GUI、命令行参数 | `docs/installation.md`、`docs/usage.md`，根 README 保留入口 |
| 增量缓存、压缩日志、故障恢复 | `architecture.md` 的缓存章节；操作建议在使用指南和常见问题中 |
| 输出清单、CSV 与 JSON 字段 | `docs/reference.md`；运行时间和缓存比例字段在 `statistics.md` |
| token 取值、重复记录、子代理归属 | `statistics.md` |
| 缓存未命中率、每日运行时间及图表坐标约定 | `statistics.md` |
| API 单价、核实日期、长上下文与情景假设 | `docs/api-cost.md` |

这些内容从原 README 拆出后继续保留，不需要在首页重复展开。开发环境与提交方式仍从 [贡献说明](../CONTRIBUTING.md) 进入。
