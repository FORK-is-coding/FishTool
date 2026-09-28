"""数据质量公共契约：严格数值解析、明确时间换算、来源状态判定。

本模块是 FishTool 03 的**唯一共享质量 helper**，是 01 正确排名 / 02 生命周期 /
04 话题决策中热点质量子集的上游依赖。设计原则：

- **绝不用 0 抢占缺失值**：缺失一律返回 ``None``，由调用方决定写 SQL NULL。
- **状态三态化**：``ok`` / ``missing`` / ``invalid``，让读端能区分「真实 0」
  与「没拿到」。真实 0 状态为 ``ok``，缺字段为 ``missing``。
- **时间必须明确**：naive 时间戳在没有显式 ``legacy_timezone`` 时一律不可用，
  禁止用当前机器时区猜测。

对外函数：
    parse_count(raw)              -> (int|None, 'ok'|'missing'|'invalid')
    parse_ratio(raw)              -> (float|None, 'ok'|'missing'|'invalid')
    utc_now_epoch_s()             -> int
    to_epoch_s(value, *, legacy_timezone=None) -> int|None
    epoch_to_utc_dt(value)        -> datetime|None（非法输入抛 ValueError）

注意：``parse_ratio`` 仅用于 0—1 的占比字段，**不要**拿它校验允许大于 100%
的均播/粉丝比之类指标。
"""
import math
import re
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

__all__ = [
    'parse_count',
    'parse_ratio',
    'utc_now_epoch_s',
    'to_epoch_s',
    'epoch_to_utc_dt',
]

# 仅由 ASCII 数字组成的字符串（允许首尾空白）才视为合法计数。
_INT_TEXT_RE = re.compile(r'[0-9]+')


def parse_count(raw):
    """把上游原始值解析为非负整数计数。

    Args:
        raw: 上游字段原值，可能为 int/str/None/bool/float。

    Returns:
        tuple: ``(value, status)``。
            - 合法非负整数（或纯数字字符串）-> ``(int, 'ok')``；
            - 字段缺失（None）-> ``(None, 'missing')``；
            - bool / 负数 / float / 含非数字字符 / 空串 -> ``(None, 'invalid')``。

    设计要点：
        - ``bool`` 是 ``int`` 的子类，必须在 ``type(raw) is int`` 之前拦截，
          否则 ``True`` 会被当成 1、``False`` 当成 0。
        - 用 ``type(raw) is int`` 而非 ``isinstance``，同样是为了排除 bool。
        - 字符串走 ``re.fullmatch`` 全串匹配，``" 123 "`` 去除空白后合法，
          ``"12a"`` / ``"1.5"`` / ``""`` 一律 invalid。
    """
    if raw is None:
        return None, 'missing'
    if isinstance(raw, bool):
        # bool 先于 int 判定，防止 True/False 冒充 1/0。
        return None, 'invalid'
    if type(raw) is int:
        return (raw, 'ok') if raw >= 0 else (None, 'invalid')
    if isinstance(raw, str):
        text = raw.strip()
        if _INT_TEXT_RE.fullmatch(text):
            return int(text), 'ok'
    return None, 'invalid'


def parse_ratio(raw):
    """把上游原始值解析为 [0, 1] 区间内的占比。

    Args:
        raw: 上游字段原值，可为数值或可转 float 的字符串。

    Returns:
        tuple: ``(value, status)``。
            - 有限且落在 [0, 1] -> ``(float, 'ok')``；
            - None -> ``(None, 'missing')``；
            - bool / NaN / ±inf / 越界 / 不可转 float -> ``(None, 'invalid')``。

    注意：本函数只适用于 0—1 的占比，不能用于允许大于 100% 的均播/粉丝比。
    """
    if raw is None:
        return None, 'missing'
    if isinstance(raw, bool):
        # bool 同样要先拦截，避免 True->1.0 被当成合法占比。
        return None, 'invalid'
    try:
        value = float(raw)
    except (TypeError, ValueError, OverflowError):
        return None, 'invalid'
    if not math.isfinite(value) or not 0 <= value <= 1:
        return None, 'invalid'
    return value, 'ok'


def utc_now_epoch_s():
    """返回当前 UTC 秒级时间戳（整数）。"""
    return int(time.time())


def to_epoch_s(value, *, legacy_timezone=None):
    """把 ``datetime`` 换算为 UTC 秒级时间戳，歧义/不存在时刻返回 None。

    Args:
        value: 待换算对象；非 ``datetime`` 直接返回 None。
        legacy_timezone: 当 ``value`` 为 naive 时间时用于解释其时区的 IANA 名称
            （如 ``'Asia/Shanghai'``）。未提供时 naive 时间**不可换算**。

    Returns:
        int | None: UTC 秒级时间戳；无法唯一确定时返回 None。

    处理规则：
        - aware 且带有效 ``utcoffset`` -> 直接 astimezone(UTC) 取整。
        - naive -> 必须在 ``legacy_timezone`` 指定的时区内做 **往返校验**：
          夏令时「春季跳时」的不存在时刻、以及「秋季回拨」的歧义时刻，
          都会导致候选时间戳数量 != 1，从而返回 None。
        - 禁止用运行机器的本地时区替代，避免可移植性错误。
    """
    if not isinstance(value, datetime):
        return None
    try:
        if value.tzinfo is None or value.utcoffset() is None:
            # naive 时间必须有明确时区来源，否则拒绝换算。
            if not legacy_timezone:
                return None
            zone = ZoneInfo(legacy_timezone)
            wall = value.replace(tzinfo=None, fold=0)
            candidates = set()
            for fold in (0, 1):
                aware = wall.replace(tzinfo=zone, fold=fold)
                utc = aware.astimezone(timezone.utc)
                back = utc.astimezone(zone).replace(tzinfo=None, fold=0)
                if back == wall:  # 春季跳时的不存在时刻不能往返，剔除
                    candidates.add(int(utc.timestamp()))
            if len(candidates) != 1:  # 0=不存在；2=秋季回拨歧义
                return None
            return candidates.pop()
        return int(value.astimezone(timezone.utc).timestamp())
    except (ValueError, OverflowError, OSError, ZoneInfoNotFoundError):
        # ZoneInfo 缺该时区数据、或时间越界时，统一视为不可用。
        return None


def epoch_to_utc_dt(value):
    """把 UTC 秒级时间戳还原为带时区的 ``datetime``。

    Args:
        value: UTC 秒级时间戳；None 直接返回 None。

    Returns:
        datetime | None: 带 UTC 时区的 datetime。

    Raises:
        ValueError: 传入非 int（bool 亦视为非法）或无法转换的时间戳时抛出
            ``'invalid_epoch'``，避免把脏时间静默当有效。
    """
    if value is None:
        return None
    if type(value) is not int:  # bool 亦被拒绝
        raise ValueError('invalid_epoch')
    try:
        return datetime.fromtimestamp(value, timezone.utc)
    except (ValueError, OverflowError, OSError) as exc:
        raise ValueError('invalid_epoch') from exc
