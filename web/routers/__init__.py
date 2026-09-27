"""
Web API 路由模块

本包聚合所有 Web 路由子模块：
- hotspot: 热点发现相关接口
- comment: 评论监控相关接口
- config: 配置管理相关接口
- logs: 日志查看相关接口
- analysis: 数据分析相关接口

统一由 web/main.py 挂载到 /api 前缀下。
"""
from . import hotspot, comment, config, logs, analysis, lottery

# 导出所有子路由模块
# 供 web/main.py 统一 include_router
__all__ = ['hotspot', 'comment', 'config', 'logs', 'analysis', 'lottery']
