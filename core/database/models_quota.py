"""
HTTP 尝试配额持久化模型（规格 §2.6：计数必须落库，进程重启不得清零）

本表是「HTTP attempt / 滚动 24h」口径的**唯一计数器**，替代任何进程内计数：

- ``domain``：凭证域（cookie / no_cookie），两域各自记账、各自退避；
- ``category``：配额类别（discovery / watch / ranking / maintenance / flex）；
- ``hour_bucket``：小时桶编号 = epoch 秒 // 3600；
- ``count``：该桶内已发生的真实 HTTP 尝试次数。

主键 ``(domain, category, hour_bucket)`` 同时充当唯一约束，
``core/quota_store.bump()`` 依赖它做 ``INSERT ... ON CONFLICT DO UPDATE`` 的 UPSERT，
因此同一桶内重复计数是「累加」而不是「新增一行」。

滚动 24h 的已用量 = 相邻 24 个小时桶求和；25 小时前的桶由 prune 裁掉。
"""
from sqlalchemy import Column, Integer, String

from .base import Base


class HttpQuotaBucket(Base):
    """HTTP 尝试配额的小时桶。

    一行 = 某个「(域, 类别) × 小时桶」内累计的尝试次数。
    不清零、不覆盖写，只做 UPSERT 累加与整行删除（裁剪）。
    """

    __tablename__ = 'http_quota_buckets'

    domain = Column(String(32), primary_key=True, nullable=False, comment='凭证域: cookie / no_cookie')
    category = Column(String(32), primary_key=True, nullable=False, comment='配额类别: discovery/watch/ranking/maintenance/flex')
    hour_bucket = Column(Integer, primary_key=True, nullable=False, comment='小时桶编号 = epoch 秒 // 3600')
    count = Column(Integer, nullable=False, default=0, comment='桶内已发生的 HTTP 尝试次数')


class DomainCooldown(Base):
    """分域冷却状态（规格 §2.4：两域各自冷却、互不连坐）。

    一行 = 一个凭证域的冷却状态，**必须落盘**（只放内存的话进程重启即绕过冷却，
    与「重启把 1800 变成 1800×N」是同一个漏洞）。

    语义（与 ``core.request_budget`` 的判定一致）：

    - ``cooldown_until_epoch <= now`` 表示该域已放行；
    - ``cooldown_until_epoch >  now`` 表示该域正在冷却，命中即拒发（reason=``domain:cooling``）；
    - ``consecutive_412`` 是「连续 412 次数」，**只有冷却期满后的首次成功请求才清零**
      （不得冷却一结束就重置，否则「连续三次翻倍」永远不会触发）。
    """

    __tablename__ = 'domain_cooldown'

    domain = Column(String(32), primary_key=True, nullable=False, comment='凭证域: cookie / no_cookie')
    cooldown_until_epoch = Column(Integer, nullable=False, default=0, comment='该域冷却截止时刻（epoch 秒）')
    consecutive_412 = Column(Integer, nullable=False, default=0, comment='连续 412 次数（冷却期满后首次成功才清零）')
    updated_epoch = Column(Integer, nullable=False, default=0, comment='本行最近更新时刻（epoch 秒）')


class HttpRiskEvent(Base):
    """风控事件流水（规格 §2.4 的 IP 级熔断滑窗依据）。

    一行 = 一次被判为风控的 HTTP 尝试（HTTP 412/403 或业务码 -412/-352）。
    用它做「10 分钟滑动窗内各域次数」统计，避免把滑窗状态塞进 ``domain_cooldown``
    （后者字段被规格固定为 4 列）。
    """

    __tablename__ = 'http_risk_events'

    id = Column(Integer, primary_key=True, autoincrement=True, comment='自增主键')
    domain = Column(String(32), nullable=False, index=True, comment='凭证域: cookie / no_cookie')
    status_code = Column(Integer, nullable=False, comment='风控码: 412 / 403 / -412 / -352')
    epoch = Column(Integer, nullable=False, comment='发生时刻（epoch 秒）')


class IpCircuitState(Base):
    """IP 级熔断状态（规格 §2.4：两域同时大量风控 -> 全局停采）。

    单行表（``key`` 固定 ``ip``）。**一旦被置开，只能由人工显式清除**
    （``core.request_budget.clear_ip_circuit()``），不随冷却到期自动恢复——
    因为两域共享同一 IP，误判恢复的代价是整条链路再次被平台拦截。
    """

    __tablename__ = 'ip_circuit_state'

    key = Column(String(16), primary_key=True, nullable=False, comment='熔断键，固定 ip')
    opened_epoch = Column(Integer, nullable=False, default=0, comment='本次熔断触发时刻（epoch 秒）')
    cooldown_until_epoch = Column(Integer, nullable=False, default=0, comment='最早可恢复时刻（仅供参考，实际须人工清除）')
    reason = Column(String(200), nullable=False, default='', comment='触发原因（可观测）')
    updated_epoch = Column(Integer, nullable=False, default=0, comment='本行最近更新时刻（epoch 秒）')
