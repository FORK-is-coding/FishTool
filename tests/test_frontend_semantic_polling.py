"""语义折叠面板轮询行为测试（app.comment.semantic.js）。

长任务（几十分钟到几小时）下轮询必须一直跟进：
- 不再有 800 次（约 20 分钟）的总次数上限，任务仍在进行就继续轮询；
- 前 2 分钟每 1.5 秒一次，之后每 5 秒一次；
- 页面隐藏时停止，回到前台且任务仍在进行时恢复；
- running 且 10 分钟 revision 未变时提示“长时间无进展”，queued 不提示；
- 终态时停止并加载结果。

用 node vm 加载真实 app.core.js + app.comment.semantic.js。时钟是假的：
setTimeout 不真正等待，而是把假时间推进 delay 毫秒，所以几小时的轮询几毫秒就能跑完；
document 的事件监听是真实记录的，可以派发 visibilitychange。
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

_JS_DIR = Path(__file__).resolve().parents[1] / "web" / "frontend" / "static" / "js"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="本机无 node")

_NODE_SCRIPT = r"""
const fs = require('fs');
const vm = require('vm');
const [corePath, semanticPath, scenarioPath] = process.argv.slice(1);
const scenario = JSON.parse(fs.readFileSync(scenarioPath, 'utf8'));

function makeElement(id) {
  const node = {
    id, _html: '', value: '', checked: false, style: {}, dataset: {}, children: [],
    classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
    setAttribute() {}, removeAttribute() {}, getAttribute() { return null; },
    focus() {}, remove() {}, append() {}, appendChild() {}, addEventListener() {},
    querySelector() { return makeElement('q'); }, querySelectorAll() { return []; },
  };
  Object.defineProperty(node, 'innerHTML', { get() { return this._html; }, set(v) { this._html = String(v); } });
  Object.defineProperty(node, 'textContent', { get() { return this._html; }, set(v) { this._html = String(v); } });
  return node;
}

let now = 1_000_000;
const RealDate = Date;
class FakeDate extends RealDate {
  constructor(...args) { if (args.length) super(...args); else super(now); }
  static now() { return now; }
}

