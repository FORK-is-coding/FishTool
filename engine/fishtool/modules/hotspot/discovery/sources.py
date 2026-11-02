"""06 采集广度 · 三源读取与容错解析（规格 §3 / §4）。

职责边界：本模块只做「读入口 + 解析成 DTO」，**不碰聚合 / 去重 / 落库**
（那些在 ``service.py``）；也**不持有轮询缓存**（共享缓存在 ``DiscoveryPollCache``）。

解析输入契约（三源统一，明确「吃哪层」——硬约束 6）
----------------------------------------------------
所有 ``parse_*`` 只吃**统一发现 envelope**::

    {"code": int, "message": str, "data": Any}

- ``code``：HTTP 层解析出的业务码；**必填**，缺失视为契约破坏（``missing_code``）。
- ``data``：**已拆外层**后的内层数据；非零 code 时允许为 None。
- 三个来源的内层路径**各不相同**，已在各自 docstring 写明：
    * ``search/square``   -> ``data["trending"]["list"]``
    * ``popular``         -> ``data["list"]``（按页）
    * ``ranking/v2?rid=0``-> ``data["list"]``，条目内另有 ``others``
- parse 层**只认这一种契约**，不会去猜「可能没有 code」。之所以由 fetch 层统一成
  envelope，是因为 client 有两条不同路径：
    * ``api.get()`` 返回**已拆外层 data**，非零 code 在 client 层抛业务异常；
    * ``get_ranking(rid=0)`` 复用现有方法，返回**二次包装** ``{"data": data}``。
  ``fetch_*`` 负责把这两条路径标准化成统一 envelope。

配额（规格 §2.2 / §2.6）
------------------------
06 是**免 Cookie 聚合通道**，三个入口的 ``(凭证域, 配额类别)`` 在本模块**显式声明**
（不再依赖 ``config/budget.yaml`` 的域归属兜底 ``resolve_domain``）：

- ``search/square``   -> ``(no_cookie, discovery)``
- ``popular``         -> ``(no_cookie, discovery)``
- ``ranking/v2?rid=0`` -> ``(no_cookie, ranking)``

配额上限本身仍只从 ``config/budget.yaml`` 读取（代码里不写死配额数字）；
每次真实 HTTP 尝试前调用 ``before_http_attempt`` 记账（可注入 ``budget_hook`` 便于离线测试）。
"""
from __future__ import annotations

import logging
import re
from typing import Any, Callable, Dict, List

from core.exceptions import BilibiliAPIError
from core.request_budget import before_http_attempt, load_quota_limits

from .contracts import (
    BroadKeyword,
    BroadVideo,
    ParseOutcome,
    SOURCE_POPULAR,
    SOURCE_RANKING_ALL,
    SOURCE_RANKING_ALL_OTHERS,
    SOURCE_SEARCH_SQUARE,
    STATE_ERROR,
    STATE_OK,
    parse_heat_score,
    parse_optional_int,
)

logger = logging.getLogger(__name__)

#: ``search/square`` 热搜入口。
SEARCH_SQUARE_URL = "https://api.bilibili.com/x/web-interface/search/square"
#: ``popular`` 综合热门入口（``ps`` 上限 20，翻页取更多）。
POPULAR_URL = "https://api.bilibili.com/x/web-interface/popular"
#: ``ranking/v2`` 全站榜入口（规格 §3.3 的裸 URL，仅 ``rid`` + ``type``）。
RANKING_URL = "https://api.bilibili.com/x/web-interface/ranking/v2"
#: ``ranking/v2`` 专用的 Referer 覆写：client 默认 ``https://www.bilibili.com/``
#: 会被该端点判为 -352，实测只有指向**真实榜单页**的 Referer 才放行。
RANKING_REFERER = "https://www.bilibili.com/v/popular/rank/all"

#: 配额类别（与 config/budget.yaml 的 categories 一致）。
CATEGORY_DISCOVERY = "discovery"
CATEGORY_RANKING = "ranking"

#: 06 三个入口的 ``(凭证域, 配额类别)`` —— **显式声明**，不依赖 budget.yaml 的域归属兜底。
#: 06 是免 Cookie 聚合通道，三个入口全部走 ``no_cookie`` 域；
#: ``search/square`` 与 ``popular`` 计 ``discovery``（480 / 24h），
#: ``ranking/v2?rid=0`` 计 ``ranking``。
DISCOVERY_QUOTA = ("no_cookie", CATEGORY_DISCOVERY)
RANKING_ALL_QUOTA = ("no_cookie", CATEGORY_RANKING)

