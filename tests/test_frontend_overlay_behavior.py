"""浮层开合行为契约：账号抽屉、QQ 群弹窗、提示弹窗、抽奖确认弹窗。

1. 静态断言：弹窗统一经 mountAppModal 挂载；navigateTo 先收起浮层；Esc 绑定到 closeAllOverlays；
   抽屉用请求序号丢弃过期响应。
2. 有 node 时，用 vm 加载真实 app.core.js / app.ui.js / app.hotspot.js / app.lottery.js，
   在最小 DOM 桩上跑一遍：切页关闭抽屉与弹窗、Esc 关闭、关闭后焦点回到触发元素、
   抽屉关闭后晚到的响应不再写入、抽奖确认弹窗被 Esc 关闭时按“取消”结束 Promise。
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

_JS = Path(__file__).resolve().parents[1] / "web" / "frontend" / "static" / "js"


def _read(name: str) -> str:
    return (_JS / name).read_text(encoding="utf-8")


def test_modals_mount_through_single_entry() -> None:
    """三类弹窗都经 mountAppModal 挂载，不再各自 append 到 body。"""
    ui, lottery = _read("app.ui.js"), _read("app.lottery.js")
    assert len(re.findall(r"\bfunction mountAppModal\s*\(", ui)) == 1
    for name, src, fn in (("app.ui.js", ui, "showGroupQrModal"), ("app.ui.js", ui, "showAppAlert"),
                          ("app.ui.js", ui, "showAppConfirm"),
                          ("app.lottery.js", lottery, "buildLotteryConfirmModal")):
        start = src.index(f"function {fn}(")
        body = src[start:src.index("\n}\n", start)]
        assert "mountAppModal(backdrop" in body, f"{name}:{fn} 未经 mountAppModal 挂载"
        assert "document.body.append(backdrop)" not in body, f"{name}:{fn} 仍直接 append 到 body"


def test_navigate_and_escape_close_overlays() -> None:
    """切换页面前收起浮层；Esc 键关闭所有浮层。"""
    core, ui = _read("app.core.js"), _read("app.ui.js")
    start = core.index("function navigateTo(")
    body = core[start:core.index("\n}\n", start)]
    assert body.index("closeAllOverlays()") < body.index("switchPage(pageName)")
    assert re.search(r"addEventListener\('keydown'[^\n]*\n[^\n]*Escape[^\n]*closeAllOverlays\(\)", ui)


def test_drawer_discards_stale_response() -> None:
    """抽屉打开时取序号，响应返回后序号不一致就丢弃；关闭时递增序号。"""
    hotspot = _read("app.hotspot.js")
    start = hotspot.index("async function openHotspotAccount(")
    body = hotspot[start:hotspot.index("\nfunction closeHotspotDrawer", start)]
    assert "const seq = ++hotspotDrawerSeq;" in body
    assert body.count("if (seq !== hotspotDrawerSeq) return;") == 2
    close = hotspot[hotspot.index("function closeHotspotDrawer("):]
    assert "hotspotDrawerSeq += 1;" in close[:close.index("\n}\n")]


_NODE_SCRIPT = r"""
const fs = require('fs');
const vm = require('vm');
const dir = process.argv[process.argv.length - 1];

