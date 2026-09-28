"""
B站API封装 - UserAPIMixin 用户与榜单接口

拆分自 api.py 原始 L683-L882。
"""
from typing import Dict, Any, Optional, List

from core.data_quality import parse_count
from core.exceptions import BilibiliAPIError
from core.logger import get_logger

logger = get_logger(__name__)


def _raw_field_status(raw: Any) -> str:
    """按补默认值之前的原始字段判定质量状态。

    Args:
        raw: 补 0 之前的原始字段值，可能为 int/str/None/bool/float。

    Returns:
        'ok'（真实值，含真实 0）/'missing'（字段缺失）/'invalid'（类型非法）。
    """
    return parse_count(raw)[1]


def _build_profile_meta(
    source: str,
    level_raw: Any,
    follower_raw: Any,
    following_raw: Any,
) -> Dict[str, Any]:
    """构造 ``get_user_info`` 的 ``_meta`` 质量信息。

    Args:
        source: 数据来源标识，``'space_info'`` 或 ``'public_card'``。
        level_raw: 补默认值之前的原始等级。
        follower_raw: 补默认值之前的原始粉丝数。
        following_raw: 补默认值之前的原始关注数。

    Returns:
        含 ``source`` 与 ``field_status`` 的字典；``field_status`` 逐字段给出
        ``ok``/``missing``/``invalid``，供 01/03 新消费者优先于兼容 0 读取。
    """
    return {
        'source': source,
        'field_status': {
            'level': _raw_field_status(level_raw),
            'follower': _raw_field_status(follower_raw),
            'following': _raw_field_status(following_raw),
        },
    }