#: 类别 -> 缺省凭证域兜底（运行期优先读 budget.yaml 的 domain，不硬编码配额数字）。
_DEFAULT_DOMAIN_BY_CATEGORY = {
    CATEGORY_DISCOVERY: "no_cookie",
    CATEGORY_RANKING: "cookie",
}

#: 预算钩子签名（默认走 core.request_budget.before_http_attempt）。
BudgetHook = Callable[..., None]

#: ``API错误 [-352]: ...`` 形态的业务码。
_API_CODE_RE = re.compile(r"\[(-?\d+)\]")
#: ``请求被风控: -352`` 这类把码放在消息尾部的形态（client 风控分支不带方括号）。
_API_TRAILING_CODE_RE = re.compile(r"(-?\d+)\s*$")


def resolve_domain(category: str) -> str:
    """从 ``config/budget.yaml`` 读取类别所属凭证域（**06 通道已不再使用**）。

    2026-10-02 批次起，06 三个入口的域在 :data:`DISCOVERY_QUOTA` /
    :data:`RANKING_ALL_QUOTA` 里显式声明（规格要求「不得靠默认值蒙」）；
    本函数保留是为兼容既有外部导入点，内部记账不再经过它。

    Args:
        category: 配额类别（``discovery`` / ``ranking``）。

    Returns:
        str: 域标识（``no_cookie`` / ``cookie``）；账本不可用时回退到类别缺省域。
    """
    try:
        limits = load_quota_limits()
        if limits is not None:
            domain = limits.category_domains.get(category)
            if domain:
                return str(domain)
    except Exception as exc:  # noqa: BLE001 - 账本读取失败不应让发现通道停摆
        logger.warning("读取 budget.yaml 域归属失败，回退缺省域 (category=%s): %r", category, exc)
    return _DEFAULT_DOMAIN_BY_CATEGORY.get(category, "cookie")


def extract_api_code(exc: Exception) -> int:
    """从 client 抛出的业务异常里提取 B 站业务码。

    client 有两种形态：业务码用方括号（``API错误 [-352]: ...``）或放在消息尾部
    （风控分支 ``请求被风控: -352``）。两者都尝试提取。

    Args:
        exc: 捕获到的异常对象。

    Returns:
        int: 提取到的业务码；无法提取时返回 ``-1``。
    """
    text = str(exc)
    match = _API_CODE_RE.search(text) or _API_TRAILING_CODE_RE.search(text)
    if match:
        try:
            return int(match.group(1))
        except (TypeError, ValueError):
            return -1
    return -1


# --------------------------------------------------------------------------
# 统一 envelope 读取
# --------------------------------------------------------------------------

async def _fetch_envelope(
    api: Any,
    url: str,
    params: Dict[str, Any],
    domain: str,
    category: str,
    budget_hook: BudgetHook,
) -> Dict[str, Any]:
    """调用 ``api.get`` 并标准化成统一发现 envelope。

    生产语义：``api.get()`` 返回**已拆外层 data**，非零 code 在 client 层抛业务异常。

    Args:
        api: B 站 API 客户端（含 ``get`` 方法）。
        url: 目标 URL。
        params: 查询参数。
        domain: 凭证域（06 通道固定 ``no_cookie``）。
        category: 配额类别。
        budget_hook: 发送前记账钩子。

    Returns:
        Dict[str, Any]: ``{"code", "message", "data"}``。

    Raises:
        RequestBudgetExceeded: 配额耗尽 / 超 deadline 时**原样抛出**（不是业务失败）。
    """
    budget_hook(domain=domain, category=category)
    try:
        data = await api.get(url, params=params)
        return {"code": 0, "message": "", "data": data}
    except BilibiliAPIError as exc:
        return {"code": extract_api_code(exc), "message": str(exc), "data": None}


async def fetch_hot_keywords(
    api: Any,
    *,
    limit: int = 10,
    budget_hook: BudgetHook = before_http_attempt,
) -> Dict[str, Any]:
    """读取 ``search/square`` 热搜关键词入口。

    内层路径：``data["trending"]["list"]``。

    Args:
        api: B 站 API 客户端。
        limit: 返回条数（接口参数，非配额）。
        budget_hook: 发送前记账钩子。

    Returns:
        Dict[str, Any]: 统一发现 envelope。非零 code -> ``{"code": 非0, "data": None}``。

    Raises:
        RequestBudgetExceeded: 配额耗尽时原样抛出。
    """
    return await _fetch_envelope(
        api, SEARCH_SQUARE_URL, {"limit": int(limit)},
        DISCOVERY_QUOTA[0], DISCOVERY_QUOTA[1], budget_hook,
    )


