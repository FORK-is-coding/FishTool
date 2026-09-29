"""
UP主分析与账号自诊API路由

本模块提供UP主运营策略分析和账号自我诊断功能的HTTP接口，包括：
1. 头部UP主拆解：分析分区头部UP主的运营策略、内容特点、数据表现
2. 账号自我诊断：对比分析自己的账号数据与分区benchmark，发现增长瓶颈
3. 报告导出：生成结构化的分析报告（Markdown/PDF格式）

核心功能：
- 分区头部UP主排行：获取指定分区的头部UP主列表（基于粉丝数、播放量等指标）
# 读取数据并赋值给当前作用域变量
- UP主数据抓取：抓取UP主的基础信息、视频列表、数据指标（粉丝、播放、点赞等）
- 策略分析：使用LLM分析UP主的内容方向、更新频率、互动策略、商业模式
# 用新值覆盖旧值，保持数据一致
- Benchmark对比：将自己的数据与分区平均水平/头部UP主对比，量化差距
- 诊断报告生成：自动生成包含数据分析、问题诊断、改进建议的结构化报告

技术栈：
- FastAPI：异步Web框架
- UPDataFetcher：UP主数据抓取引擎，支持批量抓取、限流、异常重试
- StrategyAnalyzer：基于LLM的策略分析引擎
- SelfAnalyzer：账号自诊引擎，提供多维度对比分析
- ReportGenerator：报告生成器，支持Markdown和PDF输出

依赖关系：
- bilibili.api: B站API客户端
- bilibili.rate_limiter: API限流器，避免触发封禁
- modules.up_analyzer: UP主分析核心逻辑
- modules.self_diagnosis: 账号自诊核心逻辑
- llm.client: LLM客户端（用于策略分析，可选）

使用场景：
- UP主学习头部UP主的运营策略，寻找增长突破点
- MCN机构批量分析签约UP主的数据健康度
- 数据分析人员研究分区内容趋势和竞争格局
"""
from fastapi import APIRouter, HTTPException, BackgroundTasks
# 从 pydantic 导入符号
from pydantic import BaseModel, Field
# 从 typing 导入符号
from typing import Optional, Dict, Any, List
# 导入模块
import asyncio
import time
import uuid
# 导入模块
import logging

# 从 core.logger 导入符号
from core.logger import get_logger
# 从 core.config 导入符号
from core.config import config
from core.exceptions import ValidationError
from core.database import get_session, UPMaster
# 从 bilibili.api 导入符号
from bilibili.api import BilibiliAPI
# 从 bilibili.rate_limiter 导入符号
from bilibili.rate_limiter import RateLimiter
# 从 bilibili.cookie_pool 导入符号
from bilibili.cookie_pool import get_cookie_pool
# 从 llm.client 导入符号
from llm.client import LLMClient
# 从 modules.up_analyzer.data_fetcher 导入符号
from modules.up_analyzer.data_fetcher import UPDataFetcher
# 从 modules.up_analyzer.strategy_analyzer 导入符号
from modules.up_analyzer.strategy_analyzer import StrategyAnalyzer
# 从 modules.self_diagnosis.self_analyzer 导入符号
from modules.self_diagnosis.self_analyzer import SelfAnalyzer
# 从 modules.self_diagnosis.report_generator 导入符号
from modules.self_diagnosis.report_generator import ReportGenerator
from modules.self_diagnosis.ai_reporter import AIDiagnosisReporter

# 路由实例，挂载前缀 /api/analysis
router = APIRouter(prefix="/analysis", tags=["analysis"])
logger = get_logger(__name__)

# 全局实例（延迟初始化）
# 避免模块导入时就创建重量级客户端，首次请求时才初始化
bili_api: Optional[BilibiliAPI] = None
rate_limiter: Optional[RateLimiter] = None
_analysis_tasks: Dict[str, Dict[str, Any]] = {}


def _update_analysis_task(task_id: str, stage: str, progress: int, message: str) -> None:
    """记录UP主分析的真实业务阶段，并按已完成阶段实测耗时估算剩余时间。"""
    task = _analysis_tasks.get(task_id)
    if not task:
        return
    task.update(stage=stage, progress=progress, message=message)
    elapsed = max(time.monotonic() - task["started_monotonic"], 0.1)
    task["estimated_seconds"] = round(elapsed * (100 - progress) / progress) if progress >= 10 else None



