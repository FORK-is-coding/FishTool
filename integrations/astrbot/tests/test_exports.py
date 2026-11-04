"""视图导出与自诊 PDF 共用的临时文件存储。"""
from __future__ import annotations

import logging
import os
import time
from pathlib import Path

import pytest

from fishtool_adapter.exports import MAX_BYTES, TTL_S, ExportStore, content_type_for, safe_filename

log = logging.getLogger("test")


@pytest.mark.parametrize("ext", [".html", ".txt", ".pdf"])
def test_save_and_resolve_each_type(tmp_path, ext):
    store = ExportStore(tmp_path, log)
    file_id, name = store.save(b"%PDF-1.4" if ext == ".pdf" else b"x", f"report{ext}", ext)
    path, resolved_name = store.resolve(file_id)
    assert path.suffix == ext and resolved_name == f"report{ext}"


def test_unknown_type_rejected(tmp_path):
    with pytest.raises(ValueError, match="不支持"):
        ExportStore(tmp_path, log).save(b"x", "a.exe", ".exe")


def test_too_large_rejected(tmp_path):
    with pytest.raises(ValueError, match="过大"):
        ExportStore(tmp_path, log).save(b"x" * (MAX_BYTES + 1), "a.pdf", ".pdf")


@pytest.mark.parametrize("bad", ["", "../etc/passwd", "short", "a" * 65, "abc/def" * 4])
def test_resolve_rejects_bad_ids(tmp_path, bad):
    assert ExportStore(tmp_path, log).resolve(bad) is None


def test_save_and_path_returns_absolute_resolvable_path(tmp_path):
    store = ExportStore(tmp_path / "exports", log)
    file_id, path = store.save_and_path(b"%PDF", "a.pdf", ".pdf")
    assert path.is_absolute() and path.read_bytes() == b"%PDF"
    assert store.resolve(file_id)[0] == path


def test_expired_files_cleaned_on_save(tmp_path):
    store = ExportStore(tmp_path, log)
    old_id, _ = store.save(b"old", "old.pdf", ".pdf")
    old = tmp_path / f"{old_id}.pdf"
    stale = time.time() - TTL_S - 10
    os.utime(old, (stale, stale))
    store.save(b"new", "new.pdf", ".pdf")
    assert not old.exists()


@pytest.mark.parametrize("suffix,ctype", [
    (".pdf", "application/pdf"),
    (".PDF", "application/pdf"),
    (".html", "text/html; charset=utf-8"),
    (".txt", "text/plain; charset=utf-8"),
    (".bin", "application/octet-stream"),
])
def test_content_type_for(suffix, ctype):
    assert content_type_for(Path(f"x{suffix}")) == ctype


def test_safe_filename():
    assert safe_filename("FishTool自诊_123_2026-11-04.pdf", "x") == "FishTool自诊_123_2026-11-04.pdf"
    assert safe_filename("../../a b.pdf", "x") == "a_b.pdf"
    assert safe_filename("", "default.pdf") == "default.pdf"