async def fetch_popular_page(
    api: Any,
    *,
    page: int = 1,
    ps: int = 20,
    budget_hook: BudgetHook = before_http_attempt,
) -> Dict[str, Any]:
    """读取 ``popular`` 综合热门的单页。

    内层路径：``data["list"]``（单页最多 20 条）。

    Args:
        api: B 站 API 客户端。
        page: 页码，从 1 开始。
        ps: 每页条数（接口参数，非配额）。
        budget_hook: 发送前记账钩子。

    Returns:
        Dict[str, Any]: 统一发现 envelope。

    Raises:
        RequestBudgetExceeded: 配额耗尽时原样抛出。
    """
    return await _fetch_envelope(
        api, POPULAR_URL, {"ps": int(ps), "pn": max(1, int(page))},
        DISCOVERY_QUOTA[0], DISCOVERY_QUOTA[1], budget_hook,
    )


async def fetch_ranking(
    api: Any,
    *,
    rid: int = 0,
    day: int = 7,
    budget_hook: BudgetHook = before_http_attempt,
) -> Dict[str, Any]:
    """读取 ``ranking/v2?rid=0`` 全站榜（复用同一 client 的 ``api.get``）。

    内层路径：``data["list"]``，条目内自带 ``others``。

    与 06 复核意见 §5.1 的差异（**有实测依据**）：复核建议复用 client 的
    ``get_ranking``，但该方法的既有实现会在参数里强制带 ``day`` 与 ``pn``。
    2026-10-02 实测：
        - ``ranking/v2?rid=0&type=all``            -> ``code 0``（100 条）；
        - ``ranking/v2?rid=0&type=all&day=7&pn=1`` -> ``code -352``（风控）；
        - ``ranking/v2?rid=0&type=all&day=1``      -> ``code -352``；
        - ``ranking/v2?rid=0&day=7``               -> ``code -352``。
    即任一附加参数都会触发风控，只有规格 §3.3 的裸 URL 可用。因此这里**不调用
    ``get_ranking``**，改为复用同一 client 的 ``api.get``，参数严格按规格 §3.3。

    另需覆写 Referer（同次实测）：
        - ``Referer: https://www.bilibili.com/``             -> ``-352``；
        - ``Referer: https://api.bilibili.com/``             -> ``-352``；
        - ``Referer: https://search.bilibili.com/``          -> ``-352``；
        - ``Referer: https://www.bilibili.com/v/popular/rank/all`` -> ``code 0``。
    故本函数对该端点单独覆写 Referer（见 :data:`RANKING_REFERER`）。这是平台风控的
    可观测行为，属**易变项**，冒烟脚本会持续验证；一旦平台调整需同步更新。

    解析层吃法不变：仍吃**统一发现 envelope**（``data`` 为已拆外层的内层数据）。
    若某调用方改用 ``get_ranking``，其二级包装 ``{"data": data}`` 由**该调用方在 fetch
    层拆掉**，不进入 ``parse_ranking``。

    Args:
        api: B 站 API 客户端（含 ``get``）。
        rid: 分区 ID，``0`` 为全站榜。
        day: 保留形参以兼容调用方，**实际请求不发送**（发送会触发 -352，见上）。
        budget_hook: 发送前记账钩子。

    Returns:
        Dict[str, Any]: 统一发现 envelope。

    Raises:
        RequestBudgetExceeded: 配额耗尽时原样抛出。
    """
    budget_hook(domain=RANKING_ALL_QUOTA[0], category=RANKING_ALL_QUOTA[1])
    try:
        data = await api.get(
            RANKING_URL,
            params={"rid": int(rid), "type": "all"},
            headers={"Referer": RANKING_REFERER},
        )
        return {"code": 0, "message": "", "data": data}
    except BilibiliAPIError as exc:
        return {"code": extract_api_code(exc), "message": str(exc), "data": None}


# --------------------------------------------------------------------------
# 容错解析
# --------------------------------------------------------------------------

def _read_code(envelope: Any) -> Any:
    """读取 envelope 的业务码。

    Args:
        envelope: 统一发现 envelope。

    Returns:
        Any: ``code`` 原值；非 dict 或缺失时返回 None。
    """
    if isinstance(envelope, dict):
        return envelope.get("code")
    return None


