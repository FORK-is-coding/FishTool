# FishTool watch 预算修复 · W4 回归取证与文档

> 归属：`07_FishTool_07_watch预算_证据核验与修复执行案.md` §5 / §10.1 / §11 / §12.2 / §13 W4。
> 版本：W4｜2026-10-05。W4 为收尾批次，**只做回归取证 + 文档**。
> 基线 HEAD：`86a0b29aa5622f9d4c34f8cd10611d57e1bbf963`（**未偏移**）。
> W4 **不 commit、不 push**；是否提交由叉子单独决定。

---

## 0. 一句话结论

- 本批修复**只动 L 层逻辑预算**（内存账本、重启清零），**H 层 HTTP 配额数字与语义一律未动**。
- 三项状态按执行案要求**分开陈述**，不得合成一句「配额已修复」：
  1. **【已实现】** 单请求单扣（L 层）；
  2. **【已启用】** 类别策略（L 层，`mode: partitioned`）；
  3. **【未实施】** HTTP 子额度（H 层，对应 §12 第二阶段，无独立审批不执行）。
- W4 未改 `modules/hotspot/watch_demand.py` 一行；未改 1800 / 1000 / 480 / 150 / 72 / 98 与 `ip_circuit_breaker`。

---

## 1. 代码基线与修改文件

### 1.1 HEAD 未偏移

```text
$ git rev-parse HEAD
86a0b29aa5622f9d4c34f8cd10611d57e1bbf963
```

与执行案审查提交一致。W4 期间 HEAD 未动。

### 1.2 工作区改动文件（W1—W3 成果，尚未提交）

下表为 W4 开始时的工作区状态；**这些生产/测试改动均来自 W1—W3**，W4 自身只新增/修改文档，不动生产代码。

```text
 config/budget.yaml                       |  40 ++            （纯追加 watch_scheduler 段）
 core/monitor_service.py                  |  30 +-
 modules/hotspot/collector.py             |  36 +-
 modules/hotspot/risk_control.py          | 789 ++++++++++++++++++++++-
 modules/hotspot/watch_service.py         | 552 +++++++++++++++++++---
 modules/hotspot/watch_store.py           | 163 ++++++-
 tests/test_core_monitor_service.py       |  32 ++
 tests/test_event_fast_cadence.py         |  45 +-
 tests/test_hotspot_budget_concurrency.py |  76 ++-
 tests/test_watch_batch2_fence_budget.py  | 345 ++++++++++++++-
 tests/test_watch_service.py              | 133 ++++++
 11 files changed, 2159 insertions(+), 82 deletions(-)
```

新增（untracked）测试文件：

```text
 tests/test_watch_logical_admission.py
 tests/test_watch_budget_integration.py
 tests/test_watch_scheduler_policy.py
```

`modules/hotspot/watch_demand.py` diff 为空（**未碰**，已验证）。

> 说明：`tests/test_event_fast_cadence.py` 的改动是**接口一致性**扩展——为 `RecordingCollector` 增补
> `collect_admitted` 转发，并给 `FakeBudget` 补齐 `peek/reserve/redeem/release_unused/clock`，
> 以适配新端口协议。**未削弱任何既有断言**，不属 H 层语义改动。

### 1.3 新增符号（diff 中 `+` 引入）

**`modules/hotspot/risk_control.py`**
- 异常：`InvalidLogicalAdmission`、`BudgetWiringError`、`WatchSchedulerConfigError`
- 数据结构：`LogicalAdmission`、`AdmissionResult`、`LedgerEntry`、`WatchSchedulerPolicy`
- 函数：`validated_issuer`、`_coerce_positive_int`、`_normalize_category_limits`、
  `_watch_scheduler_config_path`、`_compat_shared_policy`、`_parse_watch_scheduler_policy`、
  `load_watch_scheduler_policy`
- `WatchSchedulerPolicy.build_budget`
- `RequestBudget` 新增方法：`peek` / `reserve` / `redeem` / `release_unused` / `snapshot` /
  `_expire_reservations_locked` / `_window_used_locked` / `_category_used_locked` /
  `_freed_at_locked` / `_plan_ledger_locked`
- 常量：`WATCH_SCHEDULER_KEY`、`WATCH_SCHEDULER_MODE_PARTITIONED`、`WATCH_SCHEDULER_MODE_SHARED_ONLY`、
  `WATCH_SCHEDULER_MODES`、`DEFAULT_WATCH_FAIR_ORDER`、`WATCH_SCHEDULER_PENDING_NOTE`

**`modules/hotspot/watch_service.py`**
- `_TargetClaim`；`collect_admitted`（端口能力）；`_require_admission_capable_port`；
  `_admission_now_mono`；`logical_policy_snapshot`；`_run_tick_locked`；`_collect_category_candidates`；
  `_other_category`；`_interleave_by_rotation`；`_claim_short`；`_collect_evaluate_commit`

