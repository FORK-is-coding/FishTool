"""
评论去重分层模块
实现复杂的去重策略：同用户复读折叠、跨用户同内容聚合、模糊匹配、时间窗口热点检测

本模块实现四层评论去重与信号增强管线：

第一层：同用户复读去重（_deduplicate_by_user）
- 按 uid+content 组合键分组
- 同一用户发送的相同内容只保留一条
- 标记 duplicate_count 与 duplicate_rpids

第二层：跨用户同内容聚合（_aggregate_cross_user）
- 按 content 分组（跨用户）
- 多用户相同内容 → 标记 voice_weight 声量权重
- 保留代表评论，附 voice_users 用户列表

第三层：模糊匹配去重（_deduplicate_by_fuzzy）
- 短评论（<5字）跳过，避免误伤
- SimHash 快速粗筛（汉明距离<=3）
- 编辑距离精确计算相似度（>=0.85）
- 相似组折叠为一条代表评论

第四层：时间窗口热点检测（_detect_time_hotspots）
- 按小时窗口分组评论
- 评论数 >= 平均值*2 判定为突增热点
- 分析热点情感倾向（positive_burst/negative_burst/neutral）

核心算法：
- _normalize_content: 文本标准化（去空格/小写）
- _calculate_similarity: 相似度计算（SimHash+编辑距离）
- _simhash: 64位 SimHash 指纹
- _levenshtein_distance: 动态规划编辑距离

输出：
deduplicate() 返回完整结果：
- original_count / deduplicated_count
- deduplicated_comments: 去重后评论
- user_duplicates / cross_user_groups / fuzzy_groups / time_hotspots
"""
import asyncio
from typing import List, Dict, Any, Set, Tuple, Optional
from datetime import datetime, timedelta
from collections import defaultdict, Counter
import hashlib
import logging

from core.logger import get_logger

logger = get_logger(__name__)


