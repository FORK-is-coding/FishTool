"""Own a loopback-only API server without importing either GUI toolkit."""
from __future__ import annotations

import os
import socket
import threading
import time

import httpx


class NativeBackend:
    def __init__(self, port=8000):
        self.port = port
        self.url = f"http://127.0.0.1:{port}"
        self.server = None
        self.thread = None
        self.sock = None

    def start(self, timeout=40):
        import uvicorn
        if not 1 <= self.port <= 65535:
            raise ValueError("端口必须在 1 到 65535 之间")
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            self.sock.bind(("127.0.0.1", self.port))
            self.sock.listen(128)
            os.environ["FISHTOOL_LISTEN_HOST"] = "127.0.0.1"
            from app import init_project
            if not init_project():
                raise RuntimeError("配置或数据库初始化失败，请查看 logs")
            self.server = uvicorn.Server(uvicorn.Config(
                "web.main:app", host="127.0.0.1", port=self.port,
                log_config=None, access_log=False,
            ))
            self.thread = threading.Thread(
                target=self.server.run, kwargs={"sockets": [self.sock]}, daemon=True,
                name="FishTool-native-api",
            )
            self.thread.start()
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if not self.thread.is_alive():
                    raise RuntimeError("本机服务启动失败，请查看 logs")
                if self.server.started:
                    return
                time.sleep(0.1)
            raise TimeoutError("本机服务启动超时")
        except BaseException:
            self.stop()
            raise

    def stop(self):
        if self.server:
            self.server.should_exit = True
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=15)
            if self.thread.is_alive():
                self.server.force_exit = True
                self.thread.join(timeout=3)
        if self.sock:
            self.sock.close()
            self.sock = None

    def request(self, method, path, payload=None):
        with httpx.Client(trust_env=False, timeout=120) as session:
            response = session.request(method, self.url + path, json=payload)
        response.raise_for_status()
        return response.json()