def init_clients():
    """初始化客户端实例
    # 设置初始值/默认状态，避免后续空引用
    # 写入配置/属性，影响后续行为

    延迟初始化模式：首次调用时创建限流器、Cookie池和
    # 设置初始值/默认状态，避免后续空引用
    # 写入配置/属性，影响后续行为
    BilibiliAPI 客户端，后续请求复用全局实例。
    这样 Web 服务启动时不用等待网络相关初始化。
    # 设置初始值/默认状态，避免后续空引用
    # 写入配置/属性，影响后续行为
    """
    global bili_api, rate_limiter
    
    # 只初始化一次，后续直接复用
    if not bili_api:
        rate_limiter = RateLimiter()
        cookie_pool = get_cookie_pool()
        # 赋值并准备后续使用
        bili_api = BilibiliAPI(rate_limiter=rate_limiter, cookie_pool=cookie_pool)
        logger.info("[Analysis API] 客户端初始化完成")


# ========== 请求模型 ==========

class CategoryTopRequest(BaseModel):
    """分区头部UP主请求

    字段：
    - category: 分区名称，如美食/游戏/数码
    - limit: 返回数量，限制 1-50，默认 20
    # 将结果交回调用方
    """
    category: str = Field(..., description="分区名称，如美食/游戏/数码")
    # 赋值并准备后续使用
    limit: int = Field(20, ge=1, le=50, description="返回数量")


class UPAnalysisRequest(BaseModel):
    """UP主分析请求

    字段：
    - uid_or_url: 目标UP主的UID或主页链接，二者皆可
    """
    uid_or_url: str = Field(..., description="UP主UID或主页链接")


async def _run_analysis_task(task_id: str, request: UPAnalysisRequest) -> None:
    """后台执行数据抓取和AI策略分析，并在两项真实工作完成时推进状态。"""
    try:
        init_clients()
        _update_analysis_task(task_id, "fetching_profile", 10, "正在爬取UP主主页与视频数据")
        async with UPDataFetcher(bili_api, rate_limiter) as fetcher:
            up_data = await fetcher.fetch_up_data(request.uid_or_url)

        _update_analysis_task(task_id, "ai_research", 58, "正在接入AI大模型调研中")
        try:
            analysis_result = await StrategyAnalyzer().analyze_strategy(up_data)
        except Exception as exc:
            logger.warning(f"[API] AI大模型分析失败: {exc}")
            analysis_result = {
                "success": False,
                "error": "llm_not_configured",
                "message": "该功能需要配置AI大模型API",
            }

        _analysis_tasks[task_id].update(
            status="completed", stage="completed", progress=100,
            message="UP主拆解完成", estimated_seconds=0,
            result={"up_data": up_data, "analysis": analysis_result},
        )
    except Exception as exc:
        _analysis_tasks[task_id].update(
            status="failed", message=f"分析失败: {exc}", estimated_seconds=0
        )



class SelfDiagnosisRequest(BaseModel):
    """账号自诊请求

    字段：
    - uid: 自己的B站UID，必填
    - category: 对比分区（可选），不传则只出自身数据
    """
    uid: int = Field(..., description="自己的B站UID")
    # 赋值并准备后续使用
    category: Optional[str] = Field(None, description="对比分区（可选）")
    # 01 新增：传入冻结排名 run id 时读取同一 run 的 creator_ranking（不重采同行）
    benchmark_run_id: Optional[str] = Field(None, description="冻结排名 run id（可选）")


class ReportExportRequest(BaseModel):
    """报告导出请求

    字段：
    - uid: 要导出的B站UID
    - format: markdown 或 pdf
    - category: 对比分区（可选）
    """
    uid: int = Field(..., description="B站UID")
    # 赋值并准备后续使用
    format: str = Field("markdown", description="导出格式: markdown/pdf")
    # 赋值并准备后续使用
    category: Optional[str] = Field(None, description="对比分区")
    # 01 新增：导出报告时携带同一个冻结排名 run（拒绝跨 UID）
    benchmark_run_id: Optional[str] = Field(None, description="冻结排名 run id（可选）")


