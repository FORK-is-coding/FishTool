# Changelog

本项目所有值得记录的变更都写在这里。

格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

> **v0.2.2 之前未逐条记录。** 更早版本只保留 tag：
> v0.1.0 / v0.2.0 / v0.2.1

## [未发布]

**口径：热点生命周期双轨 —— 默认 `lifecycle_v2`，可切回 `heuristic_v1`；单视频持续追踪链（watch）固定走 v2。**

- `GET /api/hotspot/lifecycle` 与 `HotspotService` 门面的默认 `algorithm` 由 `heuristic_v1`
  切为 `lifecycle_v2`；热点页生命周期区块新增算法下拉（复用既有 `hotspot-collect-select`
  样式，会话级记忆），可一键对照 `heuristic_v1` 回放。
- 算法注册表新增 `lifecycle_v2` 注册；`create_detector()` 的**无参默认仍为 `heuristic_v1`**
  （仅注册表默认，与接口 / 门面默认解耦，既有契约测试压着）。
- 生命周期卡片按 `algorithm_version` **分流渲染指标**：v2 走 `relative_change` /
  `coverage_ratio` / `observed_windows` / `sample_count`，v1 仍走 `growth` / `up_count` /
  `observed_days` / `window_span_days`；趋势徽标的观测基数统一收敛为「有效观测单位」
  （v2 取 `observed_windows`，v1 取 `observed_days`/`days`），不再因键集不匹配而整体
  退化为 `0` /「数据不足」。
- `confidence` 第一版仍固定 `0.0` / `not_estimated`（本批不真算）；前端按 `confidence_kind`
  分流显示为「未估算」，不再展示 `0%`。
- `GET /api/hotspot/watch`（列表 / 详情 / 创建 / 释放）与事件工作台评估、机会响应统一补
  `algorithm_version`：watch 链路与事件窗口内核恒 `lifecycle_v2`，版本号写死取自各自链路，
  不经算法注册表。
- 热点页单视频跟踪列表上屏后端早已下发的 `last_confirmed_stage` / `coverage_ratio` /
  `coverage_state`（纯前端展示，字段缺失时留空，不伪造 `0`）。
- 常驻 watch 循环装配补齐默认预算门（`RequestBudget`）与事件需求整编 hook
  （`EventWatchDemandReconciler.reconcile`）；缺省采集端口复用编排层同一 `RequestBudget`。

### watch 预算修复（07 执行案 W1—W4）——三项状态**必须分开看**

> 口径前置：本批只修 **L 层逻辑预算**（内存账本、重启清零），
> **H 层 HTTP 配额数字与语义一律未动**（1800 与五类 480 / 1000 / 150 / 72 / 98 逐字不变）。
> **不许**把下面三条合成一句「配额已修复」。

**【已实现】单请求单扣（L 层）**

- 一次逻辑采集只消费**一份** L 层额度：watch 调度 `reserve` 一次，采集以 `redeem` 兑换**同一份**票据，
  不再走第二次 `acquire`。
- 凭证只兑换一次（issuer + operation_key + reserved 状态三项校验）；未兑换的在 `finally` 里
  `release_unused` 释放；已兑换不退款（逻辑尝试，不等同实际发送数）。
- 预留带硬超时（`reserve_deadline_mono`），超时清扫兜底，超时后 `redeem` 返回 `admission_expired`。

**【已启用】类别策略（L 层，`mode: partitioned`）**

- `config/budget.yaml` 新增 `watch_scheduler` 段（**纯追加，不改 quota 任何数字**）：
  `normal_watch` / `fast_watch` / `general` 类别窗 + 类间轮转 + 按类查询候选；`partitioned` 已启用。
- 分配数值：10/8/2、150/120/30、1500/1200/300，为 §10.1 提案值，**维护者 2026-10-05 批准照该组走**
  （`approved_maintainer_2026-10-05`；原 `pending_maintainer_approval` 字样保留供追溯）。
  三类各窗口合计恰等于 total（20 / 300 / 3000），自洽无超额；**非仓库既有常量、非实盘最优值**。
- `mode: shared_only` 为兼容模式，**必须显式 `category_isolation=false`**，不默认静默降级。

**【未实施】HTTP 子额度（H 层）**

- H 层 watch 父额度（1000）仍由 normal / fast 两类**共享**；跨重启 / 跨进程的实际 HTTP 子预算
  **本阶段没做**（对应执行案 §12 第二阶段，无独立审批与迁移验收则不执行）。
- **不得**表述为「已修复」或「HTTP 已分桶」。

**观测口径变化（W2 引入）**

- `budget_skipped` 语义变更：从「扫描窗内目标数」改为「**被跳过的类别队首数**」
  （因每类读取上限 = `limit`）。

