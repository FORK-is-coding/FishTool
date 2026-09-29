"""
诊断报告生成器 - 生成PDF/Markdown格式的账号诊断报告

本模块将 SelfAnalyzer 采集的账号数据渲染为可读报告：

一、Markdown 报告（generate_markdown_report）
七段结构：
1. 标题区：生成时间/UID/账号名
2. 数据可得性清单：已获取 vs 不可获取（创作者中心专属）
# 读取数据并赋值给当前作用域变量
3. 账号概览：粉丝/关注/等级/认证
4. 投稿数据分析：总量/均值/最高播放视频
5. 投稿节奏：周更频率/断更/近30天 + 自动评价
6. 互动指标：触达率/评论率/收藏率 + 分级评价
7. Benchmark 对比（可选）：分位数定位 + 排名
8. 改进建议：基于数据自动生成 4 类建议
9. 结束语

二、PDF 报告（generate_pdf_report）
- markdown2 转 HTML + pdfkit 转 PDF
- 依赖缺失时自动降级为 Markdown
- 内置 B 站主题 CSS（蓝粉配色）

三、文件保存（save_markdown_report）
# 持久化数据，防止丢失
- 输出目录 reports/（自动创建）
# 实例化对象并准备使用
- 默认文件名 diagnosis_report_{uid}_{时间戳}.md

依赖：
- markdown2 / pdfkit / wkhtmltopdf（PDF 可选，缺失降级）
"""
import asyncio
# 从 typing 导入符号
from typing import Dict, Any, Optional
# 从 datetime 导入符号
from datetime import datetime
# 从 pathlib 导入符号
from pathlib import Path
# 导入模块
import logging

# 从 core.logger 导入符号
from core.logger import get_logger

logger = get_logger(__name__)


def _format_metric(value: Any, *, suffix: str = "") -> str:
    """None 感知数值格式化：缺失 -> 暂无数据，真实 0 -> 0（规格 §5.5）。"""
    if value is None or isinstance(value, bool):
        return "暂无数据"
    if isinstance(value, (int, float)):
        return f"{value:,}{suffix}"
    return "暂无数据"


#: 目标无法排名时的原因文案（区分「未知」与「真实 0」，规格 §10.5）
RANK_UNAVAILABLE_REASONS = {
    'insufficient_posts': '窗口内有效稿件不足最少条数',
    'incomplete_selection': '无法证明选稿完整',
    'missing_selected_metrics': '选中稿件缺少播放指标',
    'error': '采集失败',
}


def _format_ranking_metric(value: Any) -> str:
    """排名专用数值格式化：None / bool -> 「无法计算」，真实 0 保持 0。

    Args:
        value: 指标值（中位累计播放等）。

    Returns:
        str: 展示文本；缺失时必须是「无法计算」而不是 0。
    """
    if value is None or isinstance(value, bool):
        return '无法计算'
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if isinstance(value, (int, float)):
        return f"{value:,}"
    return '无法计算'


def _format_ranking_percent(value: Any) -> str:
    """百分位格式化：整数值去掉多余小数（30.0 -> 30%），缺失 -> 「无法计算」。

    Args:
        value: 百分位数值。

    Returns:
        str: 展示文本。
    """
    if value is None or isinstance(value, bool):
        return '无法计算'
    if isinstance(value, float):
        return f"{int(value)}%" if value.is_integer() else f"{round(value, 1)}%"
    if isinstance(value, int):
        return f"{value}%"
    return '无法计算'


