"""
评论监控模块 API 路由

本模块提供评论监控和分析相关的Web API端点，包括：
1. 评论采集：从指定视频抓取评论数据，支持多种采集策略（快速/标准/完整）
2. 智能监控：实时监控视频评论，自动识别异常评论（负面、spam、敏感词）
3. 批量监控：同时监控多个视频或整个账号的评论动态
4. 预警管理：查询和管理评论预警记录，支持已读标记
5. 自定义规则：添加自定义关键词，个性化预警策略

核心功能：
- 评论去重：自动过滤重复、相似评论
- 情感分析：识别负面评论并分级预警（可选LLM增强）
- 敏感词检测：内置敏感词库，实时拦截风险评论
- 实时推送：异常评论触发实时推送通知

技术栈：
- FastAPI：异步Web框架
- CommentCollector：评论采集引擎，支持分页、去重、限流
- CommentMonitor：监控引擎，集成情感分析、去重、预警
- LLMClient：AI情感分析（可选）

依赖关系：
- bilibili.api: B站API客户端
- modules.comment: 评论采集和监控核心逻辑
- llm.client: LLM客户端（可选，用于增强情感分析）
- web.main.push_alert: 预警推送回调

使用场景：
- UP主实时监控视频评论区，及时发现并处理负面评论
- 运营人员批量监控账号动态，维护社区氛围
- 数据分析人员获取结构化评论数据
"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import List, Optional

from bilibili.api import BilibiliAPI
from bilibili.cookie_pool import get_cookie_pool
from llm.client import LLMClient
from core.config import ConfigManager
from core.database import Comment, Video, get_session
from modules.comment import CommentCollector, CommentMonitor

router = APIRouter()

# ============ 全局实例（延迟初始化模式） ============
# 采用单例模式管理API、LLM、监控器实例，避免重复初始化
_api: Optional[BilibiliAPI] = None  # B站API客户端
_llm_client: Optional[LLMClient] = None  # LLM客户端（可选）
_monitor: Optional[CommentMonitor] = None  # 评论监控器


def get_api() -> BilibiliAPI:
    """
    获取B站API客户端实例（单例模式）
    
    功能说明：
    - 首次调用时创建BilibiliAPI实例并初始化cookie池
    - 后续调用直接返回缓存实例
    - 保证整个应用生命周期内只有一个API客户端
    
    Returns:
        BilibiliAPI: 已初始化的B站API客户端
        
    实现细节：
    - 使用全局变量缓存实例
    - 自动从cookie池获取可用cookie
    - 线程安全性：FastAPI单进程模型下无需加锁
    """
    global _api
    if _api is None:
        # 首次初始化：从cookie池获取cookie并创建API实例
        cookie_pool = get_cookie_pool()
        _api = BilibiliAPI(cookie_pool=cookie_pool)
    return _api


def get_monitor() -> CommentMonitor:
    """
    获取评论监控器实例（单例模式）
    
    功能说明：
    - 首次调用时初始化CommentMonitor实例
    - 自动尝试初始化LLM客户端（用于情感分析增强）
    - 注册预警回调函数（用于实时推送）
    
    Returns:
        CommentMonitor: 已初始化的评论监控器
        
    依赖关系：
    - BilibiliAPI: 用于抓取评论数据
    - LLMClient: 可选，用于AI情感分析
    - push_alert: 预警回调，触发实时推送
    
    工作流程：
    1. 尝试初始化LLM客户端（失败则设为None）
    2. 从web.main导入push_alert回调函数
    3. 创建CommentMonitor实例，传入API、LLM、回调
    4. 缓存实例供后续调用
    
    容错设计：
    - LLM初始化失败不影响监控器创建
    - 监控器会根据LLM是否可用自动选择分析策略
    
    注意事项：
    - 延迟导入push_alert避免循环依赖
    - 监控器实例贯穿整个应用生命周期
    """
    global _monitor, _llm_client
    if _monitor is None:
        # 尝试初始化LLM客户端（用于AI情感分析）
        try:
            _llm_client = LLMClient()
        except Exception:
            # LLM初始化失败（如API Key未配置），设为None
            # 监控器仍可使用规则引擎进行基础分析
            _llm_client = None
        
        # 延迟导入避免循环依赖
        # push_alert是web.main中定义的预警推送函数
        from ..main import push_alert
        
        # 创建监控器实例
        _monitor = CommentMonitor(
            get_api(),  # B站API客户端
            _llm_client,  # LLM客户端（可能为None）
            alert_callback=push_alert  # 预警回调函数
        )
    return _monitor


# ============ 请求/响应数据模型 ============

class CollectRequest(BaseModel):
    """
    评论采集请求模型
    
    用于指定视频的评论采集策略和参数。
    
    Attributes:
        bvid: 视频BV号，如'BV1xx411c7mD'
              必须是有效的B站视频标识符
        strategy: 采集策略，支持以下值：
                  - 'fast': 快速模式，仅采集热门评论（最快，数据量小）
                  - 'normal': 标准模式，采集热门+部分新评论（平衡）
                  - 'full': 完整模式，采集所有评论（最慢，数据最全）
                  默认'normal'
        max_count: 最大采集数量（可选）
                   None表示不限制，会受限于strategy的默认上限
                   建议范围：100-5000
    
    策略对比：
    - fast: 适合快速预览，采集100-200条热门评论
    - normal: 适合日常监控，采集500-1000条评论
    - full: 适合深度分析，采集所有评论（可能数万条）
    
    性能考虑：
    - fast模式：1-2秒
    - normal模式：3-10秒
    - full模式：10秒-数分钟（取决于评论总数）
    
    使用示例：
        request = CollectRequest(
            bvid="BV1xx411c7mD",
            strategy="normal",
            max_count=1000
        )
    """
    bvid: str  # 视频BV号
    strategy: str = "normal"  # 采集策略: fast/normal/full
    max_count: Optional[int] = None  # 最大采集数量


class MonitorRequest(BaseModel):
    """
    单视频监控请求模型
    
    用于配置对单个视频的实时评论监控。
    
    Attributes:
        bvid: 视频BV号
        enable_dedup: 是否启用去重，默认True
                      True: 过滤重复和高度相似的评论
                      False: 保留所有评论（可能产生大量重复数据）
        enable_sentiment: 是否启用情感分析，默认True
                          True: 对评论进行情感分析，识别负面评论
                          False: 跳过情感分析（提升性能）
        strategy: 采集策略，支持以下值：
                  - 'normal': 快速（默认），热门+100条普通评论
                  - 'full': 全面，全量采集所有评论（可能数千条）
                  默认'normal'
    
    功能说明：
    - 去重：基于内容相似度算法，过滤复制粘贴、刷屏评论
    - 情感分析：识别负面、中性、正面评论，负面评论触发预警
    
    预警触发条件：
    - 检测到敏感词
    - 情感分析判定为负面（且置信度>阈值）
    - 短时间内大量相似评论（疑似水军）
    
    使用场景：
    - UP主监控新发布视频的评论区
    - 运营人员监控热点视频动态
    """
    bvid: str  # 视频BV号
    enable_dedup: bool = True  # 是否启用去重
    enable_sentiment: bool = True  # 是否启用情感分析
    strategy: str = "normal"  # 采集策略: normal(快速100条)/full(全面全量)


class ResidentMonitorRequest(BaseModel):
    """常驻监控控制请求；bvids 为空时沿用 SQLite 中已保存的目标。"""
    bvids: Optional[List[str]] = None
class AccountMonitorRequest(BaseModel):
    """
    账号监控请求模型
    
    用于监控指定UP主账号下所有视频的评论动态。
    
    Attributes:
        uid: UP主用户ID，如'12345678'
             可以从UP主主页URL中获取
        video_limit: 监控视频数量上限，默认10
                     从最新视频开始往前取
                     建议范围：5-50
        strategy: 采集策略，normal(快速100条)/full(全面全量)，默认normal
    
    工作流程：
    1. 从UP主空间获取最新的N个视频
    2. 对每个视频执行评论采集和分析
    3. 汇总所有异常评论
    4. 生成账号级别的监控报告
    
    监控维度：
    - 整体评论情感倾向
    - 高频负面评论关键词
    - 疑似水军/黑粉行为
    - 评论活跃度趋势
    
    使用场景：
    - UP主日常运营，全面了解粉丝反馈
    - MCN机构监控签约UP主的评论健康度
    - 危机公关，快速定位负面舆情源头
    """
    uid: str  # UP主用户ID
    video_limit: int = 10  # 监控视频数量上限
    strategy: str = "normal"  # 采集策略: normal(快速100条)/full(全面全量)


class AddKeywordsRequest(BaseModel):
    """
    添加关键词请求模型
    
    用于自定义预警关键词，个性化监控规则。
    
    Attributes:
        keywords: 关键词列表，如['黑你', '取关', '骗子']
                  支持中文、英文、符号
                  不区分大小写
    
    功能说明：
    - 监控器会实时检测评论中是否包含这些关键词
    - 匹配成功则触发预警（级别为'medium'）
    - 关键词在当前会话中生效，重启后需重新添加
    
    使用场景：
    - UP主添加行业特定的敏感词
    - 针对特定事件添加临时监控词
    - 补充系统内置敏感词库的不足
    
    注意事项：
    - 关键词不持久化，仅内存存储
    - 过多关键词可能影响性能（建议<100个）
    - 关键词采用精确匹配，不支持正则
    """
    keywords: List[str]  # 关键词列表


# ============ 路由端点 ============

@router.post("/collect")
async def collect_comments(request: CollectRequest):
    """
    采集视频评论
    
    功能说明：从指定视频抓取评论数据，支持多种采集策略。
    
    Args:
        request: CollectRequest对象
    
    Returns:
        dict: {
            "success": True,
            "bvid": str,  # 视频BV号
            "count": int,  # 采集到的评论数量
            "comments": List[dict]  # 评论列表
        }
    
    Raises:
        HTTPException(400): 采集策略无效
        HTTPException(500): 采集失败（如视频不存在、API限流）
    
    工作流程：
    1. 验证采集策略是否合法（fast/normal/full）
    2. 创建CommentCollector实例
    3. 根据策略调用B站API分页拉取评论
    4. 返回结构化评论数据（包含用户、时间、点赞数等）
    
    性能优化：
    - 异步并发拉取多页评论
    - 自动限流避免触发API封禁
    - 支持断点续传（API分页机制）
    """
    try:
        # 获取API客户端
        api = get_api()
        # 创建评论采集器
        collector = CommentCollector(api)
        
        # 验证采集策略是否合法
        valid_strategies = [
            CommentCollector.STRATEGY_FAST,  # 快速模式
            CommentCollector.STRATEGY_NORMAL,  # 标准模式
            CommentCollector.STRATEGY_FULL  # 完整模式
        ]
        if request.strategy not in valid_strategies:
            # 策略无效，返回400错误
            raise HTTPException(status_code=400, detail=f"无效的采集策略: {request.strategy}")
        
        # 执行评论采集（异步调用）
        comments = await collector.collect_video_comments(
            bvid=request.bvid,  # 视频BV号
            strategy=request.strategy,  # 采集策略
            max_count=request.max_count  # 最大采集数量
        )
        
        # 保存失败不会伪装为成功；警告保留采集结果供前端提示。
        save_result = collector.last_save_result
        return {
            "success": True,
            "bvid": request.bvid,
            "count": len(comments),
            "warning": save_result.get('warning'),
            "persistence": save_result,
            "comments": comments
        }
        
    except HTTPException:
        # 参数校验等 HTTP 语义异常原样透传，避免被兜底成 500
        raise
    except Exception as e:
        # 采集失败（网络错误、视频不存在、API限流等）
        raise HTTPException(status_code=500, detail=f"采集评论失败: {str(e)}")


@router.post("/monitor")
async def monitor_video(request: MonitorRequest):
    """
    监控单个视频
    
    功能说明：启动对单个视频的实时评论监控，自动识别异常评论。
    
    Args:
        request: MonitorRequest对象
    
    Returns:
        dict: {
            "success": True,
            "data": {
                "bvid": str,
                "total_comments": int,  # 评论总数
                "alerts": List[dict],  # 预警列表
                "sentiment_summary": dict,  # 情感分析汇总
                "top_negative_keywords": List[str]  # 高频负面词
            }
        }
    
    Raises:
        HTTPException(500): 监控失败
    
    监控内容：
    - 敏感词检测：匹配内置+自定义敏感词库
    - 情感分析：识别负面评论（可选LLM增强）
    - 去重分析：过滤重复刷屏评论
    - 水军识别：检测短时间内大量相似评论
    
    预警级别：
    - high: 严重负面、明确攻击性言论
    - medium: 一般负面、敏感词匹配
    - low: 疑似负面、需人工复核
    """
    try:
        # 获取监控器实例
        monitor = get_monitor()
        
        # 校验采集策略合法性：只允许 normal(快速)/full(全面)，与前端下拉栏选项一致
        valid_strategies = [
            CommentCollector.STRATEGY_NORMAL,
            CommentCollector.STRATEGY_FULL,
        ]
        if request.strategy not in valid_strategies:
            raise HTTPException(status_code=400, detail=f"无效的采集策略: {request.strategy}")
        
        # 执行视频监控（异步调用），采集策略透传给采集器
        result = await monitor.monitor_video(
            bvid=request.bvid,  # 视频BV号
            enable_dedup=request.enable_dedup,  # 是否启用去重
            enable_sentiment=request.enable_sentiment,  # 是否启用情感分析
            strategy=request.strategy  # 采集策略: normal(快速100条)/full(全面全量)
        )
        
        # 成功返回监控结果
        return {
            "success": True,
            "data": result
        }
        
    except HTTPException:
        # 参数校验等 HTTP 语义异常原样透传，避免被兜底成 500
        raise
    except Exception as e:
        # 监控失败
        raise HTTPException(status_code=500, detail=f"监控失败: {str(e)}")


@router.get("/monitor/progress")
async def get_monitor_progress(bvid: str):
    """查询评论采集进度（供前端进度条轮询）。

    返回与日志同源的实时进度：
    - phase: hot/normal/full/done
    - collected: 已采集条数
    - limit: 快速模式条数上限；None 表示全量
    - finished: 是否完成

    Args:
        bvid: 视频BV号

    Returns:
        dict: 进度快照；无记录时返回空进度。
    """
    # 直接从采集器内存进度表读取，保证与"已采集 N 条"日志同源
    progress = CommentCollector.get_progress(bvid)
    return {
        "success": True,
        "data": progress,
    }


@router.get("/dashboard")
async def get_comment_dashboard(bvid: Optional[str] = None):
    """读取已落库评论并生成可视化大屏数据。

    Args:
        bvid: 可选 BV 号；未传时自动选择最近有评论数据的视频。

    Returns:
        dict: 统一包含 video 与 visualization 的前端字段契约。

    Raises:
        HTTPException: 数据库读取失败时返回 500。
    """
    session = get_session()
    try:
        # 优先使用用户指定视频；未指定时选择最近有评论记录的视频。
        video_query = session.query(Video)
        if bvid:
            video = video_query.filter(Video.bvid == bvid).first()
        else:
            video = (
                video_query.join(Comment, Comment.video_id == Video.id)
                .order_by(Comment.created_at.desc())
                .first()
            )

        if not video:
            return {
                "success": True,
                "data": {
                    "video": None,
                    "visualization": CommentMonitor._empty_visualization_data(),
                    "message": "暂无已落库评论数据",
                },
            }

        rows = (
            session.query(Comment)
            .filter(Comment.video_id == video.id)
            .order_by(Comment.ctime.asc())
            .all()
        )
        comments = [
            {
                "rpid": row.rpid,
                "uid": row.uid,
                "uname": row.uname,
                "content": row.content,
                "ctime": row.ctime,
                "like": row.like or 0,
                "sentiment": row.sentiment or "neutral",
                "sentiment_score": row.sentiment_score,
                "is_duplicate": bool(row.is_duplicate),
                "duplicate_count": row.duplicate_count or 1,
            }
            for row in rows
        ]

        # 历史数据也走同一去重器与统计构造器，避免实时/历史接口字段漂移。
        monitor = get_monitor()
        dedup_result = monitor.deduplicator.deduplicate(comments) if comments else None
        processed = dedup_result.get("deduplicated_comments", comments) if dedup_result else comments
        visualization = monitor._build_visualization_data(comments, processed, dedup_result)
        return {
            "success": True,
            "data": {
                "video": {"bvid": video.bvid, "title": video.title or video.bvid},
                "visualization": visualization,
                "message": "已加载历史监控数据" if comments else "该视频暂无评论数据",
            },
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"读取评论大屏失败: {exc}") from exc
    finally:
        session.close()


@router.get("/resident/status")
async def resident_monitor_status():
    """读取评论区常驻监控状态卡片数据。"""
    from ..main import monitor_service
    if monitor_service is None:
        raise HTTPException(status_code=503, detail="常驻监控服务尚未初始化")
    return {"success": True, "data": monitor_service.snapshot()}


@router.post("/resident/enable")
async def resident_monitor_enable(request: ResidentMonitorRequest):
    """开启评论区常驻监控并可更新目标 BV 列表。"""
    from ..main import monitor_service
    if monitor_service is None:
        raise HTTPException(status_code=503, detail="常驻监控服务尚未初始化")
    return {"success": True, "data": await monitor_service.enable(request.bvids)}


@router.post("/resident/pause")
async def resident_monitor_pause():
    """暂停评论区常驻采集，保留目标与统计。"""
    from ..main import monitor_service
    if monitor_service is None:
        raise HTTPException(status_code=503, detail="常驻监控服务尚未初始化")
    return {"success": True, "data": await monitor_service.pause()}


@router.post("/resident/stop")
async def resident_monitor_stop():
    """停止评论区常驻采集。"""
    from ..main import monitor_service
    if monitor_service is None:
        raise HTTPException(status_code=503, detail="常驻监控服务尚未初始化")
    return {"success": True, "data": await monitor_service.stop()}


@router.post("/monitor/account")
async def monitor_account(request: AccountMonitorRequest):
    """
    监控账号的所有视频
    
    功能说明：监控指定UP主最近发布的视频，生成账号级监控报告。
    
    Args:
        request: AccountMonitorRequest对象
    
    Returns:
        dict: {
            "success": True,
            "data": {
                "uid": str,  # UP主ID
                "username": str,  # UP主昵称
                "monitored_videos": int,  # 监控视频数
                "total_alerts": int,  # 总预警数
                "overall_sentiment": str,  # 整体情感倾向
                "videos": List[dict]  # 各视频详情
            }
        }
    
    工作流程：
    1. 从UP主空间获取最新视频列表
    2. 对每个视频执行评论监控
    3. 汇总分析结果
    4. 生成账号健康度报告
    
    使用场景：
    - UP主日常运营巡检
    - MCN机构监控签约UP主
    - 危机公关快速定位问题
    """
    try:
        # 获取监控器实例
        monitor = get_monitor()
        
        # 校验采集策略合法性：只允许 normal(快速)/full(全面)，与单视频监控保持一致
        valid_strategies = [
            CommentCollector.STRATEGY_NORMAL,
            CommentCollector.STRATEGY_FULL,
        ]
        if request.strategy not in valid_strategies:
            raise HTTPException(status_code=400, detail=f"无效的采集策略: {request.strategy}")
        
        # 执行账号监控（异步调用），采集策略透传给每个视频的采集流程
        result = await monitor.monitor_user_account(
            uid=request.uid,  # UP主用户ID
            video_limit=request.video_limit,  # 监控视频数量上限
            strategy=request.strategy  # 采集策略: normal(快速100条)/full(全面全量)
        )
        
        # 成功返回账号监控结果
        return {
            "success": True,
            "data": result
        }
        
    except HTTPException:
        # 参数校验等 HTTP 语义异常原样透传，避免被兜底成 500
        raise
    except Exception as e:
        # 账号监控失败
        raise HTTPException(status_code=500, detail=f"账号监控失败: {str(e)}")


@router.get("/alerts")
async def get_alerts(
    bvid: Optional[str] = None,
    level: Optional[str] = None,
    is_read: Optional[bool] = None,
    limit: int = 50
):
    """
    查询预警记录
    
    功能说明：从数据库查询历史预警记录，支持多维度筛选。
    
    Args:
        bvid: 视频BV号筛选（可选）
        level: 预警级别筛选（可选）：high/medium/low
        is_read: 已读状态筛选（可选）：True/False/None
        limit: 返回数量上限，默认50
    
    Returns:
        dict: {
            "success": True,
            "alerts": List[dict],  # 预警列表
            "count": int  # 返回数量
        }
    
    预警记录包含：
    - id: 预警ID
    - bvid: 关联视频
    - comment_text: 评论内容
    - reason: 预警原因（敏感词/负面情感/水军）
    - level: 预警级别
    - created_at: 触发时间
    - is_read: 是否已读
    
    使用场景：
    - 查看未读预警
    - 回顾历史预警
    - 分析预警趋势
    """
    try:
        # 获取监控器实例
        monitor = get_monitor()
        
        # 查询预警记录（异步调用）
        alerts = await monitor.get_alerts(
            bvid=bvid,  # 视频筛选
            level=level,  # 级别筛选
            is_read=is_read,  # 已读状态筛选
            limit=limit  # 数量上限
        )
        
        # 成功返回预警列表
        return {
            "success": True,
            "alerts": alerts,
            "count": len(alerts)
        }
        
    except Exception as e:
        # 查询失败
        raise HTTPException(status_code=500, detail=f"查询预警失败: {str(e)}")


@router.put("/alerts/{alert_id}/read")
async def mark_alert_read(alert_id: int):
    """
    标记预警为已读
    
    功能说明：将指定预警标记为已读状态，用于预警管理。
    
    Args:
        alert_id: 预警ID
    
    Returns:
        dict: {
            "success": True,
            "message": str
        }
    
    Raises:
        HTTPException(404): 预警不存在
        HTTPException(500): 标记失败
    
    使用场景：
    - UP主处理完预警后标记已读
    - 批量标记已处理的预警
    """
    try:
        # 获取监控器实例
        monitor = get_monitor()
        
        # 标记预警为已读（异步调用）
        success = await monitor.mark_alert_read(alert_id)
        
        # 检查预警是否存在
        if not success:
            # 预警不存在，返回404
            raise HTTPException(status_code=404, detail="预警不存在")
        
        # 成功标记
        return {
            "success": True,
            "message": "预警已标记为已读"
        }
        
    except HTTPException:
        # 重新抛出HTTP异常
        raise
    except Exception as e:
        # 其他错误
        raise HTTPException(status_code=500, detail=f"标记失败: {str(e)}")


@router.post("/keywords")
async def add_custom_keywords(request: AddKeywordsRequest):
    """
    添加自定义预警关键词
    
    功能说明：添加自定义敏感词到监控器，个性化预警规则。
    
    Args:
        request: AddKeywordsRequest对象
    
    Returns:
        dict: {
            "success": True,
            "message": str,  # 操作结果
            "keywords": List[str]  # 已添加的关键词
        }
    
    Raises:
        HTTPException(500): 添加失败
    
    工作流程：
    1. 验证关键词格式（非空、去重）
    2. 添加到监控器的关键词库
    3. 后续监控会实时检测这些关键词
    
    注意事项：
    - 关键词仅内存存储，重启后失效
    - 不区分大小写
    - 支持中文、英文、符号
    
    使用场景：
    - 针对特定事件添加临时监控词
    - 补充系统内置敏感词库
    """
    try:
        # 获取监控器实例
        monitor = get_monitor()
        
        # 添加自定义关键词（同步调用）
        monitor.add_custom_keywords(request.keywords)
        
        # 成功添加
        return {
            "success": True,
            "message": f"已添加 {len(request.keywords)} 个关键词",
            "keywords": request.keywords
        }
        
    except Exception as e:
        # 添加失败
        raise HTTPException(status_code=500, detail=f"添加关键词失败: {str(e)}")