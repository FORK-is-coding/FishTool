"""语义折叠前端契约测试（D 段 · app.comment.semantic.js）。

前端无 JS 测试框架，按仓库惯例：
1. 静态断言：不重复定义 app.core.js 的共享工具；内联事件属性不含引号包裹插值；
   面板节点/脚本/样式表在 index.html 中就位，且脚本在 app.core.js / app.ui.js 之后加载。
2. 有 node 时用 vm 加载真实 app.core.js + app.ui.js + app.comment.semantic.js，覆盖：
   - 历史/刷新路径：无 live 采集，直接按 BV 加载 preflight+latest，面板常驻可用；
   - 无模型配置时 preflight 说明照常渲染（不因缺 model 而失败）；
   - auto_fold 开关只 PUT 偏好 + 本地重渲染，绝不发起 /tasks 模型请求；
   - 模型 reason / 成员原文经 escapeHtml，脚本标签不会被当 HTML 执行。
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

_FRONTEND_DIR = Path(__file__).resolve().parents[1] / "web" / "frontend"
_JS_DIR = _FRONTEND_DIR / "static" / "js"
_INDEX_HTML = _FRONTEND_DIR / "templates" / "index.html"
_SEMANTIC_JS = _JS_DIR / "app.comment.semantic.js"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 静态契约
# ---------------------------------------------------------------------------
def test_semantic_script_does_not_redefine_core_helpers() -> None:
    """语义脚本不得重定义 app.core.js 的共享工具（escapeHtml/jsArg/asArray/apiRequest/sleep）。"""
    src = _read(_SEMANTIC_JS)
    for helper in ("escapeHtml", "jsArg", "asArray", "apiRequest", "sleep"):
        assert not re.search(rf"\bfunction {helper}\s*\(", src), f"语义脚本不应重定义 {helper}"


def test_semantic_inline_handlers_have_no_quoted_interpolation() -> None:
    """语义脚本内联事件属性里不得出现引号包裹的字符串插值（与仓库 XSS 契约一致）。"""
    src = _read(_SEMANTIC_JS)
    inline_handler = re.compile(r'\bon[a-z]+="([^"]*)"')
    quoted_interp = re.compile(r"'\$\{(?!Number\()")
    hits = [m.start() for m in inline_handler.finditer(src) if quoted_interp.search(m.group(1))]
    assert hits == [], "语义脚本内联事件属性不应含引号包裹插值；应走事件委托 + data-*"


def test_index_has_panel_nodes_and_independent_selector() -> None:
    """index.html 含语义面板节点、独立 BV 选择入口，且面板不再默认隐藏。"""
    html = _read(_INDEX_HTML)
    assert 'id="semantic-panel"' in html
    assert 'id="semantic-body"' in html
    # 独立 BV 选择入口：历史数据/刷新也能用，不依赖本次采集。
    assert 'id="semantic-bvid"' in html
    assert 'id="semantic-load-btn"' in html
    # 面板节点不得带 hidden 属性（常驻可见）。
    panel = re.search(r'<section id="semantic-panel"[^>]*>', html)
    assert panel and "hidden" not in panel.group(0), "语义面板不应默认隐藏"


def test_semantic_scripts_load_after_core_and_ui() -> None:
    """app.comment.semantic.js 必须在 app.core.js 与 app.ui.js 之后加载。"""
    html = _read(_INDEX_HTML)
    pos_core = html.index("/static/js/app.core.js")
    pos_ui = html.index("/static/js/app.ui.js")
    pos_semantic = html.index("/static/js/app.comment.semantic.js")
    assert pos_core < pos_semantic, "语义脚本须在 app.core.js 之后加载"
    assert pos_ui < pos_semantic, "语义脚本须在 app.ui.js 之后加载（依赖 mountAppModal）"


def test_comment_js_exposes_current_bvid_hook() -> None:
    """app.comment.js 单视频渲染后调用 setCurrentCommentBvid，并对接面板回调。"""
    src = _read(_JS_DIR / "app.comment.js")
    assert "setCurrentCommentBvid(bvid)" in src
    assert "window.onCommentBvidSelected" in src


# ---------------------------------------------------------------------------
# Node VM：真实脚本行为
# ---------------------------------------------------------------------------
_NODE_SCRIPT = r"""
const fs = require('fs');
const vm = require('vm');
const [corePath, semanticPath, casesJson] = process.argv.slice(-3);
const cases = JSON.parse(casesJson);

