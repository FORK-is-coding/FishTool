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
                                 benchmark_data: Optional[Dict[str, Any]] = None) -> str:
        """生成Markdown格式的诊断报告
        
        Args:
            self_data: 账号数据
            benchmark_data: benchmark对比数据（可选）
            
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
        md_parts.append(f"- **总投稿数**: {video_stats.get('total_count', 0)} 个视频\n")
        # 追加到列表
        md_parts.append(f"- **总播放量**: {video_stats.get('total_play', 0):,}\n")
        # 追加到列表
        md_parts.append(f"- **总评论数**: {video_stats.get('total_comment', 0):,}\n")
        # 追加到列表
        md_parts.append(f"- **总收藏数**: {video_stats.get('total_favorite', 0):,}\n")
        # 追加到列表
        md_parts.append(f"- **平均播放**: {video_stats.get('avg_play', 0):,}\n")
        # 追加到列表
        md_parts.append(f"- **平均评论**: {video_stats.get('avg_comment', 0):,}\n")
        
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
            md_parts.append(f"- 播放: {max_play['play']:,}\n")
            # 追加到列表
            md_parts.append(f"- BV号: {max_play['bvid']}\n")
        
        # 追加到列表
        md_parts.append("\n---\n\n")
        
        # 四、投稿节奏
        md_parts.append("## ⏰ 投稿节奏\n\n")
        # 追加到列表
        md_parts.append(f"- **投稿频率**: {post_rhythm.get('videos_per_week', 0)} 视频/周\n")
        # 追加到列表
        md_parts.append(f"- **投稿频率**: {post_rhythm.get('videos_per_month', 0)} 视频/月\n")
        # 追加到列表
        md_parts.append(f"- **活跃天数**: {post_rhythm.get('total_days_active', 0)} 天\n")
        # 追加到列表
        md_parts.append(f"- **最长断更**: {post_rhythm.get('longest_gap_days', 0)} 天\n")
        # 追加到列表
        md_parts.append(f"- **近30天投稿**: {post_rhythm.get('recent_30d_count', 0)} 个视频\n")
        
        # 节奏评价（按周更频率分级）
        freq = post_rhythm.get('videos_per_week', 0)
        # 边界/有效性检查
        if freq >= 3:
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
        md_parts.append(f"- **粉丝触达率**: {engagement.get('play_to_fans_ratio', 0)}%\n")
        # 追加到列表
        md_parts.append(f"  > 平均播放量占粉丝数的比例，反映粉丝活跃度\n\n")
        # 追加到列表
        md_parts.append(f"- **评论率**: {engagement.get('comment_to_play_ratio', 0)}%\n")
        # 追加到列表
        md_parts.append(f"  > 评论数占播放量的比例，反映内容互动性\n\n")
        # 追加到列表
        md_parts.append(f"- **收藏率**: {engagement.get('favorite_to_play_ratio', 0)}%\n")
        # 追加到列表
        md_parts.append(f"  > 收藏数占播放量的比例，反映内容价值度\n\n")
        
        # 触达率评价
        touch_rate = engagement.get('play_to_fans_ratio', 0)
        # 边界/有效性检查
        if touch_rate >= 30:
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
        if benchmark_data and benchmark_data.get('has_benchmark'):
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
        
        # 七、改进建议
        md_parts.append("## 💡 改进建议\n\n")
        
        # 基于数据自动生成建议
        suggestions = []
        
        # 投稿频率低
        if post_rhythm.get('videos_per_week', 0) < 1:
            # 追加到列表
            suggestions.append("1. **提升投稿频率**: 建议保持每周至少1-2更，稳定的产出节奏有助于维持粉丝活跃")
        
        # 评论率低
        if engagement.get('comment_to_play_ratio', 0) < 0.5:
            # 追加到列表
            suggestions.append("2. **增强互动引导**: 视频结尾引导评论、置顶话题讨论、及时回复评论")
        
        # 触达率低
        if engagement.get('play_to_fans_ratio', 0) < 20:
            # 追加到列表
            suggestions.append("3. **提升粉丝触达**: 优化发布时间（晚8-10点）、增强标题吸引力、提升内容质量")
        
        # 断更过长
        if post_rhythm.get('longest_gap_days', 0) > 30:
            # 追加到列表
            suggestions.append("4. **避免长期断更**: 超过30天未更新会导致粉丝流失，建议提前储备内容")
        
        # 空值/异常保护：不满足条件时跳过
        if not suggestions:
            # 追加到列表
            suggestions.append("✅ 整体表现良好，继续保持！")
        
        # 追加到列表
        md_parts.append("\n".join(suggestions))
        # 追加到列表
        md_parts.append("\n\n---\n\n")
        
        # 报告结尾
        md_parts.append("*本报告由B站运营工具箱自动生成*\n")
        
        return "".join(md_parts)
    
    def save_markdown_report(self, 
                            self_data: Dict[str, Any],
                            benchmark_data: Optional[Dict[str, Any]] = None,
                            filename: Optional[str] = None) -> str:
        """保存Markdown报告到文件
        # 持久化数据，防止丢失
        
        Args:
            self_data: 账号数据
            benchmark_data: benchmark数据
            filename: 文件名（可选，默认使用时间戳）
            
        Returns:
            保存的文件路径
            # 持久化数据，防止丢失
        """
        # 生成报告内容
        md_content = self.generate_markdown_report(self_data, benchmark_data)
        
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
    
    def generate_pdf_report(self,
                           self_data: Dict[str, Any],
                           benchmark_data: Optional[Dict[str, Any]] = None,
                           filename: Optional[str] = None) -> str:
        """生成PDF格式的诊断报告
        
        依赖 markdown2 + pdfkit + wkhtmltopdf，
        缺失时自动降级为 Markdown。
        
        Args:
            self_data: 账号数据
            benchmark_data: benchmark数据
            filename: 文件名（可选）
            
        Returns:
            保存的PDF文件路径
            # 持久化数据，防止丢失
        """
        logger.info("[报告生成] 生成PDF报告")
        
        # 异常保护：局部失败不影响主流程
        try:
            # 先生成Markdown
            md_content = self.generate_markdown_report(self_data, benchmark_data)
            
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
                return self.save_markdown_report(self_data, benchmark_data, filename)
            
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
            # 降级为Markdown
            return self.save_markdown_report(self_data, benchmark_data, filename)