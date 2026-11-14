"""前端 apiRequest 请求头契约：默认 Content-Type 不能被调用方的 headers 覆盖。

回归背景（既有 bug，不是连接池改动引入的）：原实现是

    fetch(url, {
        headers: { 'Content-Type': 'application/json', ...options.headers },
        ...options
    })

``...options`` 展开在 ``headers`` 之后，调用方只要传了 ``options.headers``，整个
headers 对象就会被替换掉，默认的 ``Content-Type`` 随之丢失。字符串 body 没有
Content-Type 时浏览器会补 ``text/plain;charset=UTF-8``，FastAPI 对 pydantic body
直接返回 422，于是「01 正确排名」的两个写接口一直失败：

- ``POST /api/analysis/benchmark/candidates``（app.up.js，body 是 CandidateDiscoveryRequest）
- ``POST /api/analysis/benchmark/tasks``（同上）

覆盖两层：

- 静态契约：默认头与调用方头是"合并"，且合并结果必须传给 fetch。
- 行为契约（本机有 node 时）：用 vm 加载真实 ``app.core.js``，假 fetch 记录实际
  init，逐组合断言最终请求头。
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest


_CORE_JS = Path(__file__).resolve().parents[1] / "web" / "frontend" / "static" / "js" / "app.core.js"

_JSON_HEADER = {"Content-Type": "application/json"}


def _api_request_source() -> str:
    """截取 ``apiRequest`` 函数体，避免断言误命中文件其它位置。"""
    js = _CORE_JS.read_text(encoding="utf-8")
    start = js.index("async function apiRequest(")
    end = js.index("\nfunction ", start)
    return js[start:end]


# ===========================================================================
# 静态契约（无 node 也必跑）
# ===========================================================================


def test_api_request_merges_default_headers_instead_of_dropping_them():
    """默认 Content-Type 与调用方 headers 必须合并后传给 fetch。"""
    body = _api_request_source()
    # 合并方向：默认值在前、调用方在后（调用方优先）。
    assert "isFormData ? {} : { 'Content-Type': 'application/json' }" in body
    assert "options.headers || {}" in body
    # 合并结果必须真的交给 fetch，而不是被 {...options} 再覆盖一次。
    assert "Object.assign({}, options, { headers: mergedHeaders })" in body
    # 旧的覆盖式写法必须消失（headers 对象里再展开 options.headers 的老形态）。
    assert "...options.headers\n" not in body


def test_api_request_skips_content_type_for_form_data():
    """FormData 的 Content-Type（含 boundary）由浏览器生成，不能预设。"""
    body = _api_request_source()
    assert "options.body instanceof FormData" in body


# ===========================================================================
# 行为契约（需要本机 node；无 node 时 skip）
# ===========================================================================

_NODE = shutil.which("node")

_HARNESS = r"""
const fs = require('fs');
const vm = require('vm');
const [corePath, casesPath] = process.argv.slice(2);
const cases = JSON.parse(fs.readFileSync(casesPath, 'utf8'));

// 沙箱里的 FormData 替身：只用于触发 apiRequest 的 FormData 分支。
class FakeFormData {}

function makeContext() {
    const calls = [];
    const ctx = {
        console: { error() {}, log() {}, warn() {} },
        document: { querySelectorAll: () => [], getElementById: () => null },
        window: { requestAnimationFrame() {} },
        sessionStorage: { getItem: () => null, setItem() {}, removeItem() {} },
        crypto: { randomUUID: () => 'fixed-uuid' },
        showAppAlert() {},
        showToast() {},
        escapeHtml: value => String(value ?? ''),
        FormData: FakeFormData,
        fetch: async (url, init) => {
            calls.push({ url, init });
            return { ok: true, status: 200, json: async () => ({ success: true }) };
        },
    };
    vm.createContext(ctx);
    vm.runInContext(fs.readFileSync(corePath, 'utf8'), ctx, { filename: 'app.core.js' });
    return { ctx, calls };
}

(async () => {
    const results = [];
    for (const c of cases) {
        const { ctx, calls } = makeContext();
        const options = { method: c.method || 'GET' };
        if (c.headers) options.headers = c.headers;
        if (c.form) options.body = new FakeFormData();
        else if (c.body !== undefined) options.body = c.body;
        await ctx.apiRequest(c.path || '/probe', options);
        const init = calls.length ? calls[0].init : {};
        results.push({
            name: c.name,
            url: calls.length ? calls[0].url : null,
            method: init.method,
            body: typeof init.body === 'string' ? init.body : (init.body ? '__object__' : '__none__'),
            headers: init.headers || {},
        });
    }
    process.stdout.write(JSON.stringify(results));
})().catch(err => { console.error(err && err.stack || err); process.exit(1); });
"""


def _run_cases(tmp_path: Path, cases: list[dict]) -> dict[str, dict]:
    """在 node vm 里执行用例，返回 ``name -> 实际请求``。"""
    harness = tmp_path / "headers_harness.js"
    harness.write_text(_HARNESS, encoding="utf-8")
    cases_file = tmp_path / "headers_cases.json"
    cases_file.write_text(json.dumps(cases, ensure_ascii=False), encoding="utf-8")
    proc = subprocess.run(
        [_NODE, str(harness), str(_CORE_JS), str(cases_file)],
        capture_output=True,
        timeout=60,
    )
    stderr = proc.stderr.decode("utf-8", errors="replace")
    assert proc.returncode == 0, stderr
    return {item["name"]: item for item in json.loads(proc.stdout.decode("utf-8"))}


@pytest.mark.skipif(_NODE is None, reason="本机无 node，跳过 JS 行为测试（静态契约仍覆盖）")
def test_api_request_header_behavior_under_node(tmp_path):
    """四种典型调用：带 token 头、无头、显式覆盖、FormData。"""
    body = '{"rid": 4}'
    results = _run_cases(tmp_path, [
        # 排行页的真实形态：只带 X-Local-Token，body 是 JSON 字符串。
        {"name": "token_only", "method": "POST", "body": body,
         "headers": {"X-Local-Token": "tok-value"}},
        {"name": "no_headers", "method": "POST", "body": body},
        {"name": "explicit_override", "method": "POST", "body": "a,b",
         "headers": {"Content-Type": "text/csv"}},
        {"name": "form_data", "method": "POST", "form": True,
         "headers": {"X-Local-Token": "tok-value"}},
        {"name": "get_with_headers", "method": "GET",
         "headers": {"X-Local-Token": "tok-value"}},
    ])

    # 关键回归：带 token 头时默认 Content-Type 必须仍在。
    assert results["token_only"]["headers"] == {
        "Content-Type": "application/json", "X-Local-Token": "tok-value"}
    assert results["token_only"]["method"] == "POST"
    assert results["token_only"]["body"] == body
    assert results["token_only"]["url"].endswith("/probe")

    # 不传 headers 时行为与改动前一致。
    assert results["no_headers"]["headers"] == _JSON_HEADER

    # 调用方显式指定 Content-Type 时以调用方为准。
    assert results["explicit_override"]["headers"] == {"Content-Type": "text/csv"}

    # FormData：不预设 Content-Type，其它头照常保留。
    form_headers = results["form_data"]["headers"]
    assert "Content-Type" not in form_headers
    assert form_headers == {"X-Local-Token": "tok-value"}

    # GET 也保持默认 JSON 头（与改动前一致）。
    assert results["get_with_headers"]["headers"] == {
        "Content-Type": "application/json", "X-Local-Token": "tok-value"}
