"""01 正确排名：并发 / 预算 / 阶段编排与冻结（规格 §9）。

职责边界：
- 只编排：创建、抢占、采集（交给 collector）、生成榜单、冻结 hash、终结、取消、关闭；
- 排名纯计算全部委托 ``metrics``，采集全部委托 ``collector``；
- 只返回持久化结果，读取时**不访问 B 站**，也不重算历史名次；
- 取消 / 关闭**只处理本服务自己持有的任务**，绝不 close 共享 client（避免评论 / watch 断连）。
"""
from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import fields as dataclass_fields
from typing import Any, Callable, Dict, List, Optional

from core.logger import get_logger
from core.request_budget import AttemptBudget, RequestBudgetExceeded, current_attempt_budget

from .collector import RankingProfileCollector
from .contracts import (
    ALGORITHM_VERSION,
    SCHEMA_VERSION,
    BenchmarkPolicy,
    BenchmarkResult,
    CreatorSample,
    compute_snapshot_hash,
)
from .metrics import normalize_peer_uids, rank_one, reference_distribution
from .store import (
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_INTERRUPTED,
    STATUS_QUEUED,
    TERMINAL_STATUSES,
)

logger = get_logger(__name__)

#: 粉丝规模分桶（半开区间，规格 §5.3）
FOLLOWER_BANDS = (
    (0, 1000),
    (1000, 10000),
    (10000, 100000),
    (100000, 1000000),
    (1000000, None),
)

#: 默认实际 HTTP 尝试上限（与业务逻辑调用上限分开报告）
DEFAULT_MAX_HTTP_ATTEMPTS = 3000


def _follower_band(count: Optional[int]) -> Optional[tuple]:
    """返回粉丝数所属半开区间；缺失返回 None。

    Args:
        count: 粉丝数，可能为 None。

    Returns:
        Optional[tuple]: (low, high) 半开区间；high 为 None 表示上不封顶。
    """
    if count is None or isinstance(count, bool):
        return None
    for low, high in FOLLOWER_BANDS:
        if count >= low and (high is None or count < high):
            return (low, high)
    return None


def _policy_from_dict(raw: Any) -> BenchmarkPolicy:
    """把落库的 policy 字典还原为 BenchmarkPolicy（忽略未知字段）。

    Args:
        raw: 落库的 policy（dict 或 None）。

    Returns:
        BenchmarkPolicy: 还原后的策略对象。
    """
    data = raw if isinstance(raw, dict) else {}
    allowed = {item.name for item in dataclass_fields(BenchmarkPolicy)}
    return BenchmarkPolicy(**{key: value for key, value in data.items() if key in allowed})


class LogicalCallBudget:
    """业务逻辑调用上限（与 HTTP 尝试预算分离，规格 §8）。"""

    def __init__(self, max_logical_calls: int, deadline_monotonic: float):
        """初始化。

        Args:
            max_logical_calls: 逻辑调用上限（对应 policy.max_external_requests）。
            deadline_monotonic: 绝对截止时刻（monotonic 口径）。
        """
        self.max_logical_calls = int(max_logical_calls)
        self.deadline_monotonic = float(deadline_monotonic)
        self.logical_calls = 0

    def spend_logical(self) -> None:
        """记一次逻辑调用并检查预算。

        Returns:
            无。

        Raises:
            RequestBudgetExceeded: 超 deadline 或超逻辑上限时抛出。
        """
        if time.monotonic() >= self.deadline_monotonic:
            raise RequestBudgetExceeded('deadline_exceeded')
        if self.logical_calls >= self.max_logical_calls:
            raise RequestBudgetExceeded('logical_call_limit')
        self.logical_calls += 1


