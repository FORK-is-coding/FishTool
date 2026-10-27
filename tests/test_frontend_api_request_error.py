"""前端 apiRequest 错误结构契约（补齐 error.detail 与 pydantic msg）。

背景：上游 ``apiRequest`` 已给抛出的 Error 附 ``status``，5xx 走 toast，对象 detail
也不再拼成 ``[object Object]``；但仍缺两处：

- 没有附 ``error.detail`` → ``app.events.js`` 的 ``eventApiErrorCode`` 永远返回空串，
  ``generation_key_conflict`` / ``discovery_in_progress`` / ``discovery_disabled_by_config``
  等按错误码处理的分支全部不命中；
- pydantic 数组 detail 只剩「请求失败（HTTP 422）」，丢掉了各项 ``msg``。

覆盖：

- 静态契约：Error 附带 ``detail`` / ``status``；数组 detail 拼接 ``msg``；
- 行为（本机有 node 时）：用 node 的 ``vm`` 加载真实 ``app.core.js`` + ``app.events.js``，
  以假 ``fetch`` 驱动 ``apiRequest`` 与事件模块按错误码分支的入口；无 node 则 skip。
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest


_JS_DIR = Path(__file__).resolve().parents[1] / "web" / "frontend" / "static" / "js"
_CORE_JS = _JS_DIR / "app.core.js"
_EVENTS_JS = _JS_DIR / "app.events.js"


def _read(path: Path) -> str:
    """读取前端脚本文本（UTF-8）。"""
    return path.read_text(encoding="utf-8")


def _api_request_source() -> str:
    """截取 ``apiRequest`` 函数体，避免断言误命中文件其他位置。"""
    js = _read(_CORE_JS)
    start = js.index("async function apiRequest(")
    end = js.index("\nfunction ", start)
    return js[start:end]


# ===========================================================================
# 静态契约（无 node 也必跑）
# ===========================================================================


def test_api_request_error_carries_detail_and_status():
    """抛出的 Error 必须同时挂上 HTTP 状态码与后端原始 detail。"""
    body = _api_request_source()
    assert "error.status = response.status" in body
    assert "error.detail = " in body
    # detail 必须在 throw 之前挂上。
    assert body.index("error.detail = ") < body.index("throw error;")


def test_api_request_joins_pydantic_array_msgs():
    """数组 detail（pydantic 校验错误）按各项 msg 拼接，不再只剩状态码文案。"""
    body = _api_request_source()
    assert "Array.isArray(detail)" in body
    assert "item.msg" in body
    # 数组分支必须排在通用对象分支之前（数组也是 object）。
    assert body.index("Array.isArray(detail)") < body.index("detail && typeof detail === 'object'")


def test_api_request_keeps_upstream_toast_and_alert_paths():
    """不破坏上游提示逻辑：5xx 优先 toast，其余走模态弹窗。"""
    body = _api_request_source()
    assert "responseStatus >= 500 && typeof showToast === 'function'" in body
    assert "showAppAlert(`错误: ${error.message}`)" in body


def test_events_error_code_reader_matches_api_request_contract():
    """事件模块按 ``error.detail.error_code`` 分支：字段名必须与 apiRequest 挂的一致。"""
    events_js = _read(_EVENTS_JS)
    assert "const detail = error && error.detail;" in events_js
    assert "eventApiErrorCode(error) === 'generation_key_conflict'" in events_js
    assert "code === 'discovery_in_progress'" in events_js
    assert "code === 'discovery_disabled_by_config'" in events_js


# ===========================================================================
# 行为测试（需要本机 node；CI 无 node 时 skip）
# ===========================================================================

_NODE = shutil.which("node")

# vm 沙箱：只桩 DOM/存储/弹窗/toast 等浏览器全局，apiRequest 与事件模块均为仓库真实源码。
_HARNESS = r"""
const fs = require('fs');
const vm = require('vm');
const [corePath, eventsPath, casesPath] = process.argv.slice(2);
const cases = JSON.parse(fs.readFileSync(casesPath, 'utf8'));

