"""bilibili.cookie_pool 底座测试（第1批补齐）。

覆盖范围：
- Cookie.__init__
- CookiePool.__init__ / load_from_db / add_cookie / load_from_secrets
- CookiePool.get_cookie / mark_invalid / check_all_cookies
- CookiePool._parse_cookie / _check_cookie_validity / get_stats
- 模块级 get_cookie_pool

测试策略：
- 数据库一律使用 tmp_path 下真实 SQLite（SQLAlchemy ORM），不使用 Mock session。
- 加解密走真实 Fernet（CookiePool 自带的 _cipher），验证密文可回解。
- B站接口用支持 ``__aenter__/__aexit__`` 的假客户端替换，并在断言中校验
  ``entered/exited``，确保 ``async with BilibiliAPI(...)`` 这段代码真实执行过，
  避免"AsyncMock 顶替 async context manager 导致分支被静默吞掉"的历史问题。
- 风控日志为契约级替身，用例内注明"契约测试，不验真实行为"。
"""
from __future__ import annotations

import asyncio
import hashlib
import importlib
from datetime import datetime

import pytest

from core.database import Account, CookiePool as CookiePoolModel, DatabaseManager
from core.exceptions import BilibiliAPIError, CookieExpiredError

cookie_pool_module = importlib.import_module("bilibili.cookie_pool")
CONFIG_MODULE = importlib.import_module("core.config")
LOGGER_MODULE = importlib.import_module("core.logger")

Cookie = cookie_pool_module.Cookie
CookiePool = cookie_pool_module.CookiePool


# ---------------------------------------------------------------------------
# 替身
# ---------------------------------------------------------------------------


class _FakeAPIClient:
    """支持异步上下文协议的 B站接口替身（真实实现，非 AsyncMock）。"""

    def __init__(self, controller, cookie=None) -> None:
        """绑定控制器与构造时传入的 Cookie。"""
        self._controller = controller
        self.cookie = cookie
        self.entered = 0
        self.exited = 0

    async def __aenter__(self):
        """进入上下文，计数用于证明该分支真实执行。"""
        self.entered += 1
        return self

    async def __aexit__(self, *_exc):
        """退出上下文。"""
        self.exited += 1
        return False

    async def get(self, url, retry_times=1):
        """返回预置 payload 或抛出预置异常。"""
        if self._controller.error is not None:
            raise self._controller.error
        return self._controller.payload


class _APIController:
    """控制假接口的返回内容并记录实例。"""

    def __init__(self) -> None:
        """默认返回未登录 payload。"""
        self.payload = {"isLogin": False}
        self.error = None
        self.instances = []

    def factory(self, cookie=None):
        """作为 BilibiliAPI 的可调用替身。"""
        client = _FakeAPIClient(self, cookie)
        self.instances.append(client)
        return client


class _FakeRiskLogger:
    """契约测试替身：记录风控事件，不落盘。"""

    def __init__(self) -> None:
        """初始化事件容器。"""
        self.cookie_expired = []

    def log_cookie_expired(self, cookie_name):
        """记录失效 Cookie 名称。"""
        self.cookie_expired.append(cookie_name)


class _FakeLoggerManager:
    """契约测试替身：仅暴露 risk_logger。"""

    def __init__(self, risk_logger) -> None:
        """注入风控日志替身。"""
        self.risk_logger = risk_logger


class _BrokenCipher:
    """加解密全失败的加密器替身，用于覆盖迁移失败分支。"""

    def decrypt(self, _data):
        """解密必然失败。"""
        raise ValueError("no key")

    def encrypt(self, _data):
        """加密必然失败。"""
        raise RuntimeError("encrypt boom")


class _SecretConfigManager:
    """契约测试替身：提供 get_secret 与 CookiePool 需要的 _cipher。"""

    def __init__(self, cookie: str) -> None:
        """保存待返回的 Cookie 串，并给出临时 Fernet 加密器。"""
        self._cookie = cookie
        from cryptography.fernet import Fernet

        self._cipher = Fernet(Fernet.generate_key())

    def get_secret(self, _key, default=None):
        """返回预置 Cookie，空串表示未配置。"""
        return self._cookie or default


class _PoolEnv:
    """封装临时库管理器与待测 CookiePool。"""

    def __init__(self, manager: DatabaseManager, pool) -> None:
        """保存两者引用。"""
        self.manager = manager
        self.pool = pool

    def session(self):
        """开一个新会话（调用方负责关闭）。"""
        return self.manager.get_session()


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """构造真实临时 SQLite + CookiePool，并把模块级 get_session 指向临时库。"""
    manager = DatabaseManager(str(tmp_path / "cookie_pool.db"))
    # 外键开启后，cookie_pool.account_id 必须指向真实父行；先建一个默认账号，
    # 避免所有直接插 cookie_pool 的用例都依赖孤儿行（#82）。
    session = manager.get_session()
    try:
        session.add(Account(uid="local-test", username="测试账号", is_primary=True))
        session.commit()
    finally:
        session.close()
    pool = CookiePool(check_interval=123)
    monkeypatch.setattr(cookie_pool_module, "get_session", manager.get_session)
    return _PoolEnv(manager, pool)


