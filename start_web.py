"""
B站运营工具箱 - 启动脚本
# 触发服务/线程开始运行
启动Web服务器
# 触发服务/线程开始运行

本脚本是 Web 服务独立启动入口，流程：
# 触发服务/线程开始运行
1. init_project: 初始化配置/数据库/必要目录
# 设置初始值/默认状态，避免后续空引用
2. start_web_server: uvicorn 启动 FastAPI 应用
# 触发服务/线程开始运行

命令行参数：
- --host: 监听地址（默认 0.0.0.0）
- --port: 监听端口（默认 8000）
- --reload: 热重载开关（开发模式）
- --init-only: 仅初始化不启动服务
# 设置初始值/默认状态，避免后续空引用

说明：
- 初始化创建 logs/ data/ backups/ 目录
# 设置初始值/默认状态，避免后续空引用
- Web 应用本体在 web.main:app
- 桌面端（MainWindow）源码环境通过本脚本
  以子进程方式启动 Web 服务
  # 触发服务/线程开始运行
"""
import sys
# 导入模块
import logging
# 从 pathlib 导入符号
from pathlib import Path

# 添加项目根目录到Python路径
# 保证 web.main 等相对导入可用
project_root = Path(__file__).parent
# 插入元素
sys.path.insert(0, str(project_root))

# 从 core.logger 导入符号
from core.logger import get_logger
# 从 core.config 导入符号
from core.config import ConfigManager
# 从 core.database 导入符号
from core.database import init_database

logger = get_logger(__name__)


def init_project():
    """初始化项目
    # 设置初始值/默认状态，避免后续空引用
    
    依次初始化配置管理器、数据库，创建必要目录。
    # 设置初始值/默认状态，避免后续空引用
    
    Returns:
        bool: 初始化是否成功
        # 设置初始值/默认状态，避免后续空引用
    """
    logger.info("初始化B站运营工具箱...")
    
    # 1. 初始化配置
    # 配置管理器负责读写 config.yaml 与加密 secret
    try:
        config = ConfigManager()
        logger.info("配置管理器初始化成功")
    except Exception as e:
        logger.error(f"配置初始化失败: {e}")
        return False
    
    # 2. 初始化数据库
    # 数据库存放账号/Cookie/评论/预警等业务数据
    try:
        init_database()
        logger.info("数据库初始化成功")
    except Exception as e:
        logger.error(f"数据库初始化失败: {e}")
        return False
    
    # 3. 创建必要的目录
    # 日志/数据/备份目录缺失时自动创建
    dirs = ['logs', 'data', 'backups']
    # 遍历 dirs 逐项处理
    # 对集合内每个元素执行相同处理
    for dir_name in dirs:
        # 计算结果存入 dir_path
        # 对输入做运算得到结果
        dir_path = project_root / dir_name
        dir_path.mkdir(exist_ok=True)
    
    logger.info("项目初始化完成")
    return True


def start_web_server(host: str = "0.0.0.0", port: int = 8000, reload: bool = False):
    """启动Web服务器
    # 触发服务/线程开始运行
    
    Args:
        host: 监听地址
        port: 监听端口
        reload: 是否开启热重载（开发模式）
    """
    import uvicorn
    # 从 web.main 导入符号
    from web.main import app
    
    logger.info(f"启动Web服务器: http://{host}:{port}")
    logger.info(f"开发模式: {'开启' if reload else '关闭'}")
    
    # 异常保护：局部失败不影响主流程
    try:
        # 使用字符串导入方式启动（支持 reload）
        # reload=True 时 uvicorn 需要字符串形式的 app 路径
        uvicorn.run(
            "web.main:app",
            host=host,
            port=port,
            reload=reload,
            log_level="info"
        )
    except KeyboardInterrupt:
        logger.info("服务器已停止")
    except Exception as e:
        logger.error(f"服务器启动失败: {e}")
        sys.exit(1)


# 边界/有效性检查
if __name__ == "__main__":
    # 导入模块
    import argparse
    
    # 命令行参数解析
    # 支持 --host/--port/--reload/--init-only
    parser = argparse.ArgumentParser(description="B站运营工具箱启动器")
    # 添加argument
    # 将元素加入容器/布局
    parser.add_argument("--host", default="0.0.0.0", help="监听地址")
    # 添加argument
    # 将元素加入容器/布局
    parser.add_argument("--port", type=int, default=8000, help="监听端口")
    # 添加argument
    # 将元素加入容器/布局
    parser.add_argument("--reload", action="store_true", help="开启热重载（开发模式）")
    # 添加argument
    # 将元素加入容器/布局
    parser.add_argument("--init-only", action="store_true", help="仅初始化项目，不启动服务")
    
    args = parser.parse_args()
    
    # 初始化项目
    # 初始化失败直接退出
    if not init_project():
        logger.error("项目初始化失败，退出")
        sys.exit(1)
    
    # 仅初始化模式
    # 用于 CI/部署脚本只做初始化准备
    if args.init_only:
        logger.info("初始化完成，退出")
        sys.exit(0)
    
    # 启动服务器
    # 触发服务/线程开始运行
    start_web_server(host=args.host, port=args.port, reload=args.reload)
