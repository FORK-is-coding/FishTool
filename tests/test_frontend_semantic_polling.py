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
  // localStorage 内存桩：预算模式“记住上次选择”用；Map 语义与真 storage 一致。
  const storageMap = new Map();
  const localStorageStub = {
    getItem: k => (storageMap.has(k) ? storageMap.get(k) : null),
    setItem: (k, v) => storageMap.set(k, String(v)),
    removeItem: k => storageMap.delete(k),
  };
  const docListeners = {};
  const postedKeys = [];    // 每次 POST /tasks 带的 request_key
  const postedBodies = [];  // 每次 POST /tasks 的完整请求体
  const confirmCalls = [];  // 每次 showAppConfirm 的文案与选项
  let latestOverride;       // 定义后 /latest 一律返回它（用于切回时返回终态任务）
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
    showAppAlert() {}, showToast() {},
    showAppConfirm: async (msg, opts) => {
      confirmCalls.push({ msg: String(msg), opts: opts || {} });
      return scenario.confirmResult !== false;
    },
    mountAppModal: () => (() => {}),
    getComputedStyle: () => ({ getPropertyValue: () => '' }),
    encodeURIComponent, JSON, Object, Array, Number, String, Boolean, Math, Promise, RegExp, Error,
    fetch: async (url, options) => {
      let payload = { success: true, data: null };
      const method = (options && options.method) || 'GET';
      if (method === 'POST' && /\/tasks$/.test(url)) {
        // 发起任务：记录 request_key 与完整请求体，返回 queued，之后轮询按 statuses 推进
        try {
          const body = JSON.parse(options.body);
          postedKeys.push(body.request_key);
          postedBodies.push(body);
        } catch (e) { postedKeys.push(null); postedBodies.push(null); }
        payload = { success: true, data: { task_id: 't1', status: 'queued', revision: 0 } };
        return { ok: true, status: 200, json: async () => payload };
      }
      if (url.indexOf('/local-token') !== -1) {
        payload = { success: true, data: { token: 't' } };
      } else if (url.indexOf('/preflight') !== -1) {
        payload = { success: true, data: { counts: { raw_total: 3, selected: 3, eligible: 3 },
          scope: { limit: 200 }, model_configured: true,
          provider_identity: { api_base: 'https://x', model: 'm' }, consent_needed: false,
          consent_version: 'semantic-fold-v1', limits: {} } };
      } else if (url.indexOf('/preferences') !== -1) {
        payload = { success: true, data: { auto_fold: true, revision: 0,
          consent_version: 'semantic-fold-v1', model_identity: { api_base: 'https://x', model: 'm' } } };
      } else if (url.indexOf('/latest') !== -1) {
        payload = { success: true, data: url.indexOf('BVother') !== -1 ? null
          : latestOverride !== undefined ? latestOverride
          : scenario.noLatest ? null
          : Object.assign({ task_id: 't1' }, scenario.statuses[0]) };
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
    localStorage: localStorageStub,
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
  if (scenario.startTwice) {
    // 第一次发起：轮询到 statuses 的终态后，再点一次“开始 / 重新分析”。
    stopAfter = 1000;
    await ctx.window.__semanticPanel.onStart();
    await flush();
    out.statusAfterFirst = ctx.window.__semanticPanel.state.task && ctx.window.__semanticPanel.state.task.status;
    await ctx.window.__semanticPanel.onStart();
    await flush();
    out.postedKeys = postedKeys.slice();
  }
  if (scenario.reselectTerminal) {
    // 在 BV1poll 发起任务（保持 running，不触发终态重置），切去 BVother 再切回：
    // /latest 返回已终态的旧任务，之后“重新分析”必须带新 request_key。
    await ctx.window.__semanticPanel.onStart();
    await flush();
    await ctx.window.onCommentBvidSelected('BVother');
    await flush();
    latestOverride = { task_id: 't1', status: 'completed', phase: 'done', revision: 9, coverage: {} };
    await ctx.window.onCommentBvidSelected('BV1poll');
    await flush();
    await ctx.window.__semanticPanel.onStart();
    await flush();
    out.postedKeys = postedKeys.slice();
  }
  if (scenario.budgetFlow) {
    // 预算模式流程：先切档，再点“开始分析”；记录确认弹窗与 POST 请求体。
    stopAfter = 1000;
    if (scenario.budgetFlow.mode) ctx.window.__semanticPanel.setBudgetMode(scenario.budgetFlow.mode);
    await ctx.window.__semanticPanel.onStart();
    await flush();
    if (scenario.budgetFlow.thenSwitch) {
      // 运行中切换档位：幂等键必须重置，下一次发起带新模式。
      ctx.window.__semanticPanel.setBudgetMode(scenario.budgetFlow.thenSwitch);
      await ctx.window.__semanticPanel.onStart();
      await flush();
    }
    out.confirmCalls = confirmCalls.slice();
    out.postedBodies = postedBodies.slice();
    out.postedKeys = postedKeys.slice();
    out.storedMode = storageMap.get('fishtool.semantic.budgetMode') || null;
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


# ---------------------------------------------------------------------------
# PR 1 复审：任务结束后“重新分析”必须带新的 request_key（否则后端幂等返回旧任务）
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("terminal", ["completed", "partial", "failed", "cancelled"])
def test_restart_after_terminal_task_uses_new_request_key(tmp_path, terminal) -> None:
    """发起 → 轮询到终态 → 再点“重新分析”：第二次 POST /tasks 的 request_key 必须不同。"""
    statuses = [_running(1), {"status": terminal, "phase": "done", "revision": 2,
                              "coverage": {"stop_reason": "rounds_exhausted"}}]
    out = _run({"statuses": statuses, "maxStatusCalls": 1000, "noLatest": True,
                "startTwice": True}, tmp_path)
    assert out["statusAfterFirst"] == terminal
    keys = out["postedKeys"]
    assert len(keys) == 2 and keys[0] and keys[1]
    assert keys[0] != keys[1]


def test_restart_while_task_still_active_keeps_request_key(tmp_path) -> None:
    """任务还在进行（没到终态）时再次提交：沿用同一个 key，后端幂等返回同一任务，不重复计费。"""
    statuses = [_running(i) for i in range(1, 50)]
    out = _run({"statuses": statuses, "maxStatusCalls": 3, "noLatest": True,
                "startTwice": True}, tmp_path)
    keys = out["postedKeys"]
    assert len(keys) == 2
    assert keys[0] == keys[1]


def test_reselect_bv_with_terminal_task_uses_new_request_key(tmp_path) -> None:
    """任务在我们没轮询它时进入终态（切去别的 BV 再切回，/latest 返回 completed）：
    旧 request_key 已失效，“重新分析”必须带新 key，否则后端幂等返回旧任务、按钮看似没反应。"""
    statuses = [_running(i) for i in range(1, 6)]
    out = _run({"statuses": statuses, "maxStatusCalls": 0, "noLatest": True,
                "reselectTerminal": True}, tmp_path)
    keys = out["postedKeys"]
    assert len(keys) == 2 and keys[0] and keys[1]
    assert keys[0] != keys[1]

# ---------------------------------------------------------------------------
# PR 3：预算模式选择与无限制档二次确认
# ---------------------------------------------------------------------------

def test_unlimited_mode_requires_confirm_and_posts_mode(tmp_path) -> None:
    """选无限制档：开始前弹二次确认（写明费用不退），确认后 POST 带 budget_mode=unlimited。"""
    statuses = [_running(i) for i in range(1, 5)]
    out = _run({"statuses": statuses, "maxStatusCalls": 0, "noLatest": True,
                "budgetFlow": {"mode": "unlimited"}}, tmp_path)
    assert len(out["confirmCalls"]) == 1
    msg = out["confirmCalls"][0]["msg"]
    assert "费用不退" in msg
    # harness 的预检不带 estimate：不能拼出“0 条、1～1 分钟”这类假数字
    assert "暂时无法预估" in msg and "约 0 条" not in msg
    assert len(out["postedBodies"]) == 1
    assert out["postedBodies"][0]["budget_mode"] == "unlimited"
    assert out["storedMode"] == "unlimited"


def test_unlimited_mode_declined_posts_nothing(tmp_path) -> None:
    """确认弹窗点“再想想”：不发起任何任务。"""
    statuses = [_running(i) for i in range(1, 5)]
    out = _run({"statuses": statuses, "maxStatusCalls": 0, "noLatest": True,
                "confirmResult": False, "budgetFlow": {"mode": "unlimited"}}, tmp_path)
    assert len(out["confirmCalls"]) == 1
    assert out["postedBodies"] == []


def test_default_mode_posts_default_without_confirm(tmp_path) -> None:
    """默认档：不弹确认，POST 带 budget_mode=default。"""
    statuses = [_running(i) for i in range(1, 5)]
    out = _run({"statuses": statuses, "maxStatusCalls": 0, "noLatest": True,
                "budgetFlow": {}}, tmp_path)
    assert out["confirmCalls"] == []
    assert len(out["postedBodies"]) == 1
    assert out["postedBodies"][0]["budget_mode"] == "default"


def test_switching_mode_resets_request_key(tmp_path) -> None:
    """运行中切到无限制档：幂等键重置（否则同 key 不同模式被后端 409），第二次发起带新模式。"""
    statuses = [_running(i) for i in range(1, 50)]
    out = _run({"statuses": statuses, "maxStatusCalls": 3, "noLatest": True,
                "budgetFlow": {"mode": "default", "thenSwitch": "unlimited"}}, tmp_path)
    keys = out["postedKeys"]
    assert len(keys) == 2 and keys[0] and keys[1]
    assert keys[0] != keys[1]
    assert out["postedBodies"][0]["budget_mode"] == "default"
    assert out["postedBodies"][1]["budget_mode"] == "unlimited"
