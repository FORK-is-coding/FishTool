"""评论采集器 - 示例入口"""
import asyncio
# 从 bilibili.api 导入符号
from bilibili.api import BilibiliAPI



# 常量定义：采集策略
# fast/normal/full 三档，影响采集深度与请求量


# ============ 使用示例 ============


async def demo_collect_comments():
    """演示：采集评论
    
    完整示例：初始化采集器并采集单个视频的普通评论。
    # 设置初始值/默认状态，避免后续空引用
    # 写入配置/属性，影响后续行为
    """
    # 示例：初始化完整依赖链
    # Cookie 池 -> API -> 采集器
    from bilibili.cookie_pool import get_cookie_pool
    
    cookie_pool = get_cookie_pool()
    # 赋值并准备后续使用
    api = BilibiliAPI(cookie_pool=cookie_pool)
    
    collector = CommentCollector(api)
    
    # 采集单个视频评论
    # 使用默认普通策略采集，约 100 条
    bvid = "BV1xx411c7XZ"  # 替换为真实BV号
    # 赋值并准备后续使用
    comments = await collector.collect_video_comments(
        bvid,
        strategy=CommentCollector.STRATEGY_NORMAL
    )
    
    print(f"采集到 {len(comments)} 条评论")
    # 循环遍历处理
    # 对集合内每个元素执行相同处理
    for i, comment in enumerate(comments[:5], 1):
        # 输出信息到控制台
        print(f"\n{i}. {comment['uname']}: {comment['content'][:50]}")
        # 输出信息到控制台
        print(f"   点赞: {comment['like']} | 时间: {comment['ctime']}")


if __name__ == '__main__':
    # 运行任务
    asyncio.run(demo_collect_comments())