class CommentDeduplicator:
    """评论去重器 - 实现多层去重策略
    
    按顺序执行四层去重管线，
    每层输出作为下一层输入。
    """
    
    def __init__(self, 
                 short_text_threshold: int = 5,
                 fuzzy_match_threshold: float = 0.85,
                 time_window_hours: int = 24):
        """初始化去重器
        
        Args:
            short_text_threshold: 短评论阈值（字数少于此值不做模糊匹配）
            fuzzy_match_threshold: 模糊匹配相似度阈值（0-1）
            time_window_hours: 时间窗口小时数
        """
        # 短文本阈值：少于此字数的评论不做模糊匹配（避免误伤）
        self.short_text_threshold = short_text_threshold
        # 模糊匹配相似度阈值：0-1之间，越高越严格
        self.fuzzy_match_threshold = fuzzy_match_threshold
        # 时间窗口：用于热点检测的时间跨度
        self.time_window = timedelta(hours=time_window_hours)
        
    def deduplicate(self, comments: List[Dict[str, Any]]) -> Dict[str, Any]:
        """执行完整的去重分层处理
        # 对数据进行加工/分发
        
        四层去重策略：
        1. 同用户复读折叠
        2. 跨用户同内容聚合（标记声量权重）
        3. 模糊匹配去重（SimHash + 编辑距离）
        4. 时间窗口热点检测
        
        Args:
            comments: 原始评论列表
            
        Returns:
            去重结果字典，包含各层去重详情和最终评论列表
        """
        logger.info(f"开始去重处理，原始评论数: {len(comments)}")
        
        # 第一层：同用户复读去重
        # 相同用户+相同内容折叠为一条
        after_user_dedup, user_duplicates = self._deduplicate_by_user(comments)
        logger.info(f"同用户去重后: {len(after_user_dedup)} 条（折叠 {len(user_duplicates)} 组）")
        
        # 第二层：跨用户同内容聚合（不去重，标记权重）
        # 多用户相同内容标记高声量
        after_cross_user, cross_user_groups = self._aggregate_cross_user(after_user_dedup)
        logger.info(f"跨用户聚合后: {len(after_cross_user)} 条（聚合 {len(cross_user_groups)} 组）")
        
        # 第三层：模糊匹配去重（排除短评论）
        # 相似内容折叠为一条
        after_fuzzy, fuzzy_groups = self._deduplicate_by_fuzzy(after_cross_user)
        logger.info(f"模糊匹配后: {len(after_fuzzy)} 条（折叠 {len(fuzzy_groups)} 组）")
        
        # 第四层：时间窗口热点检测
        # 检测评论数突增时段
        hotspots = self._detect_time_hotspots(after_fuzzy)
        logger.info(f"检测到 {len(hotspots)} 个时间窗口热点")
        
        # 组装完整结果
        result = {
            'original_count': len(comments),
            'deduplicated_count': len(after_fuzzy),
            'deduplicated_comments': after_fuzzy,
            'user_duplicates': user_duplicates,
            'cross_user_groups': cross_user_groups,
            'fuzzy_groups': fuzzy_groups,
            'time_hotspots': hotspots,
            'processed_at': datetime.now().isoformat()
        }
        
        logger.info(f"去重完成: {len(comments)} -> {len(after_fuzzy)} 条")
        return result
    
    def _deduplicate_by_user(self, comments: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """第一层：同用户复读去重，折叠重复内容
        
        按 uid + content 分组，同一用户发送的相同内容只保留一条。
        标记重复次数和 rpid 列表，便于后续分析。
        
        Args:
            comments: 评论列表
            
        Returns:
            (去重后评论列表, 折叠的重复组信息)
        """
        # 第一步：按用户ID和内容构建分组索引
        user_content_groups = defaultdict(list)  # {"uid:content": [评论列表]}
        
        # 遍历所有评论，建立uid+content组合键
        for comment in comments:
            # 提取用户唯一标识
            # 从数据中取出目标字段，供后续逻辑使用
            uid = comment.get('uid')
            # 标准化评论内容（去空格、转小写）
            content = self._normalize_content(comment.get('content', ''))
            # 生成组合键：用户ID + 冒号 + 标准化内容
            key = f"{uid}:{content}"
            # 将评论添加到对应分组
            user_content_groups[key].append(comment)
        
        # 处理重复
        deduplicated = []
        duplicates = []
        
        for key, group in user_content_groups.items():
            if len(group) == 1:
                # 无重复
                deduplicated.append(group[0])
            else:
                # 有重复，保留第一条，标记重复次数
                first_comment = group[0].copy()
                first_comment['duplicate_count'] = len(group)
                first_comment['duplicate_type'] = 'user_repeat'
                first_comment['duplicate_rpids'] = [c['rpid'] for c in group[1:]]
                deduplicated.append(first_comment)
                
                # 记录重复组信息供审计
                duplicates.append({
                    'type': 'user_repeat',
                    'uid': group[0]['uid'],
                    'uname': group[0]['uname'],
                    'content': group[0]['content'],
                    'count': len(group),
                    'rpids': [c['rpid'] for c in group]
                })
        
        return deduplicated, duplicates
    
    def _aggregate_cross_user(self, comments: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """第二层：跨用户同内容聚合（不去重，标记为高声量信号）
        
        按内容分组，多用户发送相同内容 → 标记为高声量信号（voice_weight）。
        保留所有评论，但附加声量权重，便于后续排序和分析。
        
        Args:
            comments: 评论列表
            
        Returns:
            (聚合后评论列表, 聚合组信息)
        """
        # 按内容分组（不考虑用户）
        content_groups = defaultdict(list)
        
        # 遍历 comments 逐项处理
        for comment in comments:
            content = self._normalize_content(comment.get('content', ''))
            content_groups[content].append(comment)
        
        # 标记高声量
        aggregated = []
        groups = []
        
        for content, group in content_groups.items():
            if len(group) == 1:
                # 单条评论
                aggregated.append(group[0])
            else:
                # 多用户相同内容 - 高声量信号
                # 保留一条代表，但标记权重
                representative = group[0].copy()
                representative['voice_weight'] = len(group)  # 声量权重
                representative['voice_type'] = 'cross_user_same'
                representative['voice_users'] = [
                    {'uid': c['uid'], 'uname': c['uname'], 'rpid': c['rpid']}
                    # 循环处理
                    for c in group
                ]
                aggregated.append(representative)
                
                groups.append({
                    'type': 'cross_user_same',
                    'content': content,
                    'count': len(group),
                    'users': [{'uid': c['uid'], 'uname': c['uname']} for c in group],
                    'rpids': [c['rpid'] for c in group]
                })
        
        return aggregated, groups
    
    def _deduplicate_by_fuzzy(self, comments: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """第三层：模糊匹配去重（SimHash + 编辑距离）
        
        对长评论（>= 5 字）进行模糊匹配去重：
        1. SimHash 快速粗筛（汉明距离 <= 3）
        2. 编辑距离精确计算相似度（>= 0.85）
        
        短评论直接跳过，避免误伤。
        
        Args:
            comments: 评论列表
            
        Returns:
            (去重后评论列表, 模糊匹配组信息)
        """
        # 分离长短评论
        short_comments = []
        long_comments = []
        
        # 遍历 comments 逐项处理
        for comment in comments:
            content = comment.get('content', '')
            if len(content) < self.short_text_threshold:
                short_comments.append(comment)  # 短评论不做模糊匹配
            else:
                long_comments.append(comment)
        
        # 对长评论进行模糊匹配
        deduplicated_long = []
        fuzzy_groups = []
        # 类型转换后存入 seen_indices
        seen_indices = set()
        
        # 双重循环：对每条评论找其后相似评论。相似度方法内部负责
        # SimHash 粗筛与编辑距离精算，确保它是唯一可替换的判定入口。
        for i, comment1 in enumerate(long_comments):
            if i in seen_indices:
                # 跳过本轮继续循环
                continue
            
            # 查找与当前评论相似的评论
            similar_group = [comment1]
            similar_indices = [i]
            
            for j, comment2 in enumerate(long_comments[i+1:], start=i+1):
                if j in seen_indices:
                    # 跳过本轮继续循环
                    continue
                
                # 相似度方法封装完整判定流程，便于测试和业务策略替换。
                similarity = self._calculate_similarity(
                    comment1.get('content', ''),
                    comment2.get('content', '')
                )
                
                # 超过阈值判定为相似
                if similarity >= self.fuzzy_match_threshold:
                    similar_group.append(comment2)
                    similar_indices.append(j)
                    # 加入集合/数据库会话
                    seen_indices.add(j)
            
            # 保留代表评论
            if len(similar_group) == 1:
                deduplicated_long.append(comment1)
            else:
                # 有相似评论，折叠
                representative = comment1.copy()
                representative['fuzzy_duplicate_count'] = len(similar_group)
                representative['fuzzy_duplicate_type'] = 'similar_content'
                representative['fuzzy_variants'] = [
                    {'rpid': c['rpid'], 'content': c['content'][:50]}
                    # 循环处理
                    for c in similar_group[1:]
                ]
                deduplicated_long.append(representative)
                
                fuzzy_groups.append({
                    'type': 'fuzzy_match',
                    'representative': comment1['content'],
                    'count': len(similar_group),
                    'variants': [c['content'] for c in similar_group],
                    'rpids': [c['rpid'] for c in similar_group]
                })
            
            # 加入集合/数据库会话
            seen_indices.add(i)
        
        # 合并短评论和去重后的长评论
        result = short_comments + deduplicated_long
        
        return result, fuzzy_groups
    
    def _detect_time_hotspots(self, comments: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """第四层：时间窗口热点检测
        
        按小时窗口分组评论，检测突增时段（评论数 >= 平均值 * 2）。
        分析突增时段的情感倾向（正面/负面爆发）。
        
        Args:
            comments: 评论列表
            
        Returns:
            热点列表，包含时间窗口、突增比例、情感分布等
        """
        # 按时间窗口分组
        time_windows = defaultdict(list)
        
        # 遍历 comments 逐项处理
        for comment in comments:
            ctime = comment.get('ctime')
            if not ctime:
                # 跳过本轮继续循环
                continue
            
            # 将时间戳对齐到小时
            # 兼容两种存储类型：datetime 对象或 'YYYY-MM-DD HH:MM:SS.ffffff' 字符串
            # 修复（2026-08-22）：数据库存的是字符串，原代码直接 .replace(minute=...) 会 TypeError
            if isinstance(ctime, str):
                try:
                    ctime = datetime.fromisoformat(ctime)
                except ValueError:
                    # 格式不标准时尝试常见格式解析
                    try:
                        ctime = datetime.strptime(ctime, '%Y-%m-%d %H:%M:%S')
                    except ValueError:
                        # 解析失败跳过该评论，不影响整体去重
                        continue
            window_key = ctime.replace(minute=0, second=0, microsecond=0)
            time_windows[window_key].append(comment)
        
        # 检测热点（评论数突增）
        hotspots = []
        window_keys = sorted(time_windows.keys())
        
        # 计算平均评论数
        if not window_keys:
            return []
        
        avg_count = sum(len(time_windows[k]) for k in window_keys) / len(window_keys)
        
        # 遍历每个时间窗口判断是否突增
        for window_key in window_keys:
            window_comments = time_windows[window_key]
            
            # 突增阈值：超过平均值2倍
            if len(window_comments) >= avg_count * 2:
                # 分析情感倾向
                positive_count = sum(1 for c in window_comments if c.get('sentiment') == 'positive')
                negative_count = sum(1 for c in window_comments if c.get('sentiment') == 'negative')
                
                # 判定热点类型
                hotspot_type = 'neutral'
                if positive_count > len(window_comments) * 0.6:
                    hotspot_type = 'positive_burst'
                elif negative_count > len(window_comments) * 0.6:
                    hotspot_type = 'negative_burst'
                
                # 组装热点信息
                hotspots.append({
                    'time_window': window_key.isoformat(),
                    'type': hotspot_type,
                    'comment_count': len(window_comments),
                    'avg_baseline': avg_count,
                    'burst_ratio': len(window_comments) / avg_count,
                    'positive_ratio': positive_count / len(window_comments),
                    'negative_ratio': negative_count / len(window_comments),
                    'sample_comments': [
                        {'content': c['content'][:50], 'rpid': c['rpid']}
                        # 循环处理
                        for c in window_comments[:5]
                    ]
                })
        
        return hotspots
    
    def _normalize_content(self, content: str) -> str:
        """标准化评论内容（去除空格、表情等）
        
        预处理评论文本，便于后续去重匹配：
        # 对数据进行加工/分发
        - 去除空格、换行符
        - 转小写（针对英文）
        
        Args:
            content: 原始内容
            
        Returns:
            标准化后的内容
        """
        if not content:
            return ''
        
        # 去除空格、换行
        content = content.replace(' ', '').replace('\n', '').replace('\r', '')
        
        # 转小写（针对英文）
        content = content.lower()
        
        return content
    
    def _calculate_similarity(self, text1: str, text2: str,
                              fp1: Optional[int] = None,
                              fp2: Optional[int] = None) -> float:
        """计算两段文本的相似度（使用编辑距离）
        
        两阶段算法：
        1. SimHash 快速粗筛（汉明距离 <= 3）
        2. 编辑距离精确计算相似度
        
        性能优化（2026-08-22）：
        - 新增可选 fp1/fp2 预计算指纹参数，调用方已算好时
          直接传入，避免 41 万次重复 md5 计算
        
        Args:
            text1: 文本1
            text2: 文本2
            fp1: text1 的预计算 SimHash 指纹（可选）
            fp2: text2 的预计算 SimHash 指纹（可选）
            
        Returns:
            相似度（0-1），1 表示完全相同
        """
        # 先用SimHash快速粗筛
        # 优先使用预计算指纹，未传入才现场计算
        simhash1 = self._simhash(text1) if fp1 is None else fp1
        simhash2 = self._simhash(text2) if fp2 is None else fp2
        
        # 计算汉明距离
        hamming = bin(simhash1 ^ simhash2).count('1')
        
        # SimHash阈值：汉明距离<=3认为相似
        # 粗筛不通过直接返回0，避免昂贵的编辑距离计算
        if hamming > 3:
            return 0.0
        
        # 精确计算编辑距离
        distance = self._levenshtein_distance(text1, text2)
        max_len = max(len(text1), len(text2))
        
        if max_len == 0:
            return 1.0
        
        # 相似度 = 1 - (编辑距离 / 最大长度)
        similarity = 1.0 - (distance / max_len)
        return similarity
    
    def _simhash(self, text: str) -> int:
        """计算文本的SimHash值
        
        简化的 SimHash 算法实现，用于快速文本相似度粗筛。
        返回 64 位指纹，汉明距离 <= 3 认为相似。
        
        Args:
            text: 文本
            
        Returns:
            64位整数指纹
        """
        if not text:
            return 0
        
        # 简化的SimHash实现
        # 1. 分词（简单按字符）
        tokens = list(text)
        
        # 2. 计算每个token的hash
        # 64维权重向量，token贡献+1或-1
        v = [0] * 64
        # 遍历 tokens 逐项处理
        for token in tokens:
            # 数值转换存入 h
            h = int(hashlib.md5(token.encode('utf-8')).hexdigest(), 16)
            for i in range(64):
                bit = (h >> i) & 1
                if bit:
                    v[i] += 1
                else:
                    v[i] -= 1
        
        # 3. 降维
        # 权重为正的位设为1，其余为0
        fingerprint = 0
        for i in range(64):
            if v[i] > 0:
                fingerprint |= (1 << i)
        
        return fingerprint
    
    def _levenshtein_distance(self, s1: str, s2: str) -> int:
        """计算编辑距离（Levenshtein Distance）
        
        动态规划算法，计算将 s1 转换为 s2 需要的最少编辑操作数。
        用于精确评估文本相似度。
        
        Args:
            s1: 字符串1
            s2: 字符串2
            
        Returns:
            编辑距离（操作次数）
        """
        # 保证 s1 是较长串，减少循环次数
        if len(s1) < len(s2):
            return self._levenshtein_distance(s2, s1)
        
        # 空串距离等于另一串长度
        if len(s2) == 0:
            return len(s1)
        
        # 滚动数组优化：只需保留上一行
        previous_row = range(len(s2) + 1)
        for i, c1 in enumerate(s1):
            current_row = [i + 1]
            for j, c2 in enumerate(s2):
                # 插入、删除、替换的代价
                # 移除不再需要的数据/对象
                insertions = previous_row[j + 1] + 1
                deletions = current_row[j] + 1
                substitutions = previous_row[j] + (c1 != c2)
                current_row.append(min(insertions, deletions, substitutions))
            previous_row = current_row
        
        return previous_row[-1]


# ============ 使用示例 ============

def demo_deduplicate():
    """演示：评论去重
    
    完整示例：模拟六条评论，展示四层去重的效果。
    """
    
    # 模拟评论数据
    comments = [
        {'rpid': 1, 'uid': 100, 'uname': '用户A', 'content': '这个视频不错', 'ctime': datetime.now(), 'sentiment': 'positive'},
        {'rpid': 2, 'uid': 100, 'uname': '用户A', 'content': '这个视频不错', 'ctime': datetime.now(), 'sentiment': 'positive'},  # 用户A复读
        {'rpid': 3, 'uid': 101, 'uname': '用户B', 'content': '这个视频不错', 'ctime': datetime.now(), 'sentiment': 'positive'},  # 跨用户相同
        {'rpid': 4, 'uid': 102, 'uname': '用户C', 'content': '这个视频真不错', 'ctime': datetime.now(), 'sentiment': 'positive'},  # 模糊相似
        {'rpid': 5, 'uid': 103, 'uname': '用户D', 'content': '什么时候更新', 'ctime': datetime.now(), 'sentiment': 'neutral'},
        {'rpid': 6, 'uid': 104, 'uname': '用户E', 'content': '啥时候更新', 'ctime': datetime.now(), 'sentiment': 'neutral'},  # 模糊相似
    ]
    
    deduplicator = CommentDeduplicator()
    result = deduplicator.deduplicate(comments)
    
    print(f"原始评论: {result['original_count']} 条")
    # 输出信息到控制台
    print(f"去重后: {result['deduplicated_count']} 条")
    # 输出信息到控制台
    print(f"\n同用户重复组: {len(result['user_duplicates'])} 组")
    # 输出信息到控制台
    print(f"跨用户聚合组: {len(result['cross_user_groups'])} 组")
    # 输出信息到控制台
    print(f"模糊匹配组: {len(result['fuzzy_groups'])} 组")
    # 输出信息到控制台
    print(f"时间热点: {len(result['time_hotspots'])} 个")


if __name__ == '__main__':
    demo_deduplicate()