function makeContext(fetchResponse) {
    const elements = {};
    const storage = {};
    const alerts = [];
    const toasts = [];
    const ctx = {
        console: { error() {}, log() {}, warn() {} },
        alerts,
        toasts,
        elements,
        storage,
        document: {
            querySelectorAll: () => [],
            getElementById: id => elements[id] || null,
        },
        window: { requestAnimationFrame() {} },
        sessionStorage: {
            getItem: k => (k in storage ? storage[k] : null),
            setItem: (k, v) => { storage[k] = String(v); },
            removeItem: k => { delete storage[k]; },
        },
        crypto: { randomUUID: () => 'fixed-uuid' },
        showAppAlert: message => alerts.push(String(message)),
        showToast: (message, opts) => toasts.push({ message: String(message), level: opts && opts.level }),
        escapeHtml: value => String(value ?? ''),
        fetch: async () => ({
            ok: fetchResponse.status >= 200 && fetchResponse.status < 300,
            status: fetchResponse.status,
            json: async () => fetchResponse.body,
        }),
    };
    vm.createContext(ctx);
    vm.runInContext(fs.readFileSync(corePath, 'utf8'), ctx, { filename: 'app.core.js' });
    vm.runInContext(fs.readFileSync(eventsPath, 'utf8'), ctx, { filename: 'app.events.js' });
    return ctx;
}

async function runCase(c) {
    const ctx = makeContext(c.response);
    const out = { name: c.name };
    if (c.kind === 'apiRequest') {
        try {
            await ctx.apiRequest('/x', {}, c.showError);
            out.threw = false;
        } catch (error) {
            out.threw = true;
            out.message = error.message;
            out.status = error.status;
            out.detail = error.detail === undefined ? '__undefined__' : error.detail;
            out.code = ctx.eventApiErrorCode(error);
        }
    } else if (c.kind === 'generate') {
        try {
            await ctx.generateEventTopics({ direction: 'd' }, 'gen-el');
            out.threw = false;
        } catch (error) {
            out.threw = true;
            out.message = error.message;
        }
        out.storageKeys = Object.keys(ctx.storage);
    } else if (c.kind === 'discover') {
        ctx.window.__eventWorkbenchEventId = 'evt_1';
        ctx.elements['event-workbench-result'] = { innerHTML: '' };
        await ctx.discoverAndObserveEvent();
        out.html = ctx.elements['event-workbench-result'].innerHTML;
    }
    out.alerts = ctx.alerts;
    out.toasts = ctx.toasts;
    return out;
}

