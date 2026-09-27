"""评论预警 Mixin 契约测试（modules/comment/_alerts.py）。

覆盖 MonitorAlertsMixin 的四条预警规则、入库与查询：
- _detect_alerts：负面占比 / 评论突增 / 风险词 / 自定义词，四条规则可同时命中
- _check_comment_surge：10 次滑动窗口均值突增判定
- _check_custom_keywords：自定义关键词扫描与样本截断
- _save_monitoring_record / get_alerts / mark_alert_read：SQLite 落库、多条件查询、已读标记

测试策略：
- 监控器使用真实 CommentMonitor，DB 一律 tmp_path 下真实 SQLite（绝不触碰 data/*.db）；
- 假客户端只提供契约级对象，不使用 AsyncMock；
- 长协程用 asyncio.wait_for 兜底，保证不挂死。
"""
import asyncio
from collections import deque
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from core.database import CommentAlert, DatabaseManager, Video
from modules.comment import _alerts as alerts_module
from modules.comment.monitor import CommentMonitor

BVID = "BV1contract"


def run(coro):
    """同步测试内执行单次协程，并加 5 秒超时双保险。"""
    return asyncio.run(asyncio.wait_for(coro, timeout=5))


@pytest.fixture()
def manager(tmp_path, monkeypatch):
    """把 _alerts 的模块级 get_session 指向 tmp_path 下的真实 SQLite。"""
    db = DatabaseManager(str(tmp_path / "comment_alerts.db"))
    monkeypatch.setattr(alerts_module, "get_session", db.get_session)
    return db


def make_monitor(alert_callback=None) -> CommentMonitor:
    """构造真实 CommentMonitor，仅注入契约级假 API。"""
    return CommentMonitor(api=SimpleNamespace(), alert_callback=alert_callback)


def seed_video(manager, bvid: str, title: str = "真实标题") -> int:
    """向临时库写入一条视频记录，返回其主键。"""
    session = manager.get_session()
    try:
        video = Video(bvid=bvid, title=title)
        session.add(video)
        session.commit()
        return video.id
    finally:
        session.close()


def query_alerts(manager) -> list:
    """读取临时库中全部预警行（按 id 升序）。"""
    session = manager.get_session()
    try:
        return session.query(CommentAlert).order_by(CommentAlert.id).all()
    finally:
        session.close()


# ---------------------------------------------------------------------------
# _detect_alerts
# ---------------------------------------------------------------------------


def test_detect_alerts_flags_medium_negative_ratio():
    """负面占比超阈值但未过半时应给出 medium 级 negative_surge。"""
    monitor = make_monitor()
    sentiment = {"negative_ratio": 0.4, "sentiment_distribution": {"negative": 4}}

    alerts = run(monitor._detect_alerts(BVID, [{"rpid": 1}], [{"rpid": 1}], sentiment))

    assert len(alerts) == 1
    assert alerts[0]["type"] == "negative_surge"
    assert alerts[0]["level"] == "medium"
    assert "40.0%" in alerts[0]["message"]
    assert alerts[0]["data"] == {"negative_ratio": 0.4, "threshold": 0.3}


def test_detect_alerts_upgrades_negative_ratio_above_half():
    """负面占比超过 50% 时应升级为 high。"""
    monitor = make_monitor()
    sentiment = {"negative_ratio": 0.51, "sentiment_distribution": {"negative": 6}}

    alerts = run(monitor._detect_alerts(BVID, [], [], sentiment))

    assert [alert["type"] for alert in alerts] == ["negative_surge"]
    assert alerts[0]["level"] == "high"


def test_detect_alerts_negative_ratio_boundary_is_exclusive():
    """占比恰好等于 30% 阈值时不应触发（严格大于）。"""
    monitor = make_monitor()
    sentiment = {"negative_ratio": 0.3, "sentiment_distribution": {"negative": 3}}

    alerts = run(monitor._detect_alerts(BVID, [], [], sentiment))

    assert alerts == []


