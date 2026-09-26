"""热点 rev3 集成导入测试。"""
from sqlalchemy import inspect


def test_hotspot_routes_and_signal_model_import():
    """新路由可导入，数据库模型可注册。"""
    from web.routers.hotspot import router
    from core.database import DatabaseManager

    assert router is not None
    manager = DatabaseManager("data/test_hotspot_rev3.db")
    assert "hotspot_signal" in inspect(manager.engine).get_table_names()
