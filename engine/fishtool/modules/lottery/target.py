"""抽奖目标解析与元数据获取。"""

# 正则用于提取 BV 号与动态路径 ID。
import re
# 数据类用于承载标准化的抽奖目标。
from dataclasses import asdict, dataclass
# 类型标注保证接口一致性与 IDE 提示。
from typing import Any, Dict
# URL 解析与查询参数读取用于动态链接识别。
from urllib.parse import parse_qs, urlparse

# B 站 API 客户端，负责元数据请求。
from bilibili.api import BilibiliAPI


@dataclass(frozen=True)
# 标准化视频或动态抽奖目标数据类。
class LotteryTarget:
    """标准化的视频或动态抽奖目标。"""

    # 目标类型：video 或 dynamic。
    target_type: str
    # 目标 ID：BV 号或动态 ID。
    target_id: str
    # 评论区对象 ID，评论接口必需。
    oid: int
    # 评论类型，视频为 1，动态通常为 17。
    comment_type: int
    # 展示标题，缺失时用默认文案。
    title: str
    # 发布者昵称，缺失时用默认文案。
    author: str

    # 转成字典便于序列化返回前端。
    def to_dict(self) -> Dict[str, Any]:
        """将目标转换为可直接返回前端的字典。"""
        # 直接复用 dataclasses 的序列化方法，保证字段与构造函数一致。
        return asdict(self)


# 解析用户输入的入口函数。
def parse_target_input(raw_value: str) -> tuple[str, str]:
    """解析用户输入，返回目标类型和目标 ID。

    Args:
        raw_value: BV 号或动态完整链接。

    Returns:
        ``(video|dynamic, id)``。

    Raises:
        ValueError: 输入无法识别时抛出。
    """
    # 去掉首尾空白，避免前后空格导致误判。
    value = (raw_value or "").strip()
    # 尝试提取视频 BV 号，命中后可直接走视频元数据接口。
    bvid_match = re.search(r"BV[0-9A-Za-z]{10}", value, re.IGNORECASE)
    # 命中 BV 号直接返回视频类型。
    if bvid_match:
        # 命中即返回视频类型与 BV 号。
        return "video", bvid_match.group(0)

    # 纯数字可能是 UID/AV 号，禁止误当动态 ID。
    if value.isdigit():
        # 纯数字可能是 UID/AV 号，禁止误当动态 ID，强制要求完整链接。
        raise ValueError("动态必须输入完整链接，避免把 UID 或 AV 号误识别为动态")

    # 链接解析失败时按类型分流错误信息。
    try:
        # 解析链接为 URL 结构再做校验。
        parsed = urlparse(value)
        # 只接受 bilibili.com 域名的 http/https 链接。
        if parsed.scheme not in {"http", "https"} or not parsed.netloc.endswith("bilibili.com"):
            # 域名或协议不合法时给出明确提示。
            raise ValueError("请输入 BV 号或 bilibili.com 动态完整链接")
        # 从动态路径提取数字 ID，兼容 opus 和 dynamic 两种公开链接形式。
        path_match = re.search(r"/(?:opus|dynamic)/(\d+)", parsed.path)
        # 读取查询参数中的动态 ID，作为路径格式之外的兼容回退。
        query_id = parse_qs(parsed.query).get("dynamic_id", [""])[0]
        # 优先使用路径 ID，路径没有 ID 时再使用查询参数结果。
        dynamic_id = path_match.group(1) if path_match else query_id
        # 动态 ID 为数字才视为合法。
        if dynamic_id.isdigit():
            # 数字动态 ID 视为合法并返回。
            return "dynamic", dynamic_id
    except (TypeError, ValueError) as exc:
        # 业务校验错误（提示语）原样上抛，其余异常统一走末尾通用错误。
        if isinstance(exc, ValueError) and "请输入" in str(exc):
            # 业务校验错误原样上抛，其余异常统一走末尾通用错误。
            raise
    # 全部识别失败时给出统一错误信息。
    raise ValueError("未识别到有效 BV 号或动态完整链接")


# 读取目标元数据的入口函数。
async def fetch_target_metadata(api: BilibiliAPI, raw_value: str) -> LotteryTarget:
    """从 B 站读取目标标题、发布者和评论区标识。

    Args:
        api: 已配置限频和登录态的 B 站客户端。
        raw_value: BV 号或动态完整链接。

    Returns:
        标准化目标对象。
    """
    # 先解析输入，确定目标是视频还是动态。
    target_type, target_id = parse_target_input(raw_value)
    # 视频与动态分别走不同接口。
    if target_type == "video":
        # 视频走 web-interface/view，按 BV 号取 aid 和标题。
        data = await api.get(
            f"{api.BASE_URL}/x/web-interface/view",
            params={"bvid": target_id},
        )
        # 提取视频发布者对象，缺失时由返回层使用明确的未知标记。
        owner = data.get("owner") or {}
        # 视频目标：评论类型固定为 1。
        return LotteryTarget(
            target_type="video",
            target_id=str(data.get("bvid") or target_id),
            oid=int(data.get("aid") or 0),
            comment_type=1,
            title=str(data.get("title") or "未获取到视频标题"),
            author=str(owner.get("name") or "未获取到发布者"),
        )

    # 动态详情接口返回嵌套结构，逐层取模块和正文信息。
    detail = await api.get(
        f"{api.BASE_URL}/x/polymer/web-dynamic/v1/detail",
        params={"id": target_id},
    )
    # 动态主体、模块、作者、正文、主内容逐层展开。
    item = detail.get("item") or {}
    # 动态主体、模块、作者、正文、主内容逐层展开。
    modules = item.get("modules") or {}
    # 作者信息来自 module_author。
    author = modules.get("module_author") or {}
    # 正文信息来自 module_dynamic。
    dynamic = modules.get("module_dynamic") or {}
    # 正文文字在 desc 节点。
    desc = dynamic.get("desc") or {}
    # 主内容在 major 节点。
    major = dynamic.get("major") or {}
    # 转发视频标题在 major.archive。
    archive = major.get("archive") or {}
    # 评论区标识在 basic 节点。
    basic = item.get("basic") or {}
    # 标题优先取转发的视频标题，其次取动态文字，纯转发无文字时给默认值。
    title = archive.get("title") or desc.get("text") or "无文字动态"
    # 评论区标识从 basic 模块取，缺失时回退到动态 ID 本身。
    oid = int(basic.get("comment_id_str") or basic.get("comment_id") or target_id)
    # 动态评论类型通常是 17，接口缺失时用该默认值。
    comment_type = int(basic.get("comment_type") or 17)
    # 组装动态目标返回。
    return LotteryTarget(
        target_type="dynamic",
        target_id=target_id,
        oid=oid,
        comment_type=comment_type,
        title=str(title).strip()[:120],
        author=str(author.get("name") or "未获取到发布者"),
    )