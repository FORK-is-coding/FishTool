"""
热点发现模块 请求数据模型

拆分自 hotspot.py 原始 L143-L256。
"""
from pydantic import BaseModel, ConfigDict, Field
from typing import Any, Dict, List, Optional


# ============ 请求/响应数据模型 ============

class TagCloudRequest(BaseModel):
    """
    词云生成请求模型
    
    用于生成指定分区的热门tag词云数据，帮助UP主发现当前热点话题。
    
    Attributes:
        zone_name: 分区名称，如'游戏'、'科技'、'生活'等
                   必须是B站支持的标准分区名称
        limit: 拉取视频数量上限，默认100
               数值越大数据越全面，但耗时越长
               建议范围：50-500
        top_n: 输出top N个高频tag，默认50
               用于筛选最热门的tag，避免长尾噪音
    
    使用示例：
        request = TagCloudRequest(
            zone_name="游戏",
            limit=200,
            top_n=30
        )
    """
    zone_name: str  # 分区名称
    limit: int = 100  # 拉取视频数上限
    top_n: int = 50  # 输出top N个tag

class ActivityRequest(BaseModel):
    """
    活动拉取请求模型
    
    用于获取B站最新的官方活动和UGC活动情报。
    
    Attributes:
        include_ugc: 是否包含UGC活动，默认True
                     True: 同时拉取官方活动和UGC活动
                     False: 仅拉取官方活动
        zone: 分区筛选，默认'all'，取值：
              - all: 全部官号
              - game: 游戏区（各大游戏官号，如绝区零/明日方舟激励计划）
              - anime: 动画区（B站番剧/动画官号）
              - paint: 绘画区（画师同人站等蓝标UGC账号）
    
    数据来源：
    - 官方活动：B站官方运营的大型活动（如拜年祭、夏日绘卷）
    - UGC活动：按分区抓取对应官号动态中带活动关键词的内容
    
    使用场景：
    - UP主按自己创作分区寻找参与机会
    - 运营人员分析特定分区活动趋势
    """
    include_ugc: bool = True  # 是否包含UGC活动
    zone: str = 'all'  # 分区筛选: all/game/anime/paint

class TopicGenerateRequest(BaseModel):
    """
    AI选题生成请求模型
    
    基于创作方向和分区数据，生成AI推荐的创作选题。
    
    Attributes:
        direction: 创作方向描述，如'游戏攻略'、'科技评测'、'生活vlog'
                   越具体效果越好，建议包含内容类型+目标受众
        zone_name: 目标分区，如'游戏'、'科技'
                   用于获取分区热点数据作为选题依据
        count: 生成选题数量，默认10
               建议范围：5-20，过多会降低质量
        use_llm: 是否使用LLM生成，默认True
                 True: 使用AI生成有创意的选题
                 False: 仅基于数据统计生成基础选题
    
    工作流程：
    1. 获取目标分区的热点数据（热门视频、tag、趋势）
    2. 如果use_llm=True，调用LLM结合创作方向生成选题
    3. 如果use_llm=False或LLM不可用，使用规则生成基础选题
    4. 返回包含标题、描述、数据支撑的结构化选题
    
    使用示例：
        request = TopicGenerateRequest(
            direction="搞笑游戏实况，面向年轻观众",
            zone_name="游戏",
            count=10,
            use_llm=True
        )
    """
    # 第三批 g：带键请求 
    # ``extra=forbid``——客户端只能提交声明过的字段，
    # 不能往请求里塞 ``phase`` / 指标 / 已验证 deadline 等服务器真值。
    model_config = ConfigDict(extra="forbid")

    direction: str  # 创作方向描述
    zone_name: str  # 目标分区
    count: int = 10  # 生成数量
    use_llm: bool = True  # 是否使用LLM
    # 第三批 g：生成幂等键。event 模式必填；tag_only 无键时留 None 走旧兼容路径。
    generation_request_id: Optional[str] = None  # 客户端 UUID 幂等键
    opportunity_run_id: Optional[str] = None  # event 模式：关联机会运行 ID
    selected_event_ids: Optional[List[str]] = None  # 选中的事件 ID（需 run_id）
    context_mode: str = "current"  # current / historical

class TopicUpdateRequest(BaseModel):
    """
    选题状态更新请求模型
    
    用于更新选题库中选题的状态，跟踪选题生命周期。
    
    Attributes:
        status: 选题状态，支持以下值：
                - 'pending': 待审核（新生成的选题初始状态）
                - 'adopted': 已采纳（UP主决定使用该选题）
                - 'published': 已发布（基于该选题的视频已发布）
    
    状态流转：
        pending → adopted → published
                ↓
              rejected（可选）
    
    使用场景：
    - UP主从选题库选择创作方向
    - 跟踪选题效果，分析哪些选题更受欢迎
    """
    status: str  # 选题状态: pending/adopted/published

