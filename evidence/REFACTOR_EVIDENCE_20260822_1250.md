# 重构回归证据

项目：`D:\tasks\cola\bili_ops_toolbox`
基线备份：`backups/FishTool_pre_final_20260822_0215/FishTool.exe`
正式包保护：本轮未写入 `dist/`。

| 阶段 | 本机时间 | 全量回归 | 证据摘要 |
|---|---|---|---|
| 基线 | 2026-08-22 12:50:48 | 10 passed, 1 warning | 真实工程可读；记录正式包 SHA-256 |
| 1 核心测试 | 2026-08-22 12:52:26 | 15 passed, 1 warning | 新增 5 项抽奖缓存、元数据补全和时间解析护栏 |
| 2 拆大文件 | 2026-08-22 12:54:30 | 20 passed, 1 warning | 新增 candidate/cache 边界；依赖扫描无循环 |
| 3 拆长函数 | 2026-08-22 13:00:58 | 20 passed, 1 warning | 元数据补全、候选过滤、公开画像采集拆为专注函数/模块 |
| 4 清理注释 | 2026-08-22 13:02:03 | 20 passed, 1 warning | 修复污染 docstring，删除 54 行逐行复述注释；保留合规和异常边界说明 |

说明：`test_core_behavior.py` 在 2026-08-22 12:53:43 出现在工作区，不是本轮写入；按不覆盖外部变更原则保留，并从阶段 2 起纳入全量回归，因此测试总数由 15 增为 20。

## 正式包哈希

- `dist/FishTool.exe`: `a35a17104252c62ac46922b12a290b61c4bcda3ae0715680a137843bf98df090`
- `dist/config/config.yaml`: `198f63ab6550dc905352570008de314deb8af6c1e2b94219a19a347e86545304`
- `dist/data/bili_ops.db`: `b78641c4653c75a5b6f93787f341a6d78b80dc6b577c94bb50f9d6baec91bad5`

以上三项前后完全一致。备份基线 `FishTool.exe` SHA-256 为 `f2d7aae28d84a15e9d8e2b66328a7b15b933e3fa9ee7d48774d2e9f0fa55da5a`，仅只读核对。