**验证**

- 全量 **2359 passed / 2 skipped / 0 failed**（285.21s，口径 B 含桌面）；
  H 层聚焦 **76 passed**；W1—W3 聚焦 **116 passed**。
- 详见 `docs/watch_budget_regression_2026-10-05.md`。

**尚未验证**

- live 网络、真实 B 站、H 层真实发包计数、跨进程 / 跨重启持久；桌面 Qt 交互单跑未做。

## [0.2.4] - 2026-10-02

**主题：02 单视频时序跟踪上线；06 发现通道接通常驻调度与 watch 候选入池。**

这一版把「发现 → 跟踪」串成闭环：06 的聚合入口广度发现接入常驻调度，其中
`popular` / `ranking_all` 的条目自动成为 watch 候选；02 侧补齐单视频时序采样的
算法、落库、编排、Web 端点与常驻循环。两条新增常驻任务**默认关闭**，
不显式配置则不会采样、不会占用配额，行为与上一版完全一致。

### 新增

**02 · 单视频时序跟踪**

- **`modules/hotspot/watch_service.py`** —— 编排层。一轮 tick 顺序为
  先清 → 捞 → 领 → 采 → 评 → 写 → 排；同一轮单目标只有一条事务，任一步失败即
  rollback 并计入失败隔离
- **`modules/hotspot/watch_store.py`** —— watch 表读写；`upsert_watch` 幂等，
  已存在的目标不被重置
- **`core/database/models_hotspot_watch.py`** —— `hotspot_watch` 表
- **`modules/hotspot/algorithm/lifecycle_v2.py`** —— 单视频时序判定算法
- **`web/routers/hotspot/routes_watch.py`** —— `GET /watch`、`GET /watch/{bvid}`、
  `POST /watch`、`POST /watch/{bvid}/release`
- **前端 `index.html`** —— watch 管理区块（列表 / 三态筛选 / 新增 / 释放）

**06 · 接线**

- **`modules/hotspot/discovery/watch_ingest.py`** —— 候选入 watch 池的桥接层，
  只吃 `popular` / `ranking_all` 两个来源；读的是内存结果，**不发 HTTP**

### 变更

- **`modules/hotspot/algorithm/base.py`** —— `Detection` 新增 `metadata` 通道，
  与约定「仅数值或 None」的 `metrics` 分离，二者键集互斥
- **`modules/hotspot/algorithm/lifecycle_v2.py`** —— `coverage_state` 移出 `metrics`，
  与 `confidence_kind` 一并走 `metadata`；自此 `metrics` 为**纯数值通道**
- **`modules/hotspot/discovery/service.py`** —— `run_discovery_loop` 新增 `on_snapshot`
  回调（sync / async 均支持；不传则行为与改动前逐字一致）；`list_keyword_candidates()`
  未动
- **`core/monitor_service.py`** —— 常驻任务扩至 4 条

### 常驻任务与开关

| 任务 | task 名 | 开关 | 默认 |
| --- | --- | --- | --- |
| 评论增量采集 | `bili-comment-monitor` | `monitor.enable` | 关 |
| Cookie 巡检 | `bili-cookie-check` | 无（总是起） | — |
| watch 时序采样 | `bili-watch` | `monitor.watch_enable` | **关** |
| 发现轮询 | `bili-discovery` | `monitor.discovery_enable` | **关** |

入池不快照 discovery 单独开关：它不发请求、不花钱，跟着 `discovery_enable` 走；
真正花钱的采样由 `monitor.watch_enable` 单独把关，两个开关职责不重叠。

### 指标口径

- `confidence_kind` 与 `coverage_state` 属非数值项，走 `Detection.metadata`，
  不得写进 `metrics`（`metrics` 全键皆为数值或 `None`，前端可直接绘图）
- watch 三态 `active` / `expired` / `manual_stop`：手动停追只由 release 端点写
  `manual_stop`，与自动到期分离
- fencing 用 `state_revision` 当代际：写回代际不匹配即丢弃，不写任何列

### 验证

- 全量测试 **1871 passed / 1 skipped / 0 failed**
- `on_snapshot` 不传时行为与改动前逐字一致（有对照用例）
- 候选入池零 HTTP（有测试钉死）
- 默认配置下无任何路径拉起 watch / discovery 循环（各有反向用例兜底）

### 尚未验证

- **06 → 04 的消费接口仍未接线** —— `DiscoveryService.list_keyword_candidates()`
  已备好但零调用点，接 04 需改动 04 代码。
  即 v0.2.3 记的「06 与 04 的消费接口尚未接线」在本版收掉一半：**06 → 02 已接**
- 真实 B 站接口 —— 本版测试全程 stub
- watch 循环实盘节奏 —— 未跑满 24h 观察 `watch` 类配额 1000 的实际占用