def _load_creator_ranking(uid: int, run_id: Optional[str]):
    """读取冻结排名结果（规格 §10.3）。

    不传 ``run_id`` 时返回 ``(None, 'not_requested')``：原自诊继续运行，界面提供
    「选择同行并计算」入口，而不是报错或删除入口。传了 ``run_id`` 时校验
    ``run.target_uid == uid`` 且 ``status == completed``，只读冻结结果，
    **绝不重新触发同行采集**。

    Args:
        uid: 账号自诊的 UID。
        run_id: 冻结排名 run id（可为 None）。

    Returns:
        tuple: ``(creator_ranking 或 None, benchmark_status)``。

    Raises:
        HTTPException: 404 run 不存在；409 跨 UID / 未完成；503 排名服务未启用。
    """
    if not run_id:
        return None, 'not_requested'
    # 延迟导入避免 web.routers 包内循环引用
    from web.routers import benchmark as benchmark_router

    service = benchmark_router.get_benchmark_service()
    row = service.read_result(run_id)
    if row is None:
        raise HTTPException(status_code=404, detail='benchmark_run_not_found')
    if int(row.get('target_uid') or 0) != int(uid):
        raise HTTPException(status_code=409, detail='benchmark_run_target_mismatch')
    if row.get('status') != 'completed' or not row.get('result'):
        raise HTTPException(status_code=409, detail='benchmark_run_not_completed')
    result = dict(row['result'])
    result['benchmark_run_id'] = run_id
    result['selection_as_of_s'] = row.get('selection_as_of_s')
    result['snapshot_hash'] = row.get('snapshot_hash')
    return result, 'completed'


# ========== API端点 ==========

@router.get("/categories")
async def get_categories():
    """获取支持的分区列表
    # 读取数据并赋值给当前作用域变量

    返回内置的分区名称与 tid 映射表，
    # 将结果交回调用方
    供前端下拉框选择分析分区。
    注意：'数码' 与 '科技' 都映射到 tid=188，
    这是B站官方的分区合并行为。
    """
    # 内置分区表：name 为展示名，tid 为B站API分区ID
    # 将内容呈现到界面上
    categories = [
        {'name': '美食', 'tid': 211},
        {'name': '游戏', 'tid': 4},
        {'name': '数码', 'tid': 188},
        {'name': '生活', 'tid': 160},
        {'name': '知识', 'tid': 36},
        {'name': '动画', 'tid': 1},
        {'name': '音乐', 'tid': 3},
        {'name': '舞蹈', 'tid': 129},
        {'name': '娱乐', 'tid': 5},
        {'name': '影视', 'tid': 181},
        {'name': '科技', 'tid': 188},
        {'name': '运动', 'tid': 234},
        {'name': '汽车', 'tid': 223},
        {'name': '时尚', 'tid': 155},
        {'name': '资讯', 'tid': 202},
        {'name': '鬼畜', 'tid': 119},
        {'name': '动物圈', 'tid': 217}
    ]
    
    return {
        'success': True,
        'data': categories
    }


@router.post("/category-top-ups")
async def get_category_top_ups(request: CategoryTopRequest):
    """获取分区头部UP主列表
    # 读取数据并赋值给当前作用域变量

    实现步骤：
    1. 初始化B站客户端（限流器 + Cookie池）
    # 设置初始值/默认状态，避免后续空引用
    # 写入配置/属性，影响后续行为
    2. 用 UPDataFetcher 异步抓取指定分区的头部UP主
    3. 返回UP主列表（含粉丝数、播放量等排序指标）
    # 将结果交回调用方

    异常处理：网络/接口异常统一转 500，带错误详情
    # 对数据进行加工/分发
    """
    try:
        # 确保客户端已初始化
        init_clients()
        
        logger.info(f"[API] 获取{request.category}分区头部UP主")
        
        # 使用 async with 确保 fetcher 生命周期内资源正确释放
        async with UPDataFetcher(bili_api, rate_limiter) as fetcher:
            # 赋值并准备后续使用
            up_list = await fetcher.fetch_category_top_ups(
                request.category, 
                request.limit
            )
        
        return {
            'success': True,
            'data': {
                'category': request.category,
                'count': len(up_list),
                'up_list': up_list
            }
        }
        
    except Exception as e:
        logger.error(f"[API] 获取分区UP主失败: {e}")
        # 抛出异常中断流程
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/analyze-up/tasks")
async def start_analysis_task(request: UPAnalysisRequest):
    """启动UP主拆解后台任务并返回任务 ID。"""
    task_id = uuid.uuid4().hex
    _analysis_tasks[task_id] = {
        "task_id": task_id,
        "status": "running",
        "stage": "queued",
        "progress": 2,
        "message": "分析任务已创建",
        "estimated_seconds": None,
        "started_monotonic": time.monotonic(),
        "result": None,
    }
    asyncio.create_task(_run_analysis_task(task_id, request))
    return {"success": True, "task_id": task_id}


