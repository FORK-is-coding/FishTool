"""Windows entry isolation and real loopback server ownership."""
import socket
import subprocess
import sys

import pytest

import app
from desktop.native_backend import NativeBackend


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_native_entry_passes_port(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    import types
    calls = []
    module = types.ModuleType("desktop.native_window")
    module.run_native = lambda port: calls.append(port) or 0
    monkeypatch.setitem(sys.modules, "desktop.native_window", module)
    assert app.main(["native", "--port", "9001"]) == 0
    assert calls == [9001]


def test_native_entry_windows_only(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    with pytest.raises(SystemExit):
        app.build_parser().parse_args(["native"])


def test_import_backend_does_not_load_qt():
    subprocess.run([sys.executable, "-c", "import desktop.native_backend; import sys; assert not any(n.startswith('PySide6') for n in sys.modules)"], check=True)


def test_occupied_port_is_rejected():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen()
        backend = NativeBackend(sock.getsockname()[1])
        with pytest.raises(OSError):
            backend.start()
        assert backend.sock is None


def test_server_stops_and_releases_port(monkeypatch):
    import uvicorn
    from fastapi import FastAPI
    api = FastAPI()
    @api.get("/health")
    def health():
        return {"ok": True}
    config = uvicorn.Config
    monkeypatch.setattr(uvicorn, "Config", lambda target, **kwargs: config(api, **kwargs))
    monkeypatch.setattr(app, "init_project", lambda: True)
    backend = NativeBackend(free_port())
    try:
        backend.start()
        assert backend.request("GET", "/health") == {"ok": True}
    finally:
        backend.stop()
    assert not backend.thread.is_alive()
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", backend.port))