@pytest.fixture()
def api_stub(monkeypatch):
    """替换 cookie_pool 内的 BilibiliAPI 为可控假客户端。"""
    controller = _APIController()
    monkeypatch.setattr(cookie_pool_module, "BilibiliAPI", controller.factory)
    return controller


@pytest.fixture()
def risk_log(monkeypatch):
    """契约测试替身：捕获风控日志调用。"""
    recorder = _FakeRiskLogger()
    monkeypatch.setattr(LOGGER_MODULE, "logger_manager", _FakeLoggerManager(recorder))
    return recorder


def sessdata_digest(sessdata: str) -> str:
    """独立计算 cookie_pool.sessdata 列应存的摘要（不复用生产实现）。"""
    return "sha256:" + hashlib.sha256(sessdata.encode("utf-8")).hexdigest()


def _encrypt(pool, raw: str) -> str:
    """用池内真实 Fernet 加密，构造入库密文。"""
    return pool._cipher.encrypt(raw.encode("utf-8")).decode("utf-8")


def _add_row(session, pool, *, raw="SESSDATA=abc", sessdata="abc", is_valid=True, fail_count=0):
    """按当前落库格式向 cookie_pool 表插入一行并返回主键。

    sessdata 列只存摘要、bili_jct 列为空串，与 add_cookie 的落库口径一致。
    """
    row = CookiePoolModel(
        account_id=1,
        cookie_data=_encrypt(pool, raw),
        sessdata=sessdata_digest(sessdata),
        bili_jct="",
        buvid3="buvid",
        is_valid=is_valid,
        fail_count=fail_count,
    )
    session.add(row)
    session.commit()
    return row.id


# ---------------------------------------------------------------------------
# Cookie.__init__
# ---------------------------------------------------------------------------


def test_cookie_initialises_runtime_state():
    """Cookie 应保存全部字段并给出可用的运行时默认值。"""
    cookie = Cookie(
        id=7,
        account_id=3,
        cookie_data="SESSDATA=s",
        sessdata="s",
        bili_jct="j",
        buvid3="b",
    )

    assert (cookie.id, cookie.account_id) == (7, 3)
    assert cookie.cookie_data == "SESSDATA=s"
    assert (cookie.sessdata, cookie.bili_jct, cookie.buvid3) == ("s", "j", "b")
    assert cookie.is_valid is True
    assert cookie.last_used is None
    assert cookie.fail_count == 0


def test_cookie_accepts_invalid_flag():
    """显式 is_valid=False 必须被尊重。"""
    cookie = Cookie(id=1, account_id=1, cookie_data="x", sessdata="s", bili_jct="", buvid3="", is_valid=False)

    assert cookie.is_valid is False


# ---------------------------------------------------------------------------
# CookiePool._parse_cookie / __init__ / get_stats
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("SESSDATA=a; bili_jct=b", {"SESSDATA": "a", "bili_jct": "b"}),
        ("  SESSDATA=a  ;  bili_jct=b  ", {"SESSDATA": "a", "bili_jct": "b"}),
        ("SESSDATA=a;;junk", {"SESSDATA": "a"}),
        ("", {}),
        ("novalue", {}),
        ("k=a=b", {"k": "a=b"}),
        ("buvid3=;SESSDATA=s", {"buvid3": "", "SESSDATA": "s"}),
    ],
)
def test_parse_cookie_boundaries(env, raw, expected):
    """Cookie 解析必须容忍空白、空项、缺失等号与值内含等号。"""
    assert env.pool._parse_cookie(raw) == expected


def test_pool_starts_empty_with_lock_and_custom_interval(env):
    """新池应为空、游标归零并保存自定义检查间隔。"""
    assert env.pool.cookies == []
    assert env.pool.current_index == 0
    assert env.pool.check_interval == 123
    assert env.pool.last_check_time is None


def test_get_stats_counts_valid_and_invalid(env):
    """统计应区分有效/失效数量并序列化最后检查时间。"""
    env.pool.cookies = [
        Cookie(id=1, account_id=1, cookie_data="a", sessdata="a", bili_jct="", buvid3="", is_valid=True),
        Cookie(id=2, account_id=1, cookie_data="b", sessdata="b", bili_jct="", buvid3="", is_valid=False),
    ]

    stats = env.pool.get_stats()

    assert stats == {"total": 2, "valid": 1, "invalid": 1, "last_check": None}

    env.pool.last_check_time = datetime(2026, 1, 2, 3, 4, 5)
    assert env.pool.get_stats()["last_check"] == "2026-01-02T03:04:05"


# ---------------------------------------------------------------------------
# CookiePool.load_from_db
# ---------------------------------------------------------------------------


def test_load_from_db_decrypts_rows_and_keeps_failure_state(env):
    """加载必须解密 cookie_data，并保留 is_valid/fail_count/last_used。"""
    session = env.session()
    try:
        _add_row(session, env.pool, raw="SESSDATA=plain1", is_valid=False, fail_count=4)

        env.pool.load_from_db(session)
    finally:
        session.close()

    assert len(env.pool.cookies) == 1
    cookie = env.pool.cookies[0]
    assert cookie.cookie_data == "SESSDATA=plain1"
    assert cookie.is_valid is False
    assert cookie.fail_count == 4
    # 失效记录也必须留在内存池中，供巡检恢复；sessdata 从解密后的 Cookie 串解析
    assert env.pool.cookies[0].sessdata == "plain1"


