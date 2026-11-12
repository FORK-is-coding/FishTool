"""bilibili/link.py 视频输入识别，以及前端 parseVideoInput 与它的口径一致性。"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from bilibili.link import (
    KIND_BVID,
    KIND_SHORT_LINK,
    NOT_FOUND_MESSAGE,
    SHORT_LINK_MESSAGE,
    VideoInputError,
    VideoRef,
    coerce_bvid,
    find_video_refs,
    parse_video_input,
    require_bvid,
)

_JS_DIR = Path(__file__).resolve().parents[1] / "web" / "frontend" / "static" / "js"

#: (输入, 期望 BV 号或 None, 是否短链)。前后端共用同一张表。
CASES = [
    ("BV1xx411c7mD", "BV1xx411c7mD", False),
    ("  BV1xx411c7mD\n", "BV1xx411c7mD", False),
    ("bv1xx411c7md", "bv1xx411c7md", False),
    ("https://www.bilibili.com/video/BV1xx411c7mD", "BV1xx411c7mD", False),
    ("https://www.bilibili.com/video/BV1xx411c7mD/?p=2&t=30&spm_id_from=333.1007", "BV1xx411c7mD", False),
    ("https://m.bilibili.com/video/BV1xx411c7mD", "BV1xx411c7mD", False),
    ("【标题】 https://www.bilibili.com/video/BV1xx411c7mD 快来看", "BV1xx411c7mD", False),
    ("https://b23.tv/BV1xx411c7mD", "BV1xx411c7mD", False),
    ("https://b23.tv/AbC123x", None, True),
    ("【标题】-哔哩哔哩 https://b23.tv/AbC123x", None, True),
    ("b23.tv/AbC123x", None, True),
    ("https://bili2233.cn/AbC123x", None, True),
    ("https://bili2233.cn/BV1xx411c7mD", "BV1xx411c7mD", False),
    ("（BV1xx411c7mD），快来看！", "BV1xx411c7mD", False),
    ("BV1xx411c7mD BV1aa411c7mD", "BV1xx411c7mD", False),
    ("https://b23.tv/AbC123x 后面是 BV1xx411c7mD", None, True),
    ("BV1xx411c7mD 后面是 https://b23.tv/AbC123x", "BV1xx411c7mD", False),
    ("BV" + "1" * 30, None, False),
    ("xBV1xx411c7mD", None, False),
    ("BV1xx411c7mDx", None, False),
    ("https://www.bilibili.com/video/BV1xx411c7mDx", None, False),
    ("BV1", None, False),
    ("123456", None, False),
    ("", None, False),
]


@pytest.mark.parametrize(("raw", "bvid", "short"), CASES)
def test_parse_video_input_cases(raw: str, bvid, short: bool) -> None:
    """表内每条输入的识别结果。"""
    ref = parse_video_input(raw)
    if bvid:
        assert ref == VideoRef(KIND_BVID, bvid)
    elif short:
        assert ref is not None and ref.kind == KIND_SHORT_LINK
    else:
        assert ref is None


def test_find_video_refs_keeps_order_and_dedupes() -> None:
    """群聊消息里多个链接：按出现顺序返回，重复的只留一个。"""
    text = "先看 BV1aa411c7mD 再看 https://b23.tv/XyZ9 还有 https://www.bilibili.com/video/BV1aa411c7mD"
    assert find_video_refs(text) == [
        VideoRef(KIND_BVID, "BV1aa411c7mD"),
        VideoRef(KIND_SHORT_LINK, "https://b23.tv/XyZ9"),
    ]


def test_find_video_refs_empty_for_none() -> None:
    assert find_video_refs(None) == []


def test_find_video_refs_preserves_distinct_targets_and_dedupes_url_forms() -> None:
    """完整链接、纯 BV 和带 BV 的短链归一去重，不丢失其他目标。"""
    text = ("https://b23.tv/XyZ9 BV1aa411c7mD BV1xx411c7mD "
            "https://www.bilibili.com/video/BV1aa411c7mD?p=2 "
            "https://b23.tv/BV1xx411c7mD https://b23.tv/XyZ9")
    assert find_video_refs(text) == [
        VideoRef(KIND_SHORT_LINK, "https://b23.tv/XyZ9"),
        VideoRef(KIND_BVID, "BV1aa411c7mD"),
        VideoRef(KIND_BVID, "BV1xx411c7mD"),
    ]


@pytest.mark.parametrize("parser", [require_bvid, coerce_bvid])
def test_bvid_conversion_uses_first_reference(parser) -> None:
    """不能跳过首个短链而静默选择后面另一个视频。"""
    with pytest.raises(VideoInputError, match="b23.tv"):
        parser("https://b23.tv/AbC123x 后面是 BV1xx411c7mD")
    assert parser("BV1xx411c7mD 后面是 https://b23.tv/AbC123x") == "BV1xx411c7mD"


def test_require_bvid_messages() -> None:
    """取不到 BV 号时报可读的错误。"""
    assert require_bvid("https://www.bilibili.com/video/BV1xx411c7mD") == "BV1xx411c7mD"
    with pytest.raises(VideoInputError, match=NOT_FOUND_MESSAGE):
        require_bvid("随便写点")
    with pytest.raises(VideoInputError, match="b23.tv"):
        require_bvid("https://b23.tv/AbC123x")


def test_coerce_bvid_passes_unknown_input_through() -> None:
    """接口入参：认不出原样返回交给原有校验，只有短链报错。"""
    assert coerce_bvid("https://www.bilibili.com/video/BV1xx411c7mD?p=2") == "BV1xx411c7mD"
    assert coerce_bvid("   ") == "   "
    assert coerce_bvid("BV1") == "BV1"
    with pytest.raises(VideoInputError, match="b23.tv"):
        coerce_bvid("https://b23.tv/AbC123x")


def test_link_module_only_depends_on_stdlib() -> None:
    """群聊接入时要能拷进 AstrBot 适配层，不能依赖 core / bilibili.api。"""
    source = (Path(__file__).resolve().parents[1] / "bilibili" / "link.py").read_text(encoding="utf-8")
    imports = [line.split()[1] for line in source.splitlines() if line.startswith(("import ", "from "))]
    assert set(imports) <= {"__future__", "re", "dataclasses", "typing"}, imports


_NODE_SCRIPT = r"""
const fs = require('fs');
const vm = require('vm');
const [corePath, casesJson] = process.argv.slice(-2);
const el = () => ({ style: {}, dataset: {}, classList: { add() {}, remove() {}, toggle() {} },
  addEventListener() {}, querySelector: () => null, querySelectorAll: () => [], setAttribute() {} });