// ---- 最小 DOM：只实现这几个脚本用到的部分 ----
class ClassList {
  constructor() { this.s = new Set(); }
  add(...c) { c.forEach(x => this.s.add(x)); } remove(...c) { c.forEach(x => this.s.delete(x)); }
  toggle(c, f) { const on = f === undefined ? !this.s.has(c) : f; on ? this.s.add(c) : this.s.delete(c); return on; }
  contains(c) { return this.s.has(c); }
}
class El {
  constructor(tag) { this.tagName = tag.toUpperCase(); this.children = []; this.parent = null; this.classList = new ClassList();
    this.dataset = {}; this.style = {}; this.attrs = {}; this.listeners = {}; this._html = ''; this.textContent = ''; this.id = ''; }
  get className() { return [...this.classList.s].join(' '); }
  set className(v) { this.classList = new ClassList(); String(v).split(/\s+/).filter(Boolean).forEach(c => this.classList.add(c)); }
  set innerHTML(v) { this._html = String(v); } get innerHTML() { return this._html; }
  setAttribute(k, v) { this.attrs[k] = String(v); } getAttribute(k) { return this.attrs[k] ?? null; }
  append(...kids) { kids.forEach(k => { if (k && typeof k === 'object') { k.parent = this; this.children.push(k); } }); }
  appendChild(k) { this.append(k); return k; }
  insertBefore(k) { this.append(k); return k; }
  remove() { if (this.parent) { this.parent.children = this.parent.children.filter(c => c !== this); this.parent = null; } }
  addEventListener(t, fn) { (this.listeners[t] ||= []).push(fn); }
  dispatch(t, ev) { (this.listeners[t] || []).forEach(fn => fn(ev)); }
  click() { this.dispatch('click', { target: this }); }
  focus() { doc.activeElement = this; }
  querySelector() { return null; } querySelectorAll() { return []; } closest() { return null; }
}
const all = el => [el, ...el.children.flatMap(all)];
const body = new El('body');
const byId = {};
const mk = (id, cls) => { const e = new El('div'); e.id = id; if (cls) e.className = cls; byId[id] = e; body.append(e); return e; };
mk('hotspot-drawer-mask', 'hotspot-drawer-mask'); mk('hotspot-drawer', 'hotspot-drawer');
['welcome', 'hotspot', 'comment'].forEach(p => mk(`${p}-page`, 'page'));
const docListeners = {};
const doc = {
  body, activeElement: body,
  getElementById: id => byId[id] || null,
  createElement: t => new El(t),
  contains: el => all(body).includes(el),
  querySelector: sel => {
    const cls = sel.replace(/^\./, '');
    return all(body).find(e => e.classList.contains(cls)) || null;
  },
  querySelectorAll: sel => {
    const cls = sel.replace(/^\./, '');
    return all(body).filter(e => e.classList.contains(cls));
  },
  addEventListener: (t, fn) => { (docListeners[t] ||= []).push(fn); },
  baseURI: 'http://127.0.0.1/',
};
let pending = null;
const ctx = {
  console: { error() {}, log() {}, warn() {} }, document: doc,
  window: { addEventListener() {}, requestAnimationFrame() {}, setInterval: () => 0, clearInterval() {}, setTimeout: () => 0, clearTimeout() {},
            location: { protocol: 'http:', host: '127.0.0.1' } },
  setTimeout: () => 0, clearTimeout() {}, setInterval: () => 0, clearInterval() {},
  sessionStorage: { getItem: () => null, setItem() {}, removeItem() {} },
  URL, Promise, JSON, Number, String, Object, Array, encodeURIComponent,
  fetch: () => new Promise(res => { pending = res; }),
};
ctx.window.document = doc;
vm.createContext(ctx);
// 与 index.html 的 <script> 顺序一致：app.ui.js 最后加载，末尾导出依赖 app.lottery.js 的函数。
for (const f of ['app.core.js', 'app.hotspot.js', 'app.lottery.js', 'app.ui.js']) {
  vm.runInContext(fs.readFileSync(`${dir}/${f}`, 'utf8'), ctx, { filename: f });
}
// navigateTo 里的懒加载与侧栏更新在桩上不需要真的执行
vm.runInContext('refreshPageOnEnter = () => {}; updateNavState = () => {};', ctx);
const esc = () => (docListeners.keydown || []).forEach(fn => fn({ key: 'Escape' }));
const backdrops = () => doc.querySelectorAll('.app-modal-backdrop').length;
const drawerOpen = () => byId['hotspot-drawer'].classList.contains('hotspot-drawer-open');
const out = {};

