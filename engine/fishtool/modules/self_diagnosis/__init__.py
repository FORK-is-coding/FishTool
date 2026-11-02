"""
账号自诊模块
提供个人账号数据分析、诊断报告生成、benchmark对比功能
"""
from .self_analyzer import SelfAnalyzer
from .report_generator import ReportGenerator

__all__ = ['SelfAnalyzer', 'ReportGenerator']