async function main() {
  const elements = {};
  const getEl = id => (elements[id] = elements[id] || makeElement(id));
  const docListeners = {};
  const statusCalls = [];   // 每次 GET /tasks/{id} 的假时间
  const stallHistory = [];  // 每次轮询请求发出时，上一轮渲染留下的 pollStalled
  let statusIndex = 0;
  let stopAfter = scenario.maxStatusCalls;

  // 状态序列：按调用次序取，取完后保持最后一个。
  function nextStatus() {
    const seq = scenario.statuses;
    const item = seq[Math.min(statusIndex, seq.length - 1)];
    statusIndex += 1;
    return item;
  }

  const ctx = {
    console: { error() {}, log() {}, warn() {} },
    document: {
      readyState: 'complete', hidden: false,
      getElementById: getEl, querySelector: () => null, querySelectorAll: () => [],
      createElement: () => makeElement('x'), body: makeElement('body'),
      addEventListener(type, fn) { (docListeners[type] = docListeners[type] || []).push(fn); },
    },
    window: {},
    Date: FakeDate,
    showAppAlert() {}, showToast() {}, showAppConfirm: async () => true,
    mountAppModal: () => (() => {}),
    getComputedStyle: () => ({ getPropertyValue: () => '' }),
    encodeURIComponent, JSON, Object, Array, Number, String, Boolean, Math, Promise, RegExp, Error,
    fetch: async (url) => {
      let payload = { success: true, data: null };
      if (url.indexOf('/preflight') !== -1) {
        payload = { success: true, data: { counts: { raw_total: 3, selected: 3, eligible: 3 },
          scope: { limit: 200 }, model_configured: true,
          provider_identity: { api_base: 'https://x', model: 'm' }, consent_needed: false,
          consent_version: 'semantic-fold-v1', limits: {} } };
      } else if (url.indexOf('/preferences') !== -1) {
        payload = { success: true, data: { auto_fold: true, revision: 0 } };
      } else if (url.indexOf('/latest') !== -1) {
        payload = { success: true, data: Object.assign({ task_id: 't1' }, scenario.statuses[0]) };
      } else if (url.indexOf('/tasks/t1/result') !== -1) {
        payload = { success: true, data: { groups: [], items: [], pagination: { page: 1, pages: 1, total: 0 },
          counts: {}, task: {} } };
      } else if (url.indexOf('/tasks/t1') !== -1) {
        statusCalls.push(now);
        stallHistory.push(Boolean(ctx.window.__semanticPanel && ctx.window.__semanticPanel.state.pollStalled));
        if (scenario.hideAtCall && statusCalls.length === scenario.hideAtCall) ctx.document.hidden = true;
        payload = { success: true, data: Object.assign({ task_id: 't1' }, nextStatus()) };
      }
      return { ok: true, status: 200, json: async () => payload };
    },
  };
  // 假时钟：setTimeout 立即在微任务里执行，并把时间推进 delay；达到调用上限后不再回调（让循环挂起）。
  const fakeSetTimeout = (fn, delay) => {
    if (statusCalls.length >= stopAfter) return 0;
    now += Number(delay) || 0;
    Promise.resolve().then(fn);
    return 0;
  };
  ctx.setTimeout = fakeSetTimeout; ctx.clearTimeout = () => {};
  ctx.setInterval = () => 0; ctx.clearInterval = () => {};
  ctx.requestAnimationFrame = () => 0;
  Object.assign(ctx.window, {
    document: ctx.document, setTimeout: fakeSetTimeout, clearTimeout() {},
    setInterval: () => 0, clearInterval() {}, requestAnimationFrame: () => 0, addEventListener() {},
  });
  ctx.globalThis = ctx;
  vm.createContext(ctx);
  vm.runInContext(fs.readFileSync(corePath, 'utf8'), ctx, { filename: 'app.core.js' });
  vm.runInContext(fs.readFileSync(semanticPath, 'utf8'), ctx, { filename: 'app.comment.semantic.js' });

  const flush = async () => { for (let i = 0; i < 20000; i++) await Promise.resolve(); };

  await ctx.window.onCommentBvidSelected('BV1poll');
  await flush();
  const out = { phase1Calls: statusCalls.slice(), stallHistory: stallHistory.slice(),
                phase1Polling: ctx.window.__semanticPanel.state.polling,
                phase1Stalled: ctx.window.__semanticPanel.state.pollStalled,
                taskHtml: getEl('semantic-task-area').innerHTML };

  if (scenario.thenShow) {
    ctx.document.hidden = false;
    stopAfter = statusCalls.length + scenario.thenShow;
    for (const fn of (docListeners.visibilitychange || [])) fn();
    await flush();
    out.afterShowCalls = statusCalls.length;
    out.afterShowPolling = ctx.window.__semanticPanel.state.polling;
  }
  if (scenario.stopAtEnd) {
    ctx.window.__semanticPanel.stopPolling();
    out.stalledAfterStop = ctx.window.__semanticPanel.state.pollStalled;
  }
  out.visibilityListeners = (docListeners.visibilitychange || []).length;
  process.stdout.write(JSON.stringify(out));
}
main().catch(err => { process.stderr.write(String(err && err.stack || err)); process.exit(1); });
"""


def _run(scenario: dict, tmp_path: Path) -> dict:
    # 状态序列可能有上千项，命令行装不下（Windows 32K 限制），经临时文件传入。
    scenario_file = tmp_path / "scenario.json"
    scenario_file.write_text(json.dumps(scenario), encoding="utf-8")
    proc = subprocess.run(
        ["node", "-e", _NODE_SCRIPT, str(_JS_DIR / "app.core.js"),
         str(_JS_DIR / "app.comment.semantic.js"), str(scenario_file)],
        capture_output=True, text=True, encoding="utf-8", timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def _running(revision: int) -> dict:
    return {"status": "running", "phase": "batch", "revision": revision, "progress": 0.1}


def test_polling_continues_past_old_800_cap_with_slower_interval(tmp_path) -> None:
    """一直 running 且每次都有进展：超过原 800 次上限仍在轮询，2 分钟后间隔变为 5 秒。"""
    statuses = [_running(i) for i in range(1200)]
    out = _run({"statuses": statuses, "maxStatusCalls": 1000}, tmp_path)

    calls = out["phase1Calls"]
    assert len(calls) == 1000  # 原实现在第 800 次后停止
    assert out["phase1Polling"] is True
    gaps = [b - a for a, b in zip(calls, calls[1:])]
    elapsed = [c - calls[0] for c in calls[1:]]
    fast = [g for g, t in zip(gaps, elapsed) if t <= 2 * 60 * 1000]
    slow = [g for g, t in zip(gaps, elapsed) if t > 2 * 60 * 1000 + 5000]
    assert fast and set(fast) == {1500}
    assert slow and set(slow) == {5000}
    # 1000 次轮询覆盖的时间远超 20 分钟
    assert calls[-1] - calls[0] > 60 * 60 * 1000


def test_terminal_status_stops_polling_and_loads_result(tmp_path) -> None:
    statuses = [_running(1), _running(2), {"status": "completed", "phase": "done", "revision": 3}]
    out = _run({"statuses": statuses, "maxStatusCalls": 50}, tmp_path)
    assert len(out["phase1Calls"]) == 3
    assert out["phase1Polling"] is False


def test_hidden_page_stops_and_visible_again_resumes_while_active(tmp_path) -> None:
    """页面隐藏后停止；回到前台且任务仍在进行，就重新接管轮询。"""
    statuses = [_running(i) for i in range(100)]
    out = _run({"statuses": statuses, "maxStatusCalls": 1000, "hideAtCall": 3, "thenShow": 4}, tmp_path)
    assert len(out["phase1Calls"]) == 3
    assert out["phase1Polling"] is False
    assert out["visibilityListeners"] == 1
    assert out["afterShowCalls"] == 3 + 4
    assert out["afterShowPolling"] is True


def test_visible_again_does_not_resume_for_finished_task(tmp_path) -> None:
    """任务已结束时回到前台不重新轮询。"""
    statuses = [_running(1), {"status": "failed", "phase": "done", "revision": 2, "error": "x"}]
    out = _run({"statuses": statuses, "maxStatusCalls": 50, "thenShow": 5}, tmp_path)
    assert len(out["phase1Calls"]) == 2
    assert out["afterShowCalls"] == 2
    assert out["afterShowPolling"] is False


def _first_stall_elapsed_ms(out: dict) -> int:
    """第一次出现提示时距首次轮询的假时间。stallHistory[i] 是第 i 次请求发出时上一轮的结果。"""
    calls, history = out["phase1Calls"], out["stallHistory"]
    first = next(i for i, stalled in enumerate(history) if stalled)
    return calls[first - 1] - calls[0]


def test_running_without_progress_for_10_minutes_shows_stall_hint(tmp_path) -> None:
    """running 且 revision 长时间不变：恰在约 10 分钟时开始提示无进展，之后继续轮询。"""
    statuses = [_running(7)] * 400
    out = _run({"statuses": statuses, "maxStatusCalls": 300}, tmp_path)
    assert out["phase1Polling"] is True
    assert out["phase1Stalled"] is True
    assert "没有新进展" in out["taskHtml"]
    # 阈值边界：10 分钟前没有提示，10 分钟到下一次 5 秒轮询之间出现。
    first_ms = _first_stall_elapsed_ms(out)
    assert 10 * 60 * 1000 <= first_ms < 10 * 60 * 1000 + 5000
    first_index = out["stallHistory"].index(True)
    assert not any(out["stallHistory"][:first_index])


def test_running_without_progress_for_9_minutes_has_no_hint(tmp_path) -> None:
    """不足 10 分钟不提示（前 2 分钟 80 次 1.5 秒 + 84 次 5 秒 ≈ 9 分钟）。"""
    statuses = [_running(7)] * 400
    out = _run({"statuses": statuses, "maxStatusCalls": 165}, tmp_path)
    assert out["phase1Calls"][-1] - out["phase1Calls"][0] < 10 * 60 * 1000
    assert out["phase1Stalled"] is False
    assert not any(out["stallHistory"])


def test_queued_without_progress_does_not_show_stall_hint(tmp_path) -> None:
    """排队中 revision 本来不会变（全局串行），不能误报无进展。"""
    statuses = [{"status": "queued", "phase": "queued", "revision": 0}] * 400
    out = _run({"statuses": statuses, "maxStatusCalls": 300}, tmp_path)
    assert out["phase1Polling"] is True
    assert out["phase1Stalled"] is False
    assert "没有新进展" not in out["taskHtml"]


def test_progress_clears_stall_hint(tmp_path) -> None:
    """卡住超过 10 分钟后 revision 又变化：先出现提示，再消失。"""
    statuses = [_running(7)] * 200 + [_running(8)]
    out = _run({"statuses": statuses, "maxStatusCalls": 201}, tmp_path)
    assert any(out["stallHistory"])  # 中途确实进入过“无进展”
    assert out["phase1Stalled"] is False
    assert "没有新进展" not in out["taskHtml"]


def test_stall_flag_is_cleared_when_polling_stops(tmp_path) -> None:
    """stopPolling 清掉提示标志，不会带到下一个 BV 的首次渲染。"""
    statuses = [_running(7)] * 400
    out = _run({"statuses": statuses, "maxStatusCalls": 300, "stopAtEnd": True}, tmp_path)
    assert out["phase1Stalled"] is True
    assert out["stalledAfterStop"] is False