### 发布说明

- 两条新增常驻任务均默认关闭；不配置则行为与本版之前完全一致
- 新增 `hotspot_watch` 表，`manager.py` 幂等建表，旧库可原地升级；历史数据不回改
- 回滚只需关掉 `monitor.watch_enable` / `monitor.discovery_enable`，或切回旧界面

## [0.2.3] - 2026-10-02

**主题：配额账本落地（唯一账本 + 总闸挪位）与 06 采集广度发现通道；另含 01 排名方案 A。**

这一版把 HTTP 尝试配额从「内存里的计数器」变成**落盘、可重启续算的唯一账本**，
并新增**免 Cookie 的聚合入口发现通道**（06）——广度靠读、深度靠采，不再自采全站。
三条线相互独立，均不改变既有采集链路的调用方式。

### 新增

**01 · 账号排名（方案 A 阶段性）**

只新增一个排名子包和一张快照表，不改既有采集链路，不引入独立部署。
排名是**明确参评集合内**的账号对账号排名，不是全站排名，也不代表抽样代表性。

- **`modules/self_diagnosis/benchmark/`** —— 排名子包
  - `contracts.py` —— `BenchmarkPolicy` / `CreatorSample` / `BenchmarkResult`（schema=3）
  - `metrics.py` —— `median_twice` / `rank_one`，用 `score_twice` 整数比较，避免浮点导致并列误判
  - `collector.py` —— 只采排名必需字段，不跑整套自诊；含 `discover_candidates`
  - `store.py` —— `BenchmarkRun` 短事务，`id+status+lease_token` 条件 UPDATE 并检查 rowcount
  - `service.py` —— 编排、冻结、重试、取消，以及 2 小时观测窗约束
- **`core/database/models_benchmark.py`** —— `BenchmarkRun` 快照表，已从 `core/database/__init__.py` 导出
- **`core/request_budget.py`** —— ContextVar 请求预算，默认 `None`，不改变旧调用行为
- **`web/routers/benchmark.py`** —— `/api/analysis/benchmark` 候选发现、发起、查询、取消、重试
- **`web/local_guard.py`** —— 本机写端点校验（同源 + CSRF + session token）

**配额账本**

- **`config/budget.yaml`** —— 配额唯一账本（单账号总闸 1800 attempt / 滚动 24h）
  - 五类配额：`discovery` 480 / `watch` 1000 / `ranking` 150 / `maintenance` 72 / `flex` 98
  - 分域冷却与节流：`cookie` / `no_cookie` 两域各自独立，互不连坐
  - 硬约束：业务与基础设施代码不得再出现硬编码配额数字
- **`core/quota_store.py`** —— 小时桶窗口统计与 `http_attempt_log` 读写
- **`core/database/models_quota.py`** —— `http_attempt_log` 模型

**06 · 采集广度**

- **`modules/hotspot/discovery/`** —— 聚合入口发现通道
  - `contracts.py` —— `BroadKeyword` / `BroadVideo` DTO、质量三态、来源优先级、按 bvid 合并
  - `sources.py` —— 三源读取与容错解析（纯 I/O，不含聚合逻辑）
  - `store.py` —— 关键词与视频信号写入器
  - `snapshot.py` —— 全局发现快照（原子 JSON，含更新与保留协议）
  - `service.py` —— 调度、共享轮询缓存、去重落库、快照冻结
- **`HotKeywordSignal`**（表 `hot_keyword_signal`）—— 热搜词观测事实表，唯一键 `(keyword, captured_epoch_s)`
- `tests/data/discovery/sources_fixtures.json` —— 三源离线 fixture
- `tools/discovery_smoke.py` —— 一次性低频联网冒烟（不发送 Cookie）

### 变更

- **`bilibili/api/client.py` / `signer.py`** —— 在真实 HTTP 发送点接预算钩子；`RequestBudgetExceeded` 保留类型，不被包装成可重试的普通 API 错误
- **`web/main.py`** —— 装配 `BenchmarkService`；清理改 `try/finally`，只关闭自己拥有的 client
- **`web/routers/analysis.py`** —— 自诊与导出可带 `benchmark_run_id`，读取同一份冻结结果，不重新触发采集
- **`report_generator.py`** —— 新增 `creator_ranking` 段；keyword-only 参数，旧位置参数不受影响
- **前端** —— `app.up.js` / `index.html` / `style.up.css` 增加同行名单、指标说明、发起/取消/重试

**配额账本**

- **`core/request_budget.py`** —— 配额判定接入落盘账本；**总闸校验从请求路径挪到配置加载期**
  （分类配额本就是硬上限，留在请求路径上是死代码）