(async () => {
  // 抽屉：切页关闭
  ctx.openHotspotAccount(1, 'BV1');
  out.drawerOpened = drawerOpen();
  ctx.navigateTo('comment');
  out.drawerAfterNavigate = drawerOpen();
  // 抽屉：晚到响应被丢弃
  pending({ ok: true, status: 200, json: async () => ({ success: true, data: { name: 'LATE', mid: 1 } }) });
  await new Promise(r => setImmediate(r)); await new Promise(r => setImmediate(r));
  out.drawerLateWritten = byId['hotspot-drawer'].innerHTML.includes('LATE');
  // 抽屉：Esc 关闭
  ctx.openHotspotAccount(1, 'BV1');
  esc();
  out.drawerAfterEsc = drawerOpen();
  // 提示弹窗：Esc 关闭 + 焦点回到触发元素
  const trigger = new El('button'); body.append(trigger); trigger.focus();
  ctx.showAppAlert('probe');
  out.alertMounted = backdrops();
  esc();
  out.alertAfterEsc = backdrops();
  out.focusRestored = doc.activeElement === trigger;
  // 两个弹窗不叠加
  ctx.showAppAlert('a'); ctx.showGroupQrModal();
  out.stackedCount = backdrops();
  ctx.navigateTo('welcome');
  out.modalAfterNavigate = backdrops();
  // 抽奖确认：Esc 视为取消，Promise 得到 false
  const p = ctx.confirmLotteryTarget({ title: 't', author: 'a' });
  esc();
  out.lotteryEscResult = await Promise.race([p, new Promise(r => setTimeout(() => r('pending'), 50))]);
  out.lotteryAfterEsc = backdrops();
  // 通用确认弹窗（替代原生 confirm）：点确认得 true、点取消得 false、Esc / 点遮罩 / 切页都按取消
  const buttons = () => all(body).filter(e => e.tagName === 'BUTTON' && e.parent && e.parent.classList.contains('app-modal-actions'));
  const race = q => Promise.race([q, new Promise(r => setTimeout(() => r('pending'), 50))]);
  let q = ctx.showAppConfirm('sure?', { title: 'T', confirmText: 'OK', cancelText: 'NO', danger: true });
  out.confirmMounted = backdrops();
  out.confirmButtons = buttons().map(b => [b.textContent, b.className]);
  out.confirmDefaultFocus = doc.activeElement && doc.activeElement.textContent;
  buttons()[1].click();
  out.confirmOk = await race(q); out.confirmAfterOk = backdrops();
  q = ctx.showAppConfirm('sure?'); buttons()[0].click();
  out.confirmCancel = await race(q);
  q = ctx.showAppConfirm('sure?'); esc();
  out.confirmEsc = await race(q);
  q = ctx.showAppConfirm('sure?'); const bd = doc.querySelector('.app-modal-backdrop'); bd.dispatch('click', { target: bd });
  out.confirmBackdrop = await race(q);
  q = ctx.showAppConfirm('sure?'); ctx.navigateTo('comment');
  out.confirmNavigate = await race(q); out.confirmAfterAll = backdrops();
  process.stdout.write(JSON.stringify(out));
})().catch(e => process.stdout.write(JSON.stringify({ fatal: String(e && e.stack || e) })));
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="本机无 node")
def test_overlay_behavior_with_real_scripts() -> None:
    """真实脚本 + 最小 DOM：切页 / Esc 关闭浮层、焦点恢复、晚到响应丢弃、弹窗不叠加、抽奖确认按取消结束。"""
    proc = subprocess.run(["node", "-e", _NODE_SCRIPT, "--", str(_JS)], capture_output=True, text=True,
                          encoding="utf-8", timeout=60)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert "fatal" not in out, out.get("fatal")
    assert out["drawerOpened"] is True
    assert out["drawerAfterNavigate"] is False
    assert out["drawerLateWritten"] is False
    assert out["drawerAfterEsc"] is False
    assert out["alertMounted"] == 1 and out["alertAfterEsc"] == 0
    assert out["focusRestored"] is True
    assert out["stackedCount"] == 1
    assert out["modalAfterNavigate"] == 0
    assert out["lotteryEscResult"] is False
    assert out["lotteryAfterEsc"] == 0
    # showAppConfirm：取消在左、确认在右；默认焦点在取消；只有点确认才 resolve(true)
    assert out["confirmMounted"] == 1
    assert out["confirmButtons"] == [["NO", "btn btn-secondary"], ["OK", "btn btn-danger"]]
    assert out["confirmDefaultFocus"] == "NO"
    assert out["confirmOk"] is True and out["confirmAfterOk"] == 0
    assert out["confirmCancel"] is False
    assert out["confirmEsc"] is False
    assert out["confirmBackdrop"] is False
    assert out["confirmNavigate"] is False and out["confirmAfterAll"] == 0