def test_load_from_db_migrates_plaintext_row_to_ciphertext(env):
    """旧版明文行应被自动加密回写，之后按统一解密路径可读。"""
    session = env.session()
    try:
        row = CookiePoolModel(
            account_id=1,
            cookie_data="SESSDATA=legacy-plain",
            sessdata="legacy",
            bili_jct="",
            buvid3="",
            is_valid=True,
        )
        session.add(row)
        session.commit()
        row_id = row.id

        env.pool.load_from_db(session)
        session.expire_all()
        stored = session.query(CookiePoolModel).filter_by(id=row_id).one().cookie_data
    finally:
        session.close()

    assert env.pool.cookies[0].cookie_data == "SESSDATA=legacy-plain"
    # 库内已经是密文，且能被同一个 Fernet 解回
    assert stored != "SESSDATA=legacy-plain"
    assert env.pool._cipher.decrypt(stored.encode("utf-8")).decode("utf-8") == "SESSDATA=legacy-plain"


def test_load_from_db_keeps_plaintext_when_migration_fails(env):
    """加密迁移失败时必须回滚且仍把明文放进内存池，不能吞掉整条记录。"""
    session = env.session()
    try:
        row = CookiePoolModel(
            account_id=1,
            cookie_data="SESSDATA=broken",
            sessdata="broken",
            bili_jct="",
            buvid3="",
            is_valid=True,
        )
        session.add(row)
        session.commit()

        env.pool._cipher = _BrokenCipher()
        env.pool.load_from_db(session)
    finally:
        session.close()

    assert len(env.pool.cookies) == 1
    assert env.pool.cookies[0].cookie_data == "SESSDATA=broken"


def test_load_from_db_still_works_after_plaintext_migration(env, tmp_path):
    """旧库明文列经迁移改写为摘要/空串后，load_from_db 仍能从密文解析出完整字段。"""
    session = env.session()
    try:
        # 旧版落库格式：cookie_data 已加密，但 sessdata/bili_jct 列是明文
        session.add(CookiePoolModel(
            account_id=1,
            cookie_data=_encrypt(env.pool, "DedeUserID=6; SESSDATA=old-sess; bili_jct=old-jct; buvid3=bv"),
            sessdata="old-sess",
            bili_jct="old-jct",
            buvid3="bv",
            is_valid=True,
        ))
        session.commit()
    finally:
        session.close()

    # 重启：重新构造 DatabaseManager 触发 create_tables 中的迁移
    DatabaseManager(str(tmp_path / "cookie_pool.db"))

    session = env.session()
    try:
        session.expire_all()
        row = session.query(CookiePoolModel).one()
        assert (row.sessdata, row.bili_jct) == (sessdata_digest("old-sess"), "")
        env.pool.load_from_db(session)
    finally:
        session.close()

    cookie = env.pool.cookies[0]
    assert (cookie.sessdata, cookie.bili_jct, cookie.buvid3) == ("old-sess", "old-jct", "bv")
    # 迁移后的行仍可按 SESSDATA / DedeUserID 撤销
    assert env.pool.remove_account_cookies(sessdata="old-sess") == {"memory": 1, "db": 1}


def test_load_from_db_clears_previous_pool_content(env):
    """重复加载应重建内存池而不是追加，避免重启后 Cookie 翻倍。"""
    stale = Cookie(id=99, account_id=1, cookie_data="old", sessdata="old", bili_jct="", buvid3="")
    env.pool.cookies = [stale]

    session = env.session()
    try:
        _add_row(session, env.pool, raw="SESSDATA=fresh", sessdata="fresh")
        env.pool.load_from_db(session)
    finally:
        session.close()

    assert len(env.pool.cookies) == 1
    assert env.pool.cookies[0].cookie_data == "SESSDATA=fresh"


def test_load_from_db_skips_broken_row_and_keeps_others(env, monkeypatch):
    """单条记录解密+回写都失败时只跳过该条，不影响其余记录。"""
    session = env.session()
    try:
        _add_row(session, env.pool, raw="SESSDATA=good", sessdata="good")
        bad = CookiePoolModel(account_id=1, cookie_data="SESSDATA=bad", sessdata="bad", is_valid=True)
        session.add(bad)
        session.commit()

        # 让"明文回退"路径整体抛错，模拟该条记录不可修复
        original_cipher = env.pool._cipher

        class _Weird:
            def decrypt(self, data):
                """对 bad 记录解密失败，其余正常。"""
                if b"bad" in data:
                    raise RuntimeError("boom")
                return original_cipher.decrypt(data)

            def encrypt(self, data):
                """沿用真实加密器。"""
                return original_cipher.encrypt(data)

        class _CookieFactory:
            """对 bad 行构造失败，触发 load_from_db 的 continue 分支。"""

            def __call__(self, **kwargs):
                """bad 行抛错，其余返回真实 Cookie。"""
                if kwargs.get("sessdata") == "bad":
                    raise RuntimeError("bad row")
                return Cookie(**kwargs)

        env.pool._cipher = _Weird()
        monkeypatch.setattr(cookie_pool_module, "Cookie", _CookieFactory())
        env.pool.load_from_db(session)
    finally:
        session.close()

    assert [c.sessdata for c in env.pool.cookies] == ["good"]