**`modules/hotspot/watch_store.py`**
- `find_due_for_budget_category`；`_normalize_due_cursor`；`_hotspot_watch_columns`；
  `_query_due_normal_fallback`

**`modules/hotspot/collector.py`**
- `collect_one(..., logical_admission=None)`、`_fetch_view(..., *, logical_admission=None)`；
  `self.budget` 改为显式 `is not None` 判定

**`core/monitor_service.py`**
- `build_watch_service` 未显式注入时改用 `load_watch_scheduler_policy().build_budget()`（唯一预算实例）

---

## 2. H 层未退化证据（红线：一个数字都不许改）

### 2.1 逐字核对 `config/budget.yaml` `quota` 段

| 配置项 | 现值（文件原文） | 要求 | 结论 |
|---|---|---:|---|
| `quota.global_limit` | 1800 | 1800 | **未动** |
| `quota.window_hours` | 24 | 24 | 未动 |
| `quota.retention_hours` | 25 | 25 | 未动 |
| `categories.discovery.limit` | 480（domain no_cookie） | 480 | **未动** |
| `categories.watch.limit` | 1000（domain cookie） | 1000 | **未动** |
| `categories.ranking.limit` | 150（domain cookie） | 150 | **未动** |
| `categories.maintenance.limit` | 72（domain cookie） | 72 | **未动** |
| `categories.flex.limit` | 98（domain cookie） | 98 | **未动** |
| `domains.cookie` | 900 / 7200 / 1.5 / 2 | 原样 | 未动 |
| `domains.no_cookie` | 900 / 7200 / 1.5 / 2 | 原样 | 未动 |
| `ip_circuit_breaker.cooldown_s` | 3600 | 3600 | **未动** |

### 2.2 diff 为纯追加（quota 段零增零删）

```text
$ git diff --numstat -- config/budget.yaml
40      0       config/budget.yaml

$ git diff -- config/budget.yaml
@@ -61,3 +61,43 @@ domains:
 # IP 级熔断：两域同时大量 412/403 时全局停采（两域共享同一 IP，必须一起停）
 ip_circuit_breaker:
   cooldown_s: 3600
+
+# ---------------------------------------------------------------------------
+# watch 逻辑调度策略（L 层，仅 modules/hotspot/risk_control.RequestBudget 消费）
...（新增 watch_scheduler 段 40 行）
```

- **40 insertions / 0 deletions**，唯一 hunk 为 `@@ -61,3 +61,43 @@`，全部落在 `ip_circuit_breaker`
  之后的追加区。
- `quota` 段（第 11—42 行）**零增零删**，逐字未动。

### 2.3 H 层语义未动（文档写明）

- `HttpQuotaBucket` 小时桶（表 `http_quota_buckets`，主键 `(domain, category, hour_bucket)`）**未动**；
- 五类 HTTP 上限、`global_limit=1800` **未动**；
- 分域冷却（412 冷却 / 连击翻倍 / 常规间隔 / 并发上限）**未动**；
- IP 级熔断 **未动**；
- signer / 无凭证路径 **未动**；
- 未把 `normal_watch` / `fast_watch` 新增成未受限的 HTTP category。

### 2.4 H 层相关测试（本轮实跑）

```text
$ python -m pytest \
    tests/test_quota_persistence.py tests/test_quota_risk_gate.py \
    tests/test_bilibili_request.py tests/test_bilibili_signer.py \
    tests/test_event_fast_cadence.py -q
76 passed in 13.31s
```

> 禁止把 1800 / 1000 改大来「让测试过」，也禁止改测试断言迁就实现——本批未发生。

---

## 3. L 分配策略记录（§10.1）

### 3.1 这是**新政策**，不是仓库原有常量

`config/budget.yaml` `watch_scheduler` 段的下面这组数字，是**本案（07 执行案 §10.1）新提出的 L 层业务策略**：

| 类别 | per_minute | per_hour | per_day |
|---|---:|---:|---:|
| `normal_watch` | 10 | 150 | 1500 |
| `fast_watch` | 8 | 120 | 1200 |
| `general` | 2 | 30 | 300 |
| **合计** | **20** | **300** | **3000** |
| **total** | 20 | 300 | 3000 |

- **不是仓库原有常量**，**不是实盘最优值**；
- 三类合计**恰等于 total**（20 / 300 / 3000），数值自洽、无超额；
- `general` 保留给「同一 collector 被独立调用时仍走 `acquire('general')`」，不属于轮转队列；
- 加载期强校验：每个窗口「各类上限之和 <= total」，越界即抛 `WatchSchedulerConfigError`。

