"""01 正确排名：BenchmarkRun 快照表模型（规格 §7.2）。

一张 ``benchmark_runs`` 表即可满足本版可复现审计：保存冻结策略、请求同行、
各账号采集样本与终态结果。

约定：
- ``creator_samples`` 只存必要事实，不保存 Cookie / header / 完整 LLM 响应；
- 终态 result 与样本不可原地改写，重新采集生成新 run；
- JSON 列必须整对象赋值（in-place append 不会自动落库）。
"""
from sqlalchemy import BigInteger, Column, Integer, JSON, String

from .base import Base


class BenchmarkRun(Base):
    """排名批次快照表。

    状态机：``queued → running → completed`` / ``failed`` / ``cancelled`` /
    ``interrupted``。只有持 ``lease_token`` 的 worker 能更新运行中的 run。
    """

    __tablename__ = 'benchmark_runs'

    id = Column(String(64), primary_key=True, comment='run 标识')
    schema_version = Column(Integer, nullable=False, default=3, comment='结果契约版本')
    status = Column(
        String(20), nullable=False, default='queued', index=True,
        comment='queued/running/completed/failed/cancelled/interrupted',
    )
    stage = Column(String(50), comment='当前阶段')
    target_uid = Column(Integer, nullable=False, comment='目标账号 UID')
    policy = Column(JSON, nullable=False, comment='冻结的排名策略')
    requested_peers = Column(JSON, nullable=False, default=list, comment='请求的同行 UID 列表')
    selection_as_of_s = Column(BigInteger, nullable=False, comment='选稿参考时刻（UTC Unix 秒）')
    created_s = Column(BigInteger, comment='创建时刻（UTC Unix 秒）')
    started_s = Column(BigInteger, comment='开始采集时刻（UTC Unix 秒）')
    finished_s = Column(BigInteger, comment='终结时刻（UTC Unix 秒）')
    heartbeat_s = Column(BigInteger, comment='心跳时刻，仅用于识别失联，不重算名次')
    lease_token = Column(String(64), comment='当前租约 token；仅持有者能更新该 run')
    creator_samples = Column(JSON, nullable=False, default=list, comment='各账号采集样本')
    result = Column(JSON, comment='冻结结果（含榜单 / 名次 / 百分位）')
    error_codes = Column(JSON, nullable=False, default=list, comment='错误码列表')
    snapshot_hash = Column(String(64), comment='快照 SHA-256，用于一致性核对（非签名）')