const ctx = {
  console: { error() {}, log() {}, warn() {} },
  document: { getElementById: el, querySelector: () => null, querySelectorAll: () => [],
    createElement: el, addEventListener() {}, body: el() },
  window: { addEventListener() {}, requestAnimationFrame() {} },
  getComputedStyle: () => ({ getPropertyValue: () => '' }),
  sessionStorage: { getItem: () => null, setItem() {}, removeItem() {} },
  setTimeout: () => 0, clearTimeout() {}, setInterval: () => 0, clearInterval() {},
};
ctx.window.document = ctx.document;
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(corePath, 'utf8'), ctx, { filename: 'app.core.js' });
ctx.__cases = JSON.parse(casesJson);
process.stdout.write(JSON.stringify(vm.runInContext('__cases.map(parseVideoInput)', ctx)));
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="未安装 node")
def test_frontend_parse_video_input_matches_backend() -> None:
    """前端 parseVideoInput 对同一张表给出与后端一致的结果。"""
    raws = [raw for raw, _, _ in CASES]
    proc = subprocess.run(
        ["node", "-e", _NODE_SCRIPT, "--", str(_JS_DIR / "app.core.js"), json.dumps(raws, ensure_ascii=False)],
        capture_output=True, text=True, encoding="utf-8", timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    results = json.loads(proc.stdout)
    for (raw, bvid, short), result in zip(CASES, results):
        if not raw.strip():
            assert result == {"bvid": "", "error": ""}, raw
        elif bvid:
            assert result == {"bvid": bvid, "error": ""}, raw
        elif short:
            assert result == {"bvid": "", "error": SHORT_LINK_MESSAGE}, raw
        else:
            assert result == {"bvid": "", "error": NOT_FOUND_MESSAGE}, raw
