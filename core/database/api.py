"""
数据库模块级工厂函数

拆分自 database.py 原始 L817-L885。
全局单例 db_manager 通过 init_database() 初始化。
"""
from typing import Optional

from sqlalchemy.orm import Session

from .manager import DatabaseManager


# 全局数据库管理器实例
# 通过 init_database() 初始化，get_session()/get_db() 使用
db_manager: Optional[DatabaseManager] = None


def init_database(db_path: str = "data/bili_ops.db") -> DatabaseManager:
    """初始化全局数据库管理器
    # 设置初始值/默认状态，避免后续空引用
    # 写入配置/属性，影响后续行为
    # 将数据持久化到表中
    
    Args:
        db_path: 数据库文件路径
        
    Returns:
        DatabaseManager实例
    """
    global db_manager
    # 赋值并准备后续使用
    db_manager = DatabaseManager(db_path)
    return db_manager

def get_session() -> Session:
    """获取数据库会话（模块级函数）
    # 读取数据并赋值给当前作用域变量
    
    未初始化时自动调用 init_database()，
    # 设置初始值/默认状态，避免后续空引用
    # 写入配置/属性，影响后续行为
    # 将数据持久化到表中
    调用方负责关闭会话。
    # 释放连接/窗口资源
    # 建立/复用数据库连接
    
    Returns:
        Session对象
    """
    if db_manager is None:
        init_database()
    return db_manager.get_session()

def get_db() -> Session:
    """获取数据库会话（依赖注入用）
    # 读取数据并赋值给当前作用域变量
    
    供 FastAPI Depends(get_db) 使用，
    请求结束时自动关闭会话。
    # 释放连接/窗口资源
    # 建立/复用数据库连接
    
    Yields:
        Session对象
    """
    if db_manager is None:
        init_database()
    
    # 获取session数据
    db = db_manager.get_session()
    # 异常保护：局部失败不影响主流程
    try:
        yield db
    # 异常处理
    finally:
        # 关闭连接释放资源
        # 释放连接/窗口资源
        # 建立/复用数据库连接
        db.close()
