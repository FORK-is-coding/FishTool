"""热点算法的领域阈值配置。"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any


@dataclass(frozen=True)
class HeuristicConfig:
    """启发式算法阈值，按领域分桶。"""

    p_candidate: float = 90.0
    p_appear: float = 95.0
    p_decline: float = 50.0
    base_view: int = 10_000
    window: int = 7

    def as_dict(self) -> dict[str, float | int]:
        """返回配置字典，供接口和日志使用。"""
        return asdict(self)


DOMAIN_CONFIGS: dict[str, HeuristicConfig] = {
    "game": HeuristicConfig(base_view=10_000),
    "游戏区": HeuristicConfig(base_view=10_000),
    "default": HeuristicConfig(),
}


def get_config(domain: str | None = None) -> HeuristicConfig:
    """按领域读取配置，未知领域使用默认配置。"""
    return DOMAIN_CONFIGS.get(domain or "default", DOMAIN_CONFIGS["default"])


# ----------------------------------------------------------------- v2 领域分桶
#
# 与 v1 ``DOMAIN_CONFIGS`` 同构：一张 `domain -> 配置` 表 + 一个按域取值口
# :func:`get_lifecycle_v2_config`（对应 v1 的 :func:`get_config`）。唯一差别是 v2 的
# ``LifecycleV2Config`` 类定义留在 ``lifecycle_v2.py``（本轮不换家，``__all__`` 与既有
# import 语句均不动），故本模块以**覆盖项**（纯 kwargs dict）承载「各域相对默认值的
# 差异」，由取值口内延迟导入 ``LifecycleV2Config`` 实例化，从根上避开
# ``config <-> lifecycle_v2`` 的模块级循环 import。
#
# 桶内为空 = 该域沿用 ``LifecycleV2Config`` 的 dataclass 默认值（现值），故当前
# 五个桶的语义与改前逐字节一致；后续要按域调阈值，往对应桶里加键即可，
# 未列出的键仍回落到 dataclass 默认值。

LIFECYCLE_V2_DOMAIN_OVERRIDES: dict[str, dict[str, Any]] = {
    "game": {},
    "游戏区": {},
    "default": {},
    "anime": {},
    "paint": {},
}


# ----------------------------------------------------------------- 用户调参指南
#
# 上面这张表是给分发用户留的**自助调参口**。五个桶现在全是空的，意思是所有领域统一
# 沿用 ``LifecycleV2Config`` 的默认阈值——这与 v2 上线时的行为逐字节一致，是刻意选的
# 保守默认，**不是漏配**。手上没有实测数据之前，请不要凭感觉往里填数。
#
# 什么时候该往桶里填？下面四种都是可从日志观测到的异常，出现哪种就调对应项：
#
# 1. 某领域「误报」——候选量虚高，看着像热点其实没热度。多半是该域流量基数偏低，
#    小波动就把相对阈值顶爆了。
#    → 调 ``absolute_delta``（抬高绝对增量门槛）、``low_base``（抬高低基数保护线，
#      超过它才切相对判定），或 ``relative_delta``（抬高相对增幅比例）。
#
# 2. 某领域「漏报」——该域真出了热点却没被检出。典型是长尾慢热的领域，一个窗口内
#    涨不到默认的 20%。
#    → 调 ``window_seconds``（放宽观测窗口）、``confirmation_windows``（减少连续确认
#      轮数）、``emerge_age_days``（放宽"新出现"的时间判定）。
#
# 3. 某领域「数据不足」——日志里成片出现"数据不足"、阶段判不出来。冷门领域投稿稀疏、
#    采样点太少，覆盖率和分位数样本量两道门槛过不去。
#    → 调 ``min_window_coverage``（下调窗口覆盖率下限）、``percentile_min_n``（下调
#      分位数最小样本数）。
#
# 4. 领域节奏不同（脉冲 vs 长尾）——游戏区跟着版本、皮肤、联动走，是脉冲式，一天爆发
#    一天熄火；同人绘画区是长尾，慢涨慢退。
#    → 游戏区收紧 ``max_staleness_seconds``，让失效判定更快，别把已经凉了的还挂在榜上；
#      绘画区反向放宽 ``max_staleness_seconds`` 与 ``window_seconds``，别过早判死。
#
# 填的时候三条注意事项：
#
#   ① **一次只动一个领域、一个参数**，改完观测一轮再动下一个。一次全调会分不清是哪个
#      参数起的作用，出了问题也回不去。
#   ② **先让它自然跑一段时间**（建议至少两周），按领域统计候选量、命中率、被覆盖率卡掉
#      的比例，用数据决定改哪个，别靠猜。
#   ③ **只要任意一个桶非空，就必须把 ``lifecycle_v2.py`` 里的 ``threshold_version``
#      从 ``"lifecycle_v2_defaults_1"`` 升一位**（如 ``_2``）。这个字段就是为分桶后的
#      版本追踪准备的：全空时它代表"纯默认"，一旦有领域覆盖就必须能区分是第几版口径，
#      否则线上报出的问题无法定位是哪套阈值产生的。
#
# 示例（**仅示意写法，数值请按自己的数据定，不要照抄**）：
#
#     "game":  {"max_staleness_seconds": 86_400},   # 游戏区脉冲，判死快一点
#     "paint": {"window_seconds": 172_800},         # 绘画区长尾，窗口放到两天
#
# 键名必须与 ``LifecycleV2Config`` 的字段名完全一致，未列出的键自动回落到 dataclass
# 默认值；键名写错会在实例化时直接抛 ``TypeError``，不会静默忽略。


def get_lifecycle_v2_config(domain: str | None = None) -> "LifecycleV2Config":
    """按领域读取 v2 阈值配置；未知领域回退 ``default`` 桶（对齐 v1 ``get_config`` 口径）。

    Args:
        domain: 领域名；``None`` 或未登记时按 ``"default"`` 处理。

    Returns:
        LifecycleV2Config: 该域的 v2 配置；桶内无覆盖项时即为 dataclass 默认值。

    Note:
        ``LifecycleV2Config`` 定义在 :mod:`lifecycle_v2`，此处**函数内延迟导入**，
        避免 ``config`` 与 ``lifecycle_v2`` 的模块级循环依赖。
    """
    from .lifecycle_v2 import LifecycleV2Config

    bucket = LIFECYCLE_V2_DOMAIN_OVERRIDES.get(
        domain or "default", LIFECYCLE_V2_DOMAIN_OVERRIDES["default"]
    )
    return LifecycleV2Config(**bucket)