def test_detect_alerts_flags_risk_count_at_threshold_but_not_below():
    """风险评论数达到 5 条触发预警，4 条不触发。"""
    monitor = make_monitor()

    hit = run(monitor._detect_alerts(BVID, [], [], {"negative_ratio": 0, "sentiment_distribution": {"risk": 5}}))
    miss = run(monitor._detect_alerts("BV2", [], [], {"negative_ratio": 0, "sentiment_distribution": {"risk": 4}}))

    assert [alert["type"] for alert in hit] == ["risk_keywords"]
    assert hit[0]["data"] == {"risk_count": 5, "threshold": 5}
    assert miss == []


def test_detect_alerts_includes_custom_keywords():
    """自定义关键词命中应产出 custom_keywords 预警。"""
    monitor = make_monitor()
    monitor.add_custom_keywords(["抄袭"])

    alerts = run(monitor._detect_alerts(BVID, [], [{"rpid": 7, "content": "这明显是抄袭"}], None))

    assert [alert["type"] for alert in alerts] == ["custom_keywords"]
    assert alerts[0]["data"]["matched_count"] == 1
    assert alerts[0]["data"]["samples"][0]["keywords"] == ["抄袭"]


def test_detect_alerts_returns_empty_without_any_signal():
    """无情感结果、无自定义词、无历史基线时不应误报。"""
    monitor = make_monitor()

    alerts = run(monitor._detect_alerts(BVID, [{"rpid": 1}], [{"rpid": 1}], None))

    assert alerts == []
    # 首次监控会写入基线，供下一次突增比对。
    assert len(monitor._monitoring_history[BVID]) == 1


def test_detect_alerts_can_fire_all_four_rules_in_one_pass():
    """四条规则互相独立，同一轮监控可同时命中。"""
    monitor = make_monitor()
    monitor.add_custom_keywords(["崩"])
    monitor._monitoring_history[BVID] = deque([{"count": 1, "time": datetime.now()}], maxlen=10)
    comments = [{"rpid": index, "content": "崩了"} for index in range(10)]
    sentiment = {"negative_ratio": 0.6, "sentiment_distribution": {"risk": 6}}

    alerts = run(monitor._detect_alerts(BVID, comments, comments, sentiment))

    assert {alert["type"] for alert in alerts} == {
        "negative_surge",
        "comment_surge",
        "risk_keywords",
        "custom_keywords",
    }


# ---------------------------------------------------------------------------
# _check_comment_surge
# ---------------------------------------------------------------------------


def test_check_comment_surge_seeds_baseline_on_first_run():
    """首次监控没有历史可比，只记录基准并返回 None。"""
    monitor = make_monitor()

    result = run(monitor._check_comment_surge(BVID, 12))

    assert result is None
    history = monitor._monitoring_history[BVID]
    assert len(history) == 1
    assert history[0]["count"] == 12
    assert isinstance(history[0]["time"], datetime)


def test_check_comment_surge_reports_ratio_above_double_average():
    """当前数超过历史均值两倍时报突增，并给出真实倍数。"""
    monitor = make_monitor()
    monitor._monitoring_history[BVID] = deque([{"count": 10, "time": datetime.now()}], maxlen=10)

    alert = run(monitor._check_comment_surge(BVID, 30))

    assert alert["type"] == "comment_surge"
    assert alert["level"] == "medium"
    assert alert["data"]["avg_count"] == 10.0
    assert alert["data"]["surge_ratio"] == 3.0
    assert "突增" in alert["message"]


def test_check_comment_surge_boundary_exactly_double_is_silent():
    """恰好等于均值两倍不触发（判定为严格大于）。"""
    monitor = make_monitor()
    monitor._monitoring_history[BVID] = deque([{"count": 10, "time": datetime.now()}], maxlen=10)

    assert run(monitor._check_comment_surge(BVID, 20)) is None


