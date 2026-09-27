"""评论舆情监控器预警 Mixin 的契约级测试。

覆盖 modules/comment/_alerts.py 的 MonitorAlertsMixin：
- _detect_alerts：四条规则（负面占比/评论突增/风险词/自定义词）的组合检测
- _check_comment_surge：首采基准、突增判定、滑窗上限、零均值边界
- _check_custom_keywords：无关键词短路、命中样本、样本上限与内容截断
- _save_monitoring_record：视频占位创建、预警入库、回调推送与回调容错
- get_alerts / mark_alert_read：多条件查询、已读标记、缺失记录

数据库使用 tmp_path 下的真实 SQLite；异步入口外层带 wait_for 超时兜底。
"""

from __future__ import annotations

import asyncio

import pytest

from core.database import CommentAlert, DatabaseManager, Video
from modules.comment import _alerts as alerts_module
from modules.comment.monitor import CommentMonitor

WAIT_SECONDS = 5.0


def _run(coro):
    """带外层超时兜底的协程运行器。"""
    return asyncio.run(asyncio.wait_for(coro, timeout=WAIT_SECONDS))


def _monitor(callback=None) -> CommentMonitor:
    """构造以占位对象为 api 的真实监控器。"""
    return CommentMonitor(api=object(), alert_callback=callback)


class _CommitFailSession:
    """提交即失败的会话替身，用于验证回滚分支。"""

    def __init__(self) -> None:
        self.rolled_back = False
        self.closed = False

    def query(self, model):
        """返回一个恒为空的查询替身。"""

        class _Query:
            def filter_by(self, **kwargs):
                return self

            def first(self):
                return None

        return _Query()

    def add(self, obj) -> None:
        """接收但不持久化。"""

    def flush(self) -> None:
        """空实现。"""

    def commit(self) -> None:
        """模拟提交失败。"""
        raise RuntimeError("commit boom")

    def rollback(self) -> None:
        """记录回滚。"""
        self.rolled_back = True

    def close(self) -> None:
        """记录关闭。"""
        self.closed = True


# --------------------------------------------------------------------- 突增检测

def test_check_comment_surge_first_observation_records_baseline() -> None:
    """首次监控没有历史，只记录基准并返回 None。"""
    monitor = _monitor()

    assert _run(monitor._check_comment_surge("BV1", 10)) is None
    assert len(monitor._monitoring_history["BV1"]) == 1


def test_check_comment_surge_detects_spike() -> None:
    """当前数超过历史均值 2 倍时应产生突增预警。"""
    monitor = _monitor()
    _run(monitor._check_comment_surge("BV1", 10))

    alert = _run(monitor._check_comment_surge("BV1", 30))

    assert alert is not None
    assert alert["type"] == "comment_surge"
    assert alert["level"] == "medium"
    assert alert["data"]["current_count"] == 30
    assert alert["data"]["avg_count"] == 10
    assert alert["data"]["surge_ratio"] == 3.0


def test_check_comment_surge_no_alert_within_threshold() -> None:
    """未超过阈值时不产生预警。"""
    monitor = _monitor()
    _run(monitor._check_comment_surge("BV1", 10))

    assert _run(monitor._check_comment_surge("BV1", 20)) is None


def test_check_comment_surge_zero_average_branch() -> None:
    """历史均值 0 时 surge_ratio 回落 0（避免除零）。"""
    monitor = _monitor()
    _run(monitor._check_comment_surge("BV1", 0))

    alert = _run(monitor._check_comment_surge("BV1", 5))

    assert alert is not None
    assert alert["data"]["surge_ratio"] == 0


def test_check_comment_surge_history_is_capped() -> None:
    """历史滑动窗口应固定为 10 条。"""
    monitor = _monitor()
    for index in range(15):
        _run(monitor._check_comment_surge("BV1", index))

    assert len(monitor._monitoring_history["BV1"]) == 10


# --------------------------------------------------------------------- 自定义关键词

def test_check_custom_keywords_short_circuits_without_keywords() -> None:
    """未设置自定义关键词时应直接返回空列表。"""
    monitor = _monitor()

    assert monitor._check_custom_keywords([{"rpid": 1, "content": "任意"}]) == []