@router.get("/analyze-up/tasks/{task_id}")
async def get_analysis_task(task_id: str):
    """返回UP主拆解任务的真实阶段状态。"""
    task = _analysis_tasks.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="分析任务不存在或已过期")
    return {"success": True, "data": {
        key: value for key, value in task.items() if key != "started_monotonic"
    }}


@router.post("/analyze-up")
async def analyze_up(request: UPAnalysisRequest):
    """分析UP主运营策略

    实现步骤：
    1. 抓取UP主基础数据（资料、视频列表、互动数据）
    2. 调用 LLM 做策略分析（内容方向/更新频率/互动策略）
    # 用新值覆盖旧值，保持数据一致
    3. LLM 未配置时降级返回提示，不影响主流程
    # 将结果交回调用方

    注意：LLM 分析失败只降级不阻断，
    原始 UP 数据始终返回给前端。
    # 将结果交回调用方
    """
    try:
        # 初始化客户端
        init_clients()
        
        logger.info(f"[API] 分析UP主: {request.uid_or_url}")
        
        # 1. 获取UP主数据
        # 支持 UID 或主页链接两种输入，由 fetcher 内部解析
        # 将数据从一种形态映射为另一种
        async with UPDataFetcher(bili_api, rate_limiter) as fetcher:
            # 赋值并准备后续使用
            up_data = await fetcher.fetch_up_data(request.uid_or_url)
        
        # 2. LLM策略分析
        # 独立 try：LLM 不可用（未配密钥/余额不足）时
        # 降级为提示信息，保证接口有数据可返回
        # 将结果交回调用方
        try:
            # 赋值并准备后续使用
            analyzer = StrategyAnalyzer()
            # 赋值并准备后续使用
            analysis_result = await analyzer.analyze_strategy(up_data)
        # 异常处理
        except Exception as e:
            logger.warning(f"[API] LLM分析失败: {e}")
            analysis_result = {
                'success': False,
                'error': 'llm_not_configured',
                'message': '该功能需要配置AI大模型API'
            }
        
        return {
            'success': True,
            'data': {
                'up_data': up_data,
                'analysis': analysis_result
            }
        }
        
    except (ValueError, ValidationError) as e:
        # 输入参数非法（如无效UID）由调用方抛 ValueError
        raise HTTPException(status_code=400, detail=str(e))
    except HTTPException:
        # HTTP 语义异常原样透传，避免被兜底成 500
        raise
    # 异常处理
    except Exception as e:
        logger.error(f"[API] UP主分析失败: {e}")
        # 抛出异常中断流程
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/self-diagnosis")
async def self_diagnosis(request: SelfDiagnosisRequest):
    """账号自我诊断

    实现步骤：
    1. 抓取自己账号的数据（粉丝/播放/互动等）
    2. 若指定了对比分区，与分区 benchmark 对比
    3. 返回自身数据 + 可选 benchmark 对比结果
    # 将结果交回调用方

    返回结构：
    # 将结果交回调用方
    - self_data: 自身账号完整数据
    - benchmark: 分区对比结果，未指定分区时为 None
    """
    try:
        # 初始化客户端
        init_clients()
        
        logger.info(f"[API] 账号自诊: {request.uid}")
        
        # 1. 获取自己账号数据
        # SelfAnalyzer 封装了抓取+指标计算
        # 对输入做运算得到结果
        analyzer = SelfAnalyzer(bili_api, rate_limiter)
        # 赋值并准备后续使用
        self_data = await analyzer.fetch_self_data(request.uid)
        
        # 2. benchmark对比（如果指定分区）
        # 旧实现曾用同分区热点标签频次作参照；该口径与播放量不可比，现由分析器返回停用态
        benchmark_data = None
        # 条件分支处理
        if request.category:
            # 赋值并准备后续使用
            benchmark_data = analyzer.benchmark_with_category(
                self_data, 
                request.category
            )
        
        # 3. AI调研独立降级，模型不可用时仍返回完整真实数据。
        ai_report = await AIDiagnosisReporter().generate(self_data)

        # 4. 冻结排名（可选）：传 run_id 时读同一 run；不传则保持 not_requested。
        creator_ranking, benchmark_status = _load_creator_ranking(request.uid, request.benchmark_run_id)

        return {
            'success': True,
            'data': {
                'self_data': self_data,
                'benchmark': benchmark_data,          # 旧兼容字段保持 disabled，不让旧 UI 复用
                'benchmark_status': benchmark_status,
                'creator_ranking': creator_ranking,
                'ai_report': ai_report,
            }
        }
        
    except HTTPException:
        # 排名 run 校验失败等 HTTP 语义异常原样透传，避免被兜底成 500
        raise
    except Exception as e:
        logger.error(f"[API] 账号自诊失败: {e}")
        # 抛出异常中断流程
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/export-report")
async def export_report(request: ReportExportRequest):
    """导出诊断报告

    实现步骤：
    1. 抓取账号数据
    2. 可选抓取 benchmark 对比数据
    3. 按 format 生成 Markdown 或 PDF 报告文件
    4. 返回报告文件路径
    # 将结果交回调用方

    注意：PDF 生成依赖 ReportGenerator 内部实现，
    可能要求环境安装额外依赖（如 reportlab）。
    """
    try:
        # 初始化客户端
        init_clients()
        
        logger.info(f"[API] 导出报告: UID={request.uid}, 格式={request.format}")
        
        # 1. 获取账号数据
        analyzer = SelfAnalyzer(bili_api, rate_limiter)
        # 赋值并准备后续使用
        self_data = await analyzer.fetch_self_data(request.uid)
        
        # 2. benchmark数据
        # 与自诊接口共用同一 benchmark 逻辑
        benchmark_data = None
        # 条件分支处理
        if request.category:
            # 赋值并准备后续使用
            benchmark_data = analyzer.benchmark_with_category(
                self_data, 
                request.category
            )
        
        # 3. 生成报告（携带同一冻结 run 时抛出 creator_ranking；保持旧位置参数不变）
        # 按 format 分支调用不同的生成器
        creator_ranking, benchmark_status = _load_creator_ranking(request.uid, request.benchmark_run_id)
        generator = ReportGenerator()

        if request.format == 'pdf':
            if creator_ranking is None:
                filepath = generator.generate_pdf_report(self_data, benchmark_data)
            else:
                filepath = generator.generate_pdf_report(self_data, benchmark_data, creator_ranking=creator_ranking)
        else:
            if creator_ranking is None:
                filepath = generator.save_markdown_report(self_data, benchmark_data)
            else:
                filepath = generator.save_markdown_report(self_data, benchmark_data, creator_ranking=creator_ranking)

        return {
            'success': True,
            'data': {
                'filepath': filepath,
                'format': request.format,
                'benchmark_status': benchmark_status,
                'benchmark_run_id': request.benchmark_run_id,
                'as_of': (creator_ranking or {}).get('selection_as_of_s'),
            }
        }
        
    except HTTPException:
        # 排名 run 校验失败（含跨 UID 拒绝）原样透传
        raise
    except Exception as e:
        logger.error(f"[API] 报告导出失败: {e}")
        # 抛出异常中断流程
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/llm-status")
async def get_llm_status():
    """检查LLM配置状态
    # 验证状态/条件，决定下一步分支

    返回当前 LLM 是否可用的探测结果：
    # 将结果交回调用方
    - configured=True：已配置模型与API地址
    - configured=False：未配置或配置无效，附带原因

    用于前端控制"策略分析"按钮的可用状态。
    """
    try:
        # 尝试实例化 LLMClient，成功即视为已配置
        llm_client = LLMClient()
        return {
            'success': True,
            'data': {
                'configured': True,
                'model': llm_client.model,
                'api_base': llm_client.api_base
            }
        }
    # 异常处理
    except Exception as e:
        # 配置缺失/无效时返回 false，附原因供前端提示
        # 将结果交回调用方
        return {
            'success': True,
            'data': {
                'configured': False,
                'message': str(e)
            }
        }