# ---------------------------------------------------------------------------
# CookiePool.add_cookie
# ---------------------------------------------------------------------------


def test_add_cookie_rejects_missing_sessdata(env):
    """空串或缺 SESSDATA 的 Cookie 必须被拒绝且不入池。"""
    assert env.pool.add_cookie("") is None
    assert env.pool.add_cookie("buvid3=only") is None
    assert env.pool.cookies == []


def test_add_cookie_is_idempotent_for_same_sessdata(env):
    """同一 SESSDATA 重复添加必须返回同一对象且不重复入池。"""
    first = env.pool.add_cookie("SESSDATA=dup; buvid3=b1", persist=False)
    second = env.pool.add_cookie("SESSDATA=dup; buvid3=b2", persist=False)

    assert first is second
    assert len(env.pool.cookies) == 1
    assert env.pool.cookies[0].buvid3 == "b1"


def test_add_cookie_without_persist_keeps_temp_id(env):
    """persist=False 时只进内存池，主键保持占位值 -1。"""
    cookie = env.pool.add_cookie("SESSDATA=mem; bili_jct=j; buvid3=b", persist=False)

    assert cookie.id == -1
    assert cookie.account_id == 1
    assert (cookie.sessdata, cookie.bili_jct, cookie.buvid3) == ("mem", "j", "b")
    assert cookie.is_valid is True


def test_add_cookie_persist_writes_encrypted_row_and_backfills_id(env):
    """persist=True 必须加密入库并回填真实主键（账号存在时用其 id）。"""
    session = env.session()
    try:
        session.add(Account(uid="42", username="tester"))
        session.commit()

        cookie = env.pool.add_cookie("SESSDATA=persist; buvid3=b3", persist=True)
        session.expire_all()
        stored = session.query(CookiePoolModel).one()
    finally:
        session.close()

    assert cookie.id == stored.id
    assert cookie.id > 0
    assert stored.account_id == 1
    assert stored.cookie_data != "SESSDATA=persist; buvid3=b3"
    assert env.pool._cipher.decrypt(stored.cookie_data.encode()).decode() == "SESSDATA=persist; buvid3=b3"


def test_add_cookie_persist_creates_local_account_when_missing(tmp_path, monkeypatch):
    """空库首次持久化时补建本地账号，避免写 account_id 孤儿行（#82）。"""
    manager = DatabaseManager(str(tmp_path / "no_account.db"))
    monkeypatch.setattr(cookie_pool_module, "get_session", manager.get_session)
    pool = CookiePool()

    cookie = pool.add_cookie("SESSDATA=first; buvid3=b", persist=True)

    session = manager.get_session()
    try:
        stored = session.query(CookiePoolModel).one()
        account = session.query(Account).one()
    finally:
        session.close()

    assert cookie.id == stored.id
    assert account.uid == "local"
    assert account.username == "本地账号"
    assert account.is_primary is True
    assert stored.account_id == account.id


def test_add_cookie_persist_reuses_existing_db_row(env):
    """库中已有同 SESSDATA 记录时不新增行，只回填主键（热加载场景）。"""
    session = env.session()
    try:
        existing_id = _add_row(session, env.pool, raw="SESSDATA=exists", sessdata="exists")
        cookie = env.pool.add_cookie("SESSDATA=exists; buvid3=b", persist=True)
        count = session.query(CookiePoolModel).count()
    finally:
        session.close()

    assert cookie.id == existing_id
    assert count == 1


def test_add_cookie_degrades_to_memory_when_persist_fails(env, monkeypatch):
    """持久化异常不得影响内存池可用性，也不能向外抛。"""

    def _boom():
        """模拟数据库不可用。"""
        raise RuntimeError("db down")

    monkeypatch.setattr(cookie_pool_module, "get_session", _boom)

    cookie = env.pool.add_cookie("SESSDATA=degraded", persist=True)

    assert cookie is not None
    assert cookie.id == -1
    assert len(env.pool.cookies) == 1


def test_add_cookie_skips_db_when_encrypt_fails(env, caplog):
    """加密失败时不得落库（不再降级明文），登录态只保留在内存池并告警。"""
    session = env.session()
    try:

        class _EncryptFail:
            def decrypt(self, data):
                return data

            def encrypt(self, _data):
                raise RuntimeError("kms down")

        env.pool._cipher = _EncryptFail()
        with caplog.at_level("WARNING", logger="bilibili.cookie_pool"):
            cookie = env.pool.add_cookie("SESSDATA=plainfallback; bili_jct=jf", persist=True)
        session.expire_all()
        count = session.query(CookiePoolModel).count()
    finally:
        session.close()

    assert count == 0
    assert cookie.id == -1
    assert env.pool.cookies == [cookie]
    assert (cookie.sessdata, cookie.bili_jct) == ("plainfallback", "jf")
    assert any("加密失败" in record.getMessage() for record in caplog.records)


