"""01 正确排名：BenchmarkRun 短事务持久化（规格 §7.3 / §7.4）。

硬性约束（逐条落实）：
- 注入 session factory；每个方法各自开 / 提交 / 关闭 session，绝不跨网络请求持有事务；
- ``claim_run`` / ``save_creator_sample`` / ``finish_run`` / ``mark_interrupted``
  一律用 ``id + status + lease_token`` 条件 UPDATE 并检查 ``rowcount``，
  绝不做「SELECT 检查状态后无条件写」；
- ``save_creator_sample`` / ``finish_run`` 整体赋新 JSON，不原地 append；
- 已终态（completed / failed / cancelled / interrupted）拒绝追加样本或重写 result；
- ``mark_interrupted`` 同时清除 / 替换租约 token，使重启前的旧 worker 写入必然失败。
"""
from __future__ import annotations

import time
from typing import Any, Callable, Dict, Optional

from sqlalchemy import select, update

from core.database.models_benchmark import BenchmarkRun

# --------------------------------------------------------------------------
# 状态常量
# --------------------------------------------------------------------------
STATUS_QUEUED = 'queued'
STATUS_RUNNING = 'running'
STATUS_COMPLETED = 'completed'
STATUS_FAILED = 'failed'
STATUS_CANCELLED = 'cancelled'
STATUS_INTERRUPTED = 'interrupted'

#: 终态：不可再追加样本、不可重写 result
TERMINAL_STATUSES = (STATUS_COMPLETED, STATUS_FAILED, STATUS_CANCELLED, STATUS_INTERRUPTED)
#: 只有 queued 能被抢占
CLAIMABLE_STATUSES = (STATUS_QUEUED,)
#: 服务重启时需要清扫的未终结状态
NON_TERMINAL_STATUSES = (STATUS_QUEUED, STATUS_RUNNING)

#: read_run 返回的稳定列顺序
_RUN_COLUMNS = (
    'id', 'schema_version', 'status', 'stage', 'target_uid', 'policy',
    'requested_peers', 'selection_as_of_s', 'created_s', 'started_s',
    'finished_s', 'heartbeat_s', 'lease_token', 'creator_samples',
    'result', 'error_codes', 'snapshot_hash',
)


def _now(now_s: Optional[int] = None) -> int:
    """返回 UTC Unix 秒；显式传入时以传入值为准（便于测试）。

    Args:
        now_s: 可选时间戳（秒）。

    Returns:
        int: Unix 秒。
    """
    return int(time.time()) if now_s is None else int(now_s)


def _jsonable(value: Any) -> Any:
    """把 dataclass / 带 ``to_dict`` 的对象转成可 JSON 序列化的结构。

    Args:
        value: 任意对象。

    Returns:
        Any: 纯 dict / list / 标量结构。
    """
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    to_dict = getattr(value, 'to_dict', None)
    if callable(to_dict):
        return _jsonable(to_dict())
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def _row_to_dict(row: BenchmarkRun) -> Dict[str, Any]:
    """把 ORM 行转成纯字典（JSON 字段保持为已解析对象，不回查网络）。

    Args:
        row: BenchmarkRun ORM 实例。

    Returns:
        Dict[str, Any]: 稳定字段结构的字典。
    """
    return {name: getattr(row, name) for name in _RUN_COLUMNS}


