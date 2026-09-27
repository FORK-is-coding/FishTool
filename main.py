"""
B站运营工具箱 - 主程序入口

程序统一启动入口，流程：
# 触发服务/线程开始运行
1. init_app: 初始化日志/配置/数据库
# 设置初始值/默认状态，避免后续空引用
2. async_main: 启动 Web 服务与桌面客户端（占位）
# 触发服务/线程开始运行
3. 保持事件循环运行直到 Ctrl+C 退出

实际使用建议：
- Web 服务单独启动：python start_web.py
# 触发服务/线程开始运行
- 桌面应用启动：python start_desktop.py
# 触发服务/线程开始运行
（本入口为兼容性保留的通用入口）

初始化细节：
# 设置初始值/默认状态，避免后续空引用
- 日志目录 = 数据库路径同级的 logs/
- 日志级别从 config app.log_level 读取
- 数据库路径从 config database.path 读取
"""
import sys
# 导入模块
import asyncio
# 从 pathlib 导入符号
from pathlib import Path

# 添加项目根目录到Python路径
# 保证相对导入（core/web/modules）在任何工作目录下可用
# 否则直接执行 python main.py 时会报 ModuleNotFoundError
sys.path.insert(0, str(Path(__file__).parent))

# 从 core.config 导入符号
from core.config import config
# 从 core.logger 导入符号
from core.logger import init_logger, get_logger
# 从 core.database 导入符号
from core.database import init_database

# 模块级日志器引用
# 初始化前为 None，初始化后才可安全使用
logger = None


def init_app():
    """初始化应用
    # 设置初始值/默认状态，避免后续空引用
    
    依次初始化日志、数据库，并输出启动配置。
    # 设置初始值/默认状态，避免后续空引用
    
    Returns:
        bool: 初始化是否成功
        # 设置初始值/默认状态，避免后续空引用
    """
    global logger
    
    # 初始化日志
    # 日志目录放在数据库文件同级 logs/ 下
    log_level = config.get('app.log_level', 'INFO')
    # 计算结果存入 log_dir
    # 对输入做运算得到结果
    log_dir = Path(config.get('database.path', 'data/bili_ops.db')).parent / 'logs'
    init_logger(str(log_dir), log_level)
    logger = get_logger(__name__)
    
    # 输出启动横幅
    # 触发服务/线程开始运行
    logger.info("="*60)
    logger.info(f"启动 {config.get('app.name')} v{config.get('app.version')}")
    logger.info("="*60)
    
    # 初始化数据库
    # 数据库路径同样来自配置
    db_path = config.get('database.path', 'data/bili_ops.db')
    init_database(db_path)
    logger.info(f"数据库初始化完成: {db_path}")
    
    # 显示配置信息
    # 便于启动时快速确认运行参数
    # 触发服务/线程开始运行
    logger.info(f"调试模式: {config.get('app.debug', False)}")
    logger.info(f"Web服务: http://{config.get('server.host')}:{config.get('server.port')}")
    
    return True


async def async_main():
    """异步主函数
    
    初始化后保持事件循环运行，直到收到退出信号。
    # 设置初始值/默认状态，避免后续空引用
    """
    # 初始化
    # 初始化失败直接返回错误码 1
    if not init_app():
        logger.error("应用初始化失败")
        return 1
    
    # TODO: 启动Web服务
    # 实际使用请运行 start_web.py
    logger.info("Web服务启动（待实现）")
    
    # TODO: 启动桌面客户端
    # 实际使用请运行 start_desktop.py
    logger.info("桌面客户端启动（待实现）")
    
    # 保持运行
    # 等待直到 Ctrl+C
    # 阻塞直到条件满足或超时
    try:
        logger.info("应用运行中，按 Ctrl+C 退出")
        await asyncio.Event().wait()
    except KeyboardInterrupt:
        logger.info("收到退出信号")
    
    return 0


def main():
    """主函数
    
    运行异步主循环，异常时记录并返回非零退出码。
    # 将结果交回调用方
    """
    try:
        # 运行异步主函数
        exit_code = asyncio.run(async_main())
        sys.exit(exit_code)
    except Exception as e:
        # 日志未初始化时回退到 print
        # 避免初始化阶段的异常无法输出
        if logger:
            logger.exception(f"应用运行异常: {e}")
        else:
            print(f"应用运行异常: {e}")
        sys.exit(1)


# 边界/有效性检查
if __name__ == '__main__':
    main()