def _read_data(envelope: Any) -> Any:
    """读取 envelope 的已拆内层 data。

    Args:
        envelope: 统一发现 envelope。

    Returns:
        Any: ``data``；非 dict 时返回 None。
    """
    if isinstance(envelope, dict):
        return envelope.get("data")
    return None


def _error_outcome(error_code: Any, reason: str) -> ParseOutcome:
    """构造 error 结果。

    Args:
        error_code: 业务码（可为 None）。
        reason: 原因短码。

    Returns:
        ParseOutcome: ``state='error'``，``items`` 为空。
    """
    return ParseOutcome(
        state=STATE_ERROR,
        items=[],
        returned_count=0,
        error_code=error_code if isinstance(error_code, int) else None,
        reason=reason,
    )


def _parse_video_row(
    row: Any, source: str, captured_epoch_s: int, position: Any
) -> BroadVideo | None:
    """把一条视频条目解析成 BroadVideo（缺 bvid 视为无法定位身份，跳过）。

    三套分类字段分别解析、分别留状态，**不做统一 tid**。

    Args:
        row: 接口条目 dict。
        source: 发现来源标识。
        captured_epoch_s: 观测时刻（UTC 秒）。
        position: 列表内名次。

    Returns:
        BroadVideo | None: 解析结果；缺 bvid 时返回 None（跳过）。
    """
    if not isinstance(row, dict):
        return None
    bvid = row.get("bvid")
    if not isinstance(bvid, str) or not bvid.strip():
        return None

    owner = row.get("owner") if isinstance(row.get("owner"), dict) else {}
    stat = row.get("stat") if isinstance(row.get("stat"), dict) else {}

    legacy_tid, legacy_tid_status = parse_optional_int(row.get("tid"))
    tidv2, tidv2_status = parse_optional_int(row.get("tidv2"))
    pid_v2, pid_v2_status = parse_optional_int(row.get("pid_v2"))
    owner_mid, owner_status = parse_optional_int(owner.get("mid"))
    view, view_status = parse_optional_int(stat.get("view"))

    def _text(value: Any) -> Any:
        return value.strip() if isinstance(value, str) and value.strip() else None

    return BroadVideo(
        bvid=bvid.strip(),
        source=source,
        captured_epoch_s=captured_epoch_s,
        position=position if isinstance(position, int) else None,
        legacy_tid=legacy_tid,
        legacy_tid_status=legacy_tid_status,
        tidv2=tidv2,
        tidv2_status=tidv2_status,
        pid_v2=pid_v2,
        pid_v2_status=pid_v2_status,
        tname=_text(row.get("tname")),
        tnamev2=_text(row.get("tnamev2")),
        owner_mid=owner_mid,
        owner_status=owner_status,
        view=view,
        view_status=view_status,
        rcmd_reason=_text(row.get("rcmd_reason")),
    )


def parse_hot_keywords(envelope: Any, captured_epoch_s: int) -> ParseOutcome:
    """容错解析 ``search/square`` 统一 envelope。

    内层路径：``data["trending"]["list"]``。

    解析规则：
        - ``code != 0`` -> ``state='error'``，**不得**当成「今天没有话题」；
        - ``list`` 为空 -> ``state='ok'``、0 条（真空榜）；
        - ``keyword`` 空白 -> 跳过该条；
        - ``heat_score`` 非法（bool / 负数 / 非数值）-> 跳过该条，**不写 0**；
        - ``heat_score`` 缺失 -> 保留候选，``heat_score=None``、``heat_status='missing'``。

    Args:
        envelope: 统一发现 envelope。
        captured_epoch_s: 观测时刻（UTC 秒）。

    Returns:
        ParseOutcome: 解析结果（state / items / returned_count / error_code / reason）。
    """
    code = _read_code(envelope)
    if code is None:
        return _error_outcome(None, "missing_code")
    if code != 0:
        return _error_outcome(code, f"api_code_{code}")

    data = _read_data(envelope)
    if not isinstance(data, dict):
        return _error_outcome(code, "missing_data")
    trending = data.get("trending")
    if not isinstance(trending, dict):
        return _error_outcome(code, "missing_trending")
    raw_list = trending.get("list")
    if not isinstance(raw_list, list):
        return _error_outcome(code, "missing_list")

    items: List[BroadKeyword] = []
    for index, row in enumerate(raw_list):
        if not isinstance(row, dict):
            continue
        keyword = row.get("keyword")
        if not isinstance(keyword, str) or not keyword.strip():
            continue
        heat_score, heat_status = parse_heat_score(row.get("heat_score"))
        if heat_status == "invalid":
            # 非法 heat_score：跳过该条，绝不写 0。
            continue
        items.append(
            BroadKeyword(
                keyword=keyword.strip(),
                heat_score=heat_score,
                heat_status=heat_status,
                rank=index + 1,
                source=SOURCE_SEARCH_SQUARE,
                captured_epoch_s=captured_epoch_s,
                show_name=row.get("show_name") if isinstance(row.get("show_name"), str) else None,
            )
        )
    return ParseOutcome(state=STATE_OK, items=items, returned_count=len(raw_list), error_code=code)


