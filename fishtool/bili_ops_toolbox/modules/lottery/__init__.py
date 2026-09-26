"""抽奖工具业务包。

提供视频/动态目标解析、评论复用采集、真人筛选和随机抽奖能力。
"""

# 对外暴露抽奖服务主类，业务方统一从包入口导入。
from .service import LotteryService

# 声明包级公开接口，避免 from * 时误导入内部模块。
__all__ = ["LotteryService"]
