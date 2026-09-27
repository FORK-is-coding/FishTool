"""桌面端图标与 WebSocket 逻辑测试。"""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from desktop import icon_utils
from desktop.pet_window import ws_mixin, ws_thread


def test_icon_candidates_are_unique_and_include_project_asset(monkeypatch, tmp_path):
    """图标候选应去重并包含项目资源路径。"""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(icon_utils.sys, "executable", str(tmp_path / "python.exe"))
    monkeypatch.setattr(icon_utils.sys, "_MEIPASS", str(tmp_path), raising=False)

    candidates = icon_utils._icon_candidates()

    assert len(candidates) == len(set(candidates))
    assert (tmp_path / "assets" / "图标.png").resolve() in candidates


def test_icon_path_selects_first_existing_nonempty_file(monkeypatch, tmp_path):
    """图标查找应跳过空文件并选择首个非空候选。"""
    empty = tmp_path / "empty.png"
    valid = tmp_path / "valid.png"
    empty.write_bytes(b"")
    valid.write_bytes(b"png")
    monkeypatch.setattr(icon_utils, "_icon_candidates", lambda: (empty, valid))

    assert icon_utils.icon_path() == valid


def test_icon_path_handles_oserror(monkeypatch):
    """候选文件检查异常时应返回 None。"""
    broken = Mock()
    broken.is_file.side_effect = OSError("disk")
    monkeypatch.setattr(icon_utils, "_icon_candidates", lambda: (broken,))
    assert icon_utils.icon_path() is None


def test_load_app_icon_empty_branch(monkeypatch):
    """无图标文件时应返回空 QIcon，而不是抛异常。"""
    monkeypatch.setattr(icon_utils, "icon_path", lambda: None)
    assert icon_utils.load_app_icon().isNull()


def test_windows_model_id_skips_non_windows(monkeypatch):
    """非 Windows 平台不得调用 ctypes.windll。"""
    monkeypatch.setattr(icon_utils.sys, "platform", "linux")
    windll = Mock()
    monkeypatch.setattr(icon_utils.ctypes, "windll", windll, raising=False)
    icon_utils.set_windows_app_user_model_id()
    windll.shell32.SetCurrentProcessExplicitAppUserModelID.assert_not_called()


def test_websocket_thread_message_error_close_and_stop():
    """WebSocket 线程应分发合法消息、错误、关闭和停止信号。"""
    thread = ws_thread.WebSocketThread("ws://example.test/ws")
    messages = []
    errors = []
    disconnected = []
    thread.message_received.connect(messages.append)
    thread.error.connect(errors.append)
    thread.disconnected.connect(lambda: disconnected.append(True))

    thread.on_message(None, '{"type":"alert","data":{"title":"risk"}}')
    thread.on_message(None, "not-json")
    thread.on_error(None, ValueError("boom"))
    thread.on_close(None, 1000, "done")
    socket = Mock()
    thread.ws = socket
    thread.running = True
    thread.stop()

    assert messages == [{"type": "alert", "data": {"title": "risk"}}]
    assert errors == ["boom"]
    assert disconnected == [True]
    assert thread.running is False
    socket.close.assert_called_once_with()


def test_websocket_thread_run_builds_and_runs_app(monkeypatch):
    """线程主循环应正确注册回调并启动 WebSocketApp。"""
    app = Mock()
    factory = Mock(return_value=app)
    monkeypatch.setattr(ws_thread.websocket, "WebSocketApp", factory)
    thread = ws_thread.WebSocketThread("ws://example.test/ws")

    thread.run()

    assert thread.running is True
    factory.assert_called_once_with(
        "ws://example.test/ws",
        on_open=thread.on_open,
        on_message=thread.on_message,
        on_error=thread.on_error,
        on_close=thread.on_close,
    )
    app.run_forever.assert_called_once_with()


def test_ws_mixin_bridges_alert_and_connected_state(monkeypatch):
    """桌宠桥接器应处理连接状态与 alert 消息。"""
    host = SimpleNamespace(
        STATE_IDLE="idle",
        set_state=Mock(),
        show_notification=Mock(),
    )
    ws_mixin.WsMixin.on_ws_connected(host)
    ws_mixin.WsMixin.on_ws_message(host, {"type": "alert", "data": {"message": "warning"}})
    ws_mixin.WsMixin.on_ws_message(host, {"type": "pong"})

    host.set_state.assert_called_once_with("idle")
    host.show_notification.assert_called_once_with({"message": "warning"})


def test_ws_mixin_setup_connects_signals_and_starts(monkeypatch):
    """WebSocket 初始化应连接四个信号并启动线程。"""
    signal = lambda: SimpleNamespace(connect=Mock())
    fake_thread = SimpleNamespace(
        message_received=signal(),
        connected=signal(),
        disconnected=signal(),
        error=signal(),
        start=Mock(),
    )
    constructor = Mock(return_value=fake_thread)
    monkeypatch.setattr(ws_mixin, "WebSocketThread", constructor)
    host = SimpleNamespace(
        ws_url="ws://localhost/ws",
        on_ws_message=Mock(),
        on_ws_connected=Mock(),
        on_ws_disconnected=Mock(),
        on_ws_error=Mock(),
    )

    ws_mixin.WsMixin.setup_websocket(host)

    constructor.assert_called_once_with(host.ws_url)
    fake_thread.message_received.connect.assert_called_once_with(host.on_ws_message)
    fake_thread.connected.connect.assert_called_once_with(host.on_ws_connected)
    fake_thread.disconnected.connect.assert_called_once_with(host.on_ws_disconnected)
    fake_thread.error.connect.assert_called_once_with(host.on_ws_error)
    fake_thread.start.assert_called_once_with()