def test_add_cookie_persist_stores_digest_instead_of_plaintext(env):
    """落库行的 sessdata 列只存 sha256 摘要、bili_jct 列为空，库内不出现凭据明文。"""
    env.pool.add_cookie("DedeUserID=3; SESSDATA=secret-sess; bili_jct=secret-jct", persist=True)

    session = env.session()
    try:
        row = session.query(CookiePoolModel).one()
    finally:
        session.close()

    assert row.sessdata == sessdata_digest("secret-sess")
    assert row.sessdata.startswith("sha256:")
    assert row.bili_jct == ""
    for value in (row.cookie_data, row.sessdata, row.bili_jct):
        assert "secret-sess" not in value
        assert "secret-jct" not in value


def test_add_cookie_dedupes_by_digest_across_restart(env):
    """重启后内存池为空时，同一 SESSDATA 再次入库应按摘要命中已有行，不新增记录。"""
    first = env.pool.add_cookie("SESSDATA=same; bili_jct=a", persist=True)
    # 模拟重启：内存池清空，只剩数据库
    env.pool.cookies = []

    second = env.pool.add_cookie("SESSDATA=same; bili_jct=b", persist=True)

    session = env.session()
    try:
        count = session.query(CookiePoolModel).count()
    finally:
        session.close()

    assert count == 1
    assert second.id == first.id


# ---------------------------------------------------------------------------
# CookiePool.remove_account_cookies
# ---------------------------------------------------------------------------


def _remaining_sessdata(env):
    """返回 (内存池 sessdata 列表, 数据库 sessdata 列表)。

    数据库 sessdata 列只存摘要，这里从解密后的 cookie_data 解析 SESSDATA 再比较。
    """
    session = env.session()
    try:
        db_values = sorted(
            env.pool._parse_cookie(env.pool._cipher.decrypt(row.cookie_data.encode()).decode()).get("SESSDATA", "")
            for row in session.query(CookiePoolModel).all()
        )
    finally:
        session.close()
    return sorted(c.sessdata for c in env.pool.cookies), db_values


def test_remove_account_cookies_by_sessdata_clears_memory_and_db(env):
    """按 SESSDATA 撤销：内存与数据库同步删除，其他账号保留。"""
    env.pool.add_cookie("DedeUserID=1; SESSDATA=mine; bili_jct=j1", persist=True)
    env.pool.add_cookie("DedeUserID=2; SESSDATA=other; bili_jct=j2", persist=True)

    result = env.pool.remove_account_cookies(sessdata="mine")

    assert result == {"memory": 1, "db": 1}
    assert _remaining_sessdata(env) == (["other"], ["other"])


def test_remove_account_cookies_by_dede_user_id_clears_stale_sessions(env):
    """按 DedeUserID 撤销同一账号的历史会话（数据库中的旧行也要删）。"""
    session = env.session()
    try:
        # 同账号旧会话：只在数据库里，内存池未加载
        _add_row(session, env.pool, raw="DedeUserID=7; SESSDATA=old", sessdata="old")
        # 其他账号
        _add_row(session, env.pool, raw="DedeUserID=8; SESSDATA=keep", sessdata="keep")
    finally:
        session.close()
    env.pool.add_cookie("DedeUserID=7; SESSDATA=new", persist=True)

    result = env.pool.remove_account_cookies(sessdata="new", dede_user_id="7")

    assert result == {"memory": 1, "db": 2}
    assert _remaining_sessdata(env) == ([], ["keep"])


def test_remove_account_cookies_matches_legacy_plaintext_row(env):
    """数据库中的旧版明文行同样能按 DedeUserID 识别并删除。"""
    session = env.session()
    try:
        session.add(CookiePoolModel(account_id=1, cookie_data="DedeUserID=9; SESSDATA=plain", sessdata="plain"))
        session.commit()
    finally:
        session.close()

    result = env.pool.remove_account_cookies(dede_user_id="9")

    assert result == {"memory": 0, "db": 1}
    assert _remaining_sessdata(env) == ([], [])


def test_remove_account_cookies_matches_db_row_by_digest(env):
    """数据库行 cookie_data 无法用当前密钥解密时，按 sessdata 摘要列仍能命中并删除。"""
    from cryptography.fernet import Fernet

    other_cipher = Fernet(Fernet.generate_key())
    session = env.session()
    try:
        # 用另一把密钥加密：解密兜底匹配失效，只能靠摘要列命中
        session.add(CookiePoolModel(
            account_id=1,
            cookie_data=other_cipher.encrypt(b"SESSDATA=db-only").decode(),
            sessdata=sessdata_digest("db-only"),
            bili_jct="",
        ))
        session.commit()
        _add_row(session, env.pool, raw="SESSDATA=keep", sessdata="keep")
    finally:
        session.close()

    result = env.pool.remove_account_cookies(sessdata="db-only")

    assert result == {"memory": 0, "db": 1}
    assert _remaining_sessdata(env) == ([], ["keep"])


def test_remove_account_cookies_ignores_empty_arguments(env, monkeypatch):
    """两个参数都为空时不得删除任何记录，也不访问数据库。"""
    env.pool.add_cookie("DedeUserID=1; SESSDATA=stay", persist=True)

    def _should_not_be_called():
        """若被调用说明空参数保护失效。"""
        raise AssertionError("空参数不应访问数据库")

    monkeypatch.setattr(cookie_pool_module, "get_session", _should_not_be_called)

    assert env.pool.remove_account_cookies(sessdata="  ", dede_user_id="") == {"memory": 0, "db": 0}
    assert [c.sessdata for c in env.pool.cookies] == ["stay"]