// 极简 DOM：元素按 id 复用，记录 innerHTML / value / checked / 事件监听。
function makeElement(id) {
  const listeners = {};
  const children = [];
  const node = {
    id, _html: '', value: '', checked: false, hidden: false, disabled: false,
    style: {}, dataset: {}, children,
    classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
    setAttribute() {}, removeAttribute() {}, getAttribute(name) { return this.dataset[name] || null; },
    focus() {}, remove() {}, append(...n) { children.push(...n); }, appendChild(n) { children.push(n); },
    addEventListener(type, fn) { (listeners[type] = listeners[type] || []).push(fn); },
    async dispatch(type, event) { for (const fn of (listeners[type] || [])) { await fn(event || {}); } },
    querySelector() { return makeElement('q'); },
    querySelectorAll() { return []; },
  };
  Object.defineProperty(node, 'innerHTML', {
    get() { return this._html; },
    set(v) { this._html = String(v); },
  });
  Object.defineProperty(node, 'textContent', { get() { return this._html; }, set(v) { this._html = String(v); } });
  return node;
}

async function run(testCase) {
  const elements = {};
  const getEl = id => (elements[id] = elements[id] || makeElement(id));
  // 面板基础节点
  getEl('semantic-panel'); getEl('semantic-body'); getEl('semantic-bvid');
  getEl('semantic-load-btn'); getEl('semantic-actions'); getEl('semantic-task-area');
  getEl('semantic-result-area'); getEl('semantic-autofold-toggle');

  const fetchLog = [];
  const pendingResults = [];
  let deferResults = false;
  const ctx = {
    console: { error() {}, log() {}, warn() {} },
    document: {
      readyState: 'complete',
      hidden: false,
      getElementById: getEl,
      querySelector: () => null,
      querySelectorAll: () => [],
      createElement: () => makeElement('x'),
      body: makeElement('body'),
      addEventListener() {},
    },
    window: {},
    API_BASE: '',
    // 同步立即执行的 setTimeout：轮询没有总次数上限，用例里的任务必须是终态或 null，
    // 否则会一直轮询到超时。测试进行中的任务请用 test_frontend_semantic_polling.py 的假时钟夹具。
    setTimeout: (fn) => { if (typeof fn === 'function') fn(); return 0; },
    clearTimeout() {}, setInterval: () => 0, clearInterval() {},
    requestAnimationFrame() {},
    showAppAlert(msg) { ctx.window.__lastAlert = msg; },
    showToast() {},
    showAppConfirm: async () => true,
    // app.ui.js 的弹窗助手桩（这些 VM 用例不触发模态；仅 selectBvid/auto_fold/result 路径）。
    mountAppModal: () => (() => {}),
    getComputedStyle: () => ({ getPropertyValue: () => '' }),
    encodeURIComponent,
    JSON, Object, Array, Number, String, Boolean, Math, Date, Promise, RegExp, Error,
    fetch: async (url, options) => {
      const opts = options || {};
      fetchLog.push({
        url,
        method: opts.method || 'GET',
        headers: opts.headers || {},
        body: opts.body !== undefined ? opts.body : null,
      });
      if (deferResults && url.indexOf('/result?') !== -1) {
        return new Promise((resolve, reject) => pendingResults.push({ resolve, reject }));
      }
      const route = Object.keys(testCase.responses).find(key => url.indexOf(key) !== -1);
      const payload = route ? testCase.responses[route] : { success: true, data: null };
      return { ok: true, status: 200, json: async () => payload };
    },
  };
  ctx.window.document = ctx.document;
  // app.ui.js 等脚本在顶层用 window.setInterval / addEventListener，补齐这些桩。
  ctx.window.setInterval = () => 0;
  ctx.window.clearInterval = () => {};
  ctx.window.setTimeout = (fn) => { if (typeof fn === 'function') fn(); return 0; };
  ctx.window.clearTimeout = () => {};
  ctx.window.requestAnimationFrame = () => 0;
  ctx.window.addEventListener = () => {};
  ctx.globalThis = ctx;
  vm.createContext(ctx);
  vm.runInContext(fs.readFileSync(corePath, 'utf8'), ctx, { filename: 'app.core.js' });
  vm.runInContext(fs.readFileSync(semanticPath, 'utf8'), ctx, { filename: 'app.comment.semantic.js' });

  // 历史/刷新路径：无 live 采集，直接按 BV 加载。
  await ctx.window.onCommentBvidSelected(testCase.bvid);
  // 允许 auto_fold 切换（若用例要求）。
  let toggleFetchCountAfter = null;
  if (testCase.toggleAutoFold) {
    const before = fetchLog.length;
    const toggle = getEl('semantic-autofold-toggle');
    await toggle.dispatch('change', { target: { checked: true } });
    toggleFetchCountAfter = fetchLog.slice(before);
  }
  // 可选：点击一个决策按钮（data-decide=.. 由用例指定），捕获其写请求。
  let decisionFetch = null;
  if (testCase.clickDecide) {
    const before = fetchLog.length;
    const area = getEl('semantic-result-area');
    // 结果区 HTML 为字符串，无法真实点击；直接调用暴露的 submitDecision。
    const panel = ctx.window.__semanticPanel;
    if (panel && panel.submitDecision) {
      await panel.submitDecision(testCase.clickDecide.action,
        testCase.clickDecide.group_id || null, testCase.clickDecide.rpid || null);
    }
    decisionFetch = fetchLog.slice(before).filter(f => f.url.indexOf('/decisions') !== -1)[0] || null;
  }

  let raceHtml = null;
  if (testCase.resultRace) {
    deferResults = true;
    const area = getEl('semantic-result-area');
    const click = () => area.dispatch('click', {
      target: { getAttribute: name => name === 'data-page' ? 'next' : null },
    });
    await click();
    await click();
    if (pendingResults.length !== 2) throw new Error('expected two result requests');
    const newer = JSON.parse(JSON.stringify(testCase.responses['/result']));
    newer.data.items = [{rpid:'new', content:'NEWEST_RESULT'}];
    const older = JSON.parse(JSON.stringify(newer));
    older.data.items = [{rpid:'old', content:'STALE_RESULT'}];
    pendingResults[1].resolve({ok:true,status:200,json:async()=>newer});
    for (let i=0; i<12; i++) await Promise.resolve();
    if (testCase.resultRace === 'failure') pendingResults[0].reject(new Error('STALE_FAILURE'));
    else pendingResults[0].resolve({ok:true,status:200,json:async()=>older});
    for (let i=0; i<12; i++) await Promise.resolve();
    raceHtml = area.innerHTML;
  }

  return {
    bodyHtml: getEl('semantic-body').innerHTML,
    taskHtml: getEl('semantic-task-area').innerHTML,
    raceHtml,
    resultHtml: getEl('semantic-result-area').innerHTML,
    fetchUrls: fetchLog.map(f => `${f.method} ${f.url}`),
    fetchLog: fetchLog,
    toggleFetches: toggleFetchCountAfter,
    decisionFetch,
    lastAlert: ctx.window.__lastAlert || null,
  };
}

