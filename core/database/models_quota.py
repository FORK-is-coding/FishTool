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