def test_check_comment_surge_zero_average_yields_zero_ratio():
    """历史均值为 0 时仍可判定突增，但倍数回落为 0 以避免除零。"""
    monitor = make_monitor()
    monitor._monitoring_history[BVID] = deque([{"count": 0, "time": datetime.now()}], maxlen=10)

    alert = run(monitor._check_comment_surge(BVID, 5))

    assert alert["data"]["avg_count"] == 0.0
    assert alert["data"]["surge_ratio"] == 0


def test_check_comment_surge_window_keeps_only_ten_samples():
    """滑动窗口固定 10 条，第 11 次采集仍只保留最近 10 条。"""
    monitor = make_monitor()
    counts = list(range(1, 12))

    for count in counts:
        run(monitor._check_comment_surge(BVID, count))

    history = monitor._monitoring_history[BVID]
    assert len(history) == 10
    assert [item["count"] for item in history] == counts[-10:]


# ---------------------------------------------------------------------------
# _check_custom_keywords
# ---------------------------------------------------------------------------


def test_check_custom_keywords_returns_empty_without_keywords():
    """未配置自定义关键词时直接返回空列表，不扫描评论。"""
    monitor = make_monitor()

    assert monitor._check_custom_keywords([{"content": "任意"}]) == []


def test_check_custom_keywords_builds_alert_and_samples():
    """命中评论应生成预警，并保留 rpid/内容/关键词样本。"""
    monitor = make_monitor()
    monitor.add_custom_keywords(["维权", "举报"])

    alerts = monitor._check_custom_keywords([{"rpid": 3, "content": "我要举报并维权"}])

    assert len(alerts) == 1
    assert alerts[0]["type"] == "custom_keywords"
    assert alerts[0]["level"] == "medium"
    assert alerts[0]["data"]["matched_count"] == 1
    assert alerts[0]["data"]["samples"][0]["rpid"] == 3
    assert sorted(alerts[0]["data"]["samples"][0]["keywords"]) == ["举报", "维权"]


def test_check_custom_keywords_caps_samples_at_five():
    """命中超过 5 条时样本截断为 5 条，但计数保持真实。"""
    monitor = make_monitor()
    monitor.add_custom_keywords(["踩"])
    comments = [{"rpid": index, "content": "踩一脚"} for index in range(8)]

    alerts = monitor._check_custom_keywords(comments)

    assert alerts[0]["data"]["matched_count"] == 8
    assert len(alerts[0]["data"]["samples"]) == 5


def test_check_custom_keywords_truncates_long_content_to_100_chars():
    """样本内容截断到 100 字，避免预警负载过大。"""
    monitor = make_monitor()
    monitor.add_custom_keywords(["长"])
    long_content = "长" * 250

    alerts = monitor._check_custom_keywords([{"rpid": 1, "content": long_content}])

    assert len(alerts[0]["data"]["samples"][0]["content"]) == 100


def test_check_custom_keywords_returns_empty_when_nothing_matches():
    """未命中任何关键词时不产生预警。"""
    monitor = make_monitor()
    monitor.add_custom_keywords(["apple"])

    assert monitor._check_custom_keywords([{"rpid": 1, "content": "Apple 是大写"}]) == []


# ---------------------------------------------------------------------------
# _save_monitoring_record
# ---------------------------------------------------------------------------


def test_save_monitoring_record_creates_video_and_alerts(manager):
    """视频不存在时自动建占位记录，并把预警写入 comment_alerts 表。"""
    monitor = make_monitor()
    alerts = [{"type": "negative_surge", "level": "high", "message": "msg", "data": {"ratio": 0.6}}]

    run(monitor._save_monitoring_record(BVID, [], alerts))

    rows = query_alerts(manager)
    assert len(rows) == 1
    assert rows[0].alert_type == "negative_surge"
    assert rows[0].alert_level == "high"
    assert rows[0].details == {"ratio": 0.6}
    assert rows[0].is_read is False

    session = manager.get_session()
    try:
        video = session.query(Video).filter_by(bvid=BVID).first()
        assert video is not None
        assert video.title == f"视频_{BVID}"
        assert rows[0].video_id == video.id
    finally:
        session.close()