(async () => {
    const results = [];
    for (const c of cases) results.push(await runCase(c));
    process.stdout.write(JSON.stringify(results));
})().catch(err => { console.error(err && err.stack || err); process.exit(1); });
"""


def _run_node(tmp_path: Path, cases: list[dict]) -> dict[str, dict]:
    """在 node vm 里执行用例，返回 ``name -> 结果``。"""
    harness = tmp_path / "harness.js"
    harness.write_text(_HARNESS, encoding="utf-8")
    cases_file = tmp_path / "cases.json"
    cases_file.write_text(json.dumps(cases, ensure_ascii=False), encoding="utf-8")
    proc = subprocess.run(
        [_NODE, str(harness), str(_CORE_JS), str(_EVENTS_JS), str(cases_file)],
        capture_output=True,
        timeout=60,
    )
    stdout = proc.stdout.decode("utf-8", errors="replace")
    stderr = proc.stderr.decode("utf-8", errors="replace")
    assert proc.returncode == 0, stderr
    return {item["name"]: item for item in json.loads(stdout)}


@pytest.mark.skipif(_NODE is None, reason="本机无 node，跳过 JS 行为测试（静态契约仍覆盖）")
def test_api_request_error_behavior_under_node(tmp_path):
    """真实 apiRequest：detail 原样保留、status 正确、message 按规则取值、提示通道不变。"""
    object_detail = {"error_code": "generation_key_conflict", "message": "同键内容冲突"}
    pydantic_detail = [
        {"loc": ["body", "count"], "msg": "field required", "type": "missing"},
        {"loc": ["body", "zone_name"], "msg": "too short", "type": "string_too_short"},
    ]
    results = _run_node(tmp_path, [
        {"name": "string", "kind": "apiRequest", "showError": True,
         "response": {"status": 400, "body": {"detail": "参数错误"}}},
        {"name": "object", "kind": "apiRequest", "showError": False,
         "response": {"status": 409, "body": {"detail": object_detail}}},
        {"name": "object_no_message", "kind": "apiRequest", "showError": True,
         "response": {"status": 503, "body": {"detail": {"error_code": "generation_service_unavailable"}}}},
        {"name": "pydantic", "kind": "apiRequest", "showError": True,
         "response": {"status": 422, "body": {"detail": pydantic_detail}}},
        {"name": "empty_array", "kind": "apiRequest", "showError": False,
         "response": {"status": 422, "body": {"detail": []}}},
        {"name": "no_detail", "kind": "apiRequest", "showError": False,
         "response": {"status": 500, "body": {"message": "boom", "code": "UNKNOWN_ERROR"}}},
        {"name": "ok", "kind": "apiRequest", "showError": False,
         "response": {"status": 200, "body": {"success": True}}},
    ])

    # 字符串 detail：message 与上游一致，4xx 走模态弹窗。
    assert results["string"]["message"] == "参数错误"
    assert results["string"]["status"] == 400
    assert results["string"]["detail"] == "参数错误"
    assert results["string"]["alerts"] == ["错误: 参数错误"]
    assert results["string"]["toasts"] == []

    # 对象 detail：message 取 detail.message，detail 原样保留，error_code 可被事件模块读到。
    assert results["object"]["message"] == "同键内容冲突"
    assert results["object"]["status"] == 409
    assert results["object"]["detail"] == object_detail
    assert results["object"]["code"] == "generation_key_conflict"
    assert results["object"]["alerts"] == []  # showError=false 仍静默
    assert results["object"]["toasts"] == []

    # 对象无 message：沿用上游回退到 error_code；5xx 仍走 toast 而不是弹窗。
    assert results["object_no_message"]["message"] == "generation_service_unavailable"
    assert results["object_no_message"]["code"] == "generation_service_unavailable"
    assert results["object_no_message"]["toasts"] == [
        {"message": "generation_service_unavailable", "level": "error"}]
    assert results["object_no_message"]["alerts"] == []

    # pydantic 数组：拼接各项 msg，detail 原样保留。
    assert results["pydantic"]["message"] == "field required；too short"
    assert results["pydantic"]["status"] == 422
    assert results["pydantic"]["detail"] == pydantic_detail
    assert results["pydantic"]["code"] == ""
    assert results["pydantic"]["alerts"] == ["错误: field required；too short"]

    # 空数组取不到 msg：回退上游的状态码文案。
    assert results["empty_array"]["message"] == "请求失败（HTTP 422）"

    # 无 detail（如未处理异常的 500 兜底体）：沿用上游取 payload.message，detail 为 undefined。
    assert results["no_detail"]["message"] == "boom"
    assert results["no_detail"]["status"] == 500
    assert results["no_detail"]["detail"] == "__undefined__"

    assert results["ok"]["threw"] is False


@pytest.mark.skipif(_NODE is None, reason="本机无 node，跳过 JS 行为测试（静态契约仍覆盖）")
def test_events_error_code_branches_hit_under_node(tmp_path):
    """事件模块按 error_code 的三个分支在修复后确实命中（main 上全部落到通用失败）。"""
    results = _run_node(tmp_path, [
        {"name": "key_conflict", "kind": "generate",
         "response": {"status": 409, "body": {"detail": {
             "error_code": "generation_key_conflict", "message": "generation_key_conflict"}}}},
        {"name": "in_progress", "kind": "discover",
         "response": {"status": 409, "body": {"detail": {
             "error_code": "discovery_in_progress", "message": "发现进行中: run_1"}}}},
        {"name": "disabled", "kind": "discover",
         "response": {"status": 409, "body": {"detail": {
             "error_code": "discovery_disabled_by_config",
             "reason_code": "discovery_disabled_by_config",
             "message": "事件发现未启用（EVENT_AUTO_DISCOVERY_ENABLED=false）",
             "enabled": False}}}},
        {"name": "other", "kind": "discover",
         "response": {"status": 503, "body": {"detail": {
             "error_code": "discovery_store_unavailable", "message": "存储不可用"}}}},
    ])

    # generation_key_conflict：提示新操作，并清掉本地同键（不偷偷换键重发）。
    assert results["key_conflict"]["threw"] is True
    assert results["key_conflict"]["message"].startswith("相同请求的内容已变化")
    assert results["key_conflict"]["storageKeys"] == []

    assert "已有发现进行中" in results["in_progress"]["html"]
    assert "事件发现未启用（discovery_disabled_by_config）" in results["disabled"]["html"]

    # 其他错误码：走通用失败文案，显示人读 message。
    assert results["other"]["html"] == '<p class="event-error">发现失败：存储不可用</p>'
