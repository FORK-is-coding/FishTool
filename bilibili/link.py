"""B站视频输入解析：从用户填写的文本里认出 BV 号或 b23.tv 短链。

用户可能填：纯 BV 号、浏览器地址栏里的视频链接（含分 P / 时间点参数）、
App 分享出来的整段文字，或 b23.tv 短链。本模块只做文本识别：

- 认出 BV 号（含 ``b23.tv/BV…`` 这种直接带 BV 号的短链）就返回 BV 号；
- 认出 b23.tv 短链但链接里没有 BV 号时，只标记为 ``short_link``，**不发请求解析**。
  短链要请求一次才知道指向哪个视频（也可能指向动态），解析必须走
  ``BilibiliAPI`` 的限频与请求额度，不能在这里直接联网。

群聊接入预留：本模块只依赖标准库，不 import ``core`` / ``bilibili.api``。AstrBot 适配层
运行在单独的进程和环境里，接入群聊链接识别时可以直接复用 :func:`find_video_refs`
扫描整条消息；需要支持短链时，由调用方解析跳转后把最终地址再交给 :func:`parse_video_input`。
前端 ``app.core.js`` 的 ``parseVideoInput`` 与本模块口径一致，改规则时两边同步。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional

#: 认出的是 BV 号。
KIND_BVID = "bvid"
#: 认出的是 b23.tv 短链（链接里没有 BV 号，需联网解析）。
KIND_SHORT_LINK = "short_link"

#: BV 号：前缀 + 10 位字母数字，前后不能紧挨字母数字，避免从更长的串里截出一段。
#: 前缀大小写不敏感，返回值保留用户原样（与 modules/lottery/target.py、语义折叠口径一致）。
_BVID = r"BV[0-9A-Za-z]{10}"

#: 短链前面不能紧挨的字符：字母数字（``notb23.tv``）、域名与路径符号（``x.b23.tv``、``/b23.tv``），
#: 以及 query 里的 ``= ? & %``（``?share_from=b23.tv/xxx`` 只是分享来源参数，不是用户要填的短链）。
_SHORT_LEFT_BOUNDARY = r"0-9A-Za-z.\-_/=?&%"

#: 一次扫描同时认短链和 BV 号；短链放在前面，``b23.tv/BV…`` 整体按短链匹配后再看路径。
_VIDEO_REF_RE = re.compile(
    rf"(?<![{_SHORT_LEFT_BOUNDARY}])"
    r"(?P<short>(?:https?://)?(?:b23\.tv|bili2233\.cn)/(?P<code>[0-9A-Za-z]+))"
    rf"|(?<![0-9A-Za-z])(?P<bvid>{_BVID})(?![0-9A-Za-z])",
    re.IGNORECASE,
)
_BVID_FULL_RE = re.compile(_BVID, re.IGNORECASE)

SHORT_LINK_MESSAGE = "暂不支持 b23.tv 短链，请在浏览器打开后复制地址栏里的链接，或直接填 BV 号"
NOT_FOUND_MESSAGE = "没有识别到 BV 号，请填写 BV 号或视频链接"


class VideoInputError(ValueError):
    """输入里没有可用的 BV 号（为空、只有短链或完全认不出）。"""


@dataclass(frozen=True)
class VideoRef:
    """从文本里认出的一个视频引用。

    Attributes:
        kind: :data:`KIND_BVID` 或 :data:`KIND_SHORT_LINK`。
        value: BV 号（保留原大小写）；短链时为短链原文。
    """

    kind: str
    value: str


def find_video_refs(text: Optional[str]) -> List[VideoRef]:
    """按出现顺序找出文本里的全部视频引用，同一引用只保留第一次。

    供群聊等「一条消息里可能有多个链接」的场景使用。

    Args:
        text: 任意文本。

    Returns:
        List[VideoRef]: 视频引用列表；没有时为空列表。
    """
    refs: List[VideoRef] = []
    seen = set()
    for match in _VIDEO_REF_RE.finditer(text or ""):
        if match.group("bvid"):
            ref = VideoRef(KIND_BVID, match.group("bvid"))
        elif _BVID_FULL_RE.fullmatch(match.group("code")):
            # b23.tv/BV… 直接带 BV 号，不需要联网解析。
            ref = VideoRef(KIND_BVID, match.group("code"))
        else:
            ref = VideoRef(KIND_SHORT_LINK, match.group("short"))
        if ref not in seen:
            seen.add(ref)
            refs.append(ref)
    return refs


def parse_video_input(text: Optional[str]) -> Optional[VideoRef]:
    """返回输入里的第一个视频引用。

    Args:
        text: 用户填写的 BV 号、视频链接或分享文字。

    Returns:
        Optional[VideoRef]: 第一个视频引用；认不出时为 None。
    """
    refs = find_video_refs(text)
    return refs[0] if refs else None


def require_bvid(text: Optional[str]) -> str:
    """从输入里取出 BV 号，取不到就报错。

    Args:
        text: 用户填写的 BV 号、视频链接或分享文字。

    Returns:
        str: BV 号（保留原大小写）。

    Raises:
        VideoInputError: 只有短链，或认不出 BV 号。
    """
    ref = parse_video_input(text)
    if ref is None:
        raise VideoInputError(NOT_FOUND_MESSAGE)
    if ref.kind == KIND_SHORT_LINK:
        raise VideoInputError(SHORT_LINK_MESSAGE)
    return ref.value


def coerce_bvid(value: str) -> str:
    """接口入参用：能认出 BV 号就换成 BV 号，认不出原样返回交给原有校验。

    只有 b23.tv 短链会在这里报错：原样放过会被当成 BV 号发给 B站，报错信息让人摸不着头脑。

    Args:
        value: 请求里的 bvid 字段。

    Returns:
        str: BV 号，或原值。

    Raises:
        VideoInputError: 输入是不带 BV 号的 b23.tv 短链。
    """
    ref = parse_video_input(value)
    if ref is None:
        return value
    if ref.kind == KIND_SHORT_LINK:
        raise VideoInputError(SHORT_LINK_MESSAGE)
    return ref.value
