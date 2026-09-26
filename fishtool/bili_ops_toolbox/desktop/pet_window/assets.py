"""桌宠资源路径定位与日志器。

拆分自原 pet_window.py 的模块级资源部分：
- _asset_roots 定位桌宠资源目录候选路径
- PET_ASSET_DIRS 供占位图加载使用
- logger 供桌宠各子模块共享
"""
import sys
from pathlib import Path

from core.logger import get_logger

# 源码运行时定位项目根目录；PyInstaller 运行时优先使用临时解包目录。
# 同时保留 exe 所在目录候选，兼容 onefile 外置资源和用户手工部署资源。
def _asset_roots() -> tuple[Path, ...]:
    """返回桌宠资源目录候选路径。"""
    roots = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        roots.append(Path(meipass) / "assets")
    roots.append(Path(sys.executable).resolve().parent / "assets")
    roots.append(Path(__file__).resolve().parents[2] / "assets")
    unique_roots = []
    for root in roots:
        if root not in unique_roots:
            unique_roots.append(root)
    return tuple(unique_roots)


PET_ASSET_DIRS = _asset_roots()


logger = get_logger(__name__)
