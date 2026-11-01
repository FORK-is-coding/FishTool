"""评论大屏渲染契约：app.comment.js 依赖的共享函数必须有定义。

前端没有 JS 测试框架，按仓库惯例：
1. 静态断言：``asArray`` 只在 app.core.js 定义一次（与 escapeHtml / jsArg 同一约定），
   且 app.comment.js 中的每个调用点都能解析到它；
2. 有 node 时，用 vm 加载真实 app.core.js + app.comment.js，模拟一次单视频采集成功，
   断言结果区与大屏正常渲染，而不是落到「监控失败: asArray is not defined」。
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

_JS_DIR = Path(__file__).resolve().parents[1] / "web" / "frontend" / "static" / "js"


def _read(name: str) -> str:
    return (_JS_DIR / name).read_text(encoding="utf-8")


def test_as_array_defined_once_in_core() -> None:
    """asArray 只在 app.core.js 定义一次，其他脚本只调用不重复定义。"""
    pattern = re.compile(r"\bfunction asArray\s*\(")
    owners = [
        (path.name, len(pattern.findall(path.read_text(encoding="utf-8"))))
        for path in sorted(_JS_DIR.glob("*.js"))
        if pattern.search(path.read_text(encoding="utf-8"))
    ]
    assert owners == [("app.core.js", 1)], f"asArray 定义位置异常：{owners}"


def test_comment_dashboard_calls_resolve_to_defined_helper() -> None:
    """app.comment.js 中所有 asArray( 调用都依赖 app.core.js 的定义（防止再次只调用不定义）。"""
    calls = re.findall(r"(?<![.\w$])asArray\(", _read("app.comment.js"))
    assert calls, "app.comment.js 应通过 asArray 归一列表字段"
    assert re.search(r"\bfunction asArray\s*\(", _read("app.core.js"))


_NODE_SCRIPT = r"""
const fs = require('fs');
const vm = require('vm');
// node -e 下 argv 没有脚本路径位，取末尾三个参数，与调用方式无关。
const [corePath, commentPath, casesJson] = process.argv.slice(-3);
const cases = JSON.parse(casesJson);

function makeElement(id) {
  return {
    id, innerHTML: '', textContent: '', value: '', checked: true, hidden: false, disabled: false,
    style: {}, dataset: {},
    classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
    querySelector: () => null, querySelectorAll: () => [], appendChild() {}, insertAdjacentHTML() {},
    setAttribute() {}, removeAttribute() {}, closest: () => null,
  };
}

async function run(response) {
  const elements = {};
  const getEl = id => (elements[id] = elements[id] || makeElement(id));
  getEl('monitor-bvid').value = 'BV1xx411c7mD';
  const ctx = {
    console: { error() {}, log() {}, warn() {} },
    document: {
      getElementById: getEl, querySelector: () => null, querySelectorAll: () => [],
      createElement: () => makeElement('x'), body: makeElement('body'),
    },
    window: { requestAnimationFrame() {}, addEventListener() {}, echarts: undefined },
    getComputedStyle: () => ({ getPropertyValue: () => '' }),
    sessionStorage: { getItem: () => null, setItem() {}, removeItem() {} },
    setTimeout: () => 0, clearTimeout() {}, setInterval: () => 0, clearInterval() {},
    showAppAlert() {}, showToast() {},
    fetch: async () => ({ ok: true, status: 200, json: async () => response }),
  };
  ctx.window.document = ctx.document;
  vm.createContext(ctx);
  vm.runInContext(fs.readFileSync(corePath, 'utf8'), ctx, { filename: 'app.core.js' });
  vm.runInContext(fs.readFileSync(commentPath, 'utf8'), ctx, { filename: 'app.comment.js' });
  await ctx.monitorVideo();
  return {
    monitorResult: getEl('monitor-result').innerHTML,
    metricBefore: String(getEl('metric-before').textContent),
    metricVoice: String(getEl('metric-voice').textContent),
    top10: getEl('monitor-top10').innerHTML,
  };
}

(async () => {
  const out = [];
  for (const c of cases) out.push(await run(c));
  process.stdout.write(JSON.stringify(out));
})().catch(error => { process.stdout.write(JSON.stringify({ fatal: String(error && error.stack || error) })); });
"""


def _success_response(visualization: dict) -> dict:
    return {
        "success": True,
        "data": {
            "processed_count": 2,
            "visualization": visualization,
            "sentiment": {"positive": 2, "negative": 0, "neutral": 0},
        },
    }


@pytest.mark.skipif(shutil.which("node") is None, reason="本机无 node")
def test_single_video_monitor_renders_dashboard_with_real_scripts() -> None:
    """单视频采集成功：结果区不出现「监控失败」，大屏指标与 Top10 正常渲染；列表字段缺失或类型异常时降级为空。"""
    full = _success_response({
        "dedup_statistics": {"before_count": 3, "after_count": 2, "removed_count": 1, "reasons": {}},
        "sentiment_distribution": {"positive": 2},
        "date_comment_counts": [{"date": "2026-10-29", "count": 2}],
        "top10_voice_comments": [{"uname": "a", "like": 3, "content": "好评", "sentiment": "positive"}],
    })
    # 后端字段缺失 / 被返回成对象：asArray 应归一为空列表，而不是抛错。
    malformed = _success_response({
        "dedup_statistics": {"before_count": 0, "after_count": 0, "removed_count": 0},
        "date_comment_counts": {"2026-10-29": 2},
        "top10_voice_comments": None,
    })
    proc = subprocess.run(
        ["node", "-e", _NODE_SCRIPT, "--", str(_JS_DIR / "app.core.js"), str(_JS_DIR / "app.comment.js"),
         json.dumps([full, malformed], ensure_ascii=False)],
        capture_output=True, text=True, encoding="utf-8", timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout)
    assert isinstance(result, list), result

    ok, degraded = result
    assert "监控失败" not in ok["monitorResult"], ok["monitorResult"]
    assert ok["metricBefore"] == "3"
    assert ok["metricVoice"] == "1"
    assert "好评" in ok["top10"]

    assert "监控失败" not in degraded["monitorResult"], degraded["monitorResult"]
    assert degraded["metricVoice"] == "0"
    assert "暂无高声量评论" in degraded["top10"]