def test_save_monitoring_record_reuses_existing_video(manager):
    """视频已存在时直接复用其主键，不新建占位记录。"""
    video_id = seed_video(manager, BVID, title="已有标题")
    monitor = make_monitor()

    run(monitor._save_monitoring_record(BVID, [], [{"type": "risk_keywords", "level": "high", "message": "m"}]))

    rows = query_alerts(manager)
    assert rows[0].video_id == video_id
    session = manager.get_session()
    try:
        assert session.query(Video).count() == 1
    finally:
        session.close()


def test_save_monitoring_record_pushes_callback_payload(manager):
    """预警回调应收到含 bvid/video_id 的完整负载。"""
    received = []

    async def callback(payload):
        received.append(payload)

    monitor = make_monitor(alert_callback=callback)

    run(monitor._save_monitoring_record(BVID, [], [{"type": "custom_keywords", "level": "medium", "message": "m", "data": {"n": 1}}]))

    assert len(received) == 1
    assert received[0]["bvid"] == BVID
    assert received[0]["type"] == "custom_keywords"
    assert received[0]["details"] == {"n": 1}
    assert "created_at" in received[0]


def test_save_monitoring_record_swallows_callback_failure(manager):
    """回调抛错只记日志，不影响预警主流程落库。"""
    async def broken_callback(payload):
        raise RuntimeError("回调挂了")

    monitor = make_monitor(alert_callback=broken_callback)

    run(monitor._save_monitoring_record(BVID, [], [{"type": "negative_surge", "level": "high", "message": "m"}]))

    assert len(query_alerts(manager)) == 1


def test_save_monitoring_record_without_alerts_commits_nothing(manager):
    """空预警列表只提交事务，不产生任何预警行。"""
    monitor = make_monitor()

    run(monitor._save_monitoring_record(BVID, [], []))

    assert query_alerts(manager) == []


def test_save_monitoring_record_raises_when_session_unavailable(tmp_path, monkeypatch):
    """固化现状缺陷：会话创建失败时 except 引用了未初始化的 session，抛 NameError。"""
    def broken_session():
        raise RuntimeError("数据库不可用")

    monkeypatch.setattr(alerts_module, "get_session", broken_session)
    monitor = make_monitor()

    with pytest.raises(NameError):
        run(monitor._save_monitoring_record(BVID, [], [{"type": "t", "level": "high", "message": "m"}]))


# ---------------------------------------------------------------------------
# get_alerts
# ---------------------------------------------------------------------------


def insert_alert(manager, video_id: int, alert_type: str, level: str, created_at: datetime, is_read: bool = False):
    """直接写入一条预警，返回其主键（用于构造查询场景）。"""
    session = manager.get_session()
    try:
        alert = CommentAlert(
            video_id=video_id,
            alert_type=alert_type,
            alert_level=level,
            message=f"{alert_type}-{level}",
            details={"k": alert_type},
            is_read=is_read,
            created_at=created_at,
        )
        session.add(alert)
        session.commit()
        return alert.id
    finally:
        session.close()


def test_get_alerts_returns_latest_first_with_serialized_fields(manager):
    """查询结果按创建时间倒序，且字段已序列化为普通字典。"""
    video_id = seed_video(manager, BVID)
    now = datetime(2026, 8, 22, 12, 0)
    insert_alert(manager, video_id, "old", "low", now - timedelta(hours=1))
    insert_alert(manager, video_id, "new", "high", now, is_read=True)
    monitor = make_monitor()

    result = run(monitor.get_alerts(is_read=None))

    assert [item["type"] for item in result] == ["new", "old"]
    assert result[0]["level"] == "high"
    assert result[0]["details"] == {"k": "new"}
    assert result[0]["is_read"] is True
    assert result[0]["created_at"] == now.isoformat()