def test_remove_account_cookies_raises_after_clearing_memory_when_db_fails(env, monkeypatch):
    """数据库不可用时内存池仍先清理，异常交给调用方提示。"""
    env.pool.add_cookie("SESSDATA=gone", persist=False)

    def _boom():
        """模拟数据库不可用。"""
        raise RuntimeError("db down")

    monkeypatch.setattr(cookie_pool_module, "get_session", _boom)

    with pytest.raises(RuntimeError):
        env.pool.remove_account_cookies(sessdata="gone")
    assert env.pool.cookies == []


def test_removed_cookie_is_not_revived_by_check_or_reload(env, monkeypatch, risk_log):
    """撤销后巡检判定"有效"也不能复活，重启重新加载也不能恢复。"""
    env.pool.add_cookie("DedeUserID=5; SESSDATA=bye", persist=True)
    env.pool.remove_account_cookies(sessdata="bye")

    async def _valid(_cookie_data):
        """模拟接口认为 Cookie 仍然有效。"""
        return True

    monkeypatch.setattr(env.pool, "_check_cookie_validity", _valid)
    session = env.session()
    try:
        asyncio.run(env.pool.check_all_cookies(session))
        env.pool.load_from_db(session)
    finally:
        session.close()

    assert env.pool.cookies == []
    assert asyncio.run(env.pool.get_cookie()) is None
    assert _remaining_sessdata(env) == ([], [])


def test_remove_during_running_check_does_not_revive(env, monkeypatch, risk_log):
    """巡检进行中撤销 Cookie：巡检结束后内存池与数据库都不能出现该记录。"""
    env.pool.add_cookie("DedeUserID=5; SESSDATA=racing", persist=True)
    session = env.session()

    async def _remove_then_valid(_cookie_data):
        """在巡检等待网络期间执行退出登录，再返回"有效"。"""
        env.pool.remove_account_cookies(sessdata="racing")
        return True

    env.pool.cookies[0].is_valid = False
    monkeypatch.setattr(env.pool, "_check_cookie_validity", _remove_then_valid)
    try:
        asyncio.run(env.pool.check_all_cookies(session))
    finally:
        session.close()

    assert env.pool.cookies == []
    assert _remaining_sessdata(env) == ([], [])


# ---------------------------------------------------------------------------
# CookiePool.load_from_secrets
# ---------------------------------------------------------------------------


def test_load_from_secrets_skips_when_pool_not_empty(env, monkeypatch):
    """池内已有 Cookie 时必须直接返回 0，不读 secrets。"""
    env.pool.cookies = [Cookie(id=1, account_id=1, cookie_data="a", sessdata="a", bili_jct="", buvid3="")]

    def _should_not_be_called():
        """若被调用说明短路失效。"""
        raise AssertionError("池非空时不应读取 secrets")

    monkeypatch.setattr(CONFIG_MODULE, "ConfigManager", _should_not_be_called)

    assert env.pool.load_from_secrets() == 0


def test_load_from_secrets_backfills_pool_from_config(env, monkeypatch, api_stub, risk_log):
    """契约测试，不验真实行为：secrets 有登录态时应回填 1 条并持久化。"""
    monkeypatch.setattr(
        CONFIG_MODULE,
        "ConfigManager",
        lambda: _SecretConfigManager("SESSDATA=from-secret; buvid3=sv"),
    )

    assert env.pool.load_from_secrets() == 1
    assert len(env.pool.cookies) == 1
    assert env.pool.cookies[0].sessdata == "from-secret"

    session = env.session()
    try:
        assert session.query(CookiePoolModel).count() == 1
    finally:
        session.close()


def test_load_from_secrets_returns_zero_without_secret(env, monkeypatch):
    """secrets 为空时应返回 0 且不建池。"""
    monkeypatch.setattr(CONFIG_MODULE, "ConfigManager", lambda: _SecretConfigManager(""))

    assert env.pool.load_from_secrets() == 0
    assert env.pool.cookies == []


def test_load_from_secrets_swallows_config_error(env, monkeypatch):
    """读取 secrets 抛错时只记日志并返回 0，不得中断启动。"""

    def _boom():
        """模拟配置层异常。"""
        raise RuntimeError("config broken")

    monkeypatch.setattr(CONFIG_MODULE, "ConfigManager", _boom)

    assert env.pool.load_from_secrets() == 0


# ---------------------------------------------------------------------------
# CookiePool.get_cookie
# ---------------------------------------------------------------------------


def test_get_cookie_returns_none_when_pool_empty(env):
    """空池必须返回 None。"""
    assert asyncio.run(env.pool.get_cookie()) is None


def test_get_cookie_returns_none_when_all_invalid(env):
    """全部失效时必须返回 None，避免用失效 Cookie 发起请求。"""
    env.pool.cookies = [
        Cookie(id=1, account_id=1, cookie_data="a", sessdata="a", bili_jct="", buvid3="", is_valid=False)
    ]

    assert asyncio.run(env.pool.get_cookie()) is None


