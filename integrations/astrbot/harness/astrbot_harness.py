"""本机联调：起 AstrBot（pip 版）→ 装插件 → 登录 → 等引擎就绪 → API 冒烟。

用法::

    python integrations/astrbot/harness/astrbot_harness.py \
        --astrbot-python <装了 astrbot==4.29.4 的 python> \
        --root <AstrBot 根目录（临时）> --plugin dist/plugin [--port 6185] [--keep-running]

- AstrBot 根目录不存在时自动 ``astrbot init``，用 ``ASTRBOT_DASHBOARD_INITIAL_PASSWORD`` 设定面板密码。
- 插件以“复制到 data/plugins/astrbot_plugin_fishtool”方式安装（与仓库安装落地目录一致）。
- 冒烟结果写到 ``<root>/harness_report.json``；失败返回非零。
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import httpx

logger = logging.getLogger("fishtool.harness")
PLUGIN = "astrbot_plugin_fishtool"
USERNAME = "astrbot"
PASSWORD = "FishTool-Harness-2026"


class Harness:
    def __init__(self, python: str, root: Path, port: int):
        self.python = python
        self.root = root.resolve()
        self.port = port
        self.base = f"http://127.0.0.1:{port}"
        self.proc: subprocess.Popen | None = None
        self.token = ""
        self.http = httpx.Client(timeout=60, trust_env=False)

    # ------------------------------------------------------------- process
    def _env(self) -> dict:
        env = {**os.environ, "ASTRBOT_ROOT": str(self.root), "ASTRBOT_DASHBOARD_INITIAL_PASSWORD": PASSWORD,
               "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
        env.pop("PYTHONPATH", None)
        return env

    def init(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        if not (self.root / ".astrbot").exists():
            subprocess.run([self.python, "-m", "astrbot.cli", "init", "-y"], cwd=self.root, env=self._env(), check=True)

    def install_plugin(self, plugin_tree: Path) -> Path:
        dst = self.root / "data" / "plugins" / PLUGIN
        if dst.exists():
            shutil.rmtree(dst)
        shutil.copytree(plugin_tree, dst)
        return dst

    def start(self) -> None:
        log = open(self.root / "astrbot_stdout.log", "ab")  # noqa: SIM115
        self.proc = subprocess.Popen(
            [self.python, "-m", "astrbot.cli", "run", "-p", str(self.port)],
            cwd=self.root, env=self._env(), stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + 240
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(f"AstrBot exited: {self.proc.returncode}")
            try:
                if self.http.get(f"{self.base}/api/v1/auth/setup-status").status_code < 500:
                    return
            except httpx.HTTPError:
                time.sleep(1)
        raise RuntimeError("AstrBot dashboard not ready")

    def stop(self) -> None:
        """强杀 AstrBot 进程树（模拟崩溃），用来验证引擎随 stdin 断开自动退出。

        Windows 上 venv 的 python.exe 是启动器 stub，真实解释器是它的子进程；
        只杀 stub 会留下真正的 AstrBot，所以按进程树逐个结束。
        """
        import psutil

        if not self.proc or self.proc.poll() is not None:
            return
        try:
            root = psutil.Process(self.proc.pid)
            tree = [root, *root.children(recursive=True)]
        except psutil.Error as exc:
            logger.warning("read AstrBot process tree failed: %r", exc)
            tree = []
        # 只杀 AstrBot 本身，不杀引擎：引擎应在父进程消失后自行退出。
        targets = [p for p in tree if not any("launcher.py" in part for part in _safe_cmdline(p))]
        for proc in targets:
            try:
                proc.kill()
            except psutil.Error as exc:
                logger.debug("kill %s: %r", proc.pid, exc)
        psutil.wait_procs(targets, timeout=30)
        self.proc.wait(timeout=60)

    # ------------------------------------------------------------- api
    def login(self) -> str:
        resp = self.http.post(f"{self.base}/api/v1/auth/login", json={"username": USERNAME, "password": PASSWORD})
        data = resp.json()
        token = (data.get("data") or {}).get("token")
        if not token:
            raise RuntimeError(f"login failed: {data}")
        self.token = token
        return token

    def h(self) -> dict:
        return {"Authorization": f"Bearer {self.token}"}

    def ext(self, method: str, endpoint: str, **kw) -> httpx.Response:
        return self.http.request(method, f"{self.base}/api/v1/plugins/extensions/{PLUGIN}/{endpoint}", headers=self.h(), **kw)

    def reload_plugin(self) -> dict:
        return self.http.post(f"{self.base}/api/v1/plugins/reload", headers=self.h(), json={"plugin_id": PLUGIN}, timeout=180).json()

    def plugin_info(self) -> dict:
        return self.http.get(f"{self.base}/api/v1/plugins/{PLUGIN}", headers=self.h()).json()

    def failed(self) -> dict:
        return self.http.get(f"{self.base}/api/v1/plugins/failed", headers=self.h()).json()

    def status(self) -> dict:
        return self.ext("GET", "_status").json()

    def wait_engine(self, timeout: float = 1800) -> dict:
        deadline = time.monotonic() + timeout
        last = {}
        while time.monotonic() < deadline:
            try:
                last = self.status()
            except (httpx.HTTPError, ValueError) as exc:
                last = {"error": repr(exc)}
            if last.get("state") == "running":
                return last
            if last.get("state") == "error":
                raise RuntimeError(f"engine error: {last.get('last_error')}")
            time.sleep(3)
        raise RuntimeError(f"engine not running in {timeout}s: {last}")

    def proxy(self, path: str, method: str = "GET", query: dict | None = None, body=None) -> dict:
        if method == "GET":
            resp = self.ext("GET", f"api/{path}", params=query or {})
        else:
            resp = self.ext("POST", f"api/{path}", json={"__fishtool_tunnel": 1, "method": method, "query": query or {}, "body": body})
        assert resp.status_code == 200, f"{path}: HTTP {resp.status_code} {resp.text[:300]}"
        env = resp.json()
        assert env.get("status") == "ok", env
        return env["data"]


def smoke(h: Harness) -> list[dict]:
    """API 冒烟：返回每项结果。"""
    results = []

    def check(name, fn):
        try:
            detail = fn()
            results.append({"name": name, "ok": True, "detail": detail})
        except Exception as exc:  # noqa: BLE001 - 收集所有失败项
            logger.warning("smoke %s failed: %r", name, exc)
            results.append({"name": name, "ok": False, "detail": repr(exc)})

    def health():
        d = h.proxy("comment/resident/status")
        assert d["http_status"] == 200, d
        return d["body"]

    def auth_status():
        d = h.proxy("auth/status")
        assert d["http_status"] == 200, d
        return d["body"]

    def not_found():
        d = h.proxy("definitely/not/here")
        assert d["http_status"] == 404, d
        return d["http_status"]

    def validation():
        d = h.proxy("analysis/self-diagnosis", method="POST", body={})
        assert d["http_status"] == 422, d
        return d["http_status"]

    def token_placeholder():
        d = h.proxy("comment/semantic/local-token")
        assert d["body"]["data"]["token"] == "proxy-managed", d
        return "placeholder"

    def logs_redirect():
        d = h.proxy("logs", query={"limit": "5"})
        assert d["http_status"] == 200, d
        return list(d["body"].keys())[:5] if isinstance(d["body"], dict) else type(d["body"]).__name__

    def config_read():
        d = h.proxy("config/llm")
        assert d["http_status"] == 200, d
        return "ok"

    def tunnel_get():
        # POST 隧道还原 GET + query（bvid 必填，缺失会 422）
        resp = h.ext("POST", "api/comment/semantic/preferences",
                     json={"__fishtool_tunnel": 1, "method": "GET", "query": {"bvid": "BV1xx411c7mD"}, "body": None})
        d = resp.json()["data"]
        assert d["http_status"] == 200, d
        return d["http_status"]

    def view_entry():
        resp = h.http.get(f"{h.base}/api/v1/plugins/{PLUGIN}/views/console", headers=h.h())
        data = resp.json()
        path = (data.get("data") or {}).get("content_path")
        assert path, data
        page = h.http.get(f"{h.base}{path}")
        assert page.status_code == 200 and "bridge-shim.js" in page.text, page.status_code
        js = h.http.get(f"{h.base}{path.split('?')[0]}static/js/app.core.js")
        assert js.status_code == 200 and "javascript" in js.headers.get("content-type", ""), js.headers.get("content-type")
        return path.split("?")[0]

    def data_layout():
        root = h.root / "data" / "plugin_data" / PLUGIN
        expected = [root / "config" / ".key", root / "config" / "budget.yaml", root / "runtime" / "data" / "bili_ops.db"]
        missing = [str(p) for p in expected if not p.exists()]
        assert not missing, missing
        plugin_dir = h.root / "data" / "plugins" / PLUGIN
        leaked = [str(p.relative_to(plugin_dir)) for p in plugin_dir.rglob("*") if p.suffix in (".db", ".log") or p.name in (".key", ".secrets")]
        assert not leaked, leaked
        return "ok"

    for name, fn in [
        ("resident_status", health), ("auth_status", auth_status), ("not_found_passthrough", not_found),
        ("validation_422", validation), ("local_token_placeholder", token_placeholder),
        ("logs_307_follow", logs_redirect), ("config_read", config_read), ("tunnel_get", tunnel_get),
        ("view_entry", view_entry), ("data_layout", data_layout),
    ]:
        check(name, fn)
    return results


def _safe_cmdline(proc) -> list[str]:
    import psutil

    try:
        return proc.cmdline()
    except psutil.Error as exc:
        logger.debug("cmdline %s: %r", proc.pid, exc)
        return []


def count_engine_processes(root: Path) -> int:
    """统计使用该数据根的引擎数。

    Windows 上 venv 的 python.exe 是启动器 stub，会再起一个真实解释器子进程（同一命令行）；
    父进程也匹配的子进程不重复计数。
    """
    import psutil

    target = os.path.normcase(str(root.resolve() / "data" / "plugin_data" / PLUGIN))
    matched: dict[int, int] = {}
    for proc in psutil.process_iter(["pid", "ppid", "cmdline"]):
        try:
            cmdline = proc.info["cmdline"] or []
        except psutil.Error:
            continue
        if not any(part.endswith("launcher.py") for part in cmdline) or "--data-root" not in cmdline:
            continue
        idx = cmdline.index("--data-root")
        if idx + 1 < len(cmdline) and os.path.normcase(os.path.abspath(cmdline[idx + 1])) == target:
            matched[proc.info["pid"]] = proc.info["ppid"]
    return sum(1 for ppid in matched.values() if ppid not in matched)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--astrbot-python", required=True)
    ap.add_argument("--root", required=True)
    ap.add_argument("--plugin", required=True)
    ap.add_argument("--port", type=int, default=6185)
    ap.add_argument("--reloads", type=int, default=3)
    ap.add_argument("--keep-running", action="store_true")
    args = ap.parse_args(argv)

    h = Harness(args.astrbot_python, Path(args.root), args.port)
    report: dict = {}
    try:
        h.init()
        h.install_plugin(Path(args.plugin))
        h.start()
        h.login()
        report["failed_plugins"] = h.failed()
        report["engine_boot"] = h.wait_engine()
        report["smoke"] = smoke(h)

        reloads = []
        for i in range(args.reloads):
            h.reload_plugin()
            if i == 0:
                h.reload_plugin()  # 引擎尚未就绪时连续两次重载
            st = h.wait_engine(timeout=300)
            time.sleep(2)
            reloads.append({"i": i, "pid": st.get("pid"), "engines": count_engine_processes(h.root)})
        report["reloads"] = reloads
        report["ok"] = all(r["ok"] for r in report["smoke"]) and all(r["engines"] == 1 for r in reloads)
        if args.keep_running:
            report["dashboard"] = h.base
            report["token"] = h.token
    except Exception as exc:  # noqa: BLE001 - 汇总到报告
        logger.error("harness failed: %r", exc)
        report["ok"] = False
        report["error"] = repr(exc)
    finally:
        (h.root / "harness_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), "utf-8")
        print(json.dumps(report, ensure_ascii=False, indent=2)[:6000])
        if not args.keep_running:
            h.stop()
            left = -1
            for _ in range(20):  # 引擎优雅关闭（lifespan）通常 1-3 秒
                time.sleep(1)
                left = count_engine_processes(h.root)
                if left == 0:
                    break
            print(f"engine processes left after AstrBot kill: {left}")
            if left != 0:
                report["ok"] = False
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