class BenchmarkStore:
    """BenchmarkRun 的短事务读写入口。"""

    def __init__(self, session_factory: Callable[[], Any]):
        """注入 session factory。

        Args:
            session_factory: 无参可调用，返回一个 SQLAlchemy Session
                （例如 ``DatabaseManager.get_session``）。

        Raises:
            TypeError: session_factory 不可调用时抛出。
        """
        if not callable(session_factory):
            raise TypeError('session_factory_not_callable')
        self._session_factory = session_factory

    # ---------------------------------------------------------------- create
    def create_run(
        self,
        run_id: str,
        *,
        target_uid: int,
        policy: Any,
        requested_peers: Any,
        selection_as_of_s: int,
        schema_version: int = 3,
        stage: str = STATUS_QUEUED,
        now_s: Optional[int] = None,
    ) -> Dict[str, Any]:
        """插入一条 queued 状态的 run。

        Args:
            run_id: run 标识（主键）。
            target_uid: 目标账号 UID。
            policy: 冻结策略（BenchmarkPolicy 或等价 dict）。
            requested_peers: 请求的同行 UID 列表。
            selection_as_of_s: 选稿参考时刻（UTC Unix 秒）。
            schema_version: 结果契约版本。
            stage: 初始阶段名。
            now_s: 可选创建时刻。

        Returns:
            Dict[str, Any]: 落库后的 run 行字典。
        """
        moment = _now(now_s)
        session = self._session_factory()
        try:
            row = BenchmarkRun(
                id=run_id,
                schema_version=schema_version,
                status=STATUS_QUEUED,
                stage=stage,
                target_uid=target_uid,
                policy=_jsonable(policy),
                requested_peers=list(_jsonable(requested_peers) or []),
                selection_as_of_s=selection_as_of_s,
                created_s=moment,
                heartbeat_s=moment,
                creator_samples=[],
                error_codes=[],
            )
            session.add(row)
            session.commit()
            session.refresh(row)
            return _row_to_dict(row)
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    # ----------------------------------------------------------------- claim
    def claim_run(
        self,
        run_id: str,
        lease_token: str,
        *,
        now_s: Optional[int] = None,
        stage: str = 'claimed',
    ) -> bool:
        """以 CAS 抢占 queued run：置 running 并写入租约 token。

        条件为 ``id + status='queued' + lease_token IS NULL``；已 running / 终态
        的 run 无法二次抢占。

        Args:
            run_id: run 标识。
            lease_token: 抢占者持有的租约 token。
            now_s: 可选时刻。
            stage: 抢占后阶段名。

        Returns:
            bool: True 表示抢占成功（rowcount == 1）。

        Raises:
            ValueError: lease_token 为空时抛出。
        """
        if not lease_token:
            raise ValueError('lease_token_required')
        moment = _now(now_s)
        session = self._session_factory()
        try:
            result = session.execute(
                update(BenchmarkRun)
                .where(
                    BenchmarkRun.id == run_id,
                    BenchmarkRun.status == STATUS_QUEUED,
                    BenchmarkRun.lease_token.is_(None),
                )
                .values(
                    status=STATUS_RUNNING,
                    stage=stage,
                    lease_token=lease_token,
                    started_s=moment,
                    heartbeat_s=moment,
                )
            )
            if result.rowcount != 1:
                session.rollback()
                return False
            session.commit()
            return True
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    # ---------------------------------------------------------------- sample
    def save_creator_sample(
        self,
        run_id: str,
        lease_token: str,
        sample: Any,
        *,
        now_s: Optional[int] = None,
    ) -> bool:
        """追加 / 替换单个账号样本（整体赋新 JSON）。

        仅当 run 仍为 ``running`` 且 ``lease_token`` 匹配时写入；同一 uid 重复
        提交会整体替换旧样本（严格覆盖，不做 in-place append）。

        Args:
            run_id: run 标识。
            lease_token: 持锁 token。
            sample: CreatorSample 或等价 dict（必须含 uid）。
            now_s: 可选时刻。

        Returns:
            bool: True 表示写入成功。

        Raises:
            ValueError: lease_token 为空或样本缺少 uid 时抛出。
        """
        if not lease_token:
            raise ValueError('lease_token_required')
        sample_dict = _jsonable(sample)
        if not isinstance(sample_dict, dict) or sample_dict.get('uid') is None:
            raise ValueError('sample_missing_uid')
        sample_uid = sample_dict['uid']
        moment = _now(now_s)
        session = self._session_factory()
        try:
            # 读取当前样本仅用于计算「整份新 JSON」；写操作仍是带 token 的条件 UPDATE。
            current = session.execute(
                select(BenchmarkRun.creator_samples).where(
                    BenchmarkRun.id == run_id,
                    BenchmarkRun.status == STATUS_RUNNING,
                    BenchmarkRun.lease_token == lease_token,
                )
            ).scalar_one_or_none()
            if current is None:
                # 非运行中 / token 不符 / 已终态：拒绝写入
                session.rollback()
                return False
            samples = [
                item for item in (current or [])
                if not (isinstance(item, dict) and item.get('uid') == sample_uid)
            ]
            samples.append(sample_dict)  # 新列表对象 → 整体赋新 JSON
            updated = session.execute(
                update(BenchmarkRun)
                .where(
                    BenchmarkRun.id == run_id,
                    BenchmarkRun.status == STATUS_RUNNING,
                    BenchmarkRun.lease_token == lease_token,
                )
                .values(creator_samples=samples, heartbeat_s=moment)
            )
            if updated.rowcount != 1:
                session.rollback()
                return False
            session.commit()
            return True
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    # ---------------------------------------------------------------- finish
    def finish_run(
        self,
        run_id: str,
        lease_token: str,
        *,
        result: Any = None,
        snapshot_hash: Optional[str] = None,
        status: str = STATUS_COMPLETED,
        error_codes: Any = None,
        stage: str = 'finished',
        now_s: Optional[int] = None,
    ) -> bool:
        """以 CAS 终结 run：写 result / snapshot_hash 并置终态。

        条件为 ``id + status='running' + lease_token``；一旦进入终态，
        再次 finish 或追加样本都会被拒绝（迟到 token 写入失败）。

        Args:
            run_id: run 标识。
            lease_token: 持锁 token。
            result: 冻结结果（dataclass 或 dict）。
            snapshot_hash: 快照 SHA-256。
            status: 目标终态，必须是 completed / failed / cancelled / interrupted。
            error_codes: 错误码列表。
            stage: 终态阶段名。
            now_s: 可选时刻。

        Returns:
            bool: True 表示终结成功。

        Raises:
            ValueError: token 为空或 status 非终态时抛出。
        """
        if not lease_token:
            raise ValueError('lease_token_required')
        if status not in TERMINAL_STATUSES:
            raise ValueError('finish_status_must_be_terminal')
        moment = _now(now_s)
        values: Dict[str, Any] = {
            'status': status,
            'stage': stage,
            'finished_s': moment,
            'heartbeat_s': moment,
            'lease_token': None,       # 终结即释放租约，旧 token 不能再写
        }
        if result is not None:
            values['result'] = _jsonable(result)
        if snapshot_hash is not None:
            values['snapshot_hash'] = snapshot_hash
        if error_codes is not None:
            values['error_codes'] = list(_jsonable(error_codes) or [])
        session = self._session_factory()
        try:
            updated = session.execute(
                update(BenchmarkRun)
                .where(
                    BenchmarkRun.id == run_id,
                    BenchmarkRun.status == STATUS_RUNNING,
                    BenchmarkRun.lease_token == lease_token,
                )
                .values(**values)
            )
            if updated.rowcount != 1:
                session.rollback()
                return False
            session.commit()
            return True
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    # ------------------------------------------------------------------ read
    def read_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        """只读返回数据库快照。

        不通过 query 刷新统计，不访问网络；找不到时返回 None。

        Args:
            run_id: run 标识。

        Returns:
            Optional[Dict[str, Any]]: run 行字典或 None。
        """
        session = self._session_factory()
        try:
            row = session.get(BenchmarkRun, run_id)
            return None if row is None else _row_to_dict(row)
        finally:
            session.close()

    # ------------------------------------------------------------ interrupted
    def mark_interrupted(
        self,
        run_id: Optional[str] = None,
        *,
        now_s: Optional[int] = None,
        token_replacement: Optional[str] = None,
        stage: str = STATUS_INTERRUPTED,
    ) -> int:
        """把未完成 run 标为 interrupted 并清除 / 替换租约 token。

        服务重启时旧 worker 持有的 token 会因被清除 / 替换而失效，其后续写入
        必然失败；已终态 run 不受影响。

        Args:
            run_id: 指定单条；None 表示清扫所有非终态 run（重启场景）。
            now_s: 可选时刻。
            token_replacement: 替换后的 token；默认 None（清空）。
            stage: 终态阶段名。

        Returns:
            int: 受影响行数。
        """
        moment = _now(now_s)
        conditions = [BenchmarkRun.status.in_(NON_TERMINAL_STATUSES)]
        if run_id is not None:
            conditions.insert(0, BenchmarkRun.id == run_id)
        session = self._session_factory()
        try:
            updated = session.execute(
                update(BenchmarkRun)
                .where(*conditions)
                .values(
                    status=STATUS_INTERRUPTED,
                    stage=stage,
                    lease_token=token_replacement,
                    finished_s=moment,
                    heartbeat_s=moment,
                )
            )
            session.commit()
            return int(updated.rowcount or 0)
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()