class UserAPIMixin:
    """用户与榜单接口 Mixin"""


    async def get_user_info(self, uid: int) -> Dict[str, Any]:
        """获取用户基本信息，WBI 空间接口受控时降级公开名片接口。

        Args:
            uid: 用户ID (mid)。

        Returns:
            包含标准化用户信息的 ``data`` 字典。
        """
        uid = int(uid)
        primary_error: Optional[Exception] = None
        try:
            # 主接口字段最完整，优先使用 WBI 签名版本。
            data = await self.get(
                f"{self.BASE_URL}/x/space/wbi/acc/info",
                params={"mid": uid},
                need_sign=True,
            )
            if not isinstance(data, dict):
                data = {}
            # data 保持原样以兼容旧消费者；_meta 记录补默认值前各字段的真实状态。
            return {
                "data": data,
                "_meta": _build_profile_meta(
                    'space_info',
                    data.get('level'),
                    data.get('follower'),
                    data.get('following'),
                ),
            }
        except Exception as exc:
            primary_error = exc
            logger.warning("用户空间资料接口失败，降级公开名片接口 (uid=%s): %s", uid, exc)

        try:
            # web-interface/card 为公开只读接口，风控策略与空间 WBI 接口独立。
            fallback = await self.get(
                f"{self.BASE_URL}/x/web-interface/card",
                params={"mid": uid, "photo": "true"},
                need_sign=False,
            )
            card = fallback.get("card") or {}
            level_info = card.get("level_info") or {}
            # 补默认值前先留存原始字段，严格 parser 才能区分真实 0 与字段缺失。
            raw_level = level_info.get("current_level")
            if raw_level is None:
                raw_level = card.get("level")
            raw_follower = fallback.get("follower")
            if raw_follower is None:
                raw_follower = card.get("fans")
            raw_following = card.get("attention")

            normalized = dict(card)
            normalized.update(
                mid=int(card.get("mid") or uid),
                name=str(card.get("name") or ""),
                level=int(level_info.get("current_level") or card.get("level") or 0),
                follower=int(fallback.get("follower") or card.get("fans") or 0),
                following=int(card.get("attention") or 0),
            )
            # B站对已注销账号的公开名片统一返回 name="账号已注销"（主接口 -404 属预期行为）。
            # 单独识别并打日志，避免后续排查把已注销误判成风控/cookie 问题。
            if normalized["name"] == "账号已注销":
                logger.warning(
                    "该 UID 为已注销账号 (uid=%s)，接口 -404 属预期，降级仅拿到兜底名称",
                    uid,
                )
            if not normalized["name"]:
                raise BilibiliAPIError("公开名片接口未返回用户名称")
            # 兼容 data 里仍可能是补 0；_meta 按原始字段给出权威状态，missing 不被 0 掩盖。
            return {
                "data": normalized,
                "_meta": _build_profile_meta(
                    'public_card', raw_level, raw_follower, raw_following
                ),
            }
        except Exception as fallback_error:
            logger.error(
                "获取用户信息失败 (uid=%s)，主接口=%s，回退接口=%s",
                uid,
                primary_error,
                fallback_error,
            )
            raise BilibiliAPIError(
                f"获取用户信息失败: 主接口 {primary_error}; 回退接口 {fallback_error}"
            ) from fallback_error

    async def get_user_relation_stat(self, uid: int) -> Dict[str, Any]:
        """获取用户关系统计（粉丝数、关注数）
        
        调用 /x/relation/stat 接口。
        
        Args:
            uid: 用户ID (mid)
            
        Returns:
            包含关系统计的字典，格式：
            {
                'data': {
                    'follower': int,  # 粉丝数
                    'following': int  # 关注数
                }
            }
        """
        try:
            # 使用关系统计接口
            url = f"{self.BASE_URL}/x/relation/stat"
            params = {'vmid': uid}
            
            data = await self.get(url, params=params, need_sign=False)
            
            # 包装返回格式
            return {'data': data}
            
        except Exception as e:
            logger.error(f"获取用户关系统计失败 (uid={uid}): {e}")
            raise BilibiliAPIError(f"获取用户关系统计失败: {e}")

    async def get_user_upstat(self, uid: int) -> Dict[str, Any]:
        """获取UP主累计播放等统计数据。

        Args:
            uid: B站用户UID。

        Returns:
            B站 ``/x/space/upstat`` 接口的 data 包装结果，其中
            ``data.archive.view`` 是累计投稿播放量。
        """
        try:
            # 新版接口将累计投稿播放量放在 data.archive.view，不能再从旧字段读取。
            url = f"{self.BASE_URL}/x/space/upstat"
            data = await self.get(url, params={'mid': uid}, need_sign=False)
            return {'data': data}
        except Exception as e:
            logger.error(f"获取用户累计播放失败 (uid={uid}): {e}")
            raise BilibiliAPIError(f"获取用户累计播放失败: {e}")

    @staticmethod
    def extract_charge_count(payload: Dict[str, Any]) -> Optional[int]:
        """从充电接口响应中提取非负充电人数。

        Args:
            payload: 已剥离业务外层的接口响应。

        Returns:
            充电人数；字段缺失或格式非法时返回 ``None``。
        """
        try:
            candidates = [
                payload.get('count'),
                payload.get('total_count'),
                payload.get('charge_count'),
                payload.get('data', {}).get('count') if isinstance(payload.get('data'), dict) else None,
                payload.get('data', {}).get('total_count') if isinstance(payload.get('data'), dict) else None,
            ]
            for value in candidates:
                if value is not None:
                    return max(int(value), 0)
        except (TypeError, ValueError) as exc:
            logger.warning("充电人数字段解析失败: %s", exc)
        return None

    async def get_charge_count(self, uid: int) -> Dict[str, Any]:
        """获取 UP 主累计充电人数，并兼容备用接口。

        Args:
            uid: B站用户 UID。

        Returns:
            包含 ``charge_count``、``source`` 和 ``raw_code`` 的结果字典。
        """
        endpoints = [
            ('panel', f"{self.BASE_URL}/x/ugcpay/trade/elec/panel"),
            ('month_rank', f"{self.BASE_URL}/x/ugcpay-rank/elec/month/up"),
        ]
        last_code = None
        for source, url in endpoints:
            try:
                data = await self.request(
                    'GET',
                    url,
                    params={'up_mid': int(uid)},
                    budget_key='charge',
                )
                count = self.extract_charge_count(data)
                if count is not None:
                    return {'charge_count': count, 'source': source, 'raw_code': 0}
                logger.info("充电接口%s未提供人数，继续尝试备用接口", source)
            except BilibiliAPIError as exc:
                last_code = getattr(exc, 'code', None)
                logger.warning("充电接口%s不可用(uid=%s): %s", source, uid, exc)
            except Exception as exc:
                logger.error("获取充电人数失败(%s, uid=%s): %s", source, uid, exc)
        return {'charge_count': 0, 'source': 'unavailable', 'raw_code': last_code}

    async def get_user_videos(self, uid: int, page: int = 1, page_size: int = 30) -> Dict[str, Any]:
        """获取用户投稿视频列表
        
        调用 /x/space/wbi/arc/search 接口（需 WBI 签名），
        按发布时间倒序返回视频列表。
        
        Args:
            uid: 用户ID (mid)
            page: 页码，从1开始
            page_size: 每页数量，默认30
            
        Returns:
            包含视频列表的字典，格式：
            {
                'data': {
                    'list': {
                        'vlist': [
                            {
                                'aid': int,
                                'bvid': str,
                                'title': str,
                                'play': int,
                                'pic': str,
                                'description': str,
                                'created': int,
                                ...
                            }
                        ]
                    },
                    'page': {'pn': int, 'ps': int, 'count': int}
                }
            }
        """
        try:
            # 使用投稿视频搜索接口
            url = f"{self.BASE_URL}/x/space/wbi/arc/search"
            params = {
                'mid': uid,
                'pn': page,
                'ps': page_size,
                'order': 'pubdate',  # 按发布时间排序
                'index': 1
            }
            
            # 此接口需要 WBI 签名
            data = await self.get(url, params=params, need_sign=True)
            
            # 包装返回格式以符合调用方期望
            return {'data': data}
            
        except Exception as e:
            logger.error(f"获取用户视频失败 (uid={uid}, page={page}): {e}")
            raise BilibiliAPIError(f"获取用户视频失败: {e}")

    async def get_ranking(self, rid: int, day: int = 7, original: int = 0, page: int = 1) -> Dict[str, Any]:
        """获取分区排行榜。

        调用 /x/web-interface/ranking/v2 接口。新版接口要求 type 使用
        ``all``（全部）或 ``origin``（原创）字符串，不能传 0/1 整数。
        接口支持 pn 翻页，单页最多 50 条，热点采集超过 50 条时按页拉取。

        Args:
            rid: 分区ID。
            day: 榜单周期（1=日榜，7=周榜，30=月榜）。
            original: 是否仅获取原创内容，0=全部，非0=原创；请求时转换为
                ``all`` 或 ``origin`` 字符串。
            page: 页码，从 1 开始，单页最多 50 条。

        Returns:
            包含排行榜数据的字典，格式：
            {
                'data': {
                    'list': [
                        {
                            'aid': int,
                            'bvid': str,
                            'title': str,
                            'owner': {...},
                            'stat': {...},
                            ...
                        }
                    ]
                }
            }
        """
        try:
            url = f"{self.BASE_URL}/x/web-interface/ranking/v2"
            # 新版 ranking/v2 只接受 all/origin 字符串，避免传整数触发 -400。
            ranking_type = 'origin' if original else 'all'
            params = {
                'rid': rid,
                'day': day,
                'type': ranking_type,
                'pn': max(1, int(page)),
            }

            data = await self.get(url, params=params, need_sign=False)

            # 包装返回格式以符合调用方期望。
            return {'data': data}

        except Exception as e:
            logger.error(f"获取排行榜失败 (rid={rid}, day={day}, page={page}): {e}")
            raise BilibiliAPIError(f"获取排行榜失败: {e}")

    async def get_room_base_info(self, uids: List[int]) -> Dict[str, Any]:
        """逐个查询 UID 对应的直播间基础信息。

        Args:
            uids: 用户 UID 列表（1-100 个）。

        Returns:
            兼容原调用方的 ``uid -> room_id/live_status`` 映射。
        """
        normalized_uids = list(dict.fromkeys(int(uid) for uid in uids if int(uid) > 0))[:100]
        room_map: Dict[str, Dict[str, Any]] = {}
        url = "https://api.live.bilibili.com/room/v1/Room/getRoomInfoOld"

        for uid in normalized_uids:
            try:
                # getRoomBaseInfo 已废弃；旧接口只接受单个 mid，因此按 UID 克制串行请求。
                data = await self.get(url, params={'mid': uid}, need_sign=False)
                if not isinstance(data, dict):
                    continue
                room_id = int(data.get('roomid') or data.get('room_id') or 0)
                live_status = int(data.get('liveStatus') or data.get('live_status') or 0)
                normalized = dict(data)
                normalized.update(room_id=room_id, live_status=live_status)
                room_map[str(uid)] = normalized
            except Exception as exc:
                # 单 UID 失败不应抹掉同批其他主播的直播数据。
                logger.warning("获取直播间信息失败，跳过当前 UID (uid=%s): %s", uid, exc)

        return {'data': room_map}

    async def get_guard_top_list(self, room_id: int, ruid: int) -> Dict[str, Any]:
        """查询直播间大航海（舰长）列表与总数。

        调用直播域名 ``/xlive/app-room/v2/guardTab/topList``，
        ``data.info.num`` 为舰长总数（含提督/总督），用于热点账号的
        "舰长转化"指标展示。未开播或无大航海的直播间返回空列表。

        Args:
            room_id: 直播间房间号。
            ruid: 主播 UID。

        Returns:
            B站 ``data`` 包装结果，含 ``info.num`` 与 ``list`` 列表。
        """
        try:
            url = "https://api.live.bilibili.com/xlive/app-room/v2/guardTab/topList"
            data = await self.get(
                url,
                params={
                    'roomid': int(room_id),
                    'ruid': int(ruid),
                    'page': 1,
                    'page_size': 29,
                    'typem': 0,
                },
                need_sign=False,
            )
            return {'data': data}
        except Exception as e:
            logger.error(f"获取大航海列表失败 (room={room_id}, ruid={ruid}): {e}")
            raise BilibiliAPIError(f"获取大航海列表失败: {e}")