class ReportGenerator:
    """诊断报告生成器 - 支持PDF和Markdown格式"""
    
    def __init__(self, output_dir: str = "reports"):
        """初始化报告生成器
        # 设置初始值/默认状态，避免后续空引用
        
        Args:
            output_dir: 报告输出目录
        """
        # 创建输出目录（不存在则自动创建）
        self.output_dir = Path(output_dir)
        # 创建目录
        self.output_dir.mkdir(exist_ok=True, parents=True)
    
    def generate_markdown_report(self, 
                                 self_data: Dict[str, Any],
                                 benchmark_data: Optional[Dict[str, Any]] = None,
                                 *,
                                 creator_ranking: Optional[Dict[str, Any]] = None) -> str:
        """生成Markdown格式的诊断报告
        
        Args:
            self_data: 账号数据
            benchmark_data: benchmark对比数据（可选）
            creator_ranking: 冻结的同行排名结果（schema=3 / unit=creator，可选）；
                仅 keyword-only，不破坏旧位置参数调用
            
        Returns:
            Markdown文本
        """
        logger.info("[报告生成] 生成Markdown报告")
        
        # 解构账号数据各维度
        uid = self_data.get('uid')
        # 读取字典/配置项
        basic_info = self_data.get('basic_info', {})
        # 读取字典/配置项
        fan_stats = self_data.get('fan_stats', {})
        # 读取字典/配置项
        video_stats = self_data.get('video_stats', {})
        # 读取字典/配置项
        post_rhythm = self_data.get('post_rhythm', {})
        # 读取字典/配置项
        engagement = self_data.get('engagement_metrics', {})
        # 读取字典/配置项
        data_availability = self_data.get('data_availability', {})
        
        # 构建Markdown报告
        md_parts = []
        
        # 标题
        md_parts.append(f"# B站账号诊断报告\n")
        # 追加到列表
        md_parts.append(f"**生成时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        # 追加到列表
        md_parts.append(f"**账号UID**: {uid}\n")
        # 条件分支处理
        if basic_info.get('name'):
            # 追加到列表
            md_parts.append(f"**账号名称**: {basic_info['name']}\n")
        # 追加到列表
        md_parts.append("\n---\n\n")
        
        # 一、数据可得性清单
        md_parts.append("## 📊 数据可得性清单\n\n")
        # 追加到列表
        md_parts.append("### ✅ 已获取数据\n\n")
        
        # 动态收集已获取的数据项
        available_data = []
        # 条件分支处理
        if data_availability.get('basic_info'):
            # 追加到列表
            available_data.append("- **基础信息**: 粉丝数、等级、认证状态")
        # 条件分支处理
        if data_availability.get('video_stats'):
            # 追加到列表
            available_data.append("- **投稿数据**: 视频列表、播放量、评论数、收藏数")
        # 条件分支处理
        if data_availability.get('post_rhythm'):
            # 追加到列表
            available_data.append("- **投稿节奏**: 发布频率、断更记录")
        # 条件分支处理
        if data_availability.get('engagement_metrics'):
            # 追加到列表
            available_data.append("- **互动指标**: 粉丝触达率、评论率、收藏率（估算）")
        # 条件分支处理
        if data_availability.get('fans_growth_curve'):
            # 追加到列表
            available_data.append("- **粉丝增长曲线**: 历史增长趋势（来自三方数据源）")
        
        # 追加到列表
        md_parts.append("\n".join(available_data))
        # 追加到列表
        md_parts.append("\n\n")
        
        # 不可获取数据（创作者中心专属）
        md_parts.append("### ❌ 无法获取数据（仅创作者中心可见）\n\n")
        unavailable_data = [
            "- **完播率**: 视频完整播放比例",
            "- **观众画像**: 年龄、性别、地域分布",
            "- **流量来源**: 推荐、搜索、粉丝等来源占比",
            "- **实时弹幕热度**: 弹幕密度曲线"
        ]
        # 追加到列表
        md_parts.append("\n".join(unavailable_data))
        # 追加到列表
        md_parts.append("\n\n> ⚠️ **提示**: 以上数据需登录B站创作者中心查看\n\n")
        # 追加到列表
        md_parts.append("---\n\n")
        
        # 二、账号概览
        md_parts.append("## 👤 账号概览\n\n")
        # 追加到列表
        follower = fan_stats.get('follower')
        following = fan_stats.get('following')
        charge_count = fan_stats.get('charge_count')
        follower_text = f"{follower:,}" if isinstance(follower, (int, float)) else "暂无数据"
        following_text = f"{following:,}" if isinstance(following, (int, float)) else "暂无数据"
        charge_text = f"{charge_count:,}" if isinstance(charge_count, (int, float)) else "暂无数据"
        md_parts.append(f"- **粉丝数**: {follower_text}\n")
        md_parts.append(f"- **关注数**: {following_text}\n")
        md_parts.append(f"- **充电人数**: {charge_text}\n")
        # 追加到列表
        md_parts.append(f"- **等级**: Lv.{basic_info.get('level', 0)}\n")
        
        # 认证状态（仅显示已认证）
        # 将内容呈现到界面上
        if basic_info.get('official'):
            official = basic_info['official']
            # 边界/有效性检查
            if official.get('type') != -1:
                # 追加到列表
                md_parts.append(f"- **认证状态**: {official.get('title', '已认证')}\n")
        
        # 追加到列表
        md_parts.append("\n---\n\n")
        
        # 三、投稿数据分析
        md_parts.append("## 🎬 投稿数据分析\n\n")
        # 追加到列表
        md_parts.append(f"- **总投稿数**: {_format_metric(video_stats.get('total_count'))} 个视频\n")
        # 追加到列表
        md_parts.append(f"- **总播放量**: {_format_metric(video_stats.get('total_play'))}\n")
        # 追加到列表
        md_parts.append(f"- **总评论数**: {_format_metric(video_stats.get('total_comment'))}\n")
        # 追加到列表
        md_parts.append(f"- **总收藏数**: {_format_metric(video_stats.get('total_favorite'))}\n")
        # 追加到列表
        md_parts.append(
            f"- **平均播放**: {_format_metric(video_stats.get('avg_play'))}"
            f"（{video_stats.get('stats_scope_label', '全部已采集投稿的全历史累计口径')}）\n"
        )
        # 追加到列表
        md_parts.append(f"- **平均评论**: {_format_metric(video_stats.get('avg_comment'))}\n")
        # 采集覆盖与状态：局部数据不得被整体模板掩盖（§5.5）。
        coverage = video_stats.get('coverage')
        if isinstance(coverage, dict) and coverage:
            coverage_bits = []
            for key, item in coverage.items():
                if not isinstance(item, dict):
                    continue
                # 缺键 / 非法值一律渲染为「未知」，绝不用 0 冒充（规格 §5.5）：
                # 真实 0（int 且 >= 0）照常显示 0，与「没拿到」严格区分。
                _valid = item.get('valid_count')
                _missing = item.get('missing_count')
                _valid_ok = isinstance(_valid, int) and not isinstance(_valid, bool) and _valid >= 0
                _missing_ok = isinstance(_missing, int) and not isinstance(_missing, bool) and _missing >= 0
                _valid_text = f"{_valid:,}" if _valid_ok else "未知"
                _missing_text = f"{_missing:,}" if _missing_ok else "未知"
                coverage_bits.append(f"{key} 有效 {_valid_text}/缺失 {_missing_text}")
            if coverage_bits:
                # 追加到列表
                md_parts.append(f"- **指标覆盖度**: {'；'.join(coverage_bits)}\n")
        if video_stats.get('collection_status'):
            # 追加到列表
            md_parts.append(f"- **采集状态**: {video_stats.get('collection_status')}\n")
        
        # 最高播放视频（如有）
        max_play = video_stats.get('max_play_video')
        # 判断 max_play
        # 根据条件走向不同处理分支
        if max_play:
            # 追加到列表
            md_parts.append(f"\n**🏆 最高播放视频**:\n")
            # 追加到列表
            md_parts.append(f"- 标题: {max_play['title']}\n")
            # 追加到列表
            md_parts.append(f"- 播放: {_format_metric(max_play.get('play'))}\n")
            # 追加到列表
            md_parts.append(f"- BV号: {max_play['bvid']}\n")
        
        # 追加到列表
        md_parts.append("\n---\n\n")
        
        # 四、投稿节奏
        md_parts.append("## ⏰ 投稿节奏\n\n")
        # 追加到列表
        md_parts.append(f"- **投稿频率**: {_format_metric(post_rhythm.get('videos_per_week'))} 视频/周\n")
        # 追加到列表
        md_parts.append(f"- **投稿频率**: {_format_metric(post_rhythm.get('videos_per_month'))} 视频/月\n")
        # 追加到列表
        md_parts.append(f"- **活跃天数**: {_format_metric(post_rhythm.get('total_days_active'))} 天\n")
        # 追加到列表
        md_parts.append(f"- **最长断更**: {_format_metric(post_rhythm.get('longest_gap_days'))} 天\n")
        # 追加到列表
        md_parts.append(f"- **近30天投稿**: {_format_metric(post_rhythm.get('recent_30d_count'))} 个视频\n")
        
        # 节奏评价（按周更频率分级）
        freq = post_rhythm.get('videos_per_week')
        # 边界/有效性检查
        if freq is None:
            # 追加到列表
            md_parts.append("\nℹ️ **评价**: 投稿频率数据缺失，暂不评分\n")
        # 边界/有效性检查
        elif freq >= 3:
            # 追加到列表
            md_parts.append("\n✅ **评价**: 投稿频率高，保持稳定产出\n")
        # 边界/有效性检查
        elif freq >= 1:
            # 追加到列表
            md_parts.append("\n⚠️ **评价**: 投稿频率中等，建议提升至每周2-3更\n")
        else:
            # 追加到列表
            md_parts.append("\n❌ **评价**: 投稿频率较低，难以维持粉丝活跃度\n")
        
        # 追加到列表
        md_parts.append("\n---\n\n")
        
        # 五、互动指标
        md_parts.append("## 💬 互动指标\n\n")
        # 追加到列表
        md_parts.append(f"- **粉丝触达率**: {_format_metric(engagement.get('play_to_fans_ratio'), suffix='%')}\n")
        # 追加到列表
        md_parts.append(f"  > 平均播放量占粉丝数的比例，反映粉丝活跃度\n\n")
        # 追加到列表
        md_parts.append(f"- **评论率**: {_format_metric(engagement.get('comment_to_play_ratio'), suffix='%')}\n")
        # 追加到列表
        md_parts.append(f"  > 评论数占播放量的比例，反映内容互动性\n\n")
        # 追加到列表
        md_parts.append(f"- **收藏率**: {_format_metric(engagement.get('favorite_to_play_ratio'), suffix='%')}\n")
        # 追加到列表
        md_parts.append(f"  > 收藏数占播放量的比例，反映内容价值度\n\n")
        
        # 触达率评价
        touch_rate = engagement.get('play_to_fans_ratio')
        # 边界/有效性检查
        if touch_rate is None:
            # 追加到列表
            md_parts.append("ℹ️ **触达评价**: 触达率数据缺失，暂不评分\n")
        # 边界/有效性检查
        elif touch_rate >= 30:
            # 追加到列表
            md_parts.append("✅ **触达评价**: 粉丝触达率高，说明粉丝粘性强\n")
        # 边界/有效性检查
        elif touch_rate >= 10:
            # 追加到列表
            md_parts.append("⚠️ **触达评价**: 粉丝触达率中等，部分粉丝活跃度待提升\n")
        else:
            # 追加到列表
            md_parts.append("❌ **触达评价**: 粉丝触达率低，需加强粉丝互动和内容推送\n")
        
        # 追加到列表
        md_parts.append("\n---\n\n")
        
        # 六、benchmark对比（如果有）
        if benchmark_data and benchmark_data.get("status") == "disabled":
            # 明确说明旧口径已停用，避免报告章节静默消失。
            md_parts.append("## 📈 分区对比分析\n\n")
            md_parts.append(
                "⚠️ **当前未输出分区排名**：旧参照数据的热度口径与播放量不可比，"
                "本版本已停止使用该结果。\n\n"
            )
            md_parts.append("---\n\n")
        elif benchmark_data and benchmark_data.get('has_benchmark'):
            # 追加到列表
            md_parts.append("## 📈 分区对比分析\n\n")
            # 追加到列表
            md_parts.append(f"**对比分区**: {benchmark_data['category']}\n")
            # 追加到列表
            md_parts.append(f"**样本量**: {benchmark_data['sample_size']} 个视频\n\n")
            
            # 分区数据分布
            category_metrics = benchmark_data['category_metrics']
            # 追加到列表
            md_parts.append("### 分区数据分布\n\n")
            # 追加到列表
            md_parts.append(f"- **平均播放**: {category_metrics['avg_play']:,}\n")
            # 追加到列表
            md_parts.append(f"- **25分位**: {category_metrics['p25']:,}\n")
            # 追加到列表
            md_parts.append(f"- **50分位（中位数）**: {category_metrics['p50']:,}\n")
            # 追加到列表
            md_parts.append(f"- **75分位**: {category_metrics['p75']:,}\n\n")
            
            # 自身位置
            comparison = benchmark_data['comparison']
            # 追加到列表
            md_parts.append("### 你的位置\n\n")
            # 追加到列表
            md_parts.append(f"- **排名**: {comparison['position']}\n")
            # 追加到列表
            md_parts.append(f"- **相对分区平均**: {comparison['vs_avg']}%\n\n")
            
            # 排名评价
            if comparison['position'] == "顶部25%":
                # 追加到列表
                md_parts.append("🎉 **恭喜**: 你的数据处于分区头部水平！\n")
            # 边界/有效性检查
            elif comparison['position'] == "中上50-75%":
                # 追加到列表
                md_parts.append("👍 **不错**: 你的数据高于分区平均水平\n")
            else:
                # 追加到列表
                md_parts.append("💪 **加油**: 还有较大提升空间，继续优化内容质量\n")
            
            # 追加到列表
            md_parts.append("\n---\n\n")
        
        # 六之二、同行排名（creator_ranking，schema=3 / unit=creator）
        # 允许显示真实新名次，不永久禁用「排名」二字；只消费冻结结果，不重算。
        if creator_ranking:
            self._append_creator_ranking_section(md_parts, creator_ranking)

        # 七、改进建议
        md_parts.append("## 💡 改进建议\n\n")
        
        # 基于数据自动生成建议
        suggestions = []
        
        # 投稿频率低
        _vpw = post_rhythm.get('videos_per_week')
        if _vpw is not None and _vpw < 1:
            # 追加到列表
            suggestions.append("1. **提升投稿频率**: 建议保持每周至少1-2更，稳定的产出节奏有助于维持粉丝活跃")
        
        # 评论率低
        _comment_rate = engagement.get('comment_to_play_ratio')
        if _comment_rate is not None and _comment_rate < 0.5:
            # 追加到列表
            suggestions.append("2. **增强互动引导**: 视频结尾引导评论、置顶话题讨论、及时回复评论")
        
        # 触达率低
        _touch_rate = engagement.get('play_to_fans_ratio')
        if _touch_rate is not None and _touch_rate < 20:
            # 追加到列表
            suggestions.append("3. **提升粉丝触达**: 优化发布时间（晚8-10点）、增强标题吸引力、提升内容质量")
        
        # 断更过长
        _gap_days = post_rhythm.get('longest_gap_days')
        if _gap_days is not None and _gap_days > 30:
            # 追加到列表
            suggestions.append("4. **避免长期断更**: 超过30天未更新会导致粉丝流失，建议提前储备内容")
        
        # 兜底结论：必须区分「指标正常」与「指标不可得」，两者不得走同一条好结论。
        # 「有效指标足够」判定依据：上面四条阈值判断分属两个互相独立的评价维度——
        # 「投稿节奏」(_vpw / _gap_days) 与「互动表现」(_comment_rate / _touch_rate)。
        # 只有当两个维度各自至少有 1 个指标通过 ``is not None`` 守卫、真正参与过
        # 阈值判断时，才认定证据足以支撑**整体**结论「整体表现良好」；任一维度全为
        # None（含全未知）时整体评价无证据，只能提示「指标不足」，禁止下良好结论。
        if not suggestions:
            _rhythm_has_valid = _vpw is not None or _gap_days is not None
            _engagement_has_valid = _comment_rate is not None or _touch_rate is not None
            if _rhythm_has_valid and _engagement_has_valid:
                # 两维度均有明确数值参与过阈值判断，且均未越界 -> 证据充分。
                suggestions.append("✅ 整体表现良好，继续保持！")
            else:
                # 有效指标不足（含全未知）-> 不得下「良好」结论。
                suggestions.append("ℹ️ 指标不足，暂不能评价整体表现，请先完成数据采集。")
        
        # 追加到列表
        md_parts.append("\n".join(suggestions))
        # 追加到列表
        md_parts.append("\n\n---\n\n")
        
        # 报告结尾
        md_parts.append("*本报告由B站运营工具箱自动生成*\n")
        
        return "".join(md_parts)

    def _append_creator_ranking_section(self, md_parts: list, result: Any) -> None:
        """把冻结的同行排名（schema=3 / unit=creator）追加到 Markdown 章节。

        处理要点（规格 §10.5）：
        - ``rank=None`` 显示「无法计算」，绝不显示 0；
        - ``partial`` 标题必须写「已成功取得的 X 个参评账号」，不声称请求的所有账号均已比较；
        - 明确写出「不代表全站排名」，不编造全区 / 全站口径；
        - 只渲染传入的冻结结果，不调用任何不存在的 ready 渲染方法。

        Args:
            md_parts: Markdown 片段累积列表（原地追加）。
            result: 冻结排名结果（dict）；非 schema=3 / unit=creator 时直接跳过。

        Returns:
            无。
        """
        if not isinstance(result, dict):
            return
        if result.get('schema_version') != 3 or result.get('unit') != 'creator':
            # 旧契约 / 非 creator 单位：不渲染，避免错误排名复活
            return

        policy = result.get('policy') if isinstance(result.get('policy'), dict) else {}
        peer_source = policy.get('peer_source')
        source_label = (
            '热门作品作者参评集合' if peer_source == 'ranking_discovered_peer_set' else '手动指定同行名单'
        )
        comparison_state = result.get('comparison_state')
        valid_peer_count = result.get('valid_peer_count')
        requested_peer_count = result.get('requested_peer_count')
        excluded = result.get('excluded_peers') or []
        valid_text = _format_ranking_metric(valid_peer_count)

        # 标题：partial 必须写「已成功取得的 X 个参评账号」
        if comparison_state == 'partial':
            md_parts.append(f"## 🏆 同行排名：已成功取得的 {valid_text} 个参评账号\n\n")
        else:
            md_parts.append("## 🏆 同行排名（有限参评集合）\n\n")

        state_label = {
            'complete': '完整比较（请求的同行均参与）',
            'partial': f'部分比较（请求 {_format_ranking_metric(requested_peer_count)} 个，仅 {valid_text} 个成功参评）',
            'insufficient_peers': '目标有效，但有效同行不足，无法比较',
            'target_unavailable': '目标账号本轮无有效指标，仅展示同行事实',
        }.get(comparison_state, '状态未知')
        md_parts.append(f"- **比较状态**: {state_label}\n")
        md_parts.append(f"- **peer 来源**: {source_label}\n")
        md_parts.append(
            f"- **请求同行 / 有效参评**: {_format_ranking_metric(requested_peer_count)}"
            f" / {valid_text}\n"
        )
        md_parts.append(
            "- **指标口径**: 过去 {window} 天内、稿龄 {min_age}—{window} 天的最近最多 {maxv} 条公开稿件，"
            "取中位累计播放（一个账号一票，多 P 不加权）\n".format(
                window=policy.get('window_days', 30),
                min_age=policy.get('minimum_age_days', 7),
                maxv=policy.get('max_videos', 10),
            )
        )
        if excluded:
            md_parts.append(f"- **排除账号**: {len(excluded)} 个（失败或指标无效，不排末尾、不当 0 分）\n")
        md_parts.append(
            "\n> ℹ️ 这是「在这些明确参评账号中按同一指标的名次」，**不代表全站排名**；"
            "参评账号由用户选定或从热门作品作者中发现，采集窗口相同，"
            "未消除稿龄、内容类型和选样偏差。\n\n"
        )

        # 目标卡片
        target = result.get('target') if isinstance(result.get('target'), dict) else {}
        md_parts.append("### 你的位置\n\n")
        if target.get('rank') is None:
            reason_key = target.get('status')
            reason = RANK_UNAVAILABLE_REASONS.get(reason_key, '本轮无有效指标')
            md_parts.append(f"- **名次**: 无法计算（{reason}）\n")
        else:
            rank_text = f"第 {target['rank']}"
            if target.get('rank_end') and target['rank_end'] != target['rank']:
                rank_text += f"–{target['rank_end']}"
            rank_text += f" 名 / 共 {_format_ranking_metric(target.get('total'))} 个参评账号"
            md_parts.append(f"- **名次**: {rank_text}\n")
            md_parts.append(f"- **中位累计播放**: {_format_ranking_metric(target.get('metric_value'))}\n")
            if target.get('percentile') is None:
                md_parts.append("- **参照样本百分位**: 有效同行不足 5 个，不展示百分位（名次仍真实）\n")
            else:
                md_parts.append(
                    f"- **参照样本百分位**: {_format_ranking_percent(target.get('percentile'))}"
                    "（参照样本百分位，越高越靠前）\n"
                )
            md_parts.append(f"- **选中稿件数**: {_format_ranking_metric(target.get('selected_count'))}\n")

        # 参照分布
        distribution = result.get('reference_distribution')
        if isinstance(distribution, dict):
            md_parts.append(
                "\n- **参照分布（peer 成绩，不含目标）**: "
                f"P25 {_format_ranking_metric(distribution.get('p25'))} / "
                f"P50 {_format_ranking_metric(distribution.get('p50'))} / "
                f"P75 {_format_ranking_metric(distribution.get('p75'))}；"
                f"样本数 {_format_ranking_metric(distribution.get('count'))}\n"
            )

        # 参评名单
        leaderboard = result.get('leaderboard') or []
        if leaderboard:
            md_parts.append("\n### 参评名单\n\n")
            md_parts.append("| UID | 中位累计播放 | 名次 | 并列范围 | 选中稿件 |\n")
            md_parts.append("|---|---|---|---|---|\n")
            for row in leaderboard:
                if not isinstance(row, dict):
                    continue
                rank_cell = '无法计算' if row.get('rank') is None else str(row.get('rank'))
                end_cell = '—' if row.get('rank_end') is None else str(row.get('rank_end'))
                marker = '（你）' if row.get('is_target') else ''
                md_parts.append(
                    f"| {row.get('uid')}{marker} | {_format_ranking_metric(row.get('metric_value'))} "
                    f"| {rank_cell} | {end_cell} | {_format_ranking_metric(row.get('selected_count'))} |\n"
                )

        # 排除清单
        if excluded:
            md_parts.append("\n### 被排除的请求账号\n\n")
            for item in excluded:
                if not isinstance(item, dict):
                    continue
                md_parts.append(f"- UID {item.get('uid')}: {item.get('reason')}\n")

        # 警告
        for warning in result.get('warnings') or []:
            if isinstance(warning, dict) and warning.get('message'):
                md_parts.append(f"\n> ⚠️ {warning['message']}\n")

        md_parts.append("\n---\n\n")
    
    def save_markdown_report(self, 
                            self_data: Dict[str, Any],
                            benchmark_data: Optional[Dict[str, Any]] = None,
                            filename: Optional[str] = None,
                            *,
                            creator_ranking: Optional[Dict[str, Any]] = None) -> str:
        """保存Markdown报告到文件
        # 持久化数据，防止丢失
        
        Args:
            self_data: 账号数据
            benchmark_data: benchmark数据
            filename: 文件名（可选，默认使用时间戳）
            creator_ranking: 冻结的同行排名结果（仅 keyword-only）
            
        Returns:
            保存的文件路径
            # 持久化数据，防止丢失
        """
        # 生成报告内容
        md_content = self.generate_markdown_report(self_data, benchmark_data, creator_ranking=creator_ranking)
        
        # 确定文件名
        # 默认格式: diagnosis_report_{uid}_{时间戳}.md
        if not filename:
            # 读取字典/配置项
            uid = self_data.get('uid', 'unknown')
            # 格式化日期
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            filename = f"diagnosis_report_{uid}_{timestamp}.md"
        
        # 保存文件
        filepath = self.output_dir / filename
        
        # 异常保护：局部失败不影响主流程
        try:
            # 上下文管理：确保资源自动释放
            with open(filepath, 'w', encoding='utf-8') as f:
                # 写入数据
                f.write(md_content)
            
            logger.info(f"[报告生成] Markdown报告已保存: {filepath}")
            return str(filepath)
            
        except Exception as e:
            logger.error(f"[报告生成] 保存Markdown报告失败: {e}")
            # 抛出异常中断流程
            raise
    
    def _save_markdown_fallback(self,
                                self_data: Dict[str, Any],
                                benchmark_data: Optional[Dict[str, Any]],
                                filename: Optional[str],
                                *,
                                creator_ranking: Optional[Dict[str, Any]] = None) -> str:
        """PDF 依赖缺失时的降级：输出 .md 文件，绝不把 Markdown 写成 .pdf（§9-17）。

        Args:
            self_data: 账号数据。
            benchmark_data: benchmark 数据。
            filename: 原请求文件名（可能以 .pdf 结尾）。
            creator_ranking: 冻结的同行排名结果（仅 keyword-only）。

        Returns:
            实际保存的 Markdown 文件路径。
        """
        md_filename = filename
        if md_filename:
            md_filename = str(Path(md_filename).with_suffix(".md"))
        return self.save_markdown_report(self_data, benchmark_data, md_filename, creator_ranking=creator_ranking)

    def generate_pdf_report(self,
                           self_data: Dict[str, Any],
                           benchmark_data: Optional[Dict[str, Any]] = None,
                           filename: Optional[str] = None,
                           *,
                           creator_ranking: Optional[Dict[str, Any]] = None) -> str:
        """生成PDF格式的诊断报告
        
        依赖 markdown2 + pdfkit + wkhtmltopdf，
        缺失时自动降级为 Markdown。
        
        Args:
            self_data: 账号数据
            benchmark_data: benchmark数据
            filename: 文件名（可选）
            creator_ranking: 冻结的同行排名结果（仅 keyword-only）
            
        Returns:
            保存的PDF文件路径
            # 持久化数据，防止丢失
        """
        logger.info("[报告生成] 生成PDF报告")
        
        # 异常保护：局部失败不影响主流程
        try:
            # 先生成Markdown
            md_content = self.generate_markdown_report(self_data, benchmark_data, creator_ranking=creator_ranking)
            
            # 使用markdown2转HTML，再用pdfkit转PDF
            # 需要安装: pip install markdown2 pdfkit
            # 以及wkhtmltopdf: https://wkhtmltopdf.org/downloads.html
            try:
                # 导入模块
                import markdown2
                # 导入模块
                import pdfkit
            except ImportError:
                logger.warning("[报告生成] 缺少PDF依赖（markdown2/pdfkit），降级为Markdown")
                return self._save_markdown_fallback(self_data, benchmark_data, filename,
                                                    creator_ranking=creator_ranking)
            
            # Markdown转HTML
            html_content = markdown2.markdown(md_content, extras=['tables', 'fenced-code-blocks'])
            
            # 添加CSS样式（B站主题蓝粉配色）
            # 将元素加入容器/布局
            styled_html = f"""
            <!DOCTYPE html>
            <html>
            <head>
                <meta charset="UTF-8">
                <style>
                    body {{
                        font-family: "SimHei", "Microsoft YaHei", sans-serif;
                        line-height: 1.6;
                        max-width: 900px;
                        margin: 40px auto;
                        padding: 20px;
                        color: #333;
                    }}
                    h1 {{
                        color: #00a1d6;
                        border-bottom: 2px solid #00a1d6;
                        padding-bottom: 10px;
                    }}
                    h2 {{
                        color: #fb7299;
                        margin-top: 30px;
                        border-left: 4px solid #fb7299;
                        padding-left: 10px;
                    }}
                    h3 {{
                        color: #666;
                    }}
                    code {{
                        background: #f5f5f5;
                        padding: 2px 6px;
                        border-radius: 3px;
                    }}
                    blockquote {{
                        border-left: 4px solid #ddd;
                        padding-left: 15px;
                        color: #666;
                    }}
                    hr {{
                        border: none;
                        border-top: 1px solid #ddd;
                        margin: 30px 0;
                    }}
                </style>
            </head>
            <body>
                {html_content}
            </body>
            </html>
            """
            
            # 确定文件名
            if not filename:
                # 读取字典/配置项
                uid = self_data.get('uid', 'unknown')
                # 格式化日期
                timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
                filename = f"diagnosis_report_{uid}_{timestamp}.pdf"
            
            # 计算结果存入 filepath
            # 对输入做运算得到结果
            filepath = self.output_dir / filename
            
            # HTML转PDF
            pdfkit.from_string(styled_html, str(filepath), options={
                'encoding': 'UTF-8',
                'enable-local-file-access': None
            })
            
            logger.info(f"[报告生成] PDF报告已保存: {filepath}")
            return str(filepath)
            
        except Exception as e:
            logger.error(f"[报告生成] PDF生成失败: {e}，降级为Markdown")
            # 降级为 Markdown，返回 .md 路径而非伪装成 PDF。
            return self._save_markdown_fallback(self_data, benchmark_data, filename,
                                                creator_ranking=creator_ranking)