def test_get_alerts_filters_by_numeric_bvid_and_level(manager):
    """bvid 过滤走 video_id 数值亲和，级别过滤独立生效。"""
    video_a = seed_video(manager, BVID)
    video_b = seed_video(manager, "BV2other")
    insert_alert(manager, video_a, "a", "high", datetime(2026, 8, 22, 12, 0))
    insert_alert(manager, video_b, "b", "low", datetime(2026, 8, 22, 12, 1))
    monitor = make_monitor()

    by_video = run(monitor.get_alerts(bvid=str(video_a), is_read=None))
    by_level = run(monitor.get_alerts(level="low", is_read=None))

    assert [item["type"] for item in by_video] == ["a"]
    assert [item["type"] for item in by_level] == ["b"]


def test_get_alerts_returns_empty_for_non_numeric_bvid(manager):
    """传入真实 BV 号字符串时因 video_id 数值比较而查不到（固化现状）。"""
    video_id = seed_video(manager, BVID)
    insert_alert(manager, video_id, "a", "high", datetime(2026, 8, 22, 12, 0))
    monitor = make_monitor()

    assert run(monitor.get_alerts(bvid=BVID, is_read=None)) == []


def test_get_alerts_filters_unread_and_respects_limit(manager):
    """默认只取未读；limit 截断按时间倒序后的前 N 条。"""
    video_id = seed_video(manager, BVID)
    base = datetime(2026, 8, 22, 12, 0)
    insert_alert(manager, video_id, "read", "low", base, is_read=True)
    insert_alert(manager, video_id, "new1", "high", base + timedelta(minutes=1))
    insert_alert(manager, video_id, "new2", "high", base + timedelta(minutes=2))
    monitor = make_monitor()

    unread = run(monitor.get_alerts())
    limited = run(monitor.get_alerts(is_read=None, limit=1))

    assert [item["type"] for item in unread] == ["new2", "new1"]
    assert [item["type"] for item in limited] == ["new2"]


def test_get_alerts_raises_when_session_unavailable(monkeypatch):
    """固化现状缺陷：会话创建失败时 finally 引用了未初始化的 session，抛 NameError。"""
    monkeypatch.setattr(alerts_module, "get_session", lambda: (_ for _ in ()).throw(RuntimeError("库挂了")))
    monitor = make_monitor()

    with pytest.raises(NameError):
        run(monitor.get_alerts())


# ---------------------------------------------------------------------------
# mark_alert_read
# ---------------------------------------------------------------------------


def test_mark_alert_read_updates_flag_and_returns_true(manager):
    """存在的预警应被置为已读并返回 True。"""
    video_id = seed_video(manager, BVID)
    alert_id = insert_alert(manager, video_id, "a", "high", datetime(2026, 8, 22, 12, 0))
    monitor = make_monitor()

    assert run(monitor.mark_alert_read(alert_id)) is True

    rows = query_alerts(manager)
    assert rows[0].is_read is True


def test_mark_alert_read_returns_false_for_missing_id(manager):
    """预警不存在时返回 False，不抛异常。"""
    monitor = make_monitor()

    assert run(monitor.mark_alert_read(999999)) is False


def test_mark_alert_read_is_idempotent(manager):
    """重复标记已读仍是幂等的成功结果。"""
    video_id = seed_video(manager, BVID)
    alert_id = insert_alert(manager, video_id, "a", "high", datetime(2026, 8, 22, 12, 0), is_read=True)
    monitor = make_monitor()

    assert run(monitor.mark_alert_read(alert_id)) is True


def test_mark_alert_read_raises_when_session_unavailable(monkeypatch):
    """固化现状缺陷：会话创建失败时 except 引用了未初始化的 session，抛 NameError。"""
    monkeypatch.setattr(alerts_module, "get_session", lambda: (_ for _ in ()).throw(RuntimeError("库挂了")))
    monitor = make_monitor()

    with pytest.raises(NameError):
        run(monitor.mark_alert_read(1))
