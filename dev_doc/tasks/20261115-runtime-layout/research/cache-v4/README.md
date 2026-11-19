# v4 缓存研究候选

本目录保存 2026 年 11 月 15 日的压缩候选和合成回归，供继续研究。此次候选未通过性能验收，正式实现沿用原表结构和 SQLite 默认策略。研究使用的 v4 编号早于正式缓存因响应去重而升到版本 4。

候选保留原始 token 计数、事件身份、顺序、检查点与四个索引，去掉重复日志路径、六项可重算构成及独立 fingerprint。

压紧布局在级联删除场景出现回退，调整 SQLite 页缓存后差异仍集中在提交阶段。[研究结论](../../progress.md#缓存研究)记录了后续复查的范围。

## 复现合成回归

在项目根使用选定的开发 Python：

```powershell
python dev_doc/tasks/20261115-runtime-layout/research/cache-v4/run_tests.py
```

[runner](run_tests.py) 启动独立子进程，将本目录的 [候选缓存](cache.py) 绑定到该子进程的模块命名空间，再运行 [迁移回归](test_cache_migration.py)。测试只创建临时合成数据，不改正式包文件、用户缓存或原始日志；候选复用正式源码中的 `codex_usage.tokens` 数学定义。13 项合成回归通过只证明已覆盖边界下的数据与故障行为，不改变性能门槛失败的结论。

[基准脚本](../../benchmark_cache.py)通过 `prepare --source <v3 快照路径>` 在 `.task-tmp/` 中建立隔离副本，再用 `run` 比较 v3 与本候选。