(async () => {
  const out = [];
  for (const c of cases) out.push(await run(c));
  process.stdout.write(JSON.stringify(out));
})().catch(error => { process.stdout.write(JSON.stringify({ fatal: String(error && error.stack || error) })); });
"""


def _run_node(cases: list) -> list:
    proc = subprocess.run(
        ["node", "-e", _NODE_SCRIPT, "--",
         str(_JS_DIR / "app.core.js"), str(_SEMANTIC_JS),
         json.dumps(cases, ensure_ascii=False)],
        capture_output=True, text=True, encoding="utf-8", timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    data = json.loads(proc.stdout)
    assert isinstance(data, list), data
    return data


def _preflight(configured: bool) -> dict:
    identity = {"api_base": "https://api.example.com", "model": "demo"} if configured else {}
    return {
        "success": True,
        "data": {
            "counts": {"raw_total": 37, "selected": 37, "eligible": 34},
            "scope": {"limit": 200},
            "model_configured": configured,
            "provider_identity": identity,
            "consent_needed": True,
            "consent_version": "semantic-fold-v1",
            "limits": {},
        },
    }


@pytest.mark.skipif(shutil.which("node") is None, reason="本机无 node")
def test_history_refresh_path_renders_without_live_collection() -> None:
    """历史/刷新路径：直接按 BV 加载 preflight+latest，面板渲染概览，不要求 live 采集。"""
    case = {
        "bvid": "BV1history",
        "toggleAutoFold": False,
        "responses": {
            "/preflight": _preflight(True),
            "/preferences": {"success": True, "data": {"auto_fold": True, "revision": 0,
                                                       "consent_version": None, "model_identity": None}},
            "/latest": {"success": True, "data": None},
            "/local-token": {"success": True, "data": {"token": "t", "header": "X-Local-Token"}},
        },
    }
    out = _run_node([case])[0]
    # 概览渲染出计数（37 原始 / 34 可参与），没有落到“服务不可用”。
    assert "37" in out["bodyHtml"]
    assert "34" in out["bodyHtml"]
    assert "语义折叠服务暂不可用" not in out["bodyHtml"]
    # 历史路径不应自动发起任务（无 /tasks POST）。
    assert not any("POST /comment/semantic/tasks" in u for u in out["fetchUrls"])


@pytest.mark.skipif(shutil.which("node") is None, reason="本机无 node")
def test_preflight_renders_when_model_not_configured() -> None:
    """无模型配置：preflight 说明照常渲染，提示去配置，不因缺 model 而抛错。"""
    case = {
        "bvid": "BV1nomodel",
        "toggleAutoFold": False,
        "responses": {
            "/preflight": _preflight(False),
            "/preferences": {"success": True, "data": {"auto_fold": True, "revision": 0,
                                                       "consent_version": None, "model_identity": None}},
            "/latest": {"success": True, "data": None},
        },
    }
    out = _run_node([case])[0]
    assert "未配置大模型" in out["bodyHtml"]


@pytest.mark.skipif(shutil.which("node") is None, reason="本机无 node")
def test_autofold_toggle_persists_pref_without_model_request() -> None:
    """auto_fold 开关只 PUT /preferences（+ 可能的 local-token），绝不发起 /tasks 模型请求。"""
    case = {
        "bvid": "BV1fold",
        "toggleAutoFold": True,
        "responses": {
            "/preflight": _preflight(True),
            "/preferences": {"success": True, "data": {"auto_fold": True, "revision": 1,
                                                       "consent_version": "semantic-fold-v1",
                                                       "model_identity": {"api_base": "https://api.example.com",
                                                                          "model": "demo"}}},
            "/latest": {"success": True, "data": None},
            "/local-token": {"success": True, "data": {"token": "t", "header": "X-Local-Token"}},
        },
    }
    out = _run_node([case])[0]
    toggle_fetches = out["toggleFetches"] or []
    assert toggle_fetches, "切换应至少发生一次 PUT /preferences"
    # 切换期间不得出现任何 /tasks（模型请求）。
    assert all("/tasks" not in f["url"] for f in toggle_fetches), toggle_fetches
    assert any(f["method"] == "PUT" and "/preferences" in f["url"] for f in toggle_fetches), toggle_fetches


@pytest.mark.skipif(shutil.which("node") is None, reason="本机无 node")
def test_model_reason_and_member_text_are_escaped() -> None:
    """模型 reason / 成员原文含 <script> 时必须被 escapeHtml 转义，不原样进 innerHTML。"""
    latest_completed = {
        "success": True,
        "data": {"task_id": "t1", "bvid": "BV1xss", "status": "completed", "revision": 2,
                 "progress": {"percent": 100}},
    }
    result = {
        "success": True,
        "data": {
            "task": {"task_id": "t1", "status": "completed"},
            "revision": 2,
            "preference": {"auto_fold": True, "revision": 2},
            "counts": {},
            "groups": [{
                "group_id": "g1",
                "reason": "<script>alert(1)</script>",
                "members": [
                    {"local_id": "c0", "rpid": "r0", "content": "<img src=x onerror=alert(2)>",
                     "is_representative": True, "keep_separate": False, "stale": False},
                ],
            }],
            "items": [],
            "pagination": {"page": 1, "total_pages": 1},
        },
    }
    case = {
        "bvid": "BV1xss",
        "toggleAutoFold": False,
        "responses": {
            "/preflight": _preflight(True),
            "/preferences": {"success": True, "data": {"auto_fold": True, "revision": 2,
                                                       "consent_version": "semantic-fold-v1",
                                                       "model_identity": {"api_base": "https://api.example.com",
                                                                          "model": "demo"}}},
            "/latest": latest_completed,
            "/result": result,
            "/local-token": {"success": True, "data": {"token": "t", "header": "X-Local-Token"}},
        },
    }
    out = _run_node([case])[0]
    html = out["resultHtml"]
    # 原始危险串不得原样出现；应被转义为实体。
    assert "<script>alert(1)" not in html
    assert "<img src=x onerror=" not in html
    assert "&lt;script&gt;" in html or "&lt;img" in html


# ---------------------------------------------------------------------------
# 真实 store DTO 契约：run revision / pref revision / task_id 整数 / 写请求头
# ---------------------------------------------------------------------------
def _completed_result_dto(run_revision: int, pref_revision: int) -> dict:
    """贴近 store.result() 真实 DTO：顶层 revision 是 run 修订，preference.revision 是偏好修订；
    task_id 为整数；groups.members 带完整字段；pagination 用 total_pages。"""
    return {
        "success": True,
        "data": {
            "task": {"task_id": 777, "bvid": "BV1dto", "status": "completed",
                     "revision": run_revision, "progress": 1.0},
            "revision": run_revision,  # run 修订（≠ 偏好修订）
            "preference": {"auto_fold": True, "revision": pref_revision,
                           "consent_version": "semantic-fold-v1",
                           "model_identity": {"api_base": "https://api.example.com", "model": "demo"}},
            "counts": {"raw_total": 37, "selected": 37, "eligible": 34,
                       "independent_users": 20, "groups": 1},
            "groups": [{
                "group_id": "g1", "representative_id": "c0", "representative_rpid": "100",
                "representative_content": "代表全文", "reason": "同义", "aspect": "a", "stance": "s",
                "original_count": 3, "member_count": 3,
                "current_folded_rpids": ["101"],
                "stale": False,
                "members": [
                    {"local_id": "c0", "rpid": "100", "is_representative": True,
                     "keep_separate": False, "stale": False, "content": "代表全文",
                     "sentiment": "positive", "uid": 1, "uname": "u1", "ctime": None, "like": 2},
                    {"local_id": "c1", "rpid": "101", "is_representative": False,
                     "keep_separate": False, "stale": False, "content": "成员二全文",
                     "sentiment": "neutral", "uid": 2, "uname": "u2", "ctime": None, "like": 0},
                    {"local_id": "c2", "rpid": "102", "is_representative": False,
                     "keep_separate": True, "stale": False, "content": "受保护成员全文",
                     "sentiment": "neutral", "uid": 3, "uname": "u3", "ctime": None, "like": 0},
                ],
            }],
            "items": [{"id": "c0", "rpid": "100", "content": "代表全文", "sentiment": "positive",
                       "context_status": "known_top", "keep_separate": False, "stale": False}],
            "pagination": {"page": 1, "page_size": 50, "total": 37, "total_pages": 1, "pages": 1},
        },
    }


def _base_responses(result_dto: dict, pref_revision: int) -> dict:
    return {
        "/preflight": _preflight(True),
        "/preferences": {"success": True, "data": {"auto_fold": True, "revision": pref_revision,
                                                   "consent_version": "semantic-fold-v1",
                                                   "model_identity": {"api_base": "https://api.example.com",
                                                                      "model": "demo"}}},
        "/latest": {"success": True, "data": {"task_id": 777, "bvid": "BV1dto",
                                              "status": "completed", "revision": result_dto["data"]["revision"]}},
        "/result": result_dto,
        "/decisions": {"success": True, "data": {"revision": pref_revision + 1, "action": "undo_group",
                                                 "affected_rpids": ["100", "101", "102"],
                                                 "preference": {"auto_fold": True, "revision": pref_revision + 1,
                                                                "consent_version": "semantic-fold-v1",
                                                                "model_identity": {"api_base": "https://api.example.com",
                                                                                   "model": "demo"}}}},
        "/local-token": {"success": True, "data": {"token": "t", "header": "X-Local-Token"}},
    }


@pytest.mark.skipif(shutil.which("node") is None, reason="本机无 node")
def test_decision_uses_pref_revision_not_run_revision_and_string_task_id() -> None:
    """真实 DTO：run revision=100 / pref revision=1。决策 POST 必须用偏好修订 1、
    task_id 序列化为字符串、Content-Type application/json、body 为 JSON 文本。"""
    result_dto = _completed_result_dto(run_revision=100, pref_revision=1)
    case = {
        "bvid": "BV1dto",
        "toggleAutoFold": False,
        "responses": _base_responses(result_dto, pref_revision=1),
        "clickDecide": {"action": "undo_group", "group_id": "g1"},
    }
    out = _run_node([case])[0]
    dec = out["decisionFetch"]
    assert dec is not None, out["fetchUrls"]
    assert dec["method"] == "POST"
    # 写请求头必须显式 application/json（否则 FastAPI 422）。
    ct = {k.lower(): v for k, v in dec["headers"].items()}.get("content-type")
    assert ct == "application/json", dec["headers"]
    body = json.loads(dec["body"])
    # 关键：用偏好修订 1，而非 run 修订 100。
    assert body["expected_revision"] == 1, body
    # task_id 必须是字符串（router StrictStr），即便 DTO 给的是整数 777。
    assert body["task_id"] == "777", body
    assert isinstance(body["task_id"], str)
    assert body["action"] == "undo_group"
    assert body["group_id"] == "g1"


@pytest.mark.skipif(shutil.which("node") is None, reason="本机无 node")
def test_result_renders_member_fulltext_and_protected_member_independent() -> None:
    """>行成员全文直接展示（非 '—'）；受保护成员标“已独立”并给「重新允许折叠」按钮。"""
    result_dto = _completed_result_dto(run_revision=100, pref_revision=1)
    case = {
        "bvid": "BV1dto", "toggleAutoFold": False,
        "responses": _base_responses(result_dto, pref_revision=1),
    }
    out = _run_node([case])[0]
    html = out["resultHtml"]
    assert "代表全文" in html
    assert "成员二全文" in html
    assert "受保护成员全文" in html
    # 受保护成员：显示“已独立”，按钮是「重新允许折叠」（不是反向的“保持独立”）。
    assert "已独立" in html
    assert "重新允许折叠" in html
    assert "重新允许本组折叠" in html
    # allow 文案不得写成会误导的“始终独立展示 / 保持独立”。
    assert "始终独立展示" not in html


@pytest.mark.skipif(shutil.which("node") is None, reason="本机无 node")
def test_result_pager_present_when_multiple_pages() -> None:
    """>50 评论：total_pages>1 时渲染分页控件（上一页/下一页）。"""
    result_dto = _completed_result_dto(run_revision=5, pref_revision=0)
    result_dto["data"]["pagination"] = {"page": 1, "page_size": 50, "total": 120,
                                         "total_pages": 3, "pages": 3}
    case = {
        "bvid": "BV1pager", "toggleAutoFold": False,
        "responses": _base_responses(result_dto, pref_revision=0),
    }
    out = _run_node([case])[0]
    html = out["resultHtml"]
    assert "下一页" in html and "上一页" in html
    assert "/ 3 页" in html


@pytest.mark.skipif(shutil.which("node") is None, reason="本机无 node")
@pytest.mark.parametrize("auto_fold", [True, False])
def test_server_folded_members_hidden_only_from_folded_flat_list(auto_fold):
    result = _completed_result_dto(100, 1)
    result["data"]["preference"]["auto_fold"] = auto_fold
    result["data"]["items"] = [
        {"rpid": "100", "content": "REP_RAW"},
        {"rpid": "101", "content": "FOLDED_RAW"},
        {"rpid": "102", "content": "PROTECTED_RAW", "keep_separate": True},
        {"rpid": "103", "content": "STALE_RAW", "stale": True},
    ]
    case = {"bvid": "BV1dto", "responses": _base_responses(result, 1)}
    out = _run_node([case])[0]
    html = out["resultHtml"]
    flat = html.split('<section class="semantic-items">', 1)[1]
    assert ("FOLDED_RAW" not in flat) if auto_fold else ("FOLDED_RAW" in flat)
    assert all(value in flat for value in ("REP_RAW", "PROTECTED_RAW", "STALE_RAW"))
    # 折叠成员仍有完整证据读取路径，不能从整份结果删除。
    assert "成员二全文" in html
    assert not any(log["method"] != "GET" for log in out["fetchLog"])


@pytest.mark.skipif(shutil.which("node") is None, reason="本机无 node")
@pytest.mark.parametrize("coverage, expected, absent", [
    # 新键（_run_cross 输出）：分子是已复核对数，分母是候选对总数
    ({"total_pairs": 19900, "pairs_reviewed": 2175, "pairs_unvisited": 17725, "partial": True},
     "跨批覆盖：2175/19900", "0/17725"),
    ({"total_pairs": 66, "pairs_reviewed": 66, "pairs_unvisited": 0, "partial": False},
     "跨批覆盖：66/66</p>", None),
    # 旧版本落库的多批任务、批内阶段预算停止：旧键也是 0/0，但跨批并未开始，显示“未开始”而不是隐藏
    ({"batches": 3, "pairs_visited": 0, "pairs_unvisited": 0, "partial": True},
     "跨批覆盖：未开始", None),
    # 批内阶段就停下：跨批未开始
    ({"total_pairs": None, "pairs_reviewed": 0, "pairs_unvisited": None, "partial": True},
     "跨批覆盖：未开始", None),
    # 旧版本落库的任务：pairs_visited / pairs_unvisited
    ({"pairs_visited": 10, "pairs_unvisited": 35, "partial": True}, "跨批覆盖：10/45", None),
    ({"pairs_visited": 0, "pairs_unvisited": -1, "partial": True}, "跨批覆盖：未开始", None),
])
def test_cross_coverage_reads_reviewed_pairs_over_total(coverage, expected, absent):
    """原实现只读 pairs_visited，后端实际输出 pairs_reviewed，界面显示成 0/未复核对数。"""
    result = _completed_result_dto(100, 1)
    responses = _base_responses(result, 1)
    responses["/latest"]["data"].update({"status": "partial", "coverage": coverage})
    out = _run_node([{"bvid": "BV1dto", "responses": responses}])[0]
    assert expected in out["taskHtml"]
    if absent:
        assert absent not in out["taskHtml"]


@pytest.mark.skipif(shutil.which("node") is None, reason="本机无 node")
@pytest.mark.parametrize("coverage", [
    {"batches": 1, "total_pairs": 0, "pairs_reviewed": 0, "pairs_unvisited": 0, "partial": False},  # 只有一批
    # 单批任务在批内阶段就撞预算：永远不会进入跨批，不显示“未开始”
    {"batches": 1, "total_pairs": 0, "pairs_reviewed": 0, "pairs_unvisited": 0, "partial": True},
    {"pairs_visited": 0, "pairs_unvisited": 0, "partial": False},  # 旧版本的单批任务
    {},  # 没有覆盖率信息
])
def test_cross_coverage_hidden_when_no_cross_pairs(coverage):
    result = _completed_result_dto(100, 1)
    responses = _base_responses(result, 1)
    responses["/latest"]["data"].update({"coverage": coverage})
    out = _run_node([{"bvid": "BV1dto", "responses": responses}])[0]
    assert "跨批覆盖" not in out["taskHtml"]


@pytest.mark.skipif(shutil.which("node") is None, reason="本机无 node")
def test_budget_uses_store_token_field_names():
    result = _completed_result_dto(100, 1)
    responses = _base_responses(result, 1)
    responses["/latest"]["data"].update({
        "progress": 0.42,
        "budget": {"settled_tokens": 1234, "reserved_tokens": 5678, "daily_settled": 9876},
    })
    out = _run_node([{"bvid": "BV1dto", "responses": responses}])[0]
    assert all(value in out["taskHtml"] for value in ("1234", "5678", "9876", "42%"))


@pytest.mark.skipif(shutil.which("node") is None, reason="本机无 node")
@pytest.mark.parametrize("late_outcome", ["success", "failure"])
def test_newer_result_request_wins_over_late_success_and_failure(late_outcome):
    result = _completed_result_dto(100, 1)
    out = _run_node([{
        "bvid": "BV1dto", "responses": _base_responses(result, 1),
        "resultRace": late_outcome,
    }])[0]
    assert "NEWEST_RESULT" in out["raceHtml"]
    assert "STALE_RESULT" not in out["raceHtml"]
    assert "STALE_FAILURE" not in out["raceHtml"]
