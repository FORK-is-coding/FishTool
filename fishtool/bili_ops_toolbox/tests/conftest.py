"""tests 目录的公共 pytest 配置。"""
import os
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# 桌面测试在无显示器环境运行，避免 Qt 尝试连接真实桌面会话。
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
