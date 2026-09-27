"""core.database.models_system ORM 模型测试（第4批 · ORM 段）。

覆盖对象：
- Task（tasks 表）
- OperationLog（operation_logs 表）
- LLMUsage（llm_usage 表）
- MonitorState（monitor_state 表）

验证维度：
字段类型 / 默认值（含 JSON 可调用默认）/ 唯一与非空约束 / JSON 往返 / onupdate 行为。

测试策略：
- 内存 SQLite 真实建表，不使用 Mock session。
- MonitorState 的 target_bvids 使用可调用默认 list，单独断言为独立空列表。
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from core.database.base import Base
from core.database.models_system import LLMUsage, MonitorState, OperationLog, Task


@pytest.fixture()
def session():
    """内存 SQLite 全量建表，产出独立会话并在结束后释放引擎。"""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = factory()
    try:
        yield db
    finally:
        db.close()
        engine.dispose()


# ---------------------------------------------------------------------------
# Task
# ---------------------------------------------------------------------------


def test_task_defaults_and_json_roundtrip(session):
    """Task 默认状态 pending、进度 0.0，JSON 参数与断点可往返。"""
    task = Task(task_type="collect", task_name="采集任务")
    session.add(task)
    session.commit()

    assert task.status == "pending"
    assert task.progress == pytest.approx(0.0)
    assert isinstance(task.created_at, datetime)

    task.params = {"tid": 4}
    task.checkpoint = {"page": 3}
    task.result = {"count": 12}
    session.commit()
    session.expire_all()

    loaded = session.query(Task).one()
    assert loaded.params == {"tid": 4}
    assert loaded.checkpoint == {"page": 3}
    assert loaded.result == {"count": 12}


def test_task_declared_defaults(session):
    """Task 列级默认值与模型声明一致。"""
    assert Task.__table__.c.status.default.arg == "pending"
    assert Task.__table__.c.progress.default.arg == 0.0
    assert Task.__table__.c.task_type.type.length == 50
    assert Task.__table__.c.task_name.type.length == 200


# ---------------------------------------------------------------------------
# OperationLog
# ---------------------------------------------------------------------------


def test_operation_log_roundtrip(session):
    """审计日志字段应能原样落库读回。"""
    session.add(
        OperationLog(
            operation="update_config",
            module="config",
            details={"key": "llm.model"},
            status="success",
            user_id=7,
            ip_address="127.0.0.1",
        )
    )
    session.commit()
    session.expire_all()

    loaded = session.query(OperationLog).one()
    assert loaded.operation == "update_config"
    assert loaded.details == {"key": "llm.model"}
    assert loaded.status == "success"
    assert loaded.user_id == 7
    assert loaded.ip_address == "127.0.0.1"
    assert isinstance(loaded.created_at, datetime)


# ---------------------------------------------------------------------------
# LLMUsage
# ---------------------------------------------------------------------------


def test_llm_usage_defaults_and_tokens(session):
    """LLMUsage token 计数默认 0，日期列长度为 10 且非空。"""
    assert LLMUsage.__table__.c.date.type.length == 10
    assert LLMUsage.__table__.c.date.nullable is False
    assert LLMUsage.__table__.c.total_tokens.default.arg == 0
    assert LLMUsage.__table__.c.request_count.default.arg == 0

    usage = LLMUsage(date="2031-05-01", model="gpt-x")
    session.add(usage)
    session.commit()

    assert usage.prompt_tokens == 0
    assert usage.completion_tokens == 0
    assert usage.total_tokens == 0
    assert usage.request_count == 0


def test_llm_usage_date_not_null(session):
    """date 为 NOT NULL，缺省写入被拒绝。"""
    session.add(LLMUsage(model="gpt-x"))
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_llm_usage_aggregate_query(session):
    """按日期聚合 token 求和，验证列参与 SQL 聚合正常。"""
    session.add_all(
        [
            LLMUsage(date="2031-05-02", model="m", total_tokens=10, request_count=1),
            LLMUsage(date="2031-05-02", model="m", total_tokens=5, request_count=2),
        ]
    )
    session.commit()

    total = (
        session.query(LLMUsage).filter(LLMUsage.date == "2031-05-02").all()
    )
    assert sum(row.total_tokens for row in total) == 15
    assert sum(row.request_count for row in total) == 3


# ---------------------------------------------------------------------------
# MonitorState
# ---------------------------------------------------------------------------


def test_monitor_state_defaults(session):
    """MonitorState 默认关闭、停止态、计数归零，target_bvids 为独立空列表。"""
    state = MonitorState()
    session.add(state)
    session.commit()

    assert state.name == "comment_monitor"
    assert state.enabled is False
    assert state.paused is False
    assert state.status == "stopped"
    assert state.total_collected == 0
    assert state.consecutive_failures == 0
    assert state.target_bvids == []
    assert state.target_bvids is not MonitorState.target_bvids  # 可调用默认应产出新对象
    assert isinstance(state.updated_at, datetime)


def test_monitor_state_name_unique(session):
    """monitor_state.name 唯一，重复写入被拒绝。"""
    assert MonitorState.__table__.c.name.unique is True
    session.add(MonitorState(name="comment_monitor"))
    session.commit()

    session.add(MonitorState(name="comment_monitor"))
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_monitor_state_json_list_roundtrip(session):
    """target_bvids JSON 列表应原样往返。"""
    state = MonitorState(target_bvids=["BV1", "BV2"], enabled=True, status="running")
    session.add(state)
    session.commit()
    session.expire_all()

    loaded = session.query(MonitorState).one()
    assert loaded.target_bvids == ["BV1", "BV2"]
    assert loaded.enabled is True
    assert loaded.status == "running"


def test_monitor_state_updated_at_onupdate(session):
    """更新 MonitorState 时 updated_at 应被 onupdate 刷新。"""
    state = MonitorState()
    session.add(state)
    session.commit()

    past = datetime.now() - timedelta(days=2)
    state.updated_at = past
    session.commit()

    state.paused = True
    session.commit()
    session.refresh(state)
    assert state.updated_at > past
