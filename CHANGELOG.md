# Changelog

本项目所有值得记录的变更都写在这里。

格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

> **v0.2.2 之前未逐条记录。** 更早版本只保留 tag：
> v0.1.0 / v0.2.0 / v0.2.1

## [0.2.2] - 2026-09-29

**主题：把「未知」和「真实的 0」分开。**

此前上游缺失的字段会被伪造成 `0`，下游把「没采到」当成「真的是 0」参与计算，
自诊的分位、均值、配对率因此系统性失真。这一版从采集、落库、算法到前端，
全线拆开这两个概念。

### 新增

- **`core/data_quality.py`** —— 严格的数值与时间契约
  - `parse_count` / `parse_ratio` / `to_epoch_s`，显式区分 `missing` / `invalid` / `ok`
  - 时间戳解析处理时区歧义（回拨重复时刻、不存在时刻）与无时区输入
- **`modules/hotspot/snapshot_store.py`** —— flush-only 快照写入器
  - 缺失指标写 SQL `NULL`，而不是落列默认值 `0`
  - 不自行提交事务，scope 与 transaction 归调用方
- `VideoStats` 新增 4 个可空元数据列：
  `captured_epoch_s`、`collection_tid`、`raw_tid`、`metric_status`
- 新增索引 `ix_video_stats_video_captured`

### 变更

- **`bilibili/api/user.py`** —— wrapper 在补默认值**之前**记录原始字段状态，
  返回体新增 `_meta.source` 与 `_meta.field_status`；旧兼容默认值保留，但缺失不再被 0 穿透
- **`modules/hotspot/collector.py`** —— 去掉 `_read_stat_int(...) or 0`，缺值不再落 0 入库
- **`web/routers/hotspot/routes_lifecycle.py`** —— 读端按质量优先级解析状态，
  新增 `inconsistent_quality`；坏点转为 `None` 同时保留 epoch，避免虚假的 -100% 与跨缺口平滑
- **`modules/lottery/cache.py`** —— 缓存 schema 升级到 v3：
  full / draw 双空间、TTL、seq 定序（迟到的旧结果不覆盖新结果）、原子写（临时文件 + `os.replace`）
- **账号自诊** —— 有效样本求均值、同稿配对分母、粉丝 `None` 与 `0` 不再输出伪 0 比例、采集覆盖度写入 payload
- **前端** —— `app.up.js` / `app.lottery.js` / `app.hotspot.js` 指标缺失显示为空值或三态，不再显示 0

### 修复

- 上游字段缺失被伪造成 0，连锁导致自诊百分位、均值、配对率失真
- 旧抽奖缓存全字段为 0 时，无法区分「未验证」与「真实为 0」

### 迁移说明

- `core/database/manager.py` 提供幂等 ALTER 与幂等建索引，旧库可原地升级
- **历史数据不回改**；新写入的缺失值以 `NULL` 表达
- 旧抽奖缓存不会自动转为有效值，统一标记 `legacy_unverified`，需重采或判定为 indeterminate

### 发布说明

- 写端新增 `NULL` 与旧读端语义不兼容，因此本版**整体发布**，不拆成部分更新
- 回滚时保留新增列与历史数据，不回滚成「缺失即伪 0」的旧行为

[0.2.2]: https://github.com/FORK-is-coding/FishTool/compare/v0.2.1...v0.2.2
