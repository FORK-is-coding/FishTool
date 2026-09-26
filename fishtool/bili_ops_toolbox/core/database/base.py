"""
数据库基础：logger 与 Base（declarative_base 实例）

拆分自 database.py 原始 L85-L88。
所有模型类统一从此处导入 Base，确保注册到同一个 metadata。
"""
import logging

from sqlalchemy.ext.declarative import declarative_base

logger = logging.getLogger(__name__)

# 所有模型类的基类，SQLAlchemy 通过它注册元数据
Base = declarative_base()