class WatchCreateRequest(BaseModel):
    """
    手动加入单视频跟踪请求模型（02 · 批 4）

    用于把某个 bvid 手动加入持续跟踪池；同一 bvid 重复提交走库里幂等 UPSERT，
    不会产生重复行，也不会重置已有的调度 / 到期时间。

    Attributes:
        bvid: 视频 BV 号（必填）；入库前 trim，长度上限 20。
        collection_tid: 采集归属分区 ID，可选；None 时不写（保留库内既有值）。
        sample_interval_s: 采样间隔（秒），可选；None 时用 watch_store 默认 3600。

    使用示例:
        request = WatchCreateRequest(bvid="BV1xx411c7mD", collection_tid=4)
    """
    bvid: str  # 视频BV号（必填）
    collection_tid: Optional[int] = None  # 采集分区ID（可选）
    sample_interval_s: Optional[int] = None  # 采样间隔秒（可选，缺省 3600）


# ============ 第三批 g：事件 / 机会 / 反馈请求模型 ============


class _StrictRequest(BaseModel):
    """禁止未知字段的请求基类（extra=forbid）。

    第三批 g 硬口径：客户端只能提交自己有权提交的字段，**不能**通过额外字段
    自填 phase / 指标 / evidence 真值 / 已验证 deadline 等服务器真值。
    """

    model_config = ConfigDict(extra="forbid")


class EventCreateRequest(_StrictRequest):
    """创建事件草稿 / 激活规则。

    Attributes:
        name: 事件名（必填）。
        event_id: 可选客户端 ID；缺省由服务端生成。
        status: draft / active（受控枚举）。
        entity_scope / source_policy / links: 整对象 JSON（服务端只存不解释真值）。
    """

    name: str = Field(..., min_length=1, max_length=200)
    event_id: Optional[str] = Field(default=None, max_length=64)
    status: str = Field(default="draft", max_length=16)
    entity_scope: Optional[Dict[str, Any]] = None
    source_policy: Optional[Dict[str, Any]] = None
    links: Optional[Dict[str, Any]] = None


class EventPatchRequest(_StrictRequest):
    """PATCH /events/{id}：CAS 新规则版本 / 暂停。

    expected_revision 不匹配 → 409；事件不存在 → 404。
    """

    expected_revision: int
    name: Optional[str] = Field(default=None, min_length=1, max_length=200)
    status: Optional[str] = Field(default=None, max_length=16)
    source_policy: Optional[Dict[str, Any]] = None
    links: Optional[Dict[str, Any]] = None


class MemberDecision(_StrictRequest):
    """单条成员决定（bvid + proposed / accepted / rejected）。"""

    bvid: str = Field(..., min_length=1, max_length=20)
    status: str = Field(..., max_length=16)
    evidence: Optional[Dict[str, Any]] = None


class MemberDecisionsRequest(_StrictRequest):
    """批量 append 成员决定（CAS revision）。"""

    expected_revision: int
    decisions: List[MemberDecision] = Field(default_factory=list)


class AssessTaskRequest(_StrictRequest):
    """POST /events/{id}/assess/tasks：固定 as_of 后台计算 assessment。"""

    as_of_s: Optional[int] = None
    window_kind: str = Field(default="daily24h", max_length=16)


class OpportunityTaskRequest(_StrictRequest):
    """POST /opportunities/tasks：CreatorBrief + event ids → 冻结 OpportunityRun。"""

    creator_brief: Dict[str, Any]
    event_ids: List[str] = Field(default_factory=list)
    as_of_s: Optional[int] = None


class FeedbackRequest(_StrictRequest):
    """POST /opportunities/{id}/feedback：采用 / 拒绝 / 发布 / 结果（append 且幂等）。

    固定九字段；feedback_id + expected_revision + 服务器 created_s
    **不参与** 内容 hash（见路由层规范化）。
    """

    feedback_id: str = Field(..., min_length=1, max_length=64)
    expected_revision: int
    event_id: str = Field(..., min_length=1, max_length=64)
    topic_id: str = Field(..., min_length=1, max_length=64)
    kind: str = Field(..., max_length=16)
    reason: str = Field(..., max_length=2000)
    published_bvid: Optional[str] = Field(default=None, max_length=20)
    actual_production_hours: Optional[float] = None
    outcome_metrics: Optional[Dict[str, Any]] = None