def test_check_custom_keywords_matches_and_samples() -> None:
    """命中关键词时应生成预警并附带样本。"""
    monitor = _monitor()
    monitor.add_custom_keywords(["品牌X", "翻车"])
    comments = [
        {"rpid": 1, "content": "品牌X又出问题了"},
        {"rpid": 2, "content": "无关内容"},
        {"rpid": 3, "content": "翻车现场"},
    ]

    alerts = monitor._check_custom_keywords(comments)

    assert len(alerts) == 1
    assert alerts[0]["type"] == "custom_keywords"
    assert alerts[0]["data"]["matched_count"] == 2
    assert [sample["rpid"] for sample in alerts[0]["data"]["samples"]] == [1, 3]
    assert alerts[0]["data"]["samples"][0]["keywords"] == ["品牌X"]


def test_check_custom_keywords_caps_samples_and_truncates_content() -> None:
    """样本最多 5 条且内容截断到 100 字。"""
    monitor = _monitor()
    monitor.add_custom_keywords(["关键词"])
    comments = [{"rpid": index, "content": "关键词" + "字" * 200} for index in range(8)]

    alerts = monitor._check_custom_keywords(comments)

    assert alerts[0]["data"]["matched_count"] == 8
    assert len(alerts[0]["data"]["samples"]) == 5
    assert len(alerts[0]["data"]["samples"][0]["content"]) == 100


# --------------------------------------------------------------------- 预警组合

def test_detect_alerts_negative_ratio_medium() -> None:
    """负面占比超阈值未过半时判中危。"""
    monitor = _monitor()
    sentiment = {"negative_ratio": 0.4, "sentiment_distribution": {}}

    alerts = _run(monitor._detect_alerts("BV1", [{}], [{}], sentiment))

    assert [item["type"] for item in alerts] == ["negative_surge"]
    assert alerts[0]["level"] == "medium"


def test_detect_alerts_negative_ratio_high() -> None:
    """负面占比过半时升级为高危。"""
    monitor = _monitor()
    sentiment = {"negative_ratio": 0.6, "sentiment_distribution": {}}

    alerts = _run(monitor._detect_alerts("BV1", [{}], [{}], sentiment))

    assert alerts[0]["level"] == "high"


def test_detect_alerts_negative_ratio_exactly_threshold_no_alert() -> None:
    """恰好等于阈值（非严格大于）不触发负面预警。"""
    monitor = _monitor()
    sentiment = {"negative_ratio": 0.3, "sentiment_distribution": {}}

    assert _run(monitor._detect_alerts("BV1", [{}], [{}], sentiment)) == []


@pytest.mark.parametrize(("risk_count", "expected"), [(5, True), (6, True), (4, False)])
def test_detect_alerts_risk_keywords_threshold(risk_count, expected) -> None:
    """风险评论数达到 5 条才触发高危风险预警。"""
    monitor = _monitor()
    sentiment = {"negative_ratio": 0.0, "sentiment_distribution": {"risk": risk_count}}

    alerts = _run(monitor._detect_alerts("BV1", [{}], [{}], sentiment))

    assert any(item["type"] == "risk_keywords" for item in alerts) is expected


def test_detect_alerts_without_sentiment_skips_sentiment_rules() -> None:
    """情感结果为 None 时不应产生负面/风险预警。"""
    monitor = _monitor()

    assert _run(monitor._detect_alerts("BV1", [{}], [{}], None)) == []


def test_detect_alerts_combines_negative_and_risk() -> None:
    """同一轮可同时命中多条规则。"""
    monitor = _monitor()
    sentiment = {"negative_ratio": 0.7, "sentiment_distribution": {"risk": 5}}

    alerts = _run(monitor._detect_alerts("BV1", [{}], [{}], sentiment))

    types = {item["type"] for item in alerts}
    assert types == {"negative_surge", "risk_keywords"}


def test_detect_alerts_includes_comment_surge_and_custom() -> None:
    """评论突增与自定义关键词命中应一并返回。"""
    monitor = _monitor()
    _run(monitor._check_comment_surge("BV1", 10))
    monitor.add_custom_keywords(["翻车"])
    processed = [{"rpid": 1, "content": "翻车了"}]

    alerts = _run(monitor._detect_alerts("BV1", [{"rpid": 1}] * 30, processed, None))

    types = {item["type"] for item in alerts}
    assert "comment_surge" in types
    assert "custom_keywords" in types