### 3.2 维护者确认事实

- 原状态：`pending_maintainer_approval`（待维护者确认）；
- **叉子 2026-10-05 批准照该组走**，获批标记：`approved_maintainer_2026-10-05`；
- 该标记已写入 `config/budget.yaml`（段注释 + 三个类别各行）与
  `risk_control.WATCH_SCHEDULER_PENDING_NOTE`；
- **原 `pending` 字样保留供追溯**（常量名 `WATCH_SCHEDULER_PENDING_NOTE` 不改名，正文同时记录
  「初始 pending」与「2026-10-05 已批准」两段事实，不把提案数字伪装成既有常量）。

### 3.3 `mode: partitioned` 语义（以及 `shared_only` 兼容模式）

- `mode: partitioned`：**类别窗与总窗并行校验，任一不足即拒**（`partitioned` 已启用）。
- `mode: shared_only`：仅总窗、无类别隔离，属兼容模式，**必须显式 `category_isolation=false`**，
  **不得默认静默降级**。
- 缺文件 / 缺段：按**显式** shared_only 兼容运行并在状态里如实标 `category_isolation=false`；
  有本段但**非法**：加载期抛 `WatchSchedulerConfigError`（停 watch 采样），**绝不悄悄回 shared**。

### 3.4 生效口径

- 策略随 **watch 服务重建 / 应用重启** 生效（预算加载有 `lru_cache`）；
- `ConfigManager.reload` **不等于** `budget.yaml` 热更新；
- **不许借热重载清空 H 层 HTTP 账本**（`http_quota_buckets`）——本批只读 L 段。

---

## 4. CHANGELOG 三态（三条必须分开，不许合成一句「配额已修复」）

1. **【已实现】单请求单扣**：一次逻辑采集只消费一份 L 层额度，凭证只 `redeem` 一次，
   未兑换在 `finally` 里 `release_unused` 释放。
2. **【已启用】类别策略**：`normal_watch` / `fast_watch` / `general` 类别窗 + 类间轮转 +
   按类查询候选，`partitioned` 已启用。
3. **【未实施】HTTP 子额度**：H 层 watch 父额度仍由两类共享，**跨重启 / 跨进程的实际 HTTP 子预算
   本阶段没做**（对应 §12 第二阶段，无独立审批与迁移验收则不执行）。**绝不许写成已修复。**

另记录 W2 引入的 **`budget_skipped` 口径变化**：

- 旧口径：「扫描窗内目标数」；
- 新口径：「**被跳过的类别队首数**」（因每类读取上限 = `limit`）。

---

## 5. 诚实承诺与边界（§12.2）

> 逻辑请求只消费一次；normal / fast 按配置独立约束并公平选择。实际请求仍共同受 watch 父额度、
> 底层限频、冷却和网络限制。达到父额度后两类都会暂停，不承诺全天 SLA。

补充边界：

- 单请求单扣只约束 **L 层逻辑账本**，**不改变** `BilibiliAPICore._before_attempt` 的真实发包计数；
  一次业务逻辑操作在重试 / 指纹 / 签名下仍可能产生**多次**真实 HTTP（有测试
  `test_single_view_two_http_attempts_keep_layers_distinct` 钉死「L=1 / H=2」）。
- L 层账本**不持久**，进程崩溃即丢；H 账本**不退款、不重置**。
- 跨进程 / 跨实例的严格 exactly-once 不在本轮承诺内；本批以单 web worker 验收。

---

## 6. 未运行项（逐条列出）

1. **live 网络** —— 未发任何真实外网请求。
2. **真实 B 站接口** —— 全程 stub，未用真实账号 / Cookie。
3. **H 层真实发包** —— 未做真实 HTTP attempt 计数观测。
4. **桌面 Qt** —— 本轮未单独跑桌面交互（全量口径 B 已含桌面组，`QT_QPA_PLATFORM=offscreen`）。
5. **跨进程 / 跨重启持久** —— 未做跨重启 L 账本 / H 子预算验证（后者本阶段未实施）。
6. **旧反例复现脚本** —— 07 附件的无网络复现脚本未在 W4 重跑（该脚本会拒绝非基线 HEAD；
   其结论已由 §1.2 新测试固化）。

---

## 7. 测试命令与 passed 数（可复跑）

> 全部在仓库根执行；解释器：本机 Python 3.13 venv。

