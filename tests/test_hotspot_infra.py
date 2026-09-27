"""热点基础设施回归测试。"""
import pytest

from core.cache_service import CacheService
from modules.hotspot.risk_control import RequestBudget


def test_cache_is_singleton_and_namespaced():
    """缓存服务必须为单例且拒绝无命名空间 key。"""
    first = CacheService()
    second = CacheService()
    assert first is second
    first.set("bili:comment:BV1", {"source": "comment"}, ttl=60)
    assert second.get("bili:comment:BV1")["source"] == "comment"
    with pytest.raises(ValueError):
        first.set("raw-key", "bad")


def test_request_budget_429_backoff_is_capped():
    """429 退避从30秒指数增长并封顶10分钟。"""
    budget = RequestBudget()
    assert budget.report_429(0) == 30
    assert budget.report_429(4) == 480
    assert budget.report_429(5) == 600