def test_detect_alerts_requires_sentiment_distribution_key() -> None:
    """【缺陷固化】sentiment_result 缺 sentiment_distribution 键时抛 KeyError。

    风险词规则用下标取值而非 .get，情感结果结构不完整会直接中断整轮预警检测。
    此处仅固化当前现状，未修改生产代码。
    """
    monitor = _monitor()

    with pytest.raises(KeyError):
        _run(monitor._detect_alerts("BV1", [{}], [{}], {"negative_ratio": 0.0}))


# --------------------------------------------------------------------- 预警入库

@pytest.fixture()
def db_env(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """构造真实临时库并接管模块级 get_session。"""
    manager = DatabaseManager(str(tmp_path / "alerts.db"))
    monkeypatch.setattr(alerts_module, "get_session", manager.get_session)
    return manager


def _alert(**overrides) -> dict:
    """构造一条预警字典。"""
    base = {"type": "negative_surge", "level": "high", "message": "消息", "data": {"x": 1}}
    base.update(overrides)
    return base


def _count(manager: DatabaseManager, model) -> int:
    """统计临时库中的记录数。"""
    session = manager.get_session()
    try:
        return session.query(model).count()
    finally:
        session.close()


def test_save_monitoring_record_creates_video_and_alerts(db_env) -> None:
    """视频不存在时应创建占位记录并逐条写入预警。"""
    monitor = _monitor()

    _run(monitor._save_monitoring_record("BV1", [{"rpid": 1}], [_alert(), _alert(type="risk_keywords")]))

    assert _count(db_env, Video) == 1
    assert _count(db_env, CommentAlert) == 2
    session = db_env.get_session()
    try:
        row = session.query(CommentAlert).filter_by(alert_type="risk_keywords").one()
        assert row.alert_level == "high"
        assert row.message == "消息"
        assert row.details == {"x": 1}
        assert row.is_read is False
    finally:
        session.close()


def test_save_monitoring_record_reuses_existing_video(db_env) -> None:
    """视频已存在时不应重复创建。"""
    monitor = _monitor()
    _run(monitor._save_monitoring_record("BV1", [], []))
    _run(monitor._save_monitoring_record("BV1", [], [_alert()]))

    assert _count(db_env, Video) == 1


def test_save_monitoring_record_invokes_callback_with_payload(db_env) -> None:
    """回调应收到含 bvid/video_id/type/level 的完整载荷。"""
    received: list = []

    async def _callback(payload):
        """记录回调载荷。"""
        received.append(payload)

    monitor = _monitor(callback=_callback)

    _run(monitor._save_monitoring_record("BV1", [], [_alert()]))

    assert len(received) == 1
    payload = received[0]
    assert payload["bvid"] == "BV1"
    assert payload["type"] == "negative_surge"
    assert payload["level"] == "high"
    assert payload["details"] == {"x": 1}
    assert payload["created_at"]


def test_save_monitoring_record_survives_callback_failure(db_env) -> None:
    """回调抛异常只记日志，不影响预警入库主流程。"""
    async def _callback(payload):
        """模拟推送失败。"""
        raise RuntimeError("push failed")

    monitor = _monitor(callback=_callback)

    _run(monitor._save_monitoring_record("BV1", [], [_alert()]))

    assert _count(db_env, CommentAlert) == 1


def test_save_monitoring_record_rolls_back_on_commit_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """提交失败应回滚并关闭会话，不向上抛异常。"""
    session = _CommitFailSession()
    monkeypatch.setattr(alerts_module, "get_session", lambda: session)
    monitor = _monitor()

    _run(monitor._save_monitoring_record("BV1", [], [_alert()]))

    assert session.rolled_back is True
    assert session.closed is True


def test_save_monitoring_record_get_session_error_raises_unbound_local(monkeypatch: pytest.MonkeyPatch) -> None:
    """【缺陷固化】get_session 抛异常时 session 未绑定，抛 UnboundLocalError。

    期望应降级为仅记日志，但本方法 except/finally 均直接使用 session 且无 None 保护。
    此处仅固化当前现状，未修改生产代码。
    """
    def _boom():
        """模拟取会话失败。"""
        raise RuntimeError("db down")

    monkeypatch.setattr(alerts_module, "get_session", _boom)

    with pytest.raises(UnboundLocalError):
        _run(_monitor()._save_monitoring_record("BV1", [], [_alert()]))


def test_save_monitoring_record_empty_alerts_still_commits(db_env) -> None:
    """无预警时仍创建视频记录且不报错。"""
    monitor = _monitor()

    _run(monitor._save_monitoring_record("BV2", [], []))

    assert _count(db_env, Video) == 1
    assert _count(db_env, CommentAlert) == 0


# --------------------------------------------------------------------- 预警查询

def test_get_alerts_returns_latest_unread(db_env) -> None:
    """默认查询未读预警，按时间倒序返回。"""
    monitor = _monitor()
    _run(monitor._save_monitoring_record("BV1", [], [_alert(type="a"), _alert(type="b")]))

    rows = _run(monitor.get_alerts())

    assert len(rows) == 2
    assert {row["type"] for row in rows} == {"a", "b"}
    assert all(row["is_read"] is False for row in rows)
    assert rows[0]["created_at"]


def test_get_alerts_filters_by_level(db_env) -> None:
    """按级别过滤应只返回匹配记录。"""
    monitor = _monitor()
    _run(monitor._save_monitoring_record("BV1", [], [_alert(level="high"), _alert(level="low")]))

    rows = _run(monitor.get_alerts(level="low"))

    assert [row["level"] for row in rows] == ["low"]


def test_get_alerts_is_read_none_returns_all(db_env) -> None:
    """is_read=None 时不过滤已读状态。"""
    monitor = _monitor()
    _run(monitor._save_monitoring_record("BV1", [], [_alert()]))

    rows = _run(monitor.get_alerts(is_read=None))

    assert len(rows) == 1


def test_get_alerts_bvid_filter_branch_runs(db_env) -> None:
    """按 bvid 过滤分支可执行（video_id 为整型，字符串过滤不匹配）。"""
    monitor = _monitor()
    _run(monitor._save_monitoring_record("BV1", [], [_alert()]))

    rows = _run(monitor.get_alerts(bvid="BV1"))

    assert rows == []


def test_get_alerts_respects_limit(db_env) -> None:
    """limit 应限制返回条数。"""
    monitor = _monitor()
    _run(monitor._save_monitoring_record("BV1", [], [_alert(type=f"t{i}") for i in range(5)]))

    rows = _run(monitor.get_alerts(limit=2))

    assert len(rows) == 2


def test_get_alerts_get_session_error_raises_unbound_local(monkeypatch: pytest.MonkeyPatch) -> None:
    """【缺陷固化】get_session 抛异常时 finally 引用未绑定 session，抛 UnboundLocalError。"""
    def _boom():
        """模拟取会话失败。"""
        raise RuntimeError("db down")

    monkeypatch.setattr(alerts_module, "get_session", _boom)

    with pytest.raises(UnboundLocalError):
        _run(_monitor().get_alerts())


# --------------------------------------------------------------------- 已读标记

def test_mark_alert_read_success(db_env) -> None:
    """存在记录时应标记已读并返回 True。"""
    monitor = _monitor()
    _run(monitor._save_monitoring_record("BV1", [], [_alert()]))
    alert_id = _run(monitor.get_alerts())[0]["id"]

    assert _run(monitor.mark_alert_read(alert_id)) is True
    assert _run(monitor.get_alerts(is_read=True))[0]["id"] == alert_id
    assert _run(monitor.get_alerts(is_read=False)) == []


def test_mark_alert_read_missing_returns_false(db_env) -> None:
    """记录不存在时返回 False。"""
    assert _run(_monitor().mark_alert_read(99999)) is False


def test_mark_alert_read_get_session_error_raises_unbound_local(monkeypatch: pytest.MonkeyPatch) -> None:
    """【缺陷固化】get_session 抛异常时 finally 引用未绑定 session，抛 UnboundLocalError。"""
    def _boom():
        """模拟取会话失败。"""
        raise RuntimeError("db down")

    monkeypatch.setattr(alerts_module, "get_session", _boom)

    with pytest.raises(UnboundLocalError):
        _run(_monitor().mark_alert_read(1))