def test_get_cookie_rotates_by_cursor_and_touches_last_used(env):
    """轮换必须按游标取模循环，并在返回前刷新 last_used。"""
    first = Cookie(id=1, account_id=1, cookie_data="a", sessdata="a", bili_jct="", buvid3="")
    second = Cookie(id=2, account_id=1, cookie_data="b", sessdata="b", bili_jct="", buvid3="")
    env.pool.cookies = [first, second]

    picked = [asyncio.run(env.pool.get_cookie()) for _ in range(3)]

    assert [c.sessdata for c in picked] == ["a", "b", "a"]
    assert env.pool.current_index == 1
    assert all(isinstance(c.last_used, datetime) for c in picked)


def test_get_cookie_skips_invalid_and_uses_valid_subset(env):
    """轮换只在有效子集内进行，失效项不参与。"""
    valid_a = Cookie(id=1, account_id=1, cookie_data="a", sessdata="a", bili_jct="", buvid3="")
    invalid = Cookie(id=2, account_id=1, cookie_data="b", sessdata="b", bili_jct="", buvid3="", is_valid=False)
    valid_b = Cookie(id=3, account_id=1, cookie_data="c", sessdata="c", bili_jct="", buvid3="")
    env.pool.cookies = [valid_a, invalid, valid_b]

    picked = [asyncio.run(env.pool.get_cookie()) for _ in range(4)]

    assert [c.sessdata for c in picked] == ["a", "c", "a", "c"]


# ---------------------------------------------------------------------------
# CookiePool.mark_invalid
# ---------------------------------------------------------------------------


def test_mark_invalid_updates_memory_and_database(env, risk_log):
    """契约测试，不验真实行为：失效必须同步内存状态、失败计数与数据库行。"""
    session = env.session()
    try:
        row_id = _add_row(session, env.pool, raw="SESSDATA=z", sessdata="z")
        cookie = Cookie(id=row_id, account_id=1, cookie_data="SESSDATA=z", sessdata="z", bili_jct="", buvid3="")
        env.pool.cookies = [cookie]

        asyncio.run(env.pool.mark_invalid(cookie, session, "nav 返回未登录"))
        session.expire_all()
        row = session.query(CookiePoolModel).filter_by(id=row_id).one()
    finally:
        session.close()

    assert cookie.is_valid is False
    assert cookie.fail_count == 1
    assert row.is_valid is False
    assert row.fail_count == 1
    assert risk_log.cookie_expired == ["账号1"]


def test_mark_invalid_without_db_row_still_updates_memory(env, risk_log):
    """库中查不到行时不得抛异常，内存状态仍要更新。"""
    session = env.session()
    try:
        cookie = Cookie(id=999, account_id=5, cookie_data="x", sessdata="x", bili_jct="", buvid3="")
        asyncio.run(env.pool.mark_invalid(cookie, session, "无对应行"))
    finally:
        session.close()

    assert cookie.is_valid is False
    assert cookie.fail_count == 1
    assert risk_log.cookie_expired == ["账号5"]


def test_mark_invalid_accumulates_fail_count(env, risk_log):
    """重复标记必须累加失败次数，而不是每次都重置为 1。"""
    session = env.session()
    try:
        cookie = Cookie(id=1000, account_id=1, cookie_data="x", sessdata="x", bili_jct="", buvid3="")
        asyncio.run(env.pool.mark_invalid(cookie, session, "第一次"))
        asyncio.run(env.pool.mark_invalid(cookie, session, "第二次"))
    finally:
        session.close()

    assert cookie.fail_count == 2
    assert len(risk_log.cookie_expired) == 2


# ---------------------------------------------------------------------------
# CookiePool.check_all_cookies
# ---------------------------------------------------------------------------


def test_check_all_cookies_marks_invalid_on_failed_check(env, monkeypatch, risk_log):
    """有效转失效时必须调 mark_invalid 并落库。"""
    session = env.session()
    try:
        row_id = _add_row(session, env.pool, raw="SESSDATA=x", sessdata="x")
        env.pool.cookies = [
            Cookie(id=row_id, account_id=1, cookie_data="SESSDATA=x", sessdata="x", bili_jct="", buvid3="")
        ]

        async def _invalid(_cookie_data):
            """模拟接口明确判定未登录。"""
            return False

        monkeypatch.setattr(env.pool, "_check_cookie_validity", _invalid)
        asyncio.run(env.pool.check_all_cookies(session))

        session.expire_all()
        row = session.query(CookiePoolModel).filter_by(id=row_id).one()
    finally:
        session.close()

    assert env.pool.cookies[0].is_valid is False
    assert row.is_valid is False
    assert row.last_check is not None
    assert env.pool.last_check_time is not None


def test_check_all_cookies_restores_invalid_cookie(env, monkeypatch, risk_log):
    """失效转有效时必须恢复标记、清零失败计数并落库。"""
    session = env.session()
    try:
        row_id = _add_row(session, env.pool, raw="SESSDATA=y", sessdata="y", is_valid=False, fail_count=3)
        env.pool.cookies = [
            Cookie(id=row_id, account_id=1, cookie_data="SESSDATA=y", sessdata="y", bili_jct="", buvid3="", is_valid=False)
        ]

        async def _valid(_cookie_data):
            """模拟接口确认已登录。"""
            return True

        monkeypatch.setattr(env.pool, "_check_cookie_validity", _valid)
        asyncio.run(env.pool.check_all_cookies(session))

        session.expire_all()
        row = session.query(CookiePoolModel).filter_by(id=row_id).one()
    finally:
        session.close()

    assert env.pool.cookies[0].is_valid is True
    assert env.pool.cookies[0].fail_count == 0
    assert row.is_valid is True
    assert row.fail_count == 0


