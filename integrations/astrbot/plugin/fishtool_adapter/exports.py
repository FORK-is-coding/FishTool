"""视图下载用的临时导出文件。

iframe 沙箱内 print() 被拦、Blob 下载不可靠，所以前端把要导出的内容 POST 给插件，插件写到
``<data_root>/exports/<id>.<ext>``，再由 ``bridge.download("_export/file", {id})`` 经父页面下载。
文件 24 小时后清理，单个文件上限 32 MB。
"""
from __future__ import annotations

import re
import secrets
import time
from pathlib import Path

MAX_BYTES = 32 * 1024 * 1024
TTL_S = 24 * 3600
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
_NAME_SAFE = re.compile(r"[^\w.\-()一-鿿]+")


def safe_filename(name: str, default: str) -> str:
    name = _NAME_SAFE.sub("_", (name or "").strip())[:120].strip("._")
    return name or default


class ExportStore:
    def __init__(self, root: Path, logger):
        self.root = root
        self.log = logger

    def _cleanup(self) -> None:
        now = time.time()
        for path in self.root.glob("*"):
            try:
                if path.is_file() and now - path.stat().st_mtime > TTL_S:
                    path.unlink()
            except OSError as exc:
                self.log.warning(f"[fishtool] cleanup export {path.name} failed: {exc}")

    def save(self, data: bytes, filename: str, ext: str) -> tuple[str, str]:
        """保存导出内容。

        Returns:
            (file_id, filename)

        Raises:
            ValueError: 内容超过上限。
        """
        if len(data) > MAX_BYTES:
            raise ValueError(f"导出内容过大（{len(data) // 1024 // 1024} MB）")
        self.root.mkdir(parents=True, exist_ok=True)
        self._cleanup()
        file_id = secrets.token_urlsafe(18)
        (self.root / f"{file_id}{ext}").write_bytes(data)
        (self.root / f"{file_id}.name").write_text(filename, "utf-8")
        return file_id, filename

    def resolve(self, file_id: str) -> tuple[Path, str] | None:
        """按 id 找到导出文件与下载文件名；不存在返回 None。"""
        if not _ID_RE.fullmatch(file_id or ""):
            return None
        for ext in (".html", ".txt"):
            path = self.root / f"{file_id}{ext}"
            if path.is_file():
                name_file = self.root / f"{file_id}.name"
                name = name_file.read_text("utf-8") if name_file.is_file() else path.name
                return path, name
        return None