def parse_popular(envelope: Any, captured_epoch_s: int) -> ParseOutcome:
    """容错解析 ``popular`` 单页统一 envelope。

    内层路径：``data["list"]``。

    解析规则：
        - ``code != 0`` -> ``state='error'``；
        - ``list`` 为空 -> ``state='ok'``、0 条；
        - 缺 ``bvid`` -> 跳过该条（无法定位身份）；
        - 缺 owner / tid / tidv2 / pid_v2 / view -> 保留该条并记对应 ``missing``，**不补 0**；
        - ``view`` 非法 -> 保留该条、value 为 None、状态 ``invalid``（不为 0）。

    Args:
        envelope: 统一发现 envelope。
        captured_epoch_s: 观测时刻（UTC 秒）。

    Returns:
        ParseOutcome: 解析结果。
    """
    code = _read_code(envelope)
    if code is None:
        return _error_outcome(None, "missing_code")
    if code != 0:
        return _error_outcome(code, f"api_code_{code}")

    data = _read_data(envelope)
    if not isinstance(data, dict):
        return _error_outcome(code, "missing_data")
    raw_list = data.get("list")
    if not isinstance(raw_list, list):
        return _error_outcome(code, "missing_list")

    items: List[BroadVideo] = []
    for index, row in enumerate(raw_list):
        parsed = _parse_video_row(row, SOURCE_POPULAR, captured_epoch_s, index + 1)
        if parsed is not None:
            items.append(parsed)
    return ParseOutcome(state=STATE_OK, items=items, returned_count=len(raw_list), error_code=code)


def parse_ranking(envelope: Any, captured_epoch_s: int) -> ParseOutcome:
    """容错解析 ``ranking/v2?rid=0`` 统一 envelope。

    内层路径：``data["list"]``；条目内 ``others`` 为同 UP 其他上榜作品。

    解析规则：
        - ``code != 0`` -> ``state='error'``；
        - 主 ``list`` 为空 -> ``state='ok'``、0 条；
        - ``others`` **单独标注来源** ``ranking_all_others``，**不得丢掉**；
        - ``others`` 字段不全时保留候选并记 ``missing``，不补 0。

    Args:
        envelope: 统一发现 envelope。
        captured_epoch_s: 观测时刻（UTC 秒）。

    Returns:
        ParseOutcome: 解析结果（``others_count`` 为 others 原始条数）。
    """
    code = _read_code(envelope)
    if code is None:
        return _error_outcome(None, "missing_code")
    if code != 0:
        return _error_outcome(code, f"api_code_{code}")

    data = _read_data(envelope)
    if not isinstance(data, dict):
        return _error_outcome(code, "missing_data")
    raw_list = data.get("list")
    if not isinstance(raw_list, list):
        return _error_outcome(code, "missing_list")

    items: List[BroadVideo] = []
    others_count = 0
    for index, row in enumerate(raw_list):
        parsed = _parse_video_row(row, SOURCE_RANKING_ALL, captured_epoch_s, index + 1)
        if parsed is not None:
            items.append(parsed)
        others = row.get("others") if isinstance(row, dict) else None
        if isinstance(others, list):
            others_count += len(others)
            for other_index, other in enumerate(others):
                other_parsed = _parse_video_row(
                    other, SOURCE_RANKING_ALL_OTHERS, captured_epoch_s, other_index + 1
                )
                if other_parsed is not None:
                    items.append(other_parsed)
    return ParseOutcome(
        state=STATE_OK,
        items=items,
        returned_count=len(raw_list),
        error_code=code,
        others_count=others_count,
    )