def test_check_all_cookies_keeps_state_when_undeterminable(env, monkeypatch, risk_log):
    """返回 None（网络/风控不可判定）时必须保持原状态，不能误杀登录态。"""
    session = env.session()
    try:
        row_id = _add_row(session, env.pool, raw="SESSDATA=z2", sessdata="z2")
        env.pool.cookies = [
            Cookie(id=row_id, account_id=1, cookie_data="SESSDATA=z2", sessdata="z2", bili_jct="", buvid3="")
        ]

        async def _unknown(_cookie_data):
            """模拟无法判定。"""
            return None

        monkeypatch.setattr(env.pool, "_check_cookie_validity", _unknown)
        asyncio.run(env.pool.check_all_cookies(session))
    finally:
        session.close()

    assert env.pool.cookies[0].is_valid is True
    assert env.pool.cookies[0].fail_count == 0


def test_check_all_cookies_isolates_single_failure(env, monkeypatch, risk_log):
    """单条检查抛异常不得中断整轮巡检。"""
    session = env.session()
    try:
        env.pool.cookies = [
            Cookie(id=1, account_id=1, cookie_data="boom", sessdata="boom", bili_jct="", buvid3=""),
            Cookie(id=2, account_id=1, cookie_data="ok", sessdata="ok", bili_jct="", buvid3=""),
        ]
        seen = []

        async def _maybe(raw):
            """第一条抛错，第二条正常返回。"""
            seen.append(raw)
            if raw == "boom":
                raise BilibiliAPIError("network down")
            return True

        monkeypatch.setattr(env.pool, "_check_cookie_validity", _maybe)
        asyncio.run(env.pool.check_all_cookies(session))
    finally:
        session.close()

    assert seen == ["boom", "ok"]
    assert env.pool.cookies[1].is_valid is True


# ---------------------------------------------------------------------------
# CookiePool._check_cookie_validity
# ---------------------------------------------------------------------------


def test_check_cookie_validity_uses_async_context_and_reports_login(env, api_stub):
    """必须真的走 async with BilibiliAPI(...)：进入/退出各一次并返回 isLogin。"""
    api_stub.payload = {"isLogin": True}

    result = asyncio.run(env.pool._check_cookie_validity("SESSDATA=a"))

    assert result is True
    assert len(api_stub.instances) == 1
    # 这两条断言是"非假覆盖"的关键证据
    assert api_stub.instances[0].entered == 1
    assert api_stub.instances[0].exited == 1
    assert api_stub.instances[0].cookie == "SESSDATA=a"


def test_check_cookie_validity_returns_false_when_not_logged_in(env, api_stub):
    """payload 明确 isLogin=False 时应返回 False。"""
    api_stub.payload = {"isLogin": False}

    assert asyncio.run(env.pool._check_cookie_validity("SESSDATA=a")) is False


def test_check_cookie_validity_returns_false_on_cookie_expired(env, api_stub):
    """CookieExpiredError 必须判定为失效（False），而非不可判定。"""
    api_stub.error = CookieExpiredError()

    assert asyncio.run(env.pool._check_cookie_validity("SESSDATA=a")) is False


def test_check_cookie_validity_returns_none_on_transient_error(env, api_stub):
    """普通请求异常必须返回 None（不可判定），否则一次风控就会清空池。"""
    api_stub.error = BilibiliAPIError("502 bad gateway")

    assert asyncio.run(env.pool._check_cookie_validity("SESSDATA=a")) is None


def test_check_cookie_validity_exits_context_even_on_error(env, api_stub):
    """异常路径同样必须退出上下文（资源真实释放）。"""
    api_stub.error = BilibiliAPIError("boom")

    asyncio.run(env.pool._check_cookie_validity("SESSDATA=a"))

    assert api_stub.instances[0].entered == 1
    assert api_stub.instances[0].exited == 1


# ---------------------------------------------------------------------------
# 模块级 get_cookie_pool
# ---------------------------------------------------------------------------


def test_get_cookie_pool_is_lazy_singleton_loading_db_and_secrets(env, monkeypatch, api_stub, risk_log):
    """契约测试，不验真实行为：首次调用应建池、加载 DB、再回退 secrets，之后复用。"""
    session = env.session()
    try:
        _add_row(session, env.pool, raw="SESSDATA=db-row", sessdata="db-row")
    finally:
        session.close()

    # 新池须与入库时使用同一密钥，才能从解密后的 cookie_data 解析出 SESSDATA
    config_stub = _SecretConfigManager("SESSDATA=secret-row")
    config_stub._cipher = env.pool._cipher

    monkeypatch.setattr(cookie_pool_module, "_global_cookie_pool", None)
    monkeypatch.setattr(cookie_pool_module, "get_session", env.manager.get_session)
    monkeypatch.setattr(CONFIG_MODULE, "ConfigManager", lambda: config_stub)

    first = cookie_pool_module.get_cookie_pool()
    second = cookie_pool_module.get_cookie_pool()

    assert first is second
    assert [c.sessdata for c in first.cookies] == ["db-row"]
    assert first.check_interval == 1800