- **`core/database/manager.py`** —— 幂等建表，旧库可原地升级
- **`core/monitor_service.py`** —— 启动时读回窗口，重启后配额续算

**06 · 采集广度**

- **`core/database/models_hotspot_signal.py`** —— 新增 `HotKeywordSignal`
- **`core/database/__init__.py`** —— 显式导入注册（不注册 `create_all` 不建表）
- **`.gitignore`** —— `data/` 收窄为 `/data/`。此前裸 `data/` 连带忽略 `tests/data/`，
  以致测试 fixture 从未进过仓库，clone 后 fixture 缺失

### 指标口径

`recent10_age7_30_median_views_v1`：请求时刻往前 30 天内、稿龄 7—30 天的最近最多 10 条
公开稿件，取**中位累计播放**；至少 3 条；选中稿件缺失不打替补，该作者本轮不参与自动排名。
播放取自同次详情请求的 `stat.view`，真实 0 有效。名次为 competition rank，并列同名次。

**06 · 采集广度（易错点，写死）**

- `ranking` 全站榜**不复用** `get_ranking()`：其实现强制带 `day` + `pn`，实测三种组合全部 `-352`；
  改为裸 URL 并单独覆写 `Referer` 为 `.../v/popular/rank/all` 才通。**属易变项**，由冒烟脚本持续验证。
- 三源 parser 统一吃「已拆 data + 补 code 的统一 envelope」，逐源在 docstring 写明内层路径
  （`data.trending.list` / `data.list` / `data.list` + `others`）。
- 同一视频按 bvid 去重，**发现来源全部保留**；展示值按固定来源优先级
  `ranking_all > popular > ranking_all_others` 取值，冲突打标记，**不按最大播放量挑**
  （会引入向上选择偏差）。
- `heat_score` 只作「平台接口返回热搜分数」，**不进入播放增量计算**；
  缺失记 `NULL` / `missing`，非法值跳过且不写 0。
- `pid_v2` / `tidv2` / legacy `tid` 三套分类字段并存，不合并成单一 tid。
- 失败状态落**全局发现快照**，可区分四种情形：本轮真空榜 / 请求失败 / 仅第一页成功 / 用的是上轮缓存。

### 验证

- 全量测试 1586 passed / 0 failed（本机未装 PyQt5，桌面组四个文件排除在外）
- 数值验收：`rank=4 / rank_end=5 / total=6 / percentile=30`；全等并列 1—N 且 P50；
  单 peer 有名次无百分位；peer 列表含目标 UID 重复时只算一次
- `tools/verify_creator_ranking.py` 端到端 16 步全绿
- **本版发布前全量复跑：1672 passed / 1 skipped / 0 failed**（含桌面组全部文件）
- 配额持久化：独立复跑 17 passed；总闸判定挪位后行为不变
- 06 联网冒烟（三入口各 1 次，含礼貌间隔，不发送 Cookie）：
  `search/square` code=0 / 10 条、`popular` code=0 / 20 条、
  `ranking/v2?rid=0` code=0 / 主榜 100 条 + `others` 5 条

### 尚未验证

- 真实 B 站接口 —— 本轮全程 stub，未使用真实账号与 Cookie
- 浏览器 UI 交互 —— JS 仅通过 `node --check` 并与后端契约对齐
- PDF 真实格式 —— 本机未安装 pdfkit / wkhtmltopdf
- **06 的易变项** —— `ranking` 端点的 `Referer` 要求由平台侧决定，本次实测通过不代表长期可用，
  由 `tools/discovery_smoke.py` 持续验证
- **06 与 04 的消费接口尚未接线** —— `DiscoveryService.iter_video_candidates()` /
  `list_keyword_candidates()` 已备好，但接入 04 需改动 04 代码，本版未做
- **06 的请求节奏实盘表现** —— 仅做过一次性冒烟，未跑满 24h 观察 discovery 480 的实际占用

### 发布说明

- 本 tag 覆盖 v0.2.2 之后的全部提交，含此前以阶段性提交合入 main、未单独打 tag 的 01 排名方案 A
- 配额账本引入落盘表 `http_attempt_log`，`manager.py` 提供幂等建表，旧库可原地升级
- 06 的发现通道在接 04 之前**不影响**既有链路：新模块无人调用，调度需显式启动
- 回滚只需切回旧界面或关闭新功能，不影响既有自诊、词云与抽奖
- 回滚配额改动时注意：旧库中 `http_attempt_log` 可保留不用，不影响旧行为

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
[0.2.3]: https://github.com/FORK-is-coding/FishTool/compare/v0.2.2...v0.2.3
[0.2.4]: https://github.com/FORK-is-coding/FishTool/compare/v0.2.3...v0.2.4