class BenchmarkService:
    """排名批次编排服务（单机单用户）。"""

    def __init__(
        self,
        api: Any,
        store: Any,
        clock: Optional[Callable[[], int]] = None,
        policy_config: Optional[Dict[str, Any]] = None,
    ):
        """构造服务。

        Args:
            api: B 站 API 客户端（共享实例，本服务**不**负责 close）。
            store: BenchmarkStore（短事务持久化）。
            clock: 可注入时钟（UTC Unix 秒）。
            policy_config: 运行期配置覆盖（如 max_http_attempts）。
        """
        self.api = api
        self.store = store
        self.clock = clock or (lambda: int(time.time()))
        self.policy_config = dict(policy_config or {})
        self._tasks: Dict[str, asyncio.Task] = {}
        self._lease_tokens: Dict[str, str] = {}
        self._progress: Dict[str, Dict[str, Any]] = {}

    # ------------------------------------------------------------------ 创建
    def create_run(
        self,
        target_uid: int,
        peer_uids: Any,
        policy: Any,
        extra_policy: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """创建并冻结一个 queued run。

        Args:
            target_uid: 目标账号 UID（严格正整数）。
            peer_uids: 原始同行 UID 列表（允许重复 / 含目标）。
            policy: BenchmarkPolicy 或等价 dict。
            extra_policy: 额外固化进 run.policy 的来源证据（如 discovery_evidence）；
                不参与策略语义，读取时被 ``_policy_from_dict`` 忽略。

        Returns:
            Dict[str, Any]: 落库后的 run 行字典。

        Raises:
            ValueError: UID 非法、名单为空、超上限或 exact_raw_tid 缺 raw_tid 时抛出。
        """
        target = self._require_uid(target_uid, 'target_uid')
        policy_obj = policy if isinstance(policy, BenchmarkPolicy) else _policy_from_dict(policy)
        if policy_obj.content_scope == 'exact_raw_tid' and policy_obj.raw_tid is None:
            raise ValueError('raw_tid_required')
        if policy_obj.content_scope == 'all_public_uploads' and policy_obj.raw_tid is not None:
            raise ValueError('unexpected_raw_tid')

        peers = normalize_peer_uids(target, [self._require_uid(uid, 'peer_uid') for uid in (peer_uids or [])])
        if not peers:
            raise ValueError('empty_peer_set')
        if len(peers) > 50:
            raise ValueError('too_many_peers')

        run_id = uuid.uuid4().hex
        as_of_s = int(self.clock())
        policy_dict = policy_obj.to_dict()
        if extra_policy:
            # 来源证据整体固化进 run（不新增来源表）
            policy_dict['discovery_evidence'] = dict(extra_policy)
        row = self.store.create_run(
            run_id,
            target_uid=target,
            policy=policy_dict,
            requested_peers=peers,
            selection_as_of_s=as_of_s,
            schema_version=SCHEMA_VERSION,
            now_s=as_of_s,
        )
        logger.info("[排名] 新建 run=%s target=%s peers=%s", run_id, target, len(peers))
        return row

    @staticmethod
    def _require_uid(value: Any, label: str) -> int:
        """校验 UID 为严格正整数（拒绝 bool / float / 字符串）。

        Args:
            value: 原始值。
            label: 报错字段名。

        Returns:
            int: 校验通过的 UID。

        Raises:
            ValueError: 非严格正整数时抛出。
        """
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f'invalid_{label}')
        return int(value)

    # ------------------------------------------------------------------ 启动
    def start_run(self, run_id: str) -> asyncio.Task:
        """在后台启动一次执行（返回 Task 供取消）。

        Args:
            run_id: run 标识。

        Returns:
            asyncio.Task: 执行任务。
        """
        task = asyncio.ensure_future(self.execute_run(run_id))
        self._tasks[run_id] = task

        def _forget(_finished: asyncio.Future) -> None:
            self._tasks.pop(run_id, None)

        task.add_done_callback(_forget)
        return task

    # ------------------------------------------------------------------ 执行
    async def execute_run(self, run_id: str) -> Dict[str, Any]:
        """抢占并执行一个 run，最后按 CAS 冻结结果。

        Args:
            run_id: run 标识。

        Returns:
            Dict[str, Any]: 终结后的 run 行字典；抢占失败时返回当前行。
        """
        token = uuid.uuid4().hex
        if not self.store.claim_run(run_id, token, now_s=int(self.clock())):
            # 已被别的 worker 抢占 / 已终态：不重复执行
            return self.store.read_run(run_id) or {}
        self._lease_tokens[run_id] = token
        run = self.store.read_run(run_id) or {}
        policy = _policy_from_dict(run.get('policy'))
        self._progress[run_id] = {'stage': 'collecting_target', 'progress': 5, 'message': '正在采集目标账号'}

        try:
            result = await self._collect_and_build(run, policy)
        except asyncio.CancelledError:
            # 只终结本 run；绝不关闭共享 client
            self.store.finish_run(run_id, token, status=STATUS_CANCELLED, stage='cancelled', now_s=int(self.clock()))
            self._progress[run_id] = {'stage': 'cancelled', 'progress': 100, 'message': '已取消'}
            raise
        except RequestBudgetExceeded as exc:
            # 预算耗尽：明确部分结果，不伪装成成功、也不重试换账号
            self.store.finish_run(
                run_id, token, status=STATUS_FAILED, stage='budget_exceeded',
                error_codes=[f'budget_exceeded:{exc}'], now_s=int(self.clock()),
            )
            self._progress[run_id] = {'stage': 'budget_exceeded', 'progress': 100, 'message': '请求预算耗尽'}
            return self.store.read_run(run_id) or {}
        except Exception as exc:  # noqa: BLE001 - 统一终结为 failed，保留错误码
            logger.exception("[排名] run=%s 执行失败", run_id)
            self.store.finish_run(
                run_id, token, status=STATUS_FAILED, stage='failed',
                error_codes=['execution_failed'], now_s=int(self.clock()),
            )
            self._progress[run_id] = {'stage': 'failed', 'progress': 100, 'message': str(exc)}
            return self.store.read_run(run_id) or {}

        ok = self.store.finish_run(
            run_id, token,
            result=result,
            snapshot_hash=result.get('snapshot_hash'),
            status=STATUS_COMPLETED,
            stage='finished',
            now_s=int(self.clock()),
        )
        self._progress[run_id] = {
            'stage': 'completed' if ok else 'finalize_failed',
            'progress': 100,
            'message': '结果已冻结' if ok else '结果写入失败',
        }
        return self.store.read_run(run_id) or {}

    async def _collect_and_build(self, run: Dict[str, Any], policy: BenchmarkPolicy) -> Dict[str, Any]:
        """采集目标 + 同行，构建并返回冻结结果（含 snapshot_hash）。

        Args:
            run: run 行字典。
            policy: 冻结策略。

        Returns:
            Dict[str, Any]: BenchmarkResult 的纯字典（已含 snapshot_hash）。
        """
        span = int(policy.max_observation_span_s or 7200)
        deadline_mono = time.monotonic() + span
        attempt_budget = AttemptBudget(
            max_attempts=int(self.policy_config.get('max_http_attempts', DEFAULT_MAX_HTTP_ATTEMPTS)),
            deadline_monotonic=deadline_mono,
        )
        logical_budget = LogicalCallBudget(policy.max_external_requests, deadline_mono)
        ctx_token = current_attempt_budget.set(attempt_budget)
        collector = RankingProfileCollector(self.api, logical_budget, self.clock)

        started_s = int(self.clock())
        run_id = run.get('id')
        as_of_s = int(run.get('selection_as_of_s') or started_s)
        target_uid = int(run.get('target_uid'))
        requested_peers: List[int] = list(run.get('requested_peers') or [])
        warnings: List[Dict[str, Any]] = []

        try:
            target_sample = await self._collect_one(collector, policy, target_uid, as_of_s)
            peer_samples: List[CreatorSample] = []
            for index, uid in enumerate(requested_peers):
                self._progress[run_id] = {
                    'stage': 'collecting_peers',
                    'progress': min(95, 5 + int(90 * (index + 1) / max(1, len(requested_peers)))),
                    'message': f'正在采集同行 {index + 1}/{len(requested_peers)}',
                }
                peer_samples.append(await self._collect_one(collector, policy, uid, as_of_s))
        finally:
            current_attempt_budget.reset(ctx_token)

        finished_s = int(self.clock())
        samples = [target_sample] + peer_samples
        return self._build_result(
            run_id=run_id,
            policy=policy,
            target_uid=target_uid,
            requested_peers=requested_peers,
            target_sample=target_sample,
            peer_samples=peer_samples,
            as_of_s=as_of_s,
            started_s=started_s,
            finished_s=finished_s,
            samples=samples,
            warnings=warnings,
        )

    async def _collect_one(
        self, collector: RankingProfileCollector, policy: BenchmarkPolicy, uid: int, as_of_s: int
    ) -> CreatorSample:
        """采集单个账号，任何失败都降级为 error 样本而不是中断整批。

        Args:
            collector: 采集器。
            policy: 冻结策略。
            uid: 账号 UID。
            as_of_s: 选稿参考时刻。

        Returns:
            CreatorSample: 采集样本。
        """
        try:
            return await collector.collect_creator(uid, policy, as_of_s)
        except RequestBudgetExceeded as exc:
            # 预算/期限耗尽：明确标记，不换账号绕限流
            return CreatorSample(uid=uid, status='error', stop_reason='budget_or_deadline',
                                errors=[{'code': 'budget_or_deadline', 'message': str(exc)}])
        except Exception as exc:  # noqa: BLE001
            logger.warning("[排名] uid=%s 采集失败: %s", uid, exc)
            return CreatorSample(uid=uid, status='error',
                                errors=[{'code': 'collector_error', 'message': str(exc)}])

    # -------------------------------------------------------------- 结果构建
    def _build_result(
        self,
        *,
        run_id: Any,
        policy: BenchmarkPolicy,
        target_uid: int,
        requested_peers: List[int],
        target_sample: CreatorSample,
        peer_samples: List[CreatorSample],
        as_of_s: int,
        started_s: int,
        finished_s: int,
        samples: List[CreatorSample],
        warnings: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """按规格 §6 / §7.1 生成冻结结果（榜单、并列、百分位、状态、hash）。

        Args:
            run_id: run 标识。
            policy: 冻结策略。
            target_uid: 目标 UID。
            requested_peers: 请求同行 UID。
            target_sample: 目标样本。
            peer_samples: 同行样本（与 requested_peers 同序）。
            as_of_s: 选稿参考时刻。
            started_s: 采集开始时刻。
            finished_s: 采集结束时刻。
            samples: 全部样本（target 在前）。
            warnings: 警告累积列表。

        Returns:
            Dict[str, Any]: 结果纯字典。
        """
        # 1) 规模筛选（可选）：开启后目标粉丝缺失 -> 该轮不能构造同规模组
        target_band = None
        if policy.size_match == 'same_follower_band':
            target_band = _follower_band(target_sample.follower_count)
            if target_band is None:
                warnings.append({
                    'code': 'size_match_target_unknown',
                    'message': '开启同规模筛选但目标粉丝数缺失，该轮不能构造同规模组',
                })

        valid_peers: List[CreatorSample] = []
        excluded: List[Dict[str, Any]] = []
        for uid, sample in zip(requested_peers, peer_samples):
            if sample.status != 'valid' or sample.score_twice is None:
                excluded.append({'uid': uid, 'reason': sample.status})
                continue
            if policy.size_match == 'same_follower_band':
                if target_band is None or _follower_band(sample.follower_count) != target_band:
                    excluded.append({'uid': uid, 'reason': 'follower_band_mismatch'})
                    continue
            valid_peers.append(sample)

        target_valid = target_sample.status == 'valid' and target_sample.score_twice is not None
        if policy.size_match == 'same_follower_band' and target_band is None:
            target_valid = False  # 无法构造同规模组
        valid_peer_count = len(valid_peers)
        requested_peer_count = len(requested_peers)

        # 2) 比较状态（固定优先级）
        if not target_valid:
            comparison_state = 'target_unavailable'
        elif valid_peer_count == 0:
            comparison_state = 'insufficient_peers'
        elif len(excluded) > 0:
            comparison_state = 'partial'
        else:
            comparison_state = 'complete'

        # 3) 观测跨度：> 7200s 则该组不排名（不自动排掉低分同行）
        span_exceeded = self._observation_span(samples) > int(policy.max_observation_span_s or 7200)
        if span_exceeded:
            warnings.append({
                'code': 'observation_span_exceeded',
                'message': '采集跨度超过 2 小时，该组不排名；仅展示账号事实',
            })

        peer_scores2 = [int(sample.score_twice) for sample in valid_peers]
        all_scores2 = ([int(target_sample.score_twice)] if target_valid else []) + peer_scores2

        # 4) 目标卡片
        target_card: Optional[Dict[str, Any]] = None
        if target_valid and not span_exceeded:
            ranked = rank_one(int(target_sample.score_twice), peer_scores2)
            target_card = self._entry_dict(target_sample, ranked, all_scores2, is_target=True)
        else:
            target_card = {
                'uid': target_uid,
                'metric_value': target_sample.metric_value if target_valid else None,
                'score_twice': int(target_sample.score_twice) if target_valid else None,
                'rank': None, 'rank_end': None,
                'total': len(all_scores2),
                'reference_count': valid_peer_count,
                'percentile': None,
                'percentile_method': None,
                'selected_count': target_sample.selected_count,
                'status': target_sample.status,
                'is_target': True,
            }

        # 5) 榜单：每行 competition rank 相对全 ranked 集合计算
        leaderboard: List[Dict[str, Any]] = []
        if not span_exceeded:
            if target_valid and target_card is not None:
                leaderboard.append(target_card)
            for sample in valid_peers:
                leaderboard.append(self._entry_dict(sample, None, all_scores2, is_target=False))
            # 稳定排序：score DESC，再按 uid ASC（UID 稳定排序只影响显示，不破坏并列）
            leaderboard.sort(key=lambda row: (-(row.get('score_twice') or 0), row.get('uid') or 0))

        ranked_count = (valid_peer_count + 1) if target_valid else valid_peer_count
        reference_dist = reference_distribution(peer_scores2) if peer_scores2 else None

        result = BenchmarkResult(
            schema_version=SCHEMA_VERSION,
            run_id=run_id,
            target_uid=target_uid,
            policy=policy.to_dict(),
            selection_as_of_s=as_of_s,
            observation_started_s=started_s,
            observation_finished_s=finished_s,
            comparison_state=comparison_state,
            scope='selected_creator_set',
            unit='creator',
            metric='median_cumulative_views',
            requested_peer_count=requested_peer_count,
            valid_peer_count=valid_peer_count,
            ranked_count=ranked_count,
            reference_count=valid_peer_count,
            excluded_peers=excluded,
            target=target_card,
            reference_distribution=reference_dist,
            leaderboard=leaderboard,
            warnings=warnings,
            snapshot_hash=None,
            algorithm_version=ALGORITHM_VERSION,
        ).to_dict()

        result['snapshot_hash'] = compute_snapshot_hash(
            schema_version=SCHEMA_VERSION,
            policy=policy,
            target_uid=target_uid,
            peer_uids=requested_peers,
            selection_as_of_s=as_of_s,
            creator_samples=samples,
            observation_started_s=started_s,
            observation_finished_s=finished_s,
            result=result,
        )
        return result

    @staticmethod
    def _entry_dict(
        sample: CreatorSample, ranked: Optional[Dict[str, Any]], all_scores2: List[int], *, is_target: bool
    ) -> Dict[str, Any]:
        """把样本 + 名次组装成榜单 / 目标卡片行。

        目标卡片直接采用 ``rank_one`` 的口径（无有效 peer 时 rank=None，不以 0 排序）；
        榜单行则相对全 ranked 集合重新计算 competition rank，不套用目标的 percentile。

        Args:
            sample: 账号样本。
            ranked: ``rank_one`` 的结果（仅目标使用）。
            all_scores2: 全 ranked 集合的 score_twice。
            is_target: 是否为目标账号。

        Returns:
            Dict[str, Any]: 行字典。
        """
        score2 = int(sample.score_twice)
        if is_target and ranked is not None:
            return {
                'uid': sample.uid,
                'name': sample.name,
                'metric_value': sample.metric_value,
                'score_twice': score2,
                'rank': ranked.get('rank'),
                'rank_end': ranked.get('rank_end'),
                'total': ranked.get('total', len(all_scores2)),
                'reference_count': ranked.get('reference_count'),
                'percentile': ranked.get('percentile'),
                'percentile_method': ranked.get('percentile_method'),
                'selected_count': sample.selected_count,
                'is_target': True,
            }
        higher = sum(1 for value in all_scores2 if value > score2)
        equal = sum(1 for value in all_scores2 if value == score2)
        return {
            'uid': sample.uid,
            'name': sample.name,
            'metric_value': sample.metric_value,
            'score_twice': score2,
            'rank': higher + 1,
            'rank_end': higher + equal,      # 含自己的并列上界
            'total': len(all_scores2),
            'selected_count': sample.selected_count,
            'is_target': is_target,
        }

    @staticmethod
    def _observation_span(samples: List[CreatorSample]) -> int:
        """计算保留集合的最早 / 最晚 observed_s 跨度（秒）。

        Args:
            samples: 全部样本。

        Returns:
            int: 跨度秒数；无观测时返回 0。
        """
        observed: List[int] = []
        for sample in samples:
            for video in sample.selected_videos or []:
                value = video.get('observed_s') if isinstance(video, dict) else None
                if isinstance(value, int) and not isinstance(value, bool):
                    observed.append(value)
            evidence = sample.source_evidence if isinstance(sample.source_evidence, dict) else {}
            value = evidence.get('observed_at_s')
            if isinstance(value, int) and not isinstance(value, bool):
                observed.append(value)
        if not observed:
            return 0
        return max(observed) - min(observed)

    # ------------------------------------------------------------------ 读取
    def read_result(self, run_id: str) -> Optional[Dict[str, Any]]:
        """只返回持久化快照（不访问 B 站、不重算统计）。

        Args:
            run_id: run 标识。

        Returns:
            Optional[Dict[str, Any]]: run 行字典或 None。
        """
        return self.store.read_run(run_id)

    def get_task_view(self, run_id: str) -> Optional[Dict[str, Any]]:
        """返回任务视图：数据库状态 + 运行中阶段（供轮询端点）。

        Args:
            run_id: run 标识。

        Returns:
            Optional[Dict[str, Any]]: 任务视图或 None。
        """
        row = self.store.read_run(run_id)
        if row is None:
            return None
        live = self._progress.get(run_id) or {}
        view = {
            'run_id': row.get('id'),
            'status': row.get('status'),
            'stage': live.get('stage') or row.get('stage'),
            'progress': live.get('progress', 100 if row.get('status') in TERMINAL_STATUSES else 0),
            'message': live.get('message'),
            'target_uid': row.get('target_uid'),
            'selection_as_of_s': row.get('selection_as_of_s'),
            'created_s': row.get('created_s'),
            'started_s': row.get('started_s'),
            'finished_s': row.get('finished_s'),
            'error_codes': row.get('error_codes') or [],
            'snapshot_hash': row.get('snapshot_hash'),
            'result': row.get('result'),
        }
        return view

    # ------------------------------------------------------------------ 重试
    def retry_as_new_run(self, old_run_id: str) -> Dict[str, Any]:
        """以新 as_of 新建 run 重试（绝不把新观测拼到旧 run）。

        Args:
            old_run_id: 旧 run 标识。

        Returns:
            Dict[str, Any]: 新 run 行字典。

        Raises:
            LookupError: 旧 run 不存在。
            RuntimeError: 旧 run 仍在运行。
        """
        old = self.store.read_run(old_run_id)
        if old is None:
            raise LookupError('run_not_found')
        if old.get('status') in (STATUS_QUEUED, 'running'):
            raise RuntimeError('run_active')
        return self.create_run(old.get('target_uid'), old.get('requested_peers') or [], old.get('policy') or {})

    # ------------------------------------------------------------------ 取消
    async def cancel_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        """取消本服务持有的同 run 任务并 await 结束；已终态不删除结果。

        Args:
            run_id: run 标识。

        Returns:
            Optional[Dict[str, Any]]: 取消后的 run 行字典；不存在时 None。

        Raises:
            RuntimeError: 运行已 completed（拒绝删除结果）。
        """
        row = self.store.read_run(run_id)
        if row is None:
            return None
        status = row.get('status')
        if status == STATUS_COMPLETED:
            raise RuntimeError('already_completed')
        if status in TERMINAL_STATUSES:
            return row  # 既有终态，原样返回

        task = self._tasks.get(run_id)
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001 - 取消失败不掩盖终态读取
                logger.warning("[排名] 取消 run=%s 时任务抛错", run_id)

        token = self._lease_tokens.get(run_id)
        if not token:
            # 非本服务持有：queued 直接抢占后取消，running 由其它 worker 负责
            token = uuid.uuid4().hex
            if self.store.claim_run(run_id, token, now_s=int(self.clock())):
                self.store.finish_run(run_id, token, status=STATUS_CANCELLED, stage='cancelled', now_s=int(self.clock()))
        else:
            self.store.finish_run(run_id, token, status=STATUS_CANCELLED, stage='cancelled', now_s=int(self.clock()))
        self._progress[run_id] = {'stage': 'cancelled', 'progress': 100, 'message': '已取消'}
        return self.store.read_run(run_id)

    # ------------------------------------------------------------------ 关闭
    async def shutdown(self) -> None:
        """取消并 await 本服务创建的任务；未终结的 run 持久化为 interrupted。

        只处理自己创建的任务，**不**取消或 close 由其它模块拥有的共享 client。

        Returns:
            无。
        """
        tasks = [(run_id, task) for run_id, task in self._tasks.items() if not task.done()]
        for _run_id, task in tasks:
            task.cancel()
        for _run_id, task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001
                logger.warning("[排名] shutdown 等待任务结束时抛错", exc_info=True)
        self._tasks.clear()
        self._lease_tokens.clear()
        try:
            self.store.mark_interrupted(now_s=int(self.clock()))
        except Exception:  # noqa: BLE001 - 关闭期数据库异常不应阻塞 Web 退出
            logger.warning("[排名] shutdown 标记 interrupted 失败", exc_info=True)
