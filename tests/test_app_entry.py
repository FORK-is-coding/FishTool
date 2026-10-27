"""统一入口 app.py 的调度回归测试。"""
from __future__ import annotations

import app as entry


def test_default_runs_desktop_on_8000(monkeypatch):
    """不带子命令时默认启动桌面版，端口 8000。"""
    calls = []
    monkeypatch.setattr(entry, "run_desktop", lambda port: calls.append(port) or 0)

    assert entry.main([]) == 0
    assert calls == [8000]


def test_web_subcommand_dispatches_options(monkeypatch):
    """app.py web 将 host/port/reload 透传给 Web 启动函数。"""
    calls = []
    monkeypatch.setattr(entry, "init_project", lambda: True)
    monkeypatch.setattr(
        entry,
        "run_web",
        lambda host, port, reload: calls.append((host, port, reload)) or 0,
    )

    assert entry.main(["web", "--host", "127.0.0.1", "--port", "9000", "--reload"]) == 0
    assert calls == [("127.0.0.1", 9000, True)]


def test_web_init_only_does_not_start_server(monkeypatch):
    """app.py web --init-only 只初始化，不进入 uvicorn。"""
    monkeypatch.setattr(entry, "init_project", lambda: True)

    def _must_not_start(*args, **kwargs):
        raise AssertionError("init-only 不应启动 Web 服务")

    monkeypatch.setattr(entry, "run_web", _must_not_start)
    assert entry.main(["web", "--init-only"]) == 0


def test_desktop_port_option(monkeypatch):
    """app.py desktop --port 将端口传给桌面启动函数。"""
    calls = []
    monkeypatch.setattr(entry, "run_desktop", lambda port: calls.append(port) or 0)

    assert entry.main(["desktop", "--port", "9001"]) == 0
    assert calls == [9001]


def test_web_host_defaults_to_loopback():
    """app.py web 不传 --host 时只监听回环地址（issue #5）。"""
    args = entry.build_parser().parse_args(["web"])

    assert args.host == "127.0.0.1"


def test_web_without_host_dispatches_loopback(monkeypatch):
    """app.py web 不传 --host 时，传给 Web 启动函数的也是回环地址。"""
    calls = []
    monkeypatch.setattr(entry, "init_project", lambda: True)
    monkeypatch.setattr(
        entry,
        "run_web",
        lambda host, port, reload: calls.append((host, port, reload)) or 0,
    )

    assert entry.main(["web"]) == 0
    assert calls == [("127.0.0.1", 8000, False)]


def test_web_host_can_opt_in_all_interfaces():
    """局域网访问仍可显式传 --host 0.0.0.0。"""
    args = entry.build_parser().parse_args(["web", "--host", "0.0.0.0"])

    assert args.host == "0.0.0.0"