| 组 | 命令 | 结果 |
|---|---|---|
| H 层未退化 | `python -m pytest tests/test_quota_persistence.py tests/test_quota_risk_gate.py tests/test_bilibili_request.py tests/test_bilibili_signer.py tests/test_event_fast_cadence.py -q` | **76 passed**（13.31s） |
| W1—W3 聚焦 | `python -m pytest tests/test_watch_logical_admission.py tests/test_watch_budget_integration.py tests/test_hotspot_budget_concurrency.py tests/test_watch_batch2_fence_budget.py tests/test_watch_service.py tests/test_hotspot_collector.py tests/test_core_monitor_service.py tests/test_watch_scheduler_policy.py -q` | **116 passed**（17.69s） |
| 全量（口径 B，含桌面） | `set "QT_QPA_PLATFORM=offscreen"` 后 `python -m pytest -q` | **2359 passed / 2 skipped / 0 failed**（285.21s） |

2 skipped = `test_bilibili_contract_live.py`（live 默认跳）+ `test_discovery_smoke.py`（skipif），
**既有开关门，与本批无关**。

### 7.1 反例 → 修复后 的落点测试（按执行案九项归类）

**单扣 / 票据释放**
- `test_single_target_is_charged_once_not_twice`
- `test_selected_target_does_not_wait_at_second_gate`
- `test_default_minute_capacity_is_not_preconsumed_before_first_http`
- `test_real_factory_admission_chain_charges_once`
- `test_independent_collection_still_charges_exactly_once`
- `test_release_only_cancels_unused_and_redeemed_not_refunded`
- `test_reservation_timeout_auto_releases_and_expired_redeem_is_rejected`
- `test_replay_wrong_operation_key_and_wrong_issuer_are_rejected`
- `test_cross_task_redeem_succeeds_without_owner_task_check`

**类别隔离**
- `test_category_quota_full_leaves_other_category_usable`
- `test_shared_global_full_rejects_both_categories`
- `test_fast_category_does_not_starve_normal_of_minute_capacity`
- `test_partitioned_windows_check_parallel_in_real_budget`

**有限扫描（旧 `limit*8` 反例）**
- `test_bounded_scan_finds_eligible_normal_within_window`
- `test_fast_history_does_not_block_normal_selection`

**真实工厂 + collector 集成**
- `test_real_factory_wires_single_budget_instance_and_kwargs_priority`
- `test_real_factory_twenty_due_first_target_not_blocked`
- `test_real_factory_no_db_session_between_reserve_and_redeem`
- `test_independent_collect_one_without_ticket_still_charges_once`
- `test_real_factory_logical_observation_reports_partitioned`
- `test_capacity_deferral_is_not_platform_failure`

**配置策略**
- `test_real_repo_config_is_partitioned_and_self_consistent`（校验仓库真实配置自洽）
- `test_category_sum_over_total_raises`
- `test_shared_only_is_explicit_and_never_claims_isolation`
- `test_partitioned_missing_required_category_raises`

---

## 8. 交付九项对照（§13 必须交付）

| # | 必须交付 | 落点 |
|---|---|---|
| 1 | 代码基线与修改 commit | §1.1（HEAD 未偏移；本批未 commit，由叉子决定） |
| 2 | 逐文件 diff 及新增符号 | §1.2 / §1.3 |
| 3 | 旧反例输出与修复后对应输出 | §7.1（反例→落点测试映射；§3.3/§4.1/§4.3 为旧输出来源） |
| 4 | 单扣 / 票据释放 / 类别隔离 / 有限扫描测试 | §7.1 |
| 5 | 真实工厂 + collector 集成结果 | §7.1「真实工厂 + collector 集成」组 |
| 6 | H 层未退化证据 | §2 |
| 7 | 配置具体分配与维护者确认 | §3 |
| 8 | 未运行测试 / 外部实盘限制 | §6 |
| 9 | 回滚与已知边界 | §9 |

---

## 9. 回滚与已知边界

### 9.1 回滚

- **第一步（单扣）与第二步（类别分桶）可分提交回退**；
- 分桶策略回退到 `mode: shared_only` **不恢复双扣**，UI 如实显示降级（`category_isolation=false`）；
- **保留 H 账本 / 风控，绝不清表让额度重新可用**；
- 不以「关闭全部 watch」作为永久修复，只在配置非法或验收失败时临时暂停；
- 因本批**未 commit**，回滚即 `git checkout -- <file>` / 删除新增测试文件（但**须先经叉子确认是否提交**）。

### 9.2 已知边界

- L 层不持久，重启清零；H 层持久且不退款；
- 一次逻辑操作 ≠ 一次真实 HTTP（重试 / 签名 / 指纹会放大真实 attempt）；
- HTTP 子额度（父 watch 内为 normal/fast 预留）**未实施**；
- 跨进程 / 跨实例 exactly-once **未承诺**；
- 达到 H 父额度后两类都暂停，**无全天 SLA 承诺**。

---

*本文件为 W4 交付物之一；探针脚本/中间产物均放系统 Temp，交付后已自删，仓库根无遗留临时脚本。*
