"""core.database.models_account ORM 模型测试（第4批 · ORM 段）。

覆盖对象：
- Account（accounts 表）
- CookiePool（cookie_pool 表）

验证维度：
字段类型 / 可空性与唯一约束 / 默认值 / 关系与级联 / 真实 SQLite 往返与约束失败分支。

测试策略：
- 使用内存 SQLite（StaticPool 复用同一连接）真实建表，不使用 Mock session。
- 约束失败分支一律从外部以 ``pytest.raises(IntegrityError)`` 断言。
- 不触碰仓库 ``data/bili_ops.db``，全部实例隔离在内存库内。
"""
from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import create_engine, inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

# 导入子模块会先加载 core.database 包，从而注册全部模型到同一 Base.metadata。
from core.database.base import Base
from core.database.models_account import Account, CookiePool


@pytest.fixture()
def session():
    """在内存 SQLite 上建全部表，产出独立会话，结束后自动关闭并释放引擎。"""
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
# 表结构与字段契约
# ---------------------------------------------------------------------------


def test_account_table_name_and_primary_key(session):
    """accounts 表名、主键与自增行为符合契约。"""
    assert Account.__tablename__ == "accounts"
    id_column = Account.__table__.c.id
    assert id_column.primary_key is True
    assert id_column.autoincrement is True


def test_account_column_types_nullable_and_unique(session):
    """Account 关键列的 SQL 类型、长度与可空性应符合模型定义。"""
    columns = Account.__table__.c

    assert columns.uid.type.length == 50
    assert columns.uid.nullable is False
    assert columns.uid.unique is True

    assert columns.username.type.length == 100
    assert columns.username.nullable is True

    assert columns.face.type.length == 500
    assert columns.cookie_hash.type.length == 64


def test_account_boolean_defaults_declared(session):
    """is_primary 默认 False、is_active 默认 True 应写入列默认值。"""
    assert Account.__table__.c.is_primary.default.arg is False
    assert Account.__table__.c.is_active.default.arg is True


def test_tables_registered_in_metadata(session):
    """两张表都应真实建入内存库，便于后续往返断言。"""
    tables = set(inspect(session.get_bind()).get_table_names())
    assert {"accounts", "cookie_pool"} <= tables


# ---------------------------------------------------------------------------
# 默认值与真实往返
# ---------------------------------------------------------------------------


def test_account_defaults_applied_on_insert(session):
    """插入后布尔默认值与时间戳默认值应自动填充。"""
    account = Account(uid="1001")
    session.add(account)
    session.commit()

    assert account.is_primary is False
    assert account.is_active is True
    assert isinstance(account.created_at, datetime)
    assert isinstance(account.updated_at, datetime)


def test_account_roundtrip_persists_fields(session):
    """显式字段应能落库并原样读回。"""
    session.add(
        Account(
            uid="1002",
            username="运营小助手",
            face="https://example.com/face.png",
            is_primary=True,
            cookie_hash="a" * 64,
        )
    )
    session.commit()

    loaded = session.query(Account).filter(Account.uid == "1002").one()
    assert loaded.username == "运营小助手"
    assert loaded.is_primary is True
    assert loaded.cookie_hash == "a" * 64


def test_account_uid_unique_constraint_rejects_duplicate(session):
    """重复 uid 触发唯一约束，由外部以 IntegrityError 断言。"""
    session.add(Account(uid="dup"))
    session.commit()

    session.add(Account(uid="dup"))
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_account_uid_not_null_constraint(session):
    """uid 为 NOT NULL，缺省写入应被数据库拒绝。"""
    session.add(Account(username="no-uid"))
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


# ---------------------------------------------------------------------------
# 关系与级联
# ---------------------------------------------------------------------------


def test_cookie_pool_defaults_and_relationship(session):
    """CookiePool 默认值正确，且能反向访问所属 Account。"""
    account = Account(uid="2001")
    cookie = CookiePool(account=account, cookie_data="encrypted-blob")
    session.add(cookie)
    session.commit()

    assert cookie.is_valid is True
    assert cookie.fail_count == 0
    assert cookie.account is account
    assert len(account.cookies) == 1


def test_cookie_pool_foreign_key_cascade_delete(session):
    """删除 Account 时，delete-orphan 级联应清空其 Cookie。"""
    account = Account(uid="3001")
    session.add_all(
        [
            CookiePool(account=account, cookie_data="blob-1"),
            CookiePool(account=account, cookie_data="blob-2"),
        ]
    )
    session.commit()
    assert session.query(CookiePool).count() == 2

    session.delete(account)
    session.commit()
    assert session.query(CookiePool).count() == 0


def test_cookie_pool_requires_parent_and_data(session):
    """account_id 与 cookie_data 均为 NOT NULL，缺一即拒绝写入。"""
    session.add(CookiePool(cookie_data="orphan-blob"))
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()